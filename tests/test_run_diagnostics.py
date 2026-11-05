"""Role budgets, bounded context expansion, and failure diagnostics.

Every test here runs a deterministic, offline ``FunctionModel``: no network, no
real provider, no wall-clock dependence beyond a one second timeout. The point is
not that a model behaves well but that the runtime keeps accounting for what it
actually did when it fails, and that the limits a run was given are the limits it
ran under.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import sqlite3
import sys
import unittest
from argparse import Namespace
from pathlib import Path
from uuid import uuid4

from pydantic_ai.messages import (
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage, RunUsage

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runtime_support import (  # noqa: E402
    PROPOSAL,
    RuntimeCase,
    fenced,
    model_config,
    scripted_model,
)

from loomsphere_harness import cli  # noqa: E402
from loomsphere_harness.agent_runner import (  # noqa: E402
    AgentExecutionError,
    AgentInvocation,
)
from loomsphere_harness.config import ConfigError, parse_config  # noqa: E402
from loomsphere_harness.diagnostics import (  # noqa: E402
    COST_UNAVAILABLE,
    DIAGNOSTIC_THRESHOLD,
    MEASURED,
    PARTIAL,
    TOKEN_COMPLETE,
    TOKEN_PARTIAL,
    TOKEN_UNAVAILABLE,
    UNAVAILABLE,
    budget_report,
    usage_snapshot,
)
from loomsphere_harness.pydantic_harness import (  # noqa: E402
    DENIED_PATTERNS,
    MAX_LIST_RESULTS,
    MAX_READ_CHARS,
    MAX_READ_LINES,
    READ_TOOLS,
    ROLE_LIMIT_KEYS,
    ROLES,
    WRITE_TOOLS,
    PydanticHarnessConfig,
    PydanticHarnessRunner,
    RoleLimits,
    _argument_target,
    tools_for,
)
from loomsphere_harness.store import StoreError  # noqa: E402

#: A credential-shaped value. It must never appear in recorded evidence.
SECRET = "not-a-real-key"


def prompt_text(messages) -> str:
    return "\n".join(
        str(getattr(part, "content", ""))
        for message in messages
        for part in getattr(message, "parts", ())
    )


def tool_results(messages) -> list[str]:
    return [
        str(part.content)
        for message in messages
        for part in getattr(message, "parts", ())
        if isinstance(part, ToolReturnPart)
    ]


def looping_model(*, marker: str = "LoomSphere Intake agent") -> FunctionModel:
    """Never produce a contract: answer with one more tool call, forever.

    The selected role keeps reading a file it already read, so every response is
    another request and another successful tool call. The run can only end by
    running out of budget, which is exactly the case the accounting must survive.
    """

    def respond(messages, info):
        if marker in prompt_text(messages):
            return ModelResponse(
                parts=[ToolCallPart("read_file", {"path": "README.md"})],
                usage=RequestUsage(input_tokens=1_000, output_tokens=10),
            )
        return ModelResponse(
            parts=[TextPart(fenced(PROPOSAL))],
            usage=RequestUsage(input_tokens=25, output_tokens=4),
        )

    return FunctionModel(respond)


def echo_tool_model(tool: str, args: dict) -> FunctionModel:
    """Call one tool once, then answer with exactly what the tool returned."""

    def respond(messages, info):
        returned = tool_results(messages)
        if returned:
            return ModelResponse(parts=[TextPart(returned[-1])])
        return ModelResponse(parts=[ToolCallPart(tool, args)])

    return FunctionModel(respond)


def status_view(events: dict, *, role: str = "qa", status: str = "ok",
                failure: str | None = None) -> dict:
    """The minimal stored view ``render_status`` needs, without a full engine run."""

    return {
        "task": {
            "id": "T-0001", "status": "awaiting_approval", "title": "fixture",
            "repo_ref": "HEAD", "repo_commit": "0" * 40, "repo_dirty": False,
            "acceptance_criteria": "the fixture is returned",
        },
        "runs": [
            {
                "role": role, "status": status, "model": "test-model",
                "prompt_digest": "0" * 64, "failure_reason": failure,
                "events": events,
            }
        ],
    }


class BudgetDiagnostics(unittest.TestCase):
    """The pure report the executor and the CLI both render."""

    limits = {"request_limit": 50, "tool_call_limit": 120, "total_token_limit": 600_000}

    def test_near_budget_usage_is_reported_as_a_labelled_observation(self) -> None:
        usage = RunUsage(requests=42, tool_calls=10, input_tokens=900, output_tokens=500)
        report = budget_report(usage_snapshot(usage), self.limits)

        self.assertEqual(
            {entry["name"]: entry["state"] for entry in report["entries"]},
            {"requests": "near", "tool_calls": "within", "total_tokens": "within"},
        )
        self.assertEqual([warning["name"] for warning in report["warnings"]], ["requests"])
        self.assertEqual(report["warnings"][0]["ratio"], 0.84)
        self.assertEqual(report["threshold"], DIAGNOSTIC_THRESHOLD)
        self.assertIn("not a hard cap", report["note"])
        # No verified prices are configured, so no cost is invented.
        self.assertEqual(report["cost"], COST_UNAVAILABLE)

    def test_a_report_with_no_limits_configured_stays_empty_rather_than_inventing(self) -> None:
        report = budget_report(usage_snapshot(RunUsage(requests=1)), {})
        self.assertEqual(report["entries"], [])
        self.assertEqual(report["warnings"], [])

    def test_an_unmeasured_usage_is_unknown_and_never_zero(self) -> None:
        snapshot = usage_snapshot(RunUsage(requests=2, unmeasured_requests=2))
        self.assertEqual(snapshot["availability"], UNAVAILABLE)
        self.assertEqual(snapshot["token_accounting"], TOKEN_UNAVAILABLE)
        self.assertIsNone(snapshot["input_tokens"])
        self.assertIsNone(snapshot["output_tokens"])
        self.assertIsNone(snapshot["total_tokens"])
        self.assertNotIn(
            "total_tokens",
            [entry["name"] for entry in budget_report(snapshot, self.limits)["entries"]],
        )

        measured = usage_snapshot(RunUsage(requests=2, input_tokens=0, output_tokens=0))
        self.assertEqual(measured["availability"], MEASURED)
        self.assertEqual(measured["token_accounting"], TOKEN_COMPLETE)
        self.assertEqual(measured["total_tokens"], 0)
        self.assertFalse(measured["tokens_are_lower_bound"])

    def test_completely_measured_usage_keeps_exact_budget_reporting(self) -> None:
        snapshot = usage_snapshot(RunUsage(requests=2, input_tokens=700, output_tokens=100))
        self.assertEqual(snapshot["availability"], MEASURED)
        self.assertEqual(snapshot["token_accounting"], TOKEN_COMPLETE)
        self.assertFalse(snapshot["tokens_are_lower_bound"])

        entries = {
            entry["name"]: entry
            for entry in budget_report(snapshot, self.limits)["entries"]
        }
        self.assertEqual(entries["total_tokens"]["state"], "within")
        self.assertEqual(entries["total_tokens"]["basis"], "measured")
        self.assertIn("total_tokens used 800 of 600000", entries["total_tokens"]["message"])
        self.assertNotIn("at least", entries["total_tokens"]["message"])

    def test_partially_measured_usage_is_a_lower_bound_and_never_within_budget(self) -> None:
        # Enough observed token usage to pass the warning threshold, but one
        # request never reported usage, so the total can only be a floor.
        snapshot = usage_snapshot(
            RunUsage(
                requests=4, tool_calls=2, input_tokens=560_000, output_tokens=0,
                unmeasured_requests=1,
            )
        )
        self.assertEqual(snapshot["availability"], PARTIAL)
        self.assertEqual(snapshot["token_accounting"], TOKEN_PARTIAL)
        self.assertTrue(snapshot["tokens_are_lower_bound"])
        self.assertEqual(snapshot["total_tokens"], 560_000)

        report = budget_report(snapshot, self.limits)
        self.assertEqual(report["usage_availability"], PARTIAL)
        self.assertEqual(report["token_accounting"], TOKEN_PARTIAL)
        entries = {entry["name"]: entry for entry in report["entries"]}
        tokens = entries["total_tokens"]
        self.assertEqual(tokens["state"], "lower-bound")
        self.assertEqual(tokens["basis"], "observed-lower-bound")
        self.assertIn("at least 560000", tokens["message"])
        self.assertIn("not evidence of staying within budget", tokens["message"])
        self.assertIn(tokens, report["warnings"])

        # Counters that do not depend on provider usage stay exact.
        self.assertEqual(entries["requests"]["basis"], "measured")
        self.assertEqual(entries["requests"]["state"], "within")
        self.assertEqual(entries["tool_calls"]["basis"], "measured")
        self.assertEqual(entries["tool_calls"]["state"], "within")


class BudgetedRun(RuntimeCase):
    """A task driven to a failed Intake run by a per-role request budget."""

    role_limits = (RoleLimits("intake", request_limit=2),)

    def budgeted_config(self):
        return self.config(
            model_config(
                request_limit=12,
                tool_call_limit=40,
                total_token_limit=600_000,
                role_limits=self.role_limits,
            )
        )

    def failing_intake(self, *, model=None, config=None) -> tuple[dict, object]:
        engine = self.engine(
            model=model or looping_model(), config=config or self.budgeted_config()
        )
        engine.initialize()
        task = engine.submit(
            title="Diagnostics fixture",
            requirements="Return the greeting.",
            acceptance_criteria="The greeting is returned.",
        )
        view = engine.run(task["id"])
        self.failing_engine, self.failing_task_id, self.failing_view = engine, task["id"], view
        return view, engine

    def intake_run(self, view: dict) -> dict:
        runs = [run for run in view["runs"] if run["role"] == "intake"]
        self.assertTrue(runs, [run["role"] for run in view["runs"]])
        return runs[-1]


class FailureAccounting(BudgetedRun):
    def test_a_run_that_exceeds_its_role_budget_records_usage_and_limits(self) -> None:
        view, _ = self.failing_intake()
        run = self.intake_run(view)
        events = run["events"]

        # The failure is still reported as a resource limit, not remapped.
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["failure_reason"], "resource-limit-exceeded")
        self.assertEqual(view["task"]["status"], "intake_failed")

        # Usage survives the exception: counters are present, not a blank dict.
        usage = events["usage"]
        self.assertIsInstance(usage, dict)
        self.assertEqual(usage["requests"], 2)
        self.assertEqual(usage["tool_calls"], 2)
        self.assertEqual(usage["availability"], "measured")
        self.assertEqual(usage["input_tokens"], 2_000)
        self.assertEqual(usage["output_tokens"], 20)
        self.assertEqual(usage["total_tokens"], 2_020)
        self.assertEqual(usage["source"], "pydantic-ai RunUsage accumulator")

        # The budget it failed against is recorded, with its provenance.
        limits = events["limits"]
        self.assertEqual(limits["role"], "intake")
        self.assertEqual(limits["request_limit"], 2)
        self.assertEqual(limits["tool_call_limit"], 40)
        self.assertEqual(limits["sources"]["request_limit"], "role_limits.intake")
        self.assertEqual(limits["sources"]["tool_call_limit"], "global")
        self.assertEqual(limits["timeout_seconds"], 30)

        # A duration and a bounded tool summary make the failure diagnosable
        # without copying the workspace into the audit trail.
        self.assertGreaterEqual(events["duration_ms"], 0)
        self.assertEqual(events["tool_summary"]["by_tool"].get("read_file"), usage["tool_calls"])
        self.assertEqual(events["tool_summary"]["calls"], 2)
        self.assertNotIn("hello", json.dumps(events))

        # The observed warning is present and labelled as an observation.
        diagnostics = events["diagnostics"]
        self.assertEqual(diagnostics["warnings"][0]["name"], "requests")
        self.assertEqual(diagnostics["warnings"][0]["state"], "exceeded")
        self.assertEqual(diagnostics["warnings"][0]["allowed"], 2)
        self.assertIn("not a hard cap", diagnostics["note"])
        self.assertEqual(diagnostics["cost"]["status"], "unavailable")

    def test_the_failed_run_evidence_survives_reopening_the_store(self) -> None:
        view, _ = self.failing_intake()
        recorded = self.intake_run(view)["events"]["usage"]

        reopened = self.engine()
        stored = [
            run for run in reopened.describe(self.failing_task_id)["runs"]
            if run["role"] == "intake"
        ][-1]
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(stored["events"]["usage"], recorded)
        self.assertEqual(stored["events"]["limits"]["request_limit"], 2)
        self.assertEqual(stored["events"]["limits"]["sources"]["request_limit"],
                         "role_limits.intake")

        # At rest, too: the column is readable JSON, not an in-memory object.
        with contextlib.closing(sqlite3.connect(self.state)) as database:
            row = database.execute(
                "SELECT events FROM runs WHERE id = ?", (stored["id"],)
            ).fetchone()
        on_disk = json.loads(row[0])
        self.assertEqual(on_disk["usage"]["requests"], recorded["requests"])
        self.assertEqual(on_disk["limits"]["sources"]["request_limit"], "role_limits.intake")

    def test_a_timeout_preserves_the_usage_accumulated_so_far(self) -> None:
        async def slow(messages, info):
            if not tool_results(messages):
                return ModelResponse(
                    parts=[ToolCallPart("read_file", {"path": "README.md"})],
                    usage=RequestUsage(input_tokens=10, output_tokens=2),
                )
            await asyncio.sleep(30)
            return ModelResponse(parts=[TextPart("unreachable")])

        runner = PydanticHarnessRunner(model_config(), model=FunctionModel(slow))
        invocation = AgentInvocation(uuid4(), self.repo, "read", 1, role="builder", read_only=True)
        with self.assertRaises(AgentExecutionError) as raised:
            runner.run(invocation)

        error = raised.exception
        self.assertEqual(error.reason, "timed-out")
        self.assertEqual(error.events["usage"]["requests"], 1)
        self.assertEqual(error.events["usage"]["input_tokens"], 10)
        self.assertEqual(error.events["usage"]["tool_calls"], 1)
        self.assertGreaterEqual(error.events["duration_ms"], 1_000)
        self.assertEqual(error.events["tool_summary"]["calls"], 1)
        self.assertEqual(error.events["limits"]["role"], "builder")

    def test_a_provider_failure_preserves_the_usage_accumulated_so_far(self) -> None:
        from pydantic_ai.exceptions import ModelAPIError

        def failing_provider(messages, info):
            if not tool_results(messages):
                return ModelResponse(
                    parts=[ToolCallPart("read_file", {"path": "README.md"})],
                    usage=RequestUsage(input_tokens=7, output_tokens=1),
                )
            raise ModelAPIError("test-model", "connection reset")

        runner = PydanticHarnessRunner(model_config(), model=FunctionModel(failing_provider))
        invocation = AgentInvocation(uuid4(), self.repo, "read", 30, role="qa", read_only=True)
        with self.assertRaises(AgentExecutionError) as raised:
            runner.run(invocation)

        error = raised.exception
        self.assertEqual(error.reason, "provider-unavailable")
        self.assertEqual(error.events["usage"]["requests"], 1)
        self.assertEqual(error.events["usage"]["input_tokens"], 7)
        self.assertEqual(error.events["limits"]["role"], "qa")
        self.assertNotIn("connection reset", json.dumps(error.events))


class SuccessfulRunEvidence(RuntimeCase):
    def test_a_successful_run_reports_usage_without_leaking_content_or_credentials(self) -> None:
        (self.repo / "notes.txt").write_text("SENTINEL-FILE-CONTENT\n", encoding="utf-8")
        engine = self.engine(model=scripted_model())
        engine.initialize()
        task = engine.submit(
            title="Diagnostics fixture",
            requirements="Return the greeting.",
            acceptance_criteria="The greeting is returned.",
        )
        view = engine.run(task["id"])

        self.assertEqual(view["task"]["status"], "awaiting_approval")
        for run in view["runs"]:
            events = run["events"]
            self.assertIn("usage", events)
            self.assertIn("diagnostics", events)
            self.assertIn("limits", events)
            self.assertEqual(events["usage"]["requests"], 1)
            self.assertEqual(events["tool_summary"]["calls"], 0)
            self.assertIn("duration_ms", events)

        # The limits a role ran under are on a successful record too, so a budget
        # can be audited without waiting for something to fail.
        for run in view["runs"]:
            self.assertEqual(
                set(run["events"]["limits"]),
                {"timeout_seconds", "role", "request_limit", "tool_call_limit",
                 "total_token_limit", "max_output_tokens", "sources", "file_tools"},
            )
            self.assertEqual(run["events"]["limits"]["role"], run["role"])

        for run in view["runs"]:
            events = json.dumps(run["events"], sort_keys=True)
            self.assertNotIn(SECRET, events)
            self.assertNotIn("SENTINEL-FILE-CONTENT", events)

    def test_a_tool_using_run_records_activity_without_the_file_contents(self) -> None:
        (self.repo / "notes.txt").write_text("SENTINEL-FILE-CONTENT\n", encoding="utf-8")
        runner = PydanticHarnessRunner(
            model_config(), model=echo_tool_model("read_file", {"path": "notes.txt"})
        )
        result = runner.run(AgentInvocation(uuid4(), self.repo, "read", 30, role="qa"))

        # The model legitimately saw the content; the record must not keep it.
        self.assertIn("SENTINEL-FILE-CONTENT", result.response_text)
        activity = result.events["tool_activity"]
        self.assertEqual(len(activity), 1)
        self.assertEqual(activity[0]["tool"], "read_file")
        self.assertEqual(activity[0]["target"], "notes.txt")
        self.assertEqual(activity[0]["target_kind"], "path")
        self.assertEqual(activity[0]["target_chars"], len("notes.txt"))
        self.assertEqual(activity[0]["outcome"], "returned")
        self.assertGreater(activity[0]["result_chars"], 0)
        self.assertNotIn("SENTINEL-FILE-CONTENT", json.dumps(result.events))

    def test_a_search_selector_is_digested_and_never_recorded(self) -> None:
        selector = "SENTINEL-SEARCH-s3cr3t"
        (self.repo / "notes.txt").write_text("hello\n", encoding="utf-8")
        runner = PydanticHarnessRunner(
            model_config(),
            model=echo_tool_model("search_files", {"pattern": selector, "path": "."}),
        )
        result = runner.run(AgentInvocation(uuid4(), self.repo, "read", 30, role="qa"))

        digest = "sha256:" + hashlib.sha256(selector.encode("utf-8")).hexdigest()
        activity = result.events["tool_activity"]
        self.assertEqual(len(activity), 1)
        self.assertEqual(activity[0]["tool"], "search_files")
        self.assertEqual(activity[0]["target"], digest)
        self.assertEqual(activity[0]["target_kind"], "pattern")
        self.assertEqual(activity[0]["target_chars"], len(selector))

        # Neither the stored events nor the rendered status may carry the selector.
        serialized = json.dumps(result.events, sort_keys=True)
        self.assertNotIn(selector, serialized)
        text = cli.render_status(status_view(result.events))
        self.assertNotIn(selector, text)
        self.assertIn("search_files", text)

        # A tool that names its selector `query` is digested the same way. `query`
        # is not one of the FileSystem tool arguments, so this covers the shared
        # argument classifier directly.
        self.assertEqual(_argument_target({"query": selector})["target"], digest)
        self.assertEqual(_argument_target({"query": selector})["target_kind"], "query")


class RoleLimitResolution(RuntimeCase):
    def resolved(self, document: dict):
        return parse_config(document, environ={"AGENT_MODEL_API_KEY": SECRET}).model

    def base(self, **model) -> dict:
        section = {
            "base_url": "https://models.test/v1",
            "model_id": "test-model",
            "approved_hosts": ["models.test"],
        }
        section.update(model)
        return {"model": section}

    def test_an_override_narrows_only_its_role_and_the_rest_inherit(self) -> None:
        config = self.resolved(
            self.base(
                request_limit=10,
                tool_call_limit=20,
                total_token_limit=1000,
                role_limits={"builder": {"request_limit": 3}},
            )
        )
        builder = config.effective_limits("builder")
        self.assertEqual(builder.request_limit, 3)
        self.assertEqual(builder.tool_call_limit, 20)
        self.assertEqual(builder.total_token_limit, 1000)
        self.assertEqual(
            dict(builder.sources),
            {
                "request_limit": "role_limits.builder",
                "tool_call_limit": "global",
                "total_token_limit": "global",
            },
        )
        for role in ("intake", "architect", "qa", "reviewer"):
            inherited = config.effective_limits(role)
            self.assertEqual(
                (inherited.request_limit, inherited.tool_call_limit, inherited.total_token_limit),
                (10, 20, 1000),
            )
            self.assertEqual(set(dict(inherited.sources).values()), {"global"})

        # Only the declared role is stored, so nothing else can shadow the globals.
        self.assertEqual([override.role for override in config.role_limits], ["builder"])
        self.assertEqual(config.role_limits[0].declared(), {"request_limit": 3})

    def test_without_an_override_table_every_role_runs_under_the_globals(self) -> None:
        config = self.resolved(self.base(request_limit=7))
        for role in ROLES:
            limits = config.effective_limits(role)
            self.assertEqual(limits.request_limit, 7)
            self.assertEqual(limits.tool_call_limit, PydanticHarnessConfig().tool_call_limit)
            self.assertEqual(dict(limits.sources)["request_limit"], "global")

    def test_unsupported_roles_malformed_sections_and_unsafe_values_are_refused(self) -> None:
        cases = [
            ("unknown role", {"auditor": {"request_limit": 2}}, "config-invalid"),
            ("unknown key", {"builder": {"step_limit": 2}}, "config-invalid"),
            ("role section is not a table", {"builder": 5}, "config-invalid"),
            ("zero", {"builder": {"request_limit": 0}}, "config-invalid"),
            ("negative", {"builder": {"request_limit": -1}}, "config-invalid"),
            ("boolean", {"builder": {"request_limit": True}}, "config-invalid"),
            ("boolean tokens", {"builder": {"total_token_limit": False}}, "config-invalid"),
            ("widens the global request limit", {"builder": {"request_limit": 11}},
             "config-limit-widened"),
            ("widens the global token limit", {"qa": {"total_token_limit": 10**9}},
             "config-limit-widened"),
        ]
        for label, role_limits, code in cases:
            with self.subTest(label):
                with self.assertRaises(ConfigError) as raised:
                    self.resolved(self.base(request_limit=10, role_limits=role_limits))
                self.assertEqual(raised.exception.code, code)

        with self.assertRaises(ConfigError) as not_a_table:
            self.resolved(self.base(role_limits=[("builder", 2)]))
        self.assertEqual(not_a_table.exception.code, "config-invalid")

        # Direct construction is guarded too, so an embedded caller cannot slip a
        # widening or duplicated table past the loader.
        with self.assertRaises(ValueError):
            PydanticHarnessConfig(request_limit=10, role_limits=(RoleLimits("builder", request_limit=11),))
        with self.assertRaises(ValueError):
            PydanticHarnessConfig(role_limits=(RoleLimits("auditor", request_limit=1),))
        with self.assertRaises(ValueError):
            PydanticHarnessConfig(
                role_limits=(RoleLimits("builder", request_limit=1), RoleLimits("builder", request_limit=2))
            )
        with self.assertRaises(ValueError):
            PydanticHarnessConfig(role_limits=(RoleLimits("builder", request_limit=0),))

    def test_every_limit_key_is_reachable_from_the_override_table(self) -> None:
        declared = {
            "request_limit": 4,
            "tool_call_limit": 5,
            "total_token_limit": 600,
        }
        self.assertEqual(set(declared), set(ROLE_LIMIT_KEYS))
        config = self.resolved(self.base(request_limit=10, tool_call_limit=10,
                                        total_token_limit=1000,
                                        role_limits={"reviewer": declared}))
        resolved = config.effective_limits("reviewer")
        self.assertEqual(resolved.values(), declared)
        self.assertEqual(set(dict(resolved.sources).values()), {"role_limits.reviewer"})


class BoundedContext(RuntimeCase):
    def build_file(self, *, lines: int = 1200) -> None:
        content = "".join(f"MARK{index:04d} " + "x" * 110 + "\n" for index in range(lines))
        (self.repo / "big.txt").write_text(content, encoding="utf-8")

    def test_a_large_read_is_bounded_and_still_pageable(self) -> None:
        self.build_file()

        clipped = PydanticHarnessRunner(
            model_config(), model=echo_tool_model("read_file", {"path": "big.txt"})
        ).run(AgentInvocation(uuid4(), self.repo, "read", 30))

        text = clipped.response_text
        self.assertLessEqual(len(text), MAX_READ_CHARS + 1_000)
        self.assertLessEqual(len(text.splitlines()), MAX_READ_LINES + 2)
        self.assertNotIn("MARK1199", text)
        self.assertIn("offset=", text)

        paged = PydanticHarnessRunner(
            model_config(),
            model=echo_tool_model(
                "read_file", {"path": "big.txt", "offset": 1100, "limit": 5}
            ),
        ).run(AgentInvocation(uuid4(), self.repo, "read", 30))

        paged_text = paged.response_text
        self.assertIn("MARK1100", paged_text)
        self.assertIn("MARK1104", paged_text)
        self.assertNotIn("MARK1105", paged_text)

        # The caps are recorded with the run, so a reviewer can see the bound the
        # model actually worked under.
        self.assertEqual(clipped.events["limits"]["file_tools"]["max_read_lines"], MAX_READ_LINES)
        self.assertEqual(clipped.events["limits"]["file_tools"]["max_read_chars"], MAX_READ_CHARS)
        self.assertEqual(
            clipped.events["limits"]["file_tools"]["max_list_results"], MAX_LIST_RESULTS
        )

    def test_a_large_directory_listing_is_bounded(self) -> None:
        target = self.repo / "many"
        target.mkdir()
        for index in range(260):
            (target / f"f{index:04d}.txt").write_text("x", encoding="utf-8")

        listing = PydanticHarnessRunner(
            model_config(), model=echo_tool_model("list_directory", {"path": "many"})
        ).run(AgentInvocation(uuid4(), self.repo, "read", 30))

        text = listing.response_text
        self.assertLessEqual(len(text.splitlines()), MAX_LIST_RESULTS + 5)
        self.assertIn("f0000.txt", text)
        self.assertNotIn("f0259.txt", text)

    def test_the_tool_caps_do_not_change_which_tools_or_paths_are_allowed(self) -> None:
        readable = [AgentInvocation(uuid4(), self.repo, "p", 5, role=role, read_only=True)
                    for role in ROLES]
        for invocation in readable:
            self.assertEqual(tools_for(invocation), READ_TOOLS)
        for tool in READ_TOOLS:
            self.assertNotIn("shell", tool)
            self.assertFalse(tool.startswith(("write", "edit", "create")))

        writable = AgentInvocation(
            uuid4(), self.repo, "p", 5, role="builder", read_only=False,
            allowed_tools=("read_file", "edit"),
        )
        self.assertEqual(tools_for(writable), READ_TOOLS + WRITE_TOOLS)

        # A writable Builder whose catalog grant withholds editing gets no writer.
        withheld = AgentInvocation(
            uuid4(), self.repo, "p", 5, role="builder", read_only=False,
            allowed_tools=("read_file",),
        )
        self.assertEqual(tools_for(withheld), READ_TOOLS)

        # Every non-Builder role stays read-only even if it asks to write.
        for role in ("intake", "architect", "qa", "reviewer"):
            self.assertEqual(
                tools_for(AgentInvocation(uuid4(), self.repo, "p", 5, role=role,
                                          read_only=False, allowed_tools=("edit",))),
                READ_TOOLS,
            )

        self.assertEqual(
            DENIED_PATTERNS,
            (
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
            ),
        )


class StatusRendering(BudgetedRun):
    def render(self, view: dict, *, as_json: bool) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli._emit(Namespace(json=as_json), view, cli.render_status(view))
        return out.getvalue()

    def test_cli_text_and_json_status_agree_about_limits_and_usage(self) -> None:
        view, _ = self.failing_intake()
        run = self.intake_run(view)
        usage = run["events"]["usage"]
        limits = run["events"]["limits"]

        payload = json.loads(self.render(view, as_json=True))
        text = self.render(view, as_json=False)

        self.assertEqual(payload, json.loads(json.dumps(view)))
        self.assertIn(f"requests={usage['requests']}", text)
        self.assertIn(f"tool_calls={usage['tool_calls']}", text)
        self.assertIn(f"total_tokens={usage['total_tokens']}", text)
        self.assertIn(f"requests={limits['request_limit']} (role_limits.intake)", text)
        self.assertIn(f"tool_calls={limits['tool_call_limit']} (global)", text)
        self.assertIn(f"timeout={limits['timeout_seconds']}s", text)
        self.assertIn("observed diagnostic warnings:", text)
        self.assertIn("not a hard cap", text)
        self.assertIn("Cost: unavailable", text)
        self.assertIn("failure=resource-limit-exceeded", text)
        self.assertNotIn(SECRET, text)

    def test_an_unmeasured_count_is_rendered_unknown_and_never_zero(self) -> None:
        view, _ = self.failing_intake()
        run = self.intake_run(view)
        run["events"] = json.loads(json.dumps(run["events"]))
        run["events"]["usage"].update(
            {"input_tokens": None, "output_tokens": None, "total_tokens": None,
             "availability": "unavailable", "unmeasured_requests": 2}
        )

        text = cli.render_status(view)
        self.assertIn("unknown", text)
        self.assertNotIn("tokens=0/0", text)
        self.assertIn("token counts are unknown, not zero", text)

    def test_a_failed_run_still_shows_the_tools_it_called(self) -> None:
        view, _ = self.failing_intake()
        run = self.intake_run(view)
        self.assertEqual(run["events"]["tool_summary"]["calls"], 2)
        self.assertEqual(run["events"]["tool_summary"]["by_tool"], {"read_file": 2})

        text = cli.render_status(view)
        self.assertIn("tool calls: 2 (read_file=2)", text)
        # The failed run has no final report; the tool counts are what makes it
        # diagnosable without reading raw evidence.
        payload = json.loads(self.render(view, as_json=True))
        self.assertEqual(payload["runs"][0]["events"]["tool_summary"]["calls"], 2)

    def test_a_successful_run_shows_counts_rather_than_only_tool_names(self) -> None:
        runner = PydanticHarnessRunner(
            model_config(), model=echo_tool_model("read_file", {"path": "README.md"})
        )
        result = runner.run(AgentInvocation(uuid4(), self.repo, "read", 30, role="qa"))

        text = cli.render_status(status_view(result.events))
        self.assertIn("tool calls: 1 (read_file=1)", text)
        self.assertNotIn("    tools:", text)

    def test_a_historical_run_without_a_summary_still_renders_its_tools(self) -> None:
        runner = PydanticHarnessRunner(
            model_config(), model=echo_tool_model("read_file", {"path": "README.md"})
        )
        result = runner.run(AgentInvocation(uuid4(), self.repo, "read", 30, role="qa"))
        events = json.loads(json.dumps(result.events))
        events.pop("tool_summary")

        text = cli.render_status(status_view(events))
        self.assertIn("tools: read_file", text)
        self.assertNotIn("tool calls:", text)

    def test_partial_token_accounting_is_rendered_as_a_lower_bound(self) -> None:
        view, _ = self.failing_intake()
        run = self.intake_run(view)
        events = json.loads(json.dumps(run["events"]))
        events["usage"].update(
            {
                "input_tokens": 1_000, "output_tokens": 20, "total_tokens": 1_020,
                "availability": "partial", "token_accounting": "partial",
                "tokens_are_lower_bound": True, "unmeasured_requests": 1,
            }
        )
        events["diagnostics"] = budget_report(events["usage"], events["limits"])
        run["events"] = events

        text = cli.render_status(view)
        self.assertIn("tokens=>=1000/>=20", text)
        self.assertIn("total_tokens=>=1020", text)
        self.assertIn("token totals are observed lower bounds", text)
        self.assertIn("at least 1020", text)
        self.assertNotIn("total_tokens used 1020", text)


class SecurityBoundaries(BudgetedRun):
    def test_approval_stays_digest_bound_and_the_audit_chain_stays_intact(self) -> None:
        engine, task_id = self.submitted(model=scripted_model())
        digest = engine.run(task_id)["proposal"]["digest"]

        with self.assertRaises(StoreError) as wrong:
            engine.approve(task_id, proposal_digest="0" * 64)
        self.assertEqual(wrong.exception.code, "digest-mismatch")
        self.assertEqual(engine.describe(task_id)["task"]["status"], "awaiting_approval")

        decision = engine.approve(task_id, proposal_digest=digest)
        self.assertEqual(decision["decision"], "approved")

        reopened = self.engine()
        audit = reopened.describe(task_id)["audit"]
        self.assertGreaterEqual(reopened.store.verify_audit_chain(), len(audit))
        for expected in ("task.submitted", "intake.recorded", "proposal.recorded",
                         "proposal.approved"):
            self.assertIn(expected, [event["event"] for event in audit])

        # The diagnostics added to run evidence did not soften the append-only
        # guarantee: the audit table still refuses in-place edits.
        with contextlib.closing(sqlite3.connect(self.state)) as database:
            with self.assertRaises(sqlite3.DatabaseError):
                database.execute("UPDATE audit_events SET actor = 'mallory'")

    def test_validation_network_isolation_and_run_workspace_modes_are_unchanged(self) -> None:
        environment = {"AGENT_MODEL_API_KEY": SECRET}
        base = {
            "model": {"base_url": "https://models.test/v1", "model_id": "test-model",
                      "approved_hosts": ["models.test"]},
            "sandbox": {"image": "python:3.12-slim"},
        }
        config = parse_config(base, environ=environment)
        self.assertIsNotNone(config.sandbox)
        self.assertEqual(config.sandbox.network, "none")

        # The isolated-network requirement is still enforced, not defaulted away.
        with self.assertRaises(ConfigError):
            parse_config(
                {**base, "sandbox": {"image": "python:3.12-slim", "network": "bridge"}},
                environ=environment,
            )

        engine, task_id = self.submitted(model=scripted_model())
        view = engine.run(task_id)
        for run in view["runs"]:
            self.assertFalse(run["events"]["workspace_writable"])
            self.assertEqual(run["events"]["granted_tools"], list(READ_TOOLS))
            self.assertEqual(run["events"]["withheld_tools"], ["shell", "network"])

    def test_the_diagnostics_never_carry_credentials(self) -> None:
        view, _ = self.failing_intake()
        serialized = json.dumps(view, sort_keys=True)
        self.assertNotIn(SECRET, serialized)
        for run in view["runs"]:
            self.assertNotIn(SECRET, json.dumps(run["events"], sort_keys=True))


if __name__ == "__main__":
    unittest.main()
