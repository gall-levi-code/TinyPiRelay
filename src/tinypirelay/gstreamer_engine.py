"""Programmable GStreamer adapter for the Step 2 media runtime.

Importing this module does not import PyGObject.  The real boundary is loaded
only when the adapter is first bound to its owning GLib context; pure plans are
therefore usable by ordinary unit tests and configuration tooling.
"""

from __future__ import annotations

import copy
import logging
import math
import os
import re
import threading
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .media_config import MIN_METER_UPDATES_PER_SECOND


LOGGER = logging.getLogger(__name__)

PCM_ID = "pcm_s16le_48000_stereo_matroska"
FLAC_ID = "flac_48000_stereo_matroska"
OPUS_ID = "opus_128k_48000_stereo_matroska"
APPROVED_REPRESENTATIONS = frozenset((PCM_ID, FLAC_ID, OPUS_ID))
OPUS_BITRATES_BPS = frozenset((64_000, 96_000, 128_000, 192_000))
FLAC_INPUT_FORMATS = frozenset(("S8", "S16LE", "S24LE", "S24_32LE"))

STREAM_QUEUE_NS = 10_000_000_000
RECORDING_QUEUE_NS = 5_000_000_000
MONITOR_QUEUE_BUFFERS = 4
# A full recording queue can contain five seconds of audio.  Branch-local EOS
# must be allowed to drain that bound before cleanup is forced.
DEFAULT_EOS_TIMEOUT_SECONDS = 8.0
GST_CLOCK_TIME_NONE = (1 << 64) - 1
GST_MESSAGE_DEVICE_REMOVED = (1 << 31) + 2
MAX_TELEMETRY_VALUES = 2_048

SRT_STATS_FIELDS = frozenset(
    (
        "bandwidth-mbps",
        "bytes-retransmitted-total",
        "bytes-sent-dropped-total",
        "bytes-sent-total",
        "packets-ack-received",
        "packets-nack-received",
        "packets-retransmitted",
        "packets-sent-lost",
        "packets-sent-total",
        "rtt-ms",
        "send-duration-us",
        "send-rate-mbps",
    )
)


class EngineError(RuntimeError):
    """The requested media operation could not be completed safely."""


class GStreamerUnavailable(EngineError):
    """PyGObject or the required GStreamer runtime is unavailable."""


@dataclass(frozen=True)
class ElementSpec:
    """One programmatically-created element; secrets are excluded from repr."""

    factory: str
    name: str
    properties: tuple[tuple[str, object], ...] = ()
    secret_properties: tuple[tuple[str, object], ...] = field(
        default=(), repr=False
    )

    def public_property_map(self) -> dict[str, object]:
        return dict(self.properties)


@dataclass(frozen=True)
class CapturePlan:
    caps: str
    elements: tuple[ElementSpec, ...]


@dataclass(frozen=True)
class BranchPlan:
    kind: str
    elements: tuple[ElementSpec, ...]
    eos_probe_element: str | None = None
    representation_id: str | None = None
    location: str | None = None


@dataclass(frozen=True)
class EngineEvent:
    """Small event object consumed without importing this module's internals."""

    kind: str
    scope: str
    generation: int | None
    details: Mapping[str, object] = field(default_factory=dict)


def load_gstreamer() -> tuple[Any, Any]:
    """Load the target-only GStreamer bindings lazily."""

    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import GLib, Gst
    except (ImportError, ValueError) as exc:
        raise GStreamerUnavailable(f"PyGObject/GStreamer is unavailable: {exc}") from exc
    Gst.init(None)
    return Gst, GLib


def capture_plan(config: object, source_factory: str = "alsasrc") -> CapturePlan:
    """Return the fixed native-capture backbone without touching GI."""

    if source_factory not in ("alsasrc", "audiotestsrc"):
        raise EngineError("source_factory must be alsasrc or audiotestsrc")
    capture = _attr(config, "capture")
    mode = _attr(capture, "mode")
    if mode is None:
        if source_factory == "alsasrc":
            raise EngineError("capture device and mode are required")
        format_name, rate_hz, channels = "S16LE", 48_000, 2
    else:
        format_name = _attr(mode, "format")
        rate_hz = _attr(mode, "rate_hz")
        channels = _attr(mode, "channels")
    caps = _raw_caps(format_name, rate_hz, channels)
    if source_factory == "alsasrc":
        device = _attr(capture, "device_id")
        if not isinstance(device, str) or not device:
            raise EngineError("capture device is required")
        source = ElementSpec("alsasrc", "capture_source", (("device", device),))
    else:
        source = ElementSpec(
            "audiotestsrc",
            "capture_source",
            (("is-live", True), ("wave", 0)),
        )
    return CapturePlan(
        caps,
        (
            source,
            ElementSpec("capsfilter", "native_caps", (("caps", caps),)),
            ElementSpec("tee", "capture_tee", (("allow-not-linked", True),)),
        ),
    )


def stream_branch_plan(config: object) -> BranchPlan:
    """Build only one of the three Step 1 evidence-approved representations."""

    stream = _attr(config, "stream")
    representation = _attr(stream, "representation_id")
    if representation not in APPROVED_REPRESENTATIONS:
        raise EngineError("stream representation is not evidence-approved")
    host = _attr(stream, "destination_host")
    port = _attr(stream, "destination_port")
    if not isinstance(host, str) or not host or type(port) is not int:
        raise EngineError("stream destination is incomplete")
    uri = _srt_uri(host, port)
    latency = _attr(stream, "latency_ms")
    stream_id = _attr(stream, "stream_id")
    passphrase = _attr(stream, "passphrase")
    bitrate = _attr(stream, "opus_bitrate_bps")

    elements: list[ElementSpec] = [
        ElementSpec(
            "queue",
            "stream_queue",
            (
                ("max-size-buffers", 0),
                ("max-size-bytes", 0),
                ("max-size-time", STREAM_QUEUE_NS),
                ("leaky", 0),
            ),
        ),
        ElementSpec("audioconvert", "stream_convert"),
        ElementSpec("audioresample", "stream_resample"),
        ElementSpec(
            "capsfilter",
            "stream_caps",
            (("caps", _raw_caps("S16LE", 48_000, 2)),),
        ),
    ]
    if representation == FLAC_ID:
        elements.append(ElementSpec("flacenc", "stream_flac_encoder", (("quality", 5),)))
    elif representation == OPUS_ID:
        if bitrate not in OPUS_BITRATES_BPS:
            raise EngineError("Opus bitrate is not an approved runtime preset")
        elements.extend(
            (
                ElementSpec("opusenc", "stream_opus_encoder", (("bitrate", bitrate),)),
                ElementSpec("opusparse", "stream_opus_parser"),
            )
        )
    elements.extend(
        (
            ElementSpec("matroskamux", "stream_mux", (("streamable", True),)),
            ElementSpec("valve", "stream_transport_valve", (("drop", False),)),
        )
    )

    sink_properties: list[tuple[str, object]] = [
        ("uri", uri),
        ("mode", 1),
        ("latency", latency),
        ("poll-timeout", 1000),
        ("wait-for-connection", True),
        ("auto-reconnect", False),
        # A replacement sink joins an already-PLAYING live pipeline.  Do not
        # hold its bin in PAUSED waiting for a fresh BaseSink preroll buffer.
        ("async", False),
    ]
    if stream_id is not None:
        sink_properties.append(("streamid", stream_id))
    secrets: tuple[tuple[str, object], ...] = ()
    if passphrase is not None:
        sink_properties.append(("pbkeylen", 16))
        secrets = (("passphrase", passphrase),)
    elements.append(
        ElementSpec("srtsink", "stream_srt_sink", tuple(sink_properties), secrets)
    )
    return BranchPlan(
        "stream",
        tuple(elements),
        eos_probe_element="stream_mux",
        representation_id=representation,
    )


def recording_branch_plan(config: object, location: str | os.PathLike[str]) -> BranchPlan:
    capture = _attr(config, "capture")
    mode = _attr(capture, "mode")
    if mode is None or _attr(mode, "format") not in FLAC_INPUT_FORMATS:
        raise EngineError("selected native format cannot be recorded losslessly as FLAC")
    path = str(Path(location))
    if not path.lower().endswith(".flac"):
        raise EngineError("recording location must end in .flac")
    return BranchPlan(
        "recording",
        (
            ElementSpec(
                "queue",
                "recording_queue",
                (
                    ("max-size-buffers", 0),
                    ("max-size-bytes", 0),
                    ("max-size-time", RECORDING_QUEUE_NS),
                    ("leaky", 0),
                ),
            ),
            ElementSpec("flacenc", "recording_flac_encoder", (("quality", 5),)),
            ElementSpec("valve", "recording_io_valve", (("drop", False),)),
            ElementSpec(
                "filesink",
                "recording_file_sink",
                (("location", path), ("sync", False), ("async", False)),
            ),
        ),
        eos_probe_element="recording_flac_encoder",
        location=path,
    )


def monitoring_branch_plan(config: object, kind: str) -> BranchPlan:
    if kind not in ("level", "spectrum"):
        raise EngineError("monitoring branch must be level or spectrum")
    monitoring = _attr(config, "monitoring")
    configured_updates = float(_attr(monitoring, "spectrum_updates_per_second"))
    common = (
        ElementSpec(
            "queue",
            f"{kind}_queue",
            (
                ("max-size-buffers", MONITOR_QUEUE_BUFFERS),
                ("max-size-bytes", 0),
                ("max-size-time", 0),
                ("leaky", 2),
            ),
        ),
        ElementSpec("audioconvert", f"{kind}_convert"),
    )
    if kind == "level":
        updates = max(MIN_METER_UPDATES_PER_SECOND, configured_updates)
        analyzer = ElementSpec(
            "level",
            "level_analyzer",
            (
                ("interval", max(1, int(1_000_000_000 / updates))),
                ("post-messages", True),
            ),
        )
    else:
        updates = configured_updates
        if updates <= 0:
            raise EngineError("spectrum is disabled")
        analyzer = ElementSpec(
            "spectrum",
            "spectrum_analyzer",
            (
                ("bands", _attr(monitoring, "spectrum_bands")),
                ("interval", max(1, int(1_000_000_000 / updates))),
                ("threshold", -90),
                ("message-magnitude", True),
                ("message-phase", False),
                ("multi-channel", False),
                ("post-messages", True),
            ),
        )
    return BranchPlan(
        kind,
        common
        + (
            analyzer,
            ElementSpec(
                "fakesink",
                f"{kind}_sink",
                (("sync", False), ("async", False)),
            ),
        ),
    )


@dataclass
class _Branch:
    kind: str
    generation: int
    token: int
    plan: BranchPlan
    bin: Any
    tee_pad: Any
    ghost_sink: Any
    entry_sink: Any
    elements: dict[str, Any]
    announce: bool = True
    playing: bool = False
    announced: bool = False
    connected: bool = False
    block_probe_id: int | None = None
    drain_scheduled: bool = False
    drain_lock: Any = field(default_factory=threading.Lock, repr=False)
    eos_probe_id: int | None = None
    eos_probe_pad: Any = None
    timeout_source: Any = None
    forced: bool = False
    unlinked: bool = False


class GStreamerEngine:
    """Owner-context-only Gst graph with thread-safe command marshalling."""

    def __init__(
        self,
        config: object,
        on_event: Callable[[EngineEvent], None],
        *,
        source_factory: str = "alsasrc",
        gst_loader: Callable[[], tuple[Any, Any]] | None = None,
        eos_timeout_seconds: float = DEFAULT_EOS_TIMEOUT_SECONDS,
    ) -> None:
        if not callable(on_event):
            raise TypeError("on_event must be callable")
        if source_factory not in ("alsasrc", "audiotestsrc"):
            raise ValueError("source_factory must be alsasrc or audiotestsrc")
        if eos_timeout_seconds <= 0:
            raise ValueError("eos_timeout_seconds must be positive")
        self._config = config
        self._on_event = on_event
        self._source_factory = source_factory
        self._gst_loader = gst_loader or load_gstreamer
        self._eos_timeout_seconds = float(eos_timeout_seconds)
        self._owner_thread = threading.get_ident()
        self._Gst: Any = None
        self._GLib: Any = None
        self._context: Any = None
        self._pipeline: Any = None
        self._tee: Any = None
        self._source: Any = None
        self._core_elements: tuple[Any, ...] = ()
        self._bus: Any = None
        self._bus_handler_id: int | None = None
        self._transport_guard_lock = threading.Lock()
        self._transport_guards: dict[int, tuple[Any, Any]] = {}
        self._active: dict[str, _Branch] = {}
        self._finalizing: dict[int, _Branch] = {}
        self._token = 0
        self._capture_generation = 0
        self._capture_start_pending_generation: int | None = None
        self._latest: dict[str, dict[str, object] | None] = {
            "level": None,
            "spectrum": None,
        }
        self._capture_stop_pending = False
        self._shutdown_pending = False
        self._rotations: dict[int, tuple[_Branch, _Branch]] = {}
        self._rotation_forced: set[int] = set()

    def bind_owner(self) -> None:
        """Make the control context ready without opening an audio device."""

        self._bind_owner()

    def invoke(
        self, callback: Callable[..., Any], *args: object, **kwargs: object
    ) -> Future[Any]:
        """Execute on the captured GLib owner context without creating a loop."""

        future: Future[Any] = Future()
        if threading.get_ident() == self._owner_thread:
            try:
                future.set_result(callback(*args, **kwargs))
            except BaseException as exc:
                future.set_exception(exc)
            return future
        if self._context is None:
            future.set_exception(
                EngineError("engine must be bound by an owner-context operation first")
            )
            return future

        def run(*_unused: object) -> bool:
            if future.cancelled():
                return False
            try:
                future.set_result(callback(*args, **kwargs))
            except BaseException as exc:
                future.set_exception(exc)
            return False

        source = self._GLib.idle_source_new()
        source.set_callback(run)
        source.attach(self._context)
        return future

    def update_config(self, config: object) -> None:
        """Apply the narrow Step 3 live-config subset on the owner context."""

        self._require_owner()
        if _restart_signature(config) != _restart_signature(self._config):
            raise EngineError("configuration requires a media-service restart")
        if (
            _attr(config, "recording") != _attr(self._config, "recording")
            and (
                "recording" in self._active
                or any(branch.kind == "recording" for branch in self._finalizing.values())
            )
        ):
            raise EngineError("recording must be stopped before changing its settings")
        self._config = config

    def start_capture(self) -> None:
        if threading.get_ident() != self._owner_thread:
            raise EngineError("GStreamer mutation must run on the owning context")
        if self._pipeline is not None:
            return
        self._capture_generation += 1
        attempt_generation = self._capture_generation
        try:
            self._bind_owner()
            self._start_capture_attempt(attempt_generation)
        except Exception as exc:
            if self._context is not None:
                self._capture_start_pending_generation = None
                if self._pipeline is not None:
                    self._force_all_branches()
                    self._force_pipeline_null()
                self._emit(
                    "error",
                    "capture",
                    attempt_generation,
                    code="start_failed",
                    message=_redact(
                        str(exc) or type(exc).__name__,
                        _attr(_attr(self._config, "stream"), "passphrase"),
                    ),
                )
            raise

    def _start_capture_attempt(self, attempt_generation: int) -> None:
        plan = capture_plan(self._config, self._source_factory)
        pipeline = self._Gst.Pipeline.new("tinypirelay_media")
        if pipeline is None:
            raise EngineError("could not create GStreamer pipeline")
        self._pipeline = pipeline
        elements: list[Any] = []
        for spec in plan.elements:
            element = self._make(spec)
            if spec.factory == "capsfilter":
                element.set_property("caps", self._Gst.Caps.from_string(plan.caps))
            pipeline.add(element)
            elements.append(element)
        self._link_many(elements)
        self._source, _caps, self._tee = elements
        self._core_elements = tuple(elements)
        self._install_bus_watch()
        self._capture_start_pending_generation = attempt_generation
        result = pipeline.set_state(self._Gst.State.PLAYING)
        if result == self._Gst.StateChangeReturn.FAILURE:
            self._force_pipeline_null()
            raise EngineError("capture pipeline failed to enter PLAYING")
        self._try_add_monitoring_branch("level", attempt_generation)

    def _try_add_monitoring_branch(self, kind: str, generation: int) -> None:
        try:
            self._add_branch(
                monitoring_branch_plan(self._config, kind),
                generation,
                announce=False,
            )
        except Exception as exc:
            # Monitoring is intentionally lossy and independent.  A missing or
            # failed analyzer must not tear down capture, streaming, or files.
            self._emit(
                "error",
                kind,
                generation,
                code="branch_start_failed",
                message=_redact(
                    str(exc) or type(exc).__name__,
                    _attr(_attr(self._config, "stream"), "passphrase"),
                ),
            )

    def stop_capture(self) -> None:
        self._bind_owner()
        if self._pipeline is None:
            self._emit("capture-stopped", "capture", self._capture_generation)
            if self._shutdown_pending:
                self._shutdown_pending = False
                self._emit("shutdown-complete", "capture", self._capture_generation)
            return
        self._capture_stop_pending = True
        for kind in tuple(self._active):
            self._begin_remove(kind, drain=kind in ("stream", "recording"))
        self._maybe_finish_capture_stop()

    def set_spectrum_active(self, active: bool) -> bool:
        """Attach spectrum only while a bounded web-client lease is active."""

        if type(active) is not bool:
            raise EngineError("spectrum active state must be a boolean")
        self._require_owner()
        branch = self._active.get("spectrum")
        if not active:
            self._latest["spectrum"] = None
            if branch is not None:
                # Spectrum is disposable, but it is still a live tee branch.
                # Block and unlink it on the owner context before setting it
                # to NULL so a later lease can attach a fresh branch cleanly.
                self._begin_remove("spectrum", drain=False)
            return False
        updates = float(
            _attr(_attr(self._config, "monitoring"), "spectrum_updates_per_second")
        )
        if updates <= 0 or self._pipeline is None or self._tee is None:
            return False
        if branch is None:
            self._try_add_monitoring_branch("spectrum", self._capture_generation)
        return "spectrum" in self._active

    def start_stream(self, generation: int | None = None) -> int:
        self._require_capture()
        active = self._active.get("stream")
        if active is not None:
            # A caller-removed signal or branch ERROR removes the physical
            # branch before notifying runtime, so an active branch here is a
            # valid in-flight connection attempt and repeated commands are
            # idempotent even before caller-added arrives.
            if generation is None or active.generation == generation:
                return active.generation
            self._begin_remove("stream", drain=True)
        logical = generation if type(generation) is int and generation > 0 else self._next_token()
        return self._add_branch(stream_branch_plan(self._config), logical).generation

    def stop_stream(self, generation: int | None = None) -> int:
        self._require_owner()
        return self._remove_stream(generation, drain=True)

    def abort_stream_attempt(self, generation: int | None = None) -> int:
        """Immediately remove the current caller attempt after owner timeout."""

        self._require_owner()
        branch = self._active.get("stream")
        if branch is None:
            if type(generation) is not int or generation < 1:
                raise EngineError("stream is not active")
            self._emit(
                "branch-removed",
                "stream",
                generation,
                instance_token=None,
                forced=False,
                missing=True,
                path="",
            )
            return generation
        if type(generation) is int and branch.generation != generation:
            raise EngineError("stream generation does not match the timed-out attempt")
        return self._remove_stream(generation, drain=False)

    def _remove_stream(self, generation: int | None, *, drain: bool) -> int:
        branch = self._active.get("stream")
        if branch is None:
            if type(generation) is not int or generation < 1:
                raise EngineError("stream is not active")
            self._emit(
                "branch-removed",
                "stream",
                generation,
                instance_token=None,
                forced=False,
                missing=True,
                path="",
            )
            return generation
        generation = branch.generation
        self._begin_remove("stream", drain=drain)
        return generation

    def set_bitrate(self, bitrate_bps: int) -> int:
        self._require_owner()
        if bitrate_bps not in OPUS_BITRATES_BPS:
            raise EngineError("Opus bitrate is not an approved preset")
        branch = self._active.get("stream")
        if branch is None or branch.plan.representation_id != OPUS_ID:
            raise EngineError("an Opus stream is not active")
        encoder = branch.elements.get("stream_opus_encoder")
        if encoder is None:
            raise EngineError("Opus encoder is unavailable")
        old = int(encoder.get_property("bitrate"))
        encoder.set_property("bitrate", bitrate_bps)
        readback = int(encoder.get_property("bitrate"))
        if readback != bitrate_bps:
            encoder.set_property("bitrate", old)
            raise EngineError("opusenc rejected the runtime bitrate")
        self._emit(
            "opus-bitrate-changed",
            "stream",
            branch.generation,
            old_bps=old,
            new_bps=readback,
            instance_token=branch.token,
        )
        return readback

    def start_recording(
        self, path: str | None = None, generation: int | None = None
    ) -> tuple[int, str]:
        self._require_capture()
        if "recording" in self._active:
            branch = self._active["recording"]
            return branch.generation, str(branch.plan.location)
        location = path or self._new_recording_path()
        if Path(location).exists():
            raise EngineError("refusing to overwrite an existing recording")
        logical = generation if type(generation) is int and generation > 0 else self._next_token()
        branch = self._add_branch(recording_branch_plan(self._config, location), logical)
        return branch.generation, str(branch.plan.location)

    def stop_recording(self, generation: int | None = None) -> int:
        self._require_owner()
        branch = self._active.get("recording")
        if branch is None:
            if type(generation) is not int or generation < 1:
                raise EngineError("recording is not active")
            self._emit(
                "branch-removed",
                "recording",
                generation,
                instance_token=None,
                forced=False,
                missing=True,
                path="",
            )
            return generation
        generation = branch.generation
        self._begin_remove("recording", drain=True)
        return generation

    def rotate_recording(self, generation: int | None = None) -> tuple[int, str]:
        self._require_capture()
        old = self._active.get("recording")
        if old is None:
            raise EngineError("recording is not active")
        location = self._new_recording_path()
        logical = generation if type(generation) is int and generation > 0 else old.generation
        new = self._add_branch(
            recording_branch_plan(self._config, location), logical, announce=False
        )
        self._rotations[old.token] = (old, new)
        self._begin_remove_handle(old, drain=True)
        return new.generation, str(new.plan.location)

    def poll_stats(self) -> Mapping[str, object]:
        self._require_owner()
        branch = self._active.get("stream")
        connection: dict[str, object] = {}
        if branch is not None:
            structure = branch.elements["stream_srt_sink"].get_property("stats")
            connection = _allowed_structure_fields(structure, SRT_STATS_FIELDS)
        return {
            "connection": connection,
            "queues": self._queue_diagnostics(),
            "level": copy.deepcopy(self._latest["level"]),
            "spectrum": copy.deepcopy(self._latest["spectrum"]),
        }

    def shutdown(self) -> None:
        self._bind_owner()
        self._shutdown_pending = True
        self.stop_capture()

    def _bind_owner(self) -> None:
        if threading.get_ident() != self._owner_thread:
            raise EngineError("GStreamer mutation must run on the owning context")
        if self._Gst is not None:
            return
        self._Gst, self._GLib = self._gst_loader()
        self._context = (
            self._GLib.MainContext.get_thread_default()
            or self._GLib.MainContext.default()
        )

    def _require_owner(self) -> None:
        self._bind_owner()

    def _require_capture(self) -> None:
        self._require_owner()
        if self._pipeline is None or self._tee is None:
            raise EngineError("capture is not running")

    def _next_token(self) -> int:
        self._token += 1
        return self._token

    def _make(self, spec: ElementSpec, token: int | None = None) -> Any:
        suffix = f"_{token}" if token is not None else ""
        element = self._Gst.ElementFactory.make(spec.factory, spec.name + suffix)
        if element is None:
            raise EngineError(f"required GStreamer element is unavailable: {spec.factory}")
        for name, value in spec.properties + spec.secret_properties:
            if name == "caps" and isinstance(value, str):
                value = self._Gst.Caps.from_string(value)
            element.set_property(name, value)
        return element

    def _link_many(self, elements: Sequence[Any]) -> None:
        for upstream, downstream in zip(elements, elements[1:]):
            if not upstream.link(downstream):
                raise EngineError(
                    f"could not link {upstream.get_name()} to {downstream.get_name()}"
                )

    def _add_branch(
        self, plan: BranchPlan, generation: int, *, announce: bool = True
    ) -> _Branch:
        self._require_capture()
        token = self._next_token()
        branch_bin = self._Gst.Bin.new(f"{plan.kind}_branch_{token}")
        if branch_bin is None:
            raise EngineError(f"could not create {plan.kind} branch")
        elements: dict[str, Any] = {}
        ordered: list[Any] = []
        transport_guard_registered = False
        try:
            for spec in plan.elements:
                element = self._make(spec, token)
                branch_bin.add(element)
                elements[spec.name] = element
                ordered.append(element)
            self._link_many(ordered)
            sink_pad = ordered[0].get_static_pad("sink")
            if sink_pad is None:
                raise EngineError(f"{plan.kind} branch has no sink pad")
            ghost = self._Gst.GhostPad.new("sink", sink_pad)
            if ghost is None or not branch_bin.add_pad(ghost):
                raise EngineError(f"could not create {plan.kind} ghost pad")
            ordered[0].connect(
                "overrun", self._on_queue_overrun, plan.kind, token, generation
            )
            if plan.kind == "stream":
                sink = elements["stream_srt_sink"]
                sink.connect("caller-added", self._on_caller_added, token, generation)
                sink.connect(
                    "caller-removed", self._on_caller_removed, token, generation
                )
            if not self._pipeline.add(branch_bin):
                raise EngineError(f"could not add {plan.kind} branch")
            tee_pad = self._tee.request_pad_simple("src_%u")
            if tee_pad is None:
                raise EngineError("could not request a tee source pad")
            start_probe = tee_pad.add_probe(
                self._Gst.PadProbeType.BLOCK_DOWNSTREAM,
                lambda _pad, _info: self._Gst.PadProbeReturn.OK,
            )
            if tee_pad.link(ghost) != self._Gst.PadLinkReturn.OK:
                self._tee.release_request_pad(tee_pad)
                raise EngineError(f"could not link {plan.kind} branch to capture tee")
            if not branch_bin.sync_state_with_parent():
                tee_pad.unlink(ghost)
                self._tee.release_request_pad(tee_pad)
                raise EngineError(f"could not synchronize {plan.kind} branch state")
            if plan.kind in ("stream", "recording"):
                self._register_transport_guard(token, elements, plan.kind)
                transport_guard_registered = True
            tee_pad.remove_probe(start_probe)
        except BaseException:
            branch_bin.set_state(self._Gst.State.NULL)
            if branch_bin.get_parent() is not None:
                self._pipeline.remove(branch_bin)
            if transport_guard_registered:
                self._unregister_transport_guard(token)
            raise

        branch = _Branch(
            plan.kind,
            generation,
            token,
            plan,
            branch_bin,
            tee_pad,
            ghost,
            sink_pad,
            elements,
            announce=announce,
        )
        self._active[plan.kind] = branch
        if plan.kind in ("stream", "recording"):
            created: dict[str, object] = {"instance_token": token}
            if plan.location is not None:
                created["path"] = plan.location
            if plan.representation_id is not None:
                created["representation_id"] = plan.representation_id
            self._emit("branch-created", plan.kind, generation, **created)
        return branch

    def _begin_remove(self, kind: str, *, drain: bool) -> int | None:
        branch = self._active.pop(kind, None)
        if branch is None:
            return None
        self._cancel_rotation_for_branch(branch)
        self._begin_remove_handle(branch, drain=drain)
        return branch.generation

    def _begin_remove_handle(self, branch: _Branch, *, drain: bool) -> None:
        if branch.token in self._finalizing:
            return
        self._finalizing[branch.token] = branch
        branch.timeout_source = self._new_timeout(
            self._eos_timeout_seconds,
            lambda: self._finish_remove(branch.token, forced=True),
        )

        def blocked(_pad: Any, _info: Any) -> Any:
            with branch.drain_lock:
                if branch.drain_scheduled:
                    return self._Gst.PadProbeReturn.REMOVE
                branch.drain_scheduled = True
                # Returning REMOVE self-removes the probe.  Clear a stored ID
                # as well when the callback runs after add_probe() returns.
                branch.block_probe_id = None
            self._dispatch_owner_async(self._unlink_and_drain, branch.token, drain)
            return self._Gst.PadProbeReturn.REMOVE

        probe_id = branch.tee_pad.add_probe(
            self._Gst.PadProbeType.IDLE, blocked
        )
        # IDLE probes may invoke synchronously inside add_probe().  Do not save
        # the returned ID when that callback already removed the probe.
        with branch.drain_lock:
            if not branch.drain_scheduled:
                branch.block_probe_id = probe_id

    def _unlink_and_drain(self, token: int, drain: bool) -> None:
        branch = self._finalizing.get(token)
        if branch is None:
            return
        self._unlink_branch(branch)
        if not drain or branch.plan.eos_probe_element is None:
            self._finish_remove(token, forced=branch.kind == "recording")
            return
        terminal = branch.elements[branch.plan.eos_probe_element]
        terminal_pad = terminal.get_static_pad("src")
        if terminal_pad is None:
            self._finish_remove(token, forced=True)
            return

        def eos_seen(_pad: Any, info: Any) -> Any:
            event = info.get_event()
            if event is not None and event.type == self._Gst.EventType.EOS:
                self._dispatch_owner_async(self._finish_remove, token, False)
                return self._Gst.PadProbeReturn.DROP
            return self._Gst.PadProbeReturn.OK

        branch.eos_probe_id = terminal_pad.add_probe(
            self._Gst.PadProbeType.EVENT_DOWNSTREAM, eos_seen
        )
        branch.eos_probe_pad = terminal_pad
        # Inject the downstream EOS into the real queue sink pad.  Calling
        # send_event() on the now-unlinked GhostPad can report success without
        # forwarding the serialized event to its target on PyGObject/GStreamer.
        if not branch.entry_sink.send_event(self._Gst.Event.new_eos()):
            self._finish_remove(token, forced=True)

    def _finish_remove(self, token: int, forced: bool = False) -> bool:
        branch = self._finalizing.pop(token, None)
        if branch is None:
            return False
        branch.forced = forced
        if forced and token in self._rotations:
            self._rotation_forced.add(token)
        if branch.timeout_source is not None:
            branch.timeout_source.destroy()
            branch.timeout_source = None
        if branch.eos_probe_id is not None:
            pad = branch.eos_probe_pad
            if pad is not None:
                try:
                    pad.remove_probe(branch.eos_probe_id)
                except Exception:
                    pass
            branch.eos_probe_id = None
            branch.eos_probe_pad = None
        if forced and not branch.unlinked:
            # Setting the failed branch to NULL releases a queue push that may be
            # blocking the tee; unlink follows immediately on this owner context.
            branch.bin.set_state(self._Gst.State.NULL)
        self._unlink_branch(branch)
        branch.bin.set_state(self._Gst.State.NULL)
        if branch.kind == "recording":
            # filesink may report its buffered flush/close error while entering
            # NULL. The synchronous guard remains registered through that close.
            forced = forced or bool(branch.elements["recording_io_valve"].get_property("drop"))
            branch.forced = forced
        if branch.bin.get_parent() is not None:
            self._pipeline.remove(branch.bin)
        self._unregister_transport_guard(branch.token)
        if forced and branch.kind == "recording":
            self._emit(
                "error",
                "recording",
                branch.generation,
                code="finalization_failed",
                message="recording branch could not be finalized safely",
                instance_token=branch.token,
            )
        self._emit(
            "branch-removed",
            branch.kind,
            branch.generation,
            instance_token=token,
            forced=forced,
            path=branch.plan.location or "",
        )
        self._complete_rotation_if_ready(token, forced=forced)
        self._maybe_finish_capture_stop()
        return False

    def _maybe_finish_capture_stop(self) -> None:
        if not self._capture_stop_pending or self._active or self._finalizing:
            return
        generation = self._capture_generation
        self._force_pipeline_null()
        self._rotations.clear()
        self._rotation_forced.clear()
        self._capture_stop_pending = False
        self._emit("capture-stopped", "capture", generation)
        if self._shutdown_pending:
            self._shutdown_pending = False
            self._emit("shutdown-complete", "capture", generation)

    def _force_pipeline_null(self) -> None:
        if self._pipeline is None:
            return
        if self._bus is not None:
            try:
                self._bus.set_sync_handler(None)
            except Exception:
                pass
        self._pipeline.set_state(self._Gst.State.NULL)
        self._clear_transport_guards()
        if self._bus is not None and self._bus_handler_id is not None:
            try:
                self._bus.disconnect(self._bus_handler_id)
                self._bus.remove_signal_watch()
            except Exception:
                pass
        self._pipeline = None
        self._tee = None
        self._source = None
        self._core_elements = ()
        self._bus = None
        self._bus_handler_id = None
        self._capture_start_pending_generation = None

    def _install_bus_watch(self) -> None:
        self._bus = self._pipeline.get_bus()
        self._bus.set_sync_handler(self._on_bus_sync_message)
        self._bus.add_signal_watch()
        self._bus_handler_id = self._bus.connect("message", self._on_bus_message)

    def _on_bus_sync_message(
        self, _bus: Any, message: Any, *_unused: object
    ) -> Any:
        """Isolate an output-sink error before it propagates upstream."""

        reply = self._Gst.BusSyncReply.PASS
        try:
            if message.type != self._Gst.MessageType.ERROR:
                return reply
            valve = self._transport_valve_for_source(message.src)
            if valve is None:
                return reply
            # This callback runs in the posting streaming thread.  Only close
            # the existing valve here; the async bus handler owns all event
            # delivery and branch teardown on the GLib owner context.
            valve.set_property("drop", True)
        except Exception as exc:
            LOGGER.debug("could not isolate output-sink flow error: %s", exc)
        return reply

    def _on_bus_message(self, _bus: Any, message: Any) -> None:
        try:
            self._handle_bus_message(message)
        except Exception as exc:
            # PyGObject on 32-bit targets can leave a conversion exception set
            # while dispatching uncommon extended message values.  Never let a
            # nonessential bus message poison GLib's signal callback.
            LOGGER.debug("dropping unmarshalable GStreamer bus message: %s", exc)

    def _handle_bus_message(self, message: Any) -> None:
        if message.type == self._Gst.MessageType.ELEMENT:
            try:
                structure = message.get_structure()
                if structure is not None:
                    name = structure.get_name()
                    if name in ("level", "spectrum"):
                        branch = self._branch_for_object(message.src)
                        if branch is None or self._active.get(name) is not branch:
                            return
                        normalized = _normalize_telemetry(name, structure)
                        if normalized is not None:
                            self._latest[name] = normalized
                            self._emit("telemetry", name, None, **normalized)
            except Exception as exc:
                # Telemetry is advisory.  A GI marshalling error must never
                # escape the bus callback and destabilize capture/streaming.
                LOGGER.debug("dropping malformed GStreamer telemetry: %s", exc)
            return
        if message.type == self._Gst.MessageType.STATE_CHANGED and message.src == self._pipeline:
            _old, current, _pending = message.parse_state_changed()
            generation = self._capture_start_pending_generation
            if current == self._Gst.State.PLAYING and generation is not None:
                self._capture_start_pending_generation = None
                self._emit("capture-started", "capture", generation)
            return
        if message.type == self._Gst.MessageType.STATE_CHANGED:
            branch = self._branch_for_object(message.src)
            if branch is not None and message.src == branch.bin:
                _old, current, _pending = message.parse_state_changed()
                if current == self._Gst.State.PLAYING:
                    branch.playing = True
                    self._announce_branch_playing(branch)
                    for old_token, (_old_branch, new_branch) in tuple(self._rotations.items()):
                        if new_branch is branch:
                            self._complete_rotation_if_ready(old_token)
            return
        if message.type == self._Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            scope, branch = self._scope_for_source(message.src)
            generation = branch.generation if branch is not None else self._capture_generation
            details = {
                "message": _redact(str(error), _attr(_attr(self._config, "stream"), "passphrase")),
                "debug": _redact(debug or "", _attr(_attr(self._config, "stream"), "passphrase")),
                "domain": str(getattr(error, "domain", "")),
                "code": int(getattr(error, "code", 0)),
            }
            if branch is not None:
                details["instance_token"] = branch.token
            if scope == "capture":
                self._fail_capture(generation, **details)
                return
            if (
                scope == "stream"
                and branch is not None
                and message.src == branch.elements.get("stream_srt_sink")
            ):
                # Queue the loss before physical removal.  Runtime marks this
                # instance retiring on the loss event, then consumes that mark
                # when branch-removed arrives.
                self._emit("connection", "stream", generation, state="disconnected", **details)
                self._detach_branch(branch, drain=False)
            else:
                if branch is not None:
                    self._detach_branch(branch, drain=False)
                self._emit("error", scope, generation, **details)
            return
        if message.type == self._Gst.MessageType.EOS and message.src == self._pipeline:
            self._fail_capture(
                self._capture_generation,
                code="unexpected_eos",
                message="capture pipeline ended unexpectedly",
            )
            return
        # Gst.MessageType.DEVICE_REMOVED is 2**31 + 2.  Merely retrieving that
        # GI enum member raises OverflowError on 32-bit PyGObject because it is
        # converted through C long, so compare its unsigned wire value instead.
        if (int(message.type) & 0xFFFF_FFFF) == GST_MESSAGE_DEVICE_REMOVED:
            self._fail_capture(
                self._capture_generation,
                code="device_lost",
                message="capture device was removed",
            )

    def _fail_capture(self, generation: int, **details: object) -> None:
        completing_stop = self._capture_stop_pending or self._shutdown_pending
        self._capture_start_pending_generation = None
        self._force_all_branches()
        self._force_pipeline_null()
        self._capture_stop_pending = False
        self._emit("error", "capture", generation, **details)
        if completing_stop:
            self._emit("capture-stopped", "capture", generation)
            if self._shutdown_pending:
                self._shutdown_pending = False
                self._emit("shutdown-complete", "capture", generation)

    def _scope_for_source(self, source: Any) -> tuple[str, _Branch | None]:
        branch = self._branch_for_object(source)
        if branch is not None:
            return branch.kind, branch
        current = source
        while current is not None:
            if current == self._pipeline or any(
                current == element for element in self._core_elements
            ):
                return "capture", None
            current = current.get_parent() if hasattr(current, "get_parent") else None
        return "unknown", None

    def _branch_for_object(self, source: Any) -> _Branch | None:
        for branch in tuple(self._active.values()) + tuple(self._finalizing.values()):
            current = source
            while current is not None:
                if current == branch.bin:
                    return branch
                current = current.get_parent() if hasattr(current, "get_parent") else None
        return None

    def _on_caller_added(
        self, _sink: Any, _unused: int, address: Any, token: int, generation: int
    ) -> None:
        self._dispatch_owner_async(
            self._connection_changed, token, generation, True, address
        )

    def _on_caller_removed(
        self, _sink: Any, _unused: int, address: Any, token: int, generation: int
    ) -> None:
        self._dispatch_owner_async(
            self._connection_changed, token, generation, False, address
        )

    def _connection_changed(
        self, token: int, generation: int, connected: bool, address: Any
    ) -> None:
        branch = self._active.get("stream")
        if branch is None or branch.token != token:
            return
        branch.connected = connected
        self._emit(
            "connection",
            "stream",
            generation,
            state="connected" if connected else "disconnected",
            peer=str(address) if address is not None else "",
            instance_token=token,
        )
        if not connected:
            self._begin_remove("stream", drain=False)

    def _on_queue_overrun(
        self, _queue: Any, scope: str, token: int, generation: int
    ) -> None:
        if scope in ("level", "spectrum"):
            return
        # A non-leaky queue emits this signal from its streaming thread just
        # before it blocks.  Make only this failed queue downstream-leaky so
        # the owner-context teardown cannot deadlock behind that blocked push.
        _queue.set_property("leaky", 2)
        self._dispatch_owner_async(
            self._handle_queue_overrun, scope, token, generation
        )

    def _handle_queue_overrun(
        self, scope: str, token: int, generation: int
    ) -> None:
        branch = self._active.get(scope)
        if branch is None or branch.token != token:
            return
        details = {
            "code": "queue_overrun",
            "message": f"{scope} branch queue reached its bound",
            "instance_token": token,
        }
        if scope == "stream":
            # Runtime marks this token retiring on connection loss.  Queue the
            # loss before branch-removed so the later acknowledgement clears
            # that retiring token instead of leaving stale retry bookkeeping.
            self._emit(
                "connection", "stream", generation, state="disconnected", **details
            )
        self._force_remove_now(branch, forced=scope == "recording")
        if scope == "recording":
            self._emit("error", scope, generation, **details)

    def _detach_branch(self, branch: _Branch, *, drain: bool) -> None:
        rotation = self._rotations.get(branch.token)
        if rotation is not None and rotation[0] is branch:
            # An error from the retiring fragment is a failed rotation, not a
            # replacement failure.  Preserve the transaction so completion is
            # reported against the replacement token that runtime now owns.
            self._rotation_forced.add(branch.token)
            if branch.token in self._finalizing:
                self._finish_remove(branch.token, forced=True)
                return
        else:
            self._cancel_rotation_for_branch(branch)
        if self._active.get(branch.kind) is branch:
            self._active.pop(branch.kind, None)
        self._begin_remove_handle(branch, drain=drain)

    def _force_remove_now(self, branch: _Branch, *, forced: bool) -> None:
        if self._active.get(branch.kind) is branch:
            self._active.pop(branch.kind, None)
        self._cancel_rotation_for_branch(branch)
        branch.bin.set_state(self._Gst.State.NULL)
        self._unlink_branch(branch)
        if branch.bin.get_parent() is not None:
            self._pipeline.remove(branch.bin)
        self._unregister_transport_guard(branch.token)
        self._emit(
            "branch-removed",
            branch.kind,
            branch.generation,
            instance_token=branch.token,
            forced=forced,
            path=branch.plan.location or "",
        )
        self._maybe_finish_capture_stop()

    def _unlink_branch(self, branch: _Branch) -> None:
        if branch.unlinked:
            return
        try:
            branch.tee_pad.unlink(branch.ghost_sink)
        except Exception as exc:
            LOGGER.debug("branch unlink failed: %s", exc)
        try:
            if self._tee is not None:
                self._tee.release_request_pad(branch.tee_pad)
        except Exception as exc:
            LOGGER.debug("tee request-pad release failed: %s", exc)
        if branch.block_probe_id is not None:
            try:
                branch.tee_pad.remove_probe(branch.block_probe_id)
            except Exception:
                pass
            branch.block_probe_id = None
        branch.unlinked = True

    def _announce_branch_playing(self, branch: _Branch) -> None:
        if branch.announced or not branch.announce:
            return
        branch.announced = True
        details: dict[str, object] = {"instance_token": branch.token}
        if branch.plan.location is not None:
            details["path"] = branch.plan.location
        if branch.plan.representation_id is not None:
            details["representation_id"] = branch.plan.representation_id
        self._emit("branch-added", branch.kind, branch.generation, **details)

    def _complete_rotation_if_ready(
        self, old_token: int, *, forced: bool = False
    ) -> None:
        rotation = self._rotations.get(old_token)
        if rotation is None or old_token in self._finalizing:
            return
        old, new = rotation
        if (
            not new.playing
            or self._active.get("recording") is not new
            or new.token in self._finalizing
        ):
            return
        self._rotations.pop(old_token, None)
        was_forced = forced or old_token in self._rotation_forced
        self._rotation_forced.discard(old_token)
        self._emit(
            "recording-rotated",
            "recording",
            new.generation,
            old_path=str(old.plan.location),
            new_path=str(new.plan.location),
            old_instance_token=old.token,
            new_instance_token=new.token,
            forced=was_forced,
        )

    def _cancel_rotation_for_branch(self, branch: _Branch) -> None:
        for old_token, (old, new) in tuple(self._rotations.items()):
            if branch is old or branch is new:
                self._rotations.pop(old_token, None)
                self._rotation_forced.discard(old_token)

    def _force_all_branches(self) -> None:
        branches = tuple(self._active.values()) + tuple(self._finalizing.values())
        self._active.clear()
        self._finalizing.clear()
        for branch in branches:
            if branch.timeout_source is not None:
                branch.timeout_source.destroy()
            self._unlink_branch(branch)
            branch.bin.set_state(self._Gst.State.NULL)
            if branch.bin.get_parent() is not None:
                self._pipeline.remove(branch.bin)
            self._unregister_transport_guard(branch.token)
            self._emit(
                "branch-removed",
                branch.kind,
                branch.generation,
                instance_token=branch.token,
                forced=True,
                path=branch.plan.location or "",
            )
        self._rotations.clear()
        self._rotation_forced.clear()

    def _register_transport_guard(
        self, token: int, elements: Mapping[str, Any], kind: str = "stream"
    ) -> None:
        sink = elements.get("recording_file_sink" if kind == "recording" else "stream_srt_sink")
        valve = elements.get("recording_io_valve" if kind == "recording" else "stream_transport_valve")
        if sink is None or valve is None:
            raise EngineError("output-sink isolation elements are unavailable")
        with self._transport_guard_lock:
            self._transport_guards[token] = (sink, valve)

    def _unregister_transport_guard(self, token: int) -> None:
        with self._transport_guard_lock:
            self._transport_guards.pop(token, None)

    def _clear_transport_guards(self) -> None:
        with self._transport_guard_lock:
            self._transport_guards.clear()

    def _transport_valve_for_source(self, source: Any) -> Any | None:
        with self._transport_guard_lock:
            guards = tuple(self._transport_guards.values())
        for sink, valve in guards:
            if source == sink:
                return valve
        return None

    def _queue_diagnostics(self) -> dict[str, object]:
        result: dict[str, object] = {}
        for kind, branch in self._active.items():
            queue = branch.elements.get(f"{kind}_queue")
            if queue is None:
                continue
            result[kind] = {
                "current_buffers": int(queue.get_property("current-level-buffers")),
                "current_bytes": int(queue.get_property("current-level-bytes")),
                "current_time_ns": int(queue.get_property("current-level-time")),
                "max_buffers": int(queue.get_property("max-size-buffers")),
                "max_bytes": int(queue.get_property("max-size-bytes")),
                "max_time_ns": int(queue.get_property("max-size-time")),
                "leaky": int(queue.get_property("leaky")),
                "instance_token": branch.token,
            }
        return result

    def _emit(
        self,
        kind: str,
        scope: str,
        generation: int | None,
        **details: object,
    ) -> None:
        event = EngineEvent(kind, scope, generation, details)
        # Even owner-thread bus callbacks defer delivery.  This keeps runtime
        # effects from re-entering graph mutation half-way through a handler.
        self._dispatch_owner_async(self._deliver_event, event)

    def _deliver_event(self, event: EngineEvent) -> None:
        try:
            self._on_event(event)
        except Exception:
            LOGGER.exception("media event callback failed")

    def _dispatch_owner(self, callback: Callable[..., Any], *args: object) -> None:
        if threading.get_ident() == self._owner_thread:
            callback(*args)
            return

        def run(*_unused: object) -> bool:
            callback(*args)
            return False

        source = self._GLib.idle_source_new()
        source.set_callback(run)
        source.attach(self._context)

    def _dispatch_owner_async(
        self, callback: Callable[..., Any], *args: object
    ) -> None:
        """Always defer, including callbacks invoked synchronously by add_probe."""

        def run(*_unused: object) -> bool:
            callback(*args)
            return False

        source = self._GLib.idle_source_new()
        source.set_callback(run)
        source.attach(self._context)

    def _new_timeout(self, seconds: float, callback: Callable[[], bool]) -> Any:
        def run(*_unused: object) -> bool:
            return callback()

        source = self._GLib.timeout_source_new(max(1, int(seconds * 1000)))
        source.set_callback(run)
        source.attach(self._context)
        return source

    def _new_recording_path(self) -> str:
        directory = Path(_attr(_attr(self._config, "recording"), "directory"))
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        path = directory / f"tinypirelay-{stamp}.flac"
        suffix = 1
        while path.exists():
            path = directory / f"tinypirelay-{stamp}-{suffix}.flac"
            suffix += 1
        return str(path)


def _attr(value: object, name: str) -> Any:
    if isinstance(value, Mapping):
        if name not in value:
            raise EngineError(f"configuration is missing {name}")
        return value[name]
    try:
        return getattr(value, name)
    except AttributeError as exc:
        raise EngineError(f"configuration is missing {name}") from exc


def _restart_signature(config: object) -> tuple[object, ...]:
    stream = _attr(config, "stream")
    return (
        _attr(config, "schema_version"),
        _attr(config, "capture"),
        _attr(stream, "enabled"),
        _attr(stream, "representation_id"),
        _attr(stream, "destination_host"),
        _attr(stream, "destination_port"),
        _attr(stream, "latency_ms"),
        _attr(stream, "stream_id"),
        _attr(stream, "passphrase"),
        _attr(config, "monitoring"),
    )


def _raw_caps(format_name: object, rate_hz: object, channels: object) -> str:
    if not isinstance(format_name, str) or not re.fullmatch(r"[A-Z0-9_]+", format_name):
        raise EngineError("capture format is invalid")
    if type(rate_hz) is not int or rate_hz <= 0 or type(channels) is not int or channels <= 0:
        raise EngineError("capture rate/channels are invalid")
    return (
        "audio/x-raw,"
        f"format={format_name},layout=interleaved,rate={rate_hz},channels={channels}"
    )


def _srt_uri(host: str, port: int) -> str:
    if not 1 <= port <= 65_535 or re.search(r"[\s/?#@]", host):
        raise EngineError("SRT destination is invalid")
    if ":" in host and not (host.startswith("[") and host.endswith("]")):
        host = f"[{host}]"
    return f"srt://{host}:{port}"


def _redact(value: str, secret: object) -> str:
    if isinstance(secret, str) and secret:
        value = value.replace(secret, "<redacted>")
    return re.sub(
        r"(?i)(passphrase(?:=|%3d))[^&\s]+", r"\1<redacted>", value
    )[:1024]


def _allowed_structure_fields(
    structure: object, allowed: frozenset[str]
) -> dict[str, object]:
    if structure is None or not hasattr(structure, "n_fields"):
        return {}
    try:
        field_count = structure.n_fields()
    except Exception:
        return {}
    if type(field_count) is not int or not 0 <= field_count <= 256:
        return {}
    result: dict[str, object] = {}
    for index in range(field_count):
        try:
            name = structure.nth_field_name(index)
        except Exception:
            continue
        if name not in allowed:
            continue
        value = _structure_number(structure, name)
        if value is not None:
            result[name] = value
    return result


def _structure_number(structure: object, name: str) -> int | float | None:
    """Read numeric GstStructure fields without generic GValue conversion."""

    field_type_getter = getattr(structure, "get_field_type", None)
    if callable(field_type_getter):
        try:
            field_type = field_type_getter(name)
        except Exception:
            return None
        type_name = getattr(field_type, "name", None)
        if not isinstance(type_name, str):
            rendered = str(field_type).lower()
            type_name = next(
                (
                    candidate
                    for candidate in (
                        "guint64",
                        "gint64",
                        "gdouble",
                        "guint",
                        "gint",
                    )
                    if re.search(rf"\b{candidate}\b", rendered)
                ),
                "",
            )
        accessor = {
            "guint64": "get_uint64",
            "gint64": "get_int64",
            "gdouble": "get_double",
            "guint": "get_uint",
            "gint": "get_int",
        }.get(type_name.lower())
        method = getattr(structure, accessor, None) if accessor is not None else None
        if not callable(method):
            return None
        try:
            result = method(name)
        except Exception:
            return None
        if not isinstance(result, tuple) or len(result) != 2 or result[0] is not True:
            return None
        value = result[1]
        if type(value) is int and value >= 0:
            return value
        if type(value) is float and math.isfinite(value) and value >= 0.0:
            return value
        return None
    # Pure-Python fixtures and older test doubles do not expose typed accessors.
    try:
        value = structure.get_value(name)
    except Exception:
        return None
    if type(value) is int and value >= 0:
        return value
    if type(value) is float and math.isfinite(value) and value >= 0.0:
        return value
    return None


def _value_array_items(value: object) -> list[object] | None:
    count = getattr(value, "n_values", None)
    get_nth = getattr(value, "get_nth", None)
    if type(count) is int and callable(get_nth):
        if not 0 <= count <= MAX_TELEMETRY_VALUES:
            return None
        try:
            return [get_nth(index) for index in range(count)]
        except Exception:
            return None
    if isinstance(value, (str, bytes)):
        return None
    try:
        items = list(value)
    except Exception:
        return None
    return items if len(items) <= MAX_TELEMETRY_VALUES else None


def _numeric_value(value: object) -> float | None:
    if type(value) in (int, float):
        try:
            number = float(value)
        except (OverflowError, ValueError):
            return None
        return number if not math.isnan(number) and number != math.inf else None
    unbox = getattr(value, "get_value", None)
    if callable(unbox):
        try:
            unboxed = unbox()
        except Exception:
            return None
        if type(unboxed) in (int, float):
            try:
                number = float(unboxed)
            except (OverflowError, ValueError):
                return None
            return number if not math.isnan(number) and number != math.inf else None
    gtype = getattr(value, "g_type", None)
    type_name = str(getattr(gtype, "name", gtype)).lower()
    for marker, accessor in (
        ("double", "get_double"),
        ("float", "get_float"),
        ("uint64", "get_uint64"),
        ("int64", "get_int64"),
        ("uint", "get_uint"),
        ("int", "get_int"),
    ):
        if marker not in type_name:
            continue
        getter = getattr(value, accessor, None)
        if not callable(getter):
            return None
        try:
            typed_value = getter()
        except Exception:
            return None
        if type(typed_value) not in (int, float):
            return None
        try:
            number = float(typed_value)
        except (OverflowError, ValueError):
            return None
        return number if not math.isnan(number) and number != math.inf else None
    return None


def _sequence(value: object) -> list[float] | None:
    items = _value_array_items(value)
    if items is None:
        return None
    result: list[float] = []
    for item in items:
        number = _numeric_value(item)
        if number is None:
            return None
        result.append(number)
    return result


def _structure_clock_time(structure: object, name: str) -> int | None:
    getter = getattr(structure, "get_clock_time", None)
    if callable(getter):
        try:
            result = getter(name)
        except Exception:
            return None
        if not isinstance(result, tuple) or len(result) != 2 or result[0] is not True:
            return None
        value = result[1]
    else:
        try:
            value = structure.get_value(name)
        except Exception:
            return None
    if type(value) is not int or not 0 <= value < GST_CLOCK_TIME_NONE:
        return None
    return value


def _structure_list(structure: object, name: str) -> object | None:
    """Use Gst.Structure.get_list, made specifically for language bindings."""

    getter = getattr(structure, "get_list", None)
    if callable(getter):
        try:
            result = getter(name)
        except Exception:
            return None
        if isinstance(result, tuple) and len(result) == 2 and result[0] is True:
            return result[1]
        return None
    try:
        return structure.get_value(name)
    except Exception:
        return None


def _structure_value_array(structure: object, name: str) -> object | None:
    # level currently uses the legacy, introspectable GValueArray type rather
    # than GST_TYPE_ARRAY.  Generic extraction is safe for that known type.
    try:
        return structure.get_value(name)
    except Exception:
        return None


def _normalize_telemetry(kind: str, structure: object) -> dict[str, object] | None:
    def clock(name: str) -> int | None:
        return _structure_clock_time(structure, name)

    base: dict[str, object] = {
        "timestamp_ns": clock("timestamp"),
        "running_time_ns": clock("running-time"),
        "duration_ns": clock("duration"),
    }
    if kind == "level":
        rms = _sequence(_structure_value_array(structure, "rms"))
        peak = _sequence(_structure_value_array(structure, "peak"))
        decay = _sequence(_structure_value_array(structure, "decay"))
        if not rms or not peak or not decay or not (
            len(rms) == len(peak) == len(decay)
        ):
            return None
        base.update(
            {
                "rms_db": rms,
                "peak_db": peak,
                "decay_db": decay,
                "clipped": [value >= 0.0 for value in peak],
            }
        )
        return base
    if kind != "spectrum":
        return None
    magnitude = _sequence(_structure_list(structure, "magnitude"))
    if not magnitude:
        return None
    base["magnitude_db"] = magnitude
    return base
