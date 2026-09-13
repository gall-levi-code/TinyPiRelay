"""Bounded, non-destructive observer for an installed TinyPiRelay appliance.

The observer may start a configured stream or recording only when explicitly
requested and only when that scope is stopped.  It never starts capture,
changes configuration, injects faults, or manages services.  Its result is a
local installed-appliance observation, not by itself a hardware compatibility
claim or a Legacy/Standard/Full profile decision.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

from tinypirelay.control_protocol import ControlClient, ControlError, redact_secrets


SCHEMA_VERSION = 1
OPUS_REPRESENTATION = "opus_128k_48000_stereo_matroska"
APPROVED_REPRESENTATIONS = frozenset(
    (
        "pcm_s16le_48000_stereo_matroska",
        "flac_48000_stereo_matroska",
        OPUS_REPRESENTATION,
    )
)
OPUS_BITRATES_BPS = frozenset((64_000, 96_000, 128_000, 192_000))
QUEUE_FIELDS = (
    "current_buffers",
    "current_bytes",
    "current_time_ns",
    "max_buffers",
    "max_bytes",
    "max_time_ns",
    "leaky",
)
ACTIVE_STATES = frozenset(("starting", "running", "stopping"))
MAX_DURATION_SECONDS = 7 * 24 * 60 * 60
MAX_SAMPLES = 4_096
MAX_BITRATE_CHANGES = 8
MAX_QUEUES = 8
MAX_STATS = 64
MAX_TEXT = 512
SPECTRUM_RENEW_SECONDS = 2.0
THROTTLING_REFRESH_SECONDS = 10.0
SOURCE_BUNDLE_RE = re.compile(r"^[0-9a-fA-F]{64}$")
SCENARIO_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
BOOT_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_SECRET_IN_TEXT = re.compile(
    r"(?i)(passphrase(?:=|%3d)|password(?:=|%3d)|token(?:=|%3d))[^&\s]+"
)


class ProfileError(RuntimeError):
    """The requested observation cannot be performed or trusted safely."""


@dataclass(frozen=True)
class ProfilePlan:
    duration_seconds: float
    sample_interval_seconds: float
    transition_timeout_seconds: float
    start_stream: bool = False
    start_recording: bool = False
    spectrum: bool = False
    opus_bitrates: tuple[int, ...] = ()
    bitrate_interval_seconds: float = 10.0
    checkpoint_interval_seconds: float = 3600.0
    expected_representation: str | None = None

    def __post_init__(self) -> None:
        finite = (
            self.duration_seconds,
            self.sample_interval_seconds,
            self.transition_timeout_seconds,
            self.bitrate_interval_seconds,
            self.checkpoint_interval_seconds,
        )
        if not all(type(value) in (int, float) and math.isfinite(value) for value in finite):
            raise ValueError("durations must be finite numbers")
        if not 1.0 <= self.duration_seconds <= MAX_DURATION_SECONDS:
            raise ValueError("duration_seconds must be between 1 second and 7 days")
        if not 0.2 <= self.sample_interval_seconds <= 60.0:
            raise ValueError("sample_interval_seconds must be between 0.2 and 60 seconds")
        if not 1.0 <= self.transition_timeout_seconds <= 120.0:
            raise ValueError("transition_timeout_seconds must be between 1 and 120 seconds")
        if not 0.5 <= self.bitrate_interval_seconds <= 3600.0:
            raise ValueError("bitrate_interval_seconds must be between 0.5 and 3600 seconds")
        if not 300.0 <= self.checkpoint_interval_seconds <= 7200.0:
            raise ValueError("checkpoint_interval_seconds must be between 300 and 7200 seconds")
        if _sample_count(self.duration_seconds, self.sample_interval_seconds) > MAX_SAMPLES:
            raise ValueError(f"requested cadence exceeds the {MAX_SAMPLES}-sample cap")
        if len(self.opus_bitrates) > MAX_BITRATE_CHANGES:
            raise ValueError(f"at most {MAX_BITRATE_CHANGES} Opus bitrate changes are allowed")
        if any(type(value) is not int or value not in OPUS_BITRATES_BPS for value in self.opus_bitrates):
            raise ValueError("Opus bitrates must be one of 64000, 96000, 128000, or 192000")
        if self.opus_bitrates and not self.start_stream:
            raise ValueError("Opus bitrate scheduling requires --start-stream")
        if self.start_stream and self.expected_representation not in APPROVED_REPRESENTATIONS:
            raise ValueError("--start-stream requires one exact approved expected representation")
        if not self.start_stream and self.expected_representation is not None:
            raise ValueError("expected_representation is valid only with --start-stream")
        if self.opus_bitrates and self.expected_representation != OPUS_REPRESENTATION:
            raise ValueError("Opus bitrate scheduling requires the approved Opus representation")
        if (
            self.opus_bitrates
            and self.bitrate_interval_seconds * len(self.opus_bitrates)
            + self.sample_interval_seconds
            > self.duration_seconds
        ):
            raise ValueError(
                "the final Opus bitrate change requires at least one sample interval of dwell"
            )


@dataclass(frozen=True)
class ProcCounters:
    pid: int
    state: str
    cpu_ticks: int
    start_ticks: int
    rss_pages: int


def parse_proc_stat(text: str) -> ProcCounters:
    """Parse the fields used from one Linux ``/proc/<pid>/stat`` record."""

    if not isinstance(text, str) or len(text) > 64 * 1024:
        raise ValueError("invalid proc stat record")
    left = text.find("(")
    right = text.rfind(")")
    if left <= 0 or right <= left or right + 2 >= len(text):
        raise ValueError("malformed proc stat record")
    try:
        pid = int(text[:left].strip())
        fields = text[right + 2 :].split()
        # fields[0] is kernel field 3 (state).
        state = fields[0]
        cpu_ticks = int(fields[11]) + int(fields[12])
        start_ticks = int(fields[19])
        rss_pages = int(fields[21])
    except (IndexError, TypeError, ValueError) as exc:
        raise ValueError("malformed proc stat fields") from exc
    if pid <= 0 or len(state) != 1 or cpu_ticks < 0 or start_ticks < 0 or rss_pages < 0:
        raise ValueError("invalid proc stat values")
    return ProcCounters(pid, state, cpu_ticks, start_ticks, rss_pages)


def process_cpu_percent(
    previous_ticks: int,
    current_ticks: int,
    elapsed_seconds: float,
    clock_ticks_per_second: int,
) -> float | None:
    """Return one process's CPU use where 100% equals one fully used core."""

    values = (previous_ticks, current_ticks, clock_ticks_per_second)
    if any(type(value) is not int for value in values):
        raise TypeError("CPU tick values must be integers")
    if (
        previous_ticks < 0
        or current_ticks < previous_ticks
        or clock_ticks_per_second <= 0
        or type(elapsed_seconds) not in (int, float)
        or not math.isfinite(elapsed_seconds)
        or elapsed_seconds <= 0
    ):
        return None
    return (current_ticks - previous_ticks) * 100.0 / (
        clock_ticks_per_second * elapsed_seconds
    )


def parse_temperature(text: str) -> float:
    value = float(text.strip())
    if not math.isfinite(value):
        raise ValueError("temperature is not finite")
    if abs(value) >= 1_000:
        value /= 1_000.0
    if not -100.0 <= value <= 250.0:
        raise ValueError("temperature is outside the supported range")
    return value


def parse_throttled(text: str) -> str:
    match = re.fullmatch(r"\s*(?:throttled=)?(0x[0-9a-fA-F]+)\s*", text)
    if match is None:
        raise ValueError("invalid throttling value")
    return f"0x{int(match.group(1), 16):x}"


def parse_proc_cpu(text: str) -> tuple[int, int]:
    """Return total and idle-like ticks from the aggregate ``cpu`` row."""

    first = text.splitlines()[0].split() if text else []
    if not first or first[0] != "cpu" or len(first) < 5:
        raise ValueError("aggregate CPU counters are missing")
    try:
        counters = [int(value) for value in first[1:]]
    except ValueError as exc:
        raise ValueError("aggregate CPU counters are invalid") from exc
    if any(value < 0 for value in counters):
        raise ValueError("aggregate CPU counters are negative")
    total = sum(counters)
    idle = counters[3] + (counters[4] if len(counters) > 4 else 0)
    if total <= 0 or idle > total:
        raise ValueError("aggregate CPU counters are unusable")
    return total, idle


def system_cpu_percent(
    previous: tuple[int, int] | None, current: tuple[int, int]
) -> float | None:
    if previous is None:
        return None
    total_delta = current[0] - previous[0]
    idle_delta = current[1] - previous[1]
    if total_delta <= 0 or idle_delta < 0 or idle_delta > total_delta:
        return None
    return (total_delta - idle_delta) * 100.0 / total_delta


def parse_loadavg(text: str) -> dict[str, float | int]:
    fields = text.split()
    if len(fields) < 4 or "/" not in fields[3]:
        raise ValueError("loadavg record is malformed")
    try:
        loads = [float(value) for value in fields[:3]]
        running_text, total_text = fields[3].split("/", 1)
        running = int(running_text)
        total = int(total_text)
    except ValueError as exc:
        raise ValueError("loadavg values are malformed") from exc
    if not all(math.isfinite(value) and value >= 0 for value in loads):
        raise ValueError("load averages are invalid")
    if running < 0 or total <= 0 or running > total:
        raise ValueError("loadavg task counts are invalid")
    return {
        "load_1m": loads[0],
        "load_5m": loads[1],
        "load_15m": loads[2],
        "running_tasks": running,
        "total_tasks": total,
    }


def parse_meminfo(text: str) -> dict[str, int]:
    wanted = ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree")
    values: dict[str, int] = {}
    for line in text.splitlines():
        name, separator, remainder = line.partition(":")
        if not separator or name not in wanted:
            continue
        fields = remainder.split()
        if not fields:
            continue
        try:
            value = int(fields[0])
        except ValueError as exc:
            raise ValueError(f"meminfo {name} is invalid") from exc
        if value < 0 or (len(fields) > 1 and fields[1] != "kB"):
            raise ValueError(f"meminfo {name} is invalid")
        values[f"{_snake(name)}_bytes"] = value * 1024
    expected = {f"{_snake(name)}_bytes" for name in wanted}
    if set(values) != expected:
        raise ValueError("required meminfo fields are missing")
    return values


def parse_proc_status(text: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for line in text.splitlines():
        name, separator, remainder = line.partition(":")
        if not separator or name not in ("Threads", "VmHWM"):
            continue
        fields = remainder.split()
        try:
            value = int(fields[0])
        except (IndexError, ValueError) as exc:
            raise ValueError(f"process status {name} is invalid") from exc
        if value < 0:
            raise ValueError(f"process status {name} is negative")
        if name == "Threads":
            result["threads"] = value
        else:
            if len(fields) < 2 or fields[1] != "kB":
                raise ValueError("process VmHWM has an unsupported unit")
            result["vm_hwm_bytes"] = value * 1024
    if set(result) != {"threads", "vm_hwm_bytes"}:
        raise ValueError("required process status fields are missing")
    return result


def parse_proc_io(text: str) -> dict[str, int]:
    wanted = ("read_bytes", "write_bytes")
    result: dict[str, int] = {}
    for line in text.splitlines():
        name, separator, remainder = line.partition(":")
        if not separator or name not in wanted:
            continue
        try:
            value = int(remainder.strip())
        except ValueError as exc:
            raise ValueError(f"process I/O {name} is invalid") from exc
        if value < 0:
            raise ValueError(f"process I/O {name} is negative")
        result[name] = value
    if set(result) != set(wanted):
        raise ValueError("required process I/O fields are missing")
    return result


def parse_net_dev(text: str) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for line in text.splitlines()[2:]:
        name, separator, remainder = line.partition(":")
        if not separator:
            continue
        interface = name.strip()
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,32}", interface):
            raise ValueError("network interface name is invalid")
        fields = remainder.split()
        if len(fields) < 16:
            raise ValueError("network counter row is incomplete")
        try:
            numbers = [int(value) for value in fields[:16]]
        except ValueError as exc:
            raise ValueError("network counters are invalid") from exc
        if any(value < 0 for value in numbers):
            raise ValueError("network counters are negative")
        result[interface] = {
            "rx_bytes": numbers[0],
            "rx_dropped": numbers[3],
            "tx_bytes": numbers[8],
            "tx_dropped": numbers[11],
        }
        if len(result) > 32:
            raise ValueError("network interface count exceeds safety cap")
    if not result:
        raise ValueError("no network counters were found")
    return result


def percentile(values: Sequence[float], percent: float) -> float | None:
    clean = sorted(_finite_values(values))
    if not clean:
        return None
    if not 0.0 <= percent <= 100.0:
        raise ValueError("percent must be between 0 and 100")
    position = (len(clean) - 1) * percent / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return clean[lower]
    weight = position - lower
    return clean[lower] * (1.0 - weight) + clean[upper] * weight


def linear_slope(points: Sequence[tuple[float, float]]) -> float | None:
    clean = [
        (float(x), float(y))
        for x, y in points
        if type(x) in (int, float)
        and type(y) in (int, float)
        and math.isfinite(x)
        and math.isfinite(y)
    ]
    if len(clean) < 2:
        return None
    mean_x = statistics.fmean(x for x, _y in clean)
    mean_y = statistics.fmean(y for _x, y in clean)
    denominator = sum((x - mean_x) ** 2 for x, _y in clean)
    if denominator <= 0:
        return None
    return sum((x - mean_x) * (y - mean_y) for x, y in clean) / denominator


def summarize_points(points: Sequence[tuple[float, float]]) -> dict[str, object]:
    clean = [(float(x), float(y)) for x, y in points if _finite_pair(x, y)]
    values = [value for _elapsed, value in clean]
    return {
        "count": len(values),
        "minimum": min(values) if values else None,
        "maximum": max(values) if values else None,
        "mean": statistics.fmean(values) if values else None,
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "last": values[-1] if values else None,
        "slope_per_second": linear_slope(clean),
    }


class SystemSampler:
    """Read bounded proc/sysfs counters and a cached Pi throttle observation."""

    def __init__(
        self,
        processes: Mapping[str, int],
        *,
        proc_root: Path = Path("/proc"),
        temperature_path: Path | None = Path("/sys/class/thermal/thermal_zone0/temp"),
        clock_ticks_per_second: int | None = None,
        page_size: int | None = None,
        recording_directory: Path | None = None,
        command_runner: Callable[..., object] = subprocess.run,
    ) -> None:
        if not processes or len(processes) > 8:
            raise ValueError("one to eight processes are required")
        if any(not name or type(pid) is not int or pid <= 0 for name, pid in processes.items()):
            raise ValueError("process names and PIDs must be valid")
        self.processes = dict(processes)
        self.proc_root = proc_root
        self.temperature_path = temperature_path
        self.vcgencmd_path = "/usr/bin/vcgencmd"
        self.command_runner = command_runner
        self.clock_ticks_per_second = clock_ticks_per_second or int(os.sysconf("SC_CLK_TCK"))
        self.page_size = page_size or int(os.sysconf("SC_PAGE_SIZE"))
        self.recording_directory = recording_directory
        self._previous: dict[str, tuple[float, ProcCounters]] = {}
        self._previous_system_cpu: tuple[int, int] | None = None
        self._throttling_cache: dict[str, object] | None = None
        self._throttling_sampled_at: float | None = None

    def set_recording_directory(self, path: Path) -> None:
        if not path.is_absolute():
            raise ValueError("recording directory must be absolute")
        self.recording_directory = path

    def identity_snapshot(self) -> dict[str, object]:
        model: str | None = None
        model_source: str | None = None
        for path in (
            self.proc_root / "device-tree" / "model",
            Path("/sys/firmware/devicetree/base/model"),
        ):
            try:
                candidate = _read_small(path).strip("\x00\r\n ")
            except (OSError, ValueError):
                continue
            if candidate:
                model = candidate[:MAX_TEXT]
                model_source = str(path)
                break
        boot_id: str | None = None
        try:
            candidate = _read_small(
                self.proc_root / "sys" / "kernel" / "random" / "boot_id"
            ).strip().lower()
            if BOOT_ID_RE.fullmatch(candidate):
                boot_id = candidate
        except (OSError, ValueError):
            pass
        return {
            "board_model": model,
            "board_model_source": model_source,
            "boot_id": boot_id,
        }

    def throttling_snapshot(
        self, elapsed_seconds: float | None = None, *, force: bool = False
    ) -> dict[str, object]:
        if (
            not force
            and self._throttling_cache is not None
            and elapsed_seconds is not None
            and self._throttling_sampled_at is not None
            and elapsed_seconds - self._throttling_sampled_at
            < THROTTLING_REFRESH_SECONDS
        ):
            return dict(self._throttling_cache)
        command = [self.vcgencmd_path, "get_throttled"]
        result: dict[str, object] = {
            "command": command,
            "returncode": None,
            "throttled_hex": None,
            "error": None,
        }
        try:
            completed = self.command_runner(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=2.0,
                check=False,
                shell=False,
            )
            returncode = getattr(completed, "returncode", None)
            stdout = getattr(completed, "stdout", None)
            result["returncode"] = returncode
            if type(returncode) is not int:
                result["error"] = "invalid_result"
            elif returncode != 0:
                result["error"] = f"exit_{returncode}"
            elif not isinstance(stdout, str) or len(stdout) > 1024:
                result["error"] = "invalid_output"
            else:
                try:
                    result["throttled_hex"] = parse_throttled(stdout)
                except ValueError:
                    result["error"] = "invalid_output"
        except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
            result["error"] = type(exc).__name__
        self._throttling_cache = result
        if elapsed_seconds is not None:
            self._throttling_sampled_at = elapsed_seconds
        return dict(result)

    def sample(self, elapsed_seconds: float) -> dict[str, object]:
        result: dict[str, object] = {"processes": {}}
        process_result = result["processes"]
        assert isinstance(process_result, dict)
        for name, pid in self.processes.items():
            process_result[name] = self._sample_process(name, pid, elapsed_seconds)
        result["cpu"] = self._sample_system_cpu()
        result["memory"] = self._parse_optional(
            self.proc_root / "meminfo", parse_meminfo
        )
        result["network"] = self._parse_optional(
            self.proc_root / "net" / "dev", parse_net_dev
        )
        result["recording_storage"] = self._sample_recording_storage()
        result["temperature_c"] = self._read_optional(
            self.temperature_path, parse_temperature
        )
        result["throttling"] = self.throttling_snapshot(elapsed_seconds)
        return result

    def _sample_process(self, name: str, pid: int, elapsed: float) -> dict[str, object]:
        try:
            stat_text = _read_small(self.proc_root / str(pid) / "stat")
            counters = parse_proc_stat(stat_text)
            if counters.pid != pid:
                raise ValueError("proc stat PID does not match")
            fd_count = _count_directory(self.proc_root / str(pid) / "fd", 65_536)
            status = parse_proc_status(
                _read_small(self.proc_root / str(pid) / "status")
            )
            io_counters = parse_proc_io(
                _read_small(self.proc_root / str(pid) / "io")
            )
        except (OSError, ValueError) as exc:
            return {
                "available": False,
                "pid": pid,
                "error": type(exc).__name__,
            }
        previous = self._previous.get(name)
        cpu_percent: float | None = None
        if previous is not None and previous[1].start_ticks == counters.start_ticks:
            cpu_percent = process_cpu_percent(
                previous[1].cpu_ticks,
                counters.cpu_ticks,
                elapsed - previous[0],
                self.clock_ticks_per_second,
            )
        self._previous[name] = (elapsed, counters)
        return {
            "available": True,
            "pid": pid,
            "state": counters.state,
            "start_ticks": counters.start_ticks,
            "cpu_ticks": counters.cpu_ticks,
            "cpu_percent": cpu_percent,
            "rss_bytes": counters.rss_pages * self.page_size,
            "fd_count": fd_count,
            **status,
            "io": io_counters,
        }

    def _sample_system_cpu(self) -> dict[str, object] | None:
        try:
            current = parse_proc_cpu(_read_small(self.proc_root / "stat"))
            load = parse_loadavg(_read_small(self.proc_root / "loadavg"))
        except (OSError, ValueError):
            return None
        busy = system_cpu_percent(self._previous_system_cpu, current)
        self._previous_system_cpu = current
        return {
            "busy_percent": busy,
            "total_ticks": current[0],
            "idle_ticks": current[1],
            "logical_cpus": os.cpu_count(),
            **load,
        }

    def _sample_recording_storage(self) -> dict[str, object] | None:
        path = self.recording_directory
        if path is None:
            return None
        try:
            usage = shutil.disk_usage(path)
            file_count, size_bytes = _directory_file_totals(path, 100_000)
        except (OSError, ValueError):
            return None
        return {
            "filesystem_total_bytes": usage.total,
            "filesystem_used_bytes": usage.used,
            "filesystem_free_bytes": usage.free,
            "directory_file_count": file_count,
            "directory_bytes": size_bytes,
        }

    @staticmethod
    def _parse_optional(path: Path, parser: Callable[[str], object]) -> object | None:
        try:
            return parser(_read_small(path))
        except (OSError, ValueError):
            return None

    @staticmethod
    def _read_optional(path: Path | None, parser: Callable[[str], object]) -> object | None:
        if path is None:
            return None
        try:
            return parser(_read_small(path))
        except (OSError, ValueError):
            return None


class InstalledProfiler:
    def __init__(
        self,
        client: Any,
        sampler: Any,
        plan: ProfilePlan,
        source_bundle_sha256: str,
        scenario: str,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        utc_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if not isinstance(source_bundle_sha256, str) or SOURCE_BUNDLE_RE.fullmatch(source_bundle_sha256) is None:
            raise ValueError("source_bundle_sha256 must be exactly 64 hexadecimal characters")
        if not isinstance(scenario, str) or SCENARIO_RE.fullmatch(scenario) is None:
            raise ValueError("scenario must match [a-z0-9][a-z0-9._-]{0,63}")
        self.client = client
        self.sampler = sampler
        self.plan = plan
        self.source_bundle_sha256 = source_bundle_sha256.lower()
        self.scenario = scenario
        self.clock = clock
        self.sleep = sleep
        self.utc_now = utc_now
        self.owned_scopes: dict[str, int] = {}
        self.initial_bitrate: int | None = None
        self.initial_stream_generation: int | None = None
        self.bitrate_events: list[dict[str, object]] = []

    def run(
        self,
        checkpoint: Callable[[Mapping[str, object]], None] | None = None,
    ) -> dict[str, object]:
        report = _base_report(
            self.plan, self.source_bundle_sha256, self.scenario, self.utc_now()
        )
        try:
            identity_snapshot = getattr(self.sampler, "identity_snapshot", None)
            if callable(identity_snapshot):
                identity = identity_snapshot()
                if isinstance(identity, Mapping):
                    report["identity"].update(_bounded_redacted(identity))
            throttling_snapshot = getattr(self.sampler, "throttling_snapshot", None)
            if callable(throttling_snapshot):
                report["boundary_metrics"]["throttling_start"] = (
                    _bounded_redacted(throttling_snapshot(0.0, force=True))
                )
            initial = self._status()
            report["initial_status"] = normalize_status(initial)
            self._prepare(initial, report)
            ready = self._wait_for(
                lambda status: ready_for_collection(
                    status, require_spectrum=self.plan.spectrum
                ),
                "active scopes did not become connected/running with complete queue visibility",
            )
            report["ready_status"] = normalize_status(ready)
            if (
                self.plan.expected_representation is not None
                and scope_value(ready, "stream").get("representation_id")
                != self.plan.expected_representation
            ):
                raise ProfileError("ready stream representation differs from --expected-representation")
            report["run"]["active_scope_generations"] = {
                scope: _integer_or_none(scope_value(ready, scope).get("generation"))
                for scope in ("stream", "recording")
                if scope_state(ready, scope) == "running"
            }
            self._collect(report, checkpoint)
        except KeyboardInterrupt:
            report["interrupted"] = True
            report["errors"].append("KeyboardInterrupt: observation interrupted by operator")
        except (ControlError, OSError, ProfileError, TypeError, ValueError) as exc:
            report["errors"].append(_safe_error(exc))
        finally:
            report["cleanup"] = self._cleanup()
            try:
                report["final_status"] = normalize_status(self._status())
            except (ControlError, OSError, ProfileError, TypeError, ValueError) as exc:
                report["cleanup"]["errors"].append(f"final status: {_safe_error(exc)}")
            try:
                throttling_snapshot = getattr(self.sampler, "throttling_snapshot", None)
                if callable(throttling_snapshot):
                    report["boundary_metrics"]["throttling_end"] = (
                        _bounded_redacted(throttling_snapshot(force=True))
                    )
            except (OSError, TypeError, ValueError) as exc:
                report["cleanup"]["errors"].append(
                    f"final throttling observation: {_safe_error(exc)}"
                )
        report["bitrate_events"] = self.bitrate_events
        report["checkpoint"]["complete"] = True
        bounded = _bounded_redacted(report)
        if not isinstance(bounded, dict):
            raise ProfileError("bounded report is not an object")
        bounded["summary"] = summarize_report_samples(bounded.get("samples", []))
        bounded["result"] = assess_report(bounded)
        return bounded

    def _prepare(self, initial: Mapping[str, object], report: dict[str, Any]) -> None:
        if scope_state(initial, "capture") != "running":
            raise ProfileError("capture must already be running; this observer never starts it")
        capture = scope_value(initial, "capture")
        report["run"]["capture_generation"] = _integer_or_none(capture.get("generation"))
        config_result = self.client.request("config.get")
        report["config"] = normalize_public_config(config_result)
        configured_representation = report["config"]["config"]["stream"].get(
            "representation_id"
        )
        if (
            self.plan.expected_representation is not None
            and configured_representation != self.plan.expected_representation
        ):
            raise ProfileError(
                "configured stream representation differs from --expected-representation"
            )
        recording_directory = _recording_directory(config_result)
        setter = getattr(self.sampler, "set_recording_directory", None)
        if callable(setter):
            setter(recording_directory)
        report["run"]["recording_directory"] = str(recording_directory)
        for scope, requested in (
            ("stream", self.plan.start_stream),
            ("recording", self.plan.start_recording),
        ):
            if requested:
                self._start_missing(scope)
        status = self._status()
        stream = scope_value(status, "stream")
        self.initial_bitrate = _integer_or_none(stream.get("opus_bitrate_bps"))
        self.initial_stream_generation = _integer_or_none(stream.get("generation"))
        if self.plan.opus_bitrates:
            if scope_state(status, "stream") != "running":
                raise ProfileError("Opus bitrate scheduling requires a running stream")
            if stream.get("representation_id") != OPUS_REPRESENTATION:
                raise ProfileError("Opus bitrate scheduling requires the approved Opus representation")
        if self.plan.spectrum:
            initially_active = spectrum_active(status)
            response = self.client.request("monitoring.spectrum_lease")
            if not isinstance(response, Mapping) or response.get("active") is not True:
                raise ProfileError("spectrum lease was not acquired")
            report["run"]["spectrum_was_observed_initially"] = initially_active
            report["run"]["spectrum_lease_seconds"] = _number_or_none(response.get("lease_seconds"))
            report["run"]["spectrum_cleanup"] = (
                "stop renewing; never call shared monitoring.spectrum_release; "
                "the fixed lease expires naturally"
            )
        report["run"]["started_scopes"] = dict(self.owned_scopes)

    def _start_missing(self, scope: str) -> None:
        status = self._status()
        current = scope_state(status, scope)
        if current == "running":
            return
        if current == "starting":
            self._wait_scope(scope, "running")
            return
        if current != "stopped":
            raise ProfileError(f"{scope} is {current}; refusing to take ownership")
        before_generation = _integer_or_none(scope_value(status, scope).get("generation")) or 0
        response = self.client.request(f"{scope}.start")
        generation = _decision_generation(response, scope)
        if generation is None:
            generation = _integer_or_none(
                scope_value(self._status(), scope).get("generation")
            )
        if generation is None or generation <= before_generation:
            raise ProfileError(f"{scope} start did not expose a new generation")
        # Provisional ownership is recorded before waiting.  A later timeout
        # must still clean up a start the service already accepted.
        self.owned_scopes[scope] = generation
        running = self._wait_scope(scope, "running")
        if _integer_or_none(scope_value(running, scope).get("generation")) != generation:
            raise ProfileError(f"{scope} generation changed while start was pending")

    def _collect(
        self,
        report: dict[str, Any],
        checkpoint: Callable[[Mapping[str, object]], None] | None,
    ) -> None:
        start = self.clock()
        sample_offsets = _sample_offsets(
            self.plan.duration_seconds, self.plan.sample_interval_seconds
        )
        bitrate_offsets = [
            self.plan.bitrate_interval_seconds * (index + 1)
            for index in range(len(self.plan.opus_bitrates))
        ]
        next_sample = 0
        next_bitrate = 0
        next_renew = SPECTRUM_RENEW_SECONDS if self.plan.spectrum else math.inf
        next_checkpoint = self.plan.checkpoint_interval_seconds
        samples = report["samples"]
        assert isinstance(samples, list)
        while next_sample < len(sample_offsets):
            candidates = [sample_offsets[next_sample], next_renew]
            if next_bitrate < len(bitrate_offsets):
                candidates.append(bitrate_offsets[next_bitrate])
            target_offset = min(candidates)
            self._sleep_until(start + target_offset)
            elapsed = max(0.0, self.clock() - start)
            if self.plan.spectrum and elapsed + 1e-9 >= next_renew:
                response = self.client.request("monitoring.spectrum_lease")
                if not isinstance(response, Mapping) or response.get("active") is not True:
                    raise ProfileError("spectrum lease renewal failed")
                next_renew += SPECTRUM_RENEW_SECONDS
            while (
                next_bitrate < len(bitrate_offsets)
                and elapsed + 1e-9 >= bitrate_offsets[next_bitrate]
            ):
                self._change_bitrate(self.plan.opus_bitrates[next_bitrate], elapsed)
                next_bitrate += 1
                elapsed = max(0.0, self.clock() - start)
            if elapsed + 1e-9 >= sample_offsets[next_sample]:
                status = self._status()
                scheduled = sample_offsets[next_sample]
                samples.append(
                    {
                        "elapsed_seconds": elapsed,
                        "scheduled_elapsed_seconds": scheduled,
                        "schedule_delay_seconds": max(0.0, elapsed - scheduled),
                        "observed_at_utc": self.utc_now().isoformat(),
                        "status": normalize_status(status),
                        "system": _bounded_redacted(self.sampler.sample(elapsed)),
                    }
                )
                next_sample += 1
                while (
                    next_sample < len(sample_offsets)
                    and sample_offsets[next_sample] <= elapsed + 1e-9
                ):
                    report["run"]["missed_sample_slots"] += 1
                    next_sample += 1
                if checkpoint is not None and elapsed + 1e-9 >= next_checkpoint:
                    report["checkpoint"]["writes"] += 1
                    checkpoint(report)
                    while next_checkpoint <= elapsed + 1e-9:
                        next_checkpoint += self.plan.checkpoint_interval_seconds

    def _change_bitrate(self, target: int, elapsed: float) -> None:
        response = self.client.request(
            "stream.set_opus_bitrate", {"bitrate_bps": target}
        )
        observed = self._wait_for(
            lambda status: (
                scope_value(status, "stream").get("opus_bitrate_bps") == target
                and scope_value(status, "stream").get("pending_opus_bitrate_bps") is None
            ),
            f"Opus bitrate {target} was not applied",
        )
        self.bitrate_events.append(
            {
                "elapsed_seconds": elapsed,
                "requested_bitrate_bps": target,
                "observed_bitrate_bps": scope_value(observed, "stream").get(
                    "opus_bitrate_bps"
                ),
                "response": _bounded_redacted(response),
            }
        )

    def _cleanup(self) -> dict[str, object]:
        result: dict[str, Any] = {"actions": [], "errors": []}
        try:
            if (
                self.plan.opus_bitrates
                and "stream" not in self.owned_scopes
                and self.initial_bitrate in OPUS_BITRATES_BPS
            ):
                status = self._status()
                stream = scope_value(status, "stream")
                if (
                    scope_state(status, "stream") == "running"
                    and stream.get("generation") == self.initial_stream_generation
                    and stream.get("opus_bitrate_bps") != self.initial_bitrate
                ):
                    self.client.request(
                        "stream.set_opus_bitrate",
                        {"bitrate_bps": self.initial_bitrate},
                    )
                    self._wait_for(
                        lambda current: scope_value(current, "stream").get(
                            "opus_bitrate_bps"
                        )
                        == self.initial_bitrate,
                        "original Opus bitrate was not restored",
                    )
                    result["actions"].append("restored_preexisting_stream_bitrate")
        except (ControlError, OSError, ProfileError, TypeError, ValueError) as exc:
            result["errors"].append(f"bitrate restore: {_safe_error(exc)}")
        if self.plan.spectrum:
            result["actions"].append(
                "stopped_spectrum_renewal_without_releasing_shared_lease"
            )
        for scope in ("recording", "stream"):
            generation = self.owned_scopes.get(scope)
            if generation is None:
                continue
            try:
                status = self._status()
                current = scope_value(status, scope)
                current_generation = _integer_or_none(current.get("generation"))
                current_state = scope_state(status, scope)
                if current_generation != generation:
                    raise ProfileError(
                        f"{scope} generation changed; cleanup correctly refused to stop it"
                    )
                if current_state not in ("stopped", "failed", "blocked"):
                    self.client.request(f"{scope}.stop")
                    self._wait_scope(scope, "stopped")
                    result["actions"].append(f"stopped_owned_{scope}")
            except (ControlError, OSError, ProfileError, TypeError, ValueError) as exc:
                result["errors"].append(f"{scope} cleanup: {_safe_error(exc)}")
        return result

    def _wait_scope(self, scope: str, target: str) -> Mapping[str, object]:
        return self._wait_for(
            lambda status: scope_state(status, scope) == target,
            f"{scope} did not reach {target}",
        )

    def _wait_for(
        self,
        predicate: Callable[[Mapping[str, object]], bool],
        error: str,
    ) -> Mapping[str, object]:
        deadline = self.clock() + self.plan.transition_timeout_seconds
        while True:
            status = self._status()
            if predicate(status):
                return status
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise ProfileError(error)
            self.sleep(min(0.2, remaining))

    def _status(self) -> Mapping[str, object]:
        value = self.client.request("status")
        if not isinstance(value, Mapping):
            raise ProfileError("control status is not an object")
        return value

    def _sleep_until(self, target: float) -> None:
        remaining = target - self.clock()
        if remaining > 0:
            self.sleep(remaining)


def scope_value(status: Mapping[str, object], scope: str) -> Mapping[str, object]:
    state = status.get("state")
    if not isinstance(state, Mapping):
        raise ProfileError("status.state is missing")
    value = state.get(scope)
    if not isinstance(value, Mapping):
        raise ProfileError(f"status.state.{scope} is missing")
    return value


def scope_state(status: Mapping[str, object], scope: str) -> str:
    value = scope_value(status, scope).get("state")
    if not isinstance(value, str) or not value:
        raise ProfileError(f"status.state.{scope}.state is missing")
    return value


def spectrum_active(status: Mapping[str, object]) -> bool:
    telemetry = status.get("telemetry")
    if isinstance(telemetry, Mapping) and isinstance(telemetry.get("spectrum"), Mapping):
        return True
    queues, _errors = queue_snapshot(status)
    return isinstance(queues, Mapping) and "spectrum" in queues


def queue_snapshot(
    status: Mapping[str, object],
) -> tuple[dict[str, dict[str, int]] | None, list[str]]:
    runtime_metrics = status.get("runtime_metrics")
    if not isinstance(runtime_metrics, Mapping):
        return None, ["runtime_metrics is missing"]
    raw = runtime_metrics.get("queues")
    if not isinstance(raw, Mapping):
        return None, ["runtime_metrics.queues is missing"]
    if len(raw) > MAX_QUEUES:
        return None, [f"runtime_metrics.queues exceeds {MAX_QUEUES} entries"]
    result: dict[str, dict[str, int]] = {}
    errors: list[str] = []
    for raw_name, raw_queue in raw.items():
        if not isinstance(raw_name, str) or not raw_name or len(raw_name) > 32:
            errors.append("queue name is invalid")
            continue
        if not isinstance(raw_queue, Mapping):
            errors.append(f"queue {raw_name} is not an object")
            continue
        values: dict[str, int] = {}
        for field in QUEUE_FIELDS:
            value = raw_queue.get(field)
            if type(value) is not int:
                errors.append(f"queue {raw_name}.{field} is not an integer")
                break
            values[field] = value
        else:
            result[raw_name] = values
    return result, errors


def queue_invariant_errors(queue_name: str, queue: Mapping[str, object]) -> list[str]:
    errors: list[str] = []
    for field in QUEUE_FIELDS:
        value = queue.get(field)
        if type(value) is not int:
            errors.append(f"{queue_name}.{field} is not an integer")
        elif value < 0:
            errors.append(f"{queue_name}.{field} is negative")
    if errors:
        return errors
    if queue["leaky"] not in (0, 1, 2):
        errors.append(f"{queue_name}.leaky is outside 0..2")
    maximums = (
        ("buffers", queue["current_buffers"], queue["max_buffers"]),
        ("bytes", queue["current_bytes"], queue["max_bytes"]),
        ("time", queue["current_time_ns"], queue["max_time_ns"]),
    )
    if not any(maximum > 0 for _name, _current, maximum in maximums):
        errors.append(f"{queue_name} has no usable positive maximum dimension")
    for name, current, maximum in maximums:
        if maximum > 0 and current > maximum:
            errors.append(f"{queue_name} current {name} exceeds its maximum")
    return errors


def require_active_queue_visibility(
    status: Mapping[str, object], *, require_spectrum: bool = False
) -> None:
    queues, parse_errors = queue_snapshot(status)
    active = ["level"] if scope_state(status, "capture") == "running" else []
    active.extend(
        scope
        for scope in ("stream", "recording")
        if scope_state(status, scope) in ACTIVE_STATES
    )
    if require_spectrum:
        active.append("spectrum")
    if not active:
        return
    if queues is None or parse_errors:
        detail = "; ".join(parse_errors) or "queue mapping unavailable"
        raise ProfileError(f"active media requires trustworthy queue visibility: {detail}")
    for scope in active:
        queue = queues.get(scope)
        if queue is None:
            raise ProfileError(f"active {scope} queue is missing from runtime_metrics.queues")
        errors = queue_invariant_errors(scope, queue)
        if errors:
            raise ProfileError("; ".join(errors))


def ready_for_collection(
    status: Mapping[str, object], *, require_spectrum: bool = False
) -> bool:
    try:
        if scope_state(status, "capture") != "running":
            return False
        stream_state = scope_state(status, "stream")
        recording_state = scope_state(status, "recording")
        if stream_state in ACTIVE_STATES:
            if stream_state != "running" or scope_state(status, "connection") != "connected":
                return False
        if recording_state in ACTIVE_STATES and recording_state != "running":
            return False
        require_active_queue_visibility(
            status, require_spectrum=require_spectrum
        )
    except ProfileError:
        return False
    return True


def _decision_generation(value: object, scope: str) -> int | None:
    if not isinstance(value, Mapping):
        return None
    state = value.get("state")
    if not isinstance(state, Mapping):
        return None
    scoped = state.get(scope)
    if not isinstance(scoped, Mapping):
        return None
    return _integer_or_none(scoped.get("generation"))


def _recording_directory(value: object) -> Path:
    if not isinstance(value, Mapping):
        raise ProfileError("config.get did not return an object")
    config = value.get("config")
    recording = config.get("recording") if isinstance(config, Mapping) else None
    directory = recording.get("directory") if isinstance(recording, Mapping) else None
    if not isinstance(directory, str) or not directory or len(directory) > 2048:
        raise ProfileError("configured recording directory is unavailable")
    if not PurePosixPath(directory).is_absolute():
        raise ProfileError("configured recording directory is not absolute")
    return Path(directory)


def normalize_public_config(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ProfileError("config.get did not return an object")
    config = value.get("config")
    if not isinstance(config, Mapping):
        raise ProfileError("config.get.config is missing")
    capture = config.get("capture")
    stream = config.get("stream")
    recording = config.get("recording")
    monitoring = config.get("monitoring")
    if not all(
        isinstance(item, Mapping)
        for item in (capture, stream, recording, monitoring)
    ):
        raise ProfileError("public configuration sections are incomplete")
    assert isinstance(capture, Mapping)
    assert isinstance(stream, Mapping)
    assert isinstance(recording, Mapping)
    assert isinstance(monitoring, Mapping)
    passphrase = stream.get("passphrase")
    passphrase_metadata = (
        {
            "configured": passphrase.get("configured"),
            "action": passphrase.get("action"),
        }
        if isinstance(passphrase, Mapping)
        else {"configured": None, "action": None}
    )
    result = {
        "revision": value.get("revision"),
        "restart_required": value.get("restart_required"),
        "config": {
            "schema_version": config.get("schema_version"),
            "capture": {
                "device_id": capture.get("device_id"),
                "mode": capture.get("mode"),
            },
            "stream": {
                "enabled": stream.get("enabled"),
                "representation_id": stream.get("representation_id"),
                "destination_host": stream.get("destination_host"),
                "destination_port": stream.get("destination_port"),
                "latency_ms": stream.get("latency_ms"),
                "stream_id_configured": bool(stream.get("stream_id")),
                "opus_bitrate_bps": stream.get("opus_bitrate_bps"),
                "passphrase": passphrase_metadata,
            },
            "recording": {
                "directory": recording.get("directory"),
                "rotation_seconds": recording.get("rotation_seconds"),
                "required_mountpoint": recording.get("required_mountpoint"),
            },
            "monitoring": {
                "spectrum_updates_per_second": monitoring.get(
                    "spectrum_updates_per_second"
                ),
                "spectrum_bands": monitoring.get("spectrum_bands"),
            },
        },
    }
    return _bounded_redacted(result)  # type: ignore[return-value]


def normalize_status(status: Mapping[str, object]) -> dict[str, object]:
    state_result: dict[str, object] = {}
    for scope in ("capture", "stream", "connection", "recording", "monitoring"):
        value = scope_value(status, scope)
        if scope == "capture":
            keys = ("state", "generation", "last_error")
        elif scope == "stream":
            keys = (
                "state",
                "generation",
                "representation_id",
                "opus_bitrate_bps",
                "pending_opus_bitrate_bps",
                "last_error",
            )
        elif scope == "connection":
            keys = (
                "state",
                "reconnect_count",
                "consecutive_failures",
                "next_retry_seconds",
                "statistics",
                "last_error",
            )
        elif scope == "recording":
            keys = (
                "state",
                "generation",
                "rotation_pending",
                "current_file",
                "last_finalized_file",
                "warning",
                "last_error",
            )
        else:
            keys = ("last_error",)
        state_result[scope] = _bounded_redacted(
            {key: value.get(key) for key in keys}
        )
    telemetry = status.get("telemetry")
    telemetry_result: dict[str, object] = {}
    if isinstance(telemetry, Mapping):
        for name in ("meter", "spectrum"):
            value = telemetry.get(name)
            telemetry_result[f"{name}_sequence"] = (
                value.get("sequence") if isinstance(value, Mapping) else None
            )
    queues, queue_errors = queue_snapshot(status)
    return {
        "state": state_result,
        "active_config_revision": _bounded_redacted(
            status.get("active_config_revision")
        ),
        "saved_config_revision": _bounded_redacted(
            status.get("saved_config_revision")
        ),
        "restart_required": status.get("restart_required"),
        "runtime_error": _bounded_redacted(status.get("runtime_error")),
        "telemetry": telemetry_result,
        "queues": queues,
        "queue_errors": queue_errors,
    }


def assess_report(report: Mapping[str, object]) -> dict[str, object]:
    samples = report.get("samples")
    sample_list = samples if isinstance(samples, list) else []
    run = report.get("run")
    run_map = run if isinstance(run, Mapping) else {}
    cleanup = report.get("cleanup")
    cleanup_map = cleanup if isinstance(cleanup, Mapping) else {}
    errors = report.get("errors")
    setup_errors = errors if isinstance(errors, list) else ["errors field missing"]
    checks = {
        "evidence_binding_valid": _evidence_binding_valid(report),
        "appliance_identity_available": _appliance_identity_available(report),
        "collection_completed": not setup_errors and len(sample_list) >= 2,
        "sample_series_bounded_and_monotonic": _sample_series_valid(sample_list),
        "sample_schedule_kept": _sample_schedule_valid(
            sample_list,
            run_map.get("sample_interval_seconds"),
            run_map.get("missed_sample_slots"),
        ),
        "capture_continuous": _capture_continuous(
            sample_list, run_map.get("capture_generation")
        ),
        "active_scopes_continuous": _active_scopes_continuous(
            sample_list, run_map.get("active_scope_generations")
        ),
        "processes_available_and_stable": _processes_stable(sample_list),
        "core_system_metrics_available": _core_system_metrics_available(sample_list),
        "throttling_observed_and_clear": _throttling_observed_and_clear(report),
        "active_queue_visibility": _queue_visibility_valid(
            sample_list, require_spectrum=run_map.get("spectrum_requested") is True
        ),
        "queue_bounds_respected": _queue_bounds_valid(sample_list),
        "runtime_errors_absent": _runtime_errors_absent(sample_list),
        "requested_spectrum_observed": _spectrum_observed(sample_list)
        if run_map.get("spectrum_requested") is True
        else True,
        "scheduled_bitrates_observed": _bitrate_events_valid(report),
        "final_bitrate_dwell_observed": _final_bitrate_dwell_observed(report),
        "owned_cleanup_completed": not cleanup_map.get("errors"),
        "final_state_valid": _final_state_valid(report),
    }
    reasons = [name for name, passed in checks.items() if not passed]
    return {
        "execution_status": "pass" if all(checks.values()) else "fail",
        "scope": "bounded installed-appliance collection and local invariants only",
        "profile_status": "not-assessed",
        "hardware_compatibility_claim": "not-assigned",
        "checks": checks,
        "reasons": reasons,
    }


def summarize_report_samples(value: object) -> dict[str, object]:
    samples = value if isinstance(value, list) else []
    summary: dict[str, object] = {
        "sample_count": len(samples),
        "processes": {},
        "queues": {},
        "network": {},
    }
    process_names: set[str] = set()
    queue_names: set[str] = set()
    network_names: set[str] = set()
    for sample in samples:
        if not isinstance(sample, Mapping):
            continue
        system = sample.get("system")
        if isinstance(system, Mapping) and isinstance(system.get("processes"), Mapping):
            process_names.update(str(name) for name in system["processes"])
        if isinstance(system, Mapping) and isinstance(system.get("network"), Mapping):
            network_names.update(str(name) for name in system["network"])
        status = sample.get("status")
        if isinstance(status, Mapping) and isinstance(status.get("queues"), Mapping):
            queue_names.update(str(name) for name in status["queues"])
    for name in sorted(process_names):
        summary["processes"][name] = {
            metric: summarize_points(_sample_metric_points(samples, "process", name, metric))
            for metric in (
                "cpu_percent",
                "rss_bytes",
                "fd_count",
                "threads",
                "vm_hwm_bytes",
                "read_bytes",
                "write_bytes",
            )
        }
    for name in sorted(queue_names):
        summary["queues"][name] = {
            metric: summarize_points(_sample_metric_points(samples, "queue", name, metric))
            for metric in ("current_buffers", "current_bytes", "current_time_ns")
        }
    summary["temperature_c"] = summarize_points(
        _sample_metric_points(samples, "system", "", "temperature_c")
    )
    summary["system_cpu_busy_percent"] = summarize_points(
        _sample_metric_points(samples, "nested_system", "cpu", "busy_percent")
    )
    summary["memory_available_bytes"] = summarize_points(
        _sample_metric_points(samples, "nested_system", "memory", "mem_available_bytes")
    )
    summary["swap_free_bytes"] = summarize_points(
        _sample_metric_points(samples, "nested_system", "memory", "swap_free_bytes")
    )
    summary["recording_filesystem_used_bytes"] = summarize_points(
        _sample_metric_points(
            samples, "nested_system", "recording_storage", "filesystem_used_bytes"
        )
    )
    summary["recording_directory_bytes"] = summarize_points(
        _sample_metric_points(
            samples, "nested_system", "recording_storage", "directory_bytes"
        )
    )
    for name in sorted(network_names):
        summary["network"][name] = {
            metric: summarize_points(
                _sample_metric_points(samples, "network", name, metric)
            )
            for metric in ("rx_bytes", "rx_dropped", "tx_bytes", "tx_dropped")
        }
    return summary


def _base_report(
    plan: ProfilePlan,
    source_bundle_sha256: str,
    scenario: str,
    started: datetime,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "evidence_classification": {
            "kind": "installed-appliance-profile-observation",
            "hardware_claim_assigned": False,
            "profile_decision_made": False,
            "claim_limit": (
                "A passing execution status proves only bounded collection and local "
                "invariants; it does not prove receiver decode continuity, a fault case, "
                "soak acceptance, board compatibility, or a Legacy/Standard/Full default."
            ),
        },
        "identity": {
            "source_bundle_sha256": source_bundle_sha256,
            "started_at_utc": started.isoformat(),
            "python": platform.python_version(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "run": {
            "scenario": scenario,
            "duration_seconds": plan.duration_seconds,
            "sample_interval_seconds": plan.sample_interval_seconds,
            "sample_cap": MAX_SAMPLES,
            "start_stream_requested": plan.start_stream,
            "expected_representation": plan.expected_representation,
            "start_recording_requested": plan.start_recording,
            "spectrum_requested": plan.spectrum,
            "opus_bitrates_requested": list(plan.opus_bitrates),
            "bitrate_interval_seconds": plan.bitrate_interval_seconds,
            "checkpoint_interval_seconds": plan.checkpoint_interval_seconds,
            "started_scopes": {},
            "active_scope_generations": {},
            "missed_sample_slots": 0,
        },
        "errors": [],
        "samples": [],
        "bitrate_events": [],
        "boundary_metrics": {
            "throttling_start": None,
            "throttling_end": None,
        },
        "cleanup": {"actions": [], "errors": []},
        "checkpoint": {"complete": False, "writes": 0},
        "interrupted": False,
    }


def _sample_count(duration: float, interval: float) -> int:
    return math.ceil(duration / interval) + 1


def _sample_offsets(duration: float, interval: float) -> list[float]:
    offsets = [index * interval for index in range(math.floor(duration / interval) + 1)]
    if not math.isclose(offsets[-1], duration):
        offsets.append(duration)
    if len(offsets) > MAX_SAMPLES:
        raise ValueError("sample cap exceeded")
    return offsets


def _finite_values(values: Sequence[float]) -> list[float]:
    return [
        float(value)
        for value in values
        if type(value) in (int, float) and math.isfinite(value)
    ]


def _finite_pair(x: object, y: object) -> bool:
    return (
        type(x) in (int, float)
        and type(y) in (int, float)
        and math.isfinite(x)
        and math.isfinite(y)
    )


def _read_small(path: Path) -> str:
    with path.open("r", encoding="utf-8", errors="strict") as handle:
        value = handle.read(64 * 1024 + 1)
    if len(value) > 64 * 1024:
        raise ValueError("system metric file is oversized")
    return value


def _snake(value: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()


def _count_directory(path: Path, maximum: int) -> int:
    count = 0
    with os.scandir(path) as entries:
        for _entry in entries:
            count += 1
            if count > maximum:
                raise ValueError("file descriptor count exceeds safety cap")
    return count


def _directory_file_totals(path: Path, maximum: int) -> tuple[int, int]:
    count = 0
    total = 0
    with os.scandir(path) as entries:
        for entry in entries:
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                size = entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
            count += 1
            if count > maximum:
                raise ValueError("recording file count exceeds safety cap")
            if size < 0:
                raise ValueError("recording file size is negative")
            total += size
    return count, total


def _bounded_redacted(value: object) -> object:
    return _bounded_copy(redact_secrets(value))


def _bounded_copy(value: object, depth: int = 0) -> object:
    if depth > 8:
        return "[truncated]"
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return _SECRET_IN_TEXT.sub(r"\1[REDACTED]", value)[:MAX_TEXT]
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_STATS:
                result["[truncated]"] = True
                break
            result[str(key)[:64]] = _bounded_copy(item, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_bounded_copy(item, depth + 1) for item in value[:MAX_SAMPLES]]
    return type(value).__name__


def _safe_error(exc: BaseException) -> str:
    return str(_bounded_redacted(f"{type(exc).__name__}: {exc}"))


def _integer_or_none(value: object) -> int | None:
    return value if type(value) is int else None


def _number_or_none(value: object) -> int | float | None:
    if type(value) in (int, float) and math.isfinite(value):
        return value
    return None


def _sample_series_valid(samples: Sequence[object]) -> bool:
    if not 2 <= len(samples) <= MAX_SAMPLES:
        return False
    elapsed = [
        sample.get("elapsed_seconds") if isinstance(sample, Mapping) else None
        for sample in samples
    ]
    return all(
        type(value) in (int, float)
        and math.isfinite(value)
        and value >= 0
        and (index == 0 or value > elapsed[index - 1])
        for index, value in enumerate(elapsed)
    )


def _appliance_identity_available(report: Mapping[str, object]) -> bool:
    identity = report.get("identity")
    if not isinstance(identity, Mapping):
        return False
    model = identity.get("board_model")
    boot_id = identity.get("boot_id")
    return (
        isinstance(model, str)
        and 0 < len(model) <= MAX_TEXT
        and isinstance(boot_id, str)
        and BOOT_ID_RE.fullmatch(boot_id) is not None
    )


def _evidence_binding_valid(report: Mapping[str, object]) -> bool:
    identity = report.get("identity")
    run = report.get("run")
    if not isinstance(identity, Mapping) or not isinstance(run, Mapping):
        return False
    source_sha = identity.get("source_bundle_sha256")
    scenario = run.get("scenario")
    return (
        isinstance(source_sha, str)
        and source_sha == source_sha.lower()
        and SOURCE_BUNDLE_RE.fullmatch(source_sha) is not None
        and isinstance(scenario, str)
        and SCENARIO_RE.fullmatch(scenario) is not None
    )


def _sample_schedule_valid(
    samples: Sequence[object], interval: object, missed_slots: object
) -> bool:
    if (
        type(interval) not in (int, float)
        or not math.isfinite(interval)
        or interval <= 0
        or missed_slots != 0
    ):
        return False
    for sample in samples:
        if not isinstance(sample, Mapping):
            return False
        scheduled = sample.get("scheduled_elapsed_seconds")
        delay = sample.get("schedule_delay_seconds")
        if (
            type(scheduled) not in (int, float)
            or type(delay) not in (int, float)
            or not math.isfinite(scheduled)
            or not math.isfinite(delay)
            or scheduled < 0
            or delay < 0
            or delay > interval
        ):
            return False
    return bool(samples)


def _capture_continuous(samples: Sequence[object], expected_generation: object) -> bool:
    if type(expected_generation) is not int:
        return False
    for sample in samples:
        capture = _sample_scope(sample, "capture")
        if capture is None or capture.get("state") != "running" or capture.get("generation") != expected_generation:
            return False
    return bool(samples)


def _processes_stable(samples: Sequence[object]) -> bool:
    identities: dict[str, tuple[int, int]] = {}
    if not samples:
        return False
    for sample in samples:
        if not isinstance(sample, Mapping):
            return False
        system = sample.get("system")
        processes = system.get("processes") if isinstance(system, Mapping) else None
        if not isinstance(processes, Mapping) or not processes:
            return False
        for name, value in processes.items():
            if not isinstance(name, str) or not isinstance(value, Mapping) or value.get("available") is not True:
                return False
            pid = value.get("pid")
            start_ticks = value.get("start_ticks")
            io_value = value.get("io")
            if (
                type(pid) is not int
                or type(start_ticks) is not int
                or type(value.get("threads")) is not int
                or type(value.get("vm_hwm_bytes")) is not int
                or not isinstance(io_value, Mapping)
                or type(io_value.get("read_bytes")) is not int
                or type(io_value.get("write_bytes")) is not int
            ):
                return False
            identity = (pid, start_ticks)
            if name in identities and identities[name] != identity:
                return False
            identities[name] = identity
    return True


def _active_scopes_continuous(samples: Sequence[object], expected: object) -> bool:
    if not isinstance(expected, Mapping):
        return False
    for scope, generation in expected.items():
        if scope not in ("stream", "recording") or type(generation) is not int:
            return False
        for sample in samples:
            value = _sample_scope(sample, scope)
            if value is None or value.get("state") != "running" or value.get("generation") != generation:
                return False
            if scope == "stream":
                connection = _sample_scope(sample, "connection")
                if connection is None or connection.get("state") != "connected":
                    return False
    return bool(samples)


def _core_system_metrics_available(samples: Sequence[object]) -> bool:
    if not samples:
        return False
    for sample in samples:
        system = sample.get("system") if isinstance(sample, Mapping) else None
        if not isinstance(system, Mapping):
            return False
        cpu = system.get("cpu")
        memory = system.get("memory")
        network = system.get("network")
        storage = system.get("recording_storage")
        temperature = system.get("temperature_c")
        if (
            not isinstance(cpu, Mapping)
            or type(cpu.get("total_ticks")) is not int
            or type(cpu.get("load_1m")) not in (int, float)
            or not isinstance(memory, Mapping)
            or type(memory.get("mem_available_bytes")) is not int
            or type(memory.get("swap_total_bytes")) is not int
            or type(memory.get("swap_free_bytes")) is not int
            or not isinstance(network, Mapping)
            or not network
            or not isinstance(storage, Mapping)
            or type(storage.get("filesystem_used_bytes")) is not int
            or type(storage.get("directory_bytes")) is not int
            or type(temperature) not in (int, float)
            or not math.isfinite(temperature)
        ):
            return False
    return True


def _throttling_observation_clear(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    command = value.get("command")
    throttled = value.get("throttled_hex")
    if (
        not isinstance(command, list)
        or command != ["/usr/bin/vcgencmd", "get_throttled"]
        or value.get("returncode") != 0
        or value.get("error") is not None
        or not isinstance(throttled, str)
    ):
        return False
    try:
        return int(parse_throttled(throttled), 16) == 0
    except ValueError:
        return False


def _throttling_observed_and_clear(report: Mapping[str, object]) -> bool:
    boundary = report.get("boundary_metrics")
    samples = report.get("samples")
    if not isinstance(boundary, Mapping) or not isinstance(samples, list) or not samples:
        return False
    observations = [
        boundary.get("throttling_start"),
        *(sample.get("system", {}).get("throttling") if isinstance(sample, Mapping) and isinstance(sample.get("system"), Mapping) else None for sample in samples),
        boundary.get("throttling_end"),
    ]
    return all(_throttling_observation_clear(value) for value in observations)


def _queue_visibility_valid(
    samples: Sequence[object], *, require_spectrum: bool
) -> bool:
    for sample in samples:
        status = sample.get("status") if isinstance(sample, Mapping) else None
        if not isinstance(status, Mapping):
            return False
        state = status.get("state")
        queues = status.get("queues")
        queue_errors = status.get("queue_errors")
        if not isinstance(state, Mapping) or queue_errors:
            return False
        capture = state.get("capture")
        if (
            isinstance(capture, Mapping)
            and capture.get("state") == "running"
            and (not isinstance(queues, Mapping) or not isinstance(queues.get("level"), Mapping))
        ):
            return False
        for scope in ("stream", "recording"):
            value = state.get(scope)
            active = isinstance(value, Mapping) and value.get("state") in ACTIVE_STATES
            if active and (not isinstance(queues, Mapping) or not isinstance(queues.get(scope), Mapping)):
                return False
        if require_spectrum and (
            not isinstance(queues, Mapping)
            or not isinstance(queues.get("spectrum"), Mapping)
        ):
            return False
    return True


def _queue_bounds_valid(samples: Sequence[object]) -> bool:
    for sample in samples:
        status = sample.get("status") if isinstance(sample, Mapping) else None
        queues = status.get("queues") if isinstance(status, Mapping) else None
        if queues is None:
            continue
        if not isinstance(queues, Mapping):
            return False
        for name, queue in queues.items():
            if not isinstance(name, str) or not isinstance(queue, Mapping) or queue_invariant_errors(name, queue):
                return False
    return True


def _runtime_errors_absent(samples: Sequence[object]) -> bool:
    for sample in samples:
        status = sample.get("status") if isinstance(sample, Mapping) else None
        if not isinstance(status, Mapping) or status.get("runtime_error") is not None:
            return False
        state = status.get("state")
        if not isinstance(state, Mapping):
            return False
        for scope in ("capture", "stream", "connection", "recording", "monitoring"):
            value = state.get(scope)
            if isinstance(value, Mapping) and value.get("last_error") is not None:
                return False
        recording = state.get("recording")
        if isinstance(recording, Mapping) and recording.get("warning") is not None:
            return False
    return True


def _spectrum_observed(samples: Sequence[object]) -> bool:
    for sample in samples:
        status = sample.get("status") if isinstance(sample, Mapping) else None
        telemetry = status.get("telemetry") if isinstance(status, Mapping) else None
        if isinstance(telemetry, Mapping) and type(telemetry.get("spectrum_sequence")) is int:
            return True
    return False


def _bitrate_events_valid(report: Mapping[str, object]) -> bool:
    run = report.get("run")
    requested = run.get("opus_bitrates_requested") if isinstance(run, Mapping) else None
    events = report.get("bitrate_events")
    if not isinstance(requested, list) or not isinstance(events, list):
        return False
    observed = [
        event.get("observed_bitrate_bps")
        for event in events
        if isinstance(event, Mapping)
    ]
    return observed == requested


def _final_bitrate_dwell_observed(report: Mapping[str, object]) -> bool:
    run = report.get("run")
    samples = report.get("samples")
    events = report.get("bitrate_events")
    if not isinstance(run, Mapping):
        return False
    requested = run.get("opus_bitrates_requested")
    if requested == []:
        return True
    if (
        not isinstance(requested, list)
        or not requested
        or not isinstance(samples, list)
        or not isinstance(events, list)
        or not events
    ):
        return False
    last_event = events[-1]
    interval = run.get("sample_interval_seconds")
    if (
        not isinstance(last_event, Mapping)
        or type(last_event.get("elapsed_seconds")) not in (int, float)
        or type(interval) not in (int, float)
        or not math.isfinite(last_event["elapsed_seconds"])
        or not math.isfinite(interval)
    ):
        return False
    dwell_deadline = float(last_event["elapsed_seconds"]) + float(interval)
    final_target = requested[-1]
    for sample in samples:
        elapsed = sample.get("elapsed_seconds") if isinstance(sample, Mapping) else None
        stream = _sample_scope(sample, "stream")
        if (
            type(elapsed) in (int, float)
            and math.isfinite(elapsed)
            and elapsed + 1e-9 >= dwell_deadline
            and stream is not None
            and stream.get("opus_bitrate_bps") == final_target
        ):
            return True
    return False


def _normalized_runtime_clean(status: object) -> bool:
    if not isinstance(status, Mapping) or status.get("runtime_error") is not None:
        return False
    if status.get("queue_errors"):
        return False
    state = status.get("state")
    if not isinstance(state, Mapping):
        return False
    for scope in ("capture", "stream", "connection", "recording", "monitoring"):
        value = state.get(scope)
        if not isinstance(value, Mapping) or value.get("last_error") is not None:
            return False
    recording = state.get("recording")
    return not isinstance(recording, Mapping) or recording.get("warning") is None


def _final_state_valid(report: Mapping[str, object]) -> bool:
    final = report.get("final_status")
    run = report.get("run")
    if not _normalized_runtime_clean(final) or not isinstance(run, Mapping):
        return False
    assert isinstance(final, Mapping)
    state = final.get("state")
    queues = final.get("queues")
    if not isinstance(state, Mapping) or not isinstance(queues, Mapping):
        return False
    capture = state.get("capture")
    level = queues.get("level")
    if (
        not isinstance(capture, Mapping)
        or capture.get("state") != "running"
        or capture.get("generation") != run.get("capture_generation")
        or not isinstance(level, Mapping)
        or bool(queue_invariant_errors("level", level))
    ):
        return False
    active = run.get("active_scope_generations")
    started = run.get("started_scopes")
    if not isinstance(active, Mapping) or not isinstance(started, Mapping):
        return False
    for scope, generation in active.items():
        if scope not in ("stream", "recording") or type(generation) is not int:
            return False
        current = state.get(scope)
        if not isinstance(current, Mapping) or current.get("generation") != generation:
            return False
        if started.get(scope) == generation:
            if current.get("state") != "stopped":
                return False
        elif current.get("state") != "running":
            return False
        elif scope == "stream":
            connection = state.get("connection")
            if not isinstance(connection, Mapping) or connection.get("state") != "connected":
                return False
    return True


def _sample_scope(sample: object, scope: str) -> Mapping[str, object] | None:
    status = sample.get("status") if isinstance(sample, Mapping) else None
    state = status.get("state") if isinstance(status, Mapping) else None
    value = state.get(scope) if isinstance(state, Mapping) else None
    return value if isinstance(value, Mapping) else None


def _sample_metric_points(
    samples: Sequence[object], kind: str, name: str, metric: str
) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []
    for sample in samples:
        if not isinstance(sample, Mapping):
            continue
        elapsed = sample.get("elapsed_seconds")
        value: object = None
        if kind == "process":
            system = sample.get("system")
            processes = system.get("processes") if isinstance(system, Mapping) else None
            process = processes.get(name) if isinstance(processes, Mapping) else None
            if isinstance(process, Mapping) and metric in ("read_bytes", "write_bytes"):
                io_value = process.get("io")
                value = io_value.get(metric) if isinstance(io_value, Mapping) else None
            else:
                value = process.get(metric) if isinstance(process, Mapping) else None
        elif kind == "queue":
            status = sample.get("status")
            queues = status.get("queues") if isinstance(status, Mapping) else None
            queue = queues.get(name) if isinstance(queues, Mapping) else None
            value = queue.get(metric) if isinstance(queue, Mapping) else None
        elif kind == "nested_system":
            system = sample.get("system")
            nested = system.get(name) if isinstance(system, Mapping) else None
            value = nested.get(metric) if isinstance(nested, Mapping) else None
        elif kind == "network":
            system = sample.get("system")
            network = system.get("network") if isinstance(system, Mapping) else None
            interface = network.get(name) if isinstance(network, Mapping) else None
            value = interface.get(metric) if isinstance(interface, Mapping) else None
        else:
            system = sample.get("system")
            value = system.get(metric) if isinstance(system, Mapping) else None
        if _finite_pair(elapsed, value):
            result.append((float(elapsed), float(value)))
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-socket", type=Path, required=True)
    parser.add_argument("--media-pid", type=int, required=True)
    parser.add_argument("--web-pid", type=int, required=True)
    parser.add_argument(
        "--source-bundle-sha256",
        required=True,
        help="exact 64-hex SHA-256 of the immutable source bundle under test",
    )
    parser.add_argument(
        "--scenario",
        required=True,
        help="bounded lowercase run label, for example opus-concurrent-64bands",
    )
    parser.add_argument("--duration-seconds", type=float, default=60.0)
    parser.add_argument("--sample-interval-seconds", type=float, default=1.0)
    parser.add_argument("--transition-timeout-seconds", type=float, default=15.0)
    parser.add_argument("--start-stream", action="store_true")
    parser.add_argument(
        "--expected-representation",
        choices=tuple(sorted(APPROVED_REPRESENTATIONS)),
    )
    parser.add_argument("--start-recording", action="store_true")
    parser.add_argument("--spectrum", action="store_true")
    parser.add_argument(
        "--opus-bitrate",
        dest="opus_bitrates",
        type=int,
        action="append",
        default=[],
        help="schedule one approved bitrate; repeat at most eight times",
    )
    parser.add_argument("--bitrate-interval-seconds", type=float, default=10.0)
    parser.add_argument("--checkpoint-interval-seconds", type=float, default=3600.0)
    parser.add_argument(
        "--temperature-path",
        type=Path,
        default=Path("/sys/class/thermal/thermal_zone0/temp"),
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser


def _validated_args(argv: Sequence[str] | None) -> tuple[argparse.Namespace, ProfilePlan]:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.media_pid <= 0 or args.web_pid <= 0 or args.media_pid == args.web_pid:
        parser.error("media and web PIDs must be positive")
    try:
        plan = ProfilePlan(
            duration_seconds=args.duration_seconds,
            sample_interval_seconds=args.sample_interval_seconds,
            transition_timeout_seconds=args.transition_timeout_seconds,
            start_stream=args.start_stream,
            start_recording=args.start_recording,
            spectrum=args.spectrum,
            opus_bitrates=tuple(args.opus_bitrates),
            bitrate_interval_seconds=args.bitrate_interval_seconds,
            checkpoint_interval_seconds=args.checkpoint_interval_seconds,
            expected_representation=args.expected_representation,
        )
        if SOURCE_BUNDLE_RE.fullmatch(args.source_bundle_sha256) is None:
            raise ValueError("source bundle SHA-256 must be exactly 64 hexadecimal characters")
        if SCENARIO_RE.fullmatch(args.scenario) is None:
            raise ValueError("scenario must match [a-z0-9][a-z0-9._-]{0,63}")
    except ValueError as exc:
        parser.error(str(exc))
    return args, plan


def _write_report(path: Path, report: Mapping[str, object]) -> None:
    rendered = json.dumps(_bounded_redacted(report), indent=2, sort_keys=True) + "\n"
    parent = path.resolve().parent
    if not parent.is_dir():
        raise OSError("output parent directory does not exist")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
    temporary_path = Path(temporary)
    try:
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            temporary_path.unlink()
        except OSError:
            pass
        raise


def main(argv: Sequence[str] | None = None) -> int:
    args, plan = _validated_args(argv)
    client = ControlClient(args.control_socket, timeout=min(5.0, plan.transition_timeout_seconds))
    sampler = SystemSampler(
        {"media": args.media_pid, "web": args.web_pid},
        temperature_path=args.temperature_path,
    )
    report = InstalledProfiler(
        client, sampler, plan, args.source_bundle_sha256, args.scenario
    ).run(lambda partial: _write_report(args.output, partial))
    try:
        _write_report(args.output, report)
    except OSError as exc:
        sys.stderr.write(f"could not write profile report: {_safe_error(exc)}\n")
        return 2
    result = report.get("result")
    execution_status = (
        result.get("execution_status") if isinstance(result, Mapping) else "fail"
    )
    sys.stdout.write(
        json.dumps(
            {
                "execution_status": execution_status,
                "output": str(args.output),
                "sample_count": len(report.get("samples", [])),
                "scenario": args.scenario,
                "source_bundle_sha256": args.source_bundle_sha256.lower(),
            },
            sort_keys=True,
        )
        + "\n"
    )
    return 0 if execution_status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
