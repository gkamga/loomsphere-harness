# Standalone runtime (v0.1.0)

`loomsphere-harness` is the portable runtime and a minimal CLI for one workflow:

```
Intake → Architect → recorded human approval → Builder → validation
      → QA → Reviewer → recorded human acceptance
```

Every stage is a real command; nothing depends on a LoomSphere frontend, backend,
or control-plane database. State is one SQLite file outside the target repository,
and the only model call boundary is the `AgentRunner` seam. The loop stops at a
human acceptance decision: it never mirrors a push, opens a pull request, or merges.

Phase 2C.1 shipped Intake → Architect → approval. Phase 2C.2 adds the
build/verify/review loop and delivery acceptance on top of the same runtime.
Phase 2C.3 adds recovery of a failed Builder's preserved work and this phase adds
the one-time restart of a Builder that wrote nothing; the store is now schema v4
and migrates an existing v1, v2, or v3 state file in place.

## Install

Requirements: Python 3.11+, Git, and Docker or Podman for `implement`. The model
is your own provider; there is no bundled endpoint, model, or credential.

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install .               # from this checkout; Python >= 3.11
# or a locally built wheel: pip install dist/loomsphere_harness-0.1.0-py3-none-any.whl
loomsphere-harness --help   # or: python -m loomsphere_harness
```

`pip install -e .` works for development. The package is **not** published to
PyPI, so `pip install loomsphere-harness` will not resolve; install from a
checkout or from a wheel built with `python -m build`.

Dependencies: `pydantic>=2.11`, `pydantic-ai-slim[openai]>=2.64.0`,
`pydantic-ai-harness>=0.64.0` (dev extra: `PyYAML`, `jsonschema`, for the asset validators).

## Configure (outside the source)

A TOML file, found via `--config`, `$LOOMSPHERE_HARNESS_CONFIG`, or
`~/.config/loomsphere-harness/config.toml`. There is no default endpoint, model, or
provider; with none configured every model run is refused and recorded.

```toml
[harness]
root = "/opt/loomsphere-harness"     # trusted checkout, NOT inside the target repo

[model]
provider = "openai-compatible"
base_url = "https://openrouter.ai/api/v1"
model_id = "deepseek/deepseek-v4.1-flash-1017"   # any model the endpoint serves
approved_hosts = ["openrouter.ai"]               # the endpoint host must be listed
api_key_env = "LOOMSPHERE_MODEL_API_KEY"         # NAME of the env var holding the key
# api_key_file = "/path/to/key"                  # alternative; never an inline key
# require_api_key = false                        # local servers only
# request_limit, tool_call_limit, total_token_limit, max_output_tokens, timeout_seconds
# [model.role_limits.<role>]                     # optional per-role narrowing; see below

[state]
# dir = "/path/outside/repo"   # default: ~/.local/share/loomsphere-harness/projects/<hash>

[workspace]
# root = "~/.local/state/loomsphere-harness/workspaces"
# default: a "workspaces" directory beside the state file; never inside the repo

[sandbox]                                    # required before any implementation run
image = "loomsphere-pytest:python3.12"         # must exist locally; pinned by sha256 id per run
# runtime = "docker"                         # container runtime only: docker or podman
# cpus = 2.0, memory_mb = 2048, pids_limit = 256, tmpfs_mb = 256
# timeout_seconds = 300, startup_grace_seconds = 30
# network = "none"                           # any other value is refused

[validation]
# allowlist = [["python", "-m", "pytest", "-q"]]   # narrows the built-in allowlist
# timeout_seconds = 900, excerpt_chars = 4000

[implementation]
# max_iterations = 2                         # Builder attempts before the loop fails
```

For Python acceptance selectors, configure a local image with pytest already
installed. The repository provides one:

```
docker build -t loomsphere-pytest:python3.12 docker/pytest-sandbox
```

The image build is the only step that needs package access. Implementation-time
validation runs with `--network none`; the runtime never installs pytest or
substitutes a different test command. It probes pytest in the configured image,
collects each approved selector, then executes that exact selector in the same
read-only, digest-checked sandbox. The resulting evidence records the selector
existence, execution status and exit code, source digest, and resolved image id.

A `[sandbox]` table without `image` is refused, as is `runtime = "host"` and any
`network` other than `none`: there is no host execution fallback and no internal
network. Configuration sections are `harness`, `model`, `state`, `workspace`,
`sandbox`, `validation`, `implementation`; anything else is refused rather than
ignored.

A key written in the file is rejected. Remote endpoints must be `https`; `http` is
accepted only for `localhost`/loopback. Examples of `base_url`/`approved_hosts`:
OpenAI `https://api.openai.com/v1` / `api.openai.com`; OpenRouter
`https://openrouter.ai/api/v1` / `openrouter.ai`; Gemini-compatible
`https://generativelanguage.googleapis.com/v1beta/openai/`; DeepSeek-compatible
`https://api.deepseek.com/v1`; local `http://localhost:11434/v1`.
`deepseek/deepseek-v4.1-flash-1017` on OpenRouter is the *requested acceptance
model*, not a default or a restriction.

## Use

```sh
export LOOMSPHERE_MODEL_API_KEY=...            # never written to disk by the runtime
loomsphere-harness --repo /path/to/repo init   # existing Git repo; verifies assets
loomsphere-harness --repo . submit --title "Add /hello" \
    --requirements-file req.md --acceptance-file acceptance.md     # -> T-0001
loomsphere-harness --repo . run T-0001         # Intake, then Architect (read-only)
loomsphere-harness --repo . show T-0001        # proposal, criteria, digest
loomsphere-harness --repo . approve T-0001 --digest <proposal sha256>
loomsphere-harness --repo . reject  T-0001 --digest <sha256> --reason "..."
loomsphere-harness --repo . implement T-0001   # Builder, validation, QA, Reviewer
loomsphere-harness --repo . retry-review T-0001 # one Reviewer-only retry
loomsphere-harness --repo . inspect-recovery T-0001 # work a failed Builder left behind
loomsphere-harness --repo . recover-builder T-0001 --snapshot-digest <sha256>
loomsphere-harness --repo . restart-builder T-0001 --failed-run <run id>
loomsphere-harness --repo . changes T-0001     # recorded change set of the deliverable
loomsphere-harness --repo . evidence T-0001    # validation run and review evidence
loomsphere-harness --repo . accept  T-0001 --digest <deliverable sha256> [--reason "..."]
loomsphere-harness --repo . reject-delivery T-0001 --digest <sha256> --reason "..."
loomsphere-harness --repo . status [T-0001]    # state, runs, model, prompt digests, tools
loomsphere-harness --repo . audit  [T-0001]    # hash-chained audit trail, verified
```

`init` requires `[harness] root`; it verifies the asset set there and binds the
repository to it. The runtime never reads assets from inside the target repo.

`--json` gives machine-readable output. Exit codes: `0` ok, `2` refused (error on
stderr), `3` the command did not reach its gate (`run` → `awaiting_approval`,
`implement` → `awaiting_acceptance`, `changes`/`evidence` → no deliverable,
`inspect-recovery` → the preserved work is not recoverable, `recover-builder` → the
recovered work did not reach `awaiting_acceptance`, `restart-builder` → the
restarted work did not reach `awaiting_acceptance`).

## Recovering work a failed Builder left behind

A Builder can fail after it has written useful files — a model budget that runs out,
a provider error, a timeout, an interrupted run. The failure used to end the task with
its work stranded in the worktree. Two commands now turn that work into a decision a
human makes:

- At the moment the Builder fails, the runtime reads the worktree through the same
  secret/size screen the delivery gate uses, commits what survives on
  `loomsphere/task-<key>` with a fixup message saying the work is unfinished, and
  records the commit, the workspace digest, and a diff excerpt on the iteration. If
  the screen refuses a path, nothing is committed and the work is left exactly as it
  is; it is never deleted. Discovery, screening, staging, and the post-commit check
  all operate on one set of paths: that set includes Git-ignored, untracked files
  (they can hold credentials), and the commit is rejected unless the staged and
  committed file lists are exactly the screened list.
- A process that dies mid-Builder leaves no failure record. The next command marks
  the run `interrupted`, closes the open iteration as failed, and leaves the
  uncommitted work where it is. `inspect-recovery` then shows the preserved inventory,
  and `recover-builder` screens and commits it at authorization time under the same
  exact-set rule before validating it. A stage that is genuinely running is never
  treated this way: a recovery holds an exclusive lease on the task for its whole
  duration, so a second process running `status` or `inspect-recovery` cannot close a
  live recovery.
- `inspect-recovery <task>` is read-only. It reports the failed Builder run, the
  preserved commit, the file inventory with a per-file mode and SHA-256, the snapshot
  digest, how many recoveries are left, every obstacle that would block a recovery,
  and the exact command to run next. It creates no run, writes no audit event, and
  does not touch the worktree. (`status` is the repairing command: it marks
  interrupted stages on the way to reporting state.)
- `recover-builder <task> --snapshot-digest <sha256>` requires that digest, which
  binds the authorization to the exact bytes the human inspected. A changed worktree,
  a different HEAD, a missing worktree, a task that is not `implementation_failed`, or
  an exhausted allowance is refused (`recovery-refused`) without changing state.
  A recovery is allowed once per task.
- The recovery does **not** call the Builder. It reuses the recorded change set and
  runs the existing validation, QA, and Reviewer stages unchanged, on the same
  container-only, digest-checked path — including QA's bounded single format
  correction, so a recovered run cannot be held to a looser or stricter standard of
  answer than a normal iteration. A QA block returns the task to
  `changes_required`, a rejection fails it, and a passing review reaches
  `awaiting_acceptance` — with the same human acceptance gate as any other delivery.
- Nothing is rewritten to make the recovery look cleaner: the failed Builder run keeps
  its `failed` status and reason, the iteration keeps its original failure reason and
  detail, and the recovery is recorded beside them in an append-only `recoveries`
  table with the authorizing OS user and both digests.
- The recovered deliverable states what was never observed. Its `builder` section is
  `status: "not-recorded"` with `tests_run: null`, the QA and Reviewer prompts carry a
  recovery provenance block telling them no Builder report exists, and the top-level
  `recovery` document ends at `acceptance: "pending-human-acceptance".
## Restarting a Builder that wrote nothing

A Builder can also stop before it writes a single file — a timeout or a killed
process leaves an empty worktree. There is no work to recover, and `implement`
refuses a task that already failed, so nothing but rewriting state could continue it.
`restart-builder` is the narrow alternative:

- `inspect-recovery <task>` still leads. It reports the failed Builder run, the empty
  change set, and a separate `restart` section: whether a restart is available, every
  obstacle that would block one, the authorizations already recorded, and the exact
  command to run next. It stays read-only, writes no audit event, and does not call
  the Builder. An empty change set is reported as *not recoverable*, because
  `recover-builder` needs a change set to authorize.
- `restart-builder <task> --failed-run <run id>` refuses (`restart-refused`) unless
  every one of these holds: the task is `implementation_failed`; the original
  proposal approval and the acceptance contract are unchanged and still match; the
  failed Builder run's reason is an interruption that cut it short (`timed-out`,
  `builder-interrupted`, `interrupted` — a run that exhausted its budget or hit a
  model error is refused); no Builder report, deliverable, or delivery decision is
  recorded; the worktree holds no committed, uncommitted, untracked, or ignored
  change; the task worktree is on the same commit the task approved; and no restart
  was already authorized. Preserved work is refused, not discarded — that is the
  `recover-builder` path.
- One restart per task is a database constraint, not a flag: the authorization is
  unique per failed iteration and counted inside the same write transaction that
  moves the task back to `approved`, so a second invocation, a second process, or a
  restarted CLI cannot authorize twice. The authorization carries the failed run,
  the approval and contract digests, and the digest of the empty change set it
  verified.
- Nothing existing is rewritten. The failed run keeps its `failed` status and reason,
  the failed iteration keeps its failure reason and detail, and the authorization is
  recorded beside them in an append-only `builder_restarts` table with the
  authorizing OS user.
- Only the authorization is granted by the command; the Builder itself still runs
  under `implement`'s conditions. The task returns to `approved` and the normal loop
  opens the next iteration, so validation, QA, the Reviewer, and the human acceptance
  gate are exactly the ones a first attempt faces. Nothing is retried automatically,
  and no budget, limit, or gate is relaxed.
## What is enforced

- **Approval** is by the local OS user (not a model) and must quote the SHA-256 of
  the exact stored proposal; a different digest, a missing proposal, or a decided
  task is refused and audited. Decisions are append-only.
- **Tools**: Intake and Architect hold only `read_file`, `list_directory`,
  `find_files`, `search_files` (the existing PydanticAI Harness `FileSystem`
  capability, read-only workspace, secret/`.git` paths denied). No shell, network,
  or write tool exists; the model cannot authorize anything.
- **Trusted assets** come from `[harness] root`, outside the target repo, and are
  verified against `harness.manifest.json` (file set and every SHA-256, no
  symlinks) before each run. Failure blocks the run before any model call.
- **Provider** missing, unapproved, non-https remote, or credential-less → the run
  fails closed with a recorded reason; no fallback.
- **Repository**: the task records `HEAD`; if it moves before a stage runs the
  stage is refused. The repo is never modified; state lives outside it.
- **Blocking Intake questions** stop the task at `needs_clarification` (no Architect
  run). Clarification answers are not part of this milestone: resubmit a clarified
  requirement.
- **Crash repair**: all state is SQLite. A run left `running` by a crash is marked
  `interrupted` on the next command and the stage can be re-run with `run`; a Builder
  stage is additionally left recoverable, with its uncommitted work preserved for
  `recover-builder`, and a Builder that wrote nothing is eligible for one
  human-authorized `restart-builder`.
- **Isolated worktree**: the Builder never runs in the developer's checkout. Each
  task gets `task-<key>` at `workspace_root/task-<key>` on branch
  `loomsphere/task-<key>`, created from the approved `HEAD`. The workspace root must
  not overlap the target repository, and every git call is argument-listed, time
  limited, and hook- and signature-free.
- **Container-only validation**: the planned validation command runs in the
  configured image, resolved to its `sha256:` image id *before* the run. Every
  invocation carries `--rm`, `--network none`, a read-only root filesystem, a
  tmpfs `/tmp`, dropped capabilities, `--security-opt no-new-privileges`, a
  non-root user, CPU/memory/PID limits, and only the workspace mounted. The digest
  of the tree is taken before and after; a changed tree fails the iteration. No
  sandbox configuration means the command is refused (`sandbox-not-configured`),
  never attempted on the host.
- **The container runs validation only.** QA and the Reviewer are model calls made
  by the runtime process itself; they are not processes inside the container. The
  container's job is to execute generated code and approved test selectors, and
  that is the only thing it isolates. Model traffic leaves the host as ordinary
  HTTPS to the provider you configured. See
  [SECURITY-MODEL.md](SECURITY-MODEL.md) for the boundaries this does and does not
  provide.
- **Validation is planned, not requested**: the runtime picks the command from the
  project's own files (`pyproject.toml`, `tests/`, `package.json`, …), constrained
  by `[validation] allowlist`. A command outside the allowlist is refused before
  anything runs; there is no model-supplied command.
- **Deliverable**: the reviewed commit, changed files, diff excerpt, executed
  validation evidence, QA and Reviewer reports, and the frozen acceptance contract
  are hashed into one deliverable digest. A path outside the worktree, a secret
  file, secret-shaped content, an oversized file, or too many files fails the
  change set before delivery (`unauthorized-path`, `secret-path`, `secret-content`,
  `file-too-large`, `too-many-files`).
- **Acceptance** is a human decision bound to that exact deliverable digest: a
  different digest, an undecided task, or a task that is not
  `awaiting_acceptance` is refused (`digest-mismatch`, `not-awaiting-acceptance`).
- **Output truncation** is reported, never silent: a run whose output hit the token
  limit records `truncation` signals, its `finish_reason`, and the token counts, and
  the stage fails rather than treating a partial response as complete.

## Role budgets, usage diagnostics, and bounded reads

### Per-role limits

`[model]` holds the global budgets. An optional `[model.role_limits.<role>]` table
narrows them for one role. The supported roles are `intake`, `architect`, `builder`,
`qa`, and `reviewer`; the keys are `request_limit`, `tool_call_limit`, and
`total_token_limit`. `max_output_tokens` and `timeout_seconds` remain global only.

```toml
[model]
request_limit = 60
tool_call_limit = 120
total_token_limit = 600000

# Illustrative values, not recommended production budgets. A role override may
# only lower a global value, never raise it.
[model.role_limits.intake]
request_limit = 12
total_token_limit = 120000

[model.role_limits.builder]
tool_call_limit = 90
```

Inheritance is per field: a key omitted from a role table keeps the global value,
and a role with no table inherits every global. An override larger than the global
value is refused with `config-limit-widened`. An unknown role, an unknown key, a
duplicate role, a non-table entry, or a value that is not a positive integer is
refused with `config-invalid`. Both are configuration errors, reported before any
model call.

The limits a run actually used are recorded in its evidence (`events.limits`), and
each value carries its origin in `events.limits.sources`: `global` for an inherited
value, `role_limits.<role>` for an override. `loomsphere-harness status` shows the
same effective limits with that origin, so a failed run does not need the config
file to be interpreted.

### Usage diagnostics

Every run records `duration_ms`, `usage`, `diagnostics`, and `tool_summary`,
whether it succeeded or failed. A failed run does not produce a final report or
change set, but it does carry the accounting of what it attempted, including the
tool calls it actually made.

`usage` reports `requests`, `tool_calls`, `input_tokens`, `output_tokens`,
`total_tokens`, `unmeasured_requests`, and `source`. Token accounting is reported
separately from request and tool-call counts, because a provider can omit usage
for some responses without making the counters unknown:

- `availability = "measured"` — every request reported usage. Token counts are
  exact and the budget entries use `within`, `near`, or `exceeded` as usual.
- `availability = "partial"` — some requests reported no usage. The token counts
  are an observed lower bound (`tokens_are_lower_bound = true`, rendered as `>=N`),
  and the `total_tokens` diagnostic is never reported as `within`; its state is
  `lower-bound`. Actual consumption was at least the recorded figure and may be
  higher. Request and tool-call counts are unaffected.
- `availability = "unavailable"` — no usable usage was reported. Token fields are
  `null`, never `0`, and text output says `unknown`, not zero.

Diagnostic warnings are computed after the run. An entry at or above 80% of a
configured limit is reported as `near`; a met or exceeded limit is `exceeded`. The
warnings are observations, not enforcement: the runtime checks limits between
steps, so a run can finish slightly above one. They are also not a financial cap;
they count recorded usage against the configured budget. Cost is reported as
`unavailable` because no verified billing units or prices are configured, and the
recorded token counts are the auditable figures.

A search selector passed to `search_files` is not stored as text in the run
evidence. Tool activity records the tool name, a workspace-relative target path,
outcome, and result size; a search `pattern` or `query` is recorded only as a
SHA-256 digest with the argument name and its length. Prompts, tool results, and
file contents are never copied into the audit trail.

### Bounded reads and paging

A tool result is bounded before it enters a model's context: 400 lines and 24,000
characters per file read, 200 directory-listing entries, 100 search matches, and
200 `find_files` results. A clipped read names the first line it did not show, and
`read_file` accepts `offset` and `limit`, so paging continues from that point.
These bounds apply equally to read-only Intake and Architect runs and to the
Builder.

## Structured output

Every stage answer is a contract, not prose, and the runtime never guesses what an
answer meant. One selector (`schemas.architect_proposal.select_payload`) serves all
five roles, so Intake, Architect, Builder, QA, and Reviewer are read the same way: the
declared fenced `json` block first, then a balanced object embedded in the document.
A response is accepted only when exactly one object can be the document.

A refusal is always classified, because the class decides what happens next:

| Class | Meaning | Code |
| --- | --- | --- |
| no payload | no `json` object in the answer | `payload-missing` |
| ambiguity | two different objects could each be the answer | `payload-ambiguous` |
| contract | one object, but a field is missing, mistyped, or out of range | `payload-invalid` |
| truncation | the turn hit the output ceiling | `response-truncated` |

The two report roles name the class on the run itself: `report-unreadable`,
`report-ambiguous`, and `report-invalid` (plus the shared `response-truncated`).
Intake and Architect record the stage failure as `invalid-intake` or
`invalid-proposal` — the stage-level reason a caller branches on — and carry the
class in the refusal detail, so the *decision* to fail is one reason and the
*diagnosis* is not lost.

Only the **contract** class is a formatting problem, and only it earns a correction.
Intake and Architect get exactly one corrective attempt
(`MAX_FORMAT_ATTEMPTS = 2`), and QA gets exactly one per iteration
(`MAX_QA_FORMAT_ATTEMPTS = 2`). The correction is the *same* request plus the
parser's own complaint — "decide the same thing you already decided and correct only
the structure" — so a re-ask cannot quietly change a decision. Each attempt is a
separate recorded run with its own prompt digest, plus an audit event
(`intake.format-retry`, `architect.format-retry`, `iteration.qa-format-retry`) that
names the class it is correcting. When the last attempt is refused the stage fails
exactly as it did before: nothing is guessed, and nothing is retried forever.

Both normal iterations and human-authorized recovered-Builder iterations reach QA
through one method (`ImplementationStage._run_qa`), so a recovered run is held to
the same standard of answer and the same number of attempts as any other, and the
attempts stay separate runs with the same audit event.

A report is also a shape contract: the arrays each role's documented response
declares are **required**, and an omitted array is refused as an incomplete report
while an explicit `[]` is accepted and preserved as the agent's answer. The parser
cannot tell "the agent found nothing" from "the agent never answered the field",
and only the second one is a reason to ask again. `files`, `acceptance_criteria`,
`tests_run`, and `notes` are required for a Builder report; `findings`,
`acceptance_criteria`, and `residual_risks` for QA; `findings`,
`acceptance_criteria`, and `follow_ups` for the Reviewer. The nested fields `change`,
`criterion`, and `evidence` stay optional, and the role prompts state which is which
so the same rules are described to the model and enforced by the parser.

Four answers are deliberately never re-asked: an **unreadable Builder report** (the
files are already on disk, so a second Builder would build again rather than
re-report), a **truncated turn** (re-sending without raising
`model.max_output_tokens` would spend the same budget and fail the same way), a
**readable adverse verdict**, and a **malformed answer whose payload already
declares an adverse verdict**. A QA `failed`, a Reviewer `changes-required`, and a
Reviewer `rejected` are decisions; they are recorded and take their normal path.
When such a verdict is declared inside a report that breaks the schema, the answer is
refused as `invalid-qa-report` or `invalid-review-report` and is *not* re-asked: a
second answer could only reverse the decision, not clarify it. The verdict is read
from the declared JSON payload and never scanned out of the surrounding prose, so a
sentence like "this would have failed" is not treated as a verdict. *Every* parseable
document an answer offers is read, so an answer that offers both a `failed` and a
`passed` report has declared an adverse verdict whichever order it wrote them in, and
it is refused as ambiguous rather than asked again. Only the Reviewer has an explicit
retry, and it is the human-approved `retry-review` command, not an automatic one.

## The implementation loop

`implement T-0001` requires an `approved` (or reworkable `changes_required`) task
and a `[sandbox]` image; anything else is refused before a model call. One
iteration is:

1. **Builder** writes inside the task worktree and answers with a build report
   (`summary`, `files`, `acceptance_criteria`, `tests_run`, `notes`).
2. The runtime commits the worktree — a change that touches a path outside the
   worktree, a secret file, or nothing at all fails the iteration
   (`unauthorized-path`, `secret-path`, `no-changes`).
3. **Validation** plans and executes the allowlisted command in the container and
   records the command, status, exit code, duration, image, workspace digest, and
   output digest. It also executes every declared `pytest` acceptance selector
   exactly as approved, after checking collection. Missing selectors, failed runs,
   or source/image evidence that does not match the main validation run block
   readiness. Mandatory `unverified` and unsupported protected selectors remain
   explicit and also block readiness; optional obligations are recorded without
   being labeled verified. `protected-pytest` is **not implemented in this
   runtime**: a protected-artifact selector resolves to `unsupported` with the
   recorded obstacle, so it can neither pass nor be silently skipped.
4. **QA** reports `passed` or `failed` with findings and a claim per approved
   criterion. A `failed` verdict, a blocking finding, or a criterion the report
   never mentions sends the iteration back to the Builder with feedback. An
   unreadable QA report is corrected once, against the same recorded evidence; a
   second unreadable report fails the iteration as `invalid-qa-report`, and so does
   an unreadable report that already declared `failed` — that verdict is not
   re-requested.
5. **Reviewer** reports `accepted`, `changes-required`, or `rejected`. Accepted
   freezes the deliverable and moves the task to `awaiting_acceptance`. An
   unreadable Reviewer answer is never turned into a verdict: it fails the
   iteration as `invalid-review-report`, and the only retry is the human-approved
   `retry-review`.

Iteration statuses are `building`, `built`, `validating`, `qa_running`,
`review_running`, `ready`, `changes_required`, and the terminal `failed`,
`validation_failed`, `qa_failed`, `review_failed`. Failure reasons are
`iterations-exhausted`, `invalid-build-report`, `invalid-qa-report`,
`invalid-review-report`, `validation-failed`, `qa-blocked`,
`review-changes-required`, `review-rejected`, and
`acceptance-contract-unavailable`. A mandatory criterion without successful,
source- and image-bound selector evidence fails as
`acceptance-verification-incomplete`; a mandatory `unverified` criterion also
fails closed. Reaching the iteration limit is not a crash: every attempt stays
recorded, the task is left `changes_required`, and a human decides what happens
next.

Rework reuses the same `base_commit` and produces a new commit, so two attempts
are always distinguishable in `status` and in the audit trail.

The trail names one event per fact: each stage start appears once as
`intake.started`, `architect.started`, `builder.started`, `qa.started`, or
`reviewer.started` (recorded by `store.begin_run`), its result as `*.recorded`, and
each iteration transition as `iteration.built`, `iteration.validation-started`,
`iteration.qa-started`, `iteration.review-started`, `iteration.ready`, or
`iteration.failed`. A test asserts the start events appear exactly once, so the
trail cannot drift into duplicate labels.

## Import surface for LoomSphere

Reused modules keep their LoomSphere names so the application can re-export them:

| Package module | Replaces `app.…` |
| --- | --- |
| `loomsphere_harness.agent_runner` (`AgentInvocation`, `AgentRunResult`, `AgentExecutionError`) | contract part of `services.agent_runner` |
| `loomsphere_harness.pydantic_harness` (`PydanticHarnessConfig`, `PydanticHarnessRunner`, `runtime_identity`, `tools_for`) | `services.pydantic_harness` |
| `loomsphere_harness.agent_catalog`, `.skills`, `.architect_prompt`, `.intake_prompt` | same-named `services.*` |
| `loomsphere_harness.skill_selection` | `services.skill_selection` (DB-free: takes `TaskFacts` and recorded intake facts) |
| `loomsphere_harness.schemas.{architect_proposal,intake,acceptance_contract}` | `schemas.*` |
| `loomsphere_harness.lifecycle_hooks` (`load_registry`, `evaluate`, `evaluate_hook`, `registry_digest`, `LifecycleHookBlocked`) and `.schemas.lifecycle_hooks` | pure loader/evaluator half of `services.lifecycle_hooks`, `schemas.lifecycle_hooks` |
| `loomsphere_harness.routing` (classification, `resolve_route`, route digests, change inspection) | `services.routing` (verbatim; DB-free) |
| `loomsphere_harness.workspace` (`ensure_task_workspace`, `commit_worktree`, `inspect_change_set`, `workspace_digest`) | `services.agent_workspace` and `services.workspace_state` |
| `loomsphere_harness.sandbox` (`SandboxPolicy`, `SandboxExecutor`, `ExecutionEvidence`) | `services.sandbox` |
| `loomsphere_harness.validation` (`plan_validation`, `execute_validation`, `ValidationOutcome`) | `services.validation` |
| `loomsphere_harness.reports` (`parse_build_report`, `parse_qa_report`, `parse_review_verdict`, `missing_criteria`) | `services.report_contracts` |
| `loomsphere_harness.implementation_prompt` (`compose_builder_prompt`, `compose_qa_prompt`, `compose_reviewer_prompt`, `scope_block`) | `services.implementation_prompt` |
| `loomsphere_harness.implementation.ImplementationStage` | the Builder/QA/Reviewer half of `services.implementation` |
| `loomsphere_harness.harness_settings.bind_settings` | binds app settings for the catalog loader |

`current_catalog()` in the package reads the bound settings; the app binds its own.

## Replaceable seams

`loomsphere_harness.interfaces` declares structural Protocols; the portable package
imports no application module, so the LoomSphere app can bind its own.

| Seam | Protocol | Local implementation |
| --- | --- | --- |
| Persistence | `TaskStore` | `store.SqliteStore` |
| Identity | `IdentityProvider` | `repository.OsUserIdentity` |
| Repository | `RepositoryProvider` | `repository.GitRepository` |
| Approval / rejection | `ApprovalGate` | `approval.LocalApprovalGate` (digest-bound) |
| Execution environment | `ExecutionEnvironment` | `engine.LocalReadOnlyEnvironment` |
| Model execution | `AgentRunner` | `pydantic_harness.PydanticHarnessRunner` |

`Engine` is typed against these. `ExecutionEnvironment` is the read-only workspace
boundary (`workspace_path`, `writable`); the engine refuses a writable one, always
invokes the runner with `read_only=True`, and the model holds only read tools.
`AgentRunner` executes the model; the environment says *where* it may read.

The loop adds two further seams: `ImplementationStage` receives the `Engine` (so a
consumer can substitute the store, identity, repository, or runner), and
`Engine(..., sandbox_executor=...)` replaces the container executor without
touching the policy, the plan, or the recorded evidence shape. Tests use exactly
these seams to exercise the real gates with a scripted model and a fake runtime.

## Hooks and routing

**Lifecycle hooks.** The loader, the check evaluators, and the registry digest are
portable (`lifecycle_hooks.py`). The runtime loads the registry from the trusted
`.github/hooks` (shipped empty) and evaluates `before-agent-invocation` and
`after-agent-completion` around each Intake/Architect invocation. A hook can only
`continue` or `block`: a block fails the run with `hook-denied` (before the model is
called, for the first event), and every outcome is written to the hash-chained audit
trail. A hook never approves anything and the approval gate is unchanged; an
unusable registry fails closed (`hook-registry-*`) before any run starts. With no
hook registered nothing is evaluated or recorded, so behaviour is as before.
Application-owned and deliberately **not** extracted: payload construction from
PostgreSQL rows, the `lifecycle_hook_executions` table, and the
`before-review-progression` / `before-delivery` guards, which the application
evaluates against its own delivery records. The standalone loop enforces the same
outcome with its own gates (report contract validation, criterion coverage, the
digest-bound delivery decision) rather than the application's hook payloads.

**Routing.** `routing.py` (specialist specs, requirement classification, review
route resolution and digests, change inspection) is vendored verbatim and importable
for the application. It is **not wired into the loop**: a review route is frozen
from the approved proposal and exists to choose specialist reviews of a Builder
result. The standalone workflow runs one general Reviewer stage and only records
Intake's specialist recommendations. Importing `routing` is safe without bound
settings: the `SPECIALISTS`-style compatibility snapshots are computed lazily on
first access and the functions resolve the catalog per call, so
`bind_settings(...)` is needed only when they are used.

## Tests and acceptance

```sh
pip install -e '.[dev]'
python -m unittest discover -s tests          # 251 tests at v0.1.0: runtime and harness assets
# CI also runs this on Python 3.11, 3.13, and 3.14; it needs no secret.
```

The tests make no network call and start no container. They exercise the real
gates — worktree creation and screening, the sandbox command line and its digest
binding, the validation plan, report extraction, the approval and delivery
digests — and fake only two things: the model (`FunctionModel` with a scripted
response) and the container runtime (an injected executor or `sandbox_executor`).
A live run is opt-in and skipped without credentials:

```sh
LOOMSPHERE_ACCEPTANCE_API_KEY=... \
LOOMSPHERE_ACCEPTANCE_MODEL=deepseek/deepseek-v4.1-flash-1017 \
  python -m unittest tests.test_runtime.LiveAcceptance
# optional: LOOMSPHERE_ACCEPTANCE_BASE_URL (default https://openrouter.ai/api/v1)
```

A live implementation run additionally needs a locally present sandbox image,
because the runtime pins it by `sha256:` id before executing anything. The
repository's asset validators also pass in a plain checkout:

```sh
python tools/build_manifest.py --check     # 41 assets, suite digest unchanged
python tools/validate_harness.py           # 10 check groups, 0 errors, 0 warnings
```
