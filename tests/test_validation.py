import json
import re
import unittest

from support import ROOT, TempCase, make_harness_repo, run_tool

VALIDATE = ROOT / "tools" / "validate_harness.py"


class ValidationTests(TempCase):
    def test_assembled_consumer_shape_passes_the_unmodified_validator(self):
        done = run_tool(VALIDATE)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("0 error(s)", done.stdout)

    def test_every_validator_group_ran(self):
        report = json.loads(run_tool(VALIDATE, "--json").stdout)
        self.assertTrue(report["passed"])
        self.assertGreaterEqual(report["checks"], 10)
        self.assertIn(".github/contracts/examples/LS-DOC-001", report["scanRoots"])

    def test_a_missing_skill_fails_validation(self):
        repo = make_harness_repo(self.tmp)
        (repo / ".github" / "skills" / "safe-validation" / "SKILL.md").unlink()
        done = run_tool(repo / "tools" / "validate_harness.py", cwd=repo)
        self.assertEqual(done.returncode, 1)
        self.assertIn("ERROR", done.stdout)

    def test_a_broken_link_fails_validation(self):
        repo = make_harness_repo(self.tmp)
        doc = repo / "docs" / "workflows" / "feature-lifecycle.md"
        doc.write_text(doc.read_text(encoding="utf-8") + "\n[gone](does-not-exist.md)\n", encoding="utf-8")
        done = run_tool(repo / "tools" / "validate_harness.py", cwd=repo)
        self.assertEqual(done.returncode, 1)
        self.assertIn("link-broken", done.stdout)

    def test_an_invalid_example_record_fails_validation(self):
        repo = make_harness_repo(self.tmp)
        task = repo / ".github" / "contracts" / "examples" / "LS-DOC-001" / "task.json"
        document = json.loads(task.read_text(encoding="utf-8"))
        document.pop("taskId", None)
        document.pop("id", None)
        document["unexpectedField"] = True
        task.write_text(json.dumps(document), encoding="utf-8")
        self.assertEqual(run_tool(repo / "tools" / "validate_harness.py", cwd=repo).returncode, 1)

    def test_a_malformed_hook_registry_entry_fails_validation(self):
        repo = make_harness_repo(self.tmp)
        (repo / ".github" / "hooks" / "bad.json").write_text('{"name": "bad"}', encoding="utf-8")
        self.assertEqual(run_tool(repo / "tools" / "validate_harness.py", cwd=repo).returncode, 1)

    def test_an_agent_declaring_an_unknown_skill_fails_validation(self):
        repo = make_harness_repo(self.tmp)
        agent = repo / ".github" / "agents" / "loomsphere-qa.agent.md"
        text = agent.read_text(encoding="utf-8")
        agent.write_text(re.sub(r"skills: \[", "skills: [no-such-skill, ", text, count=1), encoding="utf-8")
        self.assertEqual(run_tool(repo / "tools" / "validate_harness.py", cwd=repo).returncode, 1)


if __name__ == "__main__":
    unittest.main()
