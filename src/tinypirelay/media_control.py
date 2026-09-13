"""Stable Step 3 control operations owned by the media process."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .audio_capabilities import (
    AudioCapabilities,
    AudioCapabilityError,
    validate_config_combination,
)
from .media_config import (
    MIN_METER_UPDATES_PER_SECOND,
    ConfigError,
    MediaConfig,
    validate_config,
)
from .media_runtime import SPECTRUM_LEASE_SECONDS
from .recording_library import RecordingLibrary, RecordingLibraryError
from .probe import parse_arecord_devices


MAX_DIAGNOSTIC_EVENTS = 32
READ_ONLY_OPERATIONS = frozenset(
    (
        "status",
        "capabilities",
        "config.get",
        "config.validate",
        "storage.list",
        "storage.info",
        "storage.read",
        # Ephemeral browser-demand traffic must not evict meaningful events.
        "monitoring.spectrum_lease",
        "monitoring.spectrum_release",
        "monitoring.telemetry",
    )
)


class MediaControlError(ValueError):
    """A stable, public control failure that contains no secret material."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


class ConfigCommitUncertain(OSError):
    """The replacement happened, but directory durability was not confirmed."""

    def __init__(self, revision: str) -> None:
        super().__init__("configuration directory could not be synchronized")
        self.revision = revision


class MediaControlHandler:
    """Map allowlisted public operations onto one serialized MediaRuntime."""

    def __init__(
        self,
        runtime: object,
        active_config: MediaConfig,
        config_path: str | os.PathLike[str],
        revision: str,
        approved_representations: Iterable[str],
        audio_capabilities: AudioCapabilities,
        *,
        clock: Callable[[], float] = time.time,
        device_telemetry: Callable[[], dict[str, object] | None] | None = None,
        audio_discovery: Callable[[], dict[str, object]] | None = None,
    ) -> None:
        self._runtime = runtime
        self._active_config = active_config
        self._saved_config = active_config
        self._config_path = Path(config_path)
        self._active_revision = revision
        self._revision = revision
        self._approved = tuple(approved_representations)
        self._capabilities = audio_capabilities
        self._clock = clock
        self._device_telemetry = device_telemetry
        self._audio_discovery = audio_discovery or discover_capture_devices
        self._events: deque[dict[str, object]] = deque(maxlen=MAX_DIAGNOSTIC_EVENTS)
        self._lock = threading.Lock()
        self._operation_lock = threading.Lock()
        self._last_state_signature: str | None = None
        self._recording_library = RecordingLibrary(
            lambda: self._active_config.recording, self._runtime.snapshot
        )
        self._record("control_ready", "Local control interface is ready.")

    def __call__(self, operation: str, arguments: Mapping[str, Any]) -> dict[str, object]:
        if not isinstance(operation, str) or not operation:
            raise MediaControlError("invalid_operation", "operation must be a string")
        if not isinstance(arguments, Mapping):
            raise MediaControlError("invalid_arguments", "arguments must be an object")
        try:
            if operation in {"storage.list", "storage.info", "storage.read"}:
                result = self._dispatch(operation, dict(arguments))
            else:
                with self._operation_lock:
                    result = self._dispatch(operation, dict(arguments))
        except RecordingLibraryError as exc:
            self._record("control_rejected", f"{operation}: {exc.code}")
            raise MediaControlError(exc.code, exc.message) from exc
        except MediaControlError as exc:
            self._record("control_rejected", f"{operation}: {exc.code}")
            raise
        except (AudioCapabilityError, ConfigError) as exc:
            self._record("control_rejected", f"{operation}: invalid_configuration")
            raise MediaControlError("invalid_configuration", str(exc)) from exc
        except (OSError, RuntimeError, ValueError) as exc:
            self._record("control_failed", f"{operation}: operation_failed")
            raise MediaControlError(
                "operation_failed", _safe_exception_message(exc)
            ) from exc
        if operation not in READ_ONLY_OPERATIONS:
            self._record("control_succeeded", operation)
        return result

    def _dispatch(self, operation: str, arguments: dict[str, Any]) -> dict[str, object]:
        if operation == "storage.list":
            _exact_keys(arguments, ("page",))
            return self._recording_library.list(arguments["page"])
        if operation in {"storage.info", "storage.delete", "storage.check", "storage.recover"}:
            _exact_keys(arguments, ("file_id",))
            method = getattr(self._recording_library, operation.split(".")[1])
            return method(arguments["file_id"])
        if operation == "storage.read":
            _exact_keys(arguments, ("file_id", "offset", "length"))
            return self._recording_library.read(**arguments)
        if operation == "status":
            _exact_keys(arguments, ())
            snapshot = _public_snapshot(self._runtime.snapshot())
            snapshot["media_available"] = True
            snapshot["audio_setup_required"] = self._active_config.capture.mode is None
            snapshot["active_config_revision"] = self._active_revision
            snapshot["saved_config_revision"] = self._revision
            snapshot["restart_required"] = self._saved_config != self._active_config
            snapshot["monitoring_updates_per_second"] = max(
                MIN_METER_UPDATES_PER_SECOND,
                self._active_config.monitoring.spectrum_updates_per_second,
            )
            if self._device_telemetry is not None:
                snapshot["device_telemetry"] = _public_snapshot(self._device_telemetry())
            self._observe_snapshot(snapshot)
            events = self._event_snapshot()
            snapshot["diagnostics"] = {"events": events}
            snapshot["logs"] = events
            return snapshot
        if operation == "capabilities":
            _exact_keys(arguments, ())
            result = self._capabilities.to_public_dict()
            discovery = self._audio_discovery()
            result["discovery"] = discovery
            present = {item["id"] for item in discovery["devices"]}
            for device in result["capture_devices"]:
                # plughw and hw name the same physical device here; presence
                # does not imply that any unlisted mode has been validated.
                device["present"] = (
                    str(device["id"]).removeprefix("plug") in present
                    if discovery["status"] == "available" else None
                )
            return result
        if operation == "monitoring.spectrum_lease":
            _exact_keys(arguments, ())
            return {
                "active": self._runtime.renew_spectrum_lease() is True,
                "lease_seconds": SPECTRUM_LEASE_SECONDS,
            }
        if operation == "monitoring.spectrum_release":
            _exact_keys(arguments, ())
            self._runtime.release_spectrum_lease()
            return {"active": False}
        if operation == "monitoring.telemetry":
            _exact_keys(arguments, ())
            return _public_snapshot(self._runtime.telemetry_snapshot())
        if operation == "config.get":
            _exact_keys(arguments, ())
            return self._config_result(self._saved_config)
        if operation == "config.validate":
            _exact_keys(arguments, ("config",))
            config = self._validate_public_config(arguments["config"])
            return {
                "valid": True,
                "config": public_config(config),
                "restart_required": _requires_restart(
                    self._active_config, config
                ),
            }
        if operation == "config.update":
            _exact_keys(
                arguments,
                ("config", "confirm_restart", "expected_revision"),
            )
            if type(arguments["confirm_restart"]) is not bool:
                raise MediaControlError(
                    "invalid_arguments", "confirm_restart must be a boolean"
                )
            expected_revision = arguments["expected_revision"]
            if not isinstance(expected_revision, str) or not expected_revision:
                raise MediaControlError(
                    "invalid_arguments", "expected_revision must be a string"
                )
            if expected_revision != self._revision:
                raise MediaControlError(
                    "revision_conflict",
                    "configuration changed; reload it before saving",
                )
            config = self._validate_public_config(arguments["config"])
            restart_required = _requires_restart(self._active_config, config)
            if restart_required and not arguments["confirm_restart"]:
                raise MediaControlError(
                    "confirmation_required",
                    "saving this configuration requires a media-service restart",
                )
            if (
                config.recording != self._active_config.recording
                and _recording_is_active(self._runtime.snapshot())
            ):
                raise MediaControlError(
                    "recording_active",
                    "stop recording before changing recording settings",
                )
            if config == self._saved_config:
                return {
                    "saved": False,
                    "restart_required": self._saved_config != self._active_config,
                    "new_revision": self._revision,
                }
            try:
                revision = write_config_atomic(self._config_path, config)
            except ConfigCommitUncertain as exc:
                self._saved_config = config
                self._revision = exc.revision
                raise MediaControlError(
                    "config_durability_unconfirmed",
                    "configuration was replaced but durability could not be confirmed; restart is required",
                ) from exc
            self._saved_config = config
            self._revision = revision
            if not restart_required:
                try:
                    self._runtime.apply_live_config(config, revision)
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    raise MediaControlError(
                        "live_apply_failed",
                        "configuration was saved but could not be applied live; restart is required",
                    ) from exc
                self._active_config = config
                self._active_revision = revision
            return {
                "saved": True,
                "restart_required": self._saved_config != self._active_config,
                "new_revision": self._revision,
            }
        if operation in {
            "capture.start",
            "stream.start",
            "stream.set_opus_bitrate",
            "recording.start",
        } and self._saved_config != self._active_config:
            raise MediaControlError(
                "reconfigure_required",
                "restart the media service before starting media with the saved configuration",
            )
        if operation == "capture.start":
            _exact_keys(arguments, ())
            return _decision_result(self._runtime.start_capture())
        if operation == "capture.stop":
            _exact_keys(arguments, ())
            return _decision_result(self._runtime.stop_capture())
        if operation == "stream.start":
            _exact_keys(arguments, ())
            return _decision_result(self._runtime.start_stream())
        if operation == "stream.stop":
            _exact_keys(arguments, ())
            return _decision_result(self._runtime.stop_stream())
        if operation == "stream.set_opus_bitrate":
            _exact_keys(arguments, ("bitrate_bps",))
            bitrate = arguments["bitrate_bps"]
            if type(bitrate) is not int:
                raise MediaControlError(
                    "invalid_arguments", "bitrate_bps must be an integer"
                )
            return _decision_result(self._runtime.set_opus_bitrate(bitrate))
        if operation == "recording.start":
            _exact_keys(arguments, ())
            return _decision_result(self._runtime.start_recording())
        if operation == "recording.stop":
            _exact_keys(arguments, ())
            return _decision_result(self._runtime.stop_recording())
        if operation == "service.restart_request":
            _exact_keys(arguments, ())
            return {
                "accepted": False,
                "restart_required": self._saved_config != self._active_config,
                "message": "Service restart is delegated to the Step 4 service manager.",
            }
        raise MediaControlError("unknown_operation", "operation is not supported")

    def _validate_public_config(self, value: Any) -> MediaConfig:
        raw = internal_config_value(value, self._saved_config.stream.passphrase)
        config = validate_config(raw, self._approved)
        validate_config_combination(config, self._capabilities)
        return config

    def _config_result(self, config: MediaConfig) -> dict[str, object]:
        return {
            "config": public_config(config),
            "revision": self._revision,
            "restart_required": config != self._active_config,
        }

    def _record(self, code: str, message: str) -> None:
        event = {
            "timestamp_unix": float(self._clock()),
            "code": code,
            "message": message[:512],
        }
        with self._lock:
            self._events.append(event)

    def _event_snapshot(self) -> list[dict[str, object]]:
        with self._lock:
            return [dict(item) for item in self._events]

    def _observe_snapshot(self, snapshot: Mapping[str, object]) -> None:
        state = snapshot.get("state")
        if not isinstance(state, Mapping):
            return
        observed: dict[str, object] = {}
        for name in ("service", "capture", "stream", "connection", "recording"):
            value = state.get(name)
            if isinstance(value, Mapping):
                observed[name] = {
                    "state": value.get("state"),
                    "last_error": value.get("last_error"),
                    "warning": value.get("warning"),
                }
            else:
                observed[name] = value
        signature = json.dumps(observed, sort_keys=True, separators=(",", ":"))
        with self._lock:
            changed = signature != self._last_state_signature
            self._last_state_signature = signature
        if changed:
            summary = ", ".join(
                f"{name}={_state_name(state.get(name))}"
                for name in ("capture", "stream", "connection", "recording")
            )
            self._record("media_state_changed", summary)


def discover_capture_devices() -> dict[str, object]:
    """List hardware without opening it or promoting unverified audio modes."""

    try:
        result = subprocess.run(
            ["arecord", "-l"], stdin=subprocess.DEVNULL, capture_output=True,
            check=False, encoding="utf-8", errors="replace", timeout=2,
            env={**os.environ, "LANG": "C", "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"status": "unavailable", "devices": []}
    output = result.stdout + "\n" + result.stderr
    devices = parse_arecord_devices(output)
    if not devices and result.returncode != 0 and "no soundcards found" not in output.lower():
        return {"status": "unavailable", "devices": []}
    return {
        "status": "available",
        "devices": [
            {
                "id": f"hw:CARD={item['card_id']},DEV={item['device']}",
                "label": str(item["card_name"]),
            }
            for item in devices[:64]
        ],
    }


def public_config(config: MediaConfig) -> dict[str, object]:
    """Return the editable DTO without returning the SRT passphrase."""

    capture_mode: dict[str, object] | None = None
    if config.capture.mode is not None:
        capture_mode = {
            "format": config.capture.mode.format,
            "rate_hz": config.capture.mode.rate_hz,
            "channels": config.capture.mode.channels,
        }
    stream = config.stream
    return {
        "schema_version": config.schema_version,
        "capture": {"device_id": config.capture.device_id, "mode": capture_mode},
        "stream": {
            "enabled": stream.enabled,
            "representation_id": stream.representation_id,
            "destination_host": stream.destination_host,
            "destination_port": stream.destination_port,
            "latency_ms": stream.latency_ms,
            "stream_id": stream.stream_id,
            "opus_bitrate_bps": stream.opus_bitrate_bps,
            "passphrase": {
                "configured": stream.passphrase is not None,
                "action": "keep",
            },
        },
        "recording": {
            "directory": config.recording.directory,
            "rotation_seconds": config.recording.rotation_seconds,
            "required_mountpoint": config.recording.required_mountpoint,
        },
        "monitoring": {
            "spectrum_updates_per_second": config.monitoring.spectrum_updates_per_second,
            "spectrum_bands": config.monitoring.spectrum_bands,
        },
    }


def internal_config_value(value: Any, current_passphrase: str | None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MediaControlError("invalid_configuration", "config must be an object")
    _exact_keys(value, ("schema_version", "capture", "stream", "recording", "monitoring"))
    result = json.loads(json.dumps(value))
    stream = result.get("stream")
    if not isinstance(stream, dict):
        raise MediaControlError("invalid_configuration", "config.stream must be an object")
    passphrase = stream.get("passphrase")
    if not isinstance(passphrase, dict):
        raise MediaControlError(
            "invalid_configuration", "config.stream.passphrase must be an object"
        )
    action = passphrase.get("action")
    configured = passphrase.get("configured")
    if configured is not None and type(configured) is not bool:
        raise MediaControlError(
            "invalid_configuration", "passphrase.configured must be a boolean"
        )
    base_keys = {"action"} | ({"configured"} if "configured" in passphrase else set())
    if action == "keep":
        _exact_keys(passphrase, base_keys)
        secret = current_passphrase
    elif action == "clear":
        _exact_keys(passphrase, base_keys)
        secret = None
    elif action == "replace":
        _exact_keys(passphrase, base_keys | {"value"})
        secret = passphrase["value"]
    else:
        raise MediaControlError(
            "invalid_configuration", "passphrase.action must be keep, clear, or replace"
        )
    stream["passphrase"] = secret
    return result


def write_config_atomic(path: str | os.PathLike[str], config: MediaConfig) -> str:
    """Durably replace one config file with restrictive permissions."""

    target = Path(path)
    if target.exists() and target.is_symlink():
        raise OSError("refusing to replace a symbolic-link configuration")
    parent = target.parent
    parent.mkdir(parents=False, exist_ok=True)
    payload = (json.dumps(_full_config(config), indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    revision = hashlib.sha256(payload).hexdigest()
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=parent
    )
    temporary = Path(temporary_name)
    replaced = False
    try:
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        replaced = True
        if os.name == "posix":
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except BaseException as exc:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        if replaced:
            raise ConfigCommitUncertain(revision) from exc
        raise
    return revision


def _full_config(config: MediaConfig) -> dict[str, object]:
    value = public_config(config)
    stream = value["stream"]
    assert isinstance(stream, dict)
    stream["passphrase"] = config.stream.passphrase
    return value


def _requires_restart(active: MediaConfig, candidate: MediaConfig) -> bool:
    active_stream = active.stream
    candidate_stream = candidate.stream
    return (
        active.schema_version != candidate.schema_version
        or active.capture != candidate.capture
        or active.monitoring != candidate.monitoring
        or active_stream.enabled != candidate_stream.enabled
        or active_stream.representation_id != candidate_stream.representation_id
        or active_stream.destination_host != candidate_stream.destination_host
        or active_stream.destination_port != candidate_stream.destination_port
        or active_stream.latency_ms != candidate_stream.latency_ms
        or active_stream.stream_id != candidate_stream.stream_id
        or active_stream.passphrase != candidate_stream.passphrase
    )


def _recording_is_active(snapshot: object) -> bool:
    if not isinstance(snapshot, Mapping):
        return False
    state = snapshot.get("state")
    if not isinstance(state, Mapping):
        return False
    recording = state.get("recording")
    if not isinstance(recording, Mapping):
        return False
    return recording.get("state") in {"starting", "running", "stopping"}


def _state_name(value: object) -> str:
    if isinstance(value, Mapping):
        state = value.get("state")
        return state if isinstance(state, str) else "unknown"
    return value if isinstance(value, str) else "unknown"


def _decision_result(decision: object) -> dict[str, object]:
    to_dict = getattr(decision, "to_dict", None)
    if not callable(to_dict):
        raise MediaControlError("media_error", "media command returned an invalid result")
    value = to_dict()
    if not isinstance(value, dict):
        raise MediaControlError("media_error", "media command returned an invalid result")
    return _public_snapshot(value)


def _public_snapshot(value: Any) -> Any:
    """Copy only JSON values while redacting secret-like object keys."""

    if value is None or type(value) in (bool, int, float, str):
        return value
    if isinstance(value, list) or isinstance(value, tuple):
        return [_public_snapshot(item) for item in value]
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            lowered = name.lower()
            if any(part in lowered for part in ("passphrase", "password", "secret", "token", "hash")):
                result[name] = "[redacted]"
            else:
                result[name] = _public_snapshot(item)
        return result
    raise MediaControlError("media_error", "media returned a non-JSON value")


def _exact_keys(value: Mapping[str, Any], keys: Iterable[str]) -> None:
    expected = set(keys)
    actual = set(value)
    if actual != expected:
        raise MediaControlError(
            "invalid_arguments",
            f"arguments have missing keys {sorted(expected - actual)} "
            f"and extra keys {sorted(actual - expected)}",
        )


def _safe_exception_message(exc: BaseException) -> str:
    if isinstance(exc, PermissionError):
        return "permission denied while updating media configuration"
    if isinstance(exc, OSError):
        return "media configuration could not be updated"
    return "media control operation failed"
