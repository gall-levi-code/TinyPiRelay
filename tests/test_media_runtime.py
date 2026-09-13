from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from concurrent.futures import Future
from dataclasses import dataclass, replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tinypirelay.media_config import (  # noqa: E402
    DestinationStatus,
    validate_config,
)
from tinypirelay.gstreamer_engine import STREAM_QUEUE_NS  # noqa: E402
from tinypirelay.media_runtime import (  # noqa: E402
    CONNECTION_ATTEMPT_TIMEOUT_SECONDS,
    MediaRuntime,
    SPECTRUM_LEASE_SECONDS,
)
from tinypirelay.media_state import (  # noqa: E402
    ConnectionState,
    RecordingState,
    ServiceState,
    StreamState,
)


APPROVED = {
    "pcm_s16le_48000_stereo_matroska",
    "flac_48000_stereo_matroska",
    "opus_128k_48000_stereo_matroska",
}


def media_config(*, passphrase: str | None = None):
    value = json.loads((ROOT / "config/example.json").read_text(encoding="utf-8"))
    value["capture"] = {
        "device_id": "hw:CARD=iMM6C,DEV=0",
        "mode": {"format": "S16LE", "rate_hz": 48000, "channels": 1},
    }
    value["stream"].update(
        {
            "enabled": True,
            "representation_id": "opus_128k_48000_stereo_matroska",
            "destination_host": "192.168.1.174",
            "destination_port": 4001,
            "stream_id": "live_birdmic",
            "passphrase": passphrase,
        }
    )
    value["recording"].update(
        {
            "directory": "/recordings",
            "rotation_seconds": 60,
            "required_mountpoint": None,
        }
    )
    value["monitoring"]["spectrum_bands"] = 64
    return validate_config(value, APPROVED)


def destination(*, safe: bool = True, reason: str = "ok") -> DestinationStatus:
    used = 100 if safe else 900
    return DestinationStatus(
        safe=safe,
        reason=reason,
        directory="/recordings",
        resolved_directory="/recordings",
        required_mountpoint=None,
        resolved_mountpoint=None,
        st_dev=17,
        mount_st_dev=None,
        total_bytes=1000,
        used_bytes=used,
        available_bytes=1000 - used,
        used_percent=used / 10,
        at_or_above_threshold=not safe,
    )


def queue_metrics(seed: int = 1, *, instance_token: int = 1) -> dict[str, int]:
    return {
        "current_buffers": seed,
        "current_bytes": seed * 100,
        "current_time_ns": seed * 1_000,
        "max_buffers": seed + 10,
        "max_bytes": seed * 10_000,
        "max_time_ns": seed * 100_000,
        "leaky": seed % 3,
        "instance_token": instance_token,
    }


@dataclass
class EngineEvent:
    kind: str
    scope: str
    generation: int
    details: dict[str, object]


class ManualTimer:
    def __init__(self, delay: float, callback):
        self.delay = delay
        self.callback = callback
        self.cancelled = False
        self.fired = False

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        if not self.cancelled and not self.fired:
            self.fired = True
            self.callback()


class ManualScheduler:
    def __init__(self) -> None:
        self.timers: list[ManualTimer] = []

    def call_later(self, delay_seconds: float, callback) -> ManualTimer:
        timer = ManualTimer(delay_seconds, callback)
        self.timers.append(timer)
        return timer

    def active(self, delay: float | None = None) -> list[ManualTimer]:
        return [
            timer
            for timer in self.timers
            if not timer.cancelled
            and not timer.fired
            and (delay is None or timer.delay == delay)
        ]

    def fire_all(self, delay: float) -> None:
        for timer in list(self.active(delay)):
            timer.fire()


class BackendSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self.runtime: MediaRuntime | None = None
        self.capture_generation = 0
        self.stream_generation = 0
        self.stream_instance_token = 0
        self.recording_generation = 0
        self.recording_instance_token = 0
        self.recording_path = "/recordings/one.flac"
        self.failures: dict[str, BaseException] = {}
        self.defer_stream_stop = False
        self.defer_recording_stop = False
        self.defer_rotation = False
        self.defer_stream_added = False
        self.fail_rotation_before_playing = False
        self.rotation_forced = False
        self.connection_stats: dict[str, object] | None = None
        self.poll_stats_extra: dict[str, object] = {}
        self.defer_stats = False
        self.deferred_stats: list[tuple[Future, object]] = []
        self.defer_bitrate = False
        self.deferred_bitrate: list[Future] = []
        self.bitrate_results: list[int] = []

    def invoke(self, callback, *args, **kwargs):
        future: Future = Future()
        if self.defer_bitrate and callback == self.set_bitrate:
            self._call("set_bitrate", *args, **kwargs)
            self.deferred_bitrate.append(future)
            return future
        try:
            value = callback(*args, **kwargs)
        except BaseException as exc:
            future.set_exception(exc)
        else:
            if self.defer_stats and callback == self.poll_stats:
                self.deferred_stats.append((future, value))
            else:
                future.set_result(value)
        return future

    def complete_stats(self) -> None:
        future, value = self.deferred_stats.pop(0)
        future.set_result(value)

    def emit(self, kind: str, scope: str, generation: int, **details: object) -> None:
        assert self.runtime is not None
        self.runtime.handle_engine_event(EngineEvent(kind, scope, generation, details))

    def _call(self, name: str, *args: object, **kwargs: object) -> None:
        self.calls.append((name, args, kwargs))
        failure = self.failures.get(name)
        if failure is not None:
            raise failure

    def count(self, name: str) -> int:
        return sum(call[0] == name for call in self.calls)

    def start_capture(self):
        self._call("start_capture")
        self.capture_generation += 1
        self.emit("capture-started", "capture", self.capture_generation)

    def stop_capture(self):
        self._call("stop_capture")
        self.emit("capture-stopped", "capture", self.capture_generation)

    def start_stream(self, generation=None):
        self._call("start_stream", generation=generation)
        self.stream_generation = generation
        self.stream_instance_token += 1
        self.emit(
            "branch-created",
            "stream",
            generation,
            instance_token=self.stream_instance_token,
            representation_id="opus_128k_48000_stereo_matroska",
        )
        if not self.defer_stream_added:
            self.emit(
                "branch-added",
                "stream",
                generation,
                instance_token=self.stream_instance_token,
                representation_id="opus_128k_48000_stereo_matroska",
            )
        return generation

    def stop_stream(self, generation=None):
        self._call("stop_stream", generation=generation)
        if not self.defer_stream_stop:
            self.emit(
                "branch-removed",
                "stream",
                self.stream_generation,
                instance_token=self.stream_instance_token,
                forced=False,
            )
        return self.stream_generation

    def abort_stream_attempt(self, generation=None):
        self._call("abort_stream_attempt", generation=generation)
        if not self.defer_stream_stop:
            self.emit(
                "branch-removed",
                "stream",
                self.stream_generation,
                instance_token=self.stream_instance_token,
                forced=False,
            )
        return self.stream_generation

    def update_config(self, config):
        self._call("update_config", config)

    def set_spectrum_active(self, active):
        self._call("set_spectrum_active", active)
        return bool(
            active
            and self.runtime is not None
            and self.runtime.state.capture.value == "running"
        )

    def set_bitrate(self, bitrate_bps):
        self._call("set_bitrate", bitrate_bps)
        self.emit(
            "opus-bitrate-changed",
            "stream",
            self.stream_generation,
            old_bps=128000,
            new_bps=bitrate_bps,
            instance_token=self.stream_instance_token,
        )
        if self.bitrate_results:
            return self.bitrate_results.pop(0)
        return bitrate_bps

    def start_recording(self, path=None, generation=None):
        self._call("start_recording", path, generation=generation)
        self.recording_generation = generation
        self.recording_instance_token += 1
        self.emit(
            "branch-created",
            "recording",
            generation,
            instance_token=self.recording_instance_token,
            path=self.recording_path,
        )
        self.emit(
            "branch-added",
            "recording",
            generation,
            instance_token=self.recording_instance_token,
            forced=False,
            path=self.recording_path,
        )
        return generation, self.recording_path

    def stop_recording(self, generation=None):
        self._call("stop_recording", generation=generation)
        if not self.defer_recording_stop:
            self.emit(
                "branch-removed",
                "recording",
                self.recording_generation,
                instance_token=self.recording_instance_token,
                forced=False,
                path=self.recording_path,
            )
        return self.recording_generation

    def rotate_recording(self, generation=None):
        self._call("rotate_recording", generation=generation)
        old_path = self.recording_path
        self.recording_path = "/recordings/two.flac"
        old_instance_token = self.recording_instance_token
        self.recording_instance_token += 1
        self.emit(
            "branch-created",
            "recording",
            generation,
            instance_token=self.recording_instance_token,
            path=self.recording_path,
        )
        if self.fail_rotation_before_playing:
            self.emit(
                "error",
                "recording",
                generation,
                instance_token=self.recording_instance_token,
                message="fixture replacement failed before PLAYING",
            )
            return generation, self.recording_path
        if self.defer_rotation:
            return generation, self.recording_path
        self.emit(
            "branch-removed", "recording", generation,
            instance_token=old_instance_token, forced=self.rotation_forced, path=old_path,
        )
        self.emit(
            "recording-rotated",
            "recording",
            generation,
            old_path=old_path,
            new_path=self.recording_path,
            old_instance_token=old_instance_token,
            new_instance_token=self.recording_instance_token,
            forced=self.rotation_forced,
        )
        return generation, self.recording_path

    def poll_stats(self):
        self._call("poll_stats")
        stats = self.connection_stats
        if stats is None:
            stats = {"packets_sent": self.count("poll_stats")}
        result = {"connection": dict(stats)}
        result.update(self.poll_stats_extra)
        return result

    def shutdown(self):
        self._call("shutdown")


def runtime_fixture(*, checks=None, passphrase=None, monotonic=time.monotonic):
    backend = BackendSpy()
    scheduler = ManualScheduler()
    check = checks or (lambda _recording: destination())
    runtime = MediaRuntime(
        media_config(passphrase=passphrase),
        "fixture-v1",
        backend,
        scheduler=scheduler,
        destination_check=check,
        monotonic=monotonic,
    )
    backend.runtime = runtime
    return runtime, backend, scheduler


class MediaRuntimeTests(unittest.TestCase):
    def test_unconfigured_capture_is_rejected_without_starting_or_failing_service(self) -> None:
        config = validate_config(json.loads((ROOT / "config/example.json").read_text(encoding="utf-8")), APPROVED)
        backend = BackendSpy()
        runtime = MediaRuntime(config, "idle-v1", backend, scheduler=ManualScheduler())
        backend.runtime = runtime
        decision = runtime.start_capture()
        self.assertEqual("audio_setup_required", decision.error.code)
        self.assertEqual("running", runtime.state.service.value)
        self.assertEqual("stopped", runtime.state.capture.value)
        self.assertEqual(0, backend.count("start_capture"))
        self.assertIsNone(runtime.telemetry_snapshot()["meter"])
        self.assertIsNone(runtime.telemetry_snapshot()["spectrum"])

    def test_storage_safety_stop_logs_once_without_destination_or_secrets(self) -> None:
        runtime, _backend, _scheduler = runtime_fixture(
            checks=lambda _config: destination(safe=False, reason="storage_threshold"),
            passphrase="secret-passphrase",
        )
        runtime.start_capture()
        with self.assertLogs("tinypirelay.media", level="WARNING") as logged:
            runtime.start_recording()
        self.assertEqual(["WARNING:tinypirelay.media:Recording safety stop: storage_unsafe"], logged.output)
        with self.assertNoLogs("tinypirelay.media", level="WARNING"):
            runtime.start_recording()

    def test_fragment_elapsed_resets_on_rotation_and_freezes_only_after_close(self) -> None:
        now = [1000.0]
        runtime, backend, scheduler = runtime_fixture(monotonic=lambda: now[0])
        self.assertIsNone(runtime.snapshot()["state"]["recording"]["elapsed_seconds"])
        runtime.start_capture()
        runtime.start_recording()
        now[0] = 1012.5
        runtime.post_event("recording.fragment_opened", generation=1, path=backend.recording_path)
        runtime.post_event("recording.fragment_opened", generation=99, path="/stale.flac")
        recording = runtime.snapshot()["state"]["recording"]
        self.assertEqual(12.5, recording["elapsed_seconds"])
        self.assertEqual("/recordings/one.flac", recording["elapsed_file"])

        now[0] = 1060.0
        scheduler.fire_all(60)
        self.assertEqual(0.0, runtime.snapshot()["state"]["recording"]["elapsed_seconds"])
        now[0] = 1068.5
        runtime.stop_recording()
        now[0] = 1100.0
        recording = runtime.snapshot()["state"]["recording"]
        self.assertEqual(8.5, recording["elapsed_seconds"])
        self.assertEqual(recording["last_finalized_file"], recording["elapsed_file"])
        runtime.start_recording()
        self.assertEqual(0.0, runtime.snapshot()["state"]["recording"]["elapsed_seconds"])

    def test_failed_recording_does_not_keep_a_finalized_elapsed_time(self) -> None:
        now = [1.0]
        runtime, backend, _scheduler = runtime_fixture(monotonic=lambda: now[0])
        runtime.start_capture()
        runtime.start_recording()
        backend.defer_recording_stop = True
        now[0] = 13.0
        runtime.stop_recording()
        backend.emit("branch-removed", "recording", 1, instance_token=1, forced=True)
        recording = runtime.snapshot()["state"]["recording"]
        self.assertEqual("failed", recording["state"])
        self.assertIsNone(recording["last_finalized_file"])
        self.assertIsNone(recording["elapsed_file"])
        self.assertIsNone(recording["elapsed_seconds"])

    def test_device_telemetry_context_tracks_active_config_and_current_fragment(self) -> None:
        runtime, _backend, _scheduler = runtime_fixture(monotonic=lambda: 10.0)
        runtime.start_capture()
        runtime.start_recording()
        config, revision, recording = runtime.device_telemetry_context()
        self.assertEqual("fixture-v1", revision)
        self.assertEqual(recording["current_file"], recording["elapsed_file"])
        recording["current_file"] = "mutated"
        self.assertEqual("/recordings/one.flac", runtime.state.recording_current_file)
        runtime.stop_recording()
        updated = replace(config, recording=replace(config.recording, directory="/other"))
        runtime.apply_live_config(updated, "fixture-v2")
        active, revision, _recording = runtime.device_telemetry_context()
        self.assertEqual("/other", active.recording.directory)
        self.assertEqual("fixture-v2", revision)

    def test_spectrum_branch_is_leased_idempotent_and_expires_after_web_crash(
        self,
    ) -> None:
        runtime, backend, scheduler = runtime_fixture()

        runtime.start_capture()
        self.assertEqual(0, backend.count("set_spectrum_active"))

        self.assertTrue(runtime.renew_spectrum_lease())
        first_timer = scheduler.active(SPECTRUM_LEASE_SECONDS)[0]
        self.assertEqual(1, backend.count("set_spectrum_active"))

        runtime.publish_spectrum([-20.0] * 64)
        self.assertTrue(runtime.renew_spectrum_lease())
        self.assertTrue(first_timer.cancelled)
        self.assertEqual(1, backend.count("set_spectrum_active"))
        self.assertEqual(1, len(scheduler.active(SPECTRUM_LEASE_SECONDS)))

        # A killed web process cannot send release; lease expiry is authoritative.
        scheduler.active(SPECTRUM_LEASE_SECONDS)[0].fire()
        self.assertEqual(2, backend.count("set_spectrum_active"))
        self.assertEqual(
            ("set_spectrum_active", (False,), {}),
            backend.calls[-1],
        )
        self.assertIsNone(runtime.telemetry_snapshot()["spectrum"])

        self.assertFalse(runtime.release_spectrum_lease())
        self.assertEqual(2, backend.count("set_spectrum_active"))

    def test_pending_direct_bitrate_blocks_config_before_backend_mutation(self) -> None:
        runtime, backend, _scheduler = runtime_fixture()
        runtime.start_capture()
        runtime.start_stream()
        backend.defer_bitrate = True

        self.assertTrue(runtime.set_opus_bitrate(96_000).ok)
        self.assertEqual(96_000, runtime.state.pending_opus_bitrate_bps)
        before_update_calls = backend.count("update_config")
        candidate = replace(
            media_config(),
            stream=replace(media_config().stream, opus_bitrate_bps=64_000),
        )

        with self.assertRaisesRegex(ValueError, "bitrate change is pending"):
            runtime.apply_live_config(candidate, "fixture-v2")

        self.assertEqual(before_update_calls, backend.count("update_config"))
        self.assertEqual(128_000, runtime.snapshot()["state"]["stream"]["opus_bitrate_bps"])

    def test_live_bitrate_readback_failure_rolls_backend_and_runtime_back(self) -> None:
        runtime, backend, _scheduler = runtime_fixture()
        runtime.start_capture()
        runtime.start_stream()
        backend.bitrate_results = [64_000, 128_000]
        old = media_config()
        candidate = replace(
            old,
            stream=replace(old.stream, opus_bitrate_bps=96_000),
        )

        with self.assertRaisesRegex(RuntimeError, "did not confirm"):
            runtime.apply_live_config(candidate, "fixture-v2")

        updates = [call for call in backend.calls if call[0] == "update_config"]
        self.assertEqual([candidate, old], [call[1][0] for call in updates])
        self.assertEqual(
            [96_000, 128_000],
            [call[1][0] for call in backend.calls if call[0] == "set_bitrate"],
        )
        self.assertEqual(128_000, runtime.state.opus_bitrate_bps)

    def test_reentrant_backend_events_are_serialized(self) -> None:
        runtime, backend, _scheduler = runtime_fixture()

        self.assertTrue(runtime.start_capture().ok)
        self.assertTrue(runtime.start_stream().ok)
        self.assertTrue(runtime.start_recording().ok)
        self.assertEqual("running", runtime.state.capture.value)
        self.assertEqual(StreamState.RUNNING, runtime.state.stream)
        self.assertEqual(RecordingState.RUNNING, runtime.state.recording)
        self.assertEqual(1, backend.count("start_capture"))
        self.assertEqual(1, backend.count("start_stream"))
        self.assertEqual(1, backend.count("start_recording"))

        runtime.start_capture()
        runtime.start_stream()
        runtime.start_recording()
        self.assertEqual(1, backend.count("start_capture"))
        self.assertEqual(1, backend.count("start_stream"))
        self.assertEqual(1, backend.count("start_recording"))

    def test_live_recording_and_opus_config_does_not_restart_stream(self) -> None:
        runtime, backend, _scheduler = runtime_fixture()
        runtime.start_capture()
        runtime.start_stream()
        original_stream_generation = runtime.state.stream_generation
        candidate = media_config()
        candidate = replace(
            candidate,
            stream=replace(candidate.stream, opus_bitrate_bps=96_000),
            recording=replace(candidate.recording, rotation_seconds=120),
        )

        runtime.apply_live_config(candidate, "fixture-v2")

        self.assertEqual(1, backend.count("update_config"))
        self.assertEqual(1, backend.count("set_bitrate"))
        self.assertEqual(0, backend.count("stop_stream"))
        self.assertEqual(original_stream_generation, runtime.state.stream_generation)
        self.assertEqual("fixture-v2", runtime.state.capture_config_revision)
        self.assertEqual("fixture-v2", runtime.state.stream_config_revision)
        self.assertEqual(96_000, runtime.state.opus_bitrate_bps)

    def test_live_recording_config_rejects_an_active_file(self) -> None:
        runtime, backend, _scheduler = runtime_fixture()
        runtime.start_capture()
        runtime.start_recording()
        candidate = media_config()
        candidate = replace(
            candidate,
            recording=replace(candidate.recording, rotation_seconds=120),
        )

        with self.assertRaisesRegex(ValueError, "recording must be stopped"):
            runtime.apply_live_config(candidate, "fixture-v2")
        self.assertEqual(0, backend.count("update_config"))

    def test_concurrent_repeated_commands_never_duplicate_a_branch(self) -> None:
        runtime, backend, _scheduler = runtime_fixture()
        barrier = threading.Barrier(8)
        errors: list[BaseException] = []

        def start() -> None:
            try:
                barrier.wait()
                runtime.start_capture()
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=start) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)

        self.assertEqual([], errors)
        self.assertEqual(1, backend.count("start_capture"))
        self.assertEqual("running", runtime.state.capture.value)

    def test_storage_preflight_latch_and_periodic_crossing(self) -> None:
        current = {"value": destination(safe=False, reason="storage_threshold")}
        runtime, backend, scheduler = runtime_fixture(
            checks=lambda _recording: current["value"]
        )
        runtime.start_capture()

        blocked = runtime.start_recording()
        self.assertFalse(blocked.ok)
        self.assertEqual(RecordingState.BLOCKED, runtime.state.recording)
        self.assertEqual(0, backend.count("start_recording"))
        self.assertEqual(1, len(scheduler.active(5.0)))

        current["value"] = destination()
        scheduler.fire_all(5.0)
        self.assertEqual(RecordingState.STOPPED, runtime.state.recording)

        runtime.start_recording()
        self.assertEqual(RecordingState.RUNNING, runtime.state.recording)
        current["value"] = destination(safe=False, reason="storage_threshold")
        scheduler.fire_all(5.0)
        self.assertEqual(RecordingState.BLOCKED, runtime.state.recording)
        self.assertEqual(1, backend.count("stop_recording"))

    def test_reconnect_has_one_capped_timer_and_rebuilds_the_stream_branch(self) -> None:
        runtime, backend, scheduler = runtime_fixture()
        runtime.start_capture()
        runtime.start_stream()
        backend.emit(
            "connection",
            "stream",
            1,
            state="connected",
            peer="fixture",
            instance_token=1,
        )
        self.assertEqual(ConnectionState.CONNECTED, runtime.state.connection)

        backend.emit(
            "connection",
            "stream",
            1,
            state="disconnected",
            peer="fixture",
            instance_token=1,
        )
        backend.emit(
            "connection",
            "stream",
            1,
            state="disconnected",
            peer="fixture",
            instance_token=1,
        )
        self.assertEqual(ConnectionState.RETRY_WAIT, runtime.state.connection)
        self.assertEqual(1, runtime.state.reconnect_delay_seconds)

        scheduler.fire_all(1.0)
        self.assertEqual(2, backend.count("start_stream"))
        self.assertEqual(1, runtime.state.reconnect_count)
        self.assertEqual(1, backend.calls[-1][2].get("generation", 1))
        backend.emit(
            "connection",
            "stream",
            1,
            state="connected",
            peer="fixture",
            instance_token=2,
        )
        self.assertEqual(ConnectionState.CONNECTED, runtime.state.connection)

    def test_connection_attempt_timeout_stops_only_current_physical_branch(self) -> None:
        runtime, backend, scheduler = runtime_fixture()
        backend.defer_stream_added = True
        backend.defer_stream_stop = True
        runtime.start_capture()
        runtime.start_stream()

        self.assertLess(
            int(CONNECTION_ATTEMPT_TIMEOUT_SECONDS * 1_000_000_000),
            STREAM_QUEUE_NS,
        )
        attempt = runtime._connection_attempt_timer
        self.assertIsNotNone(attempt)
        self.assertEqual(CONNECTION_ATTEMPT_TIMEOUT_SECONDS, attempt.delay)

        attempt.fire()

        self.assertEqual(ConnectionState.CONNECTING, runtime.state.connection)
        self.assertIsNone(runtime._reconnect_timer)
        self.assertEqual((1, 1), runtime._connection_timeout_pending)
        runtime.start_capture()
        self.assertEqual((1, 1), runtime._connection_timeout_pending)
        backend.connection_stats = {"bytes-sent-total": 100}
        runtime.poll_stats()
        for kind, details in (
            ("connection", {"state": "connected"}),
            ("connection", {"state": "disconnected"}),
            ("error", {"message": "late attempt failure"}),
            (
                "branch-added",
                {"representation_id": "opus_128k_48000_stereo_matroska"},
            ),
        ):
            backend.emit(
                kind,
                "stream",
                1,
                instance_token=1,
                **details,
            )
        self.assertEqual(ConnectionState.CONNECTING, runtime.state.connection)
        self.assertEqual((), runtime.state.connection_stats)
        backend.emit(
            "branch-removed",
            "stream",
            1,
            instance_token=1,
            forced=False,
        )

        self.assertEqual(StreamState.STARTING, runtime.state.stream)
        self.assertEqual(ConnectionState.RETRY_WAIT, runtime.state.connection)
        self.assertEqual("srt_connection_timeout", runtime.state.connection_error.code)
        self.assertEqual(1, backend.count("abort_stream_attempt"))
        self.assertEqual(0, runtime.state.reconnect_count)
        self.assertIsNone(runtime._connection_attempt_timer)
        self.assertIsNotNone(runtime._reconnect_timer)
        self.assertEqual(1, runtime._reconnect_timer.delay)

    def test_stale_connection_attempt_timeout_cannot_stop_replacement(self) -> None:
        runtime, backend, scheduler = runtime_fixture()
        backend.defer_stream_added = True
        runtime.start_capture()
        runtime.start_stream()
        stale = runtime._connection_attempt_timer
        self.assertIsNotNone(stale)

        backend.emit(
            "connection",
            "stream",
            1,
            state="disconnected",
            instance_token=1,
        )
        self.assertTrue(stale.cancelled)
        runtime._reconnect_timer.fire()
        replacement = runtime._connection_attempt_timer
        self.assertIsNotNone(replacement)
        self.assertIsNot(stale, replacement)

        stale.callback()

        self.assertEqual(0, backend.count("abort_stream_attempt"))
        self.assertEqual(ConnectionState.CONNECTING, runtime.state.connection)
        self.assertIs(replacement, runtime._connection_attempt_timer)

        generation_stale = replacement
        backend.emit(
            "branch-added",
            "stream",
            1,
            instance_token=2,
            representation_id="opus_128k_48000_stereo_matroska",
        )
        runtime.stop_stream()
        runtime.start_stream()
        self.assertTrue(generation_stale.cancelled)
        generation_stale.callback()
        self.assertEqual(1, backend.count("stop_stream"))
        self.assertEqual(2, runtime.state.stream_generation)
        self.assertEqual(ConnectionState.CONNECTING, runtime.state.connection)

    def test_stats_connection_ack_cancels_attempt_timeout(self) -> None:
        runtime, backend, scheduler = runtime_fixture()
        backend.defer_stream_added = True
        backend.connection_stats = {"bytes-sent-total": 1}
        runtime.start_capture()
        runtime.start_stream()
        attempt = runtime._connection_attempt_timer
        self.assertIsNotNone(attempt)

        scheduler.fire_all(1.0)

        self.assertEqual(ConnectionState.CONNECTED, runtime.state.connection)
        self.assertTrue(attempt.cancelled)
        self.assertIsNone(runtime._connection_attempt_timer)
        attempt.callback()
        self.assertEqual(0, backend.count("abort_stream_attempt"))
        self.assertIsNone(runtime._reconnect_timer)

    def test_attempt_abort_failure_is_redacted_and_backed_off(self) -> None:
        secret = "timeout-fixture-secret"
        runtime, backend, _scheduler = runtime_fixture(passphrase=secret)
        backend.defer_stream_added = True
        backend.failures["abort_stream_attempt"] = RuntimeError(
            f"srt://host:4001?passphrase={secret}&latency=200"
        )
        runtime.start_capture()
        runtime.start_stream()

        runtime._connection_attempt_timer.fire()

        self.assertEqual(ConnectionState.RETRY_WAIT, runtime.state.connection)
        self.assertEqual(
            "srt_connection_timeout_stop_failed",
            runtime.state.connection_error.code,
        )
        self.assertNotIn(secret, json.dumps(runtime.snapshot()))
        self.assertEqual(set(), runtime._retiring_instance_tokens)
        self.assertIsNone(runtime._connection_timeout_pending)
        self.assertIsNotNone(runtime._reconnect_timer)

    def test_repeated_attempt_timeouts_use_one_bounded_backoff_timer(self) -> None:
        runtime, backend, _scheduler = runtime_fixture()
        backend.defer_stream_added = True
        runtime.start_capture()
        runtime.start_stream()

        for retry_count, delay in enumerate((1, 2, 4, 8, 16, 30, 30), start=1):
            attempt = runtime._connection_attempt_timer
            self.assertIsNotNone(attempt)
            attempt.fire()
            self.assertEqual(ConnectionState.RETRY_WAIT, runtime.state.connection)
            self.assertIsNone(runtime._connection_attempt_timer)
            retry = runtime._reconnect_timer
            self.assertIsNotNone(retry)
            self.assertEqual(delay, retry.delay)
            starts = backend.count("start_stream")

            retry.fire()

            self.assertEqual(starts + 1, backend.count("start_stream"))
            self.assertEqual(retry_count, runtime.state.reconnect_count)
            self.assertEqual(ConnectionState.CONNECTING, runtime.state.connection)
            self.assertIsNone(runtime._reconnect_timer)
            self.assertIsNotNone(runtime._connection_attempt_timer)

        self.assertEqual(7, backend.count("abort_stream_attempt"))

    def test_rotation_timer_finalizes_old_path_without_changing_stream(self) -> None:
        runtime, backend, scheduler = runtime_fixture()
        runtime.start_capture()
        runtime.start_stream()
        runtime.start_recording()

        scheduler.fire_all(60)

        self.assertEqual(1, backend.count("rotate_recording"))
        self.assertEqual("/recordings/one.flac", runtime.state.recording_last_finalized_file)
        self.assertEqual("/recordings/two.flac", runtime.state.recording_current_file)
        self.assertFalse(runtime.state.recording_rotation_pending)
        self.assertEqual(StreamState.RUNNING, runtime.state.stream)
        self.assertEqual(1, len(scheduler.active(60)))
        self.assertEqual(["/recordings/two.flac"], runtime.snapshot()["state"]["recording"]["active_files"])

    def test_rotation_rechecks_destination_before_opening_replacement(self) -> None:
        for reason in ("storage_threshold", "required_mountpoint_unavailable"):
            with self.subTest(reason=reason):
                status = destination()
                checked = []

                def check(recording):
                    checked.append(recording)
                    return status

                runtime, backend, scheduler = runtime_fixture(checks=check)
                runtime.start_capture()
                runtime.start_stream()
                runtime.start_recording()
                status = destination(safe=False, reason=reason)
                # Trigger rotation before the pending five-second storage poll.
                with self.assertLogs("tinypirelay.media", level="WARNING"):
                    scheduler.fire_all(60)
                self.assertEqual([media_config().recording] * 2, checked)
                self.assertEqual(0, backend.count("rotate_recording"))
                self.assertEqual(1, backend.count("stop_recording"))
                self.assertEqual(RecordingState.BLOCKED, runtime.state.recording)
                self.assertEqual("/recordings/one.flac", runtime.state.recording_last_finalized_file)
                self.assertEqual("running", runtime.state.capture.value)
                self.assertEqual(StreamState.RUNNING, runtime.state.stream)
                self.assertEqual((1, 1), (runtime.state.capture_generation, runtime.state.stream_generation))

    def test_stop_during_rotation_protects_each_physical_fragment_until_removed(self) -> None:
        for shutdown in (False, True):
            with self.subTest(shutdown=shutdown):
                runtime, backend, scheduler = runtime_fixture()
                runtime.start_capture()
                runtime.start_stream()
                runtime.start_recording()
                backend.defer_rotation = backend.defer_recording_stop = True
                scheduler.fire_all(60)
                self.assertEqual("/recordings/two.flac", runtime.state.recording_current_file)
                self.assertEqual(
                    ["/recordings/one.flac", "/recordings/two.flac"],
                    runtime.snapshot()["state"]["recording"]["active_files"],
                )
                runtime.shutdown() if shutdown else runtime.stop_recording()
                backend.emit(
                    "branch-removed", "recording", 1,
                    instance_token=2, forced=False, path="/recordings/two.flac",
                )
                self.assertEqual("/recordings/two.flac", runtime.state.recording_last_finalized_file)
                self.assertEqual(
                    ["/recordings/one.flac"], runtime.snapshot()["state"]["recording"]["active_files"],
                )
                # The old fragment can finish last, including forced removal;
                # it must not change the replacement's confirmed finalization.
                backend.emit(
                    "branch-removed", "recording", 1,
                    instance_token=1, forced=True, path="/recordings/one.flac",
                )
                self.assertEqual([], runtime.snapshot()["state"]["recording"]["active_files"])
                self.assertEqual("/recordings/two.flac", runtime.state.recording_last_finalized_file)
                if not shutdown:
                    self.assertEqual(StreamState.RUNNING, runtime.state.stream)

    def test_shutdown_completion_clears_physical_recording_paths(self) -> None:
        runtime, backend, _scheduler = runtime_fixture()
        runtime.start_capture()
        runtime.start_recording()
        backend.emit("shutdown-complete", "capture", 1)
        self.assertEqual([], runtime.snapshot()["state"]["recording"]["active_files"])

    def test_retired_branch_removal_cannot_complete_a_new_stream_stop(self) -> None:
        runtime, backend, scheduler = runtime_fixture()
        runtime.start_capture()
        runtime.start_stream()
        backend.emit(
            "connection",
            "stream",
            1,
            state="disconnected",
            instance_token=1,
        )
        scheduler.fire_all(1.0)
        self.assertEqual(2, backend.stream_instance_token)

        backend.defer_stream_stop = True
        runtime.stop_stream()
        self.assertEqual(StreamState.STOPPING, runtime.state.stream)

        backend.emit(
            "branch-removed",
            "stream",
            1,
            instance_token=1,
            forced=False,
        )
        self.assertEqual(StreamState.STOPPING, runtime.state.stream)

        backend.emit(
            "branch-removed",
            "stream",
            1,
            instance_token=2,
            forced=False,
        )
        self.assertEqual(StreamState.STOPPED, runtime.state.stream)

    def test_current_retiring_branch_removal_completes_stream_stop(self) -> None:
        runtime, backend, _scheduler = runtime_fixture()
        runtime.start_capture()
        runtime.start_stream()
        backend.emit(
            "connection",
            "stream",
            1,
            state="disconnected",
            instance_token=1,
        )

        backend.defer_stream_stop = True
        runtime.stop_stream()
        backend.emit(
            "branch-removed",
            "stream",
            1,
            instance_token=1,
            forced=False,
        )

        self.assertEqual(StreamState.STOPPED, runtime.state.stream)

    def test_reconnect_failures_before_playing_keep_token_tracking_bounded(self) -> None:
        runtime, backend, scheduler = runtime_fixture()
        runtime.start_capture()
        runtime.start_stream()
        backend.defer_stream_added = True

        delays = (1.0, 2.0, 4.0, 8.0, 16.0)
        for expected_token, delay in enumerate(delays, start=1):
            backend.emit(
                "connection",
                "stream",
                1,
                state="disconnected",
                instance_token=expected_token,
            )
            backend.emit(
                "branch-removed",
                "stream",
                1,
                instance_token=expected_token,
                forced=False,
            )
            self.assertNotIn("stream", runtime._backend_instance_tokens)
            self.assertEqual(set(), runtime._retiring_instance_tokens)
            backend.emit(
                "connection",
                "stream",
                1,
                state="connected",
                instance_token=expected_token,
            )
            self.assertEqual(ConnectionState.RETRY_WAIT, runtime.state.connection)

            scheduler.fire_all(delay)
            self.assertEqual(
                expected_token + 1,
                runtime._backend_instance_tokens["stream"],
            )
            self.assertEqual(ConnectionState.CONNECTING, runtime.state.connection)

        backend.emit(
            "connection",
            "stream",
            1,
            state="disconnected",
            instance_token=6,
        )
        backend.emit(
            "branch-removed",
            "stream",
            1,
            instance_token=6,
            forced=False,
        )
        self.assertEqual(set(), runtime._retiring_instance_tokens)
        self.assertEqual(ConnectionState.RETRY_WAIT, runtime.state.connection)

    def test_rotation_replacement_error_before_playing_is_not_stale(self) -> None:
        runtime, backend, scheduler = runtime_fixture()
        runtime.start_capture()
        runtime.start_stream()
        runtime.start_recording()
        backend.fail_rotation_before_playing = True

        scheduler.fire_all(60)

        self.assertEqual(RecordingState.FAILED, runtime.state.recording)
        self.assertEqual("recording_io_failed", runtime.state.recording_error.code)
        self.assertIsNone(runtime.state.recording_last_finalized_file)
        self.assertEqual(StreamState.RUNNING, runtime.state.stream)
        self.assertEqual(set(), runtime._retiring_instance_tokens)

    def test_missing_branch_ack_completes_stop_without_false_finalization(self) -> None:
        runtime, backend, _scheduler = runtime_fixture()
        runtime.start_capture()
        runtime.start_stream()
        backend.defer_stream_stop = True
        runtime.stop_stream()

        backend.emit(
            "branch-removed",
            "stream",
            1,
            instance_token=None,
            forced=False,
            missing=True,
            path="",
        )

        self.assertEqual(StreamState.STOPPED, runtime.state.stream)
        self.assertEqual(1, backend.calls[-1][2]["generation"])

    def test_forced_recording_removal_never_claims_file_finalization(self) -> None:
        runtime, backend, _scheduler = runtime_fixture()
        runtime.start_capture()
        runtime.start_stream()
        runtime.start_recording()
        backend.defer_recording_stop = True
        runtime.stop_recording()

        backend.emit(
            "branch-removed",
            "recording",
            1,
            instance_token=1,
            forced=True,
            path="/recordings/one.flac",
        )

        self.assertEqual(RecordingState.FAILED, runtime.state.recording)
        self.assertIsNone(runtime.state.recording_last_finalized_file)
        self.assertEqual(
            "recording_finalization_failed", runtime.state.recording_error.code
        )
        self.assertEqual(StreamState.RUNNING, runtime.state.stream)

    def test_forced_rotation_fails_without_finalizing_the_old_file(self) -> None:
        runtime, backend, scheduler = runtime_fixture()
        runtime.start_capture()
        runtime.start_stream()
        runtime.start_recording()
        backend.rotation_forced = True

        scheduler.fire_all(60)

        self.assertEqual(RecordingState.FAILED, runtime.state.recording)
        self.assertIsNone(runtime.state.recording_last_finalized_file)
        self.assertEqual(
            "recording_finalization_failed", runtime.state.recording_error.code
        )
        self.assertEqual(1, backend.count("stop_recording"))
        self.assertEqual(StreamState.RUNNING, runtime.state.stream)

    def test_forced_stream_removal_records_failure_and_advances_shutdown(self) -> None:
        runtime, backend, _scheduler = runtime_fixture()
        runtime.start_capture()
        runtime.start_stream()
        backend.defer_stream_stop = True

        runtime.shutdown()
        self.assertEqual(StreamState.STOPPING, runtime.state.stream)
        backend.emit(
            "branch-removed",
            "stream",
            1,
            instance_token=1,
            forced=True,
        )

        self.assertEqual(ServiceState.STOPPED, runtime.state.service)
        self.assertEqual(StreamState.FAILED, runtime.state.stream)
        self.assertEqual("stream_finalization_failed", runtime.state.stream_error.code)
        self.assertEqual(1, backend.count("shutdown"))

    def test_telemetry_is_numeric_bounded_and_latest_only(self) -> None:
        runtime, backend, _scheduler = runtime_fixture()
        channels = [
            {
                "peak_dbfs": -3.0,
                "rms_dbfs": -18.0,
                "clipping": False,
                "no_signal": False,
            }
        ]
        for index in range(1000):
            channels[0]["peak_dbfs"] = -float(index)
            runtime.publish_meter(channels)
        runtime.publish_spectrum([-20.0] * 64)

        snapshot = runtime.telemetry_snapshot()
        self.assertEqual(1000, snapshot["meter"]["sequence"])
        self.assertEqual(-999.0, snapshot["meter"]["channels"][0]["peak_dbfs"])
        self.assertEqual(64, len(snapshot["spectrum"]["magnitudes_db"]))
        with self.assertRaises(ValueError):
            runtime.publish_spectrum([-20.0] * 65)

        backend.emit(
            "telemetry",
            "level",
            1,
            rms_db=[-120.0],
            peak_db=[-110.0],
            decay_db=[-110.0],
            clipped=False,
        )
        self.assertTrue(
            runtime.telemetry_snapshot()["meter"]["channels"][0]["no_signal"]
        )
        backend.emit(
            "telemetry",
            "spectrum",
            1,
            magnitude_db=[-30.0] * 64,
        )
        self.assertEqual(
            -30.0,
            runtime.telemetry_snapshot()["spectrum"]["magnitudes_db"][0],
        )
        backend.emit(
            "error",
            "level",
            0,
            message="fixture monitoring callback failed",
        )
        self.assertEqual(
            "monitoring_failed", runtime.state.monitoring_error.code
        )

    def test_stats_poll_once_per_second_until_capture_stops(self) -> None:
        runtime, backend, scheduler = runtime_fixture()
        runtime.start_capture()

        self.assertEqual(1, len(scheduler.active(1.0)))
        scheduler.fire_all(1.0)
        self.assertEqual(1, backend.count("poll_stats"))
        self.assertEqual(1, len(scheduler.active(1.0)))

        runtime.start_stream()

        scheduler.fire_all(1.0)
        self.assertEqual(2, backend.count("poll_stats"))
        self.assertEqual(
            2, runtime.state.to_dict()["connection"]["statistics"]["packets_sent"]
        )
        self.assertEqual(1, len(scheduler.active(1.0)))

        runtime.stop_stream()
        polls = backend.count("poll_stats")
        scheduler.fire_all(1.0)
        self.assertEqual(polls + 1, backend.count("poll_stats"))

        runtime.stop_capture()
        polls = backend.count("poll_stats")
        self.assertEqual([], scheduler.active(1.0))
        scheduler.fire_all(1.0)
        self.assertEqual(polls, backend.count("poll_stats"))

    def test_recording_only_capture_exposes_queue_metrics(self) -> None:
        runtime, backend, scheduler = runtime_fixture()
        runtime.start_capture()
        runtime.start_recording()
        recording = queue_metrics(2, instance_token=backend.recording_instance_token)
        backend.poll_stats_extra = {"queues": {"recording": recording}}

        scheduler.fire_all(1.0)

        expected = dict(recording)
        expected.pop("instance_token")
        self.assertEqual(1, backend.count("poll_stats"))
        self.assertEqual(
            {"recording": expected},
            runtime.snapshot()["runtime_metrics"]["queues"],
        )

    def test_queue_metrics_are_known_numeric_bounded_and_clear_with_scopes(
        self,
    ) -> None:
        runtime, backend, scheduler = runtime_fixture()
        runtime.start_capture()
        runtime.start_stream()
        runtime.start_recording()
        self.assertTrue(runtime.renew_spectrum_lease())
        recording = queue_metrics(2, instance_token=7)
        level = queue_metrics(0, instance_token=8)
        spectrum = queue_metrics(3, instance_token=9)
        backend.poll_stats_extra = {
            "queues": {
                "stream": {
                    "current_buffers": 2,
                    "current_bytes": -1,
                    "current_time_ns": 1 << 64,
                    "max_buffers": True,
                    "max_bytes": "10",
                    "max_time_ns": 123,
                    "leaky": 3,
                    "instance_token": 1,
                    "unknown": 99,
                },
                "recording": recording,
                "level": level,
                "spectrum": spectrum,
                "unknown": queue_metrics(4),
            }
        }

        scheduler.fire_all(1.0)

        recording.pop("instance_token")
        level.pop("instance_token")
        spectrum.pop("instance_token")
        self.assertEqual(
            {
                "stream": {"current_buffers": 2, "max_time_ns": 123},
                "recording": recording,
                "level": level,
                "spectrum": spectrum,
            },
            runtime.snapshot()["runtime_metrics"]["queues"],
        )

        runtime.stop_stream()
        self.assertNotIn(
            "stream", runtime.snapshot()["runtime_metrics"]["queues"]
        )
        runtime.stop_recording()
        self.assertNotIn(
            "recording", runtime.snapshot()["runtime_metrics"]["queues"]
        )
        runtime.release_spectrum_lease()
        self.assertNotIn(
            "spectrum", runtime.snapshot()["runtime_metrics"]["queues"]
        )
        runtime.stop_capture()
        self.assertEqual({}, runtime.snapshot()["runtime_metrics"]["queues"])

    def test_deferred_queue_metrics_cannot_survive_capture_stop(self) -> None:
        runtime, backend, scheduler = runtime_fixture()
        runtime.start_capture()
        backend.poll_stats_extra = {
            "queues": {"level": queue_metrics(2, instance_token=1)}
        }
        backend.defer_stats = True
        scheduler.fire_all(1.0)

        runtime.stop_capture()
        backend.complete_stats()

        self.assertEqual({}, runtime.snapshot()["runtime_metrics"]["queues"])
        self.assertEqual([], scheduler.active(1.0))

    def test_malformed_poll_result_clears_previous_queue_metrics(self) -> None:
        runtime, backend, scheduler = runtime_fixture()
        runtime.start_capture()
        backend.poll_stats_extra = {
            "queues": {"level": queue_metrics(2, instance_token=1)}
        }
        scheduler.fire_all(1.0)
        self.assertIn("level", runtime.snapshot()["runtime_metrics"]["queues"])

        backend.poll_stats = lambda: ["not", "a", "mapping"]
        scheduler.fire_all(1.0)

        self.assertEqual({}, runtime.snapshot()["runtime_metrics"]["queues"])

    def test_failed_poll_clears_previous_queue_metrics(self) -> None:
        runtime, backend, scheduler = runtime_fixture()
        runtime.start_capture()
        backend.poll_stats_extra = {
            "queues": {"level": queue_metrics(2, instance_token=1)}
        }
        scheduler.fire_all(1.0)
        self.assertIn("level", runtime.snapshot()["runtime_metrics"]["queues"])

        backend.failures["poll_stats"] = RuntimeError("controlled poll failure")
        scheduler.fire_all(1.0)

        snapshot = runtime.snapshot()
        self.assertEqual({}, snapshot["runtime_metrics"]["queues"])
        self.assertEqual("stats_poll_failed", snapshot["runtime_error"]["code"])

    def test_polled_telemetry_does_not_duplicate_engine_events(self) -> None:
        runtime, backend, scheduler = runtime_fixture()
        runtime.start_capture()
        self.assertTrue(runtime.renew_spectrum_lease())
        backend.emit(
            "telemetry",
            "level",
            1,
            rms_db=[-18.0],
            peak_db=[-3.0],
            decay_db=[-3.0],
            clipped=False,
        )
        backend.emit(
            "telemetry",
            "spectrum",
            1,
            magnitude_db=[-30.0] * 64,
        )
        before = runtime.telemetry_snapshot()
        backend.poll_stats_extra = {
            "level": {
                "rms_db": [-40.0],
                "peak_db": [-20.0],
                "decay_db": [-20.0],
                "clipped": False,
            },
            "spectrum": {"magnitude_db": [-60.0] * 64},
        }

        scheduler.fire_all(1.0)

        self.assertEqual(before, runtime.telemetry_snapshot())

    def test_positive_polled_srt_progress_acknowledges_caller_connection(self) -> None:
        runtime, backend, scheduler = runtime_fixture()
        runtime.start_capture()
        runtime.start_stream()
        backend.connection_stats = {"bytes-sent-total": 0}

        scheduler.fire_all(1.0)
        self.assertEqual(ConnectionState.CONNECTING, runtime.state.connection)

        backend.connection_stats = {"bytes-sent-total": 1}
        scheduler.fire_all(1.0)
        self.assertEqual(ConnectionState.CONNECTED, runtime.state.connection)
        self.assertEqual(1, dict(runtime.state.connection_stats)["bytes-sent-total"])

        backend.emit(
            "connection",
            "stream",
            1,
            state="disconnected",
            instance_token=backend.stream_instance_token,
        )
        self.assertEqual(ConnectionState.RETRY_WAIT, runtime.state.connection)
        self.assertIsNotNone(runtime._reconnect_timer)
        self.assertFalse(runtime._reconnect_timer.cancelled)

    def test_stale_polled_progress_cannot_acknowledge_a_rebuilt_stream(self) -> None:
        runtime, backend, scheduler = runtime_fixture()
        runtime.start_capture()
        runtime.start_stream()
        backend.connection_stats = {"bytes-sent-total": 100}
        backend.defer_stats = True
        scheduler.fire_all(1.0)

        backend.emit(
            "connection",
            "stream",
            1,
            state="disconnected",
            instance_token=1,
        )
        scheduler.fire_all(1.0)
        self.assertEqual(2, backend.stream_instance_token)
        self.assertEqual(ConnectionState.CONNECTING, runtime.state.connection)

        backend.complete_stats()
        self.assertEqual(ConnectionState.CONNECTING, runtime.state.connection)
        self.assertEqual((), runtime.state.connection_stats)

        backend.defer_stats = False
        scheduler.fire_all(1.0)
        self.assertEqual(ConnectionState.CONNECTED, runtime.state.connection)

    def test_stale_generation_polled_progress_is_ignored_after_restart(self) -> None:
        runtime, backend, scheduler = runtime_fixture()
        runtime.start_capture()
        runtime.start_stream()
        backend.connection_stats = {"bytes-sent-total": 100}
        backend.defer_stats = True
        scheduler.fire_all(1.0)

        runtime.stop_stream()
        runtime.start_stream()
        self.assertEqual(2, runtime.state.stream_generation)
        backend.complete_stats()

        self.assertEqual(ConnectionState.CONNECTING, runtime.state.connection)
        self.assertEqual((), runtime.state.connection_stats)

    def test_shutdown_is_ordered_idempotent_and_cancels_timers(self) -> None:
        runtime, backend, scheduler = runtime_fixture()
        runtime.start_capture()
        runtime.start_stream()
        runtime.start_recording()
        attempt = runtime._connection_attempt_timer
        self.assertIsNotNone(attempt)
        backend.calls.clear()

        decision = runtime.shutdown()

        self.assertTrue(decision.ok)
        self.assertEqual(ServiceState.STOPPED, runtime.state.service)
        self.assertEqual(
            ["stop_recording", "stop_stream", "stop_capture", "shutdown"],
            [call[0] for call in backend.calls],
        )
        self.assertEqual([], scheduler.active())
        self.assertTrue(attempt.cancelled)
        count = len(backend.calls)
        attempt.callback()
        runtime.shutdown()
        self.assertEqual(count, len(backend.calls))

    def test_backend_errors_and_snapshots_never_expose_passphrase(self) -> None:
        secret = "fixture-secret-value"
        runtime, backend, _scheduler = runtime_fixture(passphrase=secret)
        runtime.start_capture()
        backend.failures["start_stream"] = RuntimeError(
            f"srt://host:4001?passphrase={secret}&latency=200"
        )

        runtime.start_stream()

        encoded = json.dumps(runtime.snapshot(), allow_nan=False)
        self.assertNotIn(secret, encoded)
        self.assertNotIn(f"passphrase={secret}", encoded)
        self.assertEqual(StreamState.FAILED, runtime.state.stream)

    def test_connection_loss_preserves_redacted_backend_diagnostics(self) -> None:
        secret = "fixture-secret-value"
        runtime, backend, _scheduler = runtime_fixture(passphrase=secret)
        runtime.start_capture()
        runtime.start_stream()

        backend.emit(
            "connection",
            "stream",
            1,
            state="disconnected",
            instance_token=1,
            code="queue_overrun",
            message=f"stream queue failed near passphrase={secret}",
        )

        self.assertEqual("queue_overrun", runtime.state.connection_error.code)
        self.assertIn("stream queue failed", runtime.state.connection_error.message)
        self.assertNotIn(secret, runtime.state.connection_error.message)
        self.assertIn("<redacted>", runtime.state.connection_error.message)

    def test_stale_backend_generation_is_ignored(self) -> None:
        runtime, backend, _scheduler = runtime_fixture()
        runtime.start_capture()
        backend.emit("capture-stopped", "capture", 99)

        self.assertEqual("running", runtime.state.capture.value)


if __name__ == "__main__":
    unittest.main()
