"""Bounded, read-only Linux appliance measurements; no threads or subprocesses.

Call ``sample`` from one shared worker. First/reset counter samples are unknown,
not zero. Network rates describe the selected interface, not SRT traffic; disk
rates describe the destination partition, not the sum of it and its parent.
Recording totals are flat, regular, same-filesystem FLAC files (30-second cache).
ETA is an estimate from the current fragment's average size/time, not disk I/O.
It requires a matching elapsed-file identity and at least five measured seconds.
"""

from __future__ import annotations

import ipaddress
import math
import os
import platform
import re
import shlex
import socket
import stat
import struct
import time
from pathlib import Path
from typing import Callable, Mapping

from .media_config import STORAGE_LIMIT_PERCENT, MediaConfig, check_recording_destination


MAX_SCAN_ENTRIES = 4096
SCAN_INTERVAL_SECONDS = 30.0
VERSION_PATH = Path(__file__).resolve().parents[2] / "VERSION"


def _text(path: Path, limit: int = 65536) -> str | None:
    try:
        with path.open("rb") as handle:
            value = handle.read(limit + 1)
        if len(value) > limit:
            return None
        return value.decode("utf-8").strip().strip("\0")
    except (OSError, UnicodeError, ValueError):
        return None


def _number(value: object, *, integer: bool = False, minimum: float = 0) -> int | float | None:
    try:
        if isinstance(value, bool) or value is None:
            return None
        result = int(value) if integer else float(value)
        return result if result >= minimum and math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _major_minor(device: int) -> str | None:
    try:
        return f"{os.major(device)}:{os.minor(device)}"
    except (AttributeError, OverflowError, ValueError):
        return None


def _interface_name(value: str) -> bool:
    return bool(value and value != "lo" and len(value.encode("utf-8")) < 16 and not any(character in value for character in "/\\\0 \t\n"))


def _ipv4_address(interface: str) -> str | None:
    """Local address ioctl only: this socket never connects or sends a packet."""
    try:
        import fcntl

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as handle:
            result = fcntl.ioctl(handle.fileno(), 0x8915, struct.pack("256s", interface.encode("utf-8")))
        address = ipaddress.IPv4Address(result[20:24])
        return None if address.is_unspecified or address.is_loopback else str(address)
    except (ImportError, OSError, ValueError):
        return None


class DeviceTelemetryCollector:
    def __init__(
        self,
        *,
        proc_root: str | Path = "/proc",
        sys_root: str | Path = "/sys",
        etc_root: str | Path = "/etc",
        clock: Callable[[], float] = time.monotonic,
        destination_check: Callable = check_recording_destination,
    ) -> None:
        self.proc = Path(proc_root)
        self.sys = Path(sys_root)
        self.etc = Path(etc_root)
        self.clock = clock
        self.destination_check = destination_check
        self._cpu_previous: tuple[int, ...] | None = None
        self._rates: dict[str, tuple[object, float, tuple[int, ...]]] = {}
        self._scan_cache: tuple[object, float, int | None, bool] | None = None
        self._device: dict[str, object] | None = None

    def _identity(self) -> dict[str, object]:
        if self._device is None:
            release = {}
            for line in (_text(self.etc / "os-release", 8192) or "").splitlines():
                key, separator, value = line.partition("=")
                if separator and key in {"PRETTY_NAME", "NAME"}:
                    try:
                        release[key] = " ".join(shlex.split(value))[:256] or None
                    except ValueError:
                        pass
            try:
                hostname = _text(self.proc / "sys/kernel/hostname", 256) or socket.gethostname()
            except OSError:
                hostname = None
            machine = platform.machine()
            self._device = {
                "hostname": hostname,
                "model": _text(self.sys / "firmware/devicetree/base/model", 512) or _text(self.proc / "device-tree/model", 512),
                "os": release.get("PRETTY_NAME") or release.get("NAME"),
                "architecture": f"{struct.calcsize('P') * 8}-bit ({machine})" if machine else None,
                "software_version": _text(VERSION_PATH, 128),
            }
        return dict(self._device)

    def _system(self) -> dict[str, object]:
        cpu = None
        fields = (_text(self.proc / "stat") or "").splitlines()
        values = fields[0].split() if fields else []
        counters = tuple(_number(value, integer=True) for value in values[1:9])
        if values[:1] == ["cpu"] and len(counters) == 8 and all(value is not None for value in counters):
            previous, self._cpu_previous = self._cpu_previous, counters
            if previous is not None:
                deltas = [current - old for current, old in zip(counters, previous)]
                total = sum(deltas)
                if min(deltas) >= 0 and total > 0:
                    # Guest and guest_nice are already included in user/nice.
                    cpu = 100 * (total - deltas[3] - deltas[4]) / total
        else:
            self._cpu_previous = None
        memory = {}
        for line in (_text(self.proc / "meminfo") or "").splitlines():
            key, separator, value = line.partition(":")
            parts = value.split()
            if separator and key in {"MemTotal", "MemAvailable"} and len(parts) == 2 and parts[1] == "kB":
                count = _number(parts[0], integer=True)
                memory[key] = count * 1024 if count is not None else None
        total, available = memory.get("MemTotal"), memory.get("MemAvailable")
        if total == 0:
            total = None
        if available is not None and (total is None or available > total):
            available = None
        load = [_number(value) for value in (_text(self.proc / "loadavg", 1024) or "").split()[:3]]
        uptime = (_text(self.proc / "uptime", 1024) or "").split()
        temperature = _number(_text(self.sys / "class/thermal/thermal_zone0/temp", 128), minimum=-50000)
        return {
            "cpu_percent": cpu,
            "temperature_c": temperature / 1000 if temperature is not None and temperature <= 150000 else None,
            "memory_total_bytes": total,
            "memory_available_bytes": available,
            "memory_used_bytes": total - available if total is not None and available is not None else None,
            "load_average": load if len(load) == 3 and all(value is not None for value in load) else None,
            "uptime_seconds": _number(uptime[0]) if uptime else None,
        }

    def _rate(self, name: str, identity: object, counters: tuple[int | None, ...], now: float) -> tuple[float | None, ...]:
        unknown = (None,) * len(counters)
        previous = self._rates.pop(name, None)
        if any(value is None for value in counters):
            return unknown
        self._rates[name] = (identity, now, counters)
        if previous is None or previous[0] != identity or now <= previous[1]:
            return unknown
        deltas = [current - old for current, old in zip(counters, previous[2])]
        if min(deltas) < 0:
            return unknown
        rates = tuple(value / (now - previous[1]) for value in deltas)
        return rates if all(math.isfinite(value) for value in rates) else unknown

    def _mount(self, directory: str, device: int) -> tuple[str | None, str | None, str | None, str | None]:
        number = _major_minor(device)
        matches = []
        for line in (_text(self.proc / "self/mountinfo", 131072) or "").splitlines():
            left, separator, right = line.partition(" - ")
            before, after = left.split(), right.split()
            if not separator or len(before) < 6 or len(after) < 3 or before[2] != number:
                continue
            mountpoint = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), before[4])
            source = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), after[1])
            try:
                if os.path.commonpath((directory, mountpoint)) == os.path.normpath(mountpoint):
                    matches.append((mountpoint, after[0], source, number))
            except ValueError:
                continue
        return max(matches, key=lambda item: len(item[0])) if matches else (None, None, None, None)

    def _recording_total(self, directory: str, device: int, now: float) -> tuple[int | None, bool]:
        try:
            information = os.stat(directory)
            identity = (directory, device, information.st_ino)
            if information.st_dev != device:
                raise OSError("destination identity changed")
            if self._scan_cache and self._scan_cache[0] == identity and 0 <= now - self._scan_cache[1] < SCAN_INTERVAL_SECONDS:
                return self._scan_cache[2], self._scan_cache[3]
            total = 0
            complete = True
            # ponytail: flat bounded scan; add an indexed inventory if 4096 entries prove insufficient.
            with os.scandir(directory) as entries:
                for count, entry in enumerate(entries):
                    if count >= MAX_SCAN_ENTRIES:
                        complete = False
                        break
                    if not entry.name.lower().endswith(".flac"):
                        continue
                    item = os.stat(entry.path, follow_symlinks=False)
                    if stat.S_ISREG(item.st_mode) and item.st_dev == device:
                        total += item.st_size
            self._scan_cache = (identity, now, total if complete else None, complete)
            return self._scan_cache[2], complete
        except (OSError, ValueError):
            self._scan_cache = (None, now, None, False)
            return None, False

    @staticmethod
    def _recording_file(directory: str, device: int, recording: Mapping[str, object]) -> dict[str, object]:
        result = {"file": None, "size_bytes": None, "elapsed_seconds": None}
        candidate = recording.get("current_file") or recording.get("last_finalized_file")
        if not isinstance(candidate, str) or not candidate or len(candidate) > 4096:
            return result
        try:
            path = Path(candidate)
            # The media engine creates only direct-child fragments; never follow file links.
            if path.parent.resolve(strict=True) != Path(directory):
                return result
            information = path.lstat()
            if not stat.S_ISREG(information.st_mode) or information.st_dev != device:
                return result
            result.update(file=candidate, size_bytes=information.st_size)
            if recording.get("elapsed_file") == candidate:
                result["elapsed_seconds"] = _number(recording.get("elapsed_seconds"))
        except (OSError, ValueError):
            pass
        return result

    def _storage(self, config: MediaConfig, recording: Mapping[str, object], now: float) -> tuple[dict[str, object], dict[str, object]]:
        storage = dict.fromkeys(("mountpoint", "filesystem", "device", "total_bytes", "used_bytes", "available_bytes", "used_percent", "recordings_bytes", "read_bytes_per_second", "write_bytes_per_second", "seconds_until_limit", "safe"))
        storage.update(directory=config.recording.directory, stop_percent=STORAGE_LIMIT_PERCENT, recordings_complete=False, reason="usage_unavailable")
        file = {"file": None, "size_bytes": None, "elapsed_seconds": None}
        try:
            destination = self.destination_check(config.recording)
        except (OSError, ValueError, TypeError):
            destination = None
        if destination is not None:
            storage.update(safe=destination.safe, reason=destination.reason)
        if destination is None or destination.reason not in {"ok", "storage_threshold"} or destination.resolved_directory is None or destination.st_dev is None:
            self._rates.pop("disk", None)
            self._scan_cache = None
            return storage, file
        directory, device = destination.resolved_directory, destination.st_dev
        for field in ("total_bytes", "used_bytes", "available_bytes", "used_percent"):
            storage[field] = getattr(destination, field)
        mountpoint, filesystem, source, number = self._mount(directory, device)
        storage.update(mountpoint=mountpoint, filesystem=filesystem, device=source)
        values = (_text(self.sys / "dev/block" / number / "stat", 4096) or "").split() if number else []
        sectors = tuple(_number(values[index], integer=True) if len(values) > index else None for index in (2, 6))
        counters = tuple(value * 512 if value is not None else None for value in sectors)
        storage["read_bytes_per_second"], storage["write_bytes_per_second"] = self._rate("disk", (directory, number), counters, now)
        storage["recordings_bytes"], storage["recordings_complete"] = self._recording_total(directory, device, now)
        file = self._recording_file(directory, device, recording)
        size, elapsed = file["size_bytes"], file["elapsed_seconds"]
        if recording.get("state") == "running" and recording.get("current_file") == file["file"] and size and elapsed is not None and elapsed >= 5 and destination.total_bytes is not None and destination.used_bytes is not None and destination.available_bytes is not None:
            remaining = max(0, min(destination.available_bytes, (destination.total_bytes * STORAGE_LIMIT_PERCENT + 99) // 100 - destination.used_bytes))
            estimate = remaining * elapsed / size
            storage["seconds_until_limit"] = estimate if math.isfinite(estimate) else None
        return storage, file

    def _network_interface(self) -> str | None:
        candidates = []
        for line in (_text(self.proc / "net/route") or "").splitlines()[1:]:
            fields = line.split()
            try:
                if len(fields) >= 8 and _interface_name(fields[0]) and fields[1] == fields[7] == "00000000" and int(fields[3], 16) & 1:
                    candidates.append((int(fields[6]), fields[0]))
            except ValueError:
                continue
        if candidates:
            return min(candidates)[1]
        for line in (_text(self.proc / "net/ipv6_route") or "").splitlines():
            fields = line.split()
            try:
                if len(fields) == 10 and _interface_name(fields[-1]) and fields[0] == "0" * 32 and fields[1] == "00" and int(fields[8], 16) & 1 and not int(fields[8], 16) & 0x200:
                    candidates.append((int(fields[5], 16), fields[-1]))
            except ValueError:
                continue
        if candidates:
            return min(candidates)[1]
        try:
            with os.scandir(self.sys / "class/net") as entries:
                names = []
                for count, entry in enumerate(entries):
                    if count >= 256:
                        return None
                    if _interface_name(entry.name) and _text(Path(entry.path) / "operstate", 128) == "up":
                        names.append(entry.name)
                return min(names) if names else None
        except OSError:
            return None

    def _ipv6_address(self, interface: str) -> str | None:
        addresses = []
        for line in (_text(self.proc / "net/if_inet6") or "").splitlines():
            fields = line.split()
            try:
                if len(fields) == 6 and fields[5] == interface and not int(fields[4], 16) & 0x48:
                    address = ipaddress.IPv6Address(int(fields[0], 16))
                    if not address.is_unspecified and not address.is_loopback:
                        addresses.append((address.is_link_local, str(address)))
            except ValueError:
                continue
        return min(addresses)[1] if addresses else None

    def _network(self, now: float) -> dict[str, object]:
        result = dict.fromkeys(("interface", "address", "kind", "operstate", "speed_mbps", "duplex", "rx_bytes_per_second", "tx_bytes_per_second"))
        interface = self._network_interface()
        if interface is None:
            self._rates.pop("network", None)
            return result
        root = self.sys / "class/net" / interface
        speed = _number(_text(root / "speed", 128), integer=True)
        duplex = _text(root / "duplex", 128)
        result.update(
            interface=interface,
            address=_ipv4_address(interface) or self._ipv6_address(interface),
            kind="wifi" if (root / "wireless").is_dir() else "ethernet" if _text(root / "type", 128) == "1" else None,
            operstate=_text(root / "operstate", 128) or None,
            speed_mbps=speed if speed else None,
            duplex=duplex if duplex in {"full", "half"} else None,
        )
        counters = tuple(_number(_text(root / "statistics" / field, 128), integer=True) for field in ("rx_bytes", "tx_bytes"))
        identity = (interface, _text(root / "ifindex", 128))
        result["rx_bytes_per_second"], result["tx_bytes_per_second"] = self._rate("network", identity, counters, now)
        return result

    def sample(self, config: MediaConfig, recording: Mapping[str, object]) -> dict[str, object]:
        now = self.clock()
        storage, file = self._storage(config, recording, now)
        return {"device": self._identity(), "system": self._system(), "storage": storage, "network": self._network(now), "recording": file}
