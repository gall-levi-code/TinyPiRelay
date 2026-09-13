from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tinypirelay import installer, probe  # noqa: E402


class FakeRunner:
    def __init__(self, fail_once: tuple[str, ...] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.fail_once = fail_once
        self.identities_created = False

    def __call__(self, argv: object) -> probe.CommandResult:
        command = tuple(argv)  # type: ignore[arg-type]
        self.calls.append(command)
        if self.fail_once and command[: len(self.fail_once)] == self.fail_once:
            self.fail_once = None
            return probe.CommandResult(1, stderr="fixture failure")
        if command == ("ss", "-H", "-ltn"):
            return probe.CommandResult(0, "")
        if command[:2] == ("apt-get", "--simulate"):
            install_at = command.index("install")
            packages = command[install_at + 1 :]
            lines = []
            for item in packages:
                name, version = item.split("=", 1)
                lines.append(f"Inst {name} ({version} fixture [armhf])")
                lines.append(f"Conf {name} ({version} fixture [armhf])")
            lines.append(
                f"0 upgraded, {len(packages)} newly installed, 0 to remove and 0 not upgraded."
            )
            return probe.CommandResult(0, "\n".join(lines) + "\n")
        if command == ("systemd-sysusers", "tinypirelay.conf"):
            self.identities_created = True
            return probe.CommandResult(0, "")
        if command[:2] == ("getent", "passwd"):
            if not self.identities_created:
                return probe.CommandResult(2, "")
            user = command[2]
            uid = "995" if user == "tinypirelay-media" else "996"
            return probe.CommandResult(
                0, f"{user}:x:{uid}:{uid}:fixture:/:/usr/sbin/nologin\n"
            )
        if command == ("getent", "group", "tinypirelay-control"):
            return (
                probe.CommandResult(0, "tinypirelay-control:x:994:\n")
                if self.identities_created
                else probe.CommandResult(2, "")
            )
        if command[:2] == ("id", "-nG"):
            user = command[2]
            groups = (
                "tinypirelay-media audio tinypirelay-control\n"
                if user == "tinypirelay-media"
                else "tinypirelay-web tinypirelay-control\n"
            )
            return probe.CommandResult(0, groups)
        return probe.CommandResult(0, "")


class FixtureHost(installer.LocalHost):
    """Use a regular pointer file where Windows cannot create POSIX symlinks."""

    def set_current(self, version: str) -> None:
        self.write_atomic(
            "/opt/tinypirelay/current",
            f"releases/{version}\n".encode(),
            0o644,
        )
        self.events.append(f"current:{version}")


class NeverOwnedHost(FixtureHost):
    def owner_matches(self, logical_path: str, user: str, group: str) -> bool:
        return False


def complete_probe(
    *,
    supported: bool = True,
    missing_packages: tuple[str, ...] = (),
    missing_element: str | None = None,
) -> dict[str, object]:
    packages = {
        name: {
            "status": probe.PRESENT,
            "installed": name not in missing_packages,
            "installed_version": None if name in missing_packages else "1",
            "candidate_version": "1",
            "reason": None,
        }
        for name in probe.APT_PACKAGES
    }
    elements = {
        name: {
            "status": probe.MISSING if name == missing_element else probe.PRESENT,
            "plugin": "fixture",
            "plugin_version": None if name == missing_element else "1.0",
        }
        for name in probe.REQUIRED_GSTREAMER_ELEMENTS
    }
    return {
        "status": probe.PRESENT,
        "platform": {
            "status": probe.PRESENT,
            "os_architecture_baseline": {
                "status": probe.PRESENT if supported else probe.MISSING,
                "target": "Raspberry Pi OS Bookworm/Trixie armhf/arm64",
                "reason": None if supported else "fixture architecture is unsupported",
            },
        },
        "hardware": {"status": probe.PRESENT},
        "storage": {"status": probe.PRESENT},
        "audio": {"status": probe.PRESENT},
        "packages": {"status": probe.PRESENT, "items": packages},
        "gstreamer": {
            "status": probe.PRESENT,
            "required_elements": elements,
        },
    }


def write_valid_config(path: Path) -> None:
    value = {
        "schema_version": 1,
        "capture": {
            "device_id": "hw:CARD=iMM6C,DEV=0",
            "mode": {"format": "S16LE", "rate_hz": 48000, "channels": 1},
        },
        "stream": {
            "enabled": False,
            "representation_id": None,
            "destination_host": None,
            "destination_port": None,
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
        "monitoring": {"spectrum_updates_per_second": 5, "spectrum_bands": 512},
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    os.chmod(path, 0o600)


def copy_artifact(parent: Path, version: str) -> Path:
    target = parent / ("artifact-" + version)
    target.mkdir()
    for name in installer.PAYLOAD_NAMES:
        source = ROOT / name
        destination = target / name
        if source.is_dir():
            shutil.copytree(
                source,
                destination,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
        else:
            shutil.copy2(source, destination)
    (target / "VERSION").write_bytes((version + "\n").encode("ascii"))
    return target


class InstallerTests(unittest.TestCase):
    def test_first_run_needs_no_microphone_config_or_terminal_and_repeats_safely(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = copy_artifact(base, "0.6.0-dev")
            before = complete_probe(missing_packages=("ffmpeg", "avahi-daemon"))
            after = complete_probe()
            before["audio"] = after["audio"] = {"status": probe.MISSING}
            target, host, _runner = self.make_installer(base, source, [before, after, after], tty=False)
            plan = target.plan()
            self.assertTrue(plan.ready, plan.errors)
            self.assertFalse(plan.needs_credential)
            self.assertTrue(plan.setup_pending)
            self.assertEqual("lan", plan.web_access)
            self.assertIn("ffmpeg=1", plan.package_plan)
            self.assertIn("avahi-daemon=1", plan.package_plan)
            target.apply(plan)
            self.assertEqual(b"1\n", host.read_bytes(installer.SETUP_PENDING))
            self.assertFalse(host.exists("/etc/tinypirelay/web/credential.json"))
            self.assertEqual({"device_id": None, "mode": None}, json.loads(host.read_bytes("/etc/tinypirelay/media/config.json"))["capture"])
            dropin = host.read_bytes(installer.WEB_ACCESS_DROPIN).decode()
            self.assertIn("--bind 0.0.0.0 --port 80 --lan-only", dropin)
            self.assertIn("AmbientCapabilities=CAP_NET_BIND_SERVICE", dropin)
            self.assertIn("192.168.0.0/16", dropin)
            repeat = target.plan()
            self.assertEqual("noop", repeat.mode, repeat.errors)
            events = list(host.events)
            target.apply(repeat)
            self.assertEqual(events, host.events)

    def test_account_creation_is_preserved_and_missing_credentials_never_reopen_setup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            target, host, _runner = self.make_installer(
                base, copy_artifact(base, "0.6.0-dev"), [complete_probe() for _ in range(5)], tty=False
            )
            target.apply(target.plan())
            record = installer.validate_initial_credential(installer.InitialCredential("operator", "correct horse fixture battery"))
            credential_path = "/etc/tinypirelay/web/credential.json"
            host.write_atomic(credential_path, record.to_json().encode(), 0o600)
            host.set_owner(credential_path, "tinypirelay-web", "tinypirelay-web")
            host.remove_file(installer.SETUP_PENDING)
            signed_up = target.plan()
            self.assertTrue(signed_up.ready, signed_up.errors)
            target.apply(signed_up)
            host.remove_file(credential_path)
            missing = target.plan(credential_fd_available=True)
            self.assertFalse(missing.ready)
            self.assertIn("explicit account recovery", " ".join(missing.errors))
            self.assertFalse(host.exists(installer.SETUP_PENDING))

    def test_legacy_upgrade_keeps_loopback_and_lan_migration_checks_port_and_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            target, host, runner = self.make_installer(
                base, copy_artifact(base, "0.6.0-dev"), [complete_probe() for _ in range(9)]
            )
            target.apply(target.plan(web_access="loopback"), installer.InitialCredential("operator", "correct horse fixture battery"))
            host.remove_file(installer.WEB_ACCESS_PATH)  # Simulate a legacy installation.
            target.source_dir = copy_artifact(base, "0.6.1-dev")
            upgrade = target.plan()
            self.assertEqual("loopback", upgrade.web_access)
            target.apply(upgrade)
            self.assertFalse(host.exists(installer.WEB_ACCESS_DROPIN))
            checked_ports: list[int] = []
            target.port_checker = lambda port: checked_ports.append(port) or "TCP port 80 is already listening"
            conflict = target.plan(web_access="lan")
            self.assertFalse(conflict.ready)
            self.assertEqual([80], checked_ports)
            target.port_checker = lambda _port: None
            migration = target.plan(web_access="lan")
            self.assertTrue(migration.ready, migration.errors)
            self.assertEqual("repair", migration.mode)
            runner.fail_once = ("systemctl", "restart", *installer.UNITS)
            with self.assertRaisesRegex(installer.InstallError, "previous access restored"):
                target.apply(migration)
            self.assertEqual("loopback", target._read_web_access())
            self.assertFalse(host.exists(installer.WEB_ACCESS_DROPIN))
            target.apply(target.plan(web_access="lan"))
            self.assertEqual("lan", target._read_web_access())

    def make_installer(
        self,
        directory: Path,
        source: Path,
        probes: list[dict[str, object]],
        *,
        runner: FakeRunner | None = None,
        tty: bool = True,
        port_error: str | None = None,
        stages: list[str] | None = None,
    ) -> tuple[installer.Installer, FixtureHost, FakeRunner]:
        command_runner = runner or FakeRunner()
        host = FixtureHost(directory / "root", command_runner)

        def collect() -> dict[str, object]:
            return probes.pop(0)

        target = installer.Installer(
            host,
            source,
            collect,
            port_checker=lambda _port: port_error,
            tty_checker=lambda: tty,
            privileged_checker=lambda: True,
            stage_hook=(stages.append if stages is not None else installer._noop_stage),
        )
        return target, host, command_runner

    def test_live_installer_commands_use_the_long_timeout(self) -> None:
        completed = mock.Mock(returncode=0, stdout="ready\n", stderr="")
        runner = mock.Mock(side_effect=AssertionError("short probe runner used"))
        with mock.patch.object(installer.subprocess, "run", return_value=completed) as run:
            host = installer.LocalHost(runner=runner)
            result = host.read_command(("apt-get", "--simulate"))
            host.mutate_command(("apt-get", "install"))

        self.assertEqual(0, result.returncode)
        self.assertEqual("ready\n", result.stdout)
        runner.assert_not_called()
        self.assertEqual(2, run.call_count)
        self.assertEqual([300, 1800], [call.kwargs["timeout"] for call in run.call_args_list])

    def test_manual_installer_entrypoint_pins_system_path_before_helpers(self) -> None:
        text = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn("PATH=/usr/sbin:/usr/bin:/sbin:/bin\nexport PATH\n", text)
        self.assertLess(text.index("PATH=/usr/sbin"), text.index("dirname"))

    def test_plan_is_read_only_and_separates_packages_from_elements(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = copy_artifact(base, "0.4.0-dev")
            config = base / "config.json"
            write_valid_config(config)
            before = sorted(str(path.relative_to(base)) for path in base.rglob("*"))
            preflight = complete_probe(
                missing_packages=("gstreamer1.0-plugins-bad",),
                missing_element="srtsink",
            )
            target, host, _runner = self.make_installer(base, source, [preflight])

            plan = target.plan(config)

            after = sorted(str(path.relative_to(base)) for path in base.rglob("*"))
            self.assertTrue(plan.ready, plan.errors)
            self.assertEqual(("gstreamer1.0-plugins-bad=1",), plan.package_plan)
            self.assertEqual(
                (("gstreamer1.0-plugins-bad", "1"),), plan.apt_transaction
            )
            self.assertIn("srtsink", plan.deferred_elements)
            self.assertEqual([], host.events)
            self.assertEqual(before, after)

    def test_missing_arecord_is_deferred_only_when_alsa_utils_is_planned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = copy_artifact(base, "0.4.0-dev")
            config = base / "config.json"
            write_valid_config(config)
            preflight = complete_probe(missing_packages=("alsa-utils",))
            preflight["audio"] = {
                "status": probe.MISSING,
                "reason": "arecord is unavailable",
            }
            target, _host, _runner = self.make_installer(
                base, source, [preflight, complete_probe()]
            )

            plan = target.plan(config)

            self.assertTrue(plan.ready, plan.errors)
            self.assertIn("alsa-utils=1", plan.package_plan)
            self.assertTrue(any("first-visit" in action for action in plan.actions))

            blocked_probe = complete_probe()
            blocked_probe["audio"] = {
                "status": probe.MISSING,
                "reason": "capture device unavailable",
            }
            blocked, _host2, _runner2 = self.make_installer(
                base / "blocked", source, [blocked_probe]
            )
            blocked_config = base / "blocked-config.json"
            write_valid_config(blocked_config)
            refused = blocked.plan(blocked_config)
            self.assertTrue(refused.ready, refused.errors)

    def test_unsupported_platform_missing_tty_and_port_conflict_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = copy_artifact(base, "0.4.0-dev")
            config = base / "config.json"
            write_valid_config(config)
            target, host, _runner = self.make_installer(
                base,
                source,
                [complete_probe(supported=False)],
                tty=False,
                port_error="TCP port 8080 is already listening",
            )

            plan = target.plan(config)

            self.assertFalse(plan.ready)
            rendered = "\n".join(plan.errors)
            self.assertIn("unsupported", rendered)
            self.assertNotIn("/dev/tty", rendered)
            self.assertIn("port 8080", rendered)
            self.assertEqual([], host.events)

    def test_fresh_install_repeat_is_idempotent_and_never_logs_password(self) -> None:
        password = "correct horse fixture battery"
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = copy_artifact(base, "0.4.0-dev")
            config = base / "config.json"
            write_valid_config(config)
            stages: list[str] = []
            target, host, _runner = self.make_installer(
                base,
                source,
                [complete_probe(), complete_probe(), complete_probe()],
                stages=stages,
            )
            plan = target.plan(config)

            state = target.apply(
                plan, installer.InitialCredential("operator", password)
            )
            event_count = len(host.events)
            repeat = target.plan()
            repeated = target.apply(repeat)

            self.assertEqual("complete", state.phase)
            self.assertEqual("noop", repeat.mode)
            self.assertEqual(state, repeated)
            self.assertEqual(event_count, len(host.events))
            self.assertEqual(1, host.events.count("release:0.4.0-dev"))
            self.assertIn("mkdir:/opt/tinypirelay:0755", host.events)
            self.assertNotIn(password, repr(installer.InitialCredential("operator", password)))
            self.assertNotIn(password, "\n".join(host.events))
            self.assertIn(
                "owner:/etc/tinypirelay/media/config.json:tinypirelay-media:tinypirelay-control",
                host.events,
            )
            self.assertIn(
                "owner:/etc/tinypirelay/web/credential.json:tinypirelay-web:tinypirelay-web",
                host.events,
            )
            credential = host.resolve("/etc/tinypirelay/web/credential.json")
            if os.name == "posix":
                self.assertEqual(0, stat_mode(credential) & 0o077)

    def test_same_version_repairs_pointer_integration_and_private_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = copy_artifact(base, "0.4.0-dev")
            config = base / "config.json"
            write_valid_config(config)
            target, host, _runner = self.make_installer(
                base,
                source,
                [
                    complete_probe(),
                    complete_probe(),
                    complete_probe(),
                    complete_probe(),
                    complete_probe(),
                ],
            )
            target.apply(
                target.plan(config),
                installer.InitialCredential(
                    "operator", "correct horse fixture battery"
                ),
            )
            wrapper = host.resolve("/usr/bin/tinypirelay")
            wrapper.write_bytes(b"tampered\n")
            host.resolve("/opt/tinypirelay/current").write_text(
                "releases/0.3.0-dev\n", encoding="ascii"
            )
            shutil.rmtree(host.resolve("/run/tinypirelay"))

            stale_repair = target.plan()
            installed_module = host.resolve(
                "/opt/tinypirelay/releases/0.4.0-dev/src/tinypirelay/__init__.py"
            )
            original_module = installed_module.read_bytes()
            installed_module.write_bytes(original_module + b"\n# tampered after plan\n")
            before_failed_apply = list(host.events)
            with self.assertRaisesRegex(installer.InstallError, "release changed"):
                target.apply(stale_repair)
            self.assertEqual(before_failed_apply, host.events)
            installed_module.write_bytes(original_module)

            repair = target.plan()
            repaired = target.apply(repair)

            self.assertEqual("repair", repair.mode)
            self.assertEqual("complete", repaired.phase)
            self.assertEqual("0.4.0-dev", host.current_version())
            self.assertEqual(
                (source / "packaging/bin/tinypirelay").read_bytes(),
                wrapper.read_bytes(),
            )
            self.assertTrue(host.resolve("/run/tinypirelay").is_dir())
            for unit in installer.UNITS:
                self.assertGreaterEqual(
                    _runner.calls.count(
                        ("systemctl", "is-active", "--quiet", unit)
                    ),
                    2,
                )

    def test_maintenance_units_are_activated_and_uninstalled_without_data_loss(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = copy_artifact(base, "0.6.0-dev")
            config = base / "config.json"
            write_valid_config(config)
            target, host, runner = self.make_installer(base, source, [complete_probe(), complete_probe()])
            target.apply(target.plan(config), installer.InitialCredential("operator", "correct horse fixture battery"))
            for unit in installer.MAINTENANCE_UNITS:
                self.assertTrue(host.resolve("/etc/systemd/system/" + unit).is_file())
            self.assertIn(("systemctl", "is-active", "--quiet", installer.MAINTENANCE_SOCKET), runner.calls)
            self.assertTrue(host.directory_matches("/run/tinypirelay-maintenance", 0o750, "root", "tinypirelay-web"))
            before = host.read_bytes("/etc/tinypirelay/media/config.json")
            target.uninstall(target.plan_uninstall())
            self.assertIn(("systemctl", "disable", "--now", *installer.MAINTENANCE_UNITS), runner.calls)
            self.assertFalse(host.resolve("/run/tinypirelay-maintenance").exists())
            self.assertEqual(before, host.read_bytes("/etc/tinypirelay/media/config.json"))
            for unit in installer.MAINTENANCE_UNITS:
                self.assertFalse(host.resolve("/etc/systemd/system/" + unit).exists())

    def test_rollback_to_pre_maintenance_release_removes_privileged_helper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = copy_artifact(base, "0.6.0-dev")
            config = base / "config.json"
            write_valid_config(config)
            target, host, runner = self.make_installer(base, source, [complete_probe(), complete_probe()])
            target.apply(target.plan(config), installer.InitialCredential("operator", "correct horse fixture battery"))
            # A retained immutable release from before maintenance is still valid.
            old_source = copy_artifact(base, "0.5.10-dev")
            for unit in installer.MAINTENANCE_UNITS:
                (old_source / "packaging/systemd" / unit).unlink()
            (old_source / "src/tinypirelay/maintenance.py").unlink()
            host.mkdir("/opt/tinypirelay/releases/0.5.10-dev", 0o755)
            old_release = host.resolve("/opt/tinypirelay/releases/0.5.10-dev")
            for name in installer.PAYLOAD_NAMES:
                item = old_source / name
                if item.is_dir():
                    shutil.copytree(item, old_release / name)
                else:
                    shutil.copy2(item, old_release / name)
            digest = installer.validate_payload_tree(old_source)
            self.assertIsNone(target._rollback_activation("0.5.10-dev", digest))
            self.assertEqual("0.5.10-dev", host.current_version())
            for unit in installer.MAINTENANCE_UNITS:
                self.assertFalse(host.resolve("/etc/systemd/system/" + unit).exists())

    def test_maintenance_sandbox_keeps_web_unprivileged(self) -> None:
        web = (ROOT / "packaging/systemd/tinypirelay-web.service").read_text()
        helper = (ROOT / "packaging/systemd/tinypirelay-maintenance.service").read_text()
        socket_unit = (ROOT / "packaging/systemd/tinypirelay-maintenance.socket").read_text()
        tmpfiles = (ROOT / "packaging/tmpfiles.d/tinypirelay.conf").read_text()
        self.assertIn("User=tinypirelay-web", web)
        self.assertIn("NoNewPrivileges=true", web)
        self.assertEqual(["ReadWritePaths=/etc/tinypirelay/web"], [line for line in web.splitlines() if line.startswith("ReadWritePaths=")])
        self.assertIn("User=root", helper)
        self.assertIn("RestrictAddressFamilies=AF_UNIX", helper)
        self.assertIn("SocketGroup=tinypirelay-web", socket_unit)
        self.assertIn("SocketMode=0660", socket_unit)
        self.assertIn("d /run/tinypirelay-maintenance", tmpfiles)
        self.assertIn("f /run/lock/tinypirelay-installer.lock", tmpfiles)

    def test_weak_password_is_rejected_before_first_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = copy_artifact(base, "0.4.0-dev")
            config = base / "config.json"
            write_valid_config(config)
            target, host, _runner = self.make_installer(
                base, source, [complete_probe()]
            )
            plan = target.plan(config)

            with self.assertRaisesRegex(installer.InstallError, "at least 12"):
                target.apply(plan, installer.InitialCredential("operator", "too-short"))

            self.assertEqual([], host.events)

    def test_config_and_payload_changes_after_plan_are_rejected_before_mutation(self) -> None:
        for changed in ("config", "payload"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                source = copy_artifact(base, "0.4.0-dev")
                config = base / "config.json"
                write_valid_config(config)
                target, host, _runner = self.make_installer(
                    base, source, [complete_probe()]
                )
                plan = target.plan(config)
                if changed == "config":
                    config.write_bytes(config.read_bytes() + b"\n")
                else:
                    module = source / "src/tinypirelay/__init__.py"
                    module.write_bytes(module.read_bytes() + b"\n")

                with self.assertRaisesRegex(installer.InstallError, "changed after planning"):
                    target.apply(
                        plan,
                        installer.InitialCredential(
                            "operator", "correct horse fixture battery"
                        ),
                    )

                self.assertEqual([], host.events)

    def test_journal_binds_interrupted_and_complete_installs_to_exact_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = copy_artifact(base, "0.4.0-dev")
            config = base / "config.json"
            write_valid_config(config)
            runner = FakeRunner(("apt-get", "--no-remove"))
            target, host, _runner = self.make_installer(
                base,
                source,
                [
                    complete_probe(
                        missing_packages=("gstreamer1.0-plugins-bad",),
                        missing_element="srtsink",
                    )
                ],
                runner=runner,
            )
            interrupted_plan = target.plan(config)
            with self.assertRaises(installer.InstallError):
                target.apply(
                    interrupted_plan,
                    installer.InitialCredential(
                        "operator", "correct horse fixture battery"
                    ),
                )
            interrupted_state, error = target._load_state()
            self.assertIsNone(error)
            self.assertEqual(interrupted_plan.source_sha256, interrupted_state.source_sha256)

            module = source / "src/tinypirelay/__init__.py"
            module.write_bytes(module.read_bytes() + b"\n# different same-version payload\n")
            resumed, _same_host, _new_runner = self.make_installer(
                base,
                source,
                [complete_probe()],
            )
            collision = resumed.plan(config)
            self.assertFalse(collision.ready)
            self.assertEqual("blocked", collision.mode)
            self.assertTrue(any("payload" in item for item in collision.errors))
            self.assertFalse(any(event.startswith("release:") for event in host.events))

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = copy_artifact(base, "0.4.0-dev")
            config = base / "config.json"
            write_valid_config(config)
            target, _host, _runner = self.make_installer(
                base,
                source,
                [complete_probe(), complete_probe(), complete_probe()],
            )
            target.apply(
                target.plan(config),
                installer.InitialCredential(
                    "operator", "correct horse fixture battery"
                ),
            )
            module = source / "src/tinypirelay/__init__.py"
            module.write_bytes(module.read_bytes() + b"\n# version collision\n")

            collision = target.plan()

            self.assertFalse(collision.ready)
            self.assertEqual("blocked", collision.mode)
            self.assertTrue(any("different release payload" in item for item in collision.errors))

    def test_payload_fingerprint_does_not_depend_on_absolute_parent_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "__pycache__"
            parent.mkdir()
            source = copy_artifact(parent, "0.4.0-dev")
            before = installer.validate_payload_tree(source)
            module = source / "src/tinypirelay/__init__.py"
            module.write_bytes(module.read_bytes() + b"\n# changed\n")
            after = installer.validate_payload_tree(source)

            self.assertEqual(64, len(before))
            self.assertNotEqual(before, after)

    def test_real_apt_install_pins_every_simulated_dependency(self) -> None:
        class TransitiveRunner(FakeRunner):
            def __call__(self, argv: object) -> probe.CommandResult:
                command = tuple(argv)  # type: ignore[arg-type]
                if command[:2] == ("apt-get", "--simulate"):
                    self.calls.append(command)
                    return probe.CommandResult(
                        0,
                        "Inst gstreamer1.0-plugins-bad (1 fixture [armhf])\n"
                        "Inst libfixture-dependency (7 fixture [armhf])\n"
                        "0 upgraded, 2 newly installed, 0 to remove and 0 not upgraded.\n",
                    )
                return super().__call__(command)

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = copy_artifact(base, "0.4.0-dev")
            config = base / "config.json"
            write_valid_config(config)
            runner = TransitiveRunner()
            target, _host, _runner = self.make_installer(
                base,
                source,
                [
                    complete_probe(
                        missing_packages=("gstreamer1.0-plugins-bad",),
                        missing_element="srtsink",
                    ),
                    complete_probe(),
                ],
                runner=runner,
            )
            plan = target.plan(config)

            target.apply(
                plan,
                installer.InitialCredential(
                    "operator", "correct horse fixture battery"
                ),
            )

            actual = next(
                call
                for call in runner.calls
                if call[:2] == ("apt-get", "--no-remove")
            )
            self.assertIn("gstreamer1.0-plugins-bad=1", actual)
            self.assertIn("libfixture-dependency=7", actual)

    def test_package_and_post_element_failures_stop_before_service_enable(self) -> None:
        for name, runner, post_probe, expected_phase in (
            (
                "package",
                FakeRunner(("apt-get", "--no-remove")),
                complete_probe(),
                "prepared",
            ),
            (
                "element",
                FakeRunner(),
                complete_probe(missing_element="srtsink"),
                "packages",
            ),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                source = copy_artifact(base, "0.4.0-dev")
                config = base / "config.json"
                write_valid_config(config)
                preflight = complete_probe(
                    missing_packages=("gstreamer1.0-plugins-bad",),
                    missing_element="srtsink",
                )
                target, host, command_runner = self.make_installer(
                    base, source, [preflight, post_probe], runner=runner
                )
                plan = target.plan(config)

                with self.assertRaises(installer.InstallError):
                    target.apply(
                        plan,
                        installer.InitialCredential(
                            "operator", "correct horse fixture battery"
                        ),
                    )

                state, error = target._load_state()
                self.assertIsNone(error)
                self.assertEqual(expected_phase, state.phase)
                self.assertFalse(
                    any(call[:2] == ("systemctl", "enable") for call in command_runner.calls)
                )
                self.assertFalse(any(event.startswith("release:") for event in host.events))

    def test_service_failure_resumes_without_replacing_release_or_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = copy_artifact(base, "0.4.0-dev")
            config = base / "config.json"
            write_valid_config(config)
            runner = FakeRunner(("systemctl", "restart"))
            target, host, _runner = self.make_installer(
                base,
                source,
                [
                    complete_probe(),
                    complete_probe(),
                    complete_probe(),
                    complete_probe(),
                    complete_probe(),
                ],
                runner=runner,
            )
            plan = target.plan(config)
            credential = installer.InitialCredential(
                "operator", "correct horse fixture battery"
            )
            with self.assertRaises(installer.InstallError):
                target.apply(plan, credential)
            state, _error = target._load_state()
            self.assertEqual("release", state.phase)
            config_hash = hashlib_file(host.resolve("/etc/tinypirelay/media/config.json"))

            resumed = target.plan(config)
            target.apply(resumed)

            self.assertEqual("resume", resumed.mode)
            self.assertEqual(1, host.events.count("release:0.4.0-dev"))
            self.assertEqual(
                config_hash,
                hashlib_file(host.resolve("/etc/tinypirelay/media/config.json")),
            )

    def test_each_service_must_be_active_before_complete_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = copy_artifact(base, "0.4.0-dev")
            config = base / "config.json"
            write_valid_config(config)
            runner = FakeRunner(
                ("systemctl", "is-active", "--quiet", installer.WEB_UNIT)
            )
            target, _host, _runner = self.make_installer(
                base,
                source,
                [complete_probe(), complete_probe()],
                runner=runner,
            )

            with self.assertRaisesRegex(installer.InstallError, "activation failed"):
                target.apply(
                    target.plan(config),
                    installer.InitialCredential(
                        "operator", "correct horse fixture battery"
                    ),
                )

            state, error = target._load_state()
            self.assertIsNone(error)
            self.assertEqual("release", state.phase)
            self.assertIn(
                ("systemctl", "is-active", "--quiet", installer.MEDIA_UNIT),
                runner.calls,
            )
            self.assertIn(
                ("systemctl", "is-active", "--quiet", installer.WEB_UNIT),
                runner.calls,
            )
            self.assertNotIn(
                (
                    "systemctl",
                    "is-active",
                    "--quiet",
                    installer.MEDIA_UNIT,
                    installer.WEB_UNIT,
                ),
                runner.calls,
            )

    def test_upgrade_backs_up_and_preserves_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source_v1 = copy_artifact(base, "0.4.0-dev")
            config = base / "config.json"
            write_valid_config(config)
            target, host, _runner = self.make_installer(
                base,
                source_v1,
                [complete_probe(), complete_probe()],
            )
            target.apply(
                target.plan(config),
                installer.InitialCredential(
                    "operator", "correct horse fixture battery"
                ),
            )
            installed = host.resolve("/etc/tinypirelay/media/config.json")
            installed.write_bytes(installed.read_bytes() + b"\n")
            expected = installed.read_bytes()
            source_v2 = copy_artifact(base, "0.5.0-dev")
            upgraded, _same_host, _same_runner = self.make_installer(
                base,
                source_v2,
                [complete_probe(), complete_probe()],
                runner=_runner,
            )

            plan = upgraded.plan()
            state = upgraded.apply(plan)

            backup = host.resolve(
                "/var/lib/tinypirelay/backups/0.4.0-dev-before-0.5.0-dev-"
                + hashlib_file(installed)
                + ".json"
            )
            self.assertEqual("upgrade", plan.mode)
            self.assertEqual("0.5.0-dev", state.installed_version)
            self.assertEqual(expected, installed.read_bytes())
            self.assertEqual(expected, backup.read_bytes())

    def test_failed_upgrade_restores_and_verifies_previous_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source_v1 = copy_artifact(base, "0.4.0-dev")
            config = base / "config.json"
            write_valid_config(config)
            target, host, runner = self.make_installer(
                base, source_v1, [complete_probe(), complete_probe()]
            )
            installed_v1 = target.apply(
                target.plan(config),
                installer.InitialCredential(
                    "operator", "correct horse fixture battery"
                ),
            )
            source_v2 = copy_artifact(base, "0.5.0-dev")
            runner.fail_once = ("systemctl", "restart")
            upgraded, _same_host, _same_runner = self.make_installer(
                base,
                source_v2,
                [complete_probe(), complete_probe()],
                runner=runner,
            )

            with self.assertRaisesRegex(installer.InstallError, "previous release was restored"):
                upgraded.apply(upgraded.plan())

            restored, error = upgraded._load_state()
            self.assertIsNone(error)
            self.assertEqual("complete", restored.phase)
            self.assertEqual("0.4.0-dev", restored.installed_version)
            self.assertEqual(installed_v1.source_sha256, restored.source_sha256)
            self.assertEqual("0.4.0-dev", host.current_version())
            for unit in installer.UNITS:
                self.assertGreaterEqual(
                    runner.calls.count(
                        ("systemctl", "is-active", "--quiet", unit)
                    ),
                    2,
                )

    def test_resumed_upgrade_still_backs_up_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = copy_artifact(base, "0.4.0-dev")
            config = base / "config.json"
            write_valid_config(config)
            target, host, runner = self.make_installer(
                base, source, [complete_probe(), complete_probe()]
            )
            target.apply(target.plan(config), installer.InitialCredential(
                "operator", "correct horse fixture battery"
            ))
            upgraded, _host, _runner = self.make_installer(
                base, copy_artifact(base, "0.5.0-dev"),
                [complete_probe() for _ in range(4)], runner=runner,
            )

            def interrupt(stage: str) -> None:
                if stage == "release":
                    raise KeyboardInterrupt

            upgraded.stage_hook = interrupt
            with self.assertRaises(KeyboardInterrupt):
                upgraded.apply(upgraded.plan())
            upgraded.stage_hook = installer._noop_stage
            plan = upgraded.plan()
            self.assertEqual("resume", plan.mode)
            upgraded.apply(plan)
            installed = host.resolve("/etc/tinypirelay/media/config.json")
            backup = host.resolve(
                "/var/lib/tinypirelay/backups/0.4.0-dev-before-0.5.0-dev-"
                + hashlib_file(installed) + ".json"
            )
            self.assertEqual(installed.read_bytes(), backup.read_bytes())

    def test_interrupted_current_link_replacement_can_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            host = installer.LocalHost(temporary, FakeRunner())
            host.mkdir("/opt/tinypirelay", 0o755)
            try:
                os.symlink(
                    "releases/0.5.0-dev",
                    host.resolve("/opt/tinypirelay/.current.new"),
                    target_is_directory=True,
                )
            except OSError as exc:
                self.skipTest(f"symbolic links are unavailable: {exc}")
            # An interruption after symlink creation leaves this exact state.
            self.assertIsNone(host.current_version())
            host.set_current("0.5.0-dev")
            self.assertEqual("0.5.0-dev", host.current_version())
            self.assertFalse(host.resolve("/opt/tinypirelay/.current.new").is_symlink())

    def test_uninstall_preserves_config_and_recordings_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = copy_artifact(base, "0.4.0-dev")
            config = base / "config.json"
            write_valid_config(config)
            target, host, _runner = self.make_installer(
                base, source, [complete_probe(), complete_probe()]
            )
            target.apply(
                target.plan(config),
                installer.InitialCredential(
                    "operator", "correct horse fixture battery"
                ),
            )
            recording = host.resolve("/var/lib/tinypirelay/recordings/keep.flac")
            recording.write_bytes(b"fixture")

            state = target.uninstall(target.plan_uninstall())

            self.assertEqual("uninstalled", state.phase)
            self.assertTrue(host.exists("/etc/tinypirelay/media/config.json"))
            self.assertTrue(recording.exists())
            self.assertFalse(host.exists("/opt/tinypirelay"))

    def test_uninstall_requires_managed_unchanged_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = copy_artifact(base, "0.4.0-dev")
            target, host, _runner = self.make_installer(base, source, [])
            unmanaged = host.resolve("/opt/tinypirelay")
            unmanaged.mkdir(parents=True)
            sentinel = unmanaged / "keep"
            sentinel.write_text("operator data", encoding="utf-8")

            plan = target.plan_uninstall()

            self.assertFalse(plan.ready)
            with self.assertRaisesRegex(installer.InstallError, "plan with errors"):
                target.uninstall(plan)
            self.assertEqual("operator data", sentinel.read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = copy_artifact(base, "0.4.0-dev")
            config = base / "config.json"
            write_valid_config(config)
            target, host, _runner = self.make_installer(
                base, source, [complete_probe(), complete_probe()]
            )
            complete = target.apply(
                target.plan(config),
                installer.InitialCredential(
                    "operator", "correct horse fixture battery"
                ),
            )
            plan = target.plan_uninstall()
            target._write_state(
                installer.InstallState(
                    "complete",
                    complete.target_version,
                    complete.installed_version,
                    complete.installed_version,
                    complete.source_sha256,
                    complete.source_sha256,
                )
            )

            with self.assertRaisesRegex(installer.InstallError, "changed after planning"):
                target.uninstall(plan)

            self.assertTrue(host.exists("/opt/tinypirelay"))

    def test_uninstall_preserves_installation_when_stopping_a_unit_fails(self) -> None:
        for units in (installer.UNITS, installer.MAINTENANCE_UNITS):
            with self.subTest(units=units), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                source = copy_artifact(base, "0.4.0-dev")
                config = base / "config.json"
                write_valid_config(config)
                target, host, runner = self.make_installer(
                    base, source, [complete_probe(), complete_probe()]
                )
                initial = target.apply(target.plan(config), installer.InitialCredential(
                    "operator", "correct horse fixture battery"
                ))
                runner.fail_once = ("systemctl", "disable", "--now", *units)
                with self.assertRaises(installer.InstallError):
                    target.uninstall(target.plan_uninstall())
                self.assertTrue(host.exists("/opt/tinypirelay"))
                self.assertTrue(host.exists("/etc/tinypirelay/media/config.json"))
                self.assertEqual((initial, None), target._load_state())

    def test_stop_failure_is_ignored_only_for_confirmed_absent_units(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            target, host, _runner = self.make_installer(base, ROOT, [])
            with mock.patch.object(host, "mutate_command", side_effect=installer.InstallError("stop failed")):
                absent = "LoadState=not-found\nActiveState=inactive\n"
                for output in ("LoadState=loaded\n", "", "LoadState=not-found\nActiveState=active\n", absent):
                    with self.subTest(output=output), mock.patch.object(
                        host, "read_command", return_value=probe.CommandResult(0, output)
                    ):
                        if output == absent:
                            target._deactivate_units(installer.UNITS)
                        else:
                            with self.assertRaises(installer.InstallError):
                                target._deactivate_units(installer.UNITS)

    def test_uninstalled_journal_requires_one_normalized_version_digest_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = copy_artifact(base, "0.4.0-dev")
            target, host, _runner = self.make_installer(base, source, [])
            host.mkdir("/var/lib/tinypirelay", 0o710)
            malformed = {
                "schema_version": installer.STATE_SCHEMA_VERSION,
                "phase": "uninstalled",
                "target_version": None,
                "installed_version": None,
                "previous_version": "0.4.0-dev",
                "source_sha256": None,
                "previous_source_sha256": None,
            }
            host.write_atomic(
                "/var/lib/tinypirelay/install-state.json",
                (json.dumps(malformed) + "\n").encode(),
                0o600,
            )

            state, error = target._load_state()
            uninstall = target.plan_uninstall()

            self.assertIsNone(state)
            self.assertIn("journal is invalid", error)
            self.assertFalse(uninstall.ready)

    def test_empty_prejournal_state_directory_is_safe_to_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = copy_artifact(base, "0.4.0-dev")
            config = base / "config.json"
            write_valid_config(config)
            (base / "root/var/lib/tinypirelay").mkdir(parents=True)
            target, host, _runner = self.make_installer(
                base, source, [complete_probe()]
            )

            plan = target.plan(config)

            self.assertTrue(plan.ready, plan.errors)
            self.assertEqual("install", plan.mode)
            self.assertEqual([], host.events)

    @unittest.skipUnless(os.name == "posix", "POSIX file mode repair")
    def test_same_version_repairs_modes_and_rejects_invalid_credential_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = copy_artifact(base, "0.4.0-dev")
            config = base / "config.json"
            write_valid_config(config)
            target, host, _runner = self.make_installer(
                base,
                source,
                [
                    complete_probe(),
                    complete_probe(),
                    complete_probe(),
                    complete_probe(),
                    complete_probe(),
                ],
            )
            target.apply(
                target.plan(config),
                installer.InitialCredential(
                    "operator", "correct horse fixture battery"
                ),
            )
            installed_config = host.resolve(
                "/etc/tinypirelay/media/config.json"
            )
            os.chmod(installed_config, 0o644)

            repair = target.plan()
            repaired = target.apply(repair)

            self.assertEqual("repair", repair.mode)
            self.assertEqual("complete", repaired.phase)
            self.assertEqual(0o600, stat_mode(installed_config))
            credential = host.resolve(
                "/etc/tinypirelay/web/credential.json"
            )
            credential.write_text("{}\n", encoding="utf-8")
            os.chmod(credential, 0o600)

            invalid = target.plan()

            self.assertFalse(invalid.ready)
            self.assertTrue(any("credential" in error for error in invalid.errors))

    def test_reinstall_explicitly_reuses_existing_config_and_rejects_competitor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = copy_artifact(base, "0.4.0-dev")
            config = base / "config.json"
            write_valid_config(config)
            target, host, _runner = self.make_installer(
                base,
                source,
                [
                    complete_probe(),
                    complete_probe(),
                    complete_probe(),
                    complete_probe(),
                    complete_probe(),
                ],
            )
            target.apply(
                target.plan(config),
                installer.InitialCredential(
                    "operator", "correct horse fixture battery"
                ),
            )
            target.uninstall(target.plan_uninstall())
            installed = host.resolve("/etc/tinypirelay/media/config.json")
            competitor = base / "competitor.json"
            competitor.write_bytes(installed.read_bytes() + b"\n")

            reuse = target.plan()
            conflict = target.plan(competitor)
            preserved = installed.read_bytes()
            reinstalled = target.apply(reuse)
            journaled, journal_error = target._load_state()

            self.assertTrue(reuse.ready, reuse.errors)
            self.assertEqual("install", reuse.mode)
            self.assertEqual("complete", reinstalled.phase)
            self.assertIsNone(journal_error)
            self.assertEqual(reinstalled, journaled)
            self.assertEqual(preserved, installed.read_bytes())
            self.assertFalse(conflict.ready)
            self.assertTrue(
                any("omit --config" in error for error in conflict.errors)
            )

    def test_interrupted_upgrade_uninstall_preserves_old_payload_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source_v1 = copy_artifact(base, "0.4.0-dev")
            config = base / "config.json"
            write_valid_config(config)
            original, host, runner = self.make_installer(
                base, source_v1, [complete_probe(), complete_probe()]
            )
            v1_state = original.apply(
                original.plan(config),
                installer.InitialCredential(
                    "operator", "correct horse fixture battery"
                ),
            )
            preserved_config = host.resolve(
                "/etc/tinypirelay/media/config.json"
            ).read_bytes()
            source_v2 = copy_artifact(base, "0.5.0-dev")
            runner.fail_once = ("systemd-sysusers", "tinypirelay.conf")
            upgrading, _same_host, _same_runner = self.make_installer(
                base,
                source_v2,
                [complete_probe(), complete_probe()],
                runner=runner,
            )
            with self.assertRaises(installer.InstallError):
                upgrading.apply(upgrading.plan())
            interrupted, error = upgrading._load_state()
            self.assertIsNone(error)
            self.assertNotEqual("complete", interrupted.phase)
            self.assertEqual("0.5.0-dev", interrupted.target_version)
            self.assertEqual("0.4.0-dev", interrupted.installed_version)

            uninstalled = upgrading.uninstall(upgrading.plan_uninstall())
            journaled, journal_error = upgrading._load_state()

            self.assertIsNone(journal_error)
            self.assertEqual(uninstalled, journaled)
            self.assertEqual("0.4.0-dev", uninstalled.previous_version)
            self.assertEqual(v1_state.source_sha256, uninstalled.source_sha256)
            self.assertIsNone(uninstalled.previous_source_sha256)

            reinstalling, _same_host, _same_runner = self.make_installer(
                base,
                source_v1,
                [complete_probe(), complete_probe()],
                runner=runner,
            )
            reinstall_plan = reinstalling.plan()
            reinstalled = reinstalling.apply(reinstall_plan)

            self.assertTrue(reinstall_plan.ready, reinstall_plan.errors)
            self.assertEqual("complete", reinstalled.phase)
            self.assertEqual("0.4.0-dev", reinstalled.installed_version)
            self.assertEqual(v1_state.source_sha256, reinstalled.source_sha256)
            self.assertEqual(
                preserved_config,
                host.resolve("/etc/tinypirelay/media/config.json").read_bytes(),
            )

    def test_owner_verification_failure_stops_before_service_enable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = copy_artifact(base, "0.4.0-dev")
            config = base / "config.json"
            write_valid_config(config)
            runner = FakeRunner()
            host = NeverOwnedHost(base / "root", runner)
            probes = [complete_probe(), complete_probe()]
            target = installer.Installer(
                host,
                source,
                lambda: probes.pop(0),
                port_checker=lambda _port: None,
                tty_checker=lambda: True,
                privileged_checker=lambda: True,
            )

            with self.assertRaisesRegex(installer.InstallError, "ownership verification"):
                target.apply(
                    target.plan(config),
                    installer.InitialCredential(
                        "operator", "correct horse fixture battery"
                    ),
                )

            self.assertFalse(
                any(call[:2] == ("systemctl", "enable") for call in runner.calls)
            )

    @unittest.skipUnless(os.name == "posix", "symbolic-link fixture needs POSIX")
    def test_managed_symlink_prefix_and_tree_target_are_never_followed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "root"
            outside = base / "outside"
            outside.mkdir()
            sentinel = outside / "keep"
            sentinel.write_text("keep", encoding="utf-8")
            (root / "etc").mkdir(parents=True)
            os.symlink(outside, root / "etc" / "tinypirelay")
            host = installer.LocalHost(root, FakeRunner())

            with self.assertRaisesRegex(installer.InstallError, "symbolic link"):
                host.write_atomic(
                    "/etc/tinypirelay/media/config.json", b"unsafe", 0o600
                )
            self.assertEqual("keep", sentinel.read_text(encoding="utf-8"))

            (root / "opt").mkdir()
            os.symlink(outside, root / "opt" / "tinypirelay")
            with self.assertRaisesRegex(installer.InstallError, "symbolic link"):
                host.remove_tree("/opt/tinypirelay")
            self.assertTrue(sentinel.exists())

    @unittest.skipUnless(os.name == "posix", "symbolic-link fixture needs POSIX")
    def test_supplied_config_symlink_is_rejected_without_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = copy_artifact(base, "0.4.0-dev")
            real = base / "config.json"
            write_valid_config(real)
            linked = base / "linked.json"
            os.symlink(real, linked)
            target, host, _runner = self.make_installer(
                base, source, [complete_probe()]
            )

            plan = target.plan(linked)

            self.assertFalse(plan.ready)
            self.assertTrue(any("symlink" in error for error in plan.errors))
            self.assertEqual([], host.events)

    @unittest.skipUnless(os.name == "posix", "special-file fixture needs POSIX")
    def test_payload_fifo_is_rejected_in_plan_and_release_modes_are_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = copy_artifact(base, "0.4.0-dev")
            fifo = source / "src/tinypirelay/blocked.fifo"
            os.mkfifo(fifo)
            config = base / "config.json"
            write_valid_config(config)
            target, host, _runner = self.make_installer(
                base, source, [complete_probe()]
            )

            plan = target.plan(config)

            self.assertFalse(plan.ready)
            self.assertTrue(any("non-regular" in error for error in plan.errors))
            self.assertEqual([], host.events)
            fifo.unlink()
            writable = source / "src/tinypirelay/__init__.py"
            os.chmod(writable, 0o666)
            fingerprint = installer.validate_payload_tree(source)
            host.mkdir("/opt/tinypirelay/releases", 0o755)
            host.copy_release(
                source, "0.4.0-dev", expected_sha256=fingerprint
            )
            installed = host.resolve(
                "/opt/tinypirelay/releases/0.4.0-dev/src/tinypirelay/__init__.py"
            )
            self.assertEqual(0o644, stat_mode(installed))

    def test_tty_password_prompts_are_pinned_to_open_tty(self) -> None:
        class FakeInput:
            def __init__(self) -> None:
                self.lines = iter(("operator\n",))

            def __enter__(self) -> "FakeInput":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def readline(self) -> str:
                return next(self.lines)

        class FakeOutput:
            def __init__(self) -> None:
                self.writes: list[str] = []
                self.flushed = False

            def __enter__(self) -> "FakeOutput":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def write(self, value: str) -> None:
                self.writes.append(value)

            def flush(self) -> None:
                self.flushed = True

        tty_input = FakeInput()
        tty_output = FakeOutput()
        seen = 0

        def password_prompt(_prompt: str, *, stream: object) -> str:
            nonlocal seen
            self.assertIs(tty_output, stream)
            self.assertIs(tty_input, sys.stdin)
            seen += 1
            return "correct horse fixture battery"

        def open_tty(_path: str, mode: str, **_kwargs: object) -> object:
            if mode == "r":
                return tty_input
            if mode == "w":
                return tty_output
            raise io.UnsupportedOperation("terminal update streams are not seekable")

        attacker = io.StringIO("attacker-password")
        with (
            mock.patch("builtins.open", side_effect=open_tty),
            mock.patch.object(sys, "stdin", attacker),
        ):
            value = installer.credential_from_tty(
                "/dev/tty", password_prompt=password_prompt
            )

        self.assertEqual("operator", value.username)
        self.assertEqual(2, seen)
        self.assertEqual(["TinyPiRelay web username: "], tty_output.writes)
        self.assertTrue(tty_output.flushed)

    @unittest.skipUnless(hasattr(os, "openpty"), "POSIX pseudo-terminal required")
    def test_tty_credentials_support_a_real_nonseekable_terminal(self) -> None:
        master_fd, slave_fd = os.openpty()
        try:
            tty_path = os.ttyname(slave_fd)
            os.write(master_fd, b"operator\n")
            value = installer.credential_from_tty(
                tty_path,
                password_prompt=lambda _prompt, *, stream: (
                    "correct horse fixture battery"
                ),
            )
            os.set_blocking(master_fd, False)
            output = bytearray()
            while True:
                try:
                    output.extend(os.read(master_fd, 4096))
                except BlockingIOError:
                    break
        finally:
            os.close(master_fd)
            os.close(slave_fd)

        self.assertEqual("operator", value.username)
        self.assertIn(b"TinyPiRelay web username: ", output)

    def test_apt_simulation_rejects_removal_or_change_to_installed_package(self) -> None:
        cases = (
            "Remv old [1]\n0 upgraded, 0 newly installed, 1 to remove.\n",
            "Inst pkg [1] (2 fixture)\n1 upgraded, 0 newly installed, 0 to remove.\n",
        )
        for output in cases:
            with self.subTest(output=output), self.assertRaises(installer.InstallError):
                installer.parse_apt_simulation(output, ("pkg=2",))

    def test_inherited_credential_fd_and_tty_do_not_use_stdin(self) -> None:
        handle = tempfile.NamedTemporaryFile(delete=False)
        credential_path = Path(handle.name)
        handle.write(
            json.dumps(
                {
                    "username": "operator",
                    "password": "correct horse fixture battery",
                }
            ).encode()
        )
        handle.close()
        os.chmod(credential_path, 0o600)
        read_fd = os.open(credential_path, os.O_RDONLY)
        try:
            previous = sys.stdin
            sys.stdin = io.StringIO("attacker-input")
            try:
                value = installer.credential_from_fd(read_fd)
            finally:
                sys.stdin = previous
            self.assertEqual("operator", value.username)
            self.assertNotIn(value.password, repr(value))
        finally:
            os.close(read_fd)
            credential_path.unlink()

        read_fd, write_fd = os.pipe()
        try:
            with self.assertRaisesRegex(installer.InstallError, "regular file"):
                installer.credential_from_fd(read_fd)
        finally:
            os.close(read_fd)
            os.close(write_fd)


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def hashlib_file(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
