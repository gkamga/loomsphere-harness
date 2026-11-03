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
#: Keys that would put a secret in the file; refused rather than ignored.
_SECRET_KEYS = {"api_key", "key", "token", "password", "secret"}


class ConfigError(RuntimeError):
    """The configuration is unusable. The runtime fails closed on it."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RuntimeConfig:
    harness_root: Path | None
    state_dir: Path | None
    model: PydanticHarnessConfig
    timeout_seconds: int = 600
    source: str | None = None


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
    unknown = set(document) - {"harness", "model", "state"}
    if unknown:
        raise ConfigError(
            "config-invalid", f"Unknown configuration sections: {sorted(unknown)}"
        )
    harness = _section(document, "harness", _HARNESS_KEYS)
    state = _section(document, "state", _STATE_KEYS)
    model = _section(document, "model", _MODEL_KEYS)

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
    return RuntimeConfig(
        harness_root=Path(root).expanduser() if root else None,
        state_dir=Path(state_dir).expanduser() if state_dir else None,
        model=model_config,
        timeout_seconds=_positive(model, "timeout_seconds", 600),
        source=source,
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
