# TinyPiRelay

## Quick install

The current installer is **0.5.17-dev**, a development prerelease. Fresh-device
onboarding, port 80 and mDNS still need physical-Pi validation. For an existing
installation, use the [upgrade guide](docs/INSTALLATION.md#existing-installations-upgrades-and-rollback).

1. Flash **Raspberry Pi OS Bookworm or Trixie**. For the current Pi 4 evaluation
   target, use Trixie 64-bit. In Imager, configure a unique hostname, networking,
   an OS administrator and SSH. No microphone or TinyPiRelay configuration file
   is required.
2. Connect using `ssh <admin-user>@<hostname>.local` (or the Pi's IP address).
3. Paste the following **into the Pi's SSH terminal**, not Windows PowerShell.
   This single command downloads and runs the hosted installer, which installs
   TinyPiRelay's prerequisites, configures the services and starts the web GUI:

   ```sh
   tpr_setup=$(curl --disable -fsSL --proto '=https' --proto-redir '=https' https://github.com/gall-levi-code/TinyPiRelay/releases/download/v0.5.17-dev/install-tinypirelay-0.5.17-dev.sh) && sudo /bin/sh -c "$tpr_setup"
   ```

4. Open `http://<hostname>.local/` or `http://<device-ip>/` in your browser—no
   special port needed. Create your web username and password (at least 12
   characters), then sign in. Attach audio later and use **Audio → Refresh audio
   inputs** to select a validated device/mode, save and restart media.

Review the [installer and release notes](https://github.com/gall-levi-code/TinyPiRelay/releases/tag/v0.5.17-dev)
before granting root access. The command downloads the complete script before
running it and stops if the download fails. It trusts the installer served by
this repository over HTTPS; the installer verifies its pinned application archive
before extraction. For explicit installer-checksum verification, use the
[download-and-verify procedure](docs/INSTALLATION.md#download-verify-and-install).

The initial download needs `curl` and working CA certificates; if either is
missing, the linked procedure includes their setup. All remaining application
prerequisites are handled by the installer. It may ask for your OS account's
`sudo` password; TinyPiRelay's web account is created separately in the browser.

Use a **trusted LAN**: HTTP passwords/sessions are unencrypted, and the first
person reaching an unconfigured device can create its administrator account.
For SSH-only access, manual downloads, upgrades and recovery, see the
[full installation guide](docs/INSTALLATION.md).

## Current status — 2026-09-13

Development prerelease **0.5.17-dev** revamps [installation](docs/INSTALLATION.md):
automatic prerequisites including recovery tools, deployment without a microphone,
first-access web account creation, and trusted-LAN HTTP port 80 for fresh installs.
Existing credentials/settings and localhost-only access survive upgrades unless
LAN migration is explicitly selected. Source is hosted in
[gall-levi-code/TinyPiRelay](https://github.com/gall-levi-code/TinyPiRelay), with
version-pinned installation bundles in [GitHub Releases](https://github.com/gall-levi-code/TinyPiRelay/releases/tag/v0.5.17-dev).
This onboarding candidate has not been deployed to the Pi; its installed release
remains **0.5.16-dev**. The earlier acceptance below retains its historical scope.

Step 6 hardening/release review is complete with a **conditional** recommendation
for controlled development/evaluation, not a general release. It proceeded at
the operator's request, with
further endurance runs deferred. The Pi 4 now runs `0.5.16-dev`, adding opt-in,
original-preserving [FLAC recovery](docs/FLAC_RECOVERY.md) after the `0.5.15-dev`
hardening review. The authorized upgrade, 15 target recovery tests and real
web-API recovery on a synthetic damaged file passed; existing recordings and
credentials were preserved. See [deployment evidence](docs/evidence/RPI4_FLAC_RECOVERY_2026-09-11.md).
The final Windows and isolated Linux suites each ran 472 tests successfully;
finite Firefox/Chromium interaction checks passed. That earlier review did not
publish a release. See
[release readiness](docs/RELEASE_READINESS.md) for the review outcome and exact
remaining gates, and [project state](docs/PROJECT_STATE.md) for candidate identity.

The production desktop/large-tablet GUI now connects all ten pages, including
Storage playback/download/checks, maintenance/account controls, and stable
interactive controls under live telemetry. It targets at least 1160×720;
phone layout is not supported. The Pi 4's finite 30-minute native 24-bit FLAC
recording + 60 Hz spectrum + 16-bit FLAC/Matroska/SRT + BirdNET test passed.
That is not endurance, kiosk, fault-recovery or blanket board acceptance. The
Zero W's later dashboard workload saturated the board and remains unaccepted.

The historical implementation/evidence summary below retains its dated scope.

TinyPiRelay is a headless Raspberry Pi audio appliance for ALSA capture,
lossless recording, monitoring, and SRT transmission. Milestones 1-4 are
implemented: the repository contains a read-only host probe, evidence-gated
stream recipes, a programmable PyGObject/GStreamer media service, a bounded
AF_UNIX control protocol, and a separate authenticated no-build web UI with
five-Hz state snapshots and numeric monitoring telemetry up to 60 Hz. Step 4 adds a staged,
idempotent installer, least-privilege systemd packaging, an installed operator
CLI, and a pinned HTTPS bootstrap mechanism. Its finite physical acceptance run
passed on a Raspberry Pi Zero W with an iMM-6C, including installation, actual
service identities and sandboxing, repeat no-op installation, capture, the
authenticated loopback dashboard, web-only restart isolation, a finalized and
decoded FLAC recording, and automatic recovery after reboot. Step 4 is complete
for that finite scope. Step 5 remains incomplete: historical candidate `0.5.9-dev` added
generation-scoped bounded queue metrics, a bounded Pi profiler, a disposable
FFmpeg SRT decode-continuity receiver, the approved production dashboard, and
real device/storage/network telemetry plus a linear 30-second bitmap-conveyor
spectrogram. Its high-detail logarithmic frequency view supports editable bounds
and axis wheel zoom, drag pan, and reset without changing capture or media settings.
The browser retains at most 1801 raw arrival-time samples; there is no time warp
or progressive historical peak aggregation. A historical `0.5.6-dev` Pi 4 B run sustained a 512-band
spectrum, stereo meters, and one authenticated dashboard at 60 Hz with 22.84%
total system CPU, no throttling, and bounded queues. That is not SRT,
multi-client, endurance, rotation, or fault acceptance. The retained Zero W
dashboard run saturated that board and remains unaccepted. Step 6 review can
proceed under the operator's amendment; general release acceptance remains open.

The evidence gate declares three exact representations: 48 kHz stereo PCM
S16LE, FLAC, and 128 kbit/s Opus, each in streamable Matroska over SRT caller
mode. The media/control stack admits only those IDs. Step 4 does not add
MPEG-TS or a production receiving bridge; that bridge work remains deferred
until after Step 6. The disposable Step 5 FFmpeg evidence receiver does not
change the product boundary. Two finite production-engine scenarios passed on
a physical Zero W, including the
iMM-6C path, dynamic Opus bitrate, native FLAC lifecycle, reconnect, bounded
monitoring, and ordered shutdown. Step 3 additionally passed finite physical
web-crash, slow-SSE, FLAC-finalization, and spectrum-reattachment checks on the
same board, and Step 4 passed the finite installed-appliance checks above. These
are not endurance, performance-headroom, late-resource-recovery, or blanket
hardware acceptance; the Step 4 installed-service run also did not exercise
active SRT. See
[`Step 2 evidence`](docs/evidence/RPI_ZERO_W_STEP2_2026-08-18.md),
[`Step 3 evidence`](docs/evidence/RPI_ZERO_W_STEP3_2026-08-19.md), and
[`Step 4 evidence`](docs/evidence/RPI_ZERO_W_STEP4_2026-08-20.md), and the
[`dashboard deployment report`](docs/evidence/RPI_ZERO_W_DASHBOARD_2026-09-03.md).
The bounded Step 5 shakedown does not assign a board profile or final hardware
compatibility claim.

## Local checks

The probe, evidence gate, configuration/controller checks, and most unit tests
use only the Python standard library. GStreamer adapter and integration checks
require PyGObject and the documented GStreamer elements. From the repository
root on Raspberry Pi OS or another Unix-like host:

```sh
PYTHONPATH=src python3 -B -m tinypirelay.probe --format human
PYTHONPATH=src python3 -B -m tinypirelay.probe --format json
PYTHONPATH=src python3 -B -m tinypirelay.compatibility validate
PYTHONPATH=src python3 -B -m unittest discover -s tests -p 'test_*.py' -v
```

On Windows, replace `PYTHONPATH=src python3` with:

```powershell
$env:PYTHONPATH = 'src'
py -3 -B -m tinypirelay.probe --format human
py -3 -B -m tinypirelay.probe --format json
py -3 -B -m tinypirelay.compatibility validate
py -3 -B -m unittest discover -s tests -p 'test_*.py' -v
```

The probe is read-only. Missing host tools are reported; it never installs
packages or changes services. It always prints the requested report and exits
`1` when any requirement or the Raspberry Pi OS/architecture baseline is
missing or unverified; that diagnostic exit is expected on the Windows
development host. See `docs/SPIKE_MEDIA.md` for the retained media
spike procedure and [`docs/PROJECT_STATE.md`](docs/PROJECT_STATE.md) for the
exact milestone state.

The historical `0.5.9-dev` candidate ran 413 tests on Windows with no failures and
19 expected platform-dependent skips. Offline canvas/interaction checks and
synthetic real-browser checks in Firefox 134 and Chromium 133 passed; these are
not live Pi or kiosk acceptance. A separate finite 32-second Firefox 134 check
passed against the installed Pi 4 release and actual microphone feed: the full
and 4-12 kHz views were visually continuous, with no captured errors or non-login
API writes. This is not kiosk, endurance, or guaranteed-frame-rate acceptance; see the
[`linear dashboard report`](docs/evidence/RPI4_LINEAR_DASHBOARD_2026-09-08.md)
for the separately recorded validation boundary. Historical `0.5.7-dev` ran 412
tests on Windows with 19 expected skips. The earlier physical Pi dashboard
deployment exercised 409 tests without an application-test failure, although a
corrected second command was required for six initially missing test-module
imports; that result is retained in the dashboard deployment report.

## Installation details

Start with [Quick install](#quick-install) above for a fresh device.

The reviewable installer, service policies, lifecycle commands, and bootstrap
mechanism are documented in
[`docs/INSTALLATION.md`](docs/INSTALLATION.md). Their design and evidence
boundary are recorded in
[`ADR 0004`](docs/adr/0004-installer-system-integration.md). The pinned launcher
verifies the archive before extraction. Review the release notes and trust
boundary in the installation guide before running downloaded code as root.

The finite Zero W installation and reboot acceptance is retained in the
[`Step 4 physical report`](docs/evidence/RPI_ZERO_W_STEP4_2026-08-20.md).
The later `0.5.3-dev` dashboard upgrade and preservation checks are retained in
the [`dashboard deployment report`](docs/evidence/RPI_ZERO_W_DASHBOARD_2026-09-03.md).
Upgrade, reverse-version rollback, interruption recovery, preservation
uninstall, and explicit purge remain isolated-fixture results rather than
physical-board claims.

## Media service

`config/example.json` is intentionally disabled. With a control socket the service
can run idle without a microphone; select a validated capture mode later in the
GUI. For a configured developer capture run, use a private configuration and run
the owner from the repository root:

```sh
PYTHONPATH=src python3 -B -m tinypirelay.media_service --config /path/to/config.json
```

The process starts capture, starts the configured stream after capture reaches
PLAYING, and performs ordered shutdown on `SIGINT` or `SIGTERM`. Add
`--record-on-start` only when recording should begin immediately. This remains
the developer entry point. The Step 4 installed media service has also passed
finite iMM-6C capture, recording, restart-isolation, and reboot-recovery checks
on the Zero W.

For a retained real-engine loopback check with synthetic capture:

```sh
mkdir -p /tmp/tinypirelay-step2-recordings
PYTHONPATH=src python3 -B spikes/step2_media_integration.py \
  --source-factory audiotestsrc \
  --recording-directory /tmp/tinypirelay-step2-recordings \
  --output /tmp/tinypirelay-step2-report.json
```

The integration harness runs for roughly 45 seconds, creates FLAC files in the
explicit directory, and exits nonzero unless all finite checks pass. Its local
SRT receiver is disposable evidence tooling, not part of the production media
service. Reproducible synthetic and physical iMM-6C invocations and their
results are in the Step 2 evidence report.

## Web and control plane

On a Unix-like host, create the initial web credential interactively, then run
the media and web processes separately with the same permission-restricted
control socket:

```sh
PYTHONPATH=src python3 -B -m tinypirelay.web_service passwd \
  --credentials /private/runtime/web-credential.json --username operator

PYTHONPATH=src python3 -B -m tinypirelay.media_service \
  --config /private/config.json --control-socket /private/runtime/control.sock

PYTHONPATH=src python3 -B -m tinypirelay.web_service serve \
  --credentials /private/runtime/web-credential.json \
  --control-socket /private/runtime/control.sock \
  --bind 127.0.0.1 --port 8080
```

These manual developer commands use an intentional loopback bind; use an SSH
tunnel for remote access. Fresh installed appliances use trusted-LAN port 80
unless `--web-access loopback` is selected; upgrades preserve existing access.
See
[`docs/WEB_CONTROL.md`](docs/WEB_CONTROL.md) for permissions, configuration,
security behavior, HTTP/control contracts, and exact nonclaims.

## License

TinyPiRelay is licensed under the MIT License. See `LICENSE`.
