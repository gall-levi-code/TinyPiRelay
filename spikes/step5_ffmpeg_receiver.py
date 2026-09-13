#!/usr/bin/env python3
"""Disposable Step 5 FFmpeg SRT receiver, optionally publishing decoded PCM.

This workstation-only evidence tool is not a TinyPiRelay production component.
It accepts the approved streamable-Matroska baseline without SRT stream IDs or
encryption, decodes exactly one audio stream, and retains only bounded text
observations.
An optional RTSP output is a bridge-integration variant; the original null
output still measures incoming decode continuity, not downstream delivery.
"""

from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime, timezone
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SCHEMA_VERSION = 1
TOOL_NAME = "step5_ffmpeg_srt_receiver"
SUPPORTED_CODECS = ("pcm_s16le", "flac", "opus")
FORBIDDEN_OPTIONS = frozenset(
    ("--passphrase", "--stream-id", "--streamid", "--srt-streamid")
)
MAX_STDERR_LINES = 40
MAX_EVENT_LINES = 16
MAX_LINE_CHARS = 512
POLL_SECONDS = 0.05
INTERRUPT_GRACE_SECONDS = 5.0
TERMINATE_GRACE_SECONDS = 2.0
# `asetnsamples` emits one-second frames.  Allow one frame plus modest scheduler
# jitter when comparing decoded coverage and the final decoded-progress time.
PROGRESS_TOLERANCE_SECONDS = 1.25
# FFmpeg may report one final one-second frame crossing the output-duration
# boundary.  It must not accept a longer decoded overrun than that plus jitter.
PROGRESS_OVERRUN_TOLERANCE_SECONDS = 1.25
MAX_PROGRESS_WALL_GAP_SECONDS = 1.5
PTS_JITTER_TOLERANCE_SECONDS = 0.005
OPUS_STARTUP_PTS_ADJUSTMENT_SECONDS = 0.010

_INPUT_AUDIO = re.compile(
    r"Stream #0:\d+(?:\([^)]*\))?:\s*Audio:\s*"
    r"(?P<codec>[A-Za-z0-9_]+).*?,\s*(?P<rate>\d+)\s*Hz,\s*"
    r"(?P<layout>[^,\r\n]+)",
    re.IGNORECASE,
)
_ASHOWINFO = re.compile(r"\bashowinfo\b|Parsed_ashowinfo", re.IGNORECASE)
_FIELD = re.compile(r"(?:^|\s)([a-z_]+):([^\s]+)")
_ERROR = re.compile(
    r"\b(error|failed|failure|invalid data|timed out|timeout|refused|"
    r"broken pipe|unable to)\b",
    re.IGNORECASE,
)
_WARNING = re.compile(r"\bwarning\b", re.IGNORECASE)
_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
_SCENARIO = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def listener_uri(port: int, latency_ms: int, bind_address: str = "0.0.0.0") -> str:
    # FFmpeg's libsrt latency option is expressed in microseconds.
    return (
        f"srt://{bind_address}:{port}?mode=listener&latency={latency_ms * 1_000}"
    )


def ffmpeg_argv(args: argparse.Namespace) -> list[str]:
    argv = [
        str(args.ffmpeg_path),
        "-hide_banner",
        "-nostdin",
        "-nostats",
        "-loglevel",
        "info",
        "-i",
        listener_uri(args.port, args.latency_ms, getattr(args, "bind_address", "0.0.0.0")),
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-af",
        f"asetnsamples=n={args.expected_rate_hz}:p=0,ashowinfo",
        "-c:a",
        "pcm_s16le",
        "-t",
        str(args.duration_seconds),
        "-f",
        "null",
        "-",
    ]
    if getattr(args, "rtsp_publish_url", None):
        argv.extend(
            [
                "-map", "0:a:0", "-vn", "-sn", "-dn",
                "-c:a", "pcm_s16be",
                "-t", str(args.duration_seconds),
                "-rtsp_transport", "tcp",
                "-f", "rtsp", args.rtsp_publish_url,
            ]
        )
    return argv


def _redact_uri(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "srt://<listener>"
    if parsed.scheme.casefold() != "srt":
        return value
    try:
        port = parsed.port
    except ValueError:
        port = None
    netloc = "<listener>" + (f":{port}" if port is not None else "")
    query = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        if key.casefold() in {"passphrase", "streamid", "srt_streamid"}:
            item = "<redacted>"
        query.append((key, item))
    return urlunsplit(("srt", netloc, parsed.path, urlencode(query), parsed.fragment))


def redact_text(value: str) -> str:
    # FFmpeg diagnostics may repeat its input URI. Preserve the port and safe
    # transport settings while hiding the host and any unsupported identity.
    def replace(match: re.Match[str]) -> str:
        return _redact_uri(match.group(0))

    value = re.sub(r"srt://[^\s'\"]+", replace, value, flags=re.IGNORECASE)
    value = re.sub(
        r"(?i)((?:passphrase|streamid|srt_streamid)=)[^&\s'\"]+",
        r"\1<redacted>",
        value,
    )
    return value


def redact_argv(argv: Sequence[str]) -> list[str]:
    return [redact_text(item) for item in argv]


def _channels_from_layout(value: str) -> int | None:
    normalized = value.strip().casefold()
    if normalized == "mono":
        return 1
    if normalized == "stereo":
        return 2
    match = re.match(r"(\d+)\s+channels?\b", normalized)
    if match:
        return int(match.group(1))
    match = re.match(r"(\d+)\.(\d+)", normalized)
    if match:
        return int(match.group(1)) + int(match.group(2))
    return None


class FFmpegLogParser:
    """Keep bounded summaries rather than retaining FFmpeg's full log."""

    def __init__(self) -> None:
        self.input_codec: str | None = None
        self.input_rate_hz: int | None = None
        self.input_channels: int | None = None
        self.decoded_rates_hz: set[int] = set()
        self.decoded_channels: set[int] = set()
        self.frame_count = 0
        self.total_decoded_seconds = 0.0
        self.first_pts_seconds: float | None = None
        self.last_pts_seconds: float | None = None
        self.max_pts_gap_seconds = 0.0
        self.max_gap_deviation_seconds = 0.0
        self.missing_pts = 0
        self.non_monotonic_pts = 0
        self.discontinuities = 0
        self.startup_pts_adjustments = 0
        self.max_startup_pts_adjustment_seconds = 0.0
        self._last_frame_index: int | None = None
        self._last_frame_duration: float | None = None
        self._output_started = False
        self.tail: deque[str] = deque(maxlen=MAX_STDERR_LINES)
        self.warnings: deque[str] = deque(maxlen=MAX_EVENT_LINES)
        self.errors: deque[str] = deque(maxlen=MAX_EVENT_LINES)

    def feed_line(self, raw_line: str) -> bool:
        line = redact_text(raw_line.rstrip("\r\n"))[-MAX_LINE_CHARS:]
        if line:
            self.tail.append(line)
            if _WARNING.search(line):
                self.warnings.append(line)
            if _ERROR.search(line):
                self.errors.append(line)

        if "Output #" in line:
            self._output_started = True

        if self.input_codec is None and not self._output_started:
            match = _INPUT_AUDIO.search(line)
            if match:
                self.input_codec = match.group("codec").casefold()
                self.input_rate_hz = int(match.group("rate"))
                self.input_channels = _channels_from_layout(match.group("layout"))

        if not _ASHOWINFO.search(line):
            return False
        fields = dict(_FIELD.findall(line))
        if "n" not in fields or "nb_samples" not in fields or "rate" not in fields:
            return False
        try:
            frame_index = int(fields["n"])
            samples = int(fields["nb_samples"])
            rate = int(fields["rate"])
        except ValueError:
            return False
        if frame_index < 0 or samples <= 0 or rate <= 0:
            return False

        channels: int | None = None
        if "channels" in fields:
            try:
                channels = int(fields["channels"])
            except ValueError:
                channels = None
        if channels is None and "chlayout" in fields:
            channels = _channels_from_layout(fields["chlayout"])

        self.frame_count += 1
        self.total_decoded_seconds += samples / rate
        self.decoded_rates_hz.add(rate)
        if channels is not None:
            self.decoded_channels.add(channels)

        if (
            self._last_frame_index is not None
            and frame_index != self._last_frame_index + 1
        ):
            self.discontinuities += 1
        self._last_frame_index = frame_index

        pts_text = fields.get("pts_time")
        try:
            pts = float(pts_text) if pts_text not in (None, "N/A", "NOPTS") else None
        except ValueError:
            pts = None
        if pts is None or not math.isfinite(pts):
            self.missing_pts += 1
            self.discontinuities += 1
        else:
            if self.first_pts_seconds is None:
                self.first_pts_seconds = pts
            if self.last_pts_seconds is not None:
                gap = pts - self.last_pts_seconds
                if gap <= 0:
                    self.non_monotonic_pts += 1
                    self.discontinuities += 1
                else:
                    self.max_pts_gap_seconds = max(self.max_pts_gap_seconds, gap)
                    if self._last_frame_duration is not None:
                        deviation = abs(gap - self._last_frame_duration)
                        self.max_gap_deviation_seconds = max(
                            self.max_gap_deviation_seconds, deviation
                        )
                        tolerance = max(
                            PTS_JITTER_TOLERANCE_SECONDS,
                            self._last_frame_duration * 0.005,
                        )
                        startup_adjustment = (
                            self.input_codec == "opus"
                            and self.frame_count == 2
                            and frame_index == 1
                            and gap < self._last_frame_duration
                            and deviation <= OPUS_STARTUP_PTS_ADJUSTMENT_SECONDS
                        )
                        if startup_adjustment:
                            self.startup_pts_adjustments += 1
                            self.max_startup_pts_adjustment_seconds = max(
                                self.max_startup_pts_adjustment_seconds, deviation
                            )
                        elif deviation > tolerance:
                            self.discontinuities += 1
            self.last_pts_seconds = pts
        self._last_frame_duration = samples / rate
        return True

    def snapshot(self) -> dict[str, object]:
        return {
            "observed_audio": {
                "input_codec": self.input_codec,
                "input_sample_rate_hz": self.input_rate_hz,
                "input_channels": self.input_channels,
                "decoded_sample_rates_hz": sorted(self.decoded_rates_hz),
                "decoded_channel_counts": sorted(self.decoded_channels),
            },
            "decoded_progress": {
                "frame_count": self.frame_count,
                "decoded_seconds": round(self.total_decoded_seconds, 6),
                "first_pts_seconds": self.first_pts_seconds,
                "last_pts_seconds": self.last_pts_seconds,
                "max_pts_gap_seconds": round(self.max_pts_gap_seconds, 6),
                "max_gap_deviation_seconds": round(
                    self.max_gap_deviation_seconds, 6
                ),
                "missing_pts": self.missing_pts,
                "non_monotonic_pts": self.non_monotonic_pts,
                "discontinuities": self.discontinuities,
                "startup_pts_adjustments": self.startup_pts_adjustments,
                "max_startup_pts_adjustment_seconds": round(
                    self.max_startup_pts_adjustment_seconds, 6
                ),
            },
            "warnings": list(self.warnings),
            "errors": list(self.errors),
            "stderr_tail": list(self.tail),
        }


def validate_args(args: argparse.Namespace) -> None:
    bind_address = getattr(args, "bind_address", "0.0.0.0")
    if not isinstance(bind_address, str):
        raise ValueError("bind-address must be a literal IPv4 address")
    try:
        ipaddress.IPv4Address(bind_address)
    except ValueError:
        raise ValueError("bind-address must be a literal IPv4 address") from None
    if not 1 <= args.port <= 65_535:
        raise ValueError("port must be between 1 and 65535")
    if not 20 <= args.latency_ms <= 8_000:
        raise ValueError("latency-ms must be between 20 and 8000")
    if not 2 <= args.duration_seconds <= 86_400:
        raise ValueError("duration-seconds must be between 2 and 86400")
    if not args.duration_seconds < args.timeout_seconds <= 172_800:
        raise ValueError("timeout-seconds must exceed duration and be at most 172800")
    if args.expected_codec not in SUPPORTED_CODECS:
        raise ValueError("expected-codec is not a Step 5 baseline codec")
    if not 8_000 <= args.expected_rate_hz <= 384_000:
        raise ValueError("expected-rate-hz must be between 8000 and 384000")
    if not 1 <= args.expected_channels <= 32:
        raise ValueError("expected-channels must be between 1 and 32")
    executable = str(args.ffmpeg_path)
    if not executable or len(executable) > 4_096 or any(ord(item) < 32 for item in executable):
        raise ValueError("ffmpeg-path is invalid")
    if not isinstance(args.scenario, str) or not _SCENARIO.fullmatch(args.scenario):
        raise ValueError("scenario must match [a-z0-9][a-z0-9._-]{0,63}")
    if not isinstance(args.source_bundle_sha256, str) or not _SHA256.fullmatch(
        args.source_bundle_sha256
    ):
        raise ValueError("source-bundle-sha256 must be exactly 64 hexadecimal characters")
    publish_url = getattr(args, "rtsp_publish_url", None)
    if publish_url is not None:
        try:
            if (
                not isinstance(publish_url, str)
                or len(publish_url) > 2_048
                or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in publish_url)
                or "\\" in publish_url
            ):
                raise ValueError
            parsed = urlsplit(publish_url)
            if (
                parsed.scheme != "rtsp" or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.path in ("", "/")
                or (parsed.port is not None and not 1 <= parsed.port <= 65_535)
            ):
                raise ValueError
        except ValueError:
            raise ValueError(
                "rtsp-publish-url must be an rtsp URL with a host and path, "
                "without credentials, query, fragment, or whitespace"
            ) from None


def _ffmpeg_identity(
    executable: str,
    run_factory: Callable[..., Any],
) -> dict[str, object]:
    command = [executable, "-hide_banner", "-version"]
    try:
        result = run_factory(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "executable": executable,
            "identity_argv": command,
            "exit_code": None,
            "version_line": None,
            "configuration_line": None,
            "libsrt_enabled": False,
            "error": redact_text(f"{type(exc).__name__}: {exc}")[:MAX_LINE_CHARS],
        }
    lines = (result.stdout or "").splitlines()
    configuration_line = next(
        (line for line in lines if line.casefold().startswith("configuration:")),
        None,
    )
    return {
        "executable": executable,
        "identity_argv": command,
        "exit_code": result.returncode,
        "version_line": redact_text(lines[0])[:MAX_LINE_CHARS] if lines else None,
        "configuration_line": (
            redact_text(configuration_line)[:MAX_LINE_CHARS]
            if configuration_line is not None
            else None
        ),
        "libsrt_enabled": bool(
            configuration_line is not None
            and "--enable-libsrt" in configuration_line.split()
        ),
        "error": None,
    }


def _stop_process(process: Any) -> dict[str, object]:
    cleanup = {
        "signal": None,
        "terminate_used": False,
        "kill_used": False,
        "completed": False,
    }
    if process.poll() is not None:
        cleanup["completed"] = True
        return cleanup
    stop_signal = (
        getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM)
        if os.name == "nt"
        else signal.SIGINT
    )
    try:
        process.send_signal(stop_signal)
        cleanup["signal"] = getattr(stop_signal, "name", str(stop_signal))
        process.wait(timeout=INTERRUPT_GRACE_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        if process.poll() is None:
            cleanup["terminate_used"] = True
            try:
                process.terminate()
                process.wait(timeout=TERMINATE_GRACE_SECONDS)
            except (OSError, subprocess.TimeoutExpired):
                if process.poll() is None:
                    cleanup["kill_used"] = True
                    try:
                        process.kill()
                        process.wait(timeout=TERMINATE_GRACE_SECONDS)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
    cleanup["completed"] = process.poll() is not None
    return cleanup


def assess_report(report: Mapping[str, object]) -> dict[str, object]:
    configuration = report.get("configuration")
    ffmpeg = report.get("ffmpeg")
    observation = report.get("observation")
    setup_errors = report.get("setup_errors")
    if not isinstance(configuration, Mapping):
        configuration = {}
    if not isinstance(ffmpeg, Mapping):
        ffmpeg = {}
    if not isinstance(observation, Mapping):
        observation = {}
    if not isinstance(setup_errors, list):
        setup_errors = ["setup error shape is invalid"]

    audio = observation.get("observed_audio")
    progress = observation.get("decoded_progress")
    cleanup = observation.get("cleanup")
    if not isinstance(audio, Mapping):
        audio = {}
    if not isinstance(progress, Mapping):
        progress = {}
    if not isinstance(cleanup, Mapping):
        cleanup = {}

    expected_codec = configuration.get("expected_codec")
    expected_rate = configuration.get("expected_rate_hz")
    expected_channels = configuration.get("expected_channels")
    duration = configuration.get("duration_seconds")
    duration_value = (
        float(duration)
        if type(duration) in (int, float)
        and math.isfinite(float(duration))
        and float(duration) > 0
        else 0.0
    )
    timeout = configuration.get("timeout_seconds")
    timeout_value = (
        float(timeout)
        if type(timeout) in (int, float)
        and math.isfinite(float(timeout))
        and float(timeout) > 0
        else math.inf
    )
    minimum_frames = max(
        1, math.ceil(max(0.0, duration_value - PROGRESS_TOLERANCE_SECONDS))
    )
    decoded_seconds = progress.get("decoded_seconds")
    first_pts = progress.get("first_pts_seconds")
    last_pts = progress.get("last_pts_seconds")
    first_progress = observation.get("first_decoded_progress_wall_seconds")
    last_progress = observation.get("last_decoded_progress_wall_seconds")
    tail_stall = observation.get("decoded_progress_tail_stall_seconds")
    max_wall_gap = observation.get("max_decoded_progress_wall_gap_seconds")
    source_bundle_sha256 = report.get("source_bundle_sha256")
    scenario = report.get("scenario")

    checks = {
        "evidence_binding": (
            isinstance(source_bundle_sha256, str)
            and _SHA256.fullmatch(source_bundle_sha256) is not None
            and source_bundle_sha256 == source_bundle_sha256.casefold()
            and isinstance(scenario, str)
            and _SCENARIO.fullmatch(scenario) is not None
        ),
        "ffmpeg_identity": (
            not setup_errors
            and ffmpeg.get("exit_code") == 0
            and isinstance(ffmpeg.get("version_line"), str)
            and str(ffmpeg["version_line"]).casefold().startswith("ffmpeg version")
            and ffmpeg.get("libsrt_enabled") is True
        ),
        "expected_audio_caps": (
            audio.get("input_codec") == expected_codec
            and audio.get("input_sample_rate_hz") == expected_rate
            and audio.get("input_channels") == expected_channels
            and audio.get("decoded_sample_rates_hz") == [expected_rate]
            and audio.get("decoded_channel_counts") == [expected_channels]
        ),
        "decoded_progress": (
            type(progress.get("frame_count")) is int
            and int(progress.get("frame_count", 0)) >= minimum_frames
            and type(decoded_seconds) in (int, float)
            and math.isfinite(float(decoded_seconds))
            and float(decoded_seconds)
            >= duration_value - PROGRESS_TOLERANCE_SECONDS
            and type(first_pts) in (int, float)
            and type(last_pts) in (int, float)
            and math.isfinite(float(first_pts))
            and math.isfinite(float(last_pts))
            and duration_value - PROGRESS_TOLERANCE_SECONDS
            <= float(last_pts) - float(first_pts)
            <= duration_value + PROGRESS_OVERRUN_TOLERANCE_SECONDS
        ),
        "pts_are_monotonic_and_continuous": (
            progress.get("missing_pts") == 0
            and progress.get("non_monotonic_pts") == 0
            and progress.get("discontinuities") == 0
        ),
        "ffmpeg_errors_absent": observation.get("errors") == [],
        "duration_completed_without_timeout": (
            observation.get("termination_reason") == "process_exit"
            and observation.get("duration_reached") is True
            and observation.get("timed_out") is False
            and observation.get("exit_code") == 0
        ),
        "first_decoded_progress_observed": (
            type(first_progress) in (int, float)
            and math.isfinite(float(first_progress))
            and 0
            <= float(first_progress)
            <= float(last_progress)
            < timeout_value
            if type(last_progress) in (int, float)
            and math.isfinite(float(last_progress))
            else False
        ),
        "decoded_progress_wall_coverage": (
            type(first_progress) in (int, float)
            and type(last_progress) in (int, float)
            and math.isfinite(float(first_progress))
            and math.isfinite(float(last_progress))
            and float(last_progress) - float(first_progress)
            >= max(0.0, duration_value - PROGRESS_TOLERANCE_SECONDS)
        ),
        "decoded_progress_tail_stall_bounded": (
            type(tail_stall) in (int, float)
            and math.isfinite(float(tail_stall))
            and 0 <= float(tail_stall) <= PROGRESS_TOLERANCE_SECONDS
        ),
        "decoded_progress_wall_gaps_bounded": (
            type(max_wall_gap) in (int, float)
            and math.isfinite(float(max_wall_gap))
            and 0 <= float(max_wall_gap) <= MAX_PROGRESS_WALL_GAP_SECONDS
        ),
        "receiver_process_cleaned_up": (
            cleanup.get("completed") is True
            and cleanup.get("stderr_reader_completed") is True
            and cleanup.get("stderr_forced_close_used") is False
            and cleanup.get("stderr_reader_error") is None
            and cleanup.get("signal") is None
            and cleanup.get("terminate_used") is False
            and cleanup.get("kill_used") is False
        ),
        "audio_was_not_retained": (
            configuration.get("audio_retained") is False
            and configuration.get("srt_passphrase_supported") is False
            and configuration.get("srt_stream_id_supported") is False
        ),
    }
    reasons = [name for name, passed in checks.items() if not passed]
    return {
        "status": "pass" if not reasons else "fail",
        "checks": checks,
        "reasons": reasons,
    }


def run_receiver(
    args: argparse.Namespace,
    *,
    popen_factory: Callable[..., Any] = subprocess.Popen,
    run_factory: Callable[..., Any] = subprocess.run,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    validate_args(args)
    argv = ffmpeg_argv(args)
    identity = _ffmpeg_identity(str(args.ffmpeg_path), run_factory)
    report: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "step": 5,
        "tool": TOOL_NAME,
        "production_component": False,
        "evidence_classification": "disposable-workstation-receiver",
        "scenario": args.scenario,
        "source_bundle_sha256": args.source_bundle_sha256.casefold(),
        "started_at_utc": utc_now(),
        "ended_at_utc": None,
        "configuration": {
            "port": args.port,
            "latency_ms": args.latency_ms,
            "duration_seconds": args.duration_seconds,
            "timeout_seconds": args.timeout_seconds,
            "expected_codec": args.expected_codec,
            "expected_rate_hz": args.expected_rate_hz,
            "expected_channels": args.expected_channels,
            "srt_passphrase_supported": False,
            "srt_stream_id_supported": False,
            "audio_retained": False,
        },
        "ffmpeg": {**identity, "argv_redacted": redact_argv(argv)},
        "setup_errors": [],
        "observation": {},
        "result": None,
    }
    if getattr(args, "rtsp_publish_url", None):
        report["evidence_classification"] = "disposable-workstation-receiver-rtsp-bridge"
        report["configuration"]["rtsp_publish"] = {
            "url": args.rtsp_publish_url,
            "codec": "pcm_s16be",
            "transport": "tcp",
            "rate_and_channels": "preserved_from_input",
            "continuity_scope": "incoming decode; downstream delivery assessed separately",
        }
    if getattr(args, "bind_address", "0.0.0.0") != "0.0.0.0":
        report["configuration"]["bind_address"] = args.bind_address
    if identity.get("error") is not None or identity.get("exit_code") != 0:
        report["setup_errors"] = [identity.get("error") or "FFmpeg identity failed"]
        report["ended_at_utc"] = utc_now()
        report["result"] = assess_report(report)
        return report

    parser = FFmpegLogParser()
    reader_done = threading.Event()
    process: Any | None = None
    started = clock()
    first_progress_at: float | None = None
    last_progress_at: float | None = None
    max_progress_wall_gap = 0.0
    measurement_ended_at: float | None = None
    stderr_reader_error: str | None = None
    stderr_forced_close_used = False
    termination_reason = "setup_failure"
    timed_out = False
    duration_reached = False
    cleanup: dict[str, object] = {
        "signal": None,
        "terminate_used": False,
        "kill_used": False,
        "completed": True,
    }

    def read_stderr(stream: Any) -> None:
        nonlocal first_progress_at, last_progress_at, max_progress_wall_gap
        nonlocal stderr_reader_error
        try:
            for line in stream:
                if parser.feed_line(line):
                    progress_at = clock()
                    if first_progress_at is None:
                        first_progress_at = progress_at
                    if last_progress_at is not None:
                        max_progress_wall_gap = max(
                            max_progress_wall_gap,
                            max(0.0, progress_at - last_progress_at),
                        )
                    last_progress_at = progress_at
        except (OSError, ValueError) as exc:
            message = redact_text(
                f"stderr reader failed: {type(exc).__name__}: {exc}"
            )[-MAX_LINE_CHARS:]
            stderr_reader_error = message
            parser.errors.append(message)
        finally:
            reader_done.set()

    try:
        popen_kwargs: dict[str, object] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        process = popen_factory(argv, **popen_kwargs)
        if process.stderr is None:
            raise RuntimeError("FFmpeg stderr pipe was not created")
        reader = threading.Thread(
            target=read_stderr,
            args=(process.stderr,),
            name="step5-ffmpeg-stderr",
            daemon=True,
        )
        reader.start()

        while True:
            now = clock()
            if process.poll() is not None:
                termination_reason = "process_exit"
                measurement_ended_at = now
                break
            if now - started >= args.timeout_seconds:
                timed_out = True
                termination_reason = "timeout"
                measurement_ended_at = now
                break
            sleeper(POLL_SECONDS)

        if process.poll() is None:
            cleanup = _stop_process(process)
        reader.join(timeout=TERMINATE_GRACE_SECONDS)
        if not reader_done.is_set():
            stderr_forced_close_used = True
            try:
                process.stderr.close()
            except OSError:
                pass
            reader.join(timeout=TERMINATE_GRACE_SECONDS)
        cleanup["stderr_reader_completed"] = reader_done.is_set()
        cleanup["stderr_forced_close_used"] = stderr_forced_close_used
        cleanup["stderr_reader_error"] = stderr_reader_error
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        report["setup_errors"] = [
            redact_text(f"{type(exc).__name__}: {exc}")[:MAX_LINE_CHARS]
        ]
        if process is not None:
            cleanup = _stop_process(process)
        termination_reason = "setup_failure"

    finished = clock()
    if measurement_ended_at is None:
        measurement_ended_at = finished
    elapsed = max(0.0, finished - started)
    parser_snapshot = parser.snapshot()
    decoded_progress = parser_snapshot.get("decoded_progress")
    decoded_seconds = (
        decoded_progress.get("decoded_seconds")
        if isinstance(decoded_progress, Mapping)
        else None
    )
    wall_coverage = (
        last_progress_at - first_progress_at
        if first_progress_at is not None and last_progress_at is not None
        else None
    )
    duration_reached = bool(
        termination_reason == "process_exit"
        and process is not None
        and process.poll() == 0
        and type(decoded_seconds) in (int, float)
        and float(decoded_seconds)
        >= args.duration_seconds - PROGRESS_TOLERANCE_SECONDS
        and wall_coverage is not None
        and wall_coverage
        >= max(0.0, args.duration_seconds - PROGRESS_TOLERANCE_SECONDS)
    )
    observation = {
        **parser_snapshot,
        "wall_duration_seconds": round(elapsed, 6),
        "first_decoded_progress_wall_seconds": (
            round(max(0.0, first_progress_at - started), 6)
            if first_progress_at is not None
            else None
        ),
        "last_decoded_progress_wall_seconds": (
            round(max(0.0, last_progress_at - started), 6)
            if last_progress_at is not None
            else None
        ),
        "decoded_progress_tail_stall_seconds": (
            round(max(0.0, measurement_ended_at - last_progress_at), 6)
            if last_progress_at is not None
            else None
        ),
        "max_decoded_progress_wall_gap_seconds": round(
            max_progress_wall_gap, 6
        ),
        "termination_reason": termination_reason,
        "duration_reached": duration_reached,
        "timed_out": timed_out,
        "exit_code": process.poll() if process is not None else None,
        "cleanup": cleanup,
    }
    report["observation"] = observation
    report["ended_at_utc"] = utc_now()
    report["result"] = assess_report(report)
    return report


def _forbidden_option_present(argv: Sequence[str]) -> bool:
    return any(item.split("=", 1)[0].casefold() in FORBIDDEN_OPTIONS for item in argv)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--bind-address", default="0.0.0.0", help="literal IPv4 listener address")
    parser.add_argument("--latency-ms", type=int, required=True)
    parser.add_argument("--duration-seconds", type=float, required=True)
    parser.add_argument("--timeout-seconds", type=float, required=True)
    parser.add_argument("--expected-codec", choices=SUPPORTED_CODECS, required=True)
    parser.add_argument("--expected-rate-hz", type=int, required=True)
    parser.add_argument("--expected-channels", type=int, required=True)
    parser.add_argument("--ffmpeg-path", type=Path, required=True)
    parser.add_argument(
        "--rtsp-publish-url",
        help="optional credential-free RTSP destination for a bounded PCM bridge output",
    )
    parser.add_argument(
        "--scenario",
        "--run-label",
        dest="scenario",
        required=True,
        help="bounded scenario key shared with the Pi profiler artifact",
    )
    parser.add_argument("--source-bundle-sha256", required=True)
    return parser


def _write_report(report: Mapping[str, object], output: Path) -> None:
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    output.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if _forbidden_option_present(arguments):
        print(
            "SRT passphrases and stream IDs are intentionally unsupported by this baseline receiver.",
            file=sys.stderr,
        )
        return 2
    parser = build_parser()
    args = parser.parse_args(arguments)
    try:
        validate_args(args)
    except ValueError as exc:
        parser.error(str(exc))
    report = run_receiver(args)
    try:
        _write_report(report, args.output)
    except OSError as exc:
        print(
            json.dumps({"status": "fail", "error": f"output write failed: {exc}"}),
            file=sys.stderr,
        )
        return 2
    result = report.get("result")
    if isinstance(result, Mapping) and result.get("status") == "pass":
        return 0
    return 2 if report.get("setup_errors") else 1


if __name__ == "__main__":
    raise SystemExit(main())
