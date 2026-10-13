"""Content calendar approvals over HTTP: the note on a status change, the history's `by` (the owner is "you", an
agent token writes its agent's name), the 500-character note limit, and the unscheduled list for the drafts rail.
"""
from __future__ import annotations

import unittest

from tests.test_server import TOKEN, ServerCase

OWNER = {"Authorization": "Bearer " + TOKEN}


class CalendarHistoryRoutes(ServerCase):
    def cfg_overrides(self):
        return {"token": TOKEN}

    def agent(self, name):
        code, _, j = self.req("POST", "/api/agents", {"name": name, "preset": "claude"}, headers=OWNER)
        self.assertEqual(code, 200, j)
        tok = next(w for w in j["prompt"].split() if w.startswith("mla_")).strip('"')
        return {"Authorization": "Bearer " + tok}

    def test_owner_and_agent_by(self):
        ag = self.agent("Scout bot")
        code, _, e = self.req("POST", "/api/calendar", {"when": None, "title": "Idea", "note": "from research"},
                              headers=ag)
        self.assertEqual(code, 200, e)
        self.assertEqual([(r["status"], r["note"], r["by"]) for r in e["history"]],
                         [("idea", "from research", "Scout bot")])
        code, _, e = self.req("POST", f"/api/calendar/{e['id']}",
                              {"rev": 1, "status": "ready", "when": "2026-10-20", "note": "Approved"}, headers=OWNER)
        self.assertEqual(code, 200, e)
        self.assertEqual((e["history"][-1]["by"], e["history"][-1]["note"], e["when"]), ("you", "Approved", "2026-10-20"))
        code, _, e = self.req("POST", f"/api/calendar/{e['id']}", {"rev": 2, "status": "posted"}, headers=ag)
        self.assertEqual((code, e["history"][-1]["by"], e["history"][-1]["note"]), (200, "Scout bot", ""))
        code, _, out = self.req("POST", f"/api/calendar/{e['id']}", {"rev": 3, "history": []}, headers=OWNER)
        self.assertEqual((code, len(out["history"])), (200, 3))   # history can't be overwritten

    def test_note_limit_and_unscheduled_list(self):
        code, _, e = self.req("POST", "/api/calendar", {"when": "", "title": "Rail"}, headers=OWNER)
        self.assertEqual(code, 200, e)
        code, _, out = self.req("POST", f"/api/calendar/{e['id']}", {"rev": 1, "status": "ready", "note": "x" * 501},
                                headers=OWNER)
        self.assertEqual(code, 400, out)
        self.assertIn("note", out["error"])
        self.req("POST", "/api/calendar", {"when": "2026-10-12", "title": "Dated"}, headers=OWNER)
        code, _, out = self.req("GET", "/api/calendar?unscheduled=1", headers=OWNER)
        self.assertEqual((code, [x["title"] for x in out["entries"]]), (200, ["Rail"]))
        out = self.req("GET", "/api/calendar?from=2026-10-01&to=2026-10-31", headers=OWNER)[2]
        self.assertEqual([x["title"] for x in out["entries"]], ["Dated"])


if __name__ == "__main__":
    unittest.main()
