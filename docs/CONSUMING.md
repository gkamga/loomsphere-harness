# Consuming the harness from another repository

This guide installs the **declarative harness assets** into any Git repository.
It needs Python 3.9+ and Git. It does not need the LoomSphere application.

**What you get:** agent definitions, skills, a hook registry, contract schemas
with worked examples, and the workflow documents they link to, as plain files in
your repository.

**What you do not get:** an autonomous engine. Nothing here runs an agent,
selects a skill, composes a prompt, evaluates a hook, enforces an approval, or
sandboxes anything. The assets constrain whatever tool loads them; they cannot
enforce a gate on their own.

If you want the runtime that *does* enforce those gates — Intake, Architect,
digest-bound human approval, Builder, container-only validation, QA, Reviewer,
and delivery acceptance — install the CLI instead and follow
[docs/QUICKSTART.md](QUICKSTART.md). The two modes are independent: you can use
either, or install the assets for Copilot and run the CLI from a separate trusted
checkout.

## 1. Pin a version

Pin a **full commit SHA** or a **tag**. Branch names are refused because a branch
moves. Optionally also pin the asset suite digest, which fails the install if the
reviewed content is not what arrives:

```sh
git clone https://github.com/gkamga/loomsphere-harness.git
cd loomsphere-harness
git rev-parse v0.1.0                              # the tag you reviewed, or a full SHA
python3 -c "import json;print(json.load(open('harness.manifest.json'))['suite_digest'])"
```

If no tag is published yet, use the full commit SHA (`git rev-parse HEAD` on the
reviewed commit). A tag and a commit SHA are interchangeable here; a branch name
is not.

## 2. Install

From the harness checkout, aimed at your repository:

```sh
python3 tools/harness_install.py install \
  --target /path/to/your/repo \
  --ref <commit-or-tag> \
  --expect-suite-digest <suite_digest>          # optional but recommended
```

Or without a local checkout: `--url https://github.com/gkamga/loomsphere-harness.git`.
Add `--dry-run` first to see the plan.

The installer reads `git archive <commit>`, so the bytes are exactly those of the
pinned commit and your harness checkout is never modified. It first verifies the
archive against that commit's `harness.manifest.json`.

| Bucket | Installed to | Default |
| --- | --- | --- |
| `agents` | `.github/agents/` (including `catalog/`) | yes |
| `skills` | `.github/skills/<name>/SKILL.md` | yes |
| `hooks` | `.github/hooks/` | yes |
| `contracts` | `.github/contracts/` | yes |
| `docs` | `docs/workflows/` | yes |
| `instructions` | `.github/copilot-instructions.md` | **no** (opt-in) |

Select with `--bucket` (repeatable). Instructions are opt-in because most
repositories already have their own; the harness instructions describe
LoomSphere's rules, so review them before adopting them.

It writes `.harness-lock.json` at the repository root: the pinned commit, the
suite digest, and a SHA-256 for every installed file. Commit it.

### Conflicts are never overwritten silently

If a destination exists and differs, **nothing is written** and each conflict is
listed (exit code 3). To replace a file, authorize it explicitly:

```sh
... --authorize-overwrite .github/copilot-instructions.md    # repeatable, one path each
... --authorize-all-overwrites                                # every conflict
```

Authorized originals are copied to `.harness-backup/` and restored by
`uninstall`. A file that already equals the harness file is adopted and is never
removed by `uninstall`.

## 3. Discovery

**GitHub Copilot (native).** Copilot reads `.github/agents/*.agent.md`,
`.github/skills/<name>/SKILL.md`, and `.github/copilot-instructions.md` from the
repository root. Installing at the repository root is all that is needed. A harness
checkout nested in a submodule is not discovered natively; install or project it.

**DeepSeek Harness (explicit).** Point the skill filesystem at the installed tree;
no copy is needed:

```json
{ "customSkillDirs": ["/path/to/your/repo/.github/skills"] }
```

**Deterministic check (no tooling needed):**

```sh
python3 tools/harness_install.py discover --target /path/to/your/repo [--json]
```

It lists the agents, catalog specialists, and skills, checks that every skill
directory matches its `name`, that each agent has a `name` and `description`, and
that every skill an agent declares is present. Exit code 1 reports a problem.

## 4. Validate the installation

```sh
python3 tools/harness_install.py verify --target /path/to/your/repo \
  --source /path/to/loomsphere-harness --strict
```

`verify` fails on a modified, missing, or replaced-by-symlink file. `--source`
also checks the lock itself against the pinned commit's manifest, so a forged
lock is caught. `--strict` also fails on unmanaged files inside managed trees.

## 5. Update to another reviewed version

Review the new commit first (diff its `harness.manifest.json` and the changed
files), then:

```sh
python3 tools/harness_install.py update --target /path/to/your/repo --ref <new-commit-or-tag>
```

Unmodified files are replaced, files the new version no longer ships are removed,
and any file you edited is a conflict that needs authorization. The previous
commit is recorded in the lock's `history`.

## 6. Revert

- **To a previous harness version:** `update --ref <previous-commit>`.
- **Remove the harness:** `uninstall --target ...` removes unmodified installed
  files, restores authorized-overwrite backups, and deletes the lock. Files you
  modified are left in place and reported; `--force` removes them too.
- Because the target is a Git repository, `git restore`/`git revert` also work.

## Consumer-owned paths

Some assets link to files your repository owns: the ADR governance document,
your validation script, your frontend guidance, your sandbox description, your
README. The harness never ships or installs them. They are listed in
[consumer-paths.json](../consumer-paths.json) with who references them and what
happens if they are absent. Provide an equivalent, or accept that the link
dangles. `python3 tools/validate_harness.py` validates the harness against
generated placeholders for exactly those paths; `--bare` shows the unresolved
set in a plain checkout.

## Integrity and digests

`harness.manifest.json` carries two independent digests:

- `suite_digest` covers every file under `.github/` (excluding this repository's
  own `.github/workflows/`). Recorded runs cite it, so it changes only when an
  agent, skill, hook, contract, or instruction changes.
- `docs_digest` covers `docs/workflows/*.md` and `consumer-paths.json`. A
  documentation edit changes this digest and leaves `suite_digest` alone.

Regenerate with `python3 tools/build_manifest.py` after a reviewed change; CI runs
`--check`.

## Declarative assets versus the enforcing runtime

The assets and the runtime answer different questions.

| | Declarative assets (this guide) | Standalone runtime ([QUICKSTART.md](QUICKSTART.md)) |
| --- | --- | --- |
| What is installed | Markdown and JSON files | A Python package and the `loomsphere-harness` CLI |
| Who enforces a gate | The consuming repository or tool | The runtime itself |
| Model tools | Whatever the tool grants | Intake/Architect: read-only; Builder: isolated worktree |
| Approval | None installed | Proposal digest and deliverable digest, recorded |
| Execution sandbox | None | Container only, `--network none`, read-only root |
| Persistence | None | One SQLite file outside the target repository |
| Works without the LoomSphere application | Yes | Yes |

Catalog loading, skill selection, prompt composition, lifecycle hook evaluation,
agent runners, the sandbox, approval, and the audit trail all exist in the runtime.
Installing only the assets gives a model the text; running the CLI makes the gates
binding. The runtime reads the assets from a **separate trusted checkout**, never
from the repository it is working on.
