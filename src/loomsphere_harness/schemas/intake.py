"""Structured Intake assessment contract.

Intake is the factory's requirements triage step. It runs *before* the Architect and
answers a different question: not "how should this be built" but "what is being asked
for, what does it touch, what is ambiguous, and which reviews does it imply".

The assessment is a validated document, not prose. Parsing is tolerant about the
surrounding narration a model produces, but the shape is strict: a run that does not
return a well-formed assessment cannot become recorded evidence, and the framework
never guesses what a missing field meant.

Recommendations in this document are advice. `services/intake_policy` is the only
thing that turns them into routing, and it may only ever *add* review or raise risk;
the deterministic router, the human approval, and the frozen route keep the final
authority.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from loomsphere_harness.schemas.architect_proposal import (
    PAYLOAD_INVALID,
    ProposalValidationError,
    select_payload,
)

RequestType = Literal[
    "feature",
    "bugfix",
    "refactor",
    "documentation",
    "security",
    "ui",
    "database",
    "other",
]
RequirementConfidence = Literal["low", "medium", "high"]
ComponentKind = Literal[
    "backend",
    "frontend",
    "database",
    "infrastructure",
    "documentation",
    "tests",
    "shared",
    "unknown",
]
RiskLevel = Literal["low", "medium", "high"]

REQUEST_TYPES: tuple[str, ...] = (
    "feature",
    "bugfix",
    "refactor",
    "documentation",
    "security",
    "ui",
    "database",
    "other",
)
COMPONENT_KINDS: tuple[str, ...] = (
    "backend",
    "frontend",
    "database",
    "infrastructure",
    "documentation",
    "tests",
    "shared",
    "unknown",
)
CONFIDENCE_LEVELS: tuple[str, ...] = ("low", "medium", "high")
RISK_LEVELS: tuple[str, ...] = ("low", "medium", "high")

#: A recommended specialist must name a catalog entry. The pattern is the catalog's
#: own name grammar, so an unusable recommendation is rejected here rather than
#: reaching the router.
SPECIALIST_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+){0,2}$")

#: A recommendation without a reason is not a recommendation, it is a guess. The
#: floor is deliberately above a single word.
MIN_RECOMMENDATION_REASON = 10

MAX_COMPONENTS = 50
MAX_IMPLICATIONS_PER_AREA = 20
MAX_AMBIGUITIES = 25
MAX_RECOMMENDATIONS = 10
MAX_LIST_ENTRIES = 50


class IntakeReportError(ValueError):
    """The agent response did not contain a report matching the intake contract.

    ``code`` carries the failure class the runtime records, so a missing payload
    and a payload that breaks the contract are distinguishable evidence.
    """

    def __init__(self, message: str, *, code: str = PAYLOAD_INVALID) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _Text(BaseModel):
    model_config = ConfigDict(extra="ignore")


def _strip(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("must not be blank")
    return stripped


class IntakeIntent(_Text):
    """What the requester wants, separated from how it might be built."""

    summary: str = Field(min_length=1, max_length=2000)
    outcome: str = Field(default="", max_length=2000)
    request_type: RequestType = "feature"
    confidence: RequirementConfidence = "medium"

    @field_validator("summary")
    @classmethod
    def strip_summary(cls, value: str) -> str:
        return _strip(value)

    @field_validator("outcome")
    @classmethod
    def strip_outcome(cls, value: str) -> str:
        return value.strip()


class AffectedComponent(_Text):
    """One area of the software the request touches.

    `path` is the requester-facing area, not an approved scope component. It is
    evidence for the Architect and the router; it authorizes nothing.
    """

    path: str = Field(min_length=1, max_length=500)
    kind: ComponentKind = "unknown"
    reason: str = Field(default="", max_length=1000)

    @field_validator("path")
    @classmethod
    def strip_path(cls, value: str) -> str:
        return _strip(value)

    @field_validator("reason")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        return value.strip()


class IntakeImplications(_Text):
    """What the request implies for the areas that carry mandatory review."""

    security: list[str] = Field(default_factory=list, max_length=MAX_IMPLICATIONS_PER_AREA)
    ux_ui: list[str] = Field(default_factory=list, max_length=MAX_IMPLICATIONS_PER_AREA)
    database: list[str] = Field(default_factory=list, max_length=MAX_IMPLICATIONS_PER_AREA)
    architecture: list[str] = Field(
        default_factory=list, max_length=MAX_IMPLICATIONS_PER_AREA
    )

    @field_validator("security", "ux_ui", "database", "architecture")
    @classmethod
    def strip_entries(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned):
            raise ValueError("entries must not be blank")
        return cleaned

    def areas(self) -> tuple[str, ...]:
        """The implication areas that carry at least one entry."""

        return tuple(
            area
            for area in ("security", "ux_ui", "database", "architecture")
            if getattr(self, area)
        )


class IntakeAmbiguity(_Text):
    """One clarification the request needs.

    `blocking` means the Architect cannot produce a defensible scope until a human
    answers. A non-blocking ambiguity is recorded for the human decision, not hidden
    and not invented away.
    """

    question: str = Field(min_length=1, max_length=1000)
    why_it_matters: str = Field(default="", max_length=1000)
    blocking: bool = True

    @field_validator("question")
    @classmethod
    def strip_question(cls, value: str) -> str:
        return _strip(value)

    @field_validator("why_it_matters")
    @classmethod
    def strip_why(cls, value: str) -> str:
        return value.strip()


class SpecialistRecommendation(_Text):
    """One specialist the request appears to need, with the reason it is named."""

    specialist: str = Field(min_length=2, max_length=32)
    reason: str = Field(min_length=MIN_RECOMMENDATION_REASON, max_length=1000)
    confidence: RequirementConfidence = "medium"

    @field_validator("specialist")
    @classmethod
    def validate_specialist(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not SPECIALIST_NAME_PATTERN.fullmatch(normalized):
            raise ValueError("must be a catalog specialist name")
        return normalized

    @field_validator("reason")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        return _strip(value)


class IntakeRisk(_Text):
    level: RiskLevel = "low"
    reasons: list[str] = Field(default_factory=list, max_length=MAX_LIST_ENTRIES)

    @field_validator("reasons")
    @classmethod
    def strip_reasons(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned):
            raise ValueError("entries must not be blank")
        return cleaned


class IntakeAssessment(_Text):
    """The structured intake recommendation for one request."""

    intent: IntakeIntent
    affected_components: list[AffectedComponent] = Field(
        default_factory=list, max_length=MAX_COMPONENTS
    )
    implications: IntakeImplications = Field(default_factory=IntakeImplications)
    ambiguities: list[IntakeAmbiguity] = Field(
        default_factory=list, max_length=MAX_AMBIGUITIES
    )
    specialist_recommendations: list[SpecialistRecommendation] = Field(
        default_factory=list, max_length=MAX_RECOMMENDATIONS
    )
    risk: IntakeRisk = Field(default_factory=IntakeRisk)
    assumptions: list[str] = Field(default_factory=list, max_length=MAX_LIST_ENTRIES)
    notes: list[str] = Field(default_factory=list, max_length=MAX_LIST_ENTRIES)

    @field_validator("specialist_recommendations")
    @classmethod
    def unique_specialists(
        cls, values: list[SpecialistRecommendation]
    ) -> list[SpecialistRecommendation]:
        seen: set[str] = set()
        for recommendation in values:
            if recommendation.specialist in seen:
                raise ValueError(
                    f"specialist '{recommendation.specialist}' is recommended twice"
                )
            seen.add(recommendation.specialist)
        return values

    @property
    def blocking_ambiguities(self) -> tuple[IntakeAmbiguity, ...]:
        return tuple(item for item in self.ambiguities if item.blocking)

    @property
    def needs_clarification(self) -> bool:
        return bool(self.blocking_ambiguities)

    @property
    def recommended_specialists(self) -> tuple[str, ...]:
        return tuple(item.specialist for item in self.specialist_recommendations)

    @property
    def implication_areas(self) -> tuple[str, ...]:
        return self.implications.areas()


def is_intake_payload(payload: dict[str, Any]) -> bool:
    """Whether an object has the shape of an intake assessment rather than an example."""

    intent = payload.get("intent")
    return isinstance(intent, dict) and isinstance(intent.get("summary"), str)


def parse_intake_assessment(text: str) -> IntakeAssessment:
    """Parse one agent response into a validated intake assessment."""

    try:
        candidate = select_payload(text, role="Intake", matches=is_intake_payload)
    except ProposalValidationError as error:
        raise IntakeReportError(error.message, code=error.code) from error
    try:
        return IntakeAssessment.model_validate(candidate.payload)
    except Exception as error:  # pydantic.ValidationError
        raise IntakeReportError(
            f"The intake assessment did not match its contract: {error}"
        ) from error


def canonical_intake(assessment: IntakeAssessment) -> dict[str, object]:
    return assessment.model_dump(mode="json")


def intake_digest(assessment: IntakeAssessment) -> str:
    canonical = json.dumps(
        canonical_intake(assessment),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "AffectedComponent",
    "COMPONENT_KINDS",
    "CONFIDENCE_LEVELS",
    "IntakeAmbiguity",
    "IntakeAssessment",
    "IntakeImplications",
    "IntakeIntent",
    "IntakeReportError",
    "IntakeRisk",
    "MAX_AMBIGUITIES",
    "MAX_COMPONENTS",
    "MAX_RECOMMENDATIONS",
    "MIN_RECOMMENDATION_REASON",
    "REQUEST_TYPES",
    "RISK_LEVELS",
    "SPECIALIST_NAME_PATTERN",
    "SpecialistRecommendation",
    "canonical_intake",
    "intake_digest",
    "is_intake_payload",
    "parse_intake_assessment",
]
