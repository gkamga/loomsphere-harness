"""Structured Architect scope proposal contract.

The proposal is the only artifact a human may approve. Parsing is deliberately
tolerant about surrounding prose and fenced blocks, but validation is strict:
a run that does not produce a well-formed proposal cannot become approvable.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from loomsphere_harness.schemas.acceptance_contract import AcceptanceVerification

FENCED_BLOCK_PATTERN = re.compile(r"```(?:json)?\s*(.+?)```", re.DOTALL)
REVISION_REASON_MAX_LENGTH = 2_000


class ProposalValidationError(ValueError):
    """The agent response did not contain a valid scope proposal."""


class ProposalComponent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    path: str = Field(min_length=1, max_length=500)
    change: str = Field(min_length=1, max_length=4000)

    @field_validator("path", "change")
    @classmethod
    def strip_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped


class ProposalCriterion(BaseModel):
    """One proposed acceptance criterion plus its declared verification.

    ``verification`` is optional so that archived proposals keep parsing. The
    Approver's contract builder still needs a way to tell a machine-checkable
    obligation from a manual one, so an omitted verification becomes
    ``unverified`` rather than being assumed satisfied.
    """

    model_config = ConfigDict(extra="ignore")

    criterion: str = Field(min_length=1, max_length=2000)
    approach: str = Field(min_length=1, max_length=4000)
    mandatory: bool = True
    verification: AcceptanceVerification | None = None

    @field_validator("criterion", "approach")
    @classmethod
    def strip_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @field_validator("verification", mode="before")
    @classmethod
    def normalize_verification(cls, value):
        if value is None or isinstance(value, AcceptanceVerification):
            return value
        if isinstance(value, str):
            # Shorthand: a bare selector string means a pytest verification.
            # A ``protected-pytest`` verification gets no shorthand on purpose:
            # it needs an artifact key *and* a node id, and the obvious
            # ``"<key>::<node id>"`` form is indistinguishable from a worktree
            # selector, which would let a Builder-relative path be read as an
            # authoritative artifact reference. A proposal that wants protected
            # provenance must submit the full mapping.
            return {"kind": "pytest", "selector": value}
        return value


def _clean_list(values: list[str]) -> list[str]:
    cleaned = [value.strip() for value in values]
    if any(not value for value in cleaned):
        raise ValueError("entries must not be blank")
    return cleaned


class ArchitectRevisionCriterion(BaseModel):
    """One exact acceptance obligation required by a controlled revision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    criterion: str = Field(min_length=1, max_length=2_000)
    verification: AcceptanceVerification

    @field_validator("criterion")
    @classmethod
    def strip_criterion(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @model_validator(mode="after")
    def require_unpinned_protected_pytest(self) -> ArchitectRevisionCriterion:
        verification = self.verification
        if (
            verification.kind != "protected-pytest"
            or not verification.artifact
            or not verification.selector
            or verification.artifact_version is not None
            or verification.artifact_digest is not None
        ):
            raise ValueError(
                "revision obligations require an unpinned protected-pytest "
                "artifact and selector"
            )
        return self


class ArchitectRevisionRequest(BaseModel):
    """A durable, human-authorized brief for revising a pending proposal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: str = Field(min_length=1, max_length=REVISION_REASON_MAX_LENGTH)
    acceptance_requirements: list[ArchitectRevisionCriterion] = Field(
        min_length=1,
        max_length=100,
    )

    @field_validator("reason")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @model_validator(mode="after")
    def require_distinct_acceptance_requirements(self) -> ArchitectRevisionRequest:
        normalized = [
            " ".join(item.criterion.casefold().split())
            for item in self.acceptance_requirements
        ]
        if len(normalized) != len(set(normalized)):
            raise ValueError("revision acceptance requirements must be distinct")
        return self


class ArchitectProposal(BaseModel):
    model_config = ConfigDict(extra="ignore")

    summary: str = Field(min_length=1, max_length=4000)
    implementation_approach: list[str] = Field(min_length=1, max_length=50)
    components: list[ProposalComponent] = Field(min_length=1, max_length=100)
    acceptance_criteria_mapping: list[ProposalCriterion] = Field(
        min_length=1,
        max_length=100,
    )
    risks: list[str] = Field(default_factory=list, max_length=50)
    assumptions: list[str] = Field(default_factory=list, max_length=50)
    dependencies: list[str] = Field(default_factory=list, max_length=50)
    validation_strategy: list[str] = Field(min_length=1, max_length=50)
    open_questions: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("summary")
    @classmethod
    def strip_summary(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @field_validator(
        "implementation_approach",
        "risks",
        "assumptions",
        "dependencies",
        "validation_strategy",
        "open_questions",
    )
    @classmethod
    def strip_entries(cls, values: list[str]) -> list[str]:
        return _clean_list(values)


def _balanced_object(text: str) -> str | None:
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        character = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def extract_payload(text: str) -> dict[str, Any]:
    candidates = [match.group(1) for match in FENCED_BLOCK_PATTERN.finditer(text)]
    candidates.append(text)
    balanced = _balanced_object(text)
    if balanced is not None:
        candidates.append(balanced)

    for candidate in reversed(candidates):
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload

    raise ProposalValidationError(
        "The agent response did not contain a JSON object."
    )


def parse_architect_proposal(text: str) -> ArchitectProposal:
    payload = extract_payload(text)
    try:
        return ArchitectProposal.model_validate(payload)
    except Exception as error:  # pydantic.ValidationError
        raise ProposalValidationError(
            f"The scope proposal did not match the contract: {error}"
        ) from error


def canonical_proposal(proposal: ArchitectProposal) -> dict[str, Any]:
    return proposal.model_dump(mode="json")


def proposal_digest(proposal: ArchitectProposal) -> str:
    canonical = json.dumps(
        canonical_proposal(proposal),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def revision_coverage_error(
    proposal: ArchitectProposal,
    revision: ArchitectRevisionRequest,
) -> str | None:
    """Require every revision obligation to be mandatory and independently pinned."""

    criteria = proposal.acceptance_criteria_mapping
    for requirement in revision.acceptance_requirements:
        normalized = " ".join(requirement.criterion.casefold().split())
        matches = [
            criterion
            for criterion in criteria
            if " ".join(criterion.criterion.casefold().split()) == normalized
        ]
        if len(matches) != 1:
            return (
                "The revised proposal must include each requested acceptance "
                "requirement exactly once."
            )
        criterion = matches[0]
        verification = criterion.verification
        required_verification = requirement.verification
        if not criterion.mandatory:
            return (
                f"Revision requirement {requirement.criterion!r} must be mandatory."
            )
        if (
            verification is None
            or verification.kind != "protected-pytest"
            or verification.artifact != required_verification.artifact
            or verification.selector != required_verification.selector
            or verification.artifact_version is not None
            or verification.artifact_digest is not None
        ):
            return (
                f"Revision requirement {requirement.criterion!r} must use its "
                "requested protected-pytest artifact and selector."
            )
    return None


__all__ = [
    "ArchitectProposal",
    "ArchitectRevisionCriterion",
    "ArchitectRevisionRequest",
    "ProposalComponent",
    "ProposalCriterion",
    "ProposalValidationError",
    "canonical_proposal",
    "extract_payload",
    "parse_architect_proposal",
    "proposal_digest",
    "revision_coverage_error",
]
