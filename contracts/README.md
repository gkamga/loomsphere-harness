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
| [`examples/LS-DOC-001/`](examples/LS-DOC-001/) | Complete Architect → Builder → QA → Reviewer example for a documentation-only task |
| `examples/_fixtures/` | Deliberately invalid documents used to prove that validation fails; parsed as JSON only and exempt from every other check |

Both schemas use JSON Schema 2020-12. Both declare `contractVersion: 1.1.0` and
share one task-status vocabulary, one lifecycle-stage vocabulary, and one
evidence model. The validator rejects drift between the shared definitions.

## Contract versioning

`contractVersion` is an exact value, not a range. Any incompatible change
requires a new version string, a migration note in this file, and updated
examples. Adding optional fields is still a version bump unless the change is
additive and every existing example remains valid; decide explicitly and record
the decision in this file.

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
| `observed` | `command`, `exitCode`, `executedAt`, `executor`, `evidenceRef`, plus `outcome` in `passed`/`failed`/`inconclusive` | A check actually ran and its captured output is inspectable |
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
| `blocked` | `blocked` | `human` / review stage | a pending blocking human decision is required |

Two negative rules follow from the matrix and are also checked independently:

- `fix-required`, `reject`, and `blocked` can never produce
  `ready-for-human-approval`, whatever else the document says.
- A handoff in `ready-for-human-approval` may not carry an open blocking finding
  (`blocking-finding-blocks-approval`).

Only a reviewer handoff may use `fix-required`, and a rework handoff must return
control to the builder rather than to the human.

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

## Approval boundaries

- `requiredApprovals[].approver` is the constant `human`.
- `requiredApprovals[].recorded` may be `true` only with a durable `reference`.
- `requiredApprovals[].recorded` is a human action; agents must not set it.
- `recommendation.approvalGranted` is the constant `false`. Reviewer output is a
  recommendation, never an approval.
- Agents have no authority to approve ADRs, commit, push, merge, deploy, or
  override a failed gate. See [ADR governance](../../docs/decisions/README.md).

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

## Extension slot

Top-level `x-*` keys are allowed for local annotation. They are never workflow
control, never evidence, and never approval.
