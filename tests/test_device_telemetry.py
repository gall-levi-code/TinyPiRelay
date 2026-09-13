from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import tinypirelay.device_telemetry as telemetry
from tinypirelay.media_config import DestinationStatus, validate_config


class DeviceTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        # Windows cannot create the colon-named sysfs device paths. Keep only
        # these pseudo-file contents in memory; all other fixtures use files.
        self.virtual = {}
        read_text = telemetry._text
        patcher = mock.patch.object(telemetry, "_text", side_effect=lambda path, limit=65536: self.virtual[str(path)] if str(path) in self.virtual else read_text(path, limit))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.recordings = self.root / "recordings"
        self.recordings.mkdir()
        self.now = [10.0]
        self.destination = DestinationStatus(
            safe=True, reason="ok", directory=str(self.recordings),
            resolved_directory=str(self.recordings.resolve()), required_mountpoint=None,
            resolved_mountpoint=None, st_dev=self.recordings.stat().st_dev,
            mount_st_dev=None, total_bytes=10000, used_bytes=4000,
            available_bytes=6000, used_percent=40.0, at_or_above_threshold=False,
        )
        self.check = mock.Mock(side_effect=lambda _config: self.destination)
        raw = json.loads((Path(__file__).resolve().parents[1] / "config/example.json").read_text())
        raw["recording"].update(directory=str(self.recordings), required_mountpoint=None)
        self.config = validate_config(raw, ())
        self.collector = telemetry.DeviceTelemetryCollector(
            proc_root=self.root / "proc", sys_root=self.root / "sys", etc_root=self.root / "etc",
            clock=lambda: self.now[0], destination_check=self.check,
        )
        patcher = mock.patch.object(telemetry, "_major_minor", return_value="179:2")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ipv4_reader = telemetry._ipv4_address
        patcher = mock.patch.object(telemetry, "_ipv4_address", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.put("proc/stat", "cpu 100 10 20 1000 30 4 6 0 50 8\n")
        self.put("proc/meminfo", "MemTotal: 512000 kB\nMemAvailable: 300000 kB\n")
        self.put("proc/loadavg", "0.11 0.22 0.33 1/10 99\n")
        self.put("proc/uptime", "123.5 987.1\n")
        self.put("proc/sys/kernel/hostname", "fixture-pi\n")
        self.put("sys/firmware/devicetree/base/model", "Raspberry Pi Zero W fixture\0")
        self.put("sys/class/thermal/thermal_zone0/temp", "43200\n")
        self.put("etc/os-release", 'PRETTY_NAME="Fixture Linux"\n')
        mount = str(self.recordings.resolve()).replace(" ", r"\040")
        self.put("proc/self/mountinfo", f"10 1 179:2 / {mount} rw - ext4 /dev/mmcblk0p2 rw\n")
        self.put("sys/dev/block/179:2/stat", "1 0 100 0 2 0 200 0 0 0 0\n")
        self.put("proc/net/route", "Iface Destination Gateway Flags RefCnt Use Metric Mask\nwlan0 00000000 0101A8C0 0003 0 0 100 00000000\n")
        self.interface("wlan0", 1, 1000, 2000, wifi=True)

    def put(self, relative, value):
        path = self.root / relative
        if relative.startswith("sys/dev/block/"):
            self.virtual[str(path)] = value.strip()
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")

    def interface(self, name, index, rx, tx, *, wifi=False):
        for key, value in {"ifindex": index, "operstate": "up", "type": 1, "speed": -1 if wifi else 100, "duplex": "unknown" if wifi else "full", "statistics/rx_bytes": rx, "statistics/tx_bytes": tx}.items():
            self.put(f"sys/class/net/{name}/{key}", f"{value}\n")
        if wifi:
            (self.root / "sys/class/net" / name / "wireless").mkdir()

    def sample(self, recording=None):
        return self.collector.sample(self.config, recording or {"state": "stopped"})

    def test_measured_deltas_and_cached_identity(self):
        first = self.sample()
        self.assertIsNone(first["system"]["cpu_percent"])
        self.assertIsNone(first["storage"]["read_bytes_per_second"])
        self.assertIsNone(first["network"]["rx_bytes_per_second"])
        self.assertEqual("Fixture Linux", first["device"]["os"])
        self.assertEqual(43200 / 1000, first["system"]["temperature_c"])
        self.assertEqual(212000 * 1024, first["system"]["memory_used_bytes"])
        self.assertEqual([0.11, 0.22, 0.33], first["system"]["load_average"])
        self.assertEqual("wifi", first["network"]["kind"])
        self.assertIsNone(first["network"]["speed_mbps"])
        self.put("proc/stat", "cpu 120 10 30 1060 40 4 6 0 80 10\n")
        self.put("proc/sys/kernel/hostname", "must-stay-cached\n")
        self.put("sys/dev/block/179:2/stat", "1 0 140 0 2 0 260 0 0 0 0\n")
        self.put("sys/dev/block/179:0/stat", "1 0 999999 0 2 0 999999 0 0 0 0\n")
        self.put("sys/class/net/wlan0/statistics/rx_bytes", "1400")
        self.put("sys/class/net/wlan0/statistics/tx_bytes", "2600")
        self.now[0] += 2
        second = self.sample()
        self.assertEqual(30.0, second["system"]["cpu_percent"])
        self.assertEqual("fixture-pi", second["device"]["hostname"])
        self.assertEqual(20 * 512, second["storage"]["read_bytes_per_second"])
        self.assertEqual(30 * 512, second["storage"]["write_bytes_per_second"])
        self.assertEqual(200, second["network"]["rx_bytes_per_second"])
        self.assertEqual(300, second["network"]["tx_bytes_per_second"])
        self.assertEqual("/dev/mmcblk0p2", second["storage"]["device"])
        json.dumps(second, allow_nan=False)

    def test_counter_reset_missing_readings_and_nonpositive_interval_are_unknown(self):
        self.sample()
        self.put("proc/stat", "cpu 1 0 0 1 0 0 0 0 0 0")
        self.put("sys/dev/block/179:2/stat", "1 0 1 0 2 0 2 0 0 0 0")
        self.put("sys/class/net/wlan0/statistics/rx_bytes", "1")
        self.now[0] += 2
        reset = self.sample()
        for section, field in (("system", "cpu_percent"), ("storage", "read_bytes_per_second"), ("network", "rx_bytes_per_second")):
            self.assertIsNone(reset[section][field])
        self.assertIsNone(self.sample()["storage"]["write_bytes_per_second"])
        self.put("sys/dev/block/179:2/stat", "invalid")
        self.sample()
        self.put("sys/dev/block/179:2/stat", "1 0 3 0 2 0 4 0 0 0 0")
        self.now[0] += 2
        self.assertIsNone(self.sample()["storage"]["write_bytes_per_second"])

    def test_missing_required_mount_never_falls_back_to_root_capacity(self):
        self.sample()
        self.destination = replace(self.destination, safe=False, reason="required_mountpoint_unavailable", total_bytes=None, used_bytes=None, available_bytes=None, used_percent=None)
        self.now[0] += 2
        result = self.sample()
        self.assertFalse(result["storage"]["safe"])
        self.assertEqual("required_mountpoint_unavailable", result["storage"]["reason"])
        for key in ("total_bytes", "used_bytes", "read_bytes_per_second", "recordings_bytes", "seconds_until_limit", "mountpoint"):
            self.assertIsNone(result["storage"][key])
        self.assertFalse(result["storage"]["recordings_complete"])
        self.check.side_effect = OSError("unavailable")
        self.assertIsNone(self.sample()["storage"]["safe"])

    def test_recording_total_is_flat_bounded_and_cached(self):
        for index in range(3):
            (self.recordings / f"{index}.flac").write_bytes(b"x" * 10)
        nested = self.recordings / "archive"
        nested.mkdir()
        (nested / "ignored.flac").write_bytes(b"x" * 100)
        (self.recordings / "ignored.txt").write_bytes(b"x" * 100)
        with mock.patch.object(telemetry, "MAX_SCAN_ENTRIES", 2):
            result = self.sample()["storage"]
        self.assertIsNone(result["recordings_bytes"])
        self.assertFalse(result["recordings_complete"])
        self.now[0] += 2
        self.assertIsNone(self.sample()["storage"]["recordings_bytes"])
        self.now[0] += 30
        result = self.sample()["storage"]
        self.assertEqual(30, result["recordings_bytes"])
        self.assertTrue(result["recordings_complete"])

    def test_recording_scan_does_not_follow_file_links_or_return_a_partial_total(self):
        path = self.recordings / "link.flac"
        path.write_bytes(b"not followed")
        (self.recordings / "safe.flac").write_bytes(b"123")
        real_stat = telemetry.os.stat

        def stat_no_follow(target, **kwargs):
            if Path(target) == path:
                self.assertFalse(kwargs["follow_symlinks"])
                return SimpleNamespace(st_mode=stat.S_IFLNK, st_dev=self.destination.st_dev, st_size=100000)
            return real_stat(target, **kwargs)

        with mock.patch.object(telemetry.os, "stat", side_effect=stat_no_follow):
            self.assertEqual(3, self.sample()["storage"]["recordings_bytes"])
        self.now[0] += 30
        with mock.patch.object(telemetry.os, "scandir", side_effect=PermissionError("denied")):
            result = self.sample()["storage"]
            self.assertIsNone(result["recordings_bytes"])
            self.assertFalse(result["recordings_complete"])

    def test_mountinfo_selects_longest_matching_device_and_unescapes_spaces(self):
        directory = str(self.root / "space dir" / "recordings")
        mount = str(self.root / "space dir")
        escaped = mount.replace(" ", r"\040")
        escaped_directory = directory.replace(" ", r"\040")
        self.put("proc/self/mountinfo", f"1 0 179:2 / {self.root} rw - ext4 /dev/base rw\n2 1 179:2 / {escaped} rw - ext4 /dev/matched rw\n3 2 179:0 / {escaped_directory} rw - ext4 /dev/wrong rw\n")
        selected = self.collector._mount(directory, self.destination.st_dev)
        self.assertEqual((mount, "ext4", "/dev/matched", "179:2"), selected)

    def test_eta_requires_matching_current_fragment_and_is_space_capped(self):
        path = self.recordings / "active.flac"
        path.write_bytes(b"x" * 200)
        record = {"state": "running", "current_file": str(path), "elapsed_file": str(path), "elapsed_seconds": 10}
        result = self.sample(record)
        self.assertEqual(200, result["recording"]["size_bytes"])
        self.assertEqual(250, result["storage"]["seconds_until_limit"])
        for change in ({"elapsed_seconds": 4}, {"elapsed_file": "different.flac"}, {"state": "stopped"}, {"elapsed_seconds": float("nan")}):
            self.assertIsNone(self.sample({**record, **change})["storage"]["seconds_until_limit"])
        self.destination = replace(self.destination, available_bytes=1000)
        self.assertEqual(50, self.sample(record)["storage"]["seconds_until_limit"])
        self.destination = replace(self.destination, safe=False, reason="storage_threshold", used_bytes=9000)
        self.assertEqual(0, self.sample(record)["storage"]["seconds_until_limit"])

    def test_file_metadata_rejects_outside_symlink_and_wrong_device_and_preserves_large_int(self):
        outside = self.root / "outside.flac"
        outside.write_bytes(b"x")
        self.assertIsNone(self.sample({"current_file": str(outside)})["recording"]["file"])
        path = self.recordings / "active.flac"
        path.write_bytes(b"x")
        original = Path.lstat
        for mode, device, expected in ((stat.S_IFLNK, self.destination.st_dev, None), (stat.S_IFREG, self.destination.st_dev + 1, None), (stat.S_IFREG, self.destination.st_dev, 5 * 1024**3)):
            fake = SimpleNamespace(st_mode=mode, st_dev=device, st_size=5 * 1024**3)
            with mock.patch.object(Path, "lstat", autospec=True, side_effect=lambda item: fake if item == path else original(item)):
                value = self.collector._recording_file(str(self.recordings.resolve()), self.destination.st_dev, {"current_file": str(path)})
                self.assertEqual(expected, value["size_bytes"])

    def test_relative_recording_paths_use_service_cwd_and_preserve_identity(self):
        (self.recordings / "active.flac").write_bytes(b"x" * 200)
        (self.root / "outside.flac").write_bytes(b"x")
        candidate = "recordings/active.flac"
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            result = self.sample({"state": "running", "current_file": candidate, "elapsed_file": candidate, "elapsed_seconds": 10})
            self.assertEqual(candidate, result["recording"]["file"])
            self.assertEqual(200, result["recording"]["size_bytes"])
            self.assertEqual(250, result["storage"]["seconds_until_limit"])
            self.assertIsNone(self.sample({"current_file": "recordings/../outside.flac"})["recording"]["file"])
        finally:
            os.chdir(previous)

    def test_default_route_then_deterministic_up_fallback_and_ipv6(self):
        self.interface("eth0", 2, 9000, 10000)
        self.put("proc/net/route", "Iface Destination Gateway Flags RefCnt Use Metric Mask\nwlan0 00000000 0 0003 0 0 100 00000000\neth0 00000000 0 0003 0 0 10 00000000\n")
        self.put("proc/net/if_inet6", "fe800000000000000000000000000001 02 40 20 80 eth0\n20010db8000000000000000000000001 02 40 00 80 eth0\n")
        result = self.sample()["network"]
        self.assertEqual("eth0", result["interface"])
        self.assertEqual("ethernet", result["kind"])
        self.assertEqual("2001:db8::1", result["address"])
        self.assertEqual(100, result["speed_mbps"])
        self.put("proc/net/route", "Iface Destination Gateway Flags RefCnt Use Metric Mask\n")
        self.assertEqual("eth0", self.sample()["network"]["interface"])
        self.put("proc/net/ipv6_route", f"{'0' * 32} 00 {'0' * 32} 00 {'0' * 32} 00000064 00000000 00000000 00000003 wlan0\n")
        self.assertEqual("wlan0", self.sample()["network"]["interface"])
        self.put("proc/net/ipv6_route", "")
        self.put("sys/class/net/eth0/operstate", "down")
        self.assertEqual("wlan0", self.sample()["network"]["interface"])
        self.put("sys/class/net/wlan0/operstate", "down")
        self.assertIsNone(self.sample()["network"]["interface"])

    def test_ipv4_address_uses_only_local_ioctl(self):
        ioctl = mock.Mock(return_value=b"\0" * 20 + bytes((192, 0, 2, 4)) + b"\0" * 232)
        handle = mock.MagicMock()
        handle.__enter__.return_value = handle
        handle.fileno.return_value = 7
        with mock.patch.dict("sys.modules", {"fcntl": SimpleNamespace(ioctl=ioctl)}), mock.patch.object(telemetry.socket, "socket", return_value=handle):
            self.assertEqual("192.0.2.4", self.ipv4_reader("wlan0"))
        self.assertEqual(0x8915, ioctl.call_args.args[1])
        handle.connect.assert_not_called()
        handle.send.assert_not_called()
        handle.sendto.assert_not_called()

    def test_malformed_or_absent_optional_metrics_never_become_healthy_zero(self):
        self.put("proc/stat", "cpu broken")
        self.put("proc/uptime", "nan")
        self.put("proc/loadavg", "inf 0 0")
        self.put("proc/meminfo", "MemTotal: 100 kB\nMemAvailable: 200 kB\n")
        self.put("sys/class/thermal/thermal_zone0/temp", "200000")
        system = self.sample()["system"]
        for field in ("cpu_percent", "uptime_seconds", "load_average", "memory_available_bytes", "memory_used_bytes", "temperature_c"):
            self.assertIsNone(system[field])
        self.assertEqual(100 * 1024, system["memory_total_bytes"])
        self.put("sys/class/thermal/thermal_zone0/temp", "-12500")
        self.assertEqual(-12.5, self.sample()["system"]["temperature_c"])


if __name__ == "__main__":
    unittest.main()
