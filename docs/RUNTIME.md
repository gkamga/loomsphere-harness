# Standalone runtime (phase 2C.1)

`loomsphere-harness` is the portable runtime and a minimal CLI for one workflow:
**Intake → Architect → explicit human approval.** It stops there. There is no
Builder, QA, or Reviewer command or code path, and the state schema only accepts
`intake` and `architect` runs.

## Install

```sh
python3.11+ -m venv .venv && . .venv/bin/activate
pip install -e .            # Python >= 3.11
loomsphere-harness --help   # or: python -m loomsphere_harness
```

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
```

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
loomsphere-harness --repo . status [T-0001]    # state, runs, model, prompt digests, tools
loomsphere-harness --repo . audit  [T-0001]    # hash-chained audit trail, verified
```

`--json` gives machine-readable output. Exit codes: `0` ok, `2` refused (error on
stderr), `3` `run` did not reach `awaiting_approval`.

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
`before-review-progression` / `before-delivery` guards, which belong to
Builder/QA/Reviewer and delivery stages that do not exist here.

**Routing.** `routing.py` (specialist specs, requirement classification, review
route resolution and digests, change inspection) is vendored verbatim and importable
for the application. It is **not wired into the CLI**: a review route is frozen from
the approved proposal and exists to choose specialist reviews of a Builder result.
Intake→Architect→approval has no change to route, so the standalone workflow only
records Intake's specialist recommendations. Importing `routing` is safe without bound settings: the
`SPECIALISTS`-style compatibility snapshots are computed lazily on first access and
the functions resolve the catalog per call, so `bind_settings(...)` is needed only
when they are used.

## Tests and acceptance

```sh
pip install -e '.[dev]'
python -m unittest discover -s tests          # runtime + existing harness tests
# CI also runs this on Python 3.11, 3.13, and 3.14; it needs no secret.
```

The runtime tests use a scripted model; they make no network call. A live run is
opt-in and skipped without credentials:

```sh
LOOMSPHERE_ACCEPTANCE_API_KEY=... \
LOOMSPHERE_ACCEPTANCE_MODEL=deepseek/deepseek-v4.1-flash-1017 \
  python -m unittest tests.test_runtime.LiveAcceptance
# optional: LOOMSPHERE_ACCEPTANCE_BASE_URL (default https://openrouter.ai/api/v1)
```
