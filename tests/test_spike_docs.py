import hashlib
import json
import shlex
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SpikeDocumentationTests(unittest.TestCase):
    def test_documented_commands_match_machine_readable_evidence(self):
        evidence = json.loads(
            (ROOT / "evidence/stream_compatibility.json").read_text(encoding="utf-8")
        )
        documented_section = (ROOT / "docs/SPIKE_MEDIA.md").read_text(
            encoding="utf-8"
        ).split("## Runtime Opus bitrate mutation procedure", 1)[0]
        documented = [
            shlex.split(line)
            for line in documented_section.splitlines()
            if line.startswith(("gst-launch-1.0 ", "ffplay ", "cvlc "))
        ]

        representations = evidence["representations"]
        expected = [item["sender"]["synthetic_argv"] for item in representations]
        expected += [item["sender"]["alsa_argv"] for item in representations]
        expected += [
            item["receivers"]["gstreamer"]["argv"] for item in representations
        ]
        expected += [item["receivers"]["ffplay"]["argv"] for item in representations]
        expected += [item["receivers"]["vlc"]["argv"] for item in representations]

        self.assertEqual(expected, documented)

    def test_live_hardware_artifact_is_self_consistent(self):
        artifact = json.loads(
            (
                ROOT
                / "evidence/live_media_rpi_zero_w_imm6c_2026-08-18.json"
            ).read_text(encoding="utf-8")
        )
        commands = set(artifact["command_catalog"])
        loopbacks = artifact["gstreamer_loopback_runs"]
        interop = artifact["ffplay_interoperability_runs"]

        self.assertEqual(1, artifact["schema_version"])
        self.assertEqual(6, len(loopbacks))
        self.assertEqual(3, len(interop))
        self.assertEqual({"alsa", "synthetic"}, {run["source"] for run in loopbacks})
        for run in loopbacks + interop:
            self.assertEqual("pass", run["result"])
            self.assertEqual(0, run["sender_exit_code"])
            self.assertIn(run["sender_command"], commands)
            self.assertIn(run["receiver_command"], commands)

        compatibility = json.loads(
            (ROOT / "evidence/stream_compatibility.json").read_text(encoding="utf-8")
        )
        by_id = {item["id"]: item for item in compatibility["representations"]}
        for run in interop:
            parser = by_id[run["representation_id"]]["codec"]["parser_element"]
            sender = artifact["command_catalog"][run["sender_command"]].split()
            if parser is None:
                self.assertNotIn("flacparse", sender)
                self.assertNotIn("opusparse", sender)
            else:
                self.assertIn(parser, sender)
        self.assertEqual(0, artifact["queue_isolation"]["sender_exit_code"])
        self.assertEqual(0, artifact["queue_isolation"]["receiver_exit_code"])
        self.assertGreater(
            artifact["queue_isolation"]["stream_receiver_handoffs"],
            artifact["queue_isolation"]["slow_branch_handoffs"],
        )

        for key in ("probe", "opus_runtime_mutation"):
            record = artifact[key]
            digest = hashlib.sha256((ROOT / record["artifact"]).read_bytes()).hexdigest()
            self.assertEqual(record["sha256"], digest)


if __name__ == "__main__":
    unittest.main()
