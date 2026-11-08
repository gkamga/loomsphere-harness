# QA recovery: one authorised re-run of a stopped QA invocation

A QA invocation can stop before it answers. When it does, the task lands in
`qa_failed` even though the Builder finished, the change set was frozen, and the
immutable sandbox validation already proved that change set against every
acceptance selector. QA never formed an opinion, so there is nothing to
contradict and nothing to overrule — but until this workflow existed there was no
supported command out of that state.

QA recovery re-runs **only the QA invocation**, once, on the exact state that was
already proven, and then lets the normal Reviewer, delivery, and human-acceptance
stages continue. It is a human-authorised operation, not an automatic retry.

## The case it was built from

Demandro M0-02 is the reference failure:

- the Builder produced the files and the change set was frozen on the task branch;
- Docker validation succeeded and all five acceptance selectors passed inside the
  authorised sandbox image;
- the QA invocation then stopped with `resource-limit-exceeded` after consuming
  69,624 tokens against a 60,000-token run limit, in 4 requests;
- the task was left in `qa_failed` with intact implementation, evidence, and
  approval, and no command could resume it.

Nothing about that implementation was wrong. Only the QA invocation's budget ran
out, and the run spent that budget before it answered.

## Commands

```bash
# Read-only. Changes nothing, starts nothing, spends no retry.
loomsphere-harness inspect-qa-recovery T-0001

# Human-authorised. Requires the exact id of the stopped run that was inspected.
loomsphere-harness retry-qa T-0001 --failed-run <exact-run-id>
```

`inspect-qa-recovery` exits `0` when the task is eligible, `3` when it is not, and
reports every obstacle it found. It does not repair interrupted state on the way
in: repairing appends audit events and can move a task's status, which would
change the very state a human is being asked to judge.

`retry-qa` exits `0` when the recovery ran, `2` when it was refused, and prints
the reason.

## Eligibility

All of the following must hold; the first failure is reported as a refusal.

| Condition | Recorded evidence |
| --- | --- |
| Task is in `qa_failed` | task row |
| A QA run stopped for a recoverable reason | run `failure_reason` in `QA_RECOVERABLE_FAILURES`, which includes `resource-limit-exceeded` |
| QA recorded no verdict for this iteration | no `qa_report`, `qa_digest`, or `qa_run_id` on the iteration |
| No deliverable or acceptance exists | `deliverables`, `tasks.status` |
| The proposal and approval are the frozen ones | contract digest and workspace digest match the iteration |
| The source is exactly what was validated | `change_set.workspace_digest == validation.workspace_digest` |
| Validation succeeded against that source | `status = succeeded`, `exit_code = 0`, selector coverage complete |
| No earlier QA recovery for this iteration | `qa_recoveries` row count for the iteration is below `MAX_QA_RECOVERIES` |

## What it refuses

- a recorded adverse QA verdict (`failed`) — that is an opinion, and the ordinary
  rework path already handles it;
- a failed or missing validation record, or validation against a different source
  digest than the worktree now holds;
- a worktree that no longer matches the recorded change set (uncommitted changes,
  a moved commit, a deleted worktree);
- a run id that is not the stopped QA run the operator inspected;
- an exhausted retry entitlement (one recovery per iteration);
- a concurrent recovery, via an exclusive task-level lease;
- any task that is not `qa_failed`.

Every refusal leaves the task, the iteration, the runs, the audit log, the
recoveries, and the worktree byte-identical.

## Execution order

1. record the recovery authorisation atomically, before anything runs;
2. preserve the stopped QA run exactly as recorded;
3. rebuild the QA prompt from the recorded approved contract, the frozen source
   snapshot digest, and the recorded validation evidence;
4. invoke QA once, as a new auditable run;
5. QA passes → continue through the existing Reviewer stage;
6. Reviewer passes → the existing delivery-record and human-acceptance workflow;
7. any stage fails → stop and retain the complete evidence.

Intake, Architect, Builder, and the sandbox validation are **not** re-run: they
were already proved against the identical source and image, and repeating them
would cost the same budget while proving nothing new. No Builder report and no QA
evidence is ever manufactured for the re-run; the new QA run carries a provenance
block naming the run it replaces, that run's failure reason, and the fact that
nothing was rebuilt or re-validated.

## Reading the token consumption

The runtime already records what is needed to judge a budget; no code change was
required to diagnose this failure, and no limit was changed.

For a stopped run, `runs.events` holds:

- `usage` — `requests`, `tool_calls`, `input_tokens`, `output_tokens`,
  `total_tokens`, plus `availability` and `tokens_are_lower_bound` so an
  unmeasured total is never read as zero;
- `limits` / `diagnostics` — the configured `request_limit`,
  `tool_call_limit`, and `total_token_limit` with their observed values and the
  limiting one flagged;
- `tool_summary` — tool names and call counts only. No prompt, message, tool
  argument, or file content is persisted, so this is the safe place to look for
  repeated or unnecessary reads.

`inspect-qa-recovery` prints those counters for the stopped run, sorted, so the
failure can be read without opening the database.

What the Demandro numbers imply, stated as inference rather than measurement:

- ~17,400 tokens per request across 4 requests is far above a single
  question-and-answer turn, so most of that input was context the run re-read
  rather than reasoning it performed;
- the natural suspects are the same recorded evidence being read more than once
  (contract, criteria, build report, validation output, the change set, and the
  source files themselves), and tool results large enough to be re-sent on every
  subsequent request because conversation history grows;
- the recorded `tool_summary` is the check to run: repeated reads of the same
  path or repeated listings are visible there without any new instrumentation.

Deliberately not done: raising the token limits, weakening the QA instructions,
dropping required evidence, or truncating the prompt arbitrarily. Any of those
would hide the failure instead of exposing it.

For a later controlled pilot, the measured floor (~69,600 tokens for a QA run
that reads this change set and still does not answer) suggests a
`total_token_limit` near 150,000 and a `request_limit` near 20 for QA only, set
per role through `[model] role_limits` rather than as a new global default. That
is a budget to be confirmed by the next pilot's recorded `usage`, not a limit to
apply now.

## Guarantees

- `qa_recoveries` is append-only: `UPDATE` and `DELETE` are rejected by triggers,
  and each row is written in the same transaction that records the
  `qa.recovery-authorized` audit event.
- At most one recovery per iteration, enforced by a unique index on
  `iteration_id` and by `MAX_QA_RECOVERIES`.
- The authorisation is claimed atomically inside `BEGIN IMMEDIATE`, so two
  processes cannot both spend the same retry.
- Recovery execution holds an exclusive lease file next to the state database; a
  second attempt fails with `recovery-active` instead of running concurrently.
- The stopped QA run, the original iteration failure, and every prior audit event
  are never rewritten.
- Builder restart/recovery eligibility and the Reviewer retry path are unchanged.
- Human acceptance is still required: recovery ends at `awaiting_acceptance`, and
  the delivered document carries a `qa_recovery` block recording the authorising
  identity, the replaced run, the failure reason, and that nothing was rebuilt or
  re-validated.

## Schema

Schema version 5 adds one table. The migration is additive and
backward-compatible: it creates `qa_recoveries` and its triggers, and verifies row
counts in `tasks`, `runs`, `iterations`, `recoveries`, and `builder_restarts`
before and after, raising `state-corrupt` rather than continuing if any count
changes. There is no downgrade path, as with every earlier version: a runtime
that knows only version 4 refuses the database with `state-version` instead of
reading it partially.
