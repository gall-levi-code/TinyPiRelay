from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import tinypirelay.cli as cli
import tinypirelay.web_service as web_service
from tinypirelay.web_security import (
    MIN_NEW_PASSWORD_CHARACTERS,
    SecurityError,
    validate_new_password,
)


ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def test_lan_url_and_invalid_access_are_reported_without_a_tunnel(self) -> None:
        with mock.patch.object(cli, "read_web_access", return_value="lan"), mock.patch.object(
            cli.socket, "gethostname", return_value="tinypirelay-garden"
        ):
            report = cli._web_access_report()
        self.assertEqual("http://tinypirelay-garden.local/", report["web_url"])
        self.assertIsNone(report["ssh_tunnel_hint"])
        with mock.patch.object(cli, "read_web_access", side_effect=cli.InstallError("bad")):
            self.assertEqual("unavailable", cli._web_access_report()["web_access"])

    def test_diagnostics_allows_pending_account_and_missing_audio(self) -> None:
        control = mock.Mock()
        control.request.return_value = {"audio_setup_required": True}
        probe = {"status": "missing", "audio": {"status": "missing"}, **{
            key: {"status": "present"} for key in ("platform", "hardware", "storage", "packages", "gstreamer")}}
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "setup-pending"
            marker.write_bytes(b"1\n")
            with mock.patch.object(cli, "SETUP_PENDING_PATH", marker), mock.patch.object(
                cli, "CREDENTIAL_PATH", Path(directory) / "credential.json"
            ), mock.patch.object(cli, "service_status", return_value={"healthy": True, "services": {}}), mock.patch.object(
                cli, "collect_probe", return_value=probe
            ), mock.patch.object(cli, "_configuration_report", return_value={"valid": True}), mock.patch.object(
                cli, "_path_report", side_effect=lambda path, *_args, **_kwargs: {"safe": path != cli.CREDENTIAL_PATH}
            ), mock.patch.object(cli, "ControlClient", return_value=control):
                report = cli.diagnostic_report()
        self.assertTrue(report["healthy"])
        self.assertTrue(report["account_setup_required"])
        self.assertTrue(report["audio_setup_required"])

    def test_status_reports_both_services_without_mutating(self) -> None:
        output = "\n".join(
            (
                "LoadState=loaded",
                "ActiveState=active",
                "SubState=running",
                "UnitFileState=enabled",
            )
        )
        completed = subprocess.CompletedProcess((), 0, output, "")
        stdout = io.StringIO()
        with mock.patch.object(cli, "_run_capture", return_value=completed) as run:
            with redirect_stdout(stdout):
                result = cli.main(("status",))

        self.assertEqual(0, result)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["healthy"])
        self.assertEqual("http://127.0.0.1:8080/", payload["web_url"])
        self.assertIn("ssh -L 8080", payload["ssh_tunnel_hint"])
        self.assertEqual({"media", "web"}, set(payload["services"]))
        self.assertEqual(2, run.call_count)
        self.assertTrue(all("show" in call.args[0] for call in run.call_args_list))

    def test_password_argv_is_rejected_without_echoing_secret(self) -> None:
        stderr = io.StringIO()
        secret = "must-not-appear"
        with redirect_stderr(stderr), mock.patch.object(cli, "_passwd") as passwd:
            result = cli.main(("passwd", "--password", secret))

        self.assertEqual(2, result)
        self.assertNotIn(secret, stderr.getvalue())
        passwd.assert_not_called()

    def test_passwd_runs_as_web_user_then_restarts_only_web(self) -> None:
        credential = subprocess.CompletedProcess((), 0)
        restarted = subprocess.CompletedProcess((), 0, "", "")
        stdout = io.StringIO()
        terminal = io.StringIO()
        with mock.patch.object(cli, "_is_root", return_value=True), mock.patch.object(
            cli, "open_controlling_tty", return_value=terminal
        ), mock.patch.object(
            cli.subprocess, "run", side_effect=(credential, restarted)
        ) as run, redirect_stdout(stdout):
            result = cli.main(("passwd", "--username", "operator"))

        self.assertEqual(0, result)
        credential_argv = run.call_args_list[0].args[0]
        self.assertIn(cli.WEB_USER, credential_argv)
        self.assertIn("tinypirelay.web_service", credential_argv)
        self.assertNotIn("--password", credential_argv)
        self.assertIn("--tty-stdin", credential_argv)
        self.assertIs(terminal, run.call_args_list[0].kwargs["stdin"])
        self.assertIs(terminal, run.call_args_list[0].kwargs["stdout"])
        self.assertIs(terminal, run.call_args_list[0].kwargs["stderr"])
        self.assertTrue(terminal.closed)
        restart_argv = run.call_args_list[1].args[0]
        self.assertEqual(
            ("systemctl", "restart", cli.WEB_UNIT), tuple(restart_argv)
        )
        self.assertNotIn(cli.MEDIA_UNIT, restart_argv)
        self.assertEqual(
            cli.RESTART_TIMEOUT_SECONDS, run.call_args_list[1].kwargs["timeout"]
        )

    def test_passwd_reports_committed_credential_when_restart_fails(self) -> None:
        completed = (
            subprocess.CompletedProcess((), 0),
            subprocess.CompletedProcess((), 1, "", "failed"),
        )
        stderr = io.StringIO()
        terminal = io.StringIO()
        with mock.patch.object(cli, "_is_root", return_value=True), mock.patch.object(
            cli, "open_controlling_tty", return_value=terminal
        ), mock.patch.object(
            cli.subprocess, "run", side_effect=completed
        ), redirect_stderr(stderr):
            result = cli.main(("passwd", "--username", "operator"))

        self.assertEqual(1, result)
        self.assertIn("credential was committed", stderr.getvalue())

    def test_restart_is_explicit_and_root_only(self) -> None:
        stderr = io.StringIO()
        with mock.patch.object(cli, "_is_root", return_value=False), mock.patch.object(
            cli, "_run_capture"
        ) as run, redirect_stderr(stderr):
            result = cli.main(("restart", "all"))
        self.assertEqual(2, result)
        self.assertIn("requires sudo", stderr.getvalue())
        run.assert_not_called()

    def test_diagnostics_redacts_media_secrets(self) -> None:
        control = mock.Mock()
        control.request.return_value = {
            "state": {"service": "running"},
            "passphrase": "must-not-escape",
            "session_token": "also-secret",
        }
        path_report = {"safe": True}
        stdout = io.StringIO()
        with mock.patch.object(
            cli, "service_status", return_value={"healthy": True, "services": {}}
        ), mock.patch.object(
            cli, "collect_probe", return_value={"status": "present"}
        ), mock.patch.object(
            cli, "_configuration_report", return_value={"valid": True}
        ), mock.patch.object(
            cli, "_path_report", return_value=path_report
        ), mock.patch.object(
            cli, "ControlClient", return_value=control
        ), redirect_stdout(stdout):
            result = cli.main(("diagnostics",))

        self.assertEqual(0, result)
        rendered = stdout.getvalue()
        self.assertNotIn("must-not-escape", rendered)
        self.assertNotIn("also-secret", rendered)
        self.assertIn("[REDACTED]", rendered)
        payload = json.loads(rendered)
        self.assertTrue(
            all("-n 200 --no-pager" in command for command in payload["log_commands"])
        )

    def test_path_safety_includes_service_ownership(self) -> None:
        fake_pwd = SimpleNamespace(
            getpwuid=lambda _uid: SimpleNamespace(pw_name="wrong-user")
        )
        fake_grp = SimpleNamespace(
            getgrgid=lambda _gid: SimpleNamespace(gr_name="tinypirelay-control")
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "control.sock"
            path.write_bytes(b"")
            path.chmod(0o600)
            with mock.patch.dict(sys.modules, {"pwd": fake_pwd, "grp": fake_grp}):
                report = cli._path_report(  # noqa: SLF001
                    path,
                    0o600,
                    "file",
                    expected_owner="tinypirelay-media",
                    expected_group="tinypirelay-control",
                )
        self.assertFalse(report["safe"])
        self.assertEqual("wrong-user", report["owner"])
        self.assertEqual("tinypirelay-media", report["expected_owner"])

    def test_configuration_validation_uses_runtime_capability_gate(self) -> None:
        config = {
            "schema_version": 1,
            "capture": {
                "device_id": "hw:CARD=iMM6C,DEV=0",
                "mode": {"format": "S16LE", "rate_hz": 48000, "channels": 1},
            },
            "stream": {
                "enabled": False,
                "representation_id": "opus_128k_48000_stereo_matroska",
                "destination_host": "192.0.2.1",
                "destination_port": 4001,
                "latency_ms": 125,
                "stream_id": None,
                "passphrase": None,
                "opus_bitrate_bps": 128000,
            },
            "recording": {
                "directory": "/var/lib/tinypirelay/recordings",
                "rotation_seconds": 3600,
                "required_mountpoint": "/",
            },
            "monitoring": {
                "spectrum_updates_per_second": 5,
                "spectrum_bands": 512,
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = cli.main(
                    (
                        "config",
                        "validate",
                        "--config",
                        str(path),
                        "--evidence",
                        str(ROOT / "evidence/stream_compatibility.json"),
                        "--audio-capabilities",
                        str(ROOT / "evidence/audio_capabilities.json"),
                    )
                )
        self.assertEqual(0, result)
        self.assertTrue(json.loads(stdout.getvalue())["valid"])

    def test_upgrade_is_report_only_and_uninstall_defaults_to_plan(self) -> None:
        stdout = io.StringIO()
        with mock.patch.object(cli.subprocess, "run") as run, redirect_stdout(stdout):
            self.assertEqual(0, cli.main(("upgrade",)))
        run.assert_not_called()
        self.assertIn("pinned release artifact", stdout.getvalue())

        completed = subprocess.CompletedProcess((), 0)
        with mock.patch.object(cli, "_is_root", return_value=True), mock.patch.object(
            cli.subprocess, "run", return_value=completed
        ) as run:
            self.assertEqual(0, cli.main(("uninstall",)))
        argv = run.call_args.args[0]
        self.assertEqual([str(cli.INSTALLER_PATH), "uninstall"], argv)
        self.assertNotIn("--apply", argv)
        self.assertNotIn("--remove-data", argv)

        stderr = io.StringIO()
        with mock.patch.object(cli, "_is_root", return_value=False), mock.patch.object(
            cli.subprocess, "run"
        ) as blocked, redirect_stderr(stderr):
            self.assertEqual(2, cli.main(("uninstall",)))
        blocked.assert_not_called()
        self.assertIn("requires sudo", stderr.getvalue())

    def test_new_password_policy_and_missing_tty_fail_closed(self) -> None:
        self.assertEqual(12, MIN_NEW_PASSWORD_CHARACTERS)
        with self.assertRaises(SecurityError):
            validate_new_password("x" * 11)
        self.assertEqual("x" * 12, validate_new_password("x" * 12))

        stderr = io.StringIO()
        with mock.patch.object(
            web_service, "open_controlling_tty", side_effect=OSError
        ), mock.patch.object(
            web_service.getpass, "getpass"
        ) as prompt, redirect_stderr(stderr):
            result = web_service._password_command(  # noqa: SLF001
                Path("/not/read"), "operator"
            )
        self.assertEqual(2, result)
        prompt.assert_not_called()
        self.assertIn("never read from stdin", stderr.getvalue())

    def test_weak_interactive_password_is_not_written(self) -> None:
        stderr = io.StringIO()
        terminal = io.StringIO()
        original_stdin = sys.stdin

        def prompt(_message: str, *, stream: object) -> str:
            self.assertIs(terminal, stream)
            self.assertIs(terminal, sys.stdin)
            return "short"

        with mock.patch.object(
            web_service, "open_controlling_tty", return_value=terminal
        ), mock.patch.object(
            web_service.getpass, "getpass", side_effect=prompt
        ), mock.patch.object(
            web_service, "write_credential_atomic"
        ) as write, redirect_stderr(stderr):
            result = web_service._password_command(  # noqa: SLF001
                Path("/not/written"), "operator"
            )
        self.assertEqual(2, result)
        self.assertIs(original_stdin, sys.stdin)
        self.assertTrue(terminal.closed)
        write.assert_not_called()
        self.assertIn("at least 12 characters", stderr.getvalue())

    def test_inherited_password_input_must_still_be_a_terminal(self) -> None:
        stdin = io.StringIO("secret-from-a-pipe")
        stderr = io.StringIO()
        with mock.patch.object(web_service.os.sys, "stdin", stdin), mock.patch.object(
            web_service.getpass, "getpass"
        ) as prompt, redirect_stderr(stderr):
            result = web_service._password_command(  # noqa: SLF001
                Path("/not/written"), "operator", tty_stdin=True
            )
        self.assertEqual(2, result)
        prompt.assert_not_called()
        self.assertIn("never read from stdin", stderr.getvalue())

    def test_inherited_open_terminal_can_update_after_privilege_drop(self) -> None:
        inherited = mock.Mock()
        inherited.fileno.return_value = 0
        prompt_output = io.StringIO()
        record = SimpleNamespace(username="operator")
        original_stdin = sys.stdin

        def prompt(_message: str, *, stream: object) -> str:
            self.assertIs(prompt_output, stream)
            self.assertIs(inherited, sys.stdin)
            return "long-enough-password"

        with mock.patch.object(web_service.os.sys, "stdin", inherited), mock.patch.object(
            web_service.os.sys, "stderr", prompt_output
        ), mock.patch.object(
            web_service.os, "isatty", return_value=True
        ), mock.patch.object(
            web_service.os, "fstat", return_value=SimpleNamespace(st_mode=stat.S_IFCHR)
        ), mock.patch.object(
            web_service.getpass, "getpass", side_effect=prompt
        ) as prompt, mock.patch.object(
            web_service.CredentialRecord, "create", return_value=record
        ), mock.patch.object(
            web_service, "write_credential_atomic"
        ) as write:
            result = web_service._password_command(  # noqa: SLF001
                Path("/credential.json"), "operator", tty_stdin=True
            )
        self.assertEqual(0, result)
        self.assertEqual(2, prompt.call_count)
        write.assert_called_once_with(Path("/credential.json"), record)
        self.assertIs(original_stdin, sys.stdin)
        inherited.close.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "POSIX pseudo-terminal required")
    def test_controlling_tty_supports_a_real_nonseekable_pty(self) -> None:
        import pty

        master, slave = pty.openpty()
        terminal = None
        try:
            with mock.patch.object(web_service.os, "open", return_value=os.dup(slave)):
                terminal = web_service.open_controlling_tty()
            self.assertTrue(terminal.readable())
            self.assertTrue(terminal.writable())
            self.assertFalse(terminal.seekable())
            terminal.write("prompt")
            terminal.flush()
            self.assertEqual(b"prompt", os.read(master, 6))
            os.write(master, b"answer\n")
            self.assertEqual("answer\n", terminal.readline())
        finally:
            if terminal is not None:
                terminal.close()
            os.close(master)
            os.close(slave)


if __name__ == "__main__":
    unittest.main()
