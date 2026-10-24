---
name: safe-implementation
description: Make minimal, approved implementation changes with evidence, test updates, and validation awareness.
---

# Safe Implementation

Use this skill when implementing a bounded change in an existing codebase.

## Procedure

1. Inspect the relevant files and current tests before changing behavior.
2. Confirm the scope, approved decisions, and relevant acceptance criteria.
3. Choose the smallest safe modification that meets the requirement.
4. Preserve unrelated work and avoid unnecessary refactors.
5. Add or update tests for changed behavior.
6. Prepare a short validation report with exact commands and results.

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
