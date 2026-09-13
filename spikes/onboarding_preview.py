"""Disposable loopback first-run QA. Real web auth; synthetic, unconfigured media."""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from dashboard_preview import PreviewControl, ROOT
from tinypirelay.compatibility import load_evidence, validate_evidence
from tinypirelay.control_protocol import ControlOperationError
from tinypirelay.media_config import validate_config
from tinypirelay.media_control import internal_config_value, public_config
from tinypirelay.web_service import WebApplication, create_server


class OnboardingControl(PreviewControl):
    def __init__(self):
        super().__init__(running=False)
        self.config = validate_config(
            json.loads((ROOT / "config/example.json").read_text(encoding="utf-8")),
            validate_evidence(load_evidence(ROOT / "evidence/stream_compatibility.json")),
        )

    def request(self, operation, arguments=None):
        if operation == "config.update":
            proposed = validate_config(
                internal_config_value(arguments["config"], self.config.stream.passphrase),
                validate_evidence(load_evidence(ROOT / "evidence/stream_compatibility.json")),
            )
            if proposed != self.config or arguments["expected_revision"] != self.revision:
                raise ControlOperationError("preview_read_only", "Preview accepts only an unchanged configuration.", "preview")
            return {"config": public_config(self.config), "revision": self.revision, "restart_required": False}
        result = super().request(operation, arguments)
        if operation == "capabilities":
            result["discovery"] = {"status": "available", "devices": []}
            for device in result["capture_devices"]:
                device["present"] = False
        if operation == "status":
            result["audio_setup_required"] = True
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18087)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="tinypirelay-onboarding-qa-") as directory:
        credentials = Path(directory) / "credential.json"
        marker = Path(directory) / "setup-pending"
        marker.write_bytes(b"1\n")
        marker.chmod(0o600)
        app = WebApplication(None, OnboardingControl(), credential_path=credentials, setup_pending_path=marker)
        with create_server("127.0.0.1", args.port, app) as server:
            print(f"Disposable onboarding preview ready: http://127.0.0.1:{server.server_port}/", flush=True)
            try:
                server.serve_forever(poll_interval=0.2)
            except KeyboardInterrupt:
                server.stop_event.set()


if __name__ == "__main__":
    main()
