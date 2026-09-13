from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from tinypirelay.audio_capabilities import (
    AudioCapabilityError,
    load_audio_capabilities,
    validate_audio_capabilities,
    validate_config_combination,
)
from tinypirelay.media_config import validate_config


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence" / "audio_capabilities.json"
STREAM_EVIDENCE = ROOT / "evidence" / "stream_compatibility.json"


class AudioCapabilitiesTests(unittest.TestCase):
    def setUp(self) -> None:
        stream = json.loads(STREAM_EVIDENCE.read_text(encoding="utf-8"))
        self.approved = tuple(stream["declared_capabilities"])
        self.value = json.loads(EVIDENCE.read_text(encoding="utf-8"))

    def test_repository_model_is_exact_and_non_cartesian(self) -> None:
        model = load_audio_capabilities(EVIDENCE, self.approved)
        devices = model.to_dict()["capture_devices"]
        self.assertEqual(
            ["hw:CARD=iMM6C,DEV=0", "plughw:CARD=USB,DEV=0"],
            [device["id"] for device in devices],
        )
        device = devices[0]
        modes = device["modes"]
        self.assertEqual(
            [("S16LE", 48_000, 1), ("S24LE", 48_000, 1)],
            [(item["format"], item["rate_hz"], item["channels"]) for item in modes],
        )
        self.assertEqual(3, len(modes[0]["stream_options"]))
        self.assertEqual(
            ["flac_48000_stereo_matroska"],
            [option["id"] for option in modes[1]["stream_options"]],
        )
        self.assertEqual(
            [("S24_32LE", 48_000, 2, False)],
            [
                (mode["format"], mode["rate_hz"], mode["channels"], mode["native"])
                for mode in devices[1]["modes"]
            ],
        )
        self.assertEqual([], devices[1]["modes"][0]["stream_options"])

    def test_rejects_unapproved_and_missing_declared_representations(self) -> None:
        unapproved = copy.deepcopy(self.value)
        unapproved["representations"][0]["id"] = "invented"
        with self.assertRaisesRegex(AudioCapabilityError, "exactly match"):
            validate_audio_capabilities(unapproved, self.approved)

        missing = copy.deepcopy(self.value)
        missing["representations"].pop()
        with self.assertRaisesRegex(AudioCapabilityError, "exactly match"):
            validate_audio_capabilities(missing, self.approved)

    def test_rejects_duplicate_mode_and_nonfinite_json(self) -> None:
        duplicate = copy.deepcopy(self.value)
        duplicate["devices"][0]["modes"].append(
            copy.deepcopy(duplicate["devices"][0]["modes"][0])
        )
        with self.assertRaisesRegex(AudioCapabilityError, "duplicate combinations"):
            validate_audio_capabilities(duplicate, self.approved)

        with self.subTest("NaN"):
            path = self._temporary_text('{"schema_version":NaN}')
            try:
                with self.assertRaisesRegex(AudioCapabilityError, "non-finite"):
                    load_audio_capabilities(path, self.approved)
            finally:
                path.unlink()

        with self.subTest("huge integer"):
            text = json.dumps(self.value).replace(
                '"rate_hz": 48000', '"rate_hz": ' + "9" * 5_000, 1
            )
            path = self._temporary_text(text)
            try:
                with self.assertRaises(AudioCapabilityError):
                    load_audio_capabilities(path, self.approved)
            finally:
                path.unlink()

    def test_config_gate_rejects_cartesian_synthesis(self) -> None:
        model = validate_audio_capabilities(self.value, self.approved)
        raw = json.loads((ROOT / "config" / "example.json").read_text(encoding="utf-8"))
        raw["capture"] = {
            "device_id": "hw:CARD=iMM6C,DEV=0",
            "mode": {"format": "S24LE", "rate_hz": 48_000, "channels": 1},
        }
        raw["stream"].update(
            {
                "enabled": True,
                "representation_id": self.approved[0],
                "destination_host": "127.0.0.1",
                "destination_port": 9000,
            }
        )
        for representation in self.approved:
            raw["stream"]["representation_id"] = representation
            config = validate_config(raw, self.approved)
            with self.subTest(representation=representation):
                if representation == "flac_48000_stereo_matroska":
                    mode = validate_config_combination(config, model)
                    self.assertEqual(("S24LE", 48_000, 1), mode.key())
                else:
                    with self.assertRaisesRegex(AudioCapabilityError, "not validated"):
                        validate_config_combination(config, model)

        raw["stream"]["representation_id"] = "flac_48000_stereo_matroska"
        for format_name, rate_hz, channels in (("S24LE", 96_000, 1), ("S24LE", 48_000, 2)):
            raw["capture"]["mode"] = {"format": format_name, "rate_hz": rate_hz, "channels": channels}
            with self.subTest(rate_hz=rate_hz, channels=channels):
                with self.assertRaisesRegex(AudioCapabilityError, "not validated"):
                    validate_config_combination(validate_config(raw, self.approved), model)

        raw["capture"] = {
            "device_id": "plughw:CARD=USB,DEV=0",
            "mode": {"format": "S24_32LE", "rate_hz": 48_000, "channels": 2},
        }
        with self.assertRaisesRegex(AudioCapabilityError, "not validated"):
            validate_config_combination(validate_config(raw, self.approved), model)

        raw["capture"]["device_id"] = "hw:CARD=iMM6C,DEV=0"
        raw["capture"]["mode"] = {
            "format": "S16LE",
            "rate_hz": 48_000,
            "channels": 1,
        }
        config = validate_config(raw, self.approved)
        mode = validate_config_combination(config, model)
        self.assertEqual("S16LE", mode.format)

    def test_unconfigured_audio_is_valid_idle_but_cannot_configure_streaming(self) -> None:
        model = validate_audio_capabilities(self.value, self.approved)
        raw = json.loads((ROOT / "config" / "example.json").read_text(encoding="utf-8"))
        self.assertIsNone(validate_config_combination(validate_config(raw, self.approved), model))
        raw["stream"]["representation_id"] = self.approved[0]
        with self.assertRaisesRegex(AudioCapabilityError, "before configuring streaming"):
            validate_config_combination(validate_config(raw, self.approved), model)

    def test_config_gate_requires_verified_parent_chain(self) -> None:
        raw = json.loads((ROOT / "config" / "example.json").read_text(encoding="utf-8"))
        raw["capture"] = {
            "device_id": "hw:CARD=iMM6C,DEV=0",
            "mode": {"format": "S16LE", "rate_hz": 48_000, "channels": 1},
        }
        raw["stream"].update(
            {
                "enabled": True,
                "representation_id": self.approved[0],
                "destination_host": "127.0.0.1",
                "destination_port": 9000,
            }
        )
        config = validate_config(raw, self.approved)

        unsupported_device = copy.deepcopy(self.value)
        unsupported_device["devices"][0]["evidence_status"] = "Unsupported"
        with self.assertRaisesRegex(AudioCapabilityError, "device is not validated"):
            validate_config_combination(
                config,
                validate_audio_capabilities(unsupported_device, self.approved),
            )

        unsupported_representation = copy.deepcopy(self.value)
        unsupported_representation["representations"][0][
            "evidence_status"
        ] = "Unsupported"
        with self.assertRaisesRegex(
            AudioCapabilityError, "representation is not validated"
        ):
            validate_config_combination(
                config,
                validate_audio_capabilities(
                    unsupported_representation, self.approved
                ),
            )

    def test_public_model_filters_every_unverified_parent_and_child(self) -> None:
        value = copy.deepcopy(self.value)
        value["devices"][0]["modes"][0]["stream_options"][0][
            "evidence_status"
        ] = "Likely compatible / untested"
        value["representations"][1]["evidence_status"] = "Unsupported"
        model = validate_audio_capabilities(value, self.approved)

        public = model.to_public_dict()

        option_ids = {
            option["id"]
            for device in public["capture_devices"]
            for mode in device["modes"]
            for option in mode["stream_options"]
        }
        representation_ids = {
            item["id"] for item in public["stream_representations"]
        }
        self.assertNotIn(self.approved[0], option_ids)
        self.assertNotIn(self.approved[1], option_ids)
        self.assertEqual(option_ids, representation_ids)

        for device in value["devices"]:
            device["evidence_status"] = "Unsupported"
        public = validate_audio_capabilities(value, self.approved).to_public_dict()
        self.assertEqual([], public["capture_devices"])
        self.assertEqual([], public["stream_representations"])

    def test_public_model_filters_modes_outside_media_config_domain(self) -> None:
        value = copy.deepcopy(self.value)
        source = value["devices"][0]["modes"][0]
        invalid_modes = (
            {**copy.deepcopy(source), "format": "F32LE"},
            {**copy.deepcopy(source), "rate_hz": 192_000},
            {**copy.deepcopy(source), "channels": 8},
        )
        value["devices"][0]["modes"].extend(invalid_modes)

        public = validate_audio_capabilities(value, self.approved).to_public_dict()

        keys = {
            (mode["format"], mode["rate_hz"], mode["channels"])
            for device in public["capture_devices"]
            for mode in device["modes"]
        }
        self.assertNotIn(("F32LE", 48_000, 1), keys)
        self.assertNotIn(("S16LE", 192_000, 1), keys)
        self.assertNotIn(("S16LE", 48_000, 8), keys)
        self.assertIn(("S16LE", 48_000, 1), keys)

    def _temporary_text(self, text: str) -> Path:
        import tempfile

        handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
        with handle:
            handle.write(text)
        return Path(handle.name)


if __name__ == "__main__":
    unittest.main()
