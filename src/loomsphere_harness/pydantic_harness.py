"""PydanticAI Harness execution adapter.

Implements the same ``run(AgentInvocation) -> AgentRunResult`` contract as the
other runners, so the durable queue, stage handlers, lifecycle hooks, and
recording code are unchanged. The agent loop is PydanticAI; its file tools come
from the Harness ``FileSystem`` capability.

Trust boundary, enforced by what is registered rather than by the prompt:

* No shell and no network tool exist. The Harness ``Shell`` and ``Coder``
  capabilities are deliberately not used because they run unrestricted host
  commands; validation runs in the trusted sandbox after the Builder finishes.
* Every path is contained in the invocation's isolated worktree. ``.git``, key
  files, ``.env`` files, and secret directories are denied for read and write.
* Mutating tools are registered only for a writable Builder invocation, and the
  workspace itself is opened read-only for every other role.
* The model endpoint host must be explicitly approved, the credential comes only
  from the settings secret source, and there is no fallback provider.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from urllib.parse import urlsplit

from loomsphere_harness.agent_runner import (
    AgentExecutionError,
    AgentInvocation,
    AgentRunResult,
)
from loomsphere_harness.diagnostics import budget_report, usage_snapshot

EXECUTOR_NAME = "pydantic-ai-harness"
#: The roles the harness runs. A per-role limit table may name these and nothing
#: else, so a typo cannot silently fall back to the global budget.
ROLES: tuple[str, ...] = ("intake", "architect", "builder", "qa", "reviewer")
ROLE_LIMIT_KEYS: tuple[str, ...] = (
    "request_limit",
    "tool_call_limit",
    "total_token_limit",
)
READ_TOOLS: tuple[str, ...] = ("read_file", "list_directory", "find_files", "search_files")
WRITE_TOOLS: tuple[str, ...] = ("write_file", "edit_file", "create_directory")
DENIED_PATTERNS: tuple[str, ...] = (
    "**/.git",
    "**/.git/**",
    "**/.env",
    "**/.env.*",
    "**/*.pem",
    "**/*.key",
    "**/id_rsa*",
    "**/id_ed25519*",
    "**/.ssh/**",
    "**/secrets/**",
    "**/.pydantic-ai-harness/**",
)
WRITER_ROLES = frozenset({"builder"})
TOOL_ARGUMENT_CHARS = 300
TOOL_ACTIVITY_LIMIT = 400
#: Argument keys whose value is a search selector. The literal value can carry a
#: credential, customer data, or the exact content being searched for, so only a
#: digest, the argument name, and its length are recorded.
SEARCH_ARGUMENT_KEYS: tuple[str, ...] = ("query", "pattern", "include_glob", "glob")
#: Argument keys naming the object a tool acted on. A workspace-relative path is
#: the useful diagnostic and is not workspace content, so it stays readable.
TARGET_ARGUMENT_KEYS: tuple[str, ...] = ("path", "directory", "target")
#: Conservative bounds on what one tool result may add to a model context. The
#: library defaults (2,000 lines / 50,000 characters / 1,000 entries) let a single
#: read or listing fill a small model's window before any implementation starts.
#: Paging is unaffected: ``offset``/``limit`` continue from where a clipped read
#: stopped, and the result names the first line it did not show.
MAX_READ_LINES = 400
MAX_READ_CHARS = 24_000
MAX_LIST_RESULTS = 200
MAX_SEARCH_RESULTS = 100
MAX_FIND_RESULTS = 200
#: Distinct tool names kept in the bounded failure diagnostic.
TOOL_SUMMARY_LIMIT = 25
#: Per-request context measurements kept in the run evidence. A run longer than
#: this keeps its newest measurements and flags the truncation.
MEASUREMENT_LIMIT = 60
#: Smaller models call tools that do not exist (for example a shell). Each such
#: call is refused; a few retries let the model recover without widening any grant.
TOOL_RETRIES = 5
#: A provider reporting this finish reason stopped at the token ceiling rather
#: than because it had finished. It is authoritative: the text is incomplete.
TRUNCATING_FINISH_REASONS = frozenset({"length"})
#: Raised as the failure reason so a truncated run is never reported as a bad
#: contract. Reporting it as ``invalid-intake`` hid the real cause.
REASON_RESPONSE_TRUNCATED = "response-truncated"
TRUNCATION_EXCERPT_CHARS = 400
#: Tool results whose content can be fetched again by calling the same tool with
#: the same arguments. Only these are ever elided from a later request: a command
#: result cannot be reproduced, so it is evidence and stays verbatim.
ELIDABLE_TOOLS: tuple[str, ...] = READ_TOOLS
#: How much prior tool-result text one run may keep in its model context. Every
#: provider request re-sends the whole history, so an unbounded history is charged
#: again on each request: seven capped reads became 164,000 input tokens in the
#: M00-02 QA run and exhausted a 150,000-token budget before QA could answer. The
#: bound keeps the most recent evidence verbatim and names every elided result.
MAX_RETAINED_TOOL_CHARS = 48_000
#: The most recent results always stay verbatim, so the answer under construction
#: keeps the evidence it was just reasoning about.
KEEP_RECENT_TOOL_RESULTS = 2
#: Characters per token assumed when deciding whether the *next* request cannot
#: fit. Deliberately pessimistic (real tokenizers emit more than one token per
#: eight characters), so the guard never refuses a request that would have fitted.
CONTEXT_CHARS_PER_TOKEN_FLOOR = 8
#: Per-message and per-part framing that the provider charges for but that carries
#: no content: role markers, tool names, and tool call identifiers.
MESSAGE_OVERHEAD_CHARS = 40
PART_OVERHEAD_CHARS = 80

INSTRUCTIONS = (
    "You are a LoomSphere agent. Work only through the provided file tools, which "
    "are confined to the current workspace. You have no shell: do not try to run "
    "commands; the factory runs validation after you finish. When the task is "
    "complete, reply with the final answer in exactly the format the task asks for."
)


@dataclass(frozen=True)
class RoleLimits:
    """Limits for one role. A field left unset inherits the global value."""

    role: str
    request_limit: int | None = None
    tool_call_limit: int | None = None
    total_token_limit: int | None = None

    def declared(self) -> dict[str, int]:
        """Only the fields this role actually overrides."""

        values: dict[str, int] = {}
        for key in ROLE_LIMIT_KEYS:
            value = getattr(self, key)
            if value is not None:
                values[key] = value
        return values


@dataclass(frozen=True)
class EffectiveLimits:
    """The limits one role actually runs under, and where each value came from.

    The provenance matters: an operator reading a failed run must be able to tell a
    role override from the inherited global value without reading the config file.
    """

    role: str
    request_limit: int
    tool_call_limit: int
    total_token_limit: int
    sources: tuple[tuple[str, str], ...] = ()

    def values(self) -> dict[str, int]:
        return {key: getattr(self, key) for key in ROLE_LIMIT_KEYS}

    def evidence(self) -> dict[str, object]:
        return {**self.values(), "sources": dict(self.sources)}


@dataclass(frozen=True)
class PydanticHarnessConfig:
    provider: str = "openai-compatible"
    base_url: str | None = None
    model_id: str | None = None
    approved_hosts: tuple[str, ...] = ()
    api_key: str | None = None
    api_key_file: str | None = None
    require_api_key: bool = True
    workspace_root: str | None = None
    request_limit: int = 60
    tool_call_limit: int = 120
    total_token_limit: int = 600_000
    max_output_tokens: int = 4_096
    #: Per-role overrides. Empty means every role runs under the global limits.
    role_limits: tuple[RoleLimits, ...] = ()

    def __post_init__(self) -> None:
        """Refuse an unusable override table at construction, not at run time.

        The configuration loader reports these cases with stable error codes; this
        guard covers direct construction (tests, embedding), where a bad table
        would otherwise decide a run's budget silently.
        """

        globals_ = {key: getattr(self, key) for key in ROLE_LIMIT_KEYS}
        seen: set[str] = set()
        for override in self.role_limits:
            if override.role not in ROLES:
                raise ValueError(
                    f"Unknown agent role in role_limits: {override.role!r}; "
                    f"expected one of {list(ROLES)}."
                )
            if override.role in seen:
                raise ValueError(
                    f"Duplicate role_limits entry for {override.role!r}."
                )
            seen.add(override.role)
            for key, value in override.declared().items():
                if isinstance(value, bool) or value < 1:
                    raise ValueError(
                        f"role_limits.{override.role}.{key} must be a positive integer."
                    )
                if value > globals_[key]:
                    raise ValueError(
                        f"role_limits.{override.role}.{key} ({value}) must not exceed "
                        f"the global {key} ({globals_[key]}): a role override may "
                        "narrow a limit, never widen it."
                    )

    @classmethod
    def from_settings(cls, settings: object) -> PydanticHarnessConfig:
        api_key = getattr(settings, "AGENT_MODEL_API_KEY", None)
        hosts = str(getattr(settings, "AGENT_MODEL_APPROVED_HOSTS", "") or "")
        return cls(
            provider=settings.AGENT_MODEL_PROVIDER,
            base_url=settings.AGENT_MODEL_BASE_URL,
            model_id=settings.AGENT_MODEL_ID,
            approved_hosts=tuple(
                host.strip().lower() for host in hosts.split(",") if host.strip()
            ),
            api_key=api_key.get_secret_value() if api_key is not None else None,
            api_key_file=settings.AGENT_MODEL_API_KEY_FILE,
            require_api_key=settings.AGENT_MODEL_REQUIRE_API_KEY,
            workspace_root=settings.AGENT_WORKSPACE_ROOT,
            request_limit=settings.AGENT_MODEL_REQUEST_LIMIT,
            tool_call_limit=settings.AGENT_MODEL_TOOL_CALL_LIMIT,
            total_token_limit=settings.AGENT_MODEL_TOTAL_TOKEN_LIMIT,
            max_output_tokens=settings.AGENT_MODEL_MAX_OUTPUT_TOKENS,
            role_limits=tuple(getattr(settings, "AGENT_MODEL_ROLE_LIMITS", ()) or ()),
        )

    def overrides_for(self, role: str) -> RoleLimits | None:
        """The declared override for one role, or ``None`` when it inherits."""

        for override in self.role_limits:
            if override.role == role:
                return override
        return None

    def effective_limits(self, role: str) -> EffectiveLimits:
        """The limits this role runs under, field by field, with their source."""

        override = self.overrides_for(role)
        values: dict[str, int] = {}
        sources: list[tuple[str, str]] = []
        for key in ROLE_LIMIT_KEYS:
            declared = getattr(override, key) if override is not None else None
            if declared is None:
                values[key] = getattr(self, key)
                sources.append((key, "global"))
            else:
                values[key] = declared
                sources.append((key, f"role_limits.{role}"))
        return EffectiveLimits(role=role, sources=tuple(sources), **values)

    @property
    def endpoint_host(self) -> str | None:
        if not self.base_url:
            return None
        return (urlsplit(self.base_url).hostname or "").lower() or None


def runtime_identity(config: PydanticHarnessConfig) -> tuple[str, str]:
    """The ``(model, runtime)`` identifiers recorded on every run of this backend."""

    host = config.endpoint_host or "unconfigured"
    model = f"{host}/{config.model_id or 'unconfigured'}"[:120]
    try:
        runtime = (
            f"{EXECUTOR_NAME} {metadata.version('pydantic-ai-harness')} "
            f"(pydantic-ai-slim {metadata.version('pydantic-ai-slim')})"
        )
    except metadata.PackageNotFoundError:
        runtime = f"{EXECUTOR_NAME} (not installed)"
    return model, runtime[:120]


def tools_for(invocation: AgentInvocation) -> tuple[str, ...]:
    """The file tools this invocation may hold, decided from role and mount mode."""

    if invocation.read_only or invocation.role not in WRITER_ROLES:
        return READ_TOOLS
    if invocation.allowed_tools and "edit" not in invocation.allowed_tools:
        return READ_TOOLS
    return READ_TOOLS + WRITE_TOOLS


def _is_local_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class ContextPolicy:
    """How much prior tool-result text one run may keep in the model context.

    The policy bounds *history that is sent again on every request*. It never
    bounds what a tool may return, never edits the workspace, and never drops a
    result silently: an elided result is replaced by a stub naming the tool, the
    target, the original size, and a SHA-256 digest, so the model knows the exact
    bytes it is missing and can fetch them again.
    """

    max_retained_chars: int = MAX_RETAINED_TOOL_CHARS
    keep_recent: int = KEEP_RECENT_TOOL_RESULTS
    #: The tools whose results may be elided.
    elidable_tools: tuple[str, ...] = ELIDABLE_TOOLS
    #: Characters per token assumed for the next-request projection.
    chars_per_token: int = CONTEXT_CHARS_PER_TOKEN_FLOOR

    def evidence(self) -> dict[str, object]:
        return {
            "max_retained_tool_chars": self.max_retained_chars,
            "keep_recent_tool_results": self.keep_recent,
            "elidable_tools": list(self.elidable_tools),
            "projection_chars_per_token": self.chars_per_token,
        }


class _ContextLimitReached(Exception):
    """The next request cannot fit in what is left of the run's token budget.

    Raised before the request is sent, so the run stops instead of paying for a
    request that must push the total past the configured ceiling.
    """

    def __init__(self, evidence: dict[str, object]) -> None:
        self.evidence = evidence
        super().__init__(
            "the next model request cannot fit the remaining token budget "
            f"({evidence['projected_tokens']} >= {evidence['remaining_tokens']})"
        )


def _context_chars(messages: list[object]) -> int:
    """The characters of prompt the runtime is about to send for one request.

    Summed per part rather than over ``str(parts)``: rendering the part list would
    include the content a second time and double every figure, which would both
    misreport the evidence and make the pre-request guard refuse runs that fit.
    """

    total = 0
    for message in messages:
        total += MESSAGE_OVERHEAD_CHARS
        for part in getattr(message, "parts", ()):
            total += PART_OVERHEAD_CHARS
            content = getattr(part, "content", None)
            if content is not None:
                total += len(content) if isinstance(content, str) else len(str(content))
            args = getattr(part, "args", None)
            if args is not None:
                total += len(str(args))
    return total


@dataclass
class ContextBudget:
    """Bound one run's retained tool-result text and measure each request.

    PydanticAI re-sends the full message history on every request, so the cost of
    a tool result is charged once per remaining request rather than once. Two rules
    keep that geometric growth finite without losing evidence:

    1. **Duplicate elision.** A result whose content is byte-identical to a newer
       result of the same tool is replaced by a stub: the model already has the
       newer copy, so the older one adds cost and no information.
    2. **Retention bound.** Once the retained results exceed
       ``max_retained_chars``, older results are elided newest-first, keeping the
       most recent ``keep_recent`` verbatim.

    Every elision records the original character count keyed by tool call id, so
    the audit trail keeps the true result size and never a stub's size.
    """

    policy: ContextPolicy
    total_token_limit: int
    usage: object
    measurements: list[dict[str, object]] = field(default_factory=list)
    original_chars: dict[str, int] = field(default_factory=dict)
    elided_calls: dict[str, dict[str, object]] = field(default_factory=dict)

    def __call__(self, messages: list[object]) -> list[object]:
        """Register as a PydanticAI history processor and measure the request."""

        self._bound(messages)
        record = self._measure(messages)
        self.measurements.append(record)
        limit = self.total_token_limit
        used = _usage_total(self.usage)
        if limit > 0 and used > 0:
            projected = record["projected_tokens"]
            remaining = limit - used
            if projected >= remaining:
                record["refused"] = True
                raise _ContextLimitReached(
                    {
                        "request": record["request"],
                        "prompt_chars": record["prompt_chars"],
                        "projected_tokens": projected,
                        "remaining_tokens": remaining,
                        "total_token_limit": limit,
                        "chars_per_token": self.policy.chars_per_token,
                        "kind": "projected-request-cannot-fit",
                    }
                )
        return messages

    def evidence(self) -> dict[str, object]:
        """The bounded context observations recorded on the run.

        Counts and digests only: no prompt text, no file content, no arguments.
        """

        prompts = [int(record["prompt_chars"]) for record in self.measurements]
        return {
            "policy": self.policy.evidence(),
            "requests": len(self.measurements),
            "measurements": self.measurements[-MEASUREMENT_LIMIT:],
            "measurements_truncated": len(self.measurements) > MEASUREMENT_LIMIT,
            "peak_prompt_chars": max(prompts) if prompts else 0,
            "total_prompt_chars": sum(prompts),
            "elided_tool_results": len(self.elided_calls),
            "elided_chars": sum(
                int(entry["chars"]) for entry in self.elided_calls.values()
            ),
        }

    def _measure(self, messages: list[object]) -> dict[str, object]:
        from pydantic_ai.messages import ToolReturnPart

        prompt_chars = _context_chars(messages)
        returns = [
            part
            for message in messages
            for part in getattr(message, "parts", ())
            if isinstance(part, ToolReturnPart)
        ]
        retained = sum(
            len(part.content)
            for part in returns
            if isinstance(part.content, str)
            and part.tool_call_id not in self.elided_calls
        )
        return {
            "request": len(self.measurements) + 1,
            "prompt_chars": prompt_chars,
            "messages": len(messages),
            "tool_results": len(returns),
            "retained_tool_chars": retained,
            "elided_tool_results": len(self.elided_calls),
            # A floor, not an estimate of provider tokens: it exists only so a
            # request that provably cannot fit is refused before it is sent.
            "projected_tokens": prompt_chars // max(1, self.policy.chars_per_token),
        }

    def _bound(self, messages: list[object]) -> None:
        from pydantic_ai.messages import ToolCallPart, ToolReturnPart

        targets: dict[str, tuple[str, dict[str, object] | None]] = {}
        for message in messages:
            for part in getattr(message, "parts", ()):
                if isinstance(part, ToolCallPart):
                    targets[part.tool_call_id] = (
                        part.tool_name,
                        _argument_target(part.args),
                    )

        candidates: list[tuple[object, str]] = []
        for message in messages:
            for part in getattr(message, "parts", ()):
                if not isinstance(part, ToolReturnPart):
                    continue
                if not isinstance(part.content, str):
                    # Image or PDF content: not elided, so no stub can misdescribe it.
                    continue
                self.original_chars.setdefault(part.tool_call_id, len(part.content))
                if part.tool_call_id in self.elided_calls:
                    continue
                tool = targets.get(part.tool_call_id, (part.tool_name, None))[0]
                if tool not in self.policy.elidable_tools:
                    continue
                candidates.append((part, tool))

        budget = self.policy.max_retained_chars
        digest_of_kept: dict[tuple[str, str], None] = {}
        for position, (part, tool) in enumerate(reversed(candidates)):
            content = part.content
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            key = (tool, digest)
            if key in digest_of_kept:
                self._elide(part, tool, targets, content, digest, reason="duplicate")
                continue
            if position < self.policy.keep_recent or len(content) <= budget:
                digest_of_kept[key] = None
                budget -= len(content)
                continue
            self._elide(part, tool, targets, content, digest, reason="retention-bound")

    def _elide(
        self,
        part: object,
        tool: str,
        targets: dict[str, tuple[str, dict[str, object] | None]],
        content: str,
        digest: str,
        *,
        reason: str,
    ) -> None:
        target = targets.get(part.tool_call_id, (tool, None))[1] or {}
        self.elided_calls[part.tool_call_id] = {
            "tool": tool,
            "target": target.get("target"),
            "chars": len(content),
            "sha256": f"sha256:{digest}",
            "reason": reason,
        }
        part.content = _elision_stub(
            tool=tool,
            target=target.get("target"),
            chars=len(content),
            digest=digest,
            reason=reason,
        )


def _elision_stub(
    *,
    tool: str,
    target: object,
    chars: int,
    digest: str,
    reason: str,
) -> str:
    """The text that replaces an elided result.

    It is deliberately explicit: the model must not mistake an elided result for an
    empty file, a clean search, or a passing command, and it must be able to ask for
    the same bytes again.
    """

    why = (
        "an identical, newer result for the same tool is retained above"
        if reason == "duplicate"
        else "this run's retained tool-result budget is full"
    )
    return (
        f"[loomsphere-harness] Result elided from this request: {why}. "
        f"Nothing was hidden from you selectively; the content was returned in full "
        f"earlier in this run.\n"
        f"tool: {tool}\n"
        f"target: {target if target is not None else 'unknown'}\n"
        f"elided_chars: {chars}\n"
        f"sha256: {digest}\n"
        f"If you still need the content, call {tool} again with the same arguments "
        f"(use offset/limit to page a large file). Do not assume what it said."
    )


def _usage_total(usage: object) -> int:
    total = getattr(usage, "total_tokens", 0)
    return int(total) if isinstance(total, int) else 0


class PydanticHarnessRunner:
    """Run one invocation as a PydanticAI agent with confined file tools."""

    def __init__(
        self,
        config: PydanticHarnessConfig,
        *,
        model: object | None = None,
        context_policy: ContextPolicy | None = None,
    ) -> None:
        self.config = config
        # Test seam only: lets a unit test exercise the tool boundary without a
        # network endpoint. The real factory path always builds the configured model.
        self._model = model
        # Test seam and configuration point: tightening the bound never changes a
        # verdict, so it is adjustable without an approval step.
        self.context_policy = context_policy or ContextPolicy()

    def _validated(self, invocation: AgentInvocation) -> str | None:
        """Refuse an unconfigured or unapproved backend; return the API key."""

        config = self.config
        host = config.endpoint_host
        if config.provider != "openai-compatible" or not host or not config.model_id:
            raise AgentExecutionError(
                "provider-not-configured",
                "AGENT_MODEL_BASE_URL and AGENT_MODEL_ID must be set; there is no "
                "default provider and no fallback.",
            )
        if host not in config.approved_hosts:
            raise AgentExecutionError(
                "provider-not-approved",
                f"The model endpoint host {host!r} is not in AGENT_MODEL_APPROVED_HOSTS.",
            )
        scheme = urlsplit(config.base_url or "").scheme
        if scheme != "https" and not _is_local_host(host):
            raise AgentExecutionError(
                "provider-not-approved",
                "A non-local model endpoint must use https.",
            )
        if config.workspace_root is not None:
            root = Path(config.workspace_root).resolve()
            workspace = invocation.workspace_path.resolve()
            if root != workspace and root not in workspace.parents:
                raise AgentExecutionError(
                    "workspace-not-isolated",
                    "The workspace is outside the configured agent workspace root.",
                )
        return self._api_key()

    def _api_key(self) -> str | None:
        config = self.config
        key = config.api_key
        if key is None and config.api_key_file:
            try:
                key = Path(config.api_key_file).read_text(encoding="utf-8").strip()
            except OSError as error:
                raise AgentExecutionError(
                    "credential-unavailable",
                    "The model credential file could not be read.",
                ) from error
        key = (key or "").strip() or None
        if key is None and config.require_api_key:
            raise AgentExecutionError(
                "credential-unavailable",
                "No model credential is configured (AGENT_MODEL_API_KEY or "
                "AGENT_MODEL_API_KEY_FILE).",
            )
        return key

    def run(self, invocation: AgentInvocation) -> AgentRunResult:
        api_key = self._validated(invocation)
        tools = tools_for(invocation)
        effective = self.config.effective_limits(invocation.role)
        base_events: dict[str, object] = {
            "executor": EXECUTOR_NAME,
            "provider": self.config.provider,
            "endpoint_host": self.config.endpoint_host,
            "model": self.config.model_id,
            "role": invocation.role,
            "granted_tools": list(tools),
            "catalog_tools": list(invocation.allowed_tools),
            "withheld_tools": ["shell", "network"],
            "workspace_writable": "write_file" in tools,
            # Recorded before the run starts and merged into every outcome, so a
            # failed run still says which budget it was given and where that value
            # came from.
            "limits": {
                "timeout_seconds": invocation.timeout_seconds,
                "role": effective.role,
                **effective.values(),
                "max_output_tokens": self.config.max_output_tokens,
                "sources": dict(effective.sources),
                "file_tools": {
                    "max_read_lines": MAX_READ_LINES,
                    "max_read_chars": MAX_READ_CHARS,
                    "max_list_results": MAX_LIST_RESULTS,
                    "max_search_results": MAX_SEARCH_RESULTS,
                    "max_find_results": MAX_FIND_RESULTS,
                },
            },
        }
        try:
            # A fresh thread owns its event loop, so this works whether the caller
            # is a worker thread or an inline request handler.
            with ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(
                    asyncio.run,
                    self._execute(invocation, tools, api_key, effective),
                ).result()
            return AgentRunResult(
                response_text=result.response_text,
                exit_code=result.exit_code,
                events={**base_events, **result.events},
            )
        except _RunFailure as failure:
            raise AgentExecutionError(
                failure.reason,
                failure.detail,
                response_text=failure.response_text,
                events={**base_events, **failure.events},
            ) from failure.__cause__

    async def _execute(
        self,
        invocation: AgentInvocation,
        tools: tuple[str, ...],
        api_key: str | None,
        effective: EffectiveLimits,
    ) -> AgentRunResult:
        import time

        from pydantic_ai import Agent, capture_run_messages
        from pydantic_ai.capabilities import LocalWorkspace, ProcessHistory
        from pydantic_ai.exceptions import (
            ModelAPIError,
            ModelHTTPError,
            UnexpectedModelBehavior,
            UsageLimitExceeded,
        )
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider
        from pydantic_ai.usage import RunUsage, UsageLimits
        from pydantic_ai_harness import FileSystem

        config = self.config
        model = self._model or OpenAIChatModel(
            config.model_id or "",
            provider=OpenAIProvider(
                base_url=config.base_url,
                # Local servers ignore the key, but the client requires a value.
                api_key=api_key or "not-required",
            ),
        )
        writable = "write_file" in tools
        # The accumulator is owned here rather than read back from the result: it
        # is the only figure that survives an exception, a timeout, or cancellation.
        usage = RunUsage()
        budget = ContextBudget(
            policy=self.context_policy,
            total_token_limit=effective.total_token_limit,
            usage=usage,
        )
        agent = Agent(
            model,
            instructions=INSTRUCTIONS,
            retries=TOOL_RETRIES,
            capabilities=[
                LocalWorkspace(invocation.workspace_path, read_only=not writable),
                FileSystem(
                    tools=tools,
                    denied_patterns=DENIED_PATTERNS,
                    content_hashes=False,
                    read_only=not writable,
                    max_read_lines=MAX_READ_LINES,
                    max_read_chars=MAX_READ_CHARS,
                    max_list_results=MAX_LIST_RESULTS,
                    max_search_results=MAX_SEARCH_RESULTS,
                    max_find_results=MAX_FIND_RESULTS,
                ),
                # Runs before every model request, so the retained history is
                # bounded and measured before it is paid for again.
                ProcessHistory(budget),
            ],
        )
        started = time.monotonic()
        limits = UsageLimits(
            request_limit=effective.request_limit,
            tool_calls_limit=effective.tool_call_limit,
            total_tokens_limit=effective.total_token_limit,
        )
        captured: list[list[object]] = []

        async def invoke():
            # Deliberately capturing only tool names and counts later: the messages
            # are inspected, never persisted, so a failure diagnostic cannot become
            # a copy of the workspace or the conversation.
            with capture_run_messages() as messages:
                captured[:] = [messages]
                return await agent.run(
                    invocation.prompt,
                    usage=usage,
                    usage_limits=limits,
                    model_settings={"max_tokens": config.max_output_tokens},
                )

        def observed() -> dict[str, object]:
            """What is known about this run, whether it succeeded or not.

            Only counters, a duration, and tool names and counts are recorded: no
            prompt, message, tool argument, or file content reaches the evidence.
            """

            snapshot = usage_snapshot(usage)
            return {
                "duration_ms": int((time.monotonic() - started) * 1000),
                "usage": snapshot,
                "diagnostics": budget_report(snapshot, effective.values()),
                "tool_summary": tool_summary(captured[0] if captured else []),
                # One row per request: how much prompt was sent and how much of it
                # was retained tool result. This is the measurement the M00-02
                # exclusion showed was missing, and it explains a budget failure
                # without recording any of the content that caused it.
                "context": budget.evidence(),
            }

        try:
            result = await asyncio.wait_for(invoke(), timeout=invocation.timeout_seconds)
        except TimeoutError as error:
            raise _RunFailure(
                "timed-out",
                f"The agent run exceeded {invocation.timeout_seconds} seconds.",
                events=observed(),
            ) from error
        except _ContextLimitReached as error:
            # Stopped before the request was sent: the ceiling is reached without
            # paying for a request that could only breach it. Reported as the same
            # reason the runtime uses, so one reason covers one class of outcome.
            raise _RunFailure(
                "resource-limit-exceeded",
                _context_limit_detail(error.evidence),
                events={
                    **observed(),
                    "context_limit": error.evidence,
                    "tool_activity": _tool_activity(
                        captured[0] if captured else [],
                        original_chars=budget.original_chars,
                        elided=budget.elided_calls,
                    ),
                },
            ) from error
        except UsageLimitExceeded as error:
            raise _RunFailure(
                "resource-limit-exceeded", f"A run limit was reached: {error}"[:300],
                events=observed(),
            ) from error
        except ModelHTTPError as error:
            raise _RunFailure(
                "model-error",
                f"The model endpoint returned HTTP {error.status_code}.",
                events={**observed(), "status_code": error.status_code},
            ) from error
        except ModelAPIError as error:
            raise _RunFailure(
                "provider-unavailable",
                "The model endpoint could not be reached.",
                events=observed(),
            ) from error
        except UnexpectedModelBehavior as error:
            raise _RunFailure(
                "model-error", f"The model misbehaved: {error}"[:300], events=observed()
            ) from error
        except Exception as error:
            raise _RunFailure(
                "harness-error",
                f"The PydanticAI harness failed: {type(error).__name__}.",
                events=observed(),
            ) from error

        events = {
            **observed(),
            "tool_activity": _tool_activity(
                result.all_messages(),
                original_chars=budget.original_chars,
                elided=budget.elided_calls,
            ),
        }
        response = str(result.output or "").strip()
        truncation = truncation_evidence(
            finish_reason=finish_reason(result),
            output_tokens=usage.output_tokens,
            max_output_tokens=config.max_output_tokens,
            response=response,
        )
        if truncation is not None:
            # Reported before the empty check: a run cut off at the first token is
            # truncated, not empty, and the two need different remediation.
            raise _RunFailure(
                REASON_RESPONSE_TRUNCATED,
                truncation_detail(truncation, response=response),
                events={**events, "truncation": truncation},
                response_text=response or None,
            )
        if not response:
            raise _RunFailure(
                "empty-response",
                "The agent runtime produced no response.",
                events=events,
            )
        return AgentRunResult(response_text=response, exit_code=0, events=events)


def finish_reason(result: object) -> str | None:
    """The provider's stop reason for the run's final model response."""

    direct = getattr(getattr(result, "response", None), "finish_reason", None)
    if isinstance(direct, str):
        return direct
    messages = getattr(result, "all_messages", None)
    if not callable(messages):
        return None
    for message in reversed(list(messages())):
        candidate = getattr(message, "finish_reason", None)
        if isinstance(candidate, str):
            return candidate
    return None


def looks_incomplete(text: str) -> bool:
    """Whether a response stops mid-structure, by fence count or bracket balance.

    Used only as corroboration: an unfinished response is not by itself proof of
    truncation, because the model may simply have answered badly.
    """

    if text.count("```") % 2:
        return True
    depth = 0
    in_string = False
    escaped = False
    for character in text:
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
        elif character in "{[":
            depth += 1
        elif character in "}]":
            depth -= 1
            if depth < 0:
                return True
    return depth != 0 or in_string


def truncation_evidence(
    *,
    finish_reason: str | None,
    output_tokens: int | None,
    max_output_tokens: int | None,
    response: str,
) -> dict[str, object] | None:
    """Describe an output-token truncation, or ``None`` when the run finished.

    A provider ``length`` finish reason is authoritative. Reaching the configured
    output ceiling only counts when the text also stops mid-structure, so a run
    that used its whole budget and finished cleanly is still accepted.
    """

    signals: list[str] = []
    if finish_reason in TRUNCATING_FINISH_REASONS:
        signals.append("finish-reason-length")
    at_ceiling = bool(max_output_tokens) and (output_tokens or 0) >= int(max_output_tokens or 0)
    incomplete = looks_incomplete(response)
    if at_ceiling and (incomplete or finish_reason is None):
        signals.append("output-token-ceiling")
    if not signals:
        return None
    if incomplete:
        signals.append("incomplete-payload")
    return {
        "signals": signals,
        "finish_reason": finish_reason,
        "output_tokens": output_tokens,
        "max_output_tokens": max_output_tokens,
        "response_chars": len(response),
        "response_excerpt": response[-TRUNCATION_EXCERPT_CHARS:],
    }


def truncation_detail(evidence: dict[str, object], *, response: str) -> str:
    signals = ", ".join(str(signal) for signal in evidence.get("signals", ()))
    return (
        "The model's response was truncated before it finished "
        f"({signals or 'truncated'}; {len(response)} characters, finish reason "
        f"{evidence.get('finish_reason')!r}, output tokens "
        f"{evidence.get('output_tokens')} of {evidence.get('max_output_tokens')}). "
        "The incomplete text was not parsed as a contract. Raise "
        "model.max_output_tokens or shorten the required response."
    )


def _context_limit_detail(evidence: dict[str, object]) -> str:
    """The failure detail for a run stopped before a request that cannot fit."""

    return (
        f"The run stopped before request {evidence.get('request')} was sent, because "
        "its prompt would not fit the remaining token budget: the prompt is at least "
        f"{evidence.get('projected_tokens')} tokens and "
        f"{evidence.get('remaining_tokens')} of {evidence.get('total_token_limit')} "
        "remain. The request was never sent, so no tokens were spent on it and no "
        "verdict was produced. Fewer or smaller reads, a larger budget, or a "
        "narrower scope would each avoid it."
    )


class _RunFailure(Exception):
    def __init__(
        self,
        reason: str,
        detail: str,
        *,
        events: dict[str, object] | None = None,
        response_text: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.events = events or {}
        self.response_text = response_text


def _argument_target(args: object) -> dict[str, object] | None:
    """Safe diagnostic metadata for the object a tool call acted on.

    A read returns file contents and a write carries them in its arguments, so
    neither is recorded: run evidence is hash-chained and append-only, which makes
    it the worst possible place for a copy of the workspace or a credential a file
    happened to contain.

    Search selectors (``query``, ``pattern``, and glob filters) are also never
    recorded literally. A search string can be a credential, customer data, or the
    exact content being looked for. What is kept is a SHA-256 digest, the argument
    name, and the value's length: enough to correlate repeated calls without
    copying the value. Path-like arguments stay readable because a bounded
    workspace-relative path is what makes the record diagnosable.
    """

    if isinstance(args, str):
        try:
            args = json.loads(args or "{}")
        except ValueError:
            return None
    if not isinstance(args, Mapping):
        return None
    for key in SEARCH_ARGUMENT_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value:
            digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
            return {
                "target": f"sha256:{digest}",
                "target_kind": key,
                "target_chars": len(value),
            }
    for key in TARGET_ARGUMENT_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value:
            return {
                "target": _clip(value, TOOL_ARGUMENT_CHARS),
                "target_kind": key,
                "target_chars": len(value),
            }
    return None


def _tool_activity(
    messages: list[object],
    *,
    original_chars: dict[str, int] | None = None,
    elided: dict[str, dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    """Ordered tool calls with their outcome; no arguments and no results.

    Earlier revisions copied up to 300 characters of the arguments and 320 of the
    result into every run record. For a write tool the arguments *are* the file
    contents, and for a read the result is, so the record became a partial copy of
    the workspace. Only the tool name, its target, its outcome, and how much it
    returned are kept.

    ``result_chars`` is the size the tool actually returned, not the size of what
    was later elided from a subsequent request: the two differ when the context
    bound replaced a retained result with a stub, and the audit record must keep
    describing what the tool did.
    """

    from pydantic_ai.messages import ToolCallPart, ToolReturnPart, RetryPromptPart

    originals = original_chars or {}
    elided = elided or {}
    calls: dict[str, dict[str, object]] = {}
    for message in messages:
        for part in getattr(message, "parts", ()):
            if isinstance(part, ToolCallPart):
                target = _argument_target(part.args) or {
                    "target": None,
                    "target_kind": None,
                    "target_chars": None,
                }
                calls[part.tool_call_id] = {
                    "tool": part.tool_name,
                    **target,
                    "outcome": "no-result",
                }
            elif isinstance(part, ToolReturnPart) and part.tool_call_id in calls:
                # The file tools report a refused path as an ordinary result, so the
                # outcome is "returned" and only the size is kept.
                calls[part.tool_call_id]["outcome"] = "returned"
                calls[part.tool_call_id]["result_chars"] = originals.get(
                    part.tool_call_id, len(str(part.content))
                )
            elif isinstance(part, RetryPromptPart) and part.tool_call_id in calls:
                calls[part.tool_call_id]["outcome"] = "rejected"
    for tool_call_id, entry in elided.items():
        if tool_call_id in calls:
            # Recorded so a reviewer can tell a small result from a large one that
            # was later removed from the requests, without seeing either.
            calls[tool_call_id]["elided"] = True
            calls[tool_call_id]["elided_chars"] = entry.get("chars")
            calls[tool_call_id]["elision_reason"] = entry.get("reason")
    return list(calls.values())[:TOOL_ACTIVITY_LIMIT]


def tool_summary(messages: list[object]) -> dict[str, object]:
    """Aggregate tool activity: distinct names and how often each ran.

    Unlike :func:`_tool_activity` this keeps no arguments and no results, so it is
    safe to attach to a failed run without persisting workspace content or a
    conversation. It answers the failure question "what was it doing when it ran
    out of budget?" without copying the workspace into the audit trail.
    """

    from pydantic_ai.messages import ToolCallPart

    counts: dict[str, int] = {}
    total = 0
    for message in messages:
        for part in getattr(message, "parts", ()):
            if isinstance(part, ToolCallPart):
                total += 1
                counts[part.tool_name] = counts.get(part.tool_name, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return {
        "calls": total,
        "distinct": len(counts),
        "by_tool": dict(ordered[:TOOL_SUMMARY_LIMIT]),
        "truncated": len(ordered) > TOOL_SUMMARY_LIMIT,
    }


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit] + "…"
