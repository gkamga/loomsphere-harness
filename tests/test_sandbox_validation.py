"""The container boundary and the validation evidence it produces.

Every test here drives the production code with an injected runner, so the
fail-closed branches are proven without a container runtime: a missing runtime, an
unpinned image, a mutated source tree, and a refused command must all end as
"not executed" — never as a pass.
"""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

from loomsphere_harness.config import DEFAULT_VALIDATION_ALLOWLIST, SandboxConfig, ValidationConfig
from loomsphere_harness.schemas.acceptance_contract import build_contract
from loomsphere_harness.sandbox import (
    REASON_IMAGE_UNAVAILABLE,
    REASON_LAUNCH_FAILED,
    REASON_POLICY_INVALID,
    REASON_RUNTIME_UNAVAILABLE,
    REASON_SOURCE_MUTATED,
    REASON_SOURCE_UNVERIFIABLE,
    SandboxError,
    SandboxExecutor,
    SandboxPolicy,
    SandboxUnavailableError,
    executor_from_settings,
    sanitize_environment,
)
from loomsphere_harness.validation import (
    REASON_NOT_ALLOWLISTED,
    REASON_PYTEST_SELECTOR_NOT_FOUND,
    REASON_PYTEST_UNAVAILABLE,
    REASON_SANDBOX_UNAVAILABLE,
    REASON_TIMED_OUT,
    REASON_WORKSPACE_CHANGED,
    ValidationPlan,
    ValidationPlanError,
    execute_validation,
    execute_acceptance_verifications,
    plan_validation,
)
from loomsphere_harness.workspace import workspace_digest
from support import TempCase, git, make_consumer_repo

IMAGE = "python:3.12-slim"
IMAGE_ID = "sha256:" + "c" * 64


def policy(**overrides) -> SandboxPolicy:
    settings = {
        "image": IMAGE,
        "timeout_seconds": 60,
        "cpus": 1.0,
        "memory_mb": 512,
        "pids_limit": 64,
        "tmpfs_mb": 64,
    }
    settings.update(overrides)
    return SandboxPolicy(**settings)


def completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(args=["docker"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


class FakeRuntime:
    """Answers ``image inspect`` and ``run`` and records every invocation."""

    def __init__(self, *, run_result=None, image_result=None, on_run=None):
        self.calls: list[list[str]] = []
        self.run_result = run_result if run_result is not None else completed("1 passed\n")
        self.image_result = image_result if image_result is not None else completed(IMAGE_ID)
        self.on_run = on_run

    def __call__(self, command, **kwargs):
        self.calls.append([str(item) for item in command])
        if command[:3] == ["docker", "image", "inspect"]:
            return self.image_result
        if self.on_run is not None:
            self.on_run(command, **kwargs)
        return self.run_result


class SandboxPolicyTests(unittest.TestCase):
    def test_a_complete_policy_describes_its_boundary(self) -> None:
        described = policy().describe()

        self.assertEqual(described["network"], "none")
        self.assertEqual(described["user"], "65534:65534")
        self.assertEqual(described["workdir"], "/workspace")
        self.assertTrue(described["read_only_source"])

    def test_a_policy_that_could_widen_the_boundary_is_refused(self) -> None:
        cases = {
            "network": {"network": "bridge"},
            "root-user": {"user": "0"},
            "relative-workdir": {"workdir": "workspace"},
            "empty-image": {"image": "  "},
            "no-timeout": {"timeout_seconds": 0},
            "tiny-memory": {"memory_mb": 8},
            "tiny-pids": {"pids_limit": 1},
            "tiny-tmpfs": {"tmpfs_mb": 1},
            "no-cpus": {"cpus": 0},
        }
        for name, overrides in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(SandboxError) as caught:
                    policy(**overrides)
                self.assertEqual(caught.exception.code, REASON_POLICY_INVALID)

    def test_the_timeout_can_be_narrowed_for_one_run(self) -> None:
        narrowed = policy().with_timeout(5)

        self.assertEqual(narrowed.timeout_seconds, 5)
        self.assertEqual(narrowed.image, IMAGE)

    def test_only_allowlisted_environment_variables_are_accepted(self) -> None:
        self.assertEqual(sanitize_environment({"LANG": "C.UTF-8"}), {"LANG": "C.UTF-8"})
        self.assertEqual(sanitize_environment(None), {})

        for environment in ({"LOOMSPHERE_MODEL_API_KEY": "value"}, {"LANG": "a\0b"}):
            with self.subTest(environment=sorted(environment)):
                with self.assertRaises(SandboxError) as caught:
                    sanitize_environment(environment)
                self.assertEqual(caught.exception.code, REASON_POLICY_INVALID)

    def test_the_executor_bounds_its_own_settings(self) -> None:
        with self.assertRaises(SandboxError) as caught:
            SandboxExecutor(policy(), startup_grace_seconds=0)
        self.assertEqual(caught.exception.code, REASON_POLICY_INVALID)

        with self.assertRaises(SandboxError) as caught:
            SandboxExecutor(policy(), max_output_chars=10)
        self.assertEqual(caught.exception.code, REASON_POLICY_INVALID)

    def test_settings_become_an_executor(self) -> None:
        executor = executor_from_settings(
            SandboxConfig(image=IMAGE, timeout_seconds=120, network="none")
        )

        self.assertEqual(executor.policy.image, IMAGE)
        self.assertEqual(executor.policy.timeout_seconds, 120)
        self.assertEqual(executor.runtime, "docker")


class SandboxExecutionTests(TempCase):
    def setUp(self) -> None:
        super().setUp()
        # The executor digests the tree with git, so the fixture is a real one.
        self.workspace = make_consumer_repo(self.tmp)

    def executor(self, runner: FakeRuntime, **policy_overrides) -> SandboxExecutor:
        return SandboxExecutor(
            policy(**policy_overrides), runtime="docker", startup_grace_seconds=5,
            runner=runner,
        )

    def test_a_successful_run_records_bound_evidence(self) -> None:
        runtime = FakeRuntime()

        execution = self.executor(runtime).execute(
            ["python", "-m", "pytest", "-q"], self.workspace
        )

        self.assertTrue(execution.succeeded)
        self.assertEqual(execution.exit_code, 0)
        self.assertEqual(execution.image_id, IMAGE_ID)
        self.assertEqual(execution.source_digest, execution.source_digest_after)
        evidence = execution.as_evidence()
        self.assertEqual(evidence["engine"], "docker-sandbox")
        self.assertEqual(evidence["command"], ["python", "-m", "pytest", "-q"])
        self.assertEqual(evidence["limits"]["network"], "none")
        allowlist = evidence["environment_allowlist"]
        self.assertIn("LANG", allowlist)
        # The operator's own environment never reaches the validated code.
        self.assertFalse([name for name in allowlist if name.startswith("LOOMSPHERE")])

    def test_the_container_is_created_with_every_hardening_flag(self) -> None:
        runtime = FakeRuntime()

        self.executor(runtime).execute(["python", "-c", "print(1)"], self.workspace)

        run = runtime.calls[-1]
        joined = " ".join(run)
        self.assertEqual(run[:2], ["docker", "run"])
        self.assertIn("--rm", run)
        for expected in (
            "--cap-drop ALL",
            "--security-opt no-new-privileges:true",
            "--user 65534:65534",
            "--pids-limit 64",
            "--memory 512m",
            "--memory-swap 512m",
            "--cpus 1.0",
            "--read-only",
            "--network none",
            "--tmpfs /tmp:rw,nosuid,nodev,size=64m,mode=1777",
            "--workdir /workspace",
            f"--mount type=bind,source={self.workspace.resolve()},target=/workspace,ro",
            "--label loomsphere.sandbox=true",
        ):
            self.assertIn(expected, joined)
        self.assertNotIn("--privileged", joined)
        self.assertNotIn("docker.sock", joined)

    def test_a_missing_image_is_unavailable(self) -> None:
        runtime = FakeRuntime(image_result=completed("No such image", returncode=1))

        with self.assertRaises(SandboxUnavailableError) as caught:
            self.executor(runtime).execute(["python", "-V"], self.workspace)

        self.assertEqual(caught.exception.code, REASON_IMAGE_UNAVAILABLE)
        self.assertEqual(len(runtime.calls), 1)  # nothing was run

    def test_an_image_identity_that_is_not_pinned_is_refused(self) -> None:
        runtime = FakeRuntime(image_result=completed("python:3.12-slim\n"))

        with self.assertRaises(SandboxUnavailableError) as caught:
            self.executor(runtime).resolve_image_id()

        self.assertEqual(caught.exception.code, REASON_IMAGE_UNAVAILABLE)

    def test_a_missing_runtime_is_unavailable_and_never_falls_back(self) -> None:
        def missing(command, **kwargs):
            raise FileNotFoundError("docker")

        with self.assertRaises(SandboxUnavailableError) as caught:
            self.executor(missing).execute(["python", "-V"], self.workspace)

        self.assertEqual(caught.exception.code, REASON_RUNTIME_UNAVAILABLE)

    def test_a_missing_workspace_is_unverifiable(self) -> None:
        runtime = FakeRuntime()

        with self.assertRaises(SandboxUnavailableError) as caught:
            self.executor(runtime).execute(["python", "-V"], self.tmp / "absent")

        self.assertEqual(caught.exception.code, REASON_SOURCE_UNVERIFIABLE)
        self.assertEqual(runtime.calls, [])

    def test_an_empty_command_a_nul_argument_or_a_comma_mount_is_refused(self) -> None:
        comma = self.tmp / "with,comma"
        comma.mkdir()
        git(comma, "init", "-q", "-b", "main")

        for command, workspace in ((["python", "a\0b"], self.workspace),
                                   ([], self.workspace),
                                   (["python"], comma)):
            with self.subTest(command=command):
                with self.assertRaises(SandboxError) as caught:
                    self.executor(FakeRuntime()).execute(command, workspace)
                self.assertEqual(caught.exception.code, REASON_POLICY_INVALID)

    def test_a_launch_error_is_recorded_as_a_failure(self) -> None:
        runtime = FakeRuntime(run_result=completed("cannot start", returncode=125))

        execution = self.executor(runtime).execute(["python", "-V"], self.workspace)

        self.assertFalse(execution.succeeded)
        self.assertEqual(execution.failure_reason, REASON_LAUNCH_FAILED)

    def test_a_timeout_is_recorded_and_the_container_removed(self) -> None:
        def slow(command, **kwargs):
            if command[:3] == ["docker", "image", "inspect"]:
                return completed(IMAGE_ID)
            if command[:3] == ["docker", "rm", "-f"]:
                return completed()
            raise subprocess.TimeoutExpired(command, timeout=1, output="partial output")

        execution = self.executor(slow).execute(["python", "-V"], self.workspace)

        self.assertTrue(execution.timed_out)
        self.assertFalse(execution.succeeded)
        self.assertIn("partial output", execution.output)

    def test_a_mutated_source_tree_is_reported(self) -> None:
        def mutate(command, **kwargs):
            (self.workspace / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
            return completed("ok\n")
        execution = self.executor(FakeRuntime(on_run=mutate)).execute(
            ["python", "-V"], self.workspace
        )

        self.assertFalse(execution.succeeded)
        self.assertEqual(execution.failure_reason, REASON_SOURCE_MUTATED)
        self.assertNotEqual(execution.source_digest, execution.source_digest_after)

    def test_long_output_is_truncated_but_digested(self) -> None:
        runtime = FakeRuntime(run_result=completed("x" * 5_000))
        executor = SandboxExecutor(
            policy(), runtime="docker", startup_grace_seconds=5,
            max_output_chars=1_000, runner=runtime,
        )

        execution = executor.execute(["python", "-V"], self.workspace)

        self.assertLess(len(execution.output), 5_000)
        self.assertIn("output truncated", execution.output)
        self.assertRegex(execution.output_digest, r"^[0-9a-f]{64}$")


class ValidationPlanTests(TempCase):
    def test_a_python_project_is_validated_with_pytest(self) -> None:
        (self.tmp / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

        plan = plan_validation(self.tmp, allowlist=DEFAULT_VALIDATION_ALLOWLIST)

        self.assertEqual(plan.command, ("python", "-m", "pytest", "-q"))
        self.assertRegex(plan.digest, r"^[0-9a-f]{64}$")

    def test_a_standard_library_suite_is_the_fallback(self) -> None:
        (self.tmp / "tests").mkdir()

        plan = plan_validation(
            self.tmp,
            allowlist=[("python", "-m", "unittest", "discover", "-s", "tests")],
        )

        self.assertEqual(plan.command, ("python", "-m", "unittest", "discover", "-s", "tests"))

    def test_a_project_with_no_allowlisted_command_has_no_plan(self) -> None:
        with self.assertRaises(ValidationPlanError) as caught:
            plan_validation(self.tmp, allowlist=DEFAULT_VALIDATION_ALLOWLIST)

        self.assertEqual(caught.exception.code, "no-validation-plan")

    def test_an_unsafe_command_is_never_a_candidate(self) -> None:
        (self.tmp / "package.json").write_text(
            json.dumps({"scripts": {"test": "echo hi"}}), encoding="utf-8"
        )
        (self.tmp / "node_modules").mkdir()

        plan = plan_validation(self.tmp, allowlist=DEFAULT_VALIDATION_ALLOWLIST)

        self.assertEqual(plan.command, ("npm", "run", "test", "--silent"))


class ValidationExecutionTests(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.workspace = make_consumer_repo(self.tmp)
        (self.workspace / "tests").mkdir()
        self.plan = ValidationPlan(("python", "-m", "unittest", "discover", "-s", "tests"))

    def run_validation(self, runtime, **overrides):
        executor = SandboxExecutor(
            policy(), runtime="docker", startup_grace_seconds=5, runner=runtime
        )
        settings = {
            "timeout_seconds": 120,
            "excerpt_chars": 4_000,
            "allowlist": DEFAULT_VALIDATION_ALLOWLIST,
            "sandbox": ValidationConfig(),
            "executor": executor,
        }
        settings.update(overrides)
        return execute_validation(self.plan, self.workspace, **settings)

    def test_a_passing_command_records_the_evidence(self) -> None:
        outcome = self.run_validation(FakeRuntime())

        self.assertTrue(outcome.succeeded)
        self.assertEqual(outcome.exit_code, 0)
        record = outcome.as_record()
        self.assertEqual(record["sandbox_image"], IMAGE)
        self.assertEqual(record["sandbox_image_id"], IMAGE_ID)
        self.assertRegex(record["workspace_digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(record["execution"]["limits"]["network"], "none")

    def test_a_failing_command_is_a_validation_failure_not_an_error(self) -> None:
        outcome = self.run_validation(
            FakeRuntime(run_result=completed("1 failed\n", returncode=1))
        )

        self.assertFalse(outcome.succeeded)
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.exit_code, 1)

    def test_the_injected_executor_keeps_its_runner_and_gets_the_run_timeout(self) -> None:
        runtime = FakeRuntime()

        outcome = self.run_validation(runtime, timeout_seconds=77)

        # The runner was used, so the injected executor was preserved.
        self.assertTrue(runtime.calls)
        self.assertEqual(outcome.execution["limits"]["timeout_seconds"], 77)
        self.assertTrue(outcome.succeeded)

    def test_an_unavailable_sandbox_is_never_a_pass(self) -> None:
        def missing(command, **kwargs):
            raise FileNotFoundError("docker")

        outcome = self.run_validation(missing)

        self.assertFalse(outcome.succeeded)
        self.assertEqual(outcome.status, "error")
        self.assertEqual(outcome.failure_reason, REASON_SANDBOX_UNAVAILABLE)
        self.assertIsNone(outcome.exit_code)

    def test_a_timeout_is_reported_as_an_error(self) -> None:
        def slow(command, **kwargs):
            if command[:3] == ["docker", "image", "inspect"]:
                return completed(IMAGE_ID)
            if command[:3] == ["docker", "rm", "-f"]:
                return completed()
            raise subprocess.TimeoutExpired(command, timeout=1, output="partial")

        outcome = self.run_validation(slow)

        self.assertEqual(outcome.status, "error")
        self.assertEqual(outcome.failure_reason, REASON_TIMED_OUT)

    def test_a_mutated_worktree_invalidates_the_evidence(self) -> None:
        def mutate(command, **kwargs):
            (self.workspace / "extra.py").write_text("VALUE = 1\n", encoding="utf-8")
            return completed("ok\n")

        outcome = self.run_validation(FakeRuntime(on_run=mutate))

        self.assertEqual(outcome.status, "error")
        self.assertEqual(outcome.failure_reason, REASON_WORKSPACE_CHANGED)

    def test_a_command_outside_the_allowlist_is_refused_before_anything_runs(self) -> None:
        runtime = FakeRuntime()

        with self.assertRaises(ValidationPlanError) as caught:
            self.run_validation(runtime, allowlist=[("python", "-m", "pytest", "-q")])

        self.assertEqual(caught.exception.code, REASON_NOT_ALLOWLISTED)
        self.assertEqual(runtime.calls, [])


class AcceptanceVerificationTests(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.workspace = make_consumer_repo(self.tmp)

    def verify(self, runtime, *, selector="tests/test_contract.py::test_required", **overrides):
        executor = SandboxExecutor(
            policy(), runtime="docker", startup_grace_seconds=5, runner=runtime
        )
        contract = build_contract(
            [
                {
                    "criterion": "The required behavior is correct.",
                    "verification": {"kind": "pytest", "selector": selector},
                }
            ]
        )
        expected = {
            "timeout_seconds": 60,
            "sandbox": SandboxConfig(image=IMAGE),
            "executor": executor,
            "expected_source_digest": workspace_digest(self.workspace),
            "expected_image_id": IMAGE_ID,
        }
        expected.update(overrides)
        return execute_acceptance_verifications(
            contract, self.workspace, **expected
        )

    def runtime(self, *, version_code=0, collection_code=0, test_code=0, output="ok\n"):
        calls: list[list[str]] = []

        def runner(arguments, **kwargs):
            args = [str(item) for item in arguments]
            calls.append(args)
            if args[1:3] == ["image", "inspect"]:
                return completed(IMAGE_ID)
            command = args[args.index(IMAGE) + 1 :]
            if "--version" in command:
                return completed(output, returncode=version_code)
            if "--collect-only" in command:
                return completed(output, returncode=collection_code)
            return completed(output, returncode=test_code)

        return runner, calls

    def test_the_exact_selector_is_collected_and_executed_with_bound_evidence(self) -> None:
        runner, calls = self.runtime()
        selector = "tests/test_contract.py::test_required"

        coverage = self.verify(runner, selector=selector)

        result = coverage["criteria"][0]
        self.assertEqual(coverage["environment_check"]["status"], "succeeded")
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(result["selector_exists"])
        self.assertTrue(result["selector_executed"])
        self.assertEqual(
            result["collection"]["command"],
            ["python", "-m", "pytest", "--collect-only", "-q", selector],
        )
        self.assertEqual(
            result["execution"]["command"],
            ["python", "-m", "pytest", "-q", selector],
        )
        for evidence in (
            coverage["environment_check"],
            result["collection"],
            result["execution"],
        ):
            self.assertEqual(evidence["source_digest"], workspace_digest(self.workspace))
            self.assertEqual(evidence["source_digest_after"], evidence["source_digest"])
            self.assertEqual(evidence["sandbox_image_id"], IMAGE_ID)
        sandbox_runs = [call for call in calls if call[1:2] == ["run"]]
        self.assertEqual(len(sandbox_runs), 3)
        self.assertTrue(all(call[call.index("--network") + 1] == "none" for call in sandbox_runs))

    def test_a_missing_selector_is_not_counted_as_executed(self) -> None:
        runner, calls = self.runtime(
            collection_code=4,
            output="ERROR: file or directory not found: tests/test_missing.py::test_required",
        )

        coverage = self.verify(
            runner, selector="tests/test_missing.py::test_required"
        )

        result = coverage["criteria"][0]
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["selector_exists"])
        self.assertFalse(result["selector_executed"])
        self.assertEqual(result["failure_reason"], REASON_PYTEST_SELECTOR_NOT_FOUND)
        self.assertEqual(
            len([call for call in calls if call[1:2] == ["run"]]),
            2,
        )

    def test_an_image_without_pytest_reports_the_environment_obstacle(self) -> None:
        runner, calls = self.runtime(
            version_code=1, output="No module named pytest"
        )

        coverage = self.verify(runner)

        result = coverage["criteria"][0]
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["failure_reason"], REASON_PYTEST_UNAVAILABLE)
        self.assertFalse(result["selector_executed"])
        self.assertIn("No module named pytest", result["obstacle"])
        self.assertEqual(
            len([call for call in calls if call[1:2] == ["run"]]),
            1,
        )

    def test_a_different_image_identity_cannot_satisfy_the_obligation(self) -> None:
        runner, _ = self.runtime()

        coverage = self.verify(runner, expected_image_id="sha256:" + "d" * 64)

        result = coverage["criteria"][0]
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["failure_reason"], "acceptance-evidence-mismatch")
        self.assertFalse(result["selector_executed"])


if __name__ == "__main__":
    unittest.main()
