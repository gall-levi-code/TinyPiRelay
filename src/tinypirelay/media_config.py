"""Strict Step 2 media configuration and recording-destination safety."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


SCHEMA_VERSION = 1
STORAGE_LIMIT_PERCENT = 90
CAPTURE_FORMATS = frozenset({"S16LE", "S24LE", "S24_32LE", "S32LE"})
CAPTURE_RATES_HZ = frozenset({44_100, 48_000, 96_000})
CAPTURE_CHANNELS = frozenset({1, 2})
OPUS_BITRATES_BPS = frozenset({64_000, 96_000, 128_000, 192_000})
DESTINATION_HOST_MAX_BYTES = 253
DEVICE_ID_MAX_BYTES = 256
STREAM_ID_MAX_BYTES = 512
PASSPHRASE_MIN_BYTES = 10
PASSPHRASE_MAX_BYTES = 79
PATH_FIELD_MAX_BYTES = 2_048
ROTATION_SECONDS_MAX = 31 * 24 * 60 * 60
_DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")


class ConfigError(ValueError):
    """Configuration is unsafe or cannot be interpreted."""


@dataclass(frozen=True)
class CaptureMode:
    format: str
    rate_hz: int
    channels: int


@dataclass(frozen=True)
class CaptureConfig:
    device_id: str | None
    mode: CaptureMode | None


@dataclass(frozen=True)
class StreamConfig:
    enabled: bool
    representation_id: str | None
    destination_host: str | None
    destination_port: int | None
    latency_ms: int
    stream_id: str | None
    passphrase: str | None = field(repr=False)
    opus_bitrate_bps: int = 128_000


@dataclass(frozen=True)
class RecordingConfig:
    directory: str
    rotation_seconds: int
    required_mountpoint: str | None


@dataclass(frozen=True)
class MonitoringConfig:
    spectrum_updates_per_second: float
    spectrum_bands: int


MIN_METER_UPDATES_PER_SECOND = 5.0
MAX_MONITORING_UPDATES_PER_SECOND = 60.0


@dataclass(frozen=True)
class MediaConfig:
    schema_version: int
    capture: CaptureConfig
    stream: StreamConfig
    recording: RecordingConfig
    monitoring: MonitoringConfig


@dataclass(frozen=True)
class DestinationStatus:
    safe: bool
    reason: str
    directory: str
    resolved_directory: str | None
    required_mountpoint: str | None
    resolved_mountpoint: str | None
    st_dev: int | None
    mount_st_dev: int | None
    total_bytes: int | None
    used_bytes: int | None
    available_bytes: int | None
    used_percent: float | None
    at_or_above_threshold: bool | None


def _reject_constant(_value: str) -> None:
    raise ConfigError("configuration contains a non-finite number")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError(f"configuration contains duplicate key {key!r}")
        result[key] = value
    return result


def parse_config(
    text: str,
    approved_representations: Iterable[str],
) -> MediaConfig:
    """Parse and validate configuration without accepting JSON extensions."""

    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except ConfigError:
        raise
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"configuration is not valid JSON at line {exc.lineno}, column {exc.colno}"
        ) from exc
    except ValueError as exc:
        raise ConfigError(
            "configuration contains an integer outside the JSON parser limit"
        ) from exc
    return validate_config(value, approved_representations)


def load_config(
    path: str | os.PathLike[str],
    approved_representations: Iterable[str],
) -> MediaConfig:
    """Load UTF-8 configuration and validate it before use."""

    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"cannot read configuration {path}: {exc}") from exc
    return parse_config(text, approved_representations)


def validate_config(
    value: Any,
    approved_representations: Iterable[str],
) -> MediaConfig:
    """Validate a decoded configuration against runtime capabilities."""

    approved = _approved_set(approved_representations)
    root = _object(
        value,
        "configuration",
        ("schema_version", "capture", "stream", "recording", "monitoring"),
    )
    schema_version = _integer(root["schema_version"], "schema_version")
    if schema_version != SCHEMA_VERSION:
        raise ConfigError(f"schema_version must be {SCHEMA_VERSION}")

    capture = _capture_config(root["capture"])
    stream = _stream_config(root["stream"], approved)
    recording = _recording_config(root["recording"])
    monitoring = _monitoring_config(root["monitoring"])
    return MediaConfig(schema_version, capture, stream, recording, monitoring)


def check_recording_destination(
    recording: RecordingConfig,
    *,
    isdir_fn: Callable[[str], bool] = os.path.isdir,
    access_fn: Callable[[str, int], bool] = os.access,
    stat_fn: Callable[[str], Any] = os.stat,
    disk_usage_fn: Callable[[str], Any] = shutil.disk_usage,
    realpath_fn: Callable[[str], str] = os.path.realpath,
    ismount_fn: Callable[[str], bool] = os.path.ismount,
) -> DestinationStatus:
    """Return a non-mutating safety assessment of the recording filesystem."""

    directory = recording.directory
    mountpoint = recording.required_mountpoint
    resolved_directory: str | None = None
    resolved_mountpoint: str | None = None
    st_dev: int | None = None
    mount_st_dev: int | None = None

    try:
        if not isdir_fn(directory):
            return _destination_status(
                False,
                "missing_or_not_directory",
                directory,
                mountpoint,
            )
        resolved_directory = realpath_fn(directory)
        st_dev = int(stat_fn(directory).st_dev)
    except (AttributeError, OSError, TypeError, ValueError):
        return _destination_status(
            False,
            "destination_stat_failed",
            directory,
            mountpoint,
            resolved_directory=resolved_directory,
        )

    if mountpoint is not None:
        try:
            resolved_mountpoint = realpath_fn(mountpoint)
            if not ismount_fn(mountpoint):
                return _destination_status(
                    False,
                    "required_mountpoint_unavailable",
                    directory,
                    mountpoint,
                    resolved_directory=resolved_directory,
                    resolved_mountpoint=resolved_mountpoint,
                    st_dev=st_dev,
                )
            if not _is_within(resolved_directory, resolved_mountpoint):
                return _destination_status(
                    False,
                    "destination_outside_required_mountpoint",
                    directory,
                    mountpoint,
                    resolved_directory=resolved_directory,
                    resolved_mountpoint=resolved_mountpoint,
                    st_dev=st_dev,
                )
            mount_st_dev = int(stat_fn(mountpoint).st_dev)
            if mount_st_dev != st_dev:
                return _destination_status(
                    False,
                    "destination_filesystem_mismatch",
                    directory,
                    mountpoint,
                    resolved_directory=resolved_directory,
                    resolved_mountpoint=resolved_mountpoint,
                    st_dev=st_dev,
                    mount_st_dev=mount_st_dev,
                )
        except (AttributeError, OSError, TypeError, ValueError):
            return _destination_status(
                False,
                "required_mountpoint_check_failed",
                directory,
                mountpoint,
                resolved_directory=resolved_directory,
                resolved_mountpoint=resolved_mountpoint,
                st_dev=st_dev,
            )

    try:
        writable = access_fn(directory, os.W_OK | os.X_OK)
    except (OSError, TypeError, ValueError):
        writable = False
    if not writable:
        return _destination_status(
            False,
            "not_writable",
            directory,
            mountpoint,
            resolved_directory=resolved_directory,
            resolved_mountpoint=resolved_mountpoint,
            st_dev=st_dev,
            mount_st_dev=mount_st_dev,
        )

    try:
        usage = disk_usage_fn(directory)
        total = _nonnegative_integer(usage.total, "destination total bytes")
        used = _nonnegative_integer(usage.used, "destination used bytes")
        available = _nonnegative_integer(usage.free, "destination available bytes")
        if total == 0 or used > total or available > total:
            raise ValueError("invalid destination usage")
    except (AttributeError, ConfigError, OSError, TypeError, ValueError):
        return _destination_status(
            False,
            "usage_unavailable",
            directory,
            mountpoint,
            resolved_directory=resolved_directory,
            resolved_mountpoint=resolved_mountpoint,
            st_dev=st_dev,
            mount_st_dev=mount_st_dev,
        )

    at_limit = used * 100 >= total * STORAGE_LIMIT_PERCENT
    return _destination_status(
        not at_limit,
        "storage_threshold" if at_limit else "ok",
        directory,
        mountpoint,
        resolved_directory=resolved_directory,
        resolved_mountpoint=resolved_mountpoint,
        st_dev=st_dev,
        mount_st_dev=mount_st_dev,
        total_bytes=total,
        used_bytes=used,
        available_bytes=available,
        used_percent=used * 100 / total,
        at_or_above_threshold=at_limit,
    )


def _approved_set(values: Iterable[str]) -> frozenset[str]:
    if isinstance(values, (str, bytes)):
        raise ConfigError("approved representations must be a collection of identifiers")
    try:
        items = tuple(values)
    except TypeError as exc:
        raise ConfigError("approved representations must be iterable") from exc
    if any(not isinstance(item, str) or not item for item in items):
        raise ConfigError("approved representations contain an invalid identifier")
    if len(set(items)) != len(items):
        raise ConfigError("approved representations contain duplicates")
    return frozenset(items)


def _capture_config(value: Any) -> CaptureConfig:
    obj = _object(value, "capture", ("device_id", "mode"))
    device_id = _nullable_bounded_string(
        obj["device_id"], "capture.device_id", DEVICE_ID_MAX_BYTES
    )
    mode_value = obj["mode"]
    if mode_value is None:
        mode = None
    else:
        mode_obj = _object(mode_value, "capture.mode", ("format", "rate_hz", "channels"))
        format_name = _string(mode_obj["format"], "capture.mode.format")
        rate_hz = _integer(mode_obj["rate_hz"], "capture.mode.rate_hz")
        channels = _integer(mode_obj["channels"], "capture.mode.channels")
        if format_name not in CAPTURE_FORMATS:
            raise ConfigError("capture.mode.format is unsupported")
        if rate_hz not in CAPTURE_RATES_HZ:
            raise ConfigError("capture.mode.rate_hz is unsupported")
        if channels not in CAPTURE_CHANNELS:
            raise ConfigError("capture.mode.channels is unsupported")
        mode = CaptureMode(format_name, rate_hz, channels)
    if (device_id is None) != (mode is None):
        raise ConfigError("capture.device_id and capture.mode must be set together")
    return CaptureConfig(device_id, mode)


def _stream_config(value: Any, approved: frozenset[str]) -> StreamConfig:
    obj = _object(
        value,
        "stream",
        (
            "enabled",
            "representation_id",
            "destination_host",
            "destination_port",
            "latency_ms",
            "stream_id",
            "passphrase",
            "opus_bitrate_bps",
        ),
    )
    enabled = _boolean(obj["enabled"], "stream.enabled")
    representation_id = _nullable_string(
        obj["representation_id"], "stream.representation_id"
    )
    destination_host = _destination_host(obj["destination_host"])
    destination_port = _nullable_integer(
        obj["destination_port"], "stream.destination_port"
    )
    latency_ms = _integer(obj["latency_ms"], "stream.latency_ms")
    stream_id = _nullable_bounded_string(
        obj["stream_id"],
        "stream.stream_id",
        STREAM_ID_MAX_BYTES,
        allow_empty=True,
    )
    passphrase = _nullable_bounded_string(
        obj["passphrase"], "stream.passphrase", PASSPHRASE_MAX_BYTES
    )
    bitrate = _integer(obj["opus_bitrate_bps"], "stream.opus_bitrate_bps")

    if representation_id is not None and representation_id not in approved:
        raise ConfigError("stream.representation_id is not an approved capability")
    if destination_port is not None and not 1 <= destination_port <= 65_535:
        raise ConfigError("stream.destination_port must be between 1 and 65535")
    if not 20 <= latency_ms <= 8_000:
        raise ConfigError("stream.latency_ms must be between 20 and 8000")
    if passphrase is not None and (
        len(passphrase) < PASSPHRASE_MIN_BYTES
        or any(not " " <= character <= "~" for character in passphrase)
    ):
        raise ConfigError(
            "stream.passphrase must contain between 10 and 79 printable ASCII characters"
        )
    if bitrate not in OPUS_BITRATES_BPS:
        raise ConfigError("stream.opus_bitrate_bps is not an allowed preset")
    if enabled and (
        representation_id is None
        or destination_host is None
        or destination_port is None
    ):
        raise ConfigError(
            "enabled stream requires representation_id, destination_host, and destination_port"
        )
    return StreamConfig(
        enabled,
        representation_id,
        destination_host,
        destination_port,
        latency_ms,
        stream_id,
        passphrase,
        bitrate,
    )


def _recording_config(value: Any) -> RecordingConfig:
    obj = _object(
        value,
        "recording",
        ("directory", "rotation_seconds", "required_mountpoint"),
    )
    directory = _bounded_string(
        obj["directory"], "recording.directory", PATH_FIELD_MAX_BYTES
    )
    rotation = _integer(obj["rotation_seconds"], "recording.rotation_seconds")
    mountpoint = _nullable_bounded_string(
        obj["required_mountpoint"],
        "recording.required_mountpoint",
        PATH_FIELD_MAX_BYTES,
    )
    if not 60 <= rotation <= ROTATION_SECONDS_MAX:
        raise ConfigError(
            "recording.rotation_seconds must be between 60 and "
            f"{ROTATION_SECONDS_MAX}"
        )
    return RecordingConfig(directory, rotation, mountpoint)


def _monitoring_config(value: Any) -> MonitoringConfig:
    obj = _object(
        value,
        "monitoring",
        ("spectrum_updates_per_second", "spectrum_bands"),
    )
    updates = _number(
        obj["spectrum_updates_per_second"],
        "monitoring.spectrum_updates_per_second",
    )
    bands = _integer(obj["spectrum_bands"], "monitoring.spectrum_bands")
    if not 0 <= updates <= MAX_MONITORING_UPDATES_PER_SECOND:
        raise ConfigError(
            "monitoring.spectrum_updates_per_second must be between 0 and 60"
        )
    if not 64 <= bands <= 2_048:
        raise ConfigError("monitoring.spectrum_bands must be between 64 and 2048")
    return MonitoringConfig(float(updates), bands)


def _object(value: Any, name: str, keys: Iterable[str]) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be an object")
    expected = set(keys)
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ConfigError(f"{name} has missing keys {missing} and extra keys {extra}")
    return value


def _boolean(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise ConfigError(f"{name} must be a boolean")
    return value


def _integer(value: Any, name: str) -> int:
    if type(value) is not int:
        raise ConfigError(f"{name} must be an integer")
    return value


def _nullable_integer(value: Any, name: str) -> int | None:
    if value is None:
        return None
    return _integer(value, name)


def _nonnegative_integer(value: Any, name: str) -> int:
    result = _integer(value, name)
    if result < 0:
        raise ValueError(f"{name} is negative")
    return result


def _number(value: Any, name: str) -> int | float:
    if type(value) not in (int, float):
        raise ConfigError(f"{name} must be a number")
    return value


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a non-empty string")
    return value


def _nullable_string(value: Any, name: str, *, allow_empty: bool = False) -> str | None:
    if value is None:
        return None
    if allow_empty and value == "":
        return value
    return _string(value, name)


def _bounded_string(
    value: Any, name: str, maximum_bytes: int, *, allow_empty: bool = False
) -> str:
    if allow_empty and value == "":
        return value
    result = _string(value, name)
    try:
        encoded = result.encode("utf-8")
    except UnicodeError:
        raise ConfigError(f"{name} must contain valid Unicode") from None
    if len(encoded) > maximum_bytes:
        raise ConfigError(f"{name} exceeds {maximum_bytes} encoded bytes")
    if any(unicodedata.category(character) == "Cc" for character in result):
        raise ConfigError(f"{name} must not contain control characters")
    return result


def _nullable_bounded_string(
    value: Any, name: str, maximum_bytes: int, *, allow_empty: bool = False
) -> str | None:
    if value is None:
        return None
    return _bounded_string(value, name, maximum_bytes, allow_empty=allow_empty)


def _destination_host(value: Any) -> str | None:
    if value is None:
        return None
    host = _bounded_string(
        value, "stream.destination_host", DESTINATION_HOST_MAX_BYTES
    )
    try:
        host.encode("ascii")
    except UnicodeError:
        raise ConfigError(
            "stream.destination_host must be an ASCII DNS name or IP literal"
        ) from None

    bracketed = host.startswith("[") or host.endswith("]")
    if bracketed:
        if not (host.startswith("[") and host.endswith("]")):
            raise ConfigError("stream.destination_host has mismatched IPv6 brackets")
        literal = host[1:-1]
        if "%" in literal:
            raise ConfigError("stream.destination_host must not contain an IPv6 zone")
        try:
            ipaddress.IPv6Address(literal)
        except ipaddress.AddressValueError:
            raise ConfigError(
                "stream.destination_host is not a valid IPv6 literal"
            ) from None
        return host

    if ":" in host:
        if "%" in host:
            raise ConfigError("stream.destination_host must not contain an IPv6 zone")
        try:
            ipaddress.IPv6Address(host)
        except ipaddress.AddressValueError:
            raise ConfigError(
                "stream.destination_host is not a valid IPv6 literal"
            ) from None
        return host

    try:
        ipaddress.IPv4Address(host)
    except ipaddress.AddressValueError:
        pass
    else:
        return host
    if "." in host and not host.endswith(".") and re.fullmatch(r"[0-9.]+", host):
        raise ConfigError("stream.destination_host is not a valid IPv4 literal")

    dns_name = host[:-1] if host.endswith(".") else host
    labels = dns_name.split(".")
    if not dns_name or any(_DNS_LABEL.fullmatch(label) is None for label in labels):
        raise ConfigError("stream.destination_host is not a valid DNS hostname")
    return host


def _is_within(path: str, parent: str) -> bool:
    try:
        common = os.path.commonpath((path, parent))
    except ValueError:
        return False
    return os.path.normcase(os.path.normpath(common)) == os.path.normcase(
        os.path.normpath(parent)
    )


def _destination_status(
    safe: bool,
    reason: str,
    directory: str,
    required_mountpoint: str | None,
    *,
    resolved_directory: str | None = None,
    resolved_mountpoint: str | None = None,
    st_dev: int | None = None,
    mount_st_dev: int | None = None,
    total_bytes: int | None = None,
    used_bytes: int | None = None,
    available_bytes: int | None = None,
    used_percent: float | None = None,
    at_or_above_threshold: bool | None = None,
) -> DestinationStatus:
    return DestinationStatus(
        safe,
        reason,
        directory,
        resolved_directory,
        required_mountpoint,
        resolved_mountpoint,
        st_dev,
        mount_st_dev,
        total_bytes,
        used_bytes,
        available_bytes,
        used_percent,
        at_or_above_threshold,
    )
