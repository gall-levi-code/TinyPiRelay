"""Bounded local control protocol for the independent media service.

The protocol is deliberately small: one UTF-8 JSON request and one JSON response
per local Unix-domain socket connection.  Browser-facing code must talk to the
web service instead of this permission-restricted socket directly.
"""

from __future__ import annotations

import errno
import json
import math
import os
import re
import secrets
import socket
import stat
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any


PROTOCOL_VERSION = 1
# Includes the trailing newline that frames each JSON message.
MAX_MESSAGE_BYTES = 64 * 1024
DEFAULT_TIMEOUT_SECONDS = 15.0
ALLOWED_OPERATIONS = frozenset(
    {
        "status",
        "capabilities",
        "config.get",
        "config.validate",
        "config.update",
        "capture.start",
        "capture.stop",
        "stream.start",
        "stream.stop",
        "stream.set_opus_bitrate",
        "recording.start",
        "recording.stop",
        "monitoring.telemetry",
        "monitoring.spectrum_lease",
        "monitoring.spectrum_release",
        "service.restart_request",
        "storage.list",
        "storage.info",
        "storage.read",
        "storage.delete",
        "storage.check",
        "storage.recover",
    }
)

_REQUEST_KEYS = frozenset(
    {"protocol_version", "request_id", "operation", "arguments"}
)
_RESPONSE_BASE_KEYS = frozenset({"protocol_version", "request_id", "ok"})
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SECRET_KEY_PARTS = frozenset(
    {"value", "passphrase", "password", "hash", "token", "cookie", "secret"}
)
_REDACTED = "[REDACTED]"


class ControlError(Exception):
    """Base class for local control failures."""


class ControlUnavailable(ControlError):
    """The local control service could not be reached in time."""


class ControlProtocolError(ControlError):
    """A message did not conform to the stable control protocol."""


class ControlOperationError(ControlError):
    """The service understood a request but rejected the operation."""

    def __init__(self, code: str, message: str, request_id: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.request_id = request_id


class ControlRequestError(ControlError):
    """A handler's explicitly safe, client-visible operation rejection."""

    def __init__(self, code: str, message: str) -> None:
        _validate_error(code, message)
        super().__init__(message)
        self.code = code
        self.message = message


Handler = Callable[[str, dict[str, Any]], Any]
Connector = Callable[[Path, float], socket.socket]


def redact_secrets(value: Any) -> Any:
    """Return a JSON-shaped copy with recursively named secret fields redacted."""

    return _redact(value, set(), 0)


def encode_request(
    request_id: str,
    operation: str,
    arguments: Mapping[str, Any] | None = None,
    *,
    max_message_bytes: int = MAX_MESSAGE_BYTES,
) -> bytes:
    """Validate and encode one request, including its newline delimiter."""

    if arguments is None:
        argument_object: dict[str, Any] = {}
    elif isinstance(arguments, Mapping):
        argument_object = dict(arguments)
    else:
        raise ControlProtocolError("arguments must be an object")
    request = {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request_id,
        "operation": operation,
        "arguments": argument_object,
    }
    _validate_request(request)
    return _encode_line(request, max_message_bytes)


def decode_request(
    payload: bytes,
    *,
    max_message_bytes: int = MAX_MESSAGE_BYTES,
) -> dict[str, Any]:
    """Decode and strictly validate a request payload without its delimiter."""

    _validate_payload_size(payload, max_message_bytes)
    value = _decode_json(payload)
    _validate_request(value)
    return value


def encode_response(
    request_id: str,
    *,
    result: Any = None,
    error: Mapping[str, Any] | None = None,
    max_message_bytes: int = MAX_MESSAGE_BYTES,
) -> bytes:
    """Encode a response, redacting any handler-returned secret fields."""

    _validate_response_request_id(request_id)
    if error is None:
        response = {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": request_id,
            "ok": True,
            "result": redact_secrets(result),
        }
    else:
        if set(error) != {"code", "message"}:
            raise ControlProtocolError("error must contain exactly code and message")
        code = error["code"]
        message = error["message"]
        _validate_error(code, message)
        response = {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": request_id,
            "ok": False,
            "error": {"code": code, "message": message},
        }
    return _encode_line(response, max_message_bytes)


def decode_response(
    payload: bytes,
    *,
    max_message_bytes: int = MAX_MESSAGE_BYTES,
) -> dict[str, Any]:
    """Decode, validate, and defensively redact a response payload."""

    _validate_payload_size(payload, max_message_bytes)
    value = _decode_json(payload)
    _validate_response(value)
    return redact_secrets(value)


class ControlClient:
    """Timeout-bounded client for the one-request-per-connection protocol."""

    def __init__(
        self,
        socket_path: str | os.PathLike[str],
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_message_bytes: int = MAX_MESSAGE_BYTES,
        connector: Connector | None = None,
    ) -> None:
        self.socket_path = Path(os.path.abspath(os.fspath(socket_path)))
        self.timeout = _positive_timeout(timeout)
        self.max_message_bytes = _positive_size(max_message_bytes)
        self._connector = connector or _connect_unix

    def request(
        self,
        operation: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        request_id: str | None = None,
    ) -> Any:
        """Perform one request or raise an unavailable/protocol/operation error."""

        identifier = request_id or secrets.token_hex(16)
        encoded = encode_request(
            identifier,
            operation,
            arguments,
            max_message_bytes=self.max_message_bytes,
        )
        try:
            connection = self._connector(self.socket_path, self.timeout)
        except (OSError, TimeoutError) as exc:
            raise ControlUnavailable("local control service is unavailable") from exc

        try:
            connection.settimeout(self.timeout)
            try:
                connection.sendall(encoded)
                connection.shutdown(socket.SHUT_WR)
                payload = _read_line(
                    connection,
                    self.max_message_bytes,
                    empty_is_unavailable=True,
                )
            except ControlError:
                raise
            except (OSError, TimeoutError) as exc:
                raise ControlUnavailable(
                    "local control request did not complete"
                ) from exc
        finally:
            connection.close()

        response = decode_response(
            payload,
            max_message_bytes=self.max_message_bytes,
        )
        if response["request_id"] != identifier:
            raise ControlProtocolError("response request_id does not match the request")
        if not response["ok"]:
            error = response["error"]
            raise ControlOperationError(error["code"], error["message"], identifier)
        return response["result"]


class UnixControlServer:
    """A small, bounded AF_UNIX server with explicit filesystem permissions."""

    def __init__(
        self,
        socket_path: str | os.PathLike[str],
        handler: Handler,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_message_bytes: int = MAX_MESSAGE_BYTES,
        max_clients: int = 8,
        directory_mode: int = 0o700,
        socket_mode: int = 0o600,
    ) -> None:
        if not callable(handler):
            raise TypeError("handler must be callable")
        self.socket_path = Path(os.path.abspath(os.fspath(socket_path)))
        self.handler = handler
        self.timeout = _positive_timeout(timeout)
        self.max_message_bytes = _positive_size(max_message_bytes)
        if type(max_clients) is not int or max_clients < 1 or max_clients > 128:
            raise ValueError("max_clients must be an integer between 1 and 128")
        self.max_clients = max_clients
        self.directory_mode = _directory_mode(directory_mode)
        self.socket_mode = _socket_mode(socket_mode)

        self._slots = threading.BoundedSemaphore(max_clients)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._connections: set[socket.socket] = set()
        self._workers: set[threading.Thread] = set()
        self._bound_identity: tuple[int, int] | None = None

    @property
    def is_running(self) -> bool:
        thread = self._accept_thread
        return thread is not None and thread.is_alive() and not self._stop.is_set()

    @property
    def active_clients(self) -> int:
        with self._lock:
            return len(self._connections)

    def start(self) -> "UnixControlServer":
        """Bind the socket and start accepting clients in a daemon thread."""

        with self._lock:
            if self._stop.is_set():
                raise RuntimeError("a closed control server cannot be restarted")
            if self._listener is not None or self._accept_thread is not None:
                raise RuntimeError("control server has already been started")
        listener = self._open_listener()
        thread = threading.Thread(
            target=self._accept_loop,
            name="tinypirelay-control",
            daemon=True,
        )
        with self._lock:
            self._listener = listener
            self._accept_thread = thread
        try:
            thread.start()
        except BaseException:
            with self._lock:
                self._listener = None
                self._accept_thread = None
            listener.close()
            self._cleanup_bound_socket()
            raise
        return self

    def close(self) -> None:
        """Stop accepting, unblock active clients, and remove only our socket."""

        self._stop.set()
        with self._lock:
            listener = self._listener
            accept_thread = self._accept_thread
            self._listener = None
        if listener is not None:
            listener.close()
        current = threading.current_thread()
        if accept_thread is not None and accept_thread is not current:
            accept_thread.join(timeout=max(1.0, self.timeout + 0.5))
        with self._lock:
            connections = tuple(self._connections)
            workers = tuple(self._workers)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        for worker in workers:
            if worker is not current:
                worker.join(timeout=max(1.0, self.timeout + 0.5))
        with self._lock:
            self._accept_thread = None
            self._workers.clear()
            self._connections.clear()
        self._cleanup_bound_socket()

    def __enter__(self) -> "UnixControlServer":
        return self.start()

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()

    def _open_listener(self) -> socket.socket:
        family = getattr(socket, "AF_UNIX", None)
        if family is None:
            raise ControlUnavailable("AF_UNIX is unavailable on this platform")
        self._prepare_directory()
        self._remove_stale_socket(family)
        listener = socket.socket(family, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.socket_path))
            socket_stat = os.lstat(self.socket_path)
            self._bound_identity = (socket_stat.st_dev, socket_stat.st_ino)
            if os.name == "posix":
                os.chmod(self.socket_path, self.socket_mode)
            listener.listen(self.max_clients)
            listener.settimeout(0.2)
        except BaseException:
            listener.close()
            self._cleanup_bound_socket()
            raise
        return listener

    def _prepare_directory(self) -> None:
        parent = self.socket_path.parent
        try:
            try:
                parent_stat = os.lstat(parent)
            except FileNotFoundError:
                parent.mkdir(mode=self.directory_mode, parents=False)
                if os.name == "posix":
                    _set_created_directory_mode(parent, self.directory_mode)
                return
            if not stat.S_ISDIR(parent_stat.st_mode):
                raise ControlUnavailable("control socket parent is not a directory")
            if os.name == "posix":
                actual_mode = stat.S_IMODE(parent_stat.st_mode) & 0o777
                if actual_mode != self.directory_mode:
                    raise ControlUnavailable(
                        "control socket parent permissions do not match directory_mode"
                    )
                access_kwargs = (
                    {"effective_ids": True}
                    if os.access in os.supports_effective_ids
                    else {}
                )
                if not os.access(parent, os.W_OK | os.X_OK, **access_kwargs):
                    raise ControlUnavailable(
                        "control socket parent is not writable and searchable"
                    )
        except ControlUnavailable:
            raise
        except OSError as exc:
            raise ControlUnavailable("cannot prepare control socket directory") from exc

    def _remove_stale_socket(self, family: int) -> None:
        try:
            existing = os.lstat(self.socket_path)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ControlUnavailable("cannot inspect control socket path") from exc
        if not stat.S_ISSOCK(existing.st_mode):
            raise ControlUnavailable("control socket path is occupied by a non-socket")

        probe = socket.socket(family, socket.SOCK_STREAM)
        probe.settimeout(min(self.timeout, 0.25))
        try:
            probe.connect(str(self.socket_path))
        except OSError as exc:
            if exc.errno not in (errno.ECONNREFUSED, errno.ENOENT):
                raise ControlUnavailable(
                    "cannot safely replace control socket"
                ) from exc
        else:
            raise ControlUnavailable("control socket is already in use")
        finally:
            probe.close()

        try:
            current = os.lstat(self.socket_path)
            if (current.st_dev, current.st_ino) != (existing.st_dev, existing.st_ino):
                raise ControlUnavailable("control socket changed during stale check")
            if not stat.S_ISSOCK(current.st_mode):
                raise ControlUnavailable("control socket changed during stale check")
            self.socket_path.unlink()
        except FileNotFoundError:
            pass
        except ControlUnavailable:
            raise
        except OSError as exc:
            raise ControlUnavailable("cannot remove stale control socket") from exc

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                listener = self._listener
            if listener is None:
                break
            try:
                connection, _address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                self._stop.wait(0.05)
                continue
            if self._stop.is_set():
                connection.close()
                break
            connection.settimeout(self.timeout)
            if not self._slots.acquire(blocking=False):
                connection.close()
                continue
            worker = threading.Thread(
                target=self._client_worker,
                args=(connection,),
                name="tinypirelay-control-client",
                daemon=True,
            )
            with self._lock:
                self._connections.add(connection)
                self._workers.add(worker)
            try:
                worker.start()
            except BaseException:
                with self._lock:
                    self._connections.discard(connection)
                    self._workers.discard(worker)
                self._slots.release()
                connection.close()

    def _client_worker(self, connection: socket.socket) -> None:
        try:
            self._handle_connection(connection)
        finally:
            connection.close()
            with self._lock:
                self._connections.discard(connection)
                self._workers.discard(threading.current_thread())
            self._slots.release()

    def _handle_connection(self, connection: socket.socket) -> None:
        request_id = ""
        try:
            payload = _read_line(
                connection,
                self.max_message_bytes,
                empty_is_unavailable=False,
            )
            request = decode_request(
                payload,
                max_message_bytes=self.max_message_bytes,
            )
            request_id = request["request_id"]
            try:
                result = self.handler(request["operation"], request["arguments"])
            except ControlRequestError as exc:
                response = encode_response(
                    request_id,
                    error={"code": exc.code, "message": exc.message},
                    max_message_bytes=self.max_message_bytes,
                )
            except Exception:
                response = encode_response(
                    request_id,
                    error={
                        "code": "internal_error",
                        "message": "control operation failed",
                    },
                    max_message_bytes=self.max_message_bytes,
                )
            else:
                try:
                    response = encode_response(
                        request_id,
                        result=result,
                        max_message_bytes=self.max_message_bytes,
                    )
                except (ControlProtocolError, TypeError, ValueError, RecursionError):
                    response = encode_response(
                        request_id,
                        error={
                            "code": "internal_error",
                            "message": "control operation returned an invalid result",
                        },
                        max_message_bytes=self.max_message_bytes,
                    )
        except ControlProtocolError:
            response = encode_response(
                request_id,
                error={"code": "bad_request", "message": "invalid control request"},
                max_message_bytes=self.max_message_bytes,
            )
        except ControlUnavailable:
            return
        try:
            connection.sendall(response)
        except (OSError, TimeoutError):
            return

    def _cleanup_bound_socket(self) -> None:
        identity = self._bound_identity
        self._bound_identity = None
        if identity is None:
            return
        try:
            current = os.lstat(self.socket_path)
            if (
                (current.st_dev, current.st_ino) == identity
                and stat.S_ISSOCK(current.st_mode)
            ):
                self.socket_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _connect_unix(path: Path, timeout: float) -> socket.socket:
    family = getattr(socket, "AF_UNIX", None)
    if family is None:
        raise OSError("AF_UNIX is unavailable")
    connection = socket.socket(family, socket.SOCK_STREAM)
    connection.settimeout(timeout)
    try:
        connection.connect(str(path))
    except BaseException:
        connection.close()
        raise
    return connection


def _read_line(
    connection: socket.socket,
    max_message_bytes: int,
    *,
    empty_is_unavailable: bool,
) -> bytes:
    data = bytearray()
    while True:
        try:
            chunk = connection.recv(min(4096, max_message_bytes + 2))
        except socket.timeout as exc:
            raise ControlUnavailable("control message timed out") from exc
        except OSError as exc:
            raise ControlUnavailable("control connection failed") from exc
        if not chunk:
            if not data and empty_is_unavailable:
                raise ControlUnavailable("control service closed without a response")
            raise ControlProtocolError("control message is not newline terminated")
        newline = chunk.find(b"\n")
        if newline >= 0:
            data.extend(chunk[:newline])
            if len(data) + 1 > max_message_bytes:
                raise ControlProtocolError("control message exceeds the size limit")
            if chunk[newline + 1 :]:
                raise ControlProtocolError("connection contains more than one message")
            return bytes(data)
        data.extend(chunk)
        if len(data) + 1 > max_message_bytes:
            raise ControlProtocolError("control message exceeds the size limit")


def _encode_line(value: Any, max_message_bytes: int) -> bytes:
    limit = _positive_size(max_message_bytes)
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (OverflowError, TypeError, ValueError) as exc:
        raise ControlProtocolError("control message is not valid JSON data") from exc
    if len(payload) + 1 > limit:
        raise ControlProtocolError("control message exceeds the size limit")
    return payload + b"\n"


def _decode_json(payload: bytes) -> Any:
    if not isinstance(payload, bytes):
        raise ControlProtocolError("control message must be bytes")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ControlProtocolError("control message is not valid UTF-8") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except ControlProtocolError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ControlProtocolError("control message is not valid JSON") from exc


def _validate_payload_size(payload: Any, max_message_bytes: int) -> None:
    limit = _positive_size(max_message_bytes)
    if not isinstance(payload, bytes):
        raise ControlProtocolError("control message must be bytes")
    if len(payload) + 1 > limit:
        raise ControlProtocolError("control message exceeds the size limit")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ControlProtocolError("control message contains a duplicate key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ControlProtocolError("control message contains a non-finite number")


def _validate_request(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != _REQUEST_KEYS:
        raise ControlProtocolError("request has an invalid shape")
    if type(value["protocol_version"]) is not int:
        raise ControlProtocolError("protocol_version must be an integer")
    if value["protocol_version"] != PROTOCOL_VERSION:
        raise ControlProtocolError("protocol_version is unsupported")
    _validate_request_id(value["request_id"])
    operation = value["operation"]
    if not isinstance(operation, str) or operation not in ALLOWED_OPERATIONS:
        raise ControlProtocolError("operation is not allowed")
    if not isinstance(value["arguments"], dict):
        raise ControlProtocolError("arguments must be an object")


def _validate_response(value: Any) -> None:
    if not isinstance(value, dict):
        raise ControlProtocolError("response must be an object")
    if type(value.get("protocol_version")) is not int:
        raise ControlProtocolError("protocol_version must be an integer")
    if value["protocol_version"] != PROTOCOL_VERSION:
        raise ControlProtocolError("protocol_version is unsupported")
    _validate_response_request_id(value.get("request_id"))
    if type(value.get("ok")) is not bool:
        raise ControlProtocolError("response ok must be a boolean")
    expected = _RESPONSE_BASE_KEYS | ({"result"} if value["ok"] else {"error"})
    if set(value) != expected:
        raise ControlProtocolError("response has an invalid shape")
    if not value["ok"]:
        error = value["error"]
        if not isinstance(error, dict) or set(error) != {"code", "message"}:
            raise ControlProtocolError("response error has an invalid shape")
        _validate_error(error["code"], error["message"])


def _validate_request_id(value: Any) -> None:
    if not isinstance(value, str) or _REQUEST_ID_RE.fullmatch(value) is None:
        raise ControlProtocolError("request_id is invalid")


def _validate_response_request_id(value: Any) -> None:
    if value == "":
        return
    _validate_request_id(value)


def _validate_error(code: Any, message: Any) -> None:
    if not isinstance(code, str) or _ERROR_CODE_RE.fullmatch(code) is None:
        raise ControlProtocolError("error code is invalid")
    if (
        not isinstance(message, str)
        or not message
        or len(message) > 512
        or any(ord(character) < 32 for character in message)
    ):
        raise ControlProtocolError("error message is invalid")


def _redact(value: Any, seen: set[int], depth: int) -> Any:
    if depth > 32:
        return _REDACTED
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in seen:
            return _REDACTED
        seen.add(identity)
        try:
            result: dict[Any, Any] = {}
            for key, item in value.items():
                if isinstance(key, str) and _is_secret_key(key):
                    if key == "passphrase" and _is_public_passphrase_metadata(item):
                        result[key] = {
                            "configured": item["configured"],
                            "action": "keep",
                        }
                    else:
                        result[key] = _REDACTED
                else:
                    result[key] = _redact(item, seen, depth + 1)
            return result
        finally:
            seen.remove(identity)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in seen:
            return _REDACTED
        seen.add(identity)
        try:
            return [_redact(item, seen, depth + 1) for item in value]
        finally:
            seen.remove(identity)
    return value


def _is_secret_key(key: str) -> bool:
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    parts = re.split(r"[^a-z0-9]+", separated.casefold())
    return any(part in _SECRET_KEY_PARTS for part in parts)


def _is_public_passphrase_metadata(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value) == {"configured", "action"}
        and type(value["configured"]) is bool
        and value["action"] == "keep"
    )


def _set_created_directory_mode(path: Path, mode: int) -> None:
    """Set a just-created directory's mode without following a replacement link."""

    created = os.lstat(path)
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (created.st_dev, created.st_ino)
        ):
            raise ControlUnavailable(
                "control socket parent changed while it was being created"
            )
        os.fchmod(descriptor, mode)
        if stat.S_IMODE(os.fstat(descriptor).st_mode) & 0o777 != mode:
            raise ControlUnavailable(
                "control socket parent permissions could not be restricted"
            )
    finally:
        os.close(descriptor)


def _positive_timeout(value: Any) -> float:
    if type(value) not in (int, float):
        raise ValueError("timeout must be a positive finite number")
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive finite number")
    return timeout


def _positive_size(value: Any) -> int:
    if type(value) is not int or value < 256 or value > 16 * 1024 * 1024:
        raise ValueError("message size must be between 256 and 16777216 bytes")
    return value


def _directory_mode(value: Any) -> int:
    if (
        type(value) is not int
        or value < 0
        or value > 0o777
        or value & 0o007
        or value & 0o700 != 0o700
    ):
        raise ValueError("directory_mode must grant owner rwx and no access to others")
    return value


def _socket_mode(value: Any) -> int:
    if (
        type(value) is not int
        or value < 0
        or value > 0o777
        or value & 0o007
        or value & 0o600 != 0o600
    ):
        raise ValueError("socket_mode must grant owner rw and no access to others")
    return value
