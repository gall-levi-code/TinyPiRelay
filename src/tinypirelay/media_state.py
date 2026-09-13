"""Pure, serialized state transitions for the TinyPiRelay media owner."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Mapping


RETRY_DELAYS_SECONDS = (1, 2, 4, 8, 16, 30)
OPUS_BITRATES_BPS = frozenset((64_000, 96_000, 128_000, 192_000))
OPUS_REPRESENTATION = "opus_128k_48000_stereo_matroska"
SUPPORTED_REPRESENTATIONS = frozenset(
    (
        "pcm_s16le_48000_stereo_matroska",
        "flac_48000_stereo_matroska",
        OPUS_REPRESENTATION,
    )
)
STORAGE_STATUSES = frozenset(("safe", "unsafe", "unavailable", "unverified"))
MAX_CONNECTION_STATS = 64


class ServiceState(StrEnum):
    RUNNING = "running"
    SHUTTING_DOWN = "shutting_down"
    STOPPED = "stopped"


class CaptureState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    FAILED = "failed"


class StreamState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    FAILED = "failed"


class RecordingState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    BLOCKED = "blocked"
    FAILED = "failed"


class ConnectionState(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RETRY_WAIT = "retry_wait"
    FAILED = "failed"


@dataclass(frozen=True)
class ErrorInfo:
    code: str
    message: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "message", _redact_transport_secrets(self.message))

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class MediaState:
    service: ServiceState = ServiceState.RUNNING

    capture: CaptureState = CaptureState.STOPPED
    capture_generation: int = 0
    capture_config_revision: str | None = None
    capture_error: ErrorInfo | None = None

    stream: StreamState = StreamState.STOPPED
    stream_generation: int = 0
    stream_config_revision: str | None = None
    representation_id: str | None = None
    opus_bitrate_bps: int | None = None
    pending_opus_bitrate_bps: int | None = None
    stream_error: ErrorInfo | None = None

    connection: ConnectionState = ConnectionState.DISCONNECTED
    reconnect_count: int = 0
    consecutive_connection_failures: int = 0
    reconnect_delay_seconds: int | None = None
    connection_error: ErrorInfo | None = None
    connection_stats: tuple[tuple[str, bool | int | float | str | None], ...] = ()

    recording: RecordingState = RecordingState.STOPPED
    recording_generation: int = 0
    recording_config_revision: str | None = None
    recording_stop_target: RecordingState | None = None
    recording_warning: ErrorInfo | None = None
    recording_error: ErrorInfo | None = None
    recording_current_file: str | None = None
    recording_last_finalized_file: str | None = None
    recording_rotation_pending: bool = False

    monitoring_error: ErrorInfo | None = None

    def to_dict(self) -> dict[str, object]:
        """Return a bounded JSON-safe snapshot that never contains transport secrets."""

        return {
            "schema_version": 1,
            "service": self.service.value,
            "capture": {
                "state": self.capture.value,
                "generation": self.capture_generation,
                "config_revision": self.capture_config_revision,
                "last_error": _error_dict(self.capture_error),
            },
            "stream": {
                "state": self.stream.value,
                "generation": self.stream_generation,
                "config_revision": self.stream_config_revision,
                "representation_id": self.representation_id,
                "opus_bitrate_bps": self.opus_bitrate_bps,
                "pending_opus_bitrate_bps": self.pending_opus_bitrate_bps,
                "last_error": _error_dict(self.stream_error),
            },
            "connection": {
                "state": self.connection.value,
                "reconnect_count": self.reconnect_count,
                "consecutive_failures": self.consecutive_connection_failures,
                "next_retry_seconds": self.reconnect_delay_seconds,
                "statistics": dict(self.connection_stats),
                "last_error": _error_dict(self.connection_error),
            },
            "recording": {
                "state": self.recording.value,
                "generation": self.recording_generation,
                "config_revision": self.recording_config_revision,
                "stop_target": (
                    self.recording_stop_target.value
                    if self.recording_stop_target is not None
                    else None
                ),
                "rotation_pending": self.recording_rotation_pending,
                "current_file": self.recording_current_file,
                "last_finalized_file": self.recording_last_finalized_file,
                "warning": _error_dict(self.recording_warning),
                "last_error": _error_dict(self.recording_error),
            },
            "monitoring": {"last_error": _error_dict(self.monitoring_error)},
        }


@dataclass(frozen=True)
class Decision:
    state: MediaState
    changed: bool = False
    effects: tuple[str, ...] = ()
    error: ErrorInfo | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "changed": self.changed,
            "effects": list(self.effects),
            "error": _error_dict(self.error),
            "state": self.state.to_dict(),
        }


class _PayloadError(ValueError):
    pass


def classify_storage(
    total_bytes: int | None,
    used_bytes: int | None,
    *,
    available: bool = True,
    writable: bool = True,
) -> str:
    """Classify the destination filesystem; exactly 90% used is unsafe."""

    if not available or not writable:
        return "unavailable"
    if (
        not isinstance(total_bytes, int)
        or isinstance(total_bytes, bool)
        or total_bytes <= 0
        or not isinstance(used_bytes, int)
        or isinstance(used_bytes, bool)
        or used_bytes < 0
        or used_bytes > total_bytes
    ):
        return "unverified"
    return "unsafe" if used_bytes * 10 >= total_bytes * 9 else "safe"


def reduce(state: MediaState, event: str, **payload: object) -> Decision:
    """Apply one command or backend event without performing any side effect."""

    if not isinstance(event, str) or not event:
        return _reject(state, "invalid_command", "event must be a non-empty string")

    if state.service is ServiceState.STOPPED:
        if event == "service.shutdown" or event in _BACKEND_EVENTS:
            return Decision(state)
        return _reject(state, "service_shutting_down", "media service is stopped")

    if (
        state.service is ServiceState.SHUTTING_DOWN
        and event != "service.shutdown"
        and event not in _BACKEND_EVENTS
    ):
        return _reject(
            state, "service_shutting_down", "media service is shutting down"
        )

    try:
        decision = _reduce_event(state, event, payload)
    except _PayloadError as exc:
        decision = _reject(state, "invalid_command", str(exc))
    return _advance_shutdown(decision)


def _reduce_event(
    state: MediaState, event: str, payload: Mapping[str, object]
) -> Decision:
    if event == "service.shutdown":
        _only(payload)
        if state.service is ServiceState.SHUTTING_DOWN:
            return Decision(state)
        return Decision(replace(state, service=ServiceState.SHUTTING_DOWN), True)

    if event == "capture.start":
        _only(payload, "config_revision")
        revision = _revision(payload)
        if state.capture in (CaptureState.STARTING, CaptureState.RUNNING):
            if state.capture_config_revision == revision:
                return Decision(state)
            return _reject(
                state,
                "reconfigure_required",
                "capture configuration differs from the active configuration",
            )
        if state.capture is CaptureState.STOPPING:
            return _busy(state, "capture")
        generation = state.capture_generation + 1
        return Decision(
            replace(
                state,
                capture=CaptureState.STARTING,
                capture_generation=generation,
                capture_config_revision=revision,
                capture_error=None,
            ),
            True,
            ("capture.start",),
        )

    if event == "capture.stop":
        _only(payload)
        if state.capture in (CaptureState.STOPPED, CaptureState.STOPPING):
            return Decision(state)
        if state.capture is CaptureState.STARTING:
            return _busy(state, "capture")
        if state.stream in (
            StreamState.STARTING,
            StreamState.RUNNING,
            StreamState.STOPPING,
        ) or state.recording in (
            RecordingState.STARTING,
            RecordingState.RUNNING,
            RecordingState.STOPPING,
        ):
            return _reject(
                state,
                "branch_active",
                "stream or recording must stop before capture",
            )
        return Decision(
            replace(state, capture=CaptureState.STOPPING),
            True,
            ("capture.stop",),
        )

    if event in ("capture.started", "capture.stopped"):
        _only(payload, "generation")
        generation = _integer(payload, "generation", minimum=1)
        if generation != state.capture_generation:
            return Decision(state)
        expected = (
            CaptureState.STARTING if event.endswith("started") else CaptureState.STOPPING
        )
        target = (
            CaptureState.RUNNING if event.endswith("started") else CaptureState.STOPPED
        )
        if state.capture is target:
            return Decision(state)
        if state.capture is not expected:
            return _invalid_transition(state, event)
        return Decision(
            replace(
                state,
                capture=target,
                capture_config_revision=(
                    None if target is CaptureState.STOPPED else state.capture_config_revision
                ),
            ),
            True,
        )

    if event in ("capture.failed", "capture.device_lost"):
        _only(payload, "generation", "code", "message")
        generation = _integer(payload, "generation", minimum=1)
        if generation != state.capture_generation:
            return Decision(state)
        if state.capture in (CaptureState.STOPPED, CaptureState.FAILED):
            return Decision(state)
        error = _failure(
            payload,
            "device_lost" if event.endswith("device_lost") else "capture_failed",
            "capture device was lost" if event.endswith("device_lost") else "capture failed",
        )
        return _fail_capture(state, error)

    if event == "stream.start":
        _only(
            payload,
            "config_revision",
            "representation_id",
            "opus_bitrate_bps",
        )
        revision = _revision(payload)
        representation = _text(payload, "representation_id", 128)
        if representation not in SUPPORTED_REPRESENTATIONS:
            return _reject(
                state,
                "unsupported_representation",
                "representation is not enabled by the Step 1 compatibility gate",
            )
        bitrate: int | None = None
        if representation == OPUS_REPRESENTATION:
            bitrate = _integer(payload, "opus_bitrate_bps", minimum=1)
            if bitrate not in OPUS_BITRATES_BPS:
                return _reject(
                    state, "unsupported_bitrate", "unsupported Opus bitrate preset"
                )
        elif "opus_bitrate_bps" in payload and payload["opus_bitrate_bps"] is not None:
            _integer(payload, "opus_bitrate_bps", minimum=1)

        if state.capture is not CaptureState.RUNNING:
            return _reject(
                state, "capture_not_running", "capture must be running before streaming"
            )
        if state.stream in (StreamState.STARTING, StreamState.RUNNING):
            if state.stream_config_revision == revision:
                return Decision(state)
            return _reject(
                state,
                "reconfigure_required",
                "stream configuration differs from the active configuration",
            )
        if state.stream is StreamState.STOPPING:
            return _busy(state, "stream")
        generation = state.stream_generation + 1
        return Decision(
            replace(
                state,
                stream=StreamState.STARTING,
                stream_generation=generation,
                stream_config_revision=revision,
                representation_id=representation,
                opus_bitrate_bps=bitrate,
                pending_opus_bitrate_bps=None,
                stream_error=None,
                connection=ConnectionState.CONNECTING,
                reconnect_count=0,
                consecutive_connection_failures=0,
                reconnect_delay_seconds=None,
                connection_error=None,
                connection_stats=(),
            ),
            True,
            ("stream.start",),
        )

    if event == "stream.stop":
        _only(payload)
        if state.stream in (StreamState.STOPPED, StreamState.STOPPING):
            return Decision(state)
        if state.stream is StreamState.STARTING:
            return _busy(state, "stream")
        effects = _cancel_retry_effect(state) + ("stream.stop",)
        return Decision(
            replace(
                state,
                stream=StreamState.STOPPING,
                pending_opus_bitrate_bps=None,
                connection=ConnectionState.DISCONNECTED,
                consecutive_connection_failures=0,
                reconnect_delay_seconds=None,
            ),
            True,
            effects,
        )

    if event in ("stream.started", "stream.stopped"):
        _only(payload, "generation")
        generation = _integer(payload, "generation", minimum=1)
        if generation != state.stream_generation:
            return Decision(state)
        expected = StreamState.STARTING if event.endswith("started") else StreamState.STOPPING
        target = StreamState.RUNNING if event.endswith("started") else StreamState.STOPPED
        if state.stream is target:
            return Decision(state)
        if state.stream is not expected:
            return _invalid_transition(state, event)
        changes: dict[str, Any] = {"stream": target}
        if target is StreamState.STOPPED:
            changes.update(
                stream_config_revision=None,
                representation_id=None,
                opus_bitrate_bps=None,
                pending_opus_bitrate_bps=None,
                connection=ConnectionState.DISCONNECTED,
                consecutive_connection_failures=0,
                reconnect_delay_seconds=None,
            )
        return Decision(replace(state, **changes), True)

    if event == "stream.failed":
        _only(payload, "generation", "code", "message")
        generation = _integer(payload, "generation", minimum=1)
        if generation != state.stream_generation:
            return Decision(state)
        if state.stream in (StreamState.STOPPED, StreamState.FAILED):
            return Decision(state)
        error = _failure(payload, "stream_failed", "stream branch failed")
        return Decision(
            replace(
                state,
                stream=StreamState.FAILED,
                stream_error=error,
                pending_opus_bitrate_bps=None,
                connection=ConnectionState.DISCONNECTED,
                reconnect_delay_seconds=None,
            ),
            True,
            _cancel_retry_effect(state),
        )

    if event == "stream.set_opus_bitrate":
        _only(payload, "bitrate_bps")
        bitrate = _integer(payload, "bitrate_bps", minimum=1)
        if bitrate not in OPUS_BITRATES_BPS:
            return _reject(
                state, "unsupported_bitrate", "unsupported Opus bitrate preset"
            )
        if state.stream is not StreamState.RUNNING:
            return _reject(state, "busy", "stream is not running")
        if state.representation_id != OPUS_REPRESENTATION:
            return _reject(
                state,
                "bitrate_not_applicable",
                "runtime bitrate applies only to the Opus representation",
            )
        if state.opus_bitrate_bps == bitrate or state.pending_opus_bitrate_bps == bitrate:
            return Decision(state)
        if state.pending_opus_bitrate_bps is not None:
            return _reject(
                state,
                "bitrate_change_in_progress",
                "another Opus bitrate change is pending",
            )
        return Decision(
            replace(state, pending_opus_bitrate_bps=bitrate),
            True,
            ("stream.set_opus_bitrate",),
        )

    if event in ("stream.opus_bitrate_applied", "stream.opus_bitrate_failed"):
        _only(payload, "generation", "bitrate_bps", "code", "message")
        generation = _integer(payload, "generation", minimum=1)
        bitrate = _integer(payload, "bitrate_bps", minimum=1)
        if generation != state.stream_generation:
            return Decision(state)
        if (
            state.stream is not StreamState.RUNNING
            or state.pending_opus_bitrate_bps != bitrate
        ):
            return _invalid_transition(state, event)
        if event.endswith("applied"):
            return Decision(
                replace(
                    state,
                    opus_bitrate_bps=bitrate,
                    pending_opus_bitrate_bps=None,
                ),
                True,
            )
        error = _failure(payload, "opus_bitrate_failed", "Opus bitrate change failed")
        return Decision(
            replace(
                state,
                pending_opus_bitrate_bps=None,
                stream_error=error,
            ),
            True,
        )

    if event == "connection.connected":
        _only(payload, "generation")
        generation = _integer(payload, "generation", minimum=1)
        if generation != state.stream_generation:
            return Decision(state)
        if state.stream not in (StreamState.STARTING, StreamState.RUNNING):
            return Decision(state)
        if state.connection is ConnectionState.CONNECTED:
            return Decision(state)
        if state.connection not in (
            ConnectionState.CONNECTING,
            ConnectionState.RETRY_WAIT,
        ):
            return _invalid_transition(state, event)
        effects = _cancel_retry_effect(state)
        return Decision(
            replace(
                state,
                connection=ConnectionState.CONNECTED,
                consecutive_connection_failures=0,
                reconnect_delay_seconds=None,
            ),
            True,
            effects,
        )

    if event in ("connection.lost", "connection.failed"):
        allowed = ("generation", "code", "message")
        if event.endswith("failed"):
            allowed += ("retryable",)
        _only(payload, *allowed)
        generation = _integer(payload, "generation", minimum=1)
        if generation != state.stream_generation:
            return Decision(state)
        if state.stream not in (StreamState.STARTING, StreamState.RUNNING):
            return Decision(state)
        retryable = (
            _boolean(payload, "retryable", default=True)
            if event.endswith("failed")
            else True
        )
        error = _failure(payload, "srt_connection_lost", "SRT connection was lost")
        if not retryable:
            return Decision(
                replace(
                    state,
                    connection=ConnectionState.FAILED,
                    reconnect_delay_seconds=None,
                    connection_error=error,
                ),
                True,
                _cancel_retry_effect(state),
            )
        if state.connection is ConnectionState.RETRY_WAIT:
            new_state = replace(state, connection_error=error)
            return Decision(new_state, new_state != state)
        if state.connection in (ConnectionState.DISCONNECTED, ConnectionState.FAILED):
            return Decision(state)
        delay = _retry_delay(state.consecutive_connection_failures)
        new_state = replace(
            state,
            connection=ConnectionState.RETRY_WAIT,
            reconnect_delay_seconds=delay,
            connection_error=error,
        )
        if new_state == state:
            return Decision(state)
        return Decision(new_state, True, ("connection.schedule_retry",))

    if event == "connection.retry_due":
        _only(payload, "generation")
        generation = _integer(payload, "generation", minimum=1)
        if generation != state.stream_generation:
            return Decision(state)
        if state.connection is not ConnectionState.RETRY_WAIT or state.stream not in (
            StreamState.STARTING,
            StreamState.RUNNING,
        ):
            return Decision(state)
        return Decision(
            replace(
                state,
                connection=ConnectionState.CONNECTING,
                reconnect_count=state.reconnect_count + 1,
                consecutive_connection_failures=(
                    state.consecutive_connection_failures + 1
                ),
                reconnect_delay_seconds=None,
            ),
            True,
            ("connection.connect",),
        )

    if event == "connection.stats":
        _only(payload, "generation", "stats")
        generation = _integer(payload, "generation", minimum=1)
        if generation != state.stream_generation or state.stream not in (
            StreamState.STARTING,
            StreamState.RUNNING,
        ):
            return Decision(state)
        stats = _statistics(payload.get("stats"))
        if stats == state.connection_stats:
            return Decision(state)
        return Decision(replace(state, connection_stats=stats), True)

    if event == "recording.start":
        _only(payload, "config_revision", "storage_status")
        revision = _revision(payload)
        storage_status = _storage_status(payload)
        if storage_status != "safe":
            return _block_recording(state, storage_status)
        if state.capture is not CaptureState.RUNNING:
            return _reject(
                state, "capture_not_running", "capture must be running before recording"
            )
        if state.recording in (RecordingState.STARTING, RecordingState.RUNNING):
            if state.recording_config_revision == revision:
                return Decision(state)
            return _reject(
                state,
                "reconfigure_required",
                "recording configuration differs from the active configuration",
            )
        if state.recording is RecordingState.STOPPING:
            return _busy(state, "recording")
        generation = state.recording_generation + 1
        return Decision(
            replace(
                state,
                recording=RecordingState.STARTING,
                recording_generation=generation,
                recording_config_revision=revision,
                recording_stop_target=None,
                recording_warning=None,
                recording_error=None,
                recording_current_file=None,
                recording_rotation_pending=False,
            ),
            True,
            ("recording.start",),
        )

    if event == "recording.stop":
        _only(payload)
        if state.recording in (RecordingState.STOPPED, RecordingState.BLOCKED):
            return Decision(state)
        if state.recording is RecordingState.STOPPING:
            return Decision(state)
        if state.recording is RecordingState.STARTING:
            return _busy(state, "recording")
        if state.recording is RecordingState.FAILED:
            return Decision(
                replace(
                    state,
                    recording=RecordingState.STOPPED,
                    recording_config_revision=None,
                    recording_stop_target=None,
                    recording_current_file=None,
                    recording_rotation_pending=False,
                ),
                True,
            )
        return Decision(
            replace(
                state,
                recording=RecordingState.STOPPING,
                recording_stop_target=RecordingState.STOPPED,
                recording_rotation_pending=False,
            ),
            True,
            ("recording.stop",),
        )

    if event == "recording.rotate":
        _only(payload)
        if state.recording is not RecordingState.RUNNING:
            return _busy(state, "recording")
        if state.recording_rotation_pending:
            return Decision(state)
        return Decision(
            replace(state, recording_rotation_pending=True),
            True,
            ("recording.rotate",),
        )

    if event in ("recording.started", "recording.stopped"):
        _only(payload, "generation")
        generation = _integer(payload, "generation", minimum=1)
        if generation != state.recording_generation:
            return Decision(state)
        if event.endswith("started"):
            if state.recording is RecordingState.RUNNING:
                return Decision(state)
            if state.recording is not RecordingState.STARTING:
                return _invalid_transition(state, event)
            return Decision(replace(state, recording=RecordingState.RUNNING), True)
        if state.recording in (
            RecordingState.STOPPED,
            RecordingState.BLOCKED,
            RecordingState.FAILED,
        ):
            return Decision(state)
        if state.recording is not RecordingState.STOPPING:
            return _invalid_transition(state, event)
        target = state.recording_stop_target or RecordingState.STOPPED
        return Decision(
            replace(
                state,
                recording=target,
                recording_config_revision=None,
                recording_stop_target=None,
                recording_current_file=None,
                recording_rotation_pending=False,
            ),
            True,
        )

    if event == "recording.failed":
        _only(payload, "generation", "code", "message")
        generation = _integer(payload, "generation", minimum=1)
        if generation != state.recording_generation:
            return Decision(state)
        if state.recording in (
            RecordingState.STOPPED,
            RecordingState.BLOCKED,
            RecordingState.FAILED,
        ):
            return Decision(state)
        error = _failure(payload, "recording_io_failed", "recording I/O failed")
        if state.recording is RecordingState.STARTING:
            return Decision(
                replace(
                    state,
                    recording=RecordingState.FAILED,
                    recording_config_revision=None,
                    recording_error=error,
                ),
                True,
            )
        return Decision(
            replace(
                state,
                recording=RecordingState.STOPPING,
                recording_stop_target=RecordingState.FAILED,
                recording_error=error,
                recording_rotation_pending=False,
            ),
            True,
            (() if state.recording is RecordingState.STOPPING else ("recording.stop",)),
        )

    if event == "recording.storage_checked":
        _only(payload, "generation", "storage_status")
        generation = _integer(payload, "generation", minimum=0)
        if generation != state.recording_generation:
            return Decision(state)
        storage_status = _storage_status(payload)
        if storage_status == "safe":
            if state.recording is not RecordingState.BLOCKED:
                return Decision(state)
            return Decision(
                replace(
                    state,
                    recording=RecordingState.STOPPED,
                    recording_warning=None,
                    recording_error=None,
                ),
                True,
            )
        return _block_recording(state, storage_status)

    if event in ("recording.fragment_opened", "recording.fragment_closed"):
        _only(payload, "generation", "path")
        generation = _integer(payload, "generation", minimum=1)
        path = _text(payload, "path", 4096)
        if generation != state.recording_generation:
            return Decision(state)
        if state.recording not in (RecordingState.RUNNING, RecordingState.STOPPING):
            return _invalid_transition(state, event)
        if event.endswith("opened"):
            if state.recording_current_file == path:
                return Decision(state)
            return Decision(replace(state, recording_current_file=path), True)
        if state.recording_last_finalized_file == path and state.recording_current_file is None:
            return Decision(state)
        return Decision(
            replace(
                state,
                recording_last_finalized_file=path,
                recording_current_file=(
                    None if state.recording_current_file == path else state.recording_current_file
                ),
            ),
            True,
        )

    if event == "recording.rotation_completed":
        _only(payload, "generation")
        generation = _integer(payload, "generation", minimum=1)
        if generation != state.recording_generation:
            return Decision(state)
        if not state.recording_rotation_pending:
            return Decision(state)
        return Decision(replace(state, recording_rotation_pending=False), True)

    if event == "monitoring.failed":
        _only(payload, "code", "message")
        error = _failure(payload, "monitoring_failed", "monitoring branch failed")
        if error == state.monitoring_error:
            return Decision(state)
        return Decision(replace(state, monitoring_error=error), True)

    raise _PayloadError(f"unknown event: {event}")


def _fail_capture(state: MediaState, error: ErrorInfo) -> Decision:
    effects: tuple[str, ...] = ()
    changes: dict[str, Any] = {
        "capture": CaptureState.FAILED,
        "capture_error": error,
    }
    if state.stream in (StreamState.STARTING, StreamState.RUNNING, StreamState.STOPPING):
        effects += _cancel_retry_effect(state)
        effects += ("stream.stop",)
        changes.update(
            stream=StreamState.FAILED,
            stream_error=ErrorInfo("capture_failed", "stream lost its capture source"),
            pending_opus_bitrate_bps=None,
            connection=ConnectionState.DISCONNECTED,
            reconnect_delay_seconds=None,
        )
    if state.recording in (RecordingState.STARTING, RecordingState.RUNNING):
        effects += ("recording.stop",)
        changes.update(
            recording=RecordingState.STOPPING,
            recording_stop_target=RecordingState.FAILED,
            recording_error=ErrorInfo(
                "capture_failed", "recording lost its capture source"
            ),
            recording_rotation_pending=False,
        )
    return Decision(replace(state, **changes), True, _unique_effects(effects))


def _block_recording(state: MediaState, status: str) -> Decision:
    error = _storage_error(status)
    if state.recording in (RecordingState.STARTING, RecordingState.RUNNING):
        return Decision(
            replace(
                state,
                recording=RecordingState.STOPPING,
                recording_stop_target=RecordingState.BLOCKED,
                recording_warning=error,
                recording_error=error,
                recording_rotation_pending=False,
            ),
            True,
            ("recording.stop",),
            error,
        )
    if state.recording is RecordingState.STOPPING:
        new_state = replace(
            state,
            recording_stop_target=RecordingState.BLOCKED,
            recording_warning=error,
            recording_error=error,
        )
        return Decision(new_state, new_state != state, error=error)
    if state.recording is RecordingState.FAILED:
        return Decision(state, error=error)
    new_state = replace(
        state,
        recording=RecordingState.BLOCKED,
        recording_config_revision=None,
        recording_stop_target=None,
        recording_warning=error,
        recording_error=error,
        recording_current_file=None,
        recording_rotation_pending=False,
    )
    return Decision(new_state, new_state != state, error=error)


def _advance_shutdown(decision: Decision) -> Decision:
    state = decision.state
    if state.service is not ServiceState.SHUTTING_DOWN or decision.error is not None:
        return decision

    effects = decision.effects
    changed = decision.changed
    if state.recording in (RecordingState.STARTING, RecordingState.RUNNING):
        state = replace(
            state,
            recording=RecordingState.STOPPING,
            recording_stop_target=RecordingState.STOPPED,
            recording_rotation_pending=False,
        )
        effects += ("recording.stop",)
        changed = True
    if state.recording is RecordingState.STOPPING:
        return Decision(state, changed, _unique_effects(effects))

    if state.stream in (StreamState.STARTING, StreamState.RUNNING):
        effects += _cancel_retry_effect(state) + ("stream.stop",)
        state = replace(
            state,
            stream=StreamState.STOPPING,
            pending_opus_bitrate_bps=None,
            connection=ConnectionState.DISCONNECTED,
            reconnect_delay_seconds=None,
        )
        changed = True
    if state.stream is StreamState.STOPPING:
        return Decision(state, changed, _unique_effects(effects))

    if state.capture in (CaptureState.STARTING, CaptureState.RUNNING):
        state = replace(state, capture=CaptureState.STOPPING)
        effects += ("capture.stop",)
        changed = True
    if state.capture is CaptureState.STOPPING:
        return Decision(state, changed, _unique_effects(effects))

    state = replace(state, service=ServiceState.STOPPED)
    effects += ("service.stop",)
    return Decision(state, True, _unique_effects(effects))


def _storage_error(status: str) -> ErrorInfo:
    return {
        "unsafe": ErrorInfo(
            "storage_unsafe", "recording destination is at least 90% used"
        ),
        "unavailable": ErrorInfo(
            "destination_unavailable",
            "recording destination is missing, unmounted, or read-only",
        ),
        "unverified": ErrorInfo(
            "storage_unverified", "recording destination safety is unverified"
        ),
    }[status]


def _storage_status(payload: Mapping[str, object]) -> str:
    value = payload.get("storage_status")
    if value not in STORAGE_STATUSES:
        raise _PayloadError(
            "storage_status must be safe, unsafe, unavailable, or unverified"
        )
    assert isinstance(value, str)
    return value


def _retry_delay(consecutive_failures: int) -> int:
    return RETRY_DELAYS_SECONDS[
        min(consecutive_failures, len(RETRY_DELAYS_SECONDS) - 1)
    ]


def _cancel_retry_effect(state: MediaState) -> tuple[str, ...]:
    return (
        ("connection.cancel_retry",)
        if state.connection is ConnectionState.RETRY_WAIT
        else ()
    )


def _statistics(value: object) -> tuple[tuple[str, bool | int | float | str | None], ...]:
    if not isinstance(value, Mapping) or len(value) > MAX_CONNECTION_STATS:
        raise _PayloadError("stats must be a mapping with at most 64 entries")
    items: list[tuple[str, bool | int | float | str | None]] = []
    for key, item in value.items():
        if not isinstance(key, str) or not key or len(key) > 64:
            raise _PayloadError("stat names must be non-empty strings up to 64 characters")
        if not isinstance(item, (bool, int, float, str)) and item is not None:
            raise _PayloadError("stat values must be JSON scalar values")
        if isinstance(item, float) and not math.isfinite(item):
            raise _PayloadError("floating-point stat values must be finite")
        if isinstance(item, str) and len(item) > 256:
            raise _PayloadError("string stat values must not exceed 256 characters")
        if key.casefold() in {"passphrase", "password", "secret"}:
            item = "<redacted>"
        elif isinstance(item, str):
            item = _redact_transport_secrets(item)
        items.append((key, item))
    return tuple(sorted(items))


def _failure(
    payload: Mapping[str, object], default_code: str, default_message: str
) -> ErrorInfo:
    code = payload.get("code", default_code)
    message = payload.get("message", default_message)
    if (
        not isinstance(code, str)
        or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code)
        or not isinstance(message, str)
        or not message
        or len(message) > 512
    ):
        raise _PayloadError("failure code or message is invalid")
    return ErrorInfo(code, message)


def _only(payload: Mapping[str, object], *allowed: str) -> None:
    extra = set(payload) - set(allowed)
    if extra:
        raise _PayloadError("unexpected payload fields")


def _text(payload: Mapping[str, object], name: str, maximum: int) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise _PayloadError(f"{name} must be a non-empty string up to {maximum} characters")
    return value


def _revision(payload: Mapping[str, object]) -> str:
    value = _text(payload, "config_revision", 128)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
        raise _PayloadError("config_revision must be an opaque identifier")
    return value


def _integer(
    payload: Mapping[str, object], name: str, *, minimum: int
) -> int:
    value = payload.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise _PayloadError(f"{name} must be an integer of at least {minimum}")
    return value


def _boolean(
    payload: Mapping[str, object], name: str, *, default: bool
) -> bool:
    value = payload.get(name, default)
    if not isinstance(value, bool):
        raise _PayloadError(f"{name} must be a boolean")
    return value


def _invalid_transition(state: MediaState, event: str) -> Decision:
    return _reject(
        state, "invalid_transition", f"{event} is invalid for the current state"
    )


def _busy(state: MediaState, branch: str) -> Decision:
    return _reject(state, "busy", f"{branch} transition is already in progress")


def _reject(state: MediaState, code: str, message: str) -> Decision:
    return Decision(state, error=ErrorInfo(code, message))


def _unique_effects(effects: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(effects))


def _error_dict(error: ErrorInfo | None) -> dict[str, str] | None:
    return error.to_dict() if error is not None else None


def _redact_transport_secrets(value: str) -> str:
    return re.sub(
        r"(?i)(passphrase(?:=|%3d))[^&\s]+",
        r"\1<redacted>",
        value,
    )


_BACKEND_EVENTS = frozenset(
    (
        "capture.started",
        "capture.stopped",
        "capture.failed",
        "capture.device_lost",
        "stream.started",
        "stream.stopped",
        "stream.failed",
        "stream.opus_bitrate_applied",
        "stream.opus_bitrate_failed",
        "connection.connected",
        "connection.lost",
        "connection.failed",
        "connection.retry_due",
        "connection.stats",
        "recording.started",
        "recording.stopped",
        "recording.failed",
        "recording.storage_checked",
        "recording.fragment_opened",
        "recording.fragment_closed",
        "recording.rotation_completed",
        "monitoring.failed",
    )
)
