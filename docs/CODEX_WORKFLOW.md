# Codex Workflow

## Operating rule

`docs/PROJECT_SPEC.md` is the product source of truth. `docs/HARDWARE_MATRIX.md` controls compatibility language. A milestone prompt scopes one implementation pass; it does not replace either document.

Use a fresh Codex task for each milestone when practical. Recommended baseline: GPT-5.6 Sol with high reasoning; use max for Steps 1, 4, and 6 or difficult GStreamer/ARMv6 failures. Model settings are operator choices, not repository requirements.

## First run

Paste `codex/FIRST_RUN_PROMPT.md` into Codex from the repository root. It instructs Codex to inspect the repository, ingest the specification, create a factual project-state record, and execute Step 1 only.

## Later runs

For Step N:

1. Start at the repository root with the prior milestone changes present.
2. Give Codex `codex/STEP_N_*.md`.
3. Codex reads the source-of-truth documents and current project state before editing.
4. Codex implements only that milestone, runs relevant non-destructive checks, and updates evidence, docs, and project state.
5. A human reviews the diff and hardware evidence before starting the next step.

Step 6 should preferably be a fresh review task that assumes prior implementation may be wrong.

## Handoff record

Step 1 creates `docs/PROJECT_STATE.md`. Every step updates it with:

- milestone status and commit/diff identity;
- decisions and deviations from the spec;
- commands/checks run and results;
- hardware vs simulated/CI evidence;
- known failures, risks, and unverified assumptions;
- exact next-step prerequisites.

Keep it factual and compact. Do not paste logs that already live in test artifacts.

## Universal completion gate

Every milestone must:

- run relevant tests and static checks;
- record reproducible validation commands and outcomes;
- update user and developer documentation for implemented behavior;
- distinguish hardware-tested, CI-validated, and untested claims;
- report limitations and leave failing acceptance items open;
- avoid unrelated refactors and future-milestone features; and
- never push, publish, install on external hosts, or perform destructive actions without explicit authorization.

If asked to commit, include only milestone-relevant changes. A passing mock test never substitutes for physical Pi evidence.
