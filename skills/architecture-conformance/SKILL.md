---
name: architecture-conformance
description: Cross-check implementation plans and code against accepted ADRs and repository architecture constraints.
roles: [architect, reviewer]
stages: [architecture, review]
---

# Architecture Conformance

Use this skill when a change might diverge from the approved architecture, accepted decisions, or repository guardrails.

## Procedure

1. Identify the relevant ADRs, design guidance, and current implementation scope.
2. Separate accepted decisions from draft or historical proposals.
3. Compare the planned or current code against those approved constraints.
4. Highlight direct conflicts, risk areas, and missing evidence.
5. Escalate architecture deviations that require human approval before implementation continues.

## Output

- applicable ADR references
- accepted decision summary
- observed deviation or risk
- required mitigation or approval path
- validation evidence required for conformance

## Useful references

- [../../../docs/decisions/README.md](../../../docs/decisions/README.md)
- [../../../README.md](../../../README.md)
- [../../copilot-instructions.md](../../copilot-instructions.md)
