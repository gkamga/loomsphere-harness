# Quickstart: developer distribution v0.1.0

This guide takes one developer from a bare workstation to a delivered, reviewed
change using the standalone LoomSphere harness. It requires nothing from the
original LoomSphere application: no frontend, no backend, no PostgreSQL, no
control-plane account.

There are two independent things you can install. Choose deliberately:

| | **A. Declarative harness** | **B. Autonomous software factory** |
| --- | --- | --- |
| Purpose | Give GitHub Copilot (or a compatible tool) LoomSphere's agents, skills, hooks, contracts, and instructions | Run the staged engineering loop with enforced gates |
| Install | `tools/harness_install.py` into your repository | `pip install` of the Python package and CLI |
| Enforcement | None by itself — your tool decides what a model may do | Approval, digests, trusted assets, worktree isolation, container-only validation, acceptance |
| Needs a model credential | No (your tool already has one) | Yes, your own OpenAI-compatible provider |
| Needs Docker/Podman | No | Yes, for `implement` |
| Guide | [CONSUMING.md](CONSUMING.md) | this page |

You can use both: install the assets for Copilot, and run the CLI from a separate
trusted checkout against the same repository.

---

## 1. Prerequisites

| Requirement | Version | Needed for |
| --- | --- | --- |
| Python | 3.11 or newer | the CLI |
| Git | any recent version | both modes |
| Docker or Podman | any recent version | `implement` (Mode B) |
| An OpenAI-compatible LLM endpoint | — | `run`, `implement` (Mode B) |
| A terminal | — | everything |

Check what you have:

```sh
python3 --version
git --version
docker --version          # or: podman --version
```

Mode A additionally needs Python 3.9+ only. The runtime refuses to run rather
than silently degrading: with no model configured, every model call fails closed.

## 2. Clone and pin a version

Clone the harness **outside your target repository**:

```sh
git clone https://github.com/gkamga/loomsphere-harness.git ~/src/loomsphere-harness
cd ~/src/loomsphere-harness

git fetch --tags
git tag --list                     # v0.1.0, if the release tag exists
git checkout v0.1.0                # or a full commit SHA
git rev-parse HEAD                 # record this SHA
```

If no tag is published yet, check out the full commit SHA you reviewed. Do not
build from a moving branch: the runtime records the asset suite digest, and the
assets must be the bytes you reviewed.

`~/src/loomsphere-harness` is your **trusted checkout**. The runtime reads agents,
skills, hooks, and contracts only from there, verifies every file against
`harness.manifest.json`, and refuses a checkout that is the same repository as —
or nested inside — the repository it is working on.

## 3. Install the CLI

```sh
cd ~/src/loomsphere-harness
python3 -m venv .venv
. .venv/bin/activate
pip install .                      # add '.[dev]' to also run the asset validators
loomsphere-harness --help
```

The package is **not** published to PyPI, so `pip install loomsphere-harness`
will not work. Install from the checkout, or from a wheel you build locally:

```sh
pip install build
python -m build --wheel
pip install dist/loomsphere_harness-0.1.0-py3-none-any.whl
```

Verify the entry point resolves:

```sh
loomsphere-harness --help
python -m loomsphere_harness --help     # equivalent
```

## 4. Configure your LLM provider

The runtime ships no default endpoint, model, or credential. Create
`~/.config/loomsphere-harness/config.toml` (or pass `--config PATH`, or set
`$LOOMSPHERE_HARNESS_CONFIG`):

```toml
[harness]
root = "/Users/you/src/loomsphere-harness"   # the trusted checkout from step 2

[model]
provider = "openai-compatible"               # the only accepted value
base_url = "https://api.openai.com/v1"
model_id = "gpt-4o-mini"                     # any model your endpoint serves
approved_hosts = ["api.openai.com"]          # the endpoint host, exactly
api_key_env = "LOOMSPHERE_MODEL_API_KEY"     # the NAME of the env var, not the key

[state]
# dir = "/Users/you/.local/share/loomsphere-harness"   # default: outside the repo

[workspace]
# root = "/Users/you/.local/state/loomsphere-harness/workspaces"

[sandbox]                                     # required before `implement`
image = "loomsphere-pytest:python3.12"

[implementation]
# max_iterations = 2
```

Other endpoints, using the same shape:

| Provider | `base_url` | `approved_hosts` |
| --- | --- | --- |
| OpenRouter | `https://openrouter.ai/api/v1` | `["openrouter.ai"]` |
| OpenAI | `https://api.openai.com/v1` | `["api.openai.com"]` |
| Gemini (OpenAI-compatible) | `https://generativelanguage.googleapis.com/v1beta/openai/` | `["generativelanguage.googleapis.com"]` |
| DeepSeek (OpenAI-compatible) | `https://api.deepseek.com/v1` | `["api.deepseek.com"]` |
| Local server | `http://localhost:11434/v1` | `["localhost"]` (see `require_api_key` below) |

Rules the runtime enforces:

- `provider` must be exactly `openai-compatible`.
- A remote `base_url` must be `https`; plain `http` is accepted only for
  `localhost`/loopback.
- The endpoint host must appear in `approved_hosts`.
- Unknown sections or keys are refused, not ignored.
- No default endpoint exists. A missing configuration is an error, never a
  fallback.

For an unauthenticated local server only:

```toml
[model]
require_api_key = false
```

## 5. Safe credential management

The configuration file may **name** where a credential lives. It may never
**contain** one.

**Preferred: an environment variable.**

```sh
export LOOMSPHERE_MODEL_API_KEY="..."      # current shell only
loomsphere-harness --repo ~/work/my-project status
```

Keep it out of shell history and out of committed files. Use a secret manager,
your shell's private profile, or a tool such as `direnv` scoped to a private
directory. Never `export` it in a script you commit.

**Alternative: a credential file.**

```toml
[model]
api_key_file = "/Users/you/.config/loomsphere-harness/model-key"
```

```sh
chmod 600 ~/.config/loomsphere-harness/model-key
```

Guarantees:

- A literal `api_key`, `key`, `token`, `password`, or `secret` in the TOML is
  **rejected** before any model call.
- The runtime never writes the key to the SQLite state file, the audit trail, or
  any output.
- The key is read only from the environment variable or file you named.

## 6. Docker sandbox setup

`implement` refuses to run without a container image. There is no host execution
fallback.

Build the repository's pytest image from the trusted checkout:

```sh
docker build -t loomsphere-pytest:python3.12 ~/src/loomsphere-harness/docker/pytest-sandbox
docker image inspect --format '{{.Id}}' loomsphere-pytest:python3.12
```

The image needs `pytest` installed only because validation must not install
anything at run time. If your project validates with another allowlisted command,
build an image that contains that tool and point `[sandbox] image` at it.

At run time the runtime:

- resolves the image tag to its `sha256:` id **before** executing anything, and
  records that id in the evidence;
- runs with `--rm --network none`, a read-only root filesystem, a `tmpfs` `/tmp`,
  dropped capabilities, `--security-opt no-new-privileges`, a non-root user, and
  CPU/memory/PID limits;
- mounts only the task worktree;
- digests the worktree before and after and fails the iteration if it changed.

Podman works for the same setup; set `runtime = "podman"` in `[sandbox]`. Any
`network` value other than `none` is refused.

## 7. First task submission

The target repository must already be a Git repository. State is written outside
it.

```sh
cd ~/work/my-project
git status                                  # a dirty worktree is recorded, not hidden

loomsphere-harness --repo . init
# initialized /Users/you/work/my-project @ <commit>
# verified <N> assets (suite <digest>)
# state /Users/you/.local/share/loomsphere-harness/projects/<hash>/state.sqlite
```

Write the two inputs as files:

```sh
cat > req.md <<'EOF'
Add a GET /hello endpoint that returns {"message": "hello"}.
EOF

cat > acceptance.md <<'EOF'
- GET /hello returns HTTP 200 with JSON body {"message": "hello"}
- An automated test covers the endpoint
EOF

loomsphere-harness --repo . submit --title "Add /hello" \
  --requirements-file req.md \
  --acceptance-file acceptance.md
# submitted T-0001
```

Then run the read-only analysis stages:

```sh
loomsphere-harness --repo . run T-0001
```

`run` executes Intake and then Architect. Both hold only read tools
(`read_file`, `list_directory`, `find_files`, `search_files`); no write, shell, or
network tool exists. Exit code `3` means the task did not reach the approval gate:

- `needs_clarification` — Intake asked a blocking question. Clarification answers
  are not part of v0.1.0; submit a clarified requirement as a new task.
- `intake_failed` / `architect_failed` — see `status T-0001` for the recorded
  reason.

## 8. Proposal approval

```sh
loomsphere-harness --repo . show T-0001
```

Read the proposal, the component changes, and the proposed criterion-to-check
mapping. Then copy the exact digest:

```
Proposal digest: 3f2a...c9  (pending)
```

Approve or reject **that digest**:

```sh
loomsphere-harness --repo . approve T-0001 --digest 3f2a...c9
# or
loomsphere-harness --repo . reject T-0001 --digest 3f2a...c9 --reason "Scope is wrong"
```

A different digest, a missing proposal, or an already-decided task is refused and
audited. The approval is recorded against the local OS user, not the model.
Decisions are append-only.

## 9. Implementation execution

```sh
loomsphere-harness --repo . implement T-0001
```

This requires an `approved` task, a configured `[sandbox]` image, and a
credentialed provider. One iteration is:

1. **Builder** writes inside a private worktree at
   `<workspace root>/task-t-0001` on branch `loomsphere/task-t-0001`, created
   from the approved `HEAD`. Your checkout is never touched.
2. The runtime commits and screens the change set.
3. **Validation** plans the command from your project's own files, constrained by
   the allowlist, and runs it in the container. Approved `pytest` acceptance
   selectors are executed exactly as approved.
4. **QA** reports `passed`/`failed` with a claim per approved criterion.
5. **Reviewer** reports `accepted`, `changes-required`, or `rejected`.

Up to `[implementation] max_iterations` attempts run. A change that touches a path
outside the worktree, a secret file, secret-shaped content, or nothing at all
fails the attempt. Reaching the limit is not a crash: every attempt stays recorded
and the task is left `changes_required` for a human decision.

Exit code `3` means the loop did not reach `awaiting_acceptance`. Check `status`.

If the only failure is an invalid Reviewer report, one retry is available:

```sh
loomsphere-harness --repo . retry-review T-0001
```

## 10. Reviewing evidence

```sh
loomsphere-harness --repo . status T-0001     # state, runs, model, prompt digests, tools
loomsphere-harness --repo . changes T-0001    # commit, branch, changed files, diff excerpt
loomsphere-harness --repo . evidence T-0001   # validation run and QA/Reviewer reports
loomsphere-harness --repo . audit T-0001      # hash-chained trail, chain verified
```

What to look for before accepting:

- the **validation command** is the one you expected, with status `passed` and
  exit code `0`;
- the **image id** is the digest you built, and the **worktree digest** did not
  change during validation;
- every mandatory criterion has source- and image-bound selector evidence, not
  just prose;
- any `unverified` mandatory criterion is a blocker, not a note;
- the **iteration** count and `base_commit -> commit_sha` describe a change you
  can reproduce with `git show` in the task worktree.

`--json` on any command gives machine-readable output. Exit codes are `0` ok,
`2` refused (reason on stderr), `3` the command did not reach its gate.

## 11. Human delivery acceptance

`changes` prints the deliverable digest:

```
Deliverable 8b1e...44
```

Accept or reject exactly that digest:

```sh
loomsphere-harness --repo . accept T-0001 --digest 8b1e...44 --reason "Reviewed"
# or
loomsphere-harness --repo . reject-delivery T-0001 --digest 8b1e...44 --reason "Missing test"
```

Acceptance is a human decision bound to the deliverable digest. A digest
mismatch, an undecided task, or a task that is not `awaiting_acceptance` is
refused. The runtime never pushes, opens a pull request, or merges. After
acceptance the commit exists only in the task worktree; promoting it is your
decision and your command.

## 12. Updates

```sh
cd ~/src/loomsphere-harness
git fetch --tags
git checkout v0.2.0                # or the next reviewed commit SHA
. .venv/bin/activate
pip install --upgrade .
loomsphere-harness --help
```

The asset suite digest changes only when agents, skills, hooks, contracts, or
instructions change. Existing SQLite state is migrated in place (v1 → v2 at this
release); a state file from a newer schema is refused rather than rewritten.

For declarative assets installed with `tools/harness_install.py`, follow
[CONSUMING.md](CONSUMING.md) instead: `update --ref <new tag>` replaces
unmodified files and reports conflicts.

## 13. Troubleshooting

| Symptom | Code | Meaning and fix |
| --- | --- | --- |
| Any model command refuses immediately | `harness-root-not-configured` | Set `[harness] root` to the trusted checkout. |
| `init` refuses | `harness-root-inside-target` | The trusted checkout is the target repo or nested in it. Move it out. |
| `init` refuses | `assets-missing` / `manifest-corrupt` / asset digest mismatch | The checkout is not a clean harness checkout. Re-clone and re-pin. |
| Any command refuses | `config-invalid` | Unknown section/key, non-`openai-compatible` provider, non-https remote, unapproved host, inline key, or `network != none`. Read the message; it names the key. |
| Any command refuses | `state-inside-repository` | `[state] dir` or the default state path resolves inside the target repo. Move it. |
| `run` fails with no model call | provider unconfigured | Set `base_url`, `model_id`, `approved_hosts`, and either `api_key_env` or `api_key_file`. |
| `implement` refuses | `sandbox-not-configured` | Add a `[sandbox]` table with `image` and build that image. |
| `implement` refuses | `no-changes` | The Builder produced an empty commit. |
| `implement` fails | `unauthorized-path`, `secret-path`, `secret-content`, `file-too-large`, `too-many-files` | The change set was screened out. Narrow the scope or remove the secret. |
| `implement` stops | `iterations-exhausted` | Every attempt is recorded; the task is `changes_required`. Decide whether to rework. |
| `implement` fails | `acceptance-verification-incomplete` | A mandatory criterion has no successful, source- and image-bound selector evidence. |
| `approve` refuses | `digest-mismatch` | You quoted a digest that is not the stored proposal. Re-run `show`. |
| `accept` refuses | `not-awaiting-acceptance` | The task is not at the delivery gate; run `status`. |
| `run` exits `3` | `needs_clarification` | Intake asked a blocking question. Submit a clarified requirement. |
| A stage is reported `interrupted` | — | The previous process died. Re-run the same command; the stage is recoverable. |

Nothing in this guide requires the LoomSphere application. If a command fails,
`loomsphere-harness --repo . audit` shows the hash-chained trail, and `status`
shows the recorded refusal reason.

## What v0.1.0 does not do

- It does not publish to PyPI and does not create tags.
- It does not push, open a pull request, or merge.
- It does not run anything on the host: implementation validation is container
  only.
- It does not supply a license. Licensing is an open human decision.
- It does not answer Intake clarification questions in-flight; submit a clarified
  requirement.
