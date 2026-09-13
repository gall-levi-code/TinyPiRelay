from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from unittest import mock
from dataclasses import dataclass
from pathlib import Path

from tinypirelay.audio_capabilities import load_audio_capabilities
from tinypirelay.compatibility import load_evidence, validate_evidence
from tinypirelay.media_config import load_config
from tinypirelay.media_control import (
    MAX_DIAGNOSTIC_EVENTS,
    MediaControlError,
    MediaControlHandler,
    discover_capture_devices,
    internal_config_value,
)


ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Decision:
    name: str

    def to_dict(self) -> dict[str, object]:
        return {"ok": True, "changed": True, "name": self.name}


class Runtime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object | None]] = []
        self.recording_state = "stopped"
        self.queue_metrics = {"stream": {"current_time_ns": 123}}

    def snapshot(self) -> dict[str, object]:
        return {
            "state": {
                "service": "running",
                "capture": {"state": "running", "last_error": None},
                "stream": {"state": "running", "last_error": None},
                "connection": {"state": "connected", "last_error": None},
                "recording": {
                    "state": self.recording_state,
                    "last_error": None,
                    "warning": None,
                },
            },
            "telemetry": {},
            "runtime_metrics": {"queues": self.queue_metrics},
        }

    def apply_live_config(self, config: object, revision: str) -> None:
        self.calls.append(("apply_live_config", (config, revision)))

    def renew_spectrum_lease(self) -> bool:
        self.calls.append(("renew_spectrum_lease", None))
        return True

    def release_spectrum_lease(self) -> bool:
        self.calls.append(("release_spectrum_lease", None))
        return False

    def telemetry_snapshot(self) -> dict[str, object]:
        return {"meter": {"sequence": 7}, "spectrum": None}

    def __getattr__(self, name: str):
        def call(value: object | None = None) -> Decision:
            self.calls.append((name, value))
            return Decision(name)

        return call


class MediaControlTests(unittest.TestCase):
    def setUp(self) -> None:
        evidence = load_evidence(ROOT / "evidence" / "stream_compatibility.json")
        self.approved = validate_evidence(evidence)
        self.capabilities = load_audio_capabilities(
            ROOT / "evidence" / "audio_capabilities.json", self.approved
        )
        raw = json.loads((ROOT / "config" / "example.json").read_text(encoding="utf-8"))
        raw["capture"] = {
            "device_id": "hw:CARD=iMM6C,DEV=0",
            "mode": {"format": "S16LE", "rate_hz": 48_000, "channels": 1},
        }
        raw["stream"].update(
            {
                "representation_id": self.approved[-1],
                "destination_host": "127.0.0.1",
                "destination_port": 9000,
                "passphrase": "0123456789abcdef",
            }
        )
        self.temp = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp.name) / "config.json"
        self.config_path.write_text(json.dumps(raw), encoding="utf-8")
        self.config = load_config(self.config_path, self.approved)
        self.runtime = Runtime()
        self.handler = MediaControlHandler(
            self.runtime,
            self.config,
            self.config_path,
            "initial",
            self.approved,
            self.capabilities,
            clock=lambda: 10.0,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_status_capabilities_and_config_never_return_secret(self) -> None:
        status = self.handler("status", {})
        capabilities = self.handler("capabilities", {})
        config = self.handler("config.get", {})
        rendered = json.dumps((status, capabilities, config))
        self.assertNotIn("0123456789abcdef", rendered)
        self.assertTrue(config["config"]["stream"]["passphrase"]["configured"])
        self.assertEqual(2, len(capabilities["capture_devices"][0]["modes"]))
        self.assertEqual(
            self.runtime.queue_metrics,
            status["runtime_metrics"]["queues"],
        )
        self.assertEqual(5.0, status["monitoring_updates_per_second"])
        self.assertEqual(
            {"meter": {"sequence": 7}, "spectrum": None},
            self.handler("monitoring.telemetry", {}),
        )

    def test_device_refresh_marks_presence_without_inventing_validated_modes(self) -> None:
        self.handler._audio_discovery = lambda: {
            "status": "available", "devices": [
                {"id": "hw:CARD=USB,DEV=0", "label": "Scarlett"},
                {"id": "hw:CARD=Unknown,DEV=0", "label": "Unverified microphone"},
            ],
        }
        capabilities = self.handler("capabilities", {})
        devices = capabilities["capture_devices"]
        self.assertFalse(devices[0]["present"])
        self.assertTrue(devices[1]["present"])
        self.assertNotIn("hw:CARD=Unknown,DEV=0", [device["id"] for device in devices])
        self.handler._audio_discovery = lambda: {"status": "unavailable", "devices": []}
        self.assertIsNone(self.handler("capabilities", {})["capture_devices"][0]["present"])

    def test_capture_discovery_is_bounded_read_only_and_handles_no_soundcards(self) -> None:
        with mock.patch("tinypirelay.media_control.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="card 2: iMM6C [iMM6C], device 0: USB Audio [USB Audio]", stderr="")
            self.assertEqual({"status": "available", "devices": [{"id": "hw:CARD=iMM6C,DEV=0", "label": "iMM6C"}]}, discover_capture_devices())
            self.assertEqual(["arecord", "-l"], run.call_args.args[0])
            self.assertEqual(2, run.call_args.kwargs["timeout"])
            run.return_value = mock.Mock(returncode=1, stdout="", stderr="arecord: device_list:274: no soundcards found...")
            self.assertEqual({"status": "available", "devices": []}, discover_capture_devices())
            run.side_effect = PermissionError("private system detail")
            self.assertEqual({"status": "unavailable", "devices": []}, discover_capture_devices())

    def test_idle_configuration_can_save_settings_then_choose_validated_audio(self) -> None:
        idle = load_config(ROOT / "config/example.json", self.approved)
        handler = MediaControlHandler(self.runtime, idle, self.config_path, "idle", self.approved, self.capabilities)
        self.assertTrue(handler("status", {})["audio_setup_required"])
        view = handler("config.get", {})
        view["config"]["recording"]["rotation_seconds"] = 7200
        result = handler("config.update", {"config": view["config"], "confirm_restart": False, "expected_revision": view["revision"]})
        self.assertFalse(result["restart_required"])
        self.assertEqual("apply_live_config", self.runtime.calls[-1][0])
        view = handler("config.get", {})
        view["config"]["capture"] = {"device_id": "hw:CARD=iMM6C,DEV=0", "mode": {"format": "S16LE", "rate_hz": 48000, "channels": 1}}
        result = handler("config.update", {"config": view["config"], "confirm_restart": True, "expected_revision": view["revision"]})
        self.assertTrue(result["restart_required"])
        loaded = load_config(self.config_path, self.approved)
        restarted = MediaControlHandler(self.runtime, loaded, self.config_path, result["new_revision"], self.approved, self.capabilities)
        self.assertFalse(restarted("status", {})["audio_setup_required"])
        self.assertTrue(restarted("capture.start", {})["ok"])
        before = self.config_path.read_bytes()
        view = restarted("config.get", {})
        view["config"]["capture"]["mode"]["channels"] = 2
        with self.assertRaisesRegex(MediaControlError, "combination is not validated"):
            restarted("config.update", {"config": view["config"], "confirm_restart": True, "expected_revision": view["revision"]})
        self.assertEqual(before, self.config_path.read_bytes())

    def test_device_telemetry_is_copied_redacted_and_does_not_add_mutations(self) -> None:
        sample = {
            "sequence": 1,
            "config_revision": "initial",
            "system": {"cpu_percent": 0.0},
            "recording": {"file": "/recordings/one.flac", "size_bytes": 1 << 33},
            "unexpected_password": "must-not-leak",
        }
        self.handler._device_telemetry = lambda: sample
        status = self.handler("status", {})
        self.assertEqual(1 << 33, status["device_telemetry"]["recording"]["size_bytes"])
        self.assertEqual(0.0, status["device_telemetry"]["system"]["cpu_percent"])
        self.assertNotIn("must-not-leak", json.dumps(status))
        status["device_telemetry"]["system"]["cpu_percent"] = 90.0
        self.assertEqual(0.0, sample["system"]["cpu_percent"])
        self.handler._device_telemetry = lambda: None
        self.assertIsNone(self.handler("status", {})["device_telemetry"])
        self.assertEqual([], self.runtime.calls)

    def test_commands_are_allowlisted_and_opus_bitrate_is_typed(self) -> None:
        result = self.handler("stream.set_opus_bitrate", {"bitrate_bps": 96_000})
        self.assertTrue(result["ok"])
        self.assertEqual(("set_opus_bitrate", 96_000), self.runtime.calls[-1])
        with self.assertRaisesRegex(MediaControlError, "bitrate_bps"):
            self.handler("stream.set_opus_bitrate", {"bitrate_bps": "96000"})
        with self.assertRaisesRegex(MediaControlError, "not supported"):
            self.handler("service.shutdown", {})

    def test_internal_spectrum_lease_is_fixed_quiet_and_allowlisted(self) -> None:
        before = len(self.handler("status", {})["diagnostics"]["events"])

        lease = self.handler("monitoring.spectrum_lease", {})
        self.handler("monitoring.spectrum_lease", {})
        release = self.handler("monitoring.spectrum_release", {})

        after = len(self.handler("status", {})["diagnostics"]["events"])
        self.assertEqual({"active": True, "lease_seconds": 5.0}, lease)
        self.assertEqual({"active": False}, release)
        self.assertEqual(before, after)
        self.assertEqual(
            [
                ("renew_spectrum_lease", None),
                ("renew_spectrum_lease", None),
                ("release_spectrum_lease", None),
            ],
            [call for call in self.runtime.calls if "spectrum" in call[0]],
        )
        with self.assertRaisesRegex(MediaControlError, "extra keys"):
            self.handler("monitoring.spectrum_lease", {"seconds": 60})

    def test_recording_config_applies_live_and_is_atomic(self) -> None:
        view = self.handler("config.get", {})
        public = view["config"]
        public["recording"]["rotation_seconds"] = 7200
        before = self.config_path.read_bytes()
        self.assertIn(b"0123456789abcdef", before)

        result = self.handler(
            "config.update",
            {
                "config": public,
                "confirm_restart": False,
                "expected_revision": view["revision"],
            },
        )
        self.assertTrue(result["saved"])
        self.assertFalse(result["restart_required"])
        self.assertEqual("apply_live_config", self.runtime.calls[-1][0])
        stored = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(7200, stored["recording"]["rotation_seconds"])
        self.assertEqual("0123456789abcdef", stored["stream"]["passphrase"])
        if os.name == "posix":
            self.assertEqual(0o600, self.config_path.stat().st_mode & 0o777)
        self.assertNotIn("0123456789abcdef", json.dumps(result))

    def test_passphrase_replace_and_clear_are_explicit(self) -> None:
        view = self.handler("config.get", {})
        public = view["config"]
        public["stream"]["passphrase"] = {
            "configured": True,
            "action": "replace",
            "value": "new-0123456789",
        }
        result = self.handler(
            "config.update",
            {
                "config": public,
                "confirm_restart": True,
                "expected_revision": view["revision"],
            },
        )
        self.assertTrue(result["saved"])
        self.assertIn("new-0123456789", self.config_path.read_text(encoding="utf-8"))

        view = self.handler("config.get", {})
        public = view["config"]
        public["stream"]["passphrase"] = {"configured": True, "action": "clear"}
        self.handler(
            "config.update",
            {
                "config": public,
                "confirm_restart": True,
                "expected_revision": view["revision"],
            },
        )
        stored = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertIsNone(stored["stream"]["passphrase"])

    def test_impossible_combination_fails_without_writing(self) -> None:
        before = self.config_path.read_bytes()
        view = self.handler("config.get", {})
        public = view["config"]
        public["capture"]["mode"] = {
            "format": "S24LE",
            "rate_hz": 48_000,
            "channels": 1,
        }
        with self.assertRaisesRegex(MediaControlError, "not validated"):
            self.handler(
                "config.update",
                {
                    "config": public,
                    "confirm_restart": True,
                    "expected_revision": view["revision"],
                },
            )
        self.assertEqual(before, self.config_path.read_bytes())

    def test_diagnostics_are_bounded(self) -> None:
        for _ in range(MAX_DIAGNOSTIC_EVENTS + 20):
            self.handler("status", {})
        events = self.handler("status", {})["diagnostics"]["events"]
        self.assertLessEqual(len(events), 2)
        self.assertEqual("media_state_changed", events[-1]["code"])

    def test_restart_change_requires_confirmation_and_blocks_starts(self) -> None:
        view = self.handler("config.get", {})
        public = view["config"]
        public["stream"]["destination_host"] = "192.0.2.10"
        with self.assertRaisesRegex(MediaControlError, "requires a media-service restart"):
            self.handler(
                "config.update",
                {
                    "config": public,
                    "confirm_restart": False,
                    "expected_revision": view["revision"],
                },
            )
        result = self.handler(
            "config.update",
            {
                "config": public,
                "confirm_restart": True,
                "expected_revision": view["revision"],
            },
        )
        self.assertTrue(result["restart_required"])
        with self.assertRaisesRegex(MediaControlError, "restart the media service"):
            self.handler("stream.start", {})
        self.assertTrue(self.handler("stream.stop", {})["ok"])

    def test_revision_conflict_and_active_recording_fail_before_write(self) -> None:
        view = self.handler("config.get", {})
        public = view["config"]
        public["recording"]["rotation_seconds"] = 90
        with self.assertRaisesRegex(MediaControlError, "reload it before saving"):
            self.handler(
                "config.update",
                {
                    "config": public,
                    "confirm_restart": False,
                    "expected_revision": "stale",
                },
            )
        before = self.config_path.read_bytes()
        self.runtime.recording_state = "running"
        with self.assertRaisesRegex(MediaControlError, "stop recording"):
            self.handler(
                "config.update",
                {
                    "config": public,
                    "confirm_restart": False,
                    "expected_revision": view["revision"],
                },
            )
        self.assertEqual(before, self.config_path.read_bytes())

    def test_concurrent_updates_are_serialized_and_stale_writer_loses(self) -> None:
        from tinypirelay.media_control import write_config_atomic as real_write

        view = self.handler("config.get", {})
        first = json.loads(json.dumps(view["config"]))
        second = json.loads(json.dumps(view["config"]))
        first["recording"]["rotation_seconds"] = 90
        second["recording"]["rotation_seconds"] = 120
        entered = threading.Event()
        release = threading.Event()
        results: list[object] = []

        def slow_write(path: object, config: object) -> str:
            entered.set()
            self.assertTrue(release.wait(2))
            return real_write(path, config)

        def save(value: dict[str, object]) -> None:
            try:
                results.append(
                    self.handler(
                        "config.update",
                        {
                            "config": value,
                            "confirm_restart": False,
                            "expected_revision": view["revision"],
                        },
                    )
                )
            except BaseException as exc:
                results.append(exc)

        with mock.patch("tinypirelay.media_control.write_config_atomic", slow_write):
            first_thread = threading.Thread(target=save, args=(first,))
            second_thread = threading.Thread(target=save, args=(second,))
            first_thread.start()
            self.assertTrue(entered.wait(2))
            second_thread.start()
            release.set()
            first_thread.join(2)
            second_thread.join(2)

        self.assertEqual(2, len(results))
        self.assertEqual(1, sum(isinstance(item, dict) for item in results))
        conflict = next(item for item in results if isinstance(item, MediaControlError))
        self.assertEqual("revision_conflict", conflict.code)
        stored = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(90, stored["recording"]["rotation_seconds"])

    def test_public_passphrase_shape_is_strict(self) -> None:
        public = self.handler("config.get", {})["config"]
        public["stream"]["passphrase"]["extra"] = "secret"
        with self.assertRaisesRegex(MediaControlError, "extra keys"):
            internal_config_value(public, "existing-secret")


if __name__ == "__main__":
    unittest.main()
