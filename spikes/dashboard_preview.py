"""Loopback-only dashboard QA: real HTTP/auth, simulated media, no Pi access.

Run ``py -B spikes/dashboard_preview.py --port 18082``; the disposable login is
printed once and never saved. Configuration is read-only, media exists only in
memory, and all telemetry is synthetic. This is not hardware evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import secrets
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tinypirelay.audio_capabilities import load_audio_capabilities, validate_config_combination
from tinypirelay.compatibility import load_evidence, validate_evidence
from tinypirelay.control_protocol import ControlOperationError
from tinypirelay.media_config import validate_config
from tinypirelay.media_control import public_config
from tinypirelay.media_state import MediaState, reduce
from tinypirelay.web_security import CredentialRecord
from tinypirelay.web_service import WebApplication, create_server


NOTICE = "SIMULATED / LOCAL PREVIEW — no Pi connection, media files, or network streams."


class PreviewControl:
    """Use the real reducer; acknowledge effects without running any backend."""

    def __init__(self, *, running=True, clock=time.monotonic):
        approved = validate_evidence(load_evidence(ROOT / "evidence/stream_compatibility.json"))
        self.capabilities = load_audio_capabilities(ROOT / "evidence/audio_capabilities.json", approved)
        graph = self.capabilities.to_public_dict()
        device = next(device for device in graph["capture_devices"] if any(mode["stream_options"] for mode in device["modes"]))
        mode = next(mode for mode in device["modes"] if mode["stream_options"])
        option_ids = {option["id"] for option in mode["stream_options"]}
        representation = next((item["id"] for item in graph["stream_representations"] if item["id"] in option_ids and item["codec"] == "Opus"), mode["stream_options"][0]["id"])
        raw = json.loads((ROOT / "config/example.json").read_text(encoding="utf-8"))
        raw["capture"] = {"device_id": device["id"], "mode": {key: mode[key] for key in ("format", "rate_hz", "channels")}}
        raw["stream"].update(enabled=True, representation_id=representation, destination_host="preview.invalid", destination_port=9000, stream_id="local-preview")
        self.config = validate_config(raw, approved)
        validate_config_combination(self.config, self.capabilities)
        self.revision = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
        self.clock = clock
        self.started = clock()
        self.software_version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        self.state = MediaState()
        self.pending = []
        self.events = []
        self.lease_until = 0.0
        self.device_sample = None
        self.fragment_number = 0
        self.fragment_file = None
        self.fragment_opened_at = None
        self.fragment_elapsed = 0.0
        self.completed_recording_bytes = 0
        self.lock = threading.Lock()
        if running:
            for action in ("capture.start", "stream.start", "recording.start"):
                self._command(action)
                self._advance(float("inf"))

    def _advance(self, now):
        while self.pending and self.pending[0][0] <= now:
            _due, scope, event = self.pending.pop(0)
            generation = getattr(self.state, f"{scope}_generation")
            if event == "recording.stopped" and self.state.recording_current_file:
                self._close_fragment(self.clock())
            if event == "recording.rotation_completed":
                self._close_fragment(self.clock())
                self._open_fragment(self.clock())
            self.state = reduce(self.state, event, generation=generation).state
            if event == "stream.started":
                self.state = reduce(self.state, "connection.connected", generation=generation).state
            if event == "recording.started":
                self._open_fragment(self.clock())
        if (self.state.recording == "running" and not self.state.recording_rotation_pending
                and self.fragment_opened_at is not None
                and self.clock() - self.fragment_opened_at >= self.config.recording.rotation_seconds):
            self._command("recording.rotate")

    def _open_fragment(self, now):
        self.fragment_number += 1
        self.fragment_file = f"{self.config.recording.directory}/preview-{self.state.recording_generation:04d}-{self.fragment_number:04d}.flac"
        self.fragment_opened_at = now
        self.fragment_elapsed = 0.0
        self.state = reduce(self.state, "recording.fragment_opened", generation=self.state.recording_generation, path=self.fragment_file).state

    def _close_fragment(self, now):
        if self.fragment_opened_at is None:
            return
        self.fragment_elapsed = max(0.0, now - self.fragment_opened_at)
        self.completed_recording_bytes += 128 + int(self.fragment_elapsed * 64_000)
        self.fragment_opened_at = None
        self.state = reduce(self.state, "recording.fragment_closed", generation=self.state.recording_generation, path=self.fragment_file).state

    def _recording_telemetry(self, now):
        path = self.state.recording_current_file or self.state.recording_last_finalized_file
        elapsed = max(0.0, now - self.fragment_opened_at) if self.fragment_opened_at is not None else self.fragment_elapsed
        if not path or path != self.fragment_file:
            return {"file": None, "size_bytes": None, "elapsed_seconds": None}
        return {"file": path, "size_bytes": 128 + int(elapsed * 64_000), "elapsed_seconds": elapsed}

    def _monitoring_telemetry(self, now):
        elapsed = max(0, now - self.started)
        sequence = int(elapsed * max(5, self.config.monitoring.spectrum_updates_per_second)) + 1
        meter = spectrum = None
        if self.state.capture == "running":
            meter = {"sequence": sequence, "channels": [{"peak_dbfs": -10 + 7 * math.sin(elapsed + channel), "rms_dbfs": -24 + 6 * math.sin(elapsed + channel), "clipping": False, "no_signal": False} for channel in range(self.config.capture.mode.channels)]}
            if self.lease_until > now:
                spectrum = {"sequence": sequence, "magnitudes_db": [-95 + 30 * math.exp(-((band - 15 - 8 * math.sin(elapsed / 3)) / 8) ** 2) + 12 * math.sin(band * 0.25 + elapsed) ** 4 for band in range(self.config.monitoring.spectrum_bands)]}
        return {"meter": meter, "spectrum": spectrum}

    def _device_telemetry(self, now):
        """One deterministic synthetic sample per two seconds, shared by all clients."""
        elapsed = max(0.0, now - self.started)
        sequence = int(elapsed / 2) + 1
        if self.device_sample is not None and self.device_sample["sequence"] == sequence:
            return self.device_sample
        recording = self._recording_telemetry(now)
        active_recording = self.state.recording in {"running", "stopping"}
        recorded_bytes = self.completed_recording_bytes + (recording["size_bytes"] or 0 if self.fragment_opened_at is not None else 0)
        total = 128_000_000_000
        used = 92_200_000_000 + recorded_bytes
        available = max(0, total - used)
        memory_total = 512 * 1024 * 1024
        memory_available = int((286 + 3 * math.sin(elapsed / 15)) * 1024 * 1024)
        self.device_sample = {
            "sequence": sequence,
            "sampled_at_unix": 1_788_393_600.0 + (sequence - 1) * 2,
            "config_revision": self.revision,
            "device": {
                "hostname": "tinypirelay-preview", "model": "SIMULATED Pi Zero W",
                "os": "Raspberry Pi OS · simulated", "architecture": "armv6l · simulated",
                "software_version": self.software_version,
            },
            "system": {
                "cpu_percent": round(24 + 8 * math.sin(elapsed / 7), 1),
                "temperature_c": round(43.2 + 2 * math.sin(elapsed / 30), 1),
                "memory_total_bytes": memory_total, "memory_available_bytes": memory_available,
                "memory_used_bytes": memory_total - memory_available,
                "load_average": [round(0.48 + 0.1 * math.sin(elapsed / 20), 2), 0.61, 0.57],
                "uptime_seconds": 5 * 86400 + 14 * 3600 + elapsed,
            },
            "storage": {
                "directory": self.config.recording.directory, "mountpoint": "/",
                "filesystem": "ext4", "device": "/dev/mmcblk0p2", "total_bytes": total,
                "used_bytes": used, "available_bytes": available, "used_percent": used / total * 100,
                "recordings_bytes": 25_100_000_000 + recorded_bytes, "recordings_complete": True,
                "read_bytes_per_second": round(12_000 + 4_000 * math.sin(elapsed / 5)),
                "write_bytes_per_second": 66_048 if active_recording else 2048,
                "stop_percent": 90,
                "seconds_until_limit": max(0.0, (total * 0.9 - used) / 64_000) if active_recording else None,
                "safe": used < total * 0.9, "reason": None if used < total * 0.9 else "recording_limit_reached",
            },
            "network": {
                "interface": "wlan0", "address": "192.0.2.94", "kind": "wifi", "operstate": "up",
                "speed_mbps": None, "duplex": None,
                "rx_bytes_per_second": round(3800 + 1000 * math.sin(elapsed / 4)),
                "tx_bytes_per_second": round(16_800 + 1250 * math.sin(elapsed)) if self.state.stream == "running" else 800,
            },
            "recording": recording,
        }
        return self.device_sample

    def _command(self, action, arguments=None):
        values = {}
        if action.endswith(".start"):
            values["config_revision"] = self.revision
        if action == "stream.start":
            values.update(representation_id=self.config.stream.representation_id, opus_bitrate_bps=self.config.stream.opus_bitrate_bps)
        if action == "recording.start":
            values["storage_status"] = "safe"  # Simulated guard result; no disk access.
        if action == "stream.set_opus_bitrate":
            values["bitrate_bps"] = (arguments or {}).get("bitrate_bps")
        decision = reduce(self.state, action, **values)
        self.state = decision.state
        for effect in decision.effects:
            scope, command = effect.split(".", 1)
            if command in {"start", "stop"}:
                self.pending.append((self.clock() + 0.6, scope, f"{scope}.{'started' if command == 'start' else 'stopped'}"))
            elif effect == "recording.rotate":
                self.pending.append((self.clock() + 0.6, scope, "recording.rotation_completed"))
            elif effect == "stream.set_opus_bitrate":
                self.state = reduce(self.state, "stream.opus_bitrate_applied", generation=self.state.stream_generation, bitrate_bps=values["bitrate_bps"]).state
        self.events.append({"timestamp_unix": time.time(), "code": "preview_control", "message": f"SIMULATED {action}: {'accepted' if decision.ok else decision.error.code}"})
        self.events = self.events[-32:]
        return decision.to_dict()

    def request(self, operation, arguments=None):
        with self.lock:
            now = self.clock()
            self._advance(now)
            if operation == "capabilities":
                return self.capabilities.to_public_dict()
            if operation == "config.get":
                return {"config": public_config(self.config), "revision": self.revision, "restart_required": False}
            if operation in {"config.update", "config.validate"}:
                raise ControlOperationError("preview_read_only", "Local preview configuration is read-only; no settings were saved.", "preview")
            if operation == "monitoring.spectrum_lease":
                self.lease_until = now + 5.0
                return {"active": self.state.capture == "running", "lease_seconds": 5.0}
            if operation == "monitoring.spectrum_release":
                self.lease_until = 0.0
                return {"active": False}
            if operation == "monitoring.telemetry":
                return self._monitoring_telemetry(now)
            if operation == "service.restart_request":
                return {"accepted": False, "restart_required": False, "message": "Preview never restarts a service."}
            if operation != "status":
                return self._command(operation, arguments)
            elapsed = max(0, now - self.started)
            media = self.state.to_dict()
            recording = self._recording_telemetry(now)
            media["recording"].update(elapsed_file=recording["file"], elapsed_seconds=recording["elapsed_seconds"])
            if self.state.stream == "running":
                media["connection"]["statistics"] = {
                    "send-rate-mbps": 0.128 + 0.01 * math.sin(elapsed),
                    "bytes-sent-total": int(elapsed * 16_000),
                    "packets-sent-total": int(elapsed * 20),
                    "packets-sent-lost": 0,
                    "packets-retransmitted": 0,
                    "rtt-ms": 3 + math.sin(elapsed),
                }
            return {
                "state": media,
                "telemetry": self._monitoring_telemetry(now),
                "monitoring_updates_per_second": max(5, self.config.monitoring.spectrum_updates_per_second),
                "device_telemetry": self._device_telemetry(now),
                "runtime_metrics": {"queues": {}},
                "runtime_error": {"code": "simulated_preview", "message": NOTICE},
                "warnings": [NOTICE],
                "media_available": True,
                "active_config_revision": self.revision,
                "saved_config_revision": self.revision,
                "restart_required": False,
                "diagnostics": {"events": list(self.events)},
            }


def self_test():
    now = [0.0]
    control = PreviewControl(running=False, clock=lambda: now[0])
    assert not control.request("recording.start")["ok"]
    for scope in ("capture", "stream", "recording"):
        assert control.request(f"{scope}.start")["ok"]
        assert control.request("status")["state"][scope]["state"] == "starting"
        now[0] += 1
        assert control.request("status")["state"][scope]["state"] == "running"
    assert not control.request("capture.stop")["ok"]
    assert control.request("status")["telemetry"]["spectrum"] is None
    control.request("monitoring.spectrum_lease")
    first = control.request("status")["telemetry"]
    assert control.request("monitoring.telemetry") == first
    now[0] += 1
    assert control.request("status")["telemetry"]["meter"]["sequence"] > first["meter"]["sequence"]
    assert len(first["spectrum"]["magnitudes_db"]) == control.config.monitoring.spectrum_bands
    control.request("monitoring.spectrum_release")
    assert control.request("status")["telemetry"]["spectrum"] is None
    now[0] += 2
    snapshot = control.request("status")
    device = snapshot["device_telemetry"]
    first_file = snapshot["state"]["recording"]["current_file"]
    assert device["config_revision"] == snapshot["active_config_revision"]
    assert device["recording"]["file"] == first_file
    assert device["storage"]["total_bytes"] > 2**32
    assert device["storage"]["recordings_bytes"] > 2**32
    assert device["system"]["memory_total_bytes"] == device["system"]["memory_available_bytes"] + device["system"]["memory_used_bytes"]
    assert "SIMULATED" in device["device"]["model"]
    assert control.request("status")["device_telemetry"] == device
    now[0] += 2
    later = control.request("status")["device_telemetry"]
    assert later["sequence"] > device["sequence"]
    assert later["recording"]["size_bytes"] > device["recording"]["size_bytes"]
    assert later["recording"]["elapsed_seconds"] > device["recording"]["elapsed_seconds"]
    now[0] += control.config.recording.rotation_seconds
    assert control.request("status")["state"]["recording"]["rotation_pending"]
    now[0] += 1
    rotated = control.request("status")["state"]["recording"]
    assert rotated["current_file"] != first_file
    assert rotated["last_finalized_file"] == first_file
    assert rotated["elapsed_file"] == rotated["current_file"]
    assert rotated["elapsed_seconds"] == 0
    now[0] += 2
    assert control.request("status")["device_telemetry"]["recording"]["file"] == rotated["current_file"]
    for scope in ("stream", "recording", "capture"):
        assert control.request(f"{scope}.stop")["ok"]
        now[0] += 1
        assert control.request("status")["state"][scope]["state"] == "stopped"
    assert control.request("status")["telemetry"]["meter"] is None
    frozen = control.request("status")["state"]["recording"]
    assert frozen["last_finalized_file"] == rotated["current_file"]
    now[0] += 10
    stopped = control.request("status")
    assert stopped["state"]["recording"]["elapsed_seconds"] == frozen["elapsed_seconds"]
    assert stopped["device_telemetry"]["recording"]["elapsed_seconds"] == frozen["elapsed_seconds"]
    assert stopped["device_telemetry"]["storage"]["seconds_until_limit"] is None
    print("Preview self-test passed; no media or files created.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18082)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    password = secrets.token_urlsafe(18)
    application = WebApplication(CredentialRecord.create("preview", password), PreviewControl())
    with create_server("127.0.0.1", args.port, application) as server:
        print(f"{NOTICE}\nhttp://127.0.0.1:{args.port}/\nUsername: preview\nEphemeral password: {password}\nCtrl+C stops the preview.", flush=True)
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            server.stop_event.set()


if __name__ == "__main__":
    main()
