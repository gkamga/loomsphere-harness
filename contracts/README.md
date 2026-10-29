# Agent execution contracts

This directory defines the machine-readable records that LoomSphere agents and
humans exchange while working through the feature lifecycle. The contracts are
documents, not a control plane: writing a valid contract does not launch, queue,
schedule, or transfer work.

Read the lifecycle semantics in
[agent-execution-contracts.md](../../docs/workflows/agent-execution-contracts.md)
before producing either record.

## Files

| File | Purpose |
| --- | --- |
| [`task-contract.schema.json`](task-contract.schema.json) | Approved scope, boundaries, approvals, evidence, risks, and next action for one task |
| [`handoff-contract.schema.json`](handoff-contract.schema.json) | What one agent did, what it observed, what remains, and who acts next |
| [`execution-evidence.schema.json`](execution-evidence.schema.json) | Observed process evidence emitted by `scripts/factory_execute.py` for one allowlisted validation check; an input to contract evidence, not a contract |
| [`examples/LS-DOC-001/`](examples/LS-DOC-001/) | Complete Architect → Builder → QA → Reviewer example for a documentation-only task, at `1.1.0` |
| [`examples/LS-DOC-002/`](examples/LS-DOC-002/) | Six-handoff example at `1.2.0` that exercises a human scope gate, a `reviewer → qa` return, a repeat assessment, and a chain that ends blocked without approval |
| `examples/_fixtures/` | Deliberately invalid documents used to prove that validation fails; parsed as JSON only and exempt from every other check |

Both contract schemas use JSON Schema 2020-12. Both accept `contractVersion`
`1.1.0`, `1.2.0`, and `1.3.0`, and share one task-status vocabulary, one
lifecycle-stage vocabulary, and one evidence model. The validator rejects drift
between the shared definitions.

The third schema, [`execution-evidence.schema.json`](execution-evidence.schema.json),
is deliberately *not* a contract: it has its own artifact version, it is not part
of the task-status or lifecycle vocabularies, and it carries no approval fields.
It is validated by `scripts/factory_execute.py --verify` rather than by
`scripts/validate_factory.py`, and it cannot be used in place of a task or
handoff record. Read [Execution evidence](../../docs/workflows/execution-evidence.md)
for the trust boundary and the mapping into `evidenceRecord`.

## Contract versioning

`contractVersion` names one contract version. The schema publishes which versions
are still readable — currently `1.1.0`, `1.2.0`, and `1.3.0` — and every artifact must
declare exactly one of them. A range, a minimum, or an "or later" value is not
valid, because a record has to say which rules it was authored under. Any
incompatible change requires a new version string, a migration note in this file,
and updated examples. Adding optional fields is still a version bump unless the
change is additive and every existing example remains valid; decide explicitly
and record the decision in this file.

### Migrating 1.0.0 → 1.1.0

`1.1.0` is not additive: three properties became mandatory and two conditional
families became enforceable. Every `1.0.0` artifact fails `1.1.0` validation
until it is updated.

| Change | Action required |
| --- | --- |
| `evidenceRecord.outcome` is required | Add `outcome` to every validation record (`passed`, `failed`, `inconclusive`, `blocked`, or `not-executed`) |
| `kind` now constrains `outcome` | Set `blocked` for `kind: blocked` and `not-executed` for `kind: not-run`/`skipped` |
| `expectedExitCode` is required for a passing non-zero exit | Document deliberate failure-injection checks instead of silently passing them |
| `outcomeRationale` is required for non-obvious outcomes | Explain a non-zero passing exit, or any `failed`/`inconclusive` outcome |
| Human approval branches are enforced | `complete` requires a recorded `human-final-approval`; every other status must leave it unrecorded |
| Reviewer verdict branches are enforced | A verdict must agree with `taskStatusAfter`, `nextAction`, and the `findings` state |

The changed evidence semantics are listed under [Evidence versus assertion](#evidence-versus-assertion);
the reviewer rules are listed under [Review outcome rules](#review-outcome-rules).

### Migrating 1.1.0 → 1.2.0

`1.2.0` is additive. Both `1.1.0` and `1.2.0` are accepted by the schemas, and
the validator applies the rules of the version each record *declares*. It never
relabels a record and never upgrades one silently. This is deliberate: a version
number has to keep meaning what it says, or every earlier record becomes a claim
about today's rules rather than a record of yesterday's.

`1.1.0` remains the default for new records until a task owner adopts `1.2.0`
explicitly, because the new lifecycle makes a chain stricter, not looser.

| Change | Kind | Action required |
| --- | --- | --- |
| `contractVersion` accepts `1.1.0` or `1.2.0` | additive | None. Declare the version the record was actually authored against |
| `1.2.0` vocabulary is refused on a `1.1.0` record (`predecessorHandoffId`, `reassessment`, `executionState`) | guard | None for `1.1.0` records. A record that needs the new fields must declare `1.2.0` |
| `acceptanceCriteria[].verification` gains `cross-document-validation` | additive | Use it when a criterion can only be satisfied by two records agreeing |
| `repoFilePath` / `repoPathScope` accept `[` and `]` | additive | None. Bracketed Next.js route segments are now legal paths |
| `evidenceRecord.executionState` is available on `1.2.0` only | additive | Optional. Use it when execution happened but no exit status exists. A `1.1.0` record that sets it is rejected |
| `predecessorHandoffId` is available | additive | Set it on every `1.2.0` handoff except the first in the chain |
| `reassessment` is available | additive | Required when the same role reports twice in one chain |
| `architect → human` became a permitted transition | relaxation | Needed so a human scope gate can sit mid-chain |
| `reviewer → qa` became a permitted transition | relaxation | Needed so a re-verification cycle is representable |
| A block may stay inside the agent chain | relaxation | `blocked` no longer has to escalate to the human; see [Review outcome rules](#review-outcome-rules) |

Nothing was removed. A `1.1.0` record that passed before this version still
passes, unchanged, and `examples/LS-DOC-001/` is retained at `1.1.0` as the
regression baseline that proves it. `examples/LS-DOC-002/` is the `1.2.0`
counterpart: a six-handoff chain that places a human scope gate after the
architect, returns from the reviewer to QA for more evidence, declares both
repeat assessments, ends blocked without approval, and keeps a captured output
with no exit status from implying success. The two examples are validated as
separate record sets, so the mixed-version and reused-identifier cases that a
single tree would produce never arise.

### Migrating 1.2.0 → 1.3.0

`1.3.0` is additive and adds one role: the routed **security reviewer**, a
specialist reviewer that the factory selects from the approved scope, the risk
classification, and the recorded change set. It never replaces the reviewer; a
change that requires it needs both conclusions.

| Change | Kind | Action required |
| --- | --- | --- |
| `contractVersion` accepts `1.3.0` | additive | None. Declare the version the record was authored against |
| `agentRole` / `agentOrHuman` accept `security-reviewer` | additive | Use it only on a `1.3.0` record. A `1.2.0` record that names it fails its own version's transition rules |
| `qa → security-reviewer` became a permitted transition | relaxation | Needed so QA can hand evidence to a routed specialist |
| `security-reviewer → reviewer` became a permitted transition | relaxation | An approving specialist hands off to the final review |
| `security-reviewer → builder` became a permitted transition | relaxation | A specialist that requires changes returns the work, like the reviewer |
| `security-reviewer → human` became a permitted transition | relaxation | A specialist that blocks escalates; it cannot approve or close |
| `reviewer → security-reviewer` became a permitted transition | relaxation | The final reviewer may request specialist re-verification |
| The blocked-outcome consistency rule applies to `1.3.0` as well | guard | Unchanged behaviour. `1.3.0` inherits the `1.2.0` structural rule rather than dodging it |

Nothing was removed and no `1.1.0` or `1.2.0` rule changed. `1.2.0` remains a
valid version for new records; `1.3.0` is the version that can express a chain
through a specialist reviewer. The bundled examples remain at their original
versions, which is what keeps them meaningful as regression baselines.

`executionState` is the one additive field that would *weaken* a `1.1.0` record,
because it is the escape hatch that lets an `observed` entry omit `exitCode`. It
is therefore refused on `1.1.0` in both schemas, at each property that reaches
the shared `evidenceRecord` definition (`validation[]` on a handoff,
`executionEvidence.validations[]` on a task). The gate cannot sit inside
`$defs.evidenceRecord` itself: JSON Schema draft 2020-12 has no cross-subschema
data reference, so a definition shared by two schemas cannot read the parent
record's `contractVersion`. The validator restates the same rule as
`contract-version-vocabulary` so the failure is named rather than anonymous.

#### `executionState`

`executionState` records what the process itself did, kept separate from both the
semantic `outcome` and from `exitCode`:

| Value | Meaning | `exitCode` |
| --- | --- | --- |
| `exited` | The process ran to completion | required |
| `timed-out` | The process was killed at a deadline | forbidden |
| `launch-failed` | The process never started | forbidden |
| `exit-not-captured` | Execution was attempted and output was retained, but no exit status was captured | forbidden |

`exit-not-captured` exists because "the output exists, the status was lost" is a
real state that `observed` could not express: `observed` demanded an `exitCode`,
so an agent either invented one or downgraded a real run to `asserted`. The new
value is honest about the gap and **never implies a zero exit**. An `observed`
record still requires an `exitCode` *or* one of the three non-exit
`executionState` values, and a record with any of them cannot carry
`exitCode` or `expectedExitCode`.

#### `predecessorHandoffId` and `reassessment`

A chain could previously only be reconstructed by trusting array order and
timestamp rounding — both fragile, and neither checkable. In `1.2.0` every
handoff except the first states `predecessorHandoffId`, the validator requires it
to equal the handoff that actually precedes it, and the first handoff must not
declare one (`handoff-predecessor-mismatch`).

`reassessment` declares that a role is reporting for the second or later time in
the same chain. Without it, a repeated role is indistinguishable from a
duplicated handoff or a broken chain. The block is `iteration` (count of reports
by this role, counting this one), `triggeredByHandoffId` (an earlier handoff in
the same chain), and `basis` (what new evidence the second report rests on —
re-running the same checks on the same revision is not a new basis). The
validator reports `handoff-reassessment-missing` for an undeclared repeat and
`handoff-reassessment-mismatch` for a declared one that does not match the
chain.

## Identifier formats

Identifiers are schema-enforced; an artifact with a malformed id is a violation,
not a style issue.

| Field | Pattern | Valid | Invalid |
| --- | --- | --- | --- |
| `taskId` | `^[A-Z][A-Z0-9]*-[A-Z0-9]+(-[A-Z0-9]+)*$` | `LS-DOC-001`, `LS-DOC-002` | `ls-doc-001`, `LS-DOC-` |
| `projectId` | `^[a-z0-9][a-z0-9-]{1,62}$` | `loomsphere`, `agent-factory` | `LoomSphere`, `-loomsphere`, `a` |
| `handoffId` | `^HND-[0-9]{3}$` | `HND-001`, `HND-104` | `HND-1`, `HND-1004`, `hnd-001` |
| `handoffRefs[].handoffId` | same as `handoffId` | `HND-002` | `HND-2` |
| `findings[].id` | `^FND-[0-9]{2}$` | `FND-01` | `FND-1`, `FND-001` |
| `acceptanceCriteria[].id` | `^AC-[0-9]{2,3}$` | `AC-01`, `AC-001` | `AC-1`, `AC-0001` |
| `checkId` | `^[a-z0-9][a-z0-9-]{1,63}$` | `backend-safe-tests` | `Backend_Tests`, `x` |

Handoff ids are numbering, not identity. The validator requires each task's
`handoffRefs` entries to be listed in chain order and the embedded sequence
numbers to increase across that order, so `HND-001` … `HND-004` is valid while a
reversed list of the same ids is not. Multiple tasks must use disjoint number
ranges: `LS-DOC-001` uses `HND-001`–`HND-004`, and a second example starts a new
range such as `HND-101`–`HND-104` rather than reusing `HND-001`.

Finding and decision ids are record-local. `findings[].id` and
`humanDecisionsRequired[].id` are unique only inside one handoff, so the same
`FND-NN` can mean different things in different handoffs of one chain — the
Phase 0.6 record set carries 64 findings that share only 30 distinct ids. The
schemas define no cross-record finding reference and the validator never joins
findings across handoffs, so a reference from outside a record must be
handoff-qualified: write `HND-105/FND-12`, not `FND-12`. A bare finding or
decision id in a report or review is ambiguous and must not be read as naming a
single record.

## Task status vocabulary

Unknown or misspelled values are schema violations. A consumer must treat a
missing, invalid, or unrecognized status as **not successful**.

| Status | Meaning | Set by |
| --- | --- | --- |
| `draft` | Scope is being shaped; no approval exists | Architect |
| `awaiting-human-scope-approval` | Architecture handoff is complete and waiting for a human | Architect |
| `approved` | A human approved the bounded scope | Human only |
| `in-implementation` | Approved work is being implemented | Builder |
| `implemented` | Implementation is complete and awaits verification | Builder |
| `in-validation` | Independent verification is underway | Builder or QA |
| `validation-failed` | A required check failed; rework is needed | QA |
| `in-review` | Verification is complete and review is underway | QA |
| `changes-requested` | Review requires rework before approval | Reviewer |
| `ready-for-human-approval` | Review is complete and a human decision is pending | Reviewer |
| `complete` | A human recorded final approval | Human only |
| `rejected` | A human rejected the change | Human only |
| `blocked` | Progress requires a decision or unblocking action | Any agent or human |

`metadata.executionMode` is fixed to `manual-handoff`. It records explicitly
that no orchestrator performed the transfer.

## Evidence versus assertion

`evidenceRecord.kind` records how a result was obtained; `outcome` records what
the result means. The two are independent on purpose, and the schema keeps them
consistent:

| Kind | Required content | Meaning |
| --- | --- | --- |
| `observed` | `command`, `exitCode` **or** a non-exit `executionState`, `executedAt`, `executor`, `evidenceRef`, plus `outcome` in `passed`/`failed`/`inconclusive` | A check actually ran and its captured output is inspectable |
| `asserted` | `summary` and an `outcome` that is not `blocked`/`not-executed`; all execution metadata is forbidden | The agent believes a result without captured execution |
| `blocked` | `outcome: blocked`; all execution metadata is forbidden | The check was refused or impossible under safety rules |
| `not-run` | `outcome: not-executed`; all execution metadata is forbidden | The check was intentionally skipped |
| `skipped` | `outcome: not-executed`; all execution metadata is forbidden | The check did not apply |

`continuousIntegration.claim` is `not-claimed` unless the record also carries a
GitHub Actions run URL in `observedRunUrl`. A written statement that CI passed
is an assertion; it is never treated as an observed run.

## Exit codes versus outcomes

An exit code is process metadata. An outcome is the assertion the executor is
making about the check. `exitCode: 0` never implies `passed`, and `passed` may
legitimately carry a non-zero exit code when the check itself asserts failure.

| Exit code | `outcome` | Extra requirement |
| --- | --- | --- |
| `0` | `passed` | none |
| `0` | `failed` | `outcomeRationale` |
| non-zero | `failed` | `outcomeRationale` |
| non-zero | `passed` | `expectedExitCode` equal to the intended failure exit code **and** `outcomeRationale` |
| any | `inconclusive` | `outcomeRationale` |
| absent | `blocked` / `not-executed` | no execution metadata at all |

When `expectedExitCode` is present the validator requires a recorded `exitCode`
and rejects any mismatch with `evidence-exit-code-mismatch`. Enforcing equality
never changes the outcome: a matching code does not make a check `passed`, and
the `outcomeRationale` requirement for a successful negative test still applies.

The only sanctioned way to record a passing non-zero exit is a documented
negative test: `handoff-03-qa.json` in the example carries
`checkId: negative-validation-demonstration`, `exitCode: 1`,
`expectedExitCode: 1`, `outcome: passed`, and an `outcomeRationale` explaining
that the non-zero exit *is* the asserted signal. Removing `expectedExitCode`
from that record makes the artifact invalid instead of quietly permissive.

## Review outcome rules

A reviewer verdict must agree with the rest of the handoff. The schema enforces
this matrix and the validator reports `review-outcome-inconsistent` when a
document bypasses it:

| `recommendation.verdict` | `taskStatusAfter` | `nextAction` | Findings |
| --- | --- | --- | --- |
| `accept` / `accept-with-findings` | `ready-for-human-approval` | `human` / `human-final-approval` | no open blocking finding |
| `fix-required` | `changes-requested` | `builder` / `rework` | at least one open blocking finding |
| `reject` | `rejected` | `human` / human decision stage | must not claim acceptance |
| `blocked` | `blocked` | `human` / review stage (1.1.0); `human`, `qa`, or `reviewer` / review stage (1.2.0) | a pending blocking human decision **or** an open blocking finding |

Two negative rules follow from the matrix and are also checked independently:

- `fix-required`, `reject`, and `blocked` can never produce
  `ready-for-human-approval`, whatever else the document says.
- A handoff in `ready-for-human-approval` may not carry an open blocking finding
  (`blocking-finding-blocks-approval`).

Only a reviewer handoff may use `fix-required`, and a rework handoff must return
control to the builder rather than to the human.

In `1.1.0` only a reviewer handoff could record `blocked`, because the rule was
written as a reviewer verdict branch. That made an agent-initiated block
unwritable for every other role, and it forced every block to be escalated past
the agent that found it. `1.2.0` replaces that branch with a role-agnostic one:
any role that discovers a missing decision or artifact may block, but
(`review-outcome-inconsistent`):

- the task must be left `blocked`;
- the block must not be routed to `human-final-approval`;
- the block must go to the human, or back to a role that can gather the missing
  evidence — never to the builder as though the implementation were wrong;
- it must be grounded in a pending blocking human decision or an open blocking
  finding;
- if it stays inside the agent chain, it must declare a `reassessment` and name
  an open blocking finding.

The `1.1.0` reviewer-only branch is retained unchanged and pinned to
`contractVersion: 1.1.0`, so a `1.1.0` record is still judged by the rule it was
written under.

## Handoff chains

A task's `handoffRefs` array is the chain, in authored order. The validator
checks that:

- every handoff artifact names a `taskRef.taskId` that exists, and that the
  `taskRef.path` points at that task's own `task.json`;
- every referenced id exists once, and every handoff that names a task is also
  referenced by it (`handoff-unreferenced`);
- consecutive references connect (`destinationAgent` equals the next
  `sourceAgent`) and use a lifecycle transition the factory permits —
  architect → builder, builder → qa, builder → human, qa → reviewer, qa → human,
  reviewer → builder, reviewer → human;
- sequence numbers increase in reference order;
- a task in `ready-for-human-approval` or `complete` ends its chain at `human`.

`1.2.0` adds two transitions and two structural checks:

| Transition | Why it exists |
| --- | --- |
| architect → human | The architect may complete architecture and hand the scope decision to a human before any implementation starts. Without it, a human scope gate could not be placed in the middle of a chain at all |
| reviewer → qa | A reviewer may send work back for re-verification rather than for rework. Without it, the only way to represent "the reviewer needs more evidence" was to misdescribe it as builder rework |

Both are additions; no `1.1.0` transition was withdrawn or narrowed, and
`qa → builder` remains forbidden because a failed validation is rework and must
be routed as `fix-required`.

A seam where the previous handoff ends at `human` and addresses the human is a
**pause for a decision**, not a broken chain, so `handoff-chain-broken` is not
reported there. The next handoff must still start from the role that resumes the
work, and if the pending human decision is blocking, the resuming handoff may not
declare a status at or past human approval (`handoff-resume-before-decision`).

Ordering is a property of the record sequence, never of timestamps. Two handoffs
created in the same minute must not be ordered by their `createdAt` values, and a
chain must not rely on clock resolution to be readable.

## Approval boundaries

- `requiredApprovals[].approver` is the constant `human`.
- `requiredApprovals[].recorded` may be `true` only with a durable `reference`.
- `requiredApprovals[].recorded` is a human action; agents must not set it.
- `recommendation.approvalGranted` is the constant `false`. Reviewer output is a
  recommendation, never an approval.
- Agents have no authority to approve ADRs, commit, push, merge, deploy, or
  override a failed gate. See [ADR governance](../../docs/decisions/README.md).
- An execution evidence record from `scripts/factory_execute.py` grants no
  approval either. It records that one allowlisted command ran and what exit
  status it produced; it never sets `recorded`, `approvalGranted`, `status`, or a
  lifecycle stage. See [Execution evidence](../../docs/workflows/execution-evidence.md).

## Sensitive content

Task and handoff artifacts must not contain credentials, tokens, private keys,
or credentialed connection URLs. The validator applies a heuristic scan for
credential-like field names and values.

### Scan scope (non-claim)

The scan is a guardrail for accidental pastes, not a security control and not a
secrets scanner. It does not evaluate entropy, does not recognise every provider
format, does not inspect git history, and cannot detect a secret encoded in a
shape it has no pattern for. A clean run means "no obvious credential string in
the files this validator reads", nothing more. Reviewers must still read the
diff, and secret management must happen outside the repository.

### Fixtures

`examples/_fixtures/` holds deliberately invalid documents used to prove that
the contracts fail correctly. The validator parses those files as JSON only: it
skips schema conformance, link, and credential checks for them. Every other
artifact under `examples/` remains fully scanned, so a credential pasted into a
real example is still reported.

Never place secrets in a contract to make a check pass, including in a fixture.

## Produce and validate

1. Create `examples/<TASK-ID>/task.json` from the task schema.
2. Append one handoff file per stage, numbered so lexical order matches the
   agent sequence, and list them in `handoffRefs` in that same order.
3. Run the read-only validator from the repository root:

   ```sh
   bash scripts/validate.sh --factory
   ```

   The equivalent direct invocation is:

   ```sh
   cd backend
   uv run python ../scripts/validate_factory.py
   ```

4. Treat a nonzero exit code as a failing gate. The validator checks structure,
   frontmatter, links, schema conformance, cross-document integrity, handoff
   chain ordering and transition legality, review outcome consistency, and
   credential-like content, and it never writes files.

### Validating records outside the bundled examples

A pass over `examples/` says nothing about records that live elsewhere, so the
validator can be pointed at additional record sets:

```sh
cd backend
uv run python ../scripts/validate_factory.py --records-root ../reports/phase-0.6d/reconciled/LS-TASK-001
```

`--records-root` is repeatable. Each supplied directory is validated as its own
record set with the same rules as the examples: ids only have to be unique inside
the set they belong to, and references resolve inside that set first. A
validation pass only ever describes the roots it actually walked, which is why
every run prints one `SCAN  <path>` line per root — in text mode before the
findings, and in JSON mode as `scanRoots`. **A pass that does not name a root has
not checked it.** Do not read a repository-wide PASS into records that were never
scanned.

A record set is a directory, not a tree. Every directory that directly holds
contract files is validated on its own, which is why `examples/LS-DOC-001/` and
`examples/LS-DOC-002/` are two sets rather than one mixed set: they declare
different contract versions and reuse the same identifier ranges, and both are
legitimate. Grouping by directory is what keeps two tasks that happen to share a
parent from being judged against each other's identifiers. `examples/_fixtures/`
is never a set — its documents are exempt from validation by design — but a
fixture that cannot be parsed as JSON still fails the run, because a fixture that
is not readable stops proving anything.

Path resolution has two bases, applied in order:

1. the directory that holds the records, because that is how an agent addresses a
   sibling record (`task.json`, `handoff-01-architect.json`);
2. the repository root, because that is how an agent addresses product code
   (`frontend/lib/api/tasks.ts`).

Both bases are safe because the schemas forbid absolute paths and
parent-directory traversal, so neither can escape the repository. A reference
that resolves against neither is reported as `handoff-file-missing` rather than
being silently dropped. An `evidenceRecord.evidenceRef` is **not** resolved: an
evidence artifact may live outside the repository on the operator's host, and the
validator does not claim to have read it.

## Extension slot

Top-level `x-*` keys are allowed for local annotation. They are never workflow
control, never evidence, and never approval.
