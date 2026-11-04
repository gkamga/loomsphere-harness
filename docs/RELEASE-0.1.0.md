# Release 0.1.0 — developer distribution

## Status: release candidate, not published

This document is the release candidate record for `v0.1.0`. It records what the
code does, how to install it, what was actually executed while preparing it, and
which decisions remain with a human owner. The repository is a **release
candidate**: the workflow is complete and validated, but no tag and no GitHub
Release exist yet, so nothing here is a published distribution.

Deliberately **not** done while preparing this candidate:

- No Git tag was created.
- No GitHub release was published.
- Nothing was uploaded to PyPI.
- No branch was merged.

A license **was** selected and applied (Apache License 2.0, see
[../LICENSE](../LICENSE) and [LICENSING-PROVENANCE.md](LICENSING-PROVENANCE.md)),
and security documentation **was** added ([../SECURITY.md](../SECURITY.md),
[SECURITY-MODEL.md](SECURITY-MODEL.md)). GitHub private vulnerability reporting
was enabled by the repository owner, so the security-reporting prerequisite is
satisfied: `SECURITY.md` now publishes the private advisory link. The version
string `0.1.0` appears in `pyproject.toml` and in the wheel name; no tag named
`v0.1.0` exists locally or on the remote.

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
| Implementation loop | Builder proposes a commit inside an isolated worktree; validation runs in the container; QA and the Reviewer are separate model calls made by the runtime process, **not** processes inside the container. |
| Container-only validation | No host fallback. Docker or Podman, `--network none`, read-only root filesystem, dropped capabilities, non-root user, resource limits, only the task worktree mounted, image pinned by `sha256:` id before execution. The container runs generated code and approved selectors; nothing else. |
| Acceptance verification | `pytest` selectors are executed exactly as approved and bound to the before/after source digests and the image id; a mandatory criterion without that evidence fails the run, and a mandatory `unverified` criterion fails closed. |
| QA and Reviewer | Reviewer reports are validated; `retry-review` re-runs only the Reviewer. |
| Delivery acceptance | `accept --digest` closes the task. The runtime never pushes, opens a pull request, or merges. |
| Tamper-evident record | A hash-chained audit trail, a per-task worktree digest before and after execution, and a recorded refusal reason for every rejected command. |
| Persistence | SQLite store, schema v2, stored outside the target repository. A v1 store migrates in place. |
| Trusted assets | Manifest v2 covering 41 assets, with a suite digest recorded on every approved run. |
| Licensing | Apache License 2.0 in `LICENSE`; `License-Expression: Apache-2.0` and `License-File: LICENSE` in the wheel metadata; the redistribution audit is [LICENSING-PROVENANCE.md](LICENSING-PROVENANCE.md). No `NOTICE` file is required by section 4(d). |
| Installer hardening | `tools/harness_install.py` validates a whole archive before writing anything and refuses absolute paths, parent-directory traversal, symbolic and hard links, device/FIFO/sparse entries, and destinations that escape the extraction root. |

`protected-pytest` is **not implemented in this runtime**. A protected-artifact
criterion resolves to `unsupported` with the recorded obstacle and blocks
readiness; it cannot pass by default.

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

## What was verified while preparing this candidate

Everything below was executed locally on macOS (arm64) with Python 3.11.11,
3.13.1, and 3.14.2, the installer check on Python 3.9.22, and Docker 29.7.2; no
Podman was present. Nothing is reported as passing that was not run.

| Check | Command | Observed result |
| --- | --- | --- |
| Wheel build | `python -m build --wheel --outdir /tmp/lsh-wheel` | `loomsphere_harness-0.1.0-py3-none-any.whl` built |
| Wheel license metadata | inspect `dist-info/METADATA` | `Metadata-Version: 2.4`, `License-Expression: Apache-2.0`, `License-File: LICENSE` |
| Wheel license payload | inspect the archive | `dist-info/licenses/LICENSE`, 201 lines / 11357 bytes, listed in `RECORD` with a sha256 |
| Wheel install, clean venv | install the wheel, then `uv pip check` | `All installed packages are compatible` |
| CLI entry point | `loomsphere-harness --help` | entry point resolves, 14 subcommands listed |
| Full suite, Python 3.11 | `python -m unittest discover -s tests -q` | `Ran 251 tests`, `OK (skipped=2)` |
| Full suite, Python 3.13 | same | `Ran 251 tests`, `OK (skipped=2)` |
| Full suite, Python 3.14 | same | `Ran 251 tests`, `OK (skipped=2)` |
| Installer job, Python 3.9 | `python -m unittest discover -s tests -p 'test_install.py' -q` | `Ran 25 tests`, `OK` — includes 7 archive-extraction security tests |
| Manifest integrity | `python tools/build_manifest.py --check` | `manifest OK: 41 files, suite d63e813839290391e0b3479f50856b33eff8e4f1ae530134bb0bbd5dddd85fc7` |
| Asset validation | `python tools/validate_harness.py` | `10 check groups, 0 error(s), 0 warning(s)` |
| Private vulnerability reporting | `gh api repos/gkamga/loomsphere-harness/private-vulnerability-reporting` | `{"enabled": true}` — the advisory link is published in `SECURITY.md` |
| Declarative install | `harness_install.py install --ref <sha> --source <checkout>` | `installed 44 files from 5217c86d… (suite d63e8138…)` |
| Declarative verify | `harness_install.py verify --strict --source <checkout>` | `verify OK: 44 files match 5217c86d…` |
| Asset discovery | `harness_install.py discover --json` | agents, skills, and hooks enumerated |
| Declarative uninstall | `harness_install.py uninstall` | `uninstalled; original files restored`; target `git status` clean |
| Sandbox validation smoke | `docker run` with the runtime's exact flags, `pytest -q` inside | `2 passed`; image id `sha256:024910f96cf9cb958cf3c35f547e4bf2b096ba9a1a278de840e83165f52b29e0` |
| CI | GitHub Actions on the pull request | five jobs — suite on 3.11/3.13/3.14, installer on the oldest supported Python, and manifest/assets/contracts/links/consumption — recorded on the PR checks for the head commit |

Disposable repositories, no live model call, and a synthetic `pytest` fixture were
used. No model provider was contacted, so model-backed stages (Intake, Architect,
Builder, QA, Reviewer) are exercised by the test suite with fakes only; a live
end-to-end run remains an operator exercise against a real provider.

CI results are deliberately not transcribed here as a run id. This document is
committed to the branch it describes, so any run id written into it would name a
commit that is no longer the head. The authoritative record is the PR check list
for the final commit.

### Not verified here

- Podman was not installed, so only the Docker path was executed live. The
  runtime treats Podman as an alternative container engine; that branch is
  covered by the test suite, not by a live run.
- No live model provider was contacted.
- No live run of `protected-pytest`: the verification route is unimplemented in
  this runtime and reports `unsupported`.

## Decisions a human must make before publishing

Recorded so that neither list is mistaken for an implicit default.

Resolved for this candidate:

1. **License — resolved.** Apache License 2.0 is applied in `LICENSE`, declared in
   `pyproject.toml` as an SPDX expression with `license-files`, and carried into
   the wheel metadata. The redistribution audit that supports it is
   [LICENSING-PROVENANCE.md](LICENSING-PROVENANCE.md) and found no unresolved
   file-level blocker. No `NOTICE` file is included: no third-party material is
   redistributed and no existing notice needed carrying forward, so section 4(d)
   imposes no obligation.
2. **Security documentation — resolved.**
   [../SECURITY.md](../SECURITY.md) states the supported versions, the disclosure
   process, and what a report should contain;
   [SECURITY-MODEL.md](SECURITY-MODEL.md) describes the implemented trust
   boundaries and their limits. GitHub private vulnerability reporting is enabled
   on this repository (verified with the GitHub API), and `SECURITY.md` publishes
   the private advisory link.

Still open:

3. **Publication channel.** Whether to publish to PyPI, and under which name, is
   undecided. Nothing was uploaded. If a wheel is published later, rebuild it from
   the tagged commit and re-check its metadata and suite digest first.
4. **Tag and release.** `v0.1.0` exists as a version string only. Creating the tag
   and the GitHub release is a human action performed after this change is
   reviewed and merged.

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

## Installer archive extraction

The declarative installer reads `git archive <commit>` and unpacks it itself. It no
longer calls `tar.extractall()`: the original implementation relied on an
implicitly trusted archive and raised a `DeprecationWarning` on Python 3.12+.
It now validates **every** member before writing **any** of them, and refuses:

- absolute paths and any entry containing `..`;
- symbolic links and hard links;
- device nodes, FIFOs, and other non-regular, non-directory types;
- an entry whose resolved destination leaves the extraction root, including an
  entry that traverses a directory created by an earlier member.

The implementation uses only the `tarfile` API available on every supported
interpreter down to Python 3.9, so it needs no `filter=` argument and behaves the
same on 3.9 and 3.14. Modes are preserved, with `0o644` as the fallback for an
entry that declares none. Seven regression tests cover a clean archive plus each
refusal, one of them end to end through `harness_install.py install`; they run in
the Python 3.9 installer job as well as on the newer interpreters.

This changed nothing else: path resolution, the manifest verification performed
before extraction, conflict handling, locking, and uninstall are untouched, and
the install/verify/uninstall results above were produced by the hardened code.
