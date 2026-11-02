---
name: accessibility-testing
description: Verify a user-facing change is operable and understandable with a keyboard, a screen reader, and reduced motion, and record the evidence.
roles: [qa, ux-reviewer]
stages: [validation, ux-review]
areas: [frontend]
implications: [ux_ui]
---

# Accessibility Testing

Use this skill when the change touches a user-facing surface. Accessibility is a
behavioural property of the interface: it is verified by exercising the flow, not by
reading the markup and hoping.

## Procedure

1. Start from the user-visible change, not the diff: name the flow, the person who
   uses it, and the states it can be in.
2. Walk the flow with a keyboard only. Record where focus starts, where it goes, and
   whether every action that is visible is also reachable. A control reachable only
   by pointer is a defect.
3. Check the accessible name and role of every interactive element the change
   introduced: an input has a label, a button says what it does, an icon-only control
   has a name, and an image that carries meaning has alternative text.
4. Check error and status communication: does a validation error name its field, is
   it announced when it appears, and is a destructive action confirmed in a way a
   screen reader announces rather than a colour change alone?
5. Check the reading order and the semantics: headings in order, lists as lists,
   tables with headers, landmarks where the application already uses them.
6. Check motion and timing: anything that animates, auto-advances, or times out must
   respect the reduced-motion and extended-time affordances the platform offers.
7. Record the evidence: the flow you walked, the checks you performed, and the exact
   observations. Where the factory cannot exercise the interface itself, say so —
   absence of evidence is not a pass.
8. Report findings with a severity. A blocked keyboard path, a missing accessible
   name, or an unannounced error in the changed flow is blocking.

## Evidence to report

- the flow and states you exercised
- the accessibility properties you verified, and how
- findings with severity, and what you could not verify in this environment

## Guardrails

- Do not approve a user-facing change you could not inspect; prefer
  \`changes-required\` with the reason.
- Do not invent a finding from the diff alone: name the observation.
- Do not restate or reword the approved acceptance criteria; compliance with them is
  application code's decision, not yours.
