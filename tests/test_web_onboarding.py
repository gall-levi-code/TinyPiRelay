from __future__ import annotations

import concurrent.futures
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_web_service import WebServerCase
from tinypirelay.web_security import CredentialRecord, SecurityError, verify_credentials
from tinypirelay.web_service import (
    WebApplication, _http_authority, _local_network_peer, load_web_credential,
    write_credential_atomic, _password_command,
)


class WebOnboardingTests(WebServerCase):
    def setUp(self):
        super().setUp()
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "credential.json"
        self.marker = self.path.with_name("setup-pending")
        self.marker.write_bytes(b"1\n")
        self.marker.chmod(0o600)
        self.application = WebApplication(
            None, self.control, credential_path=self.path, setup_pending_path=self.marker,
        )
        self.server.application = self.application
        self.body = {"username": "operator", "password": "correct horse battery staple",
                     "confirm_password": "correct horse battery staple"}

    def tearDown(self):
        super().tearDown()
        self.directory.cleanup()

    def test_first_access_create_then_normal_login_and_restart(self):
        status, payload, _ = self.request("GET", "/api/session")
        self.assertEqual((200, {"authenticated": False, "setup_required": True}), (status, payload))
        self.assertEqual(200, self.request("GET", "/")[0])
        for route in ("/api/status", "/api/capabilities", "/api/config", "/api/storage", "/api/events", "/api/maintenance"):
            self.assertEqual(401, self.request("GET", route)[0])
        for route in ("/api/control", "/api/storage/action", "/api/maintenance", "/api/account/password"):
            self.assertEqual(401, self.request("POST", route, {})[0])
        self.assertEqual([], self.control.calls)
        self.assertEqual(409, self.request("POST", "/api/login", self.body)[0])
        status, created, _ = self.request("POST", "/api/setup", self.body)
        self.assertEqual(201, status)
        self.assertFalse(created["authenticated"])
        self.assertFalse(self.marker.exists())
        self.assertNotIn("setup_required", self.request("GET", "/api/session")[1])
        self.login()
        self.assertTrue(self.request("GET", "/api/session")[1]["authenticated"])
        self.assertEqual(409, self.request("POST", "/api/setup", self.body)[0])
        persisted = load_web_credential(self.path, self.marker)
        self.assertTrue(verify_credentials(persisted, "operator", self.body["password"]))
        self.path.unlink()
        with self.assertRaises(SecurityError):
            load_web_credential(self.path, self.marker)

    def test_explicit_password_cli_consumes_first_run_marker(self):
        with mock.patch("tinypirelay.web_service.open_controlling_tty", return_value=io.StringIO()), \
                mock.patch("tinypirelay.web_service.getpass.getpass", return_value=self.body["password"]), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(0, _password_command(self.path, "operator"))
        self.assertFalse(self.marker.exists())
        self.assertTrue(verify_credentials(load_web_credential(self.path, self.marker), "operator", self.body["password"]))

    def test_missing_marker_and_corrupt_credential_fail_closed(self):
        self.marker.unlink()
        with self.assertRaises(SecurityError):
            load_web_credential(self.path, self.marker)
        self.marker.write_bytes(b"1\n")
        self.marker.chmod(0o600)
        self.path.write_bytes(b"corrupt")
        self.path.chmod(0o600)
        with self.assertRaises(SecurityError):
            load_web_credential(self.path, self.marker)
        self.assertEqual(400, self.request("POST", "/api/setup", self.body)[0])
        self.assertEqual(b"corrupt", self.path.read_bytes())

    def test_existing_credential_wins_after_interrupted_marker_cleanup(self):
        write_credential_atomic(self.path, self.credential)
        self.assertEqual("operator", load_web_credential(self.path, self.marker).username)
        self.assertFalse(self.marker.exists())
        self.marker.write_bytes(b"not-a-setup-marker")
        self.assertEqual("operator", load_web_credential(self.path, self.marker).username)
        self.assertEqual(b"not-a-setup-marker", self.marker.read_bytes())
        with self.assertRaises(SecurityError):
            load_web_credential(self.path, self.path)
        self.assertTrue(self.path.exists())

    def test_invalid_marker_is_never_registration_authority(self):
        for content in (b"", b"0\n", b"1", b"1\nextra"):
            self.marker.write_bytes(content)
            with self.assertRaises(SecurityError):
                load_web_credential(self.path, self.marker)
        if os.name == "posix":
            self.marker.write_bytes(b"1\n")
            self.marker.chmod(0o644)
            with self.assertRaises(SecurityError):
                load_web_credential(self.path, self.marker)

    def test_competing_submissions_create_exactly_one_account(self):
        def submit(username):
            return username, self.request("POST", "/api/setup", {**self.body, "username": username})[0]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, ("first", "second")))
        self.assertEqual([201, 409], sorted(status for _, status in results))
        winner = next(username for username, status in results if status == 201)
        self.assertEqual(winner, load_web_credential(self.path, self.marker).username)

    def test_atomic_create_cannot_overwrite_an_external_winner(self):
        winner = CredentialRecord.create("external", self.body["password"])
        def racing_write(path, record, **kwargs):
            write_credential_atomic(path, winner)
            return write_credential_atomic(path, record, **kwargs)
        with mock.patch("tinypirelay.web_service.write_credential_atomic", side_effect=racing_write):
            self.assertEqual(409, self.request("POST", "/api/setup", self.body)[0])
        self.assertEqual("external", load_web_credential(self.path, self.marker).username)

    def test_invalid_input_is_bounded_and_rate_limited(self):
        self.assertEqual(413, self.request("POST", "/api/setup", b"x" * 40000,
                                         raw_headers={"Content-Type": "application/json"})[0])
        for _ in range(5):
            self.assertEqual(400, self.request("POST", "/api/setup", {**self.body, "password": "short"})[0])
        self.assertEqual(429, self.request("POST", "/api/setup", self.body)[0])
        self.assertFalse(self.path.exists())
        self.assertTrue(self.marker.exists())

    def test_failed_write_can_retry_without_overwriting(self):
        with mock.patch("tinypirelay.web_service.write_credential_atomic", side_effect=OSError("disk failure")):
            self.assertEqual(500, self.request("POST", "/api/setup", self.body)[0])
        self.assertIsNone(self.application.credential)
        self.assertTrue(self.marker.exists())
        self.assertEqual(201, self.request("POST", "/api/setup", self.body)[0])

    def test_directory_sync_uncertainty_keeps_created_account_not_registration(self):
        with mock.patch("tinypirelay.web_service._sync_credential_parent", side_effect=OSError("sync failed")):
            status, payload, _ = self.request("POST", "/api/setup", self.body)
        self.assertEqual(201, status)
        self.assertFalse(payload["durability_confirmed"])
        self.assertIsNotNone(self.application.credential)
        self.assertEqual(409, self.request("POST", "/api/setup", self.body)[0])
        self.assertTrue(verify_credentials(load_web_credential(self.path, self.marker), "operator", self.body["password"]))

    def test_host_and_origin_defenses_preserve_ssh_tunnel_ports(self):
        for route in ("/api/setup", "/api/login"):
            for headers in ({"Origin": "http://evil.example"}, {"Origin": "null"},
                            {"Sec-Fetch-Site": "same-site"}, {"Sec-Fetch-Site": "cross-site"}):
                self.assertEqual(403, self.request("POST", route, self.body, raw_headers=headers)[0])
        self.assertEqual(403, self.request("GET", "/", raw_headers={"Host": "evil.example"})[0])
        self.assertEqual(403, self.request("GET", "/", raw_headers={"Host": "localhost@evil.example"})[0])
        headers = {"Host": "127.0.0.1:18082", "Origin": "http://127.0.0.1:18082", "Sec-Fetch-Site": "same-origin"}
        self.assertEqual(201, self.request("POST", "/api/setup", self.body, raw_headers=headers)[0])
        self.assertEqual(200, self.request("POST", "/api/login", {key: self.body[key] for key in ("username", "password")}, raw_headers=headers)[0])
        self.assertEqual(("::1", 18082), _http_authority("[::1]:18082"))
        self.assertEqual(("localhost", 80), _http_authority("LOCALHOST."))
        for address in ("127.0.0.1", "192.168.1.10", "10.1.2.3", "172.31.1.2", "::1", "fc00::1", "fe80::1", "::ffff:192.168.1.10"):
            self.assertTrue(_local_network_peer(address))
        for address in ("8.8.8.8", "172.32.0.1", "100.64.0.1", "2001:4860:4860::8888"):
            self.assertFalse(_local_network_peer(address))
        self.application.lan_only = True
        with mock.patch("tinypirelay.web_service._local_network_peer", return_value=False):
            self.assertEqual(403, self.request("GET", "/")[0])


if __name__ == "__main__":
    unittest.main()
