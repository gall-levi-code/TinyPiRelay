"""Finite media-only restart check. Never reboots, shuts down, or starts outputs."""
import json
from pathlib import Path
import time

from tinypirelay.control_protocol import ControlClient, ControlUnavailable
from tinypirelay.maintenance import MaintenanceClient

control = ControlClient(Path("/run/tinypirelay/control.sock"), timeout=2)
helper = MaintenanceClient()
before = control.request("status")
assert all(before["state"][name]["state"] == "stopped" for name in ("stream", "recording")), "Outputs are active; refusing maintenance test"
assert not before["restart_required"], "Unapplied settings require operator review"
accepted = helper.request("media.restart")
assert accepted["accepted"] is True
deadline = time.monotonic() + 60
while time.monotonic() < deadline:
    result = helper.request("status")
    if not result["busy"]:
        assert result["phase"] == "complete", result.get("error")
        break
    time.sleep(0.5)
else:
    raise RuntimeError("Maintenance did not complete within the finite check")
deadline = time.monotonic() + 15
after = None
while time.monotonic() < deadline:
    try:
        after = control.request("status")
    except ControlUnavailable:
        time.sleep(0.5)
        continue
    if after["state"]["capture"]["state"] == "running":
        break
    time.sleep(0.5)
assert after is not None and after["state"]["capture"]["state"] == "running"
assert after["active_config_revision"] == before["active_config_revision"]
assert all(after["state"][name]["state"] == "stopped" for name in ("stream", "recording"))
print(json.dumps({"passed": True, "action": "media.restart", "phase": result["phase"],
                  "capture": "running", "recording": "stopped", "stream": "stopped", "config_preserved": True}))
