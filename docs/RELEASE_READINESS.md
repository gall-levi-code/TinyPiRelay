# Step 6 — release readiness

Onboarding candidate `0.5.17-dev` (2026-09-13) separately adds browser-first
account creation, no-audio installation, automatic recovery/discovery prerequisites
and fresh-device trusted-LAN HTTP access. It is distributed as the MIT-licensed
[GitHub development prerelease](https://github.com/gall-levi-code/TinyPiRelay/releases/tag/v0.5.17-dev),
not a stable/general release or a Pi deployment. See
[onboarding checks](evidence/ONBOARDING_2026-09-13.md) and
[installation](INSTALLATION.md). Fresh physical installation and actual port-80
systemd/mDNS checks remain open. HTTP first-claim
and cleartext LAN traffic are explicit operator tradeoffs, not solved security
properties. Earlier hardware and hardening evidence below retains its scope.

Publication follow-up: the owner selected MIT and authorized the repository and
GitHub Releases. The release tag identifies the committed source; release notes
provide archive and launcher SHA-256 values. Public copies were
[privacy-redacted](evidence/PUBLICATION_REDACTIONS.md) with originals retained
privately. This supplies source/artifact identity, not an independent signature,
CI infrastructure or physical acceptance. The older no-publication/provenance
statements below describe the September 11 review, not the current repository.

Review date: 2026-09-11. Reviewed hardening candidate: `0.5.15-dev`.
The subsequent local `0.5.16-dev` adds [opt-in FLAC recovery](FLAC_RECOVERY.md);
its separate finite checks do not close any hardware/endurance gate below.
It was subsequently [deployed to the Pi 4](evidence/RPI4_FLAC_RECOVERY_2026-09-11.md)
with FFmpeg and passed a real synthetic-file recovery via the web API. The
review-stage no-deployment statements and fingerprints below are historical.

## Recommendation: conditional

Suitable for continued controlled development/evaluation on the existing Pi 4
rig after a reviewed upgrade and short smoke test. **Not approved for a general
v1 release, unattended archival use, Internet exposure, or blanket SBC support.**
Step 6 software review proceeds under the operator's explicit instruction to
defer further endurance runs. That instruction does not turn missing tests into
passes. No soak, Pi deployment, power action, publication, tag, push or commit
was performed in this review. The last installed Pi release remains `0.5.14-dev`.

## Findings and disposition

| Risk | Finding | Disposition / regression |
|---|---|---|
| High — file safety | Rotation exposed a replacement FLAC while the old fragment drained; stopping could report the wrong file finalized | Fixed: track physical active/draining paths until removal, protect the transition window, and finalize the event's actual path. Runtime/library regressions |
| High — continuity | Recording filesink errors lacked the synchronous downstream-error isolation already used for SRT | Fixed: recording reuses the exact-sink valve guard. Engine callback-order/branch-plan regression; real EIO/ENOSPC continuity still unverified |
| High — file safety | A buffered filesink close error could be reported as successful finalization | Fixed: inspect the error latch after NULL/close and report failed finalization. Regression reproduced false success before the fix |
| High — destination safety | Rotation could open a new file between storage polls without a fresh destination/mount check | Fixed: reuse the exact-destination safety check before rotation; 90% and missing-mount regressions stop recording only |
| High — lifecycle | Uninstall accepted failed service deactivation and could delete code/runtime paths while services remained active | Fixed: require successful deactivation, or independently confirmed absent **and inactive** units. Both main and maintenance paths tested |
| Medium — recovery | Resuming an interrupted version transition skipped its configuration backup | Fixed: backup depends on the version transition, not the transient plan-mode name; interruption/resume regression |
| Medium — recovery | A valid leftover `.current.new` symlink prevented activation from resuming | Fixed: validate the temporary link with the same confined-release rules as `current`; real symlink regression |
| Medium — input bounds | Bootstrap read all archive metadata before enforcing its member limit | Fixed: enforce the 10,000-member bound while iterating; over-read regression |
| Medium — misleading assurance | FFmpeg could exit successfully after decoding a frame-boundary-truncated FLAC | Fixed: fallback success says **Decode only**, not Checked; completeness remains unverified. [Synthetic reproduction](evidence/STEP6_FLAC_DECODER_TAIL_2026-09-11.md) |
| Medium — availability | Descriptor/thread startup failure could leave a check permanently busy | Fixed: release descriptor and busy/check state on every startup failure; Linux fixtures |
| Medium — diagnostics | Storage safety warnings existed only in transient status | Fixed: log the safe warning code once per transition to the existing journal logger; no paths/secrets or per-poll flood |

The recording guard reuses GStreamer's documented
[valve downstream-error behavior](https://gstreamer.freedesktop.org/documentation/coreelements/valve.html).
The close-error review also inspected upstream
[filesink](https://github.com/GStreamer/gstreamer/blob/1.22.0/subprojects/gstreamer/plugins/elements/gstfilesink.c)
and [queue](https://github.com/GStreamer/gstreamer/blob/1.22.0/subprojects/gstreamer/plugins/elements/gstqueue.c)
source. No upstream source was copied; inspecting version 1.22.0 is not a live
test of the installed Pi's GStreamer version.

## Remaining release gates

| Gate / limitation | Owner and next verification |
|---|---|
| New media/lifecycle native behavior needs further validation | `0.5.16-dev` is installed with preservation and service-health checks passed; native recording/rotation/stop and write/close-fault acceptance for the hardening changes remains separate from the recovery-copy smoke test |
| Fresh real GStreamer write/close-failure behavior is unverified | Maintainer: finite disposable synthetic receiver and recording destination fault fixture; verify capture/SRT progress and failed-finalization state. Never fill or damage the user's recording filesystem |
| Installer power-loss durability is incomplete | Maintainer: `LocalHost.write_atomic` fsyncs file content but not parent-directory replacements; staged release/current metadata are not comprehensively fsynced. Design/test durable commit ordering before claiming power-cut-safe upgrades. Current process-interruption fixtures are not power-loss proof |
| A blocked filesystem call can stall the media owner | Maintainer: bound/isolate storage checks if slow/unreliable/removable destinations become supported release targets; a normal error return is covered, an indefinitely blocked kernel/filesystem operation is not |
| General audio-device discovery/hotplug acceptance incomplete | Maintainer: currently selected modes are admitted by the retained capability graph, not arbitrary plug-and-play promotion. Validate exact new device tuples and late device return |
| Endurance, multi-client/kiosk load and broad fault matrix incomplete | Explicitly deferred by operator. No new run or recurring monitor is scheduled. The existing 30-minute result remains the maximum combined-run evidence cited here |
| Broad hardware profiles and transport security interoperability incomplete | Exact tested rigs only; encryption, stream ID routing, DNS/late-network return and other receiver combinations need separate finite evidence |
| No publication/provenance infrastructure | Repository has no commits, remotes or CI. Before public release: owner reviews/commits the candidate, licenses/privacy and reproducible artifact identity, establishes a trusted checksum/signing channel and explicitly authorizes publication |

These are reported gates, not waived requirements. No other concrete high
defect was identified in the inspected authentication, protocol, filesystem,
maintenance and lifecycle paths; this is not penetration testing or a security
certification.

## Tests and evidence

| Final check | Result |
|---|---|
| Windows Python 3.12 full suite | 457 tests, 54.282 seconds, OK; 34 platform-dependent skips |
| Network-disabled Linux Python 3.12 Alpine full suite, source mounted read-only | 457 tests, 79.062 seconds, OK; two environment skips |
| Firefox 134 interaction regression | All ten routes passed; no captured errors or non-login writes; focus/drafts/native-select retention and pointer-intent guard passed |
| Chrome 153 interaction regression | Same ten-route checks passed |
| Evidence admission / offline dashboard checks | Exactly three declared representations; dashboard state checks passed |
| Synthetic FFmpeg tail test | Reproduced success on a truncated file and drove conservative fallback labeling |

The initial Windows baseline passed 442 tests. An intermediate Linux run
reported one failure while the shared source was being edited; only its tail
was retained, so the exact failure cause is not established. The subsequent
isolated lifecycle slice passed 7/7, and the final frozen-source full run above
passed without relaxing assertions or adding skips to bypass that failure.
Native GStreamer was unavailable in the local review environment: engine tests
use deterministic doubles, not a fresh real-media fault integration run.

Reproduce the full suite from the repository on Linux:

```sh
PYTHONPATH=src python3 -B -m unittest discover -s tests -p 'test_*.py'
PYTHONPATH=src python3 -B -m tinypirelay.compatibility validate
node tests/web_dashboard_checks.js
```

Windows equivalent: set `$env:PYTHONPATH='src'` and use `py -3 -B` instead of
`python3 -B`. Optional browser checks use the already-installed Playwright:
`node spikes/interaction_browser_check.cjs`, with `PLAYWRIGHT_MODULE`,
`TEST_BROWSER`, `BROWSER_EXECUTABLE` and a free `TEST_PORT` set for the local
runtime. It starts an isolated preview and rejects non-login API writes.

Logs and browser summaries are retained privately; the operator-specific
workstation path is omitted from this publication copy.
The final installer payload fingerprint (`validate_payload_tree(Path.cwd())`)
is `e6a12ec657020ce995f6afffc3ad961d8779f747a430c253ccb305e2d7263559`.
The bootstrap SHA-256, separate from the installed payload, is
`7baa41db4c3f6180f4f19859cd8f4dd93113d2537633297355c915c8641dc66a`.
These identify local content, not an authenticated published release. Final
documentation edits do not change the installer's payload fingerprint.

Historical hardware evidence is separate from this local patch set:

- Pi Zero W, 32-bit ARMv6: finite Steps 1–4 tests passed. Its later dashboard
  workload saturated the board; broad headroom/endurance acceptance is absent.
- Pi 4 B, Trixie 64-bit: [30-minute combined run](evidence/RPI4_S24_FLAC_BIRDNET_2026-09-09.md)
  with native mono S24LE/48 kHz FLAC recording, 60 Hz spectrum and converted
  S16 stereo FLAC/Matroska/SRT into BirdNET passed. Mean total CPU 23%, peak
  70.6°C, no observed throttling/decoded discontinuities. Earlier
  [admin pages](evidence/RPI4_ADMIN_PAGES_2026-09-09.md) and
  [interaction stability](evidence/RPI4_INTERACTION_STABILITY_2026-09-09.md)
  passed finite installed checks. These do not validate the new filesink valve.
- Other board/OS combinations: expectations only; see the
  [hardware matrix](HARDWARE_MATRIX.md). No CI-validated board claim is made.

## Security, dependencies and licensing

The review traced bounded scrypt work, hashed bearer sessions/expiry, CSRF,
credential replacement/revocation, HTTP framing/routes, numeric/bounded SSE,
local protocol redaction, version-bound file IDs, no-follow descriptor access,
single-link rules, active-file protection and maintenance's fixed actions plus
Linux peer identity. Shell invocations remain explicit argument arrays at
runtime. Web/media remain non-root, loopback HTTP stays behind an SSH tunnel,
and the root maintenance helper is separately restricted.

The repository carries the MIT license. Production dependencies are installed
separately from OS packages: Python/stdlib, PyGObject/GI, ALSA utilities, systemd
and GStreamer base/good/bad/ALSA/tools plus their distribution dependencies.
Those packages retain their own licenses; they are not relicensed by this
repository. FFmpeg/FLAC/VLC and Playwright/Node are optional diagnostics/checks
or development tools, not bundled production engines or a required UI build.
The later `0.5.16-dev` recovery-copy feature also uses optional OS-installed
FFmpeg at runtime; normal capture, recording and streaming do not require it.

The inspected source tree contains no vendored native binaries, downloaded
fonts or runtime CDN assets. UI fonts use the system font stack; inline SVG
marks the TinyPiRelay logo as original artwork. No BELABOX source transplant
was identified. With no Git provenance, this inspection cannot establish every
asset's historical origin or grant trademark clearance. Public packaging must
retain LICENSE and review any later third-party addition separately; installed
package copyright notices live under `/usr/share/doc/<package>/copyright`.

Pattern scans found no private-key blocks, known deployment password or common
API-token patterns in the candidate tree; password-shaped matches inspected
were synthetic fixtures. This is a bounded scan, not proof of secret absence.
Evidence/config documents retain private host/path metadata and need a privacy
review before public distribution. No dependency-version vulnerability audit
or Internet/TLS deployment certification is claimed.

## Recovery and known feature limits

Use [the installation guide](INSTALLATION.md) for exact plan/apply, password
reset, rollback and preservation-uninstall commands. Start diagnosis with
`tinypirelay status`, `sudo tinypirelay diagnostics`, and the two service
journals. Resume an interrupted upgrade using the **same verified artifact**;
retain its journal and backups. Resolve stop failures before retrying removal.
Do not hand-edit the current-release pointer or uninstall state.

After an interrupted recording, keep the original, download it for inspection,
and distinguish a decode-only result from a validator result; neither proves
the full original capture exists. Recovery is never automatic. The later
`0.5.16-dev` adds a confirmed, original-preserving
[recovery-copy action](FLAC_RECOVERY.md), requiring optional FFmpeg.
No automatic recording deletion occurs. External mounts require
an explicit mount guard and correct ownership. The 90% safety stop uses the
recording destination filesystem, not an unrelated root filesystem.

Phone layout, automatic board-profile tuning, UPS battery telemetry, Wi-Fi
configuration, full system-journal browsing, a production receiving bridge,
MPEG-TS and guaranteed kiosk/60-FPS rendering remain outside current acceptance.
The desktop layout and the permission to review Step 6 before endurance are
recorded [operator amendments](PROJECT_SPEC.md#recorded-operator-amendments-2026-09-11).
