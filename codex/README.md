# TinyPiRelay Codex Bootstrap

Use these files from the root of the TinyPiRelay repository.

## Ingestion order

1. `FIRST_RUN_PROMPT.md` — paste this into the first Codex task.
2. `../docs/PROJECT_SPEC.md` — immutable v1 requirements.
3. `../docs/HARDWARE_MATRIX.md` — compatibility expectations and claim labels.
4. `../docs/CODEX_WORKFLOW.md` — milestone and handoff rules.
5. `STEP_1_ARCHITECTURE_SPIKE.md` through `STEP_6_HARDENING_RELEASE.md` — one prompt per implementation pass.

The first-run prompt executes Step 1 only. For later milestones, start a new Codex task when practical and provide only the matching step prompt; that prompt tells Codex which shared documents to reread.

Do not concatenate all six prompts into one request. Advance only after reviewing the previous diff, validation results, and `docs/PROJECT_STATE.md`.

## Milestones

| Step | Outcome |
|---|---|
| 1 | Repository architecture and proven media/dependency spikes |
| 2 | Persistent, tested media engine and isolated branches |
| 3 | Authenticated web control plane and browser telemetry |
| 4 | Safe installer, systemd integration, CLI, upgrade/uninstall |
| 5 | Physical profiling and conservative hardware defaults |
| 6 | Independent hardening, long-run review, and release decision |
