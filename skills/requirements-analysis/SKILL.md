---
name: requirements-analysis
description: Analyze PRDs, user stories, and acceptance criteria; extract measurable requirements and surface gaps before implementation.
roles: [architect, intake]
---

# Requirement Analysis

Use this skill when a task needs requirement decomposition, missing-criteria checks, or an implementation-ready requirements brief.

## Procedure

1. Read the relevant PRD, issue, or user story and identify the business goal.
2. Extract functional requirements, non-functional constraints, and explicit acceptance criteria.
3. Look for missing conditions, security assumptions, edge cases, and dependencies.
4. Distinguish clear requirements from incomplete or contradictory language.
5. Produce a concise requirement-to-implementation map with open questions.
6. Flag any requirement that needs human clarification before engineering can proceed safely.
7. Shape each criterion so it can survive approval: one observable outcome, an explicit `mandatory` flag, and a verification route. Use the [acceptance-test-design](../acceptance-test-design/SKILL.md) skill for the mapping itself.

## Criteria that survive approval

- Application code assigns criterion identifiers (`AC-1`, `AC-2`, ...) positionally at approval time, so ordering matters and agents must not invent identifiers.
- Wording is part of the frozen contract digest. Repeat it verbatim in later reports; a paraphrase is treated as a different requirement.
- A criterion must be checkable by someone who did not write it. "The behavior is correct" is not a criterion.
- Do not fold several requirements into one criterion to keep the list short. Approval will refuse criteria that normalize to the same wording.

## Output

- objective and scope
- required behavior
- acceptance criteria mapping with mandatory flags and verification routes
- dependencies and assumptions
- ambiguities or blocked questions
- implementation references

## Useful references

- [../../../README.md](../../../README.md)
- [../../../docs/workflows/feature-lifecycle.md](../../../docs/workflows/feature-lifecycle.md)
- [../../copilot-instructions.md](../../copilot-instructions.md)
