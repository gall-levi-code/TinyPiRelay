from __future__ import annotations

import importlib.util
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_builder", ROOT / "packaging/build_release.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class ReleaseBuilderTests(unittest.TestCase):
    def test_reproducible_pinned_bundle_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "first"
            second = Path(temporary) / "second"
            result = builder.build(ROOT, first, "https://releases.example.invalid/tinypirelay")
            repeated = builder.build(ROOT, second)
            self.assertEqual(result["sha256"], repeated["sha256"])
            archive = Path(result["archive"])
            with tarfile.open(archive) as bundle:
                names = bundle.getnames()
                prefix = f"tinypirelay-{result['version']}/"
                self.assertTrue(all(name.startswith(prefix) for name in names))
                self.assertEqual((ROOT / "VERSION").read_bytes(), bundle.extractfile(prefix + "VERSION").read())
                self.assertIn(prefix + "spikes/step4_install_integration.py", names)
                self.assertTrue(all(not item.issym() and not item.islnk() for item in bundle.getmembers()))
            launcher = (first / f"install-tinypirelay-{result['version']}.sh").read_text()
            self.assertIn(result["sha256"], launcher)
            self.assertIn("https://releases.example.invalid/tinypirelay/", launcher)
            self.assertIn('"$@"', launcher)
            with self.assertRaises(FileExistsError):
                builder.build(ROOT, first)
            self.assertEqual(archive.read_bytes(), Path(repeated["archive"]).read_bytes())

    def test_untrusted_launcher_url_forms_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            for url in ("http://example.invalid", "https://user:secret@example.invalid", "https://example.invalid/?token=x", "https://example.invalid/\ncommand"):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    builder.build(ROOT, Path(temporary), url)


if __name__ == "__main__":
    unittest.main()
