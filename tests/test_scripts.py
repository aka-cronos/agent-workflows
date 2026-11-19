import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from agents_block import SUMMARY_MARK, Agent, parse_mark, rebuild  # noqa: E402
from carry import carried_description, design_only_carry, patch_id, statuses_to_carry  # noqa: E402
from common import match_paths, summarize_checks  # noqa: E402
from low_risk_merge import blockers, risk_grade  # noqa: E402
from zones import ChangedFile, classify, comment_only  # noqa: E402

PROCESS = {
    "version": 1,
    "uiFiles": ["src/routes/**", "src/components/**", "*.css"],
    "sensitiveZones": {"money": [], "access": ["src/server/auth/**"], "migrations": ["migrations/"]},
    "lowRiskLane": False,
}


def modified(path, patch="@@ -1 +1 @@\n-a = 1\n+a = 2"):
    return ChangedFile(path=path, status="modified", patch=patch)


class MatchPaths(unittest.TestCase):
    def test_gitignore_syntax(self):
        paths = ["migrations/001.sql", "lib/migrations/002.sql", "src/routes/a/b.tsx",
                 "src/x.css", "src/keep.css", "README.md"]
        matched = match_paths(["migrations/", "src/routes/**", "*.css", "!src/keep.css"], paths)
        self.assertEqual(matched, {"migrations/001.sql", "lib/migrations/002.sql",
                                   "src/routes/a/b.tsx", "src/x.css"})

    def test_empty(self):
        self.assertEqual(match_paths([], ["a"]), set())
        self.assertEqual(match_paths(["*"], []), set())


class CommentOnly(unittest.TestCase):
    def test_comment_lines(self):
        patch = "@@ -1,2 +1,2 @@\n-// old note\n+// new note\n \n+   \n+/* block */"
        self.assertTrue(comment_only(modified("src/a.ts", patch)))

    def test_code_line(self):
        self.assertFalse(comment_only(modified("src/a.ts", "@@\n+// note\n+const a = 1;")))

    def test_hash_is_code_in_css(self):
        self.assertFalse(comment_only(modified("src/a.css", "@@\n+#id { color: red; }")))

    def test_unknown_extension_or_new_file(self):
        self.assertFalse(comment_only(modified("src/a.unknown", "@@\n+// note")))
        self.assertFalse(comment_only(ChangedFile("src/a.ts", "added", patch="@@\n+// note")))
        self.assertFalse(comment_only(ChangedFile("src/a.ts", "modified", patch=None)))

    def test_sql(self):
        self.assertTrue(comment_only(modified("migrations/1.sql", "@@\n+-- why")))


class Classify(unittest.TestCase):
    def test_zones(self):
        files = [modified("src/server/auth/session.ts"), modified("migrations/002.sql"),
                 modified("src/components/Button.tsx")]
        result = classify(PROCESS, files, "someone", False)
        self.assertEqual(result.zones, ["access", "migrations", "ui"])
        self.assertIsNone(result.exempt)
        self.assertEqual(result.labels, {"zone:access", "zone:migrations", "zone:ui"})

    def test_comment_only_change_does_not_trigger(self):
        files = [modified("src/server/auth/session.ts", "@@\n-// old\n+// new")]
        self.assertEqual(classify(PROCESS, files, "someone", False).zones, [])

    def test_docs_only(self):
        files = [modified("README.md"), modified("docs/guide/setup.md")]
        result = classify(PROCESS, files, "someone", False)
        self.assertEqual(result.exempt, "docs only")
        self.assertEqual(result.labels, {"exempt"})

    def test_agent_instructions_are_not_docs(self):
        for path in ("AGENTS.md", "docs/agents/worktree.md", ".agents/skills/x/SKILL.md"):
            result = classify(PROCESS, [modified("README.md"), modified(path)], "someone", False)
            self.assertIsNone(result.exempt, path)

    def test_docs_files_extend_defaults(self):
        process = dict(PROCESS, docsFiles=["content/**"])
        result = classify(process, [modified("content/post.json")], "someone", False)
        self.assertEqual(result.exempt, "docs only")

    def test_rename_out_of_docs_is_not_docs_only(self):
        moved = ChangedFile("src/notes.ts", "renamed", previous_path="docs/notes.md")
        self.assertIsNone(classify(PROCESS, [moved], "someone", False).exempt)

    def test_dependency_update(self):
        files = [modified("package.json"), modified("pnpm-lock.yaml")]
        self.assertEqual(classify(PROCESS, files, "dependabot[bot]", True).exempt, "dependency update")
        self.assertIsNone(classify(PROCESS, files, "dependabot[bot]", False).exempt)
        self.assertIsNone(classify(PROCESS, files, "someone", True).exempt)


class Checks(unittest.TestCase):
    def test_summarize(self):
        done = {"status": "completed", "conclusion": "success"}
        self.assertEqual(summarize_checks([done, dict(done, conclusion="skipped")]), "green")
        self.assertEqual(summarize_checks([done, {"status": "in_progress", "conclusion": None}]), "pending")
        self.assertEqual(summarize_checks([done, dict(done, conclusion="failure")]), "red")
        self.assertEqual(summarize_checks([]), "red")


class Carry(unittest.TestCase):
    def test_patch_id_ignores_position(self):
        a = ("diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1,3 +1,3 @@\n x\n-y\n+z\n w\n")
        b = a.replace("@@ -1,3 +1,3 @@", "@@ -10,3 +10,3 @@")
        self.assertEqual(patch_id(a), patch_id(b))
        self.assertNotEqual(patch_id(a), patch_id(a.replace("+z", "+q")))
        self.assertIsNone(patch_id(""))

    def test_statuses_to_carry(self):
        previous = {"verify": {"context": "verify"}, "review/spec": {"context": "review/spec"}, "ci": {}}
        current = {"verify": {"context": "verify"}}
        self.assertEqual([s["context"] for s in statuses_to_carry(previous, current)], ["review/spec"])

    def test_design_only(self):
        ok = {"verify": {"state": "success"}, "review/spec": {"state": "success"}}
        self.assertTrue(design_only_carry(ok, {}, ui_touched=False))
        self.assertFalse(design_only_carry(ok, {}, ui_touched=True))
        self.assertFalse(design_only_carry(ok, {"verify": {"state": "failure"}}, ui_touched=False))
        spec_failed = dict(ok, **{"review/spec": {"state": "failure"}})
        self.assertFalse(design_only_carry(spec_failed, {}, ui_touched=False))

    def test_description(self):
        sha = "0123456789abcdef"
        self.assertEqual(carried_description(sha, "clean rebase", "0 blocking, 1 note"),
                         "carried from 0123456: clean rebase · 0 blocking, 1 note")
        self.assertEqual(carried_description(sha, "clean rebase", "carried from aaaaaaa: x"),
                         "carried from 0123456: clean rebase")


class LowRiskMerge(unittest.TestCase):
    def statuses(self, risk="low: small and revertible"):
        green = {"state": "success"}
        return {"verify": green, "review/spec": green, "review/design": green,
                "risk": {"state": "success", "description": risk}}

    def test_merges_when_all_green_and_low(self):
        self.assertEqual(blockers({"labels": []}, self.statuses(), "green"), [])

    def test_risk_grade(self):
        self.assertEqual(risk_grade("low: small"), "low")
        self.assertEqual(risk_grade("carried from abc1234: clean rebase · low: small"), "low")
        self.assertEqual(risk_grade("carried from abc1234: clean rebase"), "")
        self.assertEqual(risk_grade("lower than expected"), "lower than expected")
        self.assertEqual(risk_grade(None), "")

    def test_merges_a_carried_low_grade(self):
        statuses = self.statuses("carried from abc1234: clean rebase · low: small")
        self.assertEqual(blockers({"labels": []}, statuses, "green"), [])

    def test_waits(self):
        self.assertIn("risk is not low", blockers({"labels": []}, self.statuses("medium: x"), "green"))
        self.assertIn("checks pending", blockers({"labels": []}, self.statuses(), "pending"))
        self.assertIn("labelled zone:ui", blockers({"labels": [{"name": "zone:ui"}]}, self.statuses(), "green"))
        self.assertIn("labelled exempt", blockers({"labels": [{"name": "exempt"}]}, self.statuses(), "green"))
        self.assertIn("draft", blockers({"labels": [], "draft": True}, self.statuses(), "green"))
        missing = self.statuses()
        del missing["verify"]
        self.assertIn("verify missing", blockers({"labels": []}, missing, "green"))
        failed = dict(self.statuses(), **{"review/interrogate": {"state": "failure"}})
        self.assertIn("review/interrogate failure", blockers({"labels": []}, failed, "green"))


class AgentsBlock(unittest.TestCase):
    MARKED = "> [!NOTE]\n> 🤖 **GPT-6.1 Sol (high), design reviewer, on behalf of someone**\n\nLooks good."
    BODY = (
        "Fixes the thing.\nCloses #1\n\n## Summary\n\nStuff.\n\n## Agents\n\n"
        "| Role | Model | Effort | Tool | Thread |\n| --- | --- | --- | --- | --- |\n"
        "| Implementer | Claude Opus 5.5 | medium | Claude Code | link |"
    )

    def test_parse_mark(self):
        agent = parse_mark(self.MARKED, "u")
        self.assertEqual((agent.role, agent.model, agent.effort), ("design reviewer", "GPT-6.1 Sol", "high"))
        self.assertIsNone(parse_mark("Plain comment", "u"))
        no_effort = parse_mark("> 🤖 **Some Harness, verifier, on behalf of someone**", "u")
        self.assertEqual((no_effort.model, no_effort.effort, no_effort.role), ("Some Harness", None, "verifier"))

    def test_adds_missing_rows_and_summary(self):
        agents = [Agent("design reviewer", "GPT-6.1 Sol", "high", "https://c/1"),
                  Agent("implementer", "Claude Opus 5.5", "medium", "https://c/2")]
        statuses = {"review/design": {"state": "success", "description": "0 blocking, 1 note"}}
        out = rebuild(self.BODY, agents, statuses, 1)
        self.assertIn("| Design reviewer | GPT-6.1 Sol | high | — | [comment](https://c/1) |", out)
        self.assertEqual(out.count("Claude Opus 5.5"), 1)
        self.assertIn(SUMMARY_MARK + "\nReview rounds: 1 · review/design success (0 blocking, 1 note)", out)
        self.assertTrue(out.startswith("Fixes the thing.\nCloses #1\n\n## Summary\n\nStuff.\n\n## Agents\n"))
        self.assertEqual(rebuild(out, agents, statuses, 1), out)

    def test_summary_is_replaced(self):
        first = rebuild(self.BODY, [], {"verify": {"state": "pending"}}, 0)
        second = rebuild(first, [], {"verify": {"state": "success"}}, 0)
        self.assertNotIn("verify pending", second)
        self.assertEqual(second.count(SUMMARY_MARK), 1)

    def test_section_in_the_middle_keeps_the_next_one(self):
        body = self.BODY + "\n\n## Later\n\nText."
        out = rebuild(body, [Agent("spec reviewer", "GPT-6.1 Sol", "medium", "u")], {}, 0)
        self.assertTrue(out.endswith("| Spec reviewer | GPT-6.1 Sol | medium | — | [comment](u) |\n\n## Later\n\nText."))

    def test_creates_section_when_missing(self):
        out = rebuild("Body.", [Agent("verifier", "X", None, "u")], {}, 0)
        self.assertEqual(out, "Body.\n\n## Agents\n\n| Role | Model | Effort | Tool | Thread |\n"
                              "| --- | --- | --- | --- | --- |\n| Verifier | X | — | — | [comment](u) |")

    def test_nothing_to_write(self):
        self.assertEqual(rebuild("Body.", [], {}, 0), "Body.")


if __name__ == "__main__":
    unittest.main()
