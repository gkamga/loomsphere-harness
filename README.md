# loomsphere-harness

The reusable AI engineering harness extracted from
[LoomSphere](https://github.com/gkamga/loomsphere): the trusted agents, skills,
hooks, instructions, contracts, and the procedures that bind them. It is data
first — every file here is Markdown with YAML front-matter or JSON validated
against a JSON Schema — so it can be consumed by any repository, with or without
the LoomSphere application.

**Status: phase 2B (independent consumption and CI).** The declarative harness is
here, byte-identical to the assets it was extracted from, self-validating in CI,
and installable into any Git repository with [one script](docs/CONSUMING.md). The
portable Python engine (catalog loader, skill selection, routing, hook evaluator,
prompt composition, runners) still lives in the LoomSphere application and is a
later phase. See [Scope and remaining work](#scope-and-remaining-work).

## Layout

```text
.github/
├─ agents/                 7 role definitions (personas) + catalog/ specialists
├─ skills/                 13 SKILL.md procedures
├─ hooks/                  lifecycle hook registry (no hook ships by default)
├─ contracts/              4 JSON Schemas + worked examples
└─ copilot-instructions.md repository instructions for human and agent contributors
docs/workflows/            lifecycle, execution-contract, and evidence documents the assets link to
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
a permission. The deterministic gates — human scope approval, delivery approval,
the frozen route, the sandbox, the acceptance audit — are enforced by application
code in the consuming repository, never by anything in this repository. A file
here may be wrong, ignored, or overridden by a model update; a gate cannot be.

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

Still deferred:

1. **The Python engine.** `agent_catalog`, `skills`, `skill_selection`,
   `routing`, `lifecycle_hooks`, `implementation_prompts`, `agent_runner`,
   `sandbox`, and the contract validator remain in the application. Moving them
   needs a harness-only settings object, a pure/durable split of the hook and
   protected-verification modules, and a principal/persistence seam.
2. **Application documents.** `acceptance-contract.md` and the historical legacy
   reconciliation describe LoomSphere code and stay in the application.
3. **Distribution.** No package is published. A consumer pins a commit or tag and
   installs with the script.
