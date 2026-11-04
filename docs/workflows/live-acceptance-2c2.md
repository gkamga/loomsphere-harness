# Phase 2C.2 live acceptance (DeepSeek via OpenRouter)

Date: 2026-11-03. Result: the initial Reviewer report failed; a controlled
Reviewer retry succeeded and T-0003 reached **`awaiting_acceptance`**. No human
acceptance was recorded.
Model at every stage: `openrouter.ai/deepseek/deepseek-v4.1-flash-1017`
(provider `openai-compatible`, approved host `openrouter.ai`). Python 3.14.2,
pydantic-ai 2.65.0, pydantic-ai-harness 0.65.0. No credentials appear here.

## Reproducible procedure

1. Credential file outside the repo, mode `600` (never printed or committed).
2. Disposable Git repo with `README.md` and a frozen `tests/test_acceptance_slugify.py`
   (stdlib `unittest`, 7 tests); record its SHA-256 before any Builder run.
3. External `config.toml` (mode `600`): `[model]` as above with
   `api_key_file`, `request_limit=60`, `tool_call_limit=120`,
   `total_token_limit=600000`, `max_output_tokens=16000`, `timeout_seconds=600`;
   `[state] dir` and `[workspace] root` outside the repo;
   `[sandbox] image="python:3.12-slim" runtime="docker" network="none"`;
   `[validation] allowlist=[["python","-m","unittest","discover","-s","tests"]]`;
   `[implementation] max_iterations=2`.
4. `init`, `submit --requirements-file --acceptance-file`, `run` (Intake, Architect), `show`.
5. Stop; a human runs `approve <task> --digest <proposal sha256>`.
6. `implement`; then `changes`, `evidence`, `audit`; human `accept`/`reject-delivery`.

The requirement must tell the Architect to use `pytest`-kind verifications
(see defect 1). The sandbox image must already be present locally.

## Outcome (task T-0003)

| Stage | Status | Tokens in/out |
|---|---|---|
| Intake | succeeded | 44312 / 12080 |
| Architect | succeeded | 34016 / 12334 |
| Builder (tools incl. `write_file`) | succeeded | 54211 / 10117 |
| Validation (sandbox) | exit 0, 20 tests (7 frozen + 13 Builder) | n/a |
| QA | passed, no findings | 31605 / 4328 |
| Reviewer | initial `invalid-review-report`; controlled retry succeeded | initial 44555 / 11190 |

- Proposal digest approved by the local user: `1fd856119628bc311a8a405bb1adc62136c7bf971f9ac540ddd2d022f67aeb98`.
- Generated commit `a1b0a660c8cd` on `loomsphere/task-t-0003` (base `880ec95a1b85`):
  `slugify.py` (+45), `tests/test_slugify.py` (+130). The main checkout stayed clean.
- Frozen test SHA-256 unchanged: `f8bb81c21bd05eda1d8fcdcc64af9ee8021e5a3aa410ce354d94311419745902`.
- Sandbox record: image `python:3.12-slim` = `sha256:ff710677e697054c516f12c53c92166f1aef075f21b2a593f4c615e522112b4e`,
  `network=none`, `read_only_source=true`, user `65534:65534`, 2 CPU / 2048 MB / 256 pids,
  source digest before = after (`7977b702…5155`), output digest `0e73b611…89b6`.
  The image is Python 3.12 (no 3.14 image existed locally) and has no pytest.
- Audit chain verified (30 events) at the initial failure. The later Reviewer
  retry produced the existing deliverable and moved T-0003 to
  `awaiting_acceptance`; it was not accepted.

## Defects found

1. **Architect prompt offers `protected-pytest` with no registry** (T-0001, T-0002 failed with
   `invalid-proposal`): the contract needs a pinned artifact slug, version and digest the
   model cannot produce. Worked around by requirement wording, not by code change.
2. **Reviewer report schema rejects severity `info`** (`invalid-review-report`); the model's
   verdict was `accepted` with a `minor` finding plus one `info` finding. The Reviewer prompt
   should enumerate valid severities, or the schema should tolerate `info`.
3. **No retry path** from `review_failed`: `implement` refuses (`not-approved`), so a
   transient Reviewer format error forces a new task and new approval.
4. At the time of this run, approved `pytest`-kind selectors were never executed;
   only the allowlisted `unittest` run was recorded, which the Reviewer noted as a
   gap.
5. A dirty-worktree flag on T-0001 came from a host `__pycache__` created by the operator's
   own pre-check (an operator error, not a harness defect).

## Recovery (fix status)

- Defects 1-3 are fixed: the Architect prompt advertises only `pytest` and `unverified`;
  the Reviewer and QA prompts enumerate `blocking`, `major` and `minor` (the parser stays
  strict); `loomsphere-harness retry-review <task>` reruns only the Reviewer after an
  `invalid-review-report`, once per iteration, after re-verifying approval, commit,
  workspace digest, validation and QA evidence. A real rejection is never retried.
- Defect 4 was an **explicit acceptance blocker** in this run. The approved proposal
  declares `pytest` selectors; the sandbox image has no pytest and only the allowlisted
  `unittest` command ran. Its existing deliverable records
  `selector_executed: false` for each criterion. The follow-up enforcement executes
  approved selectors for new implementation runs, but it does not rewrite T-0003's
  approved contract, prior runs, deliverable, or audit history. The independent
  acceptance tests retain SHA-256
  `f8bb81c21bd05eda1d8fcdcc64af9ee8021e5a3aa410ce354d94311419745902`; the existing
  T-0003 deliverable is not automatically accepted.
