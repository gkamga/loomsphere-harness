---
name: safe-implementation
description: Make minimal, approved implementation changes with evidence, test updates, and validation awareness.
roles: [builder]
---

# Safe Implementation

Use this skill when implementing a bounded change in an existing codebase.

## Procedure

1. Inspect the relevant files and current tests before changing behavior.
2. Confirm the scope, approved decisions, and relevant acceptance criteria, including the approved criterion identifiers.
3. Choose the smallest safe modification that meets the requirement.
4. Preserve unrelated work and avoid unnecessary refactors.
5. Add or update tests for changed behavior.
6. Report every approved criterion with its identifier and a truthful `met`, `unmet`, or `unknown` status.
7. Prepare a short validation report with exact commands and results.

## Acceptance obligations

- An omitted criterion is not a satisfied criterion. Application code fails the attempt when a mandatory criterion has no evidence, even if every test passed.
- Do not reword a criterion to match what you built. Repeat the approved wording and report the real status.
- Do not narrow the approved scope on your own. If a criterion is wrong or impossible, stop and escalate for a new approval.
- When the loop returns acceptance feedback, address the named criteria and state what changed; do not re-run unchanged work and hope for a different verdict.

## Guardrails

- Do not bypass architecture decisions or required review steps.
- Do not overwrite unrelated edits.
- Do not hardcode secrets or credentials.
- Do not claim validation passed unless it was actually executed.
- If a database operation is unsafe or unverified, report it as blocked.

## Useful references

- [../../copilot-instructions.md](../../copilot-instructions.md)
- [../../../scripts/validate.sh](../../../scripts/validate.sh)
- [../../../docs/workflows/feature-lifecycle.md](../../../docs/workflows/feature-lifecycle.md)
