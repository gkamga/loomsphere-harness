"""``loomsphere-harness`` command line: Intake -> Architect -> human approval.

Phase 2C.2 adds the implementation loop. ``implement`` runs Builder, validation, QA,
and Reviewer inside a private worktree; ``changes`` and ``evidence`` show what was
produced; ``accept`` and ``reject-delivery`` record the second human decision. Every
approval and delivery decision requires the SHA-256 digest shown by the preceding
read command, so a decision can never be recorded against something else.
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
from loomsphere_harness.implementation_prompt import LoopPromptError
from loomsphere_harness.reports import ReportError
from loomsphere_harness.repository import GitRepository, OsUserIdentity, RepositoryError
from loomsphere_harness.sandbox import SandboxError
from loomsphere_harness.schemas.acceptance_contract import AcceptanceContractError
from loomsphere_harness.store import SqliteStore, StoreError
from loomsphere_harness.validation import ValidationPlanError
from loomsphere_harness.workspace import WorkspaceError

COMMANDS = (
    "init", "submit", "run", "show", "approve", "reject", "implement", "retry-review",
    "inspect-recovery", "recover-builder", "changes", "evidence", "accept", "reject-delivery",
    "status", "audit",
)
EXPECTED_ERRORS = (
    ConfigError, AssetIntegrityError, EngineError, StoreError, RepositoryError, ApprovalError,
    WorkspaceError, SandboxError, ValidationPlanError, ReportError, LoopPromptError,
    AcceptanceContractError,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loomsphere-harness",
        description="Standalone LoomSphere Intake -> Architect -> approval -> delivery runtime.",
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
    implement = sub.add_parser(
        "implement", help="build, validate, review an approved task (Builder, QA, Reviewer)")
    implement.add_argument("task")
    retry_review = sub.add_parser(
        "retry-review",
        help="re-run only the Reviewer after an invalid-review-report failure (one retry)")
    retry_review.add_argument("task")
    inspect_recovery = sub.add_parser(
        "inspect-recovery",
        help="show what a failed Builder preserved, without changing anything")
    inspect_recovery.add_argument("task")
    recover_builder = sub.add_parser(
        "recover-builder",
        help="authorize the exact preserved change set and take it through validation, "
             "QA, and review (does not re-run the Builder)")
    recover_builder.add_argument("task")
    recover_builder.add_argument(
        "--snapshot-digest", "--digest", dest="snapshot_digest", required=True,
        help="snapshot SHA-256 shown by `inspect-recovery`")
    changes = sub.add_parser("changes", help="show the recorded change set of the deliverable")
    changes.add_argument("task")
    evidence = sub.add_parser("evidence", help="show the validation and review evidence")
    evidence.add_argument("task")
    accept = sub.add_parser("accept", help="accept the exact deliverable digest")
    accept.add_argument("task")
    accept.add_argument("--digest", required=True, help="deliverable SHA-256 shown by `changes`")
    accept.add_argument("--reason")
    reject_delivery = sub.add_parser(
        "reject-delivery", help="reject the exact deliverable digest")
    reject_delivery.add_argument("task")
    reject_delivery.add_argument("--digest", required=True)
    reject_delivery.add_argument("--reason", required=True)
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


def render_iterations(view: dict[str, Any]) -> str:
    """Task status plus every recorded iteration, newest evidence last."""

    task = view["task"]
    lines = [f"{task['id']}  {task['status']}  {task['title']}"]
    implementation = view.get("implementation")
    if implementation:
        headline = f"implementation: {implementation.get('result')}"
        if implementation.get("reason"):
            headline += f" ({implementation['reason']})"
        lines.append(headline)
        if implementation.get("detail"):
            lines.append(f"  {implementation['detail']}")
    for item in view.get("iterations") or []:
        lines += [
            "",
            f"iteration {item['number']}  {item['status']}"
            + (f"  {item['base_commit'][:12]} -> {item['commit_sha'][:12]}"
               if item.get("commit_sha") else ""),
        ]
        report = item.get("build_report") or {}
        if report.get("summary"):
            lines.append(f"  builder: {report['summary']}")
            for entry in report.get("files") or []:
                lines.append(f"    {entry.get('change', ''):<10} {entry.get('path', '')}")
        validation = item.get("validation") or {}
        if validation.get("command"):
            lines.append(
                f"  validation: {' '.join(validation['command'])} -> {validation.get('status')}"
                f" (exit {validation.get('exit_code')})"
            )
        qa = item.get("qa_report") or {}
        if qa.get("verdict"):
            lines.append(f"  qa: {qa['verdict']} — {qa.get('summary', '')}")
        review = item.get("review_report") or {}
        if review.get("verdict"):
            lines.append(f"  review: {review['verdict']} — {review.get('summary', '')}")
        if item.get("failure_reason"):
            lines.append(f"  failure: {item['failure_reason']} — {item.get('failure_detail') or ''}")
    deliverable = view.get("deliverable")
    if deliverable:
        document = deliverable["document"]
        lines += [
            "",
            f"Deliverable {deliverable['digest']}",
            f"  commit {document['iteration']['commit']}  branch {document['iteration']['branch']}",
            f"  changed files {len(document['changed_files'])}"
            f"  criteria {len(document['acceptance_contract']['criteria'])}",
        ]
    acceptance = view.get("acceptance")
    if acceptance:
        lines.append(f"Delivery {acceptance['decision']} by {acceptance['actor']}")
    return "\n".join(lines)


def render_changes(view: dict[str, Any]) -> str:
    """The generated change set, as it was frozen into the deliverable."""

    deliverable = view.get("deliverable")
    if not deliverable:
        return f"{view['task']['id']}  {view['task']['status']}  no deliverable recorded"
    document = deliverable["document"]
    iteration = document["iteration"]
    lines = [
        f"Deliverable {deliverable['digest']}",
        f"base {iteration['base_commit']}  head {iteration['commit']}  branch {iteration['branch']}",
        f"workspace digest {iteration['workspace_digest']}",
        "",
        "Changed files:",
        *(f"  {entry['change']:<10} {entry['path']}" for entry in document["changed_files"]),
        "",
        "Diff excerpt:",
        document["diff_excerpt"],
        "",
        "Acceptance contract:",
        *(f"  {item['id']}  {item['text']}" for item in document["acceptance_contract"]["criteria"]),
    ]
    return "\n".join(lines)


def render_evidence(view: dict[str, Any]) -> str:
    """The executed validation run and the recorded review evidence."""

    deliverable = view.get("deliverable")
    if not deliverable:
        return f"{view['task']['id']}  {view['task']['status']}  no deliverable recorded"
    document = deliverable["document"]
    validation = document["validation"]
    record = validation["record"]
    lines = [
        f"Deliverable {deliverable['digest']}",
        "",
        "Executed validation:",
        f"  command {' '.join(record['command'])}",
        f"  status {record['status']}  exit {record['exit_code']}  {record['duration_ms']} ms",
        f"  image {record.get('sandbox_image')} ({record.get('sandbox_image_id')})",
        f"  workspace digest {record['workspace_digest']}",
        f"  output digest {record['output_digest']}",
        "",
        "Output excerpt:",
        str(record.get("output") or "(empty)"),
    ]
    coverage = document.get("verification_coverage")
    if coverage:
        lines += ["", "Verification coverage (declared vs executed):"]
        environment = coverage.get("environment_check")
        if environment:
            lines.append(
                f"  pytest environment: {environment['status']} "
                f"exit {environment['exit_code']} "
                f"image {environment.get('sandbox_image_id')} "
                f"source {environment.get('source_digest')}"
            )
        for item in coverage["criteria"]:
            declared = item["declared"]
            lines.append(
                f"  {item['id']}  {item['status']}  declared {declared.get('kind')} "
                f"{declared.get('selector') or ''}"
                f"  exists: {item['selector_exists']}"
                f"  selector executed: {'yes' if item['selector_executed'] else 'no'}"
            )
            execution = item.get("execution")
            if execution:
                lines.append(
                    f"    exact command {' '.join(execution['command'])}; "
                    f"exit {execution['exit_code']}; image "
                    f"{execution.get('sandbox_image_id')}; source "
                    f"{execution.get('source_digest')}"
                )
            if item.get("failure_reason"):
                lines.append(
                    f"    obstacle {item['failure_reason']}: "
                    f"{item.get('obstacle', '')}"
                )
        lines.append(f"  {coverage['note']}")
    for label, key in (("QA", "qa"), ("Reviewer", "review")):
        report = document[key]["report"]
        lines += ["", f"{label} verdict: {report['verdict']} — {report['summary']}"]
        for finding in report["findings"]:
            lines.append(f"  [{finding['severity']}] {finding['description']}")
        for claim in report["acceptance_criteria"]:
            lines.append(f"  {claim['criterion_id']}  {claim['status']}  {claim.get('evidence', '')}")
    return "\n".join(lines)


def _count_text(value: Any) -> str:
    """A measured count, or ``unknown``.

    ``unknown`` is deliberately not ``0``: a provider that reported no usage must
    not leave a failed run looking like it consumed nothing.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        return "unknown"
    return str(value)


def _tokens_text(value: Any, *, lower_bound: bool) -> str:
    """A token count, ``unknown``, or an explicit floor.

    A partially measured run reported usage for some requests and not others, so
    the stored total is a lower bound. Rendering it like an exact count would
    invite exactly the budget conclusion the evidence cannot support.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        return "unknown"
    return f">={value}" if lower_bound else str(value)


def _tool_summary_text(summary: dict[str, Any]) -> str:
    """Recorded tool-call counts for one run, successful or failed."""

    line = f"    tool calls: {_count_text(summary.get('calls'))}"
    by_tool = summary.get("by_tool") or {}
    if by_tool:
        line += " (" + ", ".join(f"{name}={count}" for name, count in by_tool.items()) + ")"
    if summary.get("truncated") and summary.get("distinct") is not None:
        line += f"; distinct={summary['distinct']}, per-tool list truncated"
    return line


def _limit_text(limits: dict[str, Any], sources: dict[str, Any], key: str) -> str:
    allowed = limits.get(key)
    if allowed is None:
        return "unknown"
    source = sources.get(key)
    return f"{allowed} ({source})" if source else str(allowed)


def _run_diagnostic_lines(run: dict[str, Any]) -> list[str]:
    """Effective limits, observed usage, and warnings for one run.

    Read only from recorded evidence, so the text view and ``--json`` view cannot
    disagree, and so a stale run rendered by an older binary shows what was
    actually stored rather than a guess.
    """

    events = run.get("events") or {}
    usage = events.get("usage") or {}
    limits = events.get("limits") or {}
    diagnostics = events.get("diagnostics") or {}
    lines: list[str] = []
    if limits:
        sources = limits.get("sources") or {}
        timeout = limits.get("timeout_seconds")
        lines.append(
            "    limits: "
            f"requests={_limit_text(limits, sources, 'request_limit')} "
            f"tool_calls={_limit_text(limits, sources, 'tool_call_limit')} "
            f"total_tokens={_limit_text(limits, sources, 'total_token_limit')}"
            + (f" timeout={timeout}s" if timeout is not None else "")
        )
    if usage:
        lower_bound = bool(usage.get("tokens_are_lower_bound")) or (
            usage.get("token_accounting") == "partial"
        )
        lines.append(
            "    usage: "
            f"requests={_count_text(usage.get('requests'))} "
            f"tool_calls={_count_text(usage.get('tool_calls'))} "
            f"total_tokens={_tokens_text(usage.get('total_tokens'), lower_bound=lower_bound)}"
            + (
                f" duration={(events.get('duration_ms') if events.get('duration_ms') is not None else 'unknown')}ms"
            )
        )
    warnings = diagnostics.get("warnings") or []
    if warnings:
        lines.append(
            "    observed diagnostic warnings: "
            + "; ".join(str(warning.get("message")) for warning in warnings)
        )
    availability = usage.get("availability")
    if availability and availability != "measured":
        unmeasured = usage.get("unmeasured_requests")
        detail = f" ({unmeasured} request(s) reported no usage)" if unmeasured else ""
        if usage.get("tokens_are_lower_bound"):
            lines.append(
                f"    usage availability: {availability}{detail}; token totals are "
                "observed lower bounds, so actual consumption may be higher"
            )
        else:
            lines.append(
                f"    usage availability: {availability}{detail}; "
                "token counts are unknown, not zero"
            )
    return lines


def render_status(view: dict[str, Any]) -> str:
    lines = [
        render_task({**view, "proposal": None, "intake": None, "approval": None}).splitlines()[0],
        "Runs:",
    ]
    for run in view["runs"]:
        events = run.get("events") or {}
        usage = events.get("usage") or {}
        duration = events.get("duration_ms")
        lower_bound = bool(usage.get("tokens_are_lower_bound"))
        lines.append(
            f"  {run['role']:<9} {run['status']:<11} model={run['model']} prompt={str(run['prompt_digest'])[:12]}"
            f" tokens={_tokens_text(usage.get('input_tokens'), lower_bound=lower_bound)}"
            f"/{_tokens_text(usage.get('output_tokens'), lower_bound=lower_bound)}"
            + (f" duration={duration}ms" if duration is not None else "")
            + (f" failure={run['failure_reason']}" if run["failure_reason"] else "")
        )
        lines += _run_diagnostic_lines(run)
        summary = events.get("tool_summary") or {}
        if summary:
            # Recorded for failed runs too, which is why status can show what the
            # run actually called even when it produced no final answer.
            lines.append(_tool_summary_text(summary))
        else:
            # Runs recorded before tool summaries existed still carry a bounded
            # activity list; show the tools rather than inventing counts.
            activity = events.get("tool_activity") or []
            if activity:
                lines.append("    tools: " + ", ".join(sorted({a["tool"] for a in activity})))
        truncation = (run.get("events") or {}).get("truncation") or {}
        if truncation:
            lines.append(
                "    truncated: signals=" + ",".join(truncation.get("signals") or [])
                + f" finish_reason={truncation.get('finish_reason')}"
                + f" tokens={truncation.get('output_tokens')}/{truncation.get('max_output_tokens')}"
            )
    note = next(
        (
            (run.get("events") or {}).get("diagnostics", {}).get("note")
            for run in view["runs"]
            if (run.get("events") or {}).get("diagnostics")
        ),
        None,
    )
    if note:
        lines += ["", f"Note: {note}"]
    cost = next(
        (
            (run.get("events") or {}).get("diagnostics", {}).get("cost")
            for run in view["runs"]
            if (run.get("events") or {}).get("diagnostics")
        ),
        None,
    )
    if cost:
        lines.append(f"Cost: {cost.get('status')} — {cost.get('reason')}")
    return "\n".join(lines)


def render_recovery(view: dict[str, Any]) -> str:
    """Show what a failed Builder preserved and whether it can be recovered."""

    inspection = view["recovery"]
    task = inspection["task"]
    lines = [f"{task['id']}  {task['status']}  {task['title']}"]
    iteration = inspection.get("iteration")
    if iteration is None:
        lines.append("no iteration is recorded for this task")
    else:
        lines.append(
            f"iteration {iteration['number']}  {iteration['status']}  "
            f"{iteration.get('failure_reason') or '-'}"
        )
    builder_run = inspection.get("builder_run")
    if builder_run is None:
        lines.append("builder run: none recorded")
    else:
        lines.append(
            f"builder run {builder_run['id']}  {builder_run['status']}  "
            f"{builder_run.get('failure_reason') or '-'}"
        )
        detail = str(builder_run.get("failure_detail") or "").strip()
        if detail:
            lines.append(f"  {detail.splitlines()[0][:160]}")
        lines.append(
            "  no build report, no claimed file list, and no claimed test run were recorded"
        )

    workspace = inspection.get("workspace")
    snapshot = inspection.get("snapshot")
    change_set = inspection.get("change_set")
    if workspace:
        lines += ["", f"workspace {workspace['path']}"]
        lines.append(f"  branch {workspace.get('branch') or '-'}")
    if change_set:
        lines.append(
            f"  base {change_set['base_commit'][:12]} -> {change_set['head_commit'][:12]}"
        )
    if snapshot:
        lines += ["", f"recoverable files ({len(snapshot['files'])})"]
        for entry in snapshot["files"]:
            lines.append(
                f"  {entry['mode']:<10} {entry['path']}  {(entry['sha256'] or '-')[:16]}"
            )
        lines += ["", f"snapshot digest {snapshot['digest']}"]
    if change_set:
        lines.append(f"change set digest {change_set['workspace_digest']}")

    used = len(inspection.get("recoveries") or [])
    lines += ["", f"authorized recoveries {used} of {inspection['limit']}"]
    obstacles = inspection.get("obstacles") or []
    if obstacles:
        lines.append("")
        lines.append("Not recoverable:")
        lines.extend(f"  - {obstacle}" for obstacle in obstacles)
    elif inspection.get("next"):
        lines.append("Recovery available. Authorize exactly this work with:")
        lines.append(f"  {inspection['next']}")
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
    elif command == "implement":
        view = engine.implement(args.task)
        _emit(args, view, render_iterations(view))
        return 0 if view["task"]["status"] == "awaiting_acceptance" else 3
    elif command == "retry-review":
        view = engine.retry_review(args.task)
        _emit(args, view, render_iterations(view))
        return 0 if view["task"]["status"] == "awaiting_acceptance" else 3
    elif command == "inspect-recovery":
        view = engine.inspect_recovery(args.task)
        _emit(args, view, render_recovery(view))
        return 0 if view["recovery"]["eligible"] else 3
    elif command == "recover-builder":
        view = engine.recover_builder(args.task, snapshot_digest=args.snapshot_digest)
        _emit(args, view, render_iterations(view))
        return 0 if view["task"]["status"] == "awaiting_acceptance" else 3
    elif command in ("changes", "evidence"):
        engine.recover()
        view = engine.describe(args.task)
        render = render_changes if command == "changes" else render_evidence
        _emit(args, view, render(view))
        return 0 if view.get("deliverable") else 3
    elif command in ("accept", "reject-delivery"):
        engine.recover()
        decision = (
            engine.accept_delivery(args.task, deliverable_digest=args.digest, reason=args.reason)
            if command == "accept"
            else engine.reject_delivery(
                args.task, deliverable_digest=args.digest, reason=args.reason)
        )
        _emit(args, decision, f"{decision['decision']} {args.task} "
              f"(deliverable {decision['deliverable_digest'][:12]}) by {decision['actor']}")
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
