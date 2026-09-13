# TinyPiRelay First-Run Prompt

Work from the repository root. Your objective is to establish the repository state and complete only Milestone 1.

Before editing:

1. Read, in full, `docs/PROJECT_SPEC.md`, `docs/HARDWARE_MATRIX.md`, `docs/CODEX_WORKFLOW.md`, and `codex/STEP_1_ARCHITECTURE_SPIKE.md`.
2. Inspect the entire current repository, including instructions, source, tests, CI, packaging, licenses, and uncommitted changes. Preserve user work and reuse existing patterns.
3. Identify contradictions between the repository and the specification. The specification wins unless changing existing user work would be destructive or materially ambiguous; report that blocker instead of guessing.
4. Create or update a concise `docs/PROJECT_STATE.md` using the handoff fields in `docs/CODEX_WORKFLOW.md`.

Then execute `codex/STEP_1_ARCHITECTURE_SPIKE.md`. Do not implement Steps 2-6. A spike may use small disposable or retained test utilities, but do not disguise unverified shell pipelines or mocks as a production media engine.

Make safe, in-scope local changes and run relevant non-destructive validation without asking. Do not install packages on the development host, modify system services, push, publish, or access external hardware unless explicitly authorized. When a real Raspberry Pi or receiver is unavailable, provide reproducible commands and mark the evidence pending.

Finish with:

- outcome and files changed;
- decisions made and any spec deviation;
- tests/checks run with results;
- hardware-tested vs simulated/CI evidence;
- open risks or blockers; and
- the exact readiness state for Step 2.
