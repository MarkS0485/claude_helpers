"""Unit tests for claude_projects.py.

Logical-data manipulation (classification, rule validation, registry round
trips, render, notes/plans) runs on temp copies with no network. Git-dependent
behaviour is guarded behind unittest.skipUnless against a temp `git init` repo.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

import claude_projects as cp

HAVE_GIT = shutil.which("git") is not None


def minimal_registry():
    """A small, valid registry covering work/group/code/children/links."""
    return {
        "schema": "claude-projects/v1",
        "root": "D:\\claude",
        "updated": "2026-01-01",
        "rule": "work may contain code; code never contains work.",
        "edgeTypes": ["consumes-nuget", "documents"],
        "groupLabels": {"w1": {"g1": "Group One"}},
        "nodes": [
            {"id": "w1", "name": "Work One", "type": "work", "path": None,
             "remote": None, "branch": None, "visibility": "private",
             "stack": "mixed", "description": "top work",
             "work": None, "group": None, "parent": None},
            {"id": "lib", "name": "Lib", "type": "code", "path": "Lib",
             "remote": "git@github.com:me/lib.git", "branch": "main",
             "visibility": "public", "stack": "C#", "description": "a library",
             "work": "w1", "group": "g1", "parent": None,
             "children": ["libwiki"]},
            {"id": "libwiki", "name": "Lib.wiki", "type": "code",
             "path": "Lib.wiki", "remote": None, "branch": None,
             "visibility": "public", "stack": "md", "description": "wiki",
             "work": "w1", "group": "g1", "parent": "lib"},
            {"id": "app", "name": "App", "type": "code", "path": "App",
             "remote": "git@github.com:me/app.git", "branch": "main",
             "visibility": "public", "stack": "C#", "description": "an app",
             "work": "w1", "group": "g1", "parent": None,
             "links": [{"to": "lib", "type": "consumes-nuget", "note": "pkg"}]},
        ],
    }


class ClassifyTests(unittest.TestCase):
    def test_own_git_is_code(self):
        self.assertEqual(cp.classify_dir("/x", True, False), "code")
        self.assertEqual(cp.classify_dir("/x", True, True), "code")

    def test_child_repos_no_git_is_work(self):
        self.assertEqual(cp.classify_dir("/x", False, True), "work")

    def test_no_git_no_child_repos_unclassified(self):
        self.assertIsNone(cp.classify_dir("/x", False, False))


class RuleValidatorTests(unittest.TestCase):
    def test_clean_registry_has_no_violations(self):
        self.assertEqual(cp.code_contains_work_violations(minimal_registry()), [])

    def test_code_with_work_child_via_parent_chain(self):
        data = minimal_registry()
        data["nodes"].append(
            {"id": "subwork", "name": "Sub", "type": "work", "path": "Lib/Sub",
             "remote": None, "branch": None, "visibility": "private",
             "stack": "x", "description": "", "work": "w1", "group": "g1",
             "parent": "lib"})
        viol = cp.code_contains_work_violations(data)
        self.assertIn(("lib", "subwork"), viol)

    def test_code_with_work_child_via_children_list(self):
        data = minimal_registry()
        data["nodes"].append(
            {"id": "subwork", "name": "Sub", "type": "work", "path": "Lib/Sub",
             "remote": None, "branch": None, "visibility": "private",
             "stack": "x", "description": "", "work": "w1", "group": "g1",
             "parent": None})
        for n in data["nodes"]:
            if n["id"] == "lib":
                n["children"].append("subwork")
        viol = cp.code_contains_work_violations(data)
        self.assertIn(("lib", "subwork"), viol)

    def test_validate_rule_exits_on_violation(self):
        data = minimal_registry()
        data["nodes"].append(
            {"id": "subwork", "name": "Sub", "type": "work", "path": None,
             "remote": None, "branch": None, "visibility": "private",
             "stack": "x", "description": "", "work": "w1", "group": "g1",
             "parent": "lib"})
        with self.assertRaises(SystemExit):
            cp.validate_rule(data)


class IntegrityTests(unittest.TestCase):
    def test_dangling_parent_flagged(self):
        data = minimal_registry()
        data["nodes"][1]["parent"] = "ghost"
        problems = cp.integrity_problems(data)
        self.assertTrue(any("dangling parent" in p for p in problems))

    def test_unresolved_link_flagged(self):
        data = minimal_registry()
        data["nodes"][3]["links"][0]["to"] = "ghost"
        problems = cp.integrity_problems(data)
        self.assertTrue(any("unresolved" in p for p in problems))

    def test_clean_registry_no_problems(self):
        self.assertEqual(cp.integrity_problems(minimal_registry()), [])


class RoundTripTests(unittest.TestCase):
    """load -> mutate -> save on a temp copy."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "projects.json")
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(minimal_registry(), f)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def reload(self):
        return cp.load_registry(self.path)

    def test_add_node_and_persist(self):
        data = self.reload()
        cp.add_node(data, "new", "New", "code", work="w1", group="g1",
                    path="New", remote="git@github.com:me/new.git")
        cp.save_registry(data, self.path)
        again = self.reload()
        self.assertIsNotNone(cp.node_by_id(again, "new"))

    def test_add_duplicate_id_exits(self):
        data = self.reload()
        with self.assertRaises(SystemExit):
            cp.add_node(data, "lib", "Dup", "code")

    def test_add_code_with_work_parent_rejected(self):
        data = self.reload()
        with self.assertRaises(SystemExit):
            # adding a work node whose parent is the code node 'lib'
            cp.add_node(data, "badwork", "Bad", "work", work="w1", parent="lib")

    def test_link_unlink_roundtrip(self):
        data = self.reload()
        cp.add_link(data, "app", "lib", "documents", note="docs")
        self.assertEqual(len(cp.node_by_id(data, "app")["links"]), 2)
        removed = cp.remove_link(data, "app", "lib", "documents")
        self.assertEqual(removed, 1)
        self.assertEqual(len(cp.node_by_id(data, "app")["links"]), 1)

    def test_unlink_nonexistent_exits(self):
        data = self.reload()
        with self.assertRaises(SystemExit):
            cp.remove_link(data, "app", "nope")

    def test_remove_node_strips_children_and_reports_dangling(self):
        data = self.reload()
        # 'app' links to 'lib'; removing 'lib' should report that dangling link
        # and strip 'libwiki' from lib's children first
        dangling = cp.remove_node(data, "lib")
        self.assertIsNone(cp.node_by_id(data, "lib"))
        self.assertIn(("app", "consumes-nuget"), dangling)

    def test_remove_child_strips_from_parent_children_list(self):
        data = self.reload()
        cp.remove_node(data, "libwiki")
        lib = cp.node_by_id(data, "lib")
        self.assertNotIn("libwiki", lib.get("children", []))

    def test_add_plan_dedups(self):
        data = self.reload()
        self.assertTrue(cp.add_plan(data, "lib", "plans/x.md"))
        self.assertFalse(cp.add_plan(data, "lib", "plans/x.md"))
        self.assertEqual(cp.node_by_id(data, "lib")["plans"], ["plans/x.md"])


class RenderTests(unittest.TestCase):
    def test_render_smoke_has_sections(self):
        text = cp.render_text(minimal_registry())
        self.assertTrue(text)
        self.assertIn("# REGISTRY", text)
        self.assertIn("## WORK", text)
        self.assertIn("### Group One", text)
        self.assertIn("## ASSETS / UNCLASSIFIED", text)
        self.assertIn("## LINK GRAPH", text)
        # a node bullet and a child line render
        self.assertIn("**Lib**", text)
        self.assertIn("`Lib.wiki`", text)

    def test_render_writes_file(self):
        d = tempfile.mkdtemp()
        try:
            out = os.path.join(d, "REGISTRY.md")
            cp.render_registry(minimal_registry(), out)
            self.assertTrue(os.path.getsize(out) > 0)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class NotesAndPlansTests(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_append_note_creates_file_and_sets_field(self):
        data = minimal_registry()
        path, set_field = cp.append_note(self.ws, data, "lib", "hello world")
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("# Lib notes", text)
        self.assertIn("hello world", text)
        self.assertTrue(set_field)
        self.assertTrue(cp.node_by_id(data, "lib")["notes"])

    def test_append_note_second_time_keeps_field(self):
        data = minimal_registry()
        cp.append_note(self.ws, data, "lib", "first")
        _, set_field = cp.append_note(self.ws, data, "lib", "second")
        self.assertFalse(set_field)  # field already set
        with open(cp.notes_file(self.ws, "lib"), encoding="utf-8") as f:
            self.assertIn("second", f.read())


class GitFreeHelperTests(unittest.TestCase):
    def test_is_conventional(self):
        self.assertTrue(cp.is_conventional("feat: add thing"))
        self.assertTrue(cp.is_conventional("fix(scope): bug"))
        self.assertTrue(cp.is_conventional("chore!: breaking"))
        self.assertFalse(cp.is_conventional("just some text"))
        self.assertFalse(cp.is_conventional(""))

    def test_push_url_injects_token(self):
        os.environ["GH_TOKEN"] = "FAKE_TOKEN_VALUE"
        try:
            url, used = cp.push_url_for("https://github.com/me/repo.git")
            self.assertTrue(used)
            self.assertIn("x-access-token:FAKE_TOKEN_VALUE@github.com", url)
        finally:
            del os.environ["GH_TOKEN"]

    def test_push_url_ssh_unchanged(self):
        os.environ["GH_TOKEN"] = "FAKE"
        try:
            url, used = cp.push_url_for("git@github.com:me/repo.git")
            self.assertFalse(used)
            self.assertEqual(url, "git@github.com:me/repo.git")
        finally:
            del os.environ["GH_TOKEN"]

    def test_hook_body_strips_attribution_patterns(self):
        self.assertIn("[Cc]o-[Aa]uthored-[Bb]y", cp.HOOK_BODY)
        self.assertIn("noreply@anthropic", cp.HOOK_BODY)
        self.assertIn("Generated with \\[Claude Code\\]", cp.HOOK_BODY)
        self.assertTrue(cp.HOOK_BODY.startswith("#!/bin/sh"))


@unittest.skipUnless(HAVE_GIT, "git not available")
class GitRepoTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q"], cwd=self.dir, check=True)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_is_git_repo_true(self):
        self.assertTrue(cp.is_git_repo(self.dir))

    def test_is_git_repo_false_for_plain_dir(self):
        plain = tempfile.mkdtemp()
        try:
            self.assertFalse(cp.is_git_repo(plain))
        finally:
            shutil.rmtree(plain, ignore_errors=True)

    def test_resolve_git_dir(self):
        gd = cp.resolve_git_dir(self.dir)
        self.assertTrue(gd and os.path.basename(gd) == ".git")

    def test_install_hook_writes_executable_commit_msg(self):
        hook = cp.install_hook(self.dir)
        self.assertTrue(os.path.exists(hook))
        self.assertEqual(os.path.basename(hook), "commit-msg")
        with open(hook, encoding="utf-8") as f:
            body = f.read()
        self.assertIn("[Cc]o-[Aa]uthored-[Bb]y", body)
        self.assertIn("anthropic", body)
        self.assertEqual(body, cp.HOOK_BODY)


if __name__ == "__main__":
    unittest.main()
