# Lifecycle hooks

This directory is the trusted registry of LoomSphere lifecycle hooks. Stage 4C adds
it so an operator can observe or restrict one of four fixed transitions without
editing the orchestrator.

**No hook ships here by default.** An empty directory means every event continues,
which is why the factory behaves exactly as it did before Stage 4C.

## What a hook may do

A hook decides `continue` or `block`. It can never allow something the factory would
otherwise refuse: the mandatory gates (human scope and delivery approvals, the frozen
route and its required specialists, the trusted skills and tool grants, the protected
acceptance contract, bounded rework, the sandbox and delivery boundaries) run first
and independently. A hook may add a refusal and record an observation; that is all.

## The four events

| Event | When it runs | Effect of a block |
| --- | --- | --- |
| `before-agent-invocation` | Immediately before an agent is invoked | The agent never runs; the run is recorded as failed with reason `hook-denied`. |
| `after-agent-completion` | After the agent answered, before its result is recorded | The result is not recorded; the run is recorded as failed with reason `hook-denied`. |
| `before-review-progression` | After every loop gate passed, before the task enters the human approval queue | The task is blocked and the stage fails with `hook-denied`. |
| `before-delivery` | After every delivery gate passed, before anything is published | The request fails with `HOOK_DENIED` (409) and nothing is pushed. |

## A definition

One JSON file per hook. Every key is required unless it has a default:

```json
{
  "hook_version": 1,
  "name": "freeze-window",
  "version": "1.0.0",
  "label": "Freeze window",
  "description": "Refuse delivery of changes that touch frozen documentation.",
  "events": ["before-delivery"],
  "priority": 100,
  "mode": "blocking",
  "capabilities": ["read-changes"],
  "checks": [
    {
      "kind": "forbidden-changed-paths",
      "reason": "Documentation is frozen for this release window.",
      "path_patterns": ["^docs/release/"]
    }
  ]
}
```

| Key | Meaning |
| --- | --- |
| `hook_version` | The definition format this build understands. Currently `1`. |
| `name` | Stable identity, lower-case kebab-case, unique in the registry. |
| `version` | The hook's own version, recorded on every decision it makes. |
| `events` | One or more of the four events above. |
| `priority` | Lower runs first. Ties are broken by name, so order is deterministic. |
| `mode` | `blocking` (a failed check refuses the transition) or `observer` (a failed check is recorded as an observation; the decision is always `continue`). |
| `capabilities` | What the hook is authorized to read. A check that needs a capability the hook did not declare is refused when the registry loads. |
| `checks` | One to sixteen checks from the vocabulary below. |

## Checks

| Kind | Needs | Fails when |
| --- | --- | --- |
| `task-status` | `read-task` | The task status is outside `allowed_statuses`, or inside `forbidden_statuses`. |
| `forbidden-changed-paths` | `read-changes` | Any recorded changed path matches one of `path_patterns` (regular expressions). |
| `required-evidence` | `read-evidence` | Any listed evidence kind is not recorded for this subject: `implementation`, `validation`, `qa`, `review`, `specialist-reviews`, `acceptance-audit`, `skill-selection`. |
| `required-specialist-verdict` | `read-route`, `read-evidence` | A named specialist has no approving verdict bound to the current implementation. |
| `note` | none | Never. It adds `text` to the recorded observations. |

A `blocking` hook needs a `reason` on every non-note check; the reason is what the
refusal says.

## Trust

The registry is read from this trusted checkout, never from an agent worktree, and
the definitions are data: a hook selects checks from the vocabulary above. Nothing
here can execute arbitrary code, install a package, or reach the network. Hooks
perform no side effects, so a deployment can record their decisions inside the
transaction boundary that dispatches them.

Every decision is stored in `lifecycle_hook_executions` with the hook version, the
event, the subject, the findings, the observations, the payload it saw, and the
digest of the registry that was in force. One subject is decided once, so a retried
stage or a restarted worker reuses the recorded decision instead of deciding again.

Run `scripts/validate.sh --factory` after editing this directory: it checks the
schema, the vocabularies, the capabilities each check needs, and that names are
unique.
