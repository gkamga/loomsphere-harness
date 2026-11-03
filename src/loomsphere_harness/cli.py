"""``loomsphere-harness`` command line: Intake -> Architect -> human approval.

There is deliberately no Builder, QA, or Reviewer command. The commands are
init, submit, run, show, approve, reject, status, and audit.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from loomsphere_harness.assets import AssetIntegrityError
from loomsphere_harness.config import ConfigError, load_config
from loomsphere_harness.approval import ApprovalError
from loomsphere_harness.engine import Engine, EngineError, state_path
from loomsphere_harness.repository import GitRepository, OsUserIdentity, RepositoryError
from loomsphere_harness.store import SqliteStore, StoreError

COMMANDS = ("init", "submit", "run", "show", "approve", "reject", "status", "audit")
EXPECTED_ERRORS = (
    ConfigError, AssetIntegrityError, EngineError, StoreError, RepositoryError, ApprovalError,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loomsphere-harness",
        description="Standalone LoomSphere Intake -> Architect -> approval runtime.",
    )
    parser.add_argument("--config", help="TOML configuration file")
    parser.add_argument("--repo", default=".", help="path inside the target Git repository")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="bind to an existing Git repository and verify assets")
    submit = sub.add_parser("submit", help="record an engineering requirement")
    submit.add_argument("--title", required=True)
    submit.add_argument("--requirements", help="text, or use --requirements-file")
    submit.add_argument("--requirements-file")
    submit.add_argument("--acceptance", help="acceptance criteria text")
    submit.add_argument("--acceptance-file")
    run = sub.add_parser("run", help="run Intake then Architect for a task")
    run.add_argument("task")
    show = sub.add_parser("show", help="show intake, proposal, criteria, and approval")
    show.add_argument("task")
    approve = sub.add_parser("approve", help="approve the exact proposal digest")
    approve.add_argument("task")
    approve.add_argument("--digest", required=True, help="proposal SHA-256 shown by `show`")
    reject = sub.add_parser("reject", help="reject the exact proposal digest")
    reject.add_argument("task")
    reject.add_argument("--digest", required=True)
    reject.add_argument("--reason", required=True)
    status = sub.add_parser("status", help="task state and execution evidence")
    status.add_argument("task", nargs="?")
    audit = sub.add_parser("audit", help="audit trail (verifies the hash chain)")
    audit.add_argument("task", nargs="?")
    return parser


def _text(value: str | None, file: str | None, label: str) -> str:
    if value and file:
        raise EngineError("invalid-request", f"Give {label} inline or as a file, not both.")
    if file:
        return Path(file).read_text(encoding="utf-8")
    if value:
        return value
    raise EngineError("invalid-request", f"{label} is required.")


def _engine(args: argparse.Namespace, *, create: bool = False) -> Engine:
    config = load_config(args.config)
    repository = GitRepository(args.repo)
    store = SqliteStore(state_path(repository.path, config))
    return Engine(
        store=store, config=config, repository=repository,
        identity=OsUserIdentity(), repo_path=repository.path,
    )


def _emit(args: argparse.Namespace, payload: Any, text: str) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str) if args.json else text)


def render_task(view: dict[str, Any]) -> str:
    task = view["task"]
    lines = [f"{task['id']}  {task['status']}  {task['title']}",
             f"repository {task['repo_ref']} @ {task['repo_commit'][:12]}"
             + (" (dirty worktree)" if task["repo_dirty"] else "")]
    intake = view.get("intake")
    if intake:
        doc = intake["document"]
        lines += ["", f"Intake {intake['digest'][:12]}: {doc['intent']['summary']}",
                  f"  risk {doc['risk']['level']}, type {doc['intent']['request_type']}"]
        for item in doc["ambiguities"]:
            lines.append(f"  {'BLOCKING ' if item['blocking'] else ''}question: {item['question']}")
    proposal = view.get("proposal")
    lines += ["", "Acceptance criteria as submitted:", *("  " + l for l in task["acceptance_criteria"].splitlines())]
    if proposal:
        doc = proposal["document"]
        lines += ["", f"Proposal digest: {proposal['digest']}  ({proposal['status']})", f"  {doc['summary']}",
                  "Components:", *(f"  - {c['path']}: {c['change']}" for c in doc["components"]),
                  "Acceptance criteria (proposed mapping):"]
        for item in doc["acceptance_criteria_mapping"]:
            check = item.get("verification")
            kind = f" [{check['kind']}]" if check else " [unverified]"
            lines.append(f"  - {'MUST ' if item['mandatory'] else ''}{item['criterion']}{kind}")
        for label, key in (("Risks", "risks"), ("Open questions", "open_questions")):
            if doc[key]:
                lines += [f"{label}:", *(f"  - {x}" for x in doc[key])]
    approval = view.get("approval")
    if approval:
        lines += ["", f"Decision: {approval['decision']} by {approval['actor']} at "
                      f"{approval['decided_at']} (digest {approval['proposal_digest'][:12]})"]
    return "\n".join(lines)


def render_status(view: dict[str, Any]) -> str:
    lines = [render_task({**view, "proposal": None, "intake": None, "approval": None}).splitlines()[0], "Runs:"]
    for run in view["runs"]:
        usage = (run.get("events") or {}).get("usage") or {}
        lines.append(
            f"  {run['role']:<9} {run['status']:<11} model={run['model']} prompt={str(run['prompt_digest'])[:12]}"
            f" tokens={usage.get('input_tokens', '-')}/{usage.get('output_tokens', '-')}"
            + (f" failure={run['failure_reason']}" if run["failure_reason"] else "")
        )
        activity = (run.get("events") or {}).get("tool_activity") or []
        if activity:
            lines.append("    tools: " + ", ".join(sorted({a["tool"] for a in activity})))
    return "\n".join(lines)


def run_command(args: argparse.Namespace) -> int:
    engine = _engine(args)
    command = args.command
    if command == "init":
        result = engine.initialize()
        _emit(args, result, f"initialized {result['repo']} @ {result['commit'][:12]}\n"
              f"verified {result['assets']} assets (suite {result['suite_digest'][:12]})\nstate {result['state']}")
    elif command == "submit":
        task = engine.submit(
            title=args.title,
            requirements=_text(args.requirements, args.requirements_file, "requirements"),
            acceptance_criteria=_text(args.acceptance, args.acceptance_file, "acceptance criteria"),
        )
        _emit(args, task, f"submitted {task['id']}")
    elif command == "run":
        view = engine.run(args.task)
        _emit(args, view, render_task(view))
        return 0 if view["task"]["status"] == "awaiting_approval" else 3
    elif command == "show":
        engine.recover()
        view = engine.describe(args.task)
        _emit(args, view, render_task(view))
    elif command in ("approve", "reject"):
        decision = (engine.approve(args.task, proposal_digest=args.digest) if command == "approve"
                    else engine.reject(args.task, proposal_digest=args.digest, reason=args.reason))
        _emit(args, decision, f"{decision['decision']} {args.task} "
              f"(proposal {decision['proposal_digest'][:12]}) by {decision['actor']}")
    elif command == "status":
        engine.recover()
        if args.task:
            view = engine.describe(args.task)
            _emit(args, view, render_status(view))
        else:
            tasks = engine.store.tasks()
            _emit(args, tasks, "\n".join(f"{t['id']}  {t['status']:<18} {t['title']}" for t in tasks) or "no tasks")
    elif command == "audit":
        count = engine.store.verify_audit_chain()
        events = engine.store.audit_events(args.task)
        _emit(args, {"chain_verified": count, "events": events},
              f"chain verified ({count} events)\n" + "\n".join(
                  f"{e['seq']:>4} {e['at']} {e['actor']} {e['event']} {e['task_id'] or '-'}" for e in events))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run_command(args)
    except EXPECTED_ERRORS as error:
        print(f"error [{getattr(error, 'code', 'error')}]: {getattr(error, 'message', error)}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
