---
name: safe-validation
description: Run the repository's safe validation path, preserve failing exit codes, and block unsafe database checks until isolation is verified.
---

# Safe Validation

Use this skill to validate a change without risking production or shared development data.

## Procedure

1. Identify the relevant validation category: backend, frontend, database, migration, or end-to-end.
2. Use the repository entry point in [../../../scripts/validate.sh](../../../scripts/validate.sh) when appropriate.
3. Prefer the smallest safe check that exercises the changed behavior.
4. For database-dependent checks, verify the database URL is isolated and disposable before continuing.
5. Preserve the real exit code and record the exact command output.
6. Report the result as pass, fail, blocked, or skipped.

## Acceptance criteria evidence

- A criterion declared as `pytest` with a selector is executed by application code, not by you. Report it honestly: if the check did not run, its criterion is unverified, not passed.
- Agent prose never satisfies a criterion on its own. A `met` claim without recorded evidence is an unverified claim, and reporting it as met is a false result.
- Distinguish the suite result from requirement coverage. "The full suite passed" answers a different question than "criterion AC-3 is satisfied".
- Record which command produced which evidence, so a later audit can bind the evidence to the same attempt.

## Guardrails

- Do not run database-dependent validation without explicit isolation confirmation.
- Do not infer success from a missing failure signal.
- Do not hide blocked checks behind a misleading pass result.
- If the environment is unsafe, stop and report the limitation.

## Useful references

- [../../copilot-instructions.md](../../copilot-instructions.md)
- [../../../scripts/validate.sh](../../../scripts/validate.sh)
- [../../../README.md](../../../README.md)
