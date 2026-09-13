from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest

from spikes import step2_media_integration as spike


def flac(path: str) -> dict[str, object]:
    return {
        "path": path,
        "exists": True,
        "size_bytes": 4096,
        "flac_header": True,
        "decoded_to_eos": True,
        "decoded_buffers": 50,
        "error": None,
    }


def marker(
    buffer_count: int,
    last_pts_ns: int,
    *,
    reconnect_count: int = 0,
    tokens: tuple[int, ...] = (1,),
    connection_loss_events: int = 0,
    missing_pts: int = 0,
    non_monotonic_pts: int = 0,
) -> dict[str, object]:
    return {
        "reconnect_count": reconnect_count,
        "branch_created_stream_count": len(tokens),
        "stream_instance_tokens": list(tokens),
        "connection_loss_events": connection_loss_events,
        "receiver": {
            "buffer_count": buffer_count,
            "last_pts_ns": last_pts_ns,
            "missing_pts": missing_pts,
            "non_monotonic_pts": non_monotonic_pts,
        },
    }


def queue(
    token: int,
    *,
    max_buffers: int,
    max_bytes: int,
    max_time_ns: int,
    leaky: int,
) -> dict[str, int]:
    return {
        "current_buffers": min(1, max_buffers) if max_buffers else 0,
        "current_bytes": 0,
        "current_time_ns": min(20_000_000, max_time_ns) if max_time_ns else 0,
        "max_buffers": max_buffers,
        "max_bytes": max_bytes,
        "max_time_ns": max_time_ns,
        "leaky": leaky,
        "instance_token": token,
    }


def passing_report() -> dict[str, object]:
    queues = {
        "stream": queue(
            1, max_buffers=0, max_bytes=0, max_time_ns=spike.STREAM_QUEUE_NS, leaky=0
        ),
        "recording": queue(
            2,
            max_buffers=0,
            max_bytes=0,
            max_time_ns=spike.RECORDING_QUEUE_NS,
            leaky=0,
        ),
        "level": queue(
            3,
            max_buffers=spike.MONITOR_QUEUE_BUFFERS,
            max_bytes=0,
            max_time_ns=0,
            leaky=2,
        ),
        "spectrum": queue(
            4,
            max_buffers=spike.MONITOR_QUEUE_BUFFERS,
            max_bytes=0,
            max_time_ns=0,
            leaky=2,
        ),
    }
    stats = {"bytes-sent-total": 65_536, "packets-sent-total": 100}
    baseline = marker(25, 1_000_000_000)
    pre_disconnect = marker(300, 10_000_000_000)
    return {
        "setup_errors": [],
        "scenario_errors": [],
        "fatal_errors": [],
        "engine_event_counts": {"connection:stream": 2},
        "receiver": {
            "bus_errors": [],
            "buffers": marker(400, 2_000_000_000)["receiver"],
        },
        "recording_output": {
            "directory": "/tmp/step2-retained",
            "explicit_directory": True,
            "retained_after_run": True,
            "cleanup_policy": "retained",
        },
        "scenarios": {
            "startup": {
                "capture_state": "running",
                "stream_state": "running",
                "connection_state": "connected",
                "capture_generation": 1,
                "stream_generation": 1,
                "receiver_buffer_delta": 25,
                "continuity": baseline,
            },
            "bitrate": {
                "initial_bps": 128_000,
                "transitions": [
                    {
                        "requested_bps": target,
                        "readback_bps": target,
                        "capture_generation": 1,
                        "stream_generation": 1,
                        "stream_state": "running",
                        "connection_state": "connected",
                        "receiver_buffer_delta": 25,
                        "continuity_before": marker(
                            25 + index * 25, 1_000_000_000 + index * 1_000_000_000
                        ),
                        "continuity_after": marker(
                            50 + index * 25, 2_000_000_000 + index * 1_000_000_000
                        ),
                    }
                    for index, target in enumerate((64_000, 192_000))
                ],
            },
            "recording": {
                "representation": "native_capture_flac",
                "paths": ["/tmp/one.flac", "/tmp/two.flac"],
                "validations": [flac("/tmp/one.flac"), flac("/tmp/two.flac")],
                "recording_state": "stopped",
                "last_finalized_file": "/tmp/two.flac",
                "capture_generation": 1,
                "stream_generation": 1,
                "receiver_buffer_delta": 100,
                "continuity_before": marker(75, 3_000_000_000),
                "continuity_after": marker(175, 6_000_000_000),
            },
            "concurrent_branches": {
                "stream_state": "running",
                "recording_state": "running",
                "level_events": 30,
                "spectrum_events": 30,
                "engine_srt_statistics": stats,
                "runtime_srt_statistics": stats,
                "queues": queues,
                "continuity": marker(100, 4_000_000_000),
            },
            "storage_threshold": {
                "threshold_simulated": True,
                "used_percent": 90.0,
                "recording_state": "blocked",
                "recording_warning_code": "storage_unsafe",
                "validation": flac("/tmp/three.flac"),
                "stream_state": "running",
                "connection_state": "connected",
                "capture_generation": 1,
                "stream_generation": 1,
                "receiver_buffer_delta_after_stop": 25,
                "latch_cleared_state": "stopped",
                "continuity_before": marker(175, 6_000_000_000),
                "continuity_after": marker(225, 8_000_000_000),
            },
            "reconnect": {
                "receiver_stopped": True,
                "receiver_restored": True,
                "retry_count_delta": 2,
                "first_retry_delay_seconds": 1.02,
                "attempt_timeout_seconds": spike.CONNECTION_ATTEMPT_TIMEOUT_SECONDS,
                "offline_attempt_duration_seconds": 5.02,
                "offline_attempt_cause": "runtime_watchdog",
                "offline_attempt_error_code": "srt_connection_timeout",
                "offline_attempt_error_message": "SRT caller connection attempt timed out",
                "offline_attempt_token": 5,
                "offline_attempt_removal": {
                    "instance_token": 5,
                    "forced": False,
                    "missing": False,
                },
                "fallback_error_code": "srt_connection_timeout",
                "retry_count_at_receiver_restore_delta": 1,
                "branch_count_at_receiver_restore_delta": 1,
                "receiver_restore_state": "retry_wait",
                "recovery_retry_delay_seconds": 2.02,
                "recovery_retry_count_delta": 1,
                "recovered_instance_token": 6,
                "connection_state": "connected",
                "capture_generation": 1,
                "stream_generation": 1,
                "receiver_buffer_delta": 25,
                "continuity_before": pre_disconnect,
                "continuity_after": marker(
                    350,
                    1_000_000_000,
                    reconnect_count=2,
                    tokens=(1, 5, 6),
                    connection_loss_events=1,
                ),
            },
            "monitoring": {
                "telemetry_consumers": 0,
                "level_events": 100,
                "spectrum_events": 100,
                "latest_meter_sequence": 100,
                "latest_spectrum_sequence": 100,
                "engine_srt_statistics": stats,
                "runtime_srt_statistics": stats,
                "queues": {
                    name: value
                    for name, value in queues.items()
                    if name != "recording"
                },
                "receiver_buffer_delta": 25,
                "continuity_before": marker(225, 8_000_000_000),
                "continuity_after": pre_disconnect,
            },
            "uninterrupted_continuity": {
                "baseline": baseline,
                "before_intentional_disconnect": pre_disconnect,
                "phase_names": [
                    "bitrate",
                    "recording",
                    "storage_threshold",
                    "monitoring",
                ],
            },
            "shutdown": {
                "requested": True,
                "engine_shutdown_complete": True,
                "service_state": "stopped",
                "recording_state": "stopped",
                "stream_state": "stopped",
                "capture_state": "stopped",
                "recording_path": "/tmp/final.flac",
                "last_finalized_file": "/tmp/final.flac",
                "validation": flac("/tmp/final.flac"),
                "recording_removals": [
                    {
                        "path": "/tmp/final.flac",
                        "forced": False,
                        "missing": False,
                    }
                ],
                "event_order": [
                    "branch-removed:recording",
                    "branch-removed:stream",
                    "capture-stopped:capture",
                    "shutdown-complete:capture",
                ],
            },
        },
    }


class Step2MediaIntegrationTests(unittest.TestCase):
    def test_assessment_passes_only_the_complete_real_engine_timeline(self) -> None:
        result = spike.assess_report(passing_report(), min_buffer_delta=25)
        self.assertEqual("pass", result["status"])
        self.assertTrue(all(result["checks"].values()))
        self.assertEqual("simulated_status_injection", result["threshold_evidence"])
        self.assertEqual([], result["reasons"])

    def test_assessment_fails_continuity_finalization_bounds_and_shutdown_gaps(self) -> None:
        report = passing_report()
        scenarios = report["scenarios"]
        scenarios["bitrate"]["transitions"][0]["stream_generation"] = 2
        scenarios["recording"]["validations"][0]["decoded_to_eos"] = False
        scenarios["storage_threshold"]["stream_state"] = "failed"
        scenarios["reconnect"]["first_retry_delay_seconds"] = 0.01
        scenarios["concurrent_branches"]["queues"]["recording"][
            "max_time_ns"
        ] = 1
        scenarios["monitoring"]["queues"]["spectrum"]["current_buffers"] = 5
        scenarios["shutdown"]["event_order"].reverse()

        result = spike.assess_report(report, min_buffer_delta=25)

        self.assertEqual("fail", result["status"])
        for name in (
            "runtime_bitrate_readback_without_restart",
            "flac_rotation_stop_and_decode",
            "concurrent_srt_stats_and_queue_contracts",
            "simulated_storage_threshold_isolated",
            "receiver_reconnect_backoff",
            "unconsumed_telemetry_and_queues_bounded",
            "ordered_shutdown",
        ):
            self.assertFalse(result["checks"][name], name)
        self.assertIn("FLAC rotation/finalization validation missing", result["reasons"])
        self.assertIn(
            "ordered active-recording shutdown evidence missing", result["reasons"]
        )

    def test_reconnect_accepts_native_srt_failure_before_watchdog(self) -> None:
        for duration in (0.01, 2.786):
            with self.subTest(duration=duration):
                report = passing_report()
                reconnect = report["scenarios"]["reconnect"]
                reconnect.update(
                    {
                        "offline_attempt_cause": "native_srt_error",
                        "offline_attempt_error_code": "srt_gstreamer_error_16",
                        "offline_attempt_error_message": "Connection timeout",
                        "offline_attempt_duration_seconds": duration,
                    }
                )

                result = spike.assess_report(report, min_buffer_delta=25)

                self.assertEqual("pass", result["status"])
                self.assertTrue(result["checks"]["receiver_reconnect_backoff"])

    def test_reconnect_requires_clean_removal_and_one_bounded_retry(self) -> None:
        mutations = (
            lambda reconnect: reconnect["offline_attempt_removal"].update(
                {"forced": True}
            ),
            lambda reconnect: reconnect.update(
                {"retry_count_at_receiver_restore_delta": 2}
            ),
            lambda reconnect: reconnect.update(
                {"recovered_instance_token": reconnect["offline_attempt_token"]}
            ),
            lambda reconnect: reconnect.update(
                {
                    "offline_attempt_cause": "native_srt_error",
                    "offline_attempt_error_code": "capture_failed",
                    "offline_attempt_duration_seconds": 0.01,
                }
            ),
            lambda reconnect: reconnect.update(
                {"offline_attempt_duration_seconds": 0.01}
            ),
            lambda reconnect: reconnect.update(
                {
                    "offline_attempt_cause": "native_srt_error",
                    "offline_attempt_error_code": "srt_gstreamer_error_16",
                    "offline_attempt_duration_seconds": (
                        spike.CONNECTION_ATTEMPT_TIMEOUT_SECONDS + 2.01
                    ),
                }
            ),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                report = passing_report()
                reconnect = report["scenarios"]["reconnect"]
                mutate(reconnect)

                result = spike.assess_report(report, min_buffer_delta=25)

                self.assertEqual("fail", result["status"])
                self.assertFalse(result["checks"]["receiver_reconnect_backoff"])

    def test_assessment_rejects_pts_anomalies_and_every_error_channel(self) -> None:
        mutations = (
            lambda report: report["scenarios"]["uninterrupted_continuity"][
                "before_intentional_disconnect"
            ]["receiver"].update({"missing_pts": 9, "non_monotonic_pts": 7}),
            lambda report: report["fatal_errors"].append(
                {"scope": "recording", "message": "fixture failure"}
            ),
            lambda report: report["engine_event_counts"].update(
                {"error:recording": 3}
            ),
            lambda report: report["receiver"]["bus_errors"].append(
                {"source": "srtsrc", "message": "fixture failure"}
            ),
        )
        expected = (
            "uninterrupted_phases_preserve_physical_stream",
            "no_unexpected_engine_or_receiver_errors",
            "no_unexpected_engine_or_receiver_errors",
            "no_unexpected_engine_or_receiver_errors",
        )
        for mutate, check in zip(mutations, expected):
            with self.subTest(check=check):
                report = passing_report()
                mutate(report)
                result = spike.assess_report(report, min_buffer_delta=25)
                self.assertEqual("fail", result["status"])
                self.assertFalse(result["checks"][check])

    def test_timeline_is_bounded_and_diagnostics_are_redacted(self) -> None:
        now = [100.0]
        timeline = spike.Timeline(100.0, limit=2, clock=lambda: now[0])
        timeline.add("one", message="passphrase=fixture-secret")
        now[0] += 1
        timeline.add("two")
        timeline.add("three")

        snapshot = timeline.snapshot()
        rendered = json.dumps(snapshot)
        self.assertEqual(2, len(snapshot["entries"]))
        self.assertEqual(1, snapshot["dropped"])
        self.assertNotIn("fixture-secret", rendered)
        self.assertIn("<redacted>", rendered)

    def test_receiver_is_explicitly_disposable_loopback_evidence(self) -> None:
        self.assertIn("valve", spike.REQUIRED_COMMON_ELEMENTS)
        description = spike.receiver_description(9113, 200)
        self.assertIn('srt://:9113?mode=listener&latency=200', description)
        self.assertIn("matroskademux ! queue ! opusparse ! opusdec", description)
        self.assertIn("identity name=receiver_counter", description)
        self.assertNotIn("alsasrc", description)

    def test_missing_gi_emits_stable_failure_json_without_a_secret(self) -> None:
        def unavailable():
            raise ImportError("fixture passphrase=do-not-print")

        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            with contextlib.redirect_stdout(stdout):
                returncode = spike.main(
                    (
                        "--minimum-duration-seconds",
                        "1",
                        "--timeout-seconds",
                        "2",
                        "--recording-directory",
                        directory,
                    ),
                    gst_loader=unavailable,
                )
        report = json.loads(stdout.getvalue())
        rendered = stdout.getvalue()
        self.assertEqual(2, returncode)
        self.assertEqual(1, report["schema_version"])
        self.assertEqual(2, report["step"])
        self.assertFalse(report["production_component"])
        self.assertEqual("fail", report["result"]["status"])
        self.assertNotIn("do-not-print", rendered)
        self.assertIn("<redacted>", report["setup_errors"][0])


if __name__ == "__main__":
    unittest.main()
