#!/usr/bin/env python3
"""Install, verify, update, and remove LoomSphere harness assets in another repository.

This is declarative asset consumption only. It copies Markdown and JSON files
(agent definitions, skills, hooks, contracts, workflow documents, optionally the
repository instructions) into a consuming Git repository so that GitHub Copilot
and DeepSeek Harness can discover them. It does not run an agent, enforce an
approval, or start any service; the autonomous engine is a separate, later piece.

Every file is taken from ``git archive <commit>`` of the harness repository, so
the bytes are exactly those of the pinned commit, the harness checkout is never
modified, and a dirty working tree cannot leak in. The archive is verified
against that commit's own ``harness.manifest.json`` before anything is written.

Nothing the consumer already has is overwritten without explicit authorization.
Authorized overwrites are backed up and restored on uninstall.

    harness_install.py install   --target REPO --ref COMMIT_OR_TAG [--source DIR|--url URL]
    harness_install.py verify    --target REPO [--source DIR] [--strict]
    harness_install.py update    --target REPO --ref COMMIT_OR_TAG [--source DIR|--url URL]
    harness_install.py uninstall --target REPO [--force]
    harness_install.py discover  --target REPO [--json]

Exit codes: 0 success, 1 verification or integrity failure, 2 invalid usage,
3 conflicts that need explicit authorization.

Standard library and git only (Python 3.9+).
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
DEFAULT_SOURCE = HERE.parent
LOCK_NAME = ".harness-lock.json"
BACKUP_DIR = ".harness-backup"
LOCK_VERSION = 1
DEFAULT_BUCKETS = ("agents", "skills", "hooks", "contracts", "docs")
ALL_BUCKETS = DEFAULT_BUCKETS + ("instructions",)
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


class Usage(Exception):
    """Invalid invocation (exit 2)."""


class Conflicts(Exception):
    """Overwrites that were not authorized (exit 3)."""

    def __init__(self, items: List[Tuple[str, str]]) -> None:
        super().__init__("conflicts")
        self.items = items


class Integrity(Exception):
    """Source or installation bytes do not match their recorded digest (exit 1)."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if check and result.returncode != 0:
        raise Usage(f"git {' '.join(args)} failed: {result.stderr.decode('utf-8', 'replace').strip()}")
    return result


# --------------------------------------------------------------------------- source


class Source:
    """A harness commit, extracted to a temporary directory and verified."""

    def __init__(self, repo: Path, ref: str, url: Optional[str]) -> None:
        self.repo = repo
        self.url = url
        self.ref = ref
        self.commit = self._resolve(ref)
        self._tmp = tempfile.TemporaryDirectory(prefix="harness-src.")
        self.tree = Path(self._tmp.name) / "tree"
        self.tree.mkdir()
        self._extract()
        self.manifest = self._load_manifest()

    def close(self) -> None:
        self._tmp.cleanup()

    def _resolve(self, ref: str) -> str:
        # A branch moves, so it cannot pin anything. Require a commit or a tag.
        for prefix in ("refs/heads/", "refs/remotes/origin/"):
            if git(self.repo, "show-ref", "--verify", "--quiet", prefix + ref, check=False).returncode == 0:
                raise Usage(f"'{ref}' is a branch; pin a commit SHA or a tag instead")
        found = git(self.repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
        commit = found.stdout.decode().strip()
        if found.returncode != 0 or not FULL_SHA.match(commit):
            raise Usage(f"cannot resolve '{ref}' to a commit in {self.repo}")
        return commit

    def _extract(self) -> None:
        archive = git(self.repo, "archive", "--format=tar", self.commit).stdout
        self._extract_archive(archive)

    def _extract_archive(self, archive: bytes) -> None:
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            # Validate the whole archive first so a bad entry extracts nothing.
            members = [(member, destination) for member, destination in self._validate(tar)]
            for member, destination in members:
                try:
                    self._write(tar, member, destination)
                except OSError as error:
                    raise Integrity(f"cannot extract {member.name!r}: {error}") from error

    #: Only ordinary files and directories may be extracted. ``isreg()`` is not
    #: used because it also accepts contiguous and GNU sparse variants.
    SAFE_TYPES = (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE)

    def _validate(self, tar: "tarfile.TarFile"):
        """Yield each member with its resolved destination, refusing anything unsafe."""

        root = self.tree.resolve()
        for member in tar.getmembers():
            if member.issym() or member.islnk():
                raise Integrity(f"harness commit contains a link: {member.name}")
            if member.type not in self.SAFE_TYPES:
                raise Integrity(f"unsupported entry type in harness archive: {member.name}")
            name = Path(member.name)
            if name.is_absolute() or ".." in name.parts:
                raise Integrity(f"unsafe path in harness archive: {member.name}")
            destination = (root / name).resolve()
            if destination != root and root not in destination.parents:
                raise Integrity(f"archive entry escapes the extraction directory: {member.name}")
            if not member.isdir() and destination == root:
                raise Integrity(f"archive entry has no usable name: {member.name!r}")
            yield member, destination

    def _write(self, tar: "tarfile.TarFile", member: "tarfile.TarInfo", destination: Path) -> None:
        if member.isdir():
            destination.mkdir(parents=True, exist_ok=True)
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = tar.extractfile(member)
        if source is None:
            raise Integrity(f"cannot read archive entry: {member.name}")
        with source, open(destination, "wb") as handle:
            shutil.copyfileobj(source, handle)
        os.chmod(destination, (member.mode & 0o777) or 0o644)

    def _load_manifest(self) -> dict:
        path = self.tree / "harness.manifest.json"
        if not path.is_file():
            raise Integrity(f"commit {self.commit[:12]} has no harness.manifest.json")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        problems = self.verify_against_manifest(manifest)
        if problems:
            raise Integrity("harness source fails its own manifest: " + "; ".join(problems[:5]))
        return manifest

    def verify_against_manifest(self, manifest: dict) -> List[str]:
        problems: List[str] = []
        github = self.tree / ".github"
        for rel, entry in manifest["assets"].items():
            path = github / rel
            if not path.is_file():
                problems.append(f"missing {rel}")
            elif sha256_bytes(path.read_bytes()) != entry["sha256"]:
                problems.append(f"modified {rel}")
        for rel, entry in (manifest.get("docs") or {}).items():
            path = self.tree / rel
            if not path.is_file():
                problems.append(f"missing {rel}")
            elif sha256_bytes(path.read_bytes()) != entry["sha256"]:
                problems.append(f"modified {rel}")
        return problems

    def plan(self, buckets: List[str]) -> Dict[str, Tuple[str, bytes]]:
        """Map destination -> (bucket, bytes) for the selected buckets."""

        planned: Dict[str, Tuple[str, bytes]] = {}
        for rel, entry in sorted(self.manifest["assets"].items()):
            bucket = entry["bucket"]
            if bucket in buckets:
                planned[f".github/{rel}"] = (bucket, (self.tree / ".github" / rel).read_bytes())
        if "docs" in buckets:
            for rel in sorted((self.manifest.get("docs") or {})):
                if rel.startswith("docs/"):
                    planned[rel] = ("docs", (self.tree / rel).read_bytes())
        return planned


def open_source(args: argparse.Namespace) -> Source:
    if args.url:
        clone = tempfile.mkdtemp(prefix="harness-clone.")
        done = subprocess.run(["git", "clone", "--quiet", "--no-checkout", args.url, clone], capture_output=True)
        if done.returncode != 0:
            shutil.rmtree(clone, ignore_errors=True)
            raise Usage(f"cannot clone {args.url}: {done.stderr.decode('utf-8', 'replace').strip()}")
        try:
            source = Source(Path(clone), args.ref, args.url)
        except BaseException:
            shutil.rmtree(clone, ignore_errors=True)
            raise
        source._clone = clone  # type: ignore[attr-defined]
        return source
    repo = Path(args.source).resolve() if args.source else DEFAULT_SOURCE
    if not (repo / ".git").exists():
        raise Usage(f"{repo} is not a Git checkout of the harness")
    return Source(repo, args.ref, None)


def close_source(source: Source) -> None:
    clone = getattr(source, "_clone", None)
    source.close()
    if clone:
        shutil.rmtree(clone, ignore_errors=True)


# --------------------------------------------------------------------------- target


def resolve_target(raw: str) -> Path:
    target = Path(raw).resolve()
    if not target.is_dir():
        raise Usage(f"target {target} is not a directory")
    if not (target / ".git").exists():
        raise Usage(f"target {target} is not a Git repository; changes must be reviewable and revertible")
    return target


def safe_dest(target: Path, dest: str) -> Path:
    path = Path(dest)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise Integrity(f"unsafe destination path: {dest}")
    current = target
    for part in path.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise Integrity(f"destination parent is a symlink: {current.relative_to(target)}")
    return target / path


def load_lock(target: Path) -> Optional[dict]:
    path = target / LOCK_NAME
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_lock(target: Path, lock: dict) -> None:
    (target / LOCK_NAME).write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def current_sha(path: Path) -> Optional[str]:
    if path.is_symlink() or not path.is_file():
        return None
    return sha256_bytes(path.read_bytes())


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".harness-tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def backup_file(target: Path, stamp: str, dest: str) -> str:
    relative = f"{BACKUP_DIR}/{stamp}/{dest}"
    destination = target / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(target / dest, destination)
    return relative


def prune_empty(target: Path, start: Path) -> None:
    current = start
    while current != target and current.is_dir() and not any(current.iterdir()):
        current.rmdir()
        current = current.parent


# --------------------------------------------------------------------------- commands


def authorized(args: argparse.Namespace, dest: str) -> bool:
    return bool(args.authorize_all_overwrites) or dest in (args.authorize_overwrite or [])


def apply_plan(args: argparse.Namespace, target: Path, source: Source, buckets: List[str], previous: Optional[dict]) -> dict:
    planned = source.plan(buckets)
    old_files: Dict[str, dict] = dict((previous or {}).get("files") or {})
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")

    conflicts: List[Tuple[str, str]] = []
    actions: List[Tuple[str, str, str, Optional[dict]]] = []  # kind, dest, bucket, extra
    for dest, (bucket, data) in planned.items():
        path = safe_dest(target, dest)
        new_sha = sha256_bytes(data)
        owned = old_files.get(dest)
        if path.is_symlink() or path.is_dir():
            conflicts.append((dest, "destination is a symlink or directory"))
            continue
        if not path.exists():
            actions.append(("write", dest, bucket, None))
            continue
        existing = current_sha(path)
        if existing == new_sha:
            actions.append(("keep", dest, bucket, {"preexisting": bool(owned["preexisting"]) if owned else True,
                                                   "backup": owned.get("backup") if owned else None}))
        elif owned and existing == owned["sha256"]:
            # A consumer file that merely matched the old harness bytes is backed up
            # before it changes, so uninstall can still give it back.
            kind = "overwrite" if owned["preexisting"] else "write"
            actions.append((kind, dest, bucket, {"preexisting": False, "backup": owned.get("backup")}))
        else:
            reason = "modified since install" if owned else "exists and differs from the harness file"
            if not authorized(args, dest):
                conflicts.append((dest, reason))
            else:
                actions.append(("overwrite", dest, bucket, {"preexisting": False,
                                                            "backup": (owned or {}).get("backup")}))
    if conflicts:
        raise Conflicts(conflicts)

    removals: List[str] = [dest for dest in old_files if dest not in planned]
    removal_notes: List[str] = []
    if args.dry_run:
        for kind, dest, _bucket, _extra in actions:
            print(f"would {kind}: {dest}")
        for dest in removals:
            print(f"would drop (no longer shipped): {dest}")
        return previous or {}

    files: Dict[str, dict] = {}
    for kind, dest, bucket, extra in actions:
        path = safe_dest(target, dest)
        data = planned[dest][1]
        entry = {"bucket": bucket, "sha256": sha256_bytes(data), "bytes": len(data),
                 "preexisting": False, "backup": None}
        if extra:
            entry["preexisting"] = bool(extra["preexisting"])
            entry["backup"] = extra["backup"]
        if kind == "overwrite":
            entry["backup"] = entry["backup"] or backup_file(target, stamp, dest)
            atomic_write(path, data)
        elif kind == "write":
            atomic_write(path, data)
        files[dest] = entry

    for dest in removals:
        entry = old_files[dest]
        path = safe_dest(target, dest)
        if entry["preexisting"]:
            continue
        if current_sha(path) == entry["sha256"]:
            path.unlink()
            if entry.get("backup"):
                restore = target / entry["backup"]
                if restore.is_file():
                    atomic_write(path, restore.read_bytes())
            prune_empty(target, path.parent)
        elif path.exists():
            removal_notes.append(dest)
    for dest in removal_notes:
        print(f"kept locally modified file that the new version no longer ships: {dest}")

    history = list((previous or {}).get("history") or [])
    if previous:
        history.append({"commit": previous["source"]["commit"], "replacedAt": stamp})
    lock = {
        "lockVersion": LOCK_VERSION,
        "source": {
            "repository": source.url or str(source.repo),
            "ref": source.ref,
            "commit": source.commit,
            "manifestVersion": source.manifest.get("manifest_version"),
            "suiteDigest": source.manifest["suite_digest"],
            "docsDigest": source.manifest.get("docs_digest"),
        },
        "buckets": buckets,
        "installedAt": stamp,
        "files": dict(sorted(files.items())),
        "history": history,
    }
    write_lock(target, lock)
    return lock


def parse_buckets(args: argparse.Namespace, previous: Optional[dict]) -> List[str]:
    chosen = args.bucket or (previous or {}).get("buckets") or list(DEFAULT_BUCKETS)
    unknown = [item for item in chosen if item not in ALL_BUCKETS]
    if unknown:
        raise Usage(f"unknown bucket(s): {', '.join(unknown)}; choose from {', '.join(ALL_BUCKETS)}")
    return list(dict.fromkeys(chosen))


def cmd_install(args: argparse.Namespace, updating: bool) -> int:
    target = resolve_target(args.target)
    previous = load_lock(target)
    if updating and previous is None:
        raise Usage("nothing is installed here; run install first")
    if not updating and previous is not None:
        raise Usage("a harness is already installed here; use update, or uninstall first")
    buckets = parse_buckets(args, previous)
    source = open_source(args)
    try:
        if args.expect_suite_digest and source.manifest["suite_digest"] != args.expect_suite_digest:
            raise Integrity(
                f"suite digest {source.manifest['suite_digest']} does not match the expected "
                f"{args.expect_suite_digest}"
            )
        if updating and previous["source"]["commit"] == source.commit and not args.dry_run:
            print(f"already at {source.commit}")
            return 0
        lock = apply_plan(args, target, source, buckets, previous)
        if args.dry_run:
            return 0
        verb = "updated" if updating else "installed"
        print(f"{verb} {len(lock['files'])} files from {source.commit} (suite {source.manifest['suite_digest'][:12]})")
        return 0
    finally:
        close_source(source)


def cmd_verify(args: argparse.Namespace) -> int:
    target = resolve_target(args.target)
    lock = load_lock(target)
    if lock is None:
        print(f"no {LOCK_NAME}; nothing is installed", file=sys.stderr)
        return 1
    problems: List[str] = []
    for dest, entry in lock["files"].items():
        path = target / dest
        if path.is_symlink():
            problems.append(f"symlink in place of file: {dest}")
        elif not path.is_file():
            problems.append(f"missing: {dest}")
        elif sha256_bytes(path.read_bytes()) != entry["sha256"]:
            problems.append(f"modified: {dest}")

    if args.source:
        repo = Path(args.source).resolve()
        commit = lock["source"]["commit"]
        shown = git(repo, "show", f"{commit}:harness.manifest.json", check=False)
        if shown.returncode != 0:
            problems.append(f"pinned commit {commit[:12]} is not available in {repo}")
        else:
            manifest = json.loads(shown.stdout)
            if manifest["suite_digest"] != lock["source"]["suiteDigest"]:
                problems.append("lock suite digest differs from the pinned commit's manifest")
            for dest, entry in lock["files"].items():
                if dest.startswith(".github/"):
                    expected = manifest["assets"].get(dest[len(".github/"):])
                else:
                    expected = (manifest.get("docs") or {}).get(dest)
                if expected is None or expected["sha256"] != entry["sha256"]:
                    problems.append(f"lock entry does not match the pinned commit: {dest}")

    if args.strict:
        known = set(lock["files"])
        for top in (".github/agents", ".github/skills", ".github/contracts", ".github/hooks"):
            base = target / top
            if base.is_dir() and any(lock_prefix.startswith(top + "/") for lock_prefix in known):
                for path in sorted(p for p in base.rglob("*") if p.is_file()):
                    relative = path.relative_to(target).as_posix()
                    if relative not in known:
                        problems.append(f"unmanaged file in a managed tree: {relative}")

    if problems:
        for problem in problems:
            print(f"INTEGRITY {problem}", file=sys.stderr)
        print(f"verify FAILED: {len(problems)} problem(s)", file=sys.stderr)
        return 1
    print(f"verify OK: {len(lock['files'])} files match {lock['source']['commit'][:12]}")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    target = resolve_target(args.target)
    lock = load_lock(target)
    if lock is None:
        print("nothing is installed", file=sys.stderr)
        return 1
    remaining: Dict[str, dict] = {}
    for dest, entry in lock["files"].items():
        path = safe_dest(target, dest)
        if entry["preexisting"]:
            continue
        present = current_sha(path)
        if present is None and not path.exists() and not path.is_symlink():
            if entry.get("backup") and (target / entry["backup"]).is_file():
                atomic_write(path, (target / entry["backup"]).read_bytes())
            continue
        if present != entry["sha256"] and not args.force:
            remaining[dest] = entry
            print(f"left in place (modified since install; use --force to remove): {dest}", file=sys.stderr)
            continue
        if path.is_symlink() or path.is_file():
            path.unlink()
        if entry.get("backup") and (target / entry["backup"]).is_file():
            atomic_write(path, (target / entry["backup"]).read_bytes())
        else:
            prune_empty(target, path.parent)
    if remaining:
        lock["files"] = remaining
        write_lock(target, lock)
        print(f"uninstall incomplete: {len(remaining)} file(s) kept", file=sys.stderr)
        return 1
    (target / LOCK_NAME).unlink()
    shutil.rmtree(target / BACKUP_DIR, ignore_errors=True)
    print("uninstalled; original files restored")
    return 0


FRONT = re.compile(r"\A---\n(.*?)\n---", re.S)


def front_matter(path: Path) -> Dict[str, object]:
    match = FRONT.match(path.read_text(encoding="utf-8"))
    fields: Dict[str, object] = {}
    if not match:
        return fields
    for line in match.group(1).splitlines():
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, _, raw = line.partition(":")
        value = raw.strip()
        if value.startswith("[") and value.endswith("]"):
            fields[key.strip()] = [i.strip().strip("'\"") for i in value[1:-1].split(",") if i.strip()]
        else:
            fields[key.strip()] = value.strip("'\"")
    return fields


def cmd_discover(args: argparse.Namespace) -> int:
    target = resolve_target(args.target)
    problems: List[str] = []
    agents = []
    for path in sorted((target / ".github" / "agents").glob("*.agent.md")):
        front = front_matter(path)
        if not front.get("name") or not front.get("description"):
            problems.append(f"agent without name/description: {path.name}")
        agents.append({"file": path.name, "name": front.get("name"), "skills": front.get("skills", [])})
    skills = []
    for path in sorted((target / ".github" / "skills").glob("*/SKILL.md")):
        front = front_matter(path)
        if front.get("name") != path.parent.name:
            problems.append(f"skill name '{front.get('name')}' does not match directory '{path.parent.name}'")
        if not front.get("description"):
            problems.append(f"skill without description: {path.parent.name}")
        skills.append({"name": path.parent.name, "roles": front.get("roles", []), "stages": front.get("stages", [])})
    have = {item["name"] for item in skills}
    for agent in agents:
        for needed in agent["skills"]:  # type: ignore[union-attr]
            if needed not in have:
                problems.append(f"agent '{agent['name']}' declares missing skill '{needed}'")
    catalog = sorted(p.name for p in (target / ".github" / "agents" / "catalog").glob("*.json"))
    report = {
        "agents": agents,
        "catalog": catalog,
        "skills": skills,
        "instructions": (target / ".github" / "copilot-instructions.md").is_file(),
        "copilot": {
            "agents": ".github/agents/*.agent.md",
            "skills": ".github/skills/<name>/SKILL.md",
            "instructions": ".github/copilot-instructions.md",
        },
        "deepseekHarness": {"customSkillDirs": [str(target / ".github" / "skills")]},
        "problems": problems,
    }
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"agents ({len(agents)}): " + ", ".join(str(a["name"]) for a in agents))
        print(f"catalog specialists ({len(catalog)}): " + ", ".join(catalog))
        print(f"skills ({len(skills)}): " + ", ".join(str(s["name"]) for s in skills))
        print(f"DeepSeek Harness customSkillDirs: {report['deepseekHarness']['customSkillDirs']}")
        for problem in problems:
            print(f"PROBLEM {problem}", file=sys.stderr)
    return 1 if problems or not agents or not skills else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Consume LoomSphere harness assets in another repository.")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser, needs_ref: bool) -> None:
        p.add_argument("--target", default=".", help="consuming Git repository (default: current directory)")
        if needs_ref:
            p.add_argument("--ref", required=True, help="commit SHA or tag to pin (branches are refused)")
            p.add_argument("--source", help="local harness Git checkout (default: the checkout holding this script)")
            p.add_argument("--url", help="clone the harness from this URL instead of --source")
            p.add_argument("--bucket", action="append", help=f"asset group to install; repeatable; one of {', '.join(ALL_BUCKETS)}")
            p.add_argument("--expect-suite-digest", help="refuse the source unless its manifest suite digest matches")
            p.add_argument("--authorize-overwrite", action="append", metavar="PATH", help="allow replacing this existing file (backed up first)")
            p.add_argument("--authorize-all-overwrites", action="store_true", help="allow replacing every conflicting file (backed up first)")
            p.add_argument("--dry-run", action="store_true")

    common(sub.add_parser("install", help="install assets at a pinned revision"), True)
    common(sub.add_parser("update", help="move an installation to another reviewed revision"), True)
    verify = sub.add_parser("verify", help="check installed files against the lock")
    common(verify, False)
    verify.add_argument("--source", help="harness checkout used to also check the lock against the pinned commit's manifest")
    verify.add_argument("--strict", action="store_true", help="also fail on unmanaged files inside managed trees")
    uninstall = sub.add_parser("uninstall", help="remove installed files and restore backups")
    common(uninstall, False)
    uninstall.add_argument("--force", action="store_true", help="remove files even if modified since install")
    discover = sub.add_parser("discover", help="list the agents and skills a consumer will see")
    common(discover, False)
    discover.add_argument("--json", action="store_true")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "install":
            return cmd_install(args, updating=False)
        if args.command == "update":
            return cmd_install(args, updating=True)
        if args.command == "verify":
            return cmd_verify(args)
        if args.command == "uninstall":
            return cmd_uninstall(args)
        return cmd_discover(args)
    except Conflicts as conflict:
        for dest, reason in conflict.items:
            print(f"CONFLICT {dest}: {reason}", file=sys.stderr)
        print(
            f"{len(conflict.items)} conflict(s); nothing was written. Re-run with "
            "--authorize-overwrite PATH (repeatable) or --authorize-all-overwrites to replace them; "
            "originals are backed up.",
            file=sys.stderr,
        )
        return 3
    except Integrity as error:
        print(f"INTEGRITY {error}", file=sys.stderr)
        return 1
    except Usage as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
