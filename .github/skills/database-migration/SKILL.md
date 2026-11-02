---
name: database-migration
description: Apply a schema change through reversible, backward-compatible migrations that production can roll forward and that reviewers can verify.
roles: [builder]
stages: [implementation]
task_types: [database]
areas: [database]
implications: [database]
requires_tools: [edit, execute]
requires_skills: [safe-validation]
---

# Database Migration

Use this skill when the approved scope changes persisted data or structure: a new
table, a new column, an index, a constraint, a backfill, or a rename. The goal is a
change a reviewer can verify and production can apply without a coordinated outage.

## Procedure

1. Find the migration mechanism the repository already uses and add one revision in
   it. Do not introduce a second mechanism, a second history table, or an ad-hoc
   script the deployment does not run.
2. Read the migration head and its parent revision before writing yours. A revision
   that branches the history is a defect even when it applies cleanly on your
   machine.
3. Prefer expand/contract over a single destructive step:
   - add the new nullable column, table, or index first;
   - backfill in a bounded, resumable way, or state why the backfill is trivial;
   - switch reads, then writes;
   - remove the old shape in a later, separately reviewable migration.
4. Never put an unbounded UPDATE, a full-table rewrite under lock, or a blocking
   index build on a table the application reads in production without saying so in
   the change summary and choosing the concurrent form the engine supports.
5. Keep the downgrade honest. Either it returns the previous shape, or the summary
   says plainly that the migration is one-way and why.
6. Guard destructive work. A DROP, a type narrowing, or a NOT NULL addition on
   existing data needs a recorded reason and a plan for rows that violate it.
7. Update the model, the query layer, and the tests that exercise them in the same
   change. A migration whose application code lands later is half a change.
8. Run the migration against a disposable database through the repository's safe
   validation path, and record the exact command, exit code, and the head revision
   the run reached.

## Evidence to report

- the revision identifier(s) added and their parent
- what the migration does to existing rows, and whether it locks or rewrites
- the downgrade behaviour
- the validation command and its observed result
- anything that makes the change unsafe on a large table

## Guardrails

- Do not run a migration against a database whose isolation you have not verified.
- Do not edit an already-applied revision to make a new change fit; add a new one.
- Do not drop or narrow a column without saying what happens to the data in it.
- Do not claim the migration is reversible when the downgrade only drops the table.
