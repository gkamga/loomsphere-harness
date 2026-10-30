---
name: acceptance-test-design
description: Turn approved acceptance criteria into frozen, identifier-addressed obligations with a verifiable evidence route for each one.
roles: [architect, qa]
stages: [architecture, validation]
---

# Acceptance Test Design

Use this skill when approving scope, or when reviewing whether the evidence for an approved scope is checkable at all. The output is a set of obligations that application code can later enforce, not a narrative list of hoped-for behavior.

## Who decides

The design work is advisory. `backend/app/services/scope_approvals.py` freezes the contract at approval time, `backend/app/schemas/acceptance_contract.py` assigns the identifiers and validates the shape, and `backend/app/services/acceptance.py` decides compliance. An agent can propose criteria and evidence routes; it cannot mark them satisfied.

## Procedure

1. Start from the approved scope proposal and its acceptance-criteria mapping.
2. For each criterion, state one observable outcome. Split anything that hides two outcomes in one sentence, because a criterion is satisfied or not satisfied as a whole.
3. Decide whether the criterion is `mandatory`. Mandatory means a delivery without recorded evidence for it must fail, so at least one criterion must be mandatory.
4. Choose the evidence route:
   - `pytest` with a bounded relative selector such as `tests/test_module.py::test_case` when application code can execute the check itself.
   - `unverified` when no machine check exists. This records a limitation; it is not evidence. Builder, QA, and Reviewer claims cannot satisfy it, and a mandatory unverified criterion blocks readiness. The current application has no explicit human-verification or waiver route.
5. Prefer a selector that exists, or that the approved scope is expected to add. A declared selector that does not exist will fail, and a failing check blocks delivery.
6. Keep wording stable. Identifiers are positional (`AC-1`, `AC-2`, ...), and the contract digest covers the wording, so re-approval produces a new version rather than a silent edit.
7. Remove criteria that cannot be observed, and say so explicitly rather than leaving them implied.

## Refuse to approve when

- every criterion is optional
- two criteria describe the same requirement in different words
- a criterion depends on "all tests pass" with no identifiable test
- the verification route is an agent or human assertion without an attributable record
- the criteria contradict an accepted ADR

## Output

- one line per criterion: identifier, observable outcome, `mandatory` flag, verification route
- the criterion most likely to be omitted, and why
- validation commands a Builder or QA agent can actually run
- residual risk for every `unverified` criterion

## Useful references

- [../../../docs/workflows/feature-lifecycle.md](../../../docs/workflows/feature-lifecycle.md)
- [../../../docs/decisions/README.md](../../../docs/decisions/README.md)
- [../../copilot-instructions.md](../../copilot-instructions.md)
