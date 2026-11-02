# loomsphere-harness

The reusable AI engineering harness extracted from
[LoomSphere](https://github.com/gkamga/loomsphere): the trusted agents, skills,
hooks, instructions, contracts, and the procedures that bind them. It is data
first — every file here is Markdown with YAML front-matter or JSON validated
against a JSON Schema — so it can be consumed by any repository, with or without
the LoomSphere application.

**Status: phase 1 (asset extraction).** The declarative harness is here and is
byte-identical to the assets it was extracted from. The portable Python engine
(catalog loader, skill selection, routing, hook evaluator, prompt composition,
runners) still lives in the LoomSphere application and moves in phase 2. See
[Scope and remaining work](#scope-and-remaining-work).

## Layout

```text
.github/
├─ agents/                 7 role definitions (personas) + catalog/ specialists
├─ skills/                 13 SKILL.md procedures
├─ hooks/                  lifecycle hook registry (no hook ships by default)
├─ contracts/              4 JSON Schemas + worked examples
└─ copilot-instructions.md repository instructions for human and agent contributors
harness.manifest.json      inventory: every asset, its digest, its role, one suite digest
tools/build_manifest.py    regenerates and verifies the manifest (stdlib only)
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
| Any repository | Copy `.github/` and verify `harness.manifest.json`; do not edit the copied files, and record the suite digest you pinned. |

## Verifying a checkout

```sh
python3 tools/build_manifest.py --check      # bytes match the committed manifest
python3 tools/build_manifest.py              # regenerate after a reviewed change
```

`--check` fails if any asset changed, was added, or was removed. That matters
because the consuming runtime records these bytes as evidence: the catalog digest
is frozen into the approved route, each skill body digest is stored on the agent
run, and each composed prompt digest identifies the exact text a model received.
A silent edit here invalidates previously recorded verdicts.

## Scope and remaining work

Delivered in phase 1:

- the four asset trees above, extracted with their Git history (`git subtree
  split`, 20 commits preserved);
- byte-for-byte identity with the source assets, proven by SHA-256 over all 41
  files and by the consuming application's prompt-parity fixture;
- an explicit manifest with per-file digests and one suite digest;
- application integration through one new setting, with no change to any queue,
  approval, ownership, or PostgreSQL semantics.

Deliberately deferred:

1. **The Python engine.** `agent_catalog`, `skills`, `skill_selection`,
   `routing`, `lifecycle_hooks`, `implementation_prompts`, `agent_runner`,
   `sandbox`, and the contract validator remain in the application. Moving them
   requires separating a harness-only settings object from the application's
   `Settings` (which currently demands `DATABASE_URL` and `JWT_SECRET_KEY` at
   import), splitting the hook and protected-verification modules into a pure
   half and a durable half, and introducing a principal/persistence seam so a
   single-user consumer needs no `users` row.
2. **Consumer-relative links.** Six link families inside `.github/**` point at
   files a *consumer repository* owns — `README.md`, `docs/workflows/*.md`,
   `docs/decisions/README.md`, `docs/architecture/execution-sandbox.md`,
   `frontend/AGENTS.md`, and `scripts/validate.sh`. They resolve in a consumer
   checkout (the application validates the harness in that shape), but a bare
   checkout of this repository cannot resolve them yet. Phase 2 either ships
   those documents with the harness or replaces the links with a
   consumer-path policy.
3. **Workflow documentation.** `docs/workflows/*.md` stays in the application
   until (2) is decided, so that document links never dangle in either
   repository.
4. **Harness CI.** This repository has no pipeline yet: the validator it would
   run is still in the application (item 1). Until then, `--check` above is the
   only self-contained gate.
5. **Installable distribution.** No package is published. A consumer pins a
   commit and vendors the tree.
