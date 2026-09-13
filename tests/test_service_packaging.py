from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging"


def _unit(name: str) -> str:
    return (PACKAGING / "systemd" / name).read_text(encoding="utf-8")


class ServicePackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.media = _unit("tinypirelay-media.service")
        cls.web = _unit("tinypirelay-web.service")

    def test_services_are_separate_non_root_identities(self) -> None:
        self.assertIn("User=tinypirelay-media", self.media)
        self.assertIn("User=tinypirelay-web", self.web)
        self.assertNotRegex(self.media + self.web, r"(?m)^User=root$")
        self.assertIn("Group=tinypirelay-control", self.media)
        self.assertIn("Group=tinypirelay-control", self.web)

    def test_only_media_gets_alsa_and_external_recording_mounts_remain_possible(self) -> None:
        self.assertIn("SupplementaryGroups=audio", self.media)
        self.assertIn("DevicePolicy=closed", self.media)
        self.assertIn("DeviceAllow=char-alsa rw", self.media)
        self.assertNotIn("PrivateDevices=true", self.media)
        self.assertNotIn("audio", self.web.casefold())
        self.assertIn("PrivateDevices=true", self.web)
        self.assertIn("ProtectSystem=full", self.media)
        self.assertNotIn(
            "ReadWritePaths=/var/lib/tinypirelay/recordings", self.media
        )

    def test_web_is_loopback_only_and_independent_of_media_readiness(self) -> None:
        self.assertIn("--bind 127.0.0.1 --port 8080", self.web)
        self.assertIn("IPAddressDeny=any", self.web)
        self.assertIn("IPAddressAllow=localhost", self.web)
        self.assertNotIn("tinypirelay-media.service", self.web)
        self.assertNotIn("network-online.target", self.media + self.web)
        self.assertIn("After=network.target sound.target", self.media)

    def test_restart_policy_distinguishes_transient_and_permanent_startup(self) -> None:
        for unit in (self.media, self.web):
            self.assertIn("Restart=on-failure", unit)
            self.assertIn("RestartSec=5s", unit)
            self.assertIn("RestartPreventExitStatus=2", unit)
            self.assertIn("StartLimitIntervalSec=0", unit)

    def test_units_have_bounded_logs_and_no_secret_arguments(self) -> None:
        for unit in (self.media, self.web):
            self.assertIn("LogRateLimitIntervalSec=30s", unit)
            self.assertIn("LogRateLimitBurst=200", unit)
            exec_start = next(
                line for line in unit.splitlines() if line.startswith("ExecStart=")
            )
            self.assertNotRegex(exec_start, r"(?i)(password|passphrase|token)=")
            self.assertIn("NoNewPrivileges=true", unit)
            self.assertRegex(unit, r"(?m)^CapabilityBoundingSet=$")

    def test_socket_and_private_state_permissions_are_explicit(self) -> None:
        tmpfiles = (PACKAGING / "tmpfiles.d/tinypirelay.conf").read_text(
            encoding="utf-8"
        )
        self.assertRegex(
            tmpfiles,
            r"/etc/tinypirelay/media\s+0700\s+tinypirelay-media\s+tinypirelay-media",
        )
        self.assertRegex(
            tmpfiles,
            r"/etc/tinypirelay/web\s+0700\s+tinypirelay-web\s+tinypirelay-web",
        )
        self.assertRegex(
            tmpfiles,
            r"/run/tinypirelay\s+0750\s+tinypirelay-media\s+tinypirelay-control",
        )
        self.assertRegex(
            tmpfiles,
            r"/var/lib/tinypirelay\s+0710\s+root\s+tinypirelay-control",
        )
        self.assertRegex(
            tmpfiles,
            r"/var/cache/tinypirelay-media\s+0750\s+tinypirelay-media",
        )
        self.assertIn("RuntimeDirectoryMode=0750", self.media)
        self.assertIn("--control-socket /run/tinypirelay/control.sock", self.media)

    def test_gstreamer_cache_is_writable_without_a_login_home(self) -> None:
        self.assertIn("XDG_CACHE_HOME=/var/cache/tinypirelay-media", self.media)
        self.assertIn(
            "GST_REGISTRY=/var/cache/tinypirelay-media/registry.bin", self.media
        )
        sysusers = (PACKAGING / "sysusers.d/tinypirelay.conf").read_text(
            encoding="utf-8"
        )
        self.assertIn("tinypirelay-media", sysusers)
        self.assertIn("tinypirelay-web", sysusers)
        self.assertIn("tinypirelay-control", sysusers)
        self.assertIn("audio", sysusers)

    def test_wrapper_forwards_arguments_without_reparsing(self) -> None:
        wrapper = (PACKAGING / "bin/tinypirelay").read_text(encoding="utf-8")
        self.assertIn("exec /usr/bin/python3 -P -B -m tinypirelay.cli", wrapper)
        self.assertIn('"$@"', wrapper)
        self.assertNotIn("eval", wrapper)

    def test_privileged_python_entry_points_ignore_the_callers_working_directory(self) -> None:
        wrapper = (PACKAGING / "bin/tinypirelay").read_text(encoding="utf-8")
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        for content in (self.media, self.web, wrapper, installer):
            self.assertIn("python3 -P -B -m tinypirelay", content)
        for content in (wrapper, installer):
            self.assertIn("PATH=/usr/sbin:/usr/bin:/sbin:/bin", content)


if __name__ == "__main__":
    unittest.main()
