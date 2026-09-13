from __future__ import annotations

import contextlib
import io
import json
import unittest

from spikes import opus_bitrate_mutation as spike


def passing_report() -> dict:
    transitions = []
    count = 25
    pts = 480_000_000
    for previous, target in zip(
        spike.BITRATE_SEQUENCE_BPS, spike.BITRATE_SEQUENCE_BPS[1:]
    ):
        before = {"buffer_count": count, "last_pts_ns": pts}
        count += 25
        pts += 500_000_000
        after = {"buffer_count": count, "last_pts_ns": pts}
        transitions.append(
            {
                "from_bps": previous,
                "requested_bps": target,
                "readback_bps": target,
                "sender_state_before": "playing",
                "sender_state_after": "playing",
                "receiver_state_before": "playing",
                "receiver_state_after": "playing",
                "progress_within_timeout": True,
                "before": before,
                "after": after,
                "buffer_delta": 25,
                "pts_advanced": True,
            }
        )
    return {
        "setup_errors": [],
        "initial": {
            "requested_bps": 128_000,
            "readback_bps": 128_000,
            "sender_state": "playing",
            "receiver_state": "playing",
            "progress_within_timeout": True,
        },
        "transitions": transitions,
        "receiver": {
            "buffer_count": count,
            "last_pts_ns": pts,
            "max_gap_ns": 20_000_000,
            "missing_pts": 0,
            "non_monotonic_pts": 0,
        },
        "bus": {"errors": [], "eos": []},
    }


class OpusMutationSpikeTests(unittest.TestCase):
    def test_pipeline_is_exact_local_stereo_opus_srt_spike(self) -> None:
        pipelines = spike.pipeline_descriptions()
        self.assertIn("is-live=true", pipelines["sender"])
        self.assertIn("samplesperbuffer=960", pipelines["sender"])
        self.assertIn(
            "audio/x-raw,format=S16LE,layout=interleaved,rate=48000,channels=2",
            pipelines["sender"],
        )
        self.assertIn("opusenc name=encoder bitrate=128000", pipelines["sender"])
        self.assertIn("srt://127.0.0.1:9103?mode=caller&latency=200", pipelines["sender"])
        self.assertIn("srt://:9103?mode=listener&latency=200", pipelines["receiver"])
        self.assertIn("identity name=receiver_counter", pipelines["receiver"])
        self.assertEqual(
            (128_000, 192_000, 64_000, 96_000, 128_000),
            spike.BITRATE_SEQUENCE_BPS,
        )

    def test_assessment_passes_only_complete_readback_and_receive_progress(self) -> None:
        result = spike.assess_report(
            passing_report(), min_buffers_per_phase=25, max_gap_ns=60_000_000
        )
        self.assertEqual("pass", result["status"])
        self.assertTrue(result["property_changes_demonstrated"])
        self.assertTrue(result["continuous_receive_demonstrated"])
        self.assertEqual([], result["reasons"])

    def test_assessment_fails_readback_bus_and_continuity_gaps(self) -> None:
        report = passing_report()
        report["transitions"][1]["readback_bps"] = 63_999
        report["transitions"][2]["buffer_delta"] = 0
        report["receiver"]["max_gap_ns"] = 80_000_000
        report["bus"]["errors"].append({"message": "fixture failure"})
        result = spike.assess_report(
            report, min_buffers_per_phase=25, max_gap_ns=60_000_000
        )
        self.assertEqual("fail", result["status"])
        self.assertFalse(result["property_changes_demonstrated"])
        self.assertFalse(result["continuous_receive_demonstrated"])
        self.assertTrue(any("64000" in reason for reason in result["reasons"]))
        self.assertIn("GStreamer bus error observed", result["reasons"])
        self.assertIn("receiver PTS gap exceeded the configured limit", result["reasons"])

    def test_buffer_tracker_records_progress_and_timestamp_anomalies(self) -> None:
        tracker = spike.BufferTracker(clock_time_none=-1)
        for pts in (0, 20_000_000, 40_000_000, 35_000_000, -1):
            tracker.record(pts)
        snapshot = tracker.snapshot()
        self.assertEqual(5, snapshot["buffer_count"])
        self.assertEqual(1, snapshot["missing_pts"])
        self.assertEqual(1, snapshot["non_monotonic_pts"])
        self.assertEqual(20_000_000, snapshot["max_gap_ns"])

    def test_missing_gi_emits_json_and_exits_nonzero(self) -> None:
        def unavailable_loader():
            raise ImportError("fixture GI unavailable")

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            returncode = spike.main((), gst_loader=unavailable_loader)
        report = json.loads(stdout.getvalue())
        self.assertEqual(2, returncode)
        self.assertEqual("fail", report["result"]["status"])
        self.assertFalse(report["production_component"])
        self.assertEqual(1, report["step"])
        self.assertIn("fixture GI unavailable", report["setup_errors"][0])


if __name__ == "__main__":
    unittest.main()
