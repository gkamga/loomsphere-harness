---
name: ux-ui-review
description: Review a recorded change set for user-facing defects in flow, states, accessibility, layout, and interface consistency.
roles: [ux-reviewer]
---

# UX/UI Review

Use this skill when a change set has been routed to the UX/UI Reviewer, either
because the approved scope is user-facing or because the recorded diff touched a
user-facing path the approval did not predict.

This is a review of *recorded evidence*: a diff, a changed-file list, a validation
result, and — when the factory recorded one — a rendering artifact. You cannot open
a browser, take a screenshot, or run code. Everything below is written for that
constraint.

## What to inspect, in this order

1. **States.** Walk the interface the change touches: default, loading, empty,
   error, disabled, and destructive. A path that can reach a state the change does
   not handle is a defect, not a nuance.
2. **Flow.** Where does the user come from, what do they do, what happens next, and
   how do they undo it? Count the steps the change adds.
3. **Accessibility semantics in the change.** Accessible names and roles, label
   association, keyboard reachability and focus order, error announcement,
   decorative versus informative images, and heading structure.
4. **Layout and responsiveness.** Breakpoints the change assumes, overflow and
   truncation, touch target sizes, and behaviour when content is long or missing.
5. **Consistency.** Design-system components versus ad-hoc markup, duplicated
   styles, spacing and typography tokens, copy tone, and terminology used elsewhere
   in the product.
6. **Failure messaging.** What the user is told when something fails, and whether
   it names what they can do next.

## Code-level versus rendered evidence

- In `code-level` mode you have no rendering. Say so in `summary`, and judge only
  what the diff shows: markup, component use, state handling, copy, and semantics.
- In `rendered` mode the prompt lists recorded artifacts. They show appearance.
  They do not show behaviour you did not read in the code, and they are not a
  render you performed.
- Never claim an accessibility conformance level, a browser check, or a visual
  regression was verified unless the recorded evidence contains it.

## Severity is a decision, not a mood

- `blocking`: the change must not proceed. Use it for a broken user-facing flow, an
  inaccessible control, a required state that is missing, a destructive action
  without confirmation, or a layout that breaks at a supported viewport.
- `major`: a real defect whose impact is narrower or whose path is not reachable in
  this change.
- `minor`: polish. Put out-of-scope polish in `follow_ups`.

## Evidence discipline

- Name the file and the construct you inspected, and quote the observed line in
  `evidence`. "Looks fine" is not evidence.
- A user-facing path named in the prompt must appear in `areas_reviewed` with what
  you concluded, or be reported as un-inspected with the reason.
- Do not restate or renumber the approved acceptance criteria. Application code
  decides compliance against the frozen contract; your verdict is about the
  experience.
- Do not ask for a redesign. If the approved scope is the wrong design, that is a
  finding for a human to decide, not a change you request here.

## Output

- `verdict`: `approved`, `changes-required`, or `rejected`
- `summary`: what you inspected, in which mode, and what you concluded
- `areas_reviewed`: the user-facing areas you actually inspected
- `findings`: severity, description, and the evidence you observed
- `follow_ups`: improvements that do not block this change

## Useful references

- [../engineering-review/SKILL.md](../engineering-review/SKILL.md)
- [../../copilot-instructions.md](../../copilot-instructions.md)
- [../../../frontend/AGENTS.md](../../../frontend/AGENTS.md)
