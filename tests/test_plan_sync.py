"""Calendar <-> Content Plan Sheet sync (plan_sync.py): the pure reconcile rules, then round trips against the
in-process fake Google from test_google."""
from __future__ import annotations

import unittest

from medialab import calendar, google, plan_sync
from tests.test_google import GoogleCase

COLS = plan_sync.COLS


def entry(eid, title, updated="2026-10-01T00:00:00Z", rev=1, **kw):
    return {"id": eid, "rev": rev, "when": "2026-11-02T10:00", "duration_min": 0, "channel": "ig", "title": title,
            "caption": "", "status": "idea", "project": None, "asset": None, "created": updated,
            "updated": updated, **kw}


class Reconcile(unittest.TestCase):
    def test_header_must_match(self):
        with self.assertRaises(ValueError):
            plan_sync.reconcile([["when", "id"]], [], {})

    def test_new_entries_append_and_new_rows_create(self):
        e = entry("e1", "From app")
        values = [COLS, ["", "2026-11-05", "tiktok", "From sheet", "", "draft"]]
        p = plan_sync.reconcile(values, [e], {})
        self.assertEqual(p["append"], ["e1"])
        self.assertEqual(len(p["creates"]), 1)
        self.assertEqual(p["creates"][0][1]["title"], "From sheet")

    def test_side_that_changed_wins_and_both_is_a_conflict(self):
        e = entry("e1", "App title", updated="2026-10-02T00:00:00Z")
        old_row = calendar.to_row(entry("e1", "Old title"))
        last = {"e1": {"row": plan_sync.row_hash(old_row), "updated": "2026-10-01T00:00:00Z"}}
        sheet_row = list(old_row)
        sheet_row[3] = "Sheet title"
        # only the sheet changed
        p = plan_sync.reconcile([COLS, sheet_row], [entry("e1", "Old title")], last)
        self.assertEqual(p["updates"][0][2]["title"], "Sheet title")
        # only the app changed: the row is rewritten from the entry, no update
        p = plan_sync.reconcile([COLS, old_row], [e], last)
        self.assertEqual((p["updates"], p["rows"], p["conflicts"]), ([], {1: "e1"}, 0))
        # both changed: calendar wins
        p = plan_sync.reconcile([COLS, sheet_row], [e], last)
        self.assertEqual((p["updates"], p["conflicts"]), ([], 1))

    def test_orphans_errors_and_duplicates(self):
        last = {"gone": {"row": "x", "updated": "y"}}
        values = [COLS, ["gone", "2026-11-01", "", "Deleted in app", "", "idea"],
                  ["", "", "", "No date", "", "idea"],
                  ["", "2026-11-01", "", "Bad status", "", "shipped"],
                  ["dup", "2026-11-01", "", "A", "", "idea"], ["dup", "2026-11-01", "", "B", "", "idea"]]
        p = plan_sync.reconcile(values, [], last)
        self.assertEqual(p["orphans"], 1)
        self.assertEqual(len(p["errors"]), 3, p["errors"])
        self.assertEqual(len(p["creates"]), 1)      # the first 'dup' row


class SyncRoundTrip(GoogleCase):
    def setUp(self):
        super().setUp()
        self.connect()
        self.g.bootstrap("Plan")
        self.sid = self.g.workspace()["sheet"]

    def rows(self):
        return self.fake.sheets[self.sid]["tabs"][google.PLAN_TAB]

    def test_round_trip(self):
        calendar.create({"when": "2026-11-02T10:00", "title": "Launch post", "channel": "ig"})
        out = plan_sync.sync(self.g)
        self.assertEqual((out["to_sheet"], out["from_sheet"], out["wrote"]), (1, 0, True))
        self.assertEqual(self.rows()[1][3], "Launch post")
        # nothing changed: no write
        self.assertFalse(plan_sync.sync(self.g)["wrote"])
        # someone edits the title in the Sheet and adds a row without an id
        self.rows()[1][3] = "Launch post v2"
        self.rows().append(["", "2026-11-09", "tiktok", "Teaser", "", "draft"])
        out = plan_sync.sync(self.g)
        self.assertEqual(out["from_sheet"], 2, out)
        titles = sorted(e["title"] for e in calendar.list_entries())
        self.assertEqual(titles, ["Launch post v2", "Teaser"])
        new_id = next(e["id"] for e in calendar.list_entries() if e["title"] == "Teaser")
        self.assertEqual(self.rows()[2][0], new_id)            # id written back
        # the app changes an entry: the Sheet follows
        e = next(e for e in calendar.list_entries() if e["title"] == "Teaser")
        calendar.update(e["id"], e["rev"], {"status": "ready"})
        out = plan_sync.sync(self.g)
        self.assertEqual(self.rows()[2][5], "ready")
        self.assertEqual(out["conflicts"], 0)

    def test_header_tampering_is_refused(self):
        self.rows()[0][0] = "ID!"
        with self.assertRaises(ValueError):
            plan_sync.sync(self.g)


if __name__ == "__main__":
    unittest.main()
