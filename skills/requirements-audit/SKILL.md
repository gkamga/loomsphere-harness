---
name: requirements-audit
description: Audit recorded agent evidence against every approved acceptance criterion and report omissions, contradictions, and unverifiable claims independently of the agents that produced them.
roles: [qa, reviewer]
---

# Requirements Audit

Use this skill when evidence for an approved scope needs an independent check: did the recorded work actually address every approved obligation, and is the evidence strong enough to support the claims?

This capability is deliberately separate from implementation review. Reviewing code quality and auditing requirement coverage are different questions, and one agent answering both tends to accept its own omissions.

## Who decides

The audit is advisory input. `backend/app/services/acceptance.py` computes the compliance verdict deterministically for each implementation attempt and records it as an immutable audit row; `require_compliance` blocks promotion and delivery on anything other than a compliant verdict. An agent audit that disagrees with the recorded verdict does not change eligibility, and a task never reaches delivery on agent prose alone.

## Procedure

1. Load the frozen contract for the task. Use the approved criterion identifiers; do not invent your own numbering or restate criteria in your own words.
2. Read the Builder report, the QA report, and the Reviewer report for the same attempt, and nothing from a previous attempt.
3. Build a coverage table: one row per criterion, one column per reporting agent.
4. Classify each criterion:
   - `met` only when recorded evidence names the behavior, not merely the intent to implement it
   - `unmet` when evidence contradicts the criterion
   - `unknown` when no evidence exists
   - omit nothing: a criterion no report mentions is `unknown`, and silence is never `met`
5. Look for the specific failure this audit exists to catch: green tests plus a claim set that quietly covers fewer requirements than were approved.
6. Report contradictions between agents rather than averaging them. One contradicting record outweighs several confirming ones.
7. Recommend one outcome: compliance is supported, rework is required with the named criteria, or the case needs human escalation.

## Refuse to conclude compliance when

- a mandatory criterion has no evidence from any agent
- the evidence refers to a different contract version or a different commit
- the only support for a criterion is that the test suite passed
- a declared machine check was not executed
- a report claims a criterion that is not in the approved contract

## Output

- the coverage table, with identifiers
- omitted criteria and why the omission matters
- contradictions with the conflicting records
- the criteria a machine check should cover next time
- recommendation: supported, rework, or escalate to a human

## Useful references

- [../../../docs/workflows/execution-evidence.md](../../../docs/workflows/execution-evidence.md)
- [../../../docs/workflows/feature-lifecycle.md](../../../docs/workflows/feature-lifecycle.md)
- [../../copilot-instructions.md](../../copilot-instructions.md)
