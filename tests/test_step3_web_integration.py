from __future__ import annotations

import contextlib
import io
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spikes import step3_web_integration as spike


def passing_assessment_report() -> dict[str, object]:
    fault_states = {
        kind: {
            "expected": dict(expected),
            "http_status": 200,
            "http": {
                name: value for name, value in expected.items() if name != "scope"
            },
            "sse_status": 200,
            "sse": {
                name: value for name, value in expected.items() if name != "scope"
            },
        }
        for kind, expected in spike.SIMULATED_FAULT_EXPECTATIONS.items()
    }
    return {
        "evidence_classification": {
            "media_participant": "stateful_simulator_no_audio_or_network"
        },
        "process_evidence": {
            "web_subprocess_started": True,
            "web_process_was_killed": True,
            "web_subprocess_restarted": True,
            "real_local_control_socket": True,
            "web_stderr_clean": True,
        },
        "scenarios": {
            "authentication": {
                "unauthorized_status": 401,
                "wrong_login_statuses": [401, 401, 401, 401, 401, 429],
                "login_status": 200,
                "session_status": 200,
                "session_authenticated": True,
                "csrf_rejection_status": 403,
                "csrf_rejection_code": "csrf_rejected",
                "logout_status": 200,
                "logout_response_unauthenticated": True,
                "old_session_status_after_logout": 401,
                "second_login_status": 200,
                "rotating_username_count": 6,
                "established_session_status_after_throttle": 200,
                "established_session_survived_throttle": True,
            },
            "configuration_and_capabilities": {
                "config_status": 200,
                "capability_status": 200,
                "passphrase_metadata_only": True,
                "known_simulator_secret_absent": True,
                "runtime_secret_probe_redacted": True,
                "malformed_setting_status": 400,
                "malformed_setting_code": "invalid_configuration",
                "non_cartesian_rejection_status": 400,
                "non_cartesian_rejection_code": "invalid_configuration",
                "validation_requests_used_expected_revision": True,
                "rejected_requests_left_config_unchanged": True,
            },
            "web_failure_isolation": {
                "states_running_throughout": True,
                "generations_unchanged": True,
                "stream_stat_counter_increased": True,
                "recording_counter_supported": True,
                "recording_counter_increased_if_supported": True,
                "recording_counter_sources": [
                    "bytes_written",
                    "bytes_written",
                    "bytes_written",
                ],
                "sse_numeric_spectrum_before_web_crash": True,
                "spectrum_present_before_web_crash": True,
                "crash_lease_wait_seconds": 6.0,
                "level_meter_present_after_lease_expiry": True,
                "spectrum_absent_after_lease_expiry": True,
                "old_session_endpoint_status": 200,
                "old_session_invalidated": True,
                "relogin_status": 200,
                "post_restart_status": 200,
            },
            "sse_bounds": {
                "configured_client_limit": 2,
                "slow_clients_held_open": 2,
                "slow_clients_unread_seconds": 12.0,
                "overload_status": 503,
                "disconnect_churn_attempts": 12,
                "slot_recovered_after_disconnects": True,
                "bounded_numeric_telemetry": True,
                "no_server_rendered_images": True,
                "process_resources": {
                    "baseline": {
                        "supported": True,
                        "rss_bytes": 20_000_000,
                        "fd_count": 8,
                    },
                    "during": {
                        "supported": True,
                        "rss_bytes": 21_000_000,
                        "fd_count": 10,
                    },
                    "after": {
                        "supported": True,
                        "rss_bytes": 20_500_000,
                        "fd_count": 8,
                    },
                    "deltas": {
                        "rss_during_bytes": 1_000_000,
                        "rss_after_bytes": 500_000,
                        "fd_during": 2,
                        "fd_after": 0,
                    },
                    "limits": {
                        "rss_during_bytes": spike.RSS_GROWTH_DURING_LIMIT_BYTES,
                        "rss_after_bytes": spike.RSS_GROWTH_AFTER_LIMIT_BYTES,
                        "fd_during": spike.FD_GROWTH_DURING_LIMIT,
                        "fd_after": spike.FD_GROWTH_AFTER_LIMIT,
                    },
                },
            },
            "fault_state_visibility": {
                "performed": True,
                "classification": "controlled_simulator_injection_not_hardware",
                "physical_fault_injection": "pending",
                "states": fault_states,
            },
            "media_unavailable_recovery": {
                "unavailable_status": 503,
                "unavailable_state_explicit": True,
                "recovery_status": 200,
                "recovered_available": True,
                "media_generations_unchanged": True,
                "media_states_remained_running": True,
            },
        },
        "setup_errors": [],
    }


def passing_physical_fallback_report() -> dict[str, object]:
    report = passing_assessment_report()
    live_file = "/recordings/tinypirelay-physical.flac"
    report["evidence_classification"] = {
        "label": "physical-hardware",
        "hardware_tested": True,
        "media_participant": "attached_existing_media_service",
    }
    report["scenarios"]["fault_state_visibility"] = {
        "performed": False,
        "classification": "attached_media_fault_injection_not_performed",
        "physical_fault_injection": "pending",
        "states": {},
    }
    isolation = report["scenarios"]["web_failure_isolation"]
    isolation.update(
        {
            "recording_counter_supported": True,
            "recording_counter_increased_if_supported": False,
            "recording_counter_sources": [
                "current_file_st_size",
                "current_file_st_size",
                "current_file_st_size",
            ],
            "recording_current_files": [live_file, live_file, live_file],
            "recording_current_files_absolute_regular": [True, True, True],
        }
    )
    report["scenarios"]["attached_recording_finalization"] = {
        "applicable": True,
        "mode": "attached_physical_finalized_flac_decode",
        "recording_started_by_harness": True,
        "current_file": live_file,
        "recording_state_after_cleanup": "stopped",
        "last_finalized_file": live_file,
        "finalized_file_matches_current_file": True,
        "file_regular": True,
        "file_size_bytes": 212_099,
        "header": "fLaC",
        "streaminfo_valid": True,
        "sample_rate_hz": 48_000,
        "total_samples": 393_120,
        "media_duration_seconds": 8.19,
        "sha256": "a" * 64,
        "decode": {
            "tool": "gst-launch-1.0",
            "command": [
                "gst-launch-1.0",
                "-q",
                "filesrc",
                f"location={live_file}",
                "!",
                "flacparse",
                "!",
                "flacdec",
                "!",
                "fakesink",
            ],
            "timeout_seconds": spike.RECORDING_DECODE_TIMEOUT_SECONDS,
            "attempted": True,
            "timed_out": False,
            "exit_code": 0,
            "result": "eos",
            "elapsed_seconds": 0.2,
            "stderr_tail": "",
            "decode_to_eos": True,
        },
    }
    return report


def media_status(
    capture: str = "stopped",
    stream: str = "stopped",
    connection: str = "disconnected",
    recording: str = "stopped",
    *,
    current_file: str | None = None,
    last_finalized_file: str | None = None,
) -> dict[str, object]:
    return {
        "state": {
            "capture": {"state": capture, "generation": 1},
            "stream": {"state": stream, "generation": 1},
            "connection": {
                "state": connection,
                "statistics": {"bytes-sent-total": 1},
            },
            "recording": {
                "state": recording,
                "generation": 1,
                "bytes_written": 1,
                "current_file": current_file,
                "last_finalized_file": last_finalized_file,
            },
        }
    }


class Step3WebIntegrationTests(unittest.TestCase):
    def test_production_telemetry_shape_and_delayed_spectrum_are_accepted(self) -> None:
        telemetry = {
            "meter": {
                "sequence": 1,
                "channels": [
                    {
                        "rms_dbfs": -24.0,
                        "peak_dbfs": -9.0,
                        "clipping": False,
                        "no_signal": False,
                    }
                ],
            },
            "spectrum": {"sequence": 1, "magnitudes_db": [-80.0, -40.0]},
        }
        self.assertTrue(spike._numeric_telemetry(telemetry))
        invalid = json.loads(json.dumps(telemetry))
        invalid["meter"]["channels"][0]["clipping"] = 1
        self.assertFalse(spike._numeric_telemetry(invalid))

        first = json.dumps({"telemetry": {"meter": telemetry["meter"], "spectrum": None}})
        second = json.dumps({"telemetry": telemetry})
        response = io.BytesIO(
            f"event: snapshot\ndata: {first}\n\nevent: snapshot\ndata: {second}\n\n".encode()
        )
        payload, _text = spike._read_numeric_sse(response)
        self.assertTrue(spike._numeric_telemetry(payload["telemetry"]))

    def test_recording_progress_falls_back_to_exact_current_file_size(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            recording = Path(directory).resolve() / "active.flac"
            recording.write_bytes(b"1234567")
            status = {
                "state": {
                    "capture": {"state": "running", "generation": 1},
                    "stream": {"state": "running", "generation": 1},
                    "connection": {
                        "state": "connected",
                        "statistics": {"bytes-sent-total": 10},
                    },
                    "recording": {
                        "state": "running",
                        "generation": 1,
                        "current_file": str(recording),
                    },
                }
            }

            sample = spike._state_sample(status)

            self.assertEqual(7, sample["recording_counter"])

    def test_attached_recording_evidence_hashes_parses_and_decodes_exact_file(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            recording = Path(directory).resolve() / "final.flac"
            packed = (48_000 << 44) | 48_000
            streaminfo = b"\0" * 10 + packed.to_bytes(8, "big") + b"\0" * 16
            recording.write_bytes(
                b"fLaC" + b"\x80\x00\x00\x22" + streaminfo + b"audio"
            )
            continuity = {
                "recording_current_files": [str(recording)] * 3,
                "recording_current_files_absolute_regular": [True] * 3,
            }
            stopped = media_status(
                recording="stopped", last_finalized_file=str(recording)
            )
            completed = mock.Mock(returncode=0, stderr="")

            with mock.patch.object(spike.subprocess, "run", return_value=completed) as run:
                evidence = spike._attached_recording_evidence(
                    spike.argparse.Namespace(gst_launch="gst-launch-1.0"),
                    continuity,
                    {"recording": True},
                    stopped,
                    (),
                )

            self.assertEqual("fLaC", evidence["header"])
            self.assertEqual(1.0, evidence["media_duration_seconds"])
            self.assertEqual(64, len(evidence["sha256"]))
            self.assertTrue(evidence["decode"]["decode_to_eos"])
            self.assertEqual(evidence["decode"]["command"], run.call_args.args[0])

    def test_assessment_accepts_only_complete_evidence(self) -> None:
        result = spike.assess_report(passing_assessment_report())

        self.assertEqual("pass", result["status"])
        self.assertTrue(all(result["checks"].values()))

    def test_assessment_fails_closed_when_continuity_or_redaction_is_missing(self) -> None:
        report = passing_assessment_report()
        report["scenarios"]["web_failure_isolation"] = {}
        report["scenarios"]["configuration_and_capabilities"] = {}

        result = spike.assess_report(report)

        self.assertEqual("fail", result["status"])
        self.assertFalse(result["checks"]["web_failure_does_not_restart_media"])
        self.assertFalse(
            result["checks"]["redaction_validation_and_non_cartesian_gate"]
        )

    def test_media_control_recovery_does_not_require_srt_connected_state(self) -> None:
        report = passing_assessment_report()
        report["scenarios"]["media_unavailable_recovery"][
            "media_states_remained_running"
        ] = False

        result = spike.assess_report(report)

        self.assertTrue(
            result["checks"]["media_unavailable_and_recovery_are_explicit"]
        )

    def test_assessment_rejects_missing_recording_progress_counter(self) -> None:
        report = passing_assessment_report()
        report["scenarios"]["web_failure_isolation"][
            "recording_counter_supported"
        ] = False

        result = spike.assess_report(report)

        self.assertEqual("fail", result["status"])
        self.assertFalse(result["checks"]["web_failure_does_not_restart_media"])

    def test_assessment_accepts_finalized_flac_for_attached_physical_only(self) -> None:
        result = spike.assess_report(passing_physical_fallback_report())

        self.assertEqual("pass", result["status"])
        self.assertTrue(result["checks"]["web_failure_does_not_restart_media"])

    def test_attached_recording_fallback_fails_closed(self) -> None:
        reports = []
        mismatched = passing_physical_fallback_report()
        mismatched["scenarios"]["attached_recording_finalization"][
            "last_finalized_file"
        ] = "/recordings/other.flac"
        reports.append(mismatched)
        decode_failed = passing_physical_fallback_report()
        decode_failed["scenarios"]["attached_recording_finalization"]["decode"][
            "exit_code"
        ] = 1
        reports.append(decode_failed)
        irregular = passing_physical_fallback_report()
        irregular["scenarios"]["web_failure_isolation"][
            "recording_current_files_absolute_regular"
        ][1] = False
        reports.append(irregular)
        unclassified = passing_physical_fallback_report()
        unclassified["evidence_classification"]["hardware_tested"] = False
        reports.append(unclassified)

        for report in reports:
            with self.subTest(report=report):
                result = spike.assess_report(report)
                self.assertEqual("fail", result["status"])
                self.assertFalse(
                    result["checks"]["web_failure_does_not_restart_media"]
                )

    def test_simulator_cannot_use_attached_recording_fallback(self) -> None:
        report = passing_assessment_report()
        physical = passing_physical_fallback_report()
        report["scenarios"]["web_failure_isolation"].update(
            {
                "recording_counter_supported": True,
                "recording_counter_increased_if_supported": False,
                "recording_current_files": physical["scenarios"][
                    "web_failure_isolation"
                ]["recording_current_files"],
                "recording_current_files_absolute_regular": [True, True, True],
            }
        )
        report["scenarios"]["attached_recording_finalization"] = physical[
            "scenarios"
        ]["attached_recording_finalization"]

        result = spike.assess_report(report)

        self.assertEqual("fail", result["status"])
        self.assertFalse(result["checks"]["web_failure_does_not_restart_media"])

    def test_assessment_rejects_short_or_unbounded_slow_client_evidence(self) -> None:
        short = passing_assessment_report()
        short["scenarios"]["sse_bounds"]["slow_clients_unread_seconds"] = 9.9
        unsupported = passing_assessment_report()
        unsupported["scenarios"]["sse_bounds"]["process_resources"]["during"][
            "supported"
        ] = False

        for report in (short, unsupported):
            with self.subTest(report=report):
                result = spike.assess_report(report)
                self.assertEqual("fail", result["status"])
                self.assertFalse(
                    result["checks"][
                        "slow_and_disconnected_sse_clients_are_bounded"
                    ]
                )

    def test_assessment_rejects_fault_state_projection_mismatch(self) -> None:
        report = passing_assessment_report()
        report["scenarios"]["fault_state_visibility"]["states"][
            "capture_device_lost"
        ]["sse"]["state"] = "running"

        result = spike.assess_report(report)

        self.assertEqual("fail", result["status"])
        self.assertFalse(
            result["checks"][
                "fault_states_are_visible_and_evidence_is_classified"
            ]
        )

    def test_slow_client_duration_is_bounded_by_cli(self) -> None:
        self.assertEqual(12.0, spike._validated_args([]).slow_client_seconds)
        for value in ("9.9", "inf", "nan"):
            with (
                self.subTest(value=value),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                spike._validated_args(["--slow-client-seconds", value])

    def test_assessment_requires_spectrum_before_crash_and_expiry_after(self) -> None:
        report = passing_assessment_report()
        report["scenarios"]["web_failure_isolation"][
            "spectrum_present_before_web_crash"
        ] = False

        result = spike.assess_report(report)

        self.assertEqual("fail", result["status"])
        self.assertFalse(result["checks"]["web_failure_does_not_restart_media"])

    def test_async_start_waits_for_each_authoritative_state(self) -> None:
        events: list[str] = []
        statuses = iter(
            (
                media_status(capture="starting"),
                media_status(capture="running"),
                media_status(capture="running", stream="starting"),
                media_status(
                    capture="running", stream="running", connection="connected"
                ),
                media_status(
                    capture="running",
                    stream="running",
                    connection="connected",
                    recording="starting",
                ),
                media_status(
                    capture="running",
                    stream="running",
                    connection="connected",
                    recording="running",
                ),
            )
        )

        class Target:
            def request(self, operation: str) -> object:
                self.assert_status(operation)
                value = next(statuses)
                sample = spike._state_sample(value)
                events.append(
                    "status:"
                    f"{sample['capture_state']}/"
                    f"{sample['stream_state']}/"
                    f"{sample['connection_state']}/"
                    f"{sample['recording_state']}"
                )
                return value

            @staticmethod
            def assert_status(operation: str) -> None:
                if operation != "status":
                    raise AssertionError(operation)

        class Browser:
            def request(
                self, method: str, path: str, body: object, *, csrf: bool
            ) -> tuple[int, object, list[tuple[str, str]]]:
                self.assert_control(method, path, csrf)
                action = body["action"]
                events.append(action)
                return 200, {}, []

            @staticmethod
            def assert_control(method: str, path: str, csrf: bool) -> None:
                if (method, path, csrf) != ("POST", "/api/control", True):
                    raise AssertionError((method, path, csrf))

        started, active = spike._ensure_active(
            Browser(), Target(), media_status(), True, 1.0
        )

        self.assertEqual(
            [
                "capture.start",
                "status:starting/stopped/disconnected/stopped",
                "status:running/stopped/disconnected/stopped",
                "stream.start",
                "status:running/starting/disconnected/stopped",
                "status:running/running/connected/stopped",
                "recording.start",
                "status:running/running/connected/starting",
                "status:running/running/connected/running",
            ],
            events,
        )
        self.assertEqual(
            {"capture": True, "stream": True, "recording": True}, started
        )
        self.assertEqual("running", spike._state_sample(active)["recording_state"])

    def test_cleanup_uses_direct_target_in_reverse_order(self) -> None:
        states = {
            "capture": "running",
            "stream": "running",
            "connection": "connected",
            "recording": "running",
        }
        operations: list[str] = []

        class Target:
            def request(self, operation: str) -> object:
                operations.append(operation)
                if operation == "status":
                    return media_status(
                        states["capture"],
                        states["stream"],
                        states["connection"],
                        states["recording"],
                    )
                scope, action = operation.split(".")
                if action != "stop":
                    raise AssertionError(operation)
                states[scope] = "stopped"
                if scope == "stream":
                    states["connection"] = "disconnected"
                return {}

        errors, recording_stop_status = spike._cleanup_started(
            Target(), {"capture": True, "stream": True, "recording": True}, 1.0
        )

        self.assertEqual([], errors)
        self.assertEqual(
            "stopped",
            spike._state_sample(recording_stop_status)["recording_state"],
        )
        self.assertEqual(
            [
                "recording.stop",
                "status",
                "stream.stop",
                "status",
                "capture.stop",
                "status",
            ],
            operations,
        )

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "AF_UNIX is unavailable")
    def test_real_subprocess_socket_boundary_scenario(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "step3.json"
            stdout = io.StringIO()
            stderr = io.StringIO()
            generated_password = "generated-web-credential-for-this-run"
            generated_media_secret = "generated-media-secret-for-this-run"
            with (
                mock.patch.object(
                    spike.secrets,
                    "token_urlsafe",
                    side_effect=(generated_password, generated_media_secret),
                ),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                code = spike.main(
                    [
                        "--startup-timeout",
                        "8",
                        "--continuity-seconds",
                        "0.4",
                        "--slow-client-seconds",
                        "10",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(0, code, stdout.getvalue() + stderr.getvalue())
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual("pass", report["result"]["status"])
            self.assertTrue(all(report["result"]["checks"].values()))
            self.assertEqual(
                "stateful_simulator_no_audio_or_network",
                report["evidence_classification"]["media_participant"],
            )
            rendered = json.dumps(report)
            self.assertNotIn(generated_password, rendered)
            self.assertNotIn(generated_media_secret, rendered)


if __name__ == "__main__":
    unittest.main()
