"""Staged, fail-closed TinyPiRelay repository installer.

Planning is read-only.  Mutation requires an already successful plan and the
explicit CLI ``--apply`` switch.  Tests inject a rooted filesystem, command
runner, probes, TTY checks, and stage failures; production defaults target the
local Raspberry Pi OS host.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Sequence

from . import probe
from .audio_capabilities import AudioCapabilityError
from .compatibility import EvidenceError
from .media_config import ConfigError
from .media_service import _validated_inputs
from .web_security import (
    CredentialRecord,
    SecurityError,
    validate_new_password,
)


STATE_SCHEMA_VERSION = 2
VERSION_PATTERN = re.compile(
    r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?\Z"
)
DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
WEB_PORT = 8080
SETUP_PENDING = "/etc/tinypirelay/web/setup-pending"
WEB_ACCESS_PATH = "/etc/tinypirelay/web-access.json"
WEB_ACCESS_DROPIN = "/etc/systemd/system/tinypirelay-web.service.d/10-access.conf"
MEDIA_UNIT = "tinypirelay-media.service"
WEB_UNIT = "tinypirelay-web.service"
UNITS = (MEDIA_UNIT, WEB_UNIT)
MAINTENANCE_UNIT = "tinypirelay-maintenance.service"
MAINTENANCE_SOCKET = "tinypirelay-maintenance.socket"
MAINTENANCE_UNITS = (MAINTENANCE_SOCKET, MAINTENANCE_UNIT)
PHASES = (
    "prepared",
    "packages",
    "dependencies",
    "release",
    "activated",
    "services",
    "complete",
)
REQUIRED_SOURCE_FILES = (
    "VERSION",
    "LICENSE",
    "install.sh",
    "config/example.json",
    "config/tinypirelay.schema.json",
    "evidence/stream_compatibility.json",
    "evidence/audio_capabilities.json",
    "packaging/bin/tinypirelay",
    "packaging/systemd/tinypirelay-media.service",
    "packaging/systemd/tinypirelay-web.service",
    "packaging/systemd/tinypirelay-maintenance.service",
    "packaging/systemd/tinypirelay-maintenance.socket",
    "packaging/sysusers.d/tinypirelay.conf",
    "packaging/tmpfiles.d/tinypirelay.conf",
    "src/tinypirelay/__init__.py",
    "src/tinypirelay/maintenance.py",
)
PAYLOAD_NAMES = (
    "VERSION",
    "LICENSE",
    "install.sh",
    "config",
    "evidence",
    "packaging",
    "src",
)
INTEGRATION_FILES = (
    (
        "packaging/systemd/tinypirelay-media.service",
        "/etc/systemd/system/tinypirelay-media.service",
        0o644,
    ),
    (
        "packaging/systemd/tinypirelay-web.service",
        "/etc/systemd/system/tinypirelay-web.service",
        0o644,
    ),
    (
        "packaging/systemd/tinypirelay-maintenance.service",
        "/etc/systemd/system/tinypirelay-maintenance.service",
        0o644,
    ),
    (
        "packaging/systemd/tinypirelay-maintenance.socket",
        "/etc/systemd/system/tinypirelay-maintenance.socket",
        0o644,
    ),
    (
        "packaging/sysusers.d/tinypirelay.conf",
        "/usr/lib/sysusers.d/tinypirelay.conf",
        0o644,
    ),
    (
        "packaging/tmpfiles.d/tinypirelay.conf",
        "/usr/lib/tmpfiles.d/tinypirelay.conf",
        0o644,
    ),
    ("packaging/bin/tinypirelay", "/usr/bin/tinypirelay", 0o755),
)
PRIVATE_DIRECTORIES = (
    ("/run/tinypirelay-maintenance", 0o750, "root", "tinypirelay-web"),
    ("/etc/tinypirelay", 0o755, "root", "root"),
    (
        "/etc/tinypirelay/media",
        0o700,
        "tinypirelay-media",
        "tinypirelay-media",
    ),
    ("/etc/tinypirelay/web", 0o700, "tinypirelay-web", "tinypirelay-web"),
    (
        "/run/tinypirelay",
        0o750,
        "tinypirelay-media",
        "tinypirelay-control",
    ),
    (
        "/var/cache/tinypirelay-media",
        0o750,
        "tinypirelay-media",
        "tinypirelay-media",
    ),
    ("/var/lib/tinypirelay", 0o710, "root", "tinypirelay-control"),
    (
        "/var/lib/tinypirelay/recordings",
        0o750,
        "tinypirelay-media",
        "tinypirelay-media",
    ),
    ("/var/lib/tinypirelay/backups", 0o700, "root", "root"),
)
MAX_PAYLOAD_ENTRIES = 2_048
MAX_PAYLOAD_FILE_BYTES = 8 * 1024 * 1024
MAX_PAYLOAD_TOTAL_BYTES = 64 * 1024 * 1024


class InstallError(RuntimeError):
    """An installation safety gate or staged operation failed."""


def _decode_web_access(payload: bytes) -> str:
    try:
        value = json.loads(payload.decode("utf-8"))
        if len(payload) > 1024 or not isinstance(value, dict) or set(value) != {"mode"} or value["mode"] not in {"lan", "loopback"}:
            raise ValueError
    except (UnicodeError, ValueError, TypeError) as exc:
        raise InstallError("installed web access configuration is invalid") from exc
    return value["mode"]


def read_web_access(path: Path) -> str:
    """Read the bounded public access setting; absent legacy files stay local."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return "loopback"
    if not stat.S_ISREG(info.st_mode) or (os.name == "posix" and (
        info.st_uid != 0 or info.st_gid != 0 or stat.S_IMODE(info.st_mode) != 0o644
    )):
        raise InstallError("web access configuration must be a root-owned mode-0644 regular file")
    with path.open("rb") as handle:
        return _decode_web_access(handle.read(1025))


@dataclass(frozen=True, repr=False)
class InitialCredential:
    """A short-lived plaintext credential whose repr is always redacted."""

    username: str
    password: str = field(repr=False)

    def __repr__(self) -> str:
        return f"InitialCredential(username={self.username!r}, password=<redacted>)"


@dataclass(frozen=True)
class InstallState:
    phase: str
    target_version: str | None
    installed_version: str | None
    previous_version: str | None = None
    source_sha256: str | None = None
    previous_source_sha256: str | None = None


@dataclass(frozen=True)
class InstallPlan:
    version: str
    source_sha256: str
    mode: str
    existing_version: str | None
    resume_phase: str | None
    package_plan: tuple[str, ...]
    apt_transaction: tuple[tuple[str, str], ...]
    apt_fingerprint: str | None
    deferred_elements: tuple[str, ...]
    needs_credential: bool
    config_source: Path | None
    config_sha256: str | None
    installed_config_sha256: str | None
    credential_sha256: str | None
    owner_repairs: tuple[tuple[str, str, str], ...]
    permission_repairs: tuple[tuple[str, int], ...]
    runtime_repairs: tuple[str, ...]
    actions: tuple[str, ...]
    errors: tuple[str, ...]
    state_snapshot: InstallState | None = field(repr=False, compare=True)
    preflight_probe: Mapping[str, object] = field(repr=False, compare=False)
    setup_pending: bool = False
    web_access: str = "loopback"
    previous_web_access: str = "loopback"

    @property
    def ready(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class UninstallPlan:
    installed_version: str | None
    remove_data: bool
    actions: tuple[str, ...]
    errors: tuple[str, ...] = ()
    state_snapshot: InstallState | None = field(default=None, repr=False, compare=True)

    @property
    def ready(self) -> bool:
        return not self.errors


Runner = Callable[[Sequence[str]], probe.CommandResult]
ProbeCollector = Callable[[], Mapping[str, object]]
PortChecker = Callable[[int], str | None]
TTYChecker = Callable[[], bool]
StageHook = Callable[[str], None]


def _noop_stage(_stage: str) -> None:
    return None


class LocalHost:
    """Rooted filesystem and argv-only command adapter with mutation events."""

    def __init__(
        self,
        root: str | os.PathLike[str] = "/",
        runner: Runner = probe.run_command,
        events: list[str] | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.runner = runner
        self.events = events if events is not None else []

    def resolve(self, logical_path: str) -> Path:
        logical = PurePosixPath(logical_path)
        if not logical.is_absolute() or ".." in logical.parts:
            raise InstallError(f"unsafe installer path: {logical_path!r}")
        return self.root.joinpath(*logical.parts[1:])

    def _guard(
        self,
        path: Path,
        *,
        target_directory: bool | None = None,
        allow_target_symlink: bool = False,
    ) -> None:
        """Reject symlinks/non-directories anywhere in a managed path."""

        try:
            relative = path.relative_to(self.root)
        except ValueError:
            raise InstallError("installer path escapes its configured root") from None
        current = self.root
        parts = relative.parts
        for index, part in enumerate(parts):
            current /= part
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise InstallError(f"cannot inspect managed path {current}: {exc}") from exc
            is_target = index == len(parts) - 1
            if stat.S_ISLNK(info.st_mode) and not (
                is_target and allow_target_symlink
            ):
                raise InstallError(f"managed path contains a symbolic link: {current}")
            if stat.S_ISLNK(info.st_mode):
                continue
            if not is_target and not stat.S_ISDIR(info.st_mode):
                raise InstallError(f"managed path ancestor is not a directory: {current}")
            if is_target and target_directory is True and not stat.S_ISDIR(info.st_mode):
                raise InstallError(f"managed path is not a directory: {current}")
            if is_target and target_directory is False and not stat.S_ISREG(info.st_mode):
                raise InstallError(f"managed path is not a regular file: {current}")

    def exists(self, logical_path: str) -> bool:
        path = self.resolve(logical_path)
        self._guard(path)
        return path.exists()

    def is_file(self, logical_path: str) -> bool:
        path = self.resolve(logical_path)
        self._guard(path)
        return path.is_file() and not path.is_symlink()

    def read_bytes(self, logical_path: str, limit: int = 128 * 1024) -> bytes:
        path = self.resolve(logical_path)
        try:
            self._guard(path, target_directory=False)
            info = path.lstat()
            if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise InstallError(f"unsafe or oversized file: {logical_path}")
            return path.read_bytes()
        except OSError as exc:
            raise InstallError(f"cannot read {logical_path}: {exc}") from exc

    def is_empty_directory(self, logical_path: str) -> bool:
        path = self.resolve(logical_path)
        self._guard(path, target_directory=True if path.exists() else None)
        if not path.exists():
            return False
        try:
            return next(path.iterdir(), None) is None
        except OSError as exc:
            raise InstallError(f"cannot inspect managed directory {logical_path}: {exc}") from exc

    def release_fingerprint(self, version: str) -> str | None:
        if VERSION_PATTERN.fullmatch(version) is None:
            raise InstallError("cannot inspect a release with an invalid version")
        logical_path = f"/opt/tinypirelay/releases/{version}"
        path = self.resolve(logical_path)
        self._guard(path, target_directory=True if path.exists() else None)
        if not path.exists():
            return None
        fingerprint = validate_payload_tree(path)
        if self.root == Path("/").resolve() and os.name == "posix":
            executable = {
                PurePosixPath("install.sh"),
                PurePosixPath("packaging/bin/tinypirelay"),
            }
            for item in (path, *path.rglob("*")):
                info = item.lstat()
                relative = PurePosixPath(item.relative_to(path).as_posix())
                expected_mode = (
                    0o755
                    if stat.S_ISDIR(info.st_mode) or relative in executable
                    else 0o644
                )
                if info.st_uid != 0 or stat.S_IMODE(info.st_mode) != expected_mode:
                    raise InstallError(
                        f"installed release permissions are unsafe: {relative}"
                    )
        if read_source_version(path) != version:
            raise InstallError(f"installed release VERSION is invalid: {version}")
        return fingerprint

    @contextlib.contextmanager
    def installer_lock(self):
        """Serialize lifecycle mutations without trusting a shell lock helper."""

        path = self.resolve("/run/lock/tinypirelay-installer.lock")
        self._guard(path, target_directory=False if path.exists() else None)
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            flags = (
                os.O_RDWR
                | os.O_CREAT
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            descriptor = os.open(path, flags, 0o600)
        except OSError as exc:
            raise InstallError(f"cannot open installer lifecycle lock: {exc}") from exc
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise InstallError("installer lifecycle lock is not a regular file")
            if (
                os.name == "posix"
                and self.root == Path("/").resolve()
                and info.st_uid != 0
            ):
                raise InstallError("installer lifecycle lock is not root-owned")
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            else:
                os.chmod(path, 0o600)
            try:
                if os.name == "posix":
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:
                    import msvcrt

                    if info.st_size == 0:
                        os.write(descriptor, b"\0")
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except (BlockingIOError, OSError) as exc:
                raise InstallError(
                    "another TinyPiRelay lifecycle operation is in progress"
                ) from exc
        except InstallError:
            os.close(descriptor)
            raise
        except OSError as exc:
            os.close(descriptor)
            raise InstallError(f"cannot acquire installer lifecycle lock: {exc}") from exc
        except BaseException:
            os.close(descriptor)
            raise
        try:
            yield
        finally:
            try:
                if os.name == "posix":
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                else:
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            os.close(descriptor)

    def mkdir(self, logical_path: str, mode: int) -> None:
        path = self.resolve(logical_path)
        self._guard(path, target_directory=True if path.exists() else None)
        existed = path.exists()
        path.mkdir(parents=True, exist_ok=True, mode=mode)
        os.chmod(path, mode)
        if not existed:
            self.events.append(f"mkdir:{logical_path}:{mode:04o}")

    def write_atomic(self, logical_path: str, payload: bytes, mode: int) -> None:
        target = self.resolve(logical_path)
        self._guard(target, target_directory=False if target.exists() else None)
        if target.exists() and target.is_symlink():
            raise InstallError(f"refusing to replace symbolic link: {logical_path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        temporary = Path(temporary_name)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, mode)
            else:
                os.chmod(temporary, mode)
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                descriptor = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            os.chmod(target, mode)
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise
        self.events.append(f"write:{logical_path}:{mode:04o}")

    def copy_file(
        self,
        source: Path,
        logical_path: str,
        mode: int,
        *,
        expected_sha256: str | None = None,
    ) -> None:
        try:
            before = source.lstat()
        except OSError as exc:
            raise InstallError(f"cannot inspect source file {source}: {exc}") from exc
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise InstallError(f"source is not a regular file: {source}")
        try:
            payload = source.read_bytes()
            after = source.lstat()
        except OSError as exc:
            raise InstallError(f"cannot read source file {source}: {exc}") from exc
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise InstallError(f"source file changed while being read: {source}")
        if expected_sha256 is not None and hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise InstallError("configuration changed after planning; rerun preflight")
        self.write_atomic(logical_path, payload, mode)

    def copy_file_if_absent(
        self,
        source: Path,
        logical_path: str,
        mode: int,
        *,
        expected_sha256: str,
    ) -> None:
        if self.exists(logical_path):
            existing = self.read_bytes(logical_path)
            if (
                hashlib.sha256(existing).hexdigest() != expected_sha256
                or not self.mode_matches(logical_path, mode)
            ):
                raise InstallError("existing configuration backup is invalid")
            return
        self.copy_file(
            source,
            logical_path,
            mode,
            expected_sha256=expected_sha256,
        )

    def copy_release(
        self, source: Path, version: str, *, expected_sha256: str | None = None
    ) -> None:
        before_fingerprint = validate_payload_tree(source)
        if expected_sha256 is not None and before_fingerprint != expected_sha256:
            raise InstallError("release payload changed after planning; rerun preflight")
        release = self.resolve(f"/opt/tinypirelay/releases/{version}")
        self._guard(release, target_directory=True if release.exists() else None)
        if release.exists():
            if (
                validate_payload_tree(release) == before_fingerprint
                and read_source_version(release) == version
            ):
                return
            raise InstallError(f"release directory already exists but is invalid: {version}")
        staging = self.resolve(f"/opt/tinypirelay/releases/.{version}.staging")
        self._guard(staging, target_directory=True if staging.exists() else None)
        if staging.exists():
            shutil.rmtree(staging)
            self.events.append(f"remove:/opt/tinypirelay/releases/.{version}.staging")
        staging.mkdir(parents=True, mode=0o700)
        try:
            for name in PAYLOAD_NAMES:
                item = source / name
                destination = staging / name
                item_mode = item.lstat().st_mode
                if stat.S_ISDIR(item_mode):
                    shutil.copytree(
                        item,
                        destination,
                        symlinks=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                    )
                elif stat.S_ISREG(item_mode):
                    shutil.copy2(item, destination, follow_symlinks=False)
                else:
                    raise InstallError(f"required payload item is missing: {name}")
            if validate_payload_tree(staging) != before_fingerprint:
                raise InstallError("staged release does not match the planned payload")
            executable = {
                PurePosixPath("install.sh"),
                PurePosixPath("packaging/bin/tinypirelay"),
            }
            for path in (staging, *staging.rglob("*")):
                relative = PurePosixPath(path.relative_to(staging).as_posix())
                mode = 0o755 if path.is_dir() or relative in executable else 0o644
                os.chmod(path, mode)
                if os.name == "posix" and stat.S_IMODE(path.lstat().st_mode) != mode:
                    raise InstallError(
                        f"could not normalize release permission: {relative}"
                    )
            if validate_payload_tree(source) != before_fingerprint:
                raise InstallError("release payload changed while being staged")
            os.replace(staging, release)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        self.events.append(f"release:{version}")

    def set_current(self, version: str) -> None:
        """Atomically point current at a complete release (POSIX production path)."""

        current = self.resolve("/opt/tinypirelay/current")
        temporary = self.resolve("/opt/tinypirelay/.current.new")
        self._guard_current(current)
        self._guard_current(temporary)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        target = f"releases/{version}"
        try:
            os.symlink(target, temporary, target_is_directory=True)
            os.replace(temporary, current)
        except OSError as exc:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise InstallError(f"cannot activate release {version}: {exc}") from exc
        self.events.append(f"current:{version}")

    def remove_file(self, logical_path: str) -> None:
        path = self.resolve(logical_path)
        self._guard(path, target_directory=False if path.exists() else None)
        try:
            path.unlink()
        except FileNotFoundError:
            return
        self.events.append(f"remove:{logical_path}")

    def inspect_removal_target(self, logical_path: str, *, directory: bool) -> None:
        path = self.resolve(logical_path)
        self._guard(
            path,
            target_directory=directory if path.exists() else None,
        )

    def remove_current(self) -> None:
        current = self.resolve("/opt/tinypirelay/current")
        self._guard_current(current)
        try:
            current.unlink()
        except FileNotFoundError:
            return
        self.events.append("remove:/opt/tinypirelay/current")

    def current_exists(self) -> bool:
        current = self.resolve("/opt/tinypirelay/current")
        self._guard_current(current)
        return current.exists() or current.is_symlink()

    def current_version(self) -> str | None:
        current = self.resolve("/opt/tinypirelay/current")
        self._guard_current(current)
        if not (current.exists() or current.is_symlink()):
            return None
        if current.is_symlink():
            target = os.readlink(current)
        elif self.root != Path("/").resolve():
            try:
                target = current.read_text(encoding="ascii").strip()
            except (OSError, UnicodeError) as exc:
                raise InstallError(f"cannot inspect managed current pointer: {exc}") from exc
        else:
            raise InstallError("managed current pointer is not a symbolic link")
        match = re.fullmatch(r"releases/([^/]+)", target)
        return match.group(1) if match and VERSION_PATTERN.fullmatch(match.group(1)) else None

    def _guard_current(self, current: Path) -> None:
        self._guard(current, allow_target_symlink=True)
        if not current.is_symlink():
            return
        try:
            target = os.readlink(current)
        except OSError as exc:
            raise InstallError(f"cannot inspect managed current link: {exc}") from exc
        match = re.fullmatch(r"releases/([^/]+)", target)
        if match is None or VERSION_PATTERN.fullmatch(match.group(1)) is None:
            raise InstallError("managed current link has an unsafe target")

    def remove_tree(self, logical_path: str) -> None:
        allowed = {
            "/opt/tinypirelay",
            "/etc/tinypirelay",
            "/var/lib/tinypirelay",
            "/var/cache/tinypirelay-media",
            "/run/tinypirelay",
            "/run/tinypirelay-maintenance",
        }
        if logical_path not in allowed:
            raise InstallError(f"refusing broad removal: {logical_path}")
        path = self.resolve(logical_path)
        self._guard(path, target_directory=True if path.exists() else None)
        if path.exists():
            shutil.rmtree(path)
            self.events.append(f"remove:{logical_path}")

    def set_owner(self, logical_path: str, user: str, group: str) -> None:
        path = self.resolve(logical_path)
        self._guard(path)
        self.mutate_command(("chown", f"{user}:{group}", str(path)))
        self.events.append(f"owner:{logical_path}:{user}:{group}")

    def owner_matches(self, logical_path: str, user: str, group: str) -> bool:
        path = self.resolve(logical_path)
        self._guard(path)
        if not path.exists():
            return False
        if self.root != Path("/").resolve():
            return f"owner:{logical_path}:{user}:{group}" in self.events
        if os.name != "posix":
            return False
        try:
            import grp
            import pwd

            info = path.stat(follow_symlinks=False)
            return info.st_uid == pwd.getpwnam(user).pw_uid and info.st_gid == grp.getgrnam(group).gr_gid
        except (KeyError, OSError):
            return False

    def mode_matches(self, logical_path: str, mode: int) -> bool:
        path = self.resolve(logical_path)
        self._guard(path)
        if not path.exists():
            return False
        if os.name != "posix" and self.root != Path("/").resolve():
            return True
        try:
            return stat.S_IMODE(path.lstat().st_mode) == mode
        except OSError:
            return False

    def file_matches(self, logical_path: str, source: Path, mode: int) -> bool:
        if not self.is_file(logical_path) or not self.mode_matches(logical_path, mode):
            return False
        if self.root == Path("/").resolve() and not self.owner_matches(
            logical_path, "root", "root"
        ):
            return False
        try:
            source_info = source.lstat()
            if not stat.S_ISREG(source_info.st_mode):
                return False
            expected = source.read_bytes()
            actual = self.read_bytes(logical_path, limit=MAX_PAYLOAD_FILE_BYTES)
        except (InstallError, OSError):
            return False
        return hashlib.sha256(actual).digest() == hashlib.sha256(expected).digest()

    def directory_matches(
        self, logical_path: str, mode: int, user: str, group: str
    ) -> bool:
        path = self.resolve(logical_path)
        self._guard(path, target_directory=True if path.exists() else None)
        if not path.exists() or not self.mode_matches(logical_path, mode):
            return False
        return (
            True
            if self.root != Path("/").resolve()
            else self.owner_matches(logical_path, user, group)
        )

    def set_mode(self, logical_path: str, mode: int) -> None:
        path = self.resolve(logical_path)
        self._guard(path)
        try:
            os.chmod(path, mode, follow_symlinks=False)
        except (NotImplementedError, OSError) as exc:
            raise InstallError(f"cannot set mode on {logical_path}: {exc}") from exc
        self.events.append(f"mode:{logical_path}:{mode:04o}")

    def read_command(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout_seconds: int = 300,
    ) -> probe.CommandResult:
        command = tuple(argv)
        if self.root != Path("/").resolve():
            return self.runner(command)
        environment = os.environ.copy()
        environment.update({"LANG": "C", "LC_ALL": "C"})
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                env=environment,
                capture_output=True,
                check=False,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return probe.CommandResult(None, error=str(exc))
        return probe.CommandResult(result.returncode, result.stdout, result.stderr)

    def mutate_command(
        self, argv: Sequence[str], *, ok_returncodes: tuple[int, ...] = (0,)
    ) -> None:
        command = tuple(str(item) for item in argv)
        self.events.append("command:" + " ".join(command))
        result = self.read_command(command, timeout_seconds=1800)
        if result.returncode not in ok_returncodes:
            code = "unavailable" if result.returncode is None else str(result.returncode)
            raise InstallError(f"{command[0]} failed (exit {code})")


def read_source_version(source_dir: Path) -> str:
    path = source_dir / "VERSION"
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise InstallError("artifact VERSION must be a regular file")
        if before.st_size > 64:
            raise InstallError("artifact VERSION must be one newline-terminated token")
        payload = path.read_bytes()
        after = path.lstat()
    except OSError as exc:
        raise InstallError(f"cannot read artifact VERSION: {exc}") from exc
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise InstallError("artifact VERSION changed while being read")
    if len(payload) > 64 or not payload.endswith(b"\n") or payload.count(b"\n") != 1:
        raise InstallError("artifact VERSION must be one newline-terminated token")
    try:
        version = payload.rstrip(b"\r\n").decode("ascii")
    except UnicodeDecodeError:
        raise InstallError("artifact VERSION must be ASCII") from None
    if VERSION_PATTERN.fullmatch(version) is None:
        raise InstallError("artifact VERSION is invalid")
    return version


def validate_payload_tree(source_dir: Path) -> str:
    """Validate and fingerprint the exact bounded payload copied by install."""

    entries: list[Path] = []
    for name in PAYLOAD_NAMES:
        root = source_dir / name
        try:
            root_mode = root.lstat().st_mode
        except OSError as exc:
            raise InstallError(f"cannot inspect release payload {root}: {exc}") from exc
        if stat.S_ISLNK(root_mode) or not (
            stat.S_ISREG(root_mode) or stat.S_ISDIR(root_mode)
        ):
            raise InstallError(
                "release payload contains a non-regular entry: " + name
            )
        candidates = (root, *root.rglob("*")) if stat.S_ISDIR(root_mode) else (root,)
        entries.extend(
            path
            for path in candidates
            if "__pycache__" not in path.relative_to(source_dir).parts
            and path.suffix != ".pyc"
        )
    if len(entries) > MAX_PAYLOAD_ENTRIES:
        raise InstallError("release payload contains too many entries")
    total = 0
    digest = hashlib.sha256()
    for path in sorted(entries, key=lambda item: item.relative_to(source_dir).as_posix()):
        relative = path.relative_to(source_dir).as_posix()
        try:
            before = path.lstat()
            mode = before.st_mode
        except OSError as exc:
            raise InstallError(f"cannot inspect release payload {path}: {exc}") from exc
        if stat.S_ISLNK(mode) or not (
            stat.S_ISREG(mode) or stat.S_ISDIR(mode)
        ):
            raise InstallError(
                "release payload contains a non-regular entry: " + relative
            )
        digest.update(("D\0" if stat.S_ISDIR(mode) else "F\0").encode("ascii"))
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        if stat.S_ISREG(mode):
            if before.st_size > MAX_PAYLOAD_FILE_BYTES:
                raise InstallError(f"release payload file is too large: {relative}")
            total += before.st_size
            if total > MAX_PAYLOAD_TOTAL_BYTES:
                raise InstallError("release payload is too large")
            try:
                payload = path.read_bytes()
                after = path.lstat()
            except OSError as exc:
                raise InstallError(f"cannot read release payload {path}: {exc}") from exc
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ) or len(payload) != before.st_size:
                raise InstallError(
                    f"release payload changed while being inspected: {relative}"
                )
            digest.update(hashlib.sha256(payload).digest())
    return digest.hexdigest()


def validate_initial_credential(value: InitialCredential) -> CredentialRecord:
    if not isinstance(value, InitialCredential):
        raise InstallError("initial credential is required")
    try:
        password = validate_new_password(value.password)
        return CredentialRecord.create(value.username, password)
    except SecurityError as exc:
        raise InstallError(str(exc)) from None


def credential_from_fd(fd: int) -> InitialCredential:
    """Read exact credential JSON from an inherited FD, never argv/environment."""

    if type(fd) is not int or fd < 3:
        raise InstallError("credential FD must be an inherited descriptor numbered 3 or higher")
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise InstallError("credential FD must refer to one regular file")
        if os.name == "posix" and stat.S_IMODE(info.st_mode) != 0o600:
            raise InstallError("regular credential FD must refer to a mode-0600 file")
        with os.fdopen(os.dup(fd), "r", encoding="utf-8", errors="strict") as handle:
            text = handle.read(4097)
    except InstallError:
        raise
    except (OSError, UnicodeError) as exc:
        raise InstallError("credential FD is unreadable") from exc
    if len(text.encode("utf-8")) > 4096:
        raise InstallError("credential FD payload is too large")
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = item
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite JSON value")

    try:
        value = json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (json.JSONDecodeError, ValueError):
        raise InstallError("credential FD must contain valid JSON") from None
    if not isinstance(value, dict) or set(value) != {"username", "password"}:
        raise InstallError("credential FD must contain only username and password")
    if not isinstance(value["username"], str) or not isinstance(value["password"], str):
        raise InstallError("credential FD must contain valid username and password strings")
    credential = InitialCredential(value["username"], value["password"])
    validate_initial_credential(credential)
    return credential


def credential_from_tty(
    tty_path: str = "/dev/tty",
    *,
    password_prompt: Callable[..., str] = getpass.getpass,
) -> InitialCredential:
    """Read username from and prompt passwords on the controlling TTY only."""

    def tty_opener(path: str, flags: int) -> int:
        return os.open(
            path,
            flags
            | getattr(os, "O_NOCTTY", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )

    try:
        with open(
            tty_path, "r", encoding="utf-8", opener=tty_opener
        ) as tty_input, open(
            tty_path,
            "w",
            encoding="utf-8",
            buffering=1,
            opener=tty_opener,
        ) as tty_output:
            tty_output.write("TinyPiRelay web username: ")
            tty_output.flush()
            username = tty_input.readline().rstrip("\r\n")
            if not username:
                raise InstallError("web username is required")
            previous_stdin = sys.stdin
            sys.stdin = tty_input
            try:
                first = password_prompt(
                    "New TinyPiRelay web password: ", stream=tty_output
                )
                second = password_prompt("Confirm password: ", stream=tty_output)
            finally:
                sys.stdin = previous_stdin
    except InstallError:
        raise
    except (EOFError, OSError) as exc:
        raise InstallError(
            "a controlling /dev/tty is required; use an inherited --credential-fd for automation"
        ) from exc
    if first != second:
        raise InstallError("passwords do not match")
    credential = InitialCredential(username, first)
    validate_initial_credential(credential)
    return credential


def build_plan(
    preflight_probe: Mapping[str, object],
    *,
    version: str,
    source_sha256: str,
    state: InstallState | None,
    release_exists: bool,
    credential_exists: bool,
    credential_available: bool,
    config_source: Path | None,
    config_sha256: str | None,
    installed_config_sha256: str | None,
    credential_sha256: str | None,
    config_error: str | None,
    source_error: str | None,
    port_conflict: str | None,
    owner_repairs: tuple[tuple[str, str, str], ...] = (),
    permission_repairs: tuple[tuple[str, int], ...] = (),
    runtime_repairs: tuple[str, ...] = (),
    setup_pending: bool = False,
    web_access: str = "loopback",
    previous_web_access: str = "loopback",
) -> InstallPlan:
    """Build an install/upgrade/resume plan without changing host state."""

    errors: list[str] = []
    baseline = _mapping(_mapping(preflight_probe.get("platform")).get("os_architecture_baseline"))
    if baseline.get("status") != probe.PRESENT:
        errors.append(
            "unsupported or unverified OS/architecture: "
            + str(baseline.get("reason") or baseline.get("target") or "unknown")
        )
    for name in ("hardware", "storage"):
        section = _mapping(preflight_probe.get(name))
        if section.get("status") != probe.PRESENT:
            errors.append(f"{name} preflight is {section.get('status', 'unverified')}")

    package_items = _mapping(_mapping(preflight_probe.get("packages")).get("items"))
    packages: list[str] = []
    for name in probe.APT_PACKAGES:
        item = _mapping(package_items.get(name))
        if item.get("status") != probe.PRESENT:
            errors.append(f"APT package is unavailable or unverified: {name}")
        elif item.get("installed") is not True:
            candidate = item.get("candidate_version")
            if not isinstance(candidate, str) or not candidate:
                errors.append(f"APT package has no exact candidate version: {name}")
            else:
                packages.append(f"{name}={candidate}")

    required = _mapping(_mapping(preflight_probe.get("gstreamer")).get("required_elements"))
    deferred: list[str] = []
    pending_names = {item.split("=", 1)[0] for item in packages}
    tools_pending = "gstreamer1.0-tools" in pending_names
    for element in probe.REQUIRED_GSTREAMER_ELEMENTS:
        item = _mapping(required.get(element))
        if item.get("status") == probe.PRESENT:
            continue
        provider = probe.ELEMENT_PACKAGES[element]
        if tools_pending or provider in pending_names:
            deferred.append(element)
        else:
            errors.append(
                f"required GStreamer element {element} is absent after its package is installed"
            )

    if state is None or state.phase == "uninstalled":
        mode = "install"
        existing = None
        resume_phase = None
        if (
            state is not None
            and state.previous_version == version
            and state.source_sha256 is not None
            and state.source_sha256 != source_sha256
        ):
            mode = "blocked"
            errors.append(
                "the preserved installation journal binds this version to a different release payload"
            )
    elif state.phase != "complete":
        existing = state.installed_version
        resume_phase = state.phase
        if state.target_version != version:
            mode = "blocked"
            errors.append(
                f"unfinished installation targets {state.target_version}; resume that artifact first"
            )
        elif state.source_sha256 != source_sha256:
            mode = "blocked"
            errors.append(
                "unfinished installation payload differs from the journal; resume the exact artifact"
            )
        else:
            mode = "resume"
    elif state.installed_version == version:
        existing = version
        resume_phase = state.phase
        if state.source_sha256 != source_sha256:
            mode = "blocked"
            errors.append(
                "installed version is already bound to a different release payload"
            )
        elif not release_exists:
            mode = "blocked"
            errors.append("installed release is missing or does not match its journal")
        else:
            mode = (
                "noop"
                if not packages
                and not errors
                and not owner_repairs
                and not permission_repairs
                and not runtime_repairs
                and (credential_exists or (setup_pending and not credential_available))
                else "repair"
            )
    else:
        existing = state.installed_version
        resume_phase = state.phase
        mode = "upgrade"

    if source_error:
        errors.append(source_error)
    if config_error:
        errors.append(config_error)
    preserved_reinstall = (
        state is not None
        and state.phase == "uninstalled"
        and installed_config_sha256 is not None
    )
    if mode == "install" and config_source is None and not preserved_reinstall:
        errors.append("fresh installation requires --config PATH")
    if preserved_reinstall and config_source is not None:
        errors.append("reinstall reuses the preserved configuration; omit --config")
    if mode == "upgrade" and config_source is not None:
        errors.append("upgrade uses the installed configuration; do not pass --config")
    if mode in {"upgrade", "repair", "noop"} and installed_config_sha256 is None:
        errors.append("installed media configuration is missing or invalid")
    if port_conflict:
        errors.append(port_conflict)
    needs_credential = not credential_exists and credential_available
    if not credential_exists and not setup_pending:
        errors.append("installed web credential is missing; use explicit account recovery")

    actions: list[str] = []
    if packages:
        actions.append("install APT packages: " + ", ".join(packages))
    if deferred:
        actions.append("validate GStreamer elements after package installation")
    if needs_credential:
        actions.append("create the initial web credential without logging plaintext")
    elif not credential_exists and setup_pending:
        actions.append("enable first-visit username and password creation in the web GUI")
    if mode != "noop":
        actions.append(
            "serve the web GUI on trusted private LAN HTTP port 80 (unencrypted)"
            if web_access == "lan" else "keep the web GUI on loopback HTTP port 8080"
        )
    for path, user, group in owner_repairs:
        actions.append(f"repair ownership of {path} to {user}:{group}")
    for path, mode_bits in permission_repairs:
        actions.append(f"repair mode of {path} to {mode_bits:04o}")
    for item in runtime_repairs:
        if item == "current":
            actions.append("repair the active release pointer")
        elif item == "directories":
            actions.append("reapply and verify the private directory policy")
        elif item.startswith("integration:"):
            actions.append("restore installed integration file " + item.split(":", 1)[1])
    if mode in {"install", "upgrade", "resume"}:
        actions.append(f"stage immutable release {version}")
        if mode == "upgrade":
            actions.append("back up and preserve the installed media configuration")
        elif mode == "install":
            actions.append(
                "reuse the validated preserved media configuration"
                if preserved_reinstall
                else "install the validated operator configuration"
            )
        actions.extend(
            (
                "atomically activate the staged release",
                "install and start the two least-privilege systemd services",
                "install the restricted socket-activated maintenance helper",
            )
        )
    elif mode == "repair":
        actions.append("apply only the listed metadata or credential repairs")
        actions.append("restart the two installed services")
    return InstallPlan(
        version=version,
        source_sha256=source_sha256,
        mode=mode,
        existing_version=existing,
        resume_phase=resume_phase,
        package_plan=tuple(packages),
        apt_transaction=(),
        apt_fingerprint=None,
        deferred_elements=tuple(deferred),
        needs_credential=needs_credential,
        config_source=config_source,
        config_sha256=config_sha256,
        installed_config_sha256=installed_config_sha256,
        credential_sha256=credential_sha256,
        owner_repairs=owner_repairs,
        permission_repairs=permission_repairs,
        runtime_repairs=runtime_repairs,
        actions=tuple(actions),
        errors=tuple(dict.fromkeys(errors)),
        state_snapshot=state,
        preflight_probe=preflight_probe,
        setup_pending=setup_pending,
        web_access=web_access,
        previous_web_access=previous_web_access,
    )


class Installer:
    def __init__(
        self,
        host: LocalHost,
        source_dir: str | os.PathLike[str],
        probe_collector: ProbeCollector,
        *,
        port_checker: PortChecker | None = None,
        tty_checker: TTYChecker | None = None,
        stage_hook: StageHook = _noop_stage,
        privileged_checker: Callable[[], bool] | None = None,
    ) -> None:
        self.host = host
        self.source_dir = Path(source_dir).resolve()
        self.probe_collector = probe_collector
        self.port_checker = port_checker or self._port_conflict
        self.tty_checker = tty_checker or _has_tty
        self.stage_hook = stage_hook
        self.privileged_checker = privileged_checker or _is_root

    def plan(
        self,
        config_source: str | os.PathLike[str] | None = None,
        *,
        credential_fd_available: bool = False,
        web_access: str | None = None,
    ) -> InstallPlan:
        """Perform the complete read-only preflight and return an explicit plan."""

        source_error: str | None = None
        source_sha256 = "invalid"
        try:
            version = read_source_version(self.source_dir)
            missing = [
                name for name in REQUIRED_SOURCE_FILES if not (self.source_dir / name).is_file()
            ]
            if missing:
                source_error = "artifact is incomplete: " + ", ".join(missing)
            else:
                source_sha256 = validate_payload_tree(self.source_dir)
        except InstallError as exc:
            version = "0.0.0-invalid"
            source_error = str(exc)

        try:
            state, state_error = self._load_state()
        except InstallError as exc:
            state, state_error = None, str(exc)
        if state_error:
            source_error = "; ".join(filter(None, (source_error, state_error)))
        try:
            release_fingerprint = self.host.release_fingerprint(version)
            release_exists = release_fingerprint == source_sha256
            if release_fingerprint is not None and not release_exists:
                source_error = "; ".join(
                    filter(
                        None,
                        (
                            source_error,
                            f"existing release {version} differs from the planned payload",
                        ),
                    )
                )
            if (
                state is not None
                and state.phase != "uninstalled"
                and state.installed_version is not None
                and state.installed_version != version
            ):
                installed_fingerprint = self.host.release_fingerprint(
                    state.installed_version
                )
                expected_installed = (
                    state.previous_source_sha256
                    if state.phase != "complete"
                    else state.source_sha256
                )
                if installed_fingerprint != expected_installed:
                    source_error = "; ".join(
                        filter(
                            None,
                            (
                                source_error,
                                "installed release does not match its journaled payload",
                            ),
                        )
                    )
            conflicts = self._unmanaged_conflicts(state)
            identity_error = self._identity_error(require_present=False)
        except InstallError as exc:
            release_exists = False
            conflicts = ()
            identity_error = str(exc)
        for error in (
            "unmanaged or partial installation paths require inspection: "
            + ", ".join(conflicts)
            if conflicts
            else None,
            identity_error,
        ):
            if error:
                source_error = "; ".join(filter(None, (source_error, error)))
        installed_config = self.host.resolve("/etc/tinypirelay/media/config.json")
        supplied = (
            Path(os.path.abspath(os.fspath(config_source)))
            if config_source is not None
            else None
        )
        config_hash: str | None = None
        installed_config_hash: str | None = None
        config_errors: list[str] = []
        fresh = state is None or (
            state.phase not in {"complete", "uninstalled"} and state.installed_version is None
        )
        try:
            previous_web_access = self._read_web_access()
            selected_access = web_access or (
                previous_web_access if self.host.exists(WEB_ACCESS_PATH) or not fresh else "lan"
            )
            if selected_access not in {"lan", "loopback"}:
                raise InstallError("web access must be lan or loopback")
            self._web_access_payload(selected_access)
        except InstallError as exc:
            previous_web_access = selected_access = "loopback"
            config_errors.append(str(exc))
        if supplied is None and fresh and not self.host.exists("/etc/tinypirelay/media/config.json"):
            supplied = self.source_dir / "config/example.json"
        if supplied is not None:
            try:
                config_hash = self._validate_config(supplied)
            except (InstallError, ConfigError, EvidenceError, AudioCapabilityError) as exc:
                config_errors.append(f"supplied configuration validation failed: {exc}")
        try:
            installed_config_exists = self.host.is_file(
                "/etc/tinypirelay/media/config.json"
            )
        except InstallError as exc:
            installed_config_exists = False
            config_errors.append(str(exc))
        if installed_config_exists:
            try:
                installed_config_hash = self._validate_config(installed_config)
            except (InstallError, ConfigError, EvidenceError, AudioCapabilityError) as exc:
                config_errors.append(f"installed configuration validation failed: {exc}")
        if (
            config_hash is not None
            and installed_config_hash is not None
            and config_hash != installed_config_hash
        ):
            config_errors.append(
                "installed configuration differs from --config; refusing to overwrite it"
            )
        port_conflict = None
        if state is None or state.phase == "uninstalled" or selected_access != previous_web_access:
            port_conflict = self.port_checker(80 if selected_access == "lan" else WEB_PORT)
        result = self.probe_collector()
        owner_repairs: list[tuple[str, str, str]] = []
        permission_repairs: list[tuple[str, int]] = []
        for path, user, group in (
            (
                "/etc/tinypirelay/media/config.json",
                "tinypirelay-media",
                "tinypirelay-control",
            ),
            (
                "/etc/tinypirelay/web/credential.json",
                "tinypirelay-web",
                "tinypirelay-web",
            ),
            (SETUP_PENDING, "tinypirelay-web", "tinypirelay-web"),
        ):
            try:
                if self.host.is_file(path):
                    if not self.host.owner_matches(path, user, group):
                        owner_repairs.append((path, user, group))
                    if not self.host.mode_matches(path, 0o600):
                        permission_repairs.append((path, 0o600))
            except InstallError as exc:
                config_errors.append(str(exc))
        credential_hash: str | None = None
        try:
            credential_exists = self.host.is_file(
                "/etc/tinypirelay/web/credential.json"
            )
            if credential_exists:
                credential_payload = self.host.read_bytes(
                    "/etc/tinypirelay/web/credential.json"
                )
                credential_text = credential_payload.decode("utf-8")
                CredentialRecord.from_json(credential_text)
                credential_hash = hashlib.sha256(credential_payload).hexdigest()
        except (SecurityError, UnicodeError) as exc:
            credential_exists = False
            config_errors.append(f"installed web credential is invalid: {exc}")
        except InstallError as exc:
            credential_exists = False
            config_errors.append(str(exc))
        setup_pending = False
        try:
            if not credential_exists:
                setup_pending = self._setup_is_pending() or fresh
        except InstallError as exc:
            config_errors.append(str(exc))
        runtime_repairs: list[str] = []
        if selected_access != previous_web_access:
            runtime_repairs.append("web-access")
        if (
            state is not None
            and state.phase == "complete"
            and state.installed_version == version
        ):
            try:
                if self.host.current_version() != version:
                    runtime_repairs.append("current")
                for relative, target, mode in INTEGRATION_FILES:
                    if not self.host.file_matches(target, self.source_dir / relative, mode):
                        runtime_repairs.append("integration:" + target)
                if not self._web_access_matches(selected_access):
                    runtime_repairs.append("web-access")
                if any(
                    not self.host.directory_matches(path, mode, user, group)
                    for path, mode, user, group in PRIVATE_DIRECTORIES
                ):
                    runtime_repairs.append("directories")
            except InstallError as exc:
                config_errors.append(str(exc))
        plan = build_plan(
            result,
            version=version,
            source_sha256=source_sha256,
            state=state,
            release_exists=release_exists,
            credential_exists=credential_exists,
            credential_available=credential_fd_available,
            config_source=supplied,
            config_sha256=config_hash,
            installed_config_sha256=installed_config_hash,
            credential_sha256=credential_hash,
            config_error="; ".join(dict.fromkeys(config_errors)) or None,
            source_error=source_error,
            port_conflict=port_conflict,
            owner_repairs=tuple(owner_repairs),
            permission_repairs=tuple(permission_repairs),
            runtime_repairs=tuple(runtime_repairs),
            setup_pending=setup_pending,
            web_access=selected_access,
            previous_web_access=previous_web_access,
        )
        if plan.package_plan:
            try:
                transaction, fingerprint = self._apt_transaction(
                    plan.package_plan, verify_downloads=True
                )
                actions = plan.actions + (
                    "verified APT transaction: "
                    + ", ".join(f"{name}={version}" for name, version in transaction),
                )
                plan = replace(
                    plan,
                    apt_transaction=transaction,
                    apt_fingerprint=fingerprint,
                    actions=actions,
                )
            except InstallError as exc:
                plan = _with_error(plan, str(exc))
        return plan

    def apply(
        self, plan: InstallPlan, credential: InitialCredential | None = None
    ) -> InstallState:
        """Serialize and revalidate a planned lifecycle mutation."""

        if not plan.ready:
            raise InstallError("refusing to apply a plan with preflight errors")
        if plan.mode == "noop":
            state, error = self._load_state()
            if error or state is None or state != plan.state_snapshot:
                raise InstallError(error or "installation changed after planning; rerun preflight")
            if (
                state.source_sha256 != plan.source_sha256
                or validate_payload_tree(self.source_dir) != plan.source_sha256
                or self.host.release_fingerprint(plan.version) != plan.source_sha256
            ):
                raise InstallError("installed release changed after planning; rerun preflight")
            if (
                plan.installed_config_sha256 is None
                or self._validate_config(
                    self.host.resolve("/etc/tinypirelay/media/config.json")
                )
                != plan.installed_config_sha256
            ):
                raise InstallError("installed configuration changed after planning; rerun preflight")
            self._verify_planned_account(plan)
            for path, user, group in (
                (
                    "/etc/tinypirelay/media/config.json",
                    "tinypirelay-media",
                    "tinypirelay-control",
                ),
                (
                    "/etc/tinypirelay/web/credential.json" if plan.credential_sha256 else SETUP_PENDING,
                    "tinypirelay-web",
                    "tinypirelay-web",
                ),
            ):
                if not self.host.mode_matches(path, 0o600) or not self.host.owner_matches(
                    path, user, group
                ):
                    raise InstallError("installed file metadata changed after planning")
            if self.host.current_version() != plan.version:
                raise InstallError("active release pointer changed after planning")
            if any(
                not self.host.file_matches(target, self.source_dir / relative, mode)
                for relative, target, mode in INTEGRATION_FILES
            ):
                raise InstallError("installed integration changed after planning")
            if self._read_web_access() != plan.web_access or not self._web_access_matches(plan.web_access):
                raise InstallError("installed web access changed after planning")
            if any(
                not self.host.directory_matches(path, mode, user, group)
                for path, mode, user, group in PRIVATE_DIRECTORIES
            ):
                raise InstallError("private directory policy changed after planning")
            return state
        if not self.privileged_checker():
            raise InstallError("installation mutation requires root privileges")
        with self.host.installer_lock():
            state, error = self._load_state()
            if error or state != plan.state_snapshot:
                raise InstallError(error or "installation changed after planning; rerun preflight")
            conflicts = self._unmanaged_conflicts(state)
            if conflicts:
                raise InstallError(
                    "installation paths changed after planning: " + ", ".join(conflicts)
                )
            return self._apply_locked(plan, credential)

    def _apply_locked(
        self, plan: InstallPlan, credential: InitialCredential | None = None
    ) -> InstallState:
        """Apply one successful plan in journaled, resumable stages."""

        if not plan.ready:
            raise InstallError("refusing to apply a plan with preflight errors")
        if not self.privileged_checker():
            raise InstallError("installation mutation requires root privileges")
        if read_source_version(self.source_dir) != plan.version:
            raise InstallError("artifact VERSION changed after planning")
        if validate_payload_tree(self.source_dir) != plan.source_sha256:
            raise InstallError("release payload changed after planning; rerun preflight")
        if self._read_web_access() != plan.previous_web_access:
            raise InstallError("web access changed after planning; rerun preflight")
        if plan.config_source is not None:
            current_hash = self._validate_config(plan.config_source)
            if current_hash != plan.config_sha256:
                raise InstallError("configuration changed after planning; rerun preflight")
        if plan.installed_config_sha256 is not None:
            installed_hash = self._validate_config(
                self.host.resolve("/etc/tinypirelay/media/config.json")
            )
            if installed_hash != plan.installed_config_sha256:
                raise InstallError(
                    "installed configuration changed after planning; rerun preflight"
                )
        self._verify_planned_account(plan)
        record: CredentialRecord | None = None
        if credential is not None and plan.credential_sha256 is None:
            record = validate_initial_credential(credential)
        elif plan.needs_credential:
            raise InstallError("initial credential was not provided")
        if (
            plan.mode == "repair"
            and self.host.release_fingerprint(plan.version) != plan.source_sha256
        ):
            raise InstallError("installed release changed after planning; rerun preflight")

        state, _error = self._load_state()
        previous = plan.existing_version
        if state is None or state.phase in {"complete", "uninstalled"}:
            previous_source = (
                state.source_sha256
                if state is not None and previous is not None
                else None
            )
            state = InstallState(
                "prepared",
                plan.version,
                previous,
                previous,
                plan.source_sha256,
                previous_source,
            )
            self.stage_hook("prepare")
            self.host.mkdir("/var/lib/tinypirelay", 0o710)
            self._write_state(state)
        elif (
            state.target_version != plan.version
            or state.source_sha256 != plan.source_sha256
        ):
            raise InstallError("installation journal target changed after planning")
        previous_source = state.previous_source_sha256

        if plan.package_plan:
            self.stage_hook("packages")
            transaction, fingerprint = self._apt_transaction(
                plan.package_plan, verify_downloads=False
            )
            if (
                transaction != plan.apt_transaction
                or fingerprint != plan.apt_fingerprint
            ):
                raise InstallError(
                    "APT transaction changed after planning; rerun preflight"
                )
            self.host.mutate_command(
                (
                    "apt-get",
                    "--no-remove",
                    "--no-upgrade",
                    "install",
                    "--yes",
                    "--no-install-recommends",
                    *(f"{name}={version}" for name, version in plan.apt_transaction),
                )
            )
        if not _phase_done(state.phase, "packages"):
            state = InstallState(
                "packages",
                plan.version,
                previous,
                previous,
                plan.source_sha256,
                previous_source,
            )
            self._write_state(state)

        self.stage_hook("post_install_elements")
        self._validate_post_install_probe(self.probe_collector())
        if plan.mode == "repair":
            release = self.host.resolve(f"/opt/tinypirelay/releases/{plan.version}")
            integration_changed = False
            for item in plan.runtime_repairs:
                if item == "current":
                    self.host.set_current(plan.version)
                elif item == "directories":
                    self.host.mutate_command(
                        ("systemd-tmpfiles", "--create", "tinypirelay.conf")
                    )
                    self._ensure_fixture_directories()
                elif item.startswith("integration:"):
                    target_path = item.split(":", 1)[1]
                    match = next(
                        (
                            (relative, mode)
                            for relative, target, mode in INTEGRATION_FILES
                            if target == target_path
                        ),
                        None,
                    )
                    if match is None:
                        raise InstallError("repair plan contains an unknown integration file")
                    relative, mode = match
                    self.host.copy_file(release / relative, target_path, mode)
                    integration_changed = True
            if "web-access" in plan.runtime_repairs:
                self._install_web_access(plan.web_access, release)
                integration_changed = True
            self._install_account(plan, record)
            self._repair_and_verify_owners()
            self._verify_private_directories()
            if integration_changed:
                self.host.mutate_command(("systemctl", "daemon-reload"))
            self._activate_maintenance(release)
            try:
                self.host.mutate_command(("systemctl", "restart", *UNITS))
                self._require_active_units()
            except InstallError as exc:
                if plan.web_access != plan.previous_web_access:
                    rollback_error = self._rollback_activation(
                        plan.existing_version, plan.source_sha256, plan.previous_web_access
                    )
                    if rollback_error is None and plan.state_snapshot is not None:
                        self._write_state(plan.state_snapshot)
                    raise InstallError(
                        "web access change failed; previous access restored"
                        if rollback_error is None else "web access change failed; rollback failed: " + rollback_error
                    ) from exc
                raise
            state = InstallState(
                "complete",
                plan.version,
                plan.version,
                state.previous_version,
                plan.source_sha256,
                state.previous_source_sha256,
            )
            self._write_state(state)
            return state
        if not _phase_done(state.phase, "dependencies"):
            state = InstallState(
                "dependencies",
                plan.version,
                previous,
                previous,
                plan.source_sha256,
                previous_source,
            )
            self._write_state(state)

        # Parent creation must not inherit a caller's restrictive umask: both
        # least-privilege services need to traverse the active release path.
        self.host.mkdir("/opt/tinypirelay", 0o755)
        if not _phase_done(state.phase, "release"):
            self.stage_hook("release")
            self.host.mkdir("/opt/tinypirelay/releases", 0o755)
            self.host.copy_release(
                self.source_dir,
                plan.version,
                expected_sha256=plan.source_sha256,
            )
            release = self.host.resolve(f"/opt/tinypirelay/releases/{plan.version}")
            config_target = "/etc/tinypirelay/media/config.json"
            if previous and previous != plan.version:
                if plan.installed_config_sha256 is None:
                    raise InstallError("installed configuration hash disappeared")
                backup = (
                    f"/var/lib/tinypirelay/backups/{previous}-before-{plan.version}-"
                    f"{plan.installed_config_sha256}.json"
                )
                self.host.copy_file_if_absent(
                    self.host.resolve(config_target),
                    backup,
                    0o600,
                    expected_sha256=plan.installed_config_sha256,
                )
            self._install_integration_files(release)
            self.host.mutate_command(("systemd-sysusers", "tinypirelay.conf"))
            identity_error = self._identity_error(require_present=True)
            if identity_error:
                raise InstallError(identity_error)
            self.host.mutate_command(("systemd-tmpfiles", "--create", "tinypirelay.conf"))
            self._ensure_fixture_directories()
            if not self.host.exists(config_target):
                if plan.config_source is None:
                    raise InstallError("installed media configuration disappeared")
                self.host.copy_file(
                    plan.config_source,
                    config_target,
                    0o600,
                    expected_sha256=plan.config_sha256,
                )
            self._install_account(plan, record)
            self._install_web_access(plan.web_access, release)
            self._repair_and_verify_owners()
            self._verify_private_directories()
            state = InstallState(
                "release",
                plan.version,
                previous,
                previous,
                plan.source_sha256,
                previous_source,
            )
            self._write_state(state)

        if not _phase_done(state.phase, "activated"):
            self.stage_hook("activate")
            self.host.set_current(plan.version)
            state = InstallState(
                "activated",
                plan.version,
                previous,
                previous,
                plan.source_sha256,
                previous_source,
            )
            self._write_state(state)

        if not _phase_done(state.phase, "services"):
            self.stage_hook("services")
            try:
                self.host.mutate_command(("systemctl", "daemon-reload"))
                self._activate_maintenance(self.host.resolve(f"/opt/tinypirelay/releases/{plan.version}"))
                self.host.mutate_command(("systemctl", "enable", *UNITS))
                self.host.mutate_command(("systemctl", "restart", *UNITS))
                self._require_active_units()
            except InstallError as exc:
                rollback_error = self._rollback_activation(previous, previous_source, plan.previous_web_access)
                state = (
                    InstallState(
                        "complete",
                        previous,
                        previous,
                        previous,
                        previous_source,
                        previous_source,
                    )
                    if previous and rollback_error is None
                    else InstallState(
                        "release",
                        plan.version,
                        previous,
                        previous,
                        plan.source_sha256,
                        previous_source,
                    )
                )
                self._write_state(state)
                if rollback_error:
                    raise InstallError(
                        f"service activation failed; rollback also failed: {rollback_error}"
                    ) from exc
                raise InstallError(
                    "service activation failed; previous release was restored; newly installed packages remain"
                    if previous
                    else "service activation failed; fresh services were disabled"
                ) from exc
            state = InstallState(
                "services",
                plan.version,
                previous,
                previous,
                plan.source_sha256,
                previous_source,
            )
            self._write_state(state)

        self.stage_hook("complete")
        state = InstallState(
            "complete",
            plan.version,
            plan.version,
            previous,
            plan.source_sha256,
            previous_source,
        )
        self._write_state(state)
        return state

    def plan_uninstall(self, *, remove_data: bool = False) -> UninstallPlan:
        state, error = self._load_state()
        errors: list[str] = []
        if error:
            errors.append(error)
        elif state is None:
            errors.append(
                "no valid managed installation journal exists; refusing to remove fixed paths"
            )
        else:
            try:
                conflicts = self._unmanaged_conflicts(state)
            except InstallError as exc:
                conflicts = ()
                errors.append(str(exc))
            if conflicts:
                errors.append(
                    "unmanaged or partial installation paths require inspection: "
                    + ", ".join(conflicts)
                )
        try:
            for _relative, path, _mode in INTEGRATION_FILES:
                self.host.inspect_removal_target(path, directory=False)
            for path in (
                "/opt/tinypirelay",
                "/var/cache/tinypirelay-media",
                "/run/tinypirelay",
                "/run/tinypirelay-maintenance",
                *(("/etc/tinypirelay", "/var/lib/tinypirelay") if remove_data else ()),
            ):
                self.host.inspect_removal_target(path, directory=True)
        except InstallError as exc:
            errors.append(f"unsafe uninstall target: {exc}")
        actions = [
            "stop and disable TinyPiRelay services",
            "remove installed program releases, units, integration files, and wrapper",
        ]
        if remove_data:
            actions.append("permanently remove configuration, recordings, backups, and state")
        else:
            actions.append("preserve configuration, recordings, backups, and install history")
        return UninstallPlan(
            (
                state.installed_version or state.previous_version
                if state is not None
                else None
            ),
            remove_data,
            tuple(actions),
            tuple(errors),
            state,
        )

    def uninstall(
        self, plan: UninstallPlan, *, confirmation: str | None = None
    ) -> InstallState:
        if not plan.ready:
            raise InstallError("refusing to apply an uninstall plan with errors")
        if not self.privileged_checker():
            raise InstallError("uninstall mutation requires root privileges")
        if plan.remove_data and confirmation != "REMOVE TINYPIRELAY DATA":
            raise InstallError(
                "data removal requires exact confirmation: REMOVE TINYPIRELAY DATA"
            )
        with self.host.installer_lock():
            current_plan = self.plan_uninstall(remove_data=plan.remove_data)
            if not current_plan.ready or current_plan.state_snapshot != plan.state_snapshot:
                detail = current_plan.errors[0] if current_plan.errors else None
                raise InstallError(detail or "installation changed after planning; rerun preflight")
            return self._uninstall_locked(plan)

    def _uninstall_locked(self, plan: UninstallPlan) -> InstallState:
        snapshot = plan.state_snapshot
        preserved_source = None
        if snapshot is not None and plan.installed_version is not None:
            preserved_source = (
                snapshot.source_sha256
                if snapshot.phase in {"complete", "uninstalled"}
                else snapshot.previous_source_sha256
            )
        self.stage_hook("uninstall")
        self._deactivate_maintenance()
        self._deactivate_units(UNITS)
        for _relative, path, _mode in INTEGRATION_FILES:
            self.host.remove_file(path)
        self.host.remove_file(WEB_ACCESS_DROPIN)
        self.host.remove_tree("/opt/tinypirelay")
        self.host.remove_tree("/var/cache/tinypirelay-media")
        self.host.remove_tree("/run/tinypirelay")
        self.host.remove_tree("/run/tinypirelay-maintenance")
        self.host.mutate_command(("systemctl", "daemon-reload"))
        if plan.remove_data:
            self.host.remove_tree("/etc/tinypirelay")
            self.host.remove_tree("/var/lib/tinypirelay")
            return InstallState(
                "uninstalled",
                None,
                None,
                plan.installed_version,
                preserved_source,
                None,
            )
        state = InstallState(
            "uninstalled",
            None,
            None,
            plan.installed_version,
            preserved_source,
            None,
        )
        self._write_state(state)
        return state

    def _repair_and_verify_owners(self) -> None:
        ownership = (
            (
                "/etc/tinypirelay/media/config.json",
                "tinypirelay-media",
                "tinypirelay-control",
            ),
            (
                "/etc/tinypirelay/web/credential.json" if self.host.exists("/etc/tinypirelay/web/credential.json") else SETUP_PENDING,
                "tinypirelay-web",
                "tinypirelay-web",
            ),
        )
        for path, user, group in ownership:
            if not self.host.is_file(path):
                raise InstallError(f"required installed file is missing: {path}")
            if not self.host.mode_matches(path, 0o600):
                self.host.set_mode(path, 0o600)
            if not self.host.mode_matches(path, 0o600):
                raise InstallError(f"mode verification failed for {path}")
            if not self.host.owner_matches(path, user, group):
                self.host.set_owner(path, user, group)
            if not self.host.owner_matches(path, user, group):
                raise InstallError(f"ownership verification failed for {path}")

    def _verify_private_directories(self) -> None:
        for path, mode, user, group in PRIVATE_DIRECTORIES:
            if not self.host.directory_matches(path, mode, user, group):
                raise InstallError(f"private directory policy verification failed for {path}")

    def _rollback_activation(
        self, previous: str | None, previous_source_sha256: str | None,
        previous_web_access: str = "loopback",
    ) -> str | None:
        errors: list[str] = []

        def attempt(label: str, operation: Callable[[], None]) -> bool:
            try:
                operation()
                return True
            except InstallError as exc:
                errors.append(f"{label}: {exc}")
                return False

        attempt(
            "stop partially activated maintenance helper",
            self._deactivate_maintenance,
        )
        attempt(
            "stop partially activated services",
            lambda: self.host.mutate_command(("systemctl", "stop", *UNITS)),
        )
        if previous:
            try:
                valid_old_release = (
                    previous_source_sha256 is not None
                    and self.host.release_fingerprint(previous)
                    == previous_source_sha256
                )
            except InstallError as exc:
                errors.append(f"validate previous release: {exc}")
                valid_old_release = False
            if not valid_old_release:
                errors.append("previous release does not match its journaled payload")
                attempt(
                    "disable partially activated services",
                    lambda: self._deactivate_units(UNITS),
                )
                attempt("remove active release pointer", self.host.remove_current)
            else:
                old_release = self.host.resolve(
                    f"/opt/tinypirelay/releases/{previous}"
                )
                integration_ok = attempt(
                    "restore previous integration files",
                    lambda: self._install_integration_files(old_release),
                )
                access_ok = attempt(
                    "restore previous web access",
                    lambda: self._install_web_access(previous_web_access, old_release),
                )
                current_ok = attempt(
                    "restore previous release pointer",
                    lambda: self.host.set_current(previous),
                )
                reload_ok = attempt(
                    "reload restored systemd units",
                    lambda: self.host.mutate_command(("systemctl", "daemon-reload")),
                )
                if integration_ok and access_ok and current_ok and reload_ok:
                    attempt("restore maintenance activation", lambda: self._activate_maintenance(old_release))
                    attempt(
                        "enable restored services",
                        lambda: self.host.mutate_command(("systemctl", "enable", *UNITS)),
                    )
                    restarted = attempt(
                        "restart restored services",
                        lambda: self.host.mutate_command(("systemctl", "restart", *UNITS)),
                    )
                    if restarted:
                        attempt("verify restored services", self._require_active_units)
        else:
            attempt(
                "disable partially activated services",
                lambda: self._deactivate_units(UNITS),
            )
            attempt("remove active release pointer", self.host.remove_current)
        return "; ".join(errors) or None

    def _validate_config(self, path: Path) -> str:
        evidence_path = self.source_dir / "evidence" / "stream_compatibility.json"
        audio_path = self.source_dir / "evidence" / "audio_capabilities.json"
        _config, revision = _validated_inputs(path, evidence_path, audio_path)
        return revision

    def _setup_is_pending(self) -> bool:
        if not self.host.exists(SETUP_PENDING):
            return False
        if self.host.read_bytes(SETUP_PENDING) != b"1\n":
            raise InstallError("first-run setup marker is invalid; inspect account state")
        return True

    def _verify_planned_account(self, plan: InstallPlan) -> None:
        credential_path = "/etc/tinypirelay/web/credential.json"
        if plan.credential_sha256 is not None:
            if hashlib.sha256(self.host.read_bytes(credential_path)).hexdigest() != plan.credential_sha256:
                raise InstallError("installed web credential changed after planning")
        elif self.host.exists(credential_path):
            raise InstallError("web account was created after planning; rerun preflight")
        elif not plan.setup_pending:
            raise InstallError("installed web credential disappeared")
        elif plan.state_snapshot is not None and (
            plan.state_snapshot.phase in {"complete", "uninstalled"}
            or plan.state_snapshot.installed_version is not None
        ) and not self._setup_is_pending():
            raise InstallError("first-run setup marker disappeared; refusing to reopen registration")

    def _install_account(self, plan: InstallPlan, record: CredentialRecord | None) -> None:
        credential_path = "/etc/tinypirelay/web/credential.json"
        if self.host.exists(credential_path):
            return
        if record is not None:
            self.host.write_atomic(credential_path, (record.to_json() + "\n").encode("utf-8"), 0o600)
            self.host.remove_file(SETUP_PENDING)
        elif not self._setup_is_pending():
            snapshot = plan.state_snapshot
            if snapshot is not None and (
                snapshot.phase in {"complete", "uninstalled"} or snapshot.installed_version is not None
            ):
                raise InstallError("refusing to reopen first-run account creation")
            self.host.write_atomic(SETUP_PENDING, b"1\n", 0o600)

    def _read_web_access(self) -> str:
        if not self.host.exists(WEB_ACCESS_PATH):
            return "loopback"  # Legacy installations must never become LAN-exposed on upgrade.
        value = _decode_web_access(self.host.read_bytes(WEB_ACCESS_PATH))
        if (self.host.root == Path("/").resolve() and not self.host.owner_matches(WEB_ACCESS_PATH, "root", "root")) or not self.host.mode_matches(WEB_ACCESS_PATH, 0o644):
            raise InstallError("web access configuration must be root-owned mode 0644")
        return value

    def _web_access_payload(self, mode: str, release: Path | None = None) -> bytes | None:
        if mode == "loopback":
            return None
        source = (release or self.source_dir) / "packaging/systemd/tinypirelay-web.service"
        try:
            command = next(line for line in source.read_text(encoding="utf-8").splitlines() if line.startswith("ExecStart="))
        except (OSError, StopIteration, UnicodeError) as exc:
            raise InstallError("web service template is missing or invalid") from exc
        command = command.replace("--bind 127.0.0.1 --port 8080", "--bind 0.0.0.0 --port 80")
        # Older rollback artifacts do not support the application LAN allowlist.
        if "--setup-pending" not in command:
            raise InstallError("this release cannot safely expose the web GUI to the LAN")
        return (
            "[Service]\nExecStart=\n" + command + " --lan-only\n"
            "CapabilityBoundingSet=CAP_NET_BIND_SERVICE\n"
            "AmbientCapabilities=CAP_NET_BIND_SERVICE\n"
            "IPAddressAllow=10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16 fc00::/7 fe80::/10\n"
            "SocketBindAllow=\nSocketBindAllow=tcp:80\n"
        ).encode("utf-8")

    def _web_access_matches(self, mode: str) -> bool:
        expected = self._web_access_payload(mode)
        if expected is None:
            return not self.host.exists(WEB_ACCESS_DROPIN)
        return (
            self.host.is_file(WEB_ACCESS_DROPIN)
            and self.host.read_bytes(WEB_ACCESS_DROPIN) == expected
            and self.host.mode_matches(WEB_ACCESS_DROPIN, 0o644)
            and (self.host.root != Path("/").resolve() or (
                self.host.owner_matches(WEB_ACCESS_DROPIN, "root", "root")
                and self.host.directory_matches(str(PurePosixPath(WEB_ACCESS_DROPIN).parent), 0o755, "root", "root")
            ))
        )

    def _install_web_access(self, mode: str, release: Path) -> None:
        payload = self._web_access_payload(mode, release)
        self.host.write_atomic(WEB_ACCESS_PATH, (json.dumps({"mode": mode}) + "\n").encode(), 0o644)
        self.host.set_owner(WEB_ACCESS_PATH, "root", "root")
        if payload is None:
            self.host.remove_file(WEB_ACCESS_DROPIN)
        else:
            directory = str(PurePosixPath(WEB_ACCESS_DROPIN).parent)
            self.host.mkdir(directory, 0o755)
            self.host.set_owner(directory, "root", "root")
            self.host.write_atomic(WEB_ACCESS_DROPIN, payload, 0o644)
            self.host.set_owner(WEB_ACCESS_DROPIN, "root", "root")

    def _require_active_units(self) -> None:
        for unit in UNITS:
            self.host.mutate_command(
                ("systemctl", "is-active", "--quiet", unit)
            )
        if self._read_web_access() == "lan":
            self.host.mutate_command(("systemctl", "enable", "--now", "avahi-daemon.service"))

    def _validate_post_install_probe(self, result: Mapping[str, object]) -> None:
        baseline = _mapping(_mapping(result.get("platform")).get("os_architecture_baseline"))
        if baseline.get("status") != probe.PRESENT:
            raise InstallError("post-install OS/architecture validation failed")
        required = _mapping(_mapping(result.get("gstreamer")).get("required_elements"))
        missing = [
            name
            for name in probe.REQUIRED_GSTREAMER_ELEMENTS
            if _mapping(required.get(name)).get("status") != probe.PRESENT
        ]
        if missing:
            raise InstallError(
                "post-install GStreamer validation failed: " + ", ".join(missing)
            )

    def _apt_transaction(
        self,
        direct_packages: tuple[str, ...],
        *,
        verify_downloads: bool,
    ) -> tuple[tuple[tuple[str, str], ...], str]:
        simulation = self.host.read_command(
            (
                "apt-get",
                "--simulate",
                "--no-install-recommends",
                "--no-remove",
                "--no-upgrade",
                "install",
                *direct_packages,
            )
        )
        if simulation.returncode != 0:
            raise InstallError("APT package simulation failed")
        transaction = parse_apt_simulation(simulation.stdout, direct_packages)
        payload = json.dumps(transaction, separators=(",", ":"), ensure_ascii=True)
        fingerprint = hashlib.sha256(payload.encode("ascii")).hexdigest()
        if verify_downloads:
            with tempfile.TemporaryDirectory(prefix="tinypirelay-apt-") as directory:
                scratch = Path(directory)
                for name, version in transaction:
                    before = set(scratch.iterdir())
                    fetched = self.host.read_command(
                        ("apt-get", "download", f"{name}={version}"), cwd=scratch
                    )
                    if fetched.returncode != 0:
                        raise InstallError(
                            f"APT repository reachability failed for {name}={version}"
                        )
                    if self.host.root == Path("/").resolve():
                        created = set(scratch.iterdir()) - before
                        if not created or any(
                            not item.is_file()
                            or item.suffix != ".deb"
                            or item.stat().st_size <= 0
                            for item in created
                        ):
                            raise InstallError(
                                f"APT download verification produced no package for {name}={version}"
                            )
        return transaction, fingerprint

    def _install_integration_files(self, release: Path) -> None:
        for relative, target, mode in INTEGRATION_FILES:
            if Path(target).name in MAINTENANCE_UNITS and not (release / relative).exists():
                # Older immutable releases predate maintenance; rollback removes it.
                self.host.remove_file(target)
                continue
            self.host.copy_file(release / relative, target, mode)

    def _deactivate_maintenance(self) -> None:
        self._deactivate_units(MAINTENANCE_UNITS)

    def _deactivate_units(self, units: Sequence[str]) -> None:
        try:
            self.host.mutate_command(("systemctl", "disable", "--now", *units))
        except InstallError:
            # Missing units are normal after uninstall or before maintenance existed.
            # Any uncertain stop must preserve the installation and recording state.
            for unit in units:
                result = self.host.read_command(
                    (
                        "systemctl", "show", "--property=LoadState",
                        "--property=ActiveState", unit,
                    )
                )
                if result.returncode != 0 or set(result.stdout.splitlines()) != {
                    "LoadState=not-found", "ActiveState=inactive",
                }:
                    raise

    def _activate_maintenance(self, release: Path) -> None:
        if not (release / "packaging/systemd" / MAINTENANCE_SOCKET).is_file():
            return
        # Stop the old process so the next socket activation imports the new release.
        self.host.mutate_command(("systemctl", "stop", MAINTENANCE_UNIT), ok_returncodes=(0, 5))
        self.host.mutate_command(("systemctl", "enable", MAINTENANCE_SOCKET))
        self.host.mutate_command(("systemctl", "restart", MAINTENANCE_SOCKET))
        self.host.mutate_command(("systemctl", "is-active", "--quiet", MAINTENANCE_SOCKET))

    def _ensure_fixture_directories(self) -> None:
        # systemd-tmpfiles owns production permissions; rooted tests need paths too.
        for path, mode, _user, _group in PRIVATE_DIRECTORIES:
            self.host.mkdir(path, mode)

    def _load_state(self) -> tuple[InstallState | None, str | None]:
        path = "/var/lib/tinypirelay/install-state.json"
        if not self.host.exists(path):
            return None, None
        try:
            value = json.loads(self.host.read_bytes(path).decode("utf-8"))
            if not isinstance(value, dict) or set(value) != {
                "schema_version",
                "phase",
                "target_version",
                "installed_version",
                "previous_version",
                "source_sha256",
                "previous_source_sha256",
            }:
                raise ValueError
            if value["schema_version"] != STATE_SCHEMA_VERSION:
                raise ValueError
            phase = value["phase"]
            if phase not in {*PHASES, "uninstalled"}:
                raise ValueError
            for key in ("target_version", "installed_version", "previous_version"):
                item = value[key]
                if item is not None and (
                    not isinstance(item, str) or VERSION_PATTERN.fullmatch(item) is None
                ):
                    raise ValueError
            for key in ("source_sha256", "previous_source_sha256"):
                item = value[key]
                if item is not None and (
                    not isinstance(item, str) or DIGEST_PATTERN.fullmatch(item) is None
                ):
                    raise ValueError
            if phase != "uninstalled" and value["source_sha256"] is None:
                raise ValueError
            if phase == "uninstalled":
                if (
                    value["target_version"] is not None
                    or value["installed_version"] is not None
                    or value["previous_source_sha256"] is not None
                    or (
                        (value["previous_version"] is None)
                        != (value["source_sha256"] is None)
                    )
                ):
                    raise ValueError
            elif phase == "complete":
                if (
                    value["target_version"] is None
                    or value["target_version"] != value["installed_version"]
                ):
                    raise ValueError
            elif (
                value["target_version"] is None
                or value["installed_version"] != value["previous_version"]
            ):
                raise ValueError
            if phase != "uninstalled" and (
                (value["previous_version"] is None)
                != (value["previous_source_sha256"] is None)
            ):
                raise ValueError
            return (
                InstallState(
                    phase,
                    value["target_version"],
                    value["installed_version"],
                    value["previous_version"],
                    value["source_sha256"],
                    value["previous_source_sha256"],
                ),
                None,
            )
        except (InstallError, UnicodeError, ValueError, json.JSONDecodeError):
            return None, "installation journal is invalid; inspect it before continuing"

    def _write_state(self, state: InstallState) -> None:
        payload = {
            "schema_version": STATE_SCHEMA_VERSION,
            "phase": state.phase,
            "target_version": state.target_version,
            "installed_version": state.installed_version,
            "previous_version": state.previous_version,
            "source_sha256": state.source_sha256,
            "previous_source_sha256": state.previous_source_sha256,
        }
        self.host.write_atomic(
            "/var/lib/tinypirelay/install-state.json",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(),
            0o600,
        )

    def _port_conflict(self, port: int) -> str | None:
        result = self.host.read_command(("ss", "-H", "-ltn"))
        if result.returncode != 0:
            return "TCP port preflight could not run `ss -H -ltn`"
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 4 and _address_port(fields[3]) == port:
                return f"TCP port {port} is already listening"
        return None

    def _unmanaged_conflicts(
        self, state: InstallState | None
    ) -> tuple[str, ...]:
        if state is not None and state.phase != "uninstalled":
            return ()
        program_paths = (
            "/opt/tinypirelay",
            "/etc/systemd/system/tinypirelay-media.service",
            "/etc/systemd/system/tinypirelay-web.service",
            WEB_ACCESS_DROPIN,
            "/etc/systemd/system/tinypirelay-maintenance.service",
            "/etc/systemd/system/tinypirelay-maintenance.socket",
            "/usr/lib/sysusers.d/tinypirelay.conf",
            "/usr/lib/tmpfiles.d/tinypirelay.conf",
            "/usr/bin/tinypirelay",
            "/run/tinypirelay",
            "/run/tinypirelay-maintenance",
            "/var/cache/tinypirelay-media",
        )
        fresh_only = (
            "/etc/tinypirelay",
            "/var/lib/tinypirelay",
        )
        conflicts: list[str] = []
        for path in program_paths + (fresh_only if state is None else ()):
            if (
                state is None
                and path == "/var/lib/tinypirelay"
                and self.host.is_empty_directory(path)
            ):
                continue
            present = (
                self.host.current_exists()
                if path == "/opt/tinypirelay/current"
                else self.host.exists(path)
            )
            if present:
                conflicts.append(path)
        return tuple(conflicts)

    def _identity_error(self, *, require_present: bool) -> str | None:
        expected = {
            "tinypirelay-media": {"tinypirelay-control", "audio"},
            "tinypirelay-web": {"tinypirelay-control"},
        }
        found_users = 0
        for user, memberships in expected.items():
            result = self.host.read_command(("getent", "passwd", user))
            if result.returncode in (1, 2):
                if require_present:
                    return f"required service identity is missing: {user}"
                continue
            if result.returncode != 0:
                return f"could not verify service identity: {user}"
            fields = result.stdout.strip().split(":")
            if len(fields) != 7 or fields[0] != user:
                return f"service identity record is invalid: {user}"
            try:
                uid = int(fields[2])
            except ValueError:
                return f"service identity UID is invalid: {user}"
            if uid < 1 or uid >= 1000 or fields[6] not in {
                "/usr/sbin/nologin",
                "/sbin/nologin",
                "/bin/false",
            }:
                return f"service identity is not a no-login system account: {user}"
            groups = self.host.read_command(("id", "-nG", user))
            if groups.returncode != 0 or not memberships <= set(groups.stdout.split()):
                return f"service identity group membership is incomplete: {user}"
            found_users += 1
        group = self.host.read_command(
            ("getent", "group", "tinypirelay-control")
        )
        if group.returncode in (1, 2):
            if require_present or found_users:
                return "required service group is missing: tinypirelay-control"
            return None
        if group.returncode != 0:
            return "could not verify service group: tinypirelay-control"
        fields = group.stdout.strip().split(":")
        try:
            valid_group = (
                len(fields) == 4
                and fields[0] == "tinypirelay-control"
                and 1 <= int(fields[2]) < 1000
            )
        except ValueError:
            valid_group = False
        return None if valid_group else "service group is not a system group"


def render_plan(plan: InstallPlan | UninstallPlan) -> str:
    lines = ["TinyPiRelay read-only installation plan"]
    if isinstance(plan, InstallPlan):
        lines.extend((f"Version: {plan.version}", f"Mode: {plan.mode}"))
        if plan.existing_version:
            lines.append(f"Installed version: {plan.existing_version}")
    else:
        lines.append("Mode: uninstall")
    lines.append("Planned changes:")
    lines.extend(f"- {item}" for item in plan.actions)
    if not plan.actions:
        lines.append("- None")
    lines.append("Preflight errors:")
    lines.extend(f"- {item}" for item in plan.errors)
    if not plan.errors:
        lines.append("- None")
    return "\n".join(lines)


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def parse_apt_simulation(
    output: str, direct_packages: Sequence[str]
) -> tuple[tuple[str, str], ...]:
    """Return the complete new-package transaction or reject ambiguous APT output."""

    installs: list[tuple[str, str]] = []
    for line in output.splitlines():
        if line.startswith("Remv "):
            raise InstallError("APT simulation proposed package removal")
        if not line.startswith("Inst "):
            continue
        match = re.fullmatch(
            r"Inst\s+([a-z0-9][a-z0-9+.-]*(?::[a-z0-9][a-z0-9-]*)?)"
            r"(?:\s+\[([^\]]+)\])?\s+\(([^\s()]+)(?:\s+.*)?\)",
            line,
        )
        if match is None:
            raise InstallError("APT simulation contained an unparseable install action")
        if match.group(2) is not None:
            raise InstallError(
                f"APT simulation proposed changing installed package {match.group(1)}"
            )
        installs.append((match.group(1), match.group(3)))
    summary = re.search(
        r"(?m)^(\d+) upgraded, (\d+) newly installed, (\d+) to remove(?: and \d+ not upgraded)?\.\s*$",
        output,
    )
    if summary is None:
        raise InstallError("APT simulation did not contain a complete transaction summary")
    upgraded, new, removed = (int(item) for item in summary.groups())
    if upgraded or removed:
        raise InstallError("APT simulation proposed upgrades or removals")
    if new != len(installs) or not installs:
        raise InstallError("APT simulation transaction count is inconsistent")
    if len(set(installs)) != len(installs):
        raise InstallError("APT simulation contains duplicate install actions")
    installed_names = {name.split(":", 1)[0] for name, _version in installs}
    direct_names = {item.split("=", 1)[0].split(":", 1)[0] for item in direct_packages}
    if not direct_names <= installed_names:
        raise InstallError("APT simulation omitted a direct package")
    return tuple(sorted(installs))


def _phase_done(actual: str, expected: str) -> bool:
    if actual == "uninstalled":
        return False
    return PHASES.index(actual) >= PHASES.index(expected)


def _address_port(value: str) -> int | None:
    match = re.search(r":(\d+)\Z", value)
    return int(match.group(1)) if match else None


def _has_tty() -> bool:
    try:
        descriptor = os.open("/dev/tty", os.O_RDWR | getattr(os, "O_NOCTTY", 0))
    except OSError:
        return False
    os.close(descriptor)
    return True


def _is_root() -> bool:
    return os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan", help="run read-only preflight")
    plan.add_argument("--config", type=Path)
    plan.add_argument("--web-access", choices=("lan", "loopback"))
    for name in ("install", "upgrade"):
        command = subparsers.add_parser(name, help=f"plan or apply a staged {name}")
        command.add_argument("--config", type=Path)
        command.add_argument("--credential-fd", type=int)
        command.add_argument("--web-access", choices=("lan", "loopback"))
        command.add_argument("--apply", action="store_true")
    uninstall = subparsers.add_parser("uninstall", help="plan or apply uninstall")
    uninstall.add_argument("--apply", action="store_true")
    uninstall.add_argument("--remove-data", action="store_true")
    uninstall.add_argument("--confirm-remove-data")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    source_dir = Path(__file__).resolve().parents[2]
    host = LocalHost()
    installer = Installer(
        host,
        source_dir,
        lambda: probe.collect_probe(),
    )
    try:
        if args.command == "uninstall":
            plan = installer.plan_uninstall(remove_data=args.remove_data)
            print(render_plan(plan))
            if not args.apply:
                return 0 if plan.ready else 2
            installer.uninstall(plan, confirmation=args.confirm_remove_data)
            print("TinyPiRelay uninstall completed")
            return 0

        credential_fd = getattr(args, "credential_fd", None)
        plan = installer.plan(
            args.config,
            credential_fd_available=credential_fd is not None,
            web_access=args.web_access,
        )
        if args.command == "install" and plan.mode not in {"install", "resume", "repair", "noop"}:
            plan = _with_error(plan, "install cannot replace a completed different version; use upgrade")
        if args.command == "upgrade" and plan.mode not in {"upgrade", "resume", "repair", "noop"}:
            plan = _with_error(plan, "upgrade requires an existing installation")
        if args.command == "upgrade" and args.config is not None:
            plan = _with_error(plan, "upgrade uses the installed configuration; omit --config")
        print(render_plan(plan))
        if args.command == "plan" or not getattr(args, "apply", False):
            return 0 if plan.ready else 2
        if not plan.ready:
            return 2
        credential = None
        if plan.needs_credential:
            credential = credential_from_fd(credential_fd)
        state = installer.apply(plan, credential)
        print(f"TinyPiRelay {state.installed_version} installation complete")
        if plan.web_access == "lan":
            import socket
            print(f"Open http://{socket.gethostname().split('.')[0]}.local/ or http://<device-ip>/ on your trusted LAN.")
            print("HTTP is unencrypted; use only on your trusted private LAN.")
        else:
            print("Web GUI: http://127.0.0.1:8080/ (use an SSH tunnel for remote access)")
        if plan.setup_pending and not plan.needs_credential:
            print("First visit: create your administrator username and password in the web GUI.")
        return 0
    except InstallError as exc:
        print(f"installer failed: {exc}", file=sys.stderr)
        return 2


def _with_error(plan: InstallPlan, error: str) -> InstallPlan:
    return replace(plan, errors=(*plan.errors, error))


if __name__ == "__main__":
    raise SystemExit(main())
