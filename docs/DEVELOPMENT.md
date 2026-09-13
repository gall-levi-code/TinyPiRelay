# Developing TinyPiRelay

For appliance setup, use the [quick install](../README.md#quick-install) and
[first-use guide](GETTING_STARTED.md). These commands are for a source checkout,
not additional steps required after installation.

## Local checks

From the repository root on Raspberry Pi OS or another Unix-like host:

```sh
PYTHONPATH=src python3 -B -m tinypirelay.probe --format human
PYTHONPATH=src python3 -B -m tinypirelay.compatibility validate
PYTHONPATH=src python3 -B -m unittest discover -s tests -p 'test_*.py' -v
node tests/web_dashboard_checks.js
```

On Windows:

```powershell
$env:PYTHONPATH = 'src'
py -3 -B -m tinypirelay.probe --format human
py -3 -B -m tinypirelay.compatibility validate
py -3 -B -m unittest discover -s tests -p 'test_*.py' -v
node tests/web_dashboard_checks.js
```

The probe is read-only. Missing tools or an unverified platform produce exit
code `1`; that is expected on the Windows development host. Most Python checks
use the standard library. Native integration checks need their documented
GStreamer/FFmpeg tools and skip when unavailable; a passing skipped suite is
not hardware acceptance. Node is for developer checks, not a Pi install requirement.

## Preview the real web UI without a Pi

```sh
python3 -B spikes/dashboard_preview.py --port 18082
```

On Windows, use `py -3 -B` instead of `python3 -B`. Open
`http://127.0.0.1:18082/` and use the disposable credentials printed by the
preview. Stop it with Ctrl+C. Choose another port if it is already occupied.

This serves the actual application with simulated media and telemetry. It
does not contact a Pi, capture audio, create recordings or send a stream.
Configuration is read-only. File-library screenshots use additional sample
API responses; the plain preview is not a fully functioning demo appliance.
See [screenshot reproduction](screenshots/README.md) for the bounded capture tool.
Keep development previews on loopback; do not expose them as a public demo service.

## Architecture and evidence

- [Media service](MEDIA_SERVICE.md): the GStreamer capture/recording/streaming owner.
- [Web and control plane](WEB_CONTROL.md): authentication, API and control contracts.
- [Installation internals](INSTALLATION.md#installed-components): services and paths.
- [Release packaging](INSTALLATION.md#release-packaging-and-pinned-download): maintainer tooling.
- [Architecture decisions](adr/0001-minimal-architecture.md) and subsequent ADRs.
- [Project state](PROJECT_STATE.md), [hardware evidence](HARDWARE_MATRIX.md), and
  [release gates](RELEASE_READINESS.md): dated results and explicit nonclaims.

Developer media runs and integration harnesses can create files and send traffic.
Read their instructions and use disposable destinations. Do not point fixture
tests at existing recordings or production receivers.

Keep changes focused and include a small regression check for changed behavior.
Documentation-only work does not require a new Pi deployment or endurance run.
Use a new version for changed installed payloads; do not replace a published tag
or release asset in place.
