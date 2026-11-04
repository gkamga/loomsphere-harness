"""Runtime configuration for the standalone harness.

Configuration is external to source: a TOML file the operator owns, plus one
environment variable *named* by that file for the model credential. There is no
default provider, endpoint, or model. A credential value is never read from the
TOML file; the file may only name an environment variable or a credential file.

Example::

    [harness]
    root = "/opt/loomsphere-harness"          # trusted checkout, outside the target repo

    [model]
    provider = "openai-compatible"
    base_url = "https://openrouter.ai/api/v1"
    model_id = "<any model the endpoint serves>"
    approved_hosts = ["openrouter.ai"]
    api_key_env = "LOOMSPHERE_MODEL_API_KEY"

    [workspace]
    root = "~/.local/state/loomsphere-harness/workspaces"   # task worktrees

    [sandbox]
    image = "python:3.12-slim"                # must exist locally; pinned by id at run time

    [validation]
    timeout_seconds = 900

    [implementation]
    max_iterations = 2
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from loomsphere_harness.pydantic_harness import PydanticHarnessConfig

CONFIG_ENV = "LOOMSPHERE_HARNESS_CONFIG"
DEFAULT_CONFIG_PATH = Path("~/.config/loomsphere-harness/config.toml")
DEFAULT_ENV_KEY_NAME = "LOOMSPHERE_MODEL_API_KEY"

#: The commands the runtime is willing to plan. Validation may only ever run one of
#: these, so an operator who declares nothing still gets a bounded validation path;
#: an operator who declares ``allowlist`` narrows it further.
DEFAULT_VALIDATION_ALLOWLIST: tuple[tuple[str, ...], ...] = (
    ("python", "-m", "pytest", "-q"),
    ("python", "-m", "unittest", "discover", "-s", "tests"),
    ("npm", "run", "test", "--silent"),
    ("node", "--test"),
)

_MODEL_KEYS = {
    "provider",
    "base_url",
    "model_id",
    "approved_hosts",
    "api_key_env",
    "api_key_file",
    "require_api_key",
    "request_limit",
    "tool_call_limit",
    "total_token_limit",
    "max_output_tokens",
    "timeout_seconds",
}
_HARNESS_KEYS = {"root"}
_STATE_KEYS = {"dir"}
_WORKSPACE_KEYS = {"root"}
_SANDBOX_KEYS = {
    "image",
    "runtime",
    "timeout_seconds",
    "cpus",
    "memory_mb",
    "pids_limit",
    "tmpfs_mb",
    "network",
    "startup_grace_seconds",
}
_VALIDATION_KEYS = {"allowlist", "timeout_seconds", "excerpt_chars"}
_IMPLEMENTATION_KEYS = {"max_iterations"}
#: Keys that would put a secret in the file; refused rather than ignored.
_SECRET_KEYS = {"api_key", "key", "token", "password", "secret"}


class ConfigError(RuntimeError):
    """The configuration is unusable. The runtime fails closed on it."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class SandboxConfig:
    """The container boundary settings, without the per-run timeout.

    Deliberately duck-typed against :mod:`loomsphere_harness.sandbox` so this
    module does not import the executor (which imports the workspace digest).
    """

    image: str
    runtime: str = "docker"
    timeout_seconds: int = 300
    cpus: float = 2.0
    memory_mb: int = 2_048
    pids_limit: int = 256
    tmpfs_mb: int = 256
    network: str = "none"
    startup_grace_seconds: int = 30

    def __post_init__(self) -> None:
        if not self.image.strip():
            raise ConfigError("config-invalid", "sandbox.image must not be empty.")
        if self.runtime.strip() not in ("docker", "podman"):
            # Only a container runtime is accepted: there is no host fallback.
            raise ConfigError(
                "config-invalid", "sandbox.runtime must be a container runtime."
            )
        for name, value in (
            ("timeout_seconds", self.timeout_seconds),
            ("memory_mb", self.memory_mb),
            ("pids_limit", self.pids_limit),
            ("tmpfs_mb", self.tmpfs_mb),
            ("startup_grace_seconds", self.startup_grace_seconds),
        ):
            if value < 1:
                raise ConfigError(
                    "config-invalid", f"sandbox.{name} must be a positive integer."
                )
        if self.cpus <= 0:
            raise ConfigError("config-invalid", "sandbox.cpus must be positive.")
        if self.network != "none":
            raise ConfigError(
                "config-invalid",
                "sandbox.network must be 'none': the standalone runtime provisions no "
                "internal network.",
            )


@dataclass(frozen=True)
class ValidationConfig:
    allowlist: tuple[tuple[str, ...], ...] = DEFAULT_VALIDATION_ALLOWLIST
    timeout_seconds: int = 900
    excerpt_chars: int = 4_000


@dataclass(frozen=True)
class RuntimeConfig:
    harness_root: Path | None
    state_dir: Path | None
    model: PydanticHarnessConfig
    timeout_seconds: int = 600
    source: str | None = None
    workspace_root: Path | None = None
    sandbox: SandboxConfig | None = None
    validation: ValidationConfig = ValidationConfig()
    max_iterations: int = 2


def locate_config(explicit: str | os.PathLike[str] | None) -> Path | None:
    """Explicit path, then the environment, then the per-user default if present."""

    if explicit:
        return Path(explicit).expanduser()
    from_env = os.environ.get(CONFIG_ENV)
    if from_env:
        return Path(from_env).expanduser()
    default = DEFAULT_CONFIG_PATH.expanduser()
    return default if default.is_file() else None


def load_config(
    path: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> RuntimeConfig:
    """Read the configuration. No file yields an unconfigured model, which every
    model run refuses; it is never replaced by a default endpoint."""

    environment = os.environ if environ is None else environ
    located = locate_config(path)
    document: dict[str, Any] = {}
    if located is not None:
        try:
            document = tomllib.loads(located.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise ConfigError(
                "config-missing", f"The configuration file {located} does not exist."
            ) from error
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise ConfigError(
                "config-invalid", f"The configuration file could not be read: {error}"
            ) from error
    return parse_config(
        document, environ=environment, source=str(located) if located else None
    )


def parse_config(
    document: Mapping[str, Any],
    *,
    environ: Mapping[str, str],
    source: str | None = None,
) -> RuntimeConfig:
    unknown = set(document) - {
        "harness",
        "model",
        "state",
        "workspace",
        "sandbox",
        "validation",
        "implementation",
    }
    if unknown:
        raise ConfigError(
            "config-invalid", f"Unknown configuration sections: {sorted(unknown)}"
        )
    harness = _section(document, "harness", _HARNESS_KEYS)
    state = _section(document, "state", _STATE_KEYS)
    model = _section(document, "model", _MODEL_KEYS)
    workspace = _section(document, "workspace", _WORKSPACE_KEYS)
    sandbox_section = _section(document, "sandbox", _SANDBOX_KEYS)
    validation_section = _section(document, "validation", _VALIDATION_KEYS)
    implementation = _section(document, "implementation", _IMPLEMENTATION_KEYS)

    hosts = model.get("approved_hosts", [])
    if not isinstance(hosts, list) or not all(isinstance(item, str) for item in hosts):
        raise ConfigError(
            "config-invalid", "model.approved_hosts must be a list of host names."
        )
    provider = str(model.get("provider", "openai-compatible"))
    if provider != "openai-compatible":
        raise ConfigError("config-invalid", "model.provider must be 'openai-compatible'.")

    key_env = model.get("api_key_env", DEFAULT_ENV_KEY_NAME)
    if not isinstance(key_env, str) or not key_env:
        raise ConfigError(
            "config-invalid", "model.api_key_env must name an environment variable."
        )
    value = environ.get(key_env)
    api_key = value.strip() if value and value.strip() else None

    defaults = PydanticHarnessConfig()
    model_config = PydanticHarnessConfig(
        provider=provider,
        base_url=_optional_text(model, "base_url"),
        model_id=_optional_text(model, "model_id"),
        approved_hosts=tuple(item.strip().lower() for item in hosts if item.strip()),
        api_key=api_key,
        api_key_file=_optional_text(model, "api_key_file"),
        require_api_key=bool(model.get("require_api_key", True)),
        workspace_root=None,
        request_limit=_positive(model, "request_limit", defaults.request_limit),
        tool_call_limit=_positive(model, "tool_call_limit", defaults.tool_call_limit),
        total_token_limit=_positive(
            model, "total_token_limit", defaults.total_token_limit
        ),
        max_output_tokens=_positive(
            model, "max_output_tokens", defaults.max_output_tokens
        ),
    )
    root = _optional_text(harness, "root")
    state_dir = _optional_text(state, "dir")
    workspace_root = _optional_text(workspace, "root")
    return RuntimeConfig(
        harness_root=Path(root).expanduser() if root else None,
        state_dir=Path(state_dir).expanduser() if state_dir else None,
        model=model_config,
        timeout_seconds=_positive(model, "timeout_seconds", 600),
        source=source,
        workspace_root=Path(workspace_root).expanduser() if workspace_root else None,
        sandbox=_sandbox_config(sandbox_section),
        validation=_validation_config(validation_section),
        max_iterations=_positive(implementation, "max_iterations", 2),
    )


def _sandbox_config(section: Mapping[str, Any]) -> SandboxConfig | None:
    """``None`` when no ``[sandbox]`` table is declared: validation then refuses."""

    if not section:
        return None
    image = _optional_text(section, "image")
    if image is None:
        raise ConfigError(
            "config-invalid",
            "[sandbox] must declare image: the runtime never executes on the host.",
        )
    cpus = section.get("cpus", 2.0)
    if isinstance(cpus, bool) or not isinstance(cpus, (int, float)) or cpus <= 0:
        raise ConfigError("config-invalid", "sandbox.cpus must be a positive number.")
    return SandboxConfig(
        image=image,
        runtime=str(section.get("runtime", "docker")),
        timeout_seconds=_positive(section, "timeout_seconds", 300),
        cpus=float(cpus),
        memory_mb=_positive(section, "memory_mb", 2_048),
        pids_limit=_positive(section, "pids_limit", 256),
        tmpfs_mb=_positive(section, "tmpfs_mb", 256),
        network=str(section.get("network", "none")),
        startup_grace_seconds=_positive(section, "startup_grace_seconds", 30),
    )


def _validation_config(section: Mapping[str, Any]) -> ValidationConfig:
    raw = section.get("allowlist")
    if raw is None:
        allowlist = DEFAULT_VALIDATION_ALLOWLIST
    else:
        if not isinstance(raw, list) or not all(
            isinstance(entry, list) and entry and all(isinstance(item, str) for item in entry)
            for entry in raw
        ):
            raise ConfigError(
                "config-invalid",
                "validation.allowlist must be a list of argument lists.",
            )
        allowlist = tuple(tuple(entry) for entry in raw)
    return ValidationConfig(
        allowlist=allowlist,
        timeout_seconds=_positive(section, "timeout_seconds", 900),
        excerpt_chars=_positive(section, "excerpt_chars", 4_000),
    )


def _section(
    document: Mapping[str, Any], name: str, allowed: set[str]
) -> Mapping[str, Any]:
    section = document.get(name, {})
    if not isinstance(section, dict):
        raise ConfigError("config-invalid", f"[{name}] must be a table.")
    secrets = {key for key in section if key.lower() in _SECRET_KEYS}
    if secrets:
        raise ConfigError(
            "config-secret-in-file",
            f"[{name}] must not contain a credential value ({sorted(secrets)}); name an "
            "environment variable with api_key_env or a file with api_key_file.",
        )
    unknown = set(section) - allowed
    if unknown:
        raise ConfigError("config-invalid", f"Unknown keys in [{name}]: {sorted(unknown)}")
    return section


def _optional_text(section: Mapping[str, Any], key: str) -> str | None:
    value = section.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError("config-invalid", f"{key} must be a string.")
    return value.strip() or None


def _positive(section: Mapping[str, Any], key: str, default: int) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError("config-invalid", f"{key} must be a positive integer.")
    return value
