"""Recovery safety checks; real decoder cases use only synthetic audio."""

import hashlib
import io
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tinypirelay.media_config import RecordingConfig
from tinypirelay.recording_library import RecordingLibrary, RecordingLibraryError, _streaminfo
from test_recording_library import flac_header


MODULE = "tinypirelay.recording_library."
LINUX = os.name == "posix" and hasattr(os, "O_PATH")


class RecoveryCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.config = RecordingConfig(str(self.path), 300, None)
        self.recording = {"state": "stopped", "current_file": None}
        self.library = RecordingLibrary(lambda: self.config,
            lambda: {"state": {"recording": dict(self.recording)}})
        self.source = self.path / "original.flac"

    def add(self, data=None):
        self.source.write_bytes(flac_header(samples=0) + b"synthetic fixture" if data is None else data)
        self.original = self.source.read_bytes()
        self.file_id = self.library.list(1)["items"][0]["file_id"]

    def assert_error(self, code, function, *args):
        with self.assertRaises(RecordingLibraryError) as error:
            function(*args)
        self.assertEqual(error.exception.code, code)
        self.assertNotIn(str(self.path), error.exception.message)

    def assert_original_only(self):
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual(list(self.path.iterdir()), [self.source])
        self.assertIsNone(self.library._checking)


@unittest.skipUnless(LINUX, "Recovery requires Linux descriptor access")
class RecoveryTests(RecoveryCase):
    def setUp(self):
        super().setUp()
        self.add()
        self.which = mock.patch(MODULE + "shutil.which", side_effect=lambda name: "/fixture/ffmpeg" if name == "ffmpeg" else None)
        self.which.start()
        self.addCleanup(self.which.stop)
        self.digest = hashlib.md5(b"synthetic decoded PCM").digest()
        self.output = flac_header()[:26] + self.digest + b"synthetic encoded frames"

    def start(self):
        # Capture the worker so failures and publication assertions run in this test's thread.
        with mock.patch(MODULE + "threading.Thread") as worker:
            accepted = self.library.recover(self.file_id)
        self.assertTrue(accepted["accepted"])
        self.assertEqual(accepted["check"]["state"], "recovering")
        return worker.call_args.kwargs["args"]

    def launch(self, command, *, stdin, stdout, **kwargs):
        self.assertNotIn("shell", kwargs)
        self.assertNotIn(str(self.source), " ".join(command))
        process = mock.Mock(returncode=0, stdout=None)
        process.poll.return_value = 0
        if stdout == subprocess.PIPE:
            self.assertIn("-xerror", command)
            self.assertIn("pcm_s24le", command)
            self.assertEqual(list(self.path.glob("tinypirelay-recovered-*.flac")), [])
            self.assertEqual(self.source.read_bytes(), self.original)
            self.assertFalse(self.library.info(self.file_id)["file"]["can_delete"])
            self.assert_error("check_in_progress", self.library.recover, self.file_id)
            self.assert_error("check_in_progress", self.library.check, self.file_id)
            self.assert_error("check_in_progress", self.library.delete, self.file_id)
            process.stdout = io.BytesIO(b"MD5=" + self.digest.hex().encode("ascii") + b"\n")
        else:
            self.assertNotIn("-xerror", command)
            os.write(stdout, self.output)
        return process

    def run_worker(self, launch=None):
        args = self.start()
        with mock.patch(MODULE + "subprocess.Popen", side_effect=launch or self.launch):
            self.library._recover_worker(*args)
        return self.library._checks[self.file_id]

    def test_publishes_separate_private_copy_only_after_checksum(self):
        result = self.run_worker()
        self.assertEqual(result["state"], "recovered")
        self.assertTrue(result["durability_confirmed"])
        copy = self.path / result["output_name"]
        self.assertEqual(copy.read_bytes(), self.output)
        self.assertEqual(copy.stat().st_mode & 0o777, 0o600)
        self.assertEqual(copy.stat().st_nlink, 1)
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual(set(self.path.iterdir()), {self.source, copy})
        self.assertIsNone(self.library._checking)

    def test_empty_unfinalized_and_changed_format_outputs_rejected(self):
        for output in (b"", flac_header(samples=0), flac_header(bits=16) + b"frames"):
            with self.subTest(output_size=len(output)):
                self.output = output
                self.assertEqual(self.run_worker()["state"], "failed")
                self.assert_original_only()

    def test_checksum_mismatch_and_decoder_failure_never_publish(self):
        self.digest = b"\xff" * 16
        self.assertIn("checksum", self.run_worker()["message"])
        self.assert_original_only()
        for stage in ("encode", "validate"):
            def fail(command, **kwargs):
                process = self.launch(command, **kwargs)
                if (kwargs["stdout"] == subprocess.PIPE) == (stage == "validate"):
                    process.returncode = process.poll.return_value = 1
                return process
            with self.subTest(stage=stage):
                self.assertEqual(self.run_worker(fail)["state"], "failed")
                self.assert_original_only()

    def test_missing_ffmpeg_disables_recovery_without_starting_worker(self):
        with mock.patch(MODULE + "shutil.which", return_value=None), \
             mock.patch(MODULE + "threading.Thread") as thread:
            item = self.library.info(self.file_id)["file"]
            self.assertFalse(item["can_recover"])
            self.assertIn("FFmpeg", item["recovery_unavailable_reason"])
            result = self.library.recover(self.file_id)
            self.assertFalse(result["accepted"])
            self.assertEqual(result["check"]["state"], "unavailable")
            thread.assert_not_called()
        self.assert_original_only()

    def test_recording_and_insufficient_space_block_start(self):
        for recording in ({"state": "running"}, {"state": "stopped", "active_files": ["other.flac"]},
                          {"state": "stopped", "rotation_pending": True}):
            with self.subTest(recording=recording):
                self.recording = recording
                self.assert_error("recording_busy", self.library.recover, self.file_id)
        self.recording = {"state": "stopped"}
        with mock.patch.object(self.library, "_recovery_space", return_value=0):
            self.assert_error("storage_unavailable", self.library.recover, self.file_id)
        self.assert_original_only()

    def test_non_linux_recovery_is_rejected(self):
        with mock.patch(MODULE + "os.name", "nt"):
            self.assert_error("storage_unavailable", self.library.recover, self.file_id)
        self.assert_original_only()

    def test_running_process_cancelled_when_recording_starts(self):
        process = mock.Mock(stdout=None)
        process.poll.return_value = None
        def launch(*args, **kwargs):
            os.write(kwargs["stdout"], self.output)
            self.recording["state"] = "running"
            return process
        result = self.run_worker(launch)
        self.assertEqual(result["state"], "cancelled")
        process.kill.assert_called_once()
        process.wait.assert_called_once()
        self.assert_original_only()

    def test_configuration_change_cancels_before_publication(self):
        def launch(command, **kwargs):
            process = self.launch(command, **kwargs)
            if kwargs["stdout"] == subprocess.PIPE:
                self.config = RecordingConfig(str(self.path), 600, None)
            return process
        self.assertEqual(self.run_worker(launch)["state"], "cancelled")
        self.assert_original_only()

    def test_timeout_cleans_up(self):
        with mock.patch(MODULE + "CHECK_TIMEOUT_SECONDS", 0):
            self.assertEqual(self.run_worker()["state"], "timeout")
        self.assert_original_only()

    def test_space_exhaustion_and_output_limit_clean_up(self):
        space = mock.Mock(return_value=1024 * 1024)
        def launch(command, **kwargs):
            process = self.launch(command, **kwargs)
            space.return_value = 0
            return process
        with mock.patch.object(self.library, "_recovery_space", space):
            self.assertEqual(self.run_worker(launch)["state"], "failed")
        self.assert_original_only()
        with mock.patch(MODULE + "RECOVERY_MAX_BYTES", len(self.output)):
            result = self.run_worker()
            self.assertEqual(result["state"], "failed")
            self.assertIn("limit", result["message"])
        self.assert_original_only()

    def test_start_failure_closes_every_duplicate_and_releases_busy_state(self):
        real_dup = os.dup
        for failure in (1, 2, "thread", "start"):
            descriptors = []
            def duplicate(descriptor):
                if failure == len(descriptors) + 1:
                    raise OSError("fixture descriptor exhaustion")
                descriptors.append(real_dup(descriptor))
                return descriptors[-1]
            target = "threading.Thread" if failure == "thread" else "threading.Thread.start"
            with self.subTest(failure=failure), \
                 mock.patch(MODULE + "os.dup", side_effect=duplicate), \
                 mock.patch(MODULE + target, side_effect=OSError("fixture thread exhaustion")):
                self.assert_error("stale_file", self.library.recover, self.file_id)
            for descriptor in descriptors:
                with self.assertRaises(OSError):
                    os.fstat(descriptor)
            self.assertNotIn(self.file_id, self.library._checks)
            self.assert_original_only()

    def test_worker_initial_stat_failure_closes_both_descriptors(self):
        args = self.start()
        with mock.patch(MODULE + "os.fstat", side_effect=OSError("fixture stat failure")):
            self.library._recover_worker(*args)
        self.assertEqual(self.library._checks[self.file_id]["state"], "failed")
        for descriptor in args[1:3]:
            with self.assertRaises(OSError):
                os.fstat(descriptor)
        self.assert_original_only()

    def test_publication_unlink_failure_rolls_back_copy_and_cleans_partial(self):
        real_unlink = os.unlink
        failed = False
        def unlink(name, *args, **kwargs):
            nonlocal failed
            if name.startswith(".tinypirelay-recovery-") and not failed:
                failed = True
                raise OSError("fixture unlink failure")
            return real_unlink(name, *args, **kwargs)
        with mock.patch(MODULE + "os.unlink", side_effect=unlink):
            self.assertEqual(self.run_worker()["state"], "failed")
        self.assertTrue(failed)
        self.assert_original_only()


@unittest.skipUnless(LINUX and shutil.which("ffmpeg"), "Requires Linux and FFmpeg")
class RealRecoveryTests(RecoveryCase):
    def synthesize(self, bits):
        # Two deterministic channels with nonzero low bits, so a 24-to-16-bit conversion is detectable.
        pcm = b"".join((((sample * 7919 + channel * 3571) % (1 << bits)) - (1 << (bits - 1)))
                       .to_bytes(bits // 8, "little", signed=True)
                       for sample in range(24000) for channel in range(2))
        subprocess.run([shutil.which("ffmpeg"), "-nostdin", "-hide_banner", "-v", "error",
                        "-f", f"s{bits}le", "-ar", "48000", "-ac", "2", "-i", "pipe:0",
                        "-c:a", "flac", "-y", str(self.source)], input=pcm, check=True,
                       capture_output=True, timeout=10)
        return pcm, self.source.read_bytes()

    def wait_recovery(self):
        self.assertTrue(self.library.recover(self.file_id)["accepted"])
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with self.library._lock:
                result = dict(self.library._checks[self.file_id])
            if result["state"] != "recovering":
                return result
            time.sleep(.01)
        self.fail("Synthetic recovery did not finish within 15 seconds")

    def test_truncated_tail_and_unfinalized_header_preserve_original_and_pcm(self):
        for bits in (16, 24):
            for damage in ("truncated_tail", "unfinalized_header"):
                with self.subTest(bits=bits, damage=damage):
                    pcm, healthy = self.synthesize(bits)
                    if damage == "truncated_tail":
                        damaged = healthy[:-128]
                    else:
                        header = bytearray(healthy)
                        packed = int.from_bytes(header[18:26], "big") & ~((1 << 36) - 1)
                        header[18:26] = packed.to_bytes(8, "big")
                        header[26:42] = b"\0" * 16
                        damaged = bytes(header)
                    self.add(damaged)
                    result = self.wait_recovery()
                    self.assertEqual(result["state"], "recovered", result)
                    copy = self.path / result["output_name"]
                    header = copy.read_bytes()[:42]
                    metadata = _streaminfo(header)
                    self.assertEqual((metadata["sample_rate_hz"], metadata["channels"], metadata["bits_per_sample"]),
                                     (48000, 2, bits))
                    decoded = subprocess.run([shutil.which("ffmpeg"), "-nostdin", "-hide_banner", "-v", "error",
                        "-xerror", "-err_detect", "crccheck+bitstream+buffer+explode", "-i", str(copy),
                        "-map", "0:a:0", "-c:a", f"pcm_s{bits}le", "-f", f"s{bits}le", "pipe:1"],
                        check=True, capture_output=True, timeout=10).stdout
                    self.assertTrue(decoded)
                    self.assertEqual(hashlib.md5(decoded).digest(), header[26:42])
                    self.assertEqual(decoded, pcm[:len(decoded)])
                    self.assertEqual(metadata["duration_seconds"], len(decoded) / (48000 * 2 * (bits // 8)))
                    if damage == "unfinalized_header":
                        self.assertEqual(decoded, pcm)
                    else:
                        self.assertLess(len(decoded), len(pcm))
                    self.assertEqual(self.source.read_bytes(), damaged)
                    self.assertEqual(set(self.path.iterdir()), {self.source, copy})
                    copy.unlink()

    def test_real_decoder_rejects_header_without_audio(self):
        self.add(flac_header(samples=0))
        self.assertEqual(self.wait_recovery()["state"], "failed")
        self.assert_original_only()


if __name__ == "__main__":
    unittest.main()
