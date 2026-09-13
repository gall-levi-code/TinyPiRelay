# Pi 4 expanded admin pages — 2026-09-09

## Scope

Storage, System/Network, Audio appearance, Settings/maintenance/account, and
Logs/About now use the production application. The separate design mockup and
the capture graph are unchanged. This is finite webGUI acceptance, not Step 5
endurance/fault acceptance or Step 6 release approval.

The final candidate is `0.5.13-dev`. Its source archive is 567721 bytes with
SHA-256 `84853313cb3ffe390ffc7e1f56a6ab57ee46c57eeda5c8f62dd46173a95d9dcf`.
The preceding `0.5.12-dev` source archive is 564564 bytes with
SHA-256 `9c04c12f08436a2c373bc2b589806e6c63bdfbb3e5a7a8655c889ffd0a98af28`.
Both include source/tests/supporting documentation; the final evidence and
project-state bookkeeping were completed after archiving.

## Validation record

- Final `0.5.13-dev` Windows Python 3.12: 442 tests passed in 46.495 seconds,
  30 platform skips.
- Isolated Linux `0.5.12-dev`: 442 tests passed in 69.202 seconds, two environment skips.
- The storage fix additionally passed 24 focused Linux tests. The unprivileged
  regression was rerun from a root-owned `0700` checkout after adjusting only
  its import-before-privilege-drop test harness.
- Fixture browser checks passed 30 route/viewport combinations each in Firefox
  134 and Chrome 152: 1160×720, 1280×720, and 1920×1080. Checks covered native
  playback/seeking of a disposable seekable FLAC, numbered pagination, check and
  deletion confirmation, persisted appearance, password/maintenance validation,
  pending states, and disabled/unconfigured SRT form hydration. These checks
  used mock APIs, not live power or account changes.
- Pi Python 3.13: `0.5.13-dev` passed 442 tests in 53.662 seconds, with
  two environment skips, from the protected root-owned staging directory.
- `0.5.12-dev` installed successfully. Both application services are active
  with zero automatic restarts; the maintenance socket is listening. The
  real four-file catalog, FLAC byte-range response, native Firefox playback
  and seeking passed. A Stream-page overflow found at 1280×720 was traced to
  the long S24-to-S16 explanation. `0.5.13-dev` renders four concise fact rows,
  preserving the explicit 24-bit stream limitation and full hover detail.
  Firefox/Chrome fixture checks passed at all three widths with the actual
  long evidence annotation.
- Final `0.5.13-dev` installed Firefox 134 acceptance passed all ten routes at
  1280×720 with no document overflow, captured JavaScript errors, or non-login
  API writes. The real catalog contained all four originals. The 93551339-byte
  30-minute recording returned `206` for bytes 0–41 with the `fLaC` signature;
  native playback advanced and seeking landed at 5.0 seconds, with 1800.07
  seconds duration and no audio error. The maintenance helper reported available
  and idle; capture was running with recording/SRT stopped. The credential-free
  browser summary and screenshots are retained in the workstation's
  `tinypirelay-web-pages-20260909` validation directory.
- Final installed/runtime hashes match the validated local JavaScript, recording
  library and maintenance helper. The repeat installer plan reports `noop`,
  no planned changes and no preflight errors. Media, web and maintenance remain
  active with zero automatic restarts; the final preservation check passes.
- A real media-owner background integrity check passed for the smallest closed
  recording (2431959 bytes), using the existing GStreamer decoder. Capture
  remained running; the original file was not modified.
- The installed socket-activated helper completed `media.restart` through the
  permitted web identity. Capture resumed with the same active configuration,
  recording/SRT remained stopped, and the Pi boot ID did not change. The finite
  harness was corrected to tolerate the expected short control-socket absence
  while the newly started media process initializes, then passed end to end.

The initial `0.5.11-dev` live check exposed an ancestor-directory permission
mismatch. The library now traverses ancestors with Linux `O_PATH` and opens
only the final recordings directory for reading. The installed parent remains
`0710 root:tinypirelay-control`; no filesystem permissions were widened.

The pre-upgrade preservation check caught a concurrent saved SRT-format change
from null to `flac_48000_stereo_matroska` through the web configuration endpoint.
Replacing only that value in memory reproduced the original configuration hash,
confirming it was the sole change. Streaming remained disabled with no destination.
The newer setting was preserved, not overwritten. The `0.5.12-dev` and final
`0.5.13-dev` post-upgrade checks matched all six hashes against a separately
retained updated baseline:
four original FLAC files, media configuration, and web credential. S24LE / 48 kHz
/ mono capture remained active with recording and SRT stopped.

## Boundaries

File deletion and password replacement are implemented but tested with disposable
fixtures; existing Pi recordings and the real web password are not test targets.
Reboot/shutdown are wired with reauthentication, confirmation and verified media
finalization, but actual Pi power actions are not part of this acceptance run.

Recovery/re-encoding, Wi-Fi changes, UPS/battery telemetry and full system-journal
access remain deferred. File checks are bounded background decoder checks, not
recovery. Logs export the existing redacted in-memory event window. Browser-native
FLAC playback avoids server transcoding and depends on browser support; original
download is the fallback. See [web contracts](../WEB_CONTROL.md) for limits.
