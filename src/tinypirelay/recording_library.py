"""Bounded, Linux-only access to the media user's configured FLAC library."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import secrets
import shutil
import stat
import subprocess
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from typing import Callable, Mapping

from .media_config import RecordingConfig, STORAGE_LIMIT_PERCENT, check_recording_destination

PAGE_SIZE = 6
MAX_DIRECTORY_ENTRIES = 4096
MAX_READ_BYTES = 24 * 1024
CHECK_TIMEOUT_SECONDS = 300
# ponytail: one five-minute, 1 GiB recovery copy; larger salvage belongs on another machine.
RECOVERY_MAX_BYTES = 1024 * 1024 * 1024
RECOVERY_RESERVE_BYTES = 16 * 1024 * 1024
_ACTIVE = {"starting", "running", "stopping"}


class RecordingLibraryError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def _error(code: str, message: str) -> None:
    raise RecordingLibraryError(code, message)


def _version(value: os.stat_result) -> tuple[int, ...]:
    return (value.st_dev, value.st_ino, value.st_mtime_ns, value.st_ctime_ns, value.st_size)


def _regular(value: os.stat_result) -> bool:
    return stat.S_ISREG(value.st_mode) and value.st_nlink == 1


def _streaminfo(data: bytes) -> dict[str, object]:
    """STREAMINFO is metadata, never evidence of a complete integrity check."""
    result: dict[str, object] = dict(duration_seconds=None, sample_rate_hz=None,
                                    channels=None, bits_per_sample=None)
    if len(data) < 42 or data[:4] != b"fLaC" or data[4] & 127 != 0 or data[5:8] != b"\0\0\x22":
        return result
    packed = int.from_bytes(data[18:26], "big")
    rate, channels, bits = packed >> 44, ((packed >> 41) & 7) + 1, ((packed >> 36) & 31) + 1
    samples = packed & ((1 << 36) - 1)
    if not 1 <= rate <= 655350 or not 4 <= bits <= 32:
        return result
    result.update(sample_rate_hz=rate, channels=channels, bits_per_sample=bits,
                  duration_seconds=samples / rate if samples else None)
    return result


class RecordingLibrary:
    def __init__(self, config: Callable[[], RecordingConfig], snapshot: Callable[[], Mapping]) -> None:
        self._config, self._snapshot = config, snapshot
        self._key = secrets.token_bytes(32)
        self._lock = threading.Lock()
        self._files: OrderedDict[str, tuple] = OrderedDict()
        self._checks: OrderedDict[str, dict] = OrderedDict()
        self._checking: str | None = None

    @contextmanager
    def _root(self):
        if os.name != "posix" or not all(hasattr(os, flag) for flag in ("O_NOFOLLOW", "O_PATH")):
            _error("storage_unavailable", "Secure recording access requires Linux.")
        config = self._config()
        path = config.directory
        if not os.path.isabs(path) or ".." in path.split("/"):
            _error("storage_unavailable", "The recording directory must be an absolute safe path.")
        # Reuse recording mount policy, but allow downloading/deleting on a full disk.
        destination = check_recording_destination(config)
        if destination.reason not in {"ok", "storage_threshold", "not_writable"}:
            _error("storage_unavailable", "The configured recording filesystem is unavailable.")
        descriptor = None
        try:
            flags = os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            # Ancestors may grant traversal only (the installed parent is 0710).
            descriptor = os.open("/", os.O_PATH | flags)
            for component in path.split("/"):
                if component and component != ".":
                    child = os.open(component, os.O_PATH | flags, dir_fd=descriptor)
                    os.close(descriptor)
                    descriptor = child
            child = os.open(".", os.O_RDONLY | flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            value = os.fstat(descriptor)
            if value.st_dev != destination.st_dev:
                _error("storage_unavailable", "The recording filesystem changed; refresh and retry.")
            if value.st_uid != os.geteuid() or value.st_mode & 0o022:
                _error("storage_unavailable", "The recording directory must be media-owned and not writable by other users.")
            yield config, descriptor, (path, value.st_dev, value.st_ino)
        except OSError as exc:
            raise RecordingLibraryError("storage_unavailable", "The configured recording directory cannot be accessed safely.") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _recording(self) -> Mapping:
        return self._snapshot().get("state", {}).get("recording", {})

    def _recording_busy(self) -> bool:
        recording = self._recording()
        return bool(recording.get("state") in _ACTIVE or recording.get("active_files") or recording.get("rotation_pending"))

    def _protected(self, path: str, name: str) -> bool:
        recording = self._recording()
        # A filesink opens before its deferred branch-created event arrives.
        if recording.get("rotation_pending") or recording.get("state") in {"starting", "stopping"}:
            return True
        candidate = os.path.join(os.path.normpath(path), name)
        if any(os.path.normpath(item) == candidate for item in recording.get("active_files", ())):
            return True
        current = recording.get("current_file")
        if current:
            return os.path.normpath(str(current)) == candidate
        return recording.get("state") in _ACTIVE

    def _register(self, root: tuple, name: str, value: os.stat_result) -> str:
        binding = (root, name, _version(value))
        file_id = hmac.new(self._key, json.dumps(binding, ensure_ascii=True).encode(), hashlib.sha256).hexdigest()
        with self._lock:
            self._files[file_id] = binding
            self._files.move_to_end(file_id)
            # ponytail: only recently displayed files are addressable; refresh after 256 different entries.
            while len(self._files) > 256:
                self._files.popitem(last=False)
        return file_id

    @staticmethod
    def _file_open(root_fd: int, name: str) -> int:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=root_fd)
        if not _regular(os.fstat(descriptor)):
            os.close(descriptor)
            _error("unsafe_file", "Only ordinary, single-link FLAC recordings are available.")
        return descriptor

    @contextmanager
    def _open(self, file_id: str):
        if not isinstance(file_id, str) or len(file_id) != 64:
            _error("invalid_arguments", "A current recording file ID is required.")
        with self._lock:
            binding = self._files.get(file_id)
        if binding is None:
            _error("stale_file", "The recording changed or its listing expired; refresh the list.")
        with self._root() as (config, root_fd, root):
            if binding[0] != root:
                _error("stale_file", "The recording filesystem changed; refresh the list.")
            descriptor = None
            try:
                descriptor = self._file_open(root_fd, binding[1])
                value = os.fstat(descriptor)
                if _version(value) != binding[2]:
                    _error("stale_file", "The recording changed; refresh the list.")
                yield config, root_fd, descriptor, binding[1], value
            except OSError as exc:
                raise RecordingLibraryError("stale_file", "The recording is no longer available; refresh the list.") from exc
            finally:
                if descriptor is not None:
                    os.close(descriptor)

    def _record(self, root: tuple, name: str, descriptor: int, value: os.stat_result) -> dict:
        file_id = self._register(root, name, value)
        metadata = _streaminfo(os.pread(descriptor, 42, 0))
        active = self._protected(root[0], name)
        with self._lock:
            check = dict(self._checks.get(file_id, {"state": "unchecked", "message": "Metadata only; integrity has not been checked."}))
            busy = self._checking is not None
        finalized = metadata["duration_seconds"] is not None and value.st_size > 42
        status = "active" if active else "finalized" if finalized else "needs_check"
        if not active and (check["state"] in {"failed", "timeout"} or check.get("action") == "recover"):
            status = "needs_check"
        reason = ("Stop recording before attempting recovery." if active or self._recording_busy() else
                  "Wait for the current check or recovery." if busy else
                  "Recovery requires FFmpeg installed on the device." if not shutil.which("ffmpeg") else None)
        return dict(file_id=file_id, name=name, size_bytes=value.st_size,
                    modified_unix=value.st_mtime, **metadata, status=status, check=check,
                    can_play=not active and status == "finalized", can_download=not active,
                    can_delete=not active and check["state"] not in {"checking", "recovering"},
                    can_check=not active and not busy and not self._recording_busy(),
                    can_recover=reason is None, recovery_unavailable_reason=reason)

    def list(self, page: int) -> dict:
        if type(page) is not int or page < 1:
            _error("invalid_arguments", "Page must be a positive integer.")
        with self._root() as (_config, root_fd, root):
            entries, skipped = [], 0
            with os.scandir(root_fd) as iterator:
                for count, entry in enumerate(iterator, 1):
                    if count > MAX_DIRECTORY_ENTRIES:
                        _error("library_too_large", "The recording directory exceeds the 4096-entry catalog limit.")
                    if not entry.name.lower().endswith(".flac"):
                        continue
                    value = entry.stat(follow_symlinks=False)
                    if not _regular(value) or any(ord(char) < 32 or ord(char) == 127 for char in entry.name):
                        skipped += 1
                        continue
                    entries.append((entry.name, value))
            entries.sort(key=lambda item: (-item[1].st_mtime_ns, item[0]))
            total_pages = max(1, math.ceil(len(entries) / PAGE_SIZE))
            if page > total_pages:
                _error("invalid_arguments", "Page is outside the current recording list; refresh the list.")
            items = []
            for name, _value in entries[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]:
                descriptor = self._file_open(root_fd, name)
                try:
                    items.append(self._record(root, name, descriptor, os.fstat(descriptor)))
                finally:
                    os.close(descriptor)
        with self._lock:
            # Retain the last outcome even when a new recovered copy moves the source off this page.
            check = dict(self._checks[self._checking] if self._checking else next(reversed(self._checks.values()))) if self._checks else None
        return dict(items=items, page=page, page_size=PAGE_SIZE, total=len(entries),
                    total_pages=total_pages, skipped_unsafe=skipped, check=check)

    def info(self, file_id: str) -> dict:
        with self._open(file_id) as (config, root_fd, descriptor, name, value):
            root_stat = os.fstat(root_fd)
            return {"file": self._record((config.directory, root_stat.st_dev, root_stat.st_ino), name, descriptor, value)}

    def read(self, file_id: str, offset: int, length: int) -> dict:
        if type(offset) is not int or offset < 0 or type(length) is not int or not 1 <= length <= MAX_READ_BYTES:
            _error("invalid_arguments", "A nonnegative offset and 1–24576 byte length are required.")
        with self._open(file_id) as (config, _root_fd, descriptor, name, value):
            if self._protected(config.directory, name):
                _error("active_recording", "The active recording cannot be read until it closes.")
            if offset > value.st_size:
                _error("invalid_range", "The requested offset is outside this recording.")
            data = os.pread(descriptor, min(length, value.st_size - offset), offset)
            if _version(os.fstat(descriptor)) != _version(value):
                _error("stale_file", "The recording changed during reading; refresh the list.")
            return dict(file_id=file_id, offset=offset, size_bytes=value.st_size,
                        data_base64=base64.b64encode(data).decode("ascii"), eof=offset + len(data) >= value.st_size)

    def delete(self, file_id: str) -> dict:
        with self._open(file_id) as (config, root_fd, _descriptor, name, value):
            with self._lock:
                if self._checking == file_id:
                    _error("check_in_progress", "Wait for this recording's check or recovery before deleting it.")
            if self._protected(config.directory, name):
                _error("active_recording", "The active recording cannot be deleted.")
            current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            if not _regular(current) or _version(current) != _version(value):
                _error("stale_file", "The recording changed; refresh the list before deleting.")
            # The media-owned directory is the trust boundary; the writer never reuses closed filenames.
            os.unlink(name, dir_fd=root_fd)
            with self._lock:
                self._files.pop(file_id, None)
                self._checks.pop(file_id, None)
            try:
                os.fsync(root_fd)
            except OSError:
                return dict(deleted=True, file_id=file_id, durability_confirmed=False)
        return dict(deleted=True, file_id=file_id, durability_confirmed=True)

    @staticmethod
    def _decoder() -> tuple[list[str], str] | None:
        flac, ffmpeg, gst = (shutil.which(name) for name in ("flac", "ffmpeg", "gst-launch-1.0"))
        if flac:
            command, method = [flac, "--test", "--silent", "-"], "flac-test"
        elif ffmpeg:
            command, method = [ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-xerror", "-err_detect", "crccheck+bitstream+buffer+explode", "-f", "flac", "-i", "pipe:0", "-map", "0:a:0", "-f", "null", "-"], "ffmpeg-decode"
        elif gst:
            command, method = [gst, "-q", "fdsrc", "fd=0", "!", "flacparse", "!", "flacdec", "!", "fakesink", "sync=false"], "gstreamer-decode"
        else:
            return None
        nice = shutil.which("nice")
        return ([nice, "-n", "10", *command] if nice else command), method

    def check(self, file_id: str) -> dict:
        with self._open(file_id) as (config, _root_fd, descriptor, name, value):
            if self._protected(config.directory, name) or self._recording_busy():
                _error("recording_busy", "Stop recording before running an integrity check.")
            decoder = self._decoder()
            with self._lock:
                if self._checking is not None:
                    _error("check_in_progress", "One recording check or recovery is already running.")
                result = dict(file_id=file_id, state="checking" if decoder else "unavailable",
                              message="Checking the complete file." if decoder else "No supported decoder is installed.")
                self._checks[file_id] = result
                self._checks.move_to_end(file_id)
                while len(self._checks) > 128:
                    self._checks.popitem(last=False)
                if decoder is None:
                    return dict(accepted=False, check=dict(result))
                duplicate = None
                try:
                    duplicate = os.dup(descriptor)
                    worker = threading.Thread(target=self._check_worker, args=(file_id, duplicate, _version(value), decoder), daemon=True)
                    self._checking = file_id
                    worker.start()
                except Exception:
                    self._checking = None
                    self._checks.pop(file_id, None)
                    if duplicate is not None:
                        os.close(duplicate)
                    raise
                return dict(accepted=True, check=dict(result))

    def _check_worker(self, file_id: str, descriptor: int, version: tuple, decoder: tuple) -> None:
        command, method = decoder
        state, message = "failed", "The recording could not be checked."
        process = None
        try:
            with os.fdopen(descriptor, "rb", buffering=0) as source:
                process = subprocess.Popen(command, stdin=source, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL, close_fds=True)
                deadline = time.monotonic() + CHECK_TIMEOUT_SECONDS
                while True:
                    if self._recording_busy():
                        state, message = "cancelled", "Check cancelled because recording started; completeness is unknown."
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        state, message = "timeout", "Check exceeded the five-minute limit; completeness is unknown."
                        break
                    try:
                        returncode = process.wait(timeout=min(1, remaining))
                    except subprocess.TimeoutExpired:
                        continue
                    if returncode == 0 and _version(os.fstat(source.fileno())) == version:
                        if method == "flac-test":
                            state, message = "passed", "FLAC validator passed; this does not guarantee the complete original recording, provenance or recovery."
                        else:
                            state, message = "decoded", "Available audio decoded, but this decoder may miss a truncated tail; file completeness is unverified."
                    else:
                        message = "Decoder reported an error or the recording changed; keep the original for investigation."
                    break
        except (OSError, ValueError):
            pass
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
            with self._lock:
                self._checks[file_id] = dict(file_id=file_id, state=state, message=message, method=method)
                self._checking = None

    @staticmethod
    def _recovery_space(root_fd: int) -> int:
        usage = os.fstatvfs(root_fd)
        total = usage.f_blocks * usage.f_frsize
        used = (usage.f_blocks - usage.f_bfree) * usage.f_frsize
        return min(usage.f_bavail * usage.f_frsize,
                   total * STORAGE_LIMIT_PERCENT // 100 - used) - RECOVERY_RESERVE_BYTES

    def recover(self, file_id: str) -> dict:
        with self._open(file_id) as (config, root_fd, descriptor, name, value):
            if self._protected(config.directory, name) or self._recording_busy():
                _error("recording_busy", "Stop recording before attempting recovery.")
            executable = shutil.which("ffmpeg")
            with self._lock:
                if self._checking is not None:
                    _error("check_in_progress", "One recording check or recovery is already running.")
                result = dict(file_id=file_id, action="recover", state="recovering" if executable else "unavailable",
                              message="Recovering to a separate copy; the original is preserved." if executable else
                              "Recovery requires FFmpeg installed on the device.")
                if executable:
                    if not check_recording_destination(config).safe:
                        _error("storage_unavailable", "The recording destination is not safe for a recovery copy.")
                    budget = min(RECOVERY_MAX_BYTES, self._recovery_space(root_fd))
                    if budget <= 0:
                        _error("storage_unavailable", "Insufficient space below the recording limit for a recovery copy.")
                self._checks[file_id] = result
                self._checks.move_to_end(file_id)
                while len(self._checks) > 128:
                    self._checks.popitem(last=False)
                if not executable:
                    return dict(accepted=False, check=dict(result))
                duplicates = []
                try:
                    duplicates.append(os.dup(descriptor))
                    duplicates.append(os.dup(root_fd))
                    worker = threading.Thread(target=self._recover_worker,
                        args=(file_id, *duplicates, config, name, _version(value), executable, budget), daemon=True)
                    self._checking = file_id
                    worker.start()
                except Exception:
                    self._checking = None
                    self._checks.pop(file_id, None)
                    for duplicate in duplicates:
                        os.close(duplicate)
                    raise
                return dict(accepted=True, check=dict(result))

    def _recover_worker(self, file_id: str, source_fd: int, root_fd: int, config: RecordingConfig,
                        name: str, version: tuple, executable: str, budget: int) -> None:
        result = dict(file_id=file_id, action="recover", state="failed", method="ffmpeg-recovery",
                      message="Recovery failed; the original is unchanged.")
        temporary = ".tinypirelay-recovery-" + secrets.token_hex(12) + ".partial"
        output_fd = None
        created = False
        deadline = time.monotonic() + CHECK_TIMEOUT_SECONDS

        def guard():
            if time.monotonic() >= deadline:
                _error("timeout", "Recovery exceeded the five-minute limit; use another machine for this file.")
            if self._recording_busy() or self._config() != config:
                _error("cancelled", "Recovery cancelled because recording started or its configuration changed.")
            with self._root() as (_, _, current_root):
                if current_root != root_identity or not check_recording_destination(config).safe:
                    _error("cancelled", "Recovery cancelled because the recording filesystem changed or became unsafe.")
            current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            if _version(current) != version or _version(os.fstat(source_fd)) != version:
                _error("cancelled", "Recovery cancelled because the source recording changed.")
            if self._recovery_space(root_fd) <= 0 or (output_fd is not None and os.fstat(output_fd).st_size >= budget):
                _error("failed", "Recovery reached its safe disk-space or 1 GiB output limit; use another machine.")

        def run(arguments, input_fd, output):
            guard()
            os.lseek(input_fd, 0, os.SEEK_SET)
            process = subprocess.Popen([*command, *arguments], stdin=input_fd, stdout=output,
                                       stderr=subprocess.DEVNULL, close_fds=True)
            try:
                while process.poll() is None:
                    guard()
                    try:
                        process.wait(timeout=min(.5, max(.001, deadline - time.monotonic())))
                    except subprocess.TimeoutExpired:
                        pass
                guard()
                if process.returncode != 0:
                    _error("failed", "The decoder could not recover and validate usable audio; the original is unchanged.")
                # Only the MD5 muxer uses PIPE and writes one fixed-size checksum line.
                return process.stdout.read(128) if process.stdout is not None else b""
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                if process.stdout is not None:
                    process.stdout.close()

        try:
            root_stat = os.fstat(root_fd)
            root_identity = (config.directory, root_stat.st_dev, root_stat.st_ino)
            nice = shutil.which("nice")
            command = ([nice, "-n", "10"] if nice else []) + [executable, "-nostdin", "-hide_banner", "-v", "error"]
            guard()
            source_metadata = _streaminfo(os.pread(source_fd, 42, 0))
            output_fd = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                                0o600, dir_fd=root_fd)
            created = True
            # Strict frame CRCs discard damaged frames; unlike validation, recovery must not use -xerror.
            run(["-threads", "1", "-err_detect", "crccheck+bitstream+buffer+explode", "-f", "flac", "-i", "pipe:0",
                 "-map", "0:a:0", "-map_metadata", "-1", "-threads", "1", "-c:a", "flac", "-compression_level", "0",
                 "-fs", str(budget), "-f", "flac", "-y", "file:/proc/self/fd/1"], source_fd, output_fd)
            header = os.pread(output_fd, 42, 0)
            metadata = _streaminfo(header)
            bits = metadata["bits_per_sample"]
            if not metadata["duration_seconds"] or bits not in {16, 24, 32}:
                _error("failed", "No usable audio with a supported sample depth was recovered; the original is unchanged.")
            if any(source_metadata[key] is not None and source_metadata[key] != metadata[key]
                   for key in ("sample_rate_hz", "channels", "bits_per_sample")):
                _error("failed", "Recovery would change the recording format; no copy was published.")
            digest = run(["-xerror", "-threads", "1", "-err_detect", "crccheck+bitstream+buffer+explode",
                          "-f", "flac", "-i", "pipe:0", "-map", "0:a:0", "-threads", "1", "-c:a", f"pcm_s{bits}le",
                          "-f", "md5", "pipe:1"], output_fd, subprocess.PIPE)
            if digest.strip() != b"MD5=" + header[26:42].hex().encode("ascii"):
                _error("failed", "The recovered copy failed its audio checksum; no copy was published.")
            os.fsync(output_fd)
            guard()
            output_name = "tinypirelay-recovered-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(6) + ".flac"
            # link publishes without overwriting anything; only our private temporary file is removed.
            os.link(temporary, output_name, src_dir_fd=root_fd, dst_dir_fd=root_fd, follow_symlinks=False)
            try:
                os.unlink(temporary, dir_fd=root_fd)
            except OSError:
                # Undo only the link we just created, never leave a silent successful publication.
                try:
                    os.unlink(output_name, dir_fd=root_fd)
                except OSError:
                    result.update(output_name=output_name,
                                  message="Recovery copy validated, but publication cleanup failed. The original is unchanged; inspect the reported copy before use.")
                raise
            created = False
            result.update(state="recovered", output_name=output_name, duration_seconds=metadata["duration_seconds"],
                          message="Recovered copy decoded and passed its audio checksum. The original is unchanged; missing or damaged audio cannot be restored.",
                          durability_confirmed=True)
            try:
                os.fsync(root_fd)
            except OSError:
                result.update(durability_confirmed=False,
                              message="Recovered copy is available, but disk durability could not be confirmed. Keep the original; missing audio cannot be restored.")
        except RecordingLibraryError as exc:
            result.update(state=exc.code if exc.code in {"cancelled", "timeout"} else "failed", message=exc.message)
        except (OSError, ValueError):
            pass
        finally:
            if output_fd is not None:
                os.close(output_fd)
            if created:
                try:
                    os.unlink(temporary, dir_fd=root_fd)
                except OSError:
                    result["message"] += " A hidden partial copy may remain; manual inspection is needed."
            os.close(source_fd)
            os.close(root_fd)
            with self._lock:
                self._checks[file_id] = result
                self._checking = None
