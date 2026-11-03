#!/usr/bin/env python3
"""Build the harness asset manifest.

The manifest is the harness repository's own inventory: every trusted file with
its SHA-256, plus the roles and skills it declares, plus one suite digest over
the whole set. It answers three questions without importing any application code:

1. what exactly does a consumer receive at this harness version;
2. did the bytes change since the version that was pinned; and
3. which role or skill does a given file belong to.

It is deliberately stdlib-only and reads nothing outside ``.github/``. The
authority for *semantic* digests (the catalog digest, each skill body digest,
each composed prompt digest) stays with the runtime that records them as
evidence; this manifest only proves byte identity.

Usage::

    python3 tools/build_manifest.py                # rewrite harness.manifest.json
    python3 tools/build_manifest.py --check        # exit 1 if the file is stale
    python3 tools/build_manifest.py --source-commit a3d88d4
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSET_ROOT = ROOT / ".github"
MANIFEST_PATH = ROOT / "harness.manifest.json"
MANIFEST_VERSION = 2

#: Directories under ``.github/`` that are this repository's own machinery, not a
#: consumer asset: the harness CI workflow. They are neither digested into the
#: suite nor installed, so adding or editing CI never changes the suite digest.
EXCLUDED_ASSET_DIRS = ("workflows",)

#: Harness documentation that sits beside ``.github/`` rather than inside it. The
#: assets link to it with ``../../docs/...`` paths, so it keeps that relative
#: position in a consumer. It is digested separately from the asset suite so a
#: documentation edit does not change ``suite_digest``, which recorded runs cite.
DOCS_ROOT = ROOT / "docs" / "workflows"
CONSUMER_PATHS_FILE = "consumer-paths.json"

BUCKETS = {
    "agents": "agent definitions (personas) that a role executes",
    "catalog": "review-specialist catalog entries",
    "skills": "SKILL.md procedures a role may load",
    "hooks": "lifecycle hook registry (data only)",
    "contracts": "task, handoff, evidence, and catalog JSON Schemas and examples",
    "instructions": "repository-level instructions for human and agent contributors",
}

#: The fixed executors the runtime names in code. A specialist is data instead.
CORE_ROLES = ("intake", "architect", "builder", "qa", "reviewer")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _bucket(relative: str) -> str:
    if relative == "copilot-instructions.md":
        return "instructions"
    head = relative.split("/", 1)[0]
    return head if head in BUCKETS else "other"


def _parse_frontmatter(text: str) -> dict[str, object]:
    """Parse the same ``key: value`` / ``key: [a, b]`` subset the loader accepts."""

    if not text.startswith("---"):
        return {}
    lines = text.splitlines()
    fields: dict[str, object] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, _, raw = line.partition(":")
        key = key.strip()
        value = raw.strip()
        if value.startswith("[") and value.endswith("]"):
            fields[key] = [
                item.strip().strip("'\"")
                for item in value[1:-1].split(",")
                if item.strip()
            ]
        else:
            fields[key] = value.strip("'\"")
    return fields


def _collect() -> dict[str, object]:
    files = sorted(
        path
        for path in ASSET_ROOT.rglob("*")
        if path.is_file()
        and path.relative_to(ASSET_ROOT).parts[0] not in EXCLUDED_ASSET_DIRS
    )
    assets: dict[str, dict[str, object]] = {}
    for path in files:
        relative = path.relative_to(ASSET_ROOT).as_posix()
        data = path.read_bytes()
        assets[relative] = {
            "bucket": _bucket(relative),
            "bytes": len(data),
            "sha256": _sha256(data),
        }

    agents: list[dict[str, object]] = []
    skills: list[dict[str, object]] = []
    hooks: list[dict[str, object]] = []
    catalog: list[dict[str, object]] = []

    for relative in assets:
        path = ASSET_ROOT / relative
        if relative.startswith("agents/") and relative.endswith(".agent.md"):
            front = _parse_frontmatter(path.read_text(encoding="utf-8"))
            agents.append(
                {
                    "file": relative,
                    "name": front.get("name"),
                    "tools": front.get("tools", []),
                    "skills": front.get("skills", []),
                    "core_role": relative.rsplit("/", 1)[-1]
                    .removeprefix("loomsphere-")
                    .removesuffix(".agent.md")
                    in CORE_ROLES,
                }
            )
        elif relative.startswith("skills/") and relative.endswith("/SKILL.md"):
            front = _parse_frontmatter(path.read_text(encoding="utf-8"))
            skills.append(
                {
                    "name": front.get("name"),
                    "file": relative,
                    "roles": front.get("roles", []),
                    "stages": front.get("stages", []),
                    "required": front.get("required") == "true",
                }
            )
        elif relative.startswith("hooks/") and relative.endswith(".json"):
            document = json.loads(path.read_text(encoding="utf-8"))
            hooks.append(
                {
                    "file": relative,
                    "name": document.get("name"),
                    "events": document.get("events", []),
                    "mode": document.get("mode"),
                }
            )
        elif relative.startswith("agents/catalog/") and relative.endswith(".json"):
            document = json.loads(path.read_text(encoding="utf-8"))
            catalog.append(
                {
                    "name": document.get("name"),
                    "file": relative,
                    "role": document.get("role"),
                    "stage": document.get("stage"),
                    "read_only": document.get("read_only"),
                    "tools": document.get("tools", []),
                    "skills": document.get("skills", []),
                }
            )

    suite = hashlib.sha256()
    for relative in sorted(assets):
        suite.update(relative.encode("utf-8"))
        suite.update(b"\0")
        suite.update(str(assets[relative]["sha256"]).encode("ascii"))
        suite.update(b"\n")

    docs: dict[str, dict[str, object]] = {}
    companions = sorted(DOCS_ROOT.glob("*.md")) + [ROOT / CONSUMER_PATHS_FILE]
    for path in companions:
        if not path.is_file():
            continue
        data = path.read_bytes()
        docs[path.relative_to(ROOT).as_posix()] = {
            "bytes": len(data),
            "sha256": _sha256(data),
        }
    docs_suite = hashlib.sha256()
    for relative in sorted(docs):
        docs_suite.update(relative.encode("utf-8"))
        docs_suite.update(b"\0")
        docs_suite.update(str(docs[relative]["sha256"]).encode("ascii"))
        docs_suite.update(b"\n")

    buckets: dict[str, int] = {}
    for entry in assets.values():
        buckets[entry["bucket"]] = buckets.get(entry["bucket"], 0) + 1

    return {
        "manifest_version": MANIFEST_VERSION,
        "assets": assets,
        "agents": sorted(agents, key=lambda item: str(item["file"])),
        "catalog": sorted(catalog, key=lambda item: str(item["name"])),
        "skills": sorted(skills, key=lambda item: str(item["name"])),
        "hooks": sorted(hooks, key=lambda item: str(item["name"])),
        "counts": {
            "files": len(assets),
            "bytes": sum(int(entry["bytes"]) for entry in assets.values()),
            "agents": len(agents),
            "catalog": len(catalog),
            "skills": len(skills),
            "hooks": len(hooks),
            "buckets": dict(sorted(buckets.items())),
        },
        "suite_digest": suite.hexdigest(),
        "docs": docs,
        "docs_digest": docs_suite.hexdigest(),
        "buckets": BUCKETS,
        "core_roles_named_in_runtime_code": list(CORE_ROLES),
    }


def _render(document: dict[str, object], source: dict[str, object]) -> str:
    merged = {"source": source, **document}
    return json.dumps(merged, indent=2, sort_keys=True) + "\n"


def _existing_source() -> dict[str, object]:
    if MANIFEST_PATH.is_file():
        try:
            return dict(json.loads(MANIFEST_PATH.read_text(encoding="utf-8")).get("source") or {})
        except (OSError, ValueError, TypeError):
            return {}
    return {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build harness.manifest.json")
    parser.add_argument("--check", action="store_true", help="verify instead of writing")
    parser.add_argument("--source-repository", default="gkamga/loomsphere")
    parser.add_argument("--source-prefix", default=".github")
    parser.add_argument("--source-commit")
    args = parser.parse_args(argv)

    if not ASSET_ROOT.is_dir():
        print(f"missing asset root: {ASSET_ROOT}", file=sys.stderr)
        return 2

    source = _existing_source()
    source.setdefault("repository", args.source_repository)
    source.setdefault("prefix", args.source_prefix)
    if args.source_commit:
        source["commit"] = args.source_commit
    source.setdefault("commit", None)

    document = _collect()
    rendered = _render(document, source)

    if args.check:
        if not MANIFEST_PATH.is_file():
            print("harness.manifest.json is missing", file=sys.stderr)
            return 1
        if MANIFEST_PATH.read_text(encoding="utf-8") != rendered:
            print(
                "harness.manifest.json is stale; regenerate with "
                "python3 tools/build_manifest.py",
                file=sys.stderr,
            )
            return 1
        print(
            f"manifest OK: {document['counts']['files']} files, "
            f"suite {document['suite_digest']}"
        )
        return 0

    MANIFEST_PATH.write_text(rendered, encoding="utf-8")
    print(
        f"wrote {MANIFEST_PATH.name}: {document['counts']['files']} files, "
        f"{document['counts']['bytes']} bytes, suite {document['suite_digest']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
