---
name: intake-triage
description: Triage an engineering request into intent, affected areas, ambiguities, implications, and specialist recommendations without designing the solution.
roles: [intake]
---

# Intake Triage

Use this procedure to turn one engineering request into structured triage evidence.
The result is evidence a human reads before approving a scope, not a plan.

## 1. Separate the request from the solution

Write the intent in the requester's terms: who wants what, and what observable
outcome says it worked. If you cannot state the outcome, that is the first
ambiguity -- not a reason to invent one.

Do not describe implementation steps, file edits, or acceptance criteria. Those
belong to the Architect, and writing them here would create a second, unapproved
plan.

## 2. Locate the affected areas

Read the repository before naming a component. Prefer a path that exists over a
plausible-looking one, and mark an area you could not locate as unknown rather
than guessing a directory.

Classify each area as backend, frontend, database, infrastructure, documentation,
tests, shared, or unknown. The classification is what tells a human whether a
specialist area is involved at all.

## 3. Find what the request does not say

Check for the usual silent gaps:

- Who acts, and under which permission?
- Where does the data come from, and what is its shape?
- What happens when the operation fails, is empty, or is denied?
- Which existing behaviour must remain unchanged?
- Which scope boundary is unclear (one module, one screen, one workflow)?

Mark a gap **blocking** only when the Architect cannot produce a defensible scope
without it. Everything else is non-blocking and stays visible for the human.

## 4. Name the implications

- **Security** -- the trust boundary or sensitive data the request touches, and how
  it could be misused.
- **UX/UI** -- the user-visible surface, state, or interaction that needs review.
- **Database** -- schema, migration, or data-integrity consequences.
- **Architecture** -- a boundary, contract, or recorded decision the request bends.

An implication names a location and a concern. "Could be risky" is not an
implication.

## 5. Recommend specialists, do not select them

For each specialist listed in the prompt, decide whether the request needs it, and
write the reason a human can check against the request text. If no specialist
applies, recommend none. If a concern has no corresponding specialist, record it
as an implication and say so in the notes instead of inventing a recommendation.

Your recommendation can add review. It cannot remove a review the deterministic
router requires, and it never decides a verdict.

## 6. Assess risk

Risk is about impact and reversibility: a wrong result that cannot be undone, a
security or data boundary, or a change that reaches every user is high risk. Effort
size is not risk.

## 7. Output

Return the single fenced JSON block the response contract requires. Record
assumptions you made to read the request, so a human can correct them.
