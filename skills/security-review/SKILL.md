---
name: security-review
description: Review one recorded change set for security defects in credentials, authorization, trust boundaries, and sandboxed execution.
roles: [security-reviewer]
stages: [security-review]
---

# Security Review

Use this skill when a change set has been routed to the Security Reviewer, either
because the approved scope is security sensitive or because the recorded diff
touched a security-sensitive path that the approval did not predict.

This is a review of *recorded evidence*, not a penetration test. The orchestrator
gives you a diff, a changed-file list, a validation result, and scope
observations. You cannot run code, reach a network, or read a secret. Report only
what those records show.

## What to inspect, in this order

1. **Trust boundaries.** Which values cross from an untrusted source into a
   privileged operation: request input into a query, a path, a shell command, a
   template, or a redirect target?
2. **Authentication and authorization.** Does the change weaken a check, widen a
   scope, drop an audience or expiry check, or make a privileged path reachable
   without its guard?
3. **Credentials and secrets.** Any new literal token, key, password, or
   connection string, and any logging or error path that would print one.
4. **Cryptography.** A hand-rolled primitive, a downgraded algorithm, a constant
   time comparison replaced by a naive one, or a nonce or salt reused.
5. **Execution boundary.** Anything that would let generated code escape the
   sandboxed validation boundary: writing outside the worktree, disabling the
   container flags, or overriding the configured command.
6. **Data exposure.** Broader serialization of an internal record, a new endpoint
   field that returns a secret, or an error detail that leaks internals.

## Severity is a decision, not a mood

- `blocking`: the change must not proceed. Use it for a reachable defect in the
  five categories above.
- `major`: a real weakness that should be fixed before this code is relied on,
  where the immediate impact is not reachable in this change.
- `minor`: hardening. Put these in `follow_ups` when they are out of scope.

## Evidence discipline

- Name the file and the construct you inspected, and quote the observed line in
  `evidence`. "Looks fine" is not evidence.
- If a security-sensitive path is named in the prompt but you could not inspect it,
  report it as not inspected with the reason. An un-inspected path is not a pass.
- Do not approve because the tests pass. A green suite shows the code does what
  its author asserted, which is exactly what a security review must not assume.
- Do not restate or renumber the approved acceptance criteria. Application code
  decides compliance against the frozen contract; your verdict is about security.

## Output

- `verdict`: `approved`, `changes-required`, or `rejected`
- `summary`: what you inspected and what you concluded
- `areas_reviewed`: the security-sensitive areas you actually inspected
- `findings`: severity, description, and the evidence you observed
- `follow_ups`: hardening that does not block this change

## Useful references

- [../engineering-review/SKILL.md](../engineering-review/SKILL.md)
- [../../copilot-instructions.md](../../copilot-instructions.md)
- [../../../docs/architecture/execution-sandbox.md](../../../docs/architecture/execution-sandbox.md)
