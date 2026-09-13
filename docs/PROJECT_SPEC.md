# TinyPiRelay Project Specification

Status: source of truth for v1. Milestone prompts may refine implementation details, but must not silently change these product requirements.

## Product

TinyPiRelay is an MIT-licensed, headless Raspberry Pi audio appliance for reliable ALSA capture, local lossless recording, monitoring, and SRT transmission. It installs onto an existing Raspberry Pi OS Lite system; it is not a custom OS image and is not tied to any particular microphone or application domain.

V1 must:

- discover ALSA-visible USB Audio Class and I2S capture devices;
- expose only configurations the selected device can actually capture, clearly labeling native and software-converted modes;
- support mono and stereo, 44.1/48 kHz, 96 kHz where supported, 16-bit, 24-bit where supported, and applicable 32-bit container formats;
- send one SRT stream in caller mode using PCM, FLAC, or Opus when a tested codec/framing/receiver combination exists;
- change Opus bitrate at runtime without restarting capture or dropping SRT when the installed GStreamer element supports it;
- start and stop timestamped, rotated FLAC recording without restarting an active stream;
- provide a password-protected desktop/tablet web UI with meters, a browser-rendered spectrogram, configuration, logs, and diagnostics;
- recover predictably from receiver and network interruptions; and
- run from one application codebase on supported 32-bit and 64-bit Raspberry Pi OS systems.

The media service must not depend on the web process. A web crash, slow browser, or disconnected telemetry client must not stop or back-pressure capture or streaming.

### Recorded operator amendments (2026-09-11)

The operator replaced the original phone-friendly layout requirement with a
desktop/large-tablet, single-viewport interface: left navigation, no vertical
page scrolling, and no intentional text wrapping/truncation. The production
layout targets at least 1160×720; phone layout is not a release requirement.

The operator authorized Step 6 hardening/release review before completing
Step 5 endurance testing, to avoid further long monitoring sessions. No new soak
is authorized by that instruction. Missing endurance/fault results remain
explicitly unverified; proceeding with review does not turn them into passes
or authorize publication, deployment, or a blanket hardware-support claim.

## Supported platform and implementation constraints

- Official target: Raspberry Pi OS Lite, current stable release first. Retain Bookworm compatibility where practical; never perform a major distribution upgrade.
- Hardware floor: Raspberry Pi Zero W v1.1 (ARMv6, 512 MB). More capable boards receive richer runtime defaults, not a different application.
- Media engine: programmable GStreamer application using ALSA and the SRT plugin. `gst-launch-1.0` is appropriate for spikes, not the production controller.
- Control layer: lightweight Python/PyGObject is preferred unless measured ARMv6 evidence supports a better choice.
- UI: plain HTML/CSS/JavaScript. No required Node/npm toolchain or heavyweight frontend framework.
- Dependencies: prefer Raspberry Pi OS/Debian APT packages. Avoid native PyPI dependencies and bundled binaries unless necessary and verified on ARMv6.
- FFmpeg/ffplay and VLC are interoperability and diagnostic tools, not the production streaming engine.
- Architecture detection and performance profiling are separate. Never equate a 32-bit userland with low performance.

Preflight must determine OS/release, `uname -m`, userland bitness and dpkg architecture, board model/revision, CPU, RAM, storage, package availability, ALSA visibility, and actual GStreamer element availability. At minimum probe `alsasrc`, `audioconvert`, `audioresample`, `capsfilter`, `tee`, `queue`, `level`, `spectrum`, `opusenc`, `flacenc`, and `srtsink`. Report missing requirements before installation mutates the host.

## Media architecture

The intended graph is:

```text
ALSA source -> convert/caps -> tee
                              |-> queue -> stream codec/framing -> SRT caller
                              |-> queue -> FLAC recorder
                              |-> leaky/bounded queue -> level telemetry
                              `-> leaky/bounded queue -> spectrum telemetry
```

Exact placement of conversion, resampling, and tees must be justified by negotiated caps and the need to keep native capture when possible. Every branch needs independent queueing. Monitoring data is lossy/disposable and must be bounded.

Priority under contention:

1. capture continuity;
2. active SRT stream;
3. local recording;
4. diagnostics;
5. meters;
6. spectrogram.

## Codec, framing, and SRT rules

Treat codec, parser/framing, muxer, SRT transport, and receiver support as separate compatibility dimensions. Do not force every codec into MPEG-TS or claim a combination merely because individual GStreamer elements exist.

For each supported streaming representation, record the exact sender pipeline, receiver command/URI, codec, framing/container, installed element versions, and results with GStreamer plus FFmpeg/ffplay and VLC where applicable. An unverified combination remains experimental or unavailable in the UI.

V1 SRT behavior:

- caller mode only;
- destination host/port, latency, optional stream ID, and encryption/passphrase where supported;
- connection state, last error, reconnect count, and available SRT statistics;
- bounded reconnect behavior with no hot loop;
- configuration schema may leave room for listener, rendezvous, multiple destinations, and bonded transports, but the UI must not expose them.

Opus bitrate presets should include useful values such as 64, 96, 128, and 192 kbps. Automatic adaptation is out of scope until a trustworthy feedback signal exists.

## Recording and storage safety

FLAC is the only required local recording format. The directory and rotation duration are configurable; hourly rotation is a reasonable default. Do not delete old recordings automatically in v1.

When the filesystem containing the recording destination reaches 90% used capacity:

1. safely finalize the active FLAC file when possible;
2. stop only the recording branch;
3. keep active capture and SRT streaming running;
4. raise a prominent UI warning and durable log event; and
5. refuse to restart recording until capacity is below the safety threshold.

The same isolation principle applies to a missing, unmounted, read-only, or failed recording destination. Use the destination filesystem's usage, not an unrelated root-filesystem value. Handle threshold crossings during recording and an already-full destination before recording starts.

## Audio discovery and configuration

Probe each capture device for channel counts, rates, and formats. The API and GUI must derive choices from a validated capability model; never offer a Cartesian product of individually observed values if the device does not support that combination. Label conversion explicitly and prefer native modes.

Configuration changes must be validated before application. Use atomic durable writes, restrictive permissions for secrets, and an explicit state machine so concurrent start/stop/reconfigure requests cannot corrupt state. A format change may require a controlled media restart; the UI must say so before applying it. Recording and supported Opus bitrate changes must not restart the stream.

## Monitoring and resource behavior

Meters must provide per-channel peak, RMS where available, clipping, and no-signal state. The spectrum branch should send bounded numeric magnitudes to the browser, which renders the spectrogram. Server-Sent Events are preferred if one-way telemetry is sufficient; use WebSockets only when they provide a measured advantage.

Initial, benchmark-dependent profile targets:

- Legacy/Zero W: one stream, conservative FLAC settings, roughly 5-10 spectrum updates/sec, about 512 bands or an equivalently modest FFT, slower UI refresh, bounded logs.
- Standard/Zero 2 W and Pi 3: full v1 features with roughly 10-20 spectrum updates/sec and moderate resolution.
- Full/Pi 4, Pi 5, CM4, CM5: higher monitoring resolution where useful, without implementing speculative multi-output features.

With sustained CPU pressure, reduce or suspend spectrum work first, then lower nonessential UI telemetry. Do not silently alter capture format, stop SRT, or discard recording to preserve visualization. With no spectrum clients, reduce or suspend spectrum processing where practical.

## Networking

Linux owns network setup and routing. TinyPiRelay detects interfaces, IP/link state, Wi-Fi RSSI when available, and the route/interface used for SRT. It may support a safe bind/interface preference and must reconnect after normal connectivity restoration.

V1 excludes bonding, SRTLA, cellular aggregation, hotspot/AP management, complex policy routing, multiple active WAN paths, and seamless handover guarantees.

## Web security and service isolation

- No default credentials. Before installation completes, an interactive SSH/sudo user creates the initial web username and password.
- For `curl | bash`, read credentials from `/dev/tty`, not the pipe or command line. If no controlling TTY exists, stop with safe instructions for an explicit noninteractive mechanism; never log credentials.
- Store only a salted, slow password hash using a mature OS/library primitive. Provide `sudo tinypirelay passwd` for reset by a local sudo-capable user.
- Protect mutating requests against CSRF, use secure session handling and cookie flags appropriate to the deployment, rate-limit authentication attempts, validate all inputs, escape rendered data, and never expose SRT passphrases or password hashes in logs or diagnostics.
- Bind conservatively by default and document that production Internet exposure requires TLS through a reviewed reverse proxy or a later native TLS design.
- Run web and media components as dedicated, least-privilege systemd services. Grant only the device/filesystem access each requires. Do not run the application as root.

## Installation and lifecycle

Provide a reviewable repository installer and an eventual pinned, HTTPS one-line bootstrap. The bootstrap must download a versioned artifact/script, verify what the release process can support, then invoke the local installer; do not stream a mutable branch directly into a privileged shell.

Installation must be staged: read-only preflight, explicit plan, credentials/config creation, package install, files/users/permissions, service enablement, postflight validation. It must be idempotent and recover cleanly from interruption. Do not overwrite user configuration without backup and confirmation.

Support status, diagnostics, password reset, safe upgrade, and uninstall. Uninstall must state what it removes and preserve recordings/configuration unless the user explicitly requests their deletion. Document SSH installation, service checks, GUI discovery/address, recovery, and offline/manual installation.

## Required failure cases

Tests or reproducible procedures must cover: no capture device; device removal and return; unsupported caps; SRT receiver absent; DNS failure; network loss and recovery; invalid SRT settings; rapid/repeated commands; web crash; slow telemetry client; disk threshold before and during recording; read-only/unmounted recording destination; corrupt config; process/power interruption during file rotation or install; missing GStreamer elements; unsupported OS/architecture; service restart; and reboot recovery.

Long-duration tests must watch audio continuity, reconnect behavior, memory/file-descriptor growth, log growth, CPU/temperature, recording rotation/finalization, and UI-client churn.

## Compatibility claims and acceptance

Use only these labels:

- Hardware tested: exercised on the named physical board with recorded evidence.
- CI validated: software or packaging checks passed without proving hardware behavior.
- Likely compatible / untested: reasoned expectation only.
- Unsupported: known blocker or deliberately outside scope.

A milestone is complete only when relevant tests and static checks pass, docs match observed behavior, evidence and commands are recorded, limitations are explicit, and no unsupported feature is presented as working. If hardware is unavailable, leave hardware acceptance open.

## Prior art and primary references

Study prior art for concepts, not code copying. BELABOX repositories use several GPL/AGPL-family licenses; inspect every source license and provenance before reuse. TinyPiRelay's MIT license does not permit casually transplanting incompatible code.

- [Raspberry Pi OS documentation](https://www.raspberrypi.com/documentation/computers/os.html)
- [Raspberry Pi hardware documentation](https://www.raspberrypi.com/documentation/hardware/rpi/os.html)
- [Raspberry Pi computer and USB notes](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html)
- [GStreamer documentation](https://gstreamer.freedesktop.org/documentation/)
- [GStreamer `srtsink`](https://gstreamer.freedesktop.org/documentation/srt/srtsink.html)
- [GStreamer `opusenc`](https://gstreamer.freedesktop.org/documentation/opus/opusenc.html)
- [GStreamer `spectrum`](https://gstreamer.freedesktop.org/documentation/spectrum/)
- [BELABOX organization](https://github.com/BELABOX)
- [ZuidWest FM Encoder](https://github.com/oszuidwest/zwfm-encoder)

References describe capabilities and influences; the installed versions and physical hardware remain authoritative.
