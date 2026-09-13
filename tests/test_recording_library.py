from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tinypirelay.media_config import RecordingConfig
from tinypirelay.recording_library import (
    MAX_READ_BYTES, PAGE_SIZE, RecordingLibrary, RecordingLibraryError, _streaminfo,
)


def flac_header(samples=48000, rate=48000, channels=2, bits=24):
    packed = (rate << 44) | ((channels - 1) << 41) | ((bits - 1) << 36) | samples
    streaminfo = b"\x10\0\x10\0" + b"\0" * 6 + packed.to_bytes(8, "big") + b"\0" * 16
    return b"fLaC\x80\0\0\x22" + streaminfo


class StreamInfoTests(unittest.TestCase):
    def test_rotating_and_draining_fragments_remain_protected(self):
        recording = {"state": "running", "current_file": "/recordings/two.flac",
                     "active_files": ["/recordings/one.flac", "/recordings/two.flac"]}
        library = RecordingLibrary(lambda: None, lambda: {"state": {"recording": recording}})
        self.assertTrue(library._protected("/recordings", "one.flac"))
        self.assertTrue(library._protected("/recordings", "two.flac"))
        self.assertFalse(library._protected("/recordings", "closed.flac"))
        recording["rotation_pending"] = True
        self.assertTrue(library._protected("/recordings", "not-yet-announced.flac"))
        recording["rotation_pending"] = False
        recording.update(state="stopped", current_file=None, active_files=["/recordings/one.flac"])
        self.assertTrue(library._protected("/recordings", "one.flac"))
        self.assertFalse(library._protected("/recordings", "two.flac"))
        recording["active_files"] = []
        self.assertFalse(library._protected("/recordings", "one.flac"))

    def test_header_is_metadata_not_verification(self):
        self.assertEqual(_streaminfo(flac_header()), dict(duration_seconds=1.0,
            sample_rate_hz=48000, channels=2, bits_per_sample=24))
        self.assertIsNone(_streaminfo(flac_header(samples=0))["duration_seconds"])
        for invalid in (b"", b"not flac" * 10, flac_header()[:41], b"fLaC\x01" + flac_header()[5:]):
            self.assertIsNone(_streaminfo(invalid)["sample_rate_hz"])


@unittest.skipUnless(os.name == "posix", "Secure recording descriptors are Linux-only")
class RecordingLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.config = RecordingConfig(str(self.path), 300, None)
        self.recording = {"state": "stopped", "current_file": None}
        self.library = RecordingLibrary(lambda: self.config, lambda: {"state": {"recording": dict(self.recording)}})

    def tearDown(self):
        self.temp.cleanup()

    def add(self, name="one.flac", data=None):
        path = self.path / name
        path.write_bytes(flac_header() + b"fixture payload" if data is None else data)
        return path

    def item(self, name="one.flac"):
        return next(item for item in self.library.list(1)["items"] if item["name"] == name)

    def assert_error(self, code, call, *args):
        with self.assertRaises(RecordingLibraryError) as result:
            call(*args)
        self.assertEqual(result.exception.code, code)
        self.assertNotIn(str(self.path), result.exception.message)

    def test_pagination_metadata_and_protocol_bound(self):
        for index in range(8):
            self.add(f"{index}.flac")
        first, second = self.library.list(1), self.library.list(2)
        self.assertEqual((first["total"], first["page_size"], first["total_pages"]), (8, PAGE_SIZE, 2))
        self.assertEqual((len(first["items"]), len(second["items"])), (6, 2))
        item = first["items"][0]
        self.assertEqual(item["status"], "finalized")
        self.assertEqual(item["check"]["state"], "unchecked")
        self.assertEqual(item["duration_seconds"], 1)
        self.assertNotIn(str(self.path), json.dumps(first))
        self.assert_error("invalid_arguments", self.library.list, True)
        self.assert_error("invalid_arguments", self.library.list, 3)
        large = self.add("large.flac", flac_header() + b"x" * 50000)
        value = self.item("large.flac")
        chunk = self.library.read(value["file_id"], 0, MAX_READ_BYTES)
        self.assertEqual(base64.b64decode(chunk["data_base64"]), large.read_bytes()[:MAX_READ_BYTES])
        self.assertLess(len(json.dumps({"ok": True, "result": chunk}).encode()), 65536)
        self.assertFalse(chunk["eof"])
        self.assertTrue(self.library.read(value["file_id"], value["size_bytes"], 1)["eof"])
        self.assert_error("invalid_arguments", self.library.read, value["file_id"], 0, MAX_READ_BYTES + 1)
        self.assert_error("invalid_range", self.library.read, value["file_id"], value["size_bytes"] + 1, 1)

    def test_active_and_unfinalized_recordings(self):
        path = self.add(data=flac_header(samples=0) + b"payload")
        closed = self.item()
        self.assertEqual(closed["status"], "needs_check")
        self.assertFalse(closed["can_play"])
        self.assertTrue(closed["can_download"])
        self.recording.update(state="running", current_file=str(path))
        active = self.item()
        self.assertEqual(active["status"], "active")
        for flag in ("can_play", "can_download", "can_delete", "can_check"):
            self.assertFalse(active[flag])
        self.assert_error("active_recording", self.library.read, active["file_id"], 0, 10)
        self.assert_error("active_recording", self.library.delete, active["file_id"])
        self.assert_error("recording_busy", self.library.check, active["file_id"])
        self.recording.update(current_file=None)
        self.assertEqual(self.item()["status"], "active")

    def test_rotation_protects_both_files_from_reads_and_deletion(self):
        old = self.add("old.flac")
        new = self.add("new.flac")
        self.recording.update(state="running", current_file=str(new), active_files=[str(old), str(new)])
        for name in (old.name, new.name):
            item = self.item(name)
            self.assertFalse(item["can_delete"])
            self.assertFalse(item["can_download"])
            self.assert_error("active_recording", self.library.read, item["file_id"], 0, 10)
            self.assert_error("active_recording", self.library.delete, item["file_id"])

    def test_symlinks_hardlinks_fifo_and_replaced_file_rejected(self):
        path = self.add()
        (self.path / "symbolic.flac").symlink_to(path)
        os.mkfifo(self.path / "pipe.flac")
        hard = self.add("hard.flac")
        os.link(hard, self.path / "hard2.flac")
        result = self.library.list(1)
        self.assertEqual([item["name"] for item in result["items"]], ["one.flac"])
        self.assertEqual(result["skipped_unsafe"], 4)
        file_id = result["items"][0]["file_id"]
        path.unlink()
        path.symlink_to(hard)
        self.assert_error("stale_file", self.library.read, file_id, 0, 1)
        self.assert_error("stale_file", self.library.delete, file_id)
        self.assertTrue(hard.exists())
        self.assert_error("invalid_arguments", self.library.info, "../../etc/passwd")

    def test_stale_versions_mount_changes_and_catalog_cap(self):
        path = self.add()
        file_id = self.item()["file_id"]
        path.write_bytes(path.read_bytes() + b"changed")
        self.assert_error("stale_file", self.library.info, file_id)
        self.config = RecordingConfig(str(self.path), 300, str(self.path))
        self.assert_error("storage_unavailable", self.library.list, 1)
        self.config = RecordingConfig(str(self.path), 300, None)
        with mock.patch("tinypirelay.recording_library.MAX_DIRECTORY_ENTRIES", 1):
            self.add("second.flac")
            self.assert_error("library_too_large", self.library.list, 1)
        with tempfile.TemporaryDirectory() as elsewhere:
            link = Path(elsewhere) / "link"
            link.symlink_to(self.path, target_is_directory=True)
            self.config = RecordingConfig(str(link), 300, None)
            self.assert_error("storage_unavailable", self.library.list, 1)

    def test_delete_is_exact_and_full_disk_does_not_block_read(self):
        path = self.add()
        other = self.add("other.flac")
        file_id = self.item()["file_id"]
        with mock.patch("tinypirelay.recording_library.check_recording_destination",
                        return_value=SimpleNamespace(reason="storage_threshold", st_dev=path.stat().st_dev)):
            self.assertEqual(self.library.info(file_id)["file"]["file_id"], file_id)
            self.assertTrue(self.library.delete(file_id)["deleted"])
        self.assertFalse(path.exists())
        self.assertTrue(other.exists())
        self.assert_error("stale_file", self.library.info, file_id)

    def test_directory_writable_by_other_users_is_rejected(self):
        self.add()
        self.path.chmod(0o777)
        try:
            self.assert_error("storage_unavailable", self.library.list, 1)
        finally:
            self.path.chmod(0o700)

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0,
                         "Requires isolated Linux root to create a separate unprivileged identity")
    def test_execute_only_ancestor_works_as_unprivileged_media_user(self):
        self.path.chmod(0o755)
        parent = self.path / "private"
        directory = parent / "recordings"
        parent.mkdir()
        directory.mkdir()
        os.chown(parent, 0, 65534)
        parent.chmod(0o710)
        os.chown(directory, 65534, 65534)
        directory.chmod(0o750)
        recording = directory / "fixture.flac"
        recording.write_bytes(flac_header() + b"fixture payload")
        os.chown(recording, 65534, 65534)
        script = """
import os, sys
from tinypirelay.media_config import RecordingConfig
from tinypirelay.recording_library import RecordingLibrary, RecordingLibraryError
parent, directory = sys.argv[1:]
# A staged checkout may deliberately be root-only. Import first, then drop
# every privilege before testing the recording filesystem access boundary.
os.setgroups([])
os.setgid(65534)
os.setuid(65534)
assert os.geteuid() == 65534
try:
    os.listdir(parent)
except PermissionError:
    pass
else:
    raise AssertionError('regression fixture unexpectedly permits ancestor listing')
config = RecordingConfig(directory, 300, None)
library = RecordingLibrary(lambda: config, lambda: {'state': {'recording': {'state': 'stopped'}}})
item = library.list(1)['items'][0]
assert item['name'] == 'fixture.flac'
assert library.info(item['file_id'])['file']['file_id'] == item['file_id']
assert library.read(item['file_id'], 0, 4)['data_base64'] == 'ZkxhQw=='
assert library.delete(item['file_id'])['deleted']
os.symlink(directory, directory + '/link')
config = RecordingConfig(directory + '/link', 300, None)
try:
    library.list(1)
except RecordingLibraryError as exc:
    assert exc.code == 'storage_unavailable'
else:
    raise AssertionError('symlink traversal was accepted')
"""
        result = subprocess.run([sys.executable, "-B", "-c", script, str(parent), str(directory)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(parent.stat().st_mode & 0o777, 0o710)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o750)

    def test_control_dispatch_strict_arguments_and_read_lock_bypass(self):
        from tinypirelay.media_control import MediaControlError, MediaControlHandler
        self.add()
        handler = MediaControlHandler(SimpleNamespace(snapshot=lambda: {"state": {"recording": self.recording}}),
            SimpleNamespace(recording=self.config), self.path / "unused.json", "fixture", (), None)
        with handler._operation_lock:
            result = handler("storage.list", {"page": 1})
            file_id = result["items"][0]["file_id"]
            self.assertEqual(handler("storage.info", {"file_id": file_id})["file"]["file_id"], file_id)
            self.assertTrue(handler("storage.read", {"file_id": file_id, "offset": 0, "length": 1})["data_base64"])
        for operation, arguments in (("storage.list", {}), ("storage.read", {"file_id": file_id}),
                                     ("storage.delete", {"file_id": file_id, "path": "ignored"})):
            with self.assertRaises(MediaControlError) as error:
                handler(operation, arguments)
            self.assertEqual(error.exception.code, "invalid_arguments")
        with self.assertRaises(MediaControlError) as error:
            handler("storage.info", {"file_id": "0" * 64})
        self.assertEqual(error.exception.code, "stale_file")

    def wait_check(self, file_id):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = self.library.info(file_id)["file"]["check"]
            if result["state"] != "checking":
                return result
            time.sleep(.01)
        self.fail("check worker did not finish")

    def test_decoder_unavailable_and_success(self):
        self.add()
        file_id = self.item()["file_id"]
        with mock.patch.object(self.library, "_decoder", return_value=None):
            self.assertFalse(self.library.check(file_id)["accepted"])
            self.assertEqual(self.library.info(file_id)["file"]["check"]["state"], "unavailable")
        process = mock.Mock()
        process.wait.return_value = 0
        process.poll.return_value = 0
        with mock.patch.object(self.library, "_decoder", return_value=(["test-decoder"], "flac-test")), \
             mock.patch("tinypirelay.recording_library.subprocess.Popen", return_value=process) as launch:
            self.assertTrue(self.library.check(file_id)["accepted"])
            result = self.wait_check(file_id)
            self.assertEqual(result["state"], "passed")
            self.assertEqual(result["method"], "flac-test")
            self.assertNotIn(str(self.path), str(launch.call_args.args))
            self.assertNotIn("shell", launch.call_args.kwargs)

    def test_fallback_decoder_success_does_not_verify_completeness(self):
        self.add(data=flac_header(samples=0) + b"fixture payload")
        file_id = self.item()["file_id"]
        process = mock.Mock()
        process.wait.return_value = process.poll.return_value = 0
        for method in ("ffmpeg-decode", "gstreamer-decode"):
            with self.subTest(method=method), \
                 mock.patch.object(self.library, "_decoder", return_value=(["test-decoder"], method)), \
                 mock.patch("tinypirelay.recording_library.subprocess.Popen", return_value=process):
                self.library.check(file_id)
                result = self.wait_check(file_id)
                self.assertEqual(result["state"], "decoded")
                self.assertIn("completeness is unverified", result["message"])
                value = self.library.info(file_id)["file"]
                self.assertEqual(value["status"], "needs_check")
                self.assertFalse(value["can_play"])

    def test_check_start_failure_releases_busy_state_and_descriptor(self):
        self.add()
        file_id = self.item()["file_id"]
        real_dup = os.dup
        for target in ("os.dup", "threading.Thread", "threading.Thread.start"):
            duplicated = []
            def duplicate(descriptor):
                duplicated.append(real_dup(descriptor))
                return duplicated[-1]
            with self.subTest(target=target), \
                 mock.patch.object(self.library, "_decoder", return_value=(["test-decoder"], "flac-test")), \
                 mock.patch("tinypirelay.recording_library.os.dup", side_effect=duplicate), \
                 mock.patch("tinypirelay.recording_library." + target, side_effect=OSError("fixture resource exhaustion")):
                self.assert_error("stale_file", self.library.check, file_id)
                self.assertIsNone(self.library._checking)
                value = self.library.info(file_id)["file"]
                self.assertEqual(value["check"]["state"], "unchecked")
                self.assertTrue(value["can_delete"])
                self.assertTrue(value["can_check"])
                if target != "os.dup":
                    with self.assertRaises(OSError):
                        os.fstat(duplicated[0])

    def test_single_worker_cancel_on_recording_and_timeout(self):
        self.add()
        file_id = self.item()["file_id"]
        entered, release = threading.Event(), threading.Event()
        process = mock.Mock()
        process.poll.return_value = None
        def wait(timeout=None):
            if timeout is None:
                return -9
            entered.set()
            release.wait(2)
            raise subprocess.TimeoutExpired("fixture", timeout)
        process.wait.side_effect = wait
        with mock.patch.object(self.library, "_decoder", return_value=(["test-decoder"], "fixture-decode")), \
             mock.patch("tinypirelay.recording_library.subprocess.Popen", return_value=process):
            self.library.check(file_id)
            self.assertTrue(entered.wait(2))
            self.assert_error("check_in_progress", self.library.check, file_id)
            self.assert_error("check_in_progress", self.library.delete, file_id)
            self.recording["state"] = "running"
            release.set()
            result = self.wait_check(file_id)
            self.assertEqual(result["state"], "cancelled")
            self.assertEqual(self.library.info(file_id)["file"]["status"], "active")
            self.assertIn("cancelled", result["message"])
            process.kill.assert_called_once()
        self.recording["state"] = "stopped"
        with mock.patch.object(self.library, "_decoder", return_value=(["test-decoder"], "fixture-decode")), \
             mock.patch("tinypirelay.recording_library.subprocess.Popen", return_value=process), \
             mock.patch("tinypirelay.recording_library.CHECK_TIMEOUT_SECONDS", 0):
            self.library.check(file_id)
            self.assertEqual(self.wait_check(file_id)["state"], "timeout")

    def test_decoder_error_never_marks_file_verified(self):
        self.add()
        file_id = self.item()["file_id"]
        process = mock.Mock()
        process.wait.return_value, process.poll.return_value = 1, 1
        with mock.patch.object(self.library, "_decoder", return_value=(["fixture"], "fixture-decode")), \
             mock.patch("tinypirelay.recording_library.subprocess.Popen", return_value=process):
            self.library.check(file_id)
            self.assertEqual(self.wait_check(file_id)["state"], "failed")
            value = self.library.info(file_id)["file"]
            self.assertEqual(value["status"], "needs_check")
            self.assertFalse(value["can_play"])
            self.assertTrue(value["can_download"])


if __name__ == "__main__":
    unittest.main()
