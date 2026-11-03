import json
import unittest

from support import (
    ROOT, TempCase, commit_all, git, make_consumer_repo, make_harness_repo, run_installer, run_tool,
)

INSTRUCTIONS = ".github/copilot-instructions.md"


class InstallTests(TempCase):
    def setUp(self):
        super().setUp()
        self.harness = make_harness_repo(self.tmp)
        self.v1 = git(self.harness, "rev-parse", "HEAD")
        self.consumer = make_consumer_repo(self.tmp)

    def install(self, *extra, consumer=None, ref=None):
        return run_installer("install", "--target", consumer or self.consumer, "--source", self.harness,
                             "--ref", ref or self.v1, *extra)

    def make_v2(self):
        skill = self.harness / ".github" / "skills" / "safe-validation" / "SKILL.md"
        skill.write_text(skill.read_text(encoding="utf-8") + "\nReviewed addition.\n", encoding="utf-8")
        self.assertEqual(run_tool(self.harness / "tools" / "build_manifest.py", cwd=self.harness).returncode, 0)
        return commit_all(self.harness, "harness v2")

    def test_install_discover_verify_and_leave_the_source_untouched(self):
        before = (git(self.harness, "rev-parse", "HEAD"), git(self.harness, "status", "--porcelain"))
        done = self.install()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual((git(self.harness, "rev-parse", "HEAD"), git(self.harness, "status", "--porcelain")), before)
        self.assertTrue((self.consumer / ".github/agents/loomsphere-qa.agent.md").is_file())
        self.assertTrue((self.consumer / ".github/skills/safe-validation/SKILL.md").is_file())
        self.assertTrue((self.consumer / "docs/workflows/feature-lifecycle.md").is_file())
        self.assertFalse((self.consumer / INSTRUCTIONS).exists(), "instructions are opt-in")
        self.assertFalse((self.consumer / "docs/decisions").exists(), "consumer-owned paths are never created")
        verify = run_installer("verify", "--target", self.consumer, "--source", self.harness, "--strict")
        self.assertEqual(verify.returncode, 0, verify.stderr)
        found = json.loads(run_installer("discover", "--target", self.consumer, "--json").stdout)
        self.assertEqual(len(found["agents"]), 7)
        self.assertEqual(len(found["skills"]), 13)
        self.assertEqual(found["problems"], [])
        self.assertTrue(found["deepseekHarness"]["customSkillDirs"][0].endswith(".github/skills"))

    def test_install_is_reproducible_from_the_pin(self):
        other = make_consumer_repo(self.tmp, "second")
        self.assertEqual(self.install().returncode, 0)
        self.make_v2()
        self.assertEqual(self.install(consumer=other).returncode, 0)
        self.assertEqual(self.lock(self.consumer)["files"], self.lock(other)["files"])
        self.assertEqual(self.lock(self.consumer)["source"]["commit"], self.v1)

    def test_pin_by_tag_and_expected_digest(self):
        git(self.harness, "tag", "v1.0.0")
        digest = json.loads((self.harness / "harness.manifest.json").read_text())["suite_digest"]
        done = self.install("--expect-suite-digest", digest, ref="v1.0.0")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.lock(self.consumer)["source"]["commit"], self.v1)
        other = make_consumer_repo(self.tmp, "second")
        bad = self.install("--expect-suite-digest", "0" * 64, consumer=other)
        self.assertEqual(bad.returncode, 1)
        self.assertFalse((other / ".github").exists())

    def test_mutable_and_unknown_refs_are_refused(self):
        self.assertEqual(self.install(ref="main").returncode, 2)
        self.assertEqual(self.install(ref="no-such-ref").returncode, 2)
        self.assertFalse((self.consumer / ".github").exists())

    def test_non_git_target_is_refused(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        self.assertEqual(self.install(consumer=plain).returncode, 2)

    def test_source_that_fails_its_manifest_is_refused(self):
        skill = self.harness / ".github" / "skills" / "safe-validation" / "SKILL.md"
        skill.write_text(skill.read_text(encoding="utf-8") + "\ntampered\n", encoding="utf-8")
        bad = commit_all(self.harness, "tamper without manifest update")
        done = self.install(ref=bad)
        self.assertEqual(done.returncode, 1)
        self.assertIn("manifest", done.stderr)
        self.assertFalse((self.consumer / ".github").exists())

    def test_existing_consumer_files_conflict_until_authorized_then_restore(self):
        original = "# my own instructions\n"
        (self.consumer / ".github").mkdir()
        (self.consumer / INSTRUCTIONS).write_text(original, encoding="utf-8")
        skill = self.consumer / ".github/skills/safe-validation/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("my skill\n", encoding="utf-8")

        refused = self.install("--bucket", "instructions", "--bucket", "skills")
        self.assertEqual(refused.returncode, 3)
        self.assertIn(f"CONFLICT {INSTRUCTIONS}", refused.stderr)
        self.assertIn("CONFLICT .github/skills/safe-validation/SKILL.md", refused.stderr)
        self.assertEqual((self.consumer / INSTRUCTIONS).read_text(encoding="utf-8"), original)
        self.assertFalse((self.consumer / ".harness-lock.json").exists(), "a refused install writes nothing")
        self.assertFalse((self.consumer / ".github/skills/accessibility-testing").exists())

        partial = self.install("--bucket", "instructions", "--bucket", "skills", "--authorize-overwrite", INSTRUCTIONS)
        self.assertEqual(partial.returncode, 3, "authorizing one file does not authorize the other")

        done = self.install("--bucket", "instructions", "--bucket", "skills", "--authorize-all-overwrites")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotEqual((self.consumer / INSTRUCTIONS).read_text(encoding="utf-8"), original)
        self.assertEqual(run_installer("verify", "--target", self.consumer).returncode, 0)

        back = run_installer("uninstall", "--target", self.consumer)
        self.assertEqual(back.returncode, 0, back.stderr)
        self.assertEqual((self.consumer / INSTRUCTIONS).read_text(encoding="utf-8"), original)
        self.assertEqual(skill.read_text(encoding="utf-8"), "my skill\n")
        self.assertFalse((self.consumer / ".harness-lock.json").exists())
        self.assertFalse((self.consumer / ".harness-backup").exists())
        self.assertFalse((self.consumer / ".github/skills/accessibility-testing").exists())

    def test_identical_preexisting_file_is_adopted_and_kept_on_uninstall(self):
        (self.consumer / "docs/workflows").mkdir(parents=True)
        source = (self.harness / "docs/workflows/feature-lifecycle.md").read_bytes()
        (self.consumer / "docs/workflows/feature-lifecycle.md").write_bytes(source)
        self.assertEqual(self.install().returncode, 0)
        run_installer("uninstall", "--target", self.consumer)
        self.assertEqual((self.consumer / "docs/workflows/feature-lifecycle.md").read_bytes(), source)
        self.assertFalse((self.consumer / ".github/agents").exists())

    def test_integrity_violations_are_detected(self):
        self.assertEqual(self.install().returncode, 0)
        skill = self.consumer / ".github/skills/safe-validation/SKILL.md"
        skill.write_text(skill.read_text(encoding="utf-8") + "\nedited\n", encoding="utf-8")
        agent = self.consumer / ".github/agents/loomsphere-qa.agent.md"
        agent.unlink()
        extra = self.consumer / ".github/skills/rogue/SKILL.md"
        extra.parent.mkdir()
        extra.write_text("---\nname: rogue\n---\n", encoding="utf-8")
        done = run_installer("verify", "--target", self.consumer, "--strict")
        self.assertEqual(done.returncode, 1)
        self.assertIn("modified: .github/skills/safe-validation/SKILL.md", done.stderr)
        self.assertIn("missing: .github/agents/loomsphere-qa.agent.md", done.stderr)
        self.assertIn("unmanaged file in a managed tree: .github/skills/rogue/SKILL.md", done.stderr)

    def test_a_forged_lock_is_caught_against_the_pinned_commit(self):
        self.assertEqual(self.install().returncode, 0)
        skill = self.consumer / ".github/skills/safe-validation/SKILL.md"
        skill.write_text("forged\n", encoding="utf-8")
        import hashlib
        lock = self.lock(self.consumer)
        lock["files"][".github/skills/safe-validation/SKILL.md"]["sha256"] = hashlib.sha256(b"forged\n").hexdigest()
        (self.consumer / ".harness-lock.json").write_text(json.dumps(lock), encoding="utf-8")
        self.assertEqual(run_installer("verify", "--target", self.consumer).returncode, 0)
        done = run_installer("verify", "--target", self.consumer, "--source", self.harness)
        self.assertEqual(done.returncode, 1)
        self.assertIn("does not match the pinned commit", done.stderr)

    def test_update_moves_to_a_reviewed_revision_and_back(self):
        self.assertEqual(self.install().returncode, 0)
        v2 = self.make_v2()
        done = run_installer("update", "--target", self.consumer, "--source", self.harness, "--ref", v2)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("Reviewed addition.", (self.consumer / ".github/skills/safe-validation/SKILL.md").read_text())
        self.assertEqual(self.lock(self.consumer)["source"]["commit"], v2)
        self.assertEqual(self.lock(self.consumer)["history"][0]["commit"], self.v1)
        self.assertEqual(run_installer("verify", "--target", self.consumer, "--source", self.harness).returncode, 0)
        back = run_installer("update", "--target", self.consumer, "--source", self.harness, "--ref", self.v1)
        self.assertEqual(back.returncode, 0, back.stderr)
        self.assertNotIn("Reviewed addition.", (self.consumer / ".github/skills/safe-validation/SKILL.md").read_text())

    def test_update_refuses_to_clobber_local_edits(self):
        self.assertEqual(self.install().returncode, 0)
        v2 = self.make_v2()
        skill = self.consumer / ".github/skills/safe-validation/SKILL.md"
        skill.write_text("local edit\n", encoding="utf-8")
        done = run_installer("update", "--target", self.consumer, "--source", self.harness, "--ref", v2)
        self.assertEqual(done.returncode, 3)
        self.assertEqual(skill.read_text(encoding="utf-8"), "local edit\n")
        self.assertEqual(self.lock(self.consumer)["source"]["commit"], self.v1)

    def test_update_drops_files_the_new_revision_no_longer_ships(self):
        self.assertEqual(self.install().returncode, 0)
        (self.harness / ".github/skills/accessibility-testing/SKILL.md").unlink()
        run_tool(self.harness / "tools" / "build_manifest.py", cwd=self.harness)
        v2 = commit_all(self.harness, "drop a skill")
        done = run_installer("update", "--target", self.consumer, "--source", self.harness, "--ref", v2)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse((self.consumer / ".github/skills/accessibility-testing").exists())

    def test_uninstall_leaves_modified_files_unless_forced(self):
        self.assertEqual(self.install().returncode, 0)
        skill = self.consumer / ".github/skills/safe-validation/SKILL.md"
        skill.write_text("local edit\n", encoding="utf-8")
        done = run_installer("uninstall", "--target", self.consumer)
        self.assertEqual(done.returncode, 1)
        self.assertTrue(skill.is_file())
        self.assertFalse((self.consumer / ".github/agents").exists())
        forced = run_installer("uninstall", "--target", self.consumer, "--force")
        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertEqual(git(self.consumer, "status", "--porcelain"), "")

    def test_clean_uninstall_leaves_the_repository_as_it_was(self):
        self.assertEqual(self.install().returncode, 0)
        self.assertNotEqual(git(self.consumer, "status", "--porcelain"), "")
        self.assertEqual(run_installer("uninstall", "--target", self.consumer).returncode, 0)
        self.assertEqual(git(self.consumer, "status", "--porcelain"), "")

    def test_dry_run_writes_nothing(self):
        done = self.install("--dry-run")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(git(self.consumer, "status", "--porcelain"), "")

    def test_install_from_a_url(self):
        done = run_installer("install", "--target", self.consumer, "--url", self.harness.as_uri(), "--ref", self.v1)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.lock(self.consumer)["source"]["repository"], self.harness.as_uri())

    def test_agents_only_selection(self):
        done = self.install("--bucket", "agents")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse((self.consumer / ".github/skills").exists())
        found = run_installer("discover", "--target", self.consumer)
        self.assertNotEqual(found.returncode, 0, "agents without their skills must be reported")


if __name__ == "__main__":
    unittest.main()
