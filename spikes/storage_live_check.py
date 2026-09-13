"""Finite decoder check of the smallest closed recording; never edits a file."""
import json
from pathlib import Path
import time

from tinypirelay.control_protocol import ControlClient

client = ControlClient(Path("/run/tinypirelay/control.sock"), timeout=2)
assert client.request("status")["state"]["recording"]["state"] == "stopped"
files = client.request("storage.list", {"page": 1})["items"]
sample = min((file for file in files if file["can_check"]), key=lambda file: file["size_bytes"])
arguments = {"file_id": sample["file_id"]}
assert client.request("storage.check", arguments)["accepted"] is True
deadline = time.monotonic() + 60
while time.monotonic() < deadline:
    result = client.request("storage.info", arguments)["file"]["check"]
    if result["state"] != "checking":
        break
    time.sleep(0.5)
assert result["state"] == "passed", result
assert client.request("status")["state"]["capture"]["state"] == "running"
print(json.dumps({"passed": True, "sample": sample["name"], "size_bytes": sample["size_bytes"],
                  "check": result, "capture": "running"}))
