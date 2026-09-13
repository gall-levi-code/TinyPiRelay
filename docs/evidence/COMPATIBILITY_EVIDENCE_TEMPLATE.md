# Compatibility Evidence Record

Copy this file for each physical-board test. Do not replace a pending field with an assumption. Compatibility claims must use exactly one label from `docs/PROJECT_SPEC.md`: **Hardware tested**, **CI validated**, **Likely compatible / untested**, or **Unsupported**. Execution status may separately be `pending`, `pass`, `fail`, or `blocked`.

## Record identity

| Field | Value |
|---|---|
| Test date/time and timezone | pending |
| Operator | pending |
| TinyPiRelay commit/diff identity | pending |
| Compatibility claim label | Likely compatible / untested |
| Execution status | pending |
| Linked logs/artifacts | pending |

## Board and operating system

| Hardware-matrix field | Value |
|---|---|
| Exact model and revision | pending |
| Architecture (`uname -m`) | pending |
| Kernel (`uname -srv`) | pending |
| OS name and release | pending |
| Userland bitness | pending |
| `dpkg` architecture | pending |
| CPU | pending |
| RAM | pending |
| Initial/runtime profile | pending |

## Physical setup

| Hardware-matrix field | Value |
|---|---|
| Power supply | pending |
| Powered hub/adapters | pending |
| Storage device/filesystem/free space | pending |
| Audio device and connection | pending |
| ALSA identifier | pending |
| Capture mode: channels/rate/format; native or converted | pending |
| Network interface and link | pending |
| Receiver host/device | pending |
| Thermal/ambient notes | pending |

## Installed media stack

| Hardware-matrix field | Value |
|---|---|
| GStreamer version | pending |
| GStreamer SRT plugin version | pending |
| `srtsink`/`srtsrc` SRT library details | pending |
| Relevant element versions | pending |
| FFmpeg/ffplay version | pending |
| VLC version | pending |

## Stream run

Repeat this section for each representation. Preserve the exact commands and resolved values in the test artifact; link the corresponding entry in `evidence/stream_compatibility.json`.

| Hardware-matrix field | Value |
|---|---|
| Representation ID | pending |
| Codec and parameters | pending |
| Parser/framing/container | pending |
| SRT mode, destination and stream ID | caller; pending |
| SRT latency, encryption/passphrase state | pending |
| Exact sender command | pending |
| Receiver and exact version | pending |
| Exact receiver command/URI | pending |
| GStreamer sender result | pending |
| GStreamer receiver result | pending |
| FFmpeg/ffplay attempt and result | pending |
| VLC attempt and result | pending |
| Duration | pending |
| Packet, error and reconnect observations | pending |
| Audio continuity/result | pending |

## Concurrent work and resource observations

| Hardware-matrix field | Value |
|---|---|
| Simultaneous recording settings | pending |
| Meter/spectrum settings and client count | pending |
| CPU baseline/peak/sustained | pending |
| Memory baseline/peak/sustained | pending |
| Temperature baseline/peak/sustained | pending |
| File-descriptor and log growth | pending |
| Recording rotation/finalization | pending |

## Required conclusions

- Runtime Opus bitrate mutation result and continuity evidence: pending.
- Independent-queue stall result and continuity/drop evidence: pending.
- Receiver/network interruption and recovery observations: pending.
- Limitations and known failures: pending.
- Exact result, including why the claim label is justified: pending.

Before assigning **Hardware tested**, confirm that the named physical board, audio device and receiver were exercised and that all relevant commands, versions, duration and observations above were retained. A host probe, command-generation test or synthetic fixture alone supports at most **CI validated** for that software behavior.
