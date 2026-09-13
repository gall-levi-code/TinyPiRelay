import copy
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from tinypirelay.media_config import (
    DESTINATION_HOST_MAX_BYTES,
    DEVICE_ID_MAX_BYTES,
    PATH_FIELD_MAX_BYTES,
    PASSPHRASE_MAX_BYTES,
    ROTATION_SECONDS_MAX,
    STREAM_ID_MAX_BYTES,
    ConfigError,
    RecordingConfig,
    check_recording_destination,
    load_config,
    parse_config,
    validate_config,
)


ROOT = Path(__file__).resolve().parents[1]
APPROVED = {
    "pcm_s16le_48000_stereo_matroska",
    "flac_48000_stereo_matroska",
    "opus_128k_48000_stereo_matroska",
}


def example() -> dict:
    return json.loads((ROOT / "config/example.json").read_text(encoding="utf-8"))


def enabled_opus() -> dict:
    value = example()
    value["capture"] = {
        "device_id": "hw:CARD=iMM6C,DEV=0",
        "mode": {"format": "S16LE", "rate_hz": 48000, "channels": 1},
    }
    value["stream"].update(
        {
            "enabled": True,
            "representation_id": "opus_128k_48000_stereo_matroska",
            "destination_host": "192.168.1.174",
            "destination_port": 4001,
            "stream_id": "live_birdmic",
        }
    )
    return value


class RuntimeConfigTests(unittest.TestCase):
    def test_disabled_example_is_valid(self) -> None:
        config = load_config(ROOT / "config/example.json", APPROVED)

        self.assertFalse(config.stream.enabled)
        self.assertIsNone(config.capture.mode)
        self.assertEqual("/", config.recording.required_mountpoint)

    def test_enabled_stream_requires_an_exact_approved_representation(self) -> None:
        value = enabled_opus()

        config = validate_config(value, APPROVED)
        self.assertEqual(
            "opus_128k_48000_stereo_matroska",
            config.stream.representation_id,
        )
        with self.assertRaisesRegex(ConfigError, "approved capability"):
            validate_config(value, APPROVED - {value["stream"]["representation_id"]})

    def test_duplicate_keys_and_nonfinite_numbers_are_rejected(self) -> None:
        text = (ROOT / "config/example.json").read_text(encoding="utf-8")
        duplicate = text.replace(
            '"schema_version": 1,',
            '"schema_version": 1, "schema_version": 1,',
            1,
        )
        nonfinite = text.replace(
            '"spectrum_updates_per_second": 5',
            '"spectrum_updates_per_second": NaN',
            1,
        )

        with self.assertRaisesRegex(ConfigError, "duplicate key"):
            parse_config(duplicate, APPROVED)
        with self.assertRaisesRegex(ConfigError, "non-finite"):
            parse_config(nonfinite, APPROVED)

        huge_integer = text.replace(
            '"rotation_seconds": 3600',
            '"rotation_seconds": ' + "9" * 5_000,
            1,
        )
        with self.assertRaises(ConfigError):
            parse_config(huge_integer, APPROVED)

    def test_unknown_and_missing_fields_are_rejected_at_nested_boundaries(self) -> None:
        unknown = example()
        unknown["recording"]["delete_old_files"] = True
        missing = example()
        del missing["stream"]["latency_ms"]

        with self.assertRaisesRegex(ConfigError, "extra keys"):
            validate_config(unknown, APPROVED)
        with self.assertRaisesRegex(ConfigError, "missing keys"):
            validate_config(missing, APPROVED)

    def test_boolean_values_are_not_accepted_as_integers_or_numbers(self) -> None:
        mutations = (
            ("schema_version", lambda value: value.__setitem__("schema_version", True)),
            (
                "rotation_seconds",
                lambda value: value["recording"].__setitem__("rotation_seconds", True),
            ),
            (
                "spectrum_bands",
                lambda value: value["monitoring"].__setitem__("spectrum_bands", True),
            ),
            (
                "spectrum_updates_per_second",
                lambda value: value["monitoring"].__setitem__(
                    "spectrum_updates_per_second", True
                ),
            ),
        )
        for name, mutate in mutations:
            with self.subTest(name=name):
                value = example()
                mutate(value)
                with self.assertRaises(ConfigError):
                    validate_config(value, APPROVED)

    def test_monitoring_rate_accepts_sixty_hz_but_no_more(self) -> None:
        value = example()
        value["monitoring"]["spectrum_updates_per_second"] = 60
        self.assertEqual(60, validate_config(value, APPROVED).monitoring.spectrum_updates_per_second)
        value["monitoring"]["spectrum_updates_per_second"] = 60.01
        with self.assertRaisesRegex(ConfigError, "between 0 and 60"):
            validate_config(value, APPROVED)

    def test_capture_mode_is_a_validated_combination(self) -> None:
        value = enabled_opus()
        config = validate_config(value, APPROVED)

        self.assertEqual(("S16LE", 48000, 1), (
            config.capture.mode.format,
            config.capture.mode.rate_hz,
            config.capture.mode.channels,
        ))

        for key, bad in (("format", "F32LE"), ("rate_hz", 32000), ("channels", 4)):
            with self.subTest(key=key):
                invalid = copy.deepcopy(value)
                invalid["capture"]["mode"][key] = bad
                with self.assertRaisesRegex(ConfigError, "unsupported"):
                    validate_config(invalid, APPROVED)

        incomplete = copy.deepcopy(value)
        incomplete["capture"]["device_id"] = None
        with self.assertRaisesRegex(ConfigError, "must be set together"):
            validate_config(incomplete, APPROVED)

    def test_passphrase_never_appears_in_error_or_repr(self) -> None:
        value = enabled_opus()
        secret = "sensitive"
        value["stream"]["passphrase"] = secret

        with self.assertRaises(ConfigError) as raised:
            validate_config(value, APPROVED)
        self.assertNotIn(secret, str(raised.exception))

        value["stream"]["passphrase"] = "sensitive-enough"
        config = validate_config(value, APPROVED)
        self.assertNotIn("sensitive-enough", repr(config))

    def test_stream_ranges_are_fail_closed(self) -> None:
        value = enabled_opus()
        cases = (
            ("destination_port", 0),
            ("latency_ms", 19),
            ("stream_id", "x" * 513),
            ("passphrase", "short"),
            ("opus_bitrate_bps", 32000),
        )
        for key, bad in cases:
            with self.subTest(key=key):
                invalid = copy.deepcopy(value)
                invalid["stream"][key] = bad
                with self.assertRaises(ConfigError):
                    validate_config(invalid, APPROVED)

    def test_destination_host_accepts_bounded_dns_ipv4_and_ipv6_literals(self) -> None:
        valid = (
            "relay",
            "123",
            "relay.example.com",
            "RELAY.Example.COM.",
            "xn--bcher-kva.example",
            "192.0.2.10",
            "2001:db8::1",
            "[2001:db8::1]",
            "::ffff:192.0.2.10",
            ".".join(("a" * 63, "b" * 63, "c" * 63, "d" * 61)),
        )
        for host in valid:
            with self.subTest(host=host):
                value = enabled_opus()
                value["stream"]["destination_host"] = host
                self.assertEqual(
                    host,
                    validate_config(value, APPROVED).stream.destination_host,
                )

    def test_destination_host_rejects_ambiguous_or_unbounded_values(self) -> None:
        invalid = (
            "999.1.1.1",
            "1.2.3",
            "-relay.example",
            "relay-.example",
            "relay..example",
            "a" * 64 + ".example",
            "http://relay.example",
            "relay.example:4001",
            "relay/example",
            "[127.0.0.1]",
            "[2001:db8::1",
            "fe80::1%eth0",
            "récepteur.example",
            "x" * (DESTINATION_HOST_MAX_BYTES + 1),
        )
        for host in invalid:
            with self.subTest(host=host[:80]):
                value = enabled_opus()
                value["stream"]["destination_host"] = host
                with self.assertRaisesRegex(ConfigError, "destination_host"):
                    validate_config(value, APPROVED)

    def test_text_fields_and_rotation_have_encoded_and_control_bounds(self) -> None:
        mutations = (
            lambda value: value["capture"].__setitem__(
                "device_id", "x" * (DEVICE_ID_MAX_BYTES + 1)
            ),
            lambda value: value["capture"].__setitem__("device_id", "hw:\x00bad"),
            lambda value: value["stream"].__setitem__(
                "stream_id", "é" * (STREAM_ID_MAX_BYTES // 2 + 1)
            ),
            lambda value: value["stream"].__setitem__("stream_id", "live\nother"),
            lambda value: value["stream"].__setitem__("passphrase", "0123456789\n"),
            lambda value: value["stream"].__setitem__("passphrase", "é" * 10),
            lambda value: value["stream"].__setitem__(
                "passphrase", "x" * (PASSPHRASE_MAX_BYTES + 1)
            ),
            lambda value: value["recording"].__setitem__(
                "directory", "/" + "x" * PATH_FIELD_MAX_BYTES
            ),
            lambda value: value["recording"].__setitem__(
                "directory", "/recordings\rhidden"
            ),
            lambda value: value["recording"].__setitem__(
                "required_mountpoint", "/media\tother"
            ),
            lambda value: value["recording"].__setitem__(
                "rotation_seconds", ROTATION_SECONDS_MAX + 1
            ),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                value = enabled_opus()
                mutate(value)
                with self.assertRaises(ConfigError):
                    validate_config(value, APPROVED)

        value = enabled_opus()
        value["recording"].update(
            {
                "directory": "/media/録音 files",
                "required_mountpoint": "/media",
                "rotation_seconds": ROTATION_SECONDS_MAX,
            }
        )
        self.assertEqual(
            ROTATION_SECONDS_MAX,
            validate_config(value, APPROVED).recording.rotation_seconds,
        )


class DestinationSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = "/media/audio/recordings"
        self.mountpoint = "/media/audio"
        self.recording = RecordingConfig(self.directory, 3600, self.mountpoint)
        self.usage_paths: list[str] = []

    def check(
        self,
        *,
        used: int = 899,
        total: int = 1000,
        free: int = 101,
        isdir: bool = True,
        writable: bool = True,
        mounted: bool = True,
        directory_device: int = 17,
        mount_device: int = 17,
        usage_error: bool = False,
        recording: RecordingConfig | None = None,
    ):
        def stat(path: str):
            device = mount_device if path == self.mountpoint else directory_device
            return SimpleNamespace(st_dev=device)

        def usage(path: str):
            self.usage_paths.append(path)
            if usage_error:
                raise OSError("unavailable")
            return SimpleNamespace(total=total, used=used, free=free)

        return check_recording_destination(
            recording or self.recording,
            isdir_fn=lambda _path: isdir,
            access_fn=lambda _path, _mode: writable,
            stat_fn=stat,
            disk_usage_fn=usage,
            realpath_fn=lambda path: path,
            ismount_fn=lambda path: mounted and path == self.mountpoint,
        )

    def test_exact_ninety_percent_gate_uses_the_destination_filesystem(self) -> None:
        below = self.check(used=899, free=101)
        at_limit = self.check(used=900, free=100)

        self.assertTrue(below.safe)
        self.assertFalse(below.at_or_above_threshold)
        self.assertFalse(at_limit.safe)
        self.assertTrue(at_limit.at_or_above_threshold)
        self.assertEqual("storage_threshold", at_limit.reason)
        self.assertEqual(90.0, at_limit.used_percent)
        self.assertEqual([self.directory, self.directory], self.usage_paths)
        self.assertEqual(17, at_limit.st_dev)

    def test_missing_unwritable_and_usage_failure_fail_closed(self) -> None:
        cases = (
            ({"isdir": False}, "missing_or_not_directory"),
            ({"writable": False}, "not_writable"),
            ({"usage_error": True}, "usage_unavailable"),
        )
        for arguments, reason in cases:
            with self.subTest(reason=reason):
                status = self.check(**arguments)
                self.assertFalse(status.safe)
                self.assertEqual(reason, status.reason)

    def test_required_mountpoint_must_be_present_and_contain_the_directory(self) -> None:
        unavailable = self.check(mounted=False)
        outside = self.check(
            recording=RecordingConfig("/var/recordings", 3600, self.mountpoint)
        )

        self.assertEqual("required_mountpoint_unavailable", unavailable.reason)
        self.assertEqual("destination_outside_required_mountpoint", outside.reason)

    def test_required_mountpoint_and_destination_must_be_the_same_filesystem(self) -> None:
        mismatch = self.check(directory_device=17, mount_device=18)

        self.assertFalse(mismatch.safe)
        self.assertEqual("destination_filesystem_mismatch", mismatch.reason)
        self.assertEqual(17, mismatch.st_dev)
        self.assertEqual(18, mismatch.mount_st_dev)

    def test_nullable_mountpoint_still_returns_factual_destination_identity(self) -> None:
        recording = RecordingConfig(self.directory, 3600, None)
        status = self.check(recording=recording)

        self.assertTrue(status.safe)
        self.assertEqual("ok", status.reason)
        self.assertEqual(17, status.st_dev)
        self.assertIsNone(status.mount_st_dev)
        self.assertEqual((1000, 899, 101), (
            status.total_bytes,
            status.used_bytes,
            status.available_bytes,
        ))


if __name__ == "__main__":
    unittest.main()
