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

    def test_group_and_status_selectors(self):
        cb.import_records(self.data, [{"text": "a"}, {"text": "b"}, {"text": "c"}])
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
        ])
        cb.assign_group(cb.select(self.data, ids=[1]), "g1")

    def test_filter_by_channel(self):
        self.assertEqual(len(cb.filter_bugs(self.data, channel="#x")), 2)

    def test_filter_ungrouped(self):
        ids = [b["id"] for b in cb.filter_bugs(self.data, ungrouped=True)]
        self.assertEqual(ids, [2, 3])

    def test_filter_by_group(self):
        self.assertEqual(len(cb.filter_bugs(self.data, group="g1")), 1)

    def test_summarize(self):
        s = cb.summarize(self.data)
        self.assertEqual(s["total"], 3)
        self.assertEqual(s["by_status"], {"open": 2, "resolved": 1})
        self.assertEqual(s["by_channel"], {"#x": 2, "#y": 1})


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
        self.assertIn("total: 0", out)

    def test_add_then_list_json(self):
        self.run_cli("add", "boom", "--channel", "#bugs")
        out = self.run_cli("list", "--json")
        bugs = json.loads(out)
        self.assertEqual(len(bugs), 1)
        self.assertEqual(bugs[0]["text"], "boom")
        self.assertEqual(bugs[0]["id"], 1)

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
        self.assertIn("total: 0", self.run_cli("stats"))


if __name__ == "__main__":
    unittest.main()
