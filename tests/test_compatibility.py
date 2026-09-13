from __future__ import annotations

import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tinypirelay.compatibility import (  # noqa: E402
    EvidenceError,
    computed_capabilities,
    load_evidence,
    main,
    receiver_results,
    validate_evidence,
)


EVIDENCE_PATH = ROOT / "evidence" / "stream_compatibility.json"


def _set_result(attempt: dict, status: str) -> None:
    attempt["availability"] = "available"
    attempt["applicability"] = "applicable"
    attempt["result"].update(
        status=status,
        tested_at_utc="2026-08-17T12:00:00Z",
        tool_version="fixture 1.0",
        notes="Fixture result.",
    )


def _resolve_representation(
    evidence: dict,
    representation: dict,
    *,
    ffplay: str = "pass",
    vlc: str = "pass",
) -> None:
    evidence["evidence_context"].update(
        recorded_at_utc="2026-08-17T12:00:00Z",
        host="fixture-host",
        os_release="Raspberry Pi OS 12 (fixture)",
        kernel="6.1.0-fixture",
        architecture="armv6l",
        userland_bits=32,
        dpkg_architecture="armhf",
        board_model="Raspberry Pi Zero W Rev 1.1",
    )
    for tool in evidence["tool_versions"]:
        evidence["tool_versions"][tool] = "fixture 1.0"
    for element in evidence["element_versions"]:
        evidence["element_versions"][element] = "fixture plugin 1.0"
    _set_result(representation["sender"], "pass")
    _set_result(representation["receivers"]["gstreamer"], "pass")
    _set_result(representation["receivers"]["ffplay"], ffplay)
    _set_result(representation["receivers"]["vlc"], vlc)


def _pending_fixture(evidence: dict) -> dict:
    pending = copy.deepcopy(evidence)
    pending["declared_capabilities"] = []
    for representation in pending["representations"]:
        sender = representation["sender"]
        sender["availability"] = "unknown"
        sender["result"].update(
            status="pending", tested_at_utc=None, tool_version=None, notes="Fixture pending."
        )
        for receiver in representation["receivers"].values():
            receiver["availability"] = "unknown"
            receiver["applicability"] = "applicable"
            receiver["result"].update(
                status="pending",
                tested_at_utc=None,
                tool_version=None,
                notes="Fixture pending.",
            )
    return pending


class CompatibilityEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository_evidence = load_evidence(EVIDENCE_PATH)
        self.evidence = _pending_fixture(self.repository_evidence)

    def test_repository_evidence_validates_three_live_capabilities(self) -> None:
        expected = tuple(
            representation["id"] for representation in self.repository_evidence["representations"]
        )
        self.assertEqual(expected, validate_evidence(self.repository_evidence))
        self.assertEqual(list(expected), self.repository_evidence["declared_capabilities"])
        for representation in self.repository_evidence["representations"]:
            self.assertEqual("pass", representation["sender"]["result"]["status"])
            self.assertEqual(
                "pass", representation["receivers"]["gstreamer"]["result"]["status"]
            )
            self.assertEqual("pass", representation["receivers"]["ffplay"]["result"]["status"])
            self.assertEqual(
                "unavailable", representation["receivers"]["vlc"]["result"]["status"]
            )

    def test_evidence_contains_exact_argv_recipes_and_listener_uris(self) -> None:
        expected_codecs = {
            "pcm_s16le_48000_stereo_matroska": (9101, None),
            "flac_48000_stereo_matroska": (9102, "flacenc"),
            "opus_128k_48000_stereo_matroska": (9103, "opusenc"),
        }
        for representation in self.repository_evidence["representations"]:
            port, encoder = expected_codecs[representation["id"]]
            synthetic = representation["sender"]["synthetic_argv"]
            alsa = representation["sender"]["alsa_argv"]
            self.assertEqual("gst-launch-1.0", synthetic[0])
            self.assertEqual("gst-launch-1.0", alsa[0])
            self.assertIn("audiotestsrc", synthetic)
            self.assertIn("alsasrc", alsa)
            self.assertIn("device=<ALSA_DEVICE>", alsa)
            self.assertIn("streamable=true", synthetic)
            self.assertIn(f"uri=srt://127.0.0.1:{port}?mode=caller&latency=200", synthetic)
            if encoder:
                self.assertIn(encoder, synthetic)

            receivers = representation["receivers"]
            self.assertEqual(
                f"srt://:{port}?mode=listener&latency=200",
                receivers["gstreamer"]["uri"],
            )
            self.assertEqual(
                f"srt://0.0.0.0:{port}?mode=listener&latency=200000",
                receivers["ffplay"]["uri"],
            )
            self.assertEqual(f"srt://@:{port}?mode=listener", receivers["vlc"]["uri"])
            self.assertEqual("gst-launch-1.0", receivers["gstreamer"]["argv"][0])
            self.assertEqual("ffplay", receivers["ffplay"]["argv"][0])
            self.assertEqual("cvlc", receivers["vlc"]["argv"][0])

    def test_gstreamer_recipe_semantic_drift_is_rejected(self) -> None:
        cases = (
            (0, "sender", "synthetic_argv", "audiotestsrc", "fakesrc"),
            (0, "sender", "synthetic_argv", "srtsink", "fakesink"),
            (1, "sender", "alsa_argv", "flacenc", "opusenc"),
            (2, "sender", "synthetic_argv", "opusparse", "flacparse"),
            (2, "sender", "synthetic_argv", "matroskamux", "mpegtsmux"),
            (1, "receivers", "gstreamer", "srtsrc", "fakesrc"),
            (1, "receivers", "gstreamer", "flacdec", "fakesink"),
            (2, "receivers", "gstreamer", "opusdec", "flacdec"),
        )
        for index, owner, command, old, new in cases:
            with self.subTest(index=index, command=command, old=old, new=new):
                evidence = copy.deepcopy(self.repository_evidence)
                representation = evidence["representations"][index]
                argv = (
                    representation[owner][command]
                    if owner == "sender"
                    else representation[owner][command]["argv"]
                )
                argv[argv.index(old)] = new
                with self.assertRaisesRegex(EvidenceError, "representation recipe"):
                    computed_capabilities(evidence)

    def test_caps_and_srt_token_drift_is_rejected(self) -> None:
        cases = (
            (0, "sender", "synthetic_argv", "rate=48000", "rate=44100"),
            (2, "sender", "synthetic_argv", "mode=caller", "mode=listener"),
            (2, "sender", "synthetic_argv", ":9103", ":9199"),
            (2, "sender", "synthetic_argv", "latency=200", "latency=201"),
            (1, "gstreamer", "argv", "mode=listener", "mode=caller"),
            (1, "gstreamer", "argv", ":9102", ":9199"),
            (1, "ffplay", "argv", "latency=200000", "latency=200001"),
            (0, "vlc", "argv", ":9101", ":9199"),
        )
        for index, owner, command, old, new in cases:
            with self.subTest(index=index, owner=owner, old=old, new=new):
                evidence = copy.deepcopy(self.repository_evidence)
                representation = evidence["representations"][index]
                argv = (
                    representation["sender"][command]
                    if owner == "sender"
                    else representation["receivers"][owner][command]
                )
                token_index = next(i for i, token in enumerate(argv) if old in token)
                argv[token_index] = argv[token_index].replace(old, new)
                with self.assertRaisesRegex(EvidenceError, "representation recipe"):
                    computed_capabilities(evidence)

        for receiver in ("gstreamer", "ffplay", "vlc"):
            with self.subTest(receiver=receiver, field="uri"):
                evidence = copy.deepcopy(self.repository_evidence)
                attempt = evidence["representations"][0]["receivers"][receiver]
                attempt["uri"] = attempt["uri"].replace(":9101", ":9199")
                with self.assertRaisesRegex(EvidenceError, "representation recipe"):
                    computed_capabilities(evidence)

    def test_required_element_group_semantic_drift_is_rejected(self) -> None:
        cases = (
            (0, "sender_synthetic", "audiotestsrc", "fakesrc"),
            (0, "sender_alsa", "alsasrc", "fakesrc"),
            (1, "gstreamer_receiver", "flacdec", "identity"),
            (2, "sender_synthetic", "opusparse", "flacparse"),
        )
        for index, group, old, new in cases:
            with self.subTest(index=index, group=group, old=old, new=new):
                evidence = copy.deepcopy(self.repository_evidence)
                elements = evidence["representations"][index]["required_elements"][group]
                elements[elements.index(old)] = new
                with self.assertRaisesRegex(EvidenceError, "representation recipe"):
                    computed_capabilities(evidence)

    def test_required_sender_and_gstreamer_must_pass(self) -> None:
        representation = self.evidence["representations"][0]
        _resolve_representation(self.evidence, representation)
        identifier = representation["id"]
        self.assertEqual((identifier,), computed_capabilities(self.evidence))

        for required_attempt in (
            representation["sender"],
            representation["receivers"]["gstreamer"],
        ):
            with self.subTest(attempt=required_attempt):
                failed = copy.deepcopy(self.evidence)
                failed_representation = failed["representations"][0]
                target = (
                    failed_representation["sender"]
                    if required_attempt is representation["sender"]
                    else failed_representation["receivers"]["gstreamer"]
                )
                target["result"]["status"] = "fail"
                self.assertEqual((), computed_capabilities(failed))

    def test_resolved_optional_failure_does_not_claim_that_receiver(self) -> None:
        representation = self.evidence["representations"][0]
        _resolve_representation(self.evidence, representation, ffplay="fail")
        identifier = representation["id"]
        self.assertEqual((identifier,), computed_capabilities(self.evidence))
        results = receiver_results(self.evidence)
        self.assertEqual("fail", results[identifier]["ffplay"])
        self.assertEqual("pass", results[identifier]["gstreamer"])

    def test_optional_pending_blocks_but_explicit_unavailable_resolves(self) -> None:
        representation = self.evidence["representations"][0]
        _resolve_representation(self.evidence, representation)
        ffplay = representation["receivers"]["ffplay"]
        ffplay["result"]["status"] = "pending"
        self.assertEqual((), computed_capabilities(self.evidence))

        ffplay["availability"] = "unavailable"
        ffplay["result"]["status"] = "unavailable"
        self.assertEqual((representation["id"],), computed_capabilities(self.evidence))

    def test_explicit_not_applicable_optional_receiver_resolves(self) -> None:
        representation = self.evidence["representations"][0]
        _resolve_representation(self.evidence, representation)
        vlc = representation["receivers"]["vlc"]
        vlc["applicability"] = "not_applicable"
        vlc["result"]["status"] = "not_applicable"
        self.assertEqual((representation["id"],), computed_capabilities(self.evidence))

    def test_pending_failed_and_malformed_required_evidence_never_enables(self) -> None:
        for status in ("pending", "fail"):
            with self.subTest(status=status):
                evidence = copy.deepcopy(self.evidence)
                representation = evidence["representations"][0]
                _resolve_representation(evidence, representation)
                representation["sender"]["result"]["status"] = status
                self.assertEqual((), computed_capabilities(evidence))

        malformed = copy.deepcopy(self.evidence)
        malformed["representations"][0]["sender"]["result"]["status"] = "maybe"
        with self.assertRaises(EvidenceError):
            computed_capabilities(malformed)

    def test_pass_and_fail_results_require_timestamp_and_tool_version(self) -> None:
        for status in ("pass", "fail"):
            for missing_field in ("tested_at_utc", "tool_version"):
                with self.subTest(status=status, missing_field=missing_field):
                    evidence = copy.deepcopy(self.evidence)
                    representation = evidence["representations"][0]
                    _resolve_representation(evidence, representation, ffplay=status)
                    representation["receivers"]["ffplay"]["result"][missing_field] = None
                    with self.assertRaisesRegex(EvidenceError, "requires tested_at_utc"):
                        computed_capabilities(evidence)

    def test_null_context_tool_or_element_version_keeps_gate_closed(self) -> None:
        representation = self.evidence["representations"][0]
        _resolve_representation(self.evidence, representation)
        identifier = representation["id"]
        self.assertEqual((identifier,), computed_capabilities(self.evidence))

        for field in self.evidence["evidence_context"]:
            with self.subTest(context=field):
                incomplete = copy.deepcopy(self.evidence)
                incomplete["evidence_context"][field] = None
                self.assertEqual((), computed_capabilities(incomplete))

        missing_gst_version = copy.deepcopy(self.evidence)
        missing_gst_version["tool_versions"]["gst-launch-1.0"] = None
        self.assertEqual((), computed_capabilities(missing_gst_version))

        referenced = {
            element
            for names in representation["required_elements"].values()
            for element in names
        }
        for element in referenced:
            with self.subTest(element=element):
                incomplete = copy.deepcopy(self.evidence)
                incomplete["element_versions"][element] = None
                self.assertEqual((), computed_capabilities(incomplete))

        missing_gst_version["declared_capabilities"] = [identifier]
        with self.assertRaisesRegex(EvidenceError, "declared_capabilities"):
            validate_evidence(missing_gst_version)

    def test_declared_capabilities_must_equal_computed_gate(self) -> None:
        representation = self.evidence["representations"][0]
        _resolve_representation(self.evidence, representation)
        with self.assertRaisesRegex(EvidenceError, "declared_capabilities"):
            validate_evidence(self.evidence)
        self.evidence["declared_capabilities"] = [representation["id"]]
        self.assertEqual((representation["id"],), validate_evidence(self.evidence))

    def test_duplicate_json_keys_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.json"
            path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
            with self.assertRaisesRegex(EvidenceError, "duplicate JSON key"):
                load_evidence(path)

    def test_oversized_json_integer_is_rejected_as_evidence_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "huge-integer.json"
            path.write_text(
                '{"schema_version":' + ("9" * 4301) + "}", encoding="utf-8"
            )
            with self.assertRaisesRegex(EvidenceError, "cannot read evidence"):
                load_evidence(path)

    def test_cli_prints_machine_readable_live_capabilities(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = main(("capabilities", str(EVIDENCE_PATH)))
        self.assertEqual(0, result)
        self.assertEqual(
            json.dumps(
                self.repository_evidence["declared_capabilities"], separators=(",", ":")
            ),
            stdout.getvalue().strip(),
        )


if __name__ == "__main__":
    unittest.main()
