"""Trusted harness assets: location and integrity.

The assets (agent definitions, skills, catalog, contracts) are read from a
configured harness checkout that must live outside the target repository, so
nothing an agent can reach through the target worktree can alter them. Before any
model run the checkout is verified byte-for-byte against ``harness.manifest.json``
(the file set and each SHA-256), and a missing, extra, modified, or symlinked asset
fails closed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

MANIFEST_NAME = "harness.manifest.json"
SUPPORTED_MANIFEST_VERSION = 2
#: Directories under ``.github/`` that are the harness repository's own machinery
#: and are not digested (mirrors tools/build_manifest.py).
EXCLUDED_ASSET_DIRS = ("workflows",)


class AssetIntegrityError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class TrustedAssets:
    root: Path
    github_root: Path
    suite_digest: str
    file_count: int

    @property
    def agents_root(self) -> Path:
        return self.github_root / "agents"

    @property
    def catalog_root(self) -> Path:
        return self.agents_root / "catalog"

    @property
    def skills_root(self) -> Path:
        return self.github_root / "skills"

    @property
    def hooks_root(self) -> Path:
        return self.github_root / "hooks"

    def persona_path(self, role: str) -> Path:
        return self.agents_root / f"loomsphere-{role}.agent.md"


#: Persona-path settings a catalog entry may name, relative to ``.github/agents``.
#: Same names and files as the LoomSphere application derives from its harness root.
PERSONA_FILES = {
    "ARCHITECT_PERSONA_PATH": "loomsphere-architect.agent.md",
    "INTAKE_PERSONA_PATH": "loomsphere-intake.agent.md",
    "BUILDER_PERSONA_PATH": "loomsphere-builder.agent.md",
    "QA_PERSONA_PATH": "loomsphere-qa.agent.md",
    "REVIEWER_PERSONA_PATH": "loomsphere-reviewer.agent.md",
    "SECURITY_REVIEWER_PERSONA_PATH": "loomsphere-security-reviewer.agent.md",
    "UX_REVIEWER_PERSONA_PATH": "loomsphere-ux-reviewer.agent.md",
}


class CatalogSettings:
    """The object bound into ``harness_settings`` for the catalog loader. Every
    path derives from the verified checkout; nothing is read from the environment."""

    def __init__(self, assets: TrustedAssets) -> None:
        self.AGENT_CATALOG_ROOT = str(assets.catalog_root)
        self.AGENT_DEFINITIONS_ROOT = str(assets.agents_root)
        self.SKILLS_ROOT = str(assets.skills_root)
        for name, filename in PERSONA_FILES.items():
            setattr(self, name, str(assets.agents_root / filename))


def verify_assets(
    harness_root: str | Path | None, *, target_repo: str | Path | None = None
) -> TrustedAssets:
    if harness_root is None:
        raise AssetIntegrityError(
            "harness-root-not-configured",
            "[harness] root is not configured; point it at a trusted harness checkout.",
        )
    root = Path(harness_root).expanduser().resolve()
    if target_repo is not None:
        target = Path(target_repo).expanduser().resolve()
        if root == target or target in root.parents or root in target.parents:
            raise AssetIntegrityError(
                "harness-root-inside-target",
                "The trusted harness checkout must be separate from the target repository.",
            )
    manifest_path = root / MANIFEST_NAME
    github = root / ".github"
    if not manifest_path.is_file() or not github.is_dir():
        raise AssetIntegrityError(
            "assets-missing",
            f"{root} is not a harness checkout (missing {MANIFEST_NAME} or .github/).",
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        recorded = manifest["assets"]
        suite_digest = str(manifest["suite_digest"])
        version = manifest["manifest_version"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise AssetIntegrityError(
            "manifest-corrupt", "The harness manifest could not be read."
        ) from error
    if version != SUPPORTED_MANIFEST_VERSION or not isinstance(recorded, dict):
        raise AssetIntegrityError(
            "manifest-corrupt", "The harness manifest version is not supported."
        )

    found: dict[str, Path] = {}
    for path in sorted(github.rglob("*")):
        relative = path.relative_to(github)
        if relative.parts[0] in EXCLUDED_ASSET_DIRS:
            continue
        if path.is_symlink():
            raise AssetIntegrityError(
                "asset-symlink", f"Asset {relative.as_posix()} is a symbolic link."
            )
        if path.is_file():
            found[relative.as_posix()] = path

    missing = sorted(set(recorded) - set(found))
    extra = sorted(set(found) - set(recorded))
    if missing or extra:
        raise AssetIntegrityError(
            "asset-set-changed",
            f"The asset set differs from the manifest (missing: {missing[:5]}, "
            f"unexpected: {extra[:5]}).",
        )
    for relative, path in found.items():
        expected = recorded[relative].get("sha256") if isinstance(recorded[relative], dict) else None
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise AssetIntegrityError(
                "asset-modified", f"Asset {relative} does not match the manifest digest."
            )
    return TrustedAssets(
        root=root, github_root=github, suite_digest=suite_digest, file_count=len(found)
    )
