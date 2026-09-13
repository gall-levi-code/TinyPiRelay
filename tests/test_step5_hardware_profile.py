from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Mapping
from unittest import mock

from spikes import step5_hardware_profile as profile


def queue(max_time: int = 10_000_000_000, current_time: int = 10) -> dict[str, int]:
    return {
        "current_buffers": 1,
        "current_bytes": 128,
        "current_time_ns": current_time,
        "max_buffers": 0,
        "max_bytes": 0,
        "max_time_ns": max_time,
        "leaky": 0,
    }


def throttling(value: str = "0x0") -> dict[str, object]:
    return {
        "command": ["/usr/bin/vcgencmd", "get_throttled"],
        "returncode": 0,
        "throttled_hex": value,
        "error": None,
    }


def status(
    *,
    stream: str = "stopped",
    recording: str = "stopped",
    stream_generation: int = 0,
    recording_generation: int = 0,
    bitrate: int = 128_000,
    spectrum: bool = False,
) -> dict[str, object]:
    queues: dict[str, object] = {
        "level": {
            **queue(0),
            "max_buffers": 4,
            "max_time_ns": 0,
            "leaky": 2,
        }
    }
    if stream in profile.ACTIVE_STATES:
        queues["stream"] = queue()
    if recording in profile.ACTIVE_STATES:
        queues["recording"] = queue(5_000_000_000)
    if spectrum:
        queues["spectrum"] = {
            **queue(0),
            "max_buffers": 4,
            "max_time_ns": 0,
            "leaky": 2,
        }
    return {
        "state": {
            "capture": {"state": "running", "generation": 3, "last_error": None},
            "stream": {
                "state": stream,
                "generation": stream_generation,
                "representation_id": profile.OPUS_REPRESENTATION,
                "opus_bitrate_bps": bitrate,
                "pending_opus_bitrate_bps": None,
                "last_error": None,
            },
            "connection": {
                "state": "connected" if stream == "running" else "disconnected",
                "reconnect_count": 0,
                "consecutive_failures": 0,
                "next_retry_seconds": None,
                "statistics": {"bytes-sent-total": 1234},
                "last_error": None,
            },
            "recording": {
                "state": recording,
                "generation": recording_generation,
                "rotation_pending": False,
                "current_file": "/var/lib/tinypirelay/recordings/current.flac"
                if recording == "running"
                else None,
                "last_finalized_file": None,
                "warning": None,
                "last_error": None,
            },
            "monitoring": {"last_error": None},
        },
        "telemetry": {
            "meter": {"sequence": 5},
            "spectrum": {"sequence": 7, "magnitudes_db": [-20.0]}
            if spectrum
            else None,
        },
        "runtime_error": None,
        "runtime_metrics": {"queues": queues},
        "active_config_revision": "active-revision",
        "saved_config_revision": "saved-revision",
        "restart_required": False,
        "passphrase": "must-not-survive",
    }


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.epoch = datetime(2026, 8, 20, tzinfo=timezone.utc)

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += max(0.0, seconds)

    def utc_now(self) -> datetime:
        return self.epoch + timedelta(seconds=self.value)


class FakeClient:
    def __init__(self, initial: dict[str, object]) -> None:
        self.value = copy.deepcopy(initial)
        self.requests: list[tuple[str, dict[str, object]]] = []

    def request(self, operation: str, arguments: dict[str, object] | None = None) -> object:
        arguments = arguments or {}
        self.requests.append((operation, dict(arguments)))
        if operation == "status":
            return copy.deepcopy(self.value)
        if operation == "config.get":
            return {
                "revision": "saved-revision",
                "restart_required": False,
                "config": {
                    "schema_version": 1,
                    "capture": {
                        "device_id": "hw:CARD=iMM6C,DEV=0",
                        "mode": {"format": "S16LE", "rate_hz": 48000, "channels": 1},
                    },
                    "stream": {
                        "enabled": True,
                        "representation_id": profile.OPUS_REPRESENTATION,
                        "destination_host": "192.0.2.1",
                        "destination_port": 9000,
                        "latency_ms": 200,
                        "stream_id": "private-stream-id-sentinel",
                        "opus_bitrate_bps": 128000,
                        "passphrase": {"configured": False, "action": "keep"},
                    },
                    "recording": {
                        "directory": "/var/lib/tinypirelay/recordings",
                        "rotation_seconds": 3600,
                        "required_mountpoint": "/",
                    },
                    "monitoring": {
                        "spectrum_updates_per_second": 5,
                        "spectrum_bands": 64,
                    },
                },
            }
        state = self.value["state"]
        queues = self.value["runtime_metrics"]["queues"]
        if operation in ("stream.start", "recording.start"):
            scope = operation.split(".")[0]
            state[scope]["state"] = "running"
            state[scope]["generation"] += 1
            queues[scope] = queue(10_000_000_000 if scope == "stream" else 5_000_000_000)
            if scope == "stream":
                state["connection"]["state"] = "connected"
            return {"ok": True, "state": copy.deepcopy(state)}
        if operation in ("stream.stop", "recording.stop"):
            scope = operation.split(".")[0]
            state[scope]["state"] = "stopped"
            queues.pop(scope, None)
            if scope == "stream":
                state["connection"]["state"] = "disconnected"
            return {"ok": True, "state": copy.deepcopy(state)}
        if operation == "monitoring.spectrum_lease":
            self.value["telemetry"]["spectrum"] = {"sequence": 8, "magnitudes_db": [-10.0]}
            queues["spectrum"] = {**queue(0), "max_buffers": 4, "max_time_ns": 0, "leaky": 2}
            return {"active": True, "lease_seconds": 5.0}
        if operation == "monitoring.spectrum_release":
            self.value["telemetry"]["spectrum"] = None
            queues.pop("spectrum", None)
            return {"active": False}
        if operation == "stream.set_opus_bitrate":
            state["stream"]["opus_bitrate_bps"] = arguments["bitrate_bps"]
            state["stream"]["pending_opus_bitrate_bps"] = None
            return {"ok": True, "state": copy.deepcopy(state), "token": "hidden"}
        raise AssertionError(operation)


class FakeSampler:
    def identity_snapshot(self) -> dict[str, object]:
        return {
            "board_model": "Raspberry Pi Zero W Rev 1.1",
            "board_model_source": "/proc/device-tree/model",
            "boot_id": "12345678-1234-1234-1234-123456789abc",
        }

    def throttling_snapshot(
        self, elapsed_seconds: float | None = None, *, force: bool = False
    ) -> dict[str, object]:
        return throttling()

    def sample(self, elapsed: float) -> dict[str, object]:
        return {
            "processes": {
                "media": {
                    "available": True,
                    "pid": 10,
                    "start_ticks": 100,
                    "cpu_percent": elapsed + 1,
                    "rss_bytes": 1000 + int(elapsed),
                    "fd_count": 8,
                    "threads": 3,
                    "vm_hwm_bytes": 1200,
                    "io": {"read_bytes": 10, "write_bytes": 20 + int(elapsed)},
                },
                "web": {
                    "available": True,
                    "pid": 11,
                    "start_ticks": 101,
                    "cpu_percent": 2.0,
                    "rss_bytes": 2000,
                    "fd_count": 9,
                    "threads": 2,
                    "vm_hwm_bytes": 2200,
                    "io": {"read_bytes": 30, "write_bytes": 40},
                },
            },
            "cpu": {
                "busy_percent": 10.0,
                "total_ticks": 1000 + int(elapsed),
                "idle_ticks": 700,
                "logical_cpus": 1,
                "load_1m": 0.1,
                "load_5m": 0.2,
                "load_15m": 0.3,
                "running_tasks": 1,
                "total_tasks": 20,
            },
            "memory": {
                "mem_total_bytes": 512_000_000,
                "mem_available_bytes": 300_000_000,
                "swap_total_bytes": 100_000_000,
                "swap_free_bytes": 90_000_000,
            },
            "network": {
                "wlan0": {
                    "rx_bytes": 1000 + int(elapsed),
                    "rx_dropped": 0,
                    "tx_bytes": 2000 + int(elapsed),
                    "tx_dropped": 0,
                }
            },
            "recording_storage": {
                "filesystem_total_bytes": 1_000_000,
                "filesystem_used_bytes": 100_000 + int(elapsed),
                "filesystem_free_bytes": 900_000 - int(elapsed),
                "directory_file_count": 1,
                "directory_bytes": 1000 + int(elapsed),
            },
            "temperature_c": 40.0,
            "throttling": throttling(),
        }


class ParsingAndMathTests(unittest.TestCase):
    def test_proc_stat_parser_handles_spaces_and_parentheses(self) -> None:
        fields = ["S"] + ["0"] * 21
        fields[11] = "11"
        fields[12] = "7"
        fields[19] = "1234"
        fields[21] = "20"
        parsed = profile.parse_proc_stat(f"42 (media worker (one)) {' '.join(fields)}")
        self.assertEqual(42, parsed.pid)
        self.assertEqual(18, parsed.cpu_ticks)
        self.assertEqual(1234, parsed.start_ticks)
        self.assertEqual(20, parsed.rss_pages)
        with self.assertRaises(ValueError):
            profile.parse_proc_stat("bad")

    def test_cpu_temperature_and_throttling_parsers_fail_closed(self) -> None:
        self.assertEqual(50.0, profile.process_cpu_percent(10, 60, 1.0, 100))
        self.assertIsNone(profile.process_cpu_percent(60, 10, 1.0, 100))
        self.assertEqual(41.25, profile.parse_temperature("41250\n"))
        self.assertEqual("0x50000", profile.parse_throttled("throttled=0x50000"))
        with self.assertRaises(ValueError):
            profile.parse_temperature("nan")
        with self.assertRaises(ValueError):
            profile.parse_throttled("unknown")

    def test_percentile_slope_and_summary_are_deterministic(self) -> None:
        self.assertEqual(2.5, profile.percentile([1, 2, 3, 4], 50))
        self.assertAlmostEqual(2.0, profile.linear_slope([(0, 1), (1, 3), (2, 5)]))
        summary = profile.summarize_points([(0, 1), (1, 3), (2, 5)])
        self.assertEqual(3, summary["count"])
        self.assertEqual(5.0, summary["maximum"])
        self.assertAlmostEqual(2.0, summary["slope_per_second"])

    def test_core_proc_parsers_cover_cpu_memory_process_io_and_network(self) -> None:
        cpu = profile.parse_proc_cpu("cpu  10 2 3 20 5 1 1 0 0 0\n")
        self.assertEqual((42, 25), cpu)
        self.assertAlmostEqual(50.0, profile.system_cpu_percent((20, 10), (40, 20)))
        self.assertEqual(0.25, profile.parse_loadavg("0.25 0.20 0.10 1/20 5")["load_1m"])
        memory = profile.parse_meminfo(
            "MemTotal: 100 kB\nMemAvailable: 75 kB\nSwapTotal: 10 kB\nSwapFree: 9 kB\n"
        )
        self.assertEqual(75 * 1024, memory["mem_available_bytes"])
        process_status = profile.parse_proc_status("Threads: 3\nVmHWM: 42 kB\n")
        self.assertEqual({"threads": 3, "vm_hwm_bytes": 42 * 1024}, process_status)
        self.assertEqual(
            {"read_bytes": 11, "write_bytes": 12},
            profile.parse_proc_io("read_bytes: 11\nwrite_bytes: 12\n"),
        )
        network = profile.parse_net_dev(
            "Inter-| Receive | Transmit\n face |bytes packets errs drop fifo frame compressed multicast|bytes packets errs drop fifo colls carrier compressed\n"
            " wlan0: 100 1 0 2 0 0 0 0 200 2 0 3 0 0 0 0\n"
        )
        self.assertEqual(
            {"rx_bytes": 100, "rx_dropped": 2, "tx_bytes": 200, "tx_dropped": 3},
            network["wlan0"],
        )

    def test_vcgencmd_observation_is_exact_bounded_and_cached(self) -> None:
        calls: list[tuple[list[str], dict[str, object]]] = []

        def runner(command: list[str], **kwargs: object) -> object:
            calls.append((command, kwargs))
            return SimpleNamespace(returncode=0, stdout="throttled=0x0\n")

        sampler = profile.SystemSampler(
            {"media": 10},
            clock_ticks_per_second=100,
            page_size=4096,
            command_runner=runner,
        )
        first = sampler.throttling_snapshot(0.0)
        cached = sampler.throttling_snapshot(9.9)
        refreshed = sampler.throttling_snapshot(10.0)
        self.assertEqual("0x0", first["throttled_hex"])
        self.assertEqual(first, cached)
        self.assertEqual("0x0", refreshed["throttled_hex"])
        self.assertEqual(2, len(calls))
        self.assertEqual(["/usr/bin/vcgencmd", "get_throttled"], calls[0][0])
        self.assertIs(False, calls[0][1]["shell"])
        self.assertEqual(2.0, calls[0][1]["timeout"])

    def test_board_model_and_boot_id_are_bounded_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            (proc / "device-tree").mkdir()
            (proc / "sys" / "kernel" / "random").mkdir(parents=True)
            (proc / "device-tree" / "model").write_text(
                "Raspberry Pi Zero W Rev 1.1\x00", encoding="utf-8"
            )
            (proc / "sys" / "kernel" / "random" / "boot_id").write_text(
                "12345678-1234-1234-1234-123456789abc\n", encoding="utf-8"
            )
            sampler = profile.SystemSampler(
                {"media": 10},
                proc_root=proc,
                clock_ticks_per_second=100,
                page_size=4096,
            )
            identity = sampler.identity_snapshot()
        self.assertEqual("Raspberry Pi Zero W Rev 1.1", identity["board_model"])
        self.assertEqual(
            "12345678-1234-1234-1234-123456789abc", identity["boot_id"]
        )


class BoundAndQueueTests(unittest.TestCase):
    def test_plan_bounds_samples_and_bitrate_schedule(self) -> None:
        with self.assertRaises(ValueError):
            profile.ProfilePlan(10_000, 0.2, 10)
        with self.assertRaises(ValueError):
            profile.ProfilePlan(10, 1, 10, opus_bitrates=(64_000,))
        with self.assertRaises(ValueError):
            profile.ProfilePlan(
                1,
                0.5,
                10,
                start_stream=True,
                expected_representation=profile.OPUS_REPRESENTATION,
                opus_bitrates=(64_000,),
                bitrate_interval_seconds=1,
            )
        with self.assertRaises(ValueError):
            profile.ProfilePlan(10, 1, 10, checkpoint_interval_seconds=60)
        with self.assertRaises(ValueError):
            profile.InstalledProfiler(
                FakeClient(status()),
                FakeSampler(),
                profile.ProfilePlan(1, 0.5, 2),
                "not-a-sha",
                "unit-test",
            )
        with self.assertRaises(ValueError):
            profile.InstalledProfiler(
                FakeClient(status()),
                FakeSampler(),
                profile.ProfilePlan(1, 0.5, 2),
                "a" * 64,
                "Bad Scenario",
            )

    def test_active_queue_requires_exact_entry_and_positive_bound(self) -> None:
        valid = status(stream="running", stream_generation=1)
        profile.require_active_queue_visibility(valid)
        missing = copy.deepcopy(valid)
        del missing["runtime_metrics"]["queues"]["stream"]
        with self.assertRaises(profile.ProfileError):
            profile.require_active_queue_visibility(missing)
        unbounded = copy.deepcopy(valid)
        q = unbounded["runtime_metrics"]["queues"]["stream"]
        q["max_buffers"] = q["max_bytes"] = q["max_time_ns"] = 0
        with self.assertRaises(profile.ProfileError):
            profile.require_active_queue_visibility(unbounded)

    def test_queue_bounds_reject_overflow_negative_and_token_is_not_retained(self) -> None:
        value = status(stream="running", stream_generation=1)
        value["runtime_metrics"]["queues"]["stream"]["instance_token"] = 99
        normalized = profile.normalize_status(value)
        self.assertNotIn("instance_token", normalized["queues"]["stream"])
        overflow = dict(normalized["queues"]["stream"])
        overflow["current_time_ns"] = overflow["max_time_ns"] + 1
        self.assertTrue(profile.queue_invariant_errors("stream", overflow))
        overflow["current_time_ns"] = -1
        self.assertTrue(profile.queue_invariant_errors("stream", overflow))

    def test_level_and_requested_spectrum_queue_are_required(self) -> None:
        value = status()
        value["runtime_metrics"]["queues"].pop("level")
        with self.assertRaises(profile.ProfileError):
            profile.require_active_queue_visibility(value)
        value = status()
        with self.assertRaises(profile.ProfileError):
            profile.require_active_queue_visibility(value, require_spectrum=True)


class LifecycleAndAssessmentTests(unittest.TestCase):
    def _run(
        self, initial: dict[str, object], plan: profile.ProfilePlan
    ) -> tuple[dict[str, object], FakeClient]:
        clock = FakeClock()
        client = FakeClient(initial)
        report = profile.InstalledProfiler(
            client,
            FakeSampler(),
            plan,
            "a" * 64,
            "unit-test",
            clock=clock.monotonic,
            sleep=clock.sleep,
            utc_now=clock.utc_now,
        ).run()
        return report, client

    def test_starts_and_cleans_only_missing_scopes_with_spectrum_and_bitrates(self) -> None:
        report, client = self._run(
            status(),
            profile.ProfilePlan(
                2,
                0.5,
                2,
                start_stream=True,
                expected_representation=profile.OPUS_REPRESENTATION,
                start_recording=True,
                spectrum=True,
                opus_bitrates=(64_000, 128_000),
                bitrate_interval_seconds=0.5,
            ),
        )
        operations = [operation for operation, _arguments in client.requests]
        self.assertIn("stream.start", operations)
        self.assertIn("recording.start", operations)
        self.assertNotIn("monitoring.spectrum_release", operations)
        self.assertIn(
            "stopped_spectrum_renewal_without_releasing_shared_lease",
            report["cleanup"]["actions"],
        )
        self.assertIn("recording.stop", operations)
        self.assertIn("stream.stop", operations)
        self.assertEqual("pass", report["result"]["execution_status"])
        self.assertEqual("not-assessed", report["result"]["profile_status"])
        rendered = str(report)
        self.assertNotIn("must-not-survive", rendered)
        self.assertNotIn("hidden", rendered)
        self.assertNotIn("private-stream-id-sentinel", rendered)
        self.assertIs(
            True,
            report["config"]["config"]["stream"]["stream_id_configured"],
        )
        self.assertGreater(
            report["summary"]["processes"]["media"]["read_bytes"]["count"], 0
        )

    def test_preexisting_scopes_are_not_stopped_and_bitrate_is_restored(self) -> None:
        report, client = self._run(
            status(stream="running", recording="running", stream_generation=4, recording_generation=2),
            profile.ProfilePlan(
                1,
                0.5,
                2,
                start_stream=True,
                expected_representation=profile.OPUS_REPRESENTATION,
                start_recording=True,
                opus_bitrates=(64_000,),
                bitrate_interval_seconds=0.5,
            ),
        )
        operations = [operation for operation, _arguments in client.requests]
        self.assertNotIn("stream.stop", operations)
        self.assertNotIn("recording.stop", operations)
        bitrate_requests = [arguments["bitrate_bps"] for operation, arguments in client.requests if operation == "stream.set_opus_bitrate"]
        self.assertEqual([64_000, 128_000], bitrate_requests)
        self.assertEqual("pass", report["result"]["execution_status"])
        no_dwell = copy.deepcopy(report)
        for sample in no_dwell["samples"]:
            if sample["elapsed_seconds"] >= 1:
                sample["status"]["state"]["stream"]["opus_bitrate_bps"] = 128_000
        self.assertFalse(
            profile.assess_report(no_dwell)["checks"]["final_bitrate_dwell_observed"]
        )

    def test_generation_change_causes_cleanup_refusal_and_failed_execution(self) -> None:
        clock = FakeClock()
        client = FakeClient(status())

        class ChangingSampler(FakeSampler):
            def sample(self, elapsed: float) -> dict[str, object]:
                if elapsed >= 1:
                    client.value["state"]["stream"]["generation"] += 1
                return super().sample(elapsed)

        report = profile.InstalledProfiler(
            client,
            ChangingSampler(),
            profile.ProfilePlan(
                1,
                0.5,
                2,
                start_stream=True,
                expected_representation=profile.OPUS_REPRESENTATION,
            ),
            "a" * 64,
            "unit-test",
            clock=clock.monotonic,
            sleep=clock.sleep,
            utc_now=clock.utc_now,
        ).run()
        self.assertEqual("fail", report["result"]["execution_status"])
        self.assertTrue(report["cleanup"]["errors"])
        self.assertNotIn("stream.stop", [name for name, _args in client.requests])

    def test_assessment_fails_missing_queue_overflow_and_capture_or_process_change(self) -> None:
        report, _client = self._run(
            status(stream="running", stream_generation=1),
            profile.ProfilePlan(1, 0.5, 2),
        )
        self.assertEqual("pass", report["result"]["execution_status"])
        variants = []
        missing = copy.deepcopy(report)
        missing["samples"][0]["status"]["queues"] = None
        variants.append(missing)
        overflow = copy.deepcopy(report)
        overflow["samples"][0]["status"]["queues"]["stream"]["current_time_ns"] = 20_000_000_000
        variants.append(overflow)
        capture = copy.deepcopy(report)
        capture["samples"][-1]["status"]["state"]["capture"]["generation"] = 99
        variants.append(capture)
        process = copy.deepcopy(report)
        process["samples"][-1]["system"]["processes"]["media"]["start_ticks"] = 999
        variants.append(process)
        temperature = copy.deepcopy(report)
        temperature["samples"][0]["system"]["temperature_c"] = None
        variants.append(temperature)
        throttled = copy.deepcopy(report)
        throttled["samples"][0]["system"]["throttling"]["throttled_hex"] = "0x50000"
        variants.append(throttled)
        missing_throttle = copy.deepcopy(report)
        missing_throttle["boundary_metrics"]["throttling_end"] = None
        variants.append(missing_throttle)
        substituted_throttle = copy.deepcopy(report)
        substituted_throttle["boundary_metrics"]["throttling_start"]["command"][0] = (
            "/tmp/vcgencmd"
        )
        variants.append(substituted_throttle)
        final_capture = copy.deepcopy(report)
        final_capture["final_status"]["state"]["capture"]["generation"] = 99
        variants.append(final_capture)
        final_runtime_error = copy.deepcopy(report)
        final_runtime_error["final_status"]["runtime_error"] = "late failure"
        variants.append(final_runtime_error)
        source_binding = copy.deepcopy(report)
        source_binding["identity"]["source_bundle_sha256"] = "B" * 64
        variants.append(source_binding)
        scenario_binding = copy.deepcopy(report)
        scenario_binding["run"]["scenario"] = "Bad Scenario"
        variants.append(scenario_binding)
        for variant in variants:
            with self.subTest(reasons=profile.assess_report(variant)["reasons"]):
                self.assertEqual("fail", profile.assess_report(variant)["execution_status"])

    def test_physical_stream_run_fails_closed_before_sampling_without_queue_visibility(self) -> None:
        initial = status(stream="running", stream_generation=1)
        del initial["runtime_metrics"]
        report, _client = self._run(initial, profile.ProfilePlan(1, 0.5, 2))
        self.assertEqual([], report["samples"])
        self.assertEqual("fail", report["result"]["execution_status"])
        self.assertIn("queue visibility", str(report["errors"]))

    def test_start_timeout_still_stops_provisionally_owned_generation(self) -> None:
        class TimeoutClient(FakeClient):
            def request(self, operation: str, arguments: dict[str, object] | None = None) -> object:
                if operation == "stream.start":
                    self.requests.append((operation, dict(arguments or {})))
                    stream = self.value["state"]["stream"]
                    stream["state"] = "starting"
                    stream["generation"] += 1
                    self.value["runtime_metrics"]["queues"]["stream"] = queue()
                    return {"ok": True, "state": copy.deepcopy(self.value["state"])}
                return super().request(operation, arguments)

        clock = FakeClock()
        client = TimeoutClient(status())
        report = profile.InstalledProfiler(
            client,
            FakeSampler(),
            profile.ProfilePlan(
                1,
                0.5,
                1,
                start_stream=True,
                expected_representation=profile.OPUS_REPRESENTATION,
            ),
            "a" * 64,
            "start-timeout",
            clock=clock.monotonic,
            sleep=clock.sleep,
            utc_now=clock.utc_now,
        ).run()
        operations = [name for name, _args in client.requests]
        self.assertIn("stream.start", operations)
        self.assertIn("stream.stop", operations)
        self.assertEqual("fail", report["result"]["execution_status"])

    def test_readiness_waits_for_delayed_queue_poll(self) -> None:
        class DelayedQueueClient(FakeClient):
            hidden_statuses = 3

            def request(self, operation: str, arguments: dict[str, object] | None = None) -> object:
                value = super().request(operation, arguments)
                if (
                    operation == "status"
                    and self.value["state"]["stream"]["state"] == "running"
                    and self.hidden_statuses > 0
                ):
                    self.hidden_statuses -= 1
                    value["runtime_metrics"]["queues"].pop("stream", None)
                return value

        clock = FakeClock()
        client = DelayedQueueClient(status())
        report = profile.InstalledProfiler(
            client,
            FakeSampler(),
            profile.ProfilePlan(
                1,
                0.5,
                2,
                start_stream=True,
                expected_representation=profile.OPUS_REPRESENTATION,
            ),
            "a" * 64,
            "delayed-queue",
            clock=clock.monotonic,
            sleep=clock.sleep,
            utc_now=clock.utc_now,
        ).run()
        self.assertEqual("pass", report["result"]["execution_status"])

    def test_missed_sample_deadline_fails_instead_of_catching_up(self) -> None:
        clock = FakeClock()

        class SlowSampler(FakeSampler):
            calls = 0

            def sample(self, elapsed: float) -> dict[str, object]:
                self.calls += 1
                if self.calls == 2:
                    clock.sleep(1.1)
                return super().sample(elapsed)

        report = profile.InstalledProfiler(
            FakeClient(status()),
            SlowSampler(),
            profile.ProfilePlan(2, 0.5, 2),
            "a" * 64,
            "slow-sampler",
            clock=clock.monotonic,
            sleep=clock.sleep,
            utc_now=clock.utc_now,
        ).run()
        self.assertGreater(report["run"]["missed_sample_slots"], 0)
        self.assertFalse(report["result"]["checks"]["sample_schedule_kept"])
        self.assertEqual("fail", report["result"]["execution_status"])

    def test_controlled_interrupt_returns_partial_report_and_cleans_up(self) -> None:
        class InterruptSampler(FakeSampler):
            calls = 0

            def sample(self, elapsed: float) -> dict[str, object]:
                self.calls += 1
                if self.calls == 2:
                    raise KeyboardInterrupt
                return super().sample(elapsed)

        clock = FakeClock()
        client = FakeClient(status())
        report = profile.InstalledProfiler(
            client,
            InterruptSampler(),
            profile.ProfilePlan(
                2,
                0.5,
                2,
                start_recording=True,
            ),
            "a" * 64,
            "controlled-interrupt",
            clock=clock.monotonic,
            sleep=clock.sleep,
            utc_now=clock.utc_now,
        ).run()
        self.assertTrue(report["interrupted"])
        self.assertEqual(1, len(report["samples"]))
        self.assertIn("recording.stop", [name for name, _args in client.requests])
        self.assertEqual("fail", report["result"]["execution_status"])

    def test_periodic_checkpoint_and_final_serialization_agree(self) -> None:
        clock = FakeClock()
        checkpoint_values: list[Mapping[str, object]] = []
        serialization_redact_calls: list[int] = []
        real_redact = profile.redact_secrets
        with tempfile.TemporaryDirectory() as directory:
            checkpoint_path = Path(directory) / "checkpoint.json"
            final_path = Path(directory) / "final.json"

            def checkpoint(value: Mapping[str, object]) -> None:
                checkpoint_values.append(value)
                with mock.patch.object(
                    profile, "redact_secrets", wraps=real_redact
                ) as redact:
                    profile._write_report(checkpoint_path, value)
                serialization_redact_calls.append(redact.call_count)

            report = profile.InstalledProfiler(
                FakeClient(status()),
                FakeSampler(),
                profile.ProfilePlan(
                    301,
                    60,
                    2,
                    checkpoint_interval_seconds=300,
                ),
                "a" * 64,
                "checkpoint",
                clock=clock.monotonic,
                sleep=clock.sleep,
                utc_now=clock.utc_now,
            ).run(checkpoint)
            with mock.patch.object(
                profile, "redact_secrets", wraps=real_redact
            ) as redact:
                profile._write_report(final_path, report)
            serialization_redact_calls.append(redact.call_count)
            checkpoint_report = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            final_report = json.loads(final_path.read_text(encoding="utf-8"))

        self.assertEqual(1, len(checkpoint_values))
        self.assertTrue(checkpoint_values[0]["checkpoint"]["complete"])
        self.assertFalse(checkpoint_report["checkpoint"]["complete"])
        self.assertTrue(final_report["checkpoint"]["complete"])
        self.assertEqual([1, 1], serialization_redact_calls)
        self.assertEqual(checkpoint_report["identity"], final_report["identity"])
        self.assertEqual(checkpoint_report["run"], final_report["run"])
        self.assertEqual(
            checkpoint_report["samples"],
            final_report["samples"][: len(checkpoint_report["samples"])],
        )

    def test_representation_mismatch_fails_before_start(self) -> None:
        report, client = self._run(
            status(),
            profile.ProfilePlan(
                1,
                0.5,
                2,
                start_stream=True,
                expected_representation="flac_48000_stereo_matroska",
            ),
        )
        self.assertEqual("fail", report["result"]["execution_status"])
        self.assertNotIn("stream.start", [name for name, _args in client.requests])

    def test_more_than_256_samples_retain_final_sample_and_serialized_result(self) -> None:
        report, _client = self._run(
            status(), profile.ProfilePlan(300, 1, 2)
        )
        self.assertEqual(301, len(report["samples"]))
        self.assertEqual(300, report["samples"][-1]["scheduled_elapsed_seconds"])
        self.assertEqual("pass", report["result"]["execution_status"])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            profile._write_report(output, report)
            serialized = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(301, len(serialized["samples"]))
        self.assertEqual(300, serialized["samples"][-1]["scheduled_elapsed_seconds"])
        self.assertEqual(profile.assess_report(serialized), serialized["result"])


class OutputTests(unittest.TestCase):
    def test_large_report_is_redacted_once_bounded_and_not_mutated(self) -> None:
        report = {
            "message": "srt://host?passphrase=uri-secret",
            "metadata": {"credentials": {"password": "nested-secret"}},
            "samples": [
                {"index": index, "nested": {"metric": index}}
                for index in range(601)
            ],
        }
        original = copy.deepcopy(report)
        real_redact = profile.redact_secrets
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            with mock.patch.object(
                profile, "redact_secrets", wraps=real_redact
            ) as redact:
                profile._write_report(path, report)
            value = path.read_text(encoding="utf-8")
            serialized = json.loads(value)
        self.assertEqual(1, redact.call_count)
        self.assertEqual(original, report)
        self.assertEqual(601, len(serialized["samples"]))
        self.assertEqual(600, serialized["samples"][-1]["index"])
        self.assertEqual(
            "[REDACTED]", serialized["metadata"]["credentials"]["password"]
        )
        self.assertIn("[REDACTED]", value)
        self.assertNotIn("uri-secret", value)
        self.assertNotIn("nested-secret", value)

    def test_main_uses_fixed_vcgencmd_sampler_contract(self) -> None:
        captured: dict[str, object] = {}

        class MainSampler:
            def __init__(self, processes: object, **kwargs: object) -> None:
                captured["processes"] = processes
                captured["sampler_kwargs"] = kwargs

        class MainProfiler:
            def __init__(self, *args: object, **kwargs: object) -> None:
                captured["profiler_args"] = args

            def run(self, checkpoint: object) -> dict[str, object]:
                return {
                    "result": {"execution_status": "pass"},
                    "samples": [],
                }

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            with (
                mock.patch.object(profile, "ControlClient", return_value=object()),
                mock.patch.object(profile, "SystemSampler", MainSampler),
                mock.patch.object(profile, "InstalledProfiler", MainProfiler),
                mock.patch.object(profile.sys, "stdout", io.StringIO()),
            ):
                exit_code = profile.main(
                    [
                        "--control-socket",
                        "/run/tinypirelay/control.sock",
                        "--media-pid",
                        "10",
                        "--web-pid",
                        "11",
                        "--source-bundle-sha256",
                        "a" * 64,
                        "--scenario",
                        "main-smoke",
                        "--output",
                        str(output),
                    ]
                )
            self.assertTrue(output.is_file())
        self.assertEqual(0, exit_code)
        self.assertEqual({"media": 10, "web": 11}, captured["processes"])
        self.assertEqual(
            {"temperature_path": Path("/sys/class/thermal/thermal_zone0/temp")},
            captured["sampler_kwargs"],
        )


if __name__ == "__main__":
    unittest.main()
