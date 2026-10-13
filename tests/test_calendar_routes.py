"""Studio merge: the content calendar's HTTP routes and page files, on the real server (as tests/test_server.py
drives it) after server.install_extension(calendar_routes).

Covers: list/create/update/delete, 409 with the current entry, bad input, the guard (CSRF header, token, the
.ics feed behind the token), text/calendar output checked by the strict parser, the two static files with the
security headers, and static checks of calendar.js / calendar.css (strict CSP, CSRF header, public API).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import unittest

from medialab import calendar_routes, server
from tests.test_calendar import parse_ics, unescape
from tests.test_server import TOKEN, ServerCase
from tests.util import ROOT

# server.py installs calendar_routes at import (server.install_extension), like every feature module.


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


class CalendarRoutesTest(ServerCase):
    def new(self, **fields):
        body = {"when": "2026-10-12T09:30", "title": "Post", **fields}
        code, _, out = self.req("POST", "/api/calendar", body)
        self.assertEqual(code, 200, out)
        return out

    def test_crud(self):
        code, _, out = self.req("GET", "/api/calendar")
        self.assertEqual((code, out), (200, {"entries": [], "statuses": ["idea", "draft", "ready", "posted", "skipped"]}))
        e = self.new(channel="instagram_reel", caption="Hi", asset={"root": "gen", "name": "a.png"},
                     project="p_0123456789ab")
        self.assertRegex(e["id"], r"^c_[0-9a-f]{10}$")
        self.assertEqual((e["rev"], e["status"], e["asset"], e["project"]),
                         (1, "idea", {"root": "gen", "name": "a.png"}, "p_0123456789ab"))
        code, _, up = self.req("POST", f"/api/calendar/{e['id']}", {"rev": 1, "status": "ready", "asset": None})
        self.assertEqual(code, 200, up)
        self.assertEqual((up["rev"], up["status"], up["asset"], up["caption"]), (2, "ready", None, "Hi"))
        self.assertEqual(self.req("GET", "/api/calendar")[2]["entries"], [up])
        code, _, out = self.req("POST", f"/api/calendar/{e['id']}/delete", {"rev": 2})
        self.assertEqual((code, out), (200, {"ok": True}))
        self.assertEqual(self.req("GET", "/api/calendar")[2]["entries"], [])
        self.assertEqual(self.req("POST", f"/api/calendar/{e['id']}/delete", {})[0], 404)
        self.assertEqual(self.req("POST", f"/api/calendar/{e['id']}", {"rev": 2, "title": "x"})[0], 404)

    def test_stale_rev_is_409_with_the_current_entry(self):
        e = self.new()
        self.req("POST", f"/api/calendar/{e['id']}", {"rev": 1, "title": "Newer"})
        code, _, out = self.req("POST", f"/api/calendar/{e['id']}", {"rev": 1, "title": "Older"})
        self.assertEqual(code, 409)
        self.assertEqual(out["error"], "this entry was changed elsewhere")
        self.assertEqual((out["current"]["title"], out["current"]["rev"]), ("Newer", 2))
        code, _, out = self.req("POST", f"/api/calendar/{e['id']}/delete", {"rev": 1})
        self.assertEqual((code, out["current"]["rev"]), (409, 2))
        self.assertEqual(len(self.req("GET", "/api/calendar")[2]["entries"]), 1)

    def test_range_filter(self):
        for w in ("2026-09-30", "2026-10-01T08:00", "2026-10-31T23:00-07:00", "2026-11-01"):
            self.new(when=w)
        out = self.req("GET", "/api/calendar?from=2026-10-01&to=2026-10-31")[2]
        self.assertEqual([e["when"] for e in out["entries"]], ["2026-10-01T08:00", "2026-10-31T23:00-07:00"])
        self.assertEqual(len(self.req("GET", "/api/calendar?from=2026-10-01")[2]["entries"]), 3)
        self.assertEqual(len(self.req("GET", "/api/calendar?from=&to=")[2]["entries"]), 4)

    def test_bad_input_is_400(self):
        e = self.new()
        bad = [("POST", "/api/calendar", {"title": "no when"}),
               ("POST", "/api/calendar", {"when": "soon"}),
               ("POST", "/api/calendar", {"when": "2026-10-12", "status": "published"}),
               ("POST", "/api/calendar", {"when": "2026-10-12", "caption": "x" * 2201}),
               ("POST", "/api/calendar", {"when": "2026-10-12", "asset": {"root": "gen", "name": "../../x"}}),
               ("POST", "/api/calendar", {"when": "2026-10-12", "project": "../p"}),
               ("POST", "/api/calendar", {"when": "2026-10-12", "colour": "red"}),
               ("POST", f"/api/calendar/{e['id']}", {"title": "no rev"}),
               ("POST", f"/api/calendar/{e['id']}", {"rev": "1"}),
               ("POST", f"/api/calendar/{e['id']}/delete", {"rev": True}),
               ("POST", "/api/calendar/bad.id", {"rev": 1}),
               ("GET", "/api/calendar?from=yesterday", None),
               ("GET", "/api/calendar.ics?to=2026-13-01", None)]
        for method, path, body in bad:
            code, _, out = self.req(method, path, body)
            self.assertEqual(code, 400, (path, body, out))
            self.assertIsInstance(out.get("error"), str)
        self.assertEqual(self.req("GET", "/api/calendar")[2]["entries"], [e])   # nothing changed

    def test_guard_applies(self):
        self.assertEqual(self.req("POST", "/api/calendar", {"when": "2026-10-12"}, csrf=False)[0], 403)
        self.assertEqual(self.req("POST", "/api/calendar", {"when": "2026-10-12"},
                                  headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.req("GET", "/api/calendar", host="evil.example")[0], 403)
        self.assertEqual(self.req("GET", "/api/calendar/c_0123456789")[0], 405)
        self.assertEqual(self.req("POST", "/api/calendar.ics", {})[0], 405)
        self.assertEqual(self.req("GET", "/api/calendar", None)[2]["entries"], [])

    def test_ics_feed(self):
        self.new(when="2026-10-12T09:30-07:00", duration_min=30, channel="tiktok", title="A, b; c",
                 caption="Line 1\nLine 2", status="posted")
        self.new(when="2026-10-14", title="All day")
        code, h, body = self.req("GET", "/api/calendar.ics")
        self.assertEqual(code, 200)
        self.assertEqual(h["content-type"], "text/calendar; charset=utf-8")
        self.assertIn("default-src 'self'", h["content-security-policy"])
        self.assertEqual(h["x-content-type-options"], "nosniff")
        events = parse_ics(body.decode("utf-8"))
        self.assertEqual(len(events), 2)
        ev = events[0]
        self.assertEqual(unescape(ev["SUMMARY"][1]), "[tiktok] A, b; c")
        self.assertEqual(ev["DTSTART"][1], "20261012T163000Z")
        self.assertEqual(ev["STATUS"][1], "CONFIRMED")
        self.assertEqual(events[1]["DTSTART"], (";VALUE=DATE", "20261014"))
        self.assertTrue(ev["UID"][1].endswith("@media-lab"))
        _, _, part = self.req("GET", "/api/calendar.ics?from=2026-10-13")
        self.assertEqual(len(parse_ics(part.decode("utf-8"))), 1)

    def test_unreadable_file_is_503_and_kept(self):
        d = os.path.join(self.r.data, "calendar")
        os.makedirs(d)
        with open(os.path.join(d, "entries.json"), "w") as f:
            f.write("{broken")
        code, _, out = self.req("POST", "/api/calendar", {"when": "2026-10-12"})
        self.assertEqual(code, 503)
        self.assertIn("can't be read", out["error"])
        self.assertNotIn(self.r.base, out["error"])
        with open(os.path.join(d, "entries.json")) as f:
            self.assertEqual(f.read(), "{broken")

    def test_static_files_are_served(self):
        for path, ctype, needle in (("/static/calendar.js", "text/javascript; charset=utf-8", b"MediaLabCalendar"),
                                    ("/static/calendar.css", "text/css; charset=utf-8", b".cal-grid")):
            code, h, body = self.req("GET", path)
            self.assertEqual((code, h["content-type"]), (200, ctype), path)
            self.assertIn(needle, body)
            self.assertEqual(h["x-frame-options"], "DENY")
        self.assertEqual(self.req("GET", "/static/calendar.html")[0], 404)


class CalendarTokenTest(ServerCase):
    def cfg_overrides(self):
        return {"token": TOKEN}

    def test_token_guards_every_route_including_the_feed(self):
        for method, path in (("GET", "/api/calendar"), ("GET", "/api/calendar.ics"), ("POST", "/api/calendar")):
            self.assertEqual(self.req(method, path, {} if method == "POST" else None)[0], 401, path)
        auth = {"Authorization": "Bearer " + TOKEN}
        self.assertEqual(self.req("GET", "/api/calendar.ics", headers=auth)[0], 200)
        # no ?token= form exists (server._authed): a subscriber that can't send the header is refused
        self.assertEqual(self.req("GET", "/api/calendar.ics?token=" + TOKEN)[0], 401)
        cookie = self.req("POST", "/api/login", {"token": TOKEN})[1]["set-cookie"].split(";")[0]
        self.assertEqual(self.req("GET", "/api/calendar.ics", headers={"Cookie": cookie})[0], 200)
        # the page files stay public, like app.js
        self.assertEqual(self.req("GET", "/static/calendar.js")[0], 200)

    def test_secret_feed_link_for_calendar_apps(self):
        auth = {"Authorization": "Bearer " + TOKEN}
        self.assertEqual(self.req("GET", "/api/calendar/feed")[0], 401)
        self.req("POST", "/api/calendar", {"when": "2026-11-02T10:00", "title": "Launch", "channel": "ig"},
                 headers=auth)
        code, _, j = self.req("GET", "/api/calendar/feed", headers=auth)
        self.assertEqual(code, 200, j)
        self.assertTrue(j["url"].endswith(j["path"]))
        code, hdrs, body = self.req("GET", j["path"])  # no credential: the secret is the credential
        self.assertEqual(code, 200)
        self.assertTrue(hdrs["content-type"].startswith("text/calendar"))
        self.assertIn(b"SUMMARY:[ig] Launch", body)
        wrong = "/api/calendar/feed/" + "A" * 43 + ".ics"
        self.assertEqual(self.req("GET", wrong)[0], 404)
        code, _, j2 = self.req("POST", "/api/calendar/feed/rotate", {}, headers=auth)
        self.assertEqual(code, 200)
        self.assertNotEqual(j2["path"], j["path"])
        self.assertEqual(self.req("GET", j["path"])[0], 404)
        self.assertEqual(self.req("GET", j2["path"])[0], 200)


class CalendarPageStaticTest(unittest.TestCase):
    JS = _read("static", "calendar.js")
    CSS = _read("static", "calendar.css")

    def test_strict_csp_rules(self):
        for bad in ("style=", ".style.", "setAttribute(\"style\"", "innerHTML", "outerHTML", "insertAdjacentHTML",
                    "eval(", "new Function", "document.write", "javascript:"):
            self.assertNotIn(bad, self.JS, bad)
        self.assertNotRegex(self.JS, r"""\son[a-z]+=\\?["']""")
        self.assertNotRegex(self.CSS, r"@import|url\(")

    def test_every_post_carries_the_csrf_header(self):
        self.assertEqual(len(re.findall(r"\bfetch\(", self.JS)), 1)       # one fetch, inside api()
        self.assertIn("try { r = await fetch(path, opt); }", self.JS)
        self.assertIn('opt.headers["X-Media-Lab"] = "1"', self.JS)
        self.assertNotIn('method: "POST"', self.JS)                        # so every POST goes through api()
        self.assertGreaterEqual(len(re.findall(r'api\("POST"', self.JS)), 4)   # create, update, move, delete

    def test_public_api_and_endpoints(self):
        self.assertIn("window.MediaLabCalendar = {mount}", self.JS)
        for url in ('"/api/calendar"', '"/api/calendar/"', '"/api/calendar.ics"', '"/api/projects"',
                    '"/api/recent?limit=40"'):
            self.assertIn(url, self.JS)
        self.assertEqual(set(calendar_routes.STATIC), {"/static/calendar.js", "/static/calendar.css"})

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_script_parses(self):
        out = subprocess.run(["node", "--check", os.path.join(ROOT, "static", "calendar.js")], capture_output=True,
                             text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)


if __name__ == "__main__":
    unittest.main()
