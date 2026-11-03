"""Trusted skill loading for agent execution.

A skill is a procedure an agent follows. Skills live in the factory's own
checkout under `.github/skills/<name>/SKILL.md`. Two things bind a skill to an
agent:

* the role's agent definition declares the procedures that role must always
  carry (`skills: [safe-implementation, ...]` in
  `.github/agents/loomsphere-builder.agent.md`);
* each skill declares, in its own frontmatter, which roles may use it
  (`roles`), which SDLC stages it belongs to (`stages`), when it applies
  (`task_types`, `areas`, `implications`, `risk_levels`), whether it is
  required whenever its role runs (`required`), and which capabilities it
  assumes (`requires_tools`, `requires_skills`).

The declaration in the agent definition is the *baseline*: those skills are
loaded for every execution of that role. Everything else is discovered from the
trusted root and selected contextually by `loomsphere_harness.skill_selection`, so a
new procedure is added by adding a reviewed file here, not by editing the
orchestrator.

Three boundaries make this trustworthy rather than decorative:

* **The source is the trusted checkout, never the agent workspace.** An agent
  worktree is a copy of the repository that the Builder can write to, so a
  `.github/skills` file inside it is Builder-controlled content. Loading only
  reads from the configured factory root, and every path is resolved and checked
  to be inside it before a byte is read. A skill the workspace tries to redefine
  is never consulted.
* **Loading fails closed.** A declared skill that is missing, unreadable, has a
  name that disagrees with its directory, or does not declare the requesting role
  raises instead of being skipped. An agent running without its procedure is a
  different agent, and silently reclassifying the run would hide that.
* **Selection can never widen authority.** Eligibility is read from the trusted
  root only. Neither the engineering request, the Intake assessment, the route,
  nor the agent's own instructions can make a skill eligible: they can only make
  an already-eligible optional skill applicable. Skills are text added to the
  prompt, so nothing in a skill file can widen a role's tools, its read-only
  workspace mount, the sandbox, or any approval gate.

The digest recorded for each skill is the digest of the exact text the agent was
given, so a later edit never rewrites history and a change between dispatch and
execution is detectable.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

#: Skill names appear in prompts, evidence rows, and validator reports, so the
#: grammar is deliberately narrower than "any directory name".
SKILL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+){0,7}$")

#: The definition file a skill must provide inside its directory.
SKILL_FILE_NAME = "SKILL.md"

#: A skill is procedure text, not an appendix. Both bounds are enforced so a
#: pathological file cannot silently consume the whole prompt budget.
MAX_SKILL_BYTES = 64 * 1024
MAX_SKILLS_BYTES_PER_PROMPT = 192 * 1024

#: Relative form recorded in evidence: portable between deployments and free of
#: host-specific paths.
SKILL_SOURCE_PREFIX = ".github/skills"

#: The SDLC stages a skill may declare. The first five are the fixed factory
#: roles; a specialist stage is the catalog stage name (`security-review`), so a
#: skill attached to a catalog specialist needs no entry in this tuple.
SKILL_STAGES: tuple[str, ...] = (
    "intake",
    "architecture",
    "implementation",
    "validation",
    "review",
)

#: Applicability vocabularies. A skill may only declare values from these sets,
#: so a typo is a load failure rather than a rule that silently never matches.
TASK_TYPE_VALUES: tuple[str, ...] = (
    "feature",
    "bugfix",
    "refactor",
    "documentation",
    "security",
    "ui",
    "database",
    "other",
)
AREA_VALUES: tuple[str, ...] = (
    "backend",
    "frontend",
    "database",
    "infrastructure",
    "documentation",
    "tests",
    "shared",
    "unknown",
)
IMPLICATION_VALUES: tuple[str, ...] = ("security", "ux_ui", "database", "architecture")
RISK_LEVEL_VALUES: tuple[str, ...] = ("low", "medium", "high")

#: Tools a skill's procedure may assume. The selector refuses to select a skill
#: whose prerequisites the agent is not authorized to satisfy.
TOOL_VALUES: tuple[str, ...] = ("read", "search", "edit", "execute", "todo")

#: The only frontmatter keys a skill may declare. An unknown key is refused: a
#: rule the loader does not read must never look like a rule that applies.
SKILL_KEYS: frozenset[str] = frozenset(
    {
        "name",
        "description",
        "roles",
        "stages",
        "required",
        "task_types",
        "areas",
        "implications",
        "risk_levels",
        "requires_tools",
        "requires_skills",
    }
)

SKILL_BINDING_MISSING = "skill-binding-missing"
SKILL_BINDING_INVALID = "skill-binding-invalid"
SKILL_NOT_FOUND = "skill-not-found"
SKILL_UNSAFE_PATH = "skill-unsafe-path"
SKILL_TOO_LARGE = "skill-too-large"
SKILL_METADATA_INVALID = "skill-metadata-invalid"
SKILL_ROLE_MISMATCH = "skill-role-mismatch"
SKILL_BUDGET_EXCEEDED = "skill-budget-exceeded"
SKILL_PREREQUISITE_MISSING = "skill-prerequisite-missing"
SKILL_STAGE_MISMATCH = "skill-stage-mismatch"

#: Where a selected skill came from. `declared` is the role definition's own
#: list; `required` is a discovered skill that is always active for its role;
#: `contextual` is a discovered skill whose applicability matched the recorded
#: task evidence.
ORIGIN_DECLARED = "declared"
ORIGIN_REQUIRED = "required"
ORIGIN_CONTEXTUAL = "contextual"
SKILL_ORIGINS: tuple[str, ...] = (ORIGIN_DECLARED, ORIGIN_REQUIRED, ORIGIN_CONTEXTUAL)


class SkillLoadError(RuntimeError):
    """A required skill could not be supplied to an agent."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class SkillApplicability:
    """When an optional skill applies, as declared by the skill itself.

    Each family is an any-of list, and the families are combined as any-of as
    well: a skill applies as soon as one declared fact matches the recorded
    context. That keeps the rule explainable ("matched: implication database")
    and prevents an over-specified skill from silently never matching.
    """

    task_types: tuple[str, ...] = ()
    areas: tuple[str, ...] = ()
    implications: tuple[str, ...] = ()
    risk_levels: tuple[str, ...] = ()

    @property
    def declared(self) -> bool:
        return bool(
            self.task_types or self.areas or self.implications or self.risk_levels
        )

    def as_document(self) -> dict[str, list[str]]:
        return {
            "task_types": list(self.task_types),
            "areas": list(self.areas),
            "implications": list(self.implications),
            "risk_levels": list(self.risk_levels),
        }


@dataclass(frozen=True)
class SkillContextView:
    """The slice of a selection context the applicability rules read.

    Kept as its own value so this module has no dependency on the database or the
    selection service, and so the matching rule can be unit-tested directly.
    """

    task_types: tuple[str, ...] = ()
    areas: tuple[str, ...] = ()
    implications: tuple[str, ...] = ()
    risk_levels: tuple[str, ...] = ()
    stage: str = ""


@dataclass(frozen=True)
class SkillDocument:
    """One skill as it was supplied to a single agent execution."""

    name: str
    role: str
    description: str
    roles: tuple[str, ...]
    source: str
    body: str
    digest: str
    stages: tuple[str, ...] = ()
    required: bool = False
    applicability: SkillApplicability = field(default_factory=SkillApplicability)
    requires_tools: tuple[str, ...] = ()
    requires_skills: tuple[str, ...] = ()
    #: Why this skill is in this prompt. Filled by the selector; empty when the
    #: document was loaded statically.
    selected_by: tuple[str, ...] = ()
    origin: str = ORIGIN_DECLARED

    def evidence(self) -> dict[str, object]:
        """The snapshot recorded on the agent run."""

        return {
            "name": self.name,
            "role": self.role,
            "source": self.source,
            "digest": self.digest,
            "description": self.description,
            "roles": list(self.roles),
            "content_chars": len(self.body),
            "stages": list(self.stages),
            "required": self.required,
            "origin": self.origin,
            "selected_by": list(self.selected_by),
            "applicability": self.applicability.as_document(),
            "requires_tools": list(self.requires_tools),
            "requires_skills": list(self.requires_skills),
        }

    def with_selection(self, *, origin: str, reasons: Iterable[str]) -> SkillDocument:
        """A copy carrying why it was selected, for the prompt and the evidence."""

        return SkillDocument(
            name=self.name,
            role=self.role,
            description=self.description,
            roles=self.roles,
            source=self.source,
            body=self.body,
            digest=self.digest,
            stages=self.stages,
            required=self.required,
            applicability=self.applicability,
            requires_tools=self.requires_tools,
            requires_skills=self.requires_skills,
            selected_by=tuple(reasons),
            origin=origin,
        )


@dataclass(frozen=True)
class Frontmatter:
    values: dict[str, str]
    body: str


def _quote(value: str) -> str:
    return value.strip().strip("'\"“”").strip()


def parse_frontmatter(text: str) -> Frontmatter:
    """Read the leading `---` block of a definition file.

    The supported subset is one `key: value` pair per line, where a value may be
    a scalar or a flow list (`[a, b]`). That is all the canonical definitions
    use; a metadata format the loader cannot read is refused rather than guessed
    at.
    """

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return Frontmatter(values={}, body=text)
    values: dict[str, str] = {}
    for index in range(1, len(lines)):
        line = lines[index]
        if line.strip() == "---":
            body = "\n".join(lines[index + 1 :]).strip()
            return Frontmatter(values=values, body=body)
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition(":")
        if not separator or not key.strip():
            raise SkillLoadError(
                SKILL_METADATA_INVALID,
                f"frontmatter line {index + 1} is not a 'key: value' pair.",
            )
        values[key.strip()] = value.strip()
    # An unterminated block is not metadata; treat the whole file as the body so a
    # markdown heading cannot silently become a key.
    return Frontmatter(values={}, body=text)


def parse_list(value: str) -> tuple[str, ...]:
    """Parse a flow list such as `[read, search]` into its items."""

    stripped = value.strip()
    if not stripped.startswith("[") or not stripped.endswith("]"):
        return (_quote(stripped),) if stripped else ()
    inner = stripped[1:-1]
    return tuple(item for item in (_quote(part) for part in inner.split(",")) if item)


def parse_flag(value: str | None, *, name: str, source: str) -> bool:
    """Parse a boolean frontmatter value, refusing anything ambiguous."""

    if value is None:
        return False
    normalized = _quote(value).lower()
    if normalized in {"true", "yes"}:
        return True
    if normalized in {"false", "no"}:
        return False
    raise SkillLoadError(
        SKILL_METADATA_INVALID,
        f"{source} declares '{name}: {value}'; only true or false is accepted.",
    )


def declared_skills(definition_path: str | Path) -> tuple[str, ...]:
    """The skills a role's definition declares, in declaration order."""

    path = Path(definition_path).expanduser()
    if not path.is_file():
        raise SkillLoadError(
            SKILL_BINDING_MISSING,
            "The agent definition that declares this role's skills is not available.",
        )
    frontmatter = parse_frontmatter(path.read_text(encoding="utf-8"))
    raw = frontmatter.values.get("skills")
    if raw is None:
        raise SkillLoadError(
            SKILL_BINDING_MISSING,
            f"The agent definition {path.name} does not declare any skills.",
        )
    names = parse_list(raw)
    if not names:
        raise SkillLoadError(
            SKILL_BINDING_INVALID,
            f"The agent definition {path.name} declares an empty skills list.",
        )
    for name in names:
        if not SKILL_NAME_PATTERN.fullmatch(name):
            raise SkillLoadError(
                SKILL_BINDING_INVALID,
                f"The agent definition {path.name} declares an unusable skill name.",
            )
    if len(set(names)) != len(names):
        raise SkillLoadError(
            SKILL_BINDING_INVALID,
            f"The agent definition {path.name} declares a skill twice.",
        )
    return names


def declared_tools(definition_path: str | Path) -> tuple[str, ...]:
    """The tools a role's definition authorizes, or () when it declares none."""

    path = Path(definition_path).expanduser()
    if not path.is_file():
        return ()
    frontmatter = parse_frontmatter(path.read_text(encoding="utf-8"))
    return parse_list(frontmatter.values.get("tools", ""))


def _resolve_skill_file(skills_root: Path, name: str) -> tuple[Path, str]:
    """Resolve one skill file, refusing anything outside the trusted root."""

    if not SKILL_NAME_PATTERN.fullmatch(name):
        raise SkillLoadError(
            SKILL_BINDING_INVALID, f"'{name}' is not a valid skill name."
        )
    root = skills_root.expanduser().resolve()
    if not root.is_dir():
        raise SkillLoadError(
            SKILL_NOT_FOUND,
            "The trusted skill directory is not available in this deployment.",
        )

    candidate = root / name / SKILL_FILE_NAME
    try:
        resolved = candidate.resolve(strict=False)
    except OSError as error:  # pragma: no cover - defensive
        raise SkillLoadError(
            SKILL_UNSAFE_PATH, f"Skill '{name}' could not be resolved: {error}"
        ) from error

    # `is_relative_to` also rejects a symlinked skill directory that escapes the
    # root, which a plain string prefix check would allow.
    if resolved == root or not resolved.is_relative_to(root):
        raise SkillLoadError(
            SKILL_UNSAFE_PATH,
            f"Skill '{name}' resolves outside the trusted skill directory.",
        )
    if not resolved.is_file():
        raise SkillLoadError(
            SKILL_NOT_FOUND, f"Skill '{name}' is not available in the trusted factory."
        )
    return resolved, f"{SKILL_SOURCE_PREFIX}/{name}/{SKILL_FILE_NAME}"


def _vocabulary(
    values: tuple[str, ...],
    *,
    label: str,
    allowed: Iterable[str],
    name: str,
) -> tuple[str, ...]:
    """Validate one declared vocabulary list, refusing duplicates and strays."""

    permitted = tuple(allowed)
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise SkillLoadError(
                SKILL_METADATA_INVALID,
                f"Skill '{name}' declares {label} '{value}' twice.",
            )
        if value not in permitted:
            raise SkillLoadError(
                SKILL_METADATA_INVALID,
                f"Skill '{name}' declares {label} '{value}', which is not one of "
                f"{', '.join(permitted)}.",
            )
        seen.add(value)
    return values


def _read_skill(name: str, path: Path, source: str) -> SkillDocument:
    """Read and validate one skill file, whatever role is asking for it."""

    try:
        raw = path.read_bytes()
    except OSError as error:
        raise SkillLoadError(
            SKILL_NOT_FOUND, f"Skill '{name}' could not be read: {error}"
        ) from error
    if len(raw) > MAX_SKILL_BYTES:
        raise SkillLoadError(
            SKILL_TOO_LARGE,
            f"Skill '{name}' exceeds the {MAX_SKILL_BYTES} byte limit.",
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SkillLoadError(
            SKILL_METADATA_INVALID, f"Skill '{name}' is not valid UTF-8."
        ) from error

    frontmatter = parse_frontmatter(text)
    unknown = sorted(set(frontmatter.values) - SKILL_KEYS)
    if unknown:
        raise SkillLoadError(
            SKILL_METADATA_INVALID,
            f"Skill '{name}' declares unsupported metadata key(s): "
            f"{', '.join(unknown)}.",
        )
    declared_name = _quote(frontmatter.values.get("name", ""))
    if declared_name != name:
        raise SkillLoadError(
            SKILL_METADATA_INVALID,
            f"Skill file for '{name}' declares name '{declared_name}'.",
        )
    description = _quote(frontmatter.values.get("description", ""))
    if len(description) < 20:
        raise SkillLoadError(
            SKILL_METADATA_INVALID,
            f"Skill '{name}' needs a description of at least 20 characters.",
        )
    roles = parse_list(frontmatter.values.get("roles", ""))
    if not roles:
        raise SkillLoadError(
            SKILL_METADATA_INVALID,
            f"Skill '{name}' does not declare the agent roles that may use it.",
        )
    for role in roles:
        if not SKILL_NAME_PATTERN.fullmatch(role):
            raise SkillLoadError(
                SKILL_METADATA_INVALID,
                f"Skill '{name}' declares an unusable role name '{role}'.",
            )
    if len(set(roles)) != len(roles):
        raise SkillLoadError(
            SKILL_METADATA_INVALID, f"Skill '{name}' declares a role twice."
        )
    if not frontmatter.body.strip():
        raise SkillLoadError(
            SKILL_METADATA_INVALID, f"Skill '{name}' has no procedure body."
        )

    stages = _stage_values(name, parse_list(frontmatter.values.get("stages", "")))
    required = parse_flag(
        frontmatter.values.get("required"), name=name, source=f"Skill '{name}'"
    )
    if required and not stages:
        raise SkillLoadError(
            SKILL_METADATA_INVALID,
            f"Skill '{name}' is required but does not declare the stages it is "
            "required in.",
        )
    applicability = SkillApplicability(
        task_types=_vocabulary(
            parse_list(frontmatter.values.get("task_types", "")),
            label="task type",
            allowed=TASK_TYPE_VALUES,
            name=name,
        ),
        areas=_vocabulary(
            parse_list(frontmatter.values.get("areas", "")),
            label="area",
            allowed=AREA_VALUES,
            name=name,
        ),
        implications=_vocabulary(
            parse_list(frontmatter.values.get("implications", "")),
            label="implication",
            allowed=IMPLICATION_VALUES,
            name=name,
        ),
        risk_levels=_vocabulary(
            parse_list(frontmatter.values.get("risk_levels", "")),
            label="risk level",
            allowed=RISK_LEVEL_VALUES,
            name=name,
        ),
    )
    requires_tools = _vocabulary(
        parse_list(frontmatter.values.get("requires_tools", "")),
        label="tool prerequisite",
        allowed=TOOL_VALUES,
        name=name,
    )
    requires_skills = parse_list(frontmatter.values.get("requires_skills", ""))
    for prerequisite in requires_skills:
        if not SKILL_NAME_PATTERN.fullmatch(prerequisite):
            raise SkillLoadError(
                SKILL_METADATA_INVALID,
                f"Skill '{name}' declares an unusable skill prerequisite.",
            )

    body = frontmatter.body.strip()
    return SkillDocument(
        name=name,
        role="",
        description=description,
        roles=roles,
        source=source,
        body=body,
        digest=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        stages=stages,
        required=required,
        applicability=applicability,
        requires_tools=requires_tools,
        requires_skills=requires_skills,
    )


def _stage_values(name: str, values: tuple[str, ...]) -> tuple[str, ...]:
    """Validate declared stages: a fixed factory stage or a catalog stage name.

    Specialist stages are declared by the trusted catalog (for example
    "security-review"), so the loader accepts any lower-case kebab-case stage
    name instead of importing the catalog. That keeps loading free of a
    dependency cycle and still refuses a typo that could never match.
    """

    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise SkillLoadError(
                SKILL_METADATA_INVALID,
                f"Skill '{name}' declares stage '{value}' twice.",
            )
        if value not in SKILL_STAGES and not SKILL_NAME_PATTERN.fullmatch(value):
            raise SkillLoadError(
                SKILL_METADATA_INVALID,
                f"Skill '{name}' declares stage '{value}', which is neither a "
                f"factory stage ({', '.join(SKILL_STAGES)}) nor a catalog stage name.",
            )
        seen.add(value)
    return values


def load_skill(name: str, *, role: str, skills_root: str | Path) -> SkillDocument:
    """Load one skill for one role, validating metadata and role compatibility."""

    path, source = _resolve_skill_file(Path(skills_root), name)
    document = _read_skill(name, path, source)
    if not role:
        return document
    if role not in document.roles:
        raise SkillLoadError(
            SKILL_ROLE_MISMATCH,
            f"Skill '{name}' is not declared for the '{role}' role.",
        )
    return SkillDocument(
        name=document.name,
        role=role,
        description=document.description,
        roles=document.roles,
        source=document.source,
        body=document.body,
        digest=document.digest,
        stages=document.stages,
        required=document.required,
        applicability=document.applicability,
        requires_tools=document.requires_tools,
        requires_skills=document.requires_skills,
        origin=ORIGIN_DECLARED,
    )


def load_role_skills(
    *,
    role: str,
    definition_path: str | Path,
    skills_root: str | Path,
) -> tuple[SkillDocument, ...]:
    """Load every skill a role's definition declares, in declaration order.

    This is the frozen baseline of a role: the procedures its own definition
    pins. Any failure raises: a partially loaded skill set would let an agent run
    with a procedure that was never reviewed for it. Contextual selection layers
    optional skills on top of this set; it never removes one.
    """

    names = declared_skills(definition_path)
    documents = tuple(
        load_skill(name, role=role, skills_root=skills_root) for name in names
    )
    total = sum(len(document.body.encode("utf-8")) for document in documents)
    if total > MAX_SKILLS_BYTES_PER_PROMPT:
        raise SkillLoadError(
            SKILL_BUDGET_EXCEEDED,
            f"The declared skills for '{role}' exceed "
            f"{MAX_SKILLS_BYTES_PER_PROMPT} bytes in total.",
        )
    return documents


def discover_skills(
    skills_root: str | Path, *, exclude: Iterable[str] = ()
) -> tuple[SkillDocument, ...]:
    """Read the metadata of every skill in the trusted root, sorted by name.

    Discovery is metadata-only: a skill is a *candidate* here. It is selected
    later, and only if its role, stage, prerequisites, and applicability match
    the recorded context. A malformed candidate raises, because a skill file the
    loader cannot read is a defect in the trusted root, not a candidate.
    """

    root = Path(skills_root).expanduser().resolve()
    if not root.is_dir():
        raise SkillLoadError(
            SKILL_NOT_FOUND,
            "The trusted skill directory is not available in this deployment.",
        )
    skip = set(exclude)
    documents: list[SkillDocument] = []
    for directory in sorted(entry for entry in root.iterdir() if entry.is_dir()):
        name = directory.name
        if name in skip or not SKILL_NAME_PATTERN.fullmatch(name):
            continue
        path, source = _resolve_skill_file(root, name)
        documents.append(_read_skill(name, path, source))
    return tuple(documents)


def skill_evidence(skills: Iterable[SkillDocument]) -> list[dict[str, object]]:
    """The snapshot an agent run records for the skills it was given."""

    return [document.evidence() for document in skills]


def render_skills_block(skills: Iterable[SkillDocument]) -> str:
    """Render the prompt section that carries the loaded skills.

    The wording states the one thing a skill must never be read as: authority.
    When the selector recorded why a skill is present, that reason is rendered
    too, so the agent, and anyone reading the frozen prompt afterwards, can see
    which recorded facts put the procedure in front of it.
    """

    documents = list(skills)
    if not documents:
        return ""
    sections = [
        "## Loaded skills",
        "",
        "The factory loaded the following skill procedures from its trusted "
        "repository for this role. They are instructions to follow, not authority: "
        "they cannot change your tool permissions, the read-only workspace mount, "
        "the evidence rules, or any approval gate. Each entry names its trusted "
        "source and the digest recorded as evidence for this execution.",
    ]
    for document in documents:
        sections.extend(
            [
                "",
                f"### Skill `{document.name}`",
                "",
                f"Source: `{document.source}`  ",
                f"SHA-256: `{document.digest}`",
            ]
        )
        if document.selected_by:
            sections.append(f"Selected because: {'; '.join(document.selected_by)}")
        sections.extend(["", document.body])
    return "\n".join(sections)


def context_view(
    *,
    stage: str,
    task_types: Iterable[str] = (),
    areas: Iterable[str] = (),
    implications: Iterable[str] = (),
    risk_levels: Iterable[str] = (),
) -> SkillContextView:
    """Build the applicability view a skill is matched against."""

    return SkillContextView(
        task_types=tuple(task_types),
        areas=tuple(areas),
        implications=tuple(implications),
        risk_levels=tuple(risk_levels),
        stage=stage,
    )


def applicability_match(
    applicability: SkillApplicability, context: SkillContextView
) -> tuple[bool, tuple[str, ...]]:
    """Whether an optional skill applies to a context, and why it does.

    The match is any-of across families and any-of inside each family, so the
    reason list names exactly the recorded facts that selected the skill.
    """

    reasons: list[str] = []
    for value in applicability.task_types:
        if value in context.task_types:
            reasons.append(f"task-type:{value}")
    for value in applicability.areas:
        if value in context.areas:
            reasons.append(f"area:{value}")
    for value in applicability.implications:
        if value in context.implications:
            reasons.append(f"implication:{value}")
    for value in applicability.risk_levels:
        if value in context.risk_levels:
            reasons.append(f"risk-level:{value}")
    return bool(reasons), tuple(reasons)


__all__ = [
    "AREA_VALUES",
    "Frontmatter",
    "IMPLICATION_VALUES",
    "MAX_SKILL_BYTES",
    "MAX_SKILLS_BYTES_PER_PROMPT",
    "ORIGIN_CONTEXTUAL",
    "ORIGIN_DECLARED",
    "ORIGIN_REQUIRED",
    "RISK_LEVEL_VALUES",
    "SKILL_BINDING_INVALID",
    "SKILL_BINDING_MISSING",
    "SKILL_BUDGET_EXCEEDED",
    "SKILL_FILE_NAME",
    "SKILL_KEYS",
    "SKILL_METADATA_INVALID",
    "SKILL_NAME_PATTERN",
    "SKILL_NOT_FOUND",
    "SKILL_ORIGINS",
    "SKILL_PREREQUISITE_MISSING",
    "SKILL_ROLE_MISMATCH",
    "SKILL_SOURCE_PREFIX",
    "SKILL_STAGES",
    "SKILL_STAGE_MISMATCH",
    "SKILL_TOO_LARGE",
    "SKILL_UNSAFE_PATH",
    "TASK_TYPE_VALUES",
    "TOOL_VALUES",
    "SkillApplicability",
    "SkillContextView",
    "SkillDocument",
    "SkillLoadError",
    "applicability_match",
    "context_view",
    "declared_skills",
    "declared_tools",
    "discover_skills",
    "load_role_skills",
    "load_skill",
    "parse_flag",
    "parse_frontmatter",
    "parse_list",
    "render_skills_block",
    "skill_evidence",
]
