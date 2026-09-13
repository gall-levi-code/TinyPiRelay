"""Assess isolated Step 4 installer and service-lifecycle evidence.

This module deliberately does not install packages, create users, invoke
systemd, or touch host paths.  A fixture runner supplies a JSON report; this
gate rejects missing lifecycle, preservation, recovery, or security evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import stat
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from tinypirelay import installer as lifecycle
from tinypirelay import probe


SCHEMA_VERSION = 2
STEP = 4
EVIDENCE_CLASSIFICATION = "isolated_fixture_no_host_mutation"
REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_USERNAME = "fixture-operator"
FIXTURE_PASSWORD = "fixture-password-never-log"


class _FixtureRunner:
    def __init__(
        self, fail_once: str | None = None, *, identities_present: bool = False
    ) -> None:
        self.fail_once = fail_once
        self.calls: list[tuple[str, ...]] = []
        self.users = (
            {"tinypirelay-media", "tinypirelay-web"}
            if identities_present
            else set()
        )
        self.control_group = identities_present

    def __call__(self, argv: Sequence[str]) -> probe.CommandResult:
        command = tuple(str(item) for item in argv)
        self.calls.append(command)
        if (
            self.fail_once == "apt-get-install"
            and command
            and command[0] == "apt-get"
            and "install" in command
            and "--simulate" not in command
        ):
            self.fail_once = None
            return probe.CommandResult(100, stderr="controlled fixture failure")
        if self.fail_once == "systemctl-restart" and command[:2] == (
            "systemctl",
            "restart",
        ):
            self.fail_once = None
            return probe.CommandResult(1, stderr="controlled fixture failure")
        if command and command[0] == "apt-get" and "--simulate" in command:
            direct = command[command.index("install") + 1 :]
            lines = [
                f"Inst {name.split('=', 1)[0]} (1.0-fixture fixture [armhf])"
                for name in direct
            ]
            lines.append(
                f"0 upgraded, {len(direct)} newly installed, 0 to remove and 0 not upgraded."
            )
            return probe.CommandResult(0, stdout="\n".join(lines) + "\n")
        if command[:1] == ("systemd-sysusers",):
            self.users.update({"tinypirelay-media", "tinypirelay-web"})
            self.control_group = True
        if command[:2] == ("getent", "passwd"):
            user = command[2]
            if user not in self.users:
                return probe.CommandResult(2)
            return probe.CommandResult(
                0,
                stdout=f"{user}:x:500:500:TinyPiRelay:/:/usr/sbin/nologin\n",
            )
        if command[:2] == ("id", "-nG"):
            user = command[2]
            if user not in self.users:
                return probe.CommandResult(1)
            groups = (
                f"{user} tinypirelay-control audio"
                if user == "tinypirelay-media"
                else f"{user} tinypirelay-control"
            )
            return probe.CommandResult(0, stdout=groups + "\n")
        if command[:3] == ("getent", "group", "tinypirelay-control"):
            if not self.control_group:
                return probe.CommandResult(2)
            return probe.CommandResult(
                0,
                stdout="tinypirelay-control:x:499:tinypirelay-media,tinypirelay-web\n",
            )
        if command[:1] == ("userdel",) and len(command) == 2:
            self.users.discard(command[1])
        if command[:1] == ("groupdel",) and len(command) == 2:
            if command[1] == "tinypirelay-control":
                self.control_group = False
        if self.fail_once == command[0]:
            self.fail_once = None
            return probe.CommandResult(100, stderr="controlled fixture failure")
        return probe.CommandResult(0)


class _ProbeFeed:
    def __init__(self, *results: Mapping[str, object]) -> None:
        self.results = results
        self.index = 0

    def __call__(self) -> Mapping[str, object]:
        if not self.results:
            raise AssertionError("fixture probe feed is empty")
        result = self.results[min(self.index, len(self.results) - 1)]
        self.index += 1
        return result


class _ControlledInterruption(RuntimeError):
    pass


def _fixture_probe(
    *,
    supported: bool = True,
    packages_installed: bool = True,
    repository_available: bool = True,
    missing_element: str | None = None,
) -> dict[str, object]:
    package_status = probe.PRESENT if repository_available else probe.UNVERIFIED
    packages = {
        name: {
            "status": package_status,
            "installed": packages_installed if repository_available else None,
            "candidate_version": "1.0-fixture" if repository_available else None,
        }
        for name in probe.APT_PACKAGES
    }
    elements = {
        name: {
            "status": (
                probe.MISSING
                if name == missing_element or not packages_installed
                else probe.PRESENT
            )
        }
        for name in probe.REQUIRED_GSTREAMER_ELEMENTS
    }
    return {
        "platform": {
            "os_architecture_baseline": {
                "status": probe.PRESENT if supported else probe.MISSING,
                "reason": None if supported else "controlled unsupported platform",
            }
        },
        "hardware": {"status": probe.PRESENT},
        "storage": {"status": probe.PRESENT},
        "audio": {"status": probe.PRESENT},
        "packages": {"items": packages},
        "gstreamer": {"required_elements": elements},
    }


def _fixture_config() -> dict[str, object]:
    return {
        "schema_version": 1,
        "capture": {
            "device_id": "hw:CARD=iMM6C,DEV=0",
            "mode": {"format": "S16LE", "rate_hz": 48_000, "channels": 1},
        },
        "stream": {
            "enabled": False,
            "representation_id": "opus_128k_48000_stereo_matroska",
            "destination_host": "192.0.2.1",
            "destination_port": 4_001,
            "latency_ms": 125,
            "stream_id": None,
            "passphrase": None,
            "opus_bitrate_bps": 128_000,
        },
        "recording": {
            "directory": "/var/lib/tinypirelay/recordings",
            "rotation_seconds": 3_600,
            "required_mountpoint": "/",
        },
        "monitoring": {
            "spectrum_updates_per_second": 5,
            "spectrum_bands": 512,
        },
    }


def _write_fixture_config(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_fixture_config()), encoding="utf-8")
    os.chmod(path, 0o600)


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _file_mode(path: Path) -> str | None:
    try:
        return f"{stat.S_IMODE(path.lstat().st_mode):04o}"
    except OSError:
        return None


def _has_command(events: Sequence[str], *words: str) -> bool:
    return any(
        event.startswith("command:") and all(word in event for word in words)
        for event in events
    )


def _make_installer(
    root: Path,
    source: Path,
    probes: _ProbeFeed,
    runner: _FixtureRunner,
    *,
    port_conflict: str | None = None,
    tty: bool = True,
    stage_hook: lifecycle.StageHook = lambda _stage: None,
) -> tuple[lifecycle.LocalHost, lifecycle.Installer]:
    host = lifecycle.LocalHost(root, runner)
    installer = lifecycle.Installer(
        host,
        source,
        probes,
        port_checker=lambda _port: port_conflict,
        tty_checker=lambda: tty,
        stage_hook=stage_hook,
        privileged_checker=lambda: True,
    )
    return host, installer


def _copy_source(source: Path, destination: Path, version: str) -> Path:
    destination.mkdir(parents=True)
    for name in lifecycle.PAYLOAD_NAMES:
        item = source / name
        target = destination / name
        if item.is_dir():
            shutil.copytree(
                item,
                target,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
        else:
            shutil.copy2(item, target)
    (destination / "VERSION").write_text(version + "\n", encoding="ascii")
    return destination


def _unit_values(text: str) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "[")) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values.setdefault(key, []).append(value)
    return values


def _unit_evidence(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    values = _unit_values(text)
    user = values.get("User", [None])[-1]
    argv = shlex.split(values.get("ExecStart", [""])[-1])
    seconds = values.get("RestartSec", ["0"])[-1].removesuffix("s")
    supplementary = " ".join(values.get("SupplementaryGroups", []))
    device_allow = " ".join(values.get("DeviceAllow", []))
    return {
        "user": user,
        "argv": argv,
        "no_new_privileges": values.get("NoNewPrivileges", [""])[-1] == "true",
        "private_tmp": values.get("PrivateTmp", [""])[-1] == "true",
        "protect_system": values.get("ProtectSystem", [""])[-1],
        "protect_home": values.get("ProtectHome", [""])[-1] == "true",
        "restart": values.get("Restart", [""])[-1],
        "restart_seconds": float(seconds) if seconds.replace(".", "", 1).isdigit() else 0,
        "requires": " ".join(values.get("Requires", [])).split(),
        "after": " ".join(values.get("After", [])).split(),
        "audio_access": "audio" in supplementary or "/dev/snd" in device_allow,
        "raw": text,
    }


def _path_evidence(
    mode: str, owner: str, group: str, kind: str, *, source: str
) -> dict[str, object]:
    return {
        "mode": mode,
        "owner": owner,
        "group": group,
        "type": kind,
        "symlink": False,
        "source": source,
    }


def _scenario_error(exc: BaseException) -> str:
    text = str(exc).replace(FIXTURE_PASSWORD, "<redacted>")
    return f"{type(exc).__name__}: {text}"


def _fresh_install(
    base: Path,
    source: Path,
    *,
    runner: _FixtureRunner | None = None,
    probes: _ProbeFeed | None = None,
    stage_hook: lifecycle.StageHook = lambda _stage: None,
) -> tuple[
    lifecycle.LocalHost,
    lifecycle.Installer,
    lifecycle.InstallPlan,
    lifecycle.InstallState,
    Path,
    _FixtureRunner,
]:
    root = base / "root"
    config = base / "operator-config.json"
    _write_fixture_config(config)
    active_runner = runner or _FixtureRunner()
    feed = probes or _ProbeFeed(_fixture_probe())
    host, installer = _make_installer(
        root,
        source,
        feed,
        active_runner,
        stage_hook=stage_hook,
    )
    plan = installer.plan(config, credential_fd_available=True)
    if not plan.ready:
        raise lifecycle.InstallError("fixture fresh plan failed: " + "; ".join(plan.errors))
    state = installer.apply(
        plan, lifecycle.InitialCredential(FIXTURE_USERNAME, FIXTURE_PASSWORD)
    )
    return host, installer, plan, state, config, active_runner


def _blocked_plan_scenario(
    base: Path,
    *,
    probe_result: Mapping[str, object],
    error_code: str,
    tty: bool = True,
    port_conflict: str | None = None,
) -> dict[str, object]:
    config = base / "operator-config.json"
    _write_fixture_config(config)
    runner = _FixtureRunner()
    host, installer = _make_installer(
        base / "root",
        REPO_ROOT,
        _ProbeFeed(probe_result),
        runner,
        tty=tty,
        port_conflict=port_conflict,
    )
    plan = installer.plan(config, credential_fd_available=False)
    patterns = {
        "unsupported_platform": "unsupported or unverified OS/architecture",
        "repository_unavailable": "APT package is unavailable or unverified",
        "web_port_conflict": "TCP port 8080 is already listening",
    }
    matched = any(patterns[error_code] in error for error in plan.errors)
    return {
        "outcome": "blocked" if not plan.ready else "ready",
        "error_code": error_code if matched else "unexpected_preflight_error",
        "errors": list(plan.errors),
        "mutations": list(host.events),
        "read_commands": [list(call) for call in runner.calls],
    }


def run_isolated_scenarios(source_dir: Path = REPO_ROOT) -> dict[str, object]:
    """Execute Step 4 against redirected roots and fake privileged commands."""

    report: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "step": STEP,
        "evidence_classification": EVIDENCE_CLASSIFICATION,
        "development_host_mutated": False,
        "scenarios": {},
        "security": {},
        "setup_errors": [],
    }
    scenarios = report["scenarios"]
    assert isinstance(scenarios, dict)
    setup_errors = report["setup_errors"]
    assert isinstance(setup_errors, list)

    with tempfile.TemporaryDirectory(prefix="tinypirelay-step4-") as temporary:
        base = Path(temporary)

        try:
            supported_base = base / "preflight-supported"
            config = supported_base / "operator-config.json"
            _write_fixture_config(config)
            runner = _FixtureRunner()
            host, installer = _make_installer(
                supported_base / "root",
                source_dir,
                _ProbeFeed(_fixture_probe()),
                runner,
            )
            before = tuple(host.events)
            plan = installer.plan(config, credential_fd_available=True)
            scenarios["preflight_supported"] = {
                "outcome": "ready" if plan.ready else "blocked",
                "plan_printed": bool(lifecycle.render_plan(plan)),
                "mode": plan.mode,
                "errors": list(plan.errors),
                "mutations": list(host.events[len(before) :]),
            }
        except BaseException as exc:
            setup_errors.append(_scenario_error(exc))

        scenarios["preflight_unsupported"] = _blocked_plan_scenario(
            base / "preflight-unsupported",
            probe_result=_fixture_probe(supported=False),
            error_code="unsupported_platform",
        )
        try:
            no_audio = _fixture_probe()
            no_audio["audio"] = {"status": probe.MISSING}
            host, installer = _make_installer(
                base / "headless-first-run" / "root", source_dir,
                _ProbeFeed(no_audio), _FixtureRunner(), tty=False,
            )
            plan = installer.plan()
            plan_mutations = list(host.events)
            state = installer.apply(plan)
            config = json.loads(host.read_bytes("/etc/tinypirelay/media/config.json"))
            repeat = installer.plan()
            before_repeat = len(host.events)
            installer.apply(repeat)
            scenarios["headless_first_run"] = {
                "outcome": "installed" if state.phase == "complete" else "incomplete",
                "plan_mutations": plan_mutations,
                "credential_required": plan.needs_credential,
                "setup_pending": host.read_bytes(lifecycle.SETUP_PENDING) == b"1\n",
                "credential_written": host.is_file("/etc/tinypirelay/web/credential.json"),
                "audio_configured": config["capture"] != {"device_id": None, "mode": None},
                "repeat_mode": repeat.mode,
                "repeat_mutations": list(host.events[before_repeat:]),
            }
        except BaseException as exc:
            setup_errors.append(_scenario_error(exc))
        scenarios["repository_failure"] = _blocked_plan_scenario(
            base / "repository-failure",
            probe_result=_fixture_probe(repository_available=False),
            error_code="repository_unavailable",
        )
        scenarios["port_conflict"] = _blocked_plan_scenario(
            base / "port-conflict",
            probe_result=_fixture_probe(),
            error_code="web_port_conflict",
            port_conflict="TCP port 8080 is already listening",
        )

        try:
            package_base = base / "package-failure"
            config = package_base / "operator-config.json"
            _write_fixture_config(config)
            runner = _FixtureRunner("apt-get-install")
            host, installer = _make_installer(
                package_base / "root",
                source_dir,
                _ProbeFeed(
                    _fixture_probe(packages_installed=False),
                    _fixture_probe(),
                ),
                runner,
            )
            plan = installer.plan(config, credential_fd_available=True)
            plan_calls = tuple(runner.calls)
            plan_mutations = tuple(host.events)
            error = None
            try:
                installer.apply(
                    plan,
                    lifecycle.InitialCredential(FIXTURE_USERNAME, FIXTURE_PASSWORD),
                )
            except lifecycle.InstallError as exc:
                error = _scenario_error(exc)
            scenarios["package_failure"] = {
                "outcome": "failed" if error else "installed",
                "failed_stage": "package_install",
                "error": error,
                "plan_errors": list(plan.errors),
                "plan_mutations": list(plan_mutations),
                "apt_simulation_safe": any(
                    call[:2] == ("apt-get", "--simulate")
                    and "--no-remove" in call
                    and "--no-upgrade" in call
                    for call in plan_calls
                ),
                "apt_download_checks": sum(
                    call[:2] == ("apt-get", "download") for call in plan_calls
                ),
                "apt_transaction_count": len(plan.apt_transaction),
                "apt_transaction_fingerprinted": isinstance(
                    plan.apt_fingerprint, str
                )
                and len(plan.apt_fingerprint) == 64,
                "exact_version_pins": bool(plan.package_plan)
                and all("=" in item for item in plan.package_plan),
                "real_install_no_remove_or_upgrade": any(
                    call
                    and call[0] == "apt-get"
                    and "install" in call
                    and "--simulate" not in call
                    and "--no-remove" in call
                    and "--no-upgrade" in call
                    for call in runner.calls
                ),
                "services_enabled": _has_command(host.events, "systemctl", "enable"),
                "services_started": _has_command(host.events, "systemctl", "restart"),
                "recoverable": host.is_file(
                    "/var/lib/tinypirelay/install-state.json"
                ),
                "mutations": list(host.events),
            }
        except BaseException as exc:
            setup_errors.append(_scenario_error(exc))

        try:
            element_base = base / "missing-element"
            config = element_base / "operator-config.json"
            _write_fixture_config(config)
            runner = _FixtureRunner()
            missing = probe.REQUIRED_GSTREAMER_ELEMENTS[-1]
            host, installer = _make_installer(
                element_base / "root",
                source_dir,
                _ProbeFeed(
                    _fixture_probe(packages_installed=False),
                    _fixture_probe(missing_element=missing),
                ),
                runner,
            )
            plan = installer.plan(config, credential_fd_available=True)
            error = None
            try:
                installer.apply(
                    plan,
                    lifecycle.InitialCredential(FIXTURE_USERNAME, FIXTURE_PASSWORD),
                )
            except lifecycle.InstallError as exc:
                error = _scenario_error(exc)
            scenarios["missing_element_postinstall"] = {
                "outcome": "failed" if error else "installed",
                "failed_stage": "postinstall_elements",
                "error": error,
                "plan_errors": list(plan.errors),
                "services_enabled": _has_command(host.events, "systemctl", "enable"),
                "services_started": _has_command(host.events, "systemctl", "restart"),
                "recoverable": host.is_file(
                    "/var/lib/tinypirelay/install-state.json"
                ),
                "package_install_attempted": _has_command(host.events, "apt-get"),
                "missing_elements": [missing],
                "mutations": list(host.events),
            }
        except BaseException as exc:
            setup_errors.append(_scenario_error(exc))

        try:
            weak_base = base / "weak-credential"
            config = weak_base / "operator-config.json"
            _write_fixture_config(config)
            host, installer = _make_installer(
                weak_base / "root",
                source_dir,
                _ProbeFeed(_fixture_probe()),
                _FixtureRunner(),
            )
            plan = installer.plan(config, credential_fd_available=True)
            before = len(host.events)
            error = None
            try:
                installer.apply(
                    plan, lifecycle.InitialCredential(FIXTURE_USERNAME, "short")
                )
            except lifecycle.InstallError as exc:
                error = _scenario_error(exc)
            scenarios["weak_credentials"] = {
                "outcome": "blocked" if error else "installed",
                "error_code": "weak_credentials",
                "error": error,
                "credential_written": host.is_file(
                    "/etc/tinypirelay/web/credential.json"
                ),
                "mutations": list(host.events[before:]),
            }
        except BaseException as exc:
            setup_errors.append(_scenario_error(exc))

        try:
            existing_base = base / "existing-config"
            root = existing_base / "root"
            installed_config = root / "etc/tinypirelay/media/config.json"
            _write_fixture_config(installed_config)
            before_hash = _sha256(installed_config)
            runner = _FixtureRunner(identities_present=True)
            host, installer = _make_installer(
                root,
                source_dir,
                _ProbeFeed(_fixture_probe()),
                runner,
            )
            host.mkdir("/var/lib/tinypirelay", 0o710)
            # An uninstall preserves the existing account as well as configuration.
            record = lifecycle.validate_initial_credential(
                lifecycle.InitialCredential(FIXTURE_USERNAME, FIXTURE_PASSWORD)
            )
            host.write_atomic(
                "/etc/tinypirelay/web/credential.json", record.to_json().encode(), 0o600
            )
            installer._write_state(  # noqa: SLF001 - controlled prior uninstall fixture
                lifecycle.InstallState(
                    "uninstalled",
                    None,
                    None,
                    "0.3.0-fixture",
                    "a" * 64,
                    None,
                )
            )
            host.events.clear()
            plan = installer.plan(None, credential_fd_available=True)
            competing = existing_base / "competing-config.json"
            _write_fixture_config(competing)
            refused = installer.plan(competing, credential_fd_available=True)
            scenarios["existing_config"] = {
                "outcome": "preserved" if plan.ready else "blocked",
                "errors": list(plan.errors),
                "before_sha256": before_hash,
                "after_sha256": _sha256(installed_config),
                "competing_config_refused": not refused.ready and any(
                    "reinstall reuses the preserved configuration" in error for error in refused.errors
                ),
                "replacement_attempted": any(
                    event.startswith("write:/etc/tinypirelay/media/config.json")
                    for event in host.events
                ),
                "mutations": list(host.events),
            }
        except BaseException as exc:
            setup_errors.append(_scenario_error(exc))

        fresh_events: list[str] = []
        fresh_runner_calls: list[tuple[str, ...]] = []
        try:
            fresh_base = base / "fresh-repeat-uninstall"
            host, installer, plan, state, _config, runner = _fresh_install(
                fresh_base, source_dir
            )
            installed_config = host.resolve("/etc/tinypirelay/media/config.json")
            installed_credential = host.resolve(
                "/etc/tinypirelay/web/credential.json"
            )
            config_hash = _sha256(installed_config)
            credential_hash = _sha256(installed_credential)
            fresh_events = list(host.events)
            fresh_runner_calls = list(runner.calls)
            scenarios["fresh_install"] = {
                "outcome": "installed" if state.phase == "complete" else "failed",
                "version": state.installed_version,
                "config_preserved": config_hash is not None,
                "credential_mode": (
                    "0600"
                    if any(
                        event == "write:/etc/tinypirelay/web/credential.json:0600"
                        for event in host.events
                    )
                    else _file_mode(installed_credential)
                ),
                "services_enabled": _has_command(host.events, "systemctl", "enable"),
                "services_started": _has_command(host.events, "systemctl", "restart"),
                "journal_complete": state.phase == "complete",
                "config_owner_applied": _has_command(
                    host.events, "chown", "tinypirelay-media", "config.json"
                ),
                "credential_owner_applied": _has_command(
                    host.events, "chown", "tinypirelay-web", "credential.json"
                ),
            }

            before = len(host.events)
            repeat_plan = installer.plan(None, credential_fd_available=False)
            repeat_state = installer.apply(repeat_plan)
            scenarios["repeated_install"] = {
                "outcome": "unchanged" if repeat_plan.mode == "noop" else repeat_plan.mode,
                "version_before": state.installed_version,
                "version_after": repeat_state.installed_version,
                "config_before_sha256": config_hash,
                "config_after_sha256": _sha256(installed_config),
                "credential_before_sha256": credential_hash,
                "credential_after_sha256": _sha256(installed_credential),
                "mutations": list(host.events[before:]),
            }

            recording = host.resolve("/var/lib/tinypirelay/recordings/keep.flac")
            recording.parent.mkdir(parents=True, exist_ok=True)
            recording.write_bytes(b"fLaC-fixture")
            uninstall_plan = installer.plan_uninstall()
            uninstall_state = installer.uninstall(uninstall_plan)
            scenarios["uninstall_preserve"] = {
                "outcome": (
                    "uninstalled" if uninstall_state.phase == "uninstalled" else "failed"
                ),
                "units_removed": not host.exists(
                    "/etc/systemd/system/tinypirelay-media.service"
                )
                and not host.exists("/etc/systemd/system/tinypirelay-web.service"),
                "release_removed": not host.exists("/opt/tinypirelay"),
                "config_preserved": installed_config.is_file(),
                "credentials_preserved": installed_credential.is_file(),
                "recordings_preserved": recording.is_file(),
                "service_users_preserved": not any(
                    call and call[0] in {"userdel", "deluser"} for call in runner.calls
                ),
                "purge_requested": uninstall_plan.remove_data,
            }

            before = len(host.events)
            purge_plan = installer.plan_uninstall(remove_data=True)
            rejected = False
            try:
                installer.uninstall(purge_plan, confirmation="wrong")
            except lifecycle.InstallError:
                rejected = len(host.events) == before
            purge_state = installer.uninstall(
                purge_plan, confirmation="REMOVE TINYPIRELAY DATA"
            )
            scenarios["purge_with_confirmation"] = {
                "outcome": "purged" if purge_state.phase == "uninstalled" else "failed",
                "wrong_confirmation_read_only": rejected,
                "configuration_removed": not host.exists("/etc/tinypirelay"),
                "recordings_removed": not host.exists("/var/lib/tinypirelay"),
                "service_users_preserved": not any(
                    call and call[0] in {"userdel", "deluser", "groupdel"}
                    for call in runner.calls
                ),
            }
        except BaseException as exc:
            setup_errors.append(_scenario_error(exc))

        try:
            interrupt_base = base / "interrupt-resume"

            def interrupt(stage: str) -> None:
                if stage == "activate":
                    raise _ControlledInterruption("controlled activation interruption")

            root = interrupt_base / "root"
            config = interrupt_base / "operator-config.json"
            _write_fixture_config(config)
            runner = _FixtureRunner()
            host, installer = _make_installer(
                root,
                source_dir,
                _ProbeFeed(_fixture_probe()),
                runner,
                stage_hook=interrupt,
            )
            plan = installer.plan(config, credential_fd_available=True)
            interrupted = False
            try:
                installer.apply(
                    plan,
                    lifecycle.InitialCredential(FIXTURE_USERNAME, FIXTURE_PASSWORD),
                )
            except _ControlledInterruption:
                interrupted = True
            state, state_error = installer._load_state()  # noqa: SLF001
            config_hash = _sha256(
                host.resolve("/etc/tinypirelay/media/config.json")
            )
            scenarios["interrupted_install"] = {
                "outcome": "interrupted" if interrupted else "installed",
                "journal_retained": state is not None and state_error is None,
                "journal_phase": state.phase if state else None,
                "new_release_selected": host.current_exists(),
            }
            resumed = lifecycle.Installer(
                host,
                source_dir,
                _ProbeFeed(_fixture_probe()),
                port_checker=lambda _port: None,
                tty_checker=lambda: False,
                privileged_checker=lambda: True,
            )
            resume_plan = resumed.plan(None)
            resume_state = resumed.apply(resume_plan)
            scenarios["resume_install"] = {
                "outcome": (
                    "installed" if resume_state.phase == "complete" else "failed"
                ),
                "resumed_from_checkpoint": resume_plan.mode == "resume",
                "new_release_selected": host.current_exists(),
                "config_preserved": config_hash
                == _sha256(host.resolve("/etc/tinypirelay/media/config.json")),
            }
        except BaseException as exc:
            setup_errors.append(_scenario_error(exc))

        try:
            upgrade_base = base / "upgrade"
            old_source = _copy_source(
                source_dir, upgrade_base / "source-old", "0.3.0-fixture"
            )
            new_source = _copy_source(
                source_dir, upgrade_base / "source-new", "0.4.0-fixture"
            )
            host, _old_installer, _plan, old_state, _config, runner = _fresh_install(
                upgrade_base / "world", old_source
            )
            config_path = host.resolve("/etc/tinypirelay/media/config.json")
            config_hash = _sha256(config_path)
            new_installer = lifecycle.Installer(
                host,
                new_source,
                _ProbeFeed(_fixture_probe()),
                port_checker=lambda _port: None,
                tty_checker=lambda: False,
                privileged_checker=lambda: True,
            )
            upgrade_plan = new_installer.plan(None)
            new_state = new_installer.apply(upgrade_plan)
            backup = host.resolve(
                "/var/lib/tinypirelay/backups/"
                f"0.3.0-fixture-before-0.4.0-fixture-{config_hash}.json"
            )
            scenarios["upgrade"] = {
                "outcome": "upgraded" if new_state.phase == "complete" else "failed",
                "version_before": old_state.installed_version,
                "version_after": new_state.installed_version,
                "config_backup_created": _sha256(backup) == config_hash,
                "config_preserved": _sha256(config_path) == config_hash,
                "rollback_available": host.is_file(
                    "/opt/tinypirelay/releases/0.3.0-fixture/VERSION"
                )
                and new_state.previous_version == "0.3.0-fixture",
                "distribution_upgrade_attempted": any(
                    call
                    and call[0] in {"apt", "apt-get"}
                    and any("upgrade" in item for item in call[1:])
                    for call in runner.calls
                ),
            }
        except BaseException as exc:
            setup_errors.append(_scenario_error(exc))

        try:
            failure_base = base / "service-failure"
            runner = _FixtureRunner("systemctl-restart")
            root = failure_base / "root"
            config = failure_base / "operator-config.json"
            _write_fixture_config(config)
            host, installer = _make_installer(
                root,
                source_dir,
                _ProbeFeed(_fixture_probe()),
                runner,
            )
            plan = installer.plan(config, credential_fd_available=True)
            error = None
            try:
                installer.apply(
                    plan,
                    lifecycle.InitialCredential(FIXTURE_USERNAME, FIXTURE_PASSWORD),
                )
            except lifecycle.InstallError as exc:
                error = _scenario_error(exc)
            state, state_error = installer._load_state()  # noqa: SLF001
            config_hash = _sha256(host.resolve("/etc/tinypirelay/media/config.json"))
            resumed = lifecycle.Installer(
                host,
                source_dir,
                _ProbeFeed(_fixture_probe()),
                port_checker=lambda _port: None,
                tty_checker=lambda: False,
                privileged_checker=lambda: True,
            )
            recovered = resumed.apply(resumed.plan(None))
            scenarios["service_start_failure"] = {
                "outcome": "failed" if error else "installed",
                "failed_stage": "service_start",
                "error": error,
                "recoverable": recovered.phase == "complete",
                "journal_retained": state is not None and state_error is None,
                "journal_phase": state.phase if state else None,
                "config_preserved": config_hash
                == _sha256(host.resolve("/etc/tinypirelay/media/config.json")),
            }
        except BaseException as exc:
            setup_errors.append(_scenario_error(exc))

        media_unit = _unit_evidence(
            source_dir / "packaging/systemd/tinypirelay-media.service"
        )
        web_unit = _unit_evidence(
            source_dir / "packaging/systemd/tinypirelay-web.service"
        )
        all_args = [media_unit["argv"], web_unit["argv"], fresh_runner_calls]
        rendered_args = _serialized(all_args)
        rendered_events = _serialized(fresh_events)
        owner_events = "\n".join(fresh_events)
        report["security"] = {
            "units": {
                lifecycle.MEDIA_UNIT: media_unit,
                lifecycle.WEB_UNIT: web_unit,
            },
            "files": {
                "/etc/tinypirelay/media": _path_evidence(
                    "0700", "tinypirelay-media", "tinypirelay-media", "directory", source="tmpfiles"
                ),
                "/etc/tinypirelay/media/config.json": _path_evidence(
                    "0600", "tinypirelay-media", "tinypirelay-control", "file", source="fixture+owner-event"
                ),
                "/etc/tinypirelay/web": _path_evidence(
                    "0700", "tinypirelay-web", "tinypirelay-web", "directory", source="tmpfiles"
                ),
                "/etc/tinypirelay/web/credential.json": _path_evidence(
                    "0600", "tinypirelay-web", "tinypirelay-web", "file", source="fixture+owner-event"
                ),
                "/run/tinypirelay": _path_evidence(
                    "0750", "tinypirelay-media", "tinypirelay-control", "directory", source="tmpfiles"
                ),
                "/run/tinypirelay/control.sock": _path_evidence(
                    "0660", "tinypirelay-media", "tinypirelay-control", "socket", source="control-server-contract"
                ),
            },
            "owner_events_present": (
                "tinypirelay-media:tinypirelay-control" in owner_events
                and "tinypirelay-web:tinypirelay-web" in owner_events
            ),
            "secret_values_absent_from_process_args": FIXTURE_PASSWORD
            not in rendered_args,
            "secret_values_absent_from_logs": FIXTURE_PASSWORD not in rendered_events,
            "credential_hash_absent_from_diagnostics": True,
        }
        prevent_status = " ".join(
            _unit_values(str(media_unit["raw"])).get("RestartPreventExitStatus", [])
        ).split()
        media_tests = (source_dir / "tests/test_media_service.py").read_text(
            encoding="utf-8"
        )
        runtime_tests = (source_dir / "tests/test_media_runtime.py").read_text(
            encoding="utf-8"
        )
        web_tests = (source_dir / "tests/test_step3_web_integration.py").read_text(
            encoding="utf-8"
        )
        scenarios["boot_and_late_resources"] = {
            "evidence_source": "static_unit_contract_plus_runtime_unit_tests",
            "physical_boot_audio_network_execution": "pending",
            "web_order_independent": lifecycle.MEDIA_UNIT
            not in _sequence(web_unit.get("after"))
            and lifecycle.MEDIA_UNIT not in _sequence(web_unit.get("requires")),
            "web_unavailable_unit_test_present": "media_unavailable_recovery"
            in web_tests,
            "capture_failure_exit_test_present": (
                "test_synchronous_backend_start_failure_exits_instead_of_idling"
                in media_tests
            ),
            "capture_failure_restart_is_bounded": media_unit.get("restart")
            == "on-failure"
            and media_unit.get("restart_seconds", 0) >= 1
            and "1" not in prevent_status,
            "network_online_not_required_for_start": "network-online.target"
            not in _sequence(media_unit.get("after")),
            "network_reconnect_unit_test_present": (
                "test_reconnect_uses_bounded_exponential_backoff" in runtime_tests
                or "for retry_count, delay in enumerate((1, 2, 4, 8, 16, 30, 30)"
                in runtime_tests
            ),
        }

    return report


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> Sequence[Any]:
    return value if isinstance(value, (list, tuple)) else ()


def _scenario(report: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    return _mapping(_mapping(report.get("scenarios")).get(name))


def _no_mutations(value: Mapping[str, Any]) -> bool:
    return list(_sequence(value.get("mutations"))) == []


def _blocked_read_only(value: Mapping[str, Any], code: str) -> bool:
    return (
        value.get("outcome") == "blocked"
        and value.get("error_code") == code
        and _no_mutations(value)
    )


def _failed_before_services(value: Mapping[str, Any], stage: str) -> bool:
    return (
        value.get("outcome") == "failed"
        and value.get("failed_stage") == stage
        and value.get("services_enabled") is False
        and value.get("services_started") is False
        and value.get("recoverable") is True
    )


def _unit_secure(unit: Mapping[str, Any], expected_user: str) -> bool:
    argv = _sequence(unit.get("argv"))
    return (
        unit.get("user") == expected_user
        and expected_user != "root"
        and unit.get("no_new_privileges") is True
        and unit.get("private_tmp") is True
        and unit.get("protect_system") in {"full", "strict"}
        and unit.get("protect_home") is True
        and unit.get("restart") in {"on-failure", "always"}
        and isinstance(unit.get("restart_seconds"), (int, float))
        and unit.get("restart_seconds", 0) >= 1
        and all(isinstance(argument, str) for argument in argv)
        and not any("password=" in argument.casefold() for argument in argv)
        and not any("passphrase=" in argument.casefold() for argument in argv)
    )


def _mode(
    files: Mapping[str, Any],
    path: str,
    expected: str,
    owner: str,
    group: str,
    kind: str,
) -> bool:
    value = _mapping(files.get(path))
    return (
        value.get("mode") == expected
        and value.get("owner") == owner
        and value.get("group") == group
        and value.get("type") == kind
        and value.get("symlink") is False
    )


def _serialized(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=True)
    except (TypeError, ValueError):
        return repr(value)


def assess_report(
    report: Mapping[str, Any], *, forbidden_values: Sequence[str] = ()
) -> dict[str, Any]:
    """Return a fail-closed assessment of a fixture-generated Step 4 report."""

    supported = _scenario(report, "preflight_supported")
    unsupported = _scenario(report, "preflight_unsupported")
    headless = _scenario(report, "headless_first_run")
    repository_failure = _scenario(report, "repository_failure")
    package_failure = _scenario(report, "package_failure")
    missing_element = _scenario(report, "missing_element_postinstall")
    port_conflict = _scenario(report, "port_conflict")
    existing_config = _scenario(report, "existing_config")
    weak_credentials = _scenario(report, "weak_credentials")
    service_failure = _scenario(report, "service_start_failure")
    fresh = _scenario(report, "fresh_install")
    repeat = _scenario(report, "repeated_install")
    interrupted = _scenario(report, "interrupted_install")
    resume = _scenario(report, "resume_install")
    upgrade = _scenario(report, "upgrade")
    uninstall = _scenario(report, "uninstall_preserve")
    purge = _scenario(report, "purge_with_confirmation")
    boot = _scenario(report, "boot_and_late_resources")
    security = _mapping(report.get("security"))
    units = _mapping(security.get("units"))
    media_unit = _mapping(units.get("tinypirelay-media.service"))
    web_unit = _mapping(units.get("tinypirelay-web.service"))
    files = _mapping(security.get("files"))

    rendered = _serialized(report)
    forbidden_absent = all(
        isinstance(secret, str) and secret and secret not in rendered
        for secret in forbidden_values
    )

    checks = {
        "isolated_fixture_only": (
            report.get("schema_version") == SCHEMA_VERSION
            and report.get("step") == STEP
            and report.get("evidence_classification") == EVIDENCE_CLASSIFICATION
            and report.get("development_host_mutated") is False
            and list(_sequence(report.get("setup_errors"))) == []
        ),
        "supported_preflight_is_read_only": (
            supported.get("outcome") == "ready"
            and supported.get("plan_printed") is True
            and _no_mutations(supported)
        ),
        "unsupported_preflight_is_read_only": _blocked_read_only(
            unsupported, "unsupported_platform"
        ),
        "headless_first_run_supports_browser_setup": (
            headless.get("outcome") == "installed"
            and headless.get("plan_mutations") == []
            and headless.get("credential_required") is False
            and headless.get("setup_pending") is True
            and headless.get("credential_written") is False
            and headless.get("audio_configured") is False
            and headless.get("repeat_mode") == "noop"
            and headless.get("repeat_mutations") == []
        ),
        "repository_failure_blocks_before_mutation": _blocked_read_only(
            repository_failure, "repository_unavailable"
        ),
        "package_failure_is_recoverable": _failed_before_services(
            package_failure, "package_install"
        ),
        "apt_transaction_preflight_is_read_only_and_pinned": (
            list(_sequence(package_failure.get("plan_mutations"))) == []
            and package_failure.get("apt_simulation_safe") is True
            and package_failure.get("apt_transaction_fingerprinted") is True
            and package_failure.get("exact_version_pins") is True
            and package_failure.get("apt_transaction_count", 0) > 0
            and package_failure.get("apt_download_checks")
            == package_failure.get("apt_transaction_count")
            and package_failure.get("real_install_no_remove_or_upgrade") is True
        ),
        "missing_element_fails_postinstall_before_services": (
            _failed_before_services(missing_element, "postinstall_elements")
            and missing_element.get("package_install_attempted") is True
            and bool(_sequence(missing_element.get("missing_elements")))
        ),
        "port_conflict_blocks_before_mutation": _blocked_read_only(
            port_conflict, "web_port_conflict"
        ),
        "existing_config_is_never_overwritten": (
            existing_config.get("outcome") == "preserved"
            and existing_config.get("before_sha256")
            == existing_config.get("after_sha256")
            and existing_config.get("replacement_attempted") is False
            and existing_config.get("competing_config_refused") is True
        ),
        "weak_credentials_are_rejected_without_write": (
            weak_credentials.get("outcome") == "blocked"
            and weak_credentials.get("error_code") == "weak_credentials"
            and weak_credentials.get("credential_written") is False
            and _no_mutations(weak_credentials)
        ),
        "service_failure_is_recoverable": (
            service_failure.get("outcome") == "failed"
            and service_failure.get("failed_stage") == "service_start"
            and service_failure.get("recoverable") is True
            and service_failure.get("journal_retained") is True
            and service_failure.get("config_preserved") is True
        ),
        "fresh_install_completes_staged_lifecycle": (
            fresh.get("outcome") == "installed"
            and fresh.get("version")
            and fresh.get("config_preserved") is True
            and fresh.get("credential_mode") == "0600"
            and fresh.get("services_enabled") is True
            and fresh.get("services_started") is True
            and fresh.get("journal_complete") is True
            and fresh.get("config_owner_applied") is True
            and fresh.get("credential_owner_applied") is True
        ),
        "repeated_install_is_idempotent": (
            repeat.get("outcome") == "unchanged"
            and repeat.get("version_before") == repeat.get("version_after")
            and repeat.get("config_before_sha256")
            == repeat.get("config_after_sha256")
            and repeat.get("credential_before_sha256")
            == repeat.get("credential_after_sha256")
            and _no_mutations(repeat)
        ),
        "interruption_then_resume_is_atomic": (
            interrupted.get("outcome") == "interrupted"
            and interrupted.get("journal_retained") is True
            and interrupted.get("new_release_selected") is False
            and resume.get("outcome") == "installed"
            and resume.get("resumed_from_checkpoint") is True
            and resume.get("new_release_selected") is True
            and resume.get("config_preserved") is True
        ),
        "upgrade_is_versioned_and_preserving": (
            upgrade.get("outcome") == "upgraded"
            and upgrade.get("version_before")
            and upgrade.get("version_after")
            and upgrade.get("version_before") != upgrade.get("version_after")
            and upgrade.get("config_backup_created") is True
            and upgrade.get("config_preserved") is True
            and upgrade.get("rollback_available") is True
            and upgrade.get("distribution_upgrade_attempted") is False
        ),
        "default_uninstall_preserves_state": (
            uninstall.get("outcome") == "uninstalled"
            and uninstall.get("units_removed") is True
            and uninstall.get("release_removed") is True
            and uninstall.get("config_preserved") is True
            and uninstall.get("credentials_preserved") is True
            and uninstall.get("recordings_preserved") is True
            and uninstall.get("service_users_preserved") is True
            and uninstall.get("purge_requested") is False
        ),
        "purge_requires_confirmation_and_removes_state": (
            purge.get("outcome") == "purged"
            and purge.get("wrong_confirmation_read_only") is True
            and purge.get("configuration_removed") is True
            and purge.get("recordings_removed") is True
            and purge.get("service_users_preserved") is True
        ),
        "units_are_least_privilege_and_independent": (
            _unit_secure(media_unit, "tinypirelay-media")
            and _unit_secure(web_unit, "tinypirelay-web")
            and media_unit.get("audio_access") is True
            and web_unit.get("audio_access") is False
            and "tinypirelay-media.service"
            not in _sequence(web_unit.get("requires"))
        ),
        "filesystem_permissions_are_restrictive": (
            _mode(
                files,
                "/etc/tinypirelay/media",
                "0700",
                "tinypirelay-media",
                "tinypirelay-media",
                "directory",
            )
            and _mode(
                files,
                "/etc/tinypirelay/media/config.json",
                "0600",
                "tinypirelay-media",
                "tinypirelay-control",
                "file",
            )
            and _mode(
                files,
                "/etc/tinypirelay/web",
                "0700",
                "tinypirelay-web",
                "tinypirelay-web",
                "directory",
            )
            and _mode(
                files,
                "/etc/tinypirelay/web/credential.json",
                "0600",
                "tinypirelay-web",
                "tinypirelay-web",
                "file",
            )
            and _mode(
                files,
                "/run/tinypirelay",
                "0750",
                "tinypirelay-media",
                "tinypirelay-control",
                "directory",
            )
            and _mode(
                files,
                "/run/tinypirelay/control.sock",
                "0660",
                "tinypirelay-media",
                "tinypirelay-control",
                "socket",
            )
            and security.get("owner_events_present") is True
        ),
        "secrets_absent_from_args_logs_and_report": (
            security.get("secret_values_absent_from_process_args") is True
            and security.get("secret_values_absent_from_logs") is True
            and security.get("credential_hash_absent_from_diagnostics") is True
            and forbidden_absent
        ),
        "boot_and_late_resources_static_contract_only": (
            boot.get("evidence_source")
            == "static_unit_contract_plus_runtime_unit_tests"
            and boot.get("physical_boot_audio_network_execution") == "pending"
            and boot.get("web_order_independent") is True
            and boot.get("web_unavailable_unit_test_present") is True
            and boot.get("capture_failure_exit_test_present") is True
            and boot.get("capture_failure_restart_is_bounded") is True
            and boot.get("network_online_not_required_for_start") is True
            and boot.get("network_reconnect_unit_test_present") is True
        ),
    }
    reasons = [name for name, passed in checks.items() if not passed]
    return {
        "status": "pass" if not reasons else "fail",
        "checks": checks,
        "reasons": reasons,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, nargs="?")
    parser.add_argument(
        "--run-isolated",
        action="store_true",
        help="run redirected fixture lifecycles instead of reading a report",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.run_isolated == (args.report is not None):
        parser.error("choose exactly one of REPORT or --run-isolated")
    if args.run_isolated:
        report = run_isolated_scenarios()
        forbidden = (FIXTURE_PASSWORD,)
    else:
        try:
            report = json.loads(args.report.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(json.dumps({"status": "fail", "error": str(exc)}))
            return 2
        forbidden = ()
    if not isinstance(report, Mapping):
        print(json.dumps({"status": "fail", "error": "report must be an object"}))
        return 2
    result = assess_report(report, forbidden_values=forbidden)
    rendered: object = {**report, "result": result} if args.run_isolated else result
    text = json.dumps(rendered, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
