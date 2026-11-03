# Agent execution contracts and lifecycle

This document defines how LoomSphere work moves from an approved requirement to
a human merge decision, and what each agent must record on the way. It is a
manual workflow specification. Nothing here starts an agent, moves a task, or
grants authority; every transition is performed by a human clicking in the
editor or by an agent asked to act.

The existing feature flow remains in
[feature-lifecycle.md](feature-lifecycle.md). Where the two names differ, the
canonical roles below are authoritative: **Intake**, **Architect**, **Builder**,
**QA**, **Security Reviewer**, **UX/UI Reviewer**, and **Reviewer**, implemented in
[`.github/agents/`](../../.github/agents/).

## Canonical roles

| Role | Native definition | Authority |
| --- | --- | --- |
| Intake | [loomsphere-intake.agent.md](../../.github/agents/loomsphere-intake.agent.md) | Read-only requirements triage: intent, affected areas, ambiguities, implications, and specialist recommendations. Does not design, scope, or approve |
| Architect | [loomsphere-architect.agent.md](../../.github/agents/loomsphere-architect.agent.md) | Read-only analysis and bounded design handoff |
| Builder | [loomsphere-builder.agent.md](../../.github/agents/loomsphere-builder.agent.md) | Implementation and tests inside approved boundaries |
| QA | [loomsphere-qa.agent.md](../../.github/agents/loomsphere-qa.agent.md) | Independent execution and evidence classification |
| Security Reviewer | [loomsphere-security-reviewer.agent.md](../../.github/agents/loomsphere-security-reviewer.agent.md) | Read-only specialist review of one recorded change set; routed, never self-appointed |
| UX/UI Reviewer | [loomsphere-ux-reviewer.agent.md](../../.github/agents/loomsphere-ux-reviewer.agent.md) | Read-only specialist review of the user-facing behaviour of one recorded change set; routed, never self-appointed |
| Reviewer | [loomsphere-reviewer.agent.md](../../.github/agents/loomsphere-reviewer.agent.md) | Read-only review and recommendation |

No agent in this list can grant an approval, approve or supersede an ADR,
commit, push, merge, deploy, or override a failed gate.

Each role also declares the skill procedures the runtime loads for it. The binding
is declared once, in the role's own definition (`.github/agents/`), and the
procedure text lives in `.github/skills/`. The application loads both from the
trusted factory checkout and records the supplied skills, with their content
digests, on every agent execution; a Builder cannot redefine a procedure by
writing to its own worktree. A skill is procedure, not authority: it cannot widen
a tool list, a workspace mount, or any approval step.

Intake runs before the Architect and answers a different question: what is being
asked for, what does it touch, what is ambiguous, and which reviews does it imply.
It records that as one validated, digest-addressed assessment bound to the run that
produced it. Its specialist recommendations are advice: the deterministic routing
engine may add review on them and will keep every review it already requires, and
a human still approves the scope. A blocking intake question stops architecture and
approval until a human answers it. The recorded human answers are included in the
Architect's context verbatim; they clarify the request but do not approve scope.

The two review specialists above are additionally described by a trusted catalog
entry in [`.github/agents/catalog/`](../../.github/agents/catalog/). The entry is
what registers the reviewer: it declares the stage, the agent role, the authorized
tools and skills, the facts that select it, and its prompt profile. Application code
reads that entry, so registering another reviewer is a catalog entry, an agent
definition, and a skill -- not an orchestration change. The catalog is validated by
[`agent-catalog.schema.json`](../../.github/contracts/agent-catalog.schema.json) and
by `scripts/validate_factory.py` before a deployment can rely on it.

## Records

- A task is described once by a
  [task contract](../../.github/contracts/task-contract.schema.json).
- Each completed stage appends one
  [handoff contract](../../.github/contracts/handoff-contract.schema.json).
- Status and evidence field rules are defined in
  [`.github/contracts/README.md`](../../.github/contracts/README.md).

## Lifecycle hooks

Stage 4C adds four typed extension points to this lifecycle. A hook is a declarative
policy in the trusted registry (`.github/hooks/`) that observes or restricts an
existing transition; it cannot create one:

- `before-agent-invocation` and `after-agent-completion` wrap every agent invocation:
  the Architect and Intake queued runs and the Builder, QA, Reviewer, and specialist
  stages. A denial means the agent never runs, or its answer is not recorded, and the
  run is failed with `hook-denied`.
- `before-review-progression` runs after the accepted review, QA, validation binding,
  workspace check, acceptance audit, and specialist approvals all pass. A denial
  blocks the task instead of promoting it.
- `before-delivery` runs after every delivery gate passed and before anything is
  published. A denial is a recorded 409 with code `HOOK_DENIED`.

A hook may return `continue` or `block` and nothing else, so the mandatory gates above
are never replaced: a hook can add a refusal, never an authorization. Observers cannot
deny at all, and a blocking hook that cannot be evaluated fails closed. Every decision
is stored in `lifecycle_hook_executions` with the hook version, the event, the subject,
the findings, the observations, and the payload it saw; one subject is decided once, so
retries and worker restarts reuse the recorded decision. The factory state endpoint
exposes those decisions per task.

## Skills at execution time

Every agent invocation carries the procedures selected for it, and the run records
them. Selection combines the role's declared baseline with the trusted skills whose
own metadata names that role, that stage, and facts the recorded evidence actually
shows: the Intake request type, affected areas, implication areas, and risk level,
plus the frozen route's task type, areas, and risk level. A skill marked
`required: true`, and every procedure a role definition declares, is always present.

`agent_runs.skills` records the procedures with their content digests and the reason
each was chosen; `agent_runs.skill_selection` records the policy, the recorded
context, the candidates that were refused, and a digest over all of it. An execution
refuses with `prompt-changed` when the rendered prompt no longer matches the digest
recorded at dispatch, and with `skill-selection-changed` when the recomputed selection
no longer matches the stored one, so editing a skill or re-triaging the request after
dispatch cannot silently change what was authorized. A request may name a procedure
(`skill:<name>`) and that name is recorded, but naming a skill never makes one
eligible.

## Lifecycle

```text
submit -> Intake -> Architect -> human scope approval -> Builder -> QA -> [specialist review] -> Reviewer -> human final approval
             ^          ^                    |         |          |                     |
             |          |                    +---------+----------+---------------------+
             |          |                    rework path (new handoff)
             |          +-- intake completion hands the request over automatically
             +-- a blocking intake question stops architecture and approval
```

Intake is part of the default flow for a request submitted through the factory. The
submission writes `intake_policy = required` on the task and queues the Intake run in
the same transaction, so a request cannot exist without its triage and cannot be
accepted when the factory cannot execute it. Only a *valid, current* assessment opens
the next step: a missing assessment, a queued or running re-triage, an assessment
replaced by a newer one, an assessment whose run did not succeed, and an unanswered
blocking question each refuse the Architect and the scope decision with their own
error code. When intake finishes (and any blocking questions are answered), the
factory hands the request to the Architect automatically and records which assessment
authorized which Architect run; nothing else has to be called to move the request on.

The compatibility policy is recorded on the task, not implied: a task written before
this policy, or created outside the factory submission path, carries
`intake_policy = grandfathered` and keeps the previous workflow (no assessment
required; only a pending blocking question stops it). That is the one controlled
bypass, and it is visible on the row and in the API.

When both specialists are routed they are dispatched together as one independent
review group: neither waits for the other, both must approve, and the loop advances
once when the last verdict is recorded. A blocking verdict sends the attempt back
through the same bounded rework. The two members may really execute at the same time
on different workers: the job queue allows one in-flight job per grouped stage, and
the run record allows one active run per specialist per task (`ux-reviewer` and
`security-reviewer` overlap; a second run for the same specialist does not), while
the core roles keep exactly one active run per task. Stage 5 acceptance found the run
record still keyed by task alone, which forced the group to run one member at a time
and failed the second stage when two workers claimed both; migration 0021 splits that
invariant.
 The bracketed specialist step is
conditional. The factory decides it from facts
that are persisted before implementation (the approved scope paths, the
classification, and the risk level) and re-checks it against the recorded change
set. A change that turns out to touch a security-sensitive path raises the
requirement after the fact, and the approved route is superseded rather than
rewritten. A specialist adds review; it never replaces QA or the Reviewer, and it
holds no approval authority. Contract `1.3.0` is the version that can express a
chain through it.

### 1. Architect

**Entry criteria**

- A requirement source exists (PRD, issue, or tracked document) and is readable.
- The asked change is not already covered by an accepted ADR that contradicts it.
- Applicable ADRs, including their status, have been inspected.

**Required outputs**

- Task contract with objective, requirements, acceptance criteria, ADR
  references, and explicit allowed and prohibited paths.
- `status: awaiting-human-scope-approval` and `responsibleRole: human`.
- Risks, open questions, and any human decision the Architect cannot make.
- Handoff to the human that states the proposed validation route.

**Failure and blocked outcomes**

- Missing or contradictory requirements: handoff with `status: blocked`, the
  open question recorded, and no implementation guidance.
- Conflict with an accepted ADR: stop and request a human decision. The
  Architect must not redefine the decision.
- No applicable validation route: record the gap instead of inventing evidence.

**Exit to the next stage** only when a human records the scope approval. Until
then the task stays at `awaiting-human-scope-approval`.

### 2. Human scope approval

**Entry criteria**

- Architect task contract is complete and the handoff chain ends at the human.
- No blocking decision is unanswered.

**Required outputs**

- An approval reference recorded in `requiredApprovals[].reference` with
  `recorded: true`, and `status: approved`.
- Rejection recorded as `status: rejected` with a rationale, or requested
  changes recorded so a new Architect handoff is produced.

**Boundary** This is the only authority that turns a design into approved scope.
An agent recommendation, a passing test, or silence is not approval.

### 3. Builder

**Entry criteria**

- `status: approved` and a recorded human scope approval exist.
- Allowed and prohibited paths are explicit.
- Applicable accepted ADRs are identified in the task contract.

**Required outputs**

- Code and tests inside the allowed paths.
- A handoff that lists reviewed files, modified files, acceptance criteria
  addressed, and each validation record with the correct `kind` **and**
  `outcome`.
- `status: in-validation` (or `implemented` when validation is intentionally
  deferred to QA), with a handoff addressed to QA.

**Evidence rules**

- `kind` records how a result was obtained; `outcome` records what it means. An
  `observed` record requires the command, exit code, execution facts, and a
  reference, and its `outcome` must be `passed`, `failed`, or `inconclusive`.
- `exitCode: 0` is never treated as `passed` on its own, and `passed` with a
  non-zero exit code is valid only with `expectedExitCode` plus an
  `outcomeRationale` describing the deliberate negative test.
- `asserted`, `blocked`, `not-run`, and `skipped` records may not carry any
  execution metadata, so a claim without a run cannot masquerade as one.

**Failure and blocked outcomes**

- A failing check stays failing: record `kind: observed` with the nonzero exit
  code, or `status: validation-failed`. Do not delete coverage to make a gate
  pass.
- Database-dependent checks require verified isolation. Otherwise record
  `kind: blocked`; never record a blocked check as passing.
- Scope growth requires stopping and returning to the Architect; the Builder
  must not widen its own boundaries.

### 4. QA

**Entry criteria**

- Builder handoff exists with actual evidence for every claimed check.
- Acceptance criteria are listed with their verification methods.

**Required outputs**

- Independent execution of the smallest safe validation route.
- Classification of each check as observed, asserted, blocked, not-run, or
  skipped, preserving nonzero exit codes.
- An explicit `outcome` per check, separate from its exit code. A record is not
  `passed` because the process exited zero; it is `passed` because the assertion
  the executor made was satisfied. A blocked check keeps `outcome: blocked` and
  carries no exit code, even when the wrapper exited zero.
- Findings with severity and blocking status.
- Handoff to Reviewer with `status: in-review`, or `status: validation-failed`
  with a rework handoff back to the Builder.

**Failure and blocked outcomes**

- Failing checks: `validation-failed`, rework path to Builder. QA does not fix
  the implementation itself.
- Unsafe or unverified validation target: record the block and the exact
  condition that would unblock it.
- Missing builder evidence: record a blocking finding rather than inferring the
  result.

### 5. Reviewer

**Entry criteria**

- QA handoff exists and distinguishes observed from unobserved checks.
- The complete diff is available for inspection.

**Required outputs**

- Full-diff review, including unexpected or unrelated files.
- ADR conformance evidence for each applicable accepted decision.
- Findings with severity, blocking status, and actionable remediation.
- `recommendation.verdict`, which stays a recommendation with
  `approvalGranted: false`.
- Handoff to the human with `status: ready-for-human-approval`, or
  `status: changes-requested` with a rework handoff.

**Verdict consistency**

The verdict has to agree with the rest of the record; the schema and validator
reject a mismatch instead of trusting prose:

| Verdict | `taskStatusAfter` | Next actor | Findings |
| --- | --- | --- | --- |
| `accept` / `accept-with-findings` | `ready-for-human-approval` | human, `human-final-approval` | no open blocking finding |
| `fix-required` | `changes-requested` | builder, `rework` | at least one open blocking finding |
| `reject` | `rejected` | human | must not claim acceptance |
| `blocked` | `blocked` | human, `review` | a pending blocking human decision |

`fix-required`, `reject`, and `blocked` can never leave a task in
`ready-for-human-approval`. Only a reviewer handoff may request rework, and
rework returns control to the Builder rather than to the human.

**Failure and blocked outcomes**

- Blocking defect: `changes-requested` and a rework handoff to the Builder.
- Unverifiable claim: record it as unverified; do not accept a narrative.
- Conflicting requirement or ADR: request a human decision and keep the gate
  closed.

### 6. Human final approval

**Entry criteria**

- Reviewer handoff ends at the human.
- No blocking finding is open.
- Reviewer recorded that no approval was granted.

**Required outputs**

- Explicit human decision with a durable reference.
- `status: complete` on approval, or `status: rejected` / a rework handoff on a
  negative decision.

**Completion criteria**

A task is complete only when all of the following hold:

1. Every acceptance criterion is `addressed` or explicitly accepted as partial
   by the human.
2. No blocking finding or blocking human decision is open.
3. Required approvals are recorded with references, including a
   `human-final-approval` entry with `recorded: true`. Every status other than
   `complete` must leave that entry unrecorded.
4. The handoff chain is Architect → Builder → QA → Reviewer → human, and the
   final `taskStatusAfter` matches the task status.
5. `bash scripts/validate.sh --factory` passes for the repository state.

## Rework paths

| Trigger | Resulting record | Next actor |
| --- | --- | --- |
| QA finds a failing check | `validation-failed` handoff with observed failure | Builder |
| Reviewer requests changes | `changes-requested` handoff with findings | Builder |
| Builder needs different scope | Handoff stating the conflict; task returns to `awaiting-human-scope-approval` | Architect, then human |
| Requirements or ADR conflict | `blocked` handoff with the question | Human decision owner |
| Human rejects the change | `rejected` with rationale | Human |

Rework produces a new handoff record; earlier records stay in place so the
history remains auditable. Handoff identifiers increase monotonically, a task's
`handoffRefs` array lists the chain in order, and the validator rejects a chain
whose sequence numbers disagree with that order.

The permitted transitions are architect → builder, builder → qa, builder →
human, qa → reviewer, qa → human, reviewer → builder, and reviewer → human. A
chain that moves between any other pair of roles is reported as
`handoff-transition-not-permitted`, so a QA handoff cannot return directly to the
Builder and a Reviewer cannot forward to QA.

## What this is not

- It is not an orchestrator. No component polls these files, assigns work, or
  advances a status automatically.
- It is not approval. A complete, schema-valid contract cannot approve itself.
- It is not security enforcement. Static checks catch inconsistent
  configuration; they do not replace runtime authorization, review, or branch
  protection.
- It is not execution hooks. Nothing here runs when a file changes.

## Validation

```sh
bash scripts/validate.sh --factory
```

The command is read-only and returns a nonzero exit code for structural,
frontmatter, link, schema, cross-document, credential-like-content, handoff-chain,
and review-outcome problems.
The credential scan is a guardrail for accidental pastes, not a secrets scanner:
a clean run does not mean the repository is free of secrets. Documents under
`examples/_fixtures/` are parsed as JSON only so that deliberately invalid
fixtures can prove the failure paths; every real contract artifact stays fully
scanned.
See [the contracts README](../../.github/contracts/README.md) for the field rules
and the LoomSphere application's historical legacy reconciliation record
(`docs/workflows/legacy-agent-skill-reconciliation.md` in that repository) for the
mapping and removal of the superseded root-level definitions.

## Readiness for Phase 0.5b.2 (execution hooks)

This phase deliberately stops short of automation. The following properties make
a later hook layer possible without redesigning the contracts:

| Property | Where it lives | Why a hook layer needs it |
| --- | --- | --- |
| Deterministic validation command | `scripts/validate.sh --factory` | A hook can call one documented command with a known exit-code contract |
| Machine-readable status | `status` enum in both schemas | A hook can branch on state without parsing prose |
| Explicit evidence kinds | `evidenceRecord.kind` | A hook can refuse to advance on `asserted`, `blocked`, `not-run`, or `skipped` |
| Human approval markers | `requiredApprovals[].recorded` / `recommendation.approvalGranted` | A hook can detect that it must stop rather than approve |
| Stable identifiers | `taskId`, `handoffId`, `checkId`, `AC-*`, `R-*` | A hook can correlate files without heuristics |

Known gaps that a hook layer must close first:

1. **No execution entry point exists.** There is no script that runs a stage, so
   hook semantics would have to be defined before being automated.
2. **Evidence is self-declared.** The validator proves shape and consistency, not
   that a command was actually executed. Hooks would need to produce the evidence
   rather than accept an agent-written record.
3. **`references` are free text.** Human approval references are durable strings
   but not verified against a system of record.
4. **Transition enforcement is static only.** The validator now rejects a
   recorded chain that uses a transition the lifecycle does not permit, but
   nothing prevents an agent from writing a state that reaches the permitted
   chain by other means; only review and the human gate catch that. Enforcement
   is a property of the validator, not of the editing surface.

Until those gaps are closed, hooks must not be enabled. The current layer is
audit-oriented: it makes agent output consistent and machine-checkable while
leaving every decision, execution, and approval with a human or an explicitly
instructed agent.
