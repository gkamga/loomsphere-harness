# Security model

What the LoomSphere harness does and does not protect, as implemented in
`src/loomsphere_harness/` at this commit. Every claim below names the code that
enforces it. Controls that do not exist are listed as missing rather than planned.

Read this before running the runtime on a repository whose contents you do not
control. The short version: **the runtime is not a security boundary against a
determined attacker.** It bounds an ordinary engineering agent, records what it
did, and requires two human decisions. It assumes the host, the container
runtime, the harness checkout, and the model provider are trusted.

## 1. Trust boundaries

| Input | Trust | Why |
| --- | --- | --- |
| The harness checkout (`[harness] root`) | **Trusted**, byte-verified | `assets.verify_assets()` compares every asset against `harness.manifest.json` before any task runs, and refuses a root that overlaps the target repository (`harness-root-inside-target`). |
| The target repository's files | **Untrusted content** | The repository is what the harness is asked to change. Files there are read by models as tool results and are never treated as instructions by the runtime itself. |
| Model responses | **Untrusted output** | Parsed into contracts, then checked. Never used to grant a tool, decide an approval, or declare a criterion satisfied. |
| The approval and acceptance decisions | **Human, digest-bound** | Recorded through the local approval gate and bound to a SHA-256 digest. |
| The SQLite state file | **Locally trusted, tamper-evident** | Append-only triggers and a hash chain detect edits made through the application; see §7 for the limit. |
| The container image | **Trusted by digest** | The image ID is resolved with `image inspect` and must start with `sha256:`; evidence is only valid when the observed ID equals the recorded one. |
| The model provider and its endpoint | **Trusted third party** | Everything the agents read is sent there (§3). |

## 2. Trusted asset integrity

`verify_assets()` (`assets.py`) requires `harness.manifest.json` and `.github/`,
resolves both paths, and refuses a harness root that is the target repository or
nested with it. It then checks every manifest asset for containment, for symbolic
links, and for a matching SHA-256 digest. A failure raises `AssetIntegrityError`
with a stable reason code and is audited as `assets.rejected` before the task
stops (`engine.py`).

Consequences worth stating plainly:

- Assets are trusted **because they are pinned, not because they are safe**. A
  malicious but internally consistent harness checkout verifies. Only review of
  the checkout protects you.
- The runtime does not fetch assets over the network. The installer
  (`tools/harness_install.py`) does, but it pins a commit, verifies the archive
  against the manifest of that commit, and refuses links, absolute paths,
  traversal, and special files before extracting.

## 3. What the model provider receives

Intake, Architect, Builder, QA, and Reviewer run through the PydanticAI adapter
(`pydantic_harness.py`) against the endpoint configured in `[model]`. Everything
those roles read with their tools — whole file contents, file listings, the task
description, the approved criteria, and the composed agent, skill, and persona
prompt — is transmitted to that endpoint and retained according to that
provider's terms.

- **No redaction is applied to repository content before it is sent.** A file
  that contains a secret is sent if an agent reads it.
- The file tools refuse a fixed set of paths first
  (`DENIED_PATTERNS`: `.git`, `.env` and `.env.*`, `*.pem`, `*.key`, `id_rsa*`,
  `id_ed25519*`, `.ssh/`, `secrets/`, and the harness state directory). That is a
  guardrail against the obvious cases, not a secret scanner. Secrets in `.json`,
  `.yaml`, `.toml`, `.ini`, source strings, or documentation are not detected.
- Prompt digests, persona digests, and catalog digests are recorded as evidence.
  They let you prove which text was sent; they do not prevent data from being
  sent.
- If your provider or endpoint is not approved for the content of the target
  repository, do not run the runtime against that repository. Nothing in the
  runtime enforces a data-classification policy.

## 4. Prompt injection through repository content

Agent roles read repository content as tool results. A file, issue text copied
into a task description, comment, fixture, or dependency README can attempt to
steer a model: to propose a harmful change, to invent an acceptance criterion, to
report a passing test that did not run, or to exfiltrate content through a
request the model can make.

Implemented limits, and what each actually bounds:

| Control | Bounds | Does not bound |
| --- | --- | --- |
| `tools_for()` returns the read-only tool set unless the role is `builder` **and** `edit` is in the approved tools | A steered Intake, Architect, QA, or Reviewer cannot write files | What a steered model writes in its report |
| Intake and Architect run in a read-only environment (`engine.py` refuses a writable environment) | Direct edits during discovery | Misleading proposals |
| Builder edits only inside a per-task `loomsphere/task-<key>` worktree | Blast radius within the target repository | Content of the committed change |
| Change-set screening (`workspace.py`) rejects unauthorized paths, secret-looking paths, and secret-looking content | Structured secret patterns and paths outside the declared scope | Logic backdoors, injected URLs, subtle test weakening |
| Only allowlisted validation commands run (`validation.py`) | Arbitrary command execution as "validation" | A weakened test file that still passes |
| Digest-bound human approval and acceptance | Nothing lands or is accepted without two explicit decisions | A human approving a convincing but harmful change |

A prompt injection that produces a plausible, human-approved change is within the
threat model of the runtime: the human decision is the control, and it is a
*judgment*, not a proof. Treat model-written rationale and review reports as
adversarial input when reviewing a change.

## 5. Generated-code execution

Code produced by the Builder is never executed on the host. The only execution
path is `SandboxExecutor` (`sandbox.py`), which builds an explicit container
command and runs it with an image resolved to `sha256:`:

```text
--rm  --cap-drop ALL  --security-opt no-new-privileges:true
--user 65534:65534  --read-only  --network none
--tmpfs /tmp:rw,nosuid,nodev,size=256m,mode=1777
--pids-limit 256  --memory 2048m  --memory-swap 2048m  --cpus 2.0
--mount type=bind,source=<worktree>,target=/workspace,ro
-e <allowlisted variables only>
```

- The mount is **read-only by default** (`read_only_source=True`) and the root
  filesystem is read-only; only `/tmp` is writable.
- No `--privileged`, no `--cap-add`, and no Docker socket mount. The container
  never receives a model API key: only an explicit allowlist of variables (`CI`,
  `LANG`, `LC_ALL`, `NO_COLOR`, `PYTHONDONTWRITEBYTECODE`, `PYTHONPATH`,
  `PYTHONUNBUFFERED`, `PYTEST_ADDOPTS`, `TERM`, `TZ`) is forwarded, and an
  unexpected variable raises an error instead of being silently dropped.
- **No host fallback.** If the runtime or image is unavailable, the executor
  raises `SandboxUnavailableError` and the task fails. Nothing "just runs
  locally".
- A timeout force-removes the container; output is truncated before it is
  recorded; the worktree digest before and after must match the expected value
  for the evidence to be bound.

QA and Reviewer are **not** run in the container. They are model calls made
in-process, reading the worktree through the file tools. Only the planned
validation command and approved `pytest` selectors execute inside the sandbox.
Documentation that says otherwise is wrong.

## 6. Container isolation limits and host permissions

- Containers share the host kernel. A kernel or runc vulnerability, or a
  privileged-enough container, can escape. The flags above reduce exposure; they
  do not make execution safe.
- **Docker daemon access is root-equivalent.** Anyone who can talk to
  `/var/run/docker.sock`, or who is in the `docker` group, can mount the host
  filesystem. The runtime assumes you already trust the account running it.
- The image is trusted by digest, not audited. `docker/pytest-sandbox/Dockerfile`
  is three lines (`python:3.12-slim`, one `pip install pytest==8.4.2`, and a
  `WORKDIR`), but the base image's contents and future rebuilds are the
  registry's and Debian's problem as much as ours. Pin and review it if that
  matters to you.
- `--network none` removes network access for the validation command. It does not
  stop the process from writing to a mounted path (the mount is read-only by
  default), from consuming resources up to the limits, or from crashing the
  runtime.
- Podman is supported as a runtime selection but was not exercised in the
  verification recorded in `docs/RELEASE-0.1.0.md`.
- The runtime does not use user namespaces, does not apply seccomp profiles
  beyond the runtime default, and does not sandbox the model provider calls at
  all — those leave the host as ordinary HTTPS.

## 7. Credentials

- The model API key comes from the environment variable named by
  `[model] api_key_env` (default `LOOMSPHERE_MODEL_API_KEY`) or from
  `[model] api_key_file`. A literal secret in the configuration file is refused
  with `config-secret-in-file`: every section rejects top-level keys named
  `api_key`, `key`, `token`, `password`, or `secret`. Nested values are not
  inspected, so a secret hidden under an allowed key's sub-table is not caught.
- `require_api_key` defaults to true: a missing key fails closed instead of
  running unauthenticated.
- The key is never written to the SQLite store, the audit log, the reports, or
  the container environment.
- The commit path (`workspace.py`) refuses secret-looking paths and content
  before committing, and commits with `--no-verify` and
  `core.hooksPath=/dev/null` so repository hooks cannot run as a side effect.
  This is a guardrail against accidental pastes, not a guarantee.
- **Not implemented:** OS keychain or secret-manager integration, per-run
  short-lived credentials, key rotation, and audit of which key was used. A key
  in the environment is readable by any process of the same user.

## 8. Approval and acceptance controls

Both human decisions go through `LocalApprovalGate` (`approval.py`) and are
recorded in the SQLite store (`store.py`).

- **Identity** is `OsUserIdentity`: the operating-system user running the CLI.
  There is no authentication, no session, and no separation between users of one
  machine beyond filesystem permissions. It is attribution, not proof.
- **Binding**: a proposal is approved or rejected against its SHA-256 digest, and
  a delivery against a deliverable digest over the reviewed commit, changed
  files, validation evidence, QA and Reviewer reports, and the frozen acceptance
  contract. Approving a digest does not approve a different later change.
- **Append-only**: `audit_no_update` and `audit_no_delete` triggers refuse
  `UPDATE` and `DELETE` on audit rows, and proposals are immutable. Refused
  approvals are audited too, with their reason code.
- **Tamper-evident, not tamper-proof**: audit rows are chained with
  `sha256(previous_hash + body)`. A user with write access to the state file can
  rewrite or recompute the chain, and the chain has no external anchor. The
  guarantee is "casual edits are detected", not "the record is provable".
- A model cannot approve, reject, or mark a task complete; it can only produce the
  proposal or report that a human decides on.

## 9. Acceptance evidence and model-output limits

- Acceptance criteria carry a verification kind: `pytest`, `protected-pytest`, or
  `unverified`. A mandatory criterion that is `unverified` fails the run
  (`acceptance-obligation-unverified`); it does not pass by default.
- `protected-pytest` is **not available** in this runtime. The approved
  protected-artifact registry is not implemented, so those criteria are recorded
  with `status="unsupported"` and an explicit obstacle message. Do not design an
  acceptance contract that depends on it.
- Evidence is bound only when the observed source digest before and after, and
  the observed sandbox image ID, equal the expected values. An unbound run is
  reported as unverified.
- QA and Reviewer output is a model claim expressed in a schema. Schema
  validation proves the *shape* of a report, not its truth. A truncated model
  response is reported as a failure (`response-truncated`) rather than being
  treated as a short answer.
- The runtime does not prove that a test is a good test, that a change is
  correct, or that a review caught the important defect.

## 10. Operating safely

Recommended, in order of impact:

1. Run the runtime only against repositories you can afford to lose, or against a
   scratch clone. The runtime does not push, open pull requests, or merge.
2. Keep the state file and workspaces **outside** the target repository (they
   default beside the state file), and keep the harness checkout separate from
   the target repository — the runtime refuses an overlapping root.
3. Pin the harness checkout to a commit you reviewed, and pin the sandbox image;
   the image ID, not the tag, is what the evidence records.
4. Give the container runtime only to accounts that already have equivalent host
   access, and review `docker/pytest-sandbox/Dockerfile` before building it.
5. Put no secrets in the target repository. Assume the provider sees any file an
   agent reads.
6. Review the committed change, not the model's summary of it. The screening
   patterns and the validation command are guardrails; the approval decision is
   the control.
7. Treat `protected-pytest` as unavailable when writing acceptance contracts.

## 11. Not implemented

Stated explicitly so no reader infers them: network egress filtering beyond
`--network none` for the validation command; secret scanning of repository
content before it reaches a model; multi-user identity, roles, or authentication;
signed audit records or external log anchoring; key management; per-task
credential scoping; artifact allowlists for protected verification; sandboxing of
the model calls themselves; and any security review by a third party.
