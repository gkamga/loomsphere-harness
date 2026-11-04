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
build/verify/review loop and delivery acceptance on top of the same runtime; the
store is now schema v2 and migrates an existing v1 state file in place.

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
`implement` → `awaiting_acceptance`, `changes`/`evidence` → no deliverable).

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
- **Restart**: all state is SQLite. A run left `running` by a crash is marked
  `interrupted` on the next command and the stage can be re-run with `run`.
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
   never mentions sends the iteration back to the Builder with feedback.
5. **Reviewer** reports `accepted`, `changes-required`, or `rejected`. Accepted
   freezes the deliverable and moves the task to `awaiting_acceptance`.

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
