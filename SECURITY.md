# Security policy

This policy covers the `loomsphere-harness` repository: the declarative agent
assets under `.github/`, the standalone runtime in `src/loomsphere_harness/`, the
installer in `tools/harness_install.py`, and the reference sandbox image under
`docker/`.

## Supported versions

| Version | Supported | Notes |
| --- | --- | --- |
| `0.1.x` (release candidate) | Yes | Only the latest commit on `main` and the most recent published pre-release receive fixes. |
| Anything older, or a pinned commit | No | Pinning is supported for reproducibility, but a pin does not receive backports. Upgrade the pin to pick up a fix. |
| The LoomSphere application | Out of scope here | Report application issues to that repository. |

The project is pre-1.0. There is no LTS branch, no backport policy, and no
security-response service-level commitment.

## Reporting a vulnerability

**There is no verified private reporting channel at this commit.** GitHub Private
Vulnerability Reporting is disabled for this repository (checked with the GitHub
API: `private-vulnerability-reporting` returns `{"enabled":false}`). This
document does not publish an email address or claim a private channel exists,
because neither would be a channel anyone monitors.

**Do not open a public GitHub issue, discussion, or pull request that contains
exploit details, proof-of-concept code, or secrets.** That includes reports about
the sandbox boundary, the installer's archive extraction, the approval or
acceptance gates, and credential handling.

### Required action by the repository owner

Enabling private reporting is a repository administration change that an agent
cannot make:

1. Open the repository on GitHub → **Settings** → **Code security and analysis**.
2. Enable **Private vulnerability reporting**.
3. Replace the paragraph above with the enabled channel, and add the advisory
   link `https://github.com/gkamga/loomsphere-harness/security/advisories/new`.

Until step 2 is done, a researcher who finds a security defect should withhold
the details and state only, in a public issue with no technical content, that
they have a report to deliver and need a private channel. Non-sensitive
hardening suggestions and ordinary bugs are welcome as public issues.

### What a report should contain

Once a private channel exists, a useful report includes:

- the affected version, commit SHA, and the file or subsystem involved;
- the configuration needed to reproduce it, with secrets and API keys removed;
- a minimal reproduction, or a precise description of the preconditions;
- the impact you believe it has, and who is exposed;
- whether the issue is already public anywhere;
- an optional disclosure preference and deadline;
- a contact method you are willing to have used.

Never include a live API key, access token, database URL, or private repository
content in a report. Replace them with placeholders.

## What is in scope

- Sandbox escape or a path that executes generated code on the host.
- A way to make the installer write outside its target, follow links, or extract
  a special file.
- Bypassing digest binding, the approval gate, delivery acceptance, or the
  change-set screening in `workspace.py`.
- Leaking `api_key_env`/`api_key_file` material into state, audit records,
  prompts, logs, or the container.
- Trusting an unverified or tampered harness checkout.
- Receiving code or instructions from repository content that silently grants a
  tool the agent should not hold.

## What is out of scope

- Vulnerabilities in `pydantic`, `pydantic-ai`, `pytest`, Docker or Podman, or a
  model provider — report those upstream.
- The security of a model provider's own data handling; see
  [docs/SECURITY-MODEL.md](docs/SECURITY-MODEL.md).
- Prompt-injection findings with no concrete effect beyond producing a bad
  suggestion that a human then approves.
- Anything that requires the attacker to already control the user's shell, the
  Docker daemon, or the trusted harness checkout.
- Reports that consist only of model output text without a demonstrated impact.

## Safe operation

The runtime is designed for disposable or low-trust repositories, and it never
pushes, opens a pull request, or merges. Read
[docs/SECURITY-MODEL.md](docs/SECURITY-MODEL.md) before running it on a repository
that matters; it describes the trust boundaries, the isolation that exists, and
the isolation that does not.
