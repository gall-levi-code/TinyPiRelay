#!/usr/bin/env python3
"""Retained Step 2 real-engine integration harness.

The sender is the production GStreamerEngine and MediaRuntime.  The local SRT
listener is deliberately disposable evidence tooling and is the only pipeline
constructed with Gst.parse_launch.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import sys
import tempfile
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tinypirelay.gstreamer_engine import (  # noqa: E402
    APPROVED_REPRESENTATIONS,
    MONITOR_QUEUE_BUFFERS,
    OPUS_ID,
    RECORDING_QUEUE_NS,
    SRT_STATS_FIELDS,
    STREAM_QUEUE_NS,
    EngineEvent,
    GStreamerEngine,
    load_gstreamer,
)
from tinypirelay.media_config import (  # noqa: E402
    DestinationStatus,
    check_recording_destination,
    validate_config,
)
from tinypirelay.media_runtime import (  # noqa: E402
    CONNECTION_ATTEMPT_TIMEOUT_SECONDS,
    MediaRuntime,
)
from tinypirelay.media_service import GLibScheduler  # noqa: E402
from tinypirelay.media_state import (  # noqa: E402
    CaptureState,
    ConnectionState,
    RecordingState,
    ServiceState,
    StreamState,
)


SCHEMA_VERSION = 1
SPIKE_NAME = "step2_real_engine_srt_recording_integration"
BITRATE_SEQUENCE_BPS = (128_000, 64_000, 192_000)
TIMELINE_LIMIT = 256
TICK_MILLISECONDS = 50
REQUIRED_COMMON_ELEMENTS = frozenset(
    (
        "capsfilter",
        "tee",
        "queue",
        "valve",
        "audioconvert",
        "audioresample",
        "opusenc",
        "opusparse",
        "matroskamux",
        "srtsink",
        "srtsrc",
        "matroskademux",
        "opusdec",
        "level",
        "spectrum",
        "flacenc",
        "filesink",
        "filesrc",
        "flacparse",
        "flacdec",
        "identity",
        "fakesink",
    )
)


class ScenarioError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def receiver_description(port: int, latency_ms: int) -> str:
    """Return the sole disposable parse-launch pipeline used by this spike."""

    return (
        f'srtsrc uri="srt://:{port}?mode=listener&latency={latency_ms}" '
        "! matroskademux ! queue ! opusparse ! opusdec "
        "! identity name=receiver_counter signal-handoffs=true silent=true "
        "! fakesink sync=false"
    )


class BufferCounter:
    def __init__(self, clock_time_none: int) -> None:
        self._clock_time_none = clock_time_none
        self._lock = threading.Lock()
        self._count = 0
        self._last_pts_ns: int | None = None
        self._missing_pts = 0
        self._non_monotonic_pts = 0

    def on_handoff(self, _identity: Any, buffer: Any) -> None:
        pts = int(buffer.pts)
        with self._lock:
            self._count += 1
            if pts == self._clock_time_none:
                self._missing_pts += 1
            elif self._last_pts_ns is not None and pts <= self._last_pts_ns:
                self._non_monotonic_pts += 1
            if pts != self._clock_time_none:
                self._last_pts_ns = pts

    def count(self) -> int:
        with self._lock:
            return self._count

    def snapshot(self) -> dict[str, int | None]:
        with self._lock:
            return {
                "buffer_count": self._count,
                "last_pts_ns": self._last_pts_ns,
                "missing_pts": self._missing_pts,
                "non_monotonic_pts": self._non_monotonic_pts,
            }


class Timeline:
    """A bounded diagnostic timeline; telemetry is counted elsewhere."""

    def __init__(
        self,
        started_monotonic: float,
        *,
        limit: int = TIMELINE_LIMIT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._started = started_monotonic
        self._limit = limit
        self._clock = clock
        self.entries: list[dict[str, object]] = []
        self.dropped = 0

    def add(self, event: str, **details: object) -> None:
        if len(self.entries) >= self._limit:
            self.dropped += 1
            return
        self.entries.append(
            {
                "sequence": len(self.entries) + 1,
                "elapsed_seconds": round(self._clock() - self._started, 3),
                "event": event,
                "details": _safe_value(details),
            }
        )

    def snapshot(self) -> dict[str, object]:
        return {"limit": self._limit, "dropped": self.dropped, "entries": self.entries}


class DestinationGate:
    """Use the real destination check except for one explicit threshold signal."""

    def __init__(self) -> None:
        self.threshold_simulated = False

    def __call__(self, recording: object) -> DestinationStatus:
        status = check_recording_destination(recording)
        if not self.threshold_simulated:
            return status
        return replace(
            status,
            safe=False,
            reason="storage_threshold",
            total_bytes=1_000,
            used_bytes=900,
            available_bytes=100,
            used_percent=90.0,
            at_or_above_threshold=True,
        )


class DisposableReceiver:
    def __init__(
        self,
        Gst: Any,
        description: str,
        counter: BufferCounter,
        timeline: Timeline,
    ) -> None:
        self._Gst = Gst
        self._description = description
        self._counter = counter
        self._timeline = timeline
        self._pipeline: Any = None
        self._bus: Any = None
        self._bus_handler: int | None = None
        self.errors: list[dict[str, object]] = []

    def start(self) -> None:
        if self._pipeline is not None:
            return
        pipeline = self._Gst.parse_launch(self._description)
        identity = pipeline.get_by_name("receiver_counter")
        if identity is None:
            pipeline.set_state(self._Gst.State.NULL)
            raise ScenarioError("disposable receiver counter was not created")
        identity.connect("handoff", self._counter.on_handoff)
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        self._bus_handler = bus.connect("message", self._on_message)
        self._pipeline = pipeline
        self._bus = bus
        result = pipeline.set_state(self._Gst.State.PLAYING)
        if result == self._Gst.StateChangeReturn.FAILURE:
            self.stop()
            raise ScenarioError("disposable receiver failed to enter PLAYING")
        self._timeline.add("receiver.started")

    def stop(self) -> None:
        pipeline, self._pipeline = self._pipeline, None
        bus, self._bus = self._bus, None
        handler, self._bus_handler = self._bus_handler, None
        if pipeline is None:
            return
        pipeline.set_state(self._Gst.State.NULL)
        if bus is not None:
            if handler is not None:
                bus.disconnect(handler)
            bus.remove_signal_watch()
        self._timeline.add("receiver.stopped")

    def _on_message(self, _bus: Any, message: Any) -> None:
        if message.type != self._Gst.MessageType.ERROR:
            return
        error, _debug = message.parse_error()
        item = {
            "source": message.src.get_name() if message.src is not None else None,
            "message": _safe_text(str(error)),
        }
        self.errors.append(item)
        self._timeline.add("receiver.error", **item)


def build_config(args: argparse.Namespace, recording_directory: Path) -> object:
    channels = args.channels
    if channels is None:
        channels = 1 if args.source_factory == "alsasrc" else 2
    device_id = args.device_id or "integration-audiotestsrc"
    return validate_config(
        {
            "schema_version": 1,
            "capture": {
                "device_id": device_id,
                "mode": {
                    "format": args.capture_format,
                    "rate_hz": args.rate_hz,
                    "channels": channels,
                },
            },
            "stream": {
                "enabled": True,
                "representation_id": OPUS_ID,
                "destination_host": "127.0.0.1",
                "destination_port": args.port,
                "latency_ms": args.latency_ms,
                "stream_id": None,
                "passphrase": None,
                "opus_bitrate_bps": BITRATE_SEQUENCE_BPS[0],
            },
            "recording": {
                "directory": str(recording_directory),
                "rotation_seconds": 3_600,
                "required_mountpoint": None,
            },
            "monitoring": {
                "spectrum_updates_per_second": 5,
                "spectrum_bands": 64,
            },
        },
        APPROVED_REPRESENTATIONS,
    )


def _element_versions(Gst: Any, source_factory: str) -> dict[str, object]:
    names = set(REQUIRED_COMMON_ELEMENTS)
    names.add(source_factory)
    result: dict[str, object] = {}
    missing: list[str] = []
    for name in sorted(names):
        factory = Gst.ElementFactory.find(name)
        if factory is None:
            missing.append(name)
            continue
        plugin = factory.get_plugin()
        result[name] = {
            "plugin": plugin.get_name() if plugin is not None else None,
            "version": plugin.get_version() if plugin is not None else None,
        }
    if missing:
        raise ScenarioError(f"missing GStreamer elements: {', '.join(missing)}")
    return result


def _make_flac_validator(Gst: Any, path: str, counter: BufferCounter) -> Any:
    pipeline = Gst.Pipeline.new("step2_flac_validator")
    if pipeline is None:
        raise ScenarioError("could not create FLAC validation pipeline")
    elements = []
    for factory_name, name in (
        ("filesrc", "validation_source"),
        ("flacparse", "validation_parser"),
        ("flacdec", "validation_decoder"),
        ("identity", "validation_counter"),
        ("fakesink", "validation_sink"),
    ):
        element = Gst.ElementFactory.make(factory_name, name)
        if element is None:
            raise ScenarioError(f"could not create {factory_name} for FLAC validation")
        pipeline.add(element)
        elements.append(element)
    elements[0].set_property("location", path)
    elements[3].set_property("signal-handoffs", True)
    elements[3].set_property("silent", True)
    elements[4].set_property("sync", False)
    elements[3].connect("handoff", counter.on_handoff)
    for left, right in zip(elements, elements[1:]):
        if not left.link(right):
            pipeline.set_state(Gst.State.NULL)
            raise ScenarioError("could not link FLAC validation pipeline")
    return pipeline


class IntegrationScenario:
    def __init__(
        self,
        args: argparse.Namespace,
        Gst: Any,
        GLib: Any,
        config: object,
        report: dict[str, Any],
        gate: DestinationGate,
    ) -> None:
        self.args = args
        self.Gst = Gst
        self.GLib = GLib
        self.config = config
        self.report = report
        self.gate = gate
        self.started = time.monotonic()
        self.timeline = Timeline(self.started)
        self.context = GLib.MainContext.default()
        self.loop = GLib.MainLoop.new(self.context, False)
        self.counter = BufferCounter(int(Gst.CLOCK_TIME_NONE))
        self.receiver = DisposableReceiver(
            Gst,
            receiver_description(args.port, args.latency_ms),
            self.counter,
            self.timeline,
        )
        self.engine_event_counts: dict[str, int] = {}
        self.event_order: list[str] = []
        self.fatal_errors: list[dict[str, object]] = []
        self.stream_instance_tokens: list[int] = []
        self.stream_removals: list[dict[str, object]] = []
        self.connection_loss_events = 0
        self.recording_removals: list[dict[str, object]] = []
        self.shutdown_complete = False
        self.shutdown_requested = False
        self.failure: str | None = None
        self._failure_deadline: float | None = None
        self._generator: Any = None

        self.engine = GStreamerEngine(
            config,
            self._on_engine_event,
            source_factory=args.source_factory,
            gst_loader=lambda: (Gst, GLib),
        )
        self.runtime = MediaRuntime(
            config,
            "step2-integration-v1",
            self.engine,
            scheduler=GLibScheduler(GLib, self.context),
            destination_check=gate,
            storage_poll_seconds=args.storage_poll_seconds,
        )

    def run(self) -> None:
        self._generator = self._scenario()
        tick = self.GLib.timeout_source_new(TICK_MILLISECONDS)
        tick.set_callback(self._tick)
        tick.attach(self.context)
        timeout = self.GLib.timeout_source_new(
            max(1, int(self.args.timeout_seconds * 1_000))
        )
        timeout.set_callback(self._global_timeout)
        timeout.attach(self.context)
        try:
            try:
                self.loop.run()
            except KeyboardInterrupt:
                self._fail("KeyboardInterrupt")
                self.loop.run()
        finally:
            tick.destroy()
            timeout.destroy()
            self.receiver.stop()
        self.report["timeline"] = self.timeline.snapshot()
        self.report["engine_event_counts"] = dict(sorted(self.engine_event_counts.items()))
        self.report["fatal_errors"] = list(self.fatal_errors)
        self.report["receiver"]["buffers"] = self.counter.snapshot()
        self.report["receiver"]["bus_errors"] = list(self.receiver.errors)
        if self.failure is not None:
            self.report["scenario_errors"].append(self.failure)
        self.report["final_snapshot"] = self.runtime.snapshot()

    def _tick(self, *_unused: object) -> bool:
        if self.failure is not None:
            if self.shutdown_complete or (
                self._failure_deadline is not None
                and time.monotonic() >= self._failure_deadline
            ):
                self.loop.quit()
                return False
            return True
        try:
            next(self._generator)
        except StopIteration:
            self.loop.quit()
            return False
        except BaseException as exc:
            self._fail(f"{type(exc).__name__}: {exc}")
        return True

    def _global_timeout(self, *_unused: object) -> bool:
        self._fail("scenario exceeded its global timeout")
        return False

    def _fail(self, message: str) -> None:
        if self.failure is not None:
            return
        self.failure = _safe_text(message)
        self.timeline.add("scenario.failed", message=self.failure)
        self._failure_deadline = time.monotonic() + 6.0
        if not self.shutdown_requested:
            self.shutdown_requested = True
            try:
                self.runtime.shutdown()
            except BaseException:
                self.loop.quit()

    def _on_engine_event(self, event: EngineEvent) -> None:
        self.runtime.handle_engine_event(event)
        key = f"{event.kind}:{event.scope}"
        self.engine_event_counts[key] = self.engine_event_counts.get(key, 0) + 1
        if event.kind != "telemetry":
            self.event_order.append(key)
            details = {
                name: value
                for name, value in event.details.items()
                if name
                in {
                    "code",
                    "message",
                    "state",
                    "instance_token",
                    "forced",
                    "missing",
                    "path",
                    "old_path",
                    "new_path",
                    "old_instance_token",
                    "new_instance_token",
                    "old_bps",
                    "new_bps",
                    "representation_id",
                }
            }
            self.timeline.add(key, generation=event.generation, **details)
        if event.kind == "branch-created" and event.scope == "stream":
            token = event.details.get("instance_token")
            if type(token) is int:
                self.stream_instance_tokens.append(token)
        if (
            event.kind == "connection"
            and event.scope == "stream"
            and event.details.get("state") == "disconnected"
        ):
            self.connection_loss_events += 1
        if event.kind == "branch-removed" and event.scope == "recording":
            self.recording_removals.append(
                {
                    "path": event.details.get("path"),
                    "forced": event.details.get("forced"),
                    "missing": event.details.get("missing", False),
                }
            )
        if event.kind == "branch-removed" and event.scope == "stream":
            self.stream_removals.append(
                {
                    "instance_token": event.details.get("instance_token"),
                    "forced": event.details.get("forced"),
                    "missing": event.details.get("missing", False),
                }
            )
        if event.kind == "error":
            self.fatal_errors.append(
                {
                    "scope": event.scope,
                    "generation": event.generation,
                    "code": _safe_value(event.details.get("code")),
                    "message": _safe_text(
                        str(event.details.get("message", "media engine error"))
                    ),
                }
            )
        if event.kind == "shutdown-complete":
            self.shutdown_complete = True

    def _raise_if_failed(self) -> None:
        if self.receiver.errors:
            raise ScenarioError("disposable receiver reported a GStreamer error")
        if self.fatal_errors:
            raise ScenarioError(str(self.fatal_errors[-1]["message"]))

    def _continuity_marker(self) -> dict[str, object]:
        return {
            "reconnect_count": self.runtime.state.reconnect_count,
            "branch_created_stream_count": len(self.stream_instance_tokens),
            "stream_instance_tokens": list(self.stream_instance_tokens),
            "connection_loss_events": self.connection_loss_events,
            "receiver": self.counter.snapshot(),
        }

    def _wait(
        self,
        label: str,
        predicate: Callable[[], bool],
        timeout_seconds: float = 10.0,
    ) -> Any:
        deadline = time.monotonic() + timeout_seconds
        self.timeline.add("wait.started", label=label)
        self._raise_if_failed()
        while not predicate():
            self._raise_if_failed()
            if time.monotonic() >= deadline:
                raise ScenarioError(f"timed out waiting for {label}")
            yield
        self._raise_if_failed()
        self.timeline.add("wait.completed", label=label)

    def _require_decision(self, name: str, decision: object) -> None:
        if not getattr(decision, "ok", False):
            error = getattr(decision, "error", None)
            message = getattr(error, "message", f"{name} was rejected")
            raise ScenarioError(f"{name} failed: {message}")

    def _validate_flac(self, path: str) -> Any:
        file_path = Path(path)
        exists = file_path.is_file()
        size = file_path.stat().st_size if exists else 0
        if exists:
            with file_path.open("rb") as handle:
                header = handle.read(4)
        else:
            header = b""
        counter = BufferCounter(int(self.Gst.CLOCK_TIME_NONE))
        result: dict[str, object] = {
            "path": path,
            "exists": exists,
            "size_bytes": size,
            "flac_header": header == b"fLaC",
            "decoded_to_eos": False,
            "decoded_buffers": 0,
            "error": None,
        }
        if not exists or size <= 4 or header != b"fLaC":
            return result
        self._raise_if_failed()
        pipeline = _make_flac_validator(self.Gst, path, counter)
        bus = pipeline.get_bus()
        if pipeline.set_state(self.Gst.State.PLAYING) == self.Gst.StateChangeReturn.FAILURE:
            pipeline.set_state(self.Gst.State.NULL)
            result["error"] = "validation pipeline failed to enter PLAYING"
            return result
        deadline = time.monotonic() + 8.0
        mask = self.Gst.MessageType.ERROR | self.Gst.MessageType.EOS
        try:
            while time.monotonic() < deadline:
                self._raise_if_failed()
                message = bus.timed_pop_filtered(0, mask)
                if message is None:
                    yield
                    continue
                if message.type == self.Gst.MessageType.ERROR:
                    error, _debug = message.parse_error()
                    result["error"] = _safe_text(str(error))
                else:
                    result["decoded_to_eos"] = True
                break
            if not result["decoded_to_eos"] and result["error"] is None:
                result["error"] = "FLAC validation timed out"
        finally:
            pipeline.set_state(self.Gst.State.NULL)
        result["decoded_buffers"] = counter.count()
        self._raise_if_failed()
        return result

    def _scenario(self) -> Any:
        scenarios = self.report["scenarios"]
        self.receiver.start()
        initial_count = self.counter.count()
        self._require_decision("capture start", self.runtime.start_capture())
        yield from self._wait(
            "capture RUNNING", lambda: self.runtime.state.capture is CaptureState.RUNNING
        )
        self._require_decision("stream start", self.runtime.start_stream())
        yield from self._wait(
            "Opus stream connection and decoded buffers",
            lambda: (
                self.runtime.state.stream is StreamState.RUNNING
                and self.runtime.state.connection is ConnectionState.CONNECTED
                and self.counter.count() - initial_count >= self.args.min_buffer_delta
            ),
            12.0,
        )
        baseline_capture_generation = self.runtime.state.capture_generation
        baseline_stream_generation = self.runtime.state.stream_generation
        uninterrupted_baseline = self._continuity_marker()
        scenarios["startup"] = {
            "capture_state": self.runtime.state.capture.value,
            "stream_state": self.runtime.state.stream.value,
            "connection_state": self.runtime.state.connection.value,
            "capture_generation": baseline_capture_generation,
            "stream_generation": baseline_stream_generation,
            "receiver_buffer_delta": self.counter.count() - initial_count,
            "continuity": uninterrupted_baseline,
        }

        transitions = []
        for target in BITRATE_SEQUENCE_BPS[1:]:
            before = self.counter.count()
            continuity_before = self._continuity_marker()
            self._require_decision(
                f"Opus bitrate {target}", self.runtime.set_opus_bitrate(target)
            )
            yield from self._wait(
                f"Opus bitrate readback {target}",
                lambda target=target, before=before: (
                    self.runtime.state.opus_bitrate_bps == target
                    and self.runtime.state.pending_opus_bitrate_bps is None
                    and self.counter.count() - before >= self.args.min_buffer_delta
                ),
            )
            transitions.append(
                {
                    "requested_bps": target,
                    "readback_bps": self.runtime.state.opus_bitrate_bps,
                    "capture_generation": self.runtime.state.capture_generation,
                    "stream_generation": self.runtime.state.stream_generation,
                    "stream_state": self.runtime.state.stream.value,
                    "connection_state": self.runtime.state.connection.value,
                    "receiver_buffer_delta": self.counter.count() - before,
                    "continuity_before": continuity_before,
                    "continuity_after": self._continuity_marker(),
                }
            )
        scenarios["bitrate"] = {
            "initial_bps": BITRATE_SEQUENCE_BPS[0],
            "transitions": transitions,
        }

        recording_before = self.counter.count()
        recording_continuity_before = self._continuity_marker()
        self._require_decision("recording start", self.runtime.start_recording())
        yield from self._wait(
            "first recording RUNNING",
            lambda: (
                self.runtime.state.recording is RecordingState.RUNNING
                and self.runtime.state.recording_current_file is not None
            ),
        )
        first_path = str(self.runtime.state.recording_current_file)
        yield from self._wait(
            "receiver progress during first recording",
            lambda: self.counter.count() - recording_before >= self.args.min_buffer_delta,
        )

        concurrent_sample: dict[str, object] = {}

        def concurrent_evidence_ready() -> bool:
            stats = self.engine.poll_stats()
            snapshot = self.runtime.snapshot()
            state = snapshot.get("state")
            connection = state.get("connection") if isinstance(state, Mapping) else {}
            runtime_statistics = (
                connection.get("statistics")
                if isinstance(connection, Mapping)
                else {}
            )
            concurrent_sample.update(
                {
                    "engine_srt_statistics": stats.get("connection", {}),
                    "runtime_srt_statistics": runtime_statistics,
                    "queues": stats.get("queues", {}),
                }
            )
            return bool(
                self.runtime.state.stream is StreamState.RUNNING
                and self.runtime.state.recording is RecordingState.RUNNING
                and _srt_stats_valid(concurrent_sample["engine_srt_statistics"])
                and _srt_stats_valid(concurrent_sample["runtime_srt_statistics"])
                and _queues_match_contract(concurrent_sample["queues"])
            )

        yield from self._wait(
            "concurrent SRT statistics and queue contracts",
            concurrent_evidence_ready,
            10.0,
        )
        concurrent_sample.update(
            {
                "stream_state": self.runtime.state.stream.value,
                "recording_state": self.runtime.state.recording.value,
                "level_events": self.engine_event_counts.get("telemetry:level", 0),
                "spectrum_events": self.engine_event_counts.get(
                    "telemetry:spectrum", 0
                ),
                "continuity": self._continuity_marker(),
            }
        )
        scenarios["concurrent_branches"] = concurrent_sample

        self._require_decision(
            "manual recording rotation", self.runtime.dispatch("recording.rotate")
        )
        yield from self._wait(
            "manual recording rotation finalization",
            lambda: (
                not self.runtime.state.recording_rotation_pending
                and self.runtime.state.recording_last_finalized_file == first_path
                and self.runtime.state.recording_current_file not in (None, first_path)
            ),
        )
        second_path = str(self.runtime.state.recording_current_file)
        rotation_count = self.counter.count()
        yield from self._wait(
            "receiver progress after rotation",
            lambda: self.counter.count() - rotation_count >= self.args.min_buffer_delta,
        )
        self._require_decision("recording stop", self.runtime.stop_recording())
        yield from self._wait(
            "rotated recording STOPPED",
            lambda: self.runtime.state.recording is RecordingState.STOPPED,
        )
        validations = []
        for path in (first_path, second_path):
            validations.append((yield from self._validate_flac(path)))
        scenarios["recording"] = {
            "representation": "native_capture_flac",
            "paths": [first_path, second_path],
            "validations": validations,
            "recording_state": self.runtime.state.recording.value,
            "last_finalized_file": self.runtime.state.recording_last_finalized_file,
            "capture_generation": self.runtime.state.capture_generation,
            "stream_generation": self.runtime.state.stream_generation,
            "receiver_buffer_delta": self.counter.count() - recording_before,
            "continuity_before": recording_continuity_before,
            "continuity_after": self._continuity_marker(),
        }

        self.gate.threshold_simulated = False
        threshold_continuity_before = self._continuity_marker()
        self._require_decision("threshold recording start", self.runtime.start_recording())
        yield from self._wait(
            "threshold recording RUNNING",
            lambda: (
                self.runtime.state.recording is RecordingState.RUNNING
                and self.runtime.state.recording_current_file is not None
            ),
        )
        threshold_path = str(self.runtime.state.recording_current_file)
        threshold_before = self.counter.count()
        yield from self._wait(
            "receiver progress before threshold injection",
            lambda: self.counter.count() - threshold_before >= self.args.min_buffer_delta,
        )
        self.gate.threshold_simulated = True
        self.timeline.add("storage.threshold_injected", used_percent=90.0)
        yield from self._wait(
            "90 percent recording hard stop",
            lambda: self.runtime.state.recording is RecordingState.BLOCKED,
            self.args.storage_poll_seconds + 8.0,
        )
        stopped_count = self.counter.count()
        yield from self._wait(
            "receiver progress after recording hard stop",
            lambda: self.counter.count() - stopped_count >= self.args.min_buffer_delta,
        )
        threshold_validation = yield from self._validate_flac(threshold_path)
        scenarios["storage_threshold"] = {
            "threshold_simulated": True,
            "used_percent": 90.0,
            "recording_state": self.runtime.state.recording.value,
            "recording_warning_code": (
                self.runtime.state.recording_warning.code
                if self.runtime.state.recording_warning is not None
                else None
            ),
            "validation": threshold_validation,
            "stream_state": self.runtime.state.stream.value,
            "connection_state": self.runtime.state.connection.value,
            "capture_generation": self.runtime.state.capture_generation,
            "stream_generation": self.runtime.state.stream_generation,
            "receiver_buffer_delta_after_stop": self.counter.count() - stopped_count,
            "continuity_before": threshold_continuity_before,
            "continuity_after": self._continuity_marker(),
        }

        self.gate.threshold_simulated = False
        self.timeline.add("storage.threshold_cleared")
        yield from self._wait(
            "storage safety latch clear",
            lambda: self.runtime.state.recording is RecordingState.STOPPED,
            self.args.storage_poll_seconds + 8.0,
        )
        scenarios["storage_threshold"]["latch_cleared_state"] = (
            self.runtime.state.recording.value
        )

        monitoring_before = self.counter.count()
        monitoring_continuity_before = self._continuity_marker()
        yield from self._wait(
            "minimum no-consumer evidence window",
            lambda: (
                time.monotonic() - self.started >= self.args.minimum_duration_seconds
                and self.counter.count() - monitoring_before >= self.args.min_buffer_delta
            ),
            self.args.timeout_seconds,
        )
        monitoring_stats = self.engine.poll_stats()
        monitoring_snapshot = self.runtime.snapshot()
        telemetry = monitoring_snapshot["telemetry"]
        monitoring_state = monitoring_snapshot.get("state")
        monitoring_connection = (
            monitoring_state.get("connection")
            if isinstance(monitoring_state, Mapping)
            else {}
        )
        runtime_statistics = (
            monitoring_connection.get("statistics")
            if isinstance(monitoring_connection, Mapping)
            else {}
        )
        scenarios["monitoring"] = {
            "telemetry_consumers": 0,
            "level_events": self.engine_event_counts.get("telemetry:level", 0),
            "spectrum_events": self.engine_event_counts.get("telemetry:spectrum", 0),
            "latest_meter_sequence": (telemetry.get("meter") or {}).get("sequence"),
            "latest_spectrum_sequence": (telemetry.get("spectrum") or {}).get(
                "sequence"
            ),
            "engine_srt_statistics": monitoring_stats.get("connection", {}),
            "runtime_srt_statistics": runtime_statistics,
            "queues": monitoring_stats.get("queues", {}),
            "receiver_buffer_delta": self.counter.count() - monitoring_before,
            "continuity_before": monitoring_continuity_before,
            "continuity_after": self._continuity_marker(),
        }
        pre_disconnect = self._continuity_marker()
        scenarios["uninterrupted_continuity"] = {
            "baseline": uninterrupted_baseline,
            "before_intentional_disconnect": pre_disconnect,
            "phase_names": ["bitrate", "recording", "storage_threshold", "monitoring"],
        }

        reconnect_before = self.counter.count()
        retry_before = self.runtime.state.reconnect_count
        reconnect_continuity_before = self._continuity_marker()
        stream_branches_before = len(self.stream_instance_tokens)
        active_token = self.stream_instance_tokens[-1]
        offline_started = time.monotonic()
        self.receiver.stop()
        yield from self._wait(
            "disconnected caller branch removal",
            lambda: (
                self.runtime.state.connection is ConnectionState.RETRY_WAIT
                and any(
                    removal.get("instance_token") == active_token
                    and removal.get("forced") is False
                    for removal in self.stream_removals
                )
            ),
        )
        yield from self._wait(
            "offline retry branch creation",
            lambda: (
                self.runtime.state.connection is ConnectionState.CONNECTING
                and self.runtime.state.reconnect_count == retry_before + 1
                and len(self.stream_instance_tokens) == stream_branches_before + 1
            ),
            12.0,
        )
        first_retry_delay = time.monotonic() - offline_started
        offline_attempt_started = time.monotonic()
        offline_token = self.stream_instance_tokens[-1]

        def offline_error() -> object | None:
            error = self.runtime.state.connection_error
            if error is None:
                return None
            code = error.code
            if code == "srt_connection_timeout" or (
                isinstance(code, str)
                and re.fullmatch(r"srt_gstreamer_error_[0-9]+", code)
            ):
                return error
            return None

        yield from self._wait(
            "offline retry completion and physical removal",
            lambda: (
                self.runtime.state.connection is ConnectionState.RETRY_WAIT
                and offline_error() is not None
                and any(
                    removal.get("instance_token") == offline_token
                    and removal.get("forced") is False
                    and removal.get("missing") is False
                    for removal in self.stream_removals
                )
            ),
            CONNECTION_ATTEMPT_TIMEOUT_SECONDS + 2.0,
        )
        offline_attempt_duration = time.monotonic() - offline_attempt_started
        attempt_error = offline_error()
        if attempt_error is None:
            raise ScenarioError("offline attempt completed without a safe error")
        attempt_code = attempt_error.code
        attempt_cause = (
            "runtime_watchdog"
            if attempt_code == "srt_connection_timeout"
            else "native_srt_error"
        )
        offline_removal = next(
            removal
            for removal in reversed(self.stream_removals)
            if removal.get("instance_token") == offline_token
        )
        retry_count_at_restore = self.runtime.state.reconnect_count
        branch_count_at_restore = len(self.stream_instance_tokens)
        restore_state = self.runtime.state.connection.value
        receiver_restore_started = time.monotonic()
        self.receiver.start()
        yield from self._wait(
            "post-offline recovery branch creation",
            lambda: (
                self.runtime.state.reconnect_count == retry_count_at_restore + 1
                and len(self.stream_instance_tokens) == branch_count_at_restore + 1
                and self.stream_instance_tokens[-1] != offline_token
            ),
            8.0,
        )
        recovery_retry_delay = time.monotonic() - receiver_restore_started
        recovered_token = self.stream_instance_tokens[-1]
        yield from self._wait(
            "receiver restore and decoded progress",
            lambda: (
                self.runtime.state.connection is ConnectionState.CONNECTED
                and self.counter.count() - reconnect_before >= self.args.min_buffer_delta
            ),
            15.0,
        )
        scenarios["reconnect"] = {
            "receiver_stopped": True,
            "receiver_restored": True,
            "retry_count_delta": self.runtime.state.reconnect_count - retry_before,
            "first_retry_delay_seconds": round(first_retry_delay, 3),
            "attempt_timeout_seconds": CONNECTION_ATTEMPT_TIMEOUT_SECONDS,
            "offline_attempt_duration_seconds": round(offline_attempt_duration, 3),
            "offline_attempt_cause": attempt_cause,
            "offline_attempt_error_code": attempt_code,
            "offline_attempt_error_message": _safe_text(attempt_error.message),
            "offline_attempt_token": offline_token,
            "offline_attempt_removal": offline_removal,
            "fallback_error_code": "srt_connection_timeout",
            "retry_count_at_receiver_restore_delta": retry_count_at_restore - retry_before,
            "branch_count_at_receiver_restore_delta": (
                branch_count_at_restore - stream_branches_before
            ),
            "receiver_restore_state": restore_state,
            "recovery_retry_delay_seconds": round(recovery_retry_delay, 3),
            "recovery_retry_count_delta": (
                self.runtime.state.reconnect_count - retry_count_at_restore
            ),
            "recovered_instance_token": recovered_token,
            "connection_state": self.runtime.state.connection.value,
            "capture_generation": self.runtime.state.capture_generation,
            "stream_generation": self.runtime.state.stream_generation,
            "receiver_buffer_delta": self.counter.count() - reconnect_before,
            "continuity_before": reconnect_continuity_before,
            "continuity_after": self._continuity_marker(),
        }

        shutdown_recording_before = self.counter.count()
        self._require_decision(
            "shutdown recording start", self.runtime.start_recording()
        )
        yield from self._wait(
            "shutdown recording RUNNING and receiver progress",
            lambda: (
                self.runtime.state.recording is RecordingState.RUNNING
                and self.runtime.state.recording_current_file is not None
                and self.counter.count() - shutdown_recording_before
                >= self.args.min_buffer_delta
            ),
        )
        shutdown_recording_path = str(self.runtime.state.recording_current_file)

        shutdown_start = len(self.event_order)
        shutdown_removal_start = len(self.recording_removals)
        self.shutdown_requested = True
        self._require_decision("ordered shutdown", self.runtime.shutdown())
        yield from self._wait(
            "engine shutdown completion", lambda: self.shutdown_complete, 12.0
        )
        scenarios["shutdown"] = {
            "requested": True,
            "engine_shutdown_complete": True,
            "service_state": self.runtime.state.service.value,
            "recording_state": self.runtime.state.recording.value,
            "stream_state": self.runtime.state.stream.value,
            "capture_state": self.runtime.state.capture.value,
            "recording_path": shutdown_recording_path,
            "last_finalized_file": self.runtime.state.recording_last_finalized_file,
            "recording_removals": self.recording_removals[
                shutdown_removal_start:
            ],
            "event_order": self.event_order[shutdown_start:],
        }
        self.receiver.stop()
        scenarios["shutdown"]["validation"] = yield from self._validate_flac(
            shutdown_recording_path
        )
        self._raise_if_failed()


def _flac_valid(value: object) -> bool:
    return bool(
        isinstance(value, Mapping)
        and value.get("exists") is True
        and value.get("flac_header") is True
        and value.get("decoded_to_eos") is True
        and type(value.get("decoded_buffers")) is int
        and value.get("decoded_buffers", 0) > 0
        and value.get("error") is None
    )


def _srt_stats_valid(value: object) -> bool:
    if not isinstance(value, Mapping) or not value:
        return False
    if any(type(key) is not str or key not in SRT_STATS_FIELDS for key in value):
        return False
    for item in value.values():
        if item is None or type(item) in (bool, int, str):
            continue
        if type(item) is float and item == item and abs(item) != float("inf"):
            continue
        return False
    sent = value.get("bytes-sent-total")
    return type(sent) is int and sent > 0


def _queues_match_contract(
    value: object,
    required: Sequence[str] = ("stream", "recording", "level", "spectrum"),
) -> bool:
    if not isinstance(value, Mapping):
        return False
    contracts = {
        "stream": (0, 0, STREAM_QUEUE_NS, 0),
        "recording": (0, 0, RECORDING_QUEUE_NS, 0),
        "level": (MONITOR_QUEUE_BUFFERS, 0, 0, 2),
        "spectrum": (MONITOR_QUEUE_BUFFERS, 0, 0, 2),
    }
    for name in required:
        queue = value.get(name)
        if not isinstance(queue, Mapping):
            return False
        expected = contracts[name]
        actual = tuple(
            queue.get(field)
            for field in ("max_buffers", "max_bytes", "max_time_ns", "leaky")
        )
        if actual != expected or type(queue.get("instance_token")) is not int:
            return False
        for current_name, maximum in zip(
            ("current_buffers", "current_bytes", "current_time_ns"), expected[:3]
        ):
            current = queue.get(current_name)
            if type(current) is not int or current < 0:
                return False
            if maximum > 0 and current > maximum:
                return False
    return True


def _continuity_unchanged(before: object, after: object) -> bool:
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        return False
    fields = (
        "reconnect_count",
        "branch_created_stream_count",
        "stream_instance_tokens",
        "connection_loss_events",
    )
    return all(before.get(field) == after.get(field) for field in fields)


def _pts_clean(marker: object) -> bool:
    if not isinstance(marker, Mapping):
        return False
    receiver = marker.get("receiver")
    return bool(
        isinstance(receiver, Mapping)
        and receiver.get("missing_pts") == 0
        and receiver.get("non_monotonic_pts") == 0
        and type(receiver.get("last_pts_ns")) is int
    )


def _pts_advanced(before: object, after: object) -> bool:
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        return False
    left = before.get("receiver")
    right = after.get("receiver")
    if not isinstance(left, Mapping) or not isinstance(right, Mapping):
        return False
    left_pts = left.get("last_pts_ns")
    right_pts = right.get("last_pts_ns")
    return bool(
        type(left_pts) is int
        and type(right_pts) is int
        and right_pts > left_pts
        and _pts_clean(after)
    )


def assess_report(report: Mapping[str, Any], min_buffer_delta: int) -> dict[str, Any]:
    scenarios = report.get("scenarios")
    scenarios = scenarios if isinstance(scenarios, Mapping) else {}
    startup = scenarios.get("startup") or {}
    bitrate = scenarios.get("bitrate") or {}
    recording = scenarios.get("recording") or {}
    concurrent = scenarios.get("concurrent_branches") or {}
    storage = scenarios.get("storage_threshold") or {}
    reconnect = scenarios.get("reconnect") or {}
    monitoring = scenarios.get("monitoring") or {}
    uninterrupted = scenarios.get("uninterrupted_continuity") or {}
    shutdown = scenarios.get("shutdown") or {}
    capture_generation = startup.get("capture_generation")
    stream_generation = startup.get("stream_generation")

    def at_least(value: object, minimum: int) -> bool:
        return type(value) is int and value >= minimum

    def increased(before: object, after: object, field: str) -> bool:
        if not isinstance(before, Mapping) or not isinstance(after, Mapping):
            return False
        left, right = before.get(field), after.get(field)
        return type(left) is int and type(right) is int and right > left

    checks: dict[str, bool] = {}
    checks["capture_opus_connected"] = bool(
        startup.get("capture_state") == "running"
        and startup.get("stream_state") == "running"
        and startup.get("connection_state") == "connected"
        and at_least(startup.get("receiver_buffer_delta"), min_buffer_delta)
    )
    transitions = bitrate.get("transitions")
    transitions = transitions if isinstance(transitions, list) else []
    checks["runtime_bitrate_readback_without_restart"] = bool(
        bitrate.get("initial_bps") == BITRATE_SEQUENCE_BPS[0]
        and len(transitions) == 2
        and all(isinstance(item, Mapping) for item in transitions)
        and [item.get("requested_bps") for item in transitions]
        == list(BITRATE_SEQUENCE_BPS[1:])
        and all(
            item.get("readback_bps") == item.get("requested_bps")
            and item.get("capture_generation") == capture_generation
            and item.get("stream_generation") == stream_generation
            and item.get("stream_state") == "running"
            and item.get("connection_state") == "connected"
            and at_least(item.get("receiver_buffer_delta"), min_buffer_delta)
            and _continuity_unchanged(
                item.get("continuity_before"), item.get("continuity_after")
            )
            and _pts_advanced(
                item.get("continuity_before"), item.get("continuity_after")
            )
            for item in transitions
        )
    )
    paths = recording.get("paths")
    paths = paths if isinstance(paths, list) else []
    validations = recording.get("validations")
    validations = validations if isinstance(validations, list) else []
    checks["flac_rotation_stop_and_decode"] = bool(
        recording.get("representation") == "native_capture_flac"
        and len(paths) == 2
        and len(validations) == 2
        and all(_flac_valid(item) for item in validations)
        and recording.get("recording_state") == "stopped"
        and recording.get("last_finalized_file") == paths[-1]
        and recording.get("capture_generation") == capture_generation
        and recording.get("stream_generation") == stream_generation
        and at_least(recording.get("receiver_buffer_delta"), min_buffer_delta)
        and _continuity_unchanged(
            recording.get("continuity_before"), recording.get("continuity_after")
        )
    )
    checks["concurrent_srt_stats_and_queue_contracts"] = bool(
        concurrent.get("stream_state") == "running"
        and concurrent.get("recording_state") == "running"
        and at_least(concurrent.get("level_events"), 1)
        and at_least(concurrent.get("spectrum_events"), 1)
        and _srt_stats_valid(concurrent.get("engine_srt_statistics"))
        and _srt_stats_valid(concurrent.get("runtime_srt_statistics"))
        and _queues_match_contract(concurrent.get("queues"))
    )
    checks["simulated_storage_threshold_isolated"] = bool(
        storage.get("threshold_simulated") is True
        and storage.get("used_percent") == 90.0
        and storage.get("recording_state") == "blocked"
        and storage.get("recording_warning_code") == "storage_unsafe"
        and storage.get("latch_cleared_state") == "stopped"
        and _flac_valid(storage.get("validation"))
        and storage.get("stream_state") == "running"
        and storage.get("connection_state") == "connected"
        and storage.get("capture_generation") == capture_generation
        and storage.get("stream_generation") == stream_generation
        and at_least(
            storage.get("receiver_buffer_delta_after_stop"), min_buffer_delta
        )
        and _continuity_unchanged(
            storage.get("continuity_before"), storage.get("continuity_after")
        )
    )
    reconnect_before = reconnect.get("continuity_before")
    reconnect_after = reconnect.get("continuity_after")
    retry_delta = reconnect.get("retry_count_delta")
    retry_delay = reconnect.get("first_retry_delay_seconds")
    attempt_timeout = reconnect.get("attempt_timeout_seconds")
    attempt_duration = reconnect.get("offline_attempt_duration_seconds")
    recovery_delay = reconnect.get("recovery_retry_delay_seconds")
    offline_token = reconnect.get("offline_attempt_token")
    recovered_token = reconnect.get("recovered_instance_token")
    offline_removal = reconnect.get("offline_attempt_removal")
    attempt_cause = reconnect.get("offline_attempt_cause")
    attempt_code = reconnect.get("offline_attempt_error_code")
    attempt_message = reconnect.get("offline_attempt_error_message")
    duration_bounded = bool(
        type(attempt_duration) in (int, float)
        and not isinstance(attempt_duration, bool)
        and 0 <= attempt_duration <= CONNECTION_ATTEMPT_TIMEOUT_SECONDS + 2.0
    )
    cause_valid = bool(
        (
            attempt_cause == "runtime_watchdog"
            and attempt_code == "srt_connection_timeout"
            and duration_bounded
            and attempt_duration >= CONNECTION_ATTEMPT_TIMEOUT_SECONDS * 0.8
        )
        or (
            attempt_cause == "native_srt_error"
            and isinstance(attempt_code, str)
            and re.fullmatch(r"srt_gstreamer_error_[0-9]+", attempt_code)
            is not None
            and duration_bounded
        )
    )
    checks["receiver_reconnect_backoff"] = bool(
        reconnect.get("receiver_stopped") is True
        and reconnect.get("receiver_restored") is True
        and retry_delta == 2
        and type(retry_delay) in (int, float)
        and not isinstance(retry_delay, bool)
        and 0.75 <= retry_delay <= 3.0
        and attempt_timeout == CONNECTION_ATTEMPT_TIMEOUT_SECONDS
        and reconnect.get("fallback_error_code") == "srt_connection_timeout"
        and cause_valid
        and isinstance(attempt_message, str)
        and bool(attempt_message)
        and type(offline_token) is int
        and isinstance(offline_removal, Mapping)
        and offline_removal.get("instance_token") == offline_token
        and offline_removal.get("forced") is False
        and offline_removal.get("missing") is False
        and reconnect.get("retry_count_at_receiver_restore_delta") == 1
        and reconnect.get("branch_count_at_receiver_restore_delta") == 1
        and reconnect.get("receiver_restore_state") == "retry_wait"
        and type(recovery_delay) in (int, float)
        and not isinstance(recovery_delay, bool)
        and 1.5 <= recovery_delay <= 5.0
        and reconnect.get("recovery_retry_count_delta") == 1
        and type(recovered_token) is int
        and recovered_token != offline_token
        and reconnect.get("connection_state") == "connected"
        and reconnect.get("capture_generation") == capture_generation
        and reconnect.get("stream_generation") == stream_generation
        and at_least(reconnect.get("receiver_buffer_delta"), min_buffer_delta)
        and increased(reconnect_before, reconnect_after, "reconnect_count")
        and increased(
            reconnect_before, reconnect_after, "branch_created_stream_count"
        )
        and increased(reconnect_before, reconnect_after, "connection_loss_events")
    )
    checks["unconsumed_telemetry_and_queues_bounded"] = bool(
        monitoring.get("telemetry_consumers") == 0
        and at_least(monitoring.get("level_events"), 2)
        and at_least(monitoring.get("spectrum_events"), 2)
        and at_least(monitoring.get("latest_meter_sequence"), 2)
        and at_least(monitoring.get("latest_spectrum_sequence"), 2)
        and at_least(monitoring.get("receiver_buffer_delta"), min_buffer_delta)
        and _srt_stats_valid(monitoring.get("engine_srt_statistics"))
        and _srt_stats_valid(monitoring.get("runtime_srt_statistics"))
        and _queues_match_contract(
            monitoring.get("queues"), ("stream", "level", "spectrum")
        )
        and _continuity_unchanged(
            monitoring.get("continuity_before"), monitoring.get("continuity_after")
        )
    )
    continuity_baseline = uninterrupted.get("baseline")
    pre_disconnect = uninterrupted.get("before_intentional_disconnect")
    checks["uninterrupted_phases_preserve_physical_stream"] = bool(
        isinstance(continuity_baseline, Mapping)
        and _continuity_unchanged(continuity_baseline, pre_disconnect)
        and _pts_clean(pre_disconnect)
    )
    receiver = report.get("receiver")
    receiver = receiver if isinstance(receiver, Mapping) else {}
    event_counts = report.get("engine_event_counts")
    event_counts = event_counts if isinstance(event_counts, Mapping) else {}
    fatal_errors = report.get("fatal_errors")
    checks["no_unexpected_engine_or_receiver_errors"] = bool(
        isinstance(fatal_errors, list)
        and not fatal_errors
        and isinstance(receiver.get("bus_errors"), list)
        and not receiver.get("bus_errors")
        and not any(
            type(name) is str
            and name.startswith("error:")
            and type(count) is int
            and count > 0
            for name, count in event_counts.items()
        )
    )
    order = shutdown.get("event_order")
    order = order if isinstance(order, list) else []
    expected = (
        "branch-removed:recording",
        "branch-removed:stream",
        "capture-stopped:capture",
        "shutdown-complete:capture",
    )
    positions = [order.index(item) if item in order else -1 for item in expected]
    removals = shutdown.get("recording_removals")
    removals = removals if isinstance(removals, list) else []
    checks["ordered_shutdown"] = bool(
        shutdown.get("requested") is True
        and shutdown.get("engine_shutdown_complete") is True
        and shutdown.get("service_state") == "stopped"
        and shutdown.get("recording_state") == "stopped"
        and shutdown.get("stream_state") == "stopped"
        and shutdown.get("capture_state") == "stopped"
        and shutdown.get("last_finalized_file") == shutdown.get("recording_path")
        and _flac_valid(shutdown.get("validation"))
        and any(
            isinstance(item, Mapping)
            and item.get("path") == shutdown.get("recording_path")
            and item.get("forced") is False
            and item.get("missing") is False
            for item in removals
        )
        and positions[0] >= 0
        and positions == sorted(positions)
    )
    recording_output = report.get("recording_output")
    checks["recording_output_retained"] = bool(
        isinstance(recording_output, Mapping)
        and recording_output.get("explicit_directory") is True
        and recording_output.get("retained_after_run") is True
    )

    reasons = []
    if report.get("setup_errors"):
        reasons.append("setup failed")
    if report.get("scenario_errors"):
        reasons.append("scenario failed")
    reason_by_check = {
        "capture_opus_connected": "capture, Opus connection, or decoded progress missing",
        "runtime_bitrate_readback_without_restart": "64/192 kbps readback or continuity missing",
        "flac_rotation_stop_and_decode": "FLAC rotation/finalization validation missing",
        "concurrent_srt_stats_and_queue_contracts": "concurrent SRT stats or exact queue contracts missing",
        "simulated_storage_threshold_isolated": "simulated 90% recording isolation missing",
        "receiver_reconnect_backoff": "bounded receiver reconnect evidence missing",
        "unconsumed_telemetry_and_queues_bounded": "telemetry/queue bound evidence missing",
        "uninterrupted_phases_preserve_physical_stream": "uninterrupted stream-instance or PTS continuity missing",
        "no_unexpected_engine_or_receiver_errors": "unexpected engine or receiver errors observed",
        "ordered_shutdown": "ordered active-recording shutdown evidence missing",
        "recording_output_retained": "explicit retained recording output missing",
    }
    reasons.extend(reason_by_check[name] for name, passed in checks.items() if not passed)
    passed = not reasons and all(checks.values())
    return {
        "status": "pass" if passed else "fail",
        "checks": checks,
        "reasons": list(dict.fromkeys(reasons)),
        "threshold_evidence": "simulated_status_injection",
    }


def _safe_text(value: str) -> str:
    return re.sub(
        r"(?i)(passphrase(?:=|%3d))[^&\s]+",
        r"\1<redacted>",
        value,
    )[:1_024]


def _safe_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _safe_value(item)
            for key, item in value.items()
            if str(key).casefold() not in {"passphrase", "password", "secret"}
        }
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value]
    if isinstance(value, str):
        return _safe_text(value)
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float and value == value and abs(value) != float("inf"):
        return value
    return _safe_text(str(value))


def _base_report(
    args: argparse.Namespace, recording_directory: Path, temporary_directory: bool
) -> dict[str, Any]:
    channels = args.channels
    if channels is None:
        channels = 1 if args.source_factory == "alsasrc" else 2
    return {
        "schema_version": SCHEMA_VERSION,
        "spike": SPIKE_NAME,
        "step": 2,
        "production_component": False,
        "started_at_utc": utc_now(),
        "ended_at_utc": None,
        "duration_seconds": None,
        "platform": {
            "hostname": platform.node(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "configuration": {
            "source_factory": args.source_factory,
            "capture_evidence": (
                "physical_alsa"
                if args.source_factory == "alsasrc"
                else "synthetic_audiotestsrc"
            ),
            "device_id": args.device_id if args.source_factory == "alsasrc" else None,
            "capture_mode": {
                "format": args.capture_format,
                "rate_hz": args.rate_hz,
                "channels": channels,
            },
            "loopback_host": "127.0.0.1",
            "listener_port": args.port,
            "latency_ms": args.latency_ms,
            "bitrate_sequence_bps": list(BITRATE_SEQUENCE_BPS),
            "recording_directory": str(recording_directory),
            "recording_directory_temporary": temporary_directory,
            "storage_poll_seconds": args.storage_poll_seconds,
            "minimum_duration_seconds": args.minimum_duration_seconds,
            "connection_attempt_timeout_seconds": CONNECTION_ATTEMPT_TIMEOUT_SECONDS,
            "timeout_seconds": args.timeout_seconds,
            "min_buffer_delta": args.min_buffer_delta,
            "encryption": "disabled_for_loopback_evidence",
        },
        "receiver": {
            "production_component": False,
            "construction": "Gst.parse_launch disposable integration listener only",
            "pipeline": receiver_description(args.port, args.latency_ms),
            "buffers": {},
            "bus_errors": [],
        },
        "recording_output": {
            "directory": str(recording_directory),
            "explicit_directory": not temporary_directory,
            "retained_after_run": not temporary_directory,
            "cleanup_policy": (
                "retained" if not temporary_directory else "auto_cleanup"
            ),
        },
        "versions": {"gstreamer": None, "elements": {}},
        "setup_errors": [],
        "scenario_errors": [],
        "fatal_errors": [],
        "scenarios": {},
        "timeline": {"limit": TIMELINE_LIMIT, "dropped": 0, "entries": []},
        "engine_event_counts": {},
        "final_snapshot": None,
        "result": None,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-factory",
        choices=("audiotestsrc", "alsasrc"),
        default="audiotestsrc",
    )
    parser.add_argument("--device-id")
    parser.add_argument("--capture-format", choices=("S16LE", "S24LE", "S24_32LE"), default="S16LE")
    parser.add_argument("--rate-hz", type=int, choices=(44_100, 48_000, 96_000), default=48_000)
    parser.add_argument("--channels", type=int, choices=(1, 2))
    parser.add_argument("--port", type=int, default=9_113)
    parser.add_argument("--latency-ms", type=int, default=200)
    parser.add_argument("--recording-directory", type=Path)
    parser.add_argument("--storage-poll-seconds", type=float, default=5.0)
    parser.add_argument("--minimum-duration-seconds", type=float, default=35.0)
    parser.add_argument("--timeout-seconds", type=float, default=65.0)
    parser.add_argument("--min-buffer-delta", type=int, default=25)
    parser.add_argument("--output", type=Path)
    return parser


def _validated_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.source_factory == "alsasrc" and not args.device_id:
        parser.error("--device-id is required with --source-factory alsasrc")
    if not 1 <= args.port <= 65_535:
        parser.error("--port must be between 1 and 65535")
    if not 20 <= args.latency_ms <= 8_000:
        parser.error("--latency-ms must be between 20 and 8000")
    if (
        args.storage_poll_seconds <= 0
        or args.minimum_duration_seconds <= 0
        or args.timeout_seconds <= args.minimum_duration_seconds
        or args.min_buffer_delta < 1
    ):
        parser.error("timing values and buffer delta are invalid")
    return args


def _write_report(report: Mapping[str, Any], output: Path | None) -> None:
    rendered = json.dumps(_safe_value(report), indent=2, sort_keys=True) + "\n"
    if output is not None:
        output.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)


def main(
    argv: Sequence[str] | None = None,
    *,
    gst_loader: Callable[[], tuple[Any, Any]] = load_gstreamer,
) -> int:
    args = _validated_args(argv)
    temporary_directory = args.recording_directory is None
    temporary_owner = (
        tempfile.TemporaryDirectory(prefix="tinypirelay-step2-")
        if temporary_directory
        else None
    )
    recording_directory = (
        Path(temporary_owner.name)
        if temporary_owner is not None
        else args.recording_directory
    )
    assert recording_directory is not None
    report = _base_report(args, recording_directory, temporary_directory)
    started = time.monotonic()
    setup_failed = False
    try:
        recording_directory.mkdir(parents=True, exist_ok=True)
        config = build_config(args, recording_directory)
        Gst, GLib = gst_loader()
        report["versions"]["gstreamer"] = Gst.version_string()
        report["versions"]["elements"] = _element_versions(Gst, args.source_factory)
        scenario = IntegrationScenario(
            args, Gst, GLib, config, report, DestinationGate()
        )
        scenario.run()
    except (Exception, KeyboardInterrupt) as exc:
        setup_failed = True
        report["setup_errors"].append(f"{type(exc).__name__}: {_safe_text(str(exc))}")
    report["ended_at_utc"] = utc_now()
    report["duration_seconds"] = round(time.monotonic() - started, 3)
    report["result"] = assess_report(report, args.min_buffer_delta)
    try:
        _write_report(report, args.output)
    finally:
        if temporary_owner is not None:
            temporary_owner.cleanup()
    if report["result"]["status"] == "pass":
        return 0
    return 2 if setup_failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
