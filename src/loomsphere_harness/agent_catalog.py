"""Trusted, declarative agent catalog.

Stage 4A turns the specialist roster from Python tables into data the runtime loads
from the factory's own checkout. One JSON file per specialist under
`.github/agents/catalog/` declares everything the loop needs to *select*, *bind*
and *execute* that specialist:

    .github/agents/catalog/<name>.json        -> identity, stage, role, tools, skills,
                                                 selection rules, path/text signals,
                                                 prompt profile, evidence policy
    .github/agents/<definition>.agent.md      -> the role's persona and declared tools
    .github/skills/<skill>/SKILL.md           -> the procedures the role may load

services/routing.py reads selection rules and signals from this catalog,
services/implementation_prompts.py reads the prompt profile from it, and
services/implementation.py resolves the stage handler, the persona path, the
authorized skill set and the invocation mode from it. Adding a reviewer is therefore
a data change: a catalog file, an agent definition, a skill, and nothing in the
orchestration.

Three boundaries keep a declarative catalog as trustworthy as the code tables it
replaced:

* The source is the trusted checkout, never an agent worktree. The catalog, the
  agent definitions and the skills are read from configured factory roots, and every
  resolved path is checked to stay inside its root before a byte is read.
* Loading fails closed. A missing, unreadable, malformed, duplicated, or
  self-inconsistent entry raises AgentCatalogError; it is never skipped. A
  specialist whose skill is not declared for its role, whose stage disagrees with its
  name, or whose definition declares different tools fails to load rather than
  running with a capability nobody reviewed.
* The catalog grants nothing. It selects work and narrows what a role may do. It
  cannot approve, satisfy an acceptance criterion, widen a gate, or make a mutating
  role read-only. Review roles must declare read_only: true and may not declare
  edit or execute; the persisted gates stay in application code.

The loaded catalog is identified by a digest over its semantic content. Route
documents record that digest, so a frozen route names the exact catalog that
selected its review team.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from loomsphere_harness.harness_settings import settings
from loomsphere_harness.skills import (
    SKILL_NAME_PATTERN,
    SkillLoadError,
    load_role_skills,
    parse_frontmatter,
    parse_list,
)

#: One file per specialist. The suffix keeps a catalog directory honest: nothing
#: else is read from it.
CATALOG_SUFFIX = ".json"

#: The catalog format this runtime understands. A newer format is refused instead
#: of being half-read.
CATALOG_VERSION = 1

#: Names appear in persisted columns, stage names, and evidence rows, so the grammar
#: is deliberately narrow. specialist_reviews.specialist and factory_jobs.stage are
#: 32 characters wide, so the derived names must fit.
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+){0,2}$")

#: Stage and role names are derived from the specialist name. Deriving them keeps
#: the persisted vocabularies (widened by migration 0016 to pattern checks) aligned
#: with the catalog without a second registry.
STAGE_SUFFIX = "-review"
ROLE_SUFFIX = "-reviewer"

#: The tool vocabulary an agent definition may declare. A catalog entry is the
#: authorization; the definition must declare the same set, so a drifting definition
#: fails to load instead of silently granting or dropping a capability.
TOOL_VOCABULARY = frozenset(
    {"read", "search", "edit", "execute", "agent", "todo", "web"}
)
#: Tools that change the workspace or run code. A review specialist may not hold
#: them: it must never be able to alter the evidence it is judging.
MUTATING_TOOLS = frozenset({"edit", "execute"})

MIN_LABEL_CHARS = 3
MIN_PROMPT_CHARS = 12

CATALOG_MISSING = "catalog-missing"
CATALOG_EMPTY = "catalog-empty"
CATALOG_UNREADABLE = "catalog-unreadable"
CATALOG_INVALID_JSON = "catalog-invalid-json"
CATALOG_VERSION_UNSUPPORTED = "catalog-version-unsupported"
CATALOG_FIELD_MISSING = "catalog-field-missing"
CATALOG_FIELD_UNKNOWN = "catalog-field-unknown"
CATALOG_FIELD_INVALID = "catalog-field-invalid"
CATALOG_NAME_INVALID = "catalog-name-invalid"
CATALOG_NAME_DUPLICATE = "catalog-name-duplicate"
CATALOG_ORDER_DUPLICATE = "catalog-order-duplicate"
CATALOG_STAGE_MISMATCH = "catalog-stage-mismatch"
CATALOG_ROLE_MISMATCH = "catalog-role-mismatch"
CATALOG_DEFINITION_MISSING = "catalog-definition-missing"
CATALOG_DEFINITION_UNSAFE = "catalog-definition-unsafe"
CATALOG_DEFINITION_CONFLICT = "catalog-definition-conflict"
CATALOG_DEFINITION_NAME_MISMATCH = "catalog-definition-name-mismatch"
CATALOG_TOOLS_UNKNOWN = "catalog-tools-unknown"
CATALOG_TOOLS_FORBIDDEN = "catalog-tools-forbidden"
CATALOG_TOOLS_MISMATCH = "catalog-tools-mismatch"
CATALOG_SKILL_INVALID = "catalog-skill-invalid"
CATALOG_SKILL_MISMATCH = "catalog-skill-mismatch"
CATALOG_READ_ONLY_REQUIRED = "catalog-read-only-required"
CATALOG_PROMPT_INVALID = "catalog-prompt-invalid"
CATALOG_SELECTION_INVALID = "catalog-selection-invalid"
CATALOG_SIGNAL_INVALID = "catalog-signal-invalid"
CATALOG_SIGNAL_UNKNOWN = "catalog-signal-unknown"

#: Keys an entry may declare. An unknown key is refused rather than ignored: a
#: misspelled tools list would otherwise silently leave the role with no grant.
ENTRY_KEYS = frozenset(
    {
        "catalog_version",
        "name",
        "order",
        "label",
        "role",
        "stage",
        "definition",
        "definition_name",
        "persona_setting",
        "factor_key",
        "read_only",
        "tools",
        "skills",
        "risk_tier",
        "implied_task_type",
        "implied_task_type_from_paths",
        "reviews_rendered_evidence",
        "path_signal",
        "text_markers",
        "selection",
        "prompt",
    }
)

_PROMPT_KEYS = ("mission", "focus", "focus_rules", "focus_heading", "path_label")
_SIGNAL_KEYS = (
    "regexes",
    "suffixes",
    "segments",
    "areas",
    "description",
    "change_reason",
)
_SELECTION_KEYS = (
    "reason",
    "task_types",
    "risk_levels",
    "complexities",
    "areas",
    "paths",
    "text",
)

RISK_TIERS: tuple[str, ...] = ("low", "medium", "high")


class AgentCatalogError(RuntimeError):
    """The trusted agent catalog could not be read or is not self-consistent."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PathSignal:
    """One kind of path that selects a specialist.

    Every field is a fact about a path: a regular expression, a file suffix, a
    directory segment, or a top-level area. A path matches when any of them matches,
    and a catalog entry may declare only the kinds it needs.
    """

    regexes: tuple[re.Pattern[str], ...] = ()
    suffixes: tuple[str, ...] = ()
    segments: frozenset[str] = frozenset()
    areas: frozenset[str] = frozenset()
    description: str = ""
    #: The recorded reason a changed path added this specialist after the commit.
    change_reason: str = ""

    def matches(self, path: str) -> bool:
        normalized = path.replace("\\", "/")
        if any(pattern.search(normalized) for pattern in self.regexes):
            return True
        lowered = normalized.lower()
        if self.suffixes and lowered.endswith(self.suffixes):
            return True
        if self.segments and (set(lowered.split("/")) & set(self.segments)):
            return True
        if self.areas and normalized.split("/")[0] in self.areas:
            return True
        return False

    def to_document(self) -> dict[str, object]:
        return {
            "regexes": [pattern.pattern for pattern in self.regexes],
            "suffixes": list(self.suffixes),
            "segments": sorted(self.segments),
            "areas": sorted(self.areas),
            "description": self.description,
            "change_reason": self.change_reason,
        }


@dataclass(frozen=True)
class SelectionRule:
    """One declarative reason to add one specialist to a route.

    Every declared condition must hold (AND); conditions that are not declared do
    not constrain anything. paths and text are self-referential: they ask whether
    this specialist's own path signal or text markers matched the scope.
    """

    reason: str
    task_types: tuple[str, ...] = ()
    risk_levels: tuple[str, ...] = ()
    complexities: tuple[str, ...] = ()
    areas: tuple[str, ...] = ()
    paths: bool = False
    text: bool = False
    #: Filled in by the entry that owns the rule, so paths/text refer to the
    #: specialist the rule selects. It is not a catalog field.
    specialist_name: str = ""

    def applies(self, classification) -> bool:
        if self.task_types and classification.task_type not in self.task_types:
            return False
        if self.risk_levels and classification.risk_level not in self.risk_levels:
            return False
        if self.complexities and classification.complexity not in self.complexities:
            return False
        if self.areas and not (set(self.areas) & set(classification.areas)):
            return False
        if self.paths and not classification.has_path_matches(self.specialist_name):
            return False
        if self.text and not classification.has_text_matches(self.specialist_name):
            return False
        return True

    def to_document(self) -> dict[str, object]:
        return {
            "reason": self.reason,
            "task_types": list(self.task_types),
            "risk_levels": list(self.risk_levels),
            "complexities": list(self.complexities),
            "areas": list(self.areas),
            "paths": self.paths,
            "text": self.text,
        }


@dataclass(frozen=True)
class PromptProfile:
    """The words a specialist contributes to its own prompt.

    The prompt structure is shared by every specialist; the profile supplies the
    label, mission, focus list, severity rule, and the heading used to justify the
    review.
    """

    mission: str
    focus: str
    focus_rules: str
    focus_heading: str
    path_label: str

    def to_document(self) -> dict[str, object]:
        return {
            "mission": self.mission,
            "focus": self.focus,
            "focus_rules": self.focus_rules,
            "focus_heading": self.focus_heading,
            "path_label": self.path_label,
        }


@dataclass(frozen=True)
class CatalogEntry:
    """One specialist, as declared by one trusted catalog file."""

    name: str
    order: int
    label: str
    role: str
    stage: str
    definition_file: str
    definition_name: str
    definition_path: Path
    source: str
    persona_setting: str
    factor_key: str
    read_only: bool
    tools: tuple[str, ...]
    skills: tuple[str, ...]
    risk_tier: str | None
    implied_task_type: str | None
    #: Whether a matched scope path, and not only matched text, implies the task
    #: type. A user-facing scope is a fact about the work, so a UI path selects the
    #: UI task type; a sensitive path raises risk and routes review without
    #: relabelling the requirement, so the security entry leaves this false.
    implied_task_type_from_paths: bool
    reviews_rendered_evidence: bool
    path_signal: PathSignal | None
    text_markers: tuple[str, ...]
    selection: tuple[SelectionRule, ...]
    prompt: PromptProfile

    @property
    def mutating_tools(self) -> tuple[str, ...]:
        return tuple(tool for tool in self.tools if tool in MUTATING_TOOLS)

    def evidence(self, *, catalog_digest: str) -> dict[str, object]:
        """What an execution records about the catalog entry that authorized it."""

        return {
            "name": self.name,
            "order": self.order,
            "role": self.role,
            "stage": self.stage,
            "catalog_digest": catalog_digest,
            "definition": self.definition_file,
            "tools": list(self.tools),
            "skills": list(self.skills),
            "read_only": self.read_only,
            "reviews_rendered_evidence": self.reviews_rendered_evidence,
        }

    def to_document(self) -> dict[str, object]:
        """Canonical semantic content, used for the catalog digest and reporting."""

        return {
            "name": self.name,
            "order": self.order,
            "label": self.label,
            "role": self.role,
            "stage": self.stage,
            "definition": self.definition_file,
            "definition_name": self.definition_name,
            "factor_key": self.factor_key,
            "read_only": self.read_only,
            "tools": list(self.tools),
            "skills": list(self.skills),
            "risk_tier": self.risk_tier,
            "implied_task_type": self.implied_task_type,
            "implied_task_type_from_paths": self.implied_task_type_from_paths,
            "reviews_rendered_evidence": self.reviews_rendered_evidence,
            "path_signal": self.path_signal.to_document() if self.path_signal else None,
            "text_markers": list(self.text_markers),
            "selection": [rule.to_document() for rule in self.selection],
            "prompt": self.prompt.to_document(),
        }


@dataclass(frozen=True)
class AgentCatalog:
    """The specialists the factory is allowed to select and execute."""

    entries: tuple[CatalogEntry, ...]
    digest: str
    root: str
    definitions_root: str
    skills_root: str

    def __iter__(self):
        return iter(self.entries)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(entry.name for entry in self.entries)

    @property
    def stages(self) -> tuple[str, ...]:
        return tuple(entry.stage for entry in self.entries)

    @property
    def roles(self) -> tuple[str, ...]:
        return tuple(entry.role for entry in self.entries)

    def by_name(self, name: str) -> CatalogEntry:
        entry = self.by_name_or_none(name)
        if entry is None:
            raise KeyError(f"unknown specialist: {name}")
        return entry

    def by_name_or_none(self, name: str) -> CatalogEntry | None:
        for entry in self.entries:
            if entry.name == name:
                return entry
        return None

    def by_stage(self, stage: str) -> CatalogEntry | None:
        for entry in self.entries:
            if entry.stage == stage:
                return entry
        return None

    def by_role(self, role: str) -> CatalogEntry | None:
        for entry in self.entries:
            if entry.role == role:
                return entry
        return None

    def selection_rules(self) -> tuple[tuple[CatalogEntry, SelectionRule], ...]:
        return tuple(
            (entry, rule) for entry in self.entries for rule in entry.selection
        )

    def signal_names(self) -> tuple[str, ...]:
        return tuple(entry.name for entry in self.entries if entry.path_signal)


def _canonical(document: Mapping[str, Any]) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


def catalog_digest(entries: Sequence[CatalogEntry]) -> str:
    """SHA-256 over the semantic content of the catalog, independent of formatting."""

    document = {
        "catalog_version": CATALOG_VERSION,
        "specialists": [entry.to_document() for entry in entries],
    }
    return hashlib.sha256(_canonical(document)).hexdigest()


# --------------------------------------------------------------------------- #
# Reading one entry
# --------------------------------------------------------------------------- #


def _require(mapping: Mapping[str, Any], key: str, source: str) -> Any:
    if key not in mapping:
        raise AgentCatalogError(
            CATALOG_FIELD_MISSING, f"{source} does not declare '{key}'."
        )
    return mapping[key]


def _require_text(
    mapping: Mapping[str, Any], key: str, source: str, *, minimum: int = 1
) -> str:
    value = _require(mapping, key, source)
    if not isinstance(value, str) or len(value.strip()) < minimum:
        raise AgentCatalogError(
            CATALOG_FIELD_INVALID,
            f"{source}: '{key}' must be a string of at least {minimum} characters.",
        )
    return value.strip()


def _optional_text(mapping: Mapping[str, Any], key: str, source: str) -> str:
    value = mapping.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise AgentCatalogError(
            CATALOG_FIELD_INVALID, f"{source}: '{key}' must be a string."
        )
    return value.strip()


def _string_list(
    mapping: Mapping[str, Any],
    key: str,
    source: str,
    *,
    required: bool = False,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if key not in mapping:
        if required:
            raise AgentCatalogError(
                CATALOG_FIELD_MISSING, f"{source} does not declare '{key}'."
            )
        return ()
    value = mapping[key]
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise AgentCatalogError(
            CATALOG_FIELD_INVALID,
            f"{source}: '{key}' must be a list of non-empty strings.",
        )
    items = tuple(item.strip() for item in value)
    if not items and not allow_empty:
        raise AgentCatalogError(
            CATALOG_FIELD_INVALID, f"{source}: '{key}' must not be empty."
        )
    if len(set(items)) != len(items):
        raise AgentCatalogError(
            CATALOG_FIELD_INVALID, f"{source}: '{key}' declares a duplicate entry."
        )
    return items


def _reject_unknown_keys(
    mapping: Mapping[str, Any], allowed: Iterable[str], source: str, *, label: str
) -> None:
    unknown = sorted(set(mapping) - set(allowed))
    if unknown:
        raise AgentCatalogError(
            CATALOG_FIELD_UNKNOWN,
            f"{source}: unsupported {label} key(s): {', '.join(unknown)}.",
        )


def _load_path_signal(document: Any, source: str) -> PathSignal | None:
    if document is None:
        return None
    if not isinstance(document, dict):
        raise AgentCatalogError(
            CATALOG_SIGNAL_INVALID, f"{source}: 'path_signal' must be an object."
        )
    _reject_unknown_keys(document, _SIGNAL_KEYS, source, label="path_signal")
    regexes: list[re.Pattern[str]] = []
    for pattern in _string_list(document, "regexes", source, allow_empty=True):
        try:
            regexes.append(re.compile(pattern))
        except re.error as error:
            raise AgentCatalogError(
                CATALOG_SIGNAL_INVALID,
                f"{source}: path_signal regex is not valid: {error}.",
            ) from error
    signal = PathSignal(
        regexes=tuple(regexes),
        suffixes=tuple(
            item.lower()
            for item in _string_list(document, "suffixes", source, allow_empty=True)
        ),
        segments=frozenset(
            item.lower()
            for item in _string_list(document, "segments", source, allow_empty=True)
        ),
        areas=frozenset(_string_list(document, "areas", source, allow_empty=True)),
        description=_optional_text(document, "description", source),
        change_reason=_require_text(document, "change_reason", source, minimum=3),
    )
    if not (signal.regexes or signal.suffixes or signal.segments or signal.areas):
        raise AgentCatalogError(
            CATALOG_SIGNAL_INVALID,
            f"{source}: 'path_signal' declares no matching fact.",
        )
    return signal


def _load_selection(
    document: Any, source: str, entry_name: str
) -> tuple[SelectionRule, ...]:
    if not isinstance(document, list) or not document:
        raise AgentCatalogError(
            CATALOG_SELECTION_INVALID,
            f"{source}: 'selection' must be a non-empty list of rules.",
        )
    rules: list[SelectionRule] = []
    for index, raw in enumerate(document):
        if not isinstance(raw, dict):
            raise AgentCatalogError(
                CATALOG_SELECTION_INVALID,
                f"{source}: selection[{index}] must be an object.",
            )
        _reject_unknown_keys(raw, _SELECTION_KEYS, source, label="selection")
        reason = _require_text(raw, "reason", source, minimum=3)
        for flag in ("paths", "text"):
            if not isinstance(raw.get(flag, False), bool):
                raise AgentCatalogError(
                    CATALOG_SELECTION_INVALID,
                    f"{source}: selection '{reason}' declares a non-boolean '{flag}'.",
                )
        risk_levels = _string_list(raw, "risk_levels", source, allow_empty=True)
        for level in risk_levels:
            if level not in RISK_TIERS:
                raise AgentCatalogError(
                    CATALOG_SELECTION_INVALID,
                    f"{source}: selection '{reason}' names unknown risk level "
                    f"'{level}'.",
                )
        rule = SelectionRule(
            reason=reason,
            task_types=_string_list(raw, "task_types", source, allow_empty=True),
            risk_levels=risk_levels,
            complexities=_string_list(raw, "complexities", source, allow_empty=True),
            areas=_string_list(raw, "areas", source, allow_empty=True),
            paths=bool(raw.get("paths", False)),
            text=bool(raw.get("text", False)),
            specialist_name=entry_name,
        )
        if not (
            rule.task_types
            or rule.risk_levels
            or rule.complexities
            or rule.areas
            or rule.paths
            or rule.text
        ):
            raise AgentCatalogError(
                CATALOG_SELECTION_INVALID,
                f"{source}: selection '{reason}' declares no condition.",
            )
        rules.append(rule)
    return tuple(rules)


def _load_prompt(document: Any, source: str) -> PromptProfile:
    if not isinstance(document, dict):
        raise AgentCatalogError(
            CATALOG_PROMPT_INVALID, f"{source}: 'prompt' must be an object."
        )
    _reject_unknown_keys(document, _PROMPT_KEYS, source, label="prompt")
    return PromptProfile(
        mission=_require_text(document, "mission", source, minimum=MIN_PROMPT_CHARS),
        focus=_require_text(document, "focus", source, minimum=MIN_PROMPT_CHARS),
        focus_rules=_require_text(
            document, "focus_rules", source, minimum=MIN_PROMPT_CHARS
        ),
        focus_heading=_require_text(
            document, "focus_heading", source, minimum=MIN_PROMPT_CHARS
        ),
        path_label=_require_text(
            document, "path_label", source, minimum=MIN_PROMPT_CHARS
        ),
    )


def _resolve_definition(definitions_root: Path, filename: str, source: str) -> Path:
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise AgentCatalogError(
            CATALOG_DEFINITION_UNSAFE,
            f"{source}: 'definition' must be a bare file name in the agent "
            "definitions root.",
        )
    root_resolved = definitions_root.expanduser().resolve()
    candidate = root_resolved / filename
    try:
        resolved = candidate.resolve(strict=False)
    except OSError as error:  # pragma: no cover - defensive
        raise AgentCatalogError(
            CATALOG_DEFINITION_UNSAFE,
            f"{source}: definition could not be resolved: {error}.",
        ) from error
    if resolved == root_resolved or not resolved.is_relative_to(root_resolved):
        raise AgentCatalogError(
            CATALOG_DEFINITION_UNSAFE,
            f"{source}: definition resolves outside the agent definitions root.",
        )
    if not resolved.is_file():
        raise AgentCatalogError(
            CATALOG_DEFINITION_MISSING,
            f"{source}: agent definition '{filename}' is not available.",
        )
    return resolved


def _definition_frontmatter(path: Path) -> Mapping[str, str]:
    return parse_frontmatter(path.read_text(encoding="utf-8")).values


def _settings_value(name: str) -> str | None:
    if not name:
        return None
    return getattr(settings, name, None)


def _load_entry(
    path: Path,
    *,
    definitions_root: Path,
    skills_root: Path,
    root: Path,
) -> CatalogEntry:
    source = f"{root.as_posix()}/{path.name}"
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise AgentCatalogError(
            CATALOG_UNREADABLE, f"{source} could not be read: {error}"
        ) from error
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AgentCatalogError(
            CATALOG_INVALID_JSON, f"{source} is not valid JSON: {error}"
        ) from error
    if not isinstance(document, dict):
        raise AgentCatalogError(
            CATALOG_INVALID_JSON, f"{source} must contain one JSON object."
        )
    _reject_unknown_keys(document, ENTRY_KEYS, source, label="catalog entry")

    version = document.get("catalog_version", CATALOG_VERSION)
    if version != CATALOG_VERSION:
        raise AgentCatalogError(
            CATALOG_VERSION_UNSUPPORTED,
            f"{source} declares catalog_version {version!r}; this runtime reads "
            f"{CATALOG_VERSION}.",
        )

    name = _require_text(document, "name", source)
    if not NAME_PATTERN.fullmatch(name):
        raise AgentCatalogError(
            CATALOG_NAME_INVALID,
            f"{source}: specialist name '{name}' is not lower-case kebab-case.",
        )
    if path.stem != name:
        raise AgentCatalogError(
            CATALOG_NAME_INVALID,
            f"{source}: file name must match the declared specialist name "
            f"'{name}.json'.",
        )

    order = _require(document, "order", source)
    if not isinstance(order, int) or isinstance(order, bool) or order < 0:
        raise AgentCatalogError(
            CATALOG_FIELD_INVALID, f"{source}: 'order' must be a non-negative integer."
        )

    label = _require_text(document, "label", source, minimum=MIN_LABEL_CHARS)
    role = _require_text(document, "role", source)
    stage = _require_text(document, "stage", source)
    if role != f"{name}{ROLE_SUFFIX}":
        raise AgentCatalogError(
            CATALOG_ROLE_MISMATCH,
            f"{source}: role '{role}' must be '{name}{ROLE_SUFFIX}'.",
        )
    if stage != f"{name}{STAGE_SUFFIX}":
        raise AgentCatalogError(
            CATALOG_STAGE_MISMATCH,
            f"{source}: stage '{stage}' must be '{name}{STAGE_SUFFIX}'.",
        )

    definition_file = _require_text(document, "definition", source)
    definition_name = _require_text(document, "definition_name", source)
    persona_setting = _optional_text(document, "persona_setting", source)
    factor_key = _require_text(document, "factor_key", source)
    if not re.fullmatch(r"[a-z][a-z0-9_]*", factor_key):
        raise AgentCatalogError(
            CATALOG_FIELD_INVALID, f"{source}: 'factor_key' must be snake_case."
        )

    read_only = _require(document, "read_only", source)
    if read_only is not True:
        raise AgentCatalogError(
            CATALOG_READ_ONLY_REQUIRED,
            f"{source}: a review specialist must declare read_only: true.",
        )

    tools = _string_list(document, "tools", source, required=True)
    unknown_tools = sorted(set(tools) - TOOL_VOCABULARY)
    if unknown_tools:
        raise AgentCatalogError(
            CATALOG_TOOLS_UNKNOWN,
            f"{source}: unsupported tool(s): {', '.join(unknown_tools)}.",
        )
    if set(tools) & MUTATING_TOOLS:
        raise AgentCatalogError(
            CATALOG_TOOLS_FORBIDDEN,
            f"{source}: a review specialist must not hold mutating tools: "
            f"{', '.join(sorted(set(tools) & MUTATING_TOOLS))}.",
        )

    skills = _string_list(document, "skills", source, required=True)
    for skill in skills:
        if not SKILL_NAME_PATTERN.fullmatch(skill):
            raise AgentCatalogError(
                CATALOG_SKILL_INVALID,
                f"{source}: skill name '{skill}' is not lower-case kebab-case.",
            )

    risk_tier = document.get("risk_tier")
    if risk_tier is not None and risk_tier not in RISK_TIERS:
        raise AgentCatalogError(
            CATALOG_FIELD_INVALID,
            f"{source}: 'risk_tier' must be one of {', '.join(RISK_TIERS)}.",
        )
    implied_task_type = document.get("implied_task_type")
    if implied_task_type is not None and (
        not isinstance(implied_task_type, str) or not implied_task_type
    ):
        raise AgentCatalogError(
            CATALOG_FIELD_INVALID, f"{source}: 'implied_task_type' must be a string."
        )
    reviews_rendered = document.get("reviews_rendered_evidence", False)
    if not isinstance(reviews_rendered, bool):
        raise AgentCatalogError(
            CATALOG_FIELD_INVALID,
            f"{source}: 'reviews_rendered_evidence' must be a boolean.",
        )
    implied_from_paths = document.get("implied_task_type_from_paths", False)
    if not isinstance(implied_from_paths, bool):
        raise AgentCatalogError(
            CATALOG_FIELD_INVALID,
            f"{source}: 'implied_task_type_from_paths' must be a boolean.",
        )
    if implied_from_paths and not implied_task_type:
        raise AgentCatalogError(
            CATALOG_FIELD_INVALID,
            f"{source}: 'implied_task_type_from_paths' requires "
            "'implied_task_type'.",
        )

    definition_path = _resolve_definition(definitions_root, definition_file, source)
    declared = _definition_frontmatter(definition_path)
    declared_name = (declared.get("name") or "").strip().strip("'\"").strip()
    if declared_name != definition_name:
        raise AgentCatalogError(
            CATALOG_DEFINITION_NAME_MISMATCH,
            f"{source}: agent definition declares the name '{declared_name}', but the "
            f"catalog expects '{definition_name}'.",
        )

    if persona_setting:
        configured = _settings_value(persona_setting)
        if not configured:
            raise AgentCatalogError(
                CATALOG_FIELD_INVALID,
                f"{source}: 'persona_setting' names an unknown setting "
                f"'{persona_setting}'.",
            )
        if Path(configured).name != definition_file:
            raise AgentCatalogError(
                CATALOG_DEFINITION_CONFLICT,
                f"{source}: setting {persona_setting} points at "
                f"'{Path(configured).name}', but the catalog declares "
                f"'{definition_file}'.",
            )

    raw_tools = declared.get("tools")
    declared_tools = parse_list(raw_tools) if raw_tools is not None else ()
    if tuple(declared_tools) != tools:
        raise AgentCatalogError(
            CATALOG_TOOLS_MISMATCH,
            f"{source}: tools {list(tools)} disagree with the agent definition's "
            f"{list(declared_tools)}.",
        )

    try:
        loaded = load_role_skills(
            role=role, definition_path=definition_path, skills_root=skills_root
        )
    except SkillLoadError as error:
        raise AgentCatalogError(
            CATALOG_SKILL_INVALID, f"{source}: {error.code}: {error.message}"
        ) from error
    loaded_names = tuple(entry.name for entry in loaded)
    if loaded_names != skills:
        raise AgentCatalogError(
            CATALOG_SKILL_MISMATCH,
            f"{source}: skills {list(skills)} disagree with the agent definition's "
            f"{list(loaded_names)}.",
        )

    path_signal = _load_path_signal(document.get("path_signal"), source)
    text_markers = _string_list(document, "text_markers", source, allow_empty=True)
    selection = _load_selection(document.get("selection"), source, name)
    for rule in selection:
        if rule.paths and path_signal is None:
            raise AgentCatalogError(
                CATALOG_SIGNAL_UNKNOWN,
                f"{source}: selection '{rule.reason}' requires a path signal, but the "
                "entry declares none.",
            )
        if rule.text and not text_markers:
            raise AgentCatalogError(
                CATALOG_SIGNAL_UNKNOWN,
                f"{source}: selection '{rule.reason}' requires text markers, but the "
                "entry declares none.",
            )
    prompt = _load_prompt(document.get("prompt"), source)

    return CatalogEntry(
        name=name,
        order=order,
        label=label,
        role=role,
        stage=stage,
        definition_file=definition_file,
        definition_name=definition_name,
        definition_path=definition_path,
        source=source,
        persona_setting=persona_setting,
        factor_key=factor_key,
        read_only=True,
        tools=tools,
        skills=skills,
        risk_tier=risk_tier,
        implied_task_type=implied_task_type,
        implied_task_type_from_paths=implied_from_paths,
        reviews_rendered_evidence=reviews_rendered,
        path_signal=path_signal,
        text_markers=text_markers,
        selection=selection,
        prompt=prompt,
    )


def load_catalog(
    catalog_root: str | Path,
    definitions_root: str | Path,
    skills_root: str | Path,
) -> AgentCatalog:
    """Load every catalog entry, refusing anything that is not self-consistent."""

    root = Path(catalog_root).expanduser()
    if not root.is_dir():
        raise AgentCatalogError(
            CATALOG_MISSING,
            "The trusted agent catalog directory is not available in this deployment.",
        )
    files = sorted(
        path
        for path in root.iterdir()
        if path.is_file() and path.suffix == CATALOG_SUFFIX
    )
    if not files:
        raise AgentCatalogError(
            CATALOG_EMPTY, "The trusted agent catalog declares no specialists."
        )

    entries = tuple(
        _load_entry(
            path,
            definitions_root=Path(definitions_root),
            skills_root=Path(skills_root),
            root=root,
        )
        for path in files
    )
    ordered = tuple(sorted(entries, key=lambda entry: (entry.order, entry.name)))

    seen_names: dict[str, str] = {}
    seen_orders: dict[int, str] = {}
    seen_stages: dict[str, str] = {}
    seen_roles: dict[str, str] = {}
    for entry in ordered:
        for value, seen, label, code in (
            (entry.name, seen_names, "specialist name", CATALOG_NAME_DUPLICATE),
            (entry.order, seen_orders, "order", CATALOG_ORDER_DUPLICATE),
            (entry.stage, seen_stages, "stage", CATALOG_NAME_DUPLICATE),
            (entry.role, seen_roles, "role", CATALOG_NAME_DUPLICATE),
        ):
            if value in seen:
                raise AgentCatalogError(
                    code,
                    f"The agent catalog declares {label} '{value}' twice: "
                    f"{seen[value]} and {entry.source}.",
                )
            seen[value] = entry.source

    return AgentCatalog(
        entries=ordered,
        digest=catalog_digest(ordered),
        root=str(root),
        definitions_root=str(Path(definitions_root)),
        skills_root=str(Path(skills_root)),
    )


def _catalog_signature(catalog_root: Path) -> tuple[tuple[str, int, int], ...]:
    try:
        return tuple(
            sorted(
                (path.name, path.stat().st_mtime_ns, path.stat().st_size)
                for path in catalog_root.iterdir()
                if path.is_file() and path.suffix == CATALOG_SUFFIX
            )
        )
    except OSError:  # pragma: no cover - a missing root is reported by the loader
        return ()


_CATALOG_CACHE: dict[
    tuple[str, str, str, tuple[tuple[str, int, int], ...]], AgentCatalog
] = {}


def current_catalog() -> AgentCatalog:
    """The catalog this process is configured with, re-read when a file changes.

    The catalog is configuration, not state: a route resolved in one request must
    see the same catalog as the stage that executes it. The cache is keyed by the
    configured roots and the catalog files' identity, so editing an entry is picked
    up on the next decision instead of requiring a restart.
    """

    catalog_root = Path(settings.AGENT_CATALOG_ROOT)
    key = (
        settings.AGENT_CATALOG_ROOT,
        settings.AGENT_DEFINITIONS_ROOT,
        settings.SKILLS_ROOT,
        _catalog_signature(catalog_root),
    )
    cached = _CATALOG_CACHE.get(key)
    if cached is not None:
        return cached
    catalog = load_catalog(
        settings.AGENT_CATALOG_ROOT,
        settings.AGENT_DEFINITIONS_ROOT,
        settings.SKILLS_ROOT,
    )
    if len(_CATALOG_CACHE) > 8:  # pragma: no cover - small map, bounded by design
        _CATALOG_CACHE.clear()
    _CATALOG_CACHE[key] = catalog
    return catalog


def reset_catalog_cache() -> None:
    """Drop the memoized catalog. Tests use it when they rewrite a catalog file."""

    _CATALOG_CACHE.clear()


# --------------------------------------------------------------------------- #
# Convenience accessors used by the runtime
# --------------------------------------------------------------------------- #


def specialists(catalog: AgentCatalog | None = None) -> tuple[str, ...]:
    return (catalog or current_catalog()).names


def specialist_entry(name: str, catalog: AgentCatalog | None = None) -> CatalogEntry:
    return (catalog or current_catalog()).by_name(name)


def specialist_for_stage(
    stage: str, catalog: AgentCatalog | None = None
) -> CatalogEntry | None:
    return (catalog or current_catalog()).by_stage(stage)


def specialist_for_role(
    role: str, catalog: AgentCatalog | None = None
) -> CatalogEntry | None:
    return (catalog or current_catalog()).by_role(role)


def stage_for_specialist(name: str, catalog: AgentCatalog | None = None) -> str:
    return specialist_entry(name, catalog).stage


def role_for_specialist(name: str, catalog: AgentCatalog | None = None) -> str:
    return specialist_entry(name, catalog).role


def definition_path(entry: CatalogEntry) -> Path:
    """The persona file a catalog entry executes with.

    persona_setting is an explicit deployment override: when it is declared the
    configured path is used, and the catalog has already checked that it names the
    same definition file.
    """

    if entry.persona_setting:
        configured = _settings_value(entry.persona_setting)
        if configured:
            return Path(configured)
    return entry.definition_path


def verify_definition(entry: CatalogEntry) -> Path:
    """Re-check the definition an entry will execute with, at execution time.

    The catalog is validated when it loads, but persona_setting may point the
    execution at a deployment override. This check runs on the file that is actually
    read, so an override that declares a different name, tool list, or skill list
    fails the stage closed instead of running with an unreviewed grant.
    """

    path = definition_path(entry)
    declared = _definition_frontmatter(path)
    declared_name = (declared.get("name") or "").strip().strip("'\"").strip()
    if declared_name != entry.definition_name:
        raise AgentCatalogError(
            CATALOG_DEFINITION_NAME_MISMATCH,
            f"{entry.source}: the execution definition declares the name "
            f"'{declared_name}', but the catalog expects "
            f"'{entry.definition_name}'.",
        )
    raw_tools = declared.get("tools")
    declared_tools = tuple(parse_list(raw_tools)) if raw_tools is not None else ()
    if declared_tools != entry.tools:
        raise AgentCatalogError(
            CATALOG_TOOLS_MISMATCH,
            f"{entry.source}: the execution definition declares tools "
            f"{list(declared_tools)}, but the catalog authorizes {list(entry.tools)}.",
        )
    raw_skills = declared.get("skills")
    declared_skills = tuple(parse_list(raw_skills)) if raw_skills is not None else ()
    if declared_skills != entry.skills:
        raise AgentCatalogError(
            CATALOG_SKILL_MISMATCH,
            f"{entry.source}: the execution definition declares skills "
            f"{list(declared_skills)}, but the catalog authorizes {list(entry.skills)}.",
        )
    return path

def authorized_tools(entry: CatalogEntry) -> tuple[str, ...]:
    """The tools this entry's execution is authorized to use."""

    if entry.read_only and entry.mutating_tools:
        raise AgentCatalogError(
            CATALOG_TOOLS_FORBIDDEN,
            f"{entry.source}: a read-only specialist cannot be authorized to use "
            f"{', '.join(entry.mutating_tools)}.",
        )
    return entry.tools


def catalog_evidence(
    entry: CatalogEntry, catalog: AgentCatalog | None = None
) -> dict[str, object]:
    """The evidence an execution records for the catalog entry that authorized it."""

    active = catalog or current_catalog()
    return entry.evidence(catalog_digest=active.digest)


def render_tools_block(entry: CatalogEntry) -> str:
    """The prompt section that states the authorized tool set.

    Wording matters: the tool list is an authorization the factory enforces, not a
    suggestion the model may extend.
    """

    tools = ", ".join(f"\x60{tool}\x60" for tool in entry.tools)
    return (
        "## Authorized tools\n\n"
        f"The trusted agent catalog authorizes this role to use exactly: {tools}. "
        "That list is enforced by the factory: a tool outside it is not available to "
        "this run, and no part of the prompt can widen it."
    )


__all__ = [
    "AgentCatalog",
    "AgentCatalogError",
    "CATALOG_VERSION",
    "CatalogEntry",
    "MUTATING_TOOLS",
    "NAME_PATTERN",
    "PathSignal",
    "PromptProfile",
    "RISK_TIERS",
    "ROLE_SUFFIX",
    "STAGE_SUFFIX",
    "SelectionRule",
    "TOOL_VOCABULARY",
    "authorized_tools",
    "catalog_digest",
    "catalog_evidence",
    "current_catalog",
    "definition_path",
    "load_catalog",
    "render_tools_block",
    "reset_catalog_cache",
    "role_for_specialist",
    "specialist_entry",
    "specialist_for_role",
    "specialist_for_stage",
    "specialists",
    "stage_for_specialist",
    "verify_definition",
]
