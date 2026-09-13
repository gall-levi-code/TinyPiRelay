"""Build a pinned installation bundle; never upload or publish it."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import shlex
import stat
import tarfile
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from tinypirelay.installer import PAYLOAD_NAMES, read_source_version, validate_payload_tree


def build(source: Path, output: Path, base_url: str | None = None) -> dict[str, str]:
    version = read_source_version(source)
    fingerprint = validate_payload_tree(source)
    if base_url is not None:
        address = urlsplit(base_url)
        if (
            address.scheme != "https" or not address.hostname or address.username
            or address.password or address.query or address.fragment
            or any(character.isspace() or ord(character) < 32 for character in base_url)
        ):
            raise ValueError("release base URL must be HTTPS without credentials, query or fragment")
    archive_name = f"tinypirelay-{version}.tar.gz"
    output.mkdir(parents=True, exist_ok=True)
    entries: list[Path] = []
    for name in (*PAYLOAD_NAMES, "bootstrap.sh", "README.md", "docs", "tests", "spikes"):
        path = source / name
        entries.extend((path, *path.rglob("*")) if path.is_dir() else (path,))
    entries = sorted(
        (path for path in entries if "__pycache__" not in path.parts and path.suffix != ".pyc"),
        key=lambda path: path.relative_to(source).as_posix(),
    )
    if len(entries) > 10_000:
        raise ValueError("release exceeds bootstrap member limit")
    with tempfile.TemporaryDirectory(prefix=".release-", dir=output) as scratch:
        staging = Path(scratch)
        with (staging / archive_name).open("xb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as bundle:
                    total = 0
                    for path in entries:
                        mode = path.lstat().st_mode
                        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                            raise ValueError("release contains a link or special file")
                        member = tarfile.TarInfo(f"tinypirelay-{version}/{path.relative_to(source).as_posix()}")
                        member.mode = 0o755 if path.is_dir() or path.suffix == ".sh" or path.name == "tinypirelay" else 0o644
                        if path.is_dir():
                            member.type = tarfile.DIRTYPE
                            bundle.addfile(member)
                        else:
                            data = path.read_bytes()
                            total += len(data)
                            if total > 100 * 1024 * 1024:
                                raise ValueError("release exceeds bootstrap extraction limit")
                            member.size = len(data)
                            bundle.addfile(member, io.BytesIO(data))
        if validate_payload_tree(source) != fingerprint:
            raise ValueError("release source changed while packaging; retry with an idle source tree")
        archive = staging / archive_name
        if archive.stat().st_size > 50 * 1024 * 1024:
            raise ValueError("release exceeds bootstrap download limit")
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        outputs = [archive_name, archive_name + ".sha256"]
        (staging / outputs[1]).write_text(f"{digest}  {archive_name}\n", encoding="ascii", newline="\n")
        if base_url is not None:
            url = base_url.rstrip("/") + "/" + archive_name
            bootstrap = (source / "bootstrap.sh").read_text(encoding="utf-8")
            launcher = (
                "#!/bin/sh\n# Pinned release launcher. Verify this file before running with sudo.\n"
                f"set -- {shlex.quote(version)} {shlex.quote(url)} {shlex.quote(digest)} \"$@\"\n"
                + bootstrap.removeprefix("#!/bin/sh\n")
            )
            outputs.append(f"install-tinypirelay-{version}.sh")
            (staging / outputs[-1]).write_text(launcher, encoding="utf-8", newline="\n")
        # Exclusive creation leaves any previously built or user-supplied files intact.
        if any((output / name).exists() or (output / name).is_symlink() for name in outputs):
            raise FileExistsError("release output already exists; choose a new output directory")
        for name in outputs:
            with (output / name).open("xb") as target:
                target.write((staging / name).read_bytes())
    return {"version": version, "archive": str(output / archive_name), "sha256": digest}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base-url", help="optional HTTPS directory where these artifacts will be hosted")
    args = parser.parse_args()
    try:
        result = build(args.source.resolve(), args.output.resolve(), args.base_url)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"release build failed: {exc}\n")
    for name, value in result.items():
        print(f"{name}: {value}")
    print("Built locally only. No upload, signature, public release or hardware acceptance is implied.")


if __name__ == "__main__":
    main()
