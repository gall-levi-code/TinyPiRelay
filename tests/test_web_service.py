from __future__ import annotations

import http.client
import json
import os
import socket
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import tinypirelay.web_service as web_service
from tinypirelay.control_protocol import ControlOperationError, ControlUnavailable
from tinypirelay.web_security import (
    CredentialRecord,
    LoginThrottle,
    SecurityError,
    SessionStore,
)
from tinypirelay.web_service import (
    CredentialCommitUncertain,
    SSE_INTERVAL_SECONDS,
    TinyPiRelayRequestHandler,
    WebApplication,
    create_server,
    load_credential,
    write_credential_atomic,
)


class FakeControl:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.unavailable = False
        self.error_code: str | None = None
        self.monitoring_rate = 5.0
        self.device_telemetry = {
            "sequence": 7, "sampled_at_unix": 1_788_393_600.0, "config_revision": "r",
            "device": {"hostname": "fixture", "model": "Synthetic fixture", "os": "Fixture OS",
                       "architecture": "armv6l", "software_version": "fixture"},
            "system": {"cpu_percent": 0, "temperature_c": None, "memory_total_bytes": 536870912,
                       "memory_available_bytes": 268435456, "memory_used_bytes": 268435456,
                       "load_average": [0, 0, 0], "uptime_seconds": 7200},
            "storage": {"directory": "/recordings", "mountpoint": "/", "filesystem": "ext4",
                        "device": "/dev/fixture", "total_bytes": 128_000_000_000,
                        "used_bytes": 8_000_000_000, "available_bytes": 120_000_000_000,
                        "used_percent": 6.25, "recordings_bytes": 5_000_000_000,
                        "recordings_complete": True, "read_bytes_per_second": 0,
                        "write_bytes_per_second": 0, "stop_percent": 90,
                        "seconds_until_limit": None, "safe": True, "reason": None},
            "network": {"interface": "wlan0", "address": "192.0.2.94", "kind": "wifi",
                        "operstate": "up", "speed_mbps": None, "duplex": None,
                        "rx_bytes_per_second": 0, "tx_bytes_per_second": 0},
            "recording": {"file": "/recordings/sample.flac", "size_bytes": 5_000_000_000,
                          "elapsed_seconds": 7200},
        }

    def request(self, operation: str, arguments: dict[str, object]) -> object:
        if self.unavailable:
            raise ControlUnavailable("offline")
        if self.error_code is not None:
            raise ControlOperationError(self.error_code, "fixture conflict", "fixture")
        self.calls.append((operation, arguments))
        if operation == "status":
            return {
                "state": {
                    "service": "running",
                    "capture": {"state": "running"},
                    "stream": {"state": "running"},
                    "connection": {
                        "state": "connected",
                        "reconnect_count": 0,
                        "statistics": {
                            "send-rate-mbps": 0.42,
                            "rtt-ms": 3.2,
                            "packets-sent-total": 100,
                            "packets-sent-lost": 0,
                        },
                    },
                    "recording": {"state": "running", "current_file": "/recordings/sample.flac",
                                  "elapsed_file": "/recordings/sample.flac", "elapsed_seconds": 7200},
                },
                "telemetry": {
                    "meter": {
                        "sequence": 1,
                        "channels": [{
                            "peak_dbfs": -6.0,
                            "rms_dbfs": -20.0,
                            "clipping": False,
                            "no_signal": False,
                        }],
                    },
                    "spectrum": {"sequence": 1, "magnitudes_db": [-80.0, -40.0]},
                },
                "device_telemetry": self.device_telemetry,
                "active_config_revision": "r",
                "saved_config_revision": "r",
                "restart_required": False,
                "monitoring_updates_per_second": self.monitoring_rate,
                "passphrase": "must-not-escape",
            }
        if operation == "monitoring.telemetry":
            return {
                "meter": {"sequence": 2, "channels": []},
                "spectrum": {"sequence": 2, "magnitudes_db": [-70.0, -30.0]},
            }
        if operation == "capabilities":
            return {"capture_devices": [], "stream_representations": []}
        if operation == "config.get":
            return {"config": {}, "revision": "r", "restart_required": False}
        return {"ok": True, "operation": operation}


class WebServerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.control = FakeControl()
        self.credential = CredentialRecord.create(
            "operator", "correct horse battery staple", rng=lambda size: b"s" * size
        )
        self.application = WebApplication(
            self.credential,
            self.control,
            sessions=SessionStore(),
            throttle=LoginThrottle(max_attempts=3),
            sse_interval_seconds=0.01,
            sse_event_limit=1,
        )
        self.server = create_server("127.0.0.1", 0, self.application, max_clients=4)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port
        self.cookie = ""
        self.csrf = ""

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(
        self,
        method: str,
        path: str,
        body: object | bytes | None = None,
        *,
        csrf: bool = False,
        raw_headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, object] | str, list[tuple[str, str]]]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        headers = dict(raw_headers or {})
        if self.cookie:
            headers["Cookie"] = self.cookie
        if csrf:
            headers["X-CSRF-Token"] = self.csrf
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        response_headers = response.getheaders()
        connection.close()
        content_type = response.getheader("Content-Type", "")
        if "application/json" in content_type and payload:
            decoded: dict[str, object] | str = json.loads(payload)
        else:
            decoded = payload.decode("utf-8", errors="replace")
        return response.status, decoded, response_headers

    def login(self) -> None:
        status, payload, headers = self.request(
            "POST",
            "/api/login",
            {"username": "operator", "password": "correct horse battery staple"},
        )
        self.assertEqual(200, status)
        assert isinstance(payload, dict)
        self.csrf = str(payload["csrf_token"])
        cookies = [value.split(";", 1)[0] for key, value in headers if key == "Set-Cookie"]
        self.cookie = "; ".join(cookies)

    def raw_exchange(self, request: bytes) -> bytes:
        chunks: list[bytes] = []
        with socket.create_connection(("127.0.0.1", self.port), timeout=3) as connection:
            connection.settimeout(3)
            connection.sendall(request)
            connection.shutdown(socket.SHUT_WR)
            while True:
                chunk = connection.recv(64 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
        return b"".join(chunks)


class WebServiceTests(WebServerCase):
    def test_login_session_logout_and_security_headers(self) -> None:
        status, _payload, headers = self.request("GET", "/")
        self.assertEqual(200, status)
        self.assertTrue(any(key == "Content-Security-Policy" for key, _ in headers))

        self.login()
        status, session, _headers = self.request("GET", "/api/session")
        self.assertEqual(200, status)
        self.assertTrue(session["authenticated"])
        status, payload, _headers = self.request("POST", "/api/logout", csrf=True)
        self.assertEqual(200, status)
        self.assertFalse(payload["authenticated"])
        status, _payload, _headers = self.request("GET", "/api/status")
        self.assertEqual(401, status)

    def test_authentication_csrf_and_malformed_json_fail_closed(self) -> None:
        status, _payload, _headers = self.request("GET", "/api/status")
        self.assertEqual(401, status)
        self.login()
        status, payload, _headers = self.request(
            "POST", "/api/control", {"action": "stream.stop"}
        )
        self.assertEqual(403, status)
        self.assertEqual("csrf_rejected", payload["error"]["code"])

        status, payload, _headers = self.request(
            "PUT",
            "/api/config",
            b'{"config":{},"config":{},"confirm_restart":true}',
            csrf=True,
            raw_headers={"Content-Type": "application/json"},
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"]["code"])

    def test_control_config_status_and_secret_redaction(self) -> None:
        self.login()
        status, result, _headers = self.request("GET", "/api/status")
        self.assertEqual(200, status)
        rendered = json.dumps(result)
        self.assertNotIn("must-not-escape", rendered)
        self.assertIn("[REDACTED]", rendered)

        status, result, _headers = self.request(
            "POST",
            "/api/control",
            {"action": "stream.set_opus_bitrate", "value": 96_000},
            csrf=True,
        )
        self.assertEqual(200, status)
        self.assertEqual(
            ("stream.set_opus_bitrate", {"bitrate_bps": 96_000}),
            self.control.calls[-1],
        )
        status, _result, _headers = self.request(
            "PUT",
            "/api/config",
            {
                "config": {"schema_version": 1},
                "confirm_restart": True,
                "expected_revision": "r",
            },
            csrf=True,
        )
        self.assertEqual(200, status)
        self.assertEqual("config.update", self.control.calls[-1][0])

    def test_login_throttle_and_media_unavailable_state(self) -> None:
        self.application.throttle = LoginThrottle(max_attempts=1, window_seconds=60)
        status, _payload, _headers = self.request(
            "POST", "/api/login", {"username": "operator", "password": "wrong"}
        )
        self.assertEqual(401, status)
        status, payload, headers = self.request(
            "POST", "/api/login", {"username": "operator", "password": "wrong"}
        )
        self.assertEqual(429, status)
        self.assertEqual("rate_limited", payload["error"]["code"])
        self.assertTrue(any(key == "Retry-After" for key, _ in headers))

        self.application.throttle = LoginThrottle()
        self.login()
        self.control.unavailable = True
        status, payload, _headers = self.request("GET", "/api/status")
        self.assertEqual(503, status)
        self.assertFalse(payload["available"])
        self.assertEqual("unavailable", payload["state"])

    def test_configuration_state_conflicts_are_http_409(self) -> None:
        self.login()
        for code in ("revision_conflict", "recording_active"):
            with self.subTest(code=code):
                self.control.error_code = code
                status, payload, _headers = self.request(
                    "PUT",
                    "/api/config",
                    {
                        "config": {"schema_version": 1},
                        "confirm_restart": False,
                        "expected_revision": "r",
                    },
                    csrf=True,
                )
                self.assertEqual(409, status)
                self.assertEqual(code, payload["error"]["code"])

    def test_sse_contains_bounded_numeric_data_not_images(self) -> None:
        self.login()
        self.control.calls.clear()
        status, payload, headers = self.request("GET", "/api/events")
        self.assertEqual(200, status)
        self.assertTrue(any(value.startswith("text/event-stream") for key, value in headers if key == "Content-Type"))
        assert isinstance(payload, str)
        self.assertIn("event: snapshot", payload)
        self.assertIn('"magnitudes_db":[-80.0,-40.0]', payload)
        self.assertNotIn("data:image", payload)
        self.assertEqual(0, getattr(self.application.sse_slots, "_value", 0) - 4)
        self.assertEqual(
            [
                "monitoring.spectrum_lease",
                "status",
                "monitoring.spectrum_release",
            ],
            [operation for operation, _arguments in self.control.calls],
        )

    def test_sixty_hz_uses_small_telemetry_events_between_status_snapshots(self) -> None:
        self.login()
        self.control.calls.clear()
        self.control.monitoring_rate = 60.0
        self.application.sse_interval_seconds = 0.2
        self.application.sse_event_limit = 3

        status, payload, _headers = self.request("GET", "/api/events")

        self.assertEqual(200, status)
        assert isinstance(payload, str)
        self.assertEqual(1, payload.count("event: snapshot"))
        self.assertEqual(2, payload.count("event: telemetry"))
        self.assertEqual(
            [
                "monitoring.spectrum_lease",
                "status",
                "monitoring.telemetry",
                "monitoring.telemetry",
                "monitoring.spectrum_release",
            ],
            [operation for operation, _arguments in self.control.calls],
        )

    def test_dashboard_status_and_sse_share_the_same_redacted_contract(self) -> None:
        self.login()
        status, snapshot, _headers = self.request("GET", "/api/status")
        self.assertEqual(200, status)
        status, events, _headers = self.request("GET", "/api/events")
        self.assertEqual(200, status)
        event = json.loads(next(line[6:] for line in events.splitlines() if line.startswith("data: ")))
        telemetry = snapshot.pop("telemetry")
        self.assertEqual(snapshot, event["status"])
        self.assertEqual(telemetry, event["telemetry"])
        self.assertNotIn("telemetry", event["status"])
        self.assertTrue(event["status"]["available"])
        self.assertNotIn("must-not-escape", events)
        channel = event["telemetry"]["meter"]["channels"][0]
        self.assertEqual({"peak_dbfs", "rms_dbfs", "clipping", "no_signal"}, set(channel))
        self.assertEqual(0.42, event["status"]["state"]["connection"]["statistics"]["send-rate-mbps"])
        device = event["status"]["device_telemetry"]
        self.assertEqual(self.control.device_telemetry, device)
        self.assertNotIn("device_telemetry", event["telemetry"])
        self.assertEqual(0, device["system"]["cpu_percent"])
        self.assertIsNone(device["system"]["temperature_c"])
        self.assertEqual(5_000_000_000, device["recording"]["size_bytes"])
        self.assertEqual(0, device["network"]["tx_bytes_per_second"])
        self.assertEqual("r", device["config_revision"])

    def test_device_telemetry_unavailable_is_not_replaced_with_zeroes(self) -> None:
        self.login()
        self.control.device_telemetry = None
        status, snapshot, _headers = self.request("GET", "/api/status")
        self.assertEqual(200, status)
        self.assertIsNone(snapshot["device_telemetry"])
        status, events, _headers = self.request("GET", "/api/events")
        self.assertEqual(200, status)
        event = json.loads(next(line[6:] for line in events.splitlines() if line.startswith("data: ")))
        self.assertIsNone(event["status"]["device_telemetry"])

    def test_unavailable_sse_has_no_stale_media_or_telemetry(self) -> None:
        self.login()
        self.control.unavailable = True
        status, events, _headers = self.request("GET", "/api/events")
        self.assertEqual(200, status)
        event = json.loads(next(line[6:] for line in events.splitlines() if line.startswith("data: ")))
        self.assertFalse(event["status"]["available"])
        self.assertFalse(event["status"]["media_available"])
        self.assertEqual("unavailable", event["status"]["state"])
        self.assertEqual("media_unavailable", event["status"]["error"]["code"])
        self.assertIsNone(event["telemetry"])
        self.assertIsNone(event["status"].get("device_telemetry"))
        self.assertEqual(0, self.application.active_sse_clients)

    def test_control_transport_success_does_not_imply_action_succeeded(self) -> None:
        self.login()
        results = (
            ("recording.start", {
                "ok": False,
                "changed": False,
                "error": {"code": "capture_not_running", "message": "capture must be running"},
                "state": {"recording": {"state": "stopped"}},
            }),
            ("service.restart_request", {"accepted": False, "restart_required": True}),
        )
        for action, result in results:
            with self.subTest(action=action), mock.patch.object(self.control, "request", return_value=result):
                status, payload, _headers = self.request(
                    "POST", "/api/control", {"action": action}, csrf=True
                )
                self.assertEqual(200, status)
                self.assertEqual(result, payload)

    def test_spectrum_lease_tracks_multiple_clients_and_last_release(self) -> None:
        self.control.calls.clear()

        self.assertTrue(self.application.acquire_sse_client())
        self.assertTrue(self.application.acquire_sse_client())
        self.assertEqual(2, self.application.active_sse_clients)
        self.assertEqual(
            1,
            sum(
                operation == "monitoring.spectrum_lease"
                for operation, _arguments in self.control.calls
            ),
        )

        self.application._spectrum_renew_at = 0.0  # noqa: SLF001 - lease clock
        self.application.renew_spectrum_lease()
        self.application.release_sse_client()
        self.assertEqual(1, self.application.active_sse_clients)
        self.assertFalse(
            any(
                operation == "monitoring.spectrum_release"
                for operation, _arguments in self.control.calls
            )
        )

        self.application.release_sse_client()
        self.assertEqual(0, self.application.active_sse_clients)
        self.assertEqual(
            2,
            sum(
                operation == "monitoring.spectrum_lease"
                for operation, _arguments in self.control.calls
            ),
        )
        self.assertEqual(
            1,
            sum(
                operation == "monitoring.spectrum_release"
                for operation, _arguments in self.control.calls
            ),
        )
        self.assertEqual(4, getattr(self.application.sse_slots, "_value", 0))

    def test_spectrum_lease_unavailable_paths_stay_bounded(self) -> None:
        self.control.unavailable = True

        self.assertTrue(self.application.acquire_sse_client())
        self.application.renew_spectrum_lease()
        self.application.release_sse_client()

        self.assertEqual(0, self.application.active_sse_clients)
        self.assertEqual(4, getattr(self.application.sse_slots, "_value", 0))
        self.assertEqual(0.2, SSE_INTERVAL_SECONDS)
        self.assertAlmostEqual(
            1 / 60,
            self.application.telemetry_interval_seconds(
                {"monitoring_updates_per_second": 60}
            ),
        )

    def test_no_default_credentials_and_password_file_is_not_an_asset(self) -> None:
        status, _payload, _headers = self.request("GET", "/credentials.json")
        self.assertEqual(404, status)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credential.json"
            path.write_text(self.credential.to_json(), encoding="utf-8")
            self.application.assets = Path(directory)
            status, _payload, _headers = self.request("GET", "/credential.json")
            self.assertEqual(404, status)

    def test_credential_file_round_trip_is_restrictive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credential.json"
            write_credential_atomic(path, self.credential)
            loaded = load_credential(path)
            self.assertEqual("operator", loaded.username)
            if os.name == "posix":
                self.assertEqual(0o600, path.stat().st_mode & 0o777)
                path.chmod(0o644)
                with self.assertRaisesRegex(SecurityError, "permissions"):
                    load_credential(path)

    def test_credential_replace_reports_committed_but_uncertain_durability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credential.json"
            old = CredentialRecord.create(
                "old", "old correct horse battery staple", rng=lambda size: b"o" * size
            )
            write_credential_atomic(path, old)

            with mock.patch.object(
                web_service,
                "_sync_credential_parent",
                side_effect=OSError("directory fsync fixture failure"),
            ):
                with self.assertRaisesRegex(
                    CredentialCommitUncertain, "replaced but directory durability"
                ):
                    write_credential_atomic(path, self.credential)

            self.assertEqual("operator", load_credential(path).username)

    def test_huge_json_integer_is_rejected_without_killing_the_server(self) -> None:
        self.login()
        body = (
            b'{"config":{"value":'
            + (b"9" * 5000)
            + b'},"confirm_restart":false,"expected_revision":"r"}'
        )

        status, payload, _headers = self.request(
            "PUT",
            "/api/config",
            body,
            csrf=True,
            raw_headers={"Content-Type": "application/json"},
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"]["code"])
        self.assertEqual(200, self.request("GET", "/")[0])

    def test_malformed_absolute_request_target_is_bounded_and_server_survives(self) -> None:
        errors: list[tuple[object, object]] = []
        self.server.handle_error = lambda request, address: errors.append(
            (request, address)
        )
        with socket.create_connection(("127.0.0.1", self.port), timeout=3) as connection:
            connection.sendall(
                b"GET http://[x]/ HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Connection: close\r\n\r\n"
            )
            response = connection.recv(4096)

        self.assertIn(b" 404 ", response)
        self.assertEqual([], errors)
        self.assertEqual(200, self.request("GET", "/")[0])

    def test_json_response_disconnects_are_quiet_across_the_write_path(self) -> None:
        stages = ("send_response", "security", "send_header", "end_headers", "write")
        for stage in stages:
            with self.subTest(stage=stage):
                handler = object.__new__(TinyPiRelayRequestHandler)
                handler.close_connection = False
                handler.command = "GET"

                def fail() -> None:
                    raise OSError("fixture connection closed")

                handler.send_response = (
                    (lambda _status: fail())
                    if stage == "send_response"
                    else (lambda _status: None)
                )
                handler._security_headers = (  # noqa: SLF001 - response I/O contract
                    fail if stage == "security" else lambda: None
                )
                handler.send_header = (
                    (lambda _key, _value: fail())
                    if stage == "send_header"
                    else (lambda _key, _value: None)
                )
                handler.end_headers = fail if stage == "end_headers" else lambda: None
                handler.wfile = mock.Mock()
                if stage == "write":
                    handler.wfile.write.side_effect = OSError("fixture connection closed")

                handler._json(200, {"ok": True})  # noqa: SLF001
                self.assertTrue(handler.close_connection)

    def test_reset_connection_churn_does_not_reach_server_error_handler(self) -> None:
        errors: list[tuple[object, object]] = []
        self.server.handle_error = lambda request, address: errors.append(
            (request, address)
        )
        request = (
            b"GET /missing HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Connection: close\r\n\r\n"
        )

        for _ in range(24):
            connection = socket.create_connection(("127.0.0.1", self.port), timeout=3)
            try:
                connection.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_LINGER,
                    struct.pack("ii", 1, 0),
                )
            except OSError:
                connection.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_LINGER,
                    struct.pack("hh", 1, 0),
                )
            connection.sendall(request)
            connection.close()

        deadline = time.monotonic() + 3.0
        while self.server.active_clients and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual([], errors)

    def test_ambiguous_or_pre_read_framing_closes_keep_alive(self) -> None:
        second = (
            b"GET / HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Connection: close\r\n\r\n"
        )
        cases = {
            "transfer_encoding": (
                b"Content-Type: application/json\r\n"
                b"Transfer-Encoding: identity\r\n"
            ),
            "duplicate_content_length": (
                b"Content-Type: application/json\r\n"
                b"Content-Length: 0\r\n"
                b"Content-Length: 0\r\n"
            ),
            "signed_content_length": (
                b"Content-Type: application/json\r\n"
                b"Content-Length: +0\r\n"
            ),
            "missing_content_length": b"Content-Type: application/json\r\n",
            "unsupported_content_type": (
                b"Content-Type: text/plain\r\n"
                b"Content-Length: 0\r\n"
            ),
            "oversized_body": (
                b"Content-Type: application/json\r\n"
                b"Content-Length: 32769\r\n"
            ),
        }
        for name, framing in cases.items():
            with self.subTest(name=name):
                response = self.raw_exchange(
                    b"POST /api/login HTTP/1.1\r\n"
                    b"Host: localhost\r\n"
                    b"Connection: keep-alive\r\n"
                    + framing
                    + b"\r\n"
                    + second
                )
                self.assertEqual(1, response.count(b"HTTP/1.1 "))
                self.assertIn(b"Connection: close\r\n", response)

    def test_non_body_routes_close_when_framing_declares_a_body(self) -> None:
        second = (
            b"GET / HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Connection: close\r\n\r\n"
        )
        for method, path in (("GET", "/"), ("OPTIONS", "/"), ("PUT", "/missing")):
            with self.subTest(method=method, path=path):
                response = self.raw_exchange(
                    f"{method} {path} HTTP/1.1\r\n".encode("ascii")
                    + b"Host: localhost\r\n"
                    + b"Connection: keep-alive\r\n"
                    + b"Content-Length: 1\r\n\r\n"
                    + second
                )
                self.assertEqual(1, response.count(b"HTTP/1.1 "))
                self.assertIn(b"HTTP/1.1 400 Bad Request\r\n", response)
                self.assertIn(b"Connection: close\r\n", response)

    def test_consumed_body_and_zero_length_get_can_reuse_keep_alive(self) -> None:
        first_body = b"{}"
        response = self.raw_exchange(
            b"POST /api/login HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Connection: keep-alive\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(first_body)}\r\n\r\n".encode("ascii")
            + first_body
            + b"GET /api/session HTTP/1.1\r\n"
            + b"Host: localhost\r\n"
            + b"Connection: keep-alive\r\n"
            + b"Content-Length: 0\r\n\r\n"
            + b"GET / HTTP/1.1\r\n"
            + b"Host: localhost\r\n"
            + b"Connection: close\r\n\r\n"
        )
        self.assertEqual(3, response.count(b"HTTP/1.1 "))
        self.assertIn(b"HTTP/1.1 401 Unauthorized\r\n", response)
        self.assertGreaterEqual(response.count(b"HTTP/1.1 200 OK\r\n"), 2)


if __name__ == "__main__":
    unittest.main()
