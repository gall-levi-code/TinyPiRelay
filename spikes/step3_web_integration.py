"""Retained Step 3 web/control-boundary integration harness.

The default media participant is an explicitly labelled state simulator.  It
does not create audio or network media.  Pass ``--control-socket`` to run the
same web-failure checks against an already-running media service instead.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import http.client
import http.cookies
import json
import math
import os
import secrets
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from tinypirelay.audio_capabilities import (
    validate_audio_capabilities,
    validate_config_combination,
)
from tinypirelay.control_protocol import (
    ControlClient,
    ControlError,
    ControlOperationError,
    ControlRequestError,
    ControlUnavailable,
    UnixControlServer,
)
from tinypirelay.media_config import MediaConfig, load_config
from tinypirelay.media_control import MediaControlError, MediaControlHandler
from tinypirelay.media_runtime import SPECTRUM_LEASE_SECONDS
from tinypirelay.web_security import CredentialRecord
from tinypirelay.web_service import write_credential_atomic


SCHEMA_VERSION = 1
SPIKE_NAME = "step3_web_control_boundary"
REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_USERNAME = "integration-operator"
REPRESENTATION_ID = "opus_128k_48000_stereo_matroska"
SIM_DEVICE_ID = "sim:noncartesian"
SSE_CLIENT_LIMIT = 2
SLOW_CLIENT_SECONDS = 12.0
RSS_GROWTH_DURING_LIMIT_BYTES = 16 * 1024 * 1024
RSS_GROWTH_AFTER_LIMIT_BYTES = 8 * 1024 * 1024
FD_GROWTH_DURING_LIMIT = SSE_CLIENT_LIMIT * 2 + 2
FD_GROWTH_AFTER_LIMIT = 2
CRASH_LEASE_WAIT_SECONDS = SPECTRUM_LEASE_SECONDS + 1.0
RECORDING_DECODE_TIMEOUT_SECONDS = 30.0
SIMULATED_FAULT_EXPECTATIONS: dict[str, dict[str, object]] = {
    "srt_retry_wait": {
        "scope": "connection",
        "state": "retry_wait",
        "next_retry_seconds": 2,
        "last_error": {
            "code": "srt_connection_failed",
            "message": "controlled simulated SRT retry wait",
        },
    },
    "capture_device_lost": {
        "scope": "capture",
        "state": "failed",
        "last_error": {
            "code": "device_lost",
            "message": "controlled simulated capture device loss",
        },
    },
    "recording_blocked_storage": {
        "scope": "recording",
        "state": "blocked",
        "warning": {
            "code": "storage_unsafe",
            "message": "controlled simulated recording storage hard stop",
        },
        "last_error": None,
    },
}

SIMULATED_CAPABILITIES = {
    "schema_version": 1,
    "devices": [
        {
            "id": SIM_DEVICE_ID,
            "label": "Step 3 non-Cartesian simulator",
            "evidence_status": "CI validated",
            "modes": [
                {
                    "format": "S16LE",
                    "rate_hz": 48_000,
                    "channels": 1,
                    "native": True,
                    "evidence_status": "CI validated",
                    "stream_options": [
                        {
                            "representation_id": REPRESENTATION_ID,
                            "conversion": "Simulated mono-to-stereo conversion contract.",
                            "evidence_status": "CI validated",
                        }
                    ],
                },
                {
                    "format": "S24LE",
                    "rate_hz": 48_000,
                    "channels": 1,
                    "native": True,
                    "evidence_status": "CI validated",
                    "stream_options": [],
                },
            ],
        }
    ],
    "representations": [
        {
            "id": REPRESENTATION_ID,
            "label": "Simulated Opus 48 kHz stereo / Matroska",
            "codec": "Opus",
            "container": "streamable Matroska",
            "lossless": False,
            "bitrate_bps": 128_000,
            "evidence_status": "CI validated",
        }
    ],
}


def _simulated_config(
    recording_directory: Path, media_secret: str
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "capture": {
            "device_id": SIM_DEVICE_ID,
            "mode": {"format": "S16LE", "rate_hz": 48_000, "channels": 1},
        },
        "stream": {
            "enabled": True,
            "representation_id": REPRESENTATION_ID,
            "destination_host": "127.0.0.1",
            "destination_port": 4_001,
            "latency_ms": 125,
            "stream_id": "step3-simulated",
            "passphrase": media_secret,
            "opus_bitrate_bps": 128_000,
        },
        "recording": {
            "directory": str(recording_directory),
            "rotation_seconds": 3_600,
            "required_mountpoint": None,
        },
        "monitoring": {
            "spectrum_updates_per_second": 5,
            "spectrum_bands": 64,
        },
    }


class _SimulatedDecision:
    def __init__(self, state: Mapping[str, object], changed: bool) -> None:
        self._state = dict(state)
        self._changed = changed

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": True,
            "changed": self._changed,
            "effects": [],
            "error": None,
            "state": self._state,
        }


class _SimulatedRuntime:
    """Persistent control-state fixture; deliberately not a media engine."""

    def __init__(
        self,
        state_path: Path,
        fault_path: Path,
        config: MediaConfig,
        config_revision: str,
    ) -> None:
        self._path = state_path
        self._fault_path = fault_path
        self._config = config
        self._revision = config_revision
        self._secret = config.stream.passphrase
        self._lock = threading.Lock()
        self._state = self._load_state()
        self._spectrum_lease_until = 0.0

    def _load_state(self) -> dict[str, object]:
        initial: dict[str, object] = {
            "capture_state": "stopped",
            "capture_generation": 0,
            "stream_state": "stopped",
            "stream_generation": 0,
            "connection_state": "disconnected",
            "recording_state": "stopped",
            "recording_generation": 0,
            "stream_bytes": 0,
            "stream_packets": 0,
            "recording_bytes": 0,
            "last_wall": time.time(),
            "opus_bitrate_bps": self._config.stream.opus_bitrate_bps,
        }
        if not self._path.exists():
            return initial
        value = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or set(value) != set(initial):
            raise ValueError("simulated media state is malformed")
        for name in (
            "capture_generation",
            "stream_generation",
            "recording_generation",
            "stream_bytes",
            "stream_packets",
            "recording_bytes",
            "opus_bitrate_bps",
        ):
            if type(value[name]) is not int or value[name] < 0:
                raise ValueError("simulated media counter is malformed")
        if type(value["last_wall"]) not in (int, float):
            raise ValueError("simulated media clock is malformed")
        return value

    def _accrue_locked(self) -> None:
        now = time.time()
        elapsed = max(0.0, min(now - float(self._state["last_wall"]), 3_600.0))
        if self._state["stream_state"] == "running":
            self._state["stream_bytes"] = int(self._state["stream_bytes"]) + int(
                elapsed * 64_000
            )
            self._state["stream_packets"] = int(self._state["stream_packets"]) + int(
                elapsed * 100
            )
        if self._state["recording_state"] == "running":
            self._state["recording_bytes"] = int(
                self._state["recording_bytes"]
            ) + int(elapsed * 48_000)
        self._state["last_wall"] = now

    def _save_locked(self) -> None:
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self._state, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(temporary, self._path)

    def _state_dict_locked(self) -> dict[str, object]:
        stream_running = self._state["stream_state"] == "running"
        recording_running = self._state["recording_state"] == "running"
        sequence = int(self._state["stream_packets"])
        state: dict[str, object] = {
            "schema_version": 1,
            "service": "running",
            "capture": {
                "state": self._state["capture_state"],
                "generation": self._state["capture_generation"],
                "config_revision": (
                    self._revision
                    if self._state["capture_state"] == "running"
                    else None
                ),
                "last_error": None,
            },
            "stream": {
                "state": self._state["stream_state"],
                "generation": self._state["stream_generation"],
                "config_revision": self._revision if stream_running else None,
                "representation_id": REPRESENTATION_ID if stream_running else None,
                "opus_bitrate_bps": (
                    self._state["opus_bitrate_bps"] if stream_running else None
                ),
                "pending_opus_bitrate_bps": None,
                "last_error": None,
            },
            "connection": {
                "state": self._state["connection_state"],
                "reconnect_count": 0,
                "consecutive_failures": 0,
                "next_retry_seconds": None,
                "statistics": {
                    "bytes-sent-total": self._state["stream_bytes"],
                    "packets-sent-total": sequence,
                },
                "last_error": None,
            },
            "recording": {
                "state": self._state["recording_state"],
                "generation": self._state["recording_generation"],
                "config_revision": self._revision if recording_running else None,
                "stop_target": None,
                "rotation_pending": False,
                "current_file": "simulated.flac" if recording_running else None,
                "last_finalized_file": None,
                "bytes_written": self._state["recording_bytes"],
                "warning": None,
                "last_error": None,
            },
            "monitoring": {"last_error": None},
        }
        fault = self._fault_kind_locked()
        if fault is not None:
            expected = SIMULATED_FAULT_EXPECTATIONS[fault]
            scope = str(expected["scope"])
            scoped = _dict(state[scope], f"simulated {scope} state")
            for name in ("state", "next_retry_seconds", "warning", "last_error"):
                if name in expected:
                    scoped[name] = copy.deepcopy(expected[name])
        return state

    def _fault_kind_locked(self) -> str | None:
        try:
            with self._fault_path.open("rb") as handle:
                raw = handle.read(129)
        except FileNotFoundError:
            return None
        if len(raw) > 128:
            raise ValueError("simulated fault state is too large")
        value = raw.decode("ascii").strip()
        if not value:
            return None
        if value not in SIMULATED_FAULT_EXPECTATIONS:
            raise ValueError("simulated fault state is invalid")
        return value

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            self._accrue_locked()
            self._save_locked()
            sequence = max(1, int(self._state["stream_packets"]))
            spectrum = (
                {
                    "sequence": sequence,
                    "magnitudes_db": [-80.0, -64.0, -48.0, -32.0],
                }
                if time.monotonic() < self._spectrum_lease_until
                else None
            )
            return {
                "state": self._state_dict_locked(),
                "telemetry": {
                    "meter": {
                        "sequence": sequence,
                        "channels": [
                            {
                                "rms_dbfs": -24.0,
                                "peak_dbfs": -9.0,
                                "clipping": False,
                                "no_signal": False,
                            }
                        ],
                    },
                    "spectrum": spectrum,
                },
                "runtime_error": None,
                "debug": {"passphrase": self._secret},
            }

    def renew_spectrum_lease(self) -> bool:
        with self._lock:
            self._spectrum_lease_until = time.monotonic() + SPECTRUM_LEASE_SECONDS
        return True

    def release_spectrum_lease(self) -> bool:
        with self._lock:
            self._spectrum_lease_until = 0.0
        return False

    def _set_running(self, scope: str) -> _SimulatedDecision:
        with self._lock:
            self._accrue_locked()
            state_key = f"{scope}_state"
            changed = self._state[state_key] != "running"
            if scope != "capture" and self._state["capture_state"] != "running":
                raise ValueError("capture must be running")
            if changed:
                self._state[state_key] = "running"
                self._state[f"{scope}_generation"] = int(
                    self._state[f"{scope}_generation"]
                ) + 1
                if scope == "stream":
                    self._state["connection_state"] = "connected"
            self._save_locked()
            return _SimulatedDecision(self._state_dict_locked(), changed)

    def _set_stopped(self, scope: str) -> _SimulatedDecision:
        with self._lock:
            self._accrue_locked()
            state_key = f"{scope}_state"
            changed = self._state[state_key] != "stopped"
            if scope == "capture" and (
                self._state["stream_state"] == "running"
                or self._state["recording_state"] == "running"
            ):
                raise ValueError("branches must stop before capture")
            self._state[state_key] = "stopped"
            if scope == "stream":
                self._state["connection_state"] = "disconnected"
            self._save_locked()
            return _SimulatedDecision(self._state_dict_locked(), changed)

    def start_capture(self) -> _SimulatedDecision:
        return self._set_running("capture")

    def stop_capture(self) -> _SimulatedDecision:
        return self._set_stopped("capture")

    def start_stream(self) -> _SimulatedDecision:
        return self._set_running("stream")

    def stop_stream(self) -> _SimulatedDecision:
        return self._set_stopped("stream")

    def start_recording(self) -> _SimulatedDecision:
        return self._set_running("recording")

    def stop_recording(self) -> _SimulatedDecision:
        return self._set_stopped("recording")

    def set_opus_bitrate(self, bitrate_bps: int) -> _SimulatedDecision:
        with self._lock:
            if bitrate_bps not in (64_000, 96_000, 128_000, 192_000):
                raise ValueError("unsupported bitrate")
            changed = self._state["opus_bitrate_bps"] != bitrate_bps
            self._state["opus_bitrate_bps"] = bitrate_bps
            self._save_locked()
            return _SimulatedDecision(self._state_dict_locked(), changed)

    def apply_live_config(self, config: MediaConfig, revision: str) -> None:
        with self._lock:
            self._config = config
            self._revision = revision
            self._state["opus_bitrate_bps"] = config.stream.opus_bitrate_bps
            self._save_locked()


def _simulated_media_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--socket", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--fault-state", required=True, type=Path)
    args = parser.parse_args(argv)
    approved = (REPRESENTATION_ID,)
    capabilities = validate_audio_capabilities(SIMULATED_CAPABILITIES, approved)
    config_bytes = args.config.read_bytes()
    config = load_config(args.config, approved)
    validate_config_combination(config, capabilities)
    revision = hashlib.sha256(config_bytes).hexdigest()
    runtime = _SimulatedRuntime(args.state, args.fault_state, config, revision)
    handler = MediaControlHandler(
        runtime,
        config,
        args.config,
        revision,
        approved,
        capabilities,
    )

    def dispatch(operation: str, arguments: dict[str, Any]) -> object:
        try:
            return handler(operation, arguments)
        except MediaControlError as exc:
            raise ControlRequestError(exc.code, exc.message) from exc

    server = UnixControlServer(args.socket, dispatch)
    stopped = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        stopped.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, stop)
    try:
        server.start()
        while not stopped.wait(0.2):
            pass
    finally:
        server.close()
    return 0


class _ControlGate:
    """A stoppable protocol gate used without stopping the media process."""

    def __init__(self, path: Path, target: ControlClient) -> None:
        self.path = path
        self.target = target
        self.server: UnixControlServer | None = None

    def start(self) -> None:
        if self.server is not None:
            raise RuntimeError("control gate is already running")

        def forward(operation: str, arguments: dict[str, Any]) -> object:
            try:
                return self.target.request(operation, arguments)
            except ControlOperationError as exc:
                raise ControlRequestError(exc.code, exc.message) from exc
            except ControlUnavailable as exc:
                raise ControlRequestError(
                    "media_unavailable", "media service is unavailable"
                ) from exc

        self.server = UnixControlServer(self.path, forward)
        self.server.start()

    def stop(self) -> None:
        server, self.server = self.server, None
        if server is not None:
            server.close()


class _Browser:
    def __init__(self, port: int, *, timeout: float = 5.0) -> None:
        self.port = port
        self.timeout = timeout
        self.cookies: dict[str, str] = {}
        self.csrf = ""

    def request(
        self,
        method: str,
        path: str,
        body: object | None = None,
        *,
        csrf: bool = False,
    ) -> tuple[int, object, list[tuple[str, str]]]:
        headers: dict[str, str] = {}
        payload: bytes | None = None
        if self.cookies:
            headers["Cookie"] = self.cookie_header()
        if csrf:
            headers["X-CSRF-Token"] = self.csrf
        if body is not None:
            payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=self.timeout
        )
        try:
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            response_headers = response.getheaders()
            self._receive_cookies(response_headers)
            content_type = response.getheader("Content-Type", "")
            value: object = raw.decode("utf-8", errors="replace")
            if raw and "application/json" in content_type:
                value = json.loads(raw)
            return response.status, value, response_headers
        finally:
            connection.close()

    def login(self, username: str, password: str) -> tuple[int, object]:
        status, value, _headers = self.request(
            "POST",
            "/api/login",
            {"username": username, "password": password},
        )
        if status == 200 and isinstance(value, dict):
            token = value.get("csrf_token")
            self.csrf = token if isinstance(token, str) else ""
        return status, value

    def open_sse(self) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=self.timeout
        )
        connection.putrequest("GET", "/api/events")
        connection.putheader("Cookie", self.cookie_header())
        connection.endheaders()
        return connection, connection.getresponse()

    def cookie_header(self) -> str:
        return "; ".join(f"{key}={value}" for key, value in self.cookies.items())

    def _receive_cookies(self, headers: Sequence[tuple[str, str]]) -> None:
        for key, value in headers:
            if key.casefold() != "set-cookie":
                continue
            cookie = http.cookies.SimpleCookie()
            cookie.load(value)
            for name, morsel in cookie.items():
                if morsel["max-age"] == "0" or not morsel.value:
                    self.cookies.pop(name, None)
                else:
                    self.cookies[name] = morsel.value


def _start_process(arguments: Sequence[str]) -> subprocess.Popen[str]:
    environment = os.environ.copy()
    source = str(REPO_ROOT / "src")
    environment["PYTHONPATH"] = source + (
        os.pathsep + environment["PYTHONPATH"]
        if environment.get("PYTHONPATH")
        else ""
    )
    return subprocess.Popen(
        list(arguments),
        cwd=REPO_ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _start_web(
    python: str, credential: Path, socket_path: Path, port: int
) -> subprocess.Popen[str]:
    return _start_process(
        (
            python,
            "-B",
            "-m",
            "tinypirelay.web_service",
            "serve",
            "--credentials",
            str(credential),
            "--control-socket",
            str(socket_path),
            "--bind",
            "127.0.0.1",
            "--port",
            str(port),
            "--max-clients",
            "8",
            "--max-sse-clients",
            str(SSE_CLIENT_LIMIT),
            "--log-level",
            "WARNING",
        )
    )


def _stop_process(
    process: subprocess.Popen[str] | None,
    *,
    kill: bool = False,
    redactions: Sequence[str] = (),
) -> str:
    if process is None:
        return ""
    if process.poll() is None:
        (process.kill if kill else process.terminate)()
    try:
        _stdout, stderr = process.communicate(timeout=4)
    except subprocess.TimeoutExpired:
        process.kill()
        _stdout, stderr = process.communicate(timeout=4)
    return _safe_text(stderr[-2_048:], redactions)


def _wait_for_control(
    client: ControlClient,
    timeout: float,
    process: subprocess.Popen[str] | None = None,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError("media participant exited before becoming ready")
        try:
            client.request("status")
            return
        except ControlError:
            time.sleep(0.05)
    raise TimeoutError("media control socket did not become ready")


def _wait_for_web(
    port: int,
    timeout: float,
    process: subprocess.Popen[str],
    redactions: Sequence[str] = (),
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            _stdout, stderr = process.communicate(timeout=1)
            raise RuntimeError(
                f"web process exited during startup: {_safe_text(stderr, redactions)}"
            )
        try:
            status, _value, _headers = _Browser(port, timeout=0.5).request("GET", "/")
            if status == 200:
                return
        except (OSError, TimeoutError, http.client.HTTPException):
            pass
        time.sleep(0.05)
    raise TimeoutError("web process did not become ready")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _dict(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} was not a JSON object")
    return value


def _error_code(value: object) -> str | None:
    if not isinstance(value, dict) or not isinstance(value.get("error"), dict):
        return None
    code = value["error"].get("code")
    return code if isinstance(code, str) else None


def _state_sample(status: object) -> dict[str, object]:
    root = _dict(status, "status")
    state = _dict(root.get("state"), "status.state")
    capture = _dict(state.get("capture"), "capture state")
    stream = _dict(state.get("stream"), "stream state")
    connection = _dict(state.get("connection"), "connection state")
    recording = _dict(state.get("recording"), "recording state")
    statistics = connection.get("statistics")
    statistics = statistics if isinstance(statistics, dict) else {}
    stream_counter = statistics.get("bytes-sent-total")
    recording_counter = recording.get("bytes_written")
    recording_counter_source = (
        "bytes_written" if type(recording_counter) is int else None
    )
    current_file = recording.get("current_file")
    current_file_regular = False
    if isinstance(current_file, str) and current_file and Path(current_file).is_absolute():
        try:
            file_status = os.stat(current_file, follow_symlinks=False)
            current_file_regular = stat.S_ISREG(file_status.st_mode)
        except OSError:
            pass
    if type(recording_counter) is not int:
        if current_file_regular:
            recording_counter = file_status.st_size
            recording_counter_source = "current_file_st_size"
    return {
        "capture_state": capture.get("state"),
        "capture_generation": capture.get("generation"),
        "stream_state": stream.get("state"),
        "stream_generation": stream.get("generation"),
        "connection_state": connection.get("state"),
        "stream_counter": stream_counter if type(stream_counter) is int else None,
        "recording_state": recording.get("state"),
        "recording_generation": recording.get("generation"),
        "recording_counter": (
            recording_counter if type(recording_counter) is int else None
        ),
        "recording_counter_source": recording_counter_source,
        "recording_current_file": current_file if isinstance(current_file, str) else None,
        "recording_current_file_absolute_regular": current_file_regular,
        "recording_last_finalized_file": (
            recording.get("last_finalized_file")
            if isinstance(recording.get("last_finalized_file"), str)
            else None
        ),
    }


def _continuity(
    pre: Mapping[str, object],
    middle: Mapping[str, object],
    post: Mapping[str, object],
) -> dict[str, object]:
    samples = [pre, middle, post]
    generations_stable = all(
        samples[index][name] == samples[0][name]
        for index in (1, 2)
        for name in (
            "capture_generation",
            "stream_generation",
            "recording_generation",
        )
    )
    states_running = all(
        sample["capture_state"] == "running"
        and sample["stream_state"] == "running"
        and sample["connection_state"] == "connected"
        and sample["recording_state"] == "running"
        for sample in samples
    )
    stream_values = [sample["stream_counter"] for sample in samples]
    stream_counter_increased = all(type(item) is int for item in stream_values) and (
        int(stream_values[0]) < int(stream_values[1]) < int(stream_values[2])
    )
    recording_values = [sample["recording_counter"] for sample in samples]
    recording_supported = all(type(item) is int for item in recording_values)
    recording_increased = recording_supported and (
        int(recording_values[0]) < int(recording_values[1]) < int(recording_values[2])
    )
    recording_files = [sample["recording_current_file"] for sample in samples]
    recording_file_checks = [
        sample["recording_current_file_absolute_regular"] for sample in samples
    ]
    return {
        "states_running_throughout": states_running,
        "generations_unchanged": generations_stable,
        "stream_stat_counter_increased": stream_counter_increased,
        "recording_counter_supported": recording_supported,
        "recording_counter_increased_if_supported": recording_increased,
        "recording_counter_sources": [
            sample["recording_counter_source"] for sample in samples
        ],
        "recording_current_files": recording_files,
        "recording_current_files_absolute_regular": recording_file_checks,
    }


def _find_non_cartesian_config(
    config: Mapping[str, Any], capabilities: Mapping[str, Any]
) -> dict[str, Any]:
    candidate = copy.deepcopy(dict(config))
    devices = capabilities.get("capture_devices")
    representations = capabilities.get("stream_representations")
    if not isinstance(devices, list) or not isinstance(representations, list):
        raise ValueError("capability response is malformed")
    representation_ids = [
        item.get("id")
        for item in representations
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    for device in devices:
        if not isinstance(device, dict) or not isinstance(device.get("modes"), list):
            continue
        for mode in device["modes"]:
            if not isinstance(mode, dict) or not isinstance(
                mode.get("stream_options"), list
            ):
                continue
            allowed = {
                option.get("id")
                for option in mode["stream_options"]
                if isinstance(option, dict)
            }
            denied = next((item for item in representation_ids if item not in allowed), None)
            if denied is None:
                continue
            candidate["capture"] = {
                "device_id": device.get("id"),
                "mode": {
                    "format": mode.get("format"),
                    "rate_hz": mode.get("rate_hz"),
                    "channels": mode.get("channels"),
                },
            }
            stream = _dict(candidate.get("stream"), "config.stream")
            stream["representation_id"] = denied
            return candidate
    raise ValueError("capability response has no non-Cartesian rejection fixture")


def _read_sse_snapshot(response: http.client.HTTPResponse) -> tuple[dict[str, Any], str]:
    lines: list[str] = []
    total = 0
    while total <= 64 * 1024:
        raw = response.readline(64 * 1024 + 1)
        if not raw:
            break
        total += len(raw)
        line = raw.decode("utf-8", errors="strict").rstrip("\r\n")
        if not line:
            break
        lines.append(line)
    data = next((line[6:] for line in lines if line.startswith("data: ")), None)
    if data is None:
        raise ValueError("SSE snapshot did not contain data")
    return _dict(json.loads(data), "SSE data"), "\n".join(lines)


def _numeric_telemetry(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    meter = value.get("meter")
    spectrum = value.get("spectrum")
    if not isinstance(meter, dict) or not isinstance(spectrum, dict):
        return False
    channels = meter.get("channels")
    magnitudes = spectrum.get("magnitudes_db")
    if not isinstance(channels, list) or not 1 <= len(channels) <= 2:
        return False
    if not isinstance(magnitudes, list) or not 1 <= len(magnitudes) <= 2_048:
        return False
    if not all(
        type(item) in (int, float) and math.isfinite(float(item))
        for item in magnitudes
    ):
        return False
    for channel in channels:
        if not isinstance(channel, dict) or set(channel) != {
            "rms_dbfs",
            "peak_dbfs",
            "clipping",
            "no_signal",
        }:
            return False
        for key in ("rms_dbfs", "peak_dbfs"):
            item = channel[key]
            if item is not None and not (
                type(item) in (int, float) and math.isfinite(float(item))
            ):
                return False
        if (
            type(channel["clipping"]) is not bool
            or type(channel["no_signal"]) is not bool
        ):
            return False
    return True


def _read_numeric_sse(
    response: http.client.HTTPResponse, timeout_seconds: float = 5.0
) -> tuple[dict[str, Any], str]:
    deadline = time.monotonic() + timeout_seconds
    rendered: list[str] = []
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        latest, text = _read_sse_snapshot(response)
        rendered.append(text)
        if _numeric_telemetry(latest.get("telemetry")):
            return latest, "\n".join(rendered)
    return latest, "\n".join(rendered)


def _process_resources(pid: int) -> dict[str, object]:
    """Read Linux process RSS and descriptor count without another dependency."""

    try:
        root = Path("/proc") / str(pid)
        rss_line = next(
            line for line in (root / "status").read_text(encoding="ascii").splitlines()
            if line.startswith("VmRSS:")
        )
        _name, amount, unit = rss_line.split()
        if unit != "kB":
            raise ValueError("unexpected VmRSS unit")
        rss_bytes = int(amount) * 1024
        fd_count = sum(1 for _entry in (root / "fd").iterdir())
    except (OSError, StopIteration, ValueError):
        return {"supported": False, "rss_bytes": None, "fd_count": None}
    return {"supported": True, "rss_bytes": rss_bytes, "fd_count": fd_count}


def _exercise_sse(
    browser: _Browser, web_pid: int, slow_client_seconds: float
) -> dict[str, object]:
    baseline = _process_resources(web_pid)
    slow: list[tuple[http.client.HTTPConnection, http.client.HTTPResponse]] = []
    hold_started = time.monotonic()
    try:
        for _index in range(SSE_CLIENT_LIMIT):
            connection, response = browser.open_sse()
            if response.status != 200:
                response.read()
                connection.close()
                raise RuntimeError("bounded SSE client could not connect")
            if connection.sock is not None:
                connection.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1_024)
            slow.append((connection, response))
        hold_started = time.monotonic()
        extra_connection, extra_response = browser.open_sse()
        overload_status = extra_response.status
        extra_response.read()
        extra_connection.close()
        time.sleep(slow_client_seconds)
        during = _process_resources(web_pid)
        held_seconds = time.monotonic() - hold_started
    finally:
        for connection, response in slow:
            response.close()
            connection.close()

    churn_attempts = 12
    request = (
        "GET /api/events HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        f"Cookie: {browser.cookie_header()}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    for _index in range(churn_attempts):
        try:
            with socket.create_connection(
                ("127.0.0.1", browser.port), timeout=1
            ) as raw:
                raw.sendall(request)
        except OSError:
            pass

    recovered = False
    numeric = False
    image_free = False
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not recovered:
        connection, response = browser.open_sse()
        if response.status == 200:
            payload, text = _read_numeric_sse(response)
            recovered = True
            numeric = _numeric_telemetry(payload.get("telemetry"))
            image_free = "data:image" not in text.casefold()
        else:
            response.read()
        response.close()
        connection.close()
        if not recovered:
            time.sleep(0.15)
    time.sleep(0.25)
    after = _process_resources(web_pid)

    def delta(sample: Mapping[str, object], name: str) -> int | None:
        before = baseline.get(name)
        current = sample.get(name)
        if type(before) is int and type(current) is int:
            return current - before
        return None

    return {
        "configured_client_limit": SSE_CLIENT_LIMIT,
        "slow_clients_held_open": len(slow),
        "slow_clients_unread_seconds": held_seconds,
        "overload_status": overload_status,
        "disconnect_churn_attempts": churn_attempts,
        "slot_recovered_after_disconnects": recovered,
        "bounded_numeric_telemetry": numeric,
        "no_server_rendered_images": image_free,
        "process_resources": {
            "baseline": baseline,
            "during": during,
            "after": after,
            "deltas": {
                "rss_during_bytes": delta(during, "rss_bytes"),
                "rss_after_bytes": delta(after, "rss_bytes"),
                "fd_during": delta(during, "fd_count"),
                "fd_after": delta(after, "fd_count"),
            },
            "limits": {
                "rss_during_bytes": RSS_GROWTH_DURING_LIMIT_BYTES,
                "rss_after_bytes": RSS_GROWTH_AFTER_LIMIT_BYTES,
                "fd_during": FD_GROWTH_DURING_LIMIT,
                "fd_after": FD_GROWTH_AFTER_LIMIT,
            },
        },
    }


def _fault_projection(
    value: object, expected: Mapping[str, object], source: str
) -> dict[str, object]:
    root = _dict(value, f"{source} fault status")
    state = _dict(root.get("state"), "fault status.state")
    scope = str(expected["scope"])
    scoped = _dict(state.get(scope), f"fault status.state.{scope}")
    return {
        name: copy.deepcopy(scoped.get(name))
        for name in expected
        if name != "scope"
    }


def _set_simulated_fault(path: Path, kind: str | None) -> None:
    if kind is not None and kind not in SIMULATED_FAULT_EXPECTATIONS:
        raise ValueError("unknown simulated fault")
    temporary = path.with_suffix(".tmp")
    temporary.write_text("" if kind is None else kind + "\n", encoding="ascii")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _exercise_simulated_faults(
    browser: _Browser, target: ControlClient, fault_path: Path
) -> dict[str, object]:
    observations: dict[str, object] = {}
    try:
        for kind, expected in SIMULATED_FAULT_EXPECTATIONS.items():
            _set_simulated_fault(fault_path, kind)
            http_status, http_value, _headers = browser.request("GET", "/api/status")
            deadline = time.monotonic() + 5.0
            while True:
                connection, response = browser.open_sse()
                if response.status == 200 or time.monotonic() >= deadline:
                    break
                response.read()
                response.close()
                connection.close()
                time.sleep(0.1)
            try:
                sse_status = response.status
                payload, _text = (
                    _read_sse_snapshot(response) if sse_status == 200 else ({}, "")
                )
                if sse_status != 200:
                    response.read()
            finally:
                response.close()
                connection.close()
            observations[kind] = {
                "expected": copy.deepcopy(dict(expected)),
                "http_status": http_status,
                "http": _fault_projection(http_value, expected, "HTTP"),
                "sse_status": sse_status,
                "sse": _fault_projection(payload.get("status"), expected, "SSE"),
            }
    finally:
        _set_simulated_fault(fault_path, None)
    return {
        "performed": True,
        "classification": "controlled_simulator_injection_not_hardware",
        "physical_fault_injection": "pending",
        "states": observations,
    }


def _control(browser: _Browser, action: str) -> tuple[int, object]:
    status, value, _headers = browser.request(
        "POST", "/api/control", {"action": action}, csrf=True
    )
    return status, value


def _wait_for_scope(
    target: ControlClient, scope: str, expected: str, timeout: float
) -> object:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = target.request("status")
        sample = _state_sample(status)
        ready = sample[f"{scope}_state"] == expected
        if scope == "stream" and expected == "running":
            ready = ready and sample["connection_state"] == "connected"
        if ready:
            return status
        time.sleep(0.05)
    detail = " and connected" if scope == "stream" and expected == "running" else ""
    raise TimeoutError(f"media {scope} did not become {expected}{detail}")


def _ensure_active(
    browser: _Browser,
    target: ControlClient,
    status: object,
    manage_media: bool,
    timeout: float,
) -> tuple[dict[str, bool], object]:
    sample = _state_sample(status)
    started = {"capture": False, "stream": False, "recording": False}
    active = status
    for scope in ("capture", "stream", "recording"):
        if sample[f"{scope}_state"] != "running":
            if not manage_media:
                raise RuntimeError(
                    f"attached media {scope} is not running; use --manage-media to start it"
                )
            code, value = _control(browser, f"{scope}.start")
            if code != 200:
                error = _error_code(value)
                raise RuntimeError(f"could not start attached {scope}: {error}")
            started[scope] = True
        active = _wait_for_scope(target, scope, "running", timeout)
        sample = _state_sample(active)
    return started, active


def _cleanup_started(
    target: ControlClient, started: Mapping[str, bool], timeout: float
) -> tuple[list[str], object | None]:
    errors: list[str] = []
    recording_stop_status: object | None = None
    for scope in ("recording", "stream", "capture"):
        if started.get(scope):
            try:
                target.request(f"{scope}.stop")
                stopped = _wait_for_scope(target, scope, "stopped", timeout)
                if scope == "recording":
                    recording_stop_status = stopped
            except (ControlError, OSError, TimeoutError) as exc:
                errors.append(f"cleanup {scope} failed: {type(exc).__name__}")
    return errors, recording_stop_status


def _flac_streaminfo(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as handle:
            prefix = handle.read(42)
    except OSError:
        return {"header": None, "streaminfo_valid": False}
    result: dict[str, object] = {
        "header": prefix[:4].decode("ascii", errors="replace"),
        "streaminfo_valid": False,
    }
    if (
        len(prefix) < 42
        or prefix[:4] != b"fLaC"
        or prefix[4] & 0x7F != 0
        or int.from_bytes(prefix[5:8], "big") != 34
    ):
        return result
    packed = int.from_bytes(prefix[18:26], "big")
    sample_rate = (packed >> 44) & 0xFFFFF
    total_samples = packed & ((1 << 36) - 1)
    if sample_rate < 1 or total_samples < 1:
        return result
    result.update(
        {
            "streaminfo_valid": True,
            "sample_rate_hz": sample_rate,
            "total_samples": total_samples,
            "media_duration_seconds": total_samples / sample_rate,
        }
    )
    return result


def _attached_recording_evidence(
    args: argparse.Namespace,
    continuity: Mapping[str, object],
    started: Mapping[str, bool],
    recording_stop_status: object | None,
    redactions: Sequence[str],
) -> dict[str, object]:
    live_files = continuity.get("recording_current_files")
    live_file = (
        live_files[0]
        if isinstance(live_files, list)
        and len(live_files) == 3
        and all(item == live_files[0] for item in live_files)
        and isinstance(live_files[0], str)
        and live_files[0]
        else None
    )
    stop_sample: dict[str, object] = {}
    if recording_stop_status is not None:
        try:
            stop_sample = _state_sample(recording_stop_status)
        except ValueError:
            pass
    finalized_file = stop_sample.get("recording_last_finalized_file")
    path = Path(live_file) if isinstance(live_file, str) else None
    regular = False
    size: int | None = None
    if path is not None and path.is_absolute():
        try:
            file_status = os.stat(path, follow_symlinks=False)
            regular = stat.S_ISREG(file_status.st_mode)
            size = file_status.st_size if regular else None
        except OSError:
            pass
    flac = _flac_streaminfo(path) if regular and path is not None else {
        "header": None,
        "streaminfo_valid": False,
    }
    digest: str | None = None
    if regular and path is not None:
        try:
            hasher = hashlib.sha256()
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    hasher.update(chunk)
            digest = hasher.hexdigest()
        except OSError:
            pass

    command = (
        [
            args.gst_launch,
            "-q",
            "filesrc",
            f"location={live_file}",
            "!",
            "flacparse",
            "!",
            "flacdec",
            "!",
            "fakesink",
        ]
        if live_file is not None
        else []
    )
    decode: dict[str, object] = {
        "tool": args.gst_launch,
        "command": command,
        "timeout_seconds": RECORDING_DECODE_TIMEOUT_SECONDS,
        "attempted": False,
        "timed_out": False,
        "exit_code": None,
        "result": "preconditions_failed",
        "elapsed_seconds": 0.0,
        "stderr_tail": "",
        "decode_to_eos": False,
    }
    ready = bool(
        started.get("recording")
        and stop_sample.get("recording_state") == "stopped"
        and finalized_file == live_file
        and regular
        and type(size) is int
        and size > 4
        and flac.get("header") == "fLaC"
        and flac.get("streaminfo_valid") is True
        and digest is not None
    )
    if ready:
        decode["attempted"] = True
        began = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=RECORDING_DECODE_TIMEOUT_SECONDS,
                check=False,
            )
            decode["exit_code"] = completed.returncode
            decode["result"] = "eos" if completed.returncode == 0 else "failed"
            decode["stderr_tail"] = _safe_text(completed.stderr[-2_048:], redactions)
            decode["decode_to_eos"] = completed.returncode == 0
        except subprocess.TimeoutExpired:
            decode["timed_out"] = True
            decode["result"] = "timeout"
        except OSError as exc:
            decode["result"] = "tool_error"
            decode["error_type"] = type(exc).__name__
        decode["elapsed_seconds"] = time.monotonic() - began
    return {
        "applicable": True,
        "mode": "attached_physical_finalized_flac_decode",
        "recording_started_by_harness": started.get("recording") is True,
        "current_file": live_file,
        "recording_state_after_cleanup": stop_sample.get("recording_state"),
        "last_finalized_file": finalized_file,
        "finalized_file_matches_current_file": finalized_file == live_file,
        "file_regular": regular,
        "file_size_bytes": size,
        **flac,
        "sha256": digest,
        "decode": decode,
    }


def _exercise(
    args: argparse.Namespace,
    work: Path,
    target: ControlClient,
    gate: _ControlGate,
    credential: Path,
    simulated_fault_path: Path | None,
    report: dict[str, Any],
    web_password: str,
    known_media_secret: str | None,
) -> None:
    redactions = tuple(
        item for item in (web_password, known_media_secret) if isinstance(item, str)
    )
    port = _free_port()
    web = _start_web(args.python, credential, gate.path, port)
    report["process_evidence"]["web_subprocess_started"] = True
    browser = _Browser(port)
    started: dict[str, bool] = {}
    web_stderr: list[str] = []
    crash_sse: tuple[http.client.HTTPConnection, http.client.HTTPResponse] | None = None
    continuity: dict[str, object] | None = None
    try:
        _wait_for_web(port, args.startup_timeout, web, redactions)
        unauthorized, _value, _headers = browser.request("GET", "/api/status")
        login_status, login_value = browser.login(WEB_USERNAME, web_password)
        session_status, session_value, _headers = browser.request("GET", "/api/session")
        csrf_status, csrf_value, _headers = browser.request(
            "POST", "/api/control", {"action": "capture.stop"}
        )
        logout_status, logout_value, _headers = browser.request(
            "POST", "/api/logout", csrf=True
        )
        after_logout_status, _value, _headers = browser.request("GET", "/api/status")
        second_login_status, _value = browser.login(WEB_USERNAME, web_password)
        wrong_statuses = []
        wrong = _Browser(port)
        for index in range(6):
            status, _value, _headers = wrong.request(
                "POST",
                "/api/login",
                {
                    "username": f"rotating-user-{index}",
                    "password": "wrong-password",
                },
            )
            wrong_statuses.append(status)
        established_status, established_value, _headers = browser.request(
            "GET", "/api/session"
        )
        report["scenarios"]["authentication"] = {
            "unauthorized_status": unauthorized,
            "wrong_login_statuses": wrong_statuses,
            "login_status": login_status,
            "session_status": session_status,
            "session_authenticated": (
                isinstance(session_value, dict)
                and session_value.get("authenticated") is True
            ),
            "csrf_rejection_status": csrf_status,
            "csrf_rejection_code": _error_code(csrf_value),
            "logout_status": logout_status,
            "logout_response_unauthenticated": (
                isinstance(logout_value, dict)
                and logout_value.get("authenticated") is False
            ),
            "old_session_status_after_logout": after_logout_status,
            "second_login_status": second_login_status,
            "rotating_username_count": 6,
            "established_session_status_after_throttle": established_status,
            "established_session_survived_throttle": (
                isinstance(established_value, dict)
                and established_value.get("authenticated") is True
            ),
        }

        config_status, config_value, _headers = browser.request("GET", "/api/config")
        capability_status, capability_value, _headers = browser.request(
            "GET", "/api/capabilities"
        )
        config_root = _dict(config_value, "config response")
        public_config = _dict(config_root.get("config"), "public config")
        revision = config_root.get("revision")
        if not isinstance(revision, str):
            raise ValueError("config revision is missing")
        passphrase = _dict(
            _dict(public_config.get("stream"), "public stream").get("passphrase"),
            "public passphrase metadata",
        )
        malformed = copy.deepcopy(public_config)
        _dict(malformed.get("monitoring"), "monitoring")["spectrum_bands"] = 63
        malformed_status, malformed_value, _headers = browser.request(
            "PUT",
            "/api/config",
            {
                "config": malformed,
                "confirm_restart": True,
                "expected_revision": revision,
            },
            csrf=True,
        )
        capabilities = _dict(capability_value, "capabilities response")
        impossible = _find_non_cartesian_config(public_config, capabilities)
        impossible_status, impossible_value, _headers = browser.request(
            "PUT",
            "/api/config",
            {
                "config": impossible,
                "confirm_restart": True,
                "expected_revision": revision,
            },
            csrf=True,
        )
        unchanged_status, unchanged_value, _headers = browser.request(
            "GET", "/api/config"
        )
        unchanged_root = _dict(unchanged_value, "post-validation config response")
        rendered_contracts = json.dumps(
            {"config": config_value, "capabilities": capability_value},
            sort_keys=True,
        )
        report["scenarios"]["configuration_and_capabilities"] = {
            "config_status": config_status,
            "capability_status": capability_status,
            "passphrase_metadata_only": (
                set(passphrase) == {"configured", "action"}
                and type(passphrase.get("configured")) is bool
                and passphrase.get("action") == "keep"
            ),
            "known_simulator_secret_absent": (
                known_media_secret is None
                or known_media_secret not in rendered_contracts
            ),
            "malformed_setting_status": malformed_status,
            "malformed_setting_code": _error_code(malformed_value),
            "non_cartesian_rejection_status": impossible_status,
            "non_cartesian_rejection_code": _error_code(impossible_value),
            "validation_requests_used_expected_revision": True,
            "rejected_requests_left_config_unchanged": (
                unchanged_status == 200
                and unchanged_root.get("revision") == revision
                and unchanged_root.get("config") == public_config
            ),
        }

        status_code, initial_status, _headers = browser.request("GET", "/api/status")
        if status_code != 200:
            raise RuntimeError("media status is unavailable before continuity test")
        rendered_status = json.dumps(initial_status, sort_keys=True)
        report["scenarios"]["configuration_and_capabilities"][
            "runtime_secret_probe_redacted"
        ] = (
            known_media_secret is None
            or (
                known_media_secret not in rendered_status
                and "[REDACTED]" in rendered_status.upper()
            )
        )
        started, _active = _ensure_active(
            browser,
            target,
            initial_status,
            args.manage_media or args.control_socket is None,
            args.startup_timeout,
        )
        crash_connection, crash_response = browser.open_sse()
        crash_sse = (crash_connection, crash_response)
        if crash_response.status != 200:
            crash_response.read()
            raise RuntimeError("pre-crash SSE client could not connect")
        crash_payload, _text = _read_numeric_sse(
            crash_response, args.startup_timeout
        )
        sse_numeric_before_crash = _numeric_telemetry(
            crash_payload.get("telemetry")
        )
        pre = target.request("status")
        pre_root = _dict(pre, "pre-web-crash media status")
        pre_telemetry = _dict(pre_root.get("telemetry"), "pre-web-crash telemetry")
        pre_spectrum = pre_telemetry.get("spectrum")
        spectrum_present_before_crash = (
            isinstance(pre_spectrum, dict)
            and isinstance(pre_spectrum.get("magnitudes_db"), list)
            and bool(pre_spectrum["magnitudes_db"])
        )
        pre_sample = _state_sample(pre)
        web_stderr.append(_stop_process(web, kill=True, redactions=redactions))
        report["process_evidence"]["web_process_was_killed"] = True
        web = None
        crash_response.close()
        crash_connection.close()
        crash_sse = None
        killed_at = time.monotonic()
        time.sleep(CRASH_LEASE_WAIT_SECONDS)
        middle = target.request("status")
        lease_wait = time.monotonic() - killed_at
        middle_root = _dict(middle, "post-web-crash media status")
        middle_telemetry = _dict(
            middle_root.get("telemetry"), "post-web-crash telemetry"
        )
        middle_meter = middle_telemetry.get("meter")
        middle_sample = _state_sample(middle)
        time.sleep(args.continuity_seconds / 2)
        web = _start_web(args.python, credential, gate.path, port)
        _wait_for_web(port, args.startup_timeout, web, redactions)
        report["process_evidence"]["web_subprocess_restarted"] = True
        old_session_status, old_session_value, _headers = browser.request(
            "GET", "/api/session"
        )
        relogin_status, _value = browser.login(WEB_USERNAME, web_password)
        post_status, post, _headers = browser.request("GET", "/api/status")
        post_sample = _state_sample(post)
        continuity = _continuity(pre_sample, middle_sample, post_sample)
        continuity.update(
            {
                "sse_numeric_spectrum_before_web_crash": sse_numeric_before_crash,
                "spectrum_present_before_web_crash": spectrum_present_before_crash,
                "crash_lease_wait_seconds": lease_wait,
                "level_meter_present_after_lease_expiry": (
                    isinstance(middle_meter, dict)
                    and isinstance(middle_meter.get("channels"), list)
                    and bool(middle_meter["channels"])
                ),
                "spectrum_absent_after_lease_expiry": (
                    middle_telemetry.get("spectrum") is None
                ),
                "old_session_endpoint_status": old_session_status,
                "old_session_invalidated": (
                    isinstance(old_session_value, dict)
                    and old_session_value.get("authenticated") is False
                ),
                "relogin_status": relogin_status,
                "post_restart_status": post_status,
            }
        )
        report["scenarios"]["web_failure_isolation"] = continuity

        assert web is not None
        report["scenarios"]["sse_bounds"] = _exercise_sse(
            browser, web.pid, args.slow_client_seconds
        )
        report["scenarios"]["fault_state_visibility"] = (
            _exercise_simulated_faults(browser, target, simulated_fault_path)
            if simulated_fault_path is not None
            else {
                "performed": False,
                "classification": "attached_media_fault_injection_not_performed",
                "physical_fault_injection": "pending",
                "states": {},
            }
        )

        before_gate = _state_sample(target.request("status"))
        gate.stop()
        unavailable_status, unavailable_value, _headers = browser.request(
            "GET", "/api/status"
        )
        gate.start()
        recovery_status = 0
        recovery_value: object = {}
        deadline = time.monotonic() + args.startup_timeout
        while time.monotonic() < deadline:
            recovery_status, recovery_value, _headers = browser.request(
                "GET", "/api/status"
            )
            if recovery_status == 200:
                break
            time.sleep(0.05)
        recovery_sample = _state_sample(recovery_value)
        after_gate = _state_sample(target.request("status"))
        gate_continuity = _continuity(before_gate, recovery_sample, after_gate)
        report["scenarios"]["media_unavailable_recovery"] = {
            "unavailable_status": unavailable_status,
            "unavailable_state_explicit": (
                isinstance(unavailable_value, dict)
                and unavailable_value.get("media_available") is False
                and unavailable_value.get("state") == "unavailable"
            ),
            "recovery_status": recovery_status,
            "recovered_available": (
                isinstance(recovery_value, dict)
                and recovery_value.get("available") is True
            ),
            "media_generations_unchanged": gate_continuity[
                "generations_unchanged"
            ],
            "media_states_remained_running": gate_continuity[
                "states_running_throughout"
            ],
        }
    finally:
        if crash_sse is not None:
            crash_sse[1].close()
            crash_sse[0].close()
        cleanup_errors, recording_stop_status = _cleanup_started(
            target, started, args.startup_timeout
        )
        report["setup_errors"].extend(cleanup_errors)
        if (
            args.control_socket is not None
            and args.evidence_kind == "physical-hardware"
            and continuity is not None
            and not (
                continuity.get("recording_counter_supported") is True
                and continuity.get("recording_counter_increased_if_supported") is True
                and continuity.get("recording_counter_sources")
                == ["bytes_written", "bytes_written", "bytes_written"]
            )
        ):
            report["scenarios"]["attached_recording_finalization"] = (
                _attached_recording_evidence(
                    args,
                    continuity,
                    started,
                    recording_stop_status,
                    redactions,
                )
            )
        web_stderr.append(_stop_process(web, redactions=redactions))
        combined_stderr = " ".join(item for item in web_stderr if item).strip()
        issue_codes: list[str] = []
        if "BrokenPipeError" in combined_stderr:
            issue_codes.append("broken_pipe_traceback")
        if "Traceback" in combined_stderr or "Exception occurred" in combined_stderr:
            issue_codes.append("unhandled_request_exception")
        if combined_stderr and not issue_codes:
            issue_codes.append("unexpected_stderr")
        report["process_evidence"]["web_stderr_clean"] = not issue_codes
        report["process_evidence"]["web_stderr_issue_codes"] = issue_codes


def _bounded_process_resources(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    samples: list[Mapping[str, object]] = []
    for name in ("baseline", "during", "after"):
        sample = value.get(name)
        if not isinstance(sample, Mapping) or sample.get("supported") is not True:
            return False
        rss = sample.get("rss_bytes")
        descriptors = sample.get("fd_count")
        if (
            type(rss) is not int
            or type(descriptors) is not int
            or rss < 0
            or descriptors < 0
        ):
            return False
        samples.append(sample)
    baseline, during, after = samples
    expected = {
        "rss_during_bytes": int(during["rss_bytes"]) - int(baseline["rss_bytes"]),
        "rss_after_bytes": int(after["rss_bytes"]) - int(baseline["rss_bytes"]),
        "fd_during": int(during["fd_count"]) - int(baseline["fd_count"]),
        "fd_after": int(after["fd_count"]) - int(baseline["fd_count"]),
    }
    limits = {
        "rss_during_bytes": RSS_GROWTH_DURING_LIMIT_BYTES,
        "rss_after_bytes": RSS_GROWTH_AFTER_LIMIT_BYTES,
        "fd_during": FD_GROWTH_DURING_LIMIT,
        "fd_after": FD_GROWTH_AFTER_LIMIT,
    }
    return value.get("deltas") == expected and value.get("limits") == limits and all(
        expected[name] <= limit for name, limit in limits.items()
    )


def _fault_state_evidence(report: Mapping[str, Any], value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    classification = report.get("evidence_classification")
    classification = classification if isinstance(classification, Mapping) else {}
    participant = classification.get("media_participant")
    simulated = participant == "stateful_simulator_no_audio_or_network"
    if participant == "attached_existing_media_service":
        return bool(
            value.get("performed") is False
            and value.get("classification")
            == "attached_media_fault_injection_not_performed"
            and value.get("physical_fault_injection") == "pending"
            and value.get("states") == {}
        )
    if not simulated:
        return False
    states = value.get("states")
    if not isinstance(states, Mapping):
        return False
    for kind, expected in SIMULATED_FAULT_EXPECTATIONS.items():
        observation = states.get(kind)
        if not isinstance(observation, Mapping):
            return False
        projection = {name: item for name, item in expected.items() if name != "scope"}
        if not (
            observation.get("expected") == expected
            and observation.get("http_status") == 200
            and observation.get("http") == projection
            and observation.get("sse_status") == 200
            and observation.get("sse") == projection
        ):
            return False
    return bool(
        value.get("performed") is True
        and value.get("classification")
        == "controlled_simulator_injection_not_hardware"
        and value.get("physical_fault_injection") == "pending"
        and set(states) == set(SIMULATED_FAULT_EXPECTATIONS)
    )


def _attached_recording_evidence_is_valid(
    report: Mapping[str, Any], isolation: Mapping[str, Any], value: object
) -> bool:
    if not isinstance(value, Mapping):
        return False
    classification = report.get("evidence_classification")
    if not isinstance(classification, Mapping) or not (
        classification.get("media_participant") == "attached_existing_media_service"
        and classification.get("hardware_tested") is True
        and classification.get("label") == "physical-hardware"
    ):
        return False
    files = isolation.get("recording_current_files")
    regular = isolation.get("recording_current_files_absolute_regular")
    if not (
        isinstance(files, list)
        and len(files) == 3
        and isinstance(files[0], str)
        and bool(files[0])
        and all(item == files[0] for item in files)
        and (Path(files[0]).is_absolute() or files[0].startswith("/"))
        and regular == [True, True, True]
    ):
        return False
    live_file = files[0]
    digest = value.get("sha256")
    duration = value.get("media_duration_seconds")
    decode = value.get("decode")
    if not isinstance(decode, Mapping):
        return False
    tool = decode.get("tool")
    command = [
        tool,
        "-q",
        "filesrc",
        f"location={live_file}",
        "!",
        "flacparse",
        "!",
        "flacdec",
        "!",
        "fakesink",
    ]
    elapsed = decode.get("elapsed_seconds")
    return bool(
        value.get("applicable") is True
        and value.get("mode") == "attached_physical_finalized_flac_decode"
        and value.get("recording_started_by_harness") is True
        and value.get("current_file") == live_file
        and value.get("recording_state_after_cleanup") == "stopped"
        and value.get("last_finalized_file") == live_file
        and value.get("finalized_file_matches_current_file") is True
        and value.get("file_regular") is True
        and type(value.get("file_size_bytes")) is int
        and int(value.get("file_size_bytes", 0)) > 4
        and value.get("header") == "fLaC"
        and value.get("streaminfo_valid") is True
        and type(value.get("sample_rate_hz")) is int
        and int(value.get("sample_rate_hz", 0)) > 0
        and type(value.get("total_samples")) is int
        and int(value.get("total_samples", 0)) > 0
        and type(duration) in (int, float)
        and math.isfinite(float(duration))
        and float(duration) > 0
        and isinstance(digest, str)
        and len(digest) == 64
        and all(character in "0123456789abcdef" for character in digest)
        and isinstance(tool, str)
        and bool(tool)
        and decode.get("command") == command
        and decode.get("timeout_seconds") == RECORDING_DECODE_TIMEOUT_SECONDS
        and decode.get("attempted") is True
        and decode.get("timed_out") is False
        and decode.get("exit_code") == 0
        and decode.get("result") == "eos"
        and decode.get("decode_to_eos") is True
        and type(elapsed) in (int, float)
        and math.isfinite(float(elapsed))
        and 0 <= float(elapsed) <= RECORDING_DECODE_TIMEOUT_SECONDS + 1.0
    )


def assess_report(report: Mapping[str, Any]) -> dict[str, object]:
    scenarios = report.get("scenarios")
    scenarios = scenarios if isinstance(scenarios, Mapping) else {}
    auth = scenarios.get("authentication")
    auth = auth if isinstance(auth, Mapping) else {}
    config = scenarios.get("configuration_and_capabilities")
    config = config if isinstance(config, Mapping) else {}
    isolation = scenarios.get("web_failure_isolation")
    isolation = isolation if isinstance(isolation, Mapping) else {}
    sse = scenarios.get("sse_bounds")
    sse = sse if isinstance(sse, Mapping) else {}
    faults = scenarios.get("fault_state_visibility")
    recording_finalization = scenarios.get("attached_recording_finalization")
    unavailable = scenarios.get("media_unavailable_recovery")
    unavailable = unavailable if isinstance(unavailable, Mapping) else {}
    process = report.get("process_evidence")
    process = process if isinstance(process, Mapping) else {}
    wrong = auth.get("wrong_login_statuses")
    strict_recording_progress = bool(
        isolation.get("recording_counter_supported") is True
        and isolation.get("recording_counter_increased_if_supported") is True
        and isolation.get("recording_counter_sources")
        == ["bytes_written", "bytes_written", "bytes_written"]
    )
    finalized_recording_progress = _attached_recording_evidence_is_valid(
        report, isolation, recording_finalization
    )
    checks = {
        "real_web_subprocess_and_control_socket": bool(
            process.get("web_subprocess_started") is True
            and process.get("web_process_was_killed") is True
            and process.get("web_subprocess_restarted") is True
            and process.get("real_local_control_socket") is True
            and process.get("web_stderr_clean") is True
        ),
        "authentication_throttle_session_csrf_logout": bool(
            auth.get("unauthorized_status") == 401
            and wrong == [401, 401, 401, 401, 401, 429]
            and auth.get("login_status") == 200
            and auth.get("session_status") == 200
            and auth.get("session_authenticated") is True
            and auth.get("csrf_rejection_status") == 403
            and auth.get("csrf_rejection_code") == "csrf_rejected"
            and auth.get("logout_status") == 200
            and auth.get("logout_response_unauthenticated") is True
            and auth.get("old_session_status_after_logout") == 401
            and auth.get("second_login_status") == 200
            and auth.get("rotating_username_count") == 6
            and auth.get("established_session_status_after_throttle") == 200
            and auth.get("established_session_survived_throttle") is True
        ),
        "redaction_validation_and_non_cartesian_gate": bool(
            config.get("config_status") == 200
            and config.get("capability_status") == 200
            and config.get("passphrase_metadata_only") is True
            and config.get("known_simulator_secret_absent") is True
            and config.get("runtime_secret_probe_redacted") is True
            and config.get("malformed_setting_status") == 400
            and config.get("malformed_setting_code") == "invalid_configuration"
            and config.get("non_cartesian_rejection_status") == 400
            and config.get("non_cartesian_rejection_code") == "invalid_configuration"
            and config.get("validation_requests_used_expected_revision") is True
            and config.get("rejected_requests_left_config_unchanged") is True
        ),
        "web_failure_does_not_restart_media": bool(
            isolation.get("states_running_throughout") is True
            and isolation.get("generations_unchanged") is True
            and isolation.get("stream_stat_counter_increased") is True
            and (strict_recording_progress or finalized_recording_progress)
            and isolation.get("sse_numeric_spectrum_before_web_crash") is True
            and isolation.get("spectrum_present_before_web_crash") is True
            and type(isolation.get("crash_lease_wait_seconds")) in (int, float)
            and math.isfinite(float(isolation.get("crash_lease_wait_seconds", 0)))
            and float(isolation.get("crash_lease_wait_seconds", 0))
            > SPECTRUM_LEASE_SECONDS
            and isolation.get("level_meter_present_after_lease_expiry") is True
            and isolation.get("spectrum_absent_after_lease_expiry") is True
            and isolation.get("old_session_endpoint_status") == 200
            and isolation.get("old_session_invalidated") is True
            and isolation.get("relogin_status") == 200
            and isolation.get("post_restart_status") == 200
        ),
        "slow_and_disconnected_sse_clients_are_bounded": bool(
            sse.get("configured_client_limit") == SSE_CLIENT_LIMIT
            and sse.get("slow_clients_held_open") == SSE_CLIENT_LIMIT
            and type(sse.get("slow_clients_unread_seconds")) in (int, float)
            and math.isfinite(float(sse.get("slow_clients_unread_seconds", 0)))
            and float(sse.get("slow_clients_unread_seconds", 0)) >= 10.0
            and sse.get("overload_status") == 503
            and type(sse.get("disconnect_churn_attempts")) is int
            and int(sse.get("disconnect_churn_attempts", 0)) >= 10
            and sse.get("slot_recovered_after_disconnects") is True
            and sse.get("bounded_numeric_telemetry") is True
            and sse.get("no_server_rendered_images") is True
            and _bounded_process_resources(sse.get("process_resources"))
        ),
        "fault_states_are_visible_and_evidence_is_classified": (
            _fault_state_evidence(report, faults)
        ),
        "media_unavailable_and_recovery_are_explicit": bool(
            unavailable.get("unavailable_status") == 503
            and unavailable.get("unavailable_state_explicit") is True
            and unavailable.get("recovery_status") == 200
            and unavailable.get("recovered_available") is True
            and unavailable.get("media_generations_unchanged") is True
        ),
    }
    setup_errors = report.get("setup_errors")
    passed = (
        isinstance(setup_errors, list)
        and not setup_errors
        and all(checks.values())
    )
    return {
        "status": "pass" if passed else "fail",
        "checks": checks,
        "reasons": [name for name, value in checks.items() if not value]
        + (["setup_or_scenario_error"] if setup_errors else []),
    }


def _base_report(args: argparse.Namespace) -> dict[str, Any]:
    simulated = args.control_socket is None
    return {
        "schema_version": SCHEMA_VERSION,
        "spike": SPIKE_NAME,
        "step": 3,
        "production_component": False,
        "evidence_classification": {
            "label": args.evidence_kind,
            "hardware_tested": args.evidence_kind == "physical-hardware",
            "web_service": "real_subprocess",
            "control_protocol": "real_unix_domain_socket",
            "media_participant": (
                "stateful_simulator_no_audio_or_network"
                if simulated
                else "attached_existing_media_service"
            ),
            "claim_limit": (
                "control-plane behavior only; no physical audio or SRT media"
                if simulated
                else "physical media claim requires operator-selected physical-hardware label"
            ),
        },
        "configuration": {
            "attached_control_socket": not simulated,
            "manage_attached_media": bool(args.manage_media),
            "sse_client_limit": SSE_CLIENT_LIMIT,
            "slow_client_seconds": args.slow_client_seconds,
            "recording_decode_tool": args.gst_launch,
            "recording_decode_timeout_seconds": RECORDING_DECODE_TIMEOUT_SECONDS,
        },
        "process_evidence": {
            "web_subprocess_started": False,
            "web_process_was_killed": False,
            "web_subprocess_restarted": False,
            "separate_media_process": True,
            "media_process_started_by_harness": simulated,
            "real_local_control_socket": True,
            "web_stderr_clean": False,
            "web_stderr_issue_codes": [],
        },
        "scenarios": {},
        "setup_errors": [],
        "result": None,
    }


def _safe_text(value: object, redactions: Sequence[str] = ()) -> str:
    text = str(value)
    for secret_value in redactions:
        if secret_value:
            text = text.replace(secret_value, "<redacted>")
    return text.replace("\r", " ").replace("\n", " ")[:2_048]


def _safe_value(value: object, redactions: Sequence[str] = ()) -> object:
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            name = str(key)
            if name.casefold() in {
                "password",
                "passphrase",
                "csrf_token",
                "cookie",
                "bearer_token",
            }:
                result[name] = "[REDACTED]"
            else:
                result[name] = _safe_value(item, redactions)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_value(item, redactions) for item in value]
    if isinstance(value, str):
        return _safe_text(value, redactions)
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    return _safe_text(value, redactions)


def _write_report(
    report: Mapping[str, Any], output: Path | None, redactions: Sequence[str]
) -> None:
    rendered = json.dumps(
        _safe_value(report, redactions), indent=2, sort_keys=True
    ) + "\n"
    if any(secret_value and secret_value in rendered for secret_value in redactions):
        raise RuntimeError("secret redaction failed")
    if output is not None:
        output.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--control-socket",
        type=Path,
        help="attach to an already-running media service instead of the simulator",
    )
    parser.add_argument(
        "--evidence-kind",
        choices=("simulated-control-plane", "physical-hardware"),
        default="simulated-control-plane",
    )
    parser.add_argument(
        "--manage-media",
        action="store_true",
        help="start/stop missing branches on an attached media service",
    )
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--gst-launch", default="gst-launch-1.0")
    parser.add_argument("--startup-timeout", type=float, default=10.0)
    parser.add_argument("--continuity-seconds", type=float, default=2.0)
    parser.add_argument(
        "--slow-client-seconds", type=float, default=SLOW_CLIENT_SECONDS
    )
    parser.add_argument("--output", type=Path)
    return parser


def _validated_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.evidence_kind == "physical-hardware" and args.control_socket is None:
        parser.error("physical-hardware evidence requires --control-socket")
    if not args.gst_launch:
        parser.error("gst-launch tool must not be empty")
    if not all(
        math.isfinite(value)
        for value in (
            args.startup_timeout,
            args.continuity_seconds,
            args.slow_client_seconds,
        )
    ):
        parser.error("durations must be finite")
    if args.startup_timeout < 2 or args.continuity_seconds < 0.2:
        parser.error("startup timeout or continuity duration is too small")
    if args.slow_client_seconds < 10:
        parser.error("slow client duration must be at least 10 seconds")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = _validated_args(argv)
    report = _base_report(args)
    web_password = secrets.token_urlsafe(24)
    media_secret = secrets.token_urlsafe(24) if args.control_socket is None else None
    redactions = tuple(
        item for item in (web_password, media_secret) if isinstance(item, str)
    )
    simulator: subprocess.Popen[str] | None = None
    gate: _ControlGate | None = None
    simulated_fault_path: Path | None = None
    with tempfile.TemporaryDirectory(prefix="tinypirelay-step3-") as directory:
        work = Path(directory)
        try:
            if args.control_socket is None:
                assert media_secret is not None
                media_socket = work / "media.sock"
                config_path = work / "media.json"
                state_path = work / "media-state.json"
                simulated_fault_path = work / "fault-state"
                _set_simulated_fault(simulated_fault_path, None)
                recording = work / "recordings"
                recording.mkdir()
                config_path.write_text(
                    json.dumps(
                        _simulated_config(recording, media_secret),
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                simulator = _start_process(
                    (
                        args.python,
                        "-B",
                        str(Path(__file__).resolve()),
                        "_simulated-media",
                        "--socket",
                        str(media_socket),
                        "--config",
                        str(config_path),
                        "--state",
                        str(state_path),
                        "--fault-state",
                        str(simulated_fault_path),
                    )
                )
            else:
                media_socket = args.control_socket.resolve()
            target = ControlClient(
                media_socket, timeout=min(args.startup_timeout, 5.0)
            )
            _wait_for_control(target, args.startup_timeout, simulator)
            gate = _ControlGate(work / "gate.sock", target)
            gate.start()
            credential = work / "credential.json"
            write_credential_atomic(
                credential, CredentialRecord.create(WEB_USERNAME, web_password)
            )
            _exercise(
                args,
                work,
                target,
                gate,
                credential,
                simulated_fault_path,
                report,
                web_password,
                media_secret,
            )
        except (Exception, KeyboardInterrupt) as exc:
            report["setup_errors"].append(
                f"{type(exc).__name__}: {_safe_text(exc, redactions)}"
            )
        finally:
            if gate is not None:
                gate.stop()
            simulator_error = _stop_process(simulator, redactions=redactions)
            if simulator_error:
                report["process_evidence"]["simulator_stderr_tail"] = simulator_error
        report["result"] = assess_report(report)
        _write_report(report, args.output, redactions)
    return 0 if report["result"]["status"] == "pass" else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "_simulated-media":
        raise SystemExit(_simulated_media_main(sys.argv[2:]))
    raise SystemExit(main())
