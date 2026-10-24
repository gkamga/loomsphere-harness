---
name: engineering-review
description: Inspect implementation and tests for correctness, ADR conformance, security, and overall engineering quality.
---

# Engineering Review

Use this skill for code review, implementation verification, and requirement-to-change traceability.

## Procedure

1. Read the requirement, acceptance criteria, and related ADRs.
2. Review the patch and test changes in full, including unexpected or unrelated files.
3. Trace the implementation to the intended behavior and confirm that the evidence supports it.
4. Check for security risks, maintainability issues, performance concerns, and missing edge cases.
5. Record findings with severity, evidence, and a recommended fix path.
6. Provide a clear recommendation: accept, fix required, or reject.

## Output

- summary of the review scope
- requirement and ADR alignment findings
- correctness and security issues
- test coverage gaps
- final recommendation with evidence

## Useful references

- [../../copilot-instructions.md](../../copilot-instructions.md)
- [../../../README.md](../../../README.md)
- [../../../docs/workflows/feature-lifecycle.md](../../../docs/workflows/feature-lifecycle.md)
- [../../../docs/decisions/README.md](../../../docs/decisions/README.md)
