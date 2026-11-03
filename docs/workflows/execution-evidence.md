# Execution evidence

This document defines the Phase 0.5b.2 execution-evidence layer: what
`scripts/factory_execute.py` does, what its artifact proves, what it does not
prove, and how a human or agent is allowed to turn it into contract evidence.

The layer records facts about one local process run. It is **not** an
orchestrator, scheduler, queue, approval service, CI replacement, or security
control. Nothing here changes the lifecycle in
[agent-execution-contracts.md](agent-execution-contracts.md), and nothing here
changes the `1.1.0` contract semantics documented in
[../../.github/contracts/README.md](../../.github/contracts/README.md).

## What problem this solves

Before this increment, an agent that claimed "the factory validator passed" was
producing an assertion. Nothing in the repository distinguished that claim from
a real run. The runner closes exactly that gap for a small, fixed set of checks:

1. it refuses every command that is not explicitly allowlisted;
2. it executes the allowlisted command itself;
3. it records the exit status, timing, and bounded output digests it observed;
4. it writes one artifact to a path outside the repository.

The result is a record whose process facts were produced by the repository's own
code rather than typed by an agent. It is still not authoritative, because the
artifact is an ordinary local file. See [Trust boundary](#trust-boundary).

## Allowlist

The allowlist is a frozen table in `scripts/factory_execute.py`. It has no
discovery mechanism, no glob, no environment override, and no plugin hook.

| Check id | Runs | Working directory | Expected exit | Timeout |
| --- | --- | --- | --- | --- |
| `factory-contract-validation` | `bash scripts/validate.sh --factory` | repository root | `0` | 600s |
| `backend-safe-tests` | `bash scripts/validate.sh --backend` | repository root | `0` | 900s |

Rules enforced by the runner:

- The caller passes a **check id**, never a command. There is no `--command`,
  `--shell`, or free-text argument, and `shell=True` is never used.
- The argument vector comes from the module-level table. A caller cannot append,
  reorder, or substitute arguments.
- `--timeout` can only *tighten* the allowlisted timeout. A larger value is
  clamped, so a caller cannot disable the guard.
- Reserved identifiers are rejected even when they are not in the table. Any id
  whose tokens include `all`, `db`, `database`, `migrate`/`migration(s)`,
  `deploy`/`deployment`, `e2e`, or `playwright` is refused with an explicit
  message. This makes the exclusion of database, migration, end-to-end,
  deployment, and unrestricted `--all` routes a stated policy rather than a
  consequence of an absent entry.
- Unknown ids are refused with exit code `2` before any process starts.
- `--list` prints the table without executing anything.
- Execution is confined to the **canonical checkout**. `--repo-root` must resolve
to the repository that contains `scripts/factory_execute.py`; any other root is
refused with exit code `2` before a process starts. `shell=False` prevents shell
interpretation of the argument vector, but it does **not** make
`scripts/validate.sh` trustworthy by itself: `bash` reads and executes that
file's content. Rejecting alternate roots is what prevents executable-content
substitution.

Both allowlisted checks delegate to the existing `scripts/validate.sh`, which
already owns the mode semantics, the database-isolation gate, and the pass/fail
rules. The runner does not reimplement factory validation, so there is exactly
one definition of each gate.

## Artifact

One execution produces one JSON document validated by
[`execution-evidence.schema.json`](../../.github/contracts/execution-evidence.schema.json)
(`artifactVersion: 1.0.0`). The runner refuses to write a document that fails
its own verification.

| Section | Records |
| --- | --- |
| `runner` | Runner name, version, implementation path, and `provenance: runner-observed-process-execution` |
| `check` | Allowlisted id, category, exact `argv` list, quoted display command, declared working directory, expected exit code, effective timeout |
| `repository` | Full commit sha and dirty-worktree flag from read-only `git rev-parse` / `git status --porcelain`, or `revisionSource: unavailable` with nulls |
| `process` | Start/completion timestamps, duration, exit code, `termination`, `timedOut`, launch error, and a digest + bounded redacted summary for each stream |
| `result` | `kind`, `outcome`, and `outcomeRationale` |
| `authority` | Every grant field constant `false`, plus `authenticated: false` |
| `limitations` | Non-empty list of what the record cannot establish |

Design choices worth knowing:

- **Exit code is a fact, not a verdict.** `result.outcome` only says whether the
  observed exit status matched the status the check declares. It never claims an
  acceptance criterion is satisfied.
- **A timeout is never a pass.** `termination: timeout` forces
  `timedOut: true`, `exitCode: null`, and `outcome: inconclusive`.
- **A launch failure carries no result.** `termination: launch-error` also has
  `exitCode: null` and `outcome: inconclusive`, with the OS error described.
- **Streams are digested, not trusted.** Output is written to a temporary file,
  hashed over the full stream, and only a bounded tail summary is stored. The
  summary is redacted for credential-shaped patterns. `truncated: true` means
  the digest covers more bytes than the stored text.
- **The environment is filtered.** Credential-shaped variable names
  (`password`, `token`, `secret`, `api_key`, `credential`, `database_url`, and
  similar) are removed from the child environment, as is
  `LOOMSPHERE_DB_ISOLATION_VERIFIED`, so an inherited flag cannot authorise a
  database check. No environment value is written to the artifact.
- **Output paths are constrained.** The destination must be absolute, end in
  `.json`, not contain `..`, not be a symlink, not already exist, live in an
  existing directory, and resolve outside the repository working tree. The
  runner never creates directories and never overwrites a file.

## Trust boundary

**In scope.** The runner makes an agent's claim falsifiable. If an artifact
exists and verifies, a check really ran with those arguments at that time and
produced that exit status, *assuming the runner binary and the host were not
tampered with*.

**Out of scope.** The artifact is a plain local JSON file. Concretely:

- It is **not tamper-proof.** Any process that can write the file can edit it.
- A digest stored **inside the same file** does not authenticate that file. There
  is no signature, no key, no attestation, and no transparency log. Do not
  describe the digests as proof of integrity.
- `--verify` performs **structural** verification: field shapes, allowlist
  membership, and internal consistency. It cannot establish that a real process
  ran or that its recorded facts are true.
- `authenticated: false` and `grantsApproval: false` are honest constants, not
  claims about a verification ceremony.
- Truncated summaries are not evidence of absence. A string missing from a
  400-character summary tells you nothing about the full standard output.
- A passing check covers only what that check covers. `backend-safe-tests` is a
  subset of backend tests, and neither check exercises Playwright, migrations,
  or any deployed environment.

- Anyone consuming an artifact must therefore ask: was this produced by a trusted
runner on a trusted host, in the intended revision? In practice that means
treating the artifact as **untrusted input** and confirming the revision and
context before relying on it. CI remains the authority for checks that must be
trustworthy, because CI controls both the runner and the host.
- The runner, the canonical checkout's script contents, and the executables on
  the caller's `PATH` are trusted. `bash`, `uv`, and the interpreter behind
  `scripts/validate.sh` are resolved from that environment. A caller who can
  alter the checkout or the environment has already left the boundary this layer
  defends, and an artifact cannot prove otherwise.

## Mapping into contract evidence

An artifact is evidence *input*. It does not become a contract field
automatically, and no script rewrites a contract.

For a completed execution (`termination: exited`), map to one
`evidenceRecord` with `kind: observed`:

| `evidenceRecord` field | Source |
| --- | --- |
| `checkId` | `check.checkId` |
| `category` | `check.category` (`pytest` or `static-validation`, both in the contract vocabulary) |
| `kind` | `"observed"` |
| `outcome` | `result.outcome` |
| `command` | `check.displayCommand` |
| `exitCode` | `process.exitCode` |
| `expectedExitCode` | `check.expectedExitCode` |
| `outcomeRationale` | `result.outcomeRationale` |
| `executedAt` | `process.startedAt` |
| `executor` | `"factory_execute.py <runner.version>"` |
| `evidenceRef` | Repository-relative or absolute path to the artifact |
| `summary` | `result.outcome` plus `check.checkId`, bounded |

For a **failed** outcome, `outcomeRationale` must carry the observed exit status
and the expected one. For a **passed** outcome with a nonzero
`expectedExitCode`, the contract requires `outcomeRationale` — the runner already
records the expectation explicitly, so copy both sides rather than asserting
"it passed".

For an execution that attempted to run but produced no exit status, the artifact
still records what happened. Read `termination` before choosing a contract kind:

| `process.termination` | Artifact facts | Was execution attempted? |
| --- | --- | --- |
| `timeout` | `timedOut: true`, `exitCode: null`, `result.kind: observed`, `result.outcome: inconclusive` | Yes. The process started and was terminated at the timeout. |
| `launch-error` | `launchError` set, `exitCode: null`, `result.kind: observed`, `result.outcome: inconclusive` | No. No child process was created. |

Contract `1.1.0` has no record that means "attempted, but no exit status":
`kind: observed` requires `command`, `exitCode`, `executedAt`, `executor`, and
`evidenceRef`, and `asserted` is the only non-observed kind that may carry
`outcome: inconclusive`. Because inventing an exit code is exactly the failure
this layer exists to prevent, map both terminations to:

- `kind: asserted` with `outcome: inconclusive`, no `command`, `exitCode`,
  `expectedExitCode`, `executedAt`, `executor`, or `evidenceRef`, and an
  `outcomeRationale` that states the termination reason, the `executionId`, and
  the artifact path. Here `asserted` describes what the contract record can
  carry, not whether the runner observed the attempt; the artifact remains the
  captured record.

Rules that follow from the distinction above:

- **A timeout is execution attempted with an inconclusive check outcome.**
  Never map a timeout to `not-run`/`not-executed`: those values mean the check
  was not attempted (intentionally deferred, not applicable, or refused before
  launch).
- **A launch failure is not completed process execution.** The allowlisted
  command never started, so do not map it to `observed` and do not describe it
  as a completed run. Use the same `asserted`/`inconclusive` shape with an
  explicit `launch failed: <reason>` rationale.
- **`blocked`/`blocked` is for refusal, not time.** Reserve it for a safety gate
  that refused the check before launch. A timeout was not refused.
- **Termination detail stays in the artifact.** The contract record carries
  prose only; `process.termination`, `process.timedOut`, `process.launchError`,
  stream digests, and output summaries remain available in the artifact named by
  the rationale. `not-run` and `skipped` records carry no artifact reference at
  all, which is another reason not to use them here.
- **No approval is inferred.** An `asserted`/`inconclusive` record does not
  record a human approval, does not set `requiredApprovals[].recorded`, and does
  not mark a task `approved` or `complete`. The runner's `authority` fields
  remain `false`.

An `asserted`, `blocked`, or `not-run` record must still never carry an exit
code, and the presence of an artifact never upgrades an agent's belief into an
observation.

### Known gaps in the mapping

These are accepted limits of this increment, not defects hidden by it. Closing
any of them changes contract semantics and therefore needs an approved decision
first; do not patch the schemas to work around them.

- **Revision context does not travel.** `evidenceRecord` has no revision or
  worktree field, so an artifact produced from a dirty tree cannot tell the
  reader that the passing check may not correspond to the commit it cites.
  Restate the commit and dirtiness in `summary` when it matters.
- **No run identity.** `evidenceRecord` cannot carry `executionId` or an
  artifact digest, so two artifacts for the same `checkId` are
  indistinguishable. Include the `executionId` in `summary`.
- **`evidenceRef` durability is a convention.** Nothing validates that the path
  still exists, and the sanctioned usage writes to a temporary directory. Keep
  artifacts that a contract depends on.
- **Termination detail is dropped.** `timedOut`, `termination`, and
  `launchError` have no contract home, so a timeout or launch failure can only
  be recorded as `asserted`/`inconclusive` prose. Adding an
  attempted-without-exit kind changes contract semantics and requires an
  approved contract version first.
- **`executor` is a version string, not an identity.** No runner hash, host, or
  toolchain digest is recorded, so environment drift is invisible downstream.
- **Degraded revision has no rule.** `revisionSource: unavailable` still maps to
  `kind: observed`. Treat such an artifact as weaker evidence and say so.
- **`limitations` does not propagate.** The artifact's caveats and truncation
  flags stop at the artifact; carry the material ones into `summary` yourself.

## Authorization boundaries

An execution record grants **nothing**. Specifically it does not:

- set `requiredApprovals[].recorded` or any human approval;
- mark a task `approved` or `complete`;
- move a handoff chain, write a handoff, or promote a lifecycle stage;
- approve, supersede, or reinterpret an ADR;
- authorise a commit, push, merge, tag, release, or deployment;
- override a failing gate or a `blocked` record;
- authorise a database, migration, end-to-end, deployment, or `--all`
  validation route.

The runner cannot stage, commit, push, open a pull request, or write into the
repository working tree at all. Its single write is the artifact at the caller's
out-of-tree path.

## Using it

List the allowlist without executing anything:

```sh
uv run --project backend python scripts/factory_execute.py --list
```

Run one check and write the artifact outside the repository:

```sh
ARTIFACT_DIR="$(mktemp -d)"
uv run --project backend python scripts/factory_execute.py \
  --check factory-contract-validation \
  --output "$ARTIFACT_DIR/factory-validation.json"
```

Re-verify an existing artifact:

```sh
uv run --project backend python scripts/factory_execute.py --verify "$ARTIFACT_DIR/factory-validation.json"
```

Exit codes: `0` passed (or artifact valid), `1` check executed and failed
(the artifact is still written), `2` invocation/allowlist/path refusal, `3`
artifact failed verification.

## Verification

`backend/tests/test_factory_execute.py` covers the allowlist refusals, the
output-path guards, the schema/enumeration agreement, timeout and launch-error
handling, redaction and summary bounds, environment filtering, the structural
verifier's contradiction rules, and the authority constants. The `backend-safe`
CI job runs those tests and one real allowlisted execution, then verifies the
produced artifact, so the vertical slice is exercised on a clean host.
