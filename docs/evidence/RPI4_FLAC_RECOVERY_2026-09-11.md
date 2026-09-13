# Pi 4 FLAC recovery deployment — 2026-09-11

Publication copy: operator-specific paths are omitted; the original report is
retained privately. See [redaction notes](PUBLICATION_REDACTIONS.md).

## Installation

The operator authorized deploying the completed recovery feature and installing
FFmpeg. The existing Pi 4 (`tinypirelay-pi4`, aarch64) was upgraded from
`0.5.14-dev` to `0.5.16-dev` with the existing reviewed installer. Its preflight
had no errors; the subsequent repeat plan is `noop`. The installer backed up
and preserved the installed configuration. No new credential was created.

The distribution's `ffmpeg` package is `8:7.1.5-0+deb13u1+rpt2`. Installation
with no recommends/no upgrades added 47 packages, with no package removals or
upgrades. No distribution upgrade, reboot or enduring monitor was requested.

## Identity and preservation

- Source archive: 602308 bytes, 160 members rooted at `TinyPiRelay/`.
- Archive SHA-256:
  `c7068fa432101032595f47d5ba0a9351a3b5590a886a2e4d7ba3a8feba344253`.
- Local/extracted installer payload fingerprint:
  `1bd21d14e192af1dfddc3a0685a83dc2635f222d29293d911ac80a7b45470ad3`.
- Installed `recording_library.py` SHA-256:
  `c1226325ea5b9b81306837161429ba7419e7e52514690ab32789e7053f393a77`.
- Installed `app.js` SHA-256:
  `002e5e4ad6165424979364a4f4150efeee5867b9436eb3f9de669c8fb33a322a`.

The installed source hashes match the local runtime. Before/after SHA-256
checks matched all four original FLAC recordings, media configuration and web
credential. The existing iMM-6C native mono S24LE/48 kHz configuration and
512-band/60 Hz monitoring settings were preserved. Capture resumed after the
upgrade; recording and SRT remained stopped. Both services reported active,
running and zero automatic restarts.

The local SSH tunnel at `http://127.0.0.1:18082/` was restored; its served
JavaScript contains the new recovery action and copy-result state.

## Finite target tests

The 15 recovery tests passed on the Pi in 5.489 seconds, including real
FFmpeg 16-/24-bit synthetic truncation/finalization recovery, strict output
validation, original preservation and mocked cancellation/space/failure paths.
All generated inputs were disposable fixtures, not user recordings.

The installed, authenticated web API was then tested with the unchanged existing
web credential and a single generated 0.5-second stereo 24-bit/48 kHz FLAC,
truncated by 128 bytes. The exact `/api/storage/action` recovery request passed
session/CSRF checks and was executed by the real least-privilege media service,
not an isolated library instance. The copy was reported recovered/durable;
authenticated HTTP byte-range access returned 206 and its 42-byte FLAC header.

Independent strict FFmpeg decoding confirmed 23040 recovered samples out of
24000 original samples, with exact original-prefix PCM and matching STREAMINFO
MD5 `f3b25479f57b051a6babae604fad75b3`. The damaged source remained byte-for-byte
unchanged. Capture stayed running and recording/SRT stayed stopped before and
after. The damaged test file and recovered copy were moved, after exact
name/inode checks, to the private `recovery-smoke-jo8u8bz0/` evidence subdirectory;
the normal Storage list retains only the four user recordings.

Synthetic source SHA-256:
`36da9bda7eb63a19f4b9e4823c66dd723f71d51100c7fe205257babaf985a170`.
Recovered copy SHA-256:
`f1726a802bd913ee81372b1696e7295e4493d59bfe7f9fa858072e89bf9dacf1`.
The helper and credential-free `summary.json` are retained alongside evidence.
No real damaged user recording was repaired, and no live browser-click test
is claimed: this check exercised the same real HTTP action path used by the
button, alongside the prior mocked Firefox/Chromium interaction tests.

## Evidence locations and limits

Private deployment evidence is on the Pi under
`/root/tinypirelay-recovery-20260911.dN0ZOh/`: preserved file fingerprints,
package log, upgrade plan/apply log, repeat plan, targeted tests and diagnostics.
The workstation artifact is retained privately; its operator-specific path is omitted.
Documentation bookkeeping followed archiving and does not change runtime identity.

This is a finite deployment/recovery check, not endurance, kiosk, power-loss,
native write/close-fault or blanket board acceptance. The
[conditional release gates](../RELEASE_READINESS.md) remain in force. No
BirdNET/MediaMTX configuration, original recording, password or public release
was changed. Recovering audio still cannot restore missing samples or establish
the complete original capture.
