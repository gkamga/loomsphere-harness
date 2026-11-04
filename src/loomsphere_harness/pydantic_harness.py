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
import ipaddress
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from urllib.parse import urlsplit

from loomsphere_harness.agent_runner import (
    AgentExecutionError,
    AgentInvocation,
    AgentRunResult,
)

EXECUTOR_NAME = "pydantic-ai-harness"
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
RESULT_PREVIEW_CHARS = 320
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

INSTRUCTIONS = (
    "You are a LoomSphere agent. Work only through the provided file tools, which "
    "are confined to the current workspace. You have no shell: do not try to run "
    "commands; the factory runs validation after you finish. When the task is "
    "complete, reply with the final answer in exactly the format the task asks for."
)


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
        )

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


class PydanticHarnessRunner:
    """Run one invocation as a PydanticAI agent with confined file tools."""

    def __init__(
        self, config: PydanticHarnessConfig, *, model: object | None = None
    ) -> None:
        self.config = config
        # Test seam only: lets a unit test exercise the tool boundary without a
        # network endpoint. The real factory path always builds the configured model.
        self._model = model

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
            "limits": {
                "timeout_seconds": invocation.timeout_seconds,
                "requests": self.config.request_limit,
                "tool_calls": self.config.tool_call_limit,
                "total_tokens": self.config.total_token_limit,
                "max_output_tokens": self.config.max_output_tokens,
            },
        }
        try:
            # A fresh thread owns its event loop, so this works whether the caller
            # is a worker thread or an inline request handler.
            with ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(
                    asyncio.run, self._execute(invocation, tools, api_key, base_events)
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
        base_events: dict[str, object],
    ) -> AgentRunResult:
        import time

        from pydantic_ai import Agent
        from pydantic_ai.capabilities import LocalWorkspace
        from pydantic_ai.exceptions import (
            ModelAPIError,
            ModelHTTPError,
            UnexpectedModelBehavior,
            UsageLimitExceeded,
        )
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider
        from pydantic_ai.usage import UsageLimits
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
                ),
            ],
        )
        started = time.monotonic()
        limits = UsageLimits(
            request_limit=config.request_limit,
            tool_calls_limit=config.tool_call_limit,
            total_tokens_limit=config.total_token_limit,
        )

        async def invoke():
            return await agent.run(
                invocation.prompt,
                usage_limits=limits,
                model_settings={"max_tokens": config.max_output_tokens},
            )

        def elapsed() -> dict[str, object]:
            return {"duration_ms": int((time.monotonic() - started) * 1000)}

        try:
            result = await asyncio.wait_for(invoke(), timeout=invocation.timeout_seconds)
        except TimeoutError as error:
            raise _RunFailure(
                "timed-out",
                f"The agent run exceeded {invocation.timeout_seconds} seconds.",
                events=elapsed(),
            ) from error
        except UsageLimitExceeded as error:
            raise _RunFailure(
                "resource-limit-exceeded", f"A run limit was reached: {error}"[:300],
                events=elapsed(),
            ) from error
        except ModelHTTPError as error:
            raise _RunFailure(
                "model-error",
                f"The model endpoint returned HTTP {error.status_code}.",
                events={**elapsed(), "status_code": error.status_code},
            ) from error
        except ModelAPIError as error:
            raise _RunFailure(
                "provider-unavailable",
                "The model endpoint could not be reached.",
                events=elapsed(),
            ) from error
        except UnexpectedModelBehavior as error:
            raise _RunFailure(
                "model-error", f"The model misbehaved: {error}"[:300], events=elapsed()
            ) from error
        except Exception as error:
            raise _RunFailure(
                "harness-error",
                f"The PydanticAI harness failed: {type(error).__name__}.",
                events=elapsed(),
            ) from error

        usage = result.usage
        events = {
            **elapsed(),
            "usage": {
                "requests": usage.requests,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "tool_calls": usage.tool_calls,
            },
            "tool_activity": _tool_activity(result.all_messages()),
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


def _tool_activity(messages: list[object]) -> list[dict[str, object]]:
    """Ordered tool calls with their outcome; arguments and results are bounded."""

    from pydantic_ai.messages import ToolCallPart, ToolReturnPart, RetryPromptPart

    calls: dict[str, dict[str, object]] = {}
    for message in messages:
        for part in getattr(message, "parts", ()):
            if isinstance(part, ToolCallPart):
                calls[part.tool_call_id] = {
                    "tool": part.tool_name,
                    "arguments": _clip(part.args_as_json_str(), TOOL_ARGUMENT_CHARS),
                    "outcome": "no-result",
                }
            elif isinstance(part, ToolReturnPart) and part.tool_call_id in calls:
                # The file tools report a refused path as an ordinary result, so the
                # outcome is "returned" and the preview carries the refusal text.
                calls[part.tool_call_id]["outcome"] = "returned"
                calls[part.tool_call_id]["result_chars"] = len(str(part.content))
                calls[part.tool_call_id]["result_preview"] = _clip(
                    str(part.content), RESULT_PREVIEW_CHARS
                )
            elif isinstance(part, RetryPromptPart) and part.tool_call_id in calls:
                calls[part.tool_call_id]["outcome"] = "rejected"
                calls[part.tool_call_id]["detail"] = _clip(str(part.content), 200)
    return list(calls.values())[:TOOL_ACTIVITY_LIMIT]


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit] + "…"
