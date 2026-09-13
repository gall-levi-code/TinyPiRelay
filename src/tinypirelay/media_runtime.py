"""Serialized orchestration for the Step 2 media state machine."""

from __future__ import annotations

import math
import logging
import re
import threading
import time
from collections import deque
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Protocol, Sequence

from .media_config import (
    DestinationStatus,
    MediaConfig,
    check_recording_destination,
)
from .media_state import (
    CaptureState,
    ConnectionState,
    Decision,
    ErrorInfo,
    MediaState,
    RecordingState,
    ServiceState,
    StreamState,
    reduce,
)


STORAGE_POLL_SECONDS = 5.0
LOGGER = logging.getLogger("tinypirelay.media")
STATS_POLL_SECONDS = 1.0
CONNECTION_ATTEMPT_TIMEOUT_SECONDS = 5.0
MAX_METER_CHANNELS = 2
CONFIG_APPLY_TIMEOUT_SECONDS = 3.0
SPECTRUM_LEASE_SECONDS = 5.0
_QUEUE_KINDS = ("stream", "recording", "level", "spectrum")
_QUEUE_METRIC_FIELDS = (
    "current_buffers",
    "current_bytes",
    "current_time_ns",
    "max_buffers",
    "max_bytes",
    "max_time_ns",
    "leaky",
)
_MAX_QUEUE_METRIC = (1 << 64) - 1


class TimerHandle(Protocol):
    def cancel(self) -> None: ...


class Scheduler(Protocol):
    def call_later(
        self, delay_seconds: float, callback: Callable[[], None]
    ) -> TimerHandle: ...


class MediaBackend(Protocol):
    """The narrow surface supplied by the real programmable media adapter."""

    def invoke(
        self, callback: Callable[..., Any], *args: object, **kwargs: object
    ) -> Future[Any]: ...

    def start_capture(self) -> None: ...

    def stop_capture(self) -> None: ...

    def start_stream(self, generation: int | None = None) -> int: ...

    def stop_stream(self, generation: int | None = None) -> int: ...

    def abort_stream_attempt(self, generation: int | None = None) -> int: ...

    def update_config(self, config: MediaConfig) -> None: ...

    def set_spectrum_active(self, active: bool) -> bool: ...

    def set_bitrate(self, bitrate_bps: int) -> int: ...

    def start_recording(
        self, path: str | None = None, generation: int | None = None
    ) -> tuple[int, str]: ...

    def stop_recording(self, generation: int | None = None) -> int: ...

    def rotate_recording(self, generation: int | None = None) -> tuple[int, str]: ...

    def poll_stats(self) -> Mapping[str, object]: ...

    def shutdown(self) -> None: ...


class _ThreadScheduler:
    def call_later(
        self, delay_seconds: float, callback: Callable[[], None]
    ) -> threading.Timer:
        timer = threading.Timer(delay_seconds, callback)
        timer.daemon = True
        timer.start()
        return timer


@dataclass
class _Work:
    kind: str
    event: str | None = None
    payload: dict[str, object] = field(default_factory=dict)
    value: object = None
    wait: bool = False
    done: threading.Event = field(default_factory=threading.Event)
    decision: Decision | None = None
    result: object = None
    error: BaseException | None = None


class MediaRuntime:
    """Own state, timers and backend effects without owning a media loop."""

    def __init__(
        self,
        config: MediaConfig,
        config_revision: str,
        backend: MediaBackend,
        *,
        scheduler: Scheduler | None = None,
        destination_check: Callable[..., DestinationStatus] = check_recording_destination,
        storage_poll_seconds: float = STORAGE_POLL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(config_revision, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", config_revision
        ):
            raise ValueError("config_revision must be an opaque safe identifier")
        if (
            not isinstance(storage_poll_seconds, (int, float))
            or isinstance(storage_poll_seconds, bool)
            or not math.isfinite(storage_poll_seconds)
            or storage_poll_seconds <= 0
        ):
            raise ValueError("storage_poll_seconds must be positive and finite")

        self._config = config
        self._config_revision = config_revision
        self._backend = backend
        self._scheduler = scheduler or _ThreadScheduler()
        self._destination_check = destination_check
        self._storage_poll_seconds = float(storage_poll_seconds)
        self._monotonic = monotonic
        self._recording_started_at: float | None = None
        self._recording_elapsed_file: str | None = None
        self._recording_elapsed_seconds: float | None = None

        self._state = MediaState()
        self._lock = threading.Lock()
        self._queue: deque[_Work] = deque()
        self._draining = False
        self._drain_thread: int | None = None
        self._runtime_error: ErrorInfo | None = None

        self._backend_generations: dict[str, tuple[int, int]] = {}
        self._backend_instance_tokens: dict[str, int] = {}
        self._recording_paths: dict[int, str] = {}
        self._retiring_instance_tokens: set[tuple[str, int]] = set()
        self._connection_timeout_pending: tuple[int, int] | None = None
        self._connection_attempt_timer: TimerHandle | None = None
        self._reconnect_timer: TimerHandle | None = None
        self._storage_timer: TimerHandle | None = None
        self._rotation_timer: TimerHandle | None = None
        self._stats_timer: TimerHandle | None = None
        self._spectrum_lease_timer: TimerHandle | None = None
        self._reconnect_token = 0
        self._storage_token = 0
        self._rotation_token = 0
        self._stats_token = 0
        self._stats_inflight = False
        self._stats_capture_generation: int | None = None
        self._stats_generation: int | None = None
        self._stats_instance_token: int | None = None
        self._connection_attempt_token = 0
        self._spectrum_lease_token = 0
        self._spectrum_backend_active = False

        self._meter_sequence = 0
        self._spectrum_sequence = 0
        self._latest_meter: dict[str, object] | None = None
        self._latest_spectrum: dict[str, object] | None = None
        self._latest_queue_metrics: dict[str, dict[str, int]] = {}

    @property
    def state(self) -> MediaState:
        with self._lock:
            return self._state

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            state = self._state.to_dict()
            state["recording"]["active_files"] = sorted(set(self._recording_paths.values()))
            self._add_recording_elapsed(state["recording"])
            runtime_error = (
                self._runtime_error.to_dict() if self._runtime_error is not None else None
            )
            meter = _copy_telemetry(self._latest_meter)
            spectrum = _copy_telemetry(self._latest_spectrum)
            queues = _copy_queue_metrics(self._latest_queue_metrics)
        return {
            "state": state,
            "telemetry": {"meter": meter, "spectrum": spectrum},
            "runtime_metrics": {"queues": queues},
            "runtime_error": runtime_error,
        }

    def device_telemetry_context(self) -> tuple[MediaConfig, str, dict[str, object]]:
        """Copy active configuration and fragment identity without filesystem I/O."""

        with self._lock:
            recording = self._state.to_dict()["recording"]
            self._add_recording_elapsed(recording)
            return self._config, self._config_revision, recording

    def _add_recording_elapsed(self, recording: dict[str, object]) -> None:
        # Elapsed wall time is not FLAC sample duration; only a confirmed close
        # retains a frozen duration after the current fragment disappears.
        elapsed = self._recording_elapsed_seconds
        if self._recording_started_at is not None:
            elapsed = max(0.0, self._monotonic() - self._recording_started_at)
        recording["elapsed_file"] = self._recording_elapsed_file
        recording["elapsed_seconds"] = elapsed

    def _track_recording_elapsed(self, previous: MediaState, current: MediaState) -> None:
        if current.recording_generation != previous.recording_generation:
            self._recording_started_at = None
            self._recording_elapsed_file = None
            self._recording_elapsed_seconds = None
        if current.recording_current_file == previous.recording_current_file:
            return
        if current.recording_current_file is not None:
            self._recording_elapsed_file = current.recording_current_file
            self._recording_started_at = self._monotonic()
            self._recording_elapsed_seconds = None
        elif (
            previous.recording_current_file == current.recording_last_finalized_file
            and self._recording_elapsed_file == current.recording_last_finalized_file
            and self._recording_started_at is not None
        ):
            self._recording_elapsed_seconds = max(
                0.0, self._monotonic() - self._recording_started_at
            )
            self._recording_started_at = None
        else:
            self._recording_started_at = None
            self._recording_elapsed_file = None
            self._recording_elapsed_seconds = None

    def telemetry_snapshot(self) -> dict[str, object | None]:
        with self._lock:
            return {
                "meter": _copy_telemetry(self._latest_meter),
                "spectrum": _copy_telemetry(self._latest_spectrum),
            }

    def dispatch(self, event: str, **payload: object) -> Decision:
        """Synchronously serialize one external command through the reducer."""

        work = _Work("state", event=event, payload=dict(payload), wait=True)
        self._enqueue(work)
        work.done.wait()
        if work.error is not None:
            raise work.error
        assert work.decision is not None
        return work.decision

    def post_event(self, event: str, **payload: object) -> None:
        """Queue a backend/timer event; safe to call reentrantly from an effect."""

        self._enqueue(_Work("state", event=event, payload=dict(payload)))

    def start_capture(self) -> Decision:
        return self.dispatch("capture.start", config_revision=self._config_revision)

    def stop_capture(self) -> Decision:
        return self.dispatch("capture.stop")

    def start_stream(self) -> Decision:
        stream = self._config.stream
        payload: dict[str, object] = {
            "config_revision": self._config_revision,
            "representation_id": stream.representation_id,
        }
        if stream.representation_id == "opus_128k_48000_stereo_matroska":
            payload["opus_bitrate_bps"] = stream.opus_bitrate_bps
        return self.dispatch("stream.start", **payload)

    def stop_stream(self) -> Decision:
        return self.dispatch("stream.stop")

    def set_opus_bitrate(self, bitrate_bps: int) -> Decision:
        return self.dispatch("stream.set_opus_bitrate", bitrate_bps=bitrate_bps)

    def start_recording(self) -> Decision:
        return self.dispatch(
            "recording.start", config_revision=self._config_revision
        )

    def stop_recording(self) -> Decision:
        return self.dispatch("recording.stop")

    def apply_live_config(self, config: MediaConfig, config_revision: str) -> None:
        """Apply only recording and Opus-bitrate changes without rebuilding media."""

        if not isinstance(config, MediaConfig):
            raise TypeError("config must be a MediaConfig")
        if not isinstance(config_revision, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", config_revision
        ):
            raise ValueError("config_revision must be an opaque safe identifier")
        work = _Work(
            "config_update",
            value=(config, config_revision),
            wait=True,
        )
        self._enqueue(work)
        work.done.wait()
        if work.error is not None:
            raise work.error

    def renew_spectrum_lease(self) -> bool:
        """Renew the fixed spectrum lease and report physical branch state."""

        work = _Work("spectrum_lease", value=True, wait=True)
        self._enqueue(work)
        work.done.wait()
        if work.error is not None:
            raise work.error
        return work.result is True

    def release_spectrum_lease(self) -> bool:
        """Release the spectrum lease; repeated releases are idempotent."""

        work = _Work("spectrum_lease", value=False, wait=True)
        self._enqueue(work)
        work.done.wait()
        if work.error is not None:
            raise work.error
        return work.result is True

    def shutdown(self) -> Decision:
        return self.dispatch("service.shutdown")

    def publish_meter(self, channels: Sequence[Mapping[str, object]]) -> None:
        normalized = _meter_channels(channels)
        with self._lock:
            self._meter_sequence += 1
            self._latest_meter = {
                "sequence": self._meter_sequence,
                "channels": normalized,
            }

    def publish_spectrum(self, magnitudes_db: Sequence[int | float]) -> None:
        normalized = _spectrum_values(
            magnitudes_db, self._config.monitoring.spectrum_bands
        )
        with self._lock:
            self._spectrum_sequence += 1
            self._latest_spectrum = {
                "sequence": self._spectrum_sequence,
                "magnitudes_db": normalized,
            }

    def handle_engine_event(self, event: object) -> None:
        """Queue a duck-typed EngineEvent without importing PyGObject code."""

        if getattr(event, "kind", None) == "telemetry":
            self._publish_engine_telemetry(
                getattr(event, "scope", None), getattr(event, "details", None)
            )
            return
        self._enqueue(_Work("engine_event", value=event))

    def poll_stats(self) -> None:
        with self._lock:
            if self._stats_inflight:
                return
            self._stats_inflight = True
            self._stats_capture_generation = self._state.capture_generation
            self._stats_generation = self._state.stream_generation
            self._stats_instance_token = self._backend_instance_tokens.get("stream")
        self._invoke_backend("stats.poll", self._backend.poll_stats, None)

    def _enqueue(self, work: _Work) -> None:
        current = threading.get_ident()
        drain = False
        with self._lock:
            if work.wait and self._draining and self._drain_thread == current:
                raise RuntimeError("reentrant synchronous dispatch is not allowed; use post_event")
            self._queue.append(work)
            if not self._draining:
                self._draining = True
                self._drain_thread = current
                drain = True
        if drain:
            self._drain()

    def _drain(self) -> None:
        while True:
            with self._lock:
                if not self._queue:
                    self._draining = False
                    self._drain_thread = None
                    return
                work = self._queue.popleft()
            try:
                self._process(work)
            except BaseException as exc:  # keep later commands serviceable
                work.error = exc
                with self._lock:
                    self._runtime_error = ErrorInfo(
                        "runtime_failure", self._safe_message(exc)
                    )
            finally:
                work.done.set()

    def _process(self, work: _Work) -> None:
        if work.kind == "state":
            assert work.event is not None
            work.decision = self._apply_state_event(work.event, work.payload)
            return

        if work.kind == "backend_result":
            self._process_backend_result(work.event or "", work.payload, work.value)
            return

        if work.kind == "backend_failure":
            self._process_backend_failure(work.event or "", work.payload, str(work.value))
            return

        if work.kind == "engine_event":
            self._process_engine_event(work.value)
            return

        if work.kind == "connection_attempt_timeout":
            self._process_connection_attempt_timeout(work.payload)
            return

        if work.kind == "config_update":
            self._process_config_update(work.value)
            return

        if work.kind == "spectrum_lease":
            if type(work.value) is not bool:
                raise ValueError("invalid spectrum lease operation")
            work.result = self._process_spectrum_lease(work.value)
            return

        if work.kind == "spectrum_lease_expired":
            if work.value != self._spectrum_lease_token:
                return
            self._spectrum_lease_timer = None
            self._process_spectrum_lease(False)
            return

        raise RuntimeError(f"unknown runtime work kind: {work.kind}")

    def _process_config_update(self, value: object) -> None:
        if (
            not isinstance(value, tuple)
            or len(value) != 2
            or not isinstance(value[0], MediaConfig)
            or not isinstance(value[1], str)
        ):
            raise ValueError("invalid live configuration update")
        config, revision = value
        old = self._config
        if _restart_signature(config) != _restart_signature(old):
            raise ValueError("configuration requires a media-service restart")
        with self._lock:
            state = self._state
        if (
            config.recording != old.recording
            and state.recording
            in (RecordingState.STARTING, RecordingState.RUNNING, RecordingState.STOPPING)
        ):
            raise ValueError("recording must be stopped before changing its settings")
        bitrate_changed = (
            config.stream.opus_bitrate_bps != old.stream.opus_bitrate_bps
        )
        if bitrate_changed and state.stream in (StreamState.STARTING, StreamState.STOPPING):
            raise ValueError("stream must be stable before changing Opus bitrate")
        if bitrate_changed and state.pending_opus_bitrate_bps is not None:
            raise ValueError("another Opus bitrate change is pending")
        apply_bitrate = (
            bitrate_changed
            and state.stream is StreamState.RUNNING
            and state.representation_id
            == "opus_128k_48000_stereo_matroska"
            and state.opus_bitrate_bps != config.stream.opus_bitrate_bps
        )

        update_attempted = False
        bitrate_attempted = False
        try:
            update_attempted = True
            self._wait_backend(
                self._backend.update_config,
                config,
                timeout_message="media backend timed out applying configuration",
            )
            if apply_bitrate:
                bitrate_attempted = True
                applied = self._wait_backend(
                    self._backend.set_bitrate,
                    config.stream.opus_bitrate_bps,
                    timeout_message="media backend timed out applying Opus bitrate",
                )
                if applied != config.stream.opus_bitrate_bps:
                    raise RuntimeError(
                        "media backend did not confirm the requested Opus bitrate"
                    )
        except BaseException as exc:
            rollback_failed = False
            if bitrate_attempted and type(state.opus_bitrate_bps) is int:
                try:
                    self._wait_backend(
                        self._backend.set_bitrate,
                        state.opus_bitrate_bps,
                        timeout_message="media backend timed out rolling back Opus bitrate",
                    )
                except BaseException:
                    rollback_failed = True
            if update_attempted:
                try:
                    self._wait_backend(
                        self._backend.update_config,
                        old,
                        timeout_message="media backend timed out rolling back configuration",
                    )
                except BaseException:
                    rollback_failed = True
            if rollback_failed:
                raise RuntimeError(
                    "media backend configuration rollback failed"
                ) from exc
            raise

        with self._lock:
            self._config = config
            self._config_revision = revision
            current = self._state
            self._state = replace(
                current,
                capture_config_revision=(
                    revision
                    if current.capture_config_revision is not None
                    else None
                ),
                stream_config_revision=(
                    revision if current.stream_config_revision is not None else None
                ),
                recording_config_revision=(
                    revision
                    if current.recording_config_revision is not None
                    else None
                ),
                opus_bitrate_bps=(
                    config.stream.opus_bitrate_bps
                    if apply_bitrate
                    else current.opus_bitrate_bps
                ),
            )

    def _wait_backend(
        self,
        callback: Callable[..., Any],
        *args: object,
        timeout_message: str,
    ) -> object:
        future = self._backend.invoke(callback, *args)
        try:
            return future.result(timeout=CONFIG_APPLY_TIMEOUT_SECONDS)
        except FutureTimeoutError as exc:
            future.cancel()
            raise RuntimeError(timeout_message) from exc

    def _process_spectrum_lease(self, active: bool) -> bool:
        if active:
            if self._config.monitoring.spectrum_updates_per_second <= 0:
                return False
            self._schedule_spectrum_lease_expiry()
            if self._spectrum_backend_active:
                return True
        else:
            self._cancel_spectrum_lease()
            with self._lock:
                self._latest_spectrum = None
                self._latest_queue_metrics.pop("spectrum", None)
            if not self._spectrum_backend_active:
                return False

        applied = self._wait_backend(
            self._backend.set_spectrum_active,
            active,
            timeout_message="media backend timed out changing spectrum state",
        )
        if type(applied) is not bool:
            raise RuntimeError("media backend returned an invalid spectrum state")
        self._spectrum_backend_active = applied
        if not applied:
            with self._lock:
                self._latest_queue_metrics.pop("spectrum", None)
        return applied

    def _apply_state_event(
        self, event: str, payload: Mapping[str, object]
    ) -> Decision:
        with self._lock:
            previous = self._state
        if (
            event == "capture.start"
            and previous.service is ServiceState.RUNNING
            and self._config.capture.mode is None
        ):
            return Decision(
                previous,
                error=ErrorInfo(
                    "audio_setup_required",
                    "Select an audio device and mode in Audio settings before starting capture.",
                ),
            )
        values = payload
        if event == "recording.start":
            values = dict(payload)
            values["storage_status"] = self._storage_status()
        elif event == "recording.rotate" and previous.recording is RecordingState.RUNNING and not previous.recording_rotation_pending:
            status = self._storage_status()
            if status != "safe":
                event = "recording.storage_checked"
                values = {"generation": previous.recording_generation, "storage_status": status}
        decision = reduce(previous, event, **values)
        with self._lock:
            self._track_recording_elapsed(previous, decision.state)
            self._state = decision.state
        warning = decision.state.recording_warning
        if warning is not None and warning != previous.recording_warning:
            LOGGER.warning("Recording safety stop: %s", warning.code)
        for effect in decision.effects:
            self._dispatch_effect(effect, decision.state)
        self._sync_timers(decision.state)
        self._clear_stopped_generation_mappings(decision.state)
        return decision

    def _dispatch_effect(self, effect: str, state: MediaState) -> None:
        if effect == "capture.start":
            self._invoke_backend(effect, self._backend.start_capture, state.capture_generation)
        elif effect == "capture.stop":
            self._invoke_backend(effect, self._backend.stop_capture, state.capture_generation)
        elif effect in ("stream.start", "connection.connect"):
            self._invoke_backend(
                effect,
                self._backend.start_stream,
                state.stream_generation,
                invoke_kwargs={"generation": state.stream_generation},
            )
        elif effect == "stream.stop":
            self._invoke_backend(
                effect,
                self._backend.stop_stream,
                state.stream_generation,
                invoke_kwargs={"generation": state.stream_generation},
            )
        elif effect == "stream.set_opus_bitrate":
            assert state.pending_opus_bitrate_bps is not None
            self._invoke_backend(
                effect,
                self._backend.set_bitrate,
                state.stream_generation,
                state.pending_opus_bitrate_bps,
            )
        elif effect == "connection.schedule_retry":
            self._schedule_reconnect(state)
        elif effect == "connection.cancel_retry":
            self._cancel_reconnect()
        elif effect == "recording.start":
            self._invoke_backend(
                effect,
                self._backend.start_recording,
                state.recording_generation,
                invoke_kwargs={"generation": state.recording_generation},
            )
        elif effect == "recording.stop":
            self._invoke_backend(
                effect,
                self._backend.stop_recording,
                state.recording_generation,
                invoke_kwargs={"generation": state.recording_generation},
            )
        elif effect == "recording.rotate":
            self._invoke_backend(
                effect,
                self._backend.rotate_recording,
                state.recording_generation,
                invoke_kwargs={"generation": state.recording_generation},
            )
        elif effect == "service.stop":
            self._cancel_all_timers()
            self._invoke_backend(effect, self._backend.shutdown, None)
        else:
            raise RuntimeError(f"unhandled reducer effect: {effect}")

    def _invoke_backend(
        self,
        effect: str,
        callback: Callable[..., Any],
        desired_generation: int | None,
        *args: object,
        invoke_kwargs: Mapping[str, object] | None = None,
    ) -> None:
        try:
            future = self._backend.invoke(callback, *args, **dict(invoke_kwargs or {}))
        except BaseException as exc:
            self._enqueue(
                _Work(
                    "backend_failure",
                    event=effect,
                    payload={"generation": desired_generation},
                    value=self._safe_message(exc),
                )
            )
            return

        def completed(result: Future[Any]) -> None:
            try:
                value = result.result()
            except BaseException as exc:
                self._enqueue(
                    _Work(
                        "backend_failure",
                        event=effect,
                        payload={"generation": desired_generation},
                        value=self._safe_message(exc),
                    )
                )
            else:
                self._enqueue(
                    _Work(
                        "backend_result",
                        event=effect,
                        payload={"generation": desired_generation},
                        value=value,
                    )
                )

        future.add_done_callback(completed)

    def _process_backend_result(
        self, effect: str, payload: Mapping[str, object], value: object
    ) -> None:
        desired = payload.get("generation")
        desired_generation = desired if type(desired) is int else None
        if effect == "service.stop":
            return
        if effect == "stats.poll":
            with self._lock:
                self._stats_inflight = False
                capture_generation = self._stats_capture_generation
                generation = self._stats_generation
                instance_token = self._stats_instance_token
                self._stats_capture_generation = None
                self._stats_generation = None
                self._stats_instance_token = None
            self._accept_polled_stats(
                value, capture_generation, generation, instance_token
            )
            with self._lock:
                state = self._state
            self._sync_stats_timer(state)
            return
        if effect in ("capture.start", "capture.stop") and value is None:
            # Capture state-change events are the authoritative acknowledgement.
            return
        if effect == "stream.set_opus_bitrate":
            bitrate = value if type(value) is int else None
            with self._lock:
                pending = self._state.pending_opus_bitrate_bps
            if desired_generation is None or pending is None:
                return
            if bitrate == pending:
                self.post_event(
                    "stream.opus_bitrate_applied",
                    generation=desired_generation,
                    bitrate_bps=bitrate,
                )
            else:
                self.post_event(
                    "stream.opus_bitrate_failed",
                    generation=desired_generation,
                    bitrate_bps=pending,
                    message="backend did not confirm the requested Opus bitrate",
                )
            return

        backend_generation: int | None = None
        path: str | None = None
        if type(value) is int:
            backend_generation = value
        elif (
            isinstance(value, tuple)
            and len(value) == 2
            and type(value[0]) is int
            and isinstance(value[1], str)
        ):
            backend_generation, path = value
        if (
            backend_generation is None
            or backend_generation < 1
            or desired_generation is None
        ):
            self._process_backend_failure(
                effect,
                payload,
                "backend returned an invalid operation result",
            )
            return
        scope = _effect_scope(effect)
        if scope is not None and effect in {
            "stream.start",
            "connection.connect",
            "recording.start",
            "recording.rotate",
        }:
            with self._lock:
                self._backend_generations[scope] = (
                    backend_generation,
                    desired_generation,
                )
        if path is not None and effect in ("recording.start", "recording.rotate"):
            # Engine events remain authoritative for lifecycle; retain no path here.
            if len(path) > 4096:
                self._process_backend_failure(
                    effect, payload, "backend returned an invalid recording path"
                )

    def _process_backend_failure(
        self, effect: str, payload: Mapping[str, object], message: str
    ) -> None:
        desired = payload.get("generation")
        generation = desired if type(desired) is int else None
        safe_message = self._safe_text(message)
        if effect == "stats.poll":
            with self._lock:
                self._stats_inflight = False
                self._stats_capture_generation = None
                self._stats_generation = None
                self._stats_instance_token = None
                self._latest_queue_metrics = {}
                self._runtime_error = ErrorInfo("stats_poll_failed", safe_message)
                state = self._state
            self._sync_stats_timer(state)
            return
        if effect == "service.stop":
            with self._lock:
                self._runtime_error = ErrorInfo("backend_shutdown_failed", safe_message)
            return
        if generation is None:
            return
        if effect.startswith("capture."):
            self.post_event(
                "capture.failed", generation=generation, message=safe_message
            )
        elif effect == "connection.timeout_stop":
            with self._lock:
                pending = self._connection_timeout_pending
                if pending is not None and pending[0] == generation:
                    self._retiring_instance_tokens.discard(("stream", pending[1]))
                    self._connection_timeout_pending = None
            self.post_event(
                "connection.failed",
                generation=generation,
                retryable=True,
                code="srt_connection_timeout_stop_failed",
                message=safe_message,
            )
        elif effect == "connection.connect":
            self.post_event(
                "connection.failed",
                generation=generation,
                retryable=True,
                message=safe_message,
            )
        elif effect == "stream.set_opus_bitrate":
            with self._lock:
                pending = self._state.pending_opus_bitrate_bps
            if pending is not None:
                self.post_event(
                    "stream.opus_bitrate_failed",
                    generation=generation,
                    bitrate_bps=pending,
                    message=safe_message,
                )
        elif effect.startswith("stream."):
            self.post_event(
                "stream.failed", generation=generation, message=safe_message
            )
        elif effect.startswith("recording."):
            self.post_event(
                "recording.failed", generation=generation, message=safe_message
            )

    def _process_engine_event(self, event: object) -> None:
        kind = getattr(event, "kind", None)
        scope = getattr(event, "scope", None)
        generation = getattr(event, "generation", None)
        details = getattr(event, "details", None)
        if not isinstance(kind, str) or not isinstance(scope, str):
            return
        if not isinstance(details, Mapping):
            details = {}

        if kind == "telemetry":
            self._publish_engine_telemetry(scope, details)
            return

        if kind == "shutdown-complete":
            with self._lock:
                self._recording_paths.clear()
            return
        if kind == "branch-removed" and scope == "recording":
            with self._lock:
                self._recording_paths.pop(details.get("instance_token"), None)
        if kind == "error" and scope in ("level", "spectrum"):
            if scope == "spectrum":
                self._spectrum_backend_active = False
            with self._lock:
                if scope == "spectrum":
                    self._latest_spectrum = None
                self._latest_queue_metrics.pop(scope, None)
            self.post_event(
                "monitoring.failed",
                message=self._safe_text(
                    _event_message(details, f"{scope} media backend failure")
                ),
            )
            return
        if kind == "error" and scope == "unknown":
            with self._lock:
                self._runtime_error = ErrorInfo(
                    "backend_error",
                    self._safe_text(_event_message(details, "media backend failure")),
                )
            return
        if type(generation) is not int:
            return
        reducer_generation = self._translate_generation(scope, generation)
        if reducer_generation is None:
            return
        payload: dict[str, object] = {"generation": reducer_generation}

        if kind == "branch-created" and scope in ("stream", "recording"):
            self._replace_instance_token(scope, details)
            token, path = details.get("instance_token"), details.get("path")
            if scope == "recording" and type(token) is int and isinstance(path, str) and path:
                with self._lock:
                    self._recording_paths[token] = path
                    running = self._state.recording in (RecordingState.RUNNING, RecordingState.STOPPING)
                if running:
                    self.post_event("recording.fragment_opened", **payload, path=path)
            return
        if kind in ("capture-started", "capture-stopped") and scope == "capture":
            if kind == "capture-stopped":
                self._spectrum_backend_active = False
                with self._lock:
                    self._latest_spectrum = None
            self.post_event(
                "capture.started" if kind.endswith("started") else "capture.stopped",
                **payload,
            )
            return
        if kind == "branch-added":
            if scope == "stream":
                if not self._is_current_instance("stream", details):
                    return
                self.post_event("stream.started", **payload)
            elif scope == "recording":
                if not self._is_current_instance("recording", details):
                    return
                self.post_event("recording.started", **payload)
                path = details.get("path")
                if isinstance(path, str) and path:
                    self.post_event(
                        "recording.fragment_opened", **payload, path=path
                    )
            return
        if kind == "branch-removed":
            timed_out = self._consume_connection_timeout_ack(
                scope, reducer_generation, details
            )
            token = details.get("instance_token")
            with self._lock:
                stopping_current_stream = bool(
                    scope == "stream"
                    and self._state.stream is StreamState.STOPPING
                    and type(token) is int
                    and self._backend_instance_tokens.get("stream") == token
                )
            retiring_or_stale = self._is_retiring_or_stale(scope, details)
            if timed_out:
                self.post_event(
                    "connection.failed",
                    **payload,
                    retryable=True,
                    code="srt_connection_timeout",
                    message="SRT caller connection attempt timed out",
                )
                return
            if retiring_or_stale and not stopping_current_stream:
                return
            missing = (
                details.get("missing") is True
                and details.get("instance_token") is None
            )
            forced = details.get("forced") is not False
            if scope == "stream":
                with self._lock:
                    stopping = self._state.stream is StreamState.STOPPING
                if forced:
                    self.post_event(
                        "stream.failed",
                        **payload,
                        code="stream_finalization_failed",
                        message="stream branch required forced removal",
                    )
                elif stopping:
                    self.post_event("stream.stopped", **payload)
            elif scope == "recording":
                with self._lock:
                    path = details.get("path") or self._state.recording_current_file
                    stopping = self._state.recording is RecordingState.STOPPING
                    failure_target = (
                        self._state.recording_stop_target
                        is RecordingState.FAILED
                    )
                if missing and not failure_target:
                    self.post_event(
                        "recording.failed",
                        **payload,
                        code="recording_finalization_failed",
                        message="recording branch was missing during finalization",
                    )
                    self.post_event("recording.stopped", **payload)
                elif forced:
                    self.post_event(
                        "recording.failed",
                        **payload,
                        code="recording_finalization_failed",
                        message="recording file finalization required forced removal",
                    )
                    self.post_event("recording.stopped", **payload)
                elif stopping:
                    if path and not failure_target:
                        self.post_event(
                            "recording.fragment_closed", **payload, path=path
                        )
                    self.post_event("recording.stopped", **payload)
            return
        if kind == "connection" and scope == "stream":
            if not self._is_current_instance("stream", details):
                return
            connection_state = details.get("state")
            if connection_state == "connected":
                self.post_event("connection.connected", **payload)
            elif connection_state == "disconnected":
                self._mark_instance_retiring("stream", details)
                self.post_event(
                    "connection.lost",
                    **payload,
                    code=_connection_error_code(details),
                    message=self._safe_text(
                        _event_message(details, "SRT receiver disconnected")
                    ),
                )
            return
        if kind == "opus-bitrate-changed" and scope == "stream":
            if not self._is_current_instance("stream", details):
                return
            new_bps = details.get("new_bps")
            with self._lock:
                pending = self._state.pending_opus_bitrate_bps
            if type(new_bps) is int and new_bps == pending:
                self.post_event(
                    "stream.opus_bitrate_applied",
                    **payload,
                    bitrate_bps=new_bps,
                )
            return
        if kind == "recording-rotated" and scope == "recording":
            old_token = details.get("old_instance_token")
            new_token = details.get("new_instance_token")
            if type(old_token) is int:
                with self._lock:
                    self._retiring_instance_tokens.discard(("recording", old_token))
            if type(new_token) is int:
                with self._lock:
                    self._backend_instance_tokens["recording"] = new_token
            old_path = details.get("old_path")
            new_path = details.get("new_path")
            if details.get("forced") is not False:
                if isinstance(new_path, str) and new_path:
                    self.post_event(
                        "recording.fragment_opened", **payload, path=new_path
                    )
                self.post_event(
                    "recording.failed",
                    **payload,
                    code="recording_finalization_failed",
                    message="recording rotation required forced removal",
                )
                return
            if isinstance(old_path, str) and old_path:
                self.post_event(
                    "recording.fragment_closed", **payload, path=old_path
                )
            if isinstance(new_path, str) and new_path:
                self.post_event(
                    "recording.fragment_opened", **payload, path=new_path
                )
            self.post_event("recording.rotation_completed", **payload)
            return
        if kind == "error":
            if scope in ("stream", "recording") and not self._is_current_instance(
                scope, details
            ):
                return
            message = self._safe_text(
                _event_message(details, f"{scope} media backend failure")
            )
            if scope == "capture":
                self._spectrum_backend_active = False
                with self._lock:
                    self._latest_spectrum = None
                event_name = (
                    "capture.device_lost"
                    if details.get("code") == "device_lost"
                    else "capture.failed"
                )
                self.post_event(event_name, **payload, message=message)
            elif scope == "stream":
                self.post_event("stream.failed", **payload, message=message)
            elif scope == "recording":
                self.post_event("recording.failed", **payload, message=message)
            else:
                with self._lock:
                    self._runtime_error = ErrorInfo("backend_error", message)

    def _publish_engine_telemetry(self, scope: object, details: object) -> None:
        if not isinstance(details, Mapping):
            return
        try:
            if scope == "level":
                self.publish_meter(_level_channels(details))
            elif scope == "spectrum":
                self.publish_spectrum(_spectrum_details(details))
        except (TypeError, ValueError):
            self.post_event(
                "monitoring.failed",
                message=f"invalid {scope} telemetry from media backend",
            )

    def _translate_generation(self, scope: str, backend_generation: int) -> int | None:
        key = "stream" if scope == "connection" else scope
        with self._lock:
            mapping = self._backend_generations.get(key)
            current = {
                "capture": self._state.capture_generation,
                "stream": self._state.stream_generation,
                "recording": self._state.recording_generation,
            }.get(key)
        if mapping is not None:
            return mapping[1] if mapping[0] == backend_generation else None
        return current if current == backend_generation else None

    def _replace_instance_token(
        self, scope: str, details: Mapping[str, object]
    ) -> None:
        token = details.get("instance_token")
        if type(token) is int:
            with self._lock:
                current = self._backend_instance_tokens.get(scope)
                changed = current != token
                if current is not None and current != token:
                    self._retiring_instance_tokens.add((scope, current))
                self._retiring_instance_tokens.discard((scope, token))
                self._backend_instance_tokens[scope] = token
                state = self._state
            if scope == "stream":
                if changed:
                    self._cancel_connection_attempt()
                    with self._lock:
                        self._connection_timeout_pending = None
                self._sync_connection_attempt(state)

    def _mark_instance_retiring(
        self, scope: str, details: Mapping[str, object]
    ) -> None:
        token = details.get("instance_token")
        if type(token) is int:
            with self._lock:
                if self._backend_instance_tokens.get(scope) == token:
                    self._retiring_instance_tokens.add((scope, token))

    def _is_current_instance(
        self, scope: str, details: Mapping[str, object]
    ) -> bool:
        token = details.get("instance_token")
        with self._lock:
            current = self._backend_instance_tokens.get(scope)
            retiring = (
                type(token) is int
                and (scope, token) in self._retiring_instance_tokens
            )
        return (
            type(token) is int
            and not retiring
            and token == current
        )

    def _is_retiring_or_stale(
        self, scope: str, details: Mapping[str, object]
    ) -> bool:
        token = details.get("instance_token")
        if token is None and details.get("missing") is True:
            with self._lock:
                current = self._backend_instance_tokens.pop(scope, None)
                if current is not None:
                    self._retiring_instance_tokens.discard((scope, current))
            return False
        if type(token) is not int:
            return True
        with self._lock:
            key = (scope, token)
            if key in self._retiring_instance_tokens:
                self._retiring_instance_tokens.discard(key)
                if self._backend_instance_tokens.get(scope) == token:
                    self._backend_instance_tokens.pop(scope, None)
                return True
            current = self._backend_instance_tokens.get(scope)
        return current is not None and token != current

    def _consume_connection_timeout_ack(
        self,
        scope: str,
        generation: int,
        details: Mapping[str, object],
    ) -> bool:
        if scope != "stream":
            return False
        token = details.get("instance_token")
        with self._lock:
            pending = self._connection_timeout_pending
            matches = bool(
                pending is not None
                and pending[0] == generation
                and (
                    token == pending[1]
                    or (token is None and details.get("missing") is True)
                )
            )
            if matches:
                self._connection_timeout_pending = None
        return matches

    def _accept_polled_stats(
        self,
        value: object,
        capture_generation: int | None,
        generation: int | None,
        instance_token: int | None,
    ) -> None:
        if not isinstance(value, Mapping):
            # A malformed successful result must not leave an older queue
            # snapshot looking current to the profiler or control API.
            with self._lock:
                self._latest_queue_metrics = {}
            return
        queues = _safe_queue_metrics(value.get("queues"))
        with self._lock:
            state = self._state
            current_capture_poll = bool(
                state.capture is CaptureState.RUNNING
                and type(capture_generation) is int
                and capture_generation == state.capture_generation
            )
            if current_capture_poll:
                if state.stream not in (StreamState.STARTING, StreamState.RUNNING):
                    queues.pop("stream", None)
                if state.recording not in (
                    RecordingState.STARTING,
                    RecordingState.RUNNING,
                ):
                    queues.pop("recording", None)
                if not self._spectrum_backend_active:
                    queues.pop("spectrum", None)
                self._latest_queue_metrics = queues
            elif state.capture is not CaptureState.RUNNING:
                self._latest_queue_metrics = {}
            current_generation = self._state.stream_generation
            current_token = self._backend_instance_tokens.get("stream")
            retiring = (
                type(instance_token) is int
                and ("stream", instance_token) in self._retiring_instance_tokens
            )
            stream_active = self._state.stream in (StreamState.STARTING, StreamState.RUNNING)
            connecting = self._state.connection is ConnectionState.CONNECTING
        stats = value.get("connection", value.get("srt", value))
        current_poll = bool(
            type(generation) is int
            and generation == current_generation
            and type(instance_token) is int
            and instance_token == current_token
            and not retiring
        )
        if stream_active and current_poll and isinstance(stats, Mapping):
            safe_stats = self._safe_stats(stats)
            self._apply_state_event(
                "connection.stats",
                {"generation": generation, "stats": safe_stats},
            )
            bytes_sent = safe_stats.get("bytes-sent-total")
            if connecting and type(bytes_sent) is int and bytes_sent > 0:
                self._apply_state_event(
                    "connection.connected", {"generation": generation}
                )

    def _storage_status(self) -> str:
        try:
            status = self._destination_check(self._config.recording)
        except BaseException as exc:
            with self._lock:
                self._runtime_error = ErrorInfo(
                    "storage_check_failed", self._safe_message(exc)
                )
            return "unverified"
        if status.safe:
            return "safe"
        if status.reason == "storage_threshold":
            return "unsafe"
        if status.reason in {
            "destination_stat_failed",
            "required_mountpoint_check_failed",
            "usage_unavailable",
        }:
            return "unverified"
        return "unavailable"

    def _schedule_reconnect(self, state: MediaState) -> None:
        delay = state.reconnect_delay_seconds
        if delay is None:
            return
        self._cancel_reconnect()
        self._reconnect_token += 1
        token = self._reconnect_token
        generation = state.stream_generation

        def due() -> None:
            with self._lock:
                if token != self._reconnect_token:
                    return
                self._reconnect_timer = None
            self.post_event("connection.retry_due", generation=generation)

        self._reconnect_timer = self._scheduler.call_later(delay, due)

    def _schedule_connection_attempt(
        self, generation: int, instance_token: int
    ) -> None:
        if self._connection_attempt_timer is not None:
            return
        self._connection_attempt_token += 1
        timer_token = self._connection_attempt_token

        def due() -> None:
            with self._lock:
                if timer_token != self._connection_attempt_token:
                    return
            self._enqueue(
                _Work(
                    "connection_attempt_timeout",
                    payload={
                        "timer_token": timer_token,
                        "generation": generation,
                        "instance_token": instance_token,
                    },
                )
            )

        self._connection_attempt_timer = self._scheduler.call_later(
            CONNECTION_ATTEMPT_TIMEOUT_SECONDS, due
        )

    def _process_connection_attempt_timeout(
        self, payload: Mapping[str, object]
    ) -> None:
        timer_token = payload.get("timer_token")
        generation = payload.get("generation")
        instance_token = payload.get("instance_token")
        if not all(type(value) is int for value in payload.values()):
            return
        with self._lock:
            if timer_token == self._connection_attempt_token:
                self._connection_attempt_timer = None
            current = self._backend_instance_tokens.get("stream")
            valid = bool(
                timer_token == self._connection_attempt_token
                and self._state.service is ServiceState.RUNNING
                and self._state.stream in (StreamState.STARTING, StreamState.RUNNING)
                and self._state.connection is ConnectionState.CONNECTING
                and self._state.stream_generation == generation
                and current == instance_token
                and ("stream", instance_token)
                not in self._retiring_instance_tokens
            )
            if valid:
                self._connection_timeout_pending = (generation, instance_token)
                self._retiring_instance_tokens.add(("stream", instance_token))
        if not valid:
            return
        self._invoke_backend(
            "connection.timeout_stop",
            self._backend.abort_stream_attempt,
            generation,
            invoke_kwargs={"generation": generation},
        )

    def _cancel_connection_attempt(self) -> None:
        self._connection_attempt_token += 1
        timer, self._connection_attempt_timer = (
            self._connection_attempt_timer,
            None,
        )
        if timer is not None:
            timer.cancel()

    def _sync_connection_attempt(self, state: MediaState) -> None:
        with self._lock:
            instance_token = self._backend_instance_tokens.get("stream")
            retiring = (
                type(instance_token) is int
                and ("stream", instance_token) in self._retiring_instance_tokens
            )
        if (
            state.service is ServiceState.RUNNING
            and state.stream in (StreamState.STARTING, StreamState.RUNNING)
            and state.connection is ConnectionState.CONNECTING
            and type(instance_token) is int
            and not retiring
        ):
            self._schedule_connection_attempt(
                state.stream_generation, instance_token
            )
        else:
            self._cancel_connection_attempt()
            if not (
                state.service is ServiceState.RUNNING
                and state.stream in (StreamState.STARTING, StreamState.RUNNING)
                and state.connection is ConnectionState.CONNECTING
            ):
                with self._lock:
                    self._connection_timeout_pending = None

    def _cancel_reconnect(self) -> None:
        self._reconnect_token += 1
        timer, self._reconnect_timer = self._reconnect_timer, None
        if timer is not None:
            timer.cancel()

    def _schedule_storage(self, state: MediaState) -> None:
        if self._storage_timer is not None:
            return
        self._storage_token += 1
        token = self._storage_token
        generation = state.recording_generation

        def due() -> None:
            with self._lock:
                if token != self._storage_token:
                    return
                self._storage_timer = None
            storage_status = self._storage_status()
            with self._lock:
                if token != self._storage_token:
                    return
            self.post_event(
                "recording.storage_checked",
                generation=generation,
                storage_status=storage_status,
            )

        self._storage_timer = self._scheduler.call_later(
            self._storage_poll_seconds, due
        )

    def _cancel_storage(self) -> None:
        self._storage_token += 1
        timer, self._storage_timer = self._storage_timer, None
        if timer is not None:
            timer.cancel()

    def _schedule_rotation(self, state: MediaState) -> None:
        if self._rotation_timer is not None:
            return
        self._rotation_token += 1
        token = self._rotation_token

        def due() -> None:
            with self._lock:
                if token != self._rotation_token:
                    return
                self._rotation_timer = None
            self.post_event("recording.rotate")

        self._rotation_timer = self._scheduler.call_later(
            self._config.recording.rotation_seconds, due
        )

    def _cancel_rotation(self) -> None:
        self._rotation_token += 1
        timer, self._rotation_timer = self._rotation_timer, None
        if timer is not None:
            timer.cancel()

    def _schedule_stats(self) -> None:
        if self._stats_timer is not None or self._stats_inflight:
            return
        self._stats_token += 1
        token = self._stats_token

        def due() -> None:
            with self._lock:
                if token != self._stats_token:
                    return
                self._stats_timer = None
            self.poll_stats()

        self._stats_timer = self._scheduler.call_later(STATS_POLL_SECONDS, due)

    def _cancel_stats(self) -> None:
        self._stats_token += 1
        timer, self._stats_timer = self._stats_timer, None
        if timer is not None:
            timer.cancel()

    def _schedule_spectrum_lease_expiry(self) -> None:
        self._spectrum_lease_token += 1
        token = self._spectrum_lease_token
        timer, self._spectrum_lease_timer = self._spectrum_lease_timer, None
        if timer is not None:
            timer.cancel()

        def due() -> None:
            with self._lock:
                if token != self._spectrum_lease_token:
                    return
            self._enqueue(_Work("spectrum_lease_expired", value=token))

        self._spectrum_lease_timer = self._scheduler.call_later(
            SPECTRUM_LEASE_SECONDS, due
        )

    def _cancel_spectrum_lease(self) -> None:
        self._spectrum_lease_token += 1
        timer, self._spectrum_lease_timer = self._spectrum_lease_timer, None
        if timer is not None:
            timer.cancel()

    def _sync_stats_timer(self, state: MediaState) -> None:
        if (
            state.service is ServiceState.RUNNING
            and state.capture is CaptureState.RUNNING
        ):
            self._schedule_stats()
        else:
            self._cancel_stats()

    def _sync_timers(self, state: MediaState) -> None:
        if state.service is not ServiceState.RUNNING:
            self._cancel_all_timers()
            return
        self._sync_connection_attempt(state)
        self._sync_stats_timer(state)
        if state.connection is not ConnectionState.RETRY_WAIT:
            self._cancel_reconnect()
        if state.recording in (RecordingState.RUNNING, RecordingState.BLOCKED):
            self._schedule_storage(state)
        else:
            self._cancel_storage()
        if state.recording is RecordingState.RUNNING and not state.recording_rotation_pending:
            self._schedule_rotation(state)
        else:
            self._cancel_rotation()

    def _cancel_all_timers(self) -> None:
        self._cancel_connection_attempt()
        with self._lock:
            self._connection_timeout_pending = None
        self._cancel_reconnect()
        self._cancel_storage()
        self._cancel_rotation()
        self._cancel_stats()
        self._cancel_spectrum_lease()
        self._spectrum_backend_active = False
        with self._lock:
            self._latest_spectrum = None

    def _clear_stopped_generation_mappings(self, state: MediaState) -> None:
        with self._lock:
            if state.capture is not CaptureState.RUNNING:
                self._latest_queue_metrics = {}
            if state.capture in (CaptureState.STOPPED, CaptureState.FAILED):
                self._backend_generations.pop("capture", None)
            if state.stream not in (StreamState.STARTING, StreamState.RUNNING):
                self._latest_queue_metrics.pop("stream", None)
            if state.stream in (StreamState.STOPPED, StreamState.FAILED):
                self._backend_generations.pop("stream", None)
                self._backend_instance_tokens.pop("stream", None)
                self._retiring_instance_tokens = {
                    item for item in self._retiring_instance_tokens if item[0] != "stream"
                }
            if state.recording not in (
                RecordingState.STARTING,
                RecordingState.RUNNING,
            ):
                self._latest_queue_metrics.pop("recording", None)
            if state.recording in (
                RecordingState.STOPPED,
                RecordingState.BLOCKED,
                RecordingState.FAILED,
            ):
                self._backend_generations.pop("recording", None)
                self._backend_instance_tokens.pop("recording", None)
                self._retiring_instance_tokens = {
                    item
                    for item in self._retiring_instance_tokens
                    if item[0] != "recording"
                }
            if not self._spectrum_backend_active:
                self._latest_queue_metrics.pop("spectrum", None)

    def _safe_message(self, error: BaseException) -> str:
        return self._safe_text(str(error) or type(error).__name__)

    def _safe_text(self, value: str) -> str:
        secret = self._config.stream.passphrase
        if secret:
            value = value.replace(secret, "<redacted>")
        value = re.sub(
            r"(?i)(passphrase(?:=|%3d))[^&\s]+",
            r"\1<redacted>",
            value,
        )
        return value[:512] or "media backend operation failed"

    def _safe_stats(self, values: Mapping[object, object]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in list(values.items())[:64]:
            if not isinstance(key, str) or not key or len(key) > 64:
                continue
            if key.casefold() in {"passphrase", "password", "secret"}:
                result[key] = "<redacted>"
            elif isinstance(value, str):
                result[key] = self._safe_text(value)[:256]
            elif value is None or type(value) in (bool, int):
                result[key] = value
            elif type(value) is float and math.isfinite(value):
                result[key] = value
        return result


def _effect_scope(effect: str) -> str | None:
    if effect.startswith("capture."):
        return "capture"
    if effect.startswith("stream.") or effect == "connection.connect":
        return "stream"
    if effect.startswith("recording."):
        return "recording"
    return None


def _event_message(details: Mapping[str, object], default: str) -> str:
    value = details.get("message", default)
    return value if isinstance(value, str) and value else default


def _connection_error_code(details: Mapping[str, object]) -> str:
    value = details.get("code")
    if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value):
        return value
    if type(value) is int and 0 <= value <= 2_147_483_647:
        return f"srt_gstreamer_error_{value}"
    return "srt_connection_lost"


def _restart_signature(config: MediaConfig) -> tuple[object, ...]:
    """Fields that the live Step 3 adapter cannot replace in-place."""

    stream = config.stream
    return (
        config.schema_version,
        config.capture,
        stream.enabled,
        stream.representation_id,
        stream.destination_host,
        stream.destination_port,
        stream.latency_ms,
        stream.stream_id,
        stream.passphrase,
        config.monitoring,
    )


def _level_channels(details: Mapping[str, object]) -> tuple[dict[str, object], ...]:
    rms_values = details.get("rms_db")
    peak_values = details.get("peak_db")
    clipped = details.get("clipped", False)
    if (
        not isinstance(rms_values, Sequence)
        or isinstance(rms_values, (str, bytes))
        or not isinstance(peak_values, Sequence)
        or isinstance(peak_values, (str, bytes))
        or len(rms_values) != len(peak_values)
        or not 1 <= len(rms_values) <= MAX_METER_CHANNELS
    ):
        raise ValueError("invalid level arrays")
    if not isinstance(clipped, Sequence) or isinstance(clipped, (str, bytes)):
        clipped_values: Sequence[object] = (clipped,) * len(rms_values)
    else:
        clipped_values = clipped
    if len(clipped_values) != len(rms_values):
        raise ValueError("invalid clipped flags")

    channels: list[dict[str, object]] = []
    for rms_value, peak_value, clipped_value in zip(
        rms_values, peak_values, clipped_values
    ):
        rms = _nullable_db(rms_value)
        peak = _nullable_db(peak_value)
        if type(clipped_value) is not bool:
            raise ValueError("invalid clipped flag")
        channels.append(
            {
                "peak_dbfs": peak,
                "rms_dbfs": rms,
                "clipping": clipped_value,
                "no_signal": rms is None or rms <= -90.0,
            }
        )
    return tuple(channels)


def _spectrum_details(details: Mapping[str, object]) -> tuple[float, ...]:
    values = details.get("magnitude_db")
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise ValueError("invalid spectrum magnitudes")
    result: list[float] = []
    for value in values:
        if type(value) not in (int, float) or math.isnan(value) or value == math.inf:
            raise ValueError("invalid spectrum magnitude")
        result.append(-200.0 if value == -math.inf else float(value))
    return tuple(result)


def _nullable_db(value: object) -> float | None:
    if type(value) not in (int, float) or math.isnan(value) or value == math.inf:
        raise ValueError("invalid decibel value")
    return None if value == -math.inf else float(value)


def _meter_channels(
    channels: Sequence[Mapping[str, object]],
) -> tuple[dict[str, object], ...]:
    if isinstance(channels, (str, bytes)) or not 1 <= len(channels) <= MAX_METER_CHANNELS:
        raise ValueError("meter telemetry must contain one or two channels")
    normalized: list[dict[str, object]] = []
    required = {"peak_dbfs", "rms_dbfs", "clipping", "no_signal"}
    for channel in channels:
        if not isinstance(channel, Mapping) or set(channel) != required:
            raise ValueError("meter channel fields are invalid")
        peak = _finite_or_none(channel["peak_dbfs"], "peak_dbfs")
        rms = _finite_or_none(channel["rms_dbfs"], "rms_dbfs")
        clipping = channel["clipping"]
        no_signal = channel["no_signal"]
        if type(clipping) is not bool or type(no_signal) is not bool:
            raise ValueError("meter flags must be booleans")
        normalized.append(
            {
                "peak_dbfs": peak,
                "rms_dbfs": rms,
                "clipping": clipping,
                "no_signal": no_signal,
            }
        )
    return tuple(normalized)


def _spectrum_values(
    magnitudes: Sequence[int | float], maximum: int
) -> tuple[float, ...]:
    if isinstance(magnitudes, (str, bytes)) or not 1 <= len(magnitudes) <= maximum:
        raise ValueError("spectrum telemetry exceeds its configured bound")
    result = tuple(_finite_number(value, "spectrum magnitude") for value in magnitudes)
    return result


def _finite_or_none(value: object, name: str) -> float | None:
    if value is None:
        return None
    return _finite_number(value, name)


def _finite_number(value: object, name: str) -> float:
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
    ):
        raise ValueError(f"{name} must be finite")
    return float(value)


def _copy_telemetry(value: dict[str, object] | None) -> dict[str, object] | None:
    if value is None:
        return None
    result = dict(value)
    if "channels" in result:
        result["channels"] = [dict(channel) for channel in result["channels"]]
    if "magnitudes_db" in result:
        result["magnitudes_db"] = list(result["magnitudes_db"])
    return result


def _safe_queue_metrics(value: object) -> dict[str, dict[str, int]]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, dict[str, int]] = {}
    for kind in _QUEUE_KINDS:
        raw = value.get(kind)
        if not isinstance(raw, Mapping):
            continue
        metrics: dict[str, int] = {}
        for field in _QUEUE_METRIC_FIELDS:
            item = raw.get(field)
            if type(item) is not int or not 0 <= item <= _MAX_QUEUE_METRIC:
                continue
            if field == "leaky" and item > 2:
                continue
            metrics[field] = item
        if metrics:
            result[kind] = metrics
    return result


def _copy_queue_metrics(
    value: Mapping[str, Mapping[str, int]],
) -> dict[str, dict[str, int]]:
    return {kind: dict(metrics) for kind, metrics in value.items()}
