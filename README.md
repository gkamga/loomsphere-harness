# loomsphere-harness

The reusable AI engineering harness extracted from
[LoomSphere](https://github.com/gkamga/loomsphere): the trusted agents, skills,
hooks, instructions, contracts, and the procedures that bind them. It is data
first — every file here is Markdown with YAML front-matter or JSON validated
against a JSON Schema — so it can be consumed by any repository, with or without
the LoomSphere application.

**Status: v0.1.0 release candidate — the complete standalone workflow is available
in this repository, but no tag or GitHub Release exists yet.** Until one does,
treat the contents as a candidate and install from a commit SHA; see
[docs/RELEASE-0.1.0.md](docs/RELEASE-0.1.0.md).

```
Intake → Architect → recorded human approval → Builder
      → sandboxed validation → QA → Reviewer → recorded human acceptance
```

Every stage runs from this repository alone, with no LoomSphere frontend, backend,
or control-plane database ([docs/RUNTIME.md](docs/RUNTIME.md)). The declarative
harness remains byte-verified and installable into any Git repository with
[one script](docs/CONSUMING.md). Start with
[docs/QUICKSTART.md](docs/QUICKSTART.md).

There are two consumption modes, and they do not offer the same guarantees:

| Mode | What you install | What is actually enforced |
| --- | --- | --- |
| **A. Declarative harness** | Agent definitions, skills, hooks, contracts, instructions, and workflow docs as plain files ([docs/CONSUMING.md](docs/CONSUMING.md)) | Nothing by itself. The consuming repository or tool reads the text and decides what a model may do. No engine, sandbox, or approval gate is installed. |
| **B. Autonomous software factory** | The `loomsphere-harness` Python CLI and its runtime ([docs/RUNTIME.md](docs/RUNTIME.md)) | Approval, digest binding, trusted assets, an isolated worktree, container-only validation, and delivery acceptance are enforced by the runtime. |

## Layout

```text
.github/
├─ agents/                 7 role definitions (personas) + catalog/ specialists
├─ skills/                 13 SKILL.md procedures
├─ hooks/                  lifecycle hook registry (no hook ships by default)
├─ contracts/              4 JSON Schemas + worked examples
└─ copilot-instructions.md repository instructions for human and agent contributors
docs/workflows/            lifecycle, execution-contract, and evidence documents the assets link to
docs/QUICKSTART.md         end-to-end onboarding for the standalone factory
docs/RUNTIME.md            standalone runtime, configuration, and CLI reference
src/loomsphere_harness/    the portable runtime package (pyproject.toml)
docs/CONSUMING.md          install, pin, verify, update, and revert declarative assets
consumer-paths.json        the files a consuming repository owns that the assets link to
harness.manifest.json      inventory: every asset, its digest, its role, suite and docs digests
tools/build_manifest.py    regenerates and verifies the manifest (stdlib only)
tools/validate_assets.py   the asset validator (PyYAML, jsonschema)
tools/validate_harness.py  runs it against the consumer shape (placeholders for consumer paths)
tools/harness_install.py   install / verify / update / uninstall / discover (stdlib + git)
tests/                     validation semantics and independent-consumption tests
```

The `.github/` prefix is deliberate: the agent definitions link into the sibling
skill tree (`../skills/<name>/SKILL.md`), GitHub Copilot reads `.github/agents/`
and `.github/copilot-instructions.md` natively, and the LoomSphere application's
existing settings already resolve to these paths.

## What the harness is, and what it is not

It **is** the text and the data that constrain an engineering agent: which roles
exist, what tools each may hold, which procedure applies to which role and stage,
which specialist review a change requires, which lifecycle hooks may refuse a
transition, and which contract a handoff must satisfy.

It **is not** authorization by itself. Loading a skill grants no tool, and a
persona is not a permission. The declarative mode installs text only; whatever
environment loads it decides what a model may do. The standalone runtime is the
enforcement point: Intake and Architect hold only read tools, implementation runs
in an isolated worktree inside a networkless container, and both human decisions
are bound to a SHA-256 digest. Neither mode can push, open a pull request, or
merge. A file here may be wrong, ignored, or overridden by a model update; a gate
cannot be.

## Consuming it

The consuming application owns the gate enforcement and reads these files from a
trusted local checkout — never from a worktree an agent may write to.

| Consumer | How it binds |
| --- | --- |
| LoomSphere application | `HARNESS_ROOT` points at a trusted checkout of this repository; the catalog, definition, skills, hooks, and persona settings derive from it. |
| GitHub Copilot in a repository | `.github/agents/*.agent.md` and `.github/copilot-instructions.md` are read natively when this tree is present in that repository. |
| DeepSeek Harness | `@deepseek-ai/dsh-skill-filesystem` discovers `SKILL.md` bundles; point `customSkillDirs` at `<harness>/.github/skills` so no copy is needed. |
| Any repository | `python3 tools/harness_install.py install --target REPO --ref <commit>`; see [docs/CONSUMING.md](docs/CONSUMING.md). Never overwrites existing files without explicit authorization; `verify`, `update`, and `uninstall` are included. |
| Developer workstation | `pip install .` from a checkout (or a locally built wheel), then run `loomsphere-harness`; see [docs/QUICKSTART.md](docs/QUICKSTART.md). The package is not published to PyPI. |

## Verifying a checkout

```sh
python3 tools/build_manifest.py --check      # bytes match the committed manifest
python3 tools/build_manifest.py              # regenerate after a reviewed change
```

`--check` fails if any asset changed, was added, or was removed. That matters
because the consuming runtime records these bytes as evidence: the catalog digest
is frozen into the approved route, each skill body digest is stored on the agent
run, and each composed prompt digest identifies the exact text a model received.
A silent edit here invalidates previously recorded verdicts. `suite_digest` covers
`.github/`; `docs_digest` covers `docs/workflows/` and `consumer-paths.json`, so a
documentation edit never changes the digest that recorded runs cite.

```sh
python3 tools/validate_harness.py            # unmodified validator, consumer shape
python3 -m unittest discover -s tests        # validation semantics + consumption
```

## License and security

Apache License 2.0 — see [LICENSE](LICENSE). The authorship and provenance audit
that cleared the repository for redistribution is
[docs/LICENSING-PROVENANCE.md](docs/LICENSING-PROVENANCE.md). No `NOTICE` file is
required or included: the repository redistributes no third-party material, and
the declared dependencies keep their own upstream licenses (all permissive).

What this harness does and does not protect you from — trust boundaries, model
data exposure, generated-code execution, container isolation limits, credential
handling, and the approval and acceptance controls — is in
[docs/SECURITY-MODEL.md](docs/SECURITY-MODEL.md). How to report a vulnerability is
in [SECURITY.md](SECURITY.md).

## Scope and remaining work

Delivered in phase 1: the four asset trees, extracted with their Git history,
byte-identical to the source, with an explicit manifest, and no change to any
queue, approval, ownership, or PostgreSQL semantics in the application.

Delivered in phase 2B:

- **Consumer-relative links resolved.** `docs/workflows/` (the lifecycle,
  execution-contract, and evidence documents) now lives here, so the links to it
  resolve in a bare checkout. The five remaining link families name files a
  consumer owns; they are declared in [consumer-paths.json](consumer-paths.json)
  instead of assumed, and a test fails if an asset depends on an undeclared one.
  No asset byte changed, so `suite_digest` is unchanged.
- **Harness CI** ([.github/workflows/ci.yml](.github/workflows/ci.yml)): manifest
  integrity, agents, skills, hooks, contract schemas and examples, links, and the
  installation tests. It needs only Python and Git.
- **Installation** with pinning, conflict detection, verification, update, and
  uninstall ([docs/CONSUMING.md](docs/CONSUMING.md)).

Delivered in phase 2C.1:

- **Portable runtime package and CLI** ([docs/RUNTIME.md](docs/RUNTIME.md)):
  catalog and skill loading/selection, prompt composition, Intake and Architect
  contracts, PydanticAI/OpenAI-compatible execution, portable hook loading and
  evaluation, and deterministic specialist routing.
- **Standalone intake workflow:** SQLite persistence and audit evidence, explicit
  identity, repository, approval, and read-only execution-environment seams.
  Intake and Architect stop at human approval or rejection of the exact proposal
  digest.
- **LoomSphere consumption:** the application imports the shared portable policy
  and data types while retaining its PostgreSQL-backed queue, authentication,
  database hook records, API, and later lifecycle gates.

Delivered in phase 2C.2 (this release):

- **Implementation loop** (`implement`): Builder writes only inside an isolated
  `task-<key>` worktree, the runtime commits and screens the change set, a
  container runs the planned validation command with `--network none` and a
  read-only root filesystem, and QA and the Reviewer then report against the
  approved criteria as separate model calls by the runtime — they are not
  processes inside that container. Rework iterates on the same base commit up to
  `[implementation] max_iterations`.
- **Delivery acceptance** (`changes`, `evidence`, `accept`, `reject-delivery`):
  the reviewed commit, changed files, validation evidence, QA and Reviewer
  reports, and the frozen acceptance contract are hashed into one deliverable
  digest, and the second human decision is bound to that digest.
- **Acceptance verification:** every approved `pytest` acceptance selector is
  executed exactly as approved, with collection checks, source and image
  digests, and explicit `unverified` handling. A mandatory criterion without
  matching evidence fails closed. `protected-pytest` is not implemented in this
  runtime; such a criterion resolves to `unsupported` and blocks acceptance
  rather than passing by default.
- **Store schema v4:** an existing v1, v2, or v3 SQLite state file migrates in
  place, gaining the append-only `builder_restarts` table.
- **Reviewer retry** (`retry-review`): one retry of only the Reviewer after an
  `invalid-review-report` failure.
- **Builder recovery** (`inspect-recovery`, `recover-builder`): a Builder that
  fails after writing usable files no longer strands them. The runtime screens and
  commits the surviving work at failure time, `inspect-recovery` shows it without
  changing anything, and `recover-builder --snapshot-digest` lets a human authorize
  one recovery of exactly that change set. The Builder is not re-run; the recorded
  work goes through the same validation, QA, Reviewer, and human acceptance gates,
  and the deliverable states that no Builder report or Builder test run exists.
- **Builder restart** (`restart-builder`): a Builder that timed out or was
  interrupted *before writing anything* leaves no work to recover, so recovery is
  the wrong tool. `restart-builder --failed-run` lets a human authorize exactly one
  fresh Builder attempt for that task. It is refused unless the task is
  `implementation_failed`, the original approval and acceptance contract are
  unchanged, the failed run's reason is an interruption, no Builder report or
  deliverable exists, the worktree is empty of committed, uncommitted, untracked,
  and ignored changes, the task is still on its approved commit, and no restart was
  already authorized. The failed run, its iteration, and the audit chain are kept
  as they were; the authorization is recorded beside them, and the reused
  implementation loop runs the same validation, QA, Reviewer, and human acceptance
  gates.

Not delivered, and not implied by this release:

1. **Packaging and publication:** no PyPI publication and no Git tag. Install
   from a commit or checkout, or build a wheel locally. See
   [docs/RELEASE-0.1.0.md](docs/RELEASE-0.1.0.md).
2. **Security posture:** the harness is documented, not audited. There is no
   third-party security review and no coordinated-disclosure programme, though
   [SECURITY.md](SECURITY.md) publishes the private reporting channel and the
   supported-version policy.
3. **Application-owned concerns:** database operations, API endpoints,
   authentication, ownership, application scope approval, the application's
   implementation queue, and hook payloads or durable hook records that depend on
   those concerns remain in the LoomSphere application.
