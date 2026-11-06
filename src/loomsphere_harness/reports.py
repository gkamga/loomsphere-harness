"""Structured reports returned by the Builder, QA, and Reviewer roles.

Each stage answers with one fenced ``json`` payload. This module turns that text
into a typed, validated record: an unreadable, ambiguous, or incomplete report is a
contract failure, never a silent partial success. Payload selection is shared with
the Architect and the Intake, so a quoted example inside the prose cannot override
the payload the model actually declared, and two candidate reports are refused
rather than silently ranked.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from loomsphere_harness.schemas.architect_proposal import (
    PAYLOAD_AMBIGUOUS,
    ProposalValidationError,
    payload_objects,
    select_payload,
)

REPORT_SCHEMA_VERSION = "1.0.0"

CRITERION_STATUSES = frozenset({"met", "unmet", "unknown"})
SEVERITIES = frozenset({"blocking", "major", "minor"})
QA_VERDICTS = frozenset({"passed", "failed"})
REVIEW_VERDICTS = frozenset({"accepted", "changes-required", "rejected"})

TEXT_MAX_LENGTH = 4_000
LIST_MAX_ITEMS = 200
MAX_CLAIMED_FILES = 200

#: Top-level arrays each role's documented response contract declares. An omitted
#: array is a contract failure, because the gate cannot tell "the agent found
#: nothing" from "the agent never answered the field", and reading the second as
#: the first turns a missing answer into clean evidence. An explicit ``[]`` is a
#: real answer and is preserved exactly as sent.
#:
#: Nested fields stay optional and are documented where they are read: ``evidence``
#: on a finding or a criterion claim, and ``change`` on a file entry. Both add
#: detail to a value the report already asserts, so their absence cannot make an
#: unsatisfied criterion look satisfied.
REQUIRED_ARRAYS: dict[str, tuple[str, ...]] = {
    "Builder": ("files", "acceptance_criteria", "tests_run", "notes"),
    "QA": ("findings", "acceptance_criteria", "residual_risks"),
    "Reviewer": ("findings", "acceptance_criteria", "follow_ups"),
}


class ReportError(RuntimeError):
    """The agent response is not a valid report for its role."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Finding:
    severity: str
    description: str
    evidence: str = ""


@dataclass(frozen=True)
class CriterionClaim:
    criterion_id: str
    criterion: str
    status: str
    evidence: str = ""


@dataclass(frozen=True)
class ChangedFileClaim:
    path: str
    change: str


@dataclass(frozen=True)
class BuildReport:
    summary: str
    files: list[ChangedFileClaim] = field(default_factory=list)
    acceptance_criteria: list[CriterionClaim] = field(default_factory=list)
    tests_run: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    digest: str = ""

    @property
    def paths(self) -> list[str]:
        return [entry.path for entry in self.files]


@dataclass(frozen=True)
class QaReport:
    verdict: str
    summary: str
    findings: list[Finding] = field(default_factory=list)
    acceptance_criteria: list[CriterionClaim] = field(default_factory=list)
    residual_risks: list[str] = field(default_factory=list)
    digest: str = ""

    @property
    def blocking_findings(self) -> list[Finding]:
        return [entry for entry in self.findings if entry.severity == "blocking"]


@dataclass(frozen=True)
class ReviewVerdict:
    verdict: str
    summary: str
    findings: list[Finding] = field(default_factory=list)
    acceptance_criteria: list[CriterionClaim] = field(default_factory=list)
    follow_ups: list[str] = field(default_factory=list)
    digest: str = ""

    @property
    def blocking_findings(self) -> list[Finding]:
        return [
            entry
            for entry in self.findings
            if entry.severity in ("blocking", "major")
        ]


def report_digest(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# -- field readers -------------------------------------------------------------


def _text(payload: dict[str, Any], key: str, *, required: bool = True) -> str:
    value = payload.get(key)
    if value is None:
        if required:
            raise ReportError(
                "report-invalid", f"The report is missing the required field {key!r}."
            )
        return ""
    if not isinstance(value, str):
        raise ReportError("report-invalid", f"The report field {key!r} must be a string.")
    stripped = value.strip()
    if required and not stripped:
        raise ReportError(
            "report-invalid", f"The report field {key!r} must not be empty."
        )
    if len(stripped) > TEXT_MAX_LENGTH:
        raise ReportError(
            "report-invalid", f"The report field {key!r} is too long."
        )
    return stripped


def _text_list(payload: dict[str, Any], key: str) -> list[str]:
    raw = _declared_array(payload, key)
    if not all(isinstance(item, str) for item in raw):
        raise ReportError(
            "report-invalid", f"The report field {key!r} must be a list of strings."
        )
    return [item.strip() for item in raw if item.strip()]


def _declared_array(
    payload: dict[str, Any], key: str, *, limit: int = LIST_MAX_ITEMS
) -> list[Any]:
    """Read a top-level array the role's response contract declares.

    An omitted array is refused as a contract failure. An explicitly empty one is
    returned as-is: ``findings: []`` is the agent saying it found nothing, which is
    a claim the gate may rely on, while a missing ``findings`` is the agent not
    making the claim at all.
    """

    if key not in payload:
        raise ReportError(
            "report-invalid",
            f"The report is missing the required field {key!r}. Reply with every "
            "field the response contract declares, and write [] when there is "
            "nothing to report.",
        )
    value = payload[key]
    if not isinstance(value, list):
        raise ReportError("report-invalid", f"The report field {key!r} must be a list.")
    if len(value) > limit:
        raise ReportError("report-invalid", f"The report field {key!r} has too many items.")
    return value


def _entry_text(entry: dict[str, Any], key: str, *, context: str,
                required: bool = False) -> str:
    """Read one nested field strictly: a wrong type is a contract failure.

    Stringifying whatever the model sent would record a report the agent did not
    mean, so a non-string value is refused instead of coerced.
    """

    if key not in entry:
        if required:
            raise ReportError(
                "report-invalid", f"{context} is missing the required field {key!r}."
            )
        return ""
    value = entry[key]
    if not isinstance(value, str):
        raise ReportError(
            "report-invalid", f"{context} field {key!r} must be a string."
        )
    stripped = value.strip()
    if required and not stripped:
        raise ReportError(
            "report-invalid", f"{context} field {key!r} must not be empty."
        )
    return stripped


def _relative_path(raw: str) -> str:
    """A reported path must stay inside the worktree and stay a plain path."""

    candidate = raw.strip().replace("\\", "/")
    if not candidate:
        raise ReportError("report-invalid", "A reported file path is empty.")
    path = PurePosixPath(candidate)
    if path.is_absolute():
        raise ReportError(
            "report-invalid", f"A reported file path must be relative: {raw!r}"
        )
    if any(part in ("..", "") for part in path.parts):
        raise ReportError(
            "report-invalid",
            f"A reported file path must not escape the worktree: {raw!r}",
        )
    return str(path)


def _criteria(payload: dict[str, Any]) -> list[CriterionClaim]:
    raw = _declared_array(payload, "acceptance_criteria")
    claims: list[CriterionClaim] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise ReportError(
                "report-invalid", "Each acceptance criterion must be an object."
            )
        identifier = _entry_text(
            entry, "criterion_id", context="An acceptance criterion", required=True
        )
        context = f"Criterion {identifier!r}"
        status = _entry_text(entry, "status", context=context, required=True).lower()
        if status not in CRITERION_STATUSES:
            raise ReportError(
                "report-invalid", f"{context} has an unknown status {status!r}."
            )
        if identifier in seen:
            raise ReportError(
                "report-invalid", f"{context} is reported twice."
            )
        seen.add(identifier)
        claims.append(
            CriterionClaim(
                criterion_id=identifier,
                criterion=_entry_text(entry, "criterion", context=context),
                status=status,
                evidence=_entry_text(entry, "evidence", context=context),
            )
        )
    return claims


def _findings(payload: dict[str, Any]) -> list[Finding]:
    raw = _declared_array(payload, "findings")
    findings: list[Finding] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ReportError("report-invalid", "Each finding must be an object.")
        severity = _entry_text(entry, "severity", context="A finding").lower()
        if severity not in SEVERITIES:
            raise ReportError(
                "report-invalid", f"A finding has an unknown severity {severity!r}."
            )
        description = _entry_text(entry, "description", context="A finding",
                                  required=True)
        findings.append(
            Finding(
                severity=severity,
                description=description,
                evidence=_entry_text(entry, "evidence", context="A finding"),
            )
        )
    return findings


def _files(payload: dict[str, Any]) -> list[ChangedFileClaim]:
    raw = _declared_array(payload, "files", limit=MAX_CLAIMED_FILES)
    files: list[ChangedFileClaim] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ReportError("report-invalid", "Each file entry must be an object.")
        path = _relative_path(
            _entry_text(entry, "path", context="A file entry", required=True)
        )
        files.append(
            ChangedFileClaim(
                path=path,
                change=_entry_text(entry, "change", context="A file entry"),
            )
        )
    return files


def _build_shape(payload: dict[str, Any]) -> bool:
    """Whether an object has the shape of a build report rather than an example."""

    return (
        isinstance(payload.get("summary"), str)
        and isinstance(payload.get("acceptance_criteria"), list)
    )


def _verdict_shape(payload: dict[str, Any]) -> bool:
    """Whether an object has the shape of a QA report or a review verdict."""

    return (
        isinstance(payload.get("verdict"), str)
        and isinstance(payload.get("summary"), str)
        and isinstance(payload.get("acceptance_criteria"), list)
    )


def _payload(
    text: str, *, role: str, matches: Callable[[dict[str, Any]], bool]
) -> dict[str, Any]:
    """The one payload the response declares, or a classified contract failure."""

    try:
        candidate = select_payload(text, role=role, matches=matches)
    except ProposalValidationError as error:
        if error.code == PAYLOAD_AMBIGUOUS:
            raise ReportError("report-ambiguous", error.message) from error
        # An answer that never became a document is unreadable, but the refusal
        # names the fields it owed: the correction prompt is this message, so a
        # vague complaint would cost the corrective attempt as well.
        name = report_role_name(role)
        required = ", ".join(repr(key) for key in REQUIRED_ARRAYS[name])
        raise ReportError(
            "report-unreadable",
            f"{error.message} A {name} report must declare the fields {required}.",
        ) from error
    return candidate.payload


#: Verdicts each role may state, keyed by role, so a caller can ask what an answer
#: declared without re-deriving the vocabulary.
VERDICTS_BY_ROLE = {
    "QA": QA_VERDICTS,
    "Reviewer": REVIEW_VERDICTS,
}

#: The runtime names roles in lower case (``qa``), while a report is described by
#: the role's documented name (``QA``). One table keeps both spellings usable so a
#: lookup can never silently miss.
REPORT_ROLE_NAMES = {"builder": "Builder", "qa": "QA", "reviewer": "Reviewer"}


def report_role_name(role: str) -> str:
    """The documented name of a role, accepting either spelling."""

    return REPORT_ROLE_NAMES.get(role.lower(), role)


def declared_verdict(text: str, *, role: str) -> str | None:
    """The verdict an answer explicitly declares in a JSON object it offers.

    Only parseable objects are inspected; the prose around them is never scanned
    for words like "failed", so a summary sentence cannot be mistaken for the
    verdict. This exists so a caller can refuse to re-ask an answer that already
    reached an adverse decision, which is exactly the case where a second attempt
    could only reverse it rather than clarify it.

    It reads the payload, not a validated report: an answer whose verdict is
    declared but whose body breaks the schema still made a decision, and that
    decision is what the caller must not talk it out of.
    """

    known = VERDICTS_BY_ROLE[report_role_name(role)]
    for candidate in payload_objects(text):
        verdict = candidate.payload.get("verdict")
        if isinstance(verdict, str) and verdict.strip().lower() in known:
            return verdict.strip().lower()
    return None


# -- parsers -------------------------------------------------------------------


def parse_build_report(text: str) -> BuildReport:
    payload = _payload(text, role="Builder", matches=_build_shape)
    return BuildReport(
        summary=_text(payload, "summary"),
        files=_files(payload),
        acceptance_criteria=_criteria(payload),
        tests_run=_text_list(payload, "tests_run"),
        notes=_text_list(payload, "notes"),
        digest=report_digest(payload),
    )


def parse_qa_report(text: str) -> QaReport:
    payload = _payload(text, role="QA", matches=_verdict_shape)
    verdict = _text(payload, "verdict").lower()
    if verdict not in QA_VERDICTS:
        raise ReportError(
            "report-invalid", f"The QA verdict must be 'passed' or 'failed', not {verdict!r}."
        )
    return QaReport(
        verdict=verdict,
        summary=_text(payload, "summary"),
        findings=_findings(payload),
        acceptance_criteria=_criteria(payload),
        residual_risks=_text_list(payload, "residual_risks"),
        digest=report_digest(payload),
    )


def parse_review_verdict(text: str) -> ReviewVerdict:
    payload = _payload(text, role="Reviewer", matches=_verdict_shape)
    verdict = _text(payload, "verdict").lower()
    if verdict not in REVIEW_VERDICTS:
        raise ReportError(
            "report-invalid",
            f"The review verdict must be 'accepted', 'changes-required', or "
            f"'rejected', not {verdict!r}.",
        )
    return ReviewVerdict(
        verdict=verdict,
        summary=_text(payload, "summary"),
        findings=_findings(payload),
        acceptance_criteria=_criteria(payload),
        follow_ups=_text_list(payload, "follow_ups"),
        digest=report_digest(payload),
    )


# -- contract coverage ---------------------------------------------------------


def missing_criteria(
    claims: list[CriterionClaim], approved: list[str] | tuple[str, ...]
) -> list[str]:
    """Approved identifiers the report never mentions, in approved order."""

    reported = {claim.criterion_id for claim in claims}
    return [identifier for identifier in approved if identifier not in reported]


def unmet_criteria(claims: list[CriterionClaim]) -> list[str]:
    """Identifiers the report itself says are not satisfied."""

    return [claim.criterion_id for claim in claims if claim.status != "met"]


__all__ = [
    "CRITERION_STATUSES",
    "ChangedFileClaim",
    "CriterionClaim",
    "Finding",
    "MAX_CLAIMED_FILES",
    "QA_VERDICTS",
    "QaReport",
    "REPORT_SCHEMA_VERSION",
    "REQUIRED_ARRAYS",
    "REVIEW_VERDICTS",
    "ReportError",
    "ReviewVerdict",
    "SEVERITIES",
    "BuildReport",
    "REPORT_ROLE_NAMES",
    "VERDICTS_BY_ROLE",
    "declared_verdict",
    "missing_criteria",
    "parse_build_report",
    "parse_qa_report",
    "parse_review_verdict",
    "report_digest",
    "report_role_name",
    "unmet_criteria",
]
