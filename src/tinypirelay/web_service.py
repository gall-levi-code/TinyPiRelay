"""Authenticated, bounded Step 3 web control plane for TinyPiRelay."""

from __future__ import annotations

import argparse
import base64
import getpass
import http.cookies
import http.server
import io
import ipaddress
import json
import logging
import math
import os
import re
import signal
import socket
import stat
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import parse_qs, quote, urlsplit

from .control_protocol import (
    ControlClient,
    ControlOperationError,
    ControlProtocolError,
    ControlUnavailable,
    redact_secrets,
)
from .web_security import (
    SESSION_COOKIE_NAME,
    CredentialRecord,
    LoginThrottle,
    SecurityError,
    SessionStore,
    build_session_cookie,
    clear_session_cookie,
    validate_new_password,
    verify_credentials,
)


LOGGER = logging.getLogger("tinypirelay.web")
ASSET_DIRECTORY = Path(__file__).with_name("web_assets")
MAX_REQUEST_BODY_BYTES = 32 * 1024
MAX_HTTP_CLIENTS = 16
MAX_SSE_CLIENTS = 4
MAX_CONCURRENT_PASSWORD_CHECKS = 2
REQUEST_TIMEOUT_SECONDS = 10.0
SSE_INTERVAL_SECONDS = 0.2
MAX_TELEMETRY_UPDATES_PER_SECOND = 60.0
SPECTRUM_LEASE_RENEW_SECONDS = 2.0
CSRF_COOKIE_NAME = "tinypirelay_csrf"
STATIC_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
API_ROUTES = frozenset(
    {
        "/api/login",
        "/api/setup",
        "/api/logout",
        "/api/session",
        "/api/status",
        "/api/capabilities",
        "/api/config",
        "/api/control",
        "/api/events",
        "/api/storage",
        "/api/storage/file",
        "/api/storage/action",
        "/api/maintenance",
        "/api/account/password",
    }
)
CONTROL_ACTIONS = {
    "capture.start": ("capture.start", False),
    "capture.stop": ("capture.stop", False),
    "stream.start": ("stream.start", False),
    "stream.stop": ("stream.stop", False),
    "stream.set_opus_bitrate": ("stream.set_opus_bitrate", True),
    "recording.start": ("recording.start", False),
    "recording.stop": ("recording.stop", False),
    "service.restart_request": ("service.restart_request", False),
}


class WebInputError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class CredentialCommitUncertain(OSError):
    """The credential was replaced, but directory durability is unconfirmed."""

    def __init__(self) -> None:
        super().__init__(
            "credential was replaced but directory durability could not be confirmed"
        )


def _is_client_disconnect(exc: OSError) -> bool:
    """Classify response-path OS errors as closed or unusable connections."""

    # Callers invoke this only around socket/header/body writes.  Limiting the
    # catch sites keeps filesystem and application errors visible while all
    # platform-specific connection OSErrors close quietly.
    return isinstance(exc, OSError)


class WebApplication:
    """Thread-safe dependencies and limits shared by request handlers."""

    def __init__(
        self,
        credential: CredentialRecord | None,
        control_client: object,
        *,
        sessions: SessionStore | None = None,
        throttle: LoginThrottle | None = None,
        secure_cookie: bool = False,
        assets: str | os.PathLike[str] = ASSET_DIRECTORY,
        max_sse_clients: int = MAX_SSE_CLIENTS,
        max_password_checks: int = MAX_CONCURRENT_PASSWORD_CHECKS,
        sse_interval_seconds: float = SSE_INTERVAL_SECONDS,
        sse_event_limit: int | None = None,
        credential_path: str | os.PathLike[str] | None = None,
        setup_pending_path: str | os.PathLike[str] | None = None,
        lan_only: bool = False,
        maintenance_client: object | None = None,
    ) -> None:
        if credential is not None and not isinstance(credential, CredentialRecord):
            raise TypeError("credential must be a CredentialRecord")
        if credential is None and (
            credential_path is None or setup_pending_path is None
            or load_web_credential(credential_path, setup_pending_path) is not None
        ):
            raise SecurityError("first-time setup is not enabled")
        if not callable(getattr(control_client, "request", None)):
            raise TypeError("control_client must provide request()")
        if type(secure_cookie) is not bool:
            raise ValueError("secure_cookie must be a boolean")
        if type(max_sse_clients) is not int or not 1 <= max_sse_clients <= 64:
            raise ValueError("max_sse_clients must be between 1 and 64")
        if type(max_password_checks) is not int or not 1 <= max_password_checks <= 8:
            raise ValueError("max_password_checks must be between 1 and 8")
        if (
            type(sse_interval_seconds) not in (int, float)
            or not math.isfinite(sse_interval_seconds)
            or sse_interval_seconds <= 0
        ):
            raise ValueError("sse_interval_seconds must be positive and finite")
        if sse_event_limit is not None and (
            type(sse_event_limit) is not int or sse_event_limit < 1
        ):
            raise ValueError("sse_event_limit must be a positive integer")
        self.credential = credential
        self.credential_path = Path(credential_path) if credential_path is not None else None
        self.setup_pending_path = Path(setup_pending_path) if setup_pending_path is not None else None
        self.lan_only = lan_only
        hostname = socket.gethostname().lower().rstrip(".")
        self.allowed_hostnames = {"localhost", hostname, hostname.split(".")[0] + ".local"}
        self.credential_lock = threading.RLock()
        self.maintenance = maintenance_client
        self.download_slots = threading.BoundedSemaphore(2)
        self.control = control_client
        self.sessions = sessions or SessionStore()
        self.throttle = throttle or LoginThrottle()
        self.secure_cookie = secure_cookie
        self.assets = Path(assets)
        self.sse_slots = threading.BoundedSemaphore(max_sse_clients)
        self._sse_lease_lock = threading.Lock()
        self._sse_clients = 0
        self._spectrum_renew_at = 0.0
        self.password_slots = threading.BoundedSemaphore(max_password_checks)
        self.sse_interval_seconds = float(sse_interval_seconds)
        self.sse_event_limit = sse_event_limit

    def telemetry_interval_seconds(self, status: object) -> float:
        rate = status.get("monitoring_updates_per_second") if isinstance(status, dict) else None
        if type(rate) not in (int, float) or not math.isfinite(rate) or rate <= 0:
            return self.sse_interval_seconds
        return max(
            1.0 / MAX_TELEMETRY_UPDATES_PER_SECOND,
            min(self.sse_interval_seconds, 1.0 / rate),
        )

    def media_request(
        self, operation: str, arguments: Mapping[str, Any] | None = None
    ) -> Any:
        try:
            return redact_secrets(self.control.request(operation, arguments or {}))
        except ControlOperationError as exc:
            status = 409 if exc.code in {
                "busy",
                "confirmation_required",
                "recording_active",
                "reconfigure_required",
                "revision_conflict",
                "service_shutting_down",
                "file_changed",
                "stale_file",
                "active_recording",
                "recording_busy",
                "check_in_progress",
            } else 400
            raise WebInputError(exc.code, exc.message, status) from exc
        except ControlUnavailable as exc:
            raise WebInputError(
                "media_unavailable", "media service is unavailable", 503
            ) from exc
        except ControlProtocolError as exc:
            raise WebInputError(
                "media_protocol_error", "media service response is invalid", 502
            ) from exc

    def verify_password(self, address: str, username: str, password: object) -> None:
        """Shared, bounded reauthentication; caller holds credential_lock."""
        decision = self.throttle.admit(address, username)
        if not decision.allowed:
            raise WebInputError("rate_limited", "too many password attempts; try again later", 429)
        if not self.password_slots.acquire(blocking=False):
            raise WebInputError("authentication_busy", "authentication is temporarily busy", 503)
        try:
            verified = verify_credentials(self.credential, username, password)
        finally:
            self.password_slots.release()
        if not verified:
            raise WebInputError("invalid_login", "current password is invalid", 401)
        self.throttle.reset(address, username)

    def maintenance_request(self, action: str) -> dict[str, object]:
        from .maintenance import MaintenanceError

        if self.maintenance is None:
            raise WebInputError("maintenance_unavailable", "maintenance service is unavailable", 503)
        try:
            return self.maintenance.request(action)
        except MaintenanceError as exc:
            raise WebInputError(exc.code, exc.message, 409 if exc.code == "busy" else 503) from exc

    def require_maintenance_idle(self) -> None:
        if self.maintenance is not None:
            try:
                status = self.maintenance_request("status")
            except WebInputError:
                return  # Ordinary media control does not depend on an optional helper.
            if status.get("busy"):
                raise WebInputError("busy", "maintenance is in progress; wait for reconnection", 409)

    @property
    def active_sse_clients(self) -> int:
        with self._sse_lease_lock:
            return self._sse_clients

    def acquire_sse_client(self) -> bool:
        if not self.sse_slots.acquire(blocking=False):
            return False
        with self._sse_lease_lock:
            self._sse_clients += 1
            if self._sse_clients == 1:
                self._renew_spectrum_locked(force=True)
        return True

    def renew_spectrum_lease(self) -> None:
        with self._sse_lease_lock:
            if self._sse_clients:
                self._renew_spectrum_locked(force=False)

    def release_sse_client(self) -> None:
        with self._sse_lease_lock:
            if self._sse_clients < 1:
                raise RuntimeError("SSE client count underflow")
            self._sse_clients -= 1
            if self._sse_clients == 0:
                self._spectrum_renew_at = 0.0
                try:
                    self.media_request("monitoring.spectrum_release")
                except WebInputError:
                    pass
        self.sse_slots.release()

    def _renew_spectrum_locked(self, *, force: bool) -> None:
        now = time.monotonic()
        if not force and now < self._spectrum_renew_at:
            return
        try:
            self.media_request("monitoring.spectrum_lease")
        except WebInputError:
            pass
        self._spectrum_renew_at = time.monotonic() + SPECTRUM_LEASE_RENEW_SECONDS


class BoundedThreadingHTTPServer(http.server.ThreadingHTTPServer):
    """Thread-per-request HTTP with a hard concurrent-client bound."""

    daemon_threads = True
    block_on_close = True
    allow_reuse_address = True
    request_queue_size = 32

    def __init__(
        self,
        server_address: tuple[str, int],
        application: WebApplication,
        *,
        max_clients: int = MAX_HTTP_CLIENTS,
    ) -> None:
        if type(max_clients) is not int or not 1 <= max_clients <= 128:
            raise ValueError("max_clients must be between 1 and 128")
        self.application = application
        self.stop_event = threading.Event()
        self._client_slots = threading.BoundedSemaphore(max_clients)
        self._active_lock = threading.Lock()
        self._active_clients = 0
        super().__init__(server_address, TinyPiRelayRequestHandler)

    @property
    def active_clients(self) -> int:
        with self._active_lock:
            return self._active_clients

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        if not self._client_slots.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Connection: close\r\nContent-Length: 0\r\n\r\n"
                )
            except OSError:
                pass
            request.close()
            return
        with self._active_lock:
            self._active_clients += 1
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._release_client()
            raise

    def process_request_thread(self, request: socket.socket, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release_client()

    def _release_client(self) -> None:
        with self._active_lock:
            self._active_clients -= 1
        self._client_slots.release()

    def shutdown(self) -> None:
        self.stop_event.set()
        super().shutdown()


class TinyPiRelayRequestHandler(http.server.BaseHTTPRequestHandler):
    """Explicit routes only; no filesystem traversal or template rendering."""

    protocol_version = "HTTP/1.1"
    server_version = "TinyPiRelay"
    sys_version = ""

    @property
    def application(self) -> WebApplication:
        return self.server.application  # type: ignore[attr-defined,no-any-return]

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(REQUEST_TIMEOUT_SECONDS)

    def handle(self) -> None:
        try:
            super().handle()
        except (ConnectionError, TimeoutError):
            self.close_connection = True

    def parse_request(self) -> bool:
        if not super().parse_request():
            return False
        hosts = self.headers.get_all("Host") or []
        try:
            host, _port = _http_authority(hosts[0]) if len(hosts) == 1 else ("", 0)
            local = ipaddress.ip_address(self.connection.getsockname()[0])
            try:
                address = ipaddress.ip_address(host)
                allowed = address == local or address.is_loopback
            except ValueError:
                allowed = host in self.application.allowed_hostnames
            if not allowed:
                raise ValueError("unrecognized host")
            if self.application.lan_only and not _local_network_peer(self.client_address[0]):
                raise ValueError("non-LAN peer")
        except (ValueError, OSError):
            self.close_connection = True
            self._error(403, "access_rejected", "use this device's local hostname or address")
            return False
        return True

    def do_GET(self) -> None:
        if not self._require_empty_body():
            return
        path = self._path()
        if path in STATIC_ASSETS:
            self._serve_asset(path)
            return
        if path == "/api/session":
            self._session_status()
            return
        if path == "/api/events":
            self._events()
            return
        if path in {"/api/storage", "/api/storage/file", "/api/maintenance"}:
            if self._require_auth() is None:
                return
            try:
                if path == "/api/storage/file":
                    self._storage_file()
                    return
                if path == "/api/storage":
                    query = self._query({"page"})
                    page = query.get("page", "1")
                    if re.fullmatch(r"[1-9][0-9]{0,5}", page) is None:
                        raise WebInputError("invalid_request", "page must be a positive integer")
                    result = self.application.media_request("storage.list", {"page": int(page)})
                else:
                    try:
                        result = self.application.maintenance_request("status")
                    except WebInputError as exc:
                        result = {"available": False, "busy": False, "reason": exc.message}
                self._json(200, result)
            except WebInputError as exc:
                self._error(exc.status, exc.code, exc.message)
            return
        if path in {"/api/status", "/api/capabilities", "/api/config"}:
            if self._require_auth() is None:
                return
            operation = {
                "/api/status": "status",
                "/api/capabilities": "capabilities",
                "/api/config": "config.get",
            }[path]
            self._media_json(operation)
            return
        self._error(404, "not_found", "resource was not found")

    def do_HEAD(self) -> None:
        if not self._require_empty_body():
            return
        if self._path() != "/api/storage/file":
            self._error(404, "not_found", "resource was not found")
            return
        if self._require_auth() is None:
            return
        try:
            self._storage_file(head=True)
        except WebInputError as exc:
            self._error(exc.status, exc.code, exc.message)

    def do_POST(self) -> None:
        path = self._path()
        if path in {"/api/login", "/api/setup"}:
            if self._require_same_origin():
                self._setup_account() if path == "/api/setup" else self._login()
            return
        auth = self._require_auth(csrf=True)
        if auth is None:
            return
        if path in {"/api/storage/action", "/api/account/password", "/api/maintenance"}:
            try:
                body = self._json_body()
                if path == "/api/storage/action":
                    if not isinstance(body, dict) or set(body) != {"action", "file_id"}:
                        raise WebInputError("invalid_request", "storage request is malformed")
                    if body["action"] not in ("delete", "check", "recover"):
                        raise WebInputError("invalid_request", "storage action is unsupported")
                    result = self.application.media_request(
                        "storage." + body["action"], {"file_id": body["file_id"]}
                    )
                    self._json(200, result)
                else:
                    self._account_or_maintenance(path, body, auth)
            except (SecurityError, WebInputError) as exc:
                if isinstance(exc, WebInputError):
                    self._error(exc.status, exc.code, exc.message)
                else:
                    self._error(400, "invalid_password", str(exc))
            return
        if path == "/api/logout":
            if not self._require_empty_body():
                return
            bearer, _principal = auth
            self.application.sessions.logout(bearer)
            self._json(
                200,
                {"authenticated": False},
                cookies=(
                    clear_session_cookie(secure=self.application.secure_cookie),
                    _clear_csrf_cookie(secure=self.application.secure_cookie),
                ),
            )
            return
        if path == "/api/control":
            try:
                body = self._json_body()
                if not isinstance(body, dict) or set(body) not in (
                    {"action"},
                    {"action", "value"},
                ):
                    raise WebInputError("invalid_request", "control request is malformed")
                action = body.get("action")
                if not isinstance(action, str) or action not in CONTROL_ACTIONS:
                    raise WebInputError("invalid_request", "control action is unsupported")
                if not action.endswith(".stop"):
                    self.application.require_maintenance_idle()
                operation, needs_value = CONTROL_ACTIONS[action]
                if needs_value:
                    if set(body) != {"action", "value"} or type(body["value"]) is not int:
                        raise WebInputError(
                            "invalid_request", "control action requires an integer value"
                        )
                    arguments = {"bitrate_bps": body["value"]}
                else:
                    if set(body) != {"action"}:
                        raise WebInputError(
                            "invalid_request", "control action does not accept a value"
                        )
                    arguments = {}
                result = self.application.media_request(operation, arguments)
            except WebInputError as exc:
                self._error(exc.status, exc.code, exc.message)
                return
            self._json(200, result)
            return
        self.close_connection = True
        self._error(404, "not_found", "resource was not found")

    def do_PUT(self) -> None:
        path = self._path()
        if path != "/api/config":
            if not self._require_empty_body():
                return
            self._error(404, "not_found", "resource was not found")
            return
        if self._require_auth(csrf=True) is None:
            return
        try:
            body = self._json_body()
            if not isinstance(body, dict) or set(body) != {
                "config",
                "confirm_restart",
                "expected_revision",
            }:
                raise WebInputError("invalid_request", "configuration request is malformed")
            if type(body["confirm_restart"]) is not bool:
                raise WebInputError(
                    "invalid_request", "confirm_restart must be a boolean"
                )
            if not isinstance(body["expected_revision"], str) or not body[
                "expected_revision"
            ]:
                raise WebInputError(
                    "invalid_request", "expected_revision must be a string"
                )
            self.application.require_maintenance_idle()
            result = self.application.media_request("config.update", body)
        except WebInputError as exc:
            self._error(exc.status, exc.code, exc.message)
            return
        self._json(200, result)

    def do_OPTIONS(self) -> None:
        if not self._require_empty_body():
            return
        self._error(405, "method_not_allowed", "cross-origin requests are not supported")

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        try:
            path = urlsplit(self.path).path
        except ValueError:
            path = ""
        known = path if path in STATIC_ASSETS or path in API_ROUTES else "<unmatched>"
        LOGGER.info(
            "client=%s method=%s route=%s status=%s bytes=%s",
            self.client_address[0],
            self.command,
            known[:128],
            code,
            size,
        )

    def log_message(self, _format: str, *_args: object) -> None:
        LOGGER.warning("client=%s malformed HTTP request", self.client_address[0])

    def _path(self) -> str:
        try:
            parsed = urlsplit(self.path)
        except ValueError:
            self.close_connection = True
            return ""
        if parsed.fragment or (parsed.query and parsed.path not in {"/api/storage", "/api/storage/file"}):
            return ""
        return parsed.path

    def _query(self, allowed: set[str]) -> dict[str, str]:
        try:
            query = parse_qs(urlsplit(self.path).query, keep_blank_values=True,
                             strict_parsing=True, max_num_fields=3)
        except ValueError as exc:
            raise WebInputError("invalid_request", "query is malformed") from exc
        if set(query) - allowed or any(len(values) != 1 for values in query.values()):
            raise WebInputError("invalid_request", "query is malformed")
        return {key: values[0] for key, values in query.items()}

    def _storage_file(self, *, head: bool = False) -> None:
        query = self._query({"id", "download"})
        file_id = query.get("id", "")
        if not file_id or len(file_id) > 2048 or query.get("download", "1") != "1":
            raise WebInputError("invalid_request", "file request is malformed")
        application = self.application
        if not application.download_slots.acquire(blocking=False):
            raise WebInputError("download_busy", "two file transfers are already active", 503)
        started = False
        try:
            info = application.media_request("storage.info", {"file_id": file_id})["file"]
            if not info.get("can_download") or ("download" not in query and not info.get("can_play")):
                raise WebInputError("file_unavailable", "this recording is not available for playback or download", 409)
            size = info.get("size_bytes")
            if type(size) is not int or size < 0:
                raise WebInputError("invalid_response", "file metadata is invalid", 502)
            etag = '"' + quote(file_id, safe="") + '"'
            range_header = self.headers.get("Range")
            if self.headers.get("If-Range", etag) != etag:
                range_header = None
            try:
                start, end = _byte_range(range_header, size)
            except ValueError:
                self._error(416, "invalid_range", "requested byte range is unavailable",
                            extra_headers={"Content-Range": f"bytes */{size}"})
                return
            self.send_response(206 if range_header else 200)
            self._security_headers()
            self.send_header("Content-Type", "audio/flac")
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("ETag", etag)
            if range_header:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            disposition = "attachment" if "download" in query else "inline"
            name = quote(str(info.get("name", "recording.flac")), safe="")
            self.send_header("Content-Disposition", f"{disposition}; filename=\"recording.flac\"; filename*=UTF-8''{name}")
            self.send_header("Content-Length", str(max(0, end - start + 1)))
            self.end_headers()
            started = True
            if head:
                return
            offset = start
            bearer = self._cookie(SESSION_COOKIE_NAME)
            while offset <= end:
                if application.sessions.authenticate(bearer) is None:
                    self.close_connection = True
                    return
                length = min(24576, end - offset + 1)
                chunk = application.media_request("storage.read", {
                    "file_id": file_id, "offset": offset, "length": length,
                })
                data = base64.b64decode(chunk["data_base64"], validate=True)
                if chunk.get("offset") != offset or chunk.get("size_bytes") != size or len(data) != length:
                    raise ValueError("file transfer changed")
                self.wfile.write(data)
                offset += len(data)
        except (OSError, WebInputError, ValueError, KeyError, TypeError) as exc:
            if not started:
                if isinstance(exc, WebInputError):
                    raise
                raise WebInputError("invalid_response", "file metadata is unavailable", 502) from exc
            # Headers are already sent; a short response must fail, never become a corrupt success.
            self.close_connection = True
        finally:
            application.download_slots.release()

    def _account_or_maintenance(self, path: str, body: object, auth: tuple[str, object]) -> None:
        application = self.application
        password_change = path == "/api/account/password"
        keys = {"current_password", "new_password", "confirm_password"} if password_change else {"action", "current_password", "confirm"}
        if not isinstance(body, dict) or set(body) != keys:
            raise WebInputError("invalid_request", "request is malformed")
        if password_change:
            if application.credential_path is None:
                raise WebInputError("account_unavailable", "credential changes are unavailable", 503)
            validate_new_password(body["new_password"])
            if body["new_password"] != body["confirm_password"]:
                raise WebInputError("invalid_password", "new passwords do not match")
        elif body["action"] not in ("media.restart", "system.reboot", "system.shutdown") or body["confirm"] is not True:
            raise WebInputError("confirmation_required", "a supported action must be confirmed")
        bearer, principal = auth
        with application.credential_lock:
            # A concurrent password change may have revoked the session since routing.
            if application.sessions.authenticate(bearer) is None:
                raise WebInputError("authentication_required", "sign in again", 401)
            application.verify_password(self.client_address[0], principal.username, body["current_password"])
            if not password_change:
                result = application.maintenance_request(body["action"])
                self._json(202, result)
                return
            record = CredentialRecord.create(principal.username, body["new_password"])
            uncertain = False
            try:
                write_credential_atomic(application.credential_path, record)
            except CredentialCommitUncertain:
                uncertain = True
            except OSError as exc:
                raise WebInputError("credential_write_failed", "password could not be saved; it is unchanged", 500) from exc
            application.credential = record
            application.sessions.revoke_all()
        self._json(200, {"changed": True, "authenticated": False, "durability_confirmed": not uncertain},
                   cookies=(clear_session_cookie(secure=application.secure_cookie),
                            _clear_csrf_cookie(secure=application.secure_cookie)))

    def _serve_asset(self, path: str) -> None:
        filename, content_type = STATIC_ASSETS[path]
        try:
            payload = (self.application.assets / filename).read_bytes()
        except OSError:
            self._error(500, "asset_unavailable", "web asset is unavailable")
            return
        try:
            self.send_response(200)
            self._security_headers()
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except OSError as exc:
            if not _is_client_disconnect(exc):
                raise
            self.close_connection = True

    def _login(self) -> None:
        if self.application.credential is None:
            self.close_connection = True
            self._error(409, "setup_required", "create your administrator account first")
            return
        try:
            body = self._json_body()
            if not isinstance(body, dict) or set(body) != {"username", "password"}:
                raise WebInputError("invalid_login", "username or password is invalid", 401)
            username = body["username"]
            password = body["password"]
            decision = self.application.throttle.admit(self.client_address[0], username)
            if not decision.allowed:
                self._error(
                    429,
                    "rate_limited",
                    "too many login attempts; try again later",
                    extra_headers={"Retry-After": str(decision.retry_after_seconds)},
                )
                return
            if not self.application.password_slots.acquire(blocking=False):
                self._error(
                    503,
                    "authentication_busy",
                    "authentication capacity is temporarily busy",
                    extra_headers={"Retry-After": "1"},
                )
                return
            try:
                with self.application.credential_lock:
                    verified = verify_credentials(self.application.credential, username, password)
                    if not verified:
                        self._error(401, "invalid_login", "username or password is invalid")
                        return
                    self.application.throttle.reset(self.client_address[0], username)
                    session = self.application.sessions.create(self.application.credential.username)
            finally:
                self.application.password_slots.release()
        except (SecurityError, WebInputError):
            self._error(401, "invalid_login", "username or password is invalid")
            return
        self._json(
            200,
            {
                "authenticated": True,
                "username": self.application.credential.username,
                "csrf_token": session.csrf_token,
            },
            cookies=(
                build_session_cookie(
                    session.bearer_token, secure=self.application.secure_cookie
                ),
                _csrf_cookie(session.csrf_token, secure=self.application.secure_cookie),
            ),
        )

    def _session_status(self) -> None:
        if self.application.credential is None:
            self._json(200, {"authenticated": False, "setup_required": True})
            return
        bearer = self._cookie(SESSION_COOKIE_NAME)
        principal = self.application.sessions.authenticate(bearer)
        if principal is None:
            self._json(200, {"authenticated": False})
            return
        csrf = self._cookie(CSRF_COOKIE_NAME)
        cookies: tuple[str, ...] = ()
        if not self.application.sessions.validate_csrf(bearer, csrf):
            rotated = self.application.sessions.rotate(bearer)
            if rotated is None:
                self._json(200, {"authenticated": False})
                return
            bearer = rotated.bearer_token
            csrf = rotated.csrf_token
            cookies = (
                build_session_cookie(bearer, secure=self.application.secure_cookie),
                _csrf_cookie(csrf, secure=self.application.secure_cookie),
            )
        self._json(
            200,
            {
                "authenticated": True,
                "username": principal.username,
                "csrf_token": csrf,
            },
            cookies=cookies,
        )

    def _require_auth(
        self, *, csrf: bool = False
    ) -> tuple[str, object] | None:
        bearer = self._cookie(SESSION_COOKIE_NAME)
        principal = self.application.sessions.authenticate(bearer) if self.application.credential is not None else None
        if principal is None:
            if self.command in {"POST", "PUT", "PATCH"}:
                self.close_connection = True
            self._error(401, "authentication_required", "authentication is required")
            return None
        if csrf:
            if not self._require_same_origin():
                return None
            header = self.headers.get("X-CSRF-Token")
            if not self.application.sessions.validate_csrf(bearer, header):
                self.close_connection = True
                self._error(403, "csrf_rejected", "CSRF token is invalid")
                return None
        return bearer, principal

    def _require_same_origin(self) -> bool:
        """JSON-only auth plus denied CORS preflight also covers older browsers."""
        try:
            sites = self.headers.get_all("Sec-Fetch-Site") or []
            if len(sites) > 1 or (sites and sites[0] not in {"same-origin", "none"}):
                raise ValueError("cross-origin request")
            origins = self.headers.get_all("Origin") or []
            if origins:
                if len(origins) != 1:
                    raise ValueError("invalid origin")
                origin = urlsplit(origins[0])
                scheme = "https" if self.application.secure_cookie else "http"
                if origin.scheme != scheme or origin.path or origin.query or origin.fragment:
                    raise ValueError("invalid origin")
                if _http_authority(origin.netloc, scheme) != _http_authority(self.headers["Host"], scheme):
                    raise ValueError("cross-origin request")
            return True
        except (ValueError, KeyError):
            self.close_connection = True
            self._error(403, "origin_rejected", "cross-origin requests are not supported")
            return False

    def _setup_account(self) -> None:
        application = self.application
        if application.credential is not None:
            self.close_connection = True
            self._error(409, "setup_complete", "an administrator account already exists; sign in")
            return
        try:
            body = self._json_body()
            decision = application.throttle.admit(self.client_address[0], "first_setup")
            if not decision.allowed:
                self._error(429, "rate_limited", "too many setup attempts; try again later",
                            extra_headers={"Retry-After": str(decision.retry_after_seconds)})
                return
            if not isinstance(body, dict) or set(body) != {"username", "password", "confirm_password"}:
                raise WebInputError("invalid_request", "account setup request is malformed")
            validate_new_password(body["password"])
            if body["password"] != body["confirm_password"]:
                raise WebInputError("invalid_password", "passwords do not match")
            if not application.password_slots.acquire(blocking=False):
                raise WebInputError("authentication_busy", "authentication is temporarily busy", 503)
            uncertain = False
            try:
                with application.credential_lock:
                    existing = load_web_credential(application.credential_path, application.setup_pending_path)
                    if application.credential is not None or existing is not None:
                        application.credential = application.credential or existing
                        raise WebInputError("setup_complete", "an administrator account already exists; sign in", 409)
                    record = CredentialRecord.create(body["username"], body["password"])
                    try:
                        write_credential_atomic(application.credential_path, record, replace_existing=False)
                    except FileExistsError:
                        application.credential = load_credential(application.credential_path)
                        raise WebInputError("setup_complete", "an administrator account already exists; sign in", 409) from None
                    except CredentialCommitUncertain:
                        uncertain = True
                    application.credential = record
                    application.sessions.revoke_all()
                    try:
                        _finish_setup(application.setup_pending_path)
                    except OSError:
                        uncertain = True
            finally:
                application.password_slots.release()
        except WebInputError as exc:
            self._error(exc.status, exc.code, exc.message)
            return
        except SecurityError as exc:
            self._error(400, "invalid_setup", str(exc))
            return
        except OSError:
            self._error(500, "credential_write_failed", "account could not be saved; check device storage")
            return
        self._json(201, {"created": True, "authenticated": False, "setup_required": False,
                         "durability_confirmed": not uncertain})

    def _media_json(self, operation: str) -> None:
        try:
            result = self.application.media_request(operation)
        except WebInputError as exc:
            if operation == "status" and exc.code == "media_unavailable":
                self._json(
                    503,
                    {
                        "available": False,
                        "media_available": False,
                        "state": "unavailable",
                        "error": {"code": exc.code, "message": exc.message},
                    },
                )
            else:
                self._error(exc.status, exc.code, exc.message)
            return
        if operation == "status" and isinstance(result, dict):
            result.setdefault("available", True)
        self._json(200, result)

    def _events(self) -> None:
        auth = self._require_auth()
        if auth is None:
            return
        bearer, _principal = auth
        if not self.application.acquire_sse_client():
            self._error(503, "telemetry_capacity", "telemetry client limit reached")
            return
        try:
            self.send_response(200)
            self._security_headers()
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            count = 0
            telemetry_interval = self.application.sse_interval_seconds
            next_status_at = 0.0
            while not self.server.stop_event.is_set():  # type: ignore[attr-defined]
                cycle_started = time.monotonic()
                if self.application.sessions.authenticate(bearer) is None:
                    break
                self.application.renew_spectrum_lease()
                now = time.monotonic()
                if now >= next_status_at:
                    event = b"snapshot"
                    try:
                        status = self.application.media_request("status")
                        if isinstance(status, dict):
                            status.setdefault("available", True)
                            telemetry = status.pop("telemetry", None)
                        else:
                            telemetry = None
                        payload = {"status": status, "telemetry": telemetry}
                        telemetry_interval = self.application.telemetry_interval_seconds(status)
                    except WebInputError as exc:
                        payload = {
                            "status": {
                                "available": False,
                                "media_available": False,
                                "state": "unavailable",
                                "error": {"code": exc.code, "message": exc.message},
                            },
                            "telemetry": None,
                        }
                        telemetry_interval = self.application.sse_interval_seconds
                    next_status_at = now + self.application.sse_interval_seconds
                else:
                    event = b"telemetry"
                    try:
                        payload = self.application.media_request("monitoring.telemetry")
                    except WebInputError:
                        payload = None
                encoded = json.dumps(
                    payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                ).encode("utf-8")
                self.wfile.write(b"event: " + event + b"\n")
                self.wfile.write(b"data: " + encoded + b"\n\n")
                self.wfile.flush()
                count += 1
                if (
                    self.application.sse_event_limit is not None
                    and count >= self.application.sse_event_limit
                ):
                    break
                wait_seconds = min(
                    max(0.0, telemetry_interval - (time.monotonic() - cycle_started)),
                    max(0.0, next_status_at - time.monotonic()),
                )
                if wait_seconds > 0 and self.server.stop_event.wait(  # type: ignore[attr-defined]
                    wait_seconds
                ):
                    break
        except OSError as exc:
            if not _is_client_disconnect(exc):
                raise
        finally:
            self.application.release_sse_client()
            self.close_connection = True

    def _json_body(self) -> Any:
        length = self._request_content_length(required=True)
        assert length is not None
        if self.headers.get_content_type() != "application/json":
            self.close_connection = True
            raise WebInputError("invalid_request", "Content-Type must be application/json")
        if length > MAX_REQUEST_BODY_BYTES:
            self.close_connection = True
            raise WebInputError("request_too_large", "request body exceeds the limit", 413)
        try:
            body = self.rfile.read(length)
        except OSError as exc:
            self.close_connection = True
            raise WebInputError("invalid_request", "request body is incomplete") from exc
        if len(body) != length:
            self.close_connection = True
            raise WebInputError("invalid_request", "request body is incomplete")
        try:
            text = body.decode("utf-8")
        except UnicodeError as exc:
            raise WebInputError("invalid_request", "request body is invalid JSON") from exc
        try:
            return json.loads(
                text,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except WebInputError:
            raise
        except (json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise WebInputError("invalid_request", "request body is invalid JSON") from exc

    def _require_empty_body(self) -> bool:
        try:
            length = self._request_content_length(required=False)
        except WebInputError as exc:
            self._error(exc.status, exc.code, exc.message)
            return False
        if length not in (None, 0):
            self.close_connection = True
            self._error(400, "invalid_request", "request body is not allowed")
            return False
        return True

    def _request_content_length(self, *, required: bool) -> int | None:
        if self.headers.get_all("Transfer-Encoding"):
            self.close_connection = True
            raise WebInputError("invalid_request", "Transfer-Encoding is unsupported")
        values = self.headers.get_all("Content-Length") or []
        if not values:
            if not required:
                return None
            self.close_connection = True
            raise WebInputError("invalid_request", "Content-Length is required")
        if len(values) != 1:
            self.close_connection = True
            raise WebInputError("invalid_request", "Content-Length is invalid")
        raw = values[0]
        if not raw or any(character < "0" or character > "9" for character in raw):
            self.close_connection = True
            raise WebInputError("invalid_request", "Content-Length is invalid")

        significant = raw.lstrip("0") or "0"
        limit = str(MAX_REQUEST_BODY_BYTES)
        if len(significant) > len(limit) or (
            len(significant) == len(limit) and significant > limit
        ):
            return MAX_REQUEST_BODY_BYTES + 1
        return int(significant)

    def _cookie(self, name: str) -> str | None:
        header = self.headers.get("Cookie")
        if not header or len(header) > 4096:
            return None
        try:
            cookie = http.cookies.SimpleCookie()
            cookie.load(header)
        except http.cookies.CookieError:
            return None
        item = cookie.get(name)
        return item.value if item is not None else None

    def _json(
        self,
        status: int,
        value: Any,
        *,
        cookies: Sequence[str] = (),
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        try:
            payload = json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            self._error(500, "invalid_response", "server response is invalid")
            return
        try:
            self.send_response(status)
            self._security_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            if self.close_connection:
                self.send_header("Connection", "close")
            for cookie in cookies:
                self.send_header("Set-Cookie", cookie)
            for key, item in (extra_headers or {}).items():
                self.send_header(key, item)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
        except OSError as exc:
            if not _is_client_disconnect(exc):
                raise
            self.close_connection = True

    def _error(
        self,
        status: int,
        code: str,
        message: str,
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        self._json(
            status,
            {"error": {"code": code, "message": message}},
            extra_headers=extra_headers,
        )

    def _security_headers(self) -> None:
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self'; media-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")


def _byte_range(header: str | None, size: int) -> tuple[int, int]:
    """One native-browser byte range, including suffix and open-ended ranges."""
    if header is None:
        return 0, size - 1
    if len(header) > 128 or size == 0:
        raise ValueError("invalid range")
    match = re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", header)
    if match is None or not any(match.groups()):
        raise ValueError("invalid range")
    first, last = match.groups()
    if first:
        start = int(first)
        end = min(int(last), size - 1) if last else size - 1
        if start >= size or end < start:
            raise ValueError("invalid range")
        return start, end
    suffix = int(last)
    if suffix <= 0:
        raise ValueError("invalid range")
    return max(0, size - suffix), size - 1


def _http_authority(value: str, scheme: str = "http") -> tuple[str, int]:
    if not value or len(value) > 255 or any(character.isspace() for character in value):
        raise ValueError("invalid HTTP host")
    parsed = urlsplit(f"{scheme}://{value}")
    if parsed.username is not None or parsed.password is not None or parsed.path or parsed.query or parsed.fragment:
        raise ValueError("invalid HTTP host")
    host = (parsed.hostname or "").lower().rstrip(".")
    port = parsed.port if parsed.port is not None else (443 if scheme == "https" else 80)
    if not host or not 1 <= port <= 65535:
        raise ValueError("invalid HTTP host")
    return host, port


_LOCAL_NETWORKS = tuple(ipaddress.ip_network(network) for network in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
    "::1/128", "fc00::/7", "fe80::/10",
))


def _local_network_peer(value: str) -> bool:
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return any(address in network for network in _LOCAL_NETWORKS)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise WebInputError("invalid_request", "request contains a duplicate key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise WebInputError("invalid_request", "request contains a non-finite number")


def _csrf_cookie(token: str, *, secure: bool) -> str:
    cookie = http.cookies.SimpleCookie()
    cookie[CSRF_COOKIE_NAME] = token
    morsel = cookie[CSRF_COOKIE_NAME]
    morsel["path"] = "/"
    morsel["samesite"] = "Strict"
    if secure:
        morsel["secure"] = True
    return cookie.output(header="").strip()


def _clear_csrf_cookie(*, secure: bool) -> str:
    cookie = http.cookies.SimpleCookie()
    cookie[CSRF_COOKIE_NAME] = ""
    morsel = cookie[CSRF_COOKIE_NAME]
    morsel["path"] = "/"
    morsel["samesite"] = "Strict"
    morsel["max-age"] = 0
    if secure:
        morsel["secure"] = True
    return cookie.output(header="").strip()


def load_credential(path: str | os.PathLike[str]) -> CredentialRecord:
    target = Path(path)
    try:
        information = target.lstat()
        if stat.S_ISLNK(information.st_mode) or not stat.S_ISREG(information.st_mode):
            raise SecurityError("invalid credential record")
        if os.name == "posix" and stat.S_IMODE(information.st_mode) & 0o077:
            raise SecurityError("credential file permissions are too broad")
        if information.st_size > 8 * 1024:
            raise SecurityError("invalid credential record")
        return CredentialRecord.from_json(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise SecurityError("credential file is unavailable") from exc


def _finish_setup(path: Path | None) -> None:
    if path is not None:
        try:
            _validate_setup_marker(path)
            path.unlink()
        except FileNotFoundError:
            return
        _sync_credential_parent(path.parent)


def _validate_setup_marker(marker: Path) -> None:
    information = marker.lstat()
    if not stat.S_ISREG(information.st_mode) or information.st_size != 2:
        raise SecurityError("invalid first-time setup marker")
    if os.name == "posix" and stat.S_IMODE(information.st_mode) & 0o077:
        raise SecurityError("first-time setup marker permissions are too broad")
    if marker.read_bytes() != b"1\n":
        raise SecurityError("invalid first-time setup marker")


def load_web_credential(
    path: str | os.PathLike[str], setup_pending_path: str | os.PathLike[str] | None = None,
) -> CredentialRecord | None:
    """Only an explicit fresh-install marker permits a credential-less service."""
    target = Path(path)
    marker = Path(setup_pending_path) if setup_pending_path is not None else None
    if marker is not None and marker.absolute() == target.absolute():
        raise SecurityError("setup marker must be separate from credentials")
    try:
        target.lstat()
    except FileNotFoundError:
        pass
    else:
        record = load_credential(target)  # Invalid, unreadable or corrupt never means first run.
        try:
            _finish_setup(marker)
        except SecurityError:
            pass  # A bad leftover marker never overrides an existing valid account.
        return record
    if marker is None:
        raise SecurityError("credential file is unavailable; first-time setup is not enabled")
    try:
        _validate_setup_marker(marker)
    except OSError as exc:
        raise SecurityError("first-time setup is not enabled") from exc
    return None


def write_credential_atomic(
    path: str | os.PathLike[str], record: CredentialRecord, *, replace_existing: bool = True,
) -> None:
    target = Path(path)
    if target.exists() and target.is_symlink():
        raise OSError("refusing to replace a symbolic-link credential file")
    target.parent.mkdir(parents=False, exist_ok=True)
    payload = (record.to_json() + "\n").encode("utf-8")
    descriptor, name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(name)
    replaced = False
    try:
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if replace_existing:
            os.replace(temporary, target)
        else:
            # Publish a complete file without clobbering another first-run claimant.
            os.link(temporary, target)
        replaced = True
        if not replace_existing:
            temporary.unlink()
        _sync_credential_parent(target.parent)
    except BaseException as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        if replaced:
            raise CredentialCommitUncertain() from exc
        raise


def _sync_credential_parent(path: Path) -> None:
    if os.name != "posix":
        return
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def create_server(
    bind: str,
    port: int,
    application: WebApplication,
    *,
    max_clients: int = MAX_HTTP_CLIENTS,
) -> BoundedThreadingHTTPServer:
    return BoundedThreadingHTTPServer((bind, port), application, max_clients=max_clients)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve", help="run the local web service")
    serve.add_argument("--credentials", required=True, type=Path)
    serve.add_argument("--setup-pending", type=Path, help="fresh-install marker enabling first-time account creation")
    serve.add_argument("--control-socket", required=True, type=Path)
    serve.add_argument("--bind", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--secure-cookie", action="store_true")
    serve.add_argument("--lan-only", action="store_true", help="accept only loopback, private and link-local clients")
    serve.add_argument("--max-clients", type=int, default=MAX_HTTP_CLIENTS)
    serve.add_argument("--max-sse-clients", type=int, default=MAX_SSE_CLIENTS)
    serve.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )
    passwd = subparsers.add_parser("passwd", help="create or replace the web credential")
    passwd.add_argument("--credentials", required=True, type=Path)
    passwd.add_argument("--username")
    passwd.add_argument("--tty-stdin", action="store_true", help=argparse.SUPPRESS)
    return parser


def open_controlling_tty() -> Any:
    """Open the controlling terminal so password input can never fall back to stdin."""

    descriptor = -1
    raw = None
    try:
        descriptor = os.open(
            "/dev/tty",
            os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOCTTY", 0),
        )
        raw = io.FileIO(descriptor, "w+", closefd=True)
        descriptor = -1
        return io.TextIOWrapper(raw, encoding="utf-8")
    except BaseException:
        if raw is not None:
            raw.close()
        elif descriptor >= 0:
            os.close(descriptor)
        raise


def controlling_tty_available() -> bool:
    try:
        terminal = open_controlling_tty()
    except OSError:
        return False
    terminal.close()
    return True


def _password_command(
    path: Path, username: str | None, *, tty_stdin: bool = False
) -> int:
    owns_terminal = not tty_stdin
    try:
        if tty_stdin:
            terminal = os.sys.stdin
            descriptor = terminal.fileno()
            if not os.isatty(descriptor) or not stat.S_ISCHR(os.fstat(descriptor).st_mode):
                raise OSError("standard input is not a terminal")
            prompt_stream = os.sys.stderr
        else:
            terminal = open_controlling_tty()
            prompt_stream = terminal
    except (AttributeError, OSError, ValueError):
        print(
            "a controlling terminal is required; password input is never read from stdin",
            file=os.sys.stderr,
        )
        return 2
    previous_stdin = os.sys.stdin
    try:
        os.sys.stdin = terminal
        if username is None:
            try:
                username = load_credential(path).username
            except SecurityError:
                print("--username is required when creating the first credential", file=os.sys.stderr)
                return 2
        first = getpass.getpass("New TinyPiRelay web password: ", stream=prompt_stream)
        second = getpass.getpass("Confirm password: ", stream=prompt_stream)
    finally:
        os.sys.stdin = previous_stdin
        if owns_terminal:
            terminal.close()
    if first != second:
        print("passwords do not match", file=os.sys.stderr)
        return 2
    try:
        validate_new_password(first)
        record = CredentialRecord.create(username, first)
        write_credential_atomic(path, record)
    except (OSError, SecurityError) as exc:
        print(f"credential update failed: {exc}", file=os.sys.stderr)
        return 2
    try:
        _finish_setup(path.with_name("setup-pending"))
    except SecurityError:
        pass  # Never delete an unrelated file, which cannot enable setup anyway.
    except OSError:
        print("web credential saved, but setup marker cleanup failed; check device storage", file=os.sys.stderr)
        return 2
    print(f"web credential updated for {record.username}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "passwd":
        return _password_command(
            args.credentials, args.username, tty_stdin=args.tty_stdin
        )

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not 1 <= args.port <= 65_535:
        print("web service cannot start: port must be between 1 and 65535", file=os.sys.stderr)
        return 2
    try:
        from .maintenance import MaintenanceClient

        credential = load_web_credential(args.credentials, args.setup_pending)
        application = WebApplication(
            credential,
            ControlClient(args.control_socket),
            secure_cookie=args.secure_cookie,
            max_sse_clients=args.max_sse_clients,
            credential_path=args.credentials,
            setup_pending_path=args.setup_pending,
            lan_only=args.lan_only,
            maintenance_client=MaintenanceClient(),
        )
        server = create_server(args.bind, args.port, application, max_clients=args.max_clients)
    except (OSError, SecurityError, ValueError) as exc:
        print(f"web service cannot start: {exc}", file=os.sys.stderr)
        return 2

    previous: dict[int, Any] = {}

    def stop(_signum: int, _frame: object) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, stop)
    LOGGER.info("web service listening on %s:%s", args.bind, server.server_port)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        server.shutdown()
    finally:
        server.server_close()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
