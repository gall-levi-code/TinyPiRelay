from __future__ import annotations

import json
import math
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tinypirelay.media_state import (  # noqa: E402
    CaptureState,
    ConnectionState,
    MediaState,
    OPUS_REPRESENTATION,
    RecordingState,
    ServiceState,
    StreamState,
    classify_storage,
    reduce,
)


PCM_REPRESENTATION = "pcm_s16le_48000_stereo_matroska"


def accepted(state: MediaState, event: str, **payload: object) -> MediaState:
    decision = reduce(state, event, **payload)
    if not decision.ok:
        raise AssertionError(decision.error)
    return decision.state


def running_capture() -> MediaState:
    state = accepted(MediaState(), "capture.start", config_revision="capture-v1")
    return accepted(
        state, "capture.started", generation=state.capture_generation
    )


def running_stream(representation: str = PCM_REPRESENTATION) -> MediaState:
    state = running_capture()
    payload: dict[str, object] = {
        "config_revision": "stream-v1",
        "representation_id": representation,
    }
    if representation == OPUS_REPRESENTATION:
        payload["opus_bitrate_bps"] = 128_000
    state = accepted(state, "stream.start", **payload)
    return accepted(state, "stream.started", generation=state.stream_generation)


def running_recording(state: MediaState | None = None) -> MediaState:
    state = state or running_capture()
    state = accepted(
        state,
        "recording.start",
        config_revision="recording-v1",
        storage_status="safe",
    )
    return accepted(
        state, "recording.started", generation=state.recording_generation
    )


class MediaStateTests(unittest.TestCase):
    def test_snapshot_and_decision_are_json_safe_and_secret_redacted(self) -> None:
        decision = reduce(
            MediaState(),
            "capture.start",
            config_revision="capture-v1",
            passphrase="must-not-be-retained",
        )
        self.assertFalse(decision.ok)
        encoded = json.dumps(decision.to_dict(), allow_nan=False)
        self.assertNotIn("must-not-be-retained", encoded)
        self.assertNotIn("passphrase", encoded)
        self.assertEqual("running", MediaState().to_dict()["service"])

    def test_capture_commands_are_serialized_idempotent_and_dependency_checked(self) -> None:
        initial = MediaState()
        start = reduce(initial, "capture.start", config_revision="capture-v1")
        self.assertTrue(start.ok)
        self.assertEqual(("capture.start",), start.effects)
        self.assertEqual(CaptureState.STARTING, start.state.capture)

        repeated = reduce(
            start.state, "capture.start", config_revision="capture-v1"
        )
        self.assertTrue(repeated.ok)
        self.assertFalse(repeated.changed)

        changed = reduce(
            start.state, "capture.start", config_revision="capture-v2"
        )
        self.assertEqual("reconfigure_required", changed.error.code)
        stop_during_start = reduce(start.state, "capture.stop")
        self.assertEqual("busy", stop_during_start.error.code)

        state = accepted(
            start.state,
            "capture.started",
            generation=start.state.capture_generation,
        )
        state = accepted(
            state,
            "stream.start",
            config_revision="stream-v1",
            representation_id=PCM_REPRESENTATION,
        )
        blocked = reduce(state, "capture.stop")
        self.assertEqual("branch_active", blocked.error.code)

    def test_stream_and_connection_are_independent(self) -> None:
        state = running_stream()
        self.assertEqual(StreamState.RUNNING, state.stream)
        self.assertEqual(ConnectionState.CONNECTING, state.connection)
        state = accepted(
            state, "connection.connected", generation=state.stream_generation
        )
        self.assertEqual(StreamState.RUNNING, state.stream)
        self.assertEqual(ConnectionState.CONNECTED, state.connection)

        lost = reduce(
            state,
            "connection.lost",
            generation=state.stream_generation,
            message="fixture receiver stopped",
        )
        self.assertTrue(lost.ok)
        self.assertEqual(StreamState.RUNNING, lost.state.stream)
        self.assertEqual(ConnectionState.RETRY_WAIT, lost.state.connection)
        self.assertEqual(("connection.schedule_retry",), lost.effects)

    def test_reconnect_backoff_is_capped_but_does_not_exhaust(self) -> None:
        state = running_stream()
        delays = []
        for _ in range(10):
            failed = reduce(
                state,
                "connection.failed",
                generation=state.stream_generation,
                message="fixture listener absent",
            )
            self.assertTrue(failed.ok)
            state = failed.state
            delays.append(state.reconnect_delay_seconds)
            state = accepted(
                state,
                "connection.retry_due",
                generation=state.stream_generation,
            )

        self.assertEqual([1, 2, 4, 8, 16, 30, 30, 30, 30, 30], delays)
        self.assertEqual(10, state.reconnect_count)
        self.assertEqual(ConnectionState.CONNECTING, state.connection)

        stale = reduce(
            state,
            "connection.retry_due",
            generation=state.stream_generation + 1,
        )
        self.assertTrue(stale.ok)
        self.assertFalse(stale.changed)

    def test_nonretryable_connection_failure_is_explicit(self) -> None:
        state = running_stream()
        decision = reduce(
            state,
            "connection.failed",
            generation=state.stream_generation,
            retryable=False,
            code="invalid_srt_settings",
            message="fixture invalid transport configuration",
        )
        self.assertTrue(decision.ok)
        self.assertEqual(ConnectionState.FAILED, decision.state.connection)
        self.assertIsNone(decision.state.reconnect_delay_seconds)
        self.assertNotIn("connection.schedule_retry", decision.effects)

    def test_repeated_retry_failure_never_schedules_a_second_timer(self) -> None:
        state = running_stream()
        first = reduce(
            state,
            "connection.failed",
            generation=state.stream_generation,
            message="fixture listener absent",
        )
        repeated = reduce(
            first.state,
            "connection.failed",
            generation=state.stream_generation,
            message="fixture listener still absent",
        )
        self.assertTrue(repeated.ok)
        self.assertEqual(ConnectionState.RETRY_WAIT, repeated.state.connection)
        self.assertEqual((), repeated.effects)
        self.assertEqual(first.state.reconnect_delay_seconds, repeated.state.reconnect_delay_seconds)

    def test_runtime_opus_bitrate_requires_confirmation_and_never_stops_stream(self) -> None:
        state = running_stream(OPUS_REPRESENTATION)
        change = reduce(state, "stream.set_opus_bitrate", bitrate_bps=64_000)
        self.assertTrue(change.ok)
        self.assertEqual(("stream.set_opus_bitrate",), change.effects)
        self.assertEqual(128_000, change.state.opus_bitrate_bps)
        self.assertEqual(64_000, change.state.pending_opus_bitrate_bps)

        conflict = reduce(
            change.state, "stream.set_opus_bitrate", bitrate_bps=192_000
        )
        self.assertEqual("bitrate_change_in_progress", conflict.error.code)

        applied = accepted(
            change.state,
            "stream.opus_bitrate_applied",
            generation=change.state.stream_generation,
            bitrate_bps=64_000,
        )
        self.assertEqual(64_000, applied.opus_bitrate_bps)
        self.assertEqual(StreamState.RUNNING, applied.stream)

        pending = accepted(applied, "stream.set_opus_bitrate", bitrate_bps=96_000)
        failed = accepted(
            pending,
            "stream.opus_bitrate_failed",
            generation=pending.stream_generation,
            bitrate_bps=96_000,
            message="fixture property rejection",
        )
        self.assertEqual(StreamState.RUNNING, failed.stream)
        self.assertEqual(64_000, failed.opus_bitrate_bps)
        self.assertIsNone(failed.pending_opus_bitrate_bps)

    def test_storage_classification_uses_exact_destination_threshold(self) -> None:
        self.assertEqual("safe", classify_storage(1_000, 899))
        self.assertEqual("unsafe", classify_storage(1_000, 900))
        self.assertEqual("unsafe", classify_storage(1_000, 901))
        self.assertEqual("unavailable", classify_storage(1_000, 100, writable=False))
        self.assertEqual("unverified", classify_storage(None, None))

    def test_storage_preflight_blocks_and_safe_recheck_clears_latch(self) -> None:
        state = running_capture()
        blocked = reduce(
            state,
            "recording.start",
            config_revision="recording-v1",
            storage_status="unsafe",
        )
        self.assertFalse(blocked.ok)
        self.assertTrue(blocked.changed)
        self.assertEqual(RecordingState.BLOCKED, blocked.state.recording)
        self.assertEqual("storage_unsafe", blocked.state.recording_warning.code)

        cleared = reduce(
            blocked.state,
            "recording.storage_checked",
            generation=blocked.state.recording_generation,
            storage_status="safe",
        )
        self.assertTrue(cleared.ok)
        self.assertEqual(RecordingState.STOPPED, cleared.state.recording)
        self.assertIsNone(cleared.state.recording_warning)

    def test_storage_crossing_and_io_failure_stop_only_recording(self) -> None:
        state = running_recording(running_stream())
        state = accepted(
            state, "connection.connected", generation=state.stream_generation
        )
        blocked = reduce(
            state,
            "recording.storage_checked",
            generation=state.recording_generation,
            storage_status="unsafe",
        )
        self.assertFalse(blocked.ok)
        self.assertEqual(("recording.stop",), blocked.effects)
        self.assertEqual(RecordingState.STOPPING, blocked.state.recording)
        self.assertEqual(StreamState.RUNNING, blocked.state.stream)
        self.assertEqual(ConnectionState.CONNECTED, blocked.state.connection)
        finalized = accepted(
            blocked.state,
            "recording.stopped",
            generation=blocked.state.recording_generation,
        )
        self.assertEqual(RecordingState.BLOCKED, finalized.recording)

        restarted = accepted(
            finalized,
            "recording.start",
            config_revision="recording-v1",
            storage_status="safe",
        )
        restarted = accepted(
            restarted,
            "recording.started",
            generation=restarted.recording_generation,
        )
        io_failure = reduce(
            restarted,
            "recording.failed",
            generation=restarted.recording_generation,
            message="fixture read-only filesystem",
        )
        self.assertTrue(io_failure.ok)
        self.assertEqual(RecordingState.STOPPING, io_failure.state.recording)
        self.assertEqual(RecordingState.FAILED, io_failure.state.recording_stop_target)
        self.assertEqual(StreamState.RUNNING, io_failure.state.stream)

    def test_rotation_and_fragment_events_do_not_change_stream(self) -> None:
        state = running_recording(running_stream())
        generation = state.recording_generation
        state = accepted(
            state,
            "recording.fragment_opened",
            generation=generation,
            path="/recordings/one.flac",
        )
        rotation = reduce(state, "recording.rotate")
        self.assertEqual(("recording.rotate",), rotation.effects)
        self.assertTrue(rotation.state.recording_rotation_pending)
        self.assertEqual(StreamState.RUNNING, rotation.state.stream)
        repeated = reduce(rotation.state, "recording.rotate")
        self.assertFalse(repeated.changed)
        state = accepted(
            rotation.state,
            "recording.fragment_closed",
            generation=generation,
            path="/recordings/one.flac",
        )
        state = accepted(
            state, "recording.rotation_completed", generation=generation
        )
        self.assertEqual("/recordings/one.flac", state.recording_last_finalized_file)
        self.assertFalse(state.recording_rotation_pending)

    def test_device_loss_cascades_only_because_all_branches_share_capture(self) -> None:
        state = running_recording(running_stream())
        decision = reduce(
            state,
            "capture.device_lost",
            generation=state.capture_generation,
            message="fixture USB removal",
        )
        self.assertTrue(decision.ok)
        self.assertEqual(CaptureState.FAILED, decision.state.capture)
        self.assertEqual(StreamState.FAILED, decision.state.stream)
        self.assertEqual(ConnectionState.DISCONNECTED, decision.state.connection)
        self.assertEqual(RecordingState.STOPPING, decision.state.recording)
        self.assertEqual(
            {"stream.stop", "recording.stop"}, set(decision.effects)
        )

    def test_shutdown_is_ordered_and_idempotent(self) -> None:
        state = running_recording(running_stream())
        first = reduce(state, "service.shutdown")
        self.assertTrue(first.ok)
        self.assertEqual(ServiceState.SHUTTING_DOWN, first.state.service)
        self.assertEqual(RecordingState.STOPPING, first.state.recording)
        self.assertEqual(("recording.stop",), first.effects)

        repeated = reduce(first.state, "service.shutdown")
        self.assertTrue(repeated.ok)
        self.assertFalse(repeated.changed)
        self.assertEqual((), repeated.effects)

        stream_stop = reduce(
            first.state,
            "recording.stopped",
            generation=first.state.recording_generation,
        )
        self.assertEqual(StreamState.STOPPING, stream_stop.state.stream)
        self.assertEqual(("stream.stop",), stream_stop.effects)

        capture_stop = reduce(
            stream_stop.state,
            "stream.stopped",
            generation=stream_stop.state.stream_generation,
        )
        self.assertEqual(CaptureState.STOPPING, capture_stop.state.capture)
        self.assertEqual(("capture.stop",), capture_stop.effects)

        complete = reduce(
            capture_stop.state,
            "capture.stopped",
            generation=capture_stop.state.capture_generation,
        )
        self.assertEqual(ServiceState.STOPPED, complete.state.service)
        self.assertEqual(("service.stop",), complete.effects)

        rejected = reduce(complete.state, "capture.start", config_revision="v2")
        self.assertEqual("service_shutting_down", rejected.error.code)

    def test_statistics_are_replaced_and_bounded(self) -> None:
        state = running_stream()
        state = accepted(
            state,
            "connection.stats",
            generation=state.stream_generation,
            stats={"packets_sent": 12, "rtt_ms": 3.5},
        )
        self.assertEqual(
            {"packets_sent": 12, "rtt_ms": 3.5},
            state.to_dict()["connection"]["statistics"],
        )
        invalid = reduce(
            state,
            "connection.stats",
            generation=state.stream_generation,
            stats={str(index): index for index in range(65)},
        )
        self.assertEqual("invalid_command", invalid.error.code)

        nonfinite = reduce(
            state,
            "connection.stats",
            generation=state.stream_generation,
            stats={"rtt_ms": math.nan},
        )
        self.assertEqual("invalid_command", nonfinite.error.code)

        redacted = accepted(
            state,
            "connection.stats",
            generation=state.stream_generation,
            stats={
                "passphrase": "fixture-secret",
                "status": "srt://host:4001?passphrase=fixture-secret&latency=200",
            },
        )
        encoded = json.dumps(redacted.to_dict(), allow_nan=False)
        self.assertNotIn("fixture-secret", encoded)

    def test_unsupported_and_unknown_commands_fail_closed(self) -> None:
        state = running_capture()
        unsupported = reduce(
            state,
            "stream.start",
            config_revision="stream-v1",
            representation_id="experimental_codec",
        )
        self.assertEqual("unsupported_representation", unsupported.error.code)
        unknown = reduce(state, "stream.teleport")
        self.assertEqual("invalid_command", unknown.error.code)


if __name__ == "__main__":
    unittest.main()
