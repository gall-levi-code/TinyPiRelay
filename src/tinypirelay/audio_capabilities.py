"""Validate exact, non-Cartesian capture and stream capability combinations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .media_config import (
    CAPTURE_CHANNELS,
    CAPTURE_FORMATS,
    CAPTURE_RATES_HZ,
    MediaConfig,
)


SCHEMA_VERSION = 1
DEFAULT_AUDIO_CAPABILITIES_PATH = (
    Path(__file__).resolve().parents[2] / "evidence" / "audio_capabilities.json"
)
EVIDENCE_STATUSES = frozenset(
    ("Hardware tested", "CI validated", "Likely compatible / untested", "Unsupported")
)
SELECTABLE_STATUSES = frozenset(("Hardware tested", "CI validated"))


class AudioCapabilityError(ValueError):
    """The capability model is malformed or does not permit a configuration."""


@dataclass(frozen=True)
class StreamOption:
    representation_id: str
    conversion: str
    evidence_status: str

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.representation_id,
            "conversion": self.conversion,
            "evidence_status": self.evidence_status,
        }


@dataclass(frozen=True)
class CaptureModeCapability:
    format: str
    rate_hz: int
    channels: int
    native: bool
    evidence_status: str
    stream_options: tuple[StreamOption, ...]

    def key(self) -> tuple[str, int, int]:
        return (self.format, self.rate_hz, self.channels)

    def to_dict(self) -> dict[str, object]:
        return {
            "format": self.format,
            "rate_hz": self.rate_hz,
            "channels": self.channels,
            "native": self.native,
            "evidence_status": self.evidence_status,
            "stream_options": [option.to_dict() for option in self.stream_options],
        }


@dataclass(frozen=True)
class CaptureDeviceCapability:
    device_id: str
    label: str
    evidence_status: str
    modes: tuple[CaptureModeCapability, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.device_id,
            "label": self.label,
            "evidence_status": self.evidence_status,
            "modes": [mode.to_dict() for mode in self.modes],
        }


@dataclass(frozen=True)
class StreamRepresentation:
    representation_id: str
    label: str
    codec: str
    container: str
    lossless: bool
    bitrate_bps: int | None
    evidence_status: str

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.representation_id,
            "label": self.label,
            "codec": self.codec,
            "container": self.container,
            "lossless": self.lossless,
            "bitrate_bps": self.bitrate_bps,
            "evidence_status": self.evidence_status,
        }


@dataclass(frozen=True)
class AudioCapabilities:
    devices: tuple[CaptureDeviceCapability, ...]
    representations: tuple[StreamRepresentation, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "capture_devices": [device.to_dict() for device in self.devices],
            "stream_representations": [item.to_dict() for item in self.representations],
        }

    def to_public_dict(self) -> dict[str, object]:
        """Expose only fully verified device/mode/option/representation chains."""

        representations = {
            item.representation_id: item
            for item in self.representations
            if item.evidence_status in SELECTABLE_STATUSES
        }
        devices: list[dict[str, object]] = []
        referenced: set[str] = set()
        for device in self.devices:
            if device.evidence_status not in SELECTABLE_STATUSES:
                continue
            modes: list[dict[str, object]] = []
            for mode in device.modes:
                if (
                    mode.evidence_status not in SELECTABLE_STATUSES
                    or mode.format not in CAPTURE_FORMATS
                    or mode.rate_hz not in CAPTURE_RATES_HZ
                    or mode.channels not in CAPTURE_CHANNELS
                ):
                    continue
                options = [
                    option
                    for option in mode.stream_options
                    if option.evidence_status in SELECTABLE_STATUSES
                    and option.representation_id in representations
                ]
                referenced.update(option.representation_id for option in options)
                value = mode.to_dict()
                value["stream_options"] = [option.to_dict() for option in options]
                modes.append(value)
            if modes:
                value = device.to_dict()
                value["modes"] = modes
                devices.append(value)
        return {
            "schema_version": SCHEMA_VERSION,
            "capture_devices": devices,
            "stream_representations": [
                item.to_dict()
                for representation_id, item in representations.items()
                if representation_id in referenced
            ],
        }

    def find_mode(
        self, device_id: str, format_name: str, rate_hz: int, channels: int
    ) -> CaptureModeCapability | None:
        for device in self.devices:
            if device.device_id != device_id:
                continue
            wanted = (format_name, rate_hz, channels)
            return next((mode for mode in device.modes if mode.key() == wanted), None)
        return None


def _reject_constant(value: str) -> None:
    raise AudioCapabilityError(f"non-finite JSON number is not allowed: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AudioCapabilityError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_audio_capabilities(
    path: str | Path = DEFAULT_AUDIO_CAPABILITIES_PATH,
    approved_representations: Iterable[str] = (),
) -> AudioCapabilities:
    try:
        value = json.loads(
            Path(path).read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except AudioCapabilityError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise AudioCapabilityError(f"cannot read audio capabilities {path}: {exc}") from exc
    return validate_audio_capabilities(value, approved_representations)


def validate_audio_capabilities(
    value: Any, approved_representations: Iterable[str]
) -> AudioCapabilities:
    approved = _approved_set(approved_representations)
    root = _object(value, "capabilities", ("schema_version", "devices", "representations"))
    if _integer(root["schema_version"], "schema_version") != SCHEMA_VERSION:
        raise AudioCapabilityError(f"schema_version must be {SCHEMA_VERSION}")

    representations_value = _list(root["representations"], "representations")
    representations = tuple(
        _representation(item, f"representations[{index}]")
        for index, item in enumerate(representations_value)
    )
    representation_ids = tuple(item.representation_id for item in representations)
    if len(set(representation_ids)) != len(representation_ids):
        raise AudioCapabilityError("representations contain duplicate IDs")
    if set(representation_ids) != approved:
        raise AudioCapabilityError(
            "representation IDs must exactly match evidence-gated capabilities"
        )

    devices_value = _list(root["devices"], "devices")
    devices = tuple(
        _device(item, f"devices[{index}]", approved)
        for index, item in enumerate(devices_value)
    )
    device_ids = tuple(item.device_id for item in devices)
    if len(set(device_ids)) != len(device_ids):
        raise AudioCapabilityError("devices contain duplicate IDs")
    if not devices:
        raise AudioCapabilityError("devices must contain at least one exact device")
    return AudioCapabilities(devices, representations)


def validate_config_combination(
    config: MediaConfig, capabilities: AudioCapabilities
) -> CaptureModeCapability | None:
    capture = config.capture
    if capture.device_id is None and capture.mode is None:
        if config.stream.enabled or config.stream.representation_id is not None:
            raise AudioCapabilityError("select a capture device and exact mode before configuring streaming")
        return None
    if capture.device_id is None or capture.mode is None:
        raise AudioCapabilityError("capture device and exact mode must be selected")
    device = next(
        (item for item in capabilities.devices if item.device_id == capture.device_id),
        None,
    )
    if device is None or device.evidence_status not in SELECTABLE_STATUSES:
        raise AudioCapabilityError("capture device is not validated")
    mode = capabilities.find_mode(
        capture.device_id,
        capture.mode.format,
        capture.mode.rate_hz,
        capture.mode.channels,
    )
    if mode is None or mode.evidence_status not in SELECTABLE_STATUSES:
        raise AudioCapabilityError("capture device/mode combination is not validated")

    representation_id = config.stream.representation_id
    if representation_id is not None:
        representation = next(
            (
                item
                for item in capabilities.representations
                if item.representation_id == representation_id
            ),
            None,
        )
        if representation is None or representation.evidence_status not in SELECTABLE_STATUSES:
            raise AudioCapabilityError("stream representation is not validated")
        allowed = {
            option.representation_id
            for option in mode.stream_options
            if option.evidence_status in SELECTABLE_STATUSES
        }
        if representation_id not in allowed:
            raise AudioCapabilityError(
                "stream representation is not validated for the selected capture mode"
            )
    return mode


def _device(
    value: Any, name: str, approved: frozenset[str]
) -> CaptureDeviceCapability:
    obj = _object(value, name, ("id", "label", "evidence_status", "modes"))
    modes_value = _list(obj["modes"], f"{name}.modes")
    modes = tuple(
        _mode(item, f"{name}.modes[{index}]", approved)
        for index, item in enumerate(modes_value)
    )
    if not modes:
        raise AudioCapabilityError(f"{name}.modes must not be empty")
    keys = tuple(mode.key() for mode in modes)
    if len(set(keys)) != len(keys):
        raise AudioCapabilityError(f"{name}.modes contain duplicate combinations")
    return CaptureDeviceCapability(
        _string(obj["id"], f"{name}.id"),
        _string(obj["label"], f"{name}.label"),
        _status(obj["evidence_status"], f"{name}.evidence_status"),
        modes,
    )


def _mode(value: Any, name: str, approved: frozenset[str]) -> CaptureModeCapability:
    obj = _object(
        value,
        name,
        ("format", "rate_hz", "channels", "native", "evidence_status", "stream_options"),
    )
    options_value = _list(obj["stream_options"], f"{name}.stream_options")
    options = tuple(
        _stream_option(item, f"{name}.stream_options[{index}]", approved)
        for index, item in enumerate(options_value)
    )
    ids = tuple(option.representation_id for option in options)
    if len(set(ids)) != len(ids):
        raise AudioCapabilityError(f"{name}.stream_options contain duplicate IDs")
    native = obj["native"]
    if type(native) is not bool:
        raise AudioCapabilityError(f"{name}.native must be a boolean")
    return CaptureModeCapability(
        _string(obj["format"], f"{name}.format"),
        _positive_integer(obj["rate_hz"], f"{name}.rate_hz"),
        _positive_integer(obj["channels"], f"{name}.channels"),
        native,
        _status(obj["evidence_status"], f"{name}.evidence_status"),
        options,
    )


def _stream_option(
    value: Any, name: str, approved: frozenset[str]
) -> StreamOption:
    obj = _object(value, name, ("representation_id", "conversion", "evidence_status"))
    representation_id = _string(
        obj["representation_id"], f"{name}.representation_id"
    )
    if representation_id not in approved:
        raise AudioCapabilityError(f"{name}.representation_id is not evidence-gated")
    return StreamOption(
        representation_id,
        _string(obj["conversion"], f"{name}.conversion", allow_empty=True),
        _status(obj["evidence_status"], f"{name}.evidence_status"),
    )


def _representation(value: Any, name: str) -> StreamRepresentation:
    obj = _object(
        value,
        name,
        ("id", "label", "codec", "container", "lossless", "bitrate_bps", "evidence_status"),
    )
    lossless = obj["lossless"]
    if type(lossless) is not bool:
        raise AudioCapabilityError(f"{name}.lossless must be a boolean")
    bitrate = obj["bitrate_bps"]
    if bitrate is not None:
        bitrate = _positive_integer(bitrate, f"{name}.bitrate_bps")
    return StreamRepresentation(
        _string(obj["id"], f"{name}.id"),
        _string(obj["label"], f"{name}.label"),
        _string(obj["codec"], f"{name}.codec"),
        _string(obj["container"], f"{name}.container"),
        lossless,
        bitrate,
        _status(obj["evidence_status"], f"{name}.evidence_status"),
    )


def _approved_set(values: Iterable[str]) -> frozenset[str]:
    if isinstance(values, (str, bytes)):
        raise AudioCapabilityError("approved representations must be a collection")
    try:
        items = tuple(values)
    except TypeError as exc:
        raise AudioCapabilityError("approved representations must be iterable") from exc
    if any(not isinstance(item, str) or not item for item in items):
        raise AudioCapabilityError("approved representations contain an invalid ID")
    if len(set(items)) != len(items):
        raise AudioCapabilityError("approved representations contain duplicates")
    return frozenset(items)


def _object(value: Any, name: str, keys: Iterable[str]) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise AudioCapabilityError(f"{name} must be an object")
    expected = set(keys)
    actual = set(value)
    if actual != expected:
        raise AudioCapabilityError(
            f"{name} has missing keys {sorted(expected - actual)} "
            f"and extra keys {sorted(actual - expected)}"
        )
    return value


def _list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise AudioCapabilityError(f"{name} must be a list")
    return value


def _string(value: Any, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise AudioCapabilityError(f"{name} must be a string")
    if len(value) > 4096:
        raise AudioCapabilityError(f"{name} is too long")
    return value


def _integer(value: Any, name: str) -> int:
    if type(value) is not int:
        raise AudioCapabilityError(f"{name} must be an integer")
    return value


def _positive_integer(value: Any, name: str) -> int:
    result = _integer(value, name)
    if result <= 0:
        raise AudioCapabilityError(f"{name} must be positive")
    return result


def _status(value: Any, name: str) -> str:
    if value not in EVIDENCE_STATUSES:
        raise AudioCapabilityError(f"{name} must use an allowed compatibility label")
    return value
