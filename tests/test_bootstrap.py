from __future__ import annotations

import io
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "bootstrap.sh"


@unittest.skipUnless(
    os.name == "posix" and Path("/bin/sh").is_file(),
    "bootstrap execution requires a POSIX shell",
)
class BootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        if not BOOTSTRAP.is_file():
            self.skipTest("bootstrap is unavailable")

    def _archive(self, directory: Path, *, unsafe: bool = False) -> Path:
        archive = directory / "tinypirelay.tar.gz"
        installer = b'#!/bin/sh\nprintf "%s\\n" "$*" > "$BOOTSTRAP_RESULT"\n'
        with tarfile.open(archive, "w:gz") as bundle:
            for name, payload in (
                ("tinypirelay-0.4.0-dev/VERSION", b"0.4.0-dev\n"),
                ("tinypirelay-0.4.0-dev/install.sh", installer),
            ):
                member = tarfile.TarInfo(name)
                member.size = len(payload)
                member.mode = 0o644
                bundle.addfile(member, io.BytesIO(payload))
            if unsafe:
                payload = b"escape"
                member = tarfile.TarInfo("../escape")
                member.size = len(payload)
                bundle.addfile(member, io.BytesIO(payload))
        return archive

    def _validate_archive(
        self, archive: Path, destination: Path
    ) -> subprocess.CompletedProcess[str]:
        script = BOOTSTRAP.read_text(encoding="utf-8")
        validator = script.split("<<'PY'\n", 1)[1].split("\nPY\n)", 1)[0]
        return subprocess.run(
            (
                sys.executable,
                "-I",
                "-",
                str(archive),
                str(destination),
                "0.4.0-dev",
            ),
            input=validator,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )

    def test_verified_archive_is_bounded_and_installer_arguments_are_forwarded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            archive = self._archive(directory)
            destination = directory / "extract"
            completed = self._validate_archive(archive, destination)

            self.assertEqual(0, completed.returncode, completed.stderr)
            root = destination / "tinypirelay-0.4.0-dev"
            self.assertEqual(str(root), completed.stdout.strip())
            self.assertEqual("0.4.0-dev\n", (root / "VERSION").read_text())
            script = BOOTSTRAP.read_text(encoding="utf-8")
            self.assertIn('/bin/sh "$source_root/install.sh" "$@"', script)
            self.assertIn("sha256sum -c -s -", script)

    def test_unsafe_archive_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            archive = self._archive(directory, unsafe=True)
            completed = self._validate_archive(archive, directory / "extract")
            self.assertNotEqual(0, completed.returncode)
            self.assertFalse((directory / "escape").exists())

    def test_member_limit_stops_parsing_before_the_remaining_archive(self) -> None:
        script = BOOTSTRAP.read_text(encoding="utf-8")
        validator = script.split("<<'PY'\n", 1)[1].split("\nPY\n)", 1)[0]
        member = tarfile.TarInfo("tinypirelay-0.4.0-dev/empty")

        def members():
            for _ in range(10_001):
                yield member
            raise AssertionError("archive was consumed beyond its member limit")

        bundle = mock.MagicMock()
        bundle.__enter__.return_value = bundle
        bundle.__iter__.side_effect = members
        bundle.getmembers.side_effect = lambda: list(members())
        with mock.patch.object(tarfile, "open", return_value=bundle), mock.patch.object(
            sys, "argv", ["bootstrap", "archive", "destination", "0.4.0-dev"]
        ):
            with self.assertRaisesRegex(SystemExit, "invalid member count"):
                exec(compile(validator, "bootstrap archive inspector", "exec"), {})
        bundle.extractall.assert_not_called()

    def test_non_https_url_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            completed = subprocess.run(
                (
                    "/bin/sh",
                    str(BOOTSTRAP),
                    "0.4.0-dev",
                    "http://fixture.invalid/release",
                    "0" * 64,
                    "plan",
                ),
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )

            self.assertEqual(2, completed.returncode)
            self.assertIn("non-HTTPS", completed.stderr)

    def test_privileged_bootstrap_ignores_caller_tool_and_python_paths(self) -> None:
        script = BOOTSTRAP.read_text(encoding="utf-8")
        self.assertIn("PATH=/usr/sbin:/usr/bin:/sbin:/bin", script)
        self.assertIn("/usr/bin/python3 -I -", script)
        self.assertIn('mktemp -d "/tmp/tinypirelay-bootstrap.XXXXXX"', script)
        self.assertIn("curl --disable", script)
        self.assertNotIn("${TMPDIR", script)

    def test_missing_bootstrap_tools_only_install_on_supported_root_apply(self) -> None:
        script = BOOTSTRAP.read_text(encoding="utf-8")
        preparation = script.split("missing_packages=\n", 1)[1].split("\numask 077", 1)[0]
        for arguments, uid, supported, installs in (
            ("install --apply", "0", True, True),
            ("upgrade --apply", "0", True, True),
            ("plan", "0", True, False),
            ("uninstall --apply", "0", True, False),
            ("install --apply", "1000", True, False),
            ("install --apply", "0", False, False),
        ):
            with self.subTest(arguments=arguments, uid=uid, supported=supported):
                # Run the real prerequisite logic with isolated shell functions;
                # no command can reach host APT or elevate privileges.
                wrapper = (
                    f"set -eu\nset -- {arguments}\nmissing_packages=\n"
                    "command() { return 1; }\n"
                    f"id() {{ printf '%s' '{uid}'; }}\n"
                    f"grep() {{ return {0 if supported else 1}; }}\n"
                    "apt-get() { printf 'APT %s\\n' \"$*\"; }\n"
                    + preparation
                )
                completed = subprocess.run(("/bin/sh", "-c", wrapper), text=True,
                                           capture_output=True, timeout=10, check=False)
                self.assertEqual(installs, "APT " in completed.stdout)
                if installs:
                    self.assertEqual(0, completed.returncode, completed.stderr)
                    self.assertIn("--no-remove --no-upgrade --no-install-recommends", completed.stdout)
                else:
                    self.assertEqual(2, completed.returncode)

    def test_default_bootstrap_forwards_install_apply_without_local_paths(self) -> None:
        script = BOOTSTRAP.read_text(encoding="utf-8").split('case "$artifact_url"', 1)[0]
        completed = subprocess.run(("/bin/sh", "-c", script + '\nprintf "%s\\n" "$@"',
                                    "bootstrap", "0.5.17-dev", "https://example.invalid/release", "0" * 64),
                                   capture_output=True, text=True, check=False, timeout=10)
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(["install", "--apply"], completed.stdout.splitlines())

    def test_installer_metadata_refresh_is_apply_only_and_not_uninstall(self) -> None:
        script = (ROOT / "install.sh").read_text(encoding="utf-8")
        preparation = "applying=no\n" + script.split("applying=no\n", 1)[1].split("\nPYTHONPATH=", 1)[0]
        for arguments, uid, supported, refreshes in (
            ("install --apply", "0", True, True), ("upgrade --apply", "0", True, True),
            ("plan", "0", True, False), ("install", "0", True, False),
            ("uninstall --apply", "0", True, False), ("install --apply", "1000", True, False),
            ("install --apply", "0", False, False),
        ):
            with self.subTest(arguments=arguments, uid=uid, supported=supported):
                wrapper = (f"set -eu\nset -- {arguments}\n"
                           f"id() {{ printf '%s' '{uid}'; }}\n"
                           f"grep() {{ return {0 if supported else 1}; }}\n"
                           "apt-get() { printf 'APT %s\\n' \"$*\"; }\n" + preparation)
                completed = subprocess.run(("/bin/sh", "-c", wrapper), capture_output=True,
                                           text=True, check=False, timeout=10)
                self.assertEqual(refreshes, "APT " in completed.stdout)
                if refreshes:
                    self.assertIn("APT::Update::Error-Mode=any", completed.stdout)


if __name__ == "__main__":
    unittest.main()
