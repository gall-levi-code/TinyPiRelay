"""Run the TinyPiRelay Step 2 media owner on one GLib context."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import logging
import os
import signal
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from .audio_capabilities import (
    DEFAULT_AUDIO_CAPABILITIES_PATH,
    AudioCapabilities,
    AudioCapabilityError,
    load_audio_capabilities,
    validate_config_combination,
)
from .compatibility import (
    DEFAULT_EVIDENCE_PATH,
    EvidenceError,
    load_evidence,
    validate_evidence,
)
from .control_protocol import ControlError, ControlRequestError, UnixControlServer
from .device_telemetry import DeviceTelemetryCollector
from .gstreamer_engine import (
    EngineEvent,
    GStreamerEngine,
    GStreamerUnavailable,
    load_gstreamer,
)
from .media_config import ConfigError, MediaConfig, parse_config
from .media_control import MediaControlError, MediaControlHandler
from .media_runtime import MediaRuntime
from .media_state import CaptureState, ServiceState, StreamState


LOGGER = logging.getLogger("tinypirelay.media")
SHUTDOWN_TIMEOUT_SECONDS = 15
MAX_CONFIG_BYTES = 32 * 1024
DEVICE_SAMPLE_SECONDS = 2.0


class DeviceTelemetryWorker:
    """One cached, read-only sampler; slow storage never holds the media loop."""

    def __init__(self, runtime: MediaRuntime) -> None:
        self._runtime = runtime
        self._collector = DeviceTelemetryCollector()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._latest: dict[str, object] | None = None
        self._thread = threading.Thread(
            target=self._run, name="device-telemetry", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def snapshot(self) -> dict[str, object] | None:
        with self._lock:
            return copy.deepcopy(self._latest)

    def close(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            # A blocked filesystem read must not delay ordered media shutdown.
            self._thread.join(timeout=1.0)
        with self._lock:
            self._latest = None

    def _run(self) -> None:
        sequence = 0
        failed = False
        while not self._stop.is_set():
            try:
                config, revision, recording = self._runtime.device_telemetry_context()
                sampled_at = time.time()
                sample = self._collector.sample(config, recording)
                sequence += 1
                sample.update(
                    sequence=sequence,
                    sampled_at_unix=sampled_at,
                    config_revision=revision,
                )
                failed = False
            except Exception:
                # Telemetry is optional: retain control and media on collector
                # failure, discard stale values, and never log private paths.
                sample = None
                if not failed:
                    LOGGER.warning("Device telemetry is unavailable.")
                failed = True
            with self._lock:
                if self._stop.is_set():
                    return
                self._latest = sample
            if self._stop.wait(DEVICE_SAMPLE_SECONDS):
                return


class _GLibTimer:
    def __init__(self, source: Any) -> None:
        self._source = source

    def cancel(self) -> None:
        source, self._source = self._source, None
        if source is not None:
            source.destroy()


class GLibScheduler:
    """Attach one-shot controller timers to the media owner's GLib context."""

    def __init__(self, GLib: Any, context: Any) -> None:
        self._GLib = GLib
        self._context = context

    def call_later(
        self, delay_seconds: float, callback: Callable[[], None]
    ) -> _GLibTimer:
        milliseconds = max(1, int(round(delay_seconds * 1000)))
        source = self._GLib.timeout_source_new(milliseconds)

        def run(*_unused: object) -> bool:
            callback()
            return False

        source.set_callback(run)
        source.attach(self._context)
        return _GLibTimer(source)


def config_revision(path: Path) -> str:
    """Return a stable, reducer-safe identity for the exact config bytes."""

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument(
        "--evidence", type=Path, default=DEFAULT_EVIDENCE_PATH
    )
    parser.add_argument(
        "--audio-capabilities",
        type=Path,
        default=DEFAULT_AUDIO_CAPABILITIES_PATH,
    )
    parser.add_argument(
        "--control-socket",
        type=Path,
        help="enable the permission-restricted Step 3 local control socket",
    )
    parser.add_argument(
        "--source-factory",
        choices=("alsasrc", "audiotestsrc"),
        default="alsasrc",
        help="audiotestsrc is for controlled integration evidence only",
    )
    parser.add_argument(
        "--record-on-start",
        action="store_true",
        help="start recording after capture (primarily an integration aid)",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )
    return parser


def _validated_inputs(
    config_path: Path,
    evidence_path: Path,
    audio_capabilities_path: Path = DEFAULT_AUDIO_CAPABILITIES_PATH,
) -> tuple[MediaConfig, str]:
    evidence = load_evidence(evidence_path)
    capabilities = validate_evidence(evidence)
    try:
        payload, config_stat = _read_config_bytes(config_path)
        config = parse_config(payload.decode("utf-8"), capabilities)
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"cannot read configuration {config_path}: {exc}") from exc
    if (
        config.stream.passphrase is not None
        and os.name == "posix"
        and stat.S_IMODE(config_stat.st_mode) & 0o077
    ):
        raise ConfigError(
            "configuration containing an SRT passphrase must not be accessible "
            "to group or other users"
        )
    audio_capabilities = load_audio_capabilities(
        audio_capabilities_path, capabilities
    )
    validate_config_combination(config, audio_capabilities)
    revision = hashlib.sha256(payload).hexdigest()
    return config, revision


def _read_config_bytes(path: Path) -> tuple[bytes, os.stat_result]:
    """Read one bounded regular config without following a replaced symlink."""

    try:
        before = os.lstat(path)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise ConfigError("configuration must be a regular file, not a symlink")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or (
                before.st_dev,
                before.st_ino,
            ) != (opened.st_dev, opened.st_ino):
                raise ConfigError("configuration changed while it was being opened")
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                payload = handle.read(MAX_CONFIG_BYTES + 1)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    except ConfigError:
        raise
    except OSError as exc:
        raise ConfigError(f"cannot safely open configuration {path}: {exc}") from exc
    if len(payload) > MAX_CONFIG_BYTES:
        raise ConfigError(f"configuration exceeds {MAX_CONFIG_BYTES} bytes")
    return payload, opened


def run_service(
    config: MediaConfig,
    revision: str,
    *,
    source_factory: str = "alsasrc",
    record_on_start: bool = False,
    config_path: Path | None = None,
    approved_representations: Sequence[str] = (),
    audio_capabilities: AudioCapabilities | None = None,
    control_socket: Path | None = None,
    gst_loader: Callable[[], tuple[Any, Any]] = load_gstreamer,
    engine_factory: Callable[..., Any] = GStreamerEngine,
    control_server_factory: Callable[..., Any] = UnixControlServer,
) -> int:
    """Run until an ordered shutdown completes or a fatal startup error occurs."""

    Gst, GLib = gst_loader()
    context = GLib.MainContext.default()
    loop = GLib.MainLoop.new(context, False)
    runtime_box: dict[str, MediaRuntime] = {}
    control_server: Any = None
    device_worker: DeviceTelemetryWorker | None = None
    shutting_down = False
    exit_code = 0
    shutdown_timeout: Any = None

    def request_shutdown(reason: str) -> None:
        nonlocal shutting_down, shutdown_timeout
        if shutting_down:
            return
        shutting_down = True
        LOGGER.info("ordered shutdown requested: %s", reason)

        def timed_out(*_unused: object) -> bool:
            nonlocal exit_code
            exit_code = 1
            LOGGER.error("media shutdown exceeded %s seconds", SHUTDOWN_TIMEOUT_SECONDS)
            loop.quit()
            return False

        shutdown_timeout = GLib.timeout_source_new_seconds(SHUTDOWN_TIMEOUT_SECONDS)
        shutdown_timeout.set_callback(timed_out)
        shutdown_timeout.attach(context)
        runtime_box["runtime"].shutdown()

    def on_engine_event(event: EngineEvent) -> None:
        nonlocal exit_code
        runtime = runtime_box["runtime"]
        runtime.handle_engine_event(event)
        state = runtime.state

        if event.kind == "capture-started" and state.capture is CaptureState.RUNNING:
            if config.stream.enabled and state.stream is StreamState.STOPPED:
                runtime.start_stream()
            if record_on_start:
                runtime.start_recording()
        elif event.kind == "shutdown-complete":
            if shutdown_timeout is not None:
                shutdown_timeout.destroy()
            loop.quit()
        elif event.kind == "error" and event.scope == "capture":
            if control_socket is None:
                exit_code = 1
                request_shutdown("capture failure")
            else:
                LOGGER.warning("Audio is unavailable; the control service remains ready.")

        LOGGER.debug(
            "engine event kind=%s scope=%s generation=%s",
            event.kind,
            event.scope,
            event.generation,
        )

    engine = engine_factory(
        config,
        on_engine_event,
        source_factory=source_factory,
        gst_loader=lambda: (Gst, GLib),
    )
    # Control worker commands must reach GLib even before any capture attempt.
    engine.bind_owner()
    runtime = MediaRuntime(
        config,
        revision,
        engine,
        scheduler=GLibScheduler(GLib, context),
    )
    runtime_box["runtime"] = runtime

    if control_socket is not None:
        if config_path is None or audio_capabilities is None:
            raise ValueError(
                "config_path and audio_capabilities are required with control_socket"
            )
        device_worker = DeviceTelemetryWorker(runtime)
        control = MediaControlHandler(
            runtime,
            config,
            config_path,
            revision,
            approved_representations,
            audio_capabilities,
            device_telemetry=device_worker.snapshot,
        )

        def handle_control(
            operation: str, arguments: dict[str, Any]
        ) -> dict[str, object]:
            try:
                return control(operation, arguments)
            except MediaControlError as exc:
                raise ControlRequestError(exc.code, exc.message) from exc

        control_server = control_server_factory(
            control_socket,
            handle_control,
            directory_mode=0o750,
            socket_mode=0o660,
        )
        control_server.start()

    def bootstrap(*_unused: object) -> bool:
        nonlocal exit_code
        if config.capture.mode is None:
            LOGGER.info("Audio setup is pending; the control service is ready.")
            return False
        decision = runtime.start_capture()
        if not decision.ok or runtime.state.capture is CaptureState.FAILED:
            message = (
                decision.error.message
                if decision.error is not None
                else "GStreamer rejected capture startup"
            )
            LOGGER.error("capture start failed: %s", message)
            if control_socket is None:
                exit_code = 1
                request_shutdown("capture start failure")
        return False

    def signal_shutdown(signum: int, _frame: object) -> None:
        source = GLib.idle_source_new()

        def request(*_unused: object) -> bool:
            request_shutdown(signal.Signals(signum).name)
            return False

        source.set_callback(request)
        source.attach(context)

    previous_handlers: dict[int, Any] = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.signal(signum, signal_shutdown)

    source = GLib.idle_source_new()
    source.set_callback(bootstrap)
    source.attach(context)
    try:
        if device_worker is not None:
            device_worker.start()
        loop.run()
    except KeyboardInterrupt:
        request_shutdown("KeyboardInterrupt")
        loop.run()
    finally:
        if device_worker is not None:
            device_worker.close()
        if control_server is not None:
            control_server.close()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)

    snapshot = runtime.snapshot()
    LOGGER.info(
        "media service stopped: %s",
        json.dumps(snapshot["state"], separators=(",", ":"), sort_keys=True),
    )
    if runtime.state.service is not ServiceState.STOPPED:
        return 1
    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        evidence = load_evidence(args.evidence)
        capabilities = validate_evidence(evidence)
        config, revision = _validated_inputs(
            args.config, args.evidence, args.audio_capabilities
        )
        audio_capabilities = load_audio_capabilities(
            args.audio_capabilities, capabilities
        )
        return run_service(
            config,
            revision,
            source_factory=args.source_factory,
            record_on_start=args.record_on_start,
            config_path=args.config,
            approved_representations=capabilities,
            audio_capabilities=audio_capabilities,
            control_socket=args.control_socket,
        )
    except (
        AudioCapabilityError,
        ConfigError,
        ControlError,
        EvidenceError,
        GStreamerUnavailable,
        OSError,
        ValueError,
    ) as exc:
        print(f"media service cannot start: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
