"""Unit tests for claude_bugs.py.

All pure store manipulation (normalize/import/group/status/filter/summarize)
runs on in-memory dicts; the CLI round-trips through a temp store file. No
network - Slack ingestion is Claude's job, not the helper's.
"""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

import claude_bugs as cb


class NormalizeTests(unittest.TestCase):
    def test_loose_field_names(self):
        rec = cb.normalize_record(
            {"message": "boom", "chan": "#x", "user": "bob",
             "timestamp": "t1", "url": "http://l"})
        self.assertEqual(rec["text"], "boom")
        self.assertEqual(rec["channel"], "#x")
        self.assertEqual(rec["author"], "bob")
        self.assertEqual(rec["ts"], "t1")
        self.assertEqual(rec["permalink"], "http://l")
        self.assertEqual(rec["source"], "slack")
        self.assertEqual(rec["status"], cb.STATUS_OPEN)

    def test_text_is_stripped(self):
        self.assertEqual(cb.normalize_record({"text": "  hi  "})["text"], "hi")

    def test_missing_text_raises(self):
        with self.assertRaises(ValueError):
            cb.normalize_record({"channel": "#x"})

    def test_bad_status_raises(self):
        with self.assertRaises(ValueError):
            cb.normalize_record({"text": "x", "status": "weird"})

    def test_summary_and_detail_split(self):
        rec = cb.normalize_record(
            {"text": "Boom: it broke\n  at frame 1\n  at frame 2"})
        self.assertEqual(rec["text"], "Boom: it broke")
        self.assertIn("at frame 2", rec["detail"])

    def test_single_line_has_no_detail(self):
        self.assertIsNone(cb.normalize_record({"text": "one liner"})["detail"])

    def test_explicit_detail_field(self):
        rec = cb.normalize_record({"text": "summary", "trace": "deep stack"})
        self.assertEqual(rec["detail"], "deep stack")


class SignatureTests(unittest.TestCase):
    def test_volatile_bits_masked_to_same_signature(self):
        a = cb.error_signature(
            "NullRef at /app/x.cs:line 42 id=ab12cd34-1111-2222-3333-444455556666")
        b = cb.error_signature(
            "NullRef at /srv/y.cs:line 99 id=99887766-aaaa-bbbb-cccc-ddddeeeeffff")
        self.assertEqual(a, b)

    def test_different_errors_differ(self):
        self.assertNotEqual(cb.error_signature("Timeout talking to db"),
                            cb.error_signature("Disk full on volume"))

    def test_signature_uses_first_line_only(self):
        self.assertEqual(cb.error_signature("Boom\n at a\n at b"),
                         cb.error_signature("Boom\n at c\n at d"))

    def test_label_is_stable_and_slugged(self):
        sig = cb.error_signature("System.NullReferenceException: nope")
        label = cb.signature_label(sig)
        self.assertEqual(label, cb.signature_label(sig))  # deterministic
        self.assertTrue(label.startswith("system-nullreferenceexception"))

    def test_empty_signature_has_no_label(self):
        self.assertIsNone(cb.signature_label(""))


class StoreMutationTests(unittest.TestCase):
    def setUp(self):
        self.data = cb.empty_store()

    def test_ids_increment_and_never_reuse(self):
        a = cb.add_bug(self.data, cb.normalize_record({"text": "one"}))
        b = cb.add_bug(self.data, cb.normalize_record({"text": "two"}))
        self.assertEqual([a["id"], b["id"]], [1, 2])
        cb.remove_bugs(self.data, lambda x: x["id"] == 2)
        c = cb.add_bug(self.data, cb.normalize_record({"text": "three"}))
        self.assertEqual(c["id"], 3)  # seq advances, id not reused

    def test_duplicates_are_kept(self):
        cb.add_bug(self.data, cb.normalize_record({"text": "same"}))
        cb.add_bug(self.data, cb.normalize_record({"text": "same"}))
        self.assertEqual(len(self.data["bugs"]), 2)

    def test_import_collects_errors(self):
        added, errors = cb.import_records(
            self.data, [{"text": "ok"}, {"channel": "#x"}, {"message": "ok2"}])
        self.assertEqual(len(added), 2)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0][0], 1)

    def test_import_autogroups_recurrences(self):
        cb.import_records(self.data, [
            {"text": "NullRef at /a.cs:line 1"},
            {"text": "NullRef at /b.cs:line 2"},
            {"text": "Timeout reaching db"},
        ])
        groups = {b["group"] for b in self.data["bugs"]}
        self.assertEqual(len(groups), 2)  # the two NullRefs share a group
        self.assertEqual(self.data["bugs"][0]["group"],
                         self.data["bugs"][1]["group"])

    def test_import_no_autogroup_leaves_groups_empty(self):
        cb.import_records(self.data, [{"text": "a"}], autogroup=False)
        self.assertIsNone(self.data["bugs"][0]["group"])

    def test_explicit_group_survives_autogroup(self):
        cb.import_records(self.data, [{"text": "a", "group": "mine"}])
        self.assertEqual(self.data["bugs"][0]["group"], "mine")

    def test_regroup_only_touches_ungrouped_by_default(self):
        cb.import_records(self.data, [
            {"text": "Same error here", "group": "keep"},
            {"text": "Same error here"},
        ], autogroup=False)
        cb.regroup_by_signature(self.data["bugs"])
        self.assertEqual(self.data["bugs"][0]["group"], "keep")
        self.assertIsNotNone(self.data["bugs"][1]["group"])

    def test_regroup_overwrite(self):
        cb.import_records(self.data, [{"text": "X", "group": "old"}],
                          autogroup=False)
        cb.regroup_by_signature(self.data["bugs"], overwrite=True)
        self.assertNotEqual(self.data["bugs"][0]["group"], "old")

    def test_group_and_status_selectors(self):
        cb.import_records(self.data, [{"text": "a"}, {"text": "b"}, {"text": "c"}],
                          autogroup=False)
        cb.assign_group(cb.select(self.data, ids=[1, 2]), "cluster")
        self.assertEqual([b["group"] for b in self.data["bugs"]],
                         ["cluster", "cluster", None])
        n = cb.set_status(cb.select(self.data, group="cluster"),
                          cb.STATUS_RESOLVED)
        self.assertEqual(n, 2)
        self.assertEqual(
            len(cb.filter_bugs(self.data, status=cb.STATUS_OPEN)), 1)

    def test_select_all(self):
        cb.import_records(self.data, [{"text": "a"}, {"text": "b"}])
        self.assertEqual(len(cb.select(self.data, all_=True)), 2)


class FilterAndSummaryTests(unittest.TestCase):
    def setUp(self):
        self.data = cb.empty_store()
        cb.import_records(self.data, [
            {"text": "a", "channel": "#x"},
            {"text": "b", "channel": "#y", "status": "resolved"},
            {"text": "c", "channel": "#x"},
        ], autogroup=False)
        cb.assign_group(cb.select(self.data, ids=[1]), "g1")

    def test_filter_by_channel(self):
        self.assertEqual(len(cb.filter_bugs(self.data, channel="#x")), 2)

    def test_filter_ungrouped(self):
        ids = [b["id"] for b in cb.filter_bugs(self.data, ungrouped=True)]
        self.assertEqual(ids, [2, 3])

    def test_filter_by_group(self):
        self.assertEqual(len(cb.filter_bugs(self.data, group="g1")), 1)

    def test_filter_closed_status(self):
        cb.set_status(cb.select(self.data, ids=[3]), cb.STATUS_CLOSED)
        self.assertEqual(
            len(cb.filter_bugs(self.data, status=cb.STATUS_CLOSED)), 1)

    def test_summarize(self):
        s = cb.summarize(self.data)
        self.assertEqual(s["total"], 3)
        self.assertEqual(s["open"], 2)
        self.assertEqual(s["resolved"], 1)
        self.assertEqual(s["closed"], 0)
        self.assertEqual(s["by_channel"], {"#x": 2, "#y": 1})

    def test_summarize_counts_closed(self):
        cb.set_status(cb.select(self.data, ids=[1]), cb.STATUS_CLOSED)
        self.assertEqual(cb.summarize(self.data)["closed"], 1)


class BacklogTests(unittest.TestCase):
    def test_900_open_is_30_days(self):
        self.assertEqual(cb.estimated_backlog_days(900), 30.0)

    def test_scales_linearly(self):
        self.assertEqual(cb.estimated_backlog_days(450), 15.0)
        self.assertEqual(cb.estimated_backlog_days(1800), 60.0)

    def test_zero_open_is_zero(self):
        self.assertEqual(cb.estimated_backlog_days(0), 0.0)


class RoundTripTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(self.path)  # load_store must cope with a missing file

    def tearDown(self):
        for p in (self.path, self.path + ".bak", self.path + ".tmp"):
            if os.path.exists(p):
                os.remove(p)

    def run_cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cb.main(["--store", self.path, *argv])
        return buf.getvalue()

    def test_missing_store_loads_empty(self):
        out = self.run_cli("stats")
        self.assertIn("total:    0", out)
        self.assertIn("backlog:", out)

    def test_add_then_list_json(self):
        self.run_cli("add", "boom", "--channel", "#bugs")
        out = self.run_cli("list", "--json")
        bugs = json.loads(out)
        self.assertEqual(len(bugs), 1)
        self.assertEqual(bugs[0]["text"], "boom")
        self.assertEqual(bugs[0]["id"], 1)

    def test_list_shows_status_emoji(self):
        self.run_cli("add", "boom")
        self.assertIn(cb.STATUS_EMOJI[cb.STATUS_OPEN], self.run_cli("list"))
        self.run_cli("resolve", "1")
        self.assertIn(cb.STATUS_EMOJI[cb.STATUS_RESOLVED], self.run_cli("list"))

    def test_close_and_purge(self):
        self.run_cli("add", "junk")
        self.run_cli("close", "1")
        out = self.run_cli("list", "--status", "closed", "--json")
        self.assertEqual(json.loads(out)[0]["status"], cb.STATUS_CLOSED)
        self.run_cli("remove", "--closed")
        self.assertEqual(json.loads(self.run_cli("list", "--json")), [])

    def test_import_then_regroup_cli(self):
        imp = self.path + ".in"
        with open(imp, "w", encoding="utf-8") as f:
            json.dump([{"text": "Crash boom A", "group": "x"},
                       {"text": "Crash boom B"}], f)
        self.run_cli("import", "--file", imp, "--no-autogroup")
        os.remove(imp)
        self.run_cli("regroup")
        bugs = json.loads(self.run_cli("list", "--json"))
        self.assertEqual(bugs[0]["group"], "x")          # kept
        self.assertIsNotNone(bugs[1]["group"])           # filled in

    def test_import_file_with_bom(self):
        imp = self.path + ".in"
        with open(imp, "w", encoding="utf-8-sig") as f:  # write a BOM
            json.dump([{"text": "x"}, {"message": "y"}], f)
        out = self.run_cli("import", "--file", imp)
        os.remove(imp)
        self.assertIn("imported 2", out)

    def test_persisted_seq_survives_reload(self):
        self.run_cli("add", "one")
        self.run_cli("remove", "1")
        self.run_cli("add", "two")
        out = self.run_cli("list", "--json")
        self.assertEqual(json.loads(out)[0]["id"], 2)  # not reused

    def test_remove_all_needs_yes(self):
        self.run_cli("add", "one")
        with self.assertRaises(SystemExit):
            self.run_cli("remove", "--all")
        self.run_cli("remove", "--all", "--yes")
        self.assertIn("total:    0", self.run_cli("stats"))


if __name__ == "__main__":
    unittest.main()
