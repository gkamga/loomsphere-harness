import json
import unittest

from support import ROOT, TempCase, make_harness_repo, run_tool


class ManifestTests(TempCase):
    def test_committed_manifest_matches_the_tree(self):
        done = run_tool(ROOT / "tools" / "build_manifest.py", "--check")
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_manifest_inventory(self):
        manifest = json.loads((ROOT / "harness.manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["counts"]["agents"], 7)
        self.assertEqual(manifest["counts"]["skills"], 13)
        self.assertEqual(manifest["counts"]["files"], len(manifest["assets"]))
        self.assertFalse([name for name in manifest["assets"] if name.startswith("workflows/")])
        self.assertIn("docs/workflows/feature-lifecycle.md", manifest["docs"])

    def test_modified_asset_is_detected(self):
        repo = make_harness_repo(self.tmp)
        skill = repo / ".github" / "skills" / "safe-validation" / "SKILL.md"
        skill.write_text(skill.read_text(encoding="utf-8") + "\nextra\n", encoding="utf-8")
        self.assertEqual(run_tool(repo / "tools" / "build_manifest.py", "--check").returncode, 1)

    def test_removed_and_added_assets_are_detected(self):
        repo = make_harness_repo(self.tmp)
        (repo / ".github" / "skills" / "safe-validation" / "SKILL.md").unlink()
        self.assertEqual(run_tool(repo / "tools" / "build_manifest.py", "--check").returncode, 1)

    def test_added_asset_is_detected(self):
        repo = make_harness_repo(self.tmp)
        (repo / ".github" / "skills" / "new.md").write_text("x", encoding="utf-8")
        self.assertEqual(run_tool(repo / "tools" / "build_manifest.py", "--check").returncode, 1)

    def test_modified_doc_is_detected(self):
        repo = make_harness_repo(self.tmp)
        doc = repo / "docs" / "workflows" / "feature-lifecycle.md"
        doc.write_text(doc.read_text(encoding="utf-8") + "\nextra\n", encoding="utf-8")
        self.assertEqual(run_tool(repo / "tools" / "build_manifest.py", "--check").returncode, 1)

    def test_ci_workflow_is_not_a_consumer_asset(self):
        repo = make_harness_repo(self.tmp)
        workflows = repo / ".github" / "workflows"
        workflows.mkdir()
        (workflows / "ci.yml").write_text("name: x\n", encoding="utf-8")
        self.assertEqual(run_tool(repo / "tools" / "build_manifest.py", "--check").returncode, 0)


if __name__ == "__main__":
    unittest.main()
