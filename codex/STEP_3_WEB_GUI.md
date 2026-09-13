# Step 3 — Web GUI, Authentication, and Control Plane

## Goal

Add a lightweight authenticated web process that controls and observes the independent media service without becoming part of its failure domain.

## Required work

- Read the shared documents, implemented service contracts, security decisions, and `docs/PROJECT_STATE.md`.
- Build the smallest phone-friendly plain HTML/CSS/JavaScript UI and lightweight server that satisfy the spec; do not add a frontend build system.
- Implement login/logout, slow salted password verification, secure sessions, CSRF protection for mutations, authentication throttling, input validation, safe output encoding, and redaction of secrets.
- Implement a stable local control/status API between web and media services with timeouts and explicit unavailable/degraded states.
- Derive audio choices from probed combination-level capabilities. Show native vs converted modes and never present impossible or unverified stream combinations.
- Provide dashboard, audio, stream, recording, diagnostics/log views, and clear confirmation when a change requires a media restart.
- Render per-channel meters and a scrolling spectrogram in the browser from bounded numeric telemetry. Prefer SSE for one-way data unless evidence justifies WebSockets.
- Make slow/many/disconnected clients bounded. Reduce or suspend spectrum work when no clients request it where the service contract permits.
- Keep accessibility basics: semantic controls, labels, keyboard operation, visible focus, sufficient contrast, status not conveyed by color alone, and reduced-motion behavior.

## Required scenario checks

- Kill/restart the web process during an active stream and recording; media must continue.
- Exercise login failure/rate limiting, logout/session invalidation, CSRF rejection, malformed settings, and secret redaction.
- Feed capability fixtures with non-Cartesian audio modes; impossible combinations must not appear or be accepted by the API.
- Use a slow telemetry client and repeated connect/disconnect churn; memory and queues remain bounded.
- Show disconnected media service, SRT reconnecting, device loss, and storage hard-stop states accurately.

## Out of scope

TLS termination, Internet exposure claims, network management, full installer/systemd creation, hardware benchmark tuning, and future SRT/network modes.

## Definition of done

- Security and API tests pass, critical UI flows are exercised, and media survives web failures.
- The browser receives data rather than server-rendered meter/spectrum images.
- UI claims match actual capability/evidence state.
- Operator docs and `docs/PROJECT_STATE.md` are ready for Step 4.
