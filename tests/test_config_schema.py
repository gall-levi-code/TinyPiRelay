import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ConfigSchemaTests(unittest.TestCase):
    def test_schema_and_example_are_versioned_and_fail_closed(self):
        schema = json.loads((ROOT / "config/tinypirelay.schema.json").read_text())
        example = json.loads((ROOT / "config/example.json").read_text())

        self.assertEqual(schema["properties"]["schema_version"]["const"], 1)
        self.assertEqual(example["schema_version"], 1)
        self.assertFalse(example["stream"]["enabled"])
        self.assertIsNone(example["stream"]["representation_id"])
        self.assertEqual(example["recording"]["required_mountpoint"], "/")
        self.assertFalse(schema["additionalProperties"])

        recording = schema["properties"]["recording"]
        self.assertIn("required_mountpoint", recording["required"])
        self.assertEqual(
            ["string", "null"],
            recording["properties"]["required_mountpoint"]["type"],
        )

    def test_capture_mode_is_a_combination_not_independent_lists(self):
        schema = json.loads((ROOT / "config/tinypirelay.schema.json").read_text())
        mode = schema["properties"]["capture"]["properties"]["mode"]
        combination = mode["oneOf"][1]

        self.assertEqual(
            combination["required"], ["format", "rate_hz", "channels"]
        )
        self.assertFalse(combination["additionalProperties"])

    def test_capture_formats_keep_24_bit_container_semantics(self):
        schema = json.loads((ROOT / "config/tinypirelay.schema.json").read_text())
        formats = schema["properties"]["capture"]["properties"]["mode"][
            "oneOf"
        ][1]["properties"]["format"]["enum"]

        self.assertIn("S24LE", formats)
        self.assertIn("S24_32LE", formats)
        self.assertIn("S32LE", formats)

    def test_enabled_stream_requires_a_concrete_representation_and_endpoint(self):
        schema = json.loads((ROOT / "config/tinypirelay.schema.json").read_text())
        conditional = schema["properties"]["stream"]["allOf"][0]

        self.assertEqual(
            {"properties": {"enabled": {"const": True}}, "required": ["enabled"]},
            conditional["if"],
        )
        enabled = conditional["then"]["properties"]
        self.assertEqual("string", enabled["representation_id"]["type"])
        self.assertEqual("string", enabled["destination_host"]["type"])
        self.assertEqual("integer", enabled["destination_port"]["type"])

    def test_text_and_rotation_bounds_match_runtime_and_form_contracts(self):
        schema = json.loads((ROOT / "config/tinypirelay.schema.json").read_text())
        capture = schema["properties"]["capture"]["properties"]
        stream = schema["properties"]["stream"]["properties"]
        recording = schema["properties"]["recording"]["properties"]

        self.assertEqual(256, capture["device_id"]["maxLength"])
        self.assertEqual(253, stream["destination_host"]["maxLength"])
        self.assertIn("IPv6", stream["destination_host"]["description"])
        self.assertEqual(512, stream["stream_id"]["maxLength"])
        self.assertEqual(10, stream["passphrase"]["minLength"])
        self.assertEqual(79, stream["passphrase"]["maxLength"])
        self.assertIn("printable ASCII", stream["passphrase"]["description"])
        self.assertEqual("^[\\u0020-\\u007E]{10,79}$", stream["passphrase"]["pattern"])
        self.assertEqual(2048, recording["directory"]["maxLength"])
        self.assertEqual(2048, recording["required_mountpoint"]["maxLength"])
        self.assertEqual(2_678_400, recording["rotation_seconds"]["maximum"])

        for field in (
            capture["device_id"],
            stream["stream_id"],
            recording["directory"],
            recording["required_mountpoint"],
        ):
            self.assertIn("\\u0000", field["pattern"])


if __name__ == "__main__":
    unittest.main()
