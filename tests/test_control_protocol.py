from __future__ import annotations

import json
import os
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Callable
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tinypirelay.control_protocol import (  # noqa: E402
    ALLOWED_OPERATIONS,
    DEFAULT_TIMEOUT_SECONDS,
    PROTOCOL_VERSION,
    ControlClient,
    ControlOperationError,
    ControlProtocolError,
    ControlRequestError,
    ControlUnavailable,
    UnixControlServer,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
    redact_secrets,
)


class ProtocolShapeTests(unittest.TestCase):
    def test_allowlist_is_explicit_and_includes_report_only_restart(self) -> None:
        self.assertEqual(
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
            },
            ALLOWED_OPERATIONS,
        )

    def test_request_round_trip_has_only_the_versioned_contract(self) -> None:
        encoded = encode_request("request-1", "stream.start", {"confirmed": True})

        self.assertTrue(encoded.endswith(b"\n"))
        decoded = decode_request(encoded[:-1])
        self.assertEqual(
            {
                "protocol_version": PROTOCOL_VERSION,
                "request_id": "request-1",
                "operation": "stream.start",
                "arguments": {"confirmed": True},
            },
            decoded,
        )

    def test_request_rejects_extensions_wrong_types_and_unknown_operations(
        self,
    ) -> None:
        valid = {
            "protocol_version": 1,
            "request_id": "request-1",
            "operation": "status",
            "arguments": {},
        }
        cases = []
        extra = dict(valid, future=True)
        cases.append(extra)
        bool_version = dict(valid, protocol_version=True)
        cases.append(bool_version)
        unknown = dict(valid, operation="shell.execute")
        cases.append(unknown)
        bad_arguments = dict(valid, arguments=[])
        cases.append(bad_arguments)
        bad_id = dict(valid, request_id="spaces are invalid")
        cases.append(bad_id)

        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(ControlProtocolError):
                    decode_request(json.dumps(value).encode("utf-8"))

        with self.assertRaisesRegex(ControlProtocolError, "arguments"):
            encode_request("request-1", "status", [])  # type: ignore[arg-type]

    def test_duplicate_keys_nonfinite_values_and_invalid_utf8_fail_closed(self) -> None:
        duplicate = (
            b'{"protocol_version":1,"request_id":"one","request_id":"two",'
            b'"operation":"status","arguments":{}}'
        )
        nonfinite = (
            b'{"protocol_version":1,"request_id":"one","operation":"status",'
            b'"arguments":{"n":NaN}}'
        )
        for payload in (duplicate, nonfinite, b"\xff"):
            with self.subTest(payload=payload):
                with self.assertRaises(ControlProtocolError):
                    decode_request(payload)

    def test_huge_json_integer_is_a_bounded_protocol_error(self) -> None:
        payload = (
            b'{"protocol_version":1,"request_id":"one","operation":"status",'
            b'"arguments":{"value":'
            + (b"9" * 5000)
            + b"}}"
        )

        with self.assertRaisesRegex(ControlProtocolError, "valid JSON"):
            decode_request(payload)

    def test_programmatic_nonfinite_and_oversized_messages_are_rejected(self) -> None:
        with self.assertRaises(ControlProtocolError):
            encode_request("one", "status", {"n": float("inf")})
        with self.assertRaisesRegex(ControlProtocolError, "size limit"):
            encode_request(
                "one",
                "status",
                {"padding": "x" * 300},
                max_message_bytes=256,
            )
        with self.assertRaisesRegex(ControlProtocolError, "size limit"):
            decode_request(b" " * 257, max_message_bytes=256)

    def test_response_requires_exact_result_or_error_shape(self) -> None:
        success = decode_response(encode_response("one", result={"ready": True})[:-1])
        failure = decode_response(
            encode_response(
                "one",
                error={"code": "not_ready", "message": "media is not ready"},
            )[:-1]
        )
        self.assertTrue(success["ok"])
        self.assertEqual({"ready": True}, success["result"])
        self.assertFalse(failure["ok"])
        self.assertEqual("not_ready", failure["error"]["code"])

        malformed = dict(success, error={"code": "x", "message": "x"})
        with self.assertRaises(ControlProtocolError):
            decode_response(json.dumps(malformed).encode("utf-8"))

    def test_recursive_redaction_covers_secret_field_names(self) -> None:
        source = {
            "stream": {
                "passphrase": "bird-secret",
                "nested": [
                    {"password_hash": "hash-value"},
                    {"csrf_token": "token-value"},
                    {"session-cookie": "cookie-value"},
                    {"value": "replacement-secret", "action": "replace"},
                ],
            },
            "representation_id": "opus",
        }

        redacted = redact_secrets(source)
        text = json.dumps(redacted)
        self.assertNotIn("bird-secret", text)
        self.assertNotIn("hash-value", text)
        self.assertNotIn("token-value", text)
        self.assertNotIn("cookie-value", text)
        self.assertNotIn("replacement-secret", text)
        self.assertEqual("opus", redacted["representation_id"])
        self.assertEqual("bird-secret", source["stream"]["passphrase"])

        encoded = encode_response("one", result=source)
        self.assertNotIn(b"bird-secret", encoded)
        self.assertNotIn(b"replacement-secret", encoded)

    def test_only_exact_safe_passphrase_metadata_survives_redaction(self) -> None:
        public_result = {
            "config": {
                "stream": {
                    "passphrase": {"configured": True, "action": "keep"},
                }
            }
        }

        response = decode_response(
            encode_response("one", result=public_result)[:-1]
        )["result"]
        response = redact_secrets(response)
        self.assertEqual(
            {"configured": True, "action": "keep"},
            response["config"]["stream"]["passphrase"],
        )

        unsafe_shapes = (
            "raw-secret",
            {"configured": True, "action": "replace", "value": "raw-secret"},
            {"configured": True, "action": "keep", "extra": "raw-secret"},
            {"configured": 1, "action": "keep"},
            {"configured": False, "action": "clear"},
        )
        for passphrase in unsafe_shapes:
            with self.subTest(passphrase=passphrase):
                redacted = redact_secrets({"passphrase": passphrase})
                self.assertEqual("[REDACTED]", redacted["passphrase"])
                self.assertNotIn("raw-secret", json.dumps(redacted))

        renamed = redact_secrets(
            {"srt_passphrase": {"configured": True, "action": "keep"}}
        )
        self.assertEqual("[REDACTED]", renamed["srt_passphrase"])


class ClientTests(unittest.TestCase):
    def _exchange(
        self,
        response: Callable[[dict[str, Any]], bytes | None],
    ) -> tuple[ControlClient, threading.Thread]:
        client_socket, server_socket = socket.socketpair()

        def serve() -> None:
            try:
                request = _receive_line(server_socket)
                decoded = decode_request(request)
                reply = response(decoded)
                if reply is not None:
                    server_socket.sendall(reply)
            finally:
                server_socket.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        client = ControlClient(
            "unused.sock",
            connector=lambda _path, _timeout: client_socket,
            timeout=0.5,
        )
        return client, thread

    def test_client_returns_success_and_matches_request_id(self) -> None:
        client, thread = self._exchange(
            lambda request: encode_response(
                request["request_id"], result={"state": "streaming"}
            )
        )

        result = client.request("status", request_id="fixed-id")
        thread.join(1)
        self.assertEqual({"state": "streaming"}, result)
        self.assertFalse(thread.is_alive())

    def test_client_distinguishes_remote_operation_error(self) -> None:
        client, thread = self._exchange(
            lambda request: encode_response(
                request["request_id"],
                error={"code": "not_ready", "message": "capture is stopped"},
            )
        )

        with self.assertRaises(ControlOperationError) as raised:
            client.request("stream.start", request_id="fixed-id")
        thread.join(1)
        self.assertEqual("not_ready", raised.exception.code)
        self.assertEqual("fixed-id", raised.exception.request_id)

    def test_client_distinguishes_malformed_or_mismatched_response(self) -> None:
        cases = (
            lambda _request: b"not-json\n",
            lambda _request: encode_response("different-id", result={}),
        )
        for response in cases:
            with self.subTest(response=response):
                client, thread = self._exchange(response)
                with self.assertRaises(ControlProtocolError):
                    client.request("status", request_id="fixed-id")
                thread.join(1)

    def test_client_distinguishes_unavailable_connection_and_empty_response(
        self,
    ) -> None:
        unavailable = ControlClient(
            "unused.sock",
            connector=lambda _path, _timeout: (_ for _ in ()).throw(
                OSError("fixture unavailable")
            ),
        )
        with self.assertRaises(ControlUnavailable):
            unavailable.request("status")

        client, thread = self._exchange(lambda _request: None)
        with self.assertRaises(ControlUnavailable):
            client.request("status", request_id="fixed-id")
        thread.join(1)

    def test_default_timeout_outlives_a_valid_delayed_save(self) -> None:
        class DelayedTransactionSocket:
            def __init__(self) -> None:
                self.timeout = 0.0
                self.request = b""
                self.committed = False
                self.responded = False

            def settimeout(self, value: float) -> None:
                self.timeout = value

            def sendall(self, value: bytes) -> None:
                self.request = value

            def shutdown(self, _direction: int) -> None:
                pass

            def recv(self, _size: int) -> bytes:
                # Model four sequential three-second backend waits without
                # sleeping. A former five-second deadline fails this contract.
                if self.timeout <= 12.0:
                    raise socket.timeout("modeled transaction exceeded deadline")
                if self.responded:
                    return b""
                self.responded = True
                self.committed = True
                request = decode_request(self.request[:-1])
                return encode_response(
                    request["request_id"],
                    result={"saved": True, "new_revision": "next"},
                )

            def close(self) -> None:
                pass

        connection = DelayedTransactionSocket()
        self.assertEqual(15.0, DEFAULT_TIMEOUT_SECONDS)
        client = ControlClient(
            "unused.sock",
            connector=lambda _path, _timeout: connection,  # type: ignore[arg-type]
        )

        result = client.request("config.update", request_id="delayed-save")

        self.assertEqual({"saved": True, "new_revision": "next"}, result)
        self.assertTrue(connection.committed)
        self.assertEqual(15.0, connection.timeout)


class ServerConnectionTests(unittest.TestCase):
    def _request(
        self,
        handler: Callable[[str, dict[str, Any]], Any],
        payload: bytes,
    ) -> dict[str, Any]:
        client_socket, server_socket = socket.socketpair()
        server = UnixControlServer("unused.sock", handler, timeout=0.5)
        thread = threading.Thread(
            target=server._handle_connection,  # noqa: SLF001 - socketpair portability
            args=(server_socket,),
            daemon=True,
        )
        thread.start()
        try:
            client_socket.sendall(payload)
            client_socket.shutdown(socket.SHUT_WR)
            response = decode_response(_receive_line(client_socket))
        finally:
            client_socket.close()
            server_socket.close()
            thread.join(1)
        self.assertFalse(thread.is_alive())
        return response

    def test_server_dispatches_exact_operation_and_redacts_result(self) -> None:
        calls: list[tuple[str, dict[str, Any]]] = []

        def handler(operation: str, arguments: dict[str, Any]) -> Any:
            calls.append((operation, arguments))
            return {"ok": "yes", "passphrase": "must-not-leak"}

        response = self._request(
            handler,
            encode_request("one", "config.get", {}),
        )

        self.assertEqual([("config.get", {})], calls)
        self.assertTrue(response["ok"])
        self.assertNotIn("must-not-leak", json.dumps(response))

    def test_handler_rejection_is_distinct_and_unexpected_error_is_generic(
        self,
    ) -> None:
        rejected = self._request(
            lambda _operation, _arguments: (_ for _ in ()).throw(
                ControlRequestError("invalid_arguments", "bitrate is not allowed")
            ),
            encode_request("one", "stream.set_opus_bitrate", {"bitrate_bps": 1}),
        )
        self.assertEqual("invalid_arguments", rejected["error"]["code"])

        secret = "exception-secret-must-not-leak"
        failed = self._request(
            lambda _operation, _arguments: (_ for _ in ()).throw(RuntimeError(secret)),
            encode_request("two", "status"),
        )
        self.assertEqual("internal_error", failed["error"]["code"])
        self.assertNotIn(secret, json.dumps(failed))

    def test_multiple_messages_on_one_connection_are_rejected(self) -> None:
        calls: list[str] = []
        payload = encode_request("one", "status") + encode_request("two", "status")
        response = self._request(
            lambda operation, _arguments: calls.append(operation),
            payload,
        )
        self.assertFalse(response["ok"])
        self.assertEqual("bad_request", response["error"]["code"])
        self.assertEqual([], calls)


class DirectoryPreparationTests(unittest.TestCase):
    def test_only_a_missing_leaf_directory_is_created(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            leaf = root / "control"
            server = UnixControlServer(leaf / "control.sock", lambda _op, _args: {})

            server._prepare_directory()  # noqa: SLF001 - filesystem safety contract
            self.assertTrue(leaf.is_dir())

            missing_ancestor = root / "missing"
            nested = missing_ancestor / "nested"
            nested_server = UnixControlServer(
                nested / "control.sock", lambda _op, _args: {}
            )
            with self.assertRaises(ControlUnavailable):
                nested_server._prepare_directory()  # noqa: SLF001
            self.assertFalse(missing_ancestor.exists())

    @unittest.skipUnless(os.name == "posix", "POSIX directory permissions")
    def test_existing_directory_mode_and_special_bits_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "control"
            parent.mkdir(mode=0o700)
            os.chmod(parent, 0o2700)
            before = stat.S_IMODE(os.lstat(parent).st_mode)

            server = UnixControlServer(parent / "control.sock", lambda _op, _args: {})
            server._prepare_directory()  # noqa: SLF001

            self.assertEqual(before, stat.S_IMODE(os.lstat(parent).st_mode))
            self.assertEqual(0o2700, before)

    @unittest.skipUnless(os.name == "posix", "POSIX directory permissions")
    def test_unsafe_symlink_and_unusable_existing_parents_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unsafe = root / "unsafe"
            unsafe.mkdir(mode=0o700)
            os.chmod(unsafe, 0o755)
            before = stat.S_IMODE(os.lstat(unsafe).st_mode)
            server = UnixControlServer(
                unsafe / "control.sock", lambda _op, _args: {}
            )
            with self.assertRaisesRegex(ControlUnavailable, "permissions"):
                server._prepare_directory()  # noqa: SLF001
            self.assertEqual(before, stat.S_IMODE(os.lstat(unsafe).st_mode))

            target = root / "target"
            target.mkdir(mode=0o700)
            link = root / "link"
            link.symlink_to(target, target_is_directory=True)
            linked_server = UnixControlServer(
                link / "control.sock", lambda _op, _args: {}
            )
            with self.assertRaisesRegex(ControlUnavailable, "not a directory"):
                linked_server._prepare_directory()  # noqa: SLF001
            self.assertEqual(0o700, stat.S_IMODE(os.lstat(target).st_mode))

            usable = root / "usable"
            usable.mkdir(mode=0o700)
            inaccessible_server = UnixControlServer(
                usable / "control.sock", lambda _op, _args: {}
            )
            with mock.patch(
                "tinypirelay.control_protocol.os.access", return_value=False
            ):
                with self.assertRaisesRegex(ControlUnavailable, "not writable"):
                    inaccessible_server._prepare_directory()  # noqa: SLF001


@unittest.skipUnless(hasattr(socket, "AF_UNIX") and os.name == "posix", "POSIX AF_UNIX")
class UnixLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.socket_path = Path(self.temporary.name) / "private" / "control.sock"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_permissions_round_trip_and_cleanup(self) -> None:
        server = UnixControlServer(
            self.socket_path,
            lambda operation, _arguments: {"operation": operation},
        ).start()
        try:
            result = ControlClient(self.socket_path).request("status")
            self.assertEqual({"operation": "status"}, result)
            self.assertEqual(
                0o700,
                stat.S_IMODE(os.stat(self.socket_path.parent).st_mode),
            )
            self.assertEqual(0o600, stat.S_IMODE(os.stat(self.socket_path).st_mode))
        finally:
            server.close()
        self.assertFalse(self.socket_path.exists())

    def test_non_socket_is_never_removed_and_active_socket_is_not_replaced(
        self,
    ) -> None:
        self.socket_path.parent.mkdir(mode=0o700)
        os.chmod(self.socket_path.parent, 0o700)
        self.socket_path.write_text("user data", encoding="utf-8")
        with self.assertRaises(ControlUnavailable):
            UnixControlServer(self.socket_path, lambda _op, _args: {}).start()
        self.assertEqual("user data", self.socket_path.read_text(encoding="utf-8"))
        self.socket_path.unlink()

        first = UnixControlServer(self.socket_path, lambda _op, _args: {}).start()
        try:
            with self.assertRaises(ControlUnavailable):
                UnixControlServer(self.socket_path, lambda _op, _args: {}).start()
            self.assertTrue(first.is_running)
        finally:
            first.close()

    def test_max_clients_bounds_slow_connections(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def handler(_operation: str, _arguments: dict[str, Any]) -> dict[str, bool]:
            entered.set()
            release.wait(2)
            return {"done": True}

        server = UnixControlServer(
            self.socket_path,
            handler,
            max_clients=1,
            timeout=1,
        ).start()
        first_result: list[Any] = []
        first = threading.Thread(
            target=lambda: first_result.append(
                ControlClient(self.socket_path, timeout=2).request("status")
            ),
            daemon=True,
        )
        first.start()
        try:
            self.assertTrue(entered.wait(1))
            deadline = time.monotonic() + 1
            while server.active_clients != 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            with self.assertRaises(ControlUnavailable):
                ControlClient(self.socket_path, timeout=0.5).request("status")
        finally:
            release.set()
            first.join(2)
            server.close()
        self.assertEqual([{"done": True}], first_result)


def _receive_line(connection: socket.socket) -> bytes:
    data = bytearray()
    while True:
        chunk = connection.recv(4096)
        if not chunk:
            raise AssertionError("fixture connection closed before newline")
        before, separator, after = chunk.partition(b"\n")
        data.extend(before)
        if separator:
            if after:
                raise AssertionError("fixture received unexpected trailing bytes")
            return bytes(data)


if __name__ == "__main__":
    unittest.main()
