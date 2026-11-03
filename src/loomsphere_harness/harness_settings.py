"""The settings seam for the portable catalog loader.

The catalog loader needs three roots and, optionally, a persona-path override
named by a catalog entry. It reads them from one bound object instead of importing
an application settings module. The standalone runtime binds a small object derived
from its own configuration; the LoomSphere application binds its own settings. An
unbound access fails closed.
"""

from __future__ import annotations

from typing import Any


class _SettingsProxy:
    _bound: Any = None

    def bind(self, source: object) -> None:
        object.__setattr__(self, "_bound", source)

    def __getattr__(self, name: str) -> Any:
        bound = object.__getattribute__(self, "_bound")
        if bound is None:
            raise RuntimeError(
                "harness settings are not bound; call "
                "loomsphere_harness.harness_settings.bind_settings() first"
            )
        return getattr(bound, name)


settings = _SettingsProxy()


def bind_settings(source: object) -> None:
    """Bind the object that provides AGENT_CATALOG_ROOT, AGENT_DEFINITIONS_ROOT,
    SKILLS_ROOT, and any persona-path setting a catalog entry names."""

    settings.bind(source)
