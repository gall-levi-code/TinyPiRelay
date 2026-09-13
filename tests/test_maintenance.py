from __future__ import annotations

import contextlib
import copy
import json
import socket
import struct
import subprocess
import unittest
from unittest import mock

from tinypirelay import maintenance


BOOT_ID = "12345678-1234-1234-1234-123456789abc"


class FakeControl:
    def __init__(self, events, *, recording="running", warning=None, stall=False):
        self.events, self.stall = events, stall
        self.state = {
            "capture": {"state": "running"}, "stream": {"state": "running"},
            "recording": {"state": recording, "current_file": "/recordings/test.flac" if recording == "running" else None,
                          "last_finalized_file": None, "warning": warning, "last_error": None, "rotation_pending": False},
        }

    def request(self, action):
        self.events.append(action)
        if action == "status":
            return {"state": copy.deepcopy(self.state)}
        branch = action.removesuffix(".stop")
        if self.state[branch]["state"] == "starting" or (
            action == "capture.stop" and any(
                self.state[key]["state"] in {"starting", "running", "stopping"}
                for key in ("stream", "recording")
            )
        ):
            return {"ok": False, "changed": False, "effects": [],
                    "error": {"code": "branch_active"}, "state": copy.deepcopy(self.state)}
        if action == "recording.stop" and not self.stall:
            self.state["recording"].update(state="stopped", current_file=None, last_finalized_file="/recordings/test.flac")
        if action in {"stream.stop", "capture.stop"} and not self.stall:
            self.state[branch]["state"] = "stopped"
        return {"ok": True, "changed": True, "effects": [action], "error": None,
                "state": copy.deepcopy(self.state)}


class Clock:
    value = 0.0

    def now(self):
        return self.value

    def sleep(self, duration):
        self.value += duration


class Connection:
    def __init__(self, request=b"status\n", uid=996):
        self.request, self.uid, self.response = request, uid, b""

    def getsockopt(self, *_args):
        return struct.pack("3i", 12, self.uid, 994)

    def settimeout(self, _timeout):
        pass

    def recv(self, count):
        part, self.request = self.request[:count], self.request[count:]
        return part

    def sendall(self, payload):
        self.response += payload


class MaintenanceTests(unittest.TestCase):
    def controller(self, **options):
        events = []
        control = FakeControl(events, **options)
        clock = Clock()

        def runner(command):
            events.append(command)
            if command[1] == "show":
                return "ActiveState=inactive\nResult=success\nExecMainCode=1\nExecMainStatus=0\n"
            return ""

        controller = maintenance.MaintenanceController(
            control, BOOT_ID, runner=runner, lifecycle_lock=contextlib.nullcontext,
            clock=clock.now, sleep=clock.sleep, power_wait=lambda: None,
        )
        return controller, control, events

    def test_fixed_actions_finalize_before_systemd_and_reject_busy(self):
        for action, expected in (
            ("media.restart", ("/usr/bin/systemctl", "start", maintenance.MEDIA_UNIT)),
            ("system.reboot", ("/usr/bin/systemctl", "--no-block", "reboot")),
            ("system.shutdown", ("/usr/bin/systemctl", "--no-block", "poweroff")),
        ):
            with self.subTest(action=action):
                controller, _control, events = self.controller()
                accepted = controller.accept(action)
                self.assertTrue(accepted["accepted"])
                self.assertEqual("queued", accepted["phase"])
                with self.assertRaises(maintenance.MaintenanceError) as failure:
                    controller.accept(action)
                self.assertEqual("busy", failure.exception.code)
                controller.perform()
                stop = ("/usr/bin/systemctl", "stop", maintenance.MEDIA_UNIT)
                self.assertLess(events.index("recording.stop"), events.index("stream.stop"))
                self.assertLess(events.index("stream.stop"), events.index("capture.stop"))
                self.assertLess(events.index("capture.stop"), events.index(stop))
                self.assertLess(events.index(stop), events.index(expected))
                self.assertEqual(BOOT_ID, controller.status()["boot_id"])
                self.assertIsNone(controller.status()["error"])
                self.assertEqual(action != "media.restart", controller.status()["busy"])

    def test_rejected_stop_decisions_fail_immediately_without_downstream_actions(self):
        for rejected in ("recording.stop", "stream.stop", "capture.stop"):
            for decision in ({"ok": False}, {"ok": True, "accepted": False}, {}):
                with self.subTest(rejected=rejected, decision=decision):
                    controller, control, events = self.controller()
                    original_request = control.request

                    def request(action):
                        if action == rejected:
                            events.append(action)
                            return decision
                        return original_request(action)

                    control.request = request
                    controller.accept("system.reboot")
                    controller.perform()
                    self.assertEqual("media_rejected", controller.status()["error"]["code"])
                    self.assertEqual(rejected, events[-1])
                    self.assertEqual(0, controller.clock())
                    self.assertFalse(any(isinstance(event, tuple) for event in events))

    def test_stream_must_finish_stopping_before_capture_and_systemd(self):
        controller, control, events = self.controller(recording="stopped")
        original_request = control.request

        def request(action):
            result = original_request(action)
            if action == "stream.stop":
                control.state["stream"]["state"] = "stopping"
            return result

        control.request = request
        controller.accept("system.shutdown")
        controller.perform()
        self.assertEqual("finalization_timeout", controller.status()["error"]["code"])
        self.assertNotIn("capture.stop", events)
        self.assertFalse(any(isinstance(event, tuple) for event in events))

    def test_recording_failures_and_transitions_never_power_or_stop_unit(self):
        cases = ({"warning": {"code": "disk_full"}}, {"recording": "failed"}, {"stall": True})
        for options in cases:
            with self.subTest(options=options):
                controller, _control, events = self.controller(**options)
                controller.accept("system.shutdown")
                controller.perform()
                self.assertEqual("failed", controller.status()["phase"])
                self.assertFalse(any(isinstance(event, tuple) for event in events))
                self.assertFalse(controller.status()["busy"])
        for field, value in (("current_file", None), ("rotation_pending", True)):
            controller, control, events = self.controller()
            control.state["recording"][field] = value
            controller.accept("system.reboot")
            controller.perform()
            self.assertEqual("failed", controller.status()["phase"])
            self.assertFalse(any(isinstance(event, tuple) for event in events))

    def test_failed_finalized_path_and_failed_systemd_exit_fail_closed(self):
        controller, control, events = self.controller(recording="stopped")
        controller.runner = lambda command: events.append(command) or "ActiveState=inactive\nResult=timeout\nExecMainStatus=9\n"
        controller.accept("system.reboot")
        controller.perform()
        self.assertEqual("media_stop_failed", controller.status()["error"]["code"])
        self.assertNotIn(("/usr/bin/systemctl", "--no-block", "reboot"), events)

        controller, control, events = self.controller()
        original_request = control.request

        def missing_finalization(action):
            result = original_request(action)
            result["state"]["recording"]["last_finalized_file"] = None
            return result

        control.request = missing_finalization
        controller.accept("system.reboot")
        controller.perform()
        self.assertEqual("finalization_timeout", controller.status()["error"]["code"])
        self.assertFalse(any(isinstance(event, tuple) for event in events))

    def test_lifecycle_lock_conflict_and_unaccepted_work_have_no_side_effects(self):
        controller, _control, events = self.controller()
        controller.perform()
        self.assertEqual([], events)
        controller.lifecycle_lock = mock.Mock(side_effect=RuntimeError("busy"))
        controller.accept("media.restart")
        controller.perform()
        self.assertEqual([], events)
        self.assertEqual("failed", controller.status()["phase"])

    def test_peer_uid_and_exact_no_argument_protocol(self):
        for payload, uid in ((b"system.shutdown\n", 0), (b"system.shutdown\n", 995),
                             (b"system.reboot --force\n", 996), (b"status\nmedia.restart\n", 996),
                             (b"status", 996), (b"x" * 65, 996), (b"\xff\n", 996)):
            with self.subTest(payload=payload, uid=uid):
                controller, _control, events = self.controller()
                connection = Connection(payload, uid)
                with mock.patch.object(socket, "SO_PEERCRED", 17, create=True):
                    self.assertFalse(maintenance.handle_connection(connection, controller, 996))
                self.assertFalse(json.loads(connection.response)["ok"])
                self.assertEqual([], events)
        controller, _control, events = self.controller()
        connection = Connection(b"media.restart\n")
        with mock.patch.object(socket, "SO_PEERCRED", 17, create=True):
            self.assertTrue(maintenance.handle_connection(connection, controller, 996))
        self.assertTrue(json.loads(connection.response)["status"]["accepted"])
        self.assertEqual([], events)  # Dispatch only after responding.

    def test_subprocess_is_bounded_without_shell_or_environment_inheritance(self):
        command = ("/usr/bin/systemctl", "stop", maintenance.MEDIA_UNIT)
        with mock.patch.object(maintenance.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="")) as run:
            maintenance._run(command)
        self.assertEqual(command, run.call_args.args[0])
        self.assertFalse(run.call_args.kwargs.get("shell", False))
        self.assertEqual(25, run.call_args.kwargs["timeout"])
        self.assertEqual({"PATH", "LANG"}, set(run.call_args.kwargs["env"]))
        with mock.patch.object(maintenance.subprocess, "run", side_effect=subprocess.TimeoutExpired(command, 25)):
            with self.assertRaises(maintenance.MaintenanceError):
                maintenance._run(command)


if __name__ == "__main__":
    unittest.main()
