# Onboarding revamp — finite local checks

Publication copy: operator-specific paths are omitted; the original report is
retained privately. See [redaction notes](PUBLICATION_REDACTIONS.md).

Date: 2026-09-13. Local candidate: `0.5.17-dev`. The Pi was not modified and
remains at `0.5.16-dev`. No public release, upload, reimage or endurance run.

This record describes the local implementation checks. Subsequent publication
uses the [MIT-licensed GitHub development prerelease](https://github.com/gall-levi-code/TinyPiRelay/releases/tag/v0.5.17-dev).
Its public bundle is rebuilt from committed, privacy-reviewed source; the local
handoff archive/hash below is historical and is **not** the uploaded asset.
Publication does not add fresh-device acceptance.

## Scope

- Fresh installation defaults to paired-null audio and disabled output, with
  no prepared configuration, microphone or terminal credential prompt.
- Core dependencies include distribution FFmpeg and Avahi. Applying shell
  launchers bootstrap missing tools and refresh APT metadata on supported OSes;
  read-only plans and uninstall do not perform those package operations.
- Fresh setup marker enables account creation, followed by normal login.
  No shared password or claim code. Existing/malformed accounts do not reopen
  registration; exclusive credential publication protects concurrent creation.
- Fresh trusted-LAN IPv4 HTTP port 80 uses a non-root service and narrowly scoped
  bind capability, systemd and application peer restrictions, and Host/Origin
  checks. Existing localhost access is preserved unless explicitly migrated.
- Idle media/control remains available. Audio refresh preserves unsaved drafts;
  detected hardware remains distinct from evidence-approved modes/conversions.
- Deterministic local release packaging produces the bootstrap-compatible
  `tinypirelay-VERSION/` root, checksum and optional pinned HTTPS launcher.
  Existing output is not overwritten. Nothing is uploaded or signed.

## Checks

Focused checks passed: 42 installer cases on Linux, 20 probe tests, 9 packaging
tests, 187 related media cases on Windows (7 environment skips), 11 real-HTTP
onboarding cases, 19 asset tests, CLI reporting and offline Node dashboard checks.
Eight POSIX bootstrap tests exercise default argument forwarding, package-mutation
guards, HTTPS rejection, archive/member limits and tool-path isolation.

The first combined Windows run passed 496 cases (49 skips). The first Linux
combined run exposed an obsolete Step 4 fixture expecting a TTY, plus source
changes during packaging. The fixture now explicitly exercises the new headless
contract as schema 2, preserves the competing-configuration refusal check, and
passes its eight cases. Historical schema-1 evidence was not edited or silently
promoted to the new contract. Release packaging deliberately refuses a changing
runtime source tree.

Final combined regressions: **501 tests, zero failures** on both hosts. Windows
Python 3.12.6 completed in 68.158 s with 52 environment-dependent skips; the
network-disabled Python 3.12 Alpine container completed in 98.160 s with four
skips. The Linux workspace was mounted read-only. A final packaging-only addition
includes the spike fixtures needed by the shipped tests; focused release-builder
checks were rerun after that change. No later runtime change followed these runs.

## Browser execution

Real Chromium against a disposable loopback service with real authentication
and explicitly synthetic, unconfigured media passed:

- first-visit account creation, password field clearing, normal sign-in, reload
  persistence and authenticated entry;
- required Audio refresh request, retaining non-null device/mode drafts and an
  unsaved monitoring-rate edit;
- switching back to audio-later and submitting an unchanged paired-null config;
- 1160×720 and 1920×1080 layouts without document/card/text overflow or JS errors.

The fixture only accepts the exact unchanged config no-op. No real microphone,
recording, Pi configuration or user account was used. Screenshots were visually
inspected and retained privately; the workstation path is omitted.

Reproducible harness: `spikes/onboarding_browser_check.cjs`, with the bundled
Playwright module selected by `PLAYWRIGHT_MODULE`, Python by `TEST_PYTHON`,
`TEST_BROWSER=chromium`, and the installed Chromium binary via
`BROWSER_EXECUTABLE`. It launches `spikes/onboarding_preview.py` on an ephemeral
loopback port. Firefox was not validated: the available browser build mismatches
the bundled Playwright protocol. This is a tooling limitation, not a passing
Firefox result or evidence of an application failure.

## Remaining gates and deliberate boundaries

Local handoff archive: `dist/onboarding-0.5.17-dev/tinypirelay-0.5.17-dev.tar.gz`,
603123 bytes, SHA-256
`645c72af53011544601fd299c2312424f421f97b4fe837a7db65a7e6e5a5f799`.
Its checksum and both release-builder tests passed again after extraction in an
isolated Linux container. This artifact predates only this final documentation
bookkeeping; runtime/packaging code is unchanged. It is not a published release.

No actual fresh-Pi APT installation, systemd port-80 binding, mDNS discovery,
physical no-microphone boot/reconnect, or installed upgrade was exercised here.
Those need a bounded fresh-device acceptance run. Earlier performance, recovery
and hardening evidence retains its original scope.

HTTP passwords/sessions are unencrypted. Whoever first reaches the unconfigured
device can create the administrator. These are explicit trusted-LAN tradeoffs,
not solved by Host/Origin checks. TLS/trust provisioning is not implemented.
At the time of these local checks, release hosting and a public installation
command awaited a destination. The subsequent GitHub prerelease provides those
under the repository/HTTPS trust model, not an independent publisher signature.
No broad board/USB support is claimed.
