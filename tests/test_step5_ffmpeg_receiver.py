from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path
import signal
import subprocess
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from spikes import step5_ffmpeg_receiver as receiver


def arguments(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "output": Path("receiver.json"),
        "port": 9153,
        "bind_address": "0.0.0.0",
        "latency_ms": 200,
        "duration_seconds": 10.0,
        "timeout_seconds": 20.0,
        "expected_codec": "opus",
        "expected_rate_hz": 48_000,
        "expected_channels": 2,
        "ffmpeg_path": Path("ffmpeg"),
        "rtsp_publish_url": None,
        "scenario": "opus-baseline",
        "source_bundle_sha256": "a" * 64,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def log_lines(
    *,
    codec: str = "opus",
    rate: int = 48_000,
    layout: str = "stereo",
    frames: int = 10,
) -> str:
    lines = [
        "Input #0, matroska,webm, from 'srt://192.168.1.94:9153?mode=listener':",
        f"  Stream #0:0: Audio: {codec}, {rate} Hz, {layout}, fltp",
    ]
    channels = 1 if layout == "mono" else 2
    for index in range(frames):
        lines.append(
            "[Parsed_ashowinfo_1 @ 0000] "
            f"n:{index} pts:{index * rate} pts_time:{index}.000000 "
            f"fmt:fltp channels:{channels} chlayout:{layout} "
            f"rate:{rate} nb_samples:{rate} checksum:00000000"
        )
    return "\n".join(lines) + "\n"


def passing_report() -> dict[str, object]:
    return {
        "scenario": "opus-baseline",
        "source_bundle_sha256": "a" * 64,
        "configuration": {
            "duration_seconds": 10.0,
            "timeout_seconds": 20.0,
            "expected_codec": "opus",
            "expected_rate_hz": 48_000,
            "expected_channels": 2,
            "audio_retained": False,
            "srt_passphrase_supported": False,
            "srt_stream_id_supported": False,
        },
        "ffmpeg": {
            "exit_code": 0,
            "version_line": "ffmpeg version 8.0.1",
            "libsrt_enabled": True,
        },
        "setup_errors": [],
        "observation": {
            "observed_audio": {
                "input_codec": "opus",
                "input_sample_rate_hz": 48_000,
                "input_channels": 2,
                "decoded_sample_rates_hz": [48_000],
                "decoded_channel_counts": [2],
            },
            "decoded_progress": {
                "frame_count": 10,
                "decoded_seconds": 10.0,
                "first_pts_seconds": 0.0,
                "last_pts_seconds": 9.0,
                "missing_pts": 0,
                "non_monotonic_pts": 0,
                "discontinuities": 0,
                "startup_pts_adjustments": 0,
                "max_startup_pts_adjustment_seconds": 0.0,
            },
            "errors": [],
            "first_decoded_progress_wall_seconds": 0.75,
            "last_decoded_progress_wall_seconds": 10.0,
            "decoded_progress_tail_stall_seconds": 0.75,
            "max_decoded_progress_wall_gap_seconds": 1.0,
            "termination_reason": "process_exit",
            "duration_reached": True,
            "timed_out": False,
            "exit_code": 0,
            "cleanup": {
                "signal": None,
                "completed": True,
                "stderr_reader_completed": True,
                "stderr_forced_close_used": False,
                "stderr_reader_error": None,
                "terminate_used": False,
                "kill_used": False,
            },
        },
    }


class FakeProcess:
    def __init__(self, stderr: str = "") -> None:
        self.stderr = io.StringIO(stderr)
        self.returncode: int | None = None
        self.signals: list[object] = []
        self.terminate_called = False
        self.kill_called = False

    def poll(self) -> int | None:
        return self.returncode

    def send_signal(self, value: object) -> None:
        self.signals.append(value)
        self.returncode = 255

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired("ffmpeg", timeout)
        return self.returncode

    def terminate(self) -> None:
        self.terminate_called = True
        self.returncode = -15

    def kill(self) -> None:
        self.kill_called = True
        self.returncode = -9


class IncrementingClock:
    def __init__(self, step: float = 1.0) -> None:
        self.value = -step
        self.step = step

    def __call__(self) -> float:
        self.value += self.step
        return self.value


class TerminateRequiredProcess(FakeProcess):
    def send_signal(self, value: object) -> None:
        self.signals.append(value)


class CleanExitProcess(FakeProcess):
    def __init__(self, stderr: str, exit_code: int = 0) -> None:
        super().__init__(stderr)
        self.exit_code = exit_code

    def poll(self) -> int | None:
        if (
            self.returncode is None
            and self.stderr.tell() == len(self.stderr.getvalue())
        ):
            self.returncode = self.exit_code
        return self.returncode


class BlockingAfterLines:
    def __init__(self, value: str) -> None:
        self.lines = iter(value.splitlines(keepends=True))
        self.exhausted = threading.Event()
        self.closed = threading.Event()

    def __iter__(self) -> BlockingAfterLines:
        return self

    def __next__(self) -> str:
        try:
            return next(self.lines)
        except StopIteration:
            self.exhausted.set()
            self.closed.wait()
            raise OSError("stderr was force-closed")

    def close(self) -> None:
        self.closed.set()


class ReaderHangProcess(FakeProcess):
    def __init__(self, value: str) -> None:
        super().__init__()
        self.stderr = BlockingAfterLines(value)

    def poll(self) -> int | None:
        if self.returncode is None and self.stderr.exhausted.is_set():
            self.returncode = 0
        return self.returncode


def identity_runner(*_args: object, **_kwargs: object) -> SimpleNamespace:
    return SimpleNamespace(
        returncode=0,
        stdout=(
            "ffmpeg version 8.0.1 Copyright FFmpeg developers\n"
            "configuration: --enable-gpl --enable-libsrt " + "x" * 600 + "\n"
        ),
        stderr="",
    )


class Step5FFmpegReceiverTests(unittest.TestCase):
    def test_argv_is_one_audio_stream_auto_demuxed_to_null(self) -> None:
        argv = receiver.ffmpeg_argv(arguments())

        self.assertEqual("ffmpeg", argv[0])
        self.assertIn("srt://0.0.0.0:9153?mode=listener&latency=200000", argv)
        self.assertEqual("0:a:0", argv[argv.index("-map") + 1])
        self.assertEqual(
            "asetnsamples=n=48000:p=0,ashowinfo", argv[argv.index("-af") + 1]
        )
        self.assertEqual("null", argv[argv.index("-f") + 1])
        self.assertEqual("10.0", argv[argv.index("-t") + 1])
        self.assertGreater(argv.index("-t"), argv.index("-c:a"))
        self.assertLess(argv.index("-t"), argv.index("-f"))
        self.assertEqual("-", argv[-1])
        self.assertNotIn("passphrase", " ".join(argv).casefold())
        self.assertNotIn("streamid", " ".join(argv).casefold())

    def test_rtsp_bridge_preserves_evidence_and_bounds_both_outputs(self) -> None:
        destination = "rtsp://127.0.0.1:8554/tinypirelay-test"
        args = arguments(
            expected_codec="flac", duration_seconds=2.0, timeout_seconds=10.0,
            rtsp_publish_url=destination, bind_address="192.168.1.174",
        )
        receiver.validate_args(args)
        argv = receiver.ffmpeg_argv(args)
        original = receiver.ffmpeg_argv(arguments(
            expected_codec="flac", duration_seconds=2.0, timeout_seconds=10.0,
            bind_address="192.168.1.174",
        ))
        self.assertEqual(original, argv[:len(original)])
        self.assertEqual(destination, argv[-1])
        self.assertIn("srt://192.168.1.174:9153?mode=listener&latency=200000", argv)
        self.assertEqual(1, argv.count("-i"))
        self.assertEqual(1, argv.count("-af"))
        self.assertEqual(["2.0", "2.0"], [argv[i + 1] for i, item in enumerate(argv) if item == "-t"])
        self.assertEqual(["pcm_s16le", "pcm_s16be"], [argv[i + 1] for i, item in enumerate(argv) if item == "-c:a"])
        self.assertNotIn("-ar", argv)
        self.assertNotIn("-ac", argv)
        report = receiver.run_receiver(
            args,
            popen_factory=lambda *_args, **_kwargs: CleanExitProcess(log_lines(codec="flac", frames=3)),
            run_factory=identity_runner,
            clock=IncrementingClock(step=1.0),
            sleeper=lambda _seconds: time.sleep(0.001),
        )
        self.assertEqual("pass", report["result"]["status"], report["result"])
        self.assertEqual("disposable-workstation-receiver-rtsp-bridge", report["evidence_classification"])
        self.assertEqual(destination, report["configuration"]["rtsp_publish"]["url"])
        self.assertEqual("pcm_s16be", report["configuration"]["rtsp_publish"]["codec"])
        self.assertEqual("192.168.1.174", report["configuration"]["bind_address"])

    def test_parser_extracts_caps_and_one_second_monotonic_progress(self) -> None:
        parser = receiver.FFmpegLogParser()
        for line in log_lines(frames=3).splitlines():
            parser.feed_line(line)

        snapshot = parser.snapshot()
        self.assertEqual(
            {
                "input_codec": "opus",
                "input_sample_rate_hz": 48_000,
                "input_channels": 2,
                "decoded_sample_rates_hz": [48_000],
                "decoded_channel_counts": [2],
            },
            snapshot["observed_audio"],
        )
        progress = snapshot["decoded_progress"]
        self.assertEqual(3, progress["frame_count"])
        self.assertEqual(3.0, progress["decoded_seconds"])
        self.assertEqual(1.0, progress["max_pts_gap_seconds"])
        self.assertEqual(0, progress["missing_pts"])
        self.assertEqual(0, progress["non_monotonic_pts"])
        self.assertEqual(0, progress["discontinuities"])

    def test_parser_flags_nonmonotonic_missing_and_large_pts_gaps(self) -> None:
        parser = receiver.FFmpegLogParser()
        lines = [
            "  Stream #0:0: Audio: flac, 48000 Hz, mono, s16",
            "[Parsed_ashowinfo_1 @ x] n:0 pts:0 pts_time:0 rate:48000 nb_samples:48000 channels:1 chlayout:mono",
            "[Parsed_ashowinfo_1 @ x] n:1 pts:144000 pts_time:3 rate:48000 nb_samples:48000 channels:1 chlayout:mono",
            "[Parsed_ashowinfo_1 @ x] n:2 pts:NOPTS pts_time:N/A rate:48000 nb_samples:48000 channels:1 chlayout:mono",
            "[Parsed_ashowinfo_1 @ x] n:3 pts:96000 pts_time:2 rate:48000 nb_samples:48000 channels:1 chlayout:mono",
        ]
        for line in lines:
            parser.feed_line(line)

        progress = parser.snapshot()["decoded_progress"]
        self.assertEqual(1, progress["missing_pts"])
        self.assertGreaterEqual(progress["non_monotonic_pts"], 1)
        self.assertGreaterEqual(progress["discontinuities"], 3)

    def test_parser_rejects_subframe_pts_drift_that_old_gate_allowed(self) -> None:
        parser = receiver.FFmpegLogParser()
        lines = [
            "  Stream #0:0: Audio: opus, 48000 Hz, stereo, fltp",
            "[Parsed_ashowinfo_1 @ x] n:0 pts:0 pts_time:0 rate:48000 nb_samples:48000 channels:2 chlayout:stereo",
            "[Parsed_ashowinfo_1 @ x] n:1 pts:57600 pts_time:1.2 rate:48000 nb_samples:48000 channels:2 chlayout:stereo",
        ]
        for line in lines:
            parser.feed_line(line)

        progress = parser.snapshot()["decoded_progress"]
        self.assertEqual(1, progress["discontinuities"])
        self.assertAlmostEqual(0.2, progress["max_gap_deviation_seconds"])

    def test_parser_accepts_only_the_initial_opus_pts_adjustment(self) -> None:
        lines = [
            "  Stream #0:0: Audio: opus, 48000 Hz, stereo, fltp",
            *(
                "[Parsed_ashowinfo_1 @ x] "
                f"n:{index} pts:{pts} pts_time:{pts_time} rate:48000 "
                "nb_samples:48000 channels:2 chlayout:stereo"
                for index, (pts, pts_time) in enumerate(
                    (
                        (336, 0.007),
                        (47_976, 0.9995),
                        (95_976, 1.9995),
                        (143_976, 2.9995),
                        (191_976, 3.9995),
                        (239_976, 4.9995),
                        (287_976, 5.9995),
                        (335_976, 6.9995),
                        (383_976, 7.9995),
                        (431_976, 8.9995),
                    )
                )
            ),
        ]
        parser = receiver.FFmpegLogParser()
        for line in lines:
            parser.feed_line(line)

        progress = parser.snapshot()["decoded_progress"]
        self.assertEqual(10, progress["frame_count"])
        self.assertEqual(10.0, progress["decoded_seconds"])
        self.assertEqual(1, progress["startup_pts_adjustments"])
        self.assertAlmostEqual(
            0.0075, progress["max_startup_pts_adjustment_seconds"]
        )
        self.assertEqual(0, progress["discontinuities"])

        report = passing_report()
        report["configuration"]["duration_seconds"] = 8.0
        report["observation"]["decoded_progress"] = progress
        self.assertEqual("pass", receiver.assess_report(report)["status"])

        late_parser = receiver.FFmpegLogParser()
        for line in (
            lines[0],
            lines[1].replace("pts_time:0.007", "pts_time:0"),
            lines[2].replace("pts_time:0.9995", "pts_time:1"),
            lines[3].replace("pts_time:1.9995", "pts_time:1.9925"),
        ):
            late_parser.feed_line(line)
        late_progress = late_parser.snapshot()["decoded_progress"]
        self.assertEqual(0, late_progress["startup_pts_adjustments"])
        self.assertEqual(1, late_progress["discontinuities"])

    def test_assessment_passes_only_complete_caps_progress_and_cleanup(self) -> None:
        result = receiver.assess_report(passing_report())
        self.assertEqual("pass", result["status"])
        self.assertTrue(all(result["checks"].values()))

        mutations = {
            "binding": lambda report: report.update({"scenario": None}),
            "caps": lambda report: report["observation"]["observed_audio"].update(
                {"input_codec": "flac"}
            ),
            "pts": lambda report: report["observation"]["decoded_progress"].update(
                {"discontinuities": 1}
            ),
            "error": lambda report: report["observation"].update(
                {"errors": ["decode error"]}
            ),
            "timeout": lambda report: report["observation"].update(
                {"timed_out": True, "termination_reason": "timeout"}
            ),
            "cleanup": lambda report: report["observation"]["cleanup"].update(
                {"completed": False}
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                report = json.loads(json.dumps(passing_report()))
                mutate(report)
                self.assertEqual("fail", receiver.assess_report(report)["status"])

    def test_assessment_rejects_eighty_percent_then_tail_stall(self) -> None:
        report = passing_report()
        report["observation"]["decoded_progress"].update(
            {"frame_count": 8, "decoded_seconds": 8.0}
        )
        report["observation"].update(
            {
                "last_decoded_progress_wall_seconds": 8.75,
                "decoded_progress_tail_stall_seconds": 2.0,
            }
        )

        result = receiver.assess_report(report)

        self.assertEqual("fail", result["status"])
        self.assertFalse(result["checks"]["decoded_progress"])
        self.assertFalse(
            result["checks"]["decoded_progress_tail_stall_bounded"]
        )

        overrun = passing_report()
        overrun["observation"]["decoded_progress"].update(
            {"frame_count": 13, "decoded_seconds": 13.0, "last_pts_seconds": 12.0}
        )
        self.assertFalse(
            receiver.assess_report(overrun)["checks"]["decoded_progress"]
        )

    def test_assessment_rejects_midrun_freeze_followed_by_catchup(self) -> None:
        report = passing_report()
        report["observation"]["max_decoded_progress_wall_gap_seconds"] = 30.0

        result = receiver.assess_report(report)

        self.assertEqual("fail", result["status"])
        self.assertFalse(
            result["checks"]["decoded_progress_wall_gaps_bounded"]
        )

    def test_timeout_interrupts_receiver_and_records_failed_cleanup_result(self) -> None:
        process = FakeProcess()

        report = receiver.run_receiver(
            arguments(duration_seconds=2.0, timeout_seconds=3.0),
            popen_factory=lambda *_args, **_kwargs: process,
            run_factory=identity_runner,
            clock=IncrementingClock(),
            sleeper=lambda _seconds: None,
        )

        observation = report["observation"]
        self.assertTrue(observation["timed_out"])
        self.assertEqual("timeout", observation["termination_reason"])
        self.assertFalse(observation["duration_reached"])
        self.assertTrue(observation["cleanup"]["completed"])
        self.assertIsNotNone(observation["cleanup"]["signal"])
        self.assertTrue(process.signals)
        self.assertEqual("fail", report["result"]["status"])

    def test_run_records_first_progress_and_bounded_libsrt_identity(self) -> None:
        process = CleanExitProcess(log_lines(frames=3))

        report = receiver.run_receiver(
            arguments(duration_seconds=2.0, timeout_seconds=10.0),
            popen_factory=lambda *_args, **_kwargs: process,
            run_factory=identity_runner,
            clock=IncrementingClock(step=1.0),
            sleeper=lambda _seconds: time.sleep(0.001),
        )

        self.assertEqual("pass", report["result"]["status"], report["result"])
        self.assertTrue(report["ffmpeg"]["libsrt_enabled"])
        self.assertLessEqual(
            len(report["ffmpeg"]["configuration_line"]), receiver.MAX_LINE_CHARS
        )
        first = report["observation"]["first_decoded_progress_wall_seconds"]
        last = report["observation"]["last_decoded_progress_wall_seconds"]
        tail_stall = report["observation"][
            "decoded_progress_tail_stall_seconds"
        ]
        max_wall_gap = report["observation"][
            "max_decoded_progress_wall_gap_seconds"
        ]
        self.assertIsInstance(first, float)
        self.assertGreaterEqual(first, 0.0)
        self.assertGreaterEqual(last, first)
        self.assertLessEqual(tail_stall, receiver.PROGRESS_TOLERANCE_SECONDS)
        self.assertLessEqual(max_wall_gap, receiver.MAX_PROGRESS_WALL_GAP_SECONDS)

    def test_terminate_escalation_is_recorded_and_cannot_pass(self) -> None:
        process = TerminateRequiredProcess(log_lines(frames=3))

        report = receiver.run_receiver(
            arguments(duration_seconds=2.0, timeout_seconds=10.0),
            popen_factory=lambda *_args, **_kwargs: process,
            run_factory=identity_runner,
            clock=IncrementingClock(step=1.0),
            sleeper=lambda _seconds: time.sleep(0.001),
        )

        cleanup = report["observation"]["cleanup"]
        self.assertTrue(cleanup["completed"])
        self.assertTrue(cleanup["terminate_used"])
        self.assertFalse(cleanup["kill_used"])
        self.assertEqual("fail", report["result"]["status"])
        self.assertFalse(
            report["result"]["checks"]["receiver_process_cleaned_up"]
        )

    def test_early_clean_process_exit_is_not_duration_success(self) -> None:
        process = FakeProcess(log_lines(frames=1))

        def reach_eos(_seconds: float) -> None:
            time.sleep(0.001)
            process.returncode = 0

        report = receiver.run_receiver(
            arguments(duration_seconds=2.0, timeout_seconds=10.0),
            popen_factory=lambda *_args, **_kwargs: process,
            run_factory=identity_runner,
            clock=IncrementingClock(step=0.5),
            sleeper=reach_eos,
        )

        self.assertEqual("process_exit", report["observation"]["termination_reason"])
        self.assertFalse(report["observation"]["duration_reached"])
        self.assertEqual("fail", report["result"]["status"])

    def test_nonzero_exit_and_forced_reader_close_are_fail_closed(self) -> None:
        nonzero = receiver.run_receiver(
            arguments(duration_seconds=2.0, timeout_seconds=10.0),
            popen_factory=lambda *_args, **_kwargs: CleanExitProcess(
                log_lines(frames=3), exit_code=1
            ),
            run_factory=identity_runner,
            clock=IncrementingClock(step=1.0),
            sleeper=lambda _seconds: time.sleep(0.001),
        )
        self.assertEqual("fail", nonzero["result"]["status"])
        self.assertFalse(
            nonzero["result"]["checks"]["duration_completed_without_timeout"]
        )

        hanging = ReaderHangProcess(log_lines(frames=3))
        with mock.patch.object(receiver, "TERMINATE_GRACE_SECONDS", 0.01):
            forced = receiver.run_receiver(
                arguments(duration_seconds=2.0, timeout_seconds=10.0),
                popen_factory=lambda *_args, **_kwargs: hanging,
                run_factory=identity_runner,
                clock=IncrementingClock(step=1.0),
                sleeper=lambda _seconds: time.sleep(0.001),
            )
        cleanup = forced["observation"]["cleanup"]
        self.assertTrue(cleanup["stderr_forced_close_used"])
        self.assertIsNotNone(cleanup["stderr_reader_error"])
        self.assertEqual("fail", forced["result"]["status"])
        self.assertFalse(
            forced["result"]["checks"]["receiver_process_cleaned_up"]
        )

    def test_redaction_hides_listener_host_and_accidental_srt_secrets(self) -> None:
        secret = "never-render-this-secret"
        value = (
            "open srt://192.168.1.174:9153?mode=listener&"
            f"passphrase={secret}&streamid=bird"
        )
        redacted = receiver.redact_text(value)

        self.assertNotIn("192.168.1.174", redacted)
        self.assertNotIn(secret, redacted)
        self.assertNotIn("bird", redacted)
        self.assertIn("<listener>:9153", redacted)
        self.assertGreaterEqual(redacted.count("<redacted>"), 2)
        self.assertEqual(
            "srt://<listener>?passphrase=<redacted>",
            receiver.redact_text("srt://host:not-a-port?passphrase=hidden"),
        )

        parser = receiver.FFmpegLogParser()
        for index in range(receiver.MAX_STDERR_LINES + 5):
            parser.feed_line(f"line {index} {value}")
        snapshot = parser.snapshot()
        self.assertEqual(receiver.MAX_STDERR_LINES, len(snapshot["stderr_tail"]))
        rendered = json.dumps(snapshot)
        self.assertNotIn(secret, rendered)
        self.assertNotIn("192.168.1.174", rendered)

    def test_invalid_arguments_and_forbidden_identity_options_fail_closed(self) -> None:
        invalid = (
            arguments(bind_address="localhost"),
            arguments(bind_address="::1"),
            arguments(bind_address=123),
            arguments(bind_address="192.168.1.174\n"),
            arguments(rtsp_publish_url="http://127.0.0.1/path"),
            arguments(rtsp_publish_url="rtsp://user:secret@127.0.0.1/path"),
            arguments(rtsp_publish_url="rtsp://127.0.0.1/path?token=secret"),
            arguments(rtsp_publish_url="rtsp://127.0.0.1/path#fragment"),
            arguments(rtsp_publish_url="rtsp://127.0.0.1"),
            arguments(rtsp_publish_url="rtsp://127.0.0.1:70000/path"),
            arguments(rtsp_publish_url="rtsp://127.0.0.1:0/path"),
            arguments(rtsp_publish_url="rtsp://127.0.0.1:bad/path"),
            arguments(rtsp_publish_url="rtsp://127.0.0.1/path\n"),
            arguments(rtsp_publish_url="rtsp://127.0.0.1\\path"),
            arguments(rtsp_publish_url=42),
            arguments(port=0),
            arguments(latency_ms=1),
            arguments(timeout_seconds=10.0),
            arguments(expected_codec="aac"),
            arguments(expected_channels=0),
            arguments(scenario="bad\nlabel"),
            arguments(scenario=None),
            arguments(source_bundle_sha256="not-a-hash"),
            arguments(source_bundle_sha256=None),
        )
        for args in invalid:
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    receiver.validate_args(args)

        parser = receiver.build_parser()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(())

        secret = "do-not-echo-me"
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = receiver.main(("--passphrase", secret))
        self.assertEqual(2, code)
        self.assertNotIn(secret, stderr.getvalue())

    def test_ffmpeg_identity_failure_is_a_bounded_setup_failure(self) -> None:
        def missing(*_args: object, **_kwargs: object) -> object:
            raise FileNotFoundError("ffmpeg was not found")

        report = receiver.run_receiver(arguments(), run_factory=missing)

        self.assertEqual("fail", report["result"]["status"])
        self.assertTrue(report["setup_errors"])
        self.assertEqual({}, report["observation"])


if __name__ == "__main__":
    unittest.main()
