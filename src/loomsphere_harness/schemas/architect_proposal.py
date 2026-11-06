"""Structured Architect scope proposal contract.

The proposal is the only artifact a human may approve. Parsing is deliberately
tolerant about surrounding prose and fenced blocks, but validation is strict:
a run that does not produce a well-formed proposal cannot become approvable.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from loomsphere_harness.schemas.acceptance_contract import AcceptanceVerification

FENCED_BLOCK_PATTERN = re.compile(r"```(?:json)?\s*(.+?)```", re.DOTALL)
REVISION_REASON_MAX_LENGTH = 2_000

#: Why a response was not accepted as a structured document. The vocabulary is
#: shared by every role so a recorded failure says *which kind* of problem the
#: response had, and so the one bounded format-correction retry can name it back.
PAYLOAD_MISSING = "payload-missing"
PAYLOAD_AMBIGUOUS = "payload-ambiguous"
PAYLOAD_INVALID = "payload-invalid"


class ProposalValidationError(ValueError):
    """The agent response did not contain a valid scope proposal."""

    def __init__(self, message: str, *, code: str = PAYLOAD_INVALID) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


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


def _balanced_objects(text: str) -> list[str]:
    """Every top-level balanced ``{...}`` span, in document order.

    Scanning stops at the first brace that never closes. An inner fragment of a
    damaged object is never offered as a candidate: accepting ``{"nested": 1}`` out
    of ``{"outer": {"nested": 1}`` would hide the truncation instead of failing.
    """

    found: list[str] = []
    index = 0
    while True:
        start = text.find("{", index)
        if start == -1:
            return found
        span = _balanced_object(text[start:])
        if span is None:
            return found
        found.append(span)
        index = start + len(span)


def payload_candidates(text: str) -> list[str]:
    """The places a JSON payload may live, in the order they may be trusted.

    The response contract asks for exactly one fenced ```json block, so a fenced
    block is the agent's declared answer and is tried first, last block first.
    Prose and inline spans are only fallbacks: a quoted JSON *example* in the
    surrounding text must never override the payload the agent declared. The raw
    response is the last resort, so a response that is nothing but an object still
    parses. Ordering never weakens validation: a candidate must still parse and
    still satisfy the contract, and no candidate at all fails closed.
    """

    fenced = [match.group(1) for match in FENCED_BLOCK_PATTERN.finditer(text)]
    return [
        *reversed(fenced),
        *reversed(_balanced_objects(text)),
        text,
    ]


@dataclass(frozen=True)
class PayloadCandidate:
    """One JSON object a response offers, with where it was found."""

    payload: dict[str, Any]
    source: str
    text: str


def _canonical_payload_key(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=str)


def _as_object(raw: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, RecursionError):
        return None
    return payload if isinstance(payload, dict) else None


def payload_objects(text: str) -> list[PayloadCandidate]:
    """Every distinct JSON object a response offers, most trustworthy first.

    Fenced blocks are the agent's declared answer and come first, last block
    first, exactly as :func:`payload_candidates` orders them. Prose spans are only
    a fallback for a response that fences nothing parseable. Byte-identical
    repeats collapse, because a restated object is not a second answer.
    """

    candidates: list[PayloadCandidate] = []
    seen: set[str] = set()

    def offer(source: str, raw: str) -> None:
        payload = _as_object(raw)
        if payload is None:
            return
        key = _canonical_payload_key(payload)
        if key in seen:
            return
        seen.add(key)
        candidates.append(PayloadCandidate(payload=payload, source=source, text=raw))

    fenced = [match.group(1) for match in FENCED_BLOCK_PATTERN.finditer(text)]
    for raw in reversed(fenced):
        offer("fenced", raw)
    if candidates:
        return candidates
    for raw in reversed(_balanced_objects(text)):
        offer("document", raw)
    return candidates


def select_payload(
    text: str,
    *,
    role: str,
    matches: Callable[[dict[str, Any]], bool],
) -> PayloadCandidate:
    """Pick the one object that can be the role's document, or fail closed.

    A response is accepted only when exactly one object could be the document and
    it is the one the agent declared. Two different documents, or a declared
    object that is not a document while another object is, is ambiguity: choosing
    one would silently record a report the agent may not have meant.
    """

    candidates = payload_objects(text)
    if not candidates:
        raise ProposalValidationError(
            f"The {role} response did not contain a JSON object.", code=PAYLOAD_MISSING
        )
    matching = [candidate for candidate in candidates if matches(candidate.payload)]
    if len(matching) > 1:
        raise ProposalValidationError(
            f"The {role} response contains {len(matching)} different documents that could "
            "each be the answer; reply with exactly one.",
            code=PAYLOAD_AMBIGUOUS,
        )
    if matching and matching[0] is not candidates[0]:
        raise ProposalValidationError(
            f"The {role} response declares a JSON object that is not a {role} document and "
            "also contains one that is; reply with exactly one document.",
            code=PAYLOAD_AMBIGUOUS,
        )
    return candidates[0]


def extract_payload(text: str) -> dict[str, Any]:
    for candidate in payload_candidates(text):
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload

    raise ProposalValidationError(
        "The agent response did not contain a JSON object.", code=PAYLOAD_MISSING
    )


def is_proposal_payload(payload: dict[str, Any]) -> bool:
    """Whether an object has the shape of a scope proposal rather than an example."""

    return (
        isinstance(payload.get("summary"), str)
        and isinstance(payload.get("components"), list)
        and isinstance(payload.get("acceptance_criteria_mapping"), list)
    )


def parse_architect_proposal(text: str) -> ArchitectProposal:
    candidate = select_payload(text, role="Architect", matches=is_proposal_payload)
    try:
        return ArchitectProposal.model_validate(candidate.payload)
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
    "PAYLOAD_AMBIGUOUS",
    "PAYLOAD_INVALID",
    "PAYLOAD_MISSING",
    "ArchitectProposal",
    "ArchitectRevisionCriterion",
    "ArchitectRevisionRequest",
    "PayloadCandidate",
    "ProposalComponent",
    "ProposalCriterion",
    "ProposalValidationError",
    "canonical_proposal",
    "extract_payload",
    "is_proposal_payload",
    "parse_architect_proposal",
    "payload_candidates",
    "payload_objects",
    "proposal_digest",
    "revision_coverage_error",
    "select_payload",
]
