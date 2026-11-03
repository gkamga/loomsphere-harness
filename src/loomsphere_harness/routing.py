"""Adaptive specialist routing for the implementation loop.

The loop used to run the same four stages for every task. This module decides
*which reviewers* a task additionally needs, from facts that are already
persisted, and records why.

Two inputs decide a route: the persisted facts about the task, and the trusted
agent catalog (`services/agent_catalog.py`). The catalog declares, per specialist,
the paths and text markers that select it and the rules that add it to a route.
This module evaluates those declarations; it never names a specialist.

The rules that bound it are unchanged:

* A route is a pure function of the persisted facts and the catalog. Nothing here
  reads a clock, a model, or a database, so a route is reproducible and testable.
  The route document records the catalog digest, so a frozen route names exactly
  which catalog selected its review team.
* It never removes a stage. A route is the core review team plus additional
  specialists; every core evidence requirement stays.
* It never grants authority. A specialist adds scrutiny. Promotions, delivery,
  and human approvals are decided in `implementation`, `delivery`, and the approval
  service, not here.

The route is frozen by the human scope approval (`scope_approvals`). After the
Builder commits, :func:`inspect_changes` re-reads what actually changed and can
*escalate* the route: a security-sensitive change that nobody predicted requires
the Security Reviewer even when approval selected the core team. Escalation only
ever adds a specialist, and a change that leaves the approved scope is reported
instead of being silently authorised.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from loomsphere_harness.agent_catalog import (
    AgentCatalog,
    CatalogEntry,
    current_catalog,
)

#: Task shapes the classifier can produce. A task type is a fact recorded about
#: the requirement; it never bypasses a stage. A catalog entry may declare the task
#: type its text markers imply, so the vocabulary is open without being free-form.
TASK_TYPES: tuple[str, ...] = (
    "feature",
    "bugfix",
    "refactor",
    "documentation",
    "security",
    "ui",
)

RISK_LEVELS: tuple[str, ...] = ("low", "medium", "high")
COMPLEXITY_LEVELS: tuple[str, ...] = ("low", "medium", "high")


@dataclass(frozen=True)
class SpecialistSpec:
    """One row of the specialist registry, as the routing layer sees it.

    Everything the runtime needs to run a specialist review is in the catalog
    entry; this is the routing-facing projection of it: the persisted agent role,
    the persisted stage, the setting that may override its definition, and the
    classified-fact key that records why it was routed.
    """

    name: str
    role: str
    stage: str
    persona_setting: str
    label: str
    factor_key: str


def _spec(entry: CatalogEntry) -> SpecialistSpec:
    return SpecialistSpec(
        name=entry.name,
        role=entry.role,
        stage=entry.stage,
        persona_setting=entry.persona_setting,
        label=entry.label,
        factor_key=entry.factor_key,
    )


def specialist_specs(catalog: AgentCatalog | None = None) -> tuple[SpecialistSpec, ...]:
    """The catalog, projected into routing rows, in review order."""

    return tuple(_spec(entry) for entry in (catalog or current_catalog()).entries)


#: Compatibility snapshots of the configured catalog. They are computed on first
#: access (PEP 562), never at import, so importing this module needs no bound
#: settings. Runtime decisions (:func:`resolve_route`, :func:`inspect_changes`,
#: :func:`classify_requirement`, and the stage machinery) resolve the catalog per
#: call, so a newly registered specialist is selected without a reload.
_SNAPSHOT_NAMES = (
    "SPECIALIST_SPECS",
    "SPECIALISTS",
    "SPECIALIST_ROLES",
    "SPECIALIST_STAGES",
    "SPECIALIST_PERSONA_SETTINGS",
    "SPECIALIST_LABELS",
    "SPECIALIST_FACTOR_KEYS",
)


def __getattr__(name: str):
    if name not in _SNAPSHOT_NAMES:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    specs = specialist_specs()
    snapshot = {
        "SPECIALIST_SPECS": specs,
        "SPECIALISTS": tuple(spec.name for spec in specs),
        "SPECIALIST_ROLES": {spec.name: spec.role for spec in specs},
        "SPECIALIST_STAGES": {spec.name: spec.stage for spec in specs},
        "SPECIALIST_PERSONA_SETTINGS": {spec.name: spec.persona_setting for spec in specs},
        "SPECIALIST_LABELS": {spec.name: spec.label for spec in specs},
        "SPECIALIST_FACTOR_KEYS": {spec.name: spec.factor_key for spec in specs},
    }
    return snapshot[name]


def specialist_spec(name: str, catalog: AgentCatalog | None = None) -> SpecialistSpec:
    """Look up one specialist's row, refusing an unknown name."""

    return _spec((catalog or current_catalog()).by_name(name))


#: Ordered stage sequence every route starts from. A route inserts specialist
#: stages between QA and the final Reviewer so the Reviewer sees their findings.
CORE_STAGES: tuple[str, ...] = ("builder", "validation", "qa", "reviewer")
SPECIALIST_STAGE_AFTER = "qa"

FALLBACK_ROUTE_NOT_RECORDED = "route-not-recorded"
FALLBACK_SCOPE_UNPARSEABLE = "scope-paths-unrecognised"

#: Recorded artifacts that show rendered appearance. The factory never renders a
#: change itself, so these are what "visual evidence" can mean here.
RENDERED_EVIDENCE_SUFFIXES: tuple[str, ...] = (
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".pdf",
)
RENDERED_EVIDENCE_SEGMENTS: frozenset[str] = frozenset(
    {"playwright-report", "test-results", "screenshots", "visual-baselines"}
)
EVIDENCE_MODE_CODE = "code-level"
EVIDENCE_MODE_RENDERED = "rendered"

BUGFIX_TEXT_MARKERS: tuple[str, ...] = (
    "fix",
    "bug",
    "defect",
    "regression",
    "broken",
    "crash",
    "guard",
    "null",
    "incorrect",
)

REFACTOR_TEXT_MARKERS: tuple[str, ...] = (
    "refactor",
    "restructure",
    "rename",
    "extract",
    "simplify",
    "deduplicate",
    "cleanup",
)

DOCUMENTATION_TEXT_MARKERS: tuple[str, ...] = (
    "documentation",
    "docs",
    "readme",
    "changelog",
    "guide",
    "adr",
)

#: A route with more affected paths than this is not low complexity. The numbers
#: are facts about scope size, not effort estimates.
COMPLEXITY_MEDIUM_PATHS = 4
COMPLEXITY_HIGH_PATHS = 16
COMPLEXITY_MEDIUM_ROOTS = 3

#: Number of leading path segments that identify the approved top-level area a
#: change belongs to (backend/app/services/x.py -> backend).
_AREA_SEGMENTS = 1


@dataclass(frozen=True)
class TaskClassification:
    """The persisted facts a route is derived from.

    `path_matches` and `text_matches` are keyed by specialist name: a fact belongs
    to the signal that produced it, not to a hard-coded column, so a catalog entry
    with its own path signal is classified exactly like the two built-in reviewers.
    """

    task_type: str
    risk_level: str
    complexity: str
    areas: tuple[str, ...]
    #: Specialist -> scope paths its path signal matched.
    path_matches: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: Specialist -> text markers its declared markers matched.
    text_matches: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: Scope paths that could not be interpreted as repository paths.
    unparsed_paths: tuple[str, ...] = ()

    def paths_for(self, specialist: str) -> tuple[str, ...]:
        return tuple(self.path_matches.get(specialist, ()))

    def text_for(self, specialist: str) -> tuple[str, ...]:
        return tuple(self.text_matches.get(specialist, ()))

    def has_path_matches(self, specialist: str) -> bool:
        return bool(self.paths_for(specialist))

    def has_text_matches(self, specialist: str) -> bool:
        return bool(self.text_for(specialist))

    @property
    def security_paths(self) -> tuple[str, ...]:
        return self.paths_for("security")

    @property
    def ui_paths(self) -> tuple[str, ...]:
        return self.paths_for("ux")

    @property
    def text_signals(self) -> tuple[str, ...]:
        found: set[str] = set()
        for markers in self.text_matches.values():
            found.update(markers)
        return tuple(sorted(found))

    @property
    def security_sensitive(self) -> bool:
        return bool(self.security_paths) or self.task_type == "security"

    def to_factors(self) -> dict[str, object]:
        """JSON-ready facts, stored next to the frozen route.

        The per-specialist maps are the general form. The three named keys are kept
        alongside them because recorded evidence is read by humans and by clients
        that predate the catalog, and a route document must stay readable.
        """

        return {
            "task_type": self.task_type,
            "risk_level": self.risk_level,
            "complexity": self.complexity,
            "areas": list(self.areas),
            "path_matches": {
                specialist: list(paths)
                for specialist, paths in sorted(self.path_matches.items())
            },
            "text_matches": {
                specialist: list(markers)
                for specialist, markers in sorted(self.text_matches.items())
            },
            "security_paths": list(self.security_paths),
            "ui_paths": list(self.ui_paths),
            "text_signals": list(self.text_signals),
            "unparsed_paths": list(self.unparsed_paths),
        }

    @classmethod
    def from_factors(cls, factors: dict[str, object]) -> TaskClassification:
        def _strings(key: str) -> tuple[str, ...]:
            value = factors.get(key, [])
            if not isinstance(value, list):
                return ()
            return tuple(str(item) for item in value)

        def _mapping(key: str) -> dict[str, tuple[str, ...]]:
            value = factors.get(key)
            if not isinstance(value, dict):
                return {}
            return {
                str(specialist): tuple(str(item) for item in items)
                for specialist, items in value.items()
                if isinstance(items, list)
            }

        path_matches = _mapping("path_matches")
        text_matches = _mapping("text_matches")
        if not path_matches:
            # A route document written before the catalog existed stored one named
            # list per specialist. Read it back rather than losing the evidence.
            for specialist, key in (("security", "security_paths"), ("ux", "ui_paths")):
                values = _strings(key)
                if values:
                    path_matches[specialist] = values
        if not text_matches:
            legacy = _strings("text_signals")
            if legacy:
                text_matches["legacy"] = legacy

        return cls(
            task_type=str(factors.get("task_type", "feature")),
            risk_level=str(factors.get("risk_level", "low")),
            complexity=str(factors.get("complexity", "low")),
            areas=_strings("areas"),
            path_matches=path_matches,
            text_matches=text_matches,
            unparsed_paths=_strings("unparsed_paths"),
        )


@dataclass(frozen=True)
class ReviewRoute:
    """The review team a task requires, and why it requires it."""

    specialists: tuple[str, ...]
    reasons: tuple[str, ...]
    fallback_reason: str | None = None
    #: Digest of the trusted catalog that resolved this route. Folded into the
    #: route document, so the freeze records which catalog selected the team.
    catalog_digest: str | None = None
    #: What an Intake assessment recommended and what deterministic policy did with
    #: it. Recorded inside the route document, so the frozen, digest-addressed route
    #: carries the agent's advice and the reason it was or was not adopted.
    intake: dict[str, object] | None = None

    @property
    def stages(self) -> tuple[str, ...]:
        """Ordered stage sequence, specialist stages before the final review."""

        catalog = current_catalog()
        stages: list[str] = []
        for stage in CORE_STAGES:
            if stage == SPECIALIST_STAGE_AFTER:
                stages.append(stage)
                for specialist in self.specialists:
                    entry = catalog.by_name_or_none(specialist)
                    if entry is not None:
                        stages.append(entry.stage)
                continue
            stages.append(stage)
        return tuple(stages)

    @property
    def specialist_stages(self) -> tuple[str, ...]:
        catalog = current_catalog()
        stages: list[str] = []
        for specialist in self.specialists:
            entry = catalog.by_name_or_none(specialist)
            if entry is not None:
                stages.append(entry.stage)
        return tuple(stages)

    def to_document(self) -> dict[str, object]:
        return {
            "specialists": list(self.specialists),
            "reasons": list(self.reasons),
            "fallback_reason": self.fallback_reason,
            "catalog_digest": self.catalog_digest,
            "intake": self.intake,
            "stages": list(self.stages),
        }

    @classmethod
    def from_document(cls, document: dict[str, object]) -> ReviewRoute:
        def _strings(key: str) -> tuple[str, ...]:
            value = document.get(key, [])
            if not isinstance(value, list):
                return ()
            return tuple(str(item) for item in value)

        fallback = document.get("fallback_reason")
        digest = document.get("catalog_digest")
        intake = document.get("intake")
        return cls(
            specialists=_strings("specialists"),
            reasons=_strings("reasons"),
            fallback_reason=str(fallback) if fallback else None,
            catalog_digest=str(digest) if digest else None,
            intake=dict(intake) if isinstance(intake, dict) else None,
        )


@dataclass(frozen=True)
class ChangeInspection:
    """What the recorded change set actually touched, relative to the route."""

    changed_paths: tuple[str, ...]
    approved_roots: tuple[str, ...]
    out_of_scope: tuple[str, ...]
    component_drift: tuple[str, ...]
    #: Specialist -> the changed paths that imply that specialist's review.
    trigger_paths: dict[str, tuple[str, ...]]
    #: Specialists the change requires but the route did not include.
    required_additions: tuple[str, ...]
    reasons: tuple[str, ...]

    @property
    def in_scope(self) -> bool:
        return not self.out_of_scope

    @property
    def security_paths(self) -> tuple[str, ...]:
        return self.trigger_paths.get("security", ())

    @property
    def ui_paths(self) -> tuple[str, ...]:
        return self.trigger_paths.get("ux", ())

    @property
    def rendered_evidence(self) -> tuple[str, ...]:
        return rendered_evidence_paths(self.changed_paths)

    @property
    def evidence_mode(self) -> str:
        return evidence_mode(self.rendered_evidence)

    def to_observations(self) -> dict[str, object]:
        return {
            "changed_paths": list(self.changed_paths),
            "approved_roots": list(self.approved_roots),
            "out_of_scope": list(self.out_of_scope),
            "component_drift": list(self.component_drift),
            # `trigger_paths` is the general form; the two named lists are kept
            # alongside it because recorded evidence is read by humans as well.
            "trigger_paths": {
                specialist: list(paths)
                for specialist, paths in self.trigger_paths.items()
            },
            "security_paths": list(self.security_paths),
            "ui_paths": list(self.ui_paths),
            "rendered_evidence": list(self.rendered_evidence),
            "evidence_mode": self.evidence_mode,
            "required_additions": list(self.required_additions),
            "reasons": list(self.reasons),
        }


# --------------------------------------------------------------------------- #
# Path and text interpretation
# --------------------------------------------------------------------------- #


def normalize_path(value: str) -> str | None:
    """Return a repository-relative path, or None when it is unusable.

    A path that is absolute, escapes its root, or is empty cannot be matched
    against repository scope, so it is reported instead of being coerced.
    """

    raw = value.strip().replace("\\", "/")
    if not raw or raw.startswith("/") or raw.startswith("~"):
        return None
    if re.match(r"^[A-Za-z]:", raw):  # Windows drive letter
        return None
    while raw.startswith("./"):
        raw = raw[2:]
    segments = [segment for segment in raw.split("/") if segment and segment != "."]
    if not segments or any(segment == ".." for segment in segments):
        return None
    return "/".join(segments)


def path_area(path: str) -> str:
    """The top-level area a path belongs to (backend/app/x.py -> backend)."""

    segments = path.split("/")
    return "/".join(segments[:_AREA_SEGMENTS])


def _signal_matches(specialist: str, path: str, catalog: AgentCatalog | None) -> bool:
    entry = (catalog or current_catalog()).by_name_or_none(specialist)
    if entry is None or entry.path_signal is None:
        return False
    return entry.path_signal.matches(path)


def is_security_sensitive_path(path: str) -> bool:
    """Whether the catalog's security path signal matches a path."""

    return _signal_matches("security", path, None)


def is_ui_path(path: str) -> bool:
    """Whether the catalog's UX path signal matches a path.

    The signal is declared data: the frontend area, the file types that define what
    a user sees, and the directories that hold presentation code.
    """

    return _signal_matches("ux", path, None)


def rendered_evidence_paths(paths: Iterable[str]) -> tuple[str, ...]:
    """Recorded artifacts that show what the change looks like when rendered.

    Only artifacts the factory actually recorded are reported. The specialist is
    told the difference between these and its own rendering, which it never does.
    """

    found: list[str] = []
    for candidate in paths:
        path = normalize_path(str(candidate))
        if path is None:
            continue
        lowered = path.lower()
        segments = set(lowered.split("/"))
        if lowered.endswith(RENDERED_EVIDENCE_SUFFIXES) or (
            segments & RENDERED_EVIDENCE_SEGMENTS
        ):
            found.append(path)
    return tuple(sorted(found))


def evidence_mode(rendered: Sequence[str]) -> str:
    """rendered when recorded artifacts exist, code-level otherwise."""

    return EVIDENCE_MODE_RENDERED if rendered else EVIDENCE_MODE_CODE


def _text_signals(text: str, markers: Sequence[str]) -> tuple[str, ...]:
    if not text or not markers:
        return ()
    lowered = text.lower()
    found = {
        marker
        for marker in markers
        if re.search(rf"(?<![a-z0-9]){re.escape(marker)}(?![a-z0-9])", lowered)
    }
    return tuple(sorted(found))


def _joined_text(*parts: str) -> str:
    return "\n".join(part for part in parts if part)


def _classify_task_type(
    text: str,
    *,
    catalog: AgentCatalog,
    text_matched: Mapping[str, bool],
    path_matched: Mapping[str, bool],
) -> str:
    """The task type, from the catalog's declared implications then the text.

    A catalog entry may declare the task type its signal implies. Matched text
    always implies it; a matched path implies it only when the entry declares
    implied_task_type_from_paths, because where the work lands is not always what
    kind of work it is. The first entry in review order that matched decides, which
    is why the catalog's order is part of the recorded route.
    """

    for entry in catalog.entries:
        if not entry.implied_task_type:
            continue
        matched = text_matched.get(entry.name) or (
            entry.implied_task_type_from_paths and path_matched.get(entry.name)
        )
        if matched:
            return entry.implied_task_type
    if _text_signals(text, BUGFIX_TEXT_MARKERS):
        return "bugfix"
    if _text_signals(text, REFACTOR_TEXT_MARKERS):
        return "refactor"
    if _text_signals(text, DOCUMENTATION_TEXT_MARKERS):
        return "documentation"
    return "feature"


def _classify_complexity(paths: Sequence[str]) -> str:
    if len(paths) >= COMPLEXITY_HIGH_PATHS:
        return "high"
    roots = {path_area(path) for path in paths}
    if len(paths) >= COMPLEXITY_MEDIUM_PATHS or len(roots) > COMPLEXITY_MEDIUM_ROOTS:
        return "medium"
    return "low"


def _classify_risk(
    *,
    complexity: str,
    areas: Sequence[str],
    task_type: str,
    matched: Mapping[str, bool],
    catalog: AgentCatalog,
) -> str:
    for entry in catalog.entries:
        if entry.risk_tier == "high" and matched.get(entry.name):
            return "high"
    if complexity == "high":
        return "high"
    if task_type in ("bugfix", "refactor") or len(set(areas)) > 1:
        return "medium"
    return "low"


def classify_requirement(
    *,
    title: str,
    requirements: str,
    acceptance_criteria: str,
    scope_paths: Iterable[str] = (),
    catalog: AgentCatalog | None = None,
) -> TaskClassification:
    """Classify one requirement from its persisted text and approved scope paths.

    scope_paths are the components the Architect proposed for the task. Every
    input is already stored on the task or the proposal, and every rule that reads
    them comes from the trusted catalog, so two calls with the same inputs and the
    same catalog always produce the same classification.
    """

    active = catalog or current_catalog()
    text = _joined_text(title, requirements, acceptance_criteria)
    normalized: list[str] = []
    unparsed: list[str] = []
    for candidate in scope_paths:
        path = normalize_path(str(candidate))
        if path is None:
            unparsed.append(str(candidate))
        else:
            normalized.append(path)

    path_matches: dict[str, tuple[str, ...]] = {}
    for entry in active.entries:
        signal = entry.path_signal
        if signal is None:
            continue
        matched_paths = tuple(
            sorted({path for path in normalized if signal.matches(path)})
        )
        if matched_paths:
            path_matches[entry.name] = matched_paths

    text_matches: dict[str, tuple[str, ...]] = {}
    for entry in active.entries:
        markers = _text_signals(text, entry.text_markers)
        if markers:
            text_matches[entry.name] = markers

    path_matched = {
        entry.name: entry.name in path_matches for entry in active.entries
    }
    text_matched = {
        entry.name: entry.name in text_matches for entry in active.entries
    }
    matched = {
        entry.name: path_matched[entry.name] or text_matched[entry.name]
        for entry in active.entries
    }
    task_type = _classify_task_type(
        text,
        catalog=active,
        text_matched=text_matched,
        path_matched=path_matched,
    )
    complexity = _classify_complexity(normalized)
    areas = tuple(sorted({path_area(path) for path in normalized}))
    risk = _classify_risk(
        complexity=complexity,
        areas=areas,
        task_type=task_type,
        matched=matched,
        catalog=active,
    )
    return TaskClassification(
        task_type=task_type,
        risk_level=risk,
        complexity=complexity,
        areas=areas,
        path_matches=path_matches,
        text_matches=text_matches,
        unparsed_paths=tuple(unparsed),
    )


# --------------------------------------------------------------------------- #
# Route resolution
# --------------------------------------------------------------------------- #


def resolve_route(
    classification: TaskClassification, catalog: AgentCatalog | None = None
) -> ReviewRoute:
    """Map classified facts to the required specialist set.

    The result is the union of the catalog's selection rules that apply, filtered
    through the catalog's review order so the route is stable. Every rule adds
    review; none removes it, and no rule inspects anything beyond the persisted
    facts and the catalog.

    When the scope could not be read at all, the route falls back to the core team
    and records why, instead of guessing a specialist set.
    """

    active = catalog or current_catalog()

    if classification.unparsed_paths and not classification.areas:
        return ReviewRoute(
            specialists=(),
            reasons=("fallback:scope-paths-unrecognised",),
            fallback_reason=FALLBACK_SCOPE_UNPARSEABLE,
            catalog_digest=active.digest,
        )

    applied: dict[str, list[str]] = {name: [] for name in active.names}
    for entry, rule in active.selection_rules():
        if rule.applies(classification):
            applied[entry.name].append(rule.reason)

    return ReviewRoute(
        specialists=tuple(name for name in active.names if applied[name]),
        reasons=tuple(
            reason for name in active.names for reason in applied[name]
        ),
        catalog_digest=active.digest,
    )


def fallback_route(reason: str = FALLBACK_ROUTE_NOT_RECORDED) -> ReviewRoute:
    """The core route, used when no approved route was ever recorded."""

    return ReviewRoute(
        specialists=(),
        reasons=(f"fallback:{reason}",),
        fallback_reason=reason,
    )


def canonical_route_document(
    classification: TaskClassification, route: ReviewRoute
) -> dict[str, object]:
    """The exact bytes a route digest is computed over."""

    return {
        "factors": classification.to_factors(),
        "route": route.to_document(),
    }


def document_digest(document: dict[str, object]) -> str:
    """SHA-256 over a canonical JSON rendering of a route document."""

    payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def route_digest(
    classification: TaskClassification, route: ReviewRoute
) -> str:
    return document_digest(canonical_route_document(classification, route))


def inspect_changes(
    *,
    route: ReviewRoute,
    changed_paths: Sequence[str],
    approved_component_paths: Sequence[str],
    catalog: AgentCatalog | None = None,
) -> ChangeInspection:
    """Compare what was committed against the route and the approved scope.

    approved_component_paths are the components of the approved scope proposal.
    They are reduced to their top-level areas for containment: a component names
    where the work belongs, and a change elsewhere in the same area is drift that
    reviewers must see, while a change outside every approved area is not covered
    by the approval at all.
    """

    active = catalog or current_catalog()
    changed: list[str] = []
    unparsed: list[str] = []
    for candidate in changed_paths:
        path = normalize_path(str(candidate))
        if path is None:
            unparsed.append(str(candidate))
        else:
            changed.append(path)

    components: list[str] = []
    for candidate in approved_component_paths:
        path = normalize_path(str(candidate))
        if path is not None:
            components.append(path)

    approved_roots = tuple(sorted({path_area(path) for path in components}))
    out_of_scope: list[str] = []
    drift: list[str] = []
    triggers: dict[str, list[str]] = {name: [] for name in active.names}
    reasons: list[str] = []

    for path in changed:
        if approved_roots and path_area(path) not in approved_roots:
            out_of_scope.append(path)
            reasons.append(f"out-of-scope:{path}")
        elif components and not any(
            path == component or path.startswith(f"{component}/")
            for component in components
        ):
            drift.append(path)
            reasons.append(f"component-drift:{path}")
        for entry in active.entries:
            signal = entry.path_signal
            if signal is None or not signal.matches(path):
                continue
            triggers.setdefault(entry.name, []).append(path)
            reasons.append(f"{signal.change_reason}:{path}")

    required: list[str] = []
    for name in active.names:
        if triggers.get(name) and name not in route.specialists:
            required.append(name)
            reasons.append(f"escalation:{name}-review-required")

    if unparsed:
        reasons.extend(f"unparsed-changed-path:{value}" for value in unparsed)

    return ChangeInspection(
        changed_paths=tuple(sorted(changed)),
        approved_roots=approved_roots,
        out_of_scope=tuple(sorted(out_of_scope)),
        component_drift=tuple(sorted(drift)),
        trigger_paths={
            specialist: tuple(sorted(paths))
            for specialist, paths in triggers.items()
        },
        required_additions=tuple(required),
        reasons=tuple(reasons),
    )


def file_boundary_violations(
    *,
    changed_paths: Sequence[str],
    approved_component_paths: Sequence[str],
) -> tuple[str, ...]:
    """Changed files the approved scope does not name, exactly.

    ``inspect_changes`` compares top-level areas, which is deliberate: a change
    elsewhere in the approved area is drift reviewers must see, not something the
    approval fails to cover. That tolerance is wrong for an attempt that has
    already produced a change set the approval did not cover, so this stricter
    rule compares the files themselves against the approved components. A path
    that cannot be read as repository-relative is a violation too: an
    unclassifiable path cannot be proven to be inside the approval.
    """

    components: list[str] = []
    for candidate in approved_component_paths:
        path = normalize_path(str(candidate))
        if path is not None:
            components.append(path)

    violations: list[str] = []
    for candidate in changed_paths:
        path = normalize_path(str(candidate))
        if path is None:
            violations.append(str(candidate))
        elif not any(
            path == component or path.startswith(f"{component}/")
            for component in components
        ):
            violations.append(path)
    return tuple(sorted(violations))


def escalate_route(
    route: ReviewRoute,
    inspection: ChangeInspection,
    catalog: AgentCatalog | None = None,
) -> ReviewRoute:
    """The route required by the change set: the approved route plus additions.

    Idempotent with respect to the route it is given: a specialist the route
    already requires adds neither a specialist nor a reason, so escalating an
    already-escalated route is a no-op.
    """

    active = catalog or current_catalog()
    additions = [
        specialist
        for specialist in inspection.required_additions
        if specialist not in route.specialists
    ]
    if not additions:
        return route
    specialists = [*route.specialists, *additions]
    ordered = tuple(name for name in active.names if name in specialists)
    return ReviewRoute(
        specialists=ordered,
        reasons=tuple(route.reasons) + tuple(
            f"escalated:{specialist}" for specialist in additions
        ),
        fallback_reason=route.fallback_reason,
        catalog_digest=route.catalog_digest or active.digest,
        # The intake record belongs to the approval that froze the route; an
        # escalation adds review after the commit and inherits that history.
        intake=route.intake,
    )


__all__ = [
    "CORE_STAGES",
    "ChangeInspection",
    "COMPLEXITY_LEVELS",
    "FALLBACK_ROUTE_NOT_RECORDED",
    "FALLBACK_SCOPE_UNPARSEABLE",
    "RISK_LEVELS",
    "ReviewRoute",
    "EVIDENCE_MODE_CODE",
    "EVIDENCE_MODE_RENDERED",
    "RENDERED_EVIDENCE_SEGMENTS",
    "RENDERED_EVIDENCE_SUFFIXES",
    "SPECIALISTS",
    "SPECIALIST_FACTOR_KEYS",
    "SPECIALIST_SPECS",
    "SpecialistSpec",
    "SPECIALIST_LABELS",
    "SPECIALIST_PERSONA_SETTINGS",
    "SPECIALIST_ROLES",
    "SPECIALIST_STAGES",
    "TASK_TYPES",
    "TaskClassification",
    "canonical_route_document",
    "classify_requirement",
    "document_digest",
    "escalate_route",
    "fallback_route",
    "file_boundary_violations",
    "inspect_changes",
    "evidence_mode",
    "is_security_sensitive_path",
    "is_ui_path",
    "normalize_path",
    "rendered_evidence_paths",
    "path_area",
    "resolve_route",
    "route_digest",
    "specialist_spec",
    "specialist_specs",
]
