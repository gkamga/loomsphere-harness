import json
import posixpath
import re
import unittest

from support import ROOT, run_tool


class ConsumerPathTests(unittest.TestCase):
    """The harness may depend on a consumer-owned path only if it declares it."""

    def test_bare_checkout_fails_only_on_declared_consumer_paths(self):
        names = {entry["path"] for entry in json.loads((ROOT / "consumer-paths.json").read_text(encoding="utf-8"))["paths"]}
        report = json.loads(run_tool(ROOT / "tools" / "validate_harness.py", "--bare", "--json").stdout)
        self.assertFalse(report["passed"], "bare checkout unexpectedly has every consumer path")
        for finding in report["errors"]:
            quoted = re.search(r"'([^']+)'", finding["message"]).group(1)
            if finding["code"] == "link-broken":
                missing = posixpath.normpath(posixpath.join(posixpath.dirname(finding["path"]), quoted.split("#")[0]))
            else:
                missing = quoted
            self.assertIn(missing, names, f"undeclared dependency: {finding}")

    def test_declared_paths_are_referenced_and_complete(self):
        declared = json.loads((ROOT / "consumer-paths.json").read_text(encoding="utf-8"))["paths"]
        for entry in declared:
            self.assertTrue({"path", "referencedBy", "purpose", "ifAbsent", "stub"} <= set(entry), entry["path"])
            for referrer in entry["referencedBy"]:
                self.assertTrue((ROOT / referrer).is_file(), f"{entry['path']} cites missing {referrer}")

    def test_no_harness_owned_document_is_shadowed_by_a_declared_path(self):
        declared = {entry["path"] for entry in json.loads((ROOT / "consumer-paths.json").read_text(encoding="utf-8"))["paths"]}
        for doc in (ROOT / "docs" / "workflows").glob("*.md"):
            self.assertNotIn(doc.relative_to(ROOT).as_posix(), declared)

    def test_top_level_documents_have_no_broken_relative_links(self):
        for name in ("README.md", "docs/CONSUMING.md"):
            path = ROOT / name
            for target in re.findall(r"\]\(([^)#\s]+)", path.read_text(encoding="utf-8")):
                if target.startswith(("http://", "https://", "mailto:")):
                    continue
                self.assertTrue((path.parent / target).exists(), f"{name} links to missing {target}")


if __name__ == "__main__":
    unittest.main()
