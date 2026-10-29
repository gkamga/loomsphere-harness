---
name: engineering-review
description: Inspect implementation and tests for correctness, ADR conformance, security, and overall engineering quality.
roles: [reviewer, security-reviewer, ux-reviewer]
---

# Engineering Review

Use this skill for code review, implementation verification, and requirement-to-change traceability.

## Procedure

1. Read the requirement, the approved acceptance contract, and related ADRs.
2. Review the patch and test changes in full, including unexpected or unrelated files.
3. Trace the implementation to the intended behavior and confirm that the evidence supports it.
4. Check for security risks, maintainability issues, performance concerns, and missing edge cases.
5. Record findings with severity, evidence, and a recommended fix path.
6. Report each approved criterion by identifier as `met`, `unmet`, or `unknown`. Treat an unreported criterion as omitted, and treat an omission as blocking rather than as a documentation gap.
7. Provide a clear recommendation: accept, fix required, or reject.

## Guardrails

- Do not infer coverage from a green suite. Passing tests show that the tests passed; they do not show that the approved requirements were implemented.
- Do not accept an attempt whose evidence you did not inspect. Application code requires a compliant audit bound to the same implementation attempt before a task becomes deliverable.
- Keep code quality and requirement coverage as separate findings so a clean diff cannot mask a missing requirement.

## Output

- summary of the review scope
- requirement and ADR alignment findings
- criterion-by-criterion coverage with identifiers
- correctness and security issues
- test coverage gaps
- final recommendation with evidence

## Useful references

- [../../copilot-instructions.md](../../copilot-instructions.md)
- [../../../README.md](../../../README.md)
- [../../../docs/workflows/feature-lifecycle.md](../../../docs/workflows/feature-lifecycle.md)
- [../../../docs/decisions/README.md](../../../docs/decisions/README.md)
