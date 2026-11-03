"""Intake prompt composition.

The Intake agent receives the repository persona, the skill procedures the factory
loaded from its trusted checkout, the request exactly as it was captured, and the
specialists the trusted catalog can actually execute. It returns one structured
assessment.

Two things in this prompt are load-bearing:

* The specialist list is read from the catalog, not hard-coded. A specialist added to
  the catalog becomes recommendable without editing this module, and the agent can
  never recommend something the factory cannot run.
* The prompt states the authority boundary in the same words the policy enforces:
  recommendations are advice, the deterministic router decides the mandatory team,
  and a human approves the scope.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from loomsphere_harness.architect_prompt import ComposedPrompt
from loomsphere_harness.skills import SkillDocument, render_skills_block

RESPONSE_CONTRACT = """\
## Required response format

Return exactly one fenced `json block and nothing else inside that block. The object
must have this shape; extra keys are ignored, missing required keys make the run
fail:

{
  "intent": {
    "summary": "what the requester wants, in their terms",
    "outcome": "the observable end state that says it worked",
    "request_type": "feature",
    "confidence": "high"
  },
  "affected_components": [
    {"path": "repository/path/or/area", "kind": "backend", "reason": "why this area is involved"}
  ],
  "implications": {
    "security": ["the trust boundary or sensitive data, and how it could be misused"],
    "ux_ui": ["the user-visible surface, state, or interaction that needs review"],
    "database": ["schema, migration, or data-integrity consequence"],
    "architecture": ["boundary, contract, or recorded decision the request bends"]
  },
  "ambiguities": [
    {
      "question": "the question a human must answer",
      "why_it_matters": "what cannot be decided until it is answered",
      "blocking": true
    }
  ],
  "specialist_recommendations": [
    {
      "specialist": "security",
      "reason": "the concrete reason this request needs that review",
      "confidence": "high"
    }
  ],
  "risk": {"level": "medium", "reasons": ["why the impact or reversibility is what you say"]},
  "assumptions": ["an assumption you made to read the request"],
  "notes": ["anything a human must know, including a concern with no matching specialist"]
}

request_type must be one of: feature, bugfix, refactor, documentation, security, ui,
database, other.
kind must be one of: backend, frontend, database, infrastructure, documentation,
tests, shared, unknown.
confidence must be one of: low, medium, high.
risk.level must be one of: low, medium, high.
A specialist recommendation must name a specialist from the provided list and carry
a reason of at least ten characters.
"""


def compose_intake_prompt(
    *,
    persona: str,
    task_title: str,
    requirements: str,
    acceptance_criteria: str,
    task_identifier: str,
    repository_reference: str,
    repository_commit: str,
    specialists: Sequence[tuple[str, str]] = (),
    catalog_digest: str | None = None,
    skills: Sequence[SkillDocument] = (),
) -> ComposedPrompt:
    """Compose the intake prompt from recorded request facts only."""

    persona_digest = hashlib.sha256(persona.encode("utf-8")).hexdigest()
    skills_block = render_skills_block(skills)
    specialist_lines = "\n".join(
        f"- {name} ({label})" for name, label in specialists
    ) or "- (none are registered; recommend nothing)"
    catalog_line = (
        f"Catalog digest: {catalog_digest}" if catalog_digest else ""
    )
    sections = [
        "You are the LoomSphere Intake agent acting inside LoomSphere, an AI",
        "software factory. You triage one engineering request before any architecture",
        "begins: you state the intent, locate the areas it touches, surface what it",
        "does not say, name the implication areas that carry mandatory review, and",
        "recommend the review specialists it appears to need.",
        "",
        "## Agent persona",
        persona,
        "",
        *([skills_block, ""] if skills_block else []),
        "## Engineering request",
        f"Task identifier: {task_identifier}",
        f"Title: {task_title}",
        "",
        "### Requirements",
        requirements,
        "",
        "### Acceptance criteria as written",
        acceptance_criteria,
        "",
        "## Repository context",
        f"Reference: {repository_reference}",
        f"Resolved commit: {repository_commit}",
        "The repository is mounted read-only at the current working directory.",
        "Read any documentation, source files, and tests you need to locate the areas",
        "this request touches.",
        "",
        "## Review specialists this factory can execute",
        specialist_lines,
        catalog_line,
        "",
        "## Authority boundary",
        "- Your assessment is advice. A deterministic routing engine decides the",
        "  mandatory review team from persisted facts and the trusted agent catalog;",
        "  it may add review on your recommendation, and it will retain every review",
        "  it already requires even if you did not name it.",
        "- A human approves the scope. You do not propose a scope, a design, an",
        "  implementation approach, or acceptance criteria.",
        "- Do not approve, reject, or claim any human decision.",
        "",
        "## Hard constraints",
        "- Do not modify, create, or delete any file. The workspace is read-only.",
        "- Do not design the solution. Name areas, questions, and implications only.",
        "- Do not invent requirements or resolve an ambiguity by guessing. Record the",
        "  question and mark whether it blocks a defensible scope.",
        "- Recommend a specialist only from the list above, and only with a concrete",
        "  reason taken from the request or the repository.",
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
    "RESPONSE_CONTRACT",
    "compose_intake_prompt",
]
