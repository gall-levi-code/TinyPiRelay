"""Socket-activated, fixed-action maintenance boundary; never accepts commands or paths.

HTTP authentication/reconfirmation belongs to the web owner. This root helper
independently verifies its Unix peer and permits only the three actions below.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import threading
import time
import uuid


SOCKET_PATH = Path("/run/tinypirelay-maintenance/control.sock")
CONTROL_PATH = Path("/run/tinypirelay/control.sock")
MEDIA_UNIT = "tinypirelay-media.service"
ACTIONS = frozenset(("media.restart", "system.reboot", "system.shutdown"))
IO_TIMEOUT_SECONDS = 2.0
FINALIZE_TIMEOUT_SECONDS = 20.0
COMMAND_TIMEOUT_SECONDS = 25.0
MAX_MESSAGE_BYTES = 4096


class MaintenanceError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _read_message(connection: socket.socket, limit: int) -> bytes:
    deadline = time.monotonic() + IO_TIMEOUT_SECONDS
    chunks = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MaintenanceError("unavailable", "Maintenance connection timed out.")
        connection.settimeout(remaining)
        chunk = connection.recv(min(1024, limit + 1 - len(chunks)))
        if not chunk:
            return bytes(chunks)
        chunks.extend(chunk)
        if len(chunks) > limit:
            raise MaintenanceError("invalid_request", "Maintenance message is too large.")


class MaintenanceClient:
    def __init__(self, path: Path = SOCKET_PATH) -> None:
        self.path = path

    def request(self, action: str) -> dict[str, object]:
        if action not in ACTIONS | {"status"}:
            raise MaintenanceError("invalid_action", "Maintenance action is not allowed.")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(IO_TIMEOUT_SECONDS)
                connection.connect(str(self.path))
                connection.sendall((action + "\n").encode("ascii"))
                connection.shutdown(socket.SHUT_WR)
                payload = _read_message(connection, MAX_MESSAGE_BYTES)
            response = json.loads(payload)
            if not isinstance(response, dict) or type(response.get("ok")) is not bool:
                raise ValueError
            if response["ok"] is not True:
                error = response.get("error", {})
                if not isinstance(error, dict) or not all(isinstance(error.get(key), str) for key in ("code", "message")):
                    raise ValueError
                raise MaintenanceError(error["code"], error["message"])
            status = response.get("status")
            if not isinstance(status, dict) or status.get("available") is not True or type(status.get("busy")) is not bool:
                raise ValueError
            uuid.UUID(status["boot_id"])
            return status
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise MaintenanceError("unavailable", "Maintenance service is unavailable.") from None


def _run(command: tuple[str, ...]) -> str:
    try:
        result = subprocess.run(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
            timeout=COMMAND_TIMEOUT_SECONDS, check=False,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        raise MaintenanceError("command_failed", "Maintenance command did not complete safely.") from None
    if result.returncode != 0 or len(result.stdout) > MAX_MESSAGE_BYTES:
        raise MaintenanceError("command_failed", "Maintenance command did not complete safely.")
    return result.stdout


def _lifecycle_lock():
    # Reuse the installer's exact root-owned lock, including symlink/owner checks.
    from .installer import LocalHost
    return LocalHost().installer_lock()


class MaintenanceController:
    def __init__(self, control, boot_id: str, *, runner=_run, lifecycle_lock=_lifecycle_lock,
                 clock=time.monotonic, sleep=time.sleep, power_wait=None) -> None:
        self.control = control
        self.runner = runner
        self.lifecycle_lock = lifecycle_lock
        self.clock, self.sleep = clock, sleep
        self.power_wait = power_wait or threading.Event().wait
        self.lock = threading.Lock()
        self.state = {
            "available": True, "busy": False, "action": None, "phase": "idle",
            "operation_id": None, "boot_id": str(uuid.UUID(boot_id)), "error": None,
        }

    def status(self) -> dict[str, object]:
        with self.lock:
            return dict(self.state)

    def accept(self, action: str) -> dict[str, object]:
        if action not in ACTIONS:
            raise MaintenanceError("invalid_action", "Maintenance action is not allowed.")
        with self.lock:
            if self.state["busy"]:
                raise MaintenanceError("busy", "Another maintenance action is in progress.")
            self.state.update(busy=True, action=action, phase="queued", operation_id=str(uuid.uuid4()), error=None)
            return {**self.state, "accepted": True}

    def _phase(self, phase: str) -> None:
        with self.lock:
            self.state["phase"] = phase

    def _media(self) -> dict[str, object]:
        from .control_protocol import ControlError
        try:
            status = self.control.request("status")
            state = status.get("state") if isinstance(status, dict) else None
            if not isinstance(state, dict) or not all(isinstance(state.get(key), dict) for key in ("capture", "stream", "recording")):
                raise ValueError
            return state
        except (ControlError, ValueError, TypeError):
            raise MaintenanceError("media_unavailable", "Cannot verify media state; maintenance was stopped.") from None

    def _wait(self, predicate) -> dict[str, object]:
        deadline = self.clock() + FINALIZE_TIMEOUT_SECONDS
        while True:
            state = self._media()
            recording = state["recording"]
            if recording.get("last_error") or recording.get("warning") or recording.get("state") in {"failed", "blocked"}:
                raise MaintenanceError("finalization_failed", "Recording finalization could not be verified; maintenance was stopped.")
            if predicate(state):
                return state
            if self.clock() >= deadline:
                raise MaintenanceError("finalization_timeout", "Media did not stop safely before the timeout.")
            self.sleep(0.1)

    def _stop_branch(self, action: str) -> None:
        decision = self.control.request(action)
        if not isinstance(decision, dict) or decision.get("ok") is not True or decision.get("accepted") is False:
            raise MaintenanceError("media_rejected", "Media rejected the stop request; maintenance was stopped.")

    def _stop_media(self) -> None:
        self._phase("finalizing")
        initial = self._media()
        recording = initial["recording"]
        if recording.get("state") not in {"stopped", "running"} or recording.get("rotation_pending"):
            raise MaintenanceError("media_busy", "Wait for the current recording transition before maintenance.")
        expected_file = recording.get("current_file") if recording["state"] == "running" else None
        if recording["state"] == "running":
            if not isinstance(expected_file, str) or not expected_file:
                raise MaintenanceError("finalization_failed", "The active recording could not be identified.")
            self._stop_branch("recording.stop")
        self._wait(lambda state: (
            state["recording"].get("state") == "stopped"
            and (expected_file is None or state["recording"].get("last_finalized_file") == expected_file)
        ))
        self._phase("stopping")
        self._stop_branch("stream.stop")
        self._wait(lambda state: state["stream"].get("state") == "stopped")
        self._stop_branch("capture.stop")
        self._wait(lambda state: all(state[key].get("state") == "stopped" for key in ("capture", "stream", "recording")))
        self.runner(("/usr/bin/systemctl", "stop", MEDIA_UNIT))
        fields = dict(line.split("=", 1) for line in self.runner((
            "/usr/bin/systemctl", "show", MEDIA_UNIT,
            "--property=ActiveState,Result,ExecMainCode,ExecMainStatus",
        )).splitlines() if "=" in line)
        if fields != {"ActiveState": "inactive", "Result": "success", "ExecMainCode": "1", "ExecMainStatus": "0"}:
            raise MaintenanceError("media_stop_failed", "The media service did not exit cleanly; maintenance was stopped.")

    def perform(self) -> None:
        """Run only after the accepted response has been sent; one worker at a time."""
        pending = self.status()
        action = pending["action"]
        if not pending["busy"] or pending["phase"] != "queued" or action not in ACTIONS:
            return
        try:
            with self.lifecycle_lock():
                self._stop_media()
                self._phase("restarting")
                if action == "media.restart":
                    self.runner(("/usr/bin/systemctl", "start", MEDIA_UNIT))
                    self.runner(("/usr/bin/systemctl", "is-active", "--quiet", MEDIA_UNIT))
                    self._phase("complete")
                elif action in {"system.reboot", "system.shutdown"}:
                    power_command = "reboot" if action == "system.reboot" else "poweroff"
                    self.runner(("/usr/bin/systemctl", "--no-block", power_command))
                    # Keep the lifecycle lock and busy status until systemd stops us.
                    # Waiting costs no CPU; status requests stay available. An operator
                    # must inspect a machine that never leaves this pending state.
                    self.power_wait()
                    return
                else:
                    raise MaintenanceError("invalid_action", "Maintenance action is not allowed.")
        except Exception as exc:
            error = exc if isinstance(exc, MaintenanceError) else MaintenanceError(
                "maintenance_failed", "Maintenance could not complete safely; inspect service diagnostics.")
            with self.lock:
                self.state.update(phase="failed", error={"code": error.code, "message": error.message})
        with self.lock:
            self.state["busy"] = False


def handle_connection(connection: socket.socket, controller: MaintenanceController, web_uid: int) -> bool:
    """Return whether an accepted action needs dispatch after the response."""
    accepted = False
    try:
        credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, peer_uid, _gid = struct.unpack("3i", credentials)
        if peer_uid != web_uid:
            raise MaintenanceError("forbidden", "Maintenance peer is not authorized.")
        payload = _read_message(connection, 64)
        action = payload.decode("ascii").removesuffix("\n")
        if payload != (action + "\n").encode("ascii") or action not in ACTIONS | {"status"}:
            raise MaintenanceError("invalid_action", "Maintenance action is not allowed.")
        status = controller.status() if action == "status" else controller.accept(action)
        accepted = action != "status"
        response = {"ok": True, "status": status}
    except MaintenanceError as exc:
        response = {"ok": False, "error": {"code": exc.code, "message": exc.message}}
    except (OSError, ValueError, UnicodeError):
        response = {"ok": False, "error": {"code": "invalid_request", "message": "Invalid maintenance request."}}
    try:
        connection.settimeout(IO_TIMEOUT_SECONDS)
        connection.sendall((json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8"))
    except OSError:
        pass  # Accepted work remains identifiable through status after a lost response.
    return accepted


def main() -> int:
    import pwd
    from .control_protocol import ControlClient
    if os.geteuid() != 0 or os.environ.get("LISTEN_PID") != str(os.getpid()) or os.environ.get("LISTEN_FDS") != "1":
        raise SystemExit("maintenance requires root and exactly one systemd-activated socket")
    web_uid = pwd.getpwnam("tinypirelay-web").pw_uid
    if web_uid <= 0:
        raise SystemExit("invalid maintenance peer identity")
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    controller = MaintenanceController(ControlClient(CONTROL_PATH, timeout=2), boot_id)
    with socket.socket(fileno=3) as listener:
        if listener.family != socket.AF_UNIX or listener.type != socket.SOCK_STREAM or listener.getsockname() != str(SOCKET_PATH):
            raise SystemExit("invalid systemd maintenance socket")
        while True:
            connection, _address = listener.accept()
            with connection:
                accepted = handle_connection(connection, controller, web_uid)
            if accepted:
                threading.Thread(target=controller.perform, name="maintenance-action", daemon=True).start()


if __name__ == "__main__":
    raise SystemExit(main())
