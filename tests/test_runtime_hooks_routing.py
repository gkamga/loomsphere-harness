"""Portable hook loader/evaluator integration and routing primitives."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runtime_support import HARNESS_ROOT, RuntimeCase, scripted_model  # noqa: E402

from loomsphere_harness.assets import CatalogSettings, verify_assets  # noqa: E402
from loomsphere_harness.harness_settings import bind_settings  # noqa: E402
from loomsphere_harness.lifecycle_hooks import load_registry  # noqa: E402
from loomsphere_harness.schemas.lifecycle_hooks import HookDefinitionError  # noqa: E402

HOOK = {
    "hook_version": 1, "name": "no-draft", "version": "1.0.0", "label": "No draft",
    "description": "Refuse invocations while the task is in the given status.",
    "events": ["before-agent-invocation"], "priority": 10, "mode": "blocking",
    "capabilities": ["read-task"],
    "checks": [{"kind": "task-status", "reason": "Frozen for this test.",
                "forbidden_statuses": ["submitted"]}],
}


class HookIntegration(RuntimeCase):
    def harness_with_hook(self, hook: dict | None) -> Path:
        copy = self.tmp / "harness"
        shutil.copytree(HARNESS_ROOT / ".github", copy / ".github",
                        ignore=shutil.ignore_patterns("workflows"))
        shutil.copytree(HARNESS_ROOT / "tools", copy / "tools",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy(HARNESS_ROOT / "harness.manifest.json", copy)
        if hook is not None:
            (copy / ".github" / "hooks" / "no-draft.json").write_text(json.dumps(hook))
            subprocess.run([sys.executable, "tools/build_manifest.py"], cwd=copy, check=True,
                           capture_output=True)
        verify_assets(copy)
        return copy

    def test_shipped_registry_is_empty(self) -> None:
        self.assertEqual(load_registry(HARNESS_ROOT / ".github" / "hooks").hooks, ())

    def test_no_hook_changes_nothing_and_records_no_hook_events(self) -> None:
        engine, task_id = self.submitted(model=scripted_model())
        self.assertEqual(engine.run(task_id)["task"]["status"], "awaiting_approval")
        self.assertFalse([e for e in engine.store.audit_events(task_id)
                          if e["event"].startswith("hook.")])

    def test_blocking_hook_denies_before_the_model_runs(self) -> None:
        calls: list = []
        config = self.config(harness_root=self.harness_with_hook(HOOK))
        engine, task_id = self.submitted(model=scripted_model(calls=calls), config=config)
        view = engine.run(task_id)
        self.assertEqual(calls, [])
        self.assertEqual(view["task"]["status"], "intake_failed")
        self.assertEqual(view["runs"][0]["failure_reason"], "hook-denied")
        events = [e["event"] for e in engine.store.audit_events(task_id)]
        self.assertIn("hook.before-agent-invocation", events)
        self.assertEqual(engine.store.verify_audit_chain() > 0, True)

    def test_hook_cannot_approve_or_skip_the_gate(self) -> None:
        observer = dict(HOOK, mode="observer", checks=[{"kind": "note", "text": "seen"}],
                        capabilities=[])
        config = self.config(harness_root=self.harness_with_hook(observer))
        engine, task_id = self.submitted(model=scripted_model(), config=config)
        self.assertEqual(engine.run(task_id)["task"]["status"], "awaiting_approval")
        self.assertIsNone(engine.describe(task_id)["approval"])

    def test_invalid_registry_fails_closed_before_any_run(self) -> None:
        bad = dict(HOOK, events=["nonsense"])
        config = self.config(harness_root=self.harness_with_hook(bad))
        engine, task_id = self.submitted(model=scripted_model(), config=config)
        with self.assertRaises(Exception) as raised:
            engine.run(task_id)
        self.assertEqual(getattr(raised.exception, "code", ""), "hook-registry-invalid")
        self.assertEqual(engine.describe(task_id)["runs"], [])

    def test_loader_rejects_malformed_directory(self) -> None:
        (self.tmp / "h").mkdir()
        (self.tmp / "h" / "x.json").write_text("{not json")
        with self.assertRaises(HookDefinitionError):
            load_registry(self.tmp / "h")


class RoutingPrimitives(unittest.TestCase):
    def test_route_primitives_match_catalog_and_are_deterministic(self) -> None:
        bind_settings(CatalogSettings(verify_assets(HARNESS_ROOT)))
        from loomsphere_harness import routing

        one = routing.classify_requirement(title="Add login", requirements="Rotate the token secret", acceptance_criteria="Works")
        two = routing.classify_requirement(title="Add login", requirements="Rotate the token secret", acceptance_criteria="Works")
        self.assertEqual(one, two)
        self.assertTrue(routing.specialist_specs())
        self.assertEqual(routing.normalize_path("./a//b.py"), "a/b.py")


class Seams(RuntimeCase):
    def test_import_routing_without_bound_settings_is_safe(self) -> None:
        code = ("import loomsphere_harness.routing as r\n"
                "assert r.normalize_path('./a/b') == 'a/b'\n"
                "try:\n    r.SPECIALISTS\nexcept RuntimeError:\n    print('lazy')\n")
        done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "lazy"), done.stderr)

    def test_lazy_snapshots_resolve_once_settings_are_bound(self) -> None:
        bind_settings(CatalogSettings(verify_assets(HARNESS_ROOT)))
        from loomsphere_harness import routing
        self.assertTrue(routing.SPECIALISTS)
        self.assertEqual(set(routing.SPECIALISTS), set(routing.SPECIALIST_ROLES))
        with self.assertRaises(AttributeError):
            routing.NOT_A_NAME

    def test_store_identity_repository_approval_environment_satisfy_protocols(self) -> None:
        from loomsphere_harness.approval import LocalApprovalGate
        from loomsphere_harness.engine import LocalReadOnlyEnvironment
        from loomsphere_harness.interfaces import ApprovalGate, ExecutionEnvironment, TaskStore
        engine = self.engine(model=scripted_model())
        self.assertIsInstance(engine.store, TaskStore)
        self.assertIsInstance(engine.approvals, ApprovalGate)
        self.assertIsInstance(engine.approvals, LocalApprovalGate)
        self.assertIsInstance(engine.environment, ExecutionEnvironment)
        self.assertIsInstance(engine.environment, LocalReadOnlyEnvironment)
        self.assertFalse(engine.environment.writable)

    def test_engine_runs_against_substituted_approval_and_environment(self) -> None:
        decisions: list = []

        class RecordingGate:
            def approve(self, task_id, *, proposal_digest):
                decisions.append(("approve", task_id, proposal_digest))
                return {"decision": "approved"}

            def reject(self, task_id, *, proposal_digest, reason):
                decisions.append(("reject", task_id, reason))
                return {"decision": "rejected"}

        engine, task_id = self.submitted(model=scripted_model())
        engine.approvals = RecordingGate()
        digest = engine.run(task_id)["proposal"]["digest"]
        engine.approve(task_id, proposal_digest=digest)
        self.assertEqual(decisions, [("approve", task_id, digest)])

    def test_writable_environment_is_refused(self) -> None:
        from loomsphere_harness.engine import Engine, EngineError

        class Writable:
            workspace_path = self.repo
            writable = True

        base = self.engine()
        with self.assertRaises(EngineError) as refused:
            Engine(store=base.store, config=base.config, repository=base.repository,
                   identity=base.identity, repo_path=self.repo, environment=Writable())
        self.assertEqual(refused.exception.code, "environment-writable")

    def test_digest_binding_still_enforced_through_the_gate(self) -> None:
        from loomsphere_harness.store import StoreError
        engine, task_id = self.submitted(model=scripted_model())
        engine.run(task_id)
        with self.assertRaises(StoreError):
            engine.approve(task_id, proposal_digest="0" * 64)
        self.assertIn("approval.refused",
                      [e["event"] for e in engine.store.audit_events(task_id)])
        self.assertIsNone(engine.describe(task_id)["approval"])


if __name__ == "__main__":
    unittest.main()
