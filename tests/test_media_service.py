from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
import threading
import unittest
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tinypirelay.media_service import (  # noqa: E402
    DeviceTelemetryWorker,
    GLibScheduler,
    _validated_inputs,
    config_revision,
    main,
    run_service,
)
from tinypirelay.gstreamer_engine import EngineEvent  # noqa: E402
from tinypirelay.control_protocol import ControlUnavailable  # noqa: E402
from tinypirelay.media_config import CaptureMode, ConfigError, load_config  # noqa: E402
from tinypirelay.audio_capabilities import load_audio_capabilities  # noqa: E402
from tinypirelay.compatibility import load_evidence, validate_evidence  # noqa: E402


class _Source:
    def __init__(self, glib=None) -> None:
        self.glib = glib
        self.callback = None
        self.attached = None
        self.destroyed = False

    def set_callback(self, callback):
        self.callback = callback

    def attach(self, context):
        self.attached = context
        if self.glib is not None:
            self.glib.pending.append(self)

    def destroy(self):
        self.destroyed = True


class _Context:
    @staticmethod
    def default():
        return "owner"


class _Loop:
    def __init__(self, glib) -> None:
        self.glib = glib
        self.stopped = False

    def run(self):
        steps = 0
        while not self.stopped and self.glib.pending:
            source = self.glib.pending.pop(0)
            if not source.destroyed:
                source.callback(None)
            steps += 1
            if steps > 50:
                raise AssertionError("fixture GLib loop did not settle")
        if not self.stopped and self.glib.when_idle is not None:
            callback, self.glib.when_idle = self.glib.when_idle, None
            callback()
            self.run()

    def quit(self):
        self.stopped = True


class _MainLoopFactory:
    def __init__(self, glib) -> None:
        self.glib = glib

    def new(self, _context, _running):
        self.glib.loop = _Loop(self.glib)
        return self.glib.loop


class _GLib:
    def __init__(self) -> None:
        self.milliseconds = None
        self.source = _Source()
        self.pending = []
        self.loop = None
        self.when_idle = None
        self.MainContext = _Context
        self.MainLoop = _MainLoopFactory(self)

    def timeout_source_new(self, milliseconds):
        self.milliseconds = milliseconds
        return self.source

    def timeout_source_new_seconds(self, _seconds):
        return _Source(self)

    def idle_source_new(self):
        return _Source(self)


class _FailingEngine:
    def __init__(self, _config, on_event, **_kwargs):
        self.on_event = on_event

    def bind_owner(self):
        pass

    def invoke(self, callback, *args, **kwargs):
        future = Future()
        try:
            future.set_result(callback(*args, **kwargs))
        except BaseException as exc:
            future.set_exception(exc)
        return future

    def start_capture(self):
        raise RuntimeError("fixture capture start failure")

    def shutdown(self):
        self.on_event(EngineEvent("shutdown-complete", "capture", 1, {}))


class _ControlServer:
    def __init__(self, path, handler, **kwargs):
        self.path = path
        self.handler = handler
        self.kwargs = kwargs
        self.started = False
        self.closed = False

    def start(self):
        self.started = True
        return self

    def close(self):
        self.closed = True


class MediaServiceTests(unittest.TestCase):
    def test_device_worker_samples_off_thread_and_serves_one_copied_cache(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        published = threading.Event()
        runtime = mock.Mock()
        runtime.device_telemetry_context.return_value = ("active-config", "active-revision", {})
        collector = mock.Mock()
        sampler_threads = []

        def sample(config, recording):
            sampler_threads.append(threading.get_ident())
            self.assertEqual("active-config", config)
            self.assertEqual({}, recording)
            entered.set()
            if not release.wait(2):
                raise RuntimeError("fixture sampler was not released")
            return {"system": {"cpu_percent": 25.0}}

        collector.sample.side_effect = sample
        with mock.patch("tinypirelay.media_service.DeviceTelemetryCollector", return_value=collector):
            worker = DeviceTelemetryWorker(runtime)
        native_wait = worker._stop.wait

        def await_next_sample(seconds):
            published.set()
            return native_wait(seconds)

        with mock.patch.object(worker._stop, "wait", side_effect=await_next_sample):
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertIsNone(worker.snapshot())  # no lock held by storage I/O
                release.set()
                self.assertTrue(published.wait(1))
                first = worker.snapshot()
                self.assertEqual("active-revision", first["config_revision"])
                self.assertEqual(1, first["sequence"])
                first["system"]["cpu_percent"] = 99.0
                for _ in range(20):
                    self.assertEqual(25.0, worker.snapshot()["system"]["cpu_percent"])
                self.assertEqual(1, collector.sample.call_count)
                self.assertNotEqual(threading.get_ident(), sampler_threads[0])
            finally:
                release.set()
                worker.close()
        self.assertFalse(worker._thread.is_alive())
        self.assertIsNone(worker.snapshot())

    def test_device_worker_failure_clears_metrics_without_disclosing_private_errors(self) -> None:
        runtime = mock.Mock()
        runtime.device_telemetry_context.return_value = (object(), "active", {})
        published = threading.Event()
        with mock.patch("tinypirelay.media_service.DeviceTelemetryCollector") as factory:
            factory.return_value.sample.side_effect = OSError("/private/path secret")
            worker = DeviceTelemetryWorker(runtime)
        native_wait = worker._stop.wait

        def await_next_sample(seconds):
            published.set()
            return native_wait(seconds)

        with mock.patch.object(worker._stop, "wait", side_effect=await_next_sample):
            with self.assertLogs("tinypirelay.media", level="WARNING") as logs:
                worker.start()
                try:
                    self.assertTrue(published.wait(1))
                    self.assertIsNone(worker.snapshot())
                finally:
                    worker.close()
            self.assertNotIn("secret", " ".join(logs.output))
            self.assertNotIn("/private/path", " ".join(logs.output))

    def test_glib_scheduler_is_one_shot_and_cancellable(self) -> None:
        glib = _GLib()
        called = []
        timer = GLibScheduler(glib, "owner").call_later(1.25, lambda: called.append(1))

        self.assertEqual(1250, glib.milliseconds)
        self.assertEqual("owner", glib.source.attached)
        self.assertFalse(glib.source.callback(None))
        self.assertEqual([1], called)
        timer.cancel()
        self.assertTrue(glib.source.destroyed)

    def test_config_revision_is_exact_and_reducer_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_bytes(b"one")
            first = config_revision(path)
            path.write_bytes(b"two")
            second = config_revision(path)

        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertNotEqual(first, second)

    def test_validated_inputs_use_live_capability_gate(self) -> None:
        value = json.loads((ROOT / "config/example.json").read_text(encoding="utf-8"))
        value["capture"] = {
            "device_id": "hw:CARD=iMM6C,DEV=0",
            "mode": {"format": "S16LE", "rate_hz": 48000, "channels": 1},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            config, revision = _validated_inputs(
                path, ROOT / "evidence/stream_compatibility.json"
            )

        self.assertEqual("hw:CARD=iMM6C,DEV=0", config.capture.device_id)
        self.assertRegex(revision, r"^[0-9a-f]{64}$")

    def test_validated_inputs_reject_non_regular_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ConfigError, "regular file"):
                _validated_inputs(
                    Path(directory), ROOT / "evidence/stream_compatibility.json"
                )

    @unittest.skipUnless(os.name == "posix", "POSIX config permissions")
    def test_passphrase_configuration_requires_private_permissions(self) -> None:
        value = json.loads((ROOT / "config/example.json").read_text(encoding="utf-8"))
        value["capture"] = {
            "device_id": "hw:CARD=iMM6C,DEV=0",
            "mode": {"format": "S16LE", "rate_hz": 48000, "channels": 1},
        }
        value["stream"].update(
            {
                "enabled": True,
                "representation_id": "opus_128k_48000_stereo_matroska",
                "destination_host": "127.0.0.1",
                "destination_port": 9000,
                "passphrase": "private-passphrase",
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            path.chmod(0o644)
            with self.assertRaisesRegex(ConfigError, "group or other") as raised:
                _validated_inputs(path, ROOT / "evidence/stream_compatibility.json")
            self.assertNotIn("private-passphrase", str(raised.exception))

            path.chmod(0o600)
            config, _revision = _validated_inputs(
                path, ROOT / "evidence/stream_compatibility.json"
            )
            self.assertIsNotNone(config.stream.passphrase)

    @unittest.skipUnless(os.name == "posix", "POSIX symlink semantics")
    def test_validated_inputs_never_follows_a_config_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target.json"
            target.write_bytes((ROOT / "config/example.json").read_bytes())
            link = Path(directory) / "config.json"
            link.symlink_to(target)
            with self.assertRaisesRegex(ConfigError, "symlink"):
                _validated_inputs(
                    link, ROOT / "evidence/stream_compatibility.json"
                )

    def test_unconfigured_capture_passes_startup_validation(self) -> None:
        config, revision = _validated_inputs(
            ROOT / "config/example.json", ROOT / "evidence/stream_compatibility.json"
        )
        self.assertIsNone(config.capture.mode)
        self.assertIsNone(config.capture.device_id)
        self.assertRegex(revision, r"^[0-9a-f]{64}$")

    def test_control_socket_startup_failure_is_a_stable_cli_error(self) -> None:
        with mock.patch(
            "tinypirelay.media_service.load_evidence", return_value={}
        ), mock.patch(
            "tinypirelay.media_service.validate_evidence", return_value=("approved",)
        ), mock.patch(
            "tinypirelay.media_service._validated_inputs",
            return_value=(object(), "a" * 64),
        ), mock.patch(
            "tinypirelay.media_service.load_audio_capabilities", return_value=object()
        ), mock.patch(
            "tinypirelay.media_service.run_service",
            side_effect=ControlUnavailable("socket unavailable"),
        ):
            exit_code = main(
                [
                    "--config",
                    str(ROOT / "config/example.json"),
                    "--control-socket",
                    "control.sock",
                ]
            )
        self.assertEqual(2, exit_code)

    def test_synchronous_backend_start_failure_exits_instead_of_idling(self) -> None:
        config = load_config(
            ROOT / "config/example.json",
            {
                "pcm_s16le_48000_stereo_matroska",
                "flac_48000_stereo_matroska",
                "opus_128k_48000_stereo_matroska",
            },
        )
        config = replace(
            config,
            capture=replace(
                config.capture,
                device_id="fixture",
                mode=CaptureMode("S16LE", 48000, 1),
            ),
        )
        glib = _GLib()

        result = run_service(
            config,
            "a" * 64,
            gst_loader=lambda: (object(), glib),
            engine_factory=_FailingEngine,
        )

        self.assertEqual(1, result)
        self.assertTrue(glib.loop.stopped)

    def test_control_socket_survives_capture_failure_until_orderly_shutdown(self) -> None:
        approved = validate_evidence(
            load_evidence(ROOT / "evidence/stream_compatibility.json")
        )
        config = load_config(
            ROOT / "config/example.json",
            approved,
        )
        config = replace(
            config,
            capture=replace(
                config.capture,
                device_id="hw:CARD=iMM6C,DEV=0",
                mode=CaptureMode("S16LE", 48000, 1),
            ),
        )
        capabilities = load_audio_capabilities(
            ROOT / "evidence/audio_capabilities.json", approved
        )
        glib = _GLib()
        servers = []
        snapshots = []

        class ReconnectedEngine(_FailingEngine):
            attempts = 0

            def start_capture(self):
                self.attempts += 1
                if self.attempts == 1:
                    self.on_event(EngineEvent("error", "capture", 1, {"code": "start_failed", "message": "microphone absent"}))
                    raise RuntimeError("microphone absent")
                self.on_event(EngineEvent("capture-started", "capture", 2, {}))

            def stop_capture(self):
                self.on_event(EngineEvent("capture-stopped", "capture", 2, {}))

        def factory(*args, **kwargs):
            server = _ControlServer(*args, **kwargs)
            servers.append(server)
            return server

        def inspect_then_shutdown():
            snapshots.append(servers[0].handler("status", {}))
            self.assertFalse(servers[0].closed)
            self.assertTrue(servers[0].handler("capture.start", {})["ok"])
            self.assertEqual("running", servers[0].handler("status", {})["state"]["capture"]["state"])
            # The installed service waits for an operator; it does not retry
            # a missing microphone in an automatic restart loop.
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

        glib.when_idle = inspect_then_shutdown

        result = run_service(
            config,
            "a" * 64,
            config_path=ROOT / "config/example.json",
            approved_representations=approved,
            audio_capabilities=capabilities,
            control_socket=Path("control.sock"),
            gst_loader=lambda: (object(), glib),
            engine_factory=ReconnectedEngine,
            control_server_factory=factory,
        )

        self.assertEqual(0, result)
        self.assertEqual("running", snapshots[0]["state"]["service"])
        self.assertEqual("failed", snapshots[0]["state"]["capture"]["state"])
        self.assertFalse(snapshots[0]["audio_setup_required"])
        self.assertTrue(servers[0].started)
        self.assertTrue(servers[0].closed)
        self.assertEqual(0o750, servers[0].kwargs["directory_mode"])
        self.assertEqual(0o660, servers[0].kwargs["socket_mode"])

    def test_unconfigured_service_answers_control_without_starting_capture(self) -> None:
        approved = validate_evidence(load_evidence(ROOT / "evidence/stream_compatibility.json"))
        config = load_config(ROOT / "config/example.json", approved)
        capabilities = load_audio_capabilities(ROOT / "evidence/audio_capabilities.json", approved)
        glib = _GLib()
        servers = []
        snapshots = []
        engine = None

        def engine_factory(*args, **kwargs):
            nonlocal engine
            engine = _FailingEngine(*args, **kwargs)
            engine.start_capture = mock.Mock(side_effect=AssertionError("must stay idle"))
            return engine

        def server_factory(*args, **kwargs):
            server = _ControlServer(*args, **kwargs)
            servers.append(server)
            return server

        def inspect_then_shutdown():
            snapshots.append(servers[0].handler("status", {}))
            result = servers[0].handler("capture.start", {})
            self.assertEqual("audio_setup_required", result["error"]["code"])
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

        glib.when_idle = inspect_then_shutdown
        result = run_service(
            config, "a" * 64, config_path=ROOT / "config/example.json",
            approved_representations=approved, audio_capabilities=capabilities,
            control_socket=Path("control.sock"), gst_loader=lambda: (object(), glib),
            engine_factory=engine_factory, control_server_factory=server_factory,
        )
        self.assertEqual(0, result)
        self.assertTrue(snapshots[0]["audio_setup_required"])
        self.assertEqual("running", snapshots[0]["state"]["service"])
        self.assertEqual("stopped", snapshots[0]["state"]["capture"]["state"])
        engine.start_capture.assert_not_called()


if __name__ == "__main__":
    unittest.main()
