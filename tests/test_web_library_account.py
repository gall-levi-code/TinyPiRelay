"""HTTP trust boundaries for file access, password changes and maintenance."""

import base64
import tempfile
from pathlib import Path
from unittest import mock

from test_web_service import FakeControl, WebServerCase
from tinypirelay.web_security import verify_credentials
from tinypirelay.web_service import (
    CredentialCommitUncertain, _byte_range, load_credential, write_credential_atomic,
)


class LibraryControl(FakeControl):
    data = b"fLaC" + b"test-audio" * 6000

    def __init__(self):
        super().__init__()
        self.file = {"file_id": "safe-id", "name": "test.flac", "size_bytes": len(self.data),
                     "can_play": True, "can_download": True, "status": "finalized"}

    def request(self, operation, arguments):
        if not operation.startswith("storage."):
            return super().request(operation, arguments)
        self.calls.append((operation, arguments))
        if operation == "storage.list":
            return {"items": [self.file], "page": arguments["page"], "page_size": 6,
                    "total": 1, "total_pages": 1}
        if operation == "storage.info":
            return {"file": self.file}
        if operation == "storage.read":
            offset, length = arguments["offset"], arguments["length"]
            return {"offset": offset, "size_bytes": len(self.data),
                    "data_base64": base64.b64encode(self.data[offset:offset + length]).decode()}
        return {"accepted": True}


class Maintenance:
    def __init__(self):
        self.calls = []
        self.busy = False

    def request(self, action):
        self.calls.append(action)
        return {"available": True, "busy": self.busy, "accepted": action != "status"}


class WebLibraryAccountTests(WebServerCase):
    def setUp(self):
        super().setUp()
        self.application.control = self.control = LibraryControl()
        self.application.maintenance = self.maintenance = Maintenance()
        self.directory = tempfile.TemporaryDirectory()
        self.application.credential_path = Path(self.directory.name) / "credential.json"
        write_credential_atomic(self.application.credential_path, self.credential)

    def tearDown(self):
        super().tearDown()
        self.directory.cleanup()

    def test_file_access_auth_ranges_head_and_query_validation(self):
        self.assertEqual(401, self.request("GET", "/api/storage/file?id=safe-id")[0])
        self.login()
        self.assertEqual(200, self.request("GET", "/api/storage?page=1")[0])
        for query in ("page=0", "page=1&page=2", "path=/etc/passwd", "page=1&bad=1"):
            self.assertEqual(400, self.request("GET", "/api/storage?" + query)[0])
        status, data, headers = self.request("GET", "/api/storage/file?id=safe-id",
                                             raw_headers={"Range": "bytes=2-12"})
        self.assertEqual(206, status)
        self.assertEqual(self.control.data[2:13].decode(), data)
        self.assertEqual(f"bytes 2-12/{len(self.control.data)}", dict(headers)["Content-Range"])
        self.assertEqual(416, self.request("GET", "/api/storage/file?id=safe-id",
                         raw_headers={"Range": "bytes=0-1,3-4"})[0])
        status, data, _ = self.request("HEAD", "/api/storage/file?id=safe-id")
        self.assertEqual((200, ""), (status, data))
        status, data, headers = self.request("GET", "/api/storage/file?id=safe-id&download=1")
        self.assertEqual(200, status)
        self.assertEqual(self.control.data.decode(), data)
        self.assertIn("attachment", dict(headers)["Content-Disposition"])
        self.assertTrue(all(call[1]["length"] <= 24576 for call in self.control.calls if call[0] == "storage.read"))
        for header, expected in ((None, (0, 5_000_000_000 - 1)), ("bytes=-10", (4_999_999_990, 4_999_999_999)),
                                 ("bytes=4294967296-", (4294967296, 4_999_999_999))):
            self.assertEqual(expected, _byte_range(header, 5_000_000_000))

    def test_active_file_and_csrf_protected_actions(self):
        self.login()
        self.control.file.update(can_play=False, can_download=False)
        self.assertEqual(409, self.request("GET", "/api/storage/file?id=safe-id")[0])
        self.assertFalse(any(op == "storage.read" for op, _ in self.control.calls))
        body = {"action": "delete", "file_id": "safe-id"}
        self.assertEqual(403, self.request("POST", "/api/storage/action", body)[0])
        self.assertEqual(200, self.request("POST", "/api/storage/action", body, csrf=True)[0])
        body["action"] = "recover"
        self.assertEqual(403, self.request("POST", "/api/storage/action", body)[0])
        self.assertEqual(200, self.request("POST", "/api/storage/action", body, csrf=True)[0])
        self.assertIn(("storage.recover", {"file_id": "safe-id"}), self.control.calls)
        body["action"] = "overwrite"
        self.assertEqual(400, self.request("POST", "/api/storage/action", body, csrf=True)[0])

    def test_password_requires_current_password_and_revokes_all_sessions(self):
        self.login()
        other = self.application.sessions.create("operator")
        body = {"current_password": "wrong", "new_password": "new correct horse password",
                "confirm_password": "new correct horse password"}
        self.assertEqual(403, self.request("POST", "/api/account/password", body)[0])
        self.assertEqual(401, self.request("POST", "/api/account/password", body, csrf=True)[0])
        body["current_password"] = "correct horse battery staple"
        status, payload, headers = self.request("POST", "/api/account/password", body, csrf=True)
        self.assertEqual(200, status)
        self.assertTrue(payload["changed"])
        self.assertFalse(payload["authenticated"])
        self.assertEqual(0, self.application.sessions.snapshot()["active_sessions"])
        self.assertIsNone(self.application.sessions.authenticate(other.bearer_token))
        record = load_credential(self.application.credential_path)
        self.assertTrue(verify_credentials(record, "operator", body["new_password"]))
        self.assertFalse(verify_credentials(record, "operator", body["current_password"]))
        self.assertEqual(401, self.request("GET", "/api/status")[0])

    def test_password_failure_preserves_old_and_uncertain_commit_revokes(self):
        self.login()
        body = {"current_password": "correct horse battery staple", "new_password": "new correct horse password",
                "confirm_password": "new correct horse password"}
        with mock.patch("tinypirelay.web_service.write_credential_atomic", side_effect=OSError()):
            self.assertEqual(500, self.request("POST", "/api/account/password", body, csrf=True)[0])
        self.assertEqual(200, self.request("GET", "/api/status")[0])
        self.assertIs(self.credential, self.application.credential)
        with mock.patch("tinypirelay.web_service._sync_credential_parent", side_effect=OSError()):
            status, payload, _ = self.request("POST", "/api/account/password", body, csrf=True)
        self.assertEqual(200, status)
        self.assertFalse(payload["durability_confirmed"])
        self.assertEqual(0, self.application.sessions.snapshot()["active_sessions"])
        self.assertTrue(verify_credentials(load_credential(self.application.credential_path), "operator", body["new_password"]))

    def test_maintenance_requires_csrf_password_confirmation_and_busy_blocks_start(self):
        self.login()
        body = {"action": "system.reboot", "current_password": "wrong", "confirm": True}
        self.assertEqual(403, self.request("POST", "/api/maintenance", body)[0])
        self.assertEqual(401, self.request("POST", "/api/maintenance", body, csrf=True)[0])
        self.assertEqual([], self.maintenance.calls)
        body["current_password"] = "correct horse battery staple"
        body["confirm"] = False
        self.assertEqual(400, self.request("POST", "/api/maintenance", body, csrf=True)[0])
        body["confirm"] = True
        self.assertEqual(202, self.request("POST", "/api/maintenance", body, csrf=True)[0])
        self.assertEqual(["system.reboot"], self.maintenance.calls)
        self.maintenance.busy = True
        self.assertEqual(409, self.request("POST", "/api/control", {"action": "stream.start"}, csrf=True)[0])
        self.assertEqual(200, self.request("POST", "/api/control", {"action": "stream.stop"}, csrf=True)[0])
