#!/usr/bin/env python3
"""Validate the harness assets with the unmodified asset validator.

The asset validator (``tools/validate_assets.py``) resolves every Markdown link
and every file named by the bundled contract examples against a repository root.
A few of those paths belong to whichever repository consumes the harness (see
``consumer-paths.json``), so this script assembles the shape a consumer has:

* the harness ``.github`` tree, without the harness's own ``workflows/``;
* the harness ``docs/workflows`` documents;
* one generated placeholder per consumer-owned path.

It then runs the validator against that root with no rule relaxed. ``--bare``
validates the checkout itself instead, which is expected to fail only on the
declared consumer paths; tests/test_consumer_paths.py enforces that.

Standard library only apart from what the validator imports (PyYAML, jsonschema).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "tools" / "validate_assets.py"
CONSUMER_PATHS = ROOT / "consumer-paths.json"


def assemble(stage: Path, root: Path = ROOT) -> None:
    shutil.copytree(
        root / ".github",
        stage / ".github",
        ignore=shutil.ignore_patterns("workflows"),
    )
    docs = root / "docs" / "workflows"
    if docs.is_dir():
        shutil.copytree(docs, stage / "docs" / "workflows")
    declared = json.loads((root / "consumer-paths.json").read_text(encoding="utf-8"))
    for entry in declared["paths"]:
        target = stage / entry["path"]
        if target.exists():
            raise SystemExit(f"consumer path {entry['path']} collides with a harness file")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(entry["stub"], encoding="utf-8")


def run_validator(repo_root: Path, extra: list) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(VALIDATOR), "--repo-root", str(repo_root), *extra],
        text=True,
        capture_output=True,
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bare", action="store_true", help="validate the checkout without consumer placeholders")
    parser.add_argument("--json", action="store_true", help="emit the validator's JSON report")
    parser.add_argument("--records-root", action="append", default=[], metavar="DIR")
    args = parser.parse_args(argv)

    extra = ["--json"] if args.json else []
    for record_root in args.records_root:
        extra += ["--records-root", record_root]

    if args.bare:
        result = run_validator(ROOT, extra)
    else:
        with tempfile.TemporaryDirectory(prefix="harness-validate.") as tmp:
            stage = Path(tmp) / "consumer-shape"
            stage.mkdir()
            assemble(stage)
            result = run_validator(stage, extra)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
