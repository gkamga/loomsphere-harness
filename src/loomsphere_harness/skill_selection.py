"""Deterministic, explainable selection of the skills one agent execution receives.

The factory used to hand every role a fixed list of procedures. That list is still
the baseline - the role's own definition declares the procedures it must always
carry - but a second source now exists: skills discovered in the trusted root whose
metadata declares the role, the stage, and the task conditions they belong to.

Selection is deliberately boring, because it decides what an agent is told to do:

* **Eligibility comes from the trusted root only.** A skill is a candidate when its
  own `roles` names the executing role and its `stages` (when declared) names the
  current stage. Nothing recorded about the task can make a skill eligible, so an
  Intake recommendation, a route, or text inside the request can never authorize a
  procedure that was not reviewed for that role.
* **Applicability comes from recorded evidence.** Task type, affected areas, Intake
  implication areas, and risk level are read from the durable Intake assessment and
  the frozen route. A candidate is selected when one of its declared facts matches
  one recorded fact, and the reason is recorded and rendered into the prompt.
* **Required procedures cannot be displaced.** A skill marked `required` is selected
  whenever its role and stage run, whatever the task looks like, and the role's
  declared baseline is always present. Selection can therefore add context to a
  procedure, never remove safety from it.
* **The result is frozen.** The selection carries a digest over the policy, the
  context, and every selected skill's content digest. The run records it, and an
  execution refuses when the recomposed selection no longer matches, so editing a
  skill or re-triaging the request after dispatch cannot silently change what was
  authorized.

Compatibility policy (explicit, and visible in the evidence): a task without Intake
evidence is selected from whatever it does have - the frozen route, and nothing
else. `sources` records exactly which evidence was available, so a historical task
selects the baseline set plus any route-matched procedure, and a task with no
recorded context at all selects the baseline only. The policy is recorded as
`context-source:<name>` per source and as `compatibility` overall; it never silently
falls back to "load everything".
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from loomsphere_harness.schemas.intake import IntakeAssessment
from loomsphere_harness.skills import (
    MAX_SKILLS_BYTES_PER_PROMPT,
    ORIGIN_CONTEXTUAL,
    ORIGIN_DECLARED,
    ORIGIN_REQUIRED,
    SKILL_BUDGET_EXCEEDED,
    SKILL_NAME_PATTERN,
    SKILL_STAGE_MISMATCH,
    SkillContextView,
    SkillDocument,
    SkillLoadError,
    applicability_match,
    discover_skills,
    load_role_skills,
)

#: The policy name recorded on every selection.
SELECTION_POLICY = "contextual-selection"

#: The stage each fixed factory role runs in. Catalog specialists use their own
#: stage name from the trusted catalog, so a registered reviewer needs no entry.
STAGE_FOR_ROLE: dict[str, str] = {
    "intake": "intake",
    "architect": "architecture",
    "builder": "implementation",
    "qa": "validation",
    "reviewer": "review",
}

#: Evidence source names, recorded so a reader can tell which facts were available.
SOURCE_INTAKE = "intake"
SOURCE_ROUTE = "route"
SOURCE_SCOPE = "scope"

#: Compatibility outcomes. The name is recorded, not inferred.
COMPATIBILITY_CONTEXTUAL = "contextual"
COMPATIBILITY_NO_INTAKE = "no-intake-evidence"
COMPATIBILITY_BASELINE_ONLY = "no-recorded-context"

#: A run whose recorded selection no longer matches fails closed with this reason.
SKILL_SELECTION_CHANGED = "skill-selection-changed"

#: Where a rejected candidate or request came from.
REJECTED_ROLE = "role-not-eligible"
REJECTED_STAGE = "stage-not-eligible"
REJECTED_PREREQUISITE = "prerequisite-missing"
REJECTED_NO_MATCH = "no-applicability-match"
REJECTED_NOT_DECLARED = "no-applicability-declared"
REJECTED_UNKNOWN = "unknown-skill"

#: A request may name a skill for information only: "follow skill:engineering-review".
#: The marker is deliberately explicit so the selector never guesses at free prose.
SKILL_REQUEST_PATTERN = re.compile(r"skill:([a-z][a-z0-9]*(?:-[a-z0-9]+){0,7})", re.I)

#: Route areas are top-level path segments. They are mapped onto the vocabulary
#: skills may declare, and anything unmapped is simply not a skill signal.
AREA_ALIASES: dict[str, str] = {
    "backend": "backend",
    "server": "backend",
    "frontend": "frontend",
    "web": "frontend",
    "ui": "frontend",
    "client": "frontend",
    "docs": "documentation",
    "doc": "documentation",
    "documentation": "documentation",
    "tests": "tests",
    "test": "tests",
    "migrations": "database",
    "migration": "database",
    "db": "database",
    "database": "database",
    "sql": "database",
    "infra": "infrastructure",
    "infrastructure": "infrastructure",
    "deploy": "infrastructure",
    "deployment": "infrastructure",
    "docker": "infrastructure",
    "scripts": "infrastructure",
    ".github": "infrastructure",
    "shared": "shared",
    "packages": "shared",
}


def _digest(document: Mapping[str, object] | list[object]) -> str:
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalize_area(value: str) -> str | None:
    """Map one route area onto the vocabulary skills may declare."""

    return AREA_ALIASES.get(value.strip().lower())


@dataclass(frozen=True)
class SkillContext:
    """The recorded facts a selection is made from.

    Every field is either an identifier of the executing stage or a fact copied
    from durable evidence. Nothing here is inferred from free text: the request
    text is only ever read to *reject* a named skill, never to select one.
    """

    role: str
    stage: str
    task_types: tuple[str, ...] = ()
    areas: tuple[str, ...] = ()
    implications: tuple[str, ...] = ()
    risk_levels: tuple[str, ...] = ()
    complexity: str | None = None
    sources: tuple[str, ...] = ()
    intake_policy: str | None = None
    intake_digest: str | None = None
    route_digest: str | None = None
    compatibility: str = COMPATIBILITY_BASELINE_ONLY

    @property
    def view(self) -> SkillContextView:
        return SkillContextView(
            task_types=self.task_types,
            areas=self.areas,
            implications=self.implications,
            risk_levels=self.risk_levels,
            stage=self.stage,
        )

    def as_document(self) -> dict[str, object]:
        """The JSON-ready context recorded on the run."""

        return {
            "role": self.role,
            "stage": self.stage,
            "task_types": list(self.task_types),
            "areas": list(self.areas),
            "implications": list(self.implications),
            "risk_levels": list(self.risk_levels),
            "complexity": self.complexity,
            "sources": list(self.sources),
            "intake_policy": self.intake_policy,
            "intake_digest": self.intake_digest,
            "route_digest": self.route_digest,
            "compatibility": self.compatibility,
        }

    @property
    def digest(self) -> str:
        return _digest(self.as_document())


@dataclass(frozen=True)
class SkillSelection:
    """The skills one execution receives, and why."""

    role: str
    stage: str
    documents: tuple[SkillDocument, ...]
    context: SkillContext
    rejected: tuple[dict[str, object], ...] = ()
    policy: str = SELECTION_POLICY

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(document.name for document in self.documents)

    @property
    def digest(self) -> str:
        return _digest(self.document())

    def document(self) -> dict[str, object]:
        """The canonical record the digest is taken over and the run stores."""

        return {
            "policy": self.policy,
            "role": self.role,
            "stage": self.stage,
            "context": self.context.as_document(),
            "context_digest": self.context.digest,
            "selected": [
                {
                    "name": document.name,
                    "digest": document.digest,
                    "source": document.source,
                    "origin": document.origin,
                    "selected_by": list(document.selected_by),
                    "required": document.required,
                }
                for document in self.documents
            ],
            "rejected": [dict(item) for item in self.rejected],
        }

    def evidence(self) -> dict[str, object]:
        """The snapshot an agent run records, including its own digest."""

        document = self.document()
        document["digest"] = self.digest
        document["selected_count"] = len(self.documents)
        document["rejected_count"] = len(self.rejected)
        return document

    def problem_against(self, recorded: Mapping[str, object] | None) -> str | None:
        """Why a recorded selection cannot authorize this execution, or None.

        A run that recorded nothing (a row written before selection existed) is
        not refused for that reason alone: the prompt digest already binds those
        runs to the text they were authorized with. A run that recorded a
        selection must still match it exactly.
        """

        if not recorded:
            return None
        expected = recorded.get("digest")
        if not isinstance(expected, str):
            return "the recorded skill selection carries no digest"
        if expected != self.digest:
            return (
                "the selected skills or the recorded context changed after the run "
                "was authorized"
            )
        return None


@dataclass(frozen=True)
class TaskFacts:
    """The request fields a selection reads. Persistence-free by design."""

    title: str
    requirements: str
    acceptance_criteria: str
    intake_policy: str | None = None


def intake_facts(
    assessment: IntakeAssessment | None, *, digest: str
) -> dict[str, object] | None:
    """The Intake contribution to the context, or None when there is no assessment."""

    if assessment is None:
        return None
    payload = assessment.model_dump(mode="json")
    areas: list[str] = []
    for component in payload.get("affected_components", []):
        kind = str(component.get("kind", "unknown")).strip().lower()
        if kind and kind != "unknown":
            areas.append(kind)
    implications = [area for area in assessment.implication_areas if area]
    return {
        "request_type": payload["intent"]["request_type"],
        "risk_level": payload["risk"]["level"],
        "areas": areas,
        "implications": implications,
        "digest": digest,
        "summary": payload["intent"].get("summary", ""),
        "implications_text": payload.get("implications", {}),
        "ambiguities": payload.get("ambiguities", []),
    }


def _ordered_unique(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            ordered.append(value)
    return tuple(ordered)


def build_skill_context(
    task: TaskFacts,
    *,
    role: str,
    stage: str,
    intake: Mapping[str, object] | None = None,
    route: Mapping[str, object] | None = None,
) -> SkillContext:
    """Build the context a selection is based on from already-recorded facts."""

    return _context_from_facts(intake, route, task=task, role=role, stage=stage)


def _context_from_facts(
    intake: Mapping[str, object] | None,
    route: Mapping[str, object] | None,
    *,
    task: TaskFacts,
    role: str,
    stage: str,
) -> SkillContext:
    """Assemble a context from already-read facts."""

    sources: list[str] = []
    if intake is not None:
        sources.append(SOURCE_INTAKE)
    if route is not None:
        sources.append(SOURCE_ROUTE)

    task_types = _ordered_unique(
        value
        for value in (
            str(intake["request_type"]) if intake else "",
            str(route["task_type"]) if route else "",
        )
        if value
    )
    areas = _ordered_unique(
        value
        for value in (
            *(intake["areas"] if intake else []),
            *(route["areas"] if route else []),
        )
        if value
    )
    implications = _ordered_unique(
        value for value in (intake["implications"] if intake else []) if value
    )
    risk_levels = _ordered_unique(
        value
        for value in (
            str(intake["risk_level"]) if intake else "",
            str(route["risk_level"]) if route else "",
        )
        if value
    )

    if intake is not None:
        compatibility = COMPATIBILITY_CONTEXTUAL
    elif route is not None:
        compatibility = COMPATIBILITY_NO_INTAKE
    else:
        compatibility = COMPATIBILITY_BASELINE_ONLY

    return SkillContext(
        role=role,
        stage=stage,
        task_types=task_types,
        areas=areas,
        implications=implications,
        risk_levels=risk_levels,
        complexity=str(route["complexity"]) if route else None,
        sources=tuple(sources),
        intake_policy=task.intake_policy,
        intake_digest=str(intake["digest"]) if intake else None,
        route_digest=str(route["digest"]) if route else None,
        compatibility=compatibility,
    )


def requested_skill_names(title: str, *texts: str) -> tuple[str, ...]:
    """Skill names explicitly named in the request text, as suggestions only."""

    names: list[str] = []
    for text in (title, *texts):
        if not text:
            continue
        for match in SKILL_REQUEST_PATTERN.finditer(text):
            name = match.group(1).lower()
            if name not in names:
                names.append(name)
    return tuple(name for name in names if SKILL_NAME_PATTERN.fullmatch(name))


def _requested_names_from_evidence(
    task: TaskFacts, intake: Mapping[str, object] | None
) -> tuple[str, ...]:
    """Every skill the durable evidence names, as unprivileged suggestions."""

    texts = [task.requirements, task.acceptance_criteria]
    if intake is not None:
        texts.append(str(intake.get("summary", "")))
        for entries in (intake.get("implications_text") or {}).values():
            if isinstance(entries, list):
                texts.extend(str(entry) for entry in entries)
        for ambiguity in intake.get("ambiguities") or []:
            if isinstance(ambiguity, dict):
                texts.append(str(ambiguity.get("question", "")))
    return requested_skill_names(task.title, *texts)


def select_role_skills(
    task: TaskFacts,
    *,
    intake: Mapping[str, object] | None = None,
    route: Mapping[str, object] | None = None,
    role: str,
    stage: str,
    definition_path: str | Path,
    skills_root: str | Path,
    tools: Iterable[str] = (),
) -> SkillSelection:
    """Select the skills one role receives for one stage of one task.

    The role definition's declared list is loaded first and fails closed. Every
    other skill in the trusted root is a candidate, and is selected only when its
    role, stage, prerequisites, and applicability match. Nothing about the task
    can widen eligibility.
    """

    intake_facts = intake
    context = _context_from_facts(
        intake_facts, route, task=task, role=role, stage=stage
    )
    authorized_tools = tuple(tools)

    baseline = load_role_skills(
        role=role, definition_path=definition_path, skills_root=skills_root
    )
    # A declared procedure whose own metadata says it belongs to other stages is a
    # contradiction in the trusted root, not a suggestion: refuse rather than run a
    # role with a procedure the skill itself excludes.
    for document in baseline:
        require_stage_match(role, stage, document)
    selected: list[SkillDocument] = [
        document.with_selection(
            origin=ORIGIN_DECLARED,
            reasons=(
                "declared-by-role-definition",
                f"stage:{stage}",
            ),
        )
        for document in baseline
    ]
    baseline_names = {document.name for document in baseline}

    candidates = discover_skills(skills_root, exclude=baseline_names)
    available_names = baseline_names | {document.name for document in candidates}

    rejected: list[dict[str, object]] = []
    contextual: list[SkillDocument] = []
    views = context.view
    for candidate in sorted(candidates, key=lambda item: item.name):
        if role not in candidate.roles:
            continue
        if candidate.stages and stage not in candidate.stages:
            rejected.append(
                {
                    "name": candidate.name,
                    "reason": REJECTED_STAGE,
                    "detail": f"declared stages: {', '.join(candidate.stages)}",
                }
            )
            continue
        missing_tools = sorted(set(candidate.requires_tools) - set(authorized_tools))
        if missing_tools:
            rejected.append(
                {
                    "name": candidate.name,
                    "reason": REJECTED_PREREQUISITE,
                    "detail": f"requires tools: {', '.join(missing_tools)}",
                }
            )
            continue
        missing_skills = sorted(
            prerequisite
            for prerequisite in candidate.requires_skills
            if prerequisite not in available_names
        )
        if missing_skills:
            rejected.append(
                {
                    "name": candidate.name,
                    "reason": REJECTED_PREREQUISITE,
                    "detail": f"requires skills: {', '.join(missing_skills)}",
                }
            )
            continue
        if candidate.required:
            contextual.append(
                candidate.with_selection(
                    origin=ORIGIN_REQUIRED,
                    reasons=("required-for-role", f"stage:{stage}"),
                )
            )
            continue
        if not candidate.applicability.declared:
            rejected.append(
                {
                    "name": candidate.name,
                    "reason": REJECTED_NOT_DECLARED,
                    "detail": "an optional skill must declare when it applies",
                }
            )
            continue
        matched, reasons = applicability_match(candidate.applicability, views)
        if not matched:
            rejected.append(
                {
                    "name": candidate.name,
                    "reason": REJECTED_NO_MATCH,
                    "detail": "no declared applicability fact matches this task",
                }
            )
            continue
        contextual.append(
            candidate.with_selection(origin=ORIGIN_CONTEXTUAL, reasons=reasons)
        )

    # A named skill is a suggestion the request may carry, never an authorization.
    # It can only ever be refused here; the reasons are recorded and surfaced, so
    # an attempt to widen an agent's procedures is visible rather than silent.
    candidate_by_name = {document.name: document for document in candidates}
    selected_names = baseline_names | {document.name for document in contextual}
    for name in _requested_names_from_evidence(task, intake_facts):
        if name in selected_names:
            continue
        if name in candidate_by_name:
            rejected.append(
                {
                    "name": name,
                    "reason": REJECTED_ROLE,
                    "detail": "the request named a skill that is not eligible here",
                    "requested": True,
                }
            )
        else:
            rejected.append(
                {
                    "name": name,
                    "reason": REJECTED_UNKNOWN,
                    "detail": "the request named a skill the trusted root does not have",
                    "requested": True,
                }
            )

    documents = tuple(selected) + tuple(sorted(contextual, key=lambda item: item.name))
    total = sum(len(document.body.encode("utf-8")) for document in documents)
    if total > MAX_SKILLS_BYTES_PER_PROMPT:
        raise SkillLoadError(
            SKILL_BUDGET_EXCEEDED,
            f"The selected skills for '{role}' exceed "
            f"{MAX_SKILLS_BYTES_PER_PROMPT} bytes in total.",
        )

    return SkillSelection(
        role=role,
        stage=stage,
        documents=documents,
        context=context,
        rejected=tuple(rejected),
    )


def require_stage_match(role: str, stage: str, document: SkillDocument) -> None:
    """Refuse a declared skill whose metadata contradicts the running stage.

    A role definition states the procedures its role must always carry. When such
    a skill also declares stages, and the current stage is not one of them, the
    metadata and the definition disagree; loading it anyway would silently keep a
    procedure the skill itself says does not belong here.
    """

    if document.stages and stage not in document.stages:
        raise SkillLoadError(
            SKILL_STAGE_MISMATCH,
            f"Skill '{document.name}' is declared by the '{role}' definition but "
            f"declares stages {', '.join(document.stages)}, which do not include "
            f"'{stage}'.",
        )


__all__ = [
    "AREA_ALIASES",
    "COMPATIBILITY_BASELINE_ONLY",
    "COMPATIBILITY_CONTEXTUAL",
    "COMPATIBILITY_NO_INTAKE",
    "SOURCE_INTAKE",
    "SOURCE_ROUTE",
    "SOURCE_SCOPE",
    "SELECTION_POLICY",
    "SKILL_SELECTION_CHANGED",
    "STAGE_FOR_ROLE",
    "SkillContext",
    "SkillSelection",
    "TaskFacts",
    "build_skill_context",
    "intake_facts",
    "normalize_area",
    "requested_skill_names",
    "require_stage_match",
    "select_role_skills",
]
