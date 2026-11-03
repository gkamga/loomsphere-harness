# loomsphere-harness

The reusable AI engineering harness extracted from
[LoomSphere](https://github.com/gkamga/loomsphere): the trusted agents, skills,
hooks, instructions, contracts, and the procedures that bind them. It is data
first — every file here is Markdown with YAML front-matter or JSON validated
against a JSON Schema — so it can be consumed by any repository, with or without
the LoomSphere application.

**Status: phase 2C.1 (standalone Intake → Architect → human approval).** The
declarative harness remains byte-verified and installable into any Git repository
with [one script](docs/CONSUMING.md). This phase adds a Python package and CLI that
reuse portable LoomSphere catalog, prompt, routing, hook, schema, and runner
components ([docs/RUNTIME.md](docs/RUNTIME.md)). Builder/QA/Reviewer, the database
queue, API, and authentication stay in the LoomSphere application.

## Layout

```text
.github/
├─ agents/                 7 role definitions (personas) + catalog/ specialists
├─ skills/                 13 SKILL.md procedures
├─ hooks/                  lifecycle hook registry (no hook ships by default)
├─ contracts/              4 JSON Schemas + worked examples
└─ copilot-instructions.md repository instructions for human and agent contributors
docs/workflows/            lifecycle, execution-contract, and evidence documents the assets link to
docs/RUNTIME.md            standalone runtime and CLI (phase 2C.1)
src/loomsphere_harness/    the portable runtime package (pyproject.toml)
docs/CONSUMING.md          install, pin, verify, update, and revert in another repository
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

It **is not** authorization. Loading a skill grants no tool, and a persona is not
a permission. The standalone CLI grants the model only read tools and stops at an
explicit proposal-digest-bound human decision; it cannot start implementation.
LoomSphere's scope approval, delivery approval, frozen route, sandbox, and
acceptance audit remain enforced by the application. A file here may be wrong,
ignored, or overridden by a model update; a gate cannot be.

## Consuming it

The consuming application owns the gate enforcement and reads these files from a
trusted local checkout — never from a worktree an agent may write to.

| Consumer | How it binds |
| --- | --- |
| LoomSphere application | `HARNESS_ROOT` points at a trusted checkout of this repository; the catalog, definition, skills, hooks, and persona settings derive from it. |
| GitHub Copilot in a repository | `.github/agents/*.agent.md` and `.github/copilot-instructions.md` are read natively when this tree is present in that repository. |
| DeepSeek Harness | `@deepseek-ai/dsh-skill-filesystem` discovers `SKILL.md` bundles; point `customSkillDirs` at `<harness>/.github/skills` so no copy is needed. |
| Any repository | `python3 tools/harness_install.py install --target REPO --ref <commit>`; see [docs/CONSUMING.md](docs/CONSUMING.md). Never overwrites existing files without explicit authorization; `verify`, `update`, and `uninstall` are included. |

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
- **Standalone workflow:** SQLite persistence and audit evidence, explicit
  identity, repository, approval, and read-only execution-environment seams. The
  only runnable stages are Intake and Architect; the workflow stops at human
  approval or rejection of the exact proposal digest.
- **LoomSphere consumption:** the application imports the shared portable policy
  and data types while retaining its PostgreSQL-backed queue, authentication,
  database hook records, API, and later lifecycle gates.

Still deferred:

1. **Implementation stages (Phase 2C.2):** Builder, QA, Reviewer, sandbox
   execution, protected verification, delivery, and the application’s existing
   implementation queue. This milestone does not authorize or implement them.
2. **Application-owned concerns:** database operations, API endpoints,
   authentication, ownership, application scope approval, and hook payloads or
   durable hook records that depend on those concerns.
3. **Distribution:** no package is published; consumers pin a commit or tag and
   install declarative assets with the script.
