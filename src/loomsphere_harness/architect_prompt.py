"""Architect prompt composition.

The Architect receives the repository persona, the skill procedures the factory
loaded from its trusted checkout, and explicit task context, all in one prompt.
``services/skills.py`` owns the loading and validation; this module only decides
where the material sits in the text.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from loomsphere_harness.skills import SkillDocument, render_skills_block


class ArchitectPromptError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ComposedPrompt:
    text: str
    digest: str
    persona_digest: str

    def __len__(self) -> int:
        return len(self.text)


def _strip_frontmatter(source: str) -> str:
    if not source.startswith("---"):
        return source
    lines = source.splitlines()
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "\n".join(lines[index + 1 :]).strip()
    return source


def load_persona_text(
    path: str | Path,
    *,
    role: str = "Architect",
    missing_code: str = "ARCHITECT_PERSONA_MISSING",
    empty_code: str = "ARCHITECT_PERSONA_EMPTY",
) -> str:
    """Read a role persona, stripping GitHub frontmatter."""

    persona_path = Path(path).expanduser()
    if not persona_path.is_file():
        raise ArchitectPromptError(
            missing_code,
            f"The {role} persona file is not available.",
        )
    persona = _strip_frontmatter(persona_path.read_text(encoding="utf-8")).strip()
    if not persona:
        raise ArchitectPromptError(
            empty_code,
            f"The {role} persona file is empty.",
        )
    return persona


def load_persona(path: str | Path) -> str:
    return load_persona_text(path)


RESPONSE_CONTRACT = """\
## Required response format

Return exactly one fenced ```json block and nothing else inside that block.
The object must have this shape; extra keys are ignored, missing required keys
make the run fail:

{
  "summary": "one paragraph describing the approved scope",
  "implementation_approach": ["ordered implementation step", "..."],
  "components": [
    {"path": "repository/path.ext", "change": "what changes there"}
  ],
  "acceptance_criteria_mapping": [
    {
      "criterion": "one observable acceptance outcome",
      "approach": "how the change satisfies it",
      "mandatory": true,
      "verification": {
        "kind": "protected-pytest",
        "artifact": "trusted-artifact-key",
        "selector": "test_case"
      }
    }
  ],
  "risks": ["risk"],
  "assumptions": ["assumption"],
  "dependencies": ["dependency"],
  "validation_strategy": ["validation step"],
  "open_questions": ["question that must be answered by a human"]
}
Use ``pytest`` with a bounded repository selector only when the requirement
permits Builder-authored verification. Use ``protected-pytest`` with a trusted
artifact key and node selector when independent verification is required.
"""


def intake_block(intake: Mapping[str, object]) -> str:
    """Render recorded Intake triage as context, not as an approved plan.

    The Architect is told what the requester means, what areas intake located, what
    human answers resolved its blocking questions, and which reviews intake
    recommended -- and told that none of it is approved scope.
    """

    def _lines(value: object) -> list[str]:
        if not isinstance(value, (list, tuple)):
            return []
        return [f"  - {item}" for item in value if str(item).strip()]

    components = intake.get("affected_components") or []
    component_lines: list[str] = []
    if isinstance(components, (list, tuple)):
        for component in components:
            if isinstance(component, Mapping):
                path = str(component.get("path", "")).strip()
                kind = str(component.get("kind", "unknown")).strip() or "unknown"
                reason = str(component.get("reason", "")).strip()
                if path:
                    component_lines.append(
                        f"  - {path} ({kind})"
                        + (f": {reason}" if reason else "")
                    )

    implications = intake.get("implications") or {}
    implication_lines: list[str] = []
    if isinstance(implications, Mapping):
        for area in ("security", "ux_ui", "database", "architecture"):
            entries = implications.get(area) or []
            for entry in entries if isinstance(entries, (list, tuple)) else []:
                implication_lines.append(f"  - {area}: {entry}")

    recommendation_lines = [
        f"  - {item.get('specialist')}: {item.get('reason')}"
        for item in (intake.get("specialist_recommendations") or [])
        if isinstance(item, Mapping) and item.get("specialist")
    ]
    blocking_lines = [
        f"  - {item.get('question')}"
        for item in (intake.get("ambiguities") or [])
        if isinstance(item, Mapping) and item.get("blocking") and item.get("question")
    ]
    clarification_lines: list[str] = []
    for item in intake.get("clarification_answers") or []:
        if not isinstance(item, Mapping):
            continue
        question = str(item.get("question", "")).strip()
        answer = str(item.get("answer", "")).strip()
        if question and answer:
            clarification_lines.append(
                f"  - Question: {question}\n    Human answer: {answer}"
            )

    blocks = [
        "## Recorded intake triage",
        "",
        "An Intake assessment was recorded for this request before this run. It is",
        "recorded evidence, not approved scope, and it is not a design: do not repeat",
        "the triage, and do not treat the areas below as an approved component list.",
        "",
        f"Intent: {intake.get('intent_summary', '')}",
        f"Request type: {intake.get('request_type', 'feature')}",
        f"Intake risk: {intake.get('risk_level', 'low')}",
        f"Implication areas: {', '.join(intake.get('implication_areas') or []) or 'none'}",
    ]
    if component_lines:
        blocks.extend(["", "Areas intake located:", *component_lines])
    if implication_lines:
        blocks.extend(["", "Implications intake recorded:", *implication_lines])
    if recommendation_lines:
        blocks.extend(
            [
                "",
                "Specialists intake recommended (advice; the routing engine decides):",
                *recommendation_lines,
            ]
        )
    if clarification_lines:
        blocks.extend(
            [
                "",
                "Human answers to Intake's blocking questions (requester decisions):",
                *clarification_lines,
            ]
        )
    elif blocking_lines:
        blocks.extend(["", "Blocking questions recorded by Intake:", *blocking_lines])
    blocks.extend(
        [
            "",
            "Treat this as a description of the request. Verify every claim against the",
            "repository yourself, and put anything still unresolved in open_questions.",
        ]
    )
    return "\n".join(blocks)


def compose_architect_prompt(
    *,
    persona: str,
    task_title: str,
    requirements: str,
    acceptance_criteria: str,
    repository_reference: str,
    repository_commit: str,
    task_identifier: str,
    intake: Mapping[str, object] | None = None,
    skills: Sequence[SkillDocument] = (),
    revision_request: Mapping[str, object] | None = None,
) -> ComposedPrompt:
    persona_digest = hashlib.sha256(persona.encode("utf-8")).hexdigest()
    skills_block = render_skills_block(skills)
    revision_block = None
    if revision_request is not None:
        revision_block = "\n".join(
            [
                "## Controlled proposal revision",
                "",
                "This is a revision of the current pending, unapproved proposal on",
                "the same engineering task. The previous proposal and its run are",
                "historical evidence and must not be rewritten. Do not repeat Intake",
                "or start implementation. Treat the reason and acceptance",
                "requirements below as mandatory constraints for this proposal.",
                "",
                "Revision request:",
                json.dumps(
                    dict(revision_request),
                    sort_keys=True,
                    indent=2,
                    ensure_ascii=False,
                ),
                "",
                "Include every requested acceptance requirement exactly once,",
                "mark each mandatory, and use its exact protected-pytest artifact",
                "and selector. Do not substitute Builder-editable pytest tests.",
            ]
        )
    sections = [
        "You are the LoomSphere Architect agent acting inside LoomSphere, an AI",
        "software factory. You are the architecture gatekeeper for one engineering",
        "requirement. Analyse the requirement against the repository in your",
        "working directory and return a structured scope proposal for a human to",
        "approve.",
        "",
        "## Agent persona",
        persona,
        "",
        *([skills_block, ""] if skills_block else []),
        "## Engineering requirement",
        f"Task identifier: {task_identifier}",
        f"Title: {task_title}",
        "",
        "### Requirements",
        requirements,
        "",
        "### Acceptance criteria",
        acceptance_criteria,
        "",
        *([intake_block(intake), ""] if intake else []),
        *([revision_block, ""] if revision_block else []),
        "## Repository context",
        f"Reference: {repository_reference}",
        f"Resolved commit: {repository_commit}",
        "The repository is mounted read-only at the current working directory.",
        "Read any documentation, decisions, and source files you need.",
        "",
        "## Hard constraints",
        "- Do not modify, create, or delete any file. The workspace is read-only.",
        "- Do not approve the scope, do not claim a human decision, and do not",
        "  start implementation. A human owner must approve your proposal.",
        "- Do not treat proposals, unchecked assumptions, or your own",
        "  recommendations as accepted architectural decisions.",
        "- For every mandatory criterion, identify a bounded pytest selector",
        "  that exists or that the Builder is expected to add. If no independent",
        "  check is possible, mark it unverified; agent reports and final human",
        "  approval do not count as verification.",
        "- Base the proposal only on the requirement and repository evidence in",
        "  front of you. Report unresolved ambiguity in open_questions.",
        "- Never include credentials, tokens, or connection strings.",
        "",
        RESPONSE_CONTRACT,
    ]
    text = "\n".join(sections)
    return ComposedPrompt(
        text=text,
        digest=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        persona_digest=persona_digest,
    )


__all__ = [
    "ArchitectPromptError",
    "ComposedPrompt",
    "compose_architect_prompt",
    "intake_block",
    "load_persona",
    "load_persona_text",
]
