"""Installed TinyPiRelay operator commands."""

from __future__ import annotations

import argparse
import json
import os
import socket
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from .audio_capabilities import AudioCapabilityError
from .compatibility import EvidenceError
from .control_protocol import ControlClient, ControlError, redact_secrets
from .media_config import ConfigError
from .media_service import _validated_inputs
from .installer import InstallError, read_web_access
from .probe import PRESENT, collect_probe
from .web_service import open_controlling_tty


MEDIA_UNIT = "tinypirelay-media.service"
WEB_UNIT = "tinypirelay-web.service"
UNITS = {"media": MEDIA_UNIT, "web": WEB_UNIT}
CURRENT_ROOT = Path("/opt/tinypirelay/current")
CONFIG_PATH = Path("/etc/tinypirelay/media/config.json")
CREDENTIAL_PATH = Path("/etc/tinypirelay/web/credential.json")
SETUP_PENDING_PATH = Path("/etc/tinypirelay/web/setup-pending")
WEB_ACCESS_PATH = Path("/etc/tinypirelay/web-access.json")
CONTROL_SOCKET_PATH = Path("/run/tinypirelay/control.sock")
EVIDENCE_PATH = CURRENT_ROOT / "evidence/stream_compatibility.json"
AUDIO_CAPABILITIES_PATH = CURRENT_ROOT / "evidence/audio_capabilities.json"
INSTALLER_PATH = CURRENT_ROOT / "install.sh"
RUNUSER_PATH = Path("/usr/sbin/runuser")
PYTHON_PATH = Path("/usr/bin/python3")
WEB_USER = "tinypirelay-web"
COMMAND_TIMEOUT_SECONDS = 10
RESTART_TIMEOUT_SECONDS = 35
CONTROL_TIMEOUT_SECONDS = 2
MAX_COMMAND_OUTPUT = 16 * 1024
WEB_URL = "http://127.0.0.1:8080/"
SSH_TUNNEL_HINT = "ssh -L 8080:127.0.0.1:8080 <user>@<device-ip>"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="show installed service state")
    commands.add_parser(
        "diagnostics", help="run bounded, secret-redacted local checks"
    )

    config = commands.add_parser("config", help="configuration commands")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    validate = config_commands.add_parser("validate", help="validate without writing")
    validate.add_argument("--config", type=Path, default=CONFIG_PATH)
    validate.add_argument("--evidence", type=Path, default=EVIDENCE_PATH)
    validate.add_argument(
        "--audio-capabilities", type=Path, default=AUDIO_CAPABILITIES_PATH
    )

    passwd = commands.add_parser(
        "passwd", help="interactively replace the web credential"
    )
    passwd.add_argument("--username")

    restart = commands.add_parser(
        "restart", help="restart services across the local privilege boundary"
    )
    restart.add_argument("target", choices=("media", "web", "all"), nargs="?", default="media")

    commands.add_parser("upgrade", help="print the pinned-artifact upgrade procedure")
    commands.add_parser(
        "uninstall", help="report an uninstall plan that preserves data"
    )
    return parser


def _run_capture(
    command: Sequence[str], *, timeout: int = COMMAND_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        tuple(command),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _systemd_status(unit: str) -> dict[str, object]:
    try:
        result = _run_capture(
            (
                "systemctl",
                "show",
                "--no-pager",
                "--property=LoadState",
                "--property=ActiveState",
                "--property=SubState",
                "--property=UnitFileState",
                unit,
            )
        )
    except (OSError, subprocess.SubprocessError):
        return {"available": False, "error": "systemctl query failed"}
    if result.returncode != 0 or len(result.stdout) > MAX_COMMAND_OUTPUT:
        return {"available": False, "error": "systemctl query failed"}
    values: dict[str, object] = {"available": True}
    names = {
        "LoadState": "load_state",
        "ActiveState": "active_state",
        "SubState": "sub_state",
        "UnitFileState": "unit_file_state",
    }
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in names:
            values[names[key]] = value
    if set(values) != {
        "available",
        "load_state",
        "active_state",
        "sub_state",
        "unit_file_state",
    }:
        return {"available": False, "error": "systemctl response was incomplete"}
    return values


def service_status() -> dict[str, object]:
    services = {name: _systemd_status(unit) for name, unit in UNITS.items()}
    healthy = all(
        service.get("available") is True
        and service.get("load_state") == "loaded"
        and service.get("active_state") == "active"
        for service in services.values()
    )
    access = _web_access_report()
    return {
        "healthy": healthy and access["web_access"] != "unavailable",
        "services": services,
        **access,
    }


def _web_access_report() -> dict[str, object]:
    try:
        mode = read_web_access(WEB_ACCESS_PATH)
    except (InstallError, OSError, ValueError):
        return {"web_access": "unavailable", "web_url": None,
                "ssh_tunnel_hint": None, "web_access_error": "invalid web access configuration"}
    if mode == "loopback":
        return {"web_access": mode, "web_url": WEB_URL, "ssh_tunnel_hint": SSH_TUNNEL_HINT}
    hostname = socket.gethostname().split(".", 1)[0]
    return {"web_access": mode, "web_url": f"http://{hostname}.local/",
            "ssh_tunnel_hint": None, "ip_fallback": "http://<device-ip>/",
            "transport_notice": "Trusted LAN HTTP; passwords and sessions are not encrypted."}


def _configuration_report(
    config: Path, evidence: Path, audio_capabilities: Path
) -> dict[str, object]:
    try:
        parsed, revision = _validated_inputs(config, evidence, audio_capabilities)
    except (AudioCapabilityError, ConfigError, EvidenceError, OSError, ValueError) as exc:
        return {"valid": False, "error": str(exc)}
    return {
        "valid": True,
        "schema_version": parsed.schema_version,
        "revision": revision,
    }


def _path_report(
    path: Path,
    expected_mode: int,
    expected_type: str,
    *,
    expected_owner: str | None = None,
    expected_group: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "path": str(path),
        "expected_mode": f"{expected_mode:04o}",
        "expected_type": expected_type,
    }
    try:
        information = path.lstat()
    except OSError:
        return {**result, "safe": False, "error": "path is unavailable"}
    if stat.S_ISLNK(information.st_mode):
        actual_type = "symlink"
    elif stat.S_ISREG(information.st_mode):
        actual_type = "file"
    elif stat.S_ISSOCK(information.st_mode):
        actual_type = "socket"
    elif stat.S_ISDIR(information.st_mode):
        actual_type = "directory"
    else:
        actual_type = "other"
    actual_mode = stat.S_IMODE(information.st_mode)
    ownership_safe = True
    if expected_owner is not None or expected_group is not None:
        try:
            import grp
            import pwd

            owner = pwd.getpwuid(information.st_uid).pw_name
            group = grp.getgrgid(information.st_gid).gr_name
        except (ImportError, KeyError):
            owner = None
            group = None
            ownership_safe = False
        if expected_owner is not None:
            ownership_safe = ownership_safe and owner == expected_owner
            result["expected_owner"] = expected_owner
        if expected_group is not None:
            ownership_safe = ownership_safe and group == expected_group
            result["expected_group"] = expected_group
        result["owner"] = owner
        result["group"] = group
    result.update(
        {
            "safe": (
                actual_type == expected_type
                and actual_mode == expected_mode
                and ownership_safe
            ),
            "type": actual_type,
            "mode": f"{actual_mode:04o}",
            "uid": information.st_uid,
            "gid": information.st_gid,
        }
    )
    return result


def diagnostic_report() -> dict[str, object]:
    services = service_status()
    host_probe = redact_secrets(collect_probe())
    configuration = _configuration_report(
        CONFIG_PATH, EVIDENCE_PATH, AUDIO_CAPABILITIES_PATH
    )
    config_file = _path_report(
        CONFIG_PATH,
        0o600,
        "file",
        expected_owner="tinypirelay-media",
        expected_group="tinypirelay-control",
    )
    socket_report = _path_report(
        CONTROL_SOCKET_PATH,
        0o660,
        "socket",
        expected_owner="tinypirelay-media",
        expected_group="tinypirelay-control",
    )
    credential = _path_report(
        CREDENTIAL_PATH,
        0o600,
        "file",
        expected_owner="tinypirelay-web",
        expected_group="tinypirelay-web",
    )
    setup_pending = False
    if not credential["safe"] and not os.path.lexists(CREDENTIAL_PATH):
        marker = _path_report(SETUP_PENDING_PATH, 0o600, "file",
                              expected_owner="tinypirelay-web", expected_group="tinypirelay-web")
        try:
            if marker["safe"]:
                with SETUP_PENDING_PATH.open("rb") as pending:
                    setup_pending = pending.read(3) == b"1\n"
        except OSError:
            pass
    try:
        media: object = redact_secrets(
            ControlClient(
                CONTROL_SOCKET_PATH, timeout=CONTROL_TIMEOUT_SECONDS
            ).request("status")
        )
        control_available = True
    except ControlError:
        media = {"available": False, "error": "media control is unavailable"}
        control_available = False
    software_groups = ("platform", "hardware", "storage", "packages", "gstreamer")
    software_ready = isinstance(host_probe, dict) and (
        all(isinstance(host_probe.get(name), dict) and host_probe[name].get("status") == PRESENT
            for name in software_groups)
        if any(name in host_probe for name in software_groups)
        else host_probe.get("status") == PRESENT
    )
    healthy = bool(
        services["healthy"]
        and software_ready
        and configuration["valid"]
        and config_file["safe"]
        and socket_report["safe"]
        and (credential["safe"] or setup_pending)
        and control_available
    )
    return {
        "healthy": healthy,
        "services": services["services"],
        "host_probe": host_probe,
        "configuration": configuration,
        "configuration_file": config_file,
        "control_socket": socket_report,
        "credential": credential,
        "account_setup_required": setup_pending,
        "audio_setup_required": media.get("audio_setup_required") if isinstance(media, dict) else None,
        "media": media,
        **_web_access_report(),
        "log_commands": [
            f"journalctl -u {MEDIA_UNIT} -n 200 --no-pager",
            f"journalctl -u {WEB_UNIT} -n 200 --no-pager",
        ],
    }


def _print_json(value: object) -> None:
    print(json.dumps(redact_secrets(value), indent=2, sort_keys=True))


def _is_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return callable(geteuid) and geteuid() == 0


def _require_root(command: str) -> bool:
    if _is_root():
        return True
    print(f"{command} requires sudo", file=sys.stderr)
    return False


def _restart(target: str) -> int:
    if not _require_root("restart"):
        return 2
    units = tuple(UNITS.values()) if target == "all" else (UNITS[target],)
    try:
        result = _run_capture(
            ("systemctl", "restart", *units), timeout=RESTART_TIMEOUT_SECONDS
        )
    except (OSError, subprocess.SubprocessError):
        result = None
    if result is None or result.returncode != 0:
        print("service restart failed; inspect systemctl status", file=sys.stderr)
        return 1
    print("restarted " + ", ".join(units))
    return 0


def _passwd(username: str | None) -> int:
    if not _require_root("passwd"):
        return 2
    try:
        terminal = open_controlling_tty()
    except OSError:
        print(
            "a controlling terminal is required; password input is never read from stdin",
            file=sys.stderr,
        )
        return 2
    command = [
        str(RUNUSER_PATH),
        "--user",
        WEB_USER,
        "--",
        str(PYTHON_PATH),
        "-P",
        "-B",
        "-m",
        "tinypirelay.web_service",
        "passwd",
        "--credentials",
        str(CREDENTIAL_PATH),
        "--tty-stdin",
    ]
    if username is not None:
        command.extend(("--username", username))
    environment = {
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "PYTHONPATH": str(CURRENT_ROOT / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    try:
        result = subprocess.run(
            command,
            check=False,
            env=environment,
            stdin=terminal,
            stdout=terminal,
            stderr=terminal,
        )
    except OSError:
        print("credential update could not start", file=sys.stderr)
        return 2
    finally:
        terminal.close()
    if result.returncode != 0:
        return result.returncode
    restart_result = _restart("web")
    if restart_result != 0:
        print(
            "credential was committed, but the web service did not restart",
            file=sys.stderr,
        )
        return 1
    print("existing web sessions were invalidated")
    return 0


def _upgrade_report() -> int:
    print(
        "Upgrade is driven by the new, pinned release artifact. Download and verify "
        "that artifact, review `sudo ./install.sh upgrade`, then apply it with "
        "`sudo ./install.sh upgrade --apply`."
    )
    return 0


def _uninstall_plan() -> int:
    if not _require_root("uninstall"):
        return 2
    command = [str(INSTALLER_PATH), "uninstall"]
    try:
        result = subprocess.run(command, check=False)
    except OSError:
        print("installed lifecycle helper is unavailable", file=sys.stderr)
        return 2
    return result.returncode


def _contains_password_argument(argv: Sequence[str]) -> bool:
    return any(
        argument in {"--password", "-p"} or argument.startswith("--password=")
        for argument in argv
    )


def main(argv: Sequence[str] | None = None) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if _contains_password_argument(arguments):
        print(
            "password arguments are forbidden; use the interactive prompt",
            file=sys.stderr,
        )
        return 2
    args = _parser().parse_args(arguments)
    if args.command == "status":
        report = service_status()
        _print_json(report)
        return 0 if report["healthy"] else 1
    if args.command == "diagnostics":
        report = diagnostic_report()
        _print_json(report)
        return 0 if report["healthy"] else 1
    if args.command == "config":
        report = _configuration_report(
            args.config, args.evidence, args.audio_capabilities
        )
        _print_json(report)
        return 0 if report["valid"] else 2
    if args.command == "passwd":
        return _passwd(args.username)
    if args.command == "restart":
        return _restart(args.target)
    if args.command == "upgrade":
        return _upgrade_report()
    return _uninstall_plan()


if __name__ == "__main__":
    raise SystemExit(main())
