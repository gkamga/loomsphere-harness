"""Prompt composition for the Builder, QA, and Reviewer roles.

Prompts are composed from the task's approved scope plus the evidence produced by
earlier stages, and the composed text is digested so a recorded stage can prove
which inputs it ran against. The persona files carry the role guardrails; this
module adds the concrete task, the evidence, and the exact response contract.

Extracted from LoomSphere's ``implementation_prompts`` service. The scope block,
the skill block, and the "reply with exactly one fenced json block" contract are
the same ones the Architect prompt uses, so a report parser can rely on them.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from loomsphere_harness.architect_prompt import ComposedPrompt, load_persona_text
from loomsphere_harness.skills import SkillDocument, render_skills_block

BUILDER_CONTRACT = """\
Reply with exactly one fenced json block and nothing after it:
```json
{
  "summary": "what you changed and why, in one short paragraph",
  "files": [{"path": "relative/path.py", "change": "what changed in this file"}],
  "acceptance_criteria": [
    {
      "criterion_id": "AC-1",
      "criterion": "the criterion text",
      "status": "met",
      "evidence": "how it is satisfied"
    }
  ],
  "tests_run": ["command you executed and its result"],
  "notes": ["anything a reviewer must know, including anything you could not do"]
}
```
`status` must be one of `met`, `unmet`, or `unknown`.
`criterion_id` must repeat the approved identifier exactly. Report every approved
criterion. A criterion you leave out is treated as omitted and blocks delivery."""

SEVERITY_GUIDE = """\
`severity` is exactly one of these three values; any other value, including
`info`, `low`, `warning`, or `critical`, makes the whole report invalid:
- `blocking`: a defect that makes the change unsafe or unacceptable as it stands.
- `major`: a significant defect or unmet expectation that must be fixed first.
- `minor`: a non-blocking observation, improvement, or purely informational note.
Report an informational note as `minor`, never as `info`."""

QA_CONTRACT = """\
Reply with exactly one fenced json block and nothing after it:
```json
{
  "verdict": "passed",
  "summary": "one short paragraph stating what you checked and what you concluded",
  "findings": [
    {"severity": "blocking", "description": "the defect", "evidence": "the observed evidence"}
  ],
  "acceptance_criteria": [
    {
      "criterion_id": "AC-1",
      "criterion": "the criterion text",
      "status": "met",
      "evidence": "the evidence you observed"
    }
  ],
  "residual_risks": ["risk that is not a blocking defect"]
}
```
`verdict` must be `passed` or `failed`.
""" + SEVERITY_GUIDE + """
`status` must be one of `met`, `unmet`, or `unknown`.
`criterion_id` must repeat the approved identifier exactly.
Use `failed` when any acceptance criterion is unmet or any blocking finding exists."""

REVIEWER_CONTRACT = """\
Reply with exactly one fenced json block and nothing after it:
```json
{
  "verdict": "accepted",
  "summary": "one short paragraph stating your conclusion and its basis",
  "findings": [
    {"severity": "major", "description": "the problem", "evidence": "the observed evidence"}
  ],
  "acceptance_criteria": [
    {
      "criterion_id": "AC-1",
      "criterion": "the criterion text",
      "status": "met",
      "evidence": "the evidence"
    }
  ],
  "follow_ups": ["non-blocking follow-up for a later task"]
}
```
`verdict` must be `accepted`, `changes-required`, or `rejected`.
""" + SEVERITY_GUIDE + """
`status` must be one of `met`, `unmet`, or `unknown`.
`criterion_id` must repeat the approved identifier exactly. An approved criterion
that is absent from your report is treated as omitted by the delivery gate.
Use `accepted` only when the recorded validation evidence belongs to this exact
change set and every acceptance criterion is supported by evidence.
Use `rejected` when the change set cannot satisfy the requirement at all."""

BUILDER_RULES = """\
1. Work only inside the current working directory; it is an isolated git worktree.
2. Implement the smallest change set that satisfies the acceptance criteria.
3. Add or update automated tests that fail without your change.
4. Never disable, delete, or weaken an existing test to make a check pass.
5. Never modify `.git`, git configuration, or anything outside the worktree.
6. Do not run `git commit`, `git push`, `git checkout`, or `git reset`; the harness
   records your change set itself, and history rewriting destroys audit evidence.
7. You cannot install packages from the network; work with what is present.
8. Validation runs in a network-isolated sandbox against the recorded change set.
   A test that needs the network, a container runtime, or an external service
   cannot be validated here: keep the suite runnable offline with what the image
   already provides.
9. Work in a bounded number of discovery steps, not an unbounded exploration.
   List the project structure once, near the start, and reuse what you learned
   instead of listing the same directory again.
10. Search for the specific names, paths, and strings your change touches rather
    than browsing files to understand the project in general.
11. Read the file you are changing, and the files its tests import, in full. Do
    not read whole directories, generated output, lock files, vendored code, or
    documentation for background.
12. Never reread a file whose content you already hold, and never repeat a search
    whose result you already have. Reopen something only when the change you just
    made could have altered it, and say why in your next step.
13. Spend your remaining steps on the implementation and its automated tests, not
    on exploratory documentation. Requirements and criteria in this prompt are the
    specification; do not search the repository for a second opinion on them.
14. If the change cannot be completed inside this budget, stop and report the
    blocker and the exact files you still need to read, rather than continuing to
    explore. A precise blocked report is an acceptable outcome; an unfinished
    exploration is not."""

QA_RULES = """\
1. Do not modify any file; you are reviewing a recorded change set.
2. Judge only the evidence shown: the diff, the validation run, and the criteria.
3. A passing validation command is necessary but not sufficient: check that the
   diff actually implements the criteria.
4. Report what you observed, never what you assume was run.
5. The command, exit code, and output in the Executed validation block come from
   the harness's own validation run bound to this implementation, executed through
   the configured sandbox. Your analysis environment is separate. Do not rerun the
   command there or infer that the recorded sandbox run did not occur because a
   tool is unavailable locally. If independent execution is unavailable, state
   that separately and assess the recorded result and diff."""

REVIEWER_RULES = """\
1. Do not modify any file and do not run code; you are reviewing recorded evidence.
2. Accept only when the validation evidence was produced against this exact change
   set and every acceptance criterion is supported by observed evidence.
3. Prefer `changes-required` over `accepted` when evidence is missing."""


class LoopPromptError(RuntimeError):
    """The prompt for a stage could not be composed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _compose(
    persona: str, body: str, skills: Sequence[SkillDocument] = ()
) -> ComposedPrompt:
    """Assemble one stage prompt: persona, then loaded skills, then the evidence.

    Skills sit between the persona and the task material because they describe how
    the role works, not what this particular attempt contains. The skills block
    states its own limits; the text is procedure, never a permission.
    """

    skills_block = render_skills_block(skills)
    parts = [persona.strip()]
    if skills_block:
        parts.append(skills_block)
    parts.append(body.strip())
    text = "\n\n".join(parts) + "\n"
    return ComposedPrompt(
        text=text,
        digest=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        persona_digest=hashlib.sha256(persona.encode("utf-8")).hexdigest(),
    )


def scope_block(
    *,
    task_title: str,
    requirements: str,
    acceptance_criteria: str,
    task_identifier: str,
    approved_criteria: Sequence[str] = (),
) -> str:
    """The frozen, human-approved scope every implementation stage is bound to."""

    contract = ""
    listed = [line.strip() for line in approved_criteria if line.strip()]
    if listed:
        bullets = "\n".join(f"- {line}" for line in listed)
        contract = (
            "## Approved acceptance contract\n\n"
            "These identifiers were frozen by a recorded human approval. Your report "
            "must repeat each `criterion_id` exactly; the delivery gate compares your "
            "report to this list and a missing identifier blocks delivery.\n\n"
            f"{bullets}\n\n"
        )
    return (
        "## Approved scope\n\n"
        f"Task: {task_title}\n\n"
        f"Requirements:\n{requirements.strip()}\n\n"
        f"Acceptance criteria:\n{acceptance_criteria.strip()}\n\n"
        f"Task identifier: {task_identifier}\n\n"
        f"{contract}".rstrip()
        + "\n"
    )


def recovery_block(recovery: Mapping[str, Any] | None) -> str:
    """The provenance note a recovered change set carries into QA and review.

    A recovered change set has no Builder report: the Builder stopped before it
    wrote one. Rather than invent one, the stage tells QA and the Reviewer exactly
    what exists and what does not, so neither role can be misled into quoting a
    summary or a test run that was never recorded.
    """

    if not recovery:
        return ""
    files = recovery.get("changed_files") or []
    listed = "\n".join(
        f"- {entry['path']} ({entry['mode']}, sha256 {entry['sha256'] or 'not-tracked'})"
        for entry in files
        if isinstance(entry, dict) and entry.get("path")
    ) or "- (none)"
    detail = str(recovery.get("failure_detail") or "").strip()
    return (
        "## Recovery provenance\n\n"
        "This change set was recovered from a failed Builder run and authorised "
        "by a recorded human decision. The evidence available to you is different "
        "from a normal iteration, and the difference is deliberate:\n\n"
        f"- The Builder run failed with `{recovery.get('failure_reason') or 'unknown'}`"
        f" (run `{recovery.get('builder_run_id') or 'not-recorded'}`)\n"
        "- No Builder build report exists. Do not expect a Builder summary, a "
        "claimed file list, or a claimed test run; none of them was recorded, and "
        "none of them has been invented.\n"
        "- The change set was frozen by the recovery at commit "
        f"`{recovery.get('head_commit') or 'unknown'}` from base "
        f"`{recovery.get('base_commit') or 'unknown'}`.\n"
        f"- Recovered file inventory (snapshot digest `{recovery.get('snapshot_digest') or 'unknown'}`,"
        f" recovery digest `{recovery.get('recovery_digest') or 'unknown'}`):\n\n{listed}\n\n"
        + (f"Recorded failure detail:\n```\n{detail[:800]}\n```\n\n" if detail else "")
    )


def compose_builder_prompt(
    *,
    persona: str,
    task_title: str,
    requirements: str,
    acceptance_criteria: str,
    task_identifier: str,
    base_commit: str,
    iteration: int,
    max_iterations: int,
    previous_feedback: Sequence[str] = (),
    approved_criteria: Sequence[str] = (),
    skills: Sequence[SkillDocument] = (),
) -> ComposedPrompt:
    feedback = "\n\n".join(part.strip() for part in previous_feedback if part.strip())
    body = "\n\n".join(
        [
            "# Role: Builder\n\n"
            "You implement one approved engineering requirement inside the task "
            "worktree, with automated tests.",
            scope_block(
                task_title=task_title,
                requirements=requirements,
                acceptance_criteria=acceptance_criteria,
                task_identifier=task_identifier,
                approved_criteria=approved_criteria,
            ),
            (
                "## This iteration\n\n"
                f"Iteration {iteration} of at most {max_iterations}.\n"
                f"The worktree is checked out from base commit {base_commit}.\n"
            ),
            (
                "## Feedback from the previous iteration\n\n"
                f"{feedback}\n"
                if feedback
                else "## Feedback from the previous iteration\n\nNone; this is the "
                "first implementation attempt.\n"
            ),
            f"## Rules\n\n{BUILDER_RULES}",
            f"## Response contract\n\n{BUILDER_CONTRACT}",
        ]
    )
    return _compose(persona, body, skills)


def compose_qa_prompt(
    *,
    persona: str,
    task_title: str,
    requirements: str,
    acceptance_criteria: str,
    task_identifier: str,
    implementation_summary: str,
    implementation_files: Sequence[str],
    diff_excerpt: str,
    validation_command: Sequence[str],
    validation_exit_code: int | None,
    validation_output: str,
    approved_criteria: Sequence[str] = (),
    skills: Sequence[SkillDocument] = (),
    recovery: Mapping[str, Any] | None = None,
) -> ComposedPrompt:
    files = "\n".join(f"- {path}" for path in implementation_files) or "- (none reported)"
    command = " ".join(validation_command) or "(no validation command was executed)"
    builder_summary = (
        "(no Builder report exists; this change set was recovered from a failed "
        "Builder run and authorised by a human)"
        if recovery
        else implementation_summary.strip()
    )
    body = "\n\n".join(
        part
        for part in [
            "# Role: QA\n\n"
            "You verify one recorded implementation against its acceptance criteria "
            "before it reaches review.",
            scope_block(
                task_title=task_title,
                requirements=requirements,
                acceptance_criteria=acceptance_criteria,
                task_identifier=task_identifier,
                approved_criteria=approved_criteria,
            ),
            recovery_block(recovery),
            (
                "## Recorded implementation\n\n"
                f"Builder summary:\n{builder_summary}\n\n"
                f"Changed files:\n{files}\n"
            ),
            f"## Diff\n\n```diff\n{diff_excerpt.strip()}\n```",
            (
                "## Executed validation\n\n"
                f"Command: {command}\n"
                f"Exit code: {validation_exit_code}\n\n"
                f"Output excerpt:\n```\n{validation_output.strip()}\n```"
            ),
            f"## Rules\n\n{QA_RULES}",
            f"## Response contract\n\n{QA_CONTRACT}",
        ]
        if part
    )
    return _compose(persona, body, skills)


def compose_reviewer_prompt(
    *,
    persona: str,
    task_title: str,
    requirements: str,
    acceptance_criteria: str,
    task_identifier: str,
    implementation_summary: str,
    diff_excerpt: str,
    validation_command: Sequence[str],
    validation_exit_code: int | None,
    qa_verdict: str,
    qa_summary: str,
    qa_findings: Sequence[str],
    approved_criteria: Sequence[str] = (),
    skills: Sequence[SkillDocument] = (),
    recovery: Mapping[str, Any] | None = None,
) -> ComposedPrompt:
    findings = "\n".join(f"- {finding}" for finding in qa_findings) or "- (none)"
    command = " ".join(validation_command) or "(no validation command was executed)"
    builder_summary = (
        "(no Builder report exists; this change set was recovered from a failed "
        "Builder run and authorised by a human)"
        if recovery
        else implementation_summary.strip()
    )
    body = "\n\n".join(
        part
        for part in [
            "# Role: Reviewer\n\n"
            "You decide whether one recorded implementation may go to a human for "
            "final approval.",
            scope_block(
                task_title=task_title,
                requirements=requirements,
                acceptance_criteria=acceptance_criteria,
                task_identifier=task_identifier,
                approved_criteria=approved_criteria,
            ),
            recovery_block(recovery),
            (
                "## Recorded implementation\n\n"
                f"Builder summary:\n{builder_summary}\n"
            ),
            f"## Diff\n\n```diff\n{diff_excerpt.strip()}\n```",
            (
                "## Recorded validation\n\n"
                f"Command: {command}\n"
                f"Exit code: {validation_exit_code}\n"
            ),
            (
                "## QA verdict\n\n"
                f"Verdict: {qa_verdict}\n"
                f"Summary: {qa_summary.strip()}\n\n"
                f"Findings:\n{findings}\n"
            ),
            f"## Rules\n\n{REVIEWER_RULES}",
            f"## Response contract\n\n{REVIEWER_CONTRACT}",
        ]
        if part
    )
    return _compose(persona, body, skills)


def load_role_persona(path: str, *, role: str) -> str:
    """Read one implementation role's persona, failing closed when it is missing."""

    return load_persona_text(
        path,
        role=role,
        missing_code=f"{role.upper()}_PERSONA_MISSING",
        empty_code=f"{role.upper()}_PERSONA_EMPTY",
    )


__all__ = [
    "BUILDER_CONTRACT",
    "BUILDER_RULES",
    "LoopPromptError",
    "QA_CONTRACT",
    "QA_RULES",
    "REVIEWER_CONTRACT",
    "REVIEWER_RULES",
    "SEVERITY_GUIDE",
    "compose_builder_prompt",
    "compose_qa_prompt",
    "compose_reviewer_prompt",
    "load_role_persona",
    "recovery_block",
    "scope_block",
]
