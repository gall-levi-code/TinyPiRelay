# Step 1 — Architecture and Technical Spike

## Goal

Establish the smallest maintainable repository architecture and collect evidence for ALSA -> GStreamer -> PCM/FLAC/Opus -> SRT on the supported platform. Do not build the production media service or GUI yet.

## Required work

- Read the three shared documents and current `docs/PROJECT_STATE.md` before editing.
- Inventory existing code and choose the minimal architecture that preserves web/media process isolation, testability, ARMv6 viability, and one shared 32/64-bit codebase.
- Add a read-only platform/dependency probe for OS, architecture/userland, board/CPU/RAM/storage, ALSA devices, package availability, and required GStreamer elements. It must produce actionable human-readable output and a stable machine-readable result.
- Establish a minimal source/test/docs layout and configuration schema sufficient for later milestones. Avoid extension points for unrequested v2 features.
- Create reproducible spike pipelines or a small spike harness for synthetic audio and, where available, ALSA capture. Test PCM, FLAC, and Opus separately over SRT.
- Record codec/parser/muxer/framing choices and receiver results. Validate with a GStreamer receiver and attempt FFmpeg/ffplay and VLC where available. Unsupported combinations must fail closed and remain absent from declared capabilities.
- Prove or explicitly leave pending: runtime `opusenc` bitrate property changes, independent queue behavior, and actual Raspberry Pi OS/ARMv6 package availability.
- Add a draft architecture decision record and a compatibility evidence template. Add an MIT license only if the repository does not already have a conflicting license and the user request clearly authorizes initialization.

## Constraints

- `gst-launch-1.0` is acceptable evidence tooling, not the production design.
- Do not install system packages or systemd units in this milestone.
- Do not build authentication, the full API/UI, persistent reconnect logic, recording lifecycle, or performance tuning.
- Do not claim hardware support from a non-Pi workstation or container.

## Definition of done

- The repository has a justified, minimal architecture and runnable local checks.
- The probe distinguishes missing, present, and unverified requirements without changing the host.
- Each proposed v1 stream representation has exact sender/receiver commands and an evidence status.
- Tests cover probe parsing/normalization and compatibility gating with representative fixtures.
- Documentation states what remains unknown on Zero W, 32-bit, 64-bit, and real ALSA/SRT hardware.
- Relevant tests/static checks pass, and `docs/PROJECT_STATE.md` is updated for Step 2.
