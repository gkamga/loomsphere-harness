"""Structured reports returned by the Builder, QA, and Reviewer roles.

Each stage answers with one fenced ``json`` payload. This module turns that text
into a typed, validated record: an unreadable, ambiguous, or incomplete report is a
contract failure, never a silent partial success. The parsers share
``extract_payload`` with the Architect, so a quoted example inside the prose cannot
override the payload the model actually declared.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from loomsphere_harness.schemas.architect_proposal import extract_payload

REPORT_SCHEMA_VERSION = "1.0.0"

CRITERION_STATUSES = frozenset({"met", "unmet", "unknown"})
SEVERITIES = frozenset({"blocking", "major", "minor"})
QA_VERDICTS = frozenset({"passed", "failed"})
REVIEW_VERDICTS = frozenset({"accepted", "changes-required", "rejected"})

TEXT_MAX_LENGTH = 4_000
LIST_MAX_ITEMS = 200
MAX_CLAIMED_FILES = 200


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
    value = payload.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ReportError(
            "report-invalid", f"The report field {key!r} must be a list of strings."
        )
    if len(value) > LIST_MAX_ITEMS:
        raise ReportError("report-invalid", f"The report field {key!r} has too many items.")
    return [item.strip() for item in value if item.strip()]


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
    raw = payload.get("acceptance_criteria", [])
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ReportError(
            "report-invalid", "acceptance_criteria must be a list of objects."
        )
    if len(raw) > LIST_MAX_ITEMS:
        raise ReportError(
            "report-invalid", "acceptance_criteria has too many entries."
        )
    claims: list[CriterionClaim] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise ReportError(
                "report-invalid", "Each acceptance criterion must be an object."
            )
        identifier = str(entry.get("criterion_id", "")).strip()
        if not identifier:
            raise ReportError(
                "report-invalid", "Each acceptance criterion needs a criterion_id."
            )
        status = str(entry.get("status", "")).strip().lower()
        if status not in CRITERION_STATUSES:
            raise ReportError(
                "report-invalid",
                f"Criterion {identifier!r} has an unknown status {status!r}.",
            )
        if identifier in seen:
            raise ReportError(
                "report-invalid", f"Criterion {identifier!r} is reported twice."
            )
        seen.add(identifier)
        claims.append(
            CriterionClaim(
                criterion_id=identifier,
                criterion=str(entry.get("criterion", "")).strip(),
                status=status,
                evidence=str(entry.get("evidence", "")).strip(),
            )
        )
    return claims


def _findings(payload: dict[str, Any]) -> list[Finding]:
    raw = payload.get("findings", [])
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ReportError("report-invalid", "findings must be a list of objects.")
    if len(raw) > LIST_MAX_ITEMS:
        raise ReportError("report-invalid", "findings has too many entries.")
    findings: list[Finding] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ReportError("report-invalid", "Each finding must be an object.")
        severity = str(entry.get("severity", "")).strip().lower()
        if severity not in SEVERITIES:
            raise ReportError(
                "report-invalid", f"A finding has an unknown severity {severity!r}."
            )
        description = str(entry.get("description", "")).strip()
        if not description:
            raise ReportError("report-invalid", "A finding needs a description.")
        findings.append(
            Finding(
                severity=severity,
                description=description,
                evidence=str(entry.get("evidence", "")).strip(),
            )
        )
    return findings


def _files(payload: dict[str, Any]) -> list[ChangedFileClaim]:
    raw = payload.get("files", [])
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ReportError("report-invalid", "files must be a list of objects.")
    if len(raw) > MAX_CLAIMED_FILES:
        raise ReportError("report-invalid", "files has too many entries.")
    files: list[ChangedFileClaim] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ReportError("report-invalid", "Each file entry must be an object.")
        path = _relative_path(str(entry.get("path", "")))
        files.append(
            ChangedFileClaim(
                path=path, change=str(entry.get("change", "")).strip()
            )
        )
    return files


def _payload(text: str, *, role: str) -> dict[str, Any]:
    try:
        return extract_payload(text)
    except Exception as error:  # noqa: BLE001 - normalised into one contract failure
        raise ReportError(
            "report-unreadable",
            f"The {role} response did not contain a JSON object: {error}",
        ) from error


# -- parsers -------------------------------------------------------------------


def parse_build_report(text: str) -> BuildReport:
    payload = _payload(text, role="Builder")
    return BuildReport(
        summary=_text(payload, "summary"),
        files=_files(payload),
        acceptance_criteria=_criteria(payload),
        tests_run=_text_list(payload, "tests_run"),
        notes=_text_list(payload, "notes"),
        digest=report_digest(payload),
    )


def parse_qa_report(text: str) -> QaReport:
    payload = _payload(text, role="QA")
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
    payload = _payload(text, role="Reviewer")
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
    "REVIEW_VERDICTS",
    "ReportError",
    "ReviewVerdict",
    "SEVERITIES",
    "BuildReport",
    "missing_criteria",
    "parse_build_report",
    "parse_qa_report",
    "parse_review_verdict",
    "report_digest",
    "unmet_criteria",
]
