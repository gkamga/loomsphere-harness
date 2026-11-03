#!/usr/bin/env python3
"""Read-only validation of LoomSphere agent, skill, and execution-contract files.

This module is a static checker. It reads repository files and reports findings.
It does not write files, call the network, start services, or transfer work
between agents. A passing run means the configuration is internally consistent;
it is not evidence that any test suite or CI job actually ran.

Exit codes:
    0  all checks passed
    1  one or more validation errors
    2  invalid invocation or unreadable inputs
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote

import yaml
from jsonschema import Draft202012Validator, FormatChecker

#: The fixed executors. Intake triages a request before the Architect; it is a core
#: role, not a review specialist, so it is listed here rather than in the catalog.
#: Specialist reviewers are declared by the trusted agent catalog under
#: .github/agents/catalog/ and are *derived* by this validator, so a registered
#: reviewer is validated without an edit here.
CORE_AGENT_ROLES = (
    "architect",
    "intake",
    "builder",
    "qa",
    "reviewer",
)
CORE_AGENT_FILES = {
    "architect": "loomsphere-architect.agent.md",
    "intake": "loomsphere-intake.agent.md",
    "builder": "loomsphere-builder.agent.md",
    "qa": "loomsphere-qa.agent.md",
    "reviewer": "loomsphere-reviewer.agent.md",
}
CORE_AGENT_NAMES = {
    "architect": "LoomSphere Architect",
    "intake": "LoomSphere Intake",
    "builder": "LoomSphere Builder",
    "qa": "LoomSphere QA",
    "reviewer": "LoomSphere Reviewer",
}

#: The catalog the runtime loads. One JSON file per review specialist.
CATALOG_DIR = ('.github', 'agents', 'catalog')
CATALOG_SUFFIX = '.json'
CATALOG_SCHEMA_PATH = '.github/contracts/agent-catalog.schema.json'
CATALOG_STAGE_SUFFIX = '-review'
CATALOG_ROLE_SUFFIX = '-reviewer'
CATALOG_NAME_PATTERN = re.compile(r'^[a-z][a-z0-9]*(?:-[a-z0-9]+){0,2}$')

ALLOWED_AGENT_TOOLS = {'read', 'search', 'edit', 'execute', 'agent', 'todo', 'web'}
# A specialist reviewer inspects a recorded change set; it must never hold a tool
# that could alter the evidence it is judging.
CORE_READ_ONLY_ROLES = {'architect', 'intake', 'reviewer'}
READ_ONLY_FORBIDDEN_TOOLS = {'edit', 'execute'}
ALLOWED_AGENT_KEYS = {'name', 'description', 'tools', 'skills'}
ALLOWED_SKILL_KEYS = {
    'name',
    'description',
    'roles',
    'stages',
    'required',
    'task_types',
    'areas',
    'implications',
    'risk_levels',
    'requires_tools',
    'requires_skills',
}

# The selection metadata a skill may declare. These vocabularies mirror
# backend/app/services/skills.py, so the validator and the loader cannot disagree
# about which rule a deployment is allowed to rely on.
FACTORY_SKILL_STAGES = ('intake', 'architecture', 'implementation', 'validation', 'review')
SKILL_TASK_TYPES = ('feature', 'bugfix', 'refactor', 'documentation', 'security', 'ui', 'database', 'other')
SKILL_AREAS = ('backend', 'frontend', 'database', 'infrastructure', 'documentation', 'tests', 'shared', 'unknown')
SKILL_IMPLICATIONS = ('security', 'ux_ui', 'database', 'architecture')
SKILL_RISK_LEVELS = ('low', 'medium', 'high')
SKILL_TOOLS = ('read', 'search', 'edit', 'execute', 'todo')

# Lifecycle hooks (Stage 4C). These mirror app/schemas/lifecycle_hooks.py, so the
# validator and the runtime cannot disagree about which event, capability, or check
# a deployment may rely on.
HOOK_SCHEMA_VERSION = 1
HOOK_EVENT_TYPES = (
    'before-agent-invocation',
    'after-agent-completion',
    'before-review-progression',
    'before-delivery',
)
HOOK_CAPABILITIES = ('read-task', 'read-run', 'read-route', 'read-changes', 'read-evidence')
HOOK_MODES = ('observer', 'blocking')
HOOK_CHECK_CAPABILITIES = {
    'note': (),
    'task-status': ('read-task',),
    'forbidden-changed-paths': ('read-changes',),
    'required-evidence': ('read-evidence',),
    'required-specialist-verdict': ('read-route', 'read-evidence'),
}
HOOK_EVIDENCE_KINDS = (
    'implementation',
    'validation',
    'qa',
    'review',
    'specialist-reviews',
    'acceptance-audit',
    'skill-selection',
)
ALLOWED_HOOK_KEYS = {
    'hook_version',
    'name',
    'version',
    'label',
    'description',
    'events',
    'priority',
    'mode',
    'capabilities',
    'checks',
}
ALLOWED_HOOK_CHECK_KEYS = {
    'kind',
    'reason',
    'allowed_statuses',
    'forbidden_statuses',
    'path_patterns',
    'evidence',
    'specialists',
    'text',
}
HOOK_NAME_PATTERN = re.compile(r'^[a-z][a-z0-9]*(?:-[a-z0-9]+){0,7}$')
HOOK_VERSION_PATTERN = re.compile(r'^[0-9]+(?:\.[0-9]+){0,2}$')
MAX_HOOKS_PER_EVENT = 32
MAX_CHECKS_PER_HOOK = 16

#: The stage each fixed factory role executes in. Specialist stages come from the
#: catalog, which is the same source the runtime uses.
FIXED_ROLE_STAGES = {
    'intake': 'intake',
    'architect': 'architecture',
    'builder': 'implementation',
    'qa': 'validation',
    'reviewer': 'review',
}

# The runtime refuses any other shape, so the validator refuses it first rather
# than letting a deployment discover it when an agent run starts.
SKILL_NAME_PATTERN = re.compile(r'^[a-z][a-z0-9]*(?:-[a-z0-9]+){0,7}$')
SCHEMA_FILES = {
    "task": (".github/contracts/task-contract.schema.json", "taskId"),
    "handoff": (".github/contracts/handoff-contract.schema.json", "handoffId"),
}
SHARED_DEFINITIONS = (
    "agentRole",
    "agentOrHuman",
    "taskStatus",
    "lifecycleStage",
    "evidenceRecord",
    "continuousIntegrationClaim",
    "nextAction",
)

# Example artifacts live under `.github/contracts/examples/`. A directory named
# `_fixtures` inside that tree marks synthetic negative fixtures: they only have
# to be valid JSON, and they are deliberately exempt from schema validation, link
# validation, and the credential scan because they exist to be rejected.
FIXTURE_DIR_NAME = "_fixtures"

# Agent handoffs may only move the task along these role transitions. Reviewer to
# builder is the legitimate rework path; builder and QA may escalate to the human
# when blocked.
#
# Contract 1.2.0 adds exactly two transitions, each because a real chain could not
# be written without them:
#
#   architect -> human  the human scope gate. A scope approval is a decision only a
#                       human can make, so a chain that needs one has to stop at the
#                       human and resume afterwards.
#   reviewer -> qa      re-verification. A reviewer that finds the evidence itself
#                       inadequate must be able to send the task back to the role
#                       that gathers evidence without claiming the implementation is
#                       wrong.
#
# The two sets are kept separate on purpose. A 1.1.0 record is still judged against
# the 1.1.0 rules, so version numbers keep meaning what they say.
PERMITTED_HANDOFF_TRANSITIONS_1_1_0 = {
    ("architect", "builder"),
    ("builder", "qa"),
    ("builder", "human"),
    ("qa", "reviewer"),
    ("qa", "human"),
    ("reviewer", "builder"),
    ("reviewer", "human"),
}

PERMITTED_HANDOFF_TRANSITIONS_1_2_0 = PERMITTED_HANDOFF_TRANSITIONS_1_1_0 | {
    ("architect", "human"),
    ("reviewer", "qa"),
}

# Contract 1.3.0 adds the routed specialist reviewer. Every added edge exists
# because a chain that routes through a specialist cannot be written without it:
#
#   qa -> security-reviewer               QA hands the evidence to the specialist
#                                         the approved route requires.
#   security-reviewer -> reviewer         an approving specialist hands off to the
#                                         final review.
#   security-reviewer -> builder          a specialist that requires changes sends
#                                         the work back, exactly like the reviewer.
#   security-reviewer -> human            a specialist that blocks escalates; it
#                                         cannot approve or close anything.
#   reviewer -> security-reviewer         the final reviewer may send the evidence
#                                         back for specialist re-verification.
#
# No 1.2.0 transition was withdrawn or narrowed, so a 1.2.0 record keeps the rules
# it declared.
PERMITTED_HANDOFF_TRANSITIONS_1_3_0 = PERMITTED_HANDOFF_TRANSITIONS_1_2_0 | {
    ("qa", "security-reviewer"),
    ("security-reviewer", "reviewer"),
    ("security-reviewer", "builder"),
    ("security-reviewer", "human"),
    ("reviewer", "security-reviewer"),
}

# Contract 1.4.0 adds the second routed specialist, the UX/UI reviewer. The added
# edges exist because a chain that routes through it cannot be written without
# them, and because a task may now require both specialists in sequence:
#
#   qa -> ux-reviewer                    QA hands the evidence to the UX reviewer.
#   security-reviewer -> ux-reviewer     both routed: security reviews first.
#   ux-reviewer -> reviewer              an approving UX reviewer hands off.
#   ux-reviewer -> builder               a UX reviewer that requires changes returns
#                                        the work, exactly like the other reviewers.
#   ux-reviewer -> human                 a UX reviewer that blocks escalates.
#   reviewer -> ux-reviewer              the final reviewer may ask for a re-review.
#
# No 1.3.0 edge was withdrawn or narrowed, so a 1.3.0 record keeps its rules and a
# 1.3.0 record cannot route through the UX/UI reviewer.
PERMITTED_HANDOFF_TRANSITIONS_1_4_0 = PERMITTED_HANDOFF_TRANSITIONS_1_3_0 | {
    ("qa", "ux-reviewer"),
    ("security-reviewer", "ux-reviewer"),
    ("ux-reviewer", "reviewer"),
    ("ux-reviewer", "builder"),
    ("ux-reviewer", "human"),
    ("reviewer", "ux-reviewer"),
}

# The newest permitted set remains the module-level default so that anything that
# inspects it sees the broadest legal lifecycle rather than a stale subset.
PERMITTED_HANDOFF_TRANSITIONS = PERMITTED_HANDOFF_TRANSITIONS_1_4_0

CONTRACT_VERSION_1_1_0 = "1.1.0"
CONTRACT_VERSION_1_2_0 = "1.2.0"
CONTRACT_VERSION_1_3_0 = "1.3.0"
CONTRACT_VERSION_1_4_0 = "1.4.0"
SUPPORTED_CONTRACT_VERSIONS = (
    CONTRACT_VERSION_1_1_0,
    CONTRACT_VERSION_1_2_0,
    CONTRACT_VERSION_1_3_0,
    CONTRACT_VERSION_1_4_0,
)

#: Transitions each contract version permits. A record is always judged by the
#: rules of the version it declares, never relabelled or upgraded silently.
TRANSITIONS_BY_VERSION = {
    CONTRACT_VERSION_1_1_0: PERMITTED_HANDOFF_TRANSITIONS_1_1_0,
    CONTRACT_VERSION_1_2_0: PERMITTED_HANDOFF_TRANSITIONS_1_2_0,
    CONTRACT_VERSION_1_3_0: PERMITTED_HANDOFF_TRANSITIONS_1_3_0,
    CONTRACT_VERSION_1_4_0: PERMITTED_HANDOFF_TRANSITIONS_1_4_0,
}

HUMAN_FINAL_APPROVAL_STAGE = "human-final-approval"
STATUSES_REQUIRING_HUMAN_DESTINATION = {"ready-for-human-approval", "complete"}
# A blocked task may legitimately stop with the agents: an agent that cannot obtain
# a decision records the block, it does not decide. 1.2.0 therefore does not force
# a blocked chain to end at the human, but it does force it to end without implying
# approval.
BLOCKED_STATUS = "blocked"

MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
EXTERNAL_PREFIXES = ("http://", "https://", "mailto:", "tel:", "data:")

# Field names that hold a credential. `authorization` is matched only as a whole
# key or as an authorization header, because governance records legitimately talk
# about *authorization to act* (who approved what) and a substring rule rejected
# those records as if they contained a secret.
SENSITIVE_KEY = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|private[_-]?key|credential"
    r"|^authorization$|authorization[_-]?header)",
    re.IGNORECASE,
)
SENSITIVE_VALUE = (
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?:postgres(?:ql)?(?:\+[a-z0-9]+)?)://[^/\s:]+:[^@\s]+@", re.IGNORECASE),
)


@dataclass
class Finding:
    code: str
    path: str
    message: str


@dataclass
class Report:
    checks: int = 0
    errors: list[Finding] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)
    # Every directory the contract scan actually walked. A pass only ever speaks
    # about these roots; see the scan-root reporting requirement in the contract
    # README.
    scan_roots: list[str] = field(default_factory=list)

    def error(self, code: str, path: str, message: str) -> None:
        self.errors.append(Finding(code, path, message))

    def warn(self, code: str, path: str, message: str) -> None:
        self.warnings.append(Finding(code, path, message))

    def scanned(self, root: Path, repo_root: Path) -> None:
        display = _display_path(repo_root, root)
        if display not in self.scan_roots:
            self.scan_roots.append(display)

    def passed(self) -> bool:
        return not self.errors


def _display_path(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _read_text(path: Path, report: Report) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        report.error("read-error", path.as_posix(), f"cannot read file: {exc}")
        return None


def parse_frontmatter(text: str, source: str, report: Report) -> dict[str, Any] | None:
    """Parse a leading YAML frontmatter block using PyYAML.

    Returns None when the block is missing or malformed. This deliberately does
    not attempt a fallback parser: an unreadable definition is a validation
    failure, not something to guess at.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        report.error("frontmatter-missing", source, "file must start with a '---' YAML frontmatter block")
        return None

    closing = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            closing = index
            break
    if closing is None:
        report.error("frontmatter-unterminated", source, "frontmatter block is not closed with '---'")
        return None

    block = "\n".join(lines[1:closing])
    try:
        parsed = yaml.safe_load(block)
    except yaml.YAMLError as exc:
        report.error("frontmatter-invalid-yaml", source, f"frontmatter is not valid YAML: {exc}")
        return None

    if not isinstance(parsed, dict):
        report.error("frontmatter-not-mapping", source, "frontmatter must be a YAML mapping")
        return None
    return parsed


def _parse_list_value(value: Any) -> list[str]:
    """Read a frontmatter list value as strings.

    PyYAML turns ``[a, b]`` into a list; a single bare value becomes one entry.
    Anything else is reported by the caller as unusable.
    """

    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            return [
                item.strip().strip("'\"")
                for item in stripped[1:-1].split(",")
                if item.strip()
            ]
        return [stripped] if stripped else []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _require_text(mapping: dict[str, Any], key: str, source: str, report: Report) -> str | None:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        report.error("frontmatter-field", source, f"frontmatter field '{key}' must be a non-empty string")
        return None
    return value.strip()


@dataclass(frozen=True)
class CatalogSpecialist:
    """One specialist the trusted agent catalog declares."""

    name: str
    role: str
    stage: str
    definition: str
    definition_name: str
    tools: tuple[str, ...]
    skills: tuple[str, ...]
    source: str


@dataclass(frozen=True)
class AgentInventory:
    """The role/file/name/read-only inventory every check shares.

    The four executors are fixed. Every review specialist is derived from the
    catalog, so registering a reviewer changes the inventory without changing this
    validator: the checks below read it, they do not restate it.
    """

    files: dict[str, str]
    names: dict[str, str]
    read_only: frozenset[str]
    specialists: tuple[CatalogSpecialist, ...]

    @property
    def roles(self) -> tuple[str, ...]:
        return tuple(self.files)

    def role_for_file(self, filename: str) -> str | None:
        for role, expected in self.files.items():
            if expected == filename:
                return role
        return None


def _catalog_directory(root: Path) -> Path:
    return root.joinpath(*CATALOG_DIR)


def _catalog_schema(root: Path, report: Report) -> dict[str, Any] | None:
    path = root / CATALOG_SCHEMA_PATH
    if not path.is_file():
        report.error(
            "structure-schema-missing",
            CATALOG_SCHEMA_PATH,
            "the agent catalog schema is missing",
        )
        return None
    document = _load_json(path, root, report)
    if not isinstance(document, dict):
        return None
    return document


def _catalog_documents(
    root: Path, report: Report
) -> list[tuple[Path, dict[str, Any]]]:
    directory = _catalog_directory(root)
    if not directory.is_dir():
        report.error(
            "structure-catalog-missing",
            "/".join(CATALOG_DIR),
            "the trusted agent catalog directory is missing",
        )
        return []
    schema = _catalog_schema(root, report)
    validator = _build_validator(schema, CATALOG_SCHEMA_PATH, report) if schema else None
    documents: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(directory.glob(f"*{CATALOG_SUFFIX}")):
        document = _load_json(path, root, report)
        if not isinstance(document, dict):
            report.error(
                "catalog-not-object",
                _display_path(root, path),
                "a catalog entry must be one JSON object",
            )
            continue
        if validator is not None:
            _validate_against(validator, document, _display_path(root, path), report)
        documents.append((path, document))
    return documents


def load_catalog_inventory(root: Path, report: Report) -> tuple[CatalogSpecialist, ...]:
    """Validate the declarative catalog and return the specialists it declares.

    This is the same contract the runtime enforces in
    backend/app/services/agent_catalog.py: identity derives the stage and the role,
    the definition must agree with the entry, and the tools and skills must match in
    both directions. A disagreement is an error, never a skipped entry.
    """

    documents = _catalog_documents(root, report)
    agents_dir = root / ".github" / "agents"
    specialists: list[CatalogSpecialist] = []
    seen_names: dict[str, str] = {}
    seen_orders: dict[Any, str] = {}
    seen_stages: dict[str, str] = {}
    seen_roles: dict[str, str] = {}

    for path, document in documents:
        source = _display_path(root, path)
        name = document.get("name")
        if not isinstance(name, str) or not CATALOG_NAME_PATTERN.fullmatch(name):
            report.error(
                "catalog-name-invalid",
                source,
                "specialist name must be lower-case kebab-case",
            )
            continue
        if path.stem != name:
            report.error(
                "catalog-name-filename",
                source,
                f"file name must match the declared specialist name '{name}'",
            )
        if name in seen_names:
            report.error(
                "catalog-name-duplicate",
                source,
                f"specialist '{name}' is already declared in {seen_names[name]}",
            )
        seen_names[name] = source

        order = document.get("order")
        if not isinstance(order, int) or isinstance(order, bool) or order < 0:
            report.error(
                "catalog-order-invalid",
                source,
                "order must be a non-negative integer",
            )
        elif order in seen_orders:
            report.error(
                "catalog-order-duplicate",
                source,
                f"order {order} is already used by {seen_orders[order]}",
            )
        else:
            seen_orders[order] = source

        role = f"{name}{CATALOG_ROLE_SUFFIX}"
        stage = f"{name}{CATALOG_STAGE_SUFFIX}"
        if document.get("role") != role:
            report.error(
                "catalog-role-mismatch",
                source,
                f"role must be '{role}' (derived from the specialist name)",
            )
        if document.get("stage") != stage:
            report.error(
                "catalog-stage-mismatch",
                source,
                f"stage must be '{stage}' (derived from the specialist name)",
            )
        if role in seen_roles:
            report.error(
                "catalog-role-duplicate",
                source,
                f"role '{role}' is already declared in {seen_roles[role]}",
            )
        seen_roles[role] = source
        if stage in seen_stages:
            report.error(
                "catalog-stage-duplicate",
                source,
                f"stage '{stage}' is already declared in {seen_stages[stage]}",
            )
        seen_stages[stage] = source

        if document.get("read_only") is not True:
            report.error(
                "catalog-read-only-required",
                source,
                "a review specialist must declare read_only: true",
            )

        tools = _parse_list_value(document.get("tools"))
        if not tools:
            report.error("catalog-tools-missing", source, "tools must be a non-empty list")
        seen_tools: set[str] = set()
        for tool in tools:
            if tool not in ALLOWED_AGENT_TOOLS:
                report.error(
                    "catalog-tools-unknown",
                    source,
                    f"unsupported tool '{tool}'; allowed: {', '.join(sorted(ALLOWED_AGENT_TOOLS))}",
                )
            if tool in seen_tools:
                report.error(
                    "catalog-tools-duplicate", source, f"tool '{tool}' is declared twice"
                )
            seen_tools.add(tool)
        forbidden = sorted(seen_tools & READ_ONLY_FORBIDDEN_TOOLS)
        if forbidden:
            report.error(
                "catalog-tools-forbidden",
                source,
                f"a review specialist must not declare: {', '.join(forbidden)}",
            )

        skills = _parse_list_value(document.get("skills"))
        if not skills:
            report.error(
                "catalog-skills-missing", source, "skills must be a non-empty list"
            )
        for skill in skills:
            if not SKILL_NAME_PATTERN.fullmatch(skill):
                report.error(
                    "catalog-skill-pattern",
                    source,
                    f"skill '{skill}' must be lower-case kebab-case",
                )

        definition = document.get("definition")
        definition_name = document.get("definition_name")
        if not isinstance(definition, str) or not definition:
            report.error(
                "catalog-definition-missing",
                source,
                "definition must name the agent definition file",
            )
            continue
        if "/" in definition or "\\" in definition or definition.startswith("."):
            report.error(
                "catalog-definition-unsafe",
                source,
                "definition must be a bare file name inside .github/agents",
            )
            continue
        definition_path = agents_dir / definition
        if not definition_path.is_file():
            report.error(
                "catalog-definition-missing",
                source,
                f"agent definition '{definition}' does not exist",
            )
        else:
            text = _read_text(definition_path, report)
            frontmatter = (
                parse_frontmatter(text, _display_path(root, definition_path), report)
                if text is not None
                else None
            )
            if frontmatter is not None:
                declared_name = frontmatter.get("name")
                if declared_name != definition_name:
                    report.error(
                        "catalog-definition-name-mismatch",
                        source,
                        f"agent definition declares '{declared_name}', catalog expects "
                        f"'{definition_name}'",
                    )
                declared_tools = _parse_list_value(frontmatter.get("tools"))
                if declared_tools != tools:
                    report.error(
                        "catalog-tools-mismatch",
                        source,
                        f"catalog tools {tools} disagree with the definition's "
                        f"{declared_tools}",
                    )
                declared_skills = _parse_list_value(frontmatter.get("skills"))
                if declared_skills != skills:
                    report.error(
                        "catalog-skills-mismatch",
                        source,
                        f"catalog skills {skills} disagree with the definition's "
                        f"{declared_skills}",
                    )

        selection = document.get("selection")
        path_signal = document.get("path_signal")
        text_markers = document.get("text_markers")
        if not isinstance(selection, list) or not selection:
            report.error(
                "catalog-selection-missing",
                source,
                "selection must be a non-empty list of rules",
            )
            selection = []
        for rule in selection:
            if not isinstance(rule, dict):
                report.error(
                    "catalog-selection-invalid", source, "each selection rule must be an object"
                )
                continue
            reason = rule.get("reason")
            if not isinstance(reason, str) or len(reason.strip()) < 3:
                report.error(
                    "catalog-selection-invalid",
                    source,
                    "each selection rule needs a reason of at least 3 characters",
                )
            if rule.get("paths") and path_signal is None:
                report.error(
                    "catalog-signal-unknown",
                    source,
                    f"selection '{reason}' requires paths, but the entry declares no path_signal",
                )
            if rule.get("text") and not _parse_list_value(text_markers):
                report.error(
                    "catalog-signal-unknown",
                    source,
                    f"selection '{reason}' requires text, but the entry declares no text_markers",
                )
        if path_signal is not None:
            if not isinstance(path_signal, dict):
                report.error(
                    "catalog-signal-invalid", source, "path_signal must be an object"
                )
                path_signal = None
            else:
                if not isinstance(path_signal.get("change_reason"), str) or not path_signal.get(
                    "change_reason", ""
                ).strip():
                    report.error(
                        "catalog-signal-invalid",
                        source,
                        "path_signal must declare change_reason",
                    )
                regexes = _parse_list_value(path_signal.get("regexes"))
                for pattern in regexes:
                    try:
                        re.compile(pattern)
                    except re.error as exc:
                        report.error(
                            "catalog-signal-invalid",
                            source,
                            f"path_signal regex is invalid: {exc}",
                        )
                facts = (
                    regexes
                    + _parse_list_value(path_signal.get("suffixes"))
                    + _parse_list_value(path_signal.get("segments"))
                    + _parse_list_value(path_signal.get("areas"))
                )
                if not facts:
                    report.error(
                        "catalog-signal-invalid",
                        source,
                        "path_signal must declare at least one matching fact",
                    )

        prompt = document.get("prompt")
        if not isinstance(prompt, dict):
            report.error("catalog-prompt-invalid", source, "prompt must be an object")
        else:
            for key in ("mission", "focus", "focus_rules", "focus_heading", "path_label"):
                value = prompt.get(key)
                if not isinstance(value, str) or len(value.strip()) < 12:
                    report.error(
                        "catalog-prompt-invalid",
                        source,
                        f"prompt '{key}' must be a string of at least 12 characters",
                    )

        specialists.append(
            CatalogSpecialist(
                name=name,
                role=role,
                stage=stage,
                definition=definition,
                definition_name=str(definition_name or ""),
                tools=tuple(tools),
                skills=tuple(skills),
                source=source,
            )
        )

    if not specialists and documents:
        report.error(
            "catalog-empty",
            "/".join(CATALOG_DIR),
            "the catalog declares no usable specialist",
        )
    report.checks += 1
    return tuple(specialists)


def build_inventory(specialists: Iterable[CatalogSpecialist]) -> AgentInventory:
    files = dict(CORE_AGENT_FILES)
    names = dict(CORE_AGENT_NAMES)
    read_only = set(CORE_READ_ONLY_ROLES)
    for specialist in specialists:
        files[specialist.role] = specialist.definition
        names[specialist.role] = specialist.definition_name
        read_only.add(specialist.role)
    return AgentInventory(
        files=files,
        names=names,
        read_only=frozenset(read_only),
        specialists=tuple(specialists),
    )


def _empty_inventory() -> AgentInventory:
    return build_inventory(())


def _check_agent_tools(
    role: str, tools: Any, source: str, report: Report, *, read_only: frozenset[str]
) -> None:
    if role in read_only:
        if tools is None:
            report.error(
                "agent-tools-missing",
                source,
                f"{role} is a read-only role and must declare an explicit tools list without edit or execute",
            )
            return

    if tools is None:
        return

    if not isinstance(tools, list) or not tools:
        report.error("agent-tools-type", source, "tools must be a non-empty list of tool names")
        return

    seen: set[str] = set()
    for tool in tools:
        if not isinstance(tool, str) or not tool:
            report.error("agent-tools-entry", source, f"invalid tool entry: {tool!r}")
            continue
        if tool not in ALLOWED_AGENT_TOOLS:
            report.error(
                "agent-tools-unknown",
                source,
                f"unsupported tool '{tool}'; allowed: {', '.join(sorted(ALLOWED_AGENT_TOOLS))}",
            )
        if tool in seen:
            report.error("agent-tools-duplicate", source, f"tool '{tool}' is declared more than once")
        seen.add(tool)

    if role in read_only:
        forbidden = sorted(seen & READ_ONLY_FORBIDDEN_TOOLS)
        if forbidden:
            report.error(
                "agent-tools-restriction",
                source,
                f"{role} must not declare mutating or executing tools: {', '.join(forbidden)}",
            )


def _check_agent_skills(
    role: str, skills: Any, source: str, report: Report
) -> None:
    """A canonical role must declare the skills the runtime will load for it."""

    declared = _parse_list_value(skills)
    if not declared:
        report.error(
            "agent-skills-missing",
            source,
            f"{role} must declare the skills the runtime loads for it",
        )
        return
    seen: set[str] = set()
    for name in declared:
        if not SKILL_NAME_PATTERN.fullmatch(name):
            report.error(
                "agent-skill-pattern",
                source,
                f"skill '{name}' must be lower-case kebab-case",
            )
        if name in seen:
            report.error(
                "agent-skill-duplicate",
                source,
                f"skill '{name}' is declared more than once",
            )
        seen.add(name)


def check_structure(
    root: Path, report: Report, inventory: AgentInventory | None = None
) -> None:
    agents_dir = root / ".github" / "agents"
    skills_dir = root / ".github" / "skills"
    contracts_dir = root / ".github" / "contracts"

    for directory in (agents_dir, skills_dir, contracts_dir):
        if not directory.is_dir():
            report.error("structure-missing", _display_path(root, directory), "required directory is missing")

    # The core definition files are fixed; every specialist definition is derived
    # from the catalog, so registering a reviewer needs no edit here.
    active = inventory or _empty_inventory()
    for role in CORE_AGENT_ROLES:
        filename = active.files[role]
        if not (agents_dir / filename).is_file():
            report.error(
                "structure-agent-missing",
                f".github/agents/{filename}",
                f"required {role} agent definition is missing",
            )
    for specialist in active.specialists:
        if not (agents_dir / specialist.definition).is_file():
            report.error(
                "structure-agent-missing",
                f".github/agents/{specialist.definition}",
                f"the catalog declares {specialist.name} but its agent definition is missing",
            )

    instructions = root / ".github" / "copilot-instructions.md"
    if not instructions.is_file():
        report.error("structure-missing", ".github/copilot-instructions.md", "repository instructions are missing")

    for kind, (relative, _) in SCHEMA_FILES.items():
        if not (root / relative).is_file():
            report.error("structure-schema-missing", relative, f"required {kind} contract schema is missing")

    for relative in (CATALOG_SCHEMA_PATH,):
        if not (root / relative).is_file():
            report.error(
                "structure-schema-missing",
                relative,
                "the agent catalog schema is missing",
            )

    report.checks += 1


def check_agents(
    root: Path, report: Report, inventory: AgentInventory | None = None
) -> None:
    agents_dir = root / ".github" / "agents"
    if not agents_dir.is_dir():
        return
    active = inventory or _empty_inventory()

    declared_names: dict[str, str] = {}
    for path in sorted(agents_dir.glob("*.agent.md")):
        source = _display_path(root, path)
        text = _read_text(path, report)
        if text is None:
            continue
        frontmatter = parse_frontmatter(text, source, report)
        if frontmatter is None:
            continue

        unexpected = sorted(set(frontmatter) - ALLOWED_AGENT_KEYS)
        if unexpected:
            report.error(
                "agent-frontmatter-key",
                source,
                f"unsupported frontmatter key(s): {', '.join(unexpected)}",
            )

        name = _require_text(frontmatter, "name", source, report)
        description = _require_text(frontmatter, "description", source, report)
        role: str | None = active.role_for_file(path.name)

        if name is not None:
            if name in declared_names:
                report.error(
                    "agent-name-duplicate",
                    source,
                    f"agent name '{name}' is already declared in {declared_names[name]}",
                )
            else:
                declared_names[name] = source

            if role is not None:
                expected = active.names[role]
                if name != expected:
                    report.error(
                        "agent-name-mismatch",
                        source,
                        f"expected agent name '{expected}' for the {role} role, found '{name}'",
                    )
            else:
                expected_slug = name.lower().replace(" ", "-")
                if path.name != f"{expected_slug}.agent.md":
                    report.error(
                        "agent-filename-mismatch",
                        source,
                        f"file name must match the declared name slug '{expected_slug}.agent.md'",
                    )

        if description is not None and len(description) < 20:
            report.error("agent-description", source, "description must be at least 20 characters")

        if role is not None:
            _check_agent_tools(
                role,
                frontmatter.get("tools"),
                source,
                report,
                read_only=active.read_only,
            )
            _check_agent_skills(role, frontmatter.get("skills"), source, report)

    for path in sorted(agents_dir.glob("*.md")):
        if not path.name.endswith(".agent.md"):
            report.warn(
                "agent-extension",
                _display_path(root, path),
                "agent files should use the '.agent.md' suffix to be discoverable",
            )

    report.checks += 1


def _role_stage_map(inventory: AgentInventory) -> dict[str, str]:
    """Every role the factory can run, mapped to the stage it executes in."""

    stages = dict(FIXED_ROLE_STAGES)
    for specialist in inventory.specialists:
        stages[specialist.role] = specialist.stage
    return stages


def _declared_skill_names(root: Path) -> dict[str, set[str]]:
    """Skill name -> the roles whose definitions declare it, read silently."""

    agents_dir = root / ".github" / "agents"
    declared: dict[str, set[str]] = {}
    if not agents_dir.is_dir():
        return declared
    for path in sorted(agents_dir.glob("*.agent.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            continue
        block: list[str] = []
        for line in lines[1:]:
            if line.strip() == "---":
                break
            block.append(line)
        try:
            frontmatter = yaml.safe_load("\n".join(block))
        except yaml.YAMLError:
            continue
        if not isinstance(frontmatter, dict):
            continue
        for name in _parse_list_value(frontmatter.get("skills")):
            declared.setdefault(name, set()).add(path.stem.removesuffix(".agent"))
    return declared


def _skill_vocabulary(
    frontmatter: dict[str, Any],
    key: str,
    source: str,
    report: Report,
    allowed: tuple[str, ...],
) -> list[str]:
    """Read one declared vocabulary list, reporting strays and duplicates."""

    values = _parse_list_value(frontmatter.get(key))
    seen: set[str] = set()
    for value in values:
        if value not in allowed:
            report.error(
                "skill-metadata-value",
                source,
                f"'{key}' value '{value}' is not one of {', '.join(allowed)}",
            )
        if value in seen:
            report.error(
                "skill-metadata-duplicate",
                source,
                f"'{key}' declares '{value}' more than once",
            )
        seen.add(value)
    return values


def check_skills(
    root: Path, report: Report, inventory: AgentInventory | None = None
) -> None:
    skills_dir = root / ".github" / "skills"
    if not skills_dir.is_dir():
        return
    active = inventory or _empty_inventory()
    role_stage = _role_stage_map(active)
    declaring_roles = _declared_skill_names(root)

    declared_names: dict[str, str] = {}
    metadata: dict[str, dict[str, Any]] = {}
    for skill_dir in sorted(entry for entry in skills_dir.iterdir() if entry.is_dir()):
        skill_file = skill_dir / "SKILL.md"
        source = _display_path(root, skill_file)
        if not skill_file.is_file():
            report.error("skill-entry-missing", source, "skill directory must contain SKILL.md")
            continue

        text = _read_text(skill_file, report)
        if text is None:
            continue
        frontmatter = parse_frontmatter(text, source, report)
        if frontmatter is None:
            continue

        unexpected = sorted(set(frontmatter) - ALLOWED_SKILL_KEYS)
        if unexpected:
            report.error(
                "skill-frontmatter-key",
                source,
                f"unsupported frontmatter key(s): {', '.join(unexpected)}",
            )

        name = _require_text(frontmatter, "name", source, report)
        description = _require_text(frontmatter, "description", source, report)

        if name is not None:
            if name != skill_dir.name:
                report.error(
                    "skill-name-directory",
                    source,
                    f"declared skill name '{name}' must match directory name '{skill_dir.name}'",
                )
            if name in declared_names:
                report.error(
                    "skill-name-duplicate",
                    source,
                    f"skill name '{name}' is already declared in {declared_names[name]}",
                )
            else:
                declared_names[name] = source

        if description is not None and len(description) < 20:
            report.error("skill-description", source, "description must be at least 20 characters")

        if name is not None and not SKILL_NAME_PATTERN.fullmatch(name):
            report.error(
                "skill-name-pattern",
                source,
                f"skill name '{name}' must be lower-case kebab-case",
            )

        declared_roles = _parse_list_value(frontmatter.get("roles"))
        if not declared_roles:
            report.error(
                "skill-roles-missing",
                source,
                "a skill must declare the agent roles that may use it ('roles: [builder]')",
            )
        else:
            seen_roles: set[str] = set()
            for entry in declared_roles:
                if entry not in active.roles:
                    report.error(
                        "skill-role-unknown",
                        source,
                        f"role '{entry}' is not a role the canonical inventory declares",
                    )
                if entry in seen_roles:
                    report.error(
                        "skill-role-duplicate",
                        source,
                        f"role '{entry}' is declared more than once",
                    )
                seen_roles.add(entry)

        # Stage names are either a fixed factory stage or a catalog stage, so a
        # skill attached to a registered specialist needs no validator change.
        stages = _parse_list_value(frontmatter.get("stages"))
        catalog_stages = {specialist.stage for specialist in active.specialists}
        seen_stages: set[str] = set()
        for stage in stages:
            if stage not in FACTORY_SKILL_STAGES and stage not in catalog_stages:
                report.error(
                    "skill-stage-unknown",
                    source,
                    f"stage '{stage}' is neither a factory stage nor a catalog stage",
                )
            if stage in seen_stages:
                report.error(
                    "skill-stage-duplicate", source, f"stage '{stage}' is declared twice"
                )
            seen_stages.add(stage)

        required = frontmatter.get("required", False)
        if not isinstance(required, bool):
            report.error(
                "skill-required-invalid", source, "'required' must be true or false"
            )
            required = False
        if required and not stages:
            report.error(
                "skill-required-stages",
                source,
                "a required skill must declare the stages it is required in",
            )

        task_types = _skill_vocabulary(frontmatter, "task_types", source, report, SKILL_TASK_TYPES)
        areas = _skill_vocabulary(frontmatter, "areas", source, report, SKILL_AREAS)
        implications = _skill_vocabulary(
            frontmatter, "implications", source, report, SKILL_IMPLICATIONS
        )
        risk_levels = _skill_vocabulary(
            frontmatter, "risk_levels", source, report, SKILL_RISK_LEVELS
        )
        requires_tools = _skill_vocabulary(
            frontmatter, "requires_tools", source, report, SKILL_TOOLS
        )
        requires_skills = _skill_vocabulary(
            frontmatter,
            "requires_skills",
            source,
            report,
            tuple(declared_names) + tuple(
                entry.name for entry in skills_dir.iterdir() if entry.is_dir()
            ),
        ) if frontmatter.get("requires_skills") is not None else []

        applicability = task_types or areas or implications or risk_levels
        declared_for = declaring_roles.get(name or "", set())
        if name is not None:
            metadata[name] = {
                "source": source,
                "roles": declared_roles,
                "stages": stages,
                "required": required,
                "applicability": bool(applicability),
                "declared_for": declared_for,
                "requires_tools": requires_tools,
                "requires_skills": requires_skills,
            }

        # A skill reachable only contextually has to say when it applies, or the
        # selection layer could never choose it.
        if (
            name is not None
            and not declared_for
            and not required
            and not applicability
        ):
            report.error(
                "skill-never-selectable",
                source,
                "a skill that no role definition declares must declare 'required: true' "
                "or at least one applicability fact (task_types, areas, implications, "
                "risk_levels)",
            )

    # A role definition pins a procedure for every execution of that role, so the
    # procedure's own stage declaration has to include that role's stage.
    for name, entry in metadata.items():
        for role in sorted(entry["declared_for"]):
            stage = role_stage.get(role)
            if stage is None:
                continue
            if entry["stages"] and stage not in entry["stages"]:
                report.error(
                    "skill-stage-contradiction",
                    entry["source"],
                    f"skill '{name}' is declared by the '{role}' definition, which runs "
                    f"in stage '{stage}', but declares stages "
                    f"{', '.join(entry['stages'])}",
                )
            for tool in entry["requires_tools"]:
                definition = active.files.get(role)
                if definition is None:
                    continue
                tools = _agent_declared_tools(root, definition)
                if tools and tool not in tools:
                    report.error(
                        "skill-prerequisite-tools",
                        entry["source"],
                        f"skill '{name}' requires tool '{tool}', but the '{role}' "
                        "definition does not authorize it",
                    )

    report.checks += 1


def _agent_declared_tools(root: Path, filename: str) -> tuple[str, ...]:
    """The tools an agent definition declares, read silently for cross-checks."""

    path = root / ".github" / "agents" / filename
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ()
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ()
    block: list[str] = []
    for line in lines[1:]:
        if line.strip() == "---":
            break
        block.append(line)
    try:
        frontmatter = yaml.safe_load("\n".join(block))
    except yaml.YAMLError:
        return ()
    if not isinstance(frontmatter, dict):
        return ()
    return tuple(_parse_list_value(frontmatter.get("tools")))



def check_markdown_links(root: Path, report: Report) -> None:
    targets: list[Path] = []
    github_dir = root / ".github"
    if github_dir.is_dir():
        targets.extend(sorted(github_dir.rglob("*.md")))
    workflows = root / "docs" / "workflows"
    if workflows.is_dir():
        targets.extend(sorted(workflows.glob("*.md")))

    for path in targets:
        text = _read_text(path, report)
        if text is None:
            continue
        for match in MARKDOWN_LINK.finditer(text):
            raw_target = match.group(1).strip()
            if not raw_target or raw_target.startswith("#"):
                continue
            if raw_target.startswith(EXTERNAL_PREFIXES):
                continue
            if raw_target.startswith("<") or " " in raw_target.split("#")[0]:
                continue

            local_part = unquote(raw_target.split("#", 1)[0])
            if not local_part:
                continue
            resolved = (path.parent / local_part).resolve()
            try:
                resolved.relative_to(root.resolve())
            except ValueError:
                report.error(
                    "link-outside-repository",
                    _display_path(root, path),
                    f"relative link '{raw_target}' escapes the repository",
                )
                continue
            if not resolved.exists():
                report.error(
                    "link-broken",
                    _display_path(root, path),
                    f"relative link '{raw_target}' does not resolve to an existing path",
                )

    report.checks += 1


def _load_json(path: Path, root: Path, report: Report) -> Any | None:
    text = _read_text(path, report)
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        report.error("json-invalid", _display_path(root, path), f"invalid JSON: {exc}")
        return None


def _build_validator(schema: dict[str, Any], source: str, report: Report) -> Draft202012Validator | None:
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:  # jsonschema raises SchemaError
        report.error("schema-invalid", source, f"schema is not a valid JSON Schema 2020-12 document: {exc}")
        return None
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _validate_against(validator: Draft202012Validator, document: Any, source: str, report: Report) -> None:
    errors = sorted(validator.iter_errors(document), key=lambda error: list(error.absolute_path))
    for error in errors:
        location = "/".join(str(part) for part in error.absolute_path) or "<root>"
        report.error("contract-schema-violation", source, f"{location}: {error.message}")


def check_shared_semantics(root: Path, schemas: dict[str, dict[str, Any]], report: Report) -> None:
    """Ensure both contracts share one status vocabulary and one evidence model."""
    task_schema = schemas.get("task")
    handoff_schema = schemas.get("handoff")
    if task_schema is None or handoff_schema is None:
        return

    task_defs = task_schema.get("$defs", {})
    handoff_defs = handoff_schema.get("$defs", {})
    for name in SHARED_DEFINITIONS:
        if name not in task_defs or name not in handoff_defs:
            report.error(
                "contract-definition-missing",
                ".github/contracts",
                f"shared definition '{name}' must exist in both contract schemas",
            )
            continue
        if task_defs[name] != handoff_defs[name]:
            report.error(
                "contract-definition-drift",
                ".github/contracts",
                f"shared definition '{name}' differs between the task and handoff schemas",
            )

    task_version = task_schema.get("properties", {}).get("contractVersion", {})
    handoff_version = handoff_schema.get("properties", {}).get("contractVersion", {})
    if task_version != handoff_version:
        report.error(
            "contract-version-drift",
            ".github/contracts",
            "contractVersion must be identical in both schemas",
        )

    report.checks += 1


def _resolve_repo_path(root: Path, value: str) -> Path:
    return (root / value).resolve()


def _scan_sensitive(document: Any, source: str, report: Report, trail: str = "") -> None:
    if isinstance(document, dict):
        for key, value in document.items():
            location = f"{trail}/{key}" if trail else str(key)
            if SENSITIVE_KEY.search(str(key)):
                report.error(
                    "sensitive-field",
                    source,
                    f"contract artifacts must not contain credential-like field '{location}'",
                )
            _scan_sensitive(value, source, report, location)
    elif isinstance(document, list):
        for index, value in enumerate(document):
            _scan_sensitive(value, source, report, f"{trail}[{index}]")
    elif isinstance(document, str):
        for pattern in SENSITIVE_VALUE:
            if pattern.search(document):
                report.error(
                    "sensitive-value",
                    source,
                    "contract artifact contains a value that looks like a credential or credentialed URL",
                )
                return


def _adr_status(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"\*\*Status:\*\*\s*([A-Za-z]+)", text)
    if match is None:
        return None
    return match.group(1).strip().lower()


HANDOFF_SEQUENCE = re.compile(r"^HND-([0-9]{3})$")


def _handoff_sequence(handoff_id: Any) -> int | None:
    if not isinstance(handoff_id, str):
        return None
    match = HANDOFF_SEQUENCE.match(handoff_id)
    if match is None:
        return None
    return int(match.group(1))


def _check_evidence_exit_codes(records: Any, source: str, report: Report, location: str) -> None:
    """Require a declared expected exit code to match the recorded exit code.

    An exit code is execution metadata, so this rule never assigns or infers an
    ``outcome``: a matching code proves only that the process exited as claimed.
    The schema keeps the separate rationale requirement for successful negative
    tests, and this rule deliberately leaves it in place.
    """
    if not isinstance(records, list):
        return

    for index, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        expected = record.get("expectedExitCode")
        if not isinstance(expected, int) or isinstance(expected, bool):
            continue

        actual = record.get("exitCode")
        where = f"{location}[{index}]"
        if not isinstance(actual, int) or isinstance(actual, bool):
            report.error(
                "evidence-exit-code-missing",
                source,
                f"{where}: expectedExitCode {expected} requires a recorded integer exitCode",
            )
            continue
        if actual != expected:
            report.error(
                "evidence-exit-code-mismatch",
                source,
                f"{where}: recorded exitCode {actual} does not match expectedExitCode {expected}",
            )


def _check_evidence_execution_state_version(
    records: Any, contract_version: Any, source: str, report: Report, location: str
) -> None:
    """Keep 1.2.0 execution-state vocabulary out of a 1.1.0 record.

    The shared ``evidenceRecord`` definition cannot read the parent record's
    ``contractVersion``: JSON Schema draft 2020-12 has no cross-subschema data
    reference, so a definition used by two schemas cannot gate itself on a value
    it does not own. The schema therefore gates this at each parent site, and
    this check restates the same rule so a violation is reported by name rather
    than as an anonymous schema failure.

    The rule matters because ``executionState`` is the escape hatch that lets an
    ``observed`` record omit ``exitCode``. Without it, a 1.1.0 record could take
    that hatch and quietly be read under weaker rules than the version it
    declares.
    """
    if contract_version != CONTRACT_VERSION_1_1_0 or not isinstance(records, list):
        return

    for index, record in enumerate(records):
        if isinstance(record, dict) and "executionState" in record:
            report.error(
                "contract-version-vocabulary",
                source,
                f"{location}[{index}]: executionState is 1.2.0 vocabulary and is not "
                "permitted on a 1.1.0 record; declare contractVersion 1.2.0 to use it",
            )


def _resolve_record_path(root: Path, record_set: Path, value: str) -> Path:
    """Resolve a path reference that was written inside a record set.

    A record set is self-contained, so a reference is resolved against the
    directory that holds the records first and against the repository root
    second. Both bases are safe because the schema already forbids absolute
    paths and parent-directory traversal.

    Records that misspell a path still have to fail closed, so an unresolved
    reference falls back to the record-set base and the caller reports it as
    missing.
    """
    for base in (record_set, root):
        candidate = (base / value).resolve()
        if candidate.exists():
            return candidate
    return (record_set / value).resolve()


def check_examples(
    root: Path,
    schemas: dict[str, dict[str, Any]],
    report: Report,
    records_roots: Iterable[Path] = (),
) -> None:
    """Validate every contract artifact under every scan root.

    A record set is validated independently: task and handoff identifiers only
    have to be unique inside the set they belong to, and references are resolved
    inside that set first. This is what makes a validation result meaningful for
    records that live outside the bundled examples, and why the scanned roots are
    always reported.
    """
    examples_dir = root / ".github" / "contracts" / "examples"
    scan_roots: list[tuple[Path, bool]] = []
    if examples_dir.is_dir():
        scan_roots.append((examples_dir.resolve(), True))
    else:
        report.error(
            "structure-examples-missing",
            ".github/contracts/examples",
            "example contract artifacts are required so the schemas stay executable",
        )

    for supplied in records_roots:
        candidate = supplied.resolve()
        if candidate == examples_dir.resolve():
            continue
        scan_roots.append((candidate, False))

    if not scan_roots:
        return

    validators = {}
    for kind, schema in schemas.items():
        validators[kind] = _build_validator(schema, f"schema:{kind}", report)

    for directory, is_examples in scan_roots:
        if is_examples:
            # Fixtures are never validated as records, but a fixture that cannot
            # even be parsed would silently stop demonstrating anything, so
            # readability is still required of every fixture file.
            for path in sorted(directory.rglob("*.json")):
                if FIXTURE_DIR_NAME in path.relative_to(directory).parts:
                    _load_json(path, root, report)
        record_sets = _record_set_directories(directory, is_examples)
        if not record_sets:
            report.error(
                "structure-examples-missing",
                _display_path(root, directory),
                "no contract artifact was found under this record root",
            )
            report.scanned(directory, root)
            continue
        for record_set in record_sets:
            report.scanned(record_set, root)
            _check_record_set(root, record_set, is_examples, validators, report)

    report.checks += 1


def _record_set_directories(directory: Path, is_examples: bool) -> list[Path]:
    """Group contract artifacts by the directory that holds them.

    One directory is one record set: task and handoff identifiers only have to be
    unique inside it, and references resolve inside it first. Grouping by
    directory rather than treating a whole tree as one set keeps two tasks that
    happen to share a parent from being validated against each other's
    identifiers, which would report a false duplicate or a false broken chain.
    """
    groups: set[Path] = set()
    for path in directory.rglob("*.json"):
        if is_examples and FIXTURE_DIR_NAME in path.relative_to(directory).parts:
            continue
        groups.add(path.parent)
    return sorted(groups)


def _check_record_set(
    root: Path,
    directory: Path,
    is_examples: bool,
    validators: dict[str, Draft202012Validator | None],
    report: Report,
) -> None:
    tasks: dict[str, tuple[Path, dict[str, Any]]] = {}
    handoffs: dict[str, tuple[Path, dict[str, Any]]] = {}
    artifacts_by_path: dict[Path, tuple[str, str]] = {}

    # Phase 1: load, classify, validate, and index every contract artifact. No
    # cross-reference is resolved here so that the result never depends on the
    # order in which files are discovered.
    for path in sorted(directory.rglob("*.json")):
        source = _display_path(root, path)
        document = _load_json(path, root, report)
        if not isinstance(document, dict):
            continue

        if is_examples and FIXTURE_DIR_NAME in path.relative_to(directory).parts:
            # Synthetic negative fixture: parsed as JSON above and deliberately
            # not validated or scanned. See FIXTURE_DIR_NAME.
            continue

        _scan_sensitive(document, source, report)

        is_task = "taskId" in document
        is_handoff = "handoffId" in document
        if is_task == is_handoff:
            report.error(
                "contract-kind-ambiguous",
                source,
                "example must contain exactly one of taskId or handoffId",
            )
            continue

        kind = "task" if is_task else "handoff"
        validator = validators.get(kind)
        if validator is not None:
            _validate_against(validator, document, source, report)

        if kind == "task":
            execution_evidence = document.get("executionEvidence")
            if isinstance(execution_evidence, dict):
                _check_evidence_exit_codes(
                    execution_evidence.get("validations"),
                    source,
                    report,
                    "executionEvidence.validations",
                )
                _check_evidence_execution_state_version(
                    execution_evidence.get("validations"),
                    document.get("contractVersion"),
                    source,
                    report,
                    "executionEvidence.validations",
                )
        else:
            _check_evidence_exit_codes(document.get("validation"), source, report, "validation")
            _check_evidence_execution_state_version(
                document.get("validation"),
                document.get("contractVersion"),
                source,
                report,
                "validation",
            )

        if kind == "task":
            task_id = document.get("taskId")
            if not isinstance(task_id, str):
                continue
            if task_id in tasks:
                report.error("task-duplicate", source, f"taskId '{task_id}' is declared more than once")
                continue
            tasks[task_id] = (path, document)
            artifacts_by_path[path.resolve()] = ("task", task_id)
        else:
            handoff_id = document.get("handoffId")
            if not isinstance(handoff_id, str):
                continue
            if handoff_id in handoffs:
                report.error(
                    "handoff-duplicate",
                    source,
                    f"handoffId '{handoff_id}' is declared more than once",
                )
                continue
            handoffs[handoff_id] = (path, document)
            artifacts_by_path[path.resolve()] = ("handoff", handoff_id)

    # Every record in a set must declare the same contract version. A mixed set
    # means a migration was left half-finished, which no later rule can judge.
    declared_versions = {
        document.get("contractVersion")
        for _, document in [*tasks.values(), *handoffs.values()]
        if isinstance(document.get("contractVersion"), str)
    }
    if len(declared_versions) > 1:
        report.error(
            "contract-version-mixed",
            _display_path(root, directory),
            "records in one set declare more than one contractVersion: "
            + ", ".join(sorted(declared_versions)),
        )
    contract_version = next(iter(declared_versions), CONTRACT_VERSION_1_1_0)
    permitted_transitions = TRANSITIONS_BY_VERSION.get(
        str(contract_version), PERMITTED_HANDOFF_TRANSITIONS_1_1_0
    )

    # Phase 2: resolve every handoff-to-task reference against the completed
    # index, in both directions.
    referenced_handoffs: dict[str, set[str]] = {task_id: set() for task_id in tasks}
    for handoff_id, (handoff_path, handoff) in sorted(handoffs.items()):
        source = _display_path(root, handoff_path)
        task_ref = handoff.get("taskRef")
        if not isinstance(task_ref, dict):
            continue
        task_id = task_ref.get("taskId")
        task_path = task_ref.get("path")
        if isinstance(task_id, str) and task_id not in tasks:
            report.error(
                "handoff-task-unknown",
                source,
                f"taskRef.taskId '{task_id}' does not match any task example",
            )
        if not isinstance(task_id, str) or not isinstance(task_path, str):
            continue
        resolved = _resolve_record_path(root, handoff_path.parent, task_path)
        if not resolved.is_file():
            report.error(
                "handoff-task-path",
                source,
                f"taskRef.path '{task_path}' does not resolve to an existing file",
            )
            continue
        target = artifacts_by_path.get(resolved)
        if target is None:
            report.error(
                "handoff-task-path-mismatch",
                source,
                f"taskRef.path '{task_path}' is not a contract artifact",
            )
        elif target[0] != "task":
            report.error(
                "handoff-task-path-mismatch",
                source,
                f"taskRef.path '{task_path}' points at a handoff artifact, not at a task",
            )
        elif target[1] != task_id:
            report.error(
                "handoff-task-path-mismatch",
                source,
                f"taskRef.path '{task_path}' does not point at the task with id '{task_id}'",
            )

    for task_id, (task_path, task) in tasks.items():
        task_source = _display_path(root, task_path)
        criteria = {
            item.get("id")
            for item in task.get("acceptanceCriteria", [])
            if isinstance(item, dict)
        }
        check_ids = {
            item.get("checkId")
            for item in task.get("executionEvidence", {}).get("validations", [])
            if isinstance(item, dict)
        }

        for reference in task.get("adrReferences", []):
            if not isinstance(reference, dict):
                continue
            adr_path = reference.get("path")
            declared = reference.get("status")
            if not isinstance(adr_path, str):
                continue
            resolved = _resolve_repo_path(root, adr_path)
            if not resolved.is_file():
                report.error("adr-path-missing", task_source, f"ADR path '{adr_path}' does not exist")
                continue
            file_status = _adr_status(resolved)
            if file_status is not None and isinstance(declared, str) and file_status != declared:
                report.error(
                    "adr-status-mismatch",
                    task_source,
                    f"{adr_path} records status '{file_status}' but the task declares '{declared}'",
                )

        for requirement in task.get("requirements", []):
            if not isinstance(requirement, dict):
                continue
            source = requirement.get("source")
            if not isinstance(source, str):
                continue
            if re.match(r"^[A-Za-z0-9._-]+/", source) and " " not in source:
                if not _resolve_repo_path(root, source).exists():
                    report.error(
                        "requirement-source-missing",
                        task_source,
                        f"requirement source '{source}' does not exist",
                    )

        task_handoffs: list[tuple[str, Path, dict[str, Any]]] = []
        for reference in task.get("handoffRefs", []):
            if not isinstance(reference, dict):
                continue
            handoff_id = reference.get("handoffId")
            handoff_path = reference.get("path")
            if not isinstance(handoff_id, str):
                continue
            if handoff_id in referenced_handoffs[task_id]:
                report.error(
                    "handoff-ref-duplicate",
                    task_source,
                    f"handoffRefs lists handoff '{handoff_id}' more than once",
                )
                continue
            if handoff_id not in handoffs:
                report.error(
                    "handoff-ref-unknown",
                    task_source,
                    f"handoffRefs id '{handoff_id}' does not match any handoff example",
                )
                continue
            if not isinstance(handoff_path, str):
                continue
            resolved = _resolve_record_path(root, task_path.parent, handoff_path)
            if not resolved.is_file():
                report.error(
                    "handoff-ref-missing",
                    task_source,
                    f"handoffRefs path '{handoff_path}' does not exist",
                )
                continue
            entry = handoffs.get(handoff_id)
            if entry is None or entry[0].resolve() != resolved:
                report.error(
                    "handoff-ref-mismatch",
                    task_source,
                    f"handoffRefs path '{handoff_path}' does not contain handoffId '{handoff_id}'",
                )
                continue
            document = entry[1]
            if document.get("taskRef", {}).get("taskId") != task_id:
                report.error(
                    "handoff-task-mismatch",
                    task_source,
                    f"handoff '{handoff_id}' does not reference task '{task_id}'",
                )
            if reference.get("role") != document.get("sourceAgent"):
                report.error(
                    "handoff-role-mismatch",
                    task_source,
                    f"handoffRefs role for '{handoff_id}' must equal its sourceAgent",
                )
            referenced_handoffs[task_id].add(handoff_id)
            task_handoffs.append((handoff_id, resolved, document))

        for handoff_id, (handoff_path, handoff) in sorted(handoffs.items()):
            task_ref = handoff.get("taskRef")
            if not isinstance(task_ref, dict) or task_ref.get("taskId") != task_id:
                continue
            if handoff_id in referenced_handoffs[task_id]:
                continue
            report.error(
                "handoff-unreferenced",
                _display_path(root, handoff_path),
                f"handoff '{handoff_id}' references task '{task_id}' but is absent from its handoffRefs",
            )

        if not task_handoffs:
            continue

        # The authored handoffRefs order is the chain order; the sequence numbers
        # embedded in the handoff ids must agree with it rather than silently re-sort.
        first = task_handoffs[0][2]
        if first.get("sourceAgent") != "architect":
            report.error(
                "handoff-chain-start",
                task_source,
                "handoff chain must start with the architect role",
            )
        if contract_version == CONTRACT_VERSION_1_2_0 and first.get("predecessorHandoffId") is not None:
            report.error(
                "handoff-predecessor-mismatch",
                task_source,
                f"handoff '{task_handoffs[0][0]}' begins the chain and must not declare a predecessor",
            )

        for (previous_id, _, previous), (current_id, _, current) in zip(task_handoffs, task_handoffs[1:]):
            previous_destination = previous.get("destinationAgent")
            current_source = current.get("sourceAgent")
            previous_action = previous.get("nextAction")
            previous_next_agent = (
                previous_action.get("agent") if isinstance(previous_action, dict) else None
            )

            # A chain is unbroken when the next handoff comes from the role the
            # previous handoff was addressed to. When the previous handoff is
            # addressed to the human, the chain resumes only after that human
            # gate, so the seam is a pause for a decision rather than a break.
            seam_is_human_gate = (
                contract_version == CONTRACT_VERSION_1_2_0
                and previous_destination == "human"
                and previous_next_agent == "human"
            )
            if previous_destination != current_source and not seam_is_human_gate:
                report.error(
                    "handoff-chain-broken",
                    task_source,
                    f"handoff '{previous_id}' ends at '{previous_destination}'"
                    f" but handoff '{current_id}' starts at '{current_source}'",
                )

            transition = (previous.get("sourceAgent"), previous_destination)
            if transition not in permitted_transitions:
                report.error(
                    "handoff-transition-not-permitted",
                    task_source,
                    f"handoff '{previous_id}' records transition"
                    f" '{transition[0]}' -> '{transition[1]}', which the lifecycle does not permit",
                )

            if seam_is_human_gate:
                # Resuming after a human gate is legal, but a chain that resumes
                # while the human decision is still outstanding may not declare
                # the task approved.
                pending_gate_decision = [
                    decision
                    for decision in (previous.get("humanDecisionsRequired") or [])
                    if isinstance(decision, dict)
                    and decision.get("state") == "pending"
                    and decision.get("blocking") is True
                ]
                if pending_gate_decision and current.get("taskStatusAfter") in (
                    STATUSES_REQUIRING_HUMAN_DESTINATION
                ):
                    report.error(
                        "handoff-resume-before-decision",
                        task_source,
                        f"handoff '{current_id}' resumes the chain at '{current_source}' while the"
                        f" blocking human decision requested by '{previous_id}' is still pending,"
                        " yet declares the task ready for or past human approval",
                    )

            if contract_version == CONTRACT_VERSION_1_2_0:
                declared_predecessor = current.get("predecessorHandoffId")
                if declared_predecessor != previous_id:
                    report.error(
                        "handoff-predecessor-mismatch",
                        task_source,
                        f"handoff '{current_id}' declares predecessor"
                        f" '{declared_predecessor}' but follows '{previous_id}'",
                    )

            previous_sequence = _handoff_sequence(previous_id)
            current_sequence = _handoff_sequence(current_id)
            if (
                previous_sequence is not None
                and current_sequence is not None
                and previous_sequence >= current_sequence
            ):
                report.error(
                    "handoff-chain-order",
                    task_source,
                    f"handoff '{current_id}' must have a greater sequence number than '{previous_id}'",
                )

        # A role that reports more than once in one chain is doing a repeat
        # assessment, not repairing a broken chain. 1.2.0 requires that to be
        # declared, because an undeclared repeat is indistinguishable from a
        # duplicated or out-of-order handoff.
        seen_roles: dict[str, int] = {}
        earlier_ids: set[str] = set()
        for handoff_id, _, handoff in task_handoffs:
            role = handoff.get("sourceAgent")
            if isinstance(role, str):
                seen_roles[role] = seen_roles.get(role, 0) + 1
            if contract_version == CONTRACT_VERSION_1_2_0:
                declared = handoff.get("reassessment")
                appearance = seen_roles.get(role, 1)
                if appearance > 1 and not isinstance(declared, dict):
                    report.error(
                        "handoff-reassessment-missing",
                        task_source,
                        f"handoff '{handoff_id}' is the {appearance}th report by '{role}' in this"
                        " chain but does not declare a reassessment",
                    )
                elif isinstance(declared, dict):
                    if appearance == 1:
                        report.error(
                            "handoff-reassessment-mismatch",
                            task_source,
                            f"handoff '{handoff_id}' declares a reassessment but is the first"
                            f" report by '{role}' in this chain",
                        )
                    if declared.get("iteration") != appearance:
                        report.error(
                            "handoff-reassessment-mismatch",
                            task_source,
                            f"handoff '{handoff_id}' declares reassessment iteration"
                            f" '{declared.get('iteration')}' but is the {appearance}th report"
                            f" by '{role}'",
                        )
                    triggered_by = declared.get("triggeredByHandoffId")
                    if triggered_by not in earlier_ids:
                        report.error(
                            "handoff-reassessment-mismatch",
                            task_source,
                            f"handoff '{handoff_id}' declares reassessment triggered by"
                            f" '{triggered_by}', which is not an earlier handoff in this chain",
                        )
            earlier_ids.add(handoff_id)

        last = task_handoffs[-1][2]
        if task.get("status") in STATUSES_REQUIRING_HUMAN_DESTINATION and last.get("destinationAgent") != "human":
            report.error(
                "handoff-chain-end",
                task_source,
                "a task ready for or past human approval must end its chain at the human approver",
            )
        if last.get("taskStatusAfter") != task.get("status"):
            report.error(
                "handoff-status-mismatch",
                task_source,
                "task status must equal the final handoff's taskStatusAfter value",
            )
        if task.get("nextAction", {}).get("agent") != last.get("destinationAgent"):
            report.error(
                "handoff-next-action-mismatch",
                task_source,
                "task nextAction.agent must equal the final handoff destinationAgent",
            )
        if task.get("responsibleRole") != task.get("nextAction", {}).get("agent"):
            report.error(
                "task-responsible-role-mismatch",
                task_source,
                "task responsibleRole must equal nextAction.agent",
            )

        check_ids |= {
            item.get("checkId")
            for _, _, handoff in task_handoffs
            for item in handoff.get("validation", [])
            if isinstance(item, dict)
        }
        for _, handoff_path, handoff in task_handoffs:
            handoff_source = _display_path(root, handoff_path)
            for criterion in handoff.get("acceptanceCriteriaAddressed", []):
                if not isinstance(criterion, dict):
                    continue
                if criterion.get("criterionRef") not in criteria:
                    report.error(
                        "handoff-criterion-unknown",
                        handoff_source,
                        f"criterionRef '{criterion.get('criterionRef')}' is not declared by task '{task_id}'",
                    )
                for check_ref in criterion.get("evidenceCheckRefs", []):
                    if check_ref not in check_ids:
                        report.error(
                            "handoff-evidence-ref-unknown",
                            handoff_source,
                            f"evidenceCheckRef '{check_ref}' has no matching validation record",
                        )

            for key in ("filesReviewed", "filesModified"):
                for relative in handoff.get(key, []):
                    if not isinstance(relative, str):
                        continue
                    # A handoff may cite a sibling record by its name inside the
                    # record set, because that is how an agent addresses it.
                    if not _resolve_record_path(root, handoff_path.parent, relative).exists():
                        report.error(
                            "handoff-file-missing",
                            handoff_source,
                            f"{key} entry '{relative}' does not exist in the repository"
                            " or in the record set that contains this handoff",
                        )

    # Phase 4: a review outcome must agree with the status, findings, and next
    # action it is recorded next to. A zero exit code never implies acceptance.
    for handoff_id, (handoff_path, handoff) in sorted(handoffs.items()):
        handoff_source = _display_path(root, handoff_path)
        recommendation = handoff.get("recommendation")
        if not isinstance(recommendation, dict):
            continue
        verdict = recommendation.get("verdict")
        status = handoff.get("taskStatusAfter")
        next_action = handoff.get("nextAction")
        next_agent = next_action.get("agent") if isinstance(next_action, dict) else None
        next_stage = next_action.get("stage") if isinstance(next_action, dict) else None
        findings = handoff.get("findings")
        findings = findings if isinstance(findings, list) else []
        open_blocking = [
            finding
            for finding in findings
            if isinstance(finding, dict)
            and finding.get("blocking") is True
            and finding.get("status") == "open"
        ]
        decisions = handoff.get("humanDecisionsRequired")
        decisions = decisions if isinstance(decisions, list) else []
        pending_blocking_decision = [
            decision
            for decision in decisions
            if isinstance(decision, dict)
            and decision.get("blocking") is True
            and decision.get("state") == "pending"
        ]
        is_reviewer = handoff.get("sourceAgent") == "reviewer"
        permitted_blocked_agents = (
            {"human", "qa", "reviewer"}
            if contract_version == CONTRACT_VERSION_1_2_0
            else {"human"}
        )

        if verdict in {"accept", "accept-with-findings"} and is_reviewer:
            if status != "ready-for-human-approval":
                report.error(
                    "review-outcome-inconsistent",
                    handoff_source,
                    "an accepting review must leave the task ready-for-human-approval",
                )
            if next_agent != "human" or next_stage != HUMAN_FINAL_APPROVAL_STAGE:
                report.error(
                    "review-outcome-inconsistent",
                    handoff_source,
                    "an accepting review must hand the task back for human final approval",
                )
        elif verdict == "fix-required":
            if not is_reviewer:
                report.error(
                    "review-outcome-inconsistent",
                    handoff_source,
                    "only a reviewer handoff may request rework",
                )
            if status != "changes-requested":
                report.error(
                    "review-outcome-inconsistent",
                    handoff_source,
                    "a fix-required review must leave the task in changes-requested",
                )
            if next_agent != "builder" or next_stage != "rework":
                report.error(
                    "review-outcome-inconsistent",
                    handoff_source,
                    "a fix-required review must return the task to the builder for rework",
                )
            if not open_blocking:
                report.error(
                    "review-outcome-inconsistent",
                    handoff_source,
                    "a fix-required review must record at least one open blocking finding",
                )
        elif verdict == "reject":
            if status != "rejected" or next_agent != "human":
                report.error(
                    "review-outcome-inconsistent",
                    handoff_source,
                    "a rejecting review must leave the task rejected and the decision to the human",
                )
        elif verdict == "blocked":
            # A block states that a decision or an artifact is missing. Any role
            # can discover that, so the rule is role-agnostic: the block must
            # leave the task blocked, must not be routed to the builder as
            # though the implementation were wrong, and must be grounded in a
            # named open blocking finding or a pending blocking human decision.
            # 1.1.0 only ever allowed the human destination for a block; 1.2.0
            # also allows the block to stay with the role that must produce the
            # missing evidence, which the reviewer-blocked schema branch could
            # not express at all.
            if status != BLOCKED_STATUS:
                report.error(
                    "review-outcome-inconsistent",
                    handoff_source,
                    "a blocked handoff must leave the task blocked",
                )
            if next_stage == HUMAN_FINAL_APPROVAL_STAGE:
                report.error(
                    "review-outcome-inconsistent",
                    handoff_source,
                    "a blocked handoff must not route the task to human final approval",
                )
            if next_agent not in permitted_blocked_agents:
                report.error(
                    "review-outcome-inconsistent",
                    handoff_source,
                    "a blocked handoff must go to the human"
                    + (
                        " or back to a role that can gather the missing evidence"
                        if contract_version == CONTRACT_VERSION_1_2_0
                        else ""
                    )
                    + "; it must not be sent to the builder as rework",
                )
            if not pending_blocking_decision and not open_blocking:
                report.error(
                    "review-outcome-inconsistent",
                    handoff_source,
                    "a blocked handoff must record a pending blocking human decision or an open"
                    " blocking finding",
                )
            if contract_version == CONTRACT_VERSION_1_2_0 and next_agent != "human":
                # Staying with the agents means the same role is reporting again.
                if not isinstance(handoff.get("reassessment"), dict):
                    report.error(
                        "review-outcome-inconsistent",
                        handoff_source,
                        "a blocked handoff kept inside the agent chain must declare a reassessment",
                    )
                if not open_blocking:
                    report.error(
                        "review-outcome-inconsistent",
                        handoff_source,
                        "a blocked handoff kept inside the agent chain must name the open blocking"
                        " finding that justifies it",
                    )

        if open_blocking and status == "ready-for-human-approval":
            report.error(
                "blocking-finding-blocks-approval",
                handoff_source,
                "a task with an open blocking finding is not ready for human approval",
            )

    if not tasks:
        report.error(
            "examples-empty",
            _display_path(root, directory),
            "at least one task record is required in a scanned record set",
        )


def check_contracts(root: Path, report: Report, records_roots: Iterable[Path] = ()) -> None:
    schemas: dict[str, dict[str, Any]] = {}
    for kind, (relative, _) in SCHEMA_FILES.items():
        path = root / relative
        if not path.is_file():
            continue
        document = _load_json(path, root, report)
        if not isinstance(document, dict):
            continue
        if document.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
            report.error(
                "schema-dialect",
                relative,
                "schemas must declare the JSON Schema 2020-12 dialect",
            )
        _build_validator(document, relative, report)
        schemas[kind] = document

    check_shared_semantics(root, schemas, report)
    check_examples(root, schemas, report, records_roots)
    report.checks += 1


def _check_hook_check(
    check: object, source: str, report: Report, capabilities: set[str], index: int
) -> None:
    label = f"check {index + 1}"
    if not isinstance(check, dict):
        report.error("hook-check-not-object", source, f"{label} must be an object")
        return
    unexpected = sorted(set(check) - ALLOWED_HOOK_CHECK_KEYS)
    if unexpected:
        report.error(
            "hook-check-key",
            source,
            f"{label} has unsupported key(s): {', '.join(unexpected)}",
        )
    kind = check.get("kind")
    if kind not in HOOK_CHECK_CAPABILITIES:
        report.error(
            "hook-check-kind",
            source,
            f"{label} kind '{kind}' is not one of {', '.join(HOOK_CHECK_CAPABILITIES)}",
        )
        return
    for capability in HOOK_CHECK_CAPABILITIES[kind]:
        if capability not in capabilities:
            report.error(
                "hook-check-capability",
                source,
                f"{label} ('{kind}') needs the capability '{capability}', which the "
                "hook does not declare",
            )

    reason = check.get("reason", "")
    if kind != "note" and (not isinstance(reason, str) or len(reason.strip()) < 20):
        report.error(
            "hook-check-reason",
            source,
            f"{label} ('{kind}') needs a reason of at least 20 characters",
        )

    if kind == "note" and not str(check.get("text", "")).strip():
        report.error("hook-check-text", source, f"{label} ('note') needs text")
    if kind == "task-status" and not (
        check.get("allowed_statuses") or check.get("forbidden_statuses")
    ):
        report.error(
            "hook-check-status",
            source,
            f"{label} ('task-status') needs allowed_statuses or forbidden_statuses",
        )
    if kind == "forbidden-changed-paths":
        patterns = check.get("path_patterns")
        if not isinstance(patterns, list) or not patterns:
            report.error(
                "hook-check-paths", source, f"{label} needs a non-empty path_patterns list"
            )
        else:
            for value in patterns:
                if not isinstance(value, str) or not value:
                    report.error("hook-check-pattern", source, f"{label} has an empty pattern")
                    continue
                try:
                    re.compile(value)
                except re.error as error:
                    report.error(
                        "hook-check-pattern",
                        source,
                        f"{label} pattern '{value}' is not a valid regular expression: {error}",
                    )
    if kind == "required-evidence":
        evidence = check.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            report.error(
                "hook-check-evidence", source, f"{label} needs a non-empty evidence list"
            )
        else:
            for value in evidence:
                if value not in HOOK_EVIDENCE_KINDS:
                    report.error(
                        "hook-check-evidence",
                        source,
                        f"{label} evidence '{value}' is not one of "
                        f"{', '.join(HOOK_EVIDENCE_KINDS)}",
                    )
    if kind == "required-specialist-verdict":
        specialists = check.get("specialists")
        if not isinstance(specialists, list) or not specialists:
            report.error(
                "hook-check-specialists",
                source,
                f"{label} needs a non-empty specialists list",
            )
        else:
            for value in specialists:
                if not isinstance(value, str) or not HOOK_NAME_PATTERN.fullmatch(value):
                    report.error(
                        "hook-check-specialists",
                        source,
                        f"{label} specialist '{value}' is not a catalog name",
                    )


def check_hooks(root: Path, report: Report) -> None:
    """Validate the trusted lifecycle-hook registry, when one exists.

    The rules mirror app/schemas/lifecycle_hooks.py: the same events, capabilities,
    checks, and modes. A deployment with no hooks is valid and is the default, so a
    missing directory is not an error.
    """

    hooks_dir = root / ".github" / "hooks"
    if not hooks_dir.is_dir():
        return

    seen_names: dict[str, str] = {}
    per_event: dict[str, int] = {event: 0 for event in HOOK_EVENT_TYPES}
    for path in sorted(hooks_dir.glob("*.json")):
        source = _display_path(root, path)
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except OSError as error:
            report.error(
                "hook-unreadable", source, f"hook definition could not be read: {error}"
            )
            continue
        except json.JSONDecodeError as error:
            report.error(
                "hook-invalid-json",
                source,
                f"hook definition is not valid JSON: {error}",
            )
            continue
        if not isinstance(document, dict):
            report.error(
                "hook-not-object", source, "a hook definition must be one JSON object"
            )
            continue

        unexpected = sorted(set(document) - ALLOWED_HOOK_KEYS)
        if unexpected:
            report.error(
                "hook-frontmatter-key",
                source,
                f"unsupported hook key(s): {', '.join(unexpected)}",
            )

        if document.get("hook_version") != HOOK_SCHEMA_VERSION:
            report.error(
                "hook-schema-version",
                source,
                f"hook_version must be {HOOK_SCHEMA_VERSION}",
            )

        name = _require_text(document, "name", source, report)
        if name is not None:
            if not HOOK_NAME_PATTERN.fullmatch(name):
                report.error(
                    "hook-name-pattern",
                    source,
                    f"hook name '{name}' must be lower-case kebab-case",
                )
            if name in seen_names:
                report.error(
                    "hook-name-duplicate",
                    source,
                    f"hook name '{name}' is already declared in {seen_names[name]}",
                )
            else:
                seen_names[name] = source

        version = _require_text(document, "version", source, report)
        if version is not None and not HOOK_VERSION_PATTERN.fullmatch(version):
            report.error(
                "hook-version-pattern",
                source,
                f"hook version '{version}' must look like 1, 1.2, or 1.2.3",
            )
        _require_text(document, "label", source, report)
        description = _require_text(document, "description", source, report)
        if description is not None and len(description) < 20:
            report.error(
                "hook-description",
                source,
                "description must be at least 20 characters",
            )

        priority = document.get("priority")
        if (
            not isinstance(priority, int)
            or isinstance(priority, bool)
            or not 0 <= priority <= 10000
        ):
            report.error(
                "hook-priority", source, "priority must be an integer from 0 to 10000"
            )

        mode = document.get("mode")
        if mode not in HOOK_MODES:
            report.error("hook-mode", source, f"mode must be one of {', '.join(HOOK_MODES)}")

        events = _parse_list_value(document.get("events"))
        if not events:
            report.error("hook-events", source, "a hook must declare at least one event")
        else:
            seen_events: set[str] = set()
            for event in events:
                if event not in HOOK_EVENT_TYPES:
                    report.error(
                        "hook-event-unknown",
                        source,
                        f"'{event}' is not one of {', '.join(HOOK_EVENT_TYPES)}",
                    )
                    continue
                if event in seen_events:
                    report.error(
                        "hook-event-duplicate", source, f"event '{event}' is repeated"
                    )
                seen_events.add(event)
                per_event[event] += 1

        capabilities = _parse_list_value(document.get("capabilities"))
        for capability in capabilities:
            if capability not in HOOK_CAPABILITIES:
                report.error(
                    "hook-capability-unknown",
                    source,
                    f"capability '{capability}' is not one of "
                    f"{', '.join(HOOK_CAPABILITIES)}",
                )

        checks = document.get("checks")
        if not isinstance(checks, list) or not checks:
            report.error(
                "hook-checks-missing", source, "a hook must declare at least one check"
            )
            continue
        if len(checks) > MAX_CHECKS_PER_HOOK:
            report.error(
                "hook-checks-excessive",
                source,
                f"a hook may declare at most {MAX_CHECKS_PER_HOOK} checks",
            )
        for index, check in enumerate(checks):
            _check_hook_check(check, source, report, set(capabilities), index)

    for event, count in per_event.items():
        if count > MAX_HOOKS_PER_EVENT:
            report.error(
                "hook-event-crowded",
                ".github/hooks",
                f"{count} hooks are registered for '{event}', which exceeds "
                f"{MAX_HOOKS_PER_EVENT}",
            )

    report.checks += 1


def check_skill_bindings(
    root: Path, report: Report, inventory: AgentInventory | None = None
) -> None:
    """Check both directions of the role-to-skill binding.

    An agent definition's skills list is the baseline the runtime always loads, and
    a skill's own roles list states who a procedure was written for. The two must
    agree: a role may not load a skill written for someone else, and every skill
    in the trusted root must be reachable - either declared by a role definition,
    or selectable by the contextual layer (a role it names, plus the stage and
    applicability metadata that let the selection choose it).
    """

    agents_dir = root / ".github" / "agents"
    skills_dir = root / ".github" / "skills"
    if not agents_dir.is_dir() or not skills_dir.is_dir():
        return
    active = inventory or _empty_inventory()

    skill_roles: dict[str, set[str]] = {}
    selectable: dict[str, bool] = {}
    for skill_dir in sorted(entry for entry in skills_dir.iterdir() if entry.is_dir()):
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.is_file():
            continue
        text = _read_text(skill_file, report)
        if text is None:
            continue
        frontmatter = parse_frontmatter(
            text, _display_path(root, skill_file), report
        )
        if frontmatter is None:
            continue
        skill_roles[skill_dir.name] = set(_parse_list_value(frontmatter.get("roles")))
        applicability = any(
            _parse_list_value(frontmatter.get(key))
            for key in ("task_types", "areas", "implications", "risk_levels")
        )
        required = frontmatter.get("required", False) is True
        selectable[skill_dir.name] = required or bool(applicability)

    claimed: set[str] = set()
    for path in sorted(agents_dir.glob("*.agent.md")):
        source = _display_path(root, path)
        text = _read_text(path, report)
        if text is None:
            continue
        frontmatter = parse_frontmatter(text, source, report)
        if frontmatter is None:
            continue
        role = active.role_for_file(path.name)
        if role is None:
            continue
        for name in _parse_list_value(frontmatter.get("skills")):
            claimed.add(name)
            if name not in skill_roles:
                report.error(
                    "agent-skill-unknown",
                    source,
                    f"declared skill '{name}' has no .github/skills/{name}/SKILL.md",
                )
                continue
            if role not in skill_roles[name]:
                report.error(
                    "agent-skill-role-mismatch",
                    source,
                    f"skill '{name}' does not declare the '{role}' role "
                    f"(it declares: {', '.join(sorted(skill_roles[name])) or 'none'})",
                )

    for name in sorted(skill_roles):
        if name in claimed:
            continue
        if not selectable.get(name):
            report.error(
                "skill-unused",
                f".github/skills/{name}/SKILL.md",
                "no role definition declares this skill and it declares no "
                "applicability, so the contextual selection can never choose it",
            )
            continue
        if not skill_roles[name].intersection(active.roles):
            report.error(
                "skill-unreachable",
                f".github/skills/{name}/SKILL.md",
                "the skill names no canonical role, so no execution can select it",
            )

    report.checks += 1


def validate(root: Path, records_roots: Iterable[Path] = ()) -> Report:
    """Validate one repository root.

    The catalog is read first: it declares every review specialist, and the
    inventory built from it is what the agent, skill, and binding checks use. A
    registered reviewer is therefore validated without an edit to this script.
    """

    report = Report()
    specialists = load_catalog_inventory(root, report)
    inventory = build_inventory(specialists)
    check_structure(root, report, inventory)
    check_agents(root, report, inventory)
    check_skills(root, report, inventory)
    check_skill_bindings(root, report, inventory)
    check_hooks(root, report)
    check_markdown_links(root, report)
    check_contracts(root, report, records_roots)
    return report


def _print_report(report: Report, as_json: bool) -> None:
    if as_json:
        payload = {
            "checks": report.checks,
            "passed": report.passed(),
            "scanRoots": report.scan_roots,
            "errors": [finding.__dict__ for finding in report.errors],
            "warnings": [finding.__dict__ for finding in report.warnings],
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return

    for root in report.scan_roots:
        print(f"SCAN  {root}")
    for finding in report.warnings:
        print(f"WARN  [{finding.code}] {finding.path}: {finding.message}")
    for finding in report.errors:
        print(f"ERROR [{finding.code}] {finding.path}: {finding.message}")
    print(
        f"factory-validation: {report.checks} check groups, "
        f"{len(report.errors)} error(s), {len(report.warnings)} warning(s)"
    )


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only validation of LoomSphere agents, skills, and execution contracts.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root to validate (default: the parent of this script's directory).",
    )
    parser.add_argument(
        "--records-root",
        type=Path,
        action="append",
        default=[],
        metavar="DIR",
        help=(
            "Additional directory of task and handoff records to validate with the same "
            "rules as the bundled examples. Repeatable. A validation pass only ever "
            "describes the roots printed as SCAN; records outside them are not covered."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable results.")
    args = parser.parse_args(list(argv) if argv is not None else None)

    root = args.repo_root.resolve()
    if not root.is_dir():
        print(f"repo root does not exist: {root}", file=sys.stderr)
        return 2

    records_roots: list[Path] = []
    for raw in args.records_root:
        candidate = raw if raw.is_absolute() else (Path.cwd() / raw)
        candidate = candidate.resolve()
        if not candidate.is_dir():
            print(f"records root does not exist: {candidate}", file=sys.stderr)
            return 2
        records_roots.append(candidate)

    report = validate(root, records_roots)
    _print_report(report, args.json)
    return 0 if report.passed() else 1


if __name__ == "__main__":
    raise SystemExit(main())
