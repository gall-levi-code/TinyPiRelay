# TinyPiRelay Project State

Updated: 2026-09-13 UTC

> Publication continuation: source and the `v0.5.17-dev` development prerelease
> use [gall-levi-code/TinyPiRelay](https://github.com/gall-levi-code/TinyPiRelay)
> under MIT, as confirmed by the owner. The initial repository commit is retained.
> Public evidence copies omit private endpoints/operator paths; originals remain
> outside the repository. See [redactions](evidence/PUBLICATION_REDACTIONS.md).
> Release artifacts are built from the committed source with LF text checkout;
> checksum-linked evidence bytes are preserved. Version-pinned assets and their
> final digests are listed in the GitHub release notes. This publication does not
> deploy to the Pi or close the fresh-device, Firefox, endurance or general-release
> gates below. No signing key or CI/hardware acceptance is implied.
> The reviewed public checkout passed 501 tests on Windows (87.412 s, 52 skips)
> and isolated network-disabled Linux (105.570 s, four skips), plus offline
> dashboard checks. Publication changes no runtime code; evidence redactions
> and source line endings are explicitly accounted for.

> Previous implementation continuation: local `0.5.17-dev` revamps onboarding: fresh installation
> automatically supplies a disabled configuration and prerequisites, permits no
> microphone/TTY, serves trusted-LAN IPv4 HTTP port 80, and presents first-time
> username/password creation in the GUI. After creation only normal login is
> available. Accounts/settings are preserved and legacy localhost access does
> not change without `--web-access lan`. Device discovery refresh does not expand
> the evidence-gated mode catalogue. A stdlib release builder produces bounded,
> deterministic pinned artifacts but publishes nothing. See [installation](INSTALLATION.md)
> and [finite checks](evidence/ONBOARDING_2026-09-13.md). Final Windows/Linux suites
> each ran 501 tests without failures (52/four skips); Chromium first-run/login,
> idle-audio refresh/draft preservation and null-config no-op checks passed.
> Firefox browser verification remains blocked by local test-tool mismatch.
> No Pi deployment,
> reimage, package installation, account reset, endurance run or public release
> was performed. The working Pi remains installed at `0.5.16-dev`. HTTP traffic
> and first-claim LAN access remain explicit risks; no TLS is provisioned.

> Previous continuation: `0.5.16-dev` is installed on the Pi 4 with distribution
> FFmpeg 7.1.5. The 15 recovery tests passed on target, followed by a real
> authenticated HTTP recovery through the installed media service: a damaged
> synthetic 24-bit FLAC produced a validated separate copy with exact surviving
> PCM, while preserving its source. Both test FLACs were archived outside the
> library. All four user recordings, config and credential fingerprints still
> match; capture is running, recording/SRT stopped, and both services healthy
> with zero automatic restarts. Repeat plan is `noop`. No endurance, reboot,
> user-recording recovery or public release was performed. See
> [deployment evidence](evidence/RPI4_FLAC_RECOVERY_2026-09-11.md).

> Previous continuation: local `0.5.16-dev` connects Storage's recovery button to
> an optional FFmpeg salvage-and-validate job. A validated, separately named FLAC
> copy is published without changing the original. Existing media-owned file
> access, single-job control, CSRF, storage limits and stable UI controls are
> reused. Recovery is limited to five minutes and 1 GiB of output; missing audio
> cannot be reconstructed. Synthetic real-FFmpeg tests and Firefox/Chromium UI
> checks passed; final Windows/Linux suites each ran 472 tests successfully
> (49/four environment skips). No Pi deployment/package install, real-recording recovery,
> endurance run or release publication was performed. Pi remains `0.5.14-dev`;
> release readiness remains conditional. See [FLAC recovery](FLAC_RECOVERY.md).

> Previous continuation: Step 6 hardening review completed with a **conditional**
> recommendation for controlled development/evaluation, not a general v1 release.
> Local candidate `0.5.15-dev` fixes rotation/file protection, recording write and
> close-error handling, rotation destination checks, interrupted-upgrade backup
> and pointer recovery, uninstall stop-failure handling, archive bounds, and
> file-check state/assurance. Windows and isolated Linux suites each passed 457
> tests (34 and two skips); Firefox 134 and Chrome 153 interaction checks passed.
> The Pi was not changed: last installed release remains `0.5.14-dev`.
> The user explicitly deferred further endurance runs and authorized Step 6
> review. No soak or recurring monitor was scheduled, and no release was
> published/tagged/pushed. Step 5 remains incomplete. Exact gates, recovery,
> provenance and reproducible checks: [release readiness](RELEASE_READINESS.md).

> Previous continuation: `0.5.14-dev` is installed on the Pi 4. Live updates now
> leave unchanged controls and editable drafts intact. Shared change-only
> updates, interaction-safe text fitting and stable Storage nodes replace the
> repeated control resets. Firefox/Chrome regressions passed; an installed
> Firefox check observed 76 actual snapshots across all ten pages with no
> control mutations, detachment, focus loss or dropdown selection changes.
> Both full Python suites passed 442 tests. The final preservation check matched
> all four recordings, configuration and credential; capture remains S24LE /
> 48 kHz / mono with recording and SRT stopped. The repeat installer plan is
> `noop`. See the [interaction report](evidence/RPI4_INTERACTION_STABILITY_2026-09-09.md).
> Step 5 remains in progress; this does not establish Step 6 readiness.

> Previous continuation: `0.5.13-dev` was installed on the Pi 4. Storage now has
> a real FLAC catalog, native playback/seeking/download, confirmed deletion,
> and bounded background integrity checks. System/Network, browser appearance
> preferences, maintenance/account controls, and Logs/About are connected.
> The final Windows and Pi suites each passed 442 tests (30 and two skips).
> The media-only helper restart and real decoder check passed without changing
> recordings or credentials. A concurrent saved FLAC SRT-format selection was
> preserved; native S24LE / 48 kHz / mono capture remains active and both output
> branches remain stopped. Scope and final browser evidence are recorded in the
> [admin-page report](evidence/RPI4_ADMIN_PAGES_2026-09-09.md). The separate mockup
> and media graph remain unchanged; Step 5 is still in progress, not Step 6 ready.

> Previous continuation: `0.5.10-dev` was installed on the Pi 4. Native iMM-6C
> S24LE / 48 kHz / mono capture feeds lossless local FLAC and the newly tested
> conversion to S16 stereo FLAC / Matroska / SRT. No media graph code changed.
> A 120-second installed combined shakedown and 180-second receiver check passed.
> The user's 30-minute recording + 60 Hz spectrum + BirdNET test **passed**:
> 23% mean total CPU, 70.6 C peak, no throttling or decoded discontinuities;
> the 24-bit recording decoded through EOS. BirdNET logged 183 detections across
> 36 labels within the main playback-test window. Existing files/settings were
> preserved and the temporary path was disabled/stopped; native 24-bit capture
> remains active with recording/SRT stopped. See the
> [combined test results](evidence/RPI4_S24_FLAC_BIRDNET_2026-09-09.md).
> Historical release identities and renderer evidence below remain unchanged;
> Step 5 is still in progress and Step 6 is not ready.

> Previous continuation: `0.5.9-dev` was installed on the returned Raspberry Pi 4
> Model B Rev 1.4, using the iMM-6C's validated mono S16LE / 48 kHz mode. The
> browser now uses a linear 30-second bitmap conveyor, at most 1801 raw
> arrival-time samples, and display-only frequency bounds/axis zoom/pan/reset.
> The time warp and age-dependent peak rollups are removed. The Windows suite
> ran 413 tests with no failures and 19 expected skips; offline regressions and
> synthetic real-browser checks in Firefox 134 and Chromium 133 passed.
> A finite 32-second Firefox 134 check passed against the installed `0.5.9-dev`
> and actual 48 kHz mono microphone feed, with visually continuous full-range
> and 4-12 kHz views, no captured errors, and no non-login API writes. Results
> are recorded separately in the
> [linear dashboard report](evidence/RPI4_LINEAR_DASHBOARD_2026-09-08.md).
> Historical `0.5.7-dev` produced meter/spectrum sequences at 59.99 Hz over
> 150 seconds with 23.65% mean system CPU, bounded queues, and no restarts or
> throttling. Its 120-second warped history filled in the tested browser;
> `0.5.8-dev` later passed a tall-canvas striping correction check. Those checks
> did not resolve the user's subsequent Firefox rendering failure or measure
> browser frame time. See the retained
> [warped-dashboard report](evidence/RPI4_WARPED_DASHBOARD_2026-09-08.md).
> Neither the old results nor the new synthetic checks establish SRT,
> multi-client, kiosk, endurance, rotation, or fault acceptance.

## Milestone status and diff identity

- Step 1 — Architecture and Technical Spike: complete for its finite evidence
  scope. Exactly three sender representations are approved: PCM S16LE/48
  kHz/stereo, FLAC/48 kHz/stereo, and Opus 128 kbit/s/48 kHz/stereo, all in
  streamable Matroska over SRT caller mode.
- Step 2 — Media Engine: complete for its finite scope. Strict configuration,
  serialized control, programmable PyGObject/GStreamer ownership, recording,
  reconnect, monitoring, and ordered shutdown are implemented.
- Step 3 — Web GUI, Authentication, and Control Plane: complete for its finite
  implementation/evidence scope. The independent standard-library web process,
  authentication/session/CSRF/throttling controls, exact capability graph,
  atomic configuration API, bounded AF_UNIX protocol, numeric SSE telemetry,
  and no-build accessible UI are implemented.
- Step 4 — Installer, Services, and Lifecycle: **complete for its finite
  acceptance scope**. The staged installer, dedicated identity/filesystem
  policy, hardened systemd units, installed operator CLI, and pinned HTTPS
  bootstrap mechanism have isolated POSIX fixture coverage. Fresh install,
  repeat no-op, actual least-privilege/systemd boundaries, iMM-6C capture,
  authenticated loopback dashboard, web-only restart isolation, real FLAC
  finalization/decode, and reboot recovery passed on a physical Zero W.
- Step 5 — Hardware Profiling and Tuning: **in progress**. The current candidate
  adds generation-scoped bounded queue metrics to control snapshots,
  a bounded Pi profiler, a disposable FFmpeg SRT decode-continuity receiver,
  and a reproducible [physical profiling procedure](HARDWARE_PROFILING.md).
  The finite Pi 4 60 Hz monitoring row passed. Complete stream/recording
  profiles, endurance/fault rows, and tuning decisions remain pending. The user
  deferred further endurance runs on 2026-09-11; no missing row is marked passed.
- Step 6 — Hardening and Release Review: **review complete, conditional**.
  Source fixes and finite local regressions are complete for this review pass.
  No general release approval: power-loss durability, blocking filesystem
  calls, native validation of the new media changes, broad hardware profiles
  and publication/provenance gates remain reported in RELEASE_READINESS.md.
- Working identity: unborn `master` with no commits or remotes. The complete
  tree remains uncommitted/untracked, so no commit hash or tracked diff exists.
- Historical `0.5.16-dev` identity (still installed on the Pi; local candidate
  is now `0.5.17-dev`):
  Local/extracted installer payload fingerprint is
  `1bd21d14e192af1dfddc3a0685a83dc2635f222d29293d911ac80a7b45470ad3`;
  bootstrap SHA-256 is
  `7baa41db4c3f6180f4f19859cd8f4dd93113d2537633297355c915c8641dc66a`.
  Installed archive SHA-256 is
  `c7068fa432101032595f47d5ba0a9351a3b5590a886a2e4d7ba3a8feba344253`
  (602308 bytes). No commit or published artifact was created. Final documentation
  edits are outside the installer payload identity; see the deployment report.
- Historical `0.5.14-dev` identity: the 573212-byte source/test/documentation archive has SHA-256
  `886c87151d9a79f2ee2c44047ec0fdfa8b4efec1d6b526d54272ec095dd9d572`.
  The live-browser test extension and final evidence/bookkeeping followed
  archiving; runtime code did not change. See the
  [interaction report](evidence/RPI4_INTERACTION_STABILITY_2026-09-09.md).
- Historical `0.5.13-dev` identity: the 567721-byte archive has SHA-256
  `84853313cb3ffe390ffc7e1f56a6ab57ee46c57eeda5c8f62dd46173a95d9dcf`.
  Final evidence and project-state bookkeeping followed archiving. See the
  [admin-page report](evidence/RPI4_ADMIN_PAGES_2026-09-09.md).
- Historical `0.5.9-dev` identity: the 177060-byte runtime archive has SHA-256
  `6fcff04f0e5119664c1fc3596bb0e03a40751dce3146d1fac65d69191998eafe`.
  Configuration and credential hashes were preserved during installation;
  live validation is recorded separately in the
  [linear dashboard report](evidence/RPI4_LINEAR_DASHBOARD_2026-09-08.md).
- Historical `0.5.8-dev` identity: the 174349-byte runtime archive has SHA-256
  `6cdb57806166209881c11a447c718eeb6e6afd1eec3c384a98fff784e4a59cf7`.
- Retained Step 3 artifacts: simulated control-plane SHA-256
  `aa066e04494e5fa016789a4ccdf721e8668a0d7ad74845facafa754a0b4e0be1`;
  physical iMM-6C/web-isolation SHA-256
  `68d0d426150aa3461a12720dc54c51245d55d851e3178f60971dd3c7d84f7997`;
  physical spectrum-reattachment SHA-256
  `6366e0e044594cf5762f3bc8b56b52aefb4bc3236dbe1aae35bc490315d02ca2`.
- Original Pi Step 3 bundle: SHA-256
  `12d81834309cecaaa9d429e3ebbcb436625048a8654164f3716a1b354359d50d`,
  92 members and 269041 bytes, excluding `.git`, `__pycache__`, and `*.pyc`.
  It predates the final spectrum and assessor patches.
- Final Step 3 target-tested source bundle, with documentation complete except the
  final identity/readiness prose here and in the matching physical report:
  SHA-256 `407c5d4c524d7d315d05de2e279f426c22a612f1a4f39a9a9cb784ba22ec0bed`,
  96 members and 282826 bytes, excluding `.git`, `__pycache__`, and `*.pyc`;
  Pi Python 3.13.5 passed all 269 tests in 80.432 seconds (89.832 seconds
  wall) with one expected missing-Node skip, compatibility validation returned
  exactly three representations, the probe exited 0 with status `present` and
  zero actions, and the immutable physical artifact reassessed 7/7 pass.
  After target validation, only this identity/readiness prose and the matching
  physical-report prose changed; no source, configuration, test, or retained
  machine-evidence payload changed.
- Step 4 target artifact: archive SHA-256
  `9505ac00966b0015289d0f214cf824e280ee24371083a892a608fa424b044b10`,
  119 members and 347804 bytes; extracted payload SHA-256
  `287ccd1436e112f012dd1610cab9f8a81da7d18c336b6804e7f045a8f02cc339`.
  The installed configuration SHA-256 was
  `679ea6737e406abbda976fcff381db38d0c3da8e1ff1a5533600419c0de0ff96`.
  Final evidence prose and the package status docstring were updated after the
  run; runtime logic, configuration, and tests did not change.
- Final Step 4 documented target-tested source bundle, with documentation
  complete except the final identity/readiness prose here and in the matching
  physical report: SHA-256
  `9cc1833f2fe7bbb6ea42e5f0b676c15c5cb02308126fe2e3b52c3ba79c2bb0d0`,
  121 members and 361337 bytes, excluding `.git`, `__pycache__`, and `*.pyc`.
  Pi Python 3.13.5 ran all 333 tests successfully in 237.246 seconds (252.22
  seconds shell wall), with one expected missing-Node skip. After target
  validation, only this identity/readiness prose and the matching report row
  changed; source, configuration, tests, and retained machine evidence did not.
- Retained Step 4 results: [physical report](evidence/RPI_ZERO_W_STEP4_2026-08-20.md),
  SHA-256 `0e64c8ba8d4e2487b3ac47a3643f5cff5ebc02161f17eb6b02c585b9cb6036e8`,
  and [machine evidence](../evidence/step4_install_rpi_zero_w_imm6c_2026-08-20.json),
  SHA-256 `78422d82fcef15453c6cc766a3a3552302d61aed8c3720a0251e2d3641ccf609`.
- Scope not implemented or accepted: Step 5 physical hardware profiles,
  endurance/fault acceptance, board-profile tuning decisions, late
  audio/network recovery, active-SRT acceptance through the installed service,
  production receiver-bridge integration, and general-release acceptance.

## Historical Step 1–5 detail

The sections below retain the earlier implementation/hardware handoff record
through the `0.5.9-dev` era. Statements about then-current candidates, pending
Pi 4/64-bit work, or Step 6 entry prerequisites are historical, superseded by
the dated continuation and release-readiness report above. They do not revoke
later finite evidence or the operator's Step 6 review authorization.

## Decisions and deviations

- Media and web remain separate processes. The media owner alone owns the GLib
  context, GStreamer graph, state, configuration authority, and branch
  lifecycle; web failure does not issue media stop commands.
- Version 1 of the local protocol is one strict newline-delimited JSON request
  and response per AF_UNIX connection. It is bounded to eight clients, 64 KiB,
  and a 15-second timeout. The Step 4 implementation assigns the socket
  directory/group ownership without changing the protocol; its finite
  ownership and cross-user boundary checks passed on the target board.
- The web service uses standard-library HTTP and `hashlib.scrypt`: no default
  credential, slow constant-work verification, two derivations at once,
  nested bounded login throttles, at most 128 hashed-token sessions, strict
  same-site cookies, CSRF on every mutation, fixed routes, strict framing, and
  recursive secret redaction.
- The API/UI consume an exact non-Cartesian hierarchy of device -> capture mode
  -> stream option. The capability representation set must equal the Step 1
  compatibility gate; impossible combinations fail closed.
- Configuration uses bounded no-symlink reads, restrictive secret-file modes,
  SHA-256 revision conflicts, same-directory mode-`0600` atomic writes, and
  explicit live-versus-restart classification. Step 3 does not pretend to own
  a service-manager restart.
- SSE uses full latest-state snapshots at approximately 5 Hz plus lightweight
  latest-only meter/spectrum events at the configured 5-60 Hz cadence, with
  four clients at most. The browser retains at most 1801 raw arrival-time
  samples for a linear 30-second view, including at most one needed boundary
  predecessor. Its bitmap conveyor paints only newly exposed strips; view
  changes replay the bounded history through the shared animation-frame gate.
  Frequency bounds, axis wheel zoom/drag pan, and reset are local display
  preferences, not media or configuration writes. Level stays attached with
  capture. Spectrum is one shared
  disposable branch under a fixed five-second media lease renewed every two
  seconds while clients exist.
- The Step 5 candidate exposes current-generation bounded queue metrics for the
  stream, recording, level, and spectrum branches. Inactive or stale branch
  values are removed rather than presented as current measurements.
- Physical reattachment exposed unsafe immediate teardown of a live spectrum
  tee branch. Spectrum now uses owner-context IDLE unlink/removal, and bus
  telemetry is accepted only from the current active branch. Three physical
  release/expiry cycles then passed without changing primary generations.
- No product-spec deviation is known. Step 3 did not add MPEG-TS, SMPTE ST
  302M, `gstreamer1.0-libav`, listener/rendezvous mode, TLS, or network
  management. The same three Matroska representations remain authoritative.
- Step 4 packages the Step 2/3 behavior unchanged. Its split service identities,
  private config/credential directories, group-owned local socket, loopback web
  listener, and staged release were exercised in the finite target run.
  Preservation and destructive/version-transition lifecycle paths remain
  isolated-fixture claims. The receiving appliance and workstation bridge
  remain deferred until after Step 6.
- The Step 5 profiler and FFmpeg receiver are bounded evidence tools, not new
  appliance services or a production receiving bridge. They retain no decoded
  audio and do not expand the three approved Matroska-over-SRT representations.

## Commands/checks and results

- Current `0.5.9-dev` on Windows — 413 tests ran with no failures and 19
  expected platform-specific skips using `PYTHONPATH=src`. All 19 focused web
  asset tests passed. Offline bitmap/interaction checks cover linear time,
  retained-history bounds, jitter/outage/stall behavior, fractional DPR,
  restart clearing, frequency validation/gestures, and coalesced repainting.
  Synthetic real-browser checks passed in Firefox 134 and Chromium 133.
  A separate live Firefox 134 check against the installed Pi 4 release passed
  over 32005 ms at 1920 × 1080/DPR 1.25: 1921 producer-sequence increments,
  1905 render calls, and 1694 retained raw samples covering 30006 ms including
  the boundary predecessor. Render-function median/p95/maximum was 2/4/6 ms,
  with zero captured errors and zero non-login API writes. Full-range and
  4-12 kHz views were visually continuous. These are finite execution-time and
  visual checks, not end-to-end FPS, kiosk, or endurance acceptance.
- Historical `0.5.8-dev` — offline rendered-span regressions and 18 focused web
  asset tests passed. Live 1920x1440 browser QA showed continuous strips with
  no captured warnings/errors. Config/credential hashes were preserved across
  upgrade; both services stayed active with zero restarts, throttle flags `0x0`.
- Prior local `0.5.7-dev` candidate on Windows — 412 tests ran successfully
  with 19 expected platform-specific skips; offline dashboard state checks and
  visual simulated-preview QA passed. Offline dashboard checks passed again
  before the 2026-09-08 Pi upgrade.
- Historical installed `0.5.7-dev` finite Pi 4/iMM-6C row — 150 seconds, one dashboard,
  59.99 Hz media meter/spectrum sequences, 23.65% mean total system CPU,
  57.94 C maximum, bounded queues, no runtime errors/restarts/throttling.
  The browser filled the full 120-second history without document overflow.
- Historical installed `0.5.6-dev` finite Pi 4 row — one authenticated dashboard delivered
  60.0 Hz meter and spectrum sequences with 512 bands over 20 seconds; total
  system CPU was 22.84%, both services stayed active with zero restarts, queues
  stayed bounded, temperature was 62.809 C, and throttling was `0x0`.
- Historical `0.5.0-dev` Step 5 candidate on Windows —
  `py -3 -B -m unittest discover -s tests -p 'test_*.py' -v`: 378 tests ran
  successfully with 18 expected platform-specific skips.
- That same historical candidate ran 378 tests successfully in a network-disabled
  Alpine POSIX container, with one expected missing-Node skip. These candidate
  checks validate software behavior only; no Step 5 Pi result is implied.
- Final Step 4 source on Windows —
  `py -3 -B -m unittest discover -s tests -p 'test_*.py' -v`: 333 tests ran
  successfully in 33.791 seconds; 18 were expected platform-specific skips.
- The same final source ran 333 tests successfully in a network-disabled
  Alpine POSIX container in 60.821 seconds; one was the expected missing-Node
  skip. This is software evidence, not a board claim.
- The exact final Step 4 bundle ran 333 tests successfully on Pi Python 3.13.5
  in 187.106 seconds; one was the expected missing-Node skip.
- Original Pi bundle —
  `PYTHONPATH=src python3 -B -m unittest discover -s tests -p 'test_*.py' -v`:
  263 tests passed with one expected Node.js skip in 80.394 seconds (90 seconds
  shell wall). Compatibility validation returned exactly three
  representations; the read-only probe exited 0.
- Simulated Step 3 harness —
  `PYTHONPATH=src python3 -B spikes/step3_web_integration.py --evidence-kind simulated-control-plane --slow-client-seconds 12 --output evidence/step3_web_simulated_rpi_zero_w_2026-08-19.json`:
  7/7 checks passed using a real web subprocess and AF_UNIX control server but
  simulated media/fault states.
- Physical Step 3 harness — attached to the production media service with
  `--evidence-kind physical-hardware --manage-media`: the current assessor
  passes 7/7 checks. The immutable JSON embeds one obsolete failing result from
  an earlier control-gate predicate; current assessment intentionally preserves
  the raw artifact and is reproducible in
  [the Step 3 report](evidence/RPI_ZERO_W_STEP3_2026-08-19.md).
- Physical web-failure slice: real web `SIGKILL`/restart with capture, logical
  stream, recording, and their generations unchanged; sender SRT bytes advanced,
  level remained present, and spectrum disappeared after its crash lease.
- Physical slow-SSE slice: two unread clients for 12.088 seconds, overload
  `503`, 12 connection-churn attempts, recovered slot, bounded numeric data,
  RSS deltas +159744/+176128 bytes during/after, and file-descriptor deltas
  +4/+2 during/after.
- Physical FLAC: 933821 bytes, 36.29 seconds, mono 48 kHz, SHA-256
  `78284bf8a24120bb34bf457b976db7812db0c9f29d9fc2affbc74bf63c388b17`;
  finalized and decoded to EOS.
- Physical spectrum reattachment: three lease cycles (release, expiry,
  release) passed; spectrum sequences 1/6/34, primary generations stable, and
  SRT bytes advanced from 1497708 to 1660933.
- Browser QA in a simulated POSIX container passed login, dashboard,
  capability, telemetry-canvas, unavailable/recovery, mobile 390x844 and
  320x568, contrast/focus, and reduced-motion checks with no console errors.
  A full automated Tab-order traversal was not completed.
- Isolated Step 4 POSIX fixtures exercise read-only planning, idempotence,
  interruption/resume, dependency and service failures, permissions, credential
  handling, upgrade/rollback, preservation uninstall, and explicit purge
  controls without using those potentially disruptive cases on the Raspberry
  Pi.
- Physical Step 4 acceptance — the retained
  [report](evidence/RPI_ZERO_W_STEP4_2026-08-20.md) records fresh installation,
  repeat no-op, least-privilege/systemd checks, installed iMM-6C capture,
  authenticated loopback dashboard access, web-only restart isolation, a real
  FLAC finalized and decoded to end-of-stream, and reboot auto-recovery with
  private state and recording hashes preserved.

## Hardware vs simulated/CI evidence

- **Candidate software evidence, not hardware:** the Step 5 queue
  instrumentation, profiler, receiver assessor, and artifact contracts passed
  the Windows and network-disabled POSIX suites. The procedure is documented,
  but no Step 5 target run has yet established performance headroom, endurance,
  fault recovery, or a board profile.
- **Hardware tested**, narrowly: the production media/control components ran
  on a physical Zero W Rev 1.1 (`armv6l`, 32-bit `armhf`, Trixie, kernel
  `6.18.34+rpt-rpi-v6`, GStreamer 1.26.2) with the iMM-6C at native mono
  S16LE/48 kHz. The exact finite web-kill, bounded slow-client, FLAC
  finalization, spectrum reattachment, and Step 4 installed-appliance rows
  passed.
- A live FFplay 8.0.1 listener on the workstation offloaded decode. Its output
  was not retained or hashed. The artifact proves sender connection/byte
  progress only during the strict web-failure slice, not receiver decode
  continuity, packet loss, or new codec compatibility.
- **Simulated-control-plane**, not hardware: device-loss, SRT retry-wait, and
  storage-hard-stop UI/API/SSE state visibility, plus browser QA. The physical
  Pi separately exercised temporary control-proxy unavailable/recovery. Real
  disk/device/network fault injection was not performed.
- **Hardware tested**, narrowly for Step 4: fresh and no-op installation,
  Linux identity/ownership/socket boundaries, systemd properties and sandbox,
  iMM-6C capture, authenticated loopback dashboard, web-only restart isolation,
  real FLAC finalization/decode, and reboot auto-recovery.
- **Isolated system-integration fixtures**, not hardware: interruption/resume,
  dependency and service failures, upgrade/reverse-version rollback,
  preservation uninstall, and explicit purge.
- The overall Zero W remains **Likely compatible / untested**. No CI exists, so
  no **CI validated** board claim is made. Alpine/Windows checks validate
  software behavior only; other boards, Bookworm, and 64-bit systems are
  untested.

## Known failures, risks, and unverified assumptions

- A separate same-Pi sender/receiver diagnostic hit exact `queue_overrun` after
  50.953 seconds. Step 2 had already observed 7.560 seconds queued against the
  10-second stream bound. This is a material Zero W performance/headroom risk
  for Step 5, not proof that the Step 3 web process caused media loss.
- Later in the 91-second physical Step 3 run, SRT stopped advancing about 36.84
  seconds after recording started. Cause was not proven; FFplay lifecycle is a
  confounder, and reconnect count eventually reached 10 after the listener
  exited. Do not claim full-run continuity, slow-client noninterference,
  endurance, or real-time headroom from this run.
- Physical capture-device unplug/return, real disk-full/read-only/unmount,
  network interruption/late return, active SRT through the installed service,
  CPU load, sustained thermal behavior, packet impairment, and long-duration
  memory/FD/log growth remain open. Reboot and normal automatic service/capture
  recovery passed, but this does not prove late-resource recovery.
- Stream ID and encryption/passphrase interoperability remain unverified. The
  live FFplay listener does not add a compatibility row; FRAME ingest at
  `192.0.2.30:4001` (publication-redacted endpoint) remains unverified.
- Least-privilege identities, systemd sandboxing, and web-only restart isolation
  have finite target-board evidence. Credential reset, interruption recovery,
  upgrade/rollback, uninstall, and purge remain fixture-only. Internet exposure
  remains unsupported without a reviewed TLS deployment.

## Exact Step 4 status and Step 5 candidate state

Step 3's finite implementation and retained evidence remain complete. Step 4 is
**complete for its finite acceptance scope**, with isolated fixtures and the
retained physical Zero W report linked above. Step 5 is **in progress** at local
candidate `0.5.9-dev`: instrumentation and bounded evidence tooling are
implemented. The prior installed Pi 4 60 Hz monitoring row passed with the
iMM-6C; the current release replaces its warped renderer with the linear
30-second bitmap conveyor and local frequency controls. Host tests and
synthetic Firefox/Chromium checks and a finite 32-second live Firefox 134 check
passed for `0.5.9-dev`. Full streaming/recording profiles, kiosk validation, endurance, fault
work, and tuning decisions remain pending. Step 6 is not ready.

Step 4 repository deliverables now available:

1. a read-only preflight and explicit staged install/upgrade/uninstall plan;
2. separate media/web identities, private state, hardened systemd units, and a
   group-owned bounded AF_UNIX socket;
3. interactive `/dev/tty` credential handling plus a bounded inherited-FD path;
4. installed status, diagnostics, config validation, password-reset, and
   explicit privileged restart commands; and
5. a constrained version/checksum-pinned HTTPS bootstrap mechanism, isolated
   lifecycle fixtures, the
   [installation guide](INSTALLATION.md), and
   [ADR 0004](adr/0004-installer-system-integration.md).

Step 5 candidate deliverables now available:

1. current-generation bounded queue visibility in media status snapshots;
2. a bounded, checkpointed
   [Pi hardware profiler](../spikes/step5_hardware_profile.py);
3. a disposable, no-audio-retention
   [FFmpeg SRT receiver](../spikes/step5_ffmpeg_receiver.py); and
4. the [physical profiling procedure](HARDWARE_PROFILING.md), including
   scenario bindings, abort thresholds, evidence pairing, and explicit
   nonclaims.

The physical run closes only the listed installation rows. Interruption,
upgrade/reverse-version rollback, preservation uninstall, and explicit purge
remain isolated-fixture evidence, while late audio/network and active SRT remain
untested in the installed service. Do not reinterpret Step 4 as endurance,
performance-headroom, or blanket Zero W compatibility evidence; those remain
open Step 5 concerns. The new software tooling does not close them without a
retained physical run.
