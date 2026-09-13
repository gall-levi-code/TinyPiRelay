"""Root-only before/after deployment check; outputs hashes, never credential contents."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from tinypirelay.control_protocol import ControlClient


def inventory():
    client = ControlClient(Path("/run/tinypirelay/control.sock"))
    status = client.request("status")
    assert all(status["state"][name]["state"] == "stopped" for name in ("stream", "recording")), "Active media output; review before deployment"
    config = client.request("config.get")
    paths = [Path("/etc/tinypirelay/media/config.json"), Path("/etc/tinypirelay/web/credential.json")]
    paths += sorted(Path(config["config"]["recording"]["directory"]).glob("*.flac"))
    result = {}
    for path in paths:
        assert not path.is_symlink() and path.is_file()
        with path.open("rb") as source:
            result[str(path)] = {"size": path.stat().st_size, "sha256": hashlib.file_digest(source, "sha256").hexdigest()}
    return {"files": result, "capture_mode": config["config"]["capture"]["mode"], "revision": config["revision"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("before", "after"))
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    assert os.geteuid() == 0, "Run as root to check private files"
    observed = inventory()
    if args.mode == "before":
        descriptor = os.open(args.manifest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            json.dump(observed, target, indent=2)
    else:
        expected = json.loads(args.manifest.read_text(encoding="utf-8"))
        assert observed == expected, "Configuration, credential, or recordings changed; inspect preservation evidence"
    print(json.dumps({"check": args.mode, "preserved_file_count": len(observed["files"]), "capture_mode": observed["capture_mode"], "passed": True}))


if __name__ == "__main__":
    main()
