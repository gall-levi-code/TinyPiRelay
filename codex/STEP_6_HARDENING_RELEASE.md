# Step 6 — Hardening and Release Review

## Goal

Review TinyPiRelay as an independent maintainer, repair release-blocking defects within v1 scope, and produce an evidence-based release decision.

Prefer a fresh Codex task. Assume prior architecture, tests, security, compatibility claims, and documentation may be wrong.

## Review order

1. Read the shared source-of-truth documents and `docs/PROJECT_STATE.md`.
2. Inspect the full repository, dependency/licensing provenance, current diff/history, CI, generated artifacts, and user documentation.
3. Trace end-to-end trust, media, recording, install/upgrade/uninstall, and failure paths before editing.
4. Rank findings by risk. Fix in-scope release blockers and add the smallest regression evidence; report scope-expanding choices instead of guessing.

## Required review areas

- Media continuity: branch isolation, queue bounds, state races, shutdown/finalization, reconnect loops, negotiated caps, device return, and telemetry client churn.
- Data safety: exact-destination 90% threshold, write/rotation failures, partial FLAC handling, atomic config, interrupted upgrade/install, and uninstall preservation.
- Security: authentication primitive, sessions/cookies, CSRF, brute-force throttling, injection/path traversal, secret exposure, filesystem ownership, service sandboxing, bind defaults, diagnostic redaction, and dependency/provenance review.
- Installer: preflight-before-mutation, idempotency, pinning/integrity, unsupported OS/architecture behavior, `/dev/tty`, rollback/recovery, service ordering, and no hidden distribution upgrade.
- Portability: ARMv6 dependencies, 32/64-bit assumptions, standard Linux interfaces, SBC claims, and fixture-vs-hardware gaps.
- Reliability: long-duration memory/FD/log growth, CPU/thermal degradation order, network/DNS/receiver loss, reboot, web crash, repeated commands, and corrupt input/config.
- Documentation: exact install/GUI/recovery commands, receiver recipes, config semantics, compatibility labels, limitations, licensing, and absence of unimplemented v2 features.

## Release gate

Run the complete available test/static/integration suite and the longest authorized soak tests. Review failures rather than suppressing them. Recheck the working tree for accidental secrets, generated junk, unrelated changes, and misleading claims.

Produce `docs/RELEASE_READINESS.md` containing:

- release recommendation: ready, conditional, or not ready;
- blocking and non-blocking findings with evidence;
- tests and hardware matrix actually completed;
- security and license review outcome;
- known limitations and recovery guidance;
- exact remaining actions, owners if known, and verification command/procedure.

## Definition of done

- No known critical/high release blocker is left unreported.
- Fixed defects have focused regression evidence.
- All compatibility and feature claims match evidence.
- Release docs are internally consistent and reproducible.
- `docs/PROJECT_STATE.md` records the final outcome; do not tag, publish, push, or create a release without explicit authorization.
