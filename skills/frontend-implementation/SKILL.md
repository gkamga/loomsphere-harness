---
name: frontend-implementation
description: Implement a user-facing change with every state, accessible semantics, and the existing design system rather than ad-hoc markup.
roles: [builder]
stages: [implementation]
task_types: [ui]
areas: [frontend]
implications: [ux_ui]
requires_tools: [edit]
requires_skills: [safe-validation]
---

# Frontend Implementation

Use this skill when the approved scope changes what a person sees or does in the
interface. The change is finished when the default, loading, empty, error, disabled,
and destructive states all exist — not when the happy path renders.

## Procedure

1. Find the closest existing screen, component, or form and follow its structure.
   Reuse the design-system primitives and the established styling mechanism; do not
   introduce a component library, a styling system, or a second convention.
2. Write the states before the markup:
   - default and populated
   - loading or pending, if the view waits for anything
   - empty, with the action that fills it
   - error, with what the person can do next
   - disabled and destructive, with confirmation when the action loses data
3. Make it operable without a pointer: every control reachable by keyboard, an
   accessible name on every input and button, focus order that follows reading
   order, and focus that moves to the new content after an action.
4. Associate validation errors with the field they belong to, and keep the entered
   value after a rejected submit.
5. Keep layout resilient: no fixed pixel widths that clip, no text that truncates
   the only label, no target smaller than the surrounding convention, and a sensible
   result at the breakpoints the repository already supports.
6. Add or update the test that would fail if the state disappeared. Prefer the test
   style the repository already uses over a new one.
7. Run the safe validation path for the touched surface and report the exact command
   and result. A component that renders but whose test suite was not run is not
   verified.

## Evidence to report

- which states you implemented, and which already existed
- the accessibility properties you relied on (names, roles, focus behaviour)
- the design-system pieces you reused, and anything new you introduced with a reason
- the validation command and observed result

## Guardrails

- Do not restyle unrelated screens, rename shared components, or "fix" layout outside
  the approved scope; report it as a follow-up instead.
- Do not hard-code copy that belongs in the interface's text catalog, if the
  repository has one.
- Do not hide a state you could not implement; report it as unmet.
- Do not treat a screenshot as proof of behaviour that only a test can show.
