"""Structured, versioned acceptance contracts.

The free-text acceptance criteria on an engineering task are a narrative, not an
executable obligation. An agent can silently drop one of them, report every
criterion it *did* address as ``met``, and still be accepted by QA and the
Reviewer: no component compared the approved requirement set to the recorded
evidence.

This module turns the approved acceptance criteria into an immutable, versioned
contract owned by the application rather than by the agent prompts:

* every criterion has a stable identifier (``AC-1``, ``AC-2``, ...) that agents
  must echo back, so matching does not depend on paraphrase;
* every criterion declares whether it is mandatory;
* every criterion declares *how* it can be verified. ``pytest`` means the
  application can execute a bounded test selector on its own; ``unverified``
  means no machine check exists and the criterion cannot silently pass;
* the whole contract is digest-addressed, so the contract a delivery relies on
  is the contract a human approved.

The contract is deliberately boring data. Orchestration and authorization stay
in application code (see the LoomSphere application); an LLM can propose
criteria but never decides whether they are satisfied.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CONTRACT_SCHEMA_VERSION = "1.0.0"
CONTRACT_VERSION_MIN = 1
CONTRACT_SOURCES: tuple[str, ...] = ("scope-approval", "amendment")
VERIFICATION_KINDS: tuple[str, ...] = (
    "pytest",
    "protected-pytest",
    "unverified",
)

CRITERION_TEXT_MAX_LENGTH = 2_000
CRITERION_APPROACH_MAX_LENGTH = 2_000
SELECTOR_MAX_LENGTH = 500
MAX_CRITERIA = 200
ARTIFACT_KEY_MAX_LENGTH = 64

_CRITERION_ID = re.compile(r"^AC-[1-9][0-9]{0,2}$")
_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
_NODE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_+.\-\[\]=]*$")
_ARTIFACT_KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class AcceptanceContractError(ValueError):
    """The proposed or stored contract cannot be trusted.

    The code is part of the rendered exception so it survives being raised from
    inside a pydantic validator, which re-wraps ``ValueError`` into a generic
    ``ValidationError`` and would otherwise discard the reason. ``_from_validation_error``
    reads that prefix back.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


_VALIDATION_MESSAGE = re.compile(r"^(?:Value error, )?([a-z][a-z0-9-]+): (.+)$", re.DOTALL)


def _from_validation_error(error: Exception) -> AcceptanceContractError:
    """Recover the criterion-level reason from a wrapped pydantic error."""

    details = error.errors() if hasattr(error, "errors") else []
    for detail in details:
        match = _VALIDATION_MESSAGE.match(str(detail.get("msg", "")))
        if match:
            return AcceptanceContractError(match.group(1), match.group(2))
    return AcceptanceContractError(
        "acceptance-contract-invalid",
        f"The acceptance contract is invalid: {error}",
    )


def normalize_criterion_text(value: str) -> str:
    """Comparison form of a criterion, without changing the stored wording."""

    collapsed = " ".join(value.split())
    return collapsed.rstrip(" .;:").casefold()


def validate_selector(value: str) -> str:
    """Validate one bounded pytest selector such as ``tests/test_x.py::test_y``.

    The selector becomes an argument of a subprocess, so it is restricted to a
    conservative character set, a relative repository path, and optional
    ``::``-separated node identifiers. It can never introduce a shell command,
    an absolute path, or a parent-directory escape.
    """

    selector = value.strip()
    if not selector:
        raise AcceptanceContractError(
            "acceptance-selector-invalid",
            "A machine-verifiable criterion needs a test selector.",
        )
    if len(selector) > SELECTOR_MAX_LENGTH:
        raise AcceptanceContractError(
            "acceptance-selector-invalid",
            f"The test selector must be at most {SELECTOR_MAX_LENGTH} characters.",
        )
    if (
        selector.startswith("/")
        or selector.startswith("-")
        or "\\" in selector
        or ".." in selector
        or any(character.isspace() for character in selector)
    ):
        raise AcceptanceContractError(
            "acceptance-selector-invalid",
            "The test selector must be a relative, plain path with no whitespace "
            "and no parent-directory segments.",
        )

    path, *identifiers = selector.split("::")
    segments = path.split("/")
    if not path.endswith(".py") or not all(
        _PATH_SEGMENT.match(segment) for segment in segments
    ):
        raise AcceptanceContractError(
            "acceptance-selector-invalid",
            "The test selector must be a relative Python test path such as "
            "`tests/test_module.py::test_case`.",
        )
    for identifier in identifiers:
        if not _NODE_IDENTIFIER.match(identifier):
            raise AcceptanceContractError(
                "acceptance-selector-invalid",
                "The test selector contains an unsupported node identifier.",
            )
    return selector


def validate_node_selector(value: str) -> str:
    """Validate a node identifier inside a protected artifact.

    A protected artifact is addressed by its manifest entry, so its selector is
    only the ``::``-suffixed node part. A repository path here would point the
    runner back into the Builder's commit, which is the exact bypass this
    feature removes.
    """

    selector = value.strip()
    if not selector:
        raise AcceptanceContractError(
            "acceptance-protected-selector-invalid",
            "A protected selector must name a test node or be omitted.",
        )
    if len(selector) > SELECTOR_MAX_LENGTH:
        raise AcceptanceContractError(
            "acceptance-protected-selector-invalid",
            f"A protected selector must be at most {SELECTOR_MAX_LENGTH} "
            "characters.",
        )
    if (
        selector.startswith("/")
        or selector.startswith("-")
        or "\\" in selector
        or ".." in selector
        or ".py" in selector
        or any(character.isspace() for character in selector)
    ):
        raise AcceptanceContractError(
            "acceptance-protected-selector-invalid",
            "A protected selector is a test node such as `test_null`, not a "
            "repository path.",
        )
    if not all(
        _NODE_IDENTIFIER.match(identifier) for identifier in selector.split("::")
    ):
        raise AcceptanceContractError(
            "acceptance-protected-selector-invalid",
            "The protected selector contains an unsupported node identifier.",
        )
    return selector


class AcceptanceVerification(BaseModel):
    """How application code can check one criterion without an LLM.

    ``pytest`` is the original, Builder-relative route: the selector is resolved
    inside the implementation commit. ``protected-pytest`` names a verification
    artifact held in the human-owned registry and pins the exact version and
    content digest a human approved, so execution reads those bytes instead of
    whatever happens to be in the Builder's checkout.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["pytest", "protected-pytest", "unverified"] = "unverified"
    selector: str | None = None
    artifact: str | None = None
    artifact_version: int | None = None
    artifact_digest: str | None = None

    @model_validator(mode="after")
    def check_shape(self) -> AcceptanceVerification:
        if self.kind == "pytest":
            if self.selector is None:
                raise AcceptanceContractError(
                    "acceptance-selector-invalid",
                    "A pytest verification requires a test selector.",
                )
            validate_selector(self.selector)
            self._reject_artifact_fields()
        elif self.kind == "protected-pytest":
            self._check_protected()
        else:
            if self.selector is not None:
                raise AcceptanceContractError(
                    "acceptance-selector-invalid",
                    "An unverified criterion cannot declare a test selector.",
                )
            self._reject_artifact_fields()
        return self

    def _reject_artifact_fields(self) -> None:
        if (
            self.artifact is not None
            or self.artifact_version is not None
            or self.artifact_digest is not None
        ):
            raise AcceptanceContractError(
                "acceptance-protected-selector-invalid",
                "Only a protected-pytest verification may reference a protected "
                "artifact.",
            )

    def _check_protected(self) -> None:
        artifact = str(self.artifact or "").strip()
        if not _ARTIFACT_KEY.match(artifact):
            raise AcceptanceContractError(
                "acceptance-protected-selector-invalid",
                "A protected verification must name its registry artifact with "
                "a lowercase slug such as `slugify-null`.",
            )
        if self.artifact_version is not None and self.artifact_version < 1:
            raise AcceptanceContractError(
                "acceptance-protected-selector-invalid",
                "A protected artifact version starts at 1.",
            )
        if self.artifact_digest is not None and not _SHA256.match(
            str(self.artifact_digest).strip()
        ):
            raise AcceptanceContractError(
                "acceptance-protected-selector-invalid",
                "A protected artifact digest must be a lowercase SHA-256 hex "
                "string.",
            )
        if self.selector is not None:
            validate_node_selector(self.selector)

    @property
    def machine_verifiable(self) -> bool:
        if self.kind == "pytest":
            return self.selector is not None
        if self.kind == "protected-pytest":
            return self.artifact is not None and self.artifact_digest is not None
        return False


class AcceptanceCriterion(BaseModel):
    """One frozen obligation with a stable identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    text: str
    approach: str = ""
    mandatory: bool = True
    verification: AcceptanceVerification = Field(
        default_factory=AcceptanceVerification
    )

    @field_validator("id")
    @classmethod
    def check_id(cls, value: str) -> str:
        stripped = value.strip()
        if not _CRITERION_ID.match(stripped):
            raise AcceptanceContractError(
                "acceptance-criterion-id-invalid",
                "Criterion identifiers must look like `AC-1`.",
            )
        return stripped

    @field_validator("text")
    @classmethod
    def check_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise AcceptanceContractError(
                "acceptance-criterion-text-blank",
                "A criterion must state an observable requirement.",
            )
        if len(stripped) > CRITERION_TEXT_MAX_LENGTH:
            raise AcceptanceContractError(
                "acceptance-criterion-text-too-long",
                f"A criterion must be at most {CRITERION_TEXT_MAX_LENGTH} "
                "characters.",
            )
        return stripped

    @field_validator("approach")
    @classmethod
    def check_approach(cls, value: str) -> str:
        if len(value) > CRITERION_APPROACH_MAX_LENGTH:
            raise AcceptanceContractError(
                "acceptance-criterion-approach-too-long",
                f"A criterion approach must be at most "
                f"{CRITERION_APPROACH_MAX_LENGTH} characters.",
            )
        return value

    @property
    def normalized_text(self) -> str:
        return normalize_criterion_text(self.text)


class AcceptanceContract(BaseModel):
    """The immutable, versioned set of obligations an approval froze."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = CONTRACT_SCHEMA_VERSION
    version: int = 1
    source: Literal["scope-approval", "amendment"] = "scope-approval"
    criteria: list[AcceptanceCriterion]

    @field_validator("version")
    @classmethod
    def check_version(cls, value: int) -> int:
        if value < CONTRACT_VERSION_MIN:
            raise AcceptanceContractError(
                "acceptance-contract-version-invalid",
                "An acceptance contract version starts at 1.",
            )
        return value

    @field_validator("schema_version")
    @classmethod
    def check_schema_version(cls, value: str) -> str:
        if value != CONTRACT_SCHEMA_VERSION:
            raise AcceptanceContractError(
                "acceptance-contract-schema-unsupported",
                f"Unsupported acceptance contract schema '{value}'.",
            )
        return value

    @field_validator("criteria")
    @classmethod
    def check_criteria(
        cls, value: list[AcceptanceCriterion]
    ) -> list[AcceptanceCriterion]:
        if not value:
            raise AcceptanceContractError(
                "acceptance-contract-empty",
                "An acceptance contract needs at least one criterion.",
            )
        if len(value) > MAX_CRITERIA:
            raise AcceptanceContractError(
                "acceptance-contract-too-large",
                f"An acceptance contract holds at most {MAX_CRITERIA} criteria.",
            )
        identifiers = [criterion.id for criterion in value]
        if len(set(identifiers)) != len(identifiers):
            raise AcceptanceContractError(
                "acceptance-contract-duplicate-id",
                "Criterion identifiers must be unique within a contract.",
            )
        texts = [criterion.normalized_text for criterion in value]
        if len(set(texts)) != len(texts):
            raise AcceptanceContractError(
                "acceptance-contract-duplicate-text",
                "Criteria must be distinguishable; two criteria describe the "
                "same requirement.",
            )
        if not any(criterion.mandatory for criterion in value):
            raise AcceptanceContractError(
                "acceptance-contract-no-mandatory-criterion",
                "An acceptance contract needs at least one mandatory criterion.",
            )
        for criterion in value:
            verification = criterion.verification
            if verification.kind != "protected-pytest":
                continue
            if (
                verification.artifact_version is None
                or verification.artifact_digest is None
            ):
                raise AcceptanceContractError(
                    "acceptance-protected-unpinned",
                    f"Criterion {criterion.id} names a protected artifact but "
                    "does not pin its approved version and digest, so the "
                    "contract cannot prove which artifact it trusts.",
                )
        return value

    @property
    def mandatory_criteria(self) -> list[AcceptanceCriterion]:
        return [criterion for criterion in self.criteria if criterion.mandatory]

    def canonical(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def canonical_contract(contract: AcceptanceContract) -> dict[str, Any]:
    return contract.canonical()


def contract_digest(contract: AcceptanceContract) -> str:
    """Digest of the canonical contract document."""

    canonical = json.dumps(
        contract.canonical(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _verification_from(value: Any) -> AcceptanceVerification:
    if isinstance(value, AcceptanceVerification):
        return value
    if isinstance(value, dict):
        return AcceptanceVerification.model_validate(value)
    return AcceptanceVerification()


def build_contract(
    criteria: list[Any],
    *,
    version: int = CONTRACT_VERSION_MIN,
    source: str = "scope-approval",
) -> AcceptanceContract:
    """Freeze approved proposal criteria into a numbered contract.

    Numbering is positional and therefore deterministic: the first approved
    criterion becomes ``AC-1`` and keeps that identity for the life of the
    contract. A re-approval produces a new contract version, not a mutation.
    """

    frozen: list[AcceptanceCriterion] = []
    try:
        for index, item in enumerate(criteria, start=1):
            if isinstance(item, dict):
                text = str(item.get("criterion", "")).strip()
                approach = str(item.get("approach", ""))
                mandatory = bool(item.get("mandatory", True))
                verification = _verification_from(item.get("verification"))
            else:
                text = str(getattr(item, "criterion", "")).strip()
                approach = str(getattr(item, "approach", ""))
                mandatory = bool(getattr(item, "mandatory", True))
                verification = _verification_from(
                    getattr(item, "verification", None)
                )
            frozen.append(
                AcceptanceCriterion(
                    id=f"AC-{index}",
                    text=text,
                    approach=approach,
                    mandatory=mandatory,
                    verification=verification,
                )
            )
        return AcceptanceContract(
            version=version,
            source=source,  # type: ignore[arg-type]
            criteria=frozen,
        )
    except AcceptanceContractError:
        raise
    except Exception as error:  # a wrapped pydantic ValidationError
        raise _from_validation_error(error) from error


def parse_contract(document: Any) -> AcceptanceContract:
    """Validate a stored contract document."""

    if not isinstance(document, dict):
        raise AcceptanceContractError(
            "acceptance-contract-invalid",
            "A stored acceptance contract is not a JSON object.",
        )
    try:
        return AcceptanceContract.model_validate(document)
    except AcceptanceContractError:
        raise
    except Exception as error:  # pydantic.ValidationError
        raise _from_validation_error(error) from error


__all__ = [
    "AcceptanceContract",
    "AcceptanceContractError",
    "AcceptanceCriterion",
    "AcceptanceVerification",
    "ARTIFACT_KEY_MAX_LENGTH",
    "CONTRACT_SCHEMA_VERSION",
    "CONTRACT_SOURCES",
    "CONTRACT_VERSION_MIN",
    "MAX_CRITERIA",
    "SELECTOR_MAX_LENGTH",
    "VERIFICATION_KINDS",
    "build_contract",
    "canonical_contract",
    "contract_digest",
    "normalize_criterion_text",
    "parse_contract",
    "validate_node_selector",
    "validate_selector",
]
