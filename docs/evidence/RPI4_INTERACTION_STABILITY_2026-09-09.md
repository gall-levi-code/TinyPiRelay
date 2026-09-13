# Pi 4 control interaction stability — 2026-09-09

Publication copy: operator-specific paths are omitted; the original report is
retained privately. See [redaction notes](PUBLICATION_REDACTIONS.md).

## Scope and cause

The user's dropdown flicker came from shared snapshot rendering, not the
microphone or a need to slow telemetry. Each refresh reset control availability
before applying guards, refitted select fonts, and recreated Storage action and
pagination nodes. A temporary media outage also discarded the editable form's
configuration baseline, allowing automatic rehydration on recovery.

`0.5.14-dev` changes only the production client's runtime JavaScript:

- Apply final properties/attributes only when changed; actual safety changes
  still disable controls immediately.
- Cache text fitting by content/layout and defer styling of focused or pressed
  controls. Native selects fit the full option set.
- Reuse Storage rows by version-bound file ID and keep action/page nodes stable.
  Revalidate file identity and permissions after confirmation.
- Preserve drafts, capability choices and their original expected revision over
  temporary outages. Explicit reload/save/login/maintenance refreshes retain
  their existing hydration behavior and server conflict checks.
- Reject a dynamic Start/Stop press if its meaning changed before the click.

The shared contract for future controls is in [WEB_CONTROL.md](../WEB_CONTROL.md).
No new dependency, renderer framework, polling throttle or media graph change
was introduced. The separate design mockup is unchanged.

## Candidate identity

- Installed release: `0.5.14-dev`, upgraded from `0.5.13-dev`.
- Source/test/documentation archive: 573212 bytes.
- Archive SHA-256:
  `886c87151d9a79f2ee2c44047ec0fdfa8b4efec1d6b526d54272ec095dd9d572`.
- Installed and local `app.js` SHA-256:
  `8e4a1698f25e083193f7df733058e7ae1e2dbafca0ad6ab6819c54864a6f7e96`.
- The live-browser harness extension and final report/project-state bookkeeping
  followed archiving; runtime code did not change afterward.

## Validation

- Offline dashboard JavaScript checks and syntax checks passed. The focused
  web-asset Python suite passed 19 tests.
- Windows Python 3.12: 442 tests passed in 47.335 seconds, 30 platform skips.
  This run preceded only the final VERSION bump, not runtime changes.
- Pi Python 3.13, protected root-owned final staging: 442 tests passed in
  54.899 seconds, two environment skips.
- `spikes/interaction_browser_check.cjs` passed in Firefox 134 and Chrome 152.
  It checks all ten pages against repeated snapshots, focused/native-dropdown
  stability, draft/caret/revision retention across simulated outages, immediate
  genuine availability changes, stable Storage controls when other file data
  changes, and a pointer-down/state-change/pointer-up race. Simulated APIs reject
  non-login writes. Native menu opening/Escape and selection retention are
  automated checks, not manual OS-popup visual acceptance.
- The existing admin browser suite passed 30 route/viewport combinations per
  browser at 1160×720, 1280×720 and 1920×1080, including its disposable playback,
  confirmation, settings and maintenance fixtures.
- Installed Firefox 134 acceptance passed all ten routes at 1280×720. Each
  route observed 1.5 seconds of actual SSE updates, totaling 76 snapshots and
  829 telemetry events. Across 70 visible page controls there were no observed
  disabled/style/child-list mutations and no detached nodes. Focus and dropdown
  selection were retained wherever an enabled focus target was available.
  About had no such page control. This is a finite check, not an endurance run.
- Live checks captured no JavaScript errors or non-login API writes. All pages
  fit the document viewport without scrollbars. The diagnostic overflow scan
  still reports a small sidebar model-label overrun and hidden/accessibility
  content; this result is not a claim that every text-fit issue is resolved.
- All four original recordings were listed. The 93551339-byte FLAC returned
  HTTP 206 for bytes 0–41 with the `fLaC` signature. Native muted playback
  advanced, duration was 1800.07 seconds, and seeking reached 5.0 seconds
  without an audio error. No transcoding or original-file modification occurred.

Credential-free live results (`live-summary.json`) and adjacent `live-*.png`
screenshots are retained privately; the workstation path is omitted.

## Final installed state and boundaries

The repeat installer plan reports `noop`, no planned changes and no preflight
errors. Media, web and maintenance services are active with zero automatic
restarts. Maintenance reports available and idle. The boot ID remains
`23496b11-4d4f-4b83-82f4-731f4557478e`.

The final preservation check matches six files against the pre-upgrade baseline:
all four FLAC recordings, media configuration and web credential. The existing
saved FLAC SRT selection is retained. Native S24LE / 48 kHz / mono capture is
running, with recording and SRT stopped.

No real deletion, password replacement, reboot, shutdown or BirdNET/MediaMTX
change was performed for this fix. Step 5 endurance/fault acceptance and Step 6
release readiness remain outside this finite interaction validation.
