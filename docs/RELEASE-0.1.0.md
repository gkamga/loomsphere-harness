# Release 0.1.0 — developer distribution

## Status: prepared, not published

This document is the release candidate record for `v0.1.0`. It was written during
release preparation and records what the code does, how to install it, and which
decisions remain with a human owner.

Deliberately **not** done here:

- No Git tag was created.
- No GitHub release was published.
- Nothing was uploaded to PyPI.
- No license was chosen.
- No branch was merged.

The version string `0.1.0` appears only in `pyproject.toml`. Neither a local tag
nor a remote tag named `v0.1.0` existed when this document was written.

## What this release contains

The standalone harness runs a complete engineering workflow without the original
LoomSphere application:

```
Intake -> Architect -> Human approval -> Builder -> Sandboxed validation
       -> QA -> Reviewer -> Human delivery acceptance
```

Delivered capabilities:

| Capability | Notes |
| --- | --- |
| Intake and Architect stages | Run against your configured model provider; Intake can stop with a blocking clarification. |
| Digest-bound approval | `approve` and `accept` require the exact digest printed by `show`; a stale digest is refused. |
| Implementation loop | Builder proposes a commit; validation and review run inside the container. |
| Container-only validation | No host fallback. Docker or Podman, `--network none`, read-only root filesystem, dropped capabilities, non-root user, resource limits, only the task worktree mounted, image pinned by `sha256:` id before execution. |
| Protected verification | A mandatory acceptance criterion without digest-bound, image-bound selector evidence fails the run. |
| QA and Reviewer | Reviewer reports are validated; `retry-review` re-runs only the Reviewer. |
| Delivery acceptance | `accept --digest` closes the task. The runtime never pushes, opens a pull request, or merges. |
| Tamper-evident record | A hash-chained audit trail, a per-task worktree digest before and after execution, and a recorded refusal reason for every rejected command. |
| Persistence | SQLite store, schema v2, stored outside the target repository. A v1 store migrates in place. |
| Trusted assets | Manifest v2 covering 41 assets, with a suite digest recorded on every approved run. |

## Two ways to consume the harness

They give different guarantees. Pick one deliberately.

### A. Declarative harness — guidance without enforcement

Installs agent definitions, skills, hooks, and workflow documents into **your**
repository. It attaches nothing to your CI and enforces nothing at run time.
Every gate in this mode holds only because a human or an agent honours it.

```sh
git clone https://github.com/gkamga/loomsphere-harness.git ~/tools/loomsphere-harness
cd ~/tools/loomsphere-harness && git checkout <commit-sha-of-v0.1.0>

python tools/harness_install.py install \
  --target /path/to/your/repo \
  --source ~/tools/loomsphere-harness \
  --ref <commit-sha-of-v0.1.0>

python tools/harness_install.py verify \
  --target /path/to/your/repo \
  --source ~/tools/loomsphere-harness --strict
```

Branches are refused as a `--ref`; pin a commit SHA (or a tag, once one exists).
`--expect-suite-digest` refuses a source checkout whose manifest digest differs
from the one you trust. See `docs/CONSUMING.md` for buckets, overwrite
authorization, and revert.

### B. Autonomous software factory — enforcement by the runtime

Installs the runtime that actually enforces approval, digest binding, trusted
assets, isolated worktrees, container-only validation, and delivery acceptance.

```sh
git clone https://github.com/gkamga/loomsphere-harness.git ~/tools/loomsphere-harness
cd ~/tools/loomsphere-harness && git checkout <commit-sha-of-v0.1.0>

python3 -m venv .venv
.venv/bin/python -m pip install .

.venv/bin/python -m build --wheel          # optional; produces dist/loomsphere_harness-0.1.0-py3-none-any.whl
.venv/bin/loomsphere-harness --help
```

You need Python 3.11 or newer, Git, Docker or Podman, your own configured model
provider, a trusted harness checkout kept **outside** the target repository,
SQLite persistence, and a human at the approval and acceptance gates.

The package is **not on PyPI**: `pip install loomsphere-harness` does not
resolve. Install from a checkout or from a locally built wheel.

Follow `docs/QUICKSTART.md` for the full path from an empty config to a delivered
task. `docs/RUNTIME.md` is the configuration and CLI reference.

## What was verified while preparing this release

Everything below was executed locally on macOS with Python 3.11.11, 3.13.1, and
3.14.2 and Docker 29.7.2; no Podman was present. Nothing is reported as passing
that was not run.

| Check | Command | Observed result |
| --- | --- | --- |
| Wheel build | `python -m build --wheel --outdir /tmp/lsh-wheel` | `loomsphere_harness-0.1.0-py3-none-any.whl` built |
| Wheel install, clean venv | `pip install /tmp/lsh-wheel/…whl` then `pip check` | `No broken requirements found.` |
| CLI entry point | `loomsphere-harness --help` | 14 subcommands listed |
| Fresh `git clone` install | `pip install -e '.[dev]'` in a new venv | exit 0, CLI resolves |
| Full suite, Python 3.11 | `python -m unittest discover -s tests -q` | `Ran 244 tests`, `OK (skipped=2)` |
| Full suite, Python 3.13 | same | `Ran 244 tests`, `OK (skipped=2)` |
| Full suite, Python 3.14 | same | `Ran 244 tests`, `OK (skipped=2)` |
| Full suite, fresh clone | same, from the clone | `Ran 244 tests`, `OK (skipped=2)` |
| Manifest integrity | `python tools/build_manifest.py --check` | `manifest OK: 41 files, suite d63e813839290391e0b3479f50856b33eff8e4f1ae530134bb0bbd5dddd85fc7` |
| Asset validation | `python tools/validate_harness.py` | `10 check groups, 0 error(s), 0 warning(s)` |
| Declarative dry run | `harness_install.py install … --dry-run` | listed the files it would write, wrote nothing |
| Declarative install | `harness_install.py install --ref <sha> --source <checkout>` | `installed 44 files from <sha> (suite d63e8138…)` |
| Declarative verify | `harness_install.py verify --strict --source <checkout>` | `verify OK: 44 files match <sha>` |
| Asset discovery | `harness_install.py discover --json` | agents, skills, and hooks enumerated |
| Sandbox image | `docker build -t loomsphere-pytest:python3.12 docker/pytest-sandbox` | built, id `sha256:024910f96cf9cb958cf3c35f547e4bf2b096ba9a1a278de840e83165f52b29e0` |
| Happy-path init | `loomsphere-harness --config … init` | `verified 41 assets`, state written outside the target repo |
| Inline credential refused | config with `[model] api_key = "…"` | `config-secret-in-file`, exit 2 |
| Harness root inside target refused | `init` with `harness.root` = target | `harness-root-inside-target`, exit 2 |
| Sandbox network refused | `sandbox.network = "bridge"` | `config-invalid`, exit 2 |
| CI on the pull request | GitHub Actions, run 41767493994 | all five checks passed: suite on 3.11, 3.13, 3.14; installer on the oldest supported Python; manifest, assets, contracts, links, and consumption |

Disposable repositories, a synthetic non-functional API key, and a nonexistent
provider host were used. No live model call was made, so model-backed stages
(Intake, Architect, Builder, QA, Reviewer) are exercised by the test suite with
fakes only; a live end-to-end run remains an operator exercise against a real
provider.

### Not verified here

- Podman was not installed, so only the Docker path was executed. The runtime
  treats Podman as an alternative container engine; that branch is covered by the
  test suite, not by a live run.
- No live model provider was contacted.
- CI has run once against the pull request that introduces this document, and
  passed. A later change to anything under `.github/` that is covered by the suite
  digest should be re-validated before the tag is created.

## Decisions a human must make before publishing

These are recorded as open, not as defaults.

1. **License.** No `LICENSE` file exists and `pyproject.toml` deliberately has no
   `license` field. Selecting a license is a legal decision with consequences for
   downstream reuse; it was not made here. Until it is, the repository is
   all-rights-reserved by default in most jurisdictions, which contradicts the
   intent of a developer distribution.
2. **Security documentation.** There is no `SECURITY.md` and no disclosure
   process. A tool that runs a language model against your repository and
   executes generated changes in a container needs a stated policy for reporting
   vulnerabilities and a stated threat model. Decide the policy, then add the
   document; do not invent one in a release commit.
3. **Publication channel.** Whether to publish to PyPI, and under which name, is
   undecided. Nothing was uploaded. If a wheel is published later, the version
   and the manifest suite digest must be re-verified against the tagged commit
   first.
4. **Tag and release.** `v0.1.0` exists as a version string only. Creating the tag
   and the GitHub release is a human action performed after this PR is reviewed
   and merged.

## Upgrade notes

- **Store schema.** Schema v2 migrates a v1 store in place on first open; the
  migration is covered by `tests/test_store_migration.py`. Back up a store you
  care about before opening it with 0.1.0.
- **Manifest.** Manifest v2 records a suite digest over `.github/` (excluding
  `.github/workflows/`) and a docs digest over `docs/workflows/*.md` plus
  `consumer-paths.json`. Editing a digested file invalidates previously recorded
  approvals, which is the intent. Documents added at the `docs/` root — including
  this file — are outside digest scope.
- **Declarative consumers.** Re-run `harness_install.py update` to move an
  existing installation to the 0.1.0 commit; conflicting local edits are backed
  up and must be authorized explicitly.

## Known observation

`tools/harness_install.py` extracts its source tarball with `tar.extractall()`
and no `filter` argument, which raises a `DeprecationWarning` on Python 3.12+ and
will change default behaviour in 3.14. It did not affect the installs verified
above. Passing an explicit filter is a small, separate change and was left out of
this documentation-focused release.
