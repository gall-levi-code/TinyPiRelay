# ADR 0001: Minimal process and code architecture

- Status: accepted; three exact stream representations are evidence-backed for Step 2 implementation
- Date: 2026-08-17
- Evidence updated: 2026-08-18

## Context

TinyPiRelay must use one application codebase across supported 32-bit and 64-bit Raspberry Pi OS systems, including the ARMv6 Zero W floor. Capture and streaming must continue independently of the web process. Step 1 must prove the media building blocks without creating the production media service or UI.

## Decision

Use one small Python codebase, with PyGObject only at the GStreamer boundary and Python's standard library for control/domain code where practical. Later milestones will run the media owner and web application as separate least-privilege processes. The media process will own the GStreamer graph and authoritative runtime state; web failure or slow telemetry consumers must not feed back into capture.

Keep the boundaries narrow:

- domain/configuration and probe results are plain, serializable data;
- the media boundary is a programmable PyGObject application, not a shell pipeline;
- inter-process control will use a local, permission-restricted Unix-domain interface implemented with standard-library facilities unless Step 2 produces evidence that this is insufficient;
- media branches use independent queues, and disposable monitoring data uses bounded/leaky queues;
- persistent configuration will be validated before use and written atomically; secret storage remains separate and restrictive;
- no architecture or performance tier is inferred from userland bitness.

The Step 1 code is limited to a read-only platform probe, a draft configuration schema, compatibility evidence and its fail-closed gate, and tests. The spike commands in `docs/SPIKE_MEDIA.md` are evidence tools only. They are not the production controller.

Provisional media topology starts by negotiating and validating a native source mode. When no conversion is needed, use one tee after the validated native caps. When stream/monitoring need converted caps while recording should remain native, use a native tee with a queued recording branch and one queued convert/resample path feeding a second tee for stream and telemetry. Every branch is queued; disposable telemetry queues are bounded/leaky. Step 2 must finalize placement from observed negotiated caps and measurements.

Physical Step 1 evidence supports this placement. The iMM-6C exposed native mono S16LE/S24LE at 48 kHz; the exact tested stream representations consumed stereo S16LE/48 kHz after mono-to-stereo conversion. In compatibility records, `source_audio` names the representation input caps after conversion, not a claim that the capture device is natively stereo. Native recording should remain ahead of conversion when preservation is required.

## Stream compatibility gate

The initial candidates are 48 kHz stereo PCM S16LE, FLAC, and Opus at 128 kbit/s, each in streamable Matroska over SRT caller mode. A candidate is not a declared capability until retained evidence records a successful real GStreamer sender and GStreamer receiver run plus documented FFmpeg/ffplay and VLC interoperability attempts. All three exact candidates met this gate in finite physical ARMv6 runs with GStreamer 1.26.2 and cached-container FFplay 7.1.5; VLC was explicitly unavailable, so no VLC claim is made.

Streamable Matroska is the initial framing choice because it can self-describe all three candidates. It avoids out-of-band raw-PCM caps and avoids assuming unverified PCM/FLAC/Opus support in MPEG-TS. This rationale does not establish compatibility; each codec/container/receiver combination remains evidence-gated.

## Consequences

- The control and domain layers remain portable and inexpensive on ARMv6; GStreamer-specific code stays isolated.
- A later web implementation can be restarted without owning or stopping capture.
- Step 2 may implement only the three declared exact representations; other representations remain unavailable until their own evidence promotes them.
- A finite leaky-queue test isolated 251 stream handoffs from a deliberately slow 13-handoff branch, and a programmable Opus spike changed 128/192/64/96/128 kbit/s with continuous receive. Step 2 must still implement lifecycle, error handling, reconnect behavior, branch removal and runtime mutation in PyGObject; the spike pipelines and mutation utility cannot be promoted as that implementation.
- The short runs do not establish endurance, reconnect, packet-loss, recording, simultaneous monitoring, audible quality or broad hardware acceptance.
- The local Unix-domain protocol details, authentication/UI, systemd units, installation and tuning are deliberately deferred to their scoped milestones.

## Alternatives rejected

- A single media/web process violates the required failure isolation.
- `gst-launch-1.0`, FFmpeg, or VLC as the production engine cannot provide the required programmable lifecycle and are retained only for evidence and diagnostics.
- Separate 32-bit and 64-bit implementations would duplicate behavior without addressing measured performance.
- A Node frontend toolchain or native Python package stack adds cost without a demonstrated v1 need.
