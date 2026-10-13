"""WP4: the page (§10). Static checks always run; the fake-API checks run anywhere; the browser checks run only
where Python Playwright and its Chromium are installed (skipped otherwise, e.g. inside the media-lab image).

    python3 -m unittest tests.test_ui
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import unittest
import urllib.error
import urllib.request

from tests.ui_fake_server import FakeUI
from tests.util import ROOT


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


HTML, JS, CSS = _read("static", "index.html"), _read("static", "app.js"), _read("static", "style.css")
NOTICE = ("YourContent doesn't filter prompts. You're responsible for what you make: don't depict real people without "
          "their consent, never sexualize minors, and follow the law where you live. Some models are non-commercial; "
          "every file is labeled.")
FOOTER = "Only use a real person's likeness with their consent. You're responsible for what you make and share."
# The names are split so this file does not itself trip a repo-wide scan (same rule as test_portability.py).
_J = "".join
DENY = [_J(["/ho", "me/"]), "(?i)" + _J(["crea", "ture"]), "(?i)" + _J(["tailc", "39d0c"]), "(?i)" + _J(["sin", "-pi"]),
        "(?i)" + _J(["musicheard", "worldwide"]), r"100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.",
        "(?i)" + _J(["un", "censored"]), r"(?i)\b" + _J(["u", "c-"]), "(?i)" + _J(["tail", "net"])]


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s)


class StaticPageTest(unittest.TestCase):
    def test_no_inline_script_style_or_handlers(self):
        self.assertNotRegex(HTML, r"<script(?![^>]*\bsrc=)[^>]*>")
        self.assertNotIn("<style", HTML)
        self.assertNotRegex(HTML, r"\sstyle=")
        self.assertNotRegex(HTML, r"\son[a-z]+=")
        self.assertNotIn("javascript:", HTML + JS)
        for bad in ("style=", "setAttribute(\"style\"", "innerHTML", "outerHTML", "insertAdjacentHTML",
                    "eval(", "new Function", "document.write"):
            self.assertNotIn(bad, JS, bad)
        self.assertNotRegex(JS, r"""\son[a-z]+=\\?["']""")

    def test_only_the_fixed_static_files(self):
        srcs = re.findall(r"""(?:src|href)="(/[^"]*)\"""", HTML)
        self.assertEqual(sorted(set(s.partition("#")[0] for s in srcs)),
                         ["/static/app.js", "/static/favicon.svg", "/static/icons.svg", "/static/style.css",
                          "/static/theme.js", "/static/tokens.css"])
        self.assertNotRegex(CSS, r"@import|url\(")
        tokens = _read("static", "tokens.css")       # visual pass: the only url()s are the app's own fonts
        self.assertNotIn("@import", tokens)
        self.assertEqual(sorted(set(re.findall(r"url\(([^)]*)\)", tokens))),
                         ["/static/fonts/Inter_opsz_wght.ttf", "/static/fonts/JetBrainsMono_wght.ttf",
                          "/static/fonts/SpaceGrotesk_wght.ttf"])

    def test_sign_in_page_has_no_inline_code(self):
        page = _read("static", "login.html")
        self.assertNotRegex(page, r"<script(?![^>]*\bsrc=)[^>]*>")
        self.assertNotIn("<style", page)
        self.assertNotRegex(page, r"\s(style|on[a-z]+)=")
        js = _read("static", "login.js") + _read("static", "home.js") + _read("static", "studio.js")
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "new Function", "document.write"):
            self.assertNotIn(bad, js, bad)

    def test_header_is_just_media_lab(self):
        self.assertIn("<title>YourContent</title>", HTML)
        # the logo (brand/build.py) is a decorative inline SVG; the heading's text is just the name
        h1 = re.findall(r"<h1>(.*?)</h1>", HTML)
        self.assertEqual(len(h1), 1)
        self.assertEqual(re.sub(r'<svg class="brandmark"[^>]*aria-hidden="true">.*?</svg>', "", h1[0]), "YourContent")

    def test_deny_scan(self):
        for name, text in (("index.html", HTML), ("app.js", JS), ("style.css", CSS)):
            for pat in DENY:
                self.assertIsNone(re.search(pat, text), f"{name}: {pat}")

    def test_consent_notice_footer_and_about(self):
        page = _squash(HTML)
        self.assertEqual(page.count(NOTICE), 2)          # first-run bar and About
        self.assertIn(FOOTER, page)
        for row in ("PolyForm Small Business", "Non-commercial research", "Wan 2.2, lightx2v, umt5", "Apache-2.0",
                    "ACE-Step 1.5", "outputs commercial OK"):
            self.assertIn(row, page)
        self.assertIn('"ml-notice-1"', JS)

    def test_design_function_names_exist(self):
        for fn in ("api", "poll", "renderCut", "onDrag", "selectItem", "autosave", "preview", "exportCut",
                   "renderHealth"):
            self.assertRegex(JS, r"\bfunction %s\(" % fn, fn)

    def test_every_id_the_script_uses_exists(self):
        ids = set(re.findall(r'\bid="([^"]+)"', HTML))
        used = set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', JS))
        used |= {s for s in re.findall(r'(?:SLOT|BUTTON)\s*=\s*\{([^}]*)\}', JS) for s in re.findall(r'"([a-z]+)"', s)}
        self.assertEqual(sorted(used - ids), [])

    def test_every_post_carries_the_csrf_header(self):
        # every fetch() in the page either goes through api() / upload() or sets the header itself
        for m in re.finditer(r"fetch\(", JS):
            window = JS[m.start():m.start() + 400]
            if 'method: "POST"' in window or "method: method" in window or "opt)" in window:
                self.assertTrue('"X-Media-Lab": "1"' in window or "fetch(path, opt)" in window, window[:120])
        self.assertIn('opt.headers["X-Media-Lab"] = "1"', JS)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_script_parses(self):
        out = subprocess.run(["node", "--check", os.path.join(ROOT, "static", "app.js")], capture_output=True,
                             text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)


class LicenceTextTest(unittest.TestCase):
    """Review r2: the export row shows every licence warning, not only the non-commercial one."""

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_non_commercial_and_uploads_both_shown(self):
        src = JS[JS.index("function licenceText(L)"):]
        src = src[:src.index("\nfunction renderExport()")]
        cases = [
            {"commercial_ok": False, "most_restrictive": "qwen-image-nc", "uploads": 1},
            {"commercial_ok": False, "uploads": 1, "warnings": ["Contains Qwen-Image 2.1 output: non-commercial use only.",
                                                                 "Includes uploads: make sure you have the rights."]},
            {"commercial_ok": None, "uploads": 0},
            {"commercial_ok": True, "uploads": 0, "warnings": []},
        ]
        script = src + "\nconsole.log(JSON.stringify(" + json.dumps(cases) + ".map(licenceText)));"
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        got = json.loads(out.stdout)
        for t in got[:2]:
            self.assertTrue(t.startswith("⚠"), t)
            self.assertIn("non-commercial", t)
            self.assertIn("Includes uploads", t)
        self.assertIn("unknown license", got[2])
        self.assertEqual(got[3], "")


class FakeApiTest(unittest.TestCase):
    """The fake answers in §6 shapes, so the browser tests below exercise the real contract."""

    @classmethod
    def setUpClass(cls):
        cls.srv = FakeUI(job_seconds=0.05)
        cls.url = cls.srv.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def call(self, method, path, body=None, headers=None):
        hdr = {"X-Media-Lab": "1", "Content-Type": "application/json"} if method == "POST" else {}
        hdr.update(headers or {})
        data = json.dumps(body if body is not None else {}).encode() if method == "POST" else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=hdr)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw and r.headers.get_content_type() == "application/json" else raw), r.headers
        except urllib.error.HTTPError as e:
            raw = e.read()
            return e.code, json.loads(raw) if raw else None, e.headers

    def wait(self, jid):
        import time
        for _ in range(200):
            _, j, _ = self.call("GET", "/api/jobs/" + jid)
            if j["state"] not in ("queued", "running"):
                return j
            time.sleep(0.02)
        self.fail("job did not finish")

    def test_page_headers(self):
        code, _, hdr = self.call("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn("script-src 'self'", hdr["Content-Security-Policy"])
        self.assertEqual(hdr["X-Content-Type-Options"], "nosniff")

    def test_post_without_header_is_refused(self):
        code, j, _ = self.call("POST", "/api/projects", {}, headers={"X-Media-Lab": "0"})
        self.assertEqual(code, 403)
        self.assertIn("error", j)

    def test_three_step_export_contract(self):
        _, recent, _ = self.call("GET", "/api/recent?limit=60")
        first = recent[0]
        for k in ("ref", "type", "kind", "url", "poster", "label", "w", "h", "seconds", "created", "commercial_ok"):
            self.assertIn(k, first)
        code, j, _ = self.call("POST", "/api/projects", {"name": "Untitled cut"})
        self.assertEqual(code, 200)
        p = j["project"]
        self.assertEqual(p["rev"], 1)
        p["items"].append({"id": "i_abc123", "kind": "still", "src": recent[1]["ref"], "dur": 2.5, "join": "cut"})
        code, saved, _ = self.call("POST", f"/api/projects/{p['id']}", {"rev": 1, "project": p})
        self.assertEqual((code, saved["rev"]), (200, 2))
        self.assertEqual(saved["resolved"]["frames"], 60)
        code, stale, _ = self.call("POST", f"/api/projects/{p['id']}", {"rev": 1, "project": p})
        self.assertEqual(code, 409)
        self.assertEqual(stale["current"]["project"]["rev"], 2)
        _, j, _ = self.call("POST", f"/api/projects/{p['id']}/export", {"rev": 2})
        res = self.wait(j["job"])["result"]
        self.assertEqual(res["kind"], "export")
        self.assertIs(res["commercial_ok"], False)
        self.assertTrue(res["checks"]["ok"])
        _, recent2, _ = self.call("GET", "/api/recent")
        self.assertEqual(recent2[0]["ref"], res["ref"])

    def test_range_request(self):
        req = urllib.request.Request(self.url + "/m/out/qwen_studio_00001_.png", headers={"Range": "bytes=0-7"})
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertEqual(r.status, 206)
            self.assertEqual(r.read(), b"\x89PNG\r\n\x1a\n")


def _browser_ok() -> str | None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "playwright not installed"
    try:
        with sync_playwright() as p:
            p.chromium.launch().close()
    except Exception as exc:  # noqa: BLE001 - any launch failure means "skip"
        return f"chromium not available: {exc.__class__.__name__}"
    return None


_NO_BROWSER = _browser_ok()


@unittest.skipIf(_NO_BROWSER, _NO_BROWSER or "")
class BrowserTest(unittest.TestCase):
    """Headless Chromium against the fake API, with the real CSP in force (no bypass)."""

    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import expect, sync_playwright
        cls.expect = staticmethod(expect)
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.srv = FakeUI(job_seconds=0.3)
        self.url = self.srv.start()
        self.addCleanup(self.srv.stop)
        self.problems: list[str] = []
        self.dialogs: list[str] = []

    def page(self, width=1280, height=900, init=None, fresh=False):
        ctx = self.browser.new_context(viewport={"width": width, "height": height},
                                       is_mobile=width < 800, has_touch=width < 800)
        self.addCleanup(ctx.close)
        if not fresh:   # visual pass: the first-run welcome is a modal dialog; only the tests about it see it
            ctx.add_init_script("try{localStorage.setItem('ml-notice-1','1')}catch(e){}")
        if init:
            ctx.add_init_script(init)
        pg = ctx.new_page()
        pg.on("pageerror", lambda e: self.problems.append("pageerror: " + str(e)))
        pg.on("console", lambda m: m.type == "error" and self.problems.append("console: " + m.text))

        def on_dialog(d):
            self.dialogs.append(d.message)
            d.accept()
        pg.on("dialog", on_dialog)
        pg.goto(self.url)
        self.expect(pg.locator("#strip .thumb").first).to_be_visible()
        return pg

    def no_problems(self):
        self.assertEqual([p for p in self.problems if "Failed to load resource" not in p], [])

    def project(self):
        self.assertEqual(len(self.srv.projects), 1)
        return next(iter(self.srv.projects.values()))

    def saved(self, pg):
        self.expect(pg.locator("#psave")).to_have_text("Saved")

    def no_sideways_scroll(self, pg, where):
        sw, cw = pg.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
        self.assertLessEqual(sw, cw, f"sideways scroll at {where}: {sw} > {cw}")

    def three_click_export(self, pg):
        pg.locator("#strip .thumb img").nth(1).click()                 # 1. a Recent thumb
        pg.click("#tocut")                                              # 2. + Cut
        self.expect(pg.locator("#cutstrip .ccard[data-id]")).to_have_count(1)
        pg.click("#xexport")                                            # 3. Export
        self.expect(pg.locator("#xstatus")).to_contain_text("Exported", timeout=10_000)

    # ------------------------------------------------------------------ tests

    def test_loads_clean_under_csp(self):
        pg = self.page(fresh=True)
        self.expect(pg.locator("h1")).to_have_text("YourContent")
        self.expect(pg.locator("#notice")).to_be_visible()
        self.expect(pg.locator("footer")).to_have_text(FOOTER)
        self.expect(pg.locator("#dot")).to_have_class(re.compile(r"\bok\b"))
        self.assertEqual(pg.locator("#model option").count(), 3)
        self.assertTrue(pg.locator("#model option[value=bf16-lora]").is_disabled())
        self.expect(pg.locator("#model option[value=bf16-lora]")).to_have_text(re.compile(r"\(missing file\)"))
        self.expect(pg.locator("#strip .nc").first).to_be_visible()
        self.no_problems()

    def test_three_click_export_desktop(self):
        pg = self.page()
        self.three_click_export(pg)
        p = self.project()
        self.assertEqual(p["name"], "Untitled cut")
        exports = [r for r in self.srv.requests if r[0] == "POST" and r[1].endswith("/export")]
        self.assertEqual(len(exports), 1)
        self.assertEqual(exports[0][3], {"rev": p["rev"]})              # the export used the saved rev
        export = next(a for a in self.srv.assets.values() if a["kind"] == "export")
        self.expect(pg.locator("#stage video")).to_have_attribute("src", f"/m/lab/{export['name']}")
        self.expect(pg.locator("#dl")).to_have_attribute("href", f"/m/lab/{export['name']}?dl=1")
        self.expect(pg.locator("#strip .thumb").first).to_have_class(re.compile(r"\bon\b"))   # cut first in Recent
        self.expect(pg.locator("#xlic")).to_contain_text("non-commercial use only")
        self.assertTrue(all(r[2].get("X-Media-Lab") == "1" for r in self.srv.requests if r[0] == "POST"))
        self.no_problems()

    def test_phone_375_three_clicks_and_no_sideways_scroll(self):
        pg = self.page(375, 812)
        self.expect(pg.locator("#ptabs")).to_be_visible()
        self.expect(pg.locator(".ctabs")).to_be_hidden()
        self.no_sideways_scroll(pg, "picture tab")
        pg.click("#ptabs [data-tab=clip]")
        self.no_sideways_scroll(pg, "clip tab")
        self.three_click_export(pg)
        self.expect(pg.locator("#ptabs [data-tab=cut]")).to_have_class(re.compile(r"\bon\b"))
        # fill the cut, open every disclosure, then check again
        for i in (0, 2, 3):
            pg.locator("#strip .thumb img").nth(i).click()
            pg.click("#tocut")
        self.expect(pg.locator("#cutstrip .ccard[data-id]")).to_have_count(4)
        pg.select_option("#msel", "t:lab/up_20261007-210000_a1b2c3.mp3")
        self.expect(pg.locator("#madjust")).to_be_visible()
        pg.click("#madjust summary")
        pg.click("#xmore summary")
        pg.locator("#cutstrip .ccard[data-id]").first.click()
        self.expect(pg.locator("#editor")).to_be_visible()
        self.saved(pg)
        self.no_sideways_scroll(pg, "cut tab, full")
        strip = pg.evaluate("[document.getElementById('cutstrip').scrollWidth, document.getElementById('cutstrip').clientWidth]")
        self.assertGreater(strip[0], strip[1])                          # the strip scrolls inside its own row
        small = pg.evaluate("""[...document.querySelectorAll('button:not([hidden]), select, .ptabs button')]
            .filter(b => b.offsetParent && !b.closest('dialog, .ccard, .ref, .thumb') && b.getBoundingClientRect().height < 44)
            .map(b => b.id || b.textContent)""")
        self.assertEqual([s for s in small if s not in ("preload", "pretry", "mlongerbtn")], [])
        self.no_problems()

    def test_keyboard_focus_survives_strip_rebuilds(self):
        pg = self.page()
        thumb = pg.locator("#strip .thumb img").nth(2)
        thumb.focus()
        pg.keyboard.press("Enter")
        fk = thumb.get_attribute("data-fk")
        self.assertEqual(pg.evaluate("document.activeElement.dataset.fk"), fk)   # Recent was rebuilt, focus kept
        pg.click("#tocut")
        pg.locator("#strip .thumb img").nth(0).click()
        pg.click("#tocut")
        card = pg.locator("#cutstrip .ccard[data-id]").first
        card.focus()
        pg.keyboard.press("Enter")
        self.expect(pg.locator("#editor")).to_be_visible()
        self.assertEqual(pg.evaluate("document.activeElement.dataset.fk"), card.get_attribute("data-fk"))
        joint = pg.locator("#cutstrip .joint").first
        joint.focus()
        pg.keyboard.press("Enter")
        self.assertEqual(pg.evaluate("document.activeElement.className"), "joint fade")
        self.no_problems()

    def test_status_lines_are_live_regions(self):
        pg = self.page()
        for sid in ("status", "vstatus", "mstatus", "xstatus", "tstatus"):
            self.assertIn(pg.get_attribute("#" + sid, "role"), ("status", "alert"), sid)
        pg.fill("#prompt", "")
        pg.click("#go")
        self.expect(pg.locator("#status")).to_have_attribute("role", "alert")

    def test_phone_tap_shows_the_stage_and_hit_areas_are_44px(self):
        pg = self.page(375, 812)
        pg.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        pg.locator("#strip .thumb img").nth(1).click()
        for _ in range(40):   # smooth scroll; wait_for_function would need eval, which the CSP forbids
            top, vh = pg.evaluate("[document.getElementById('stage').getBoundingClientRect().top, innerHeight]")
            if -1 <= top < vh / 2:
                break
            pg.wait_for_timeout(50)
        self.assertTrue(-1 <= top < vh / 2, f"stage top {top} not on screen")
        hit = """(sel => { const b = document.querySelector(sel);
            const a = getComputedStyle(b, '::after'); const r = b.getBoundingClientRect();
            return [r.width - parseFloat(a.left) - parseFloat(a.right), r.height - parseFloat(a.top) - parseFloat(a.bottom)]; })"""
        w, hgt = pg.evaluate(hit + "('#strip .thumb button')")
        self.assertGreaterEqual(w, 44)
        self.assertGreaterEqual(hgt, 36)      # '+ ref' stops short of the 64 px thumb's centre, which opens it
        pg.locator("#strip .thumb button").first.click()             # + ref adds a reference
        self.expect(pg.locator(".ref button").first).to_be_attached()
        self.assertGreaterEqual(min(pg.evaluate(hit + "('.ref button')")), 44)
        self.assertNotEqual(pg.evaluate("getComputedStyle(document.getElementById('cutstrip')).touchAction"), "pan-x")
        self.no_problems()

    def test_cut_editing_saves(self):
        pg = self.page()
        for i in (1, 2, 3):                                             # clip, still, still
            pg.locator("#strip .thumb img").nth(i).click()
            pg.click("#tocut")
        self.expect(pg.locator("#cutstrip .ccard[data-id]")).to_have_count(3)
        self.saved(pg)
        ids = [i["id"] for i in self.project()["items"]]
        # joint toggle -> fade
        pg.locator("#cutstrip .joint").first.click()
        self.expect(pg.locator("#cutstrip .joint").first).to_have_text("◇")
        # trim the clip: select it, move In to frame 7 and Out to 40
        clip_id = next(i["id"] for i in self.project()["items"] if i["kind"] == "clip")
        pg.locator(f"#cutstrip .ccard[data-id={clip_id}]").click()
        self.expect(pg.locator("#eclip")).to_be_visible()
        pg.locator("#ein").fill("7")
        pg.locator("#eout").fill("40")
        self.expect(pg.locator("#ereadout")).to_have_text("0.44 s → 2.50 s (2.06 s)")
        # move it right with ▶
        pg.click("#eright")
        self.saved(pg)
        p = self.project()
        clip = next(i for i in p["items"] if i["id"] == clip_id)
        self.assertEqual((clip["in"], clip["out"]), (7, 40))
        self.assertEqual([i["id"] for i in p["items"]], [ids[1], ids[0], ids[2]])
        self.assertEqual(p["items"][1]["join"], "fade")
        # still length
        pg.locator("#cutstrip .ccard[data-id]").nth(2).click()
        pg.locator("#edur").fill("4")
        self.saved(pg)
        self.assertEqual(self.project()["items"][2]["dur"], 4)
        # export settings
        pg.click("#xmore summary")
        pg.select_option("#xfps", "30")
        pg.select_option("#xshape", "16:9")
        self.saved(pg)
        self.assertEqual(self.project()["export"]["fps"], 30)
        self.assertEqual(self.project()["export"]["preset"], "16:9")
        self.expect(pg.locator("#ptotal")).not_to_be_empty()
        self.no_problems()

    def test_mouse_drag_reorders(self):
        pg = self.page()
        for i in (1, 3):
            pg.locator("#strip .thumb img").nth(i).click()
            pg.click("#tocut")
        self.saved(pg)
        first, second = [i["id"] for i in self.project()["items"]]
        a = pg.locator(f"#cutstrip .ccard[data-id={first}]").bounding_box()
        b = pg.locator(f"#cutstrip .ccard[data-id={second}]").bounding_box()
        pg.mouse.move(a["x"] + 30, a["y"] + 30)
        pg.mouse.down()
        pg.mouse.move(a["x"] + 50, a["y"] + 30, steps=3)
        pg.mouse.move(b["x"] + b["width"] - 5, b["y"] + 30, steps=5)
        self.expect(pg.locator("#cutstrip .caret")).to_have_count(1)
        pg.mouse.up()
        self.saved(pg)
        self.assertEqual([i["id"] for i in self.project()["items"]], [second, first])

    def test_music_make_and_longer(self):
        pg = self.page()
        for i in (0, 1, 2, 3):
            pg.locator("#strip .thumb img").nth(i).click()
            pg.click("#tocut")
        self.saved(pg)
        pg.select_option("#msel", "make")
        self.expect(pg.locator("#mmake")).to_be_visible()
        self.expect(pg.locator("#msecs")).to_have_attribute("placeholder", re.compile(r"matches cut: \d+\.\d s"))
        pg.fill("#mtags", "warm lo-fi, mellow piano")
        pg.fill("#msecs", "5")
        pg.click("#mgo")
        self.expect(pg.locator("#mstatus")).to_contain_text("Music ready", timeout=10_000)
        self.saved(pg)
        music = self.project()["music"]
        self.assertEqual(music["src"]["root"], "gen")
        self.assertEqual(music["gen"]["tags"], "warm lo-fi, mellow piano")
        seed = music["gen"]["seed"]
        self.assertIsInstance(seed, int)
        self.expect(pg.locator("#mlonger")).to_be_visible()             # 5 s track, longer cut
        pg.click("#mlongerbtn")
        self.expect(pg.locator("#mstatus")).to_contain_text("Music ready", timeout=10_000)
        self.saved(pg)
        last = [r for r in self.srv.requests if r[1] == "/api/music"][-1][3]
        self.assertEqual((last["seed"], last["tags"]), (seed, "warm lo-fi, mellow piano"))
        self.assertNotIn("seconds", last)                              # the server matches the cut
        self.expect(pg.locator("#mlonger")).to_be_hidden()
        # volume
        pg.click("#madjust summary")
        pg.locator("#mvol").fill("80")
        self.saved(pg)
        self.assertAlmostEqual(self.project()["music"]["volume"], 0.8)
        self.no_problems()

    def test_conflict_offers_reload(self):
        pg = self.page()
        pg.locator("#strip .thumb img").nth(1).click()
        pg.click("#tocut")
        self.saved(pg)
        p = self.project()
        with self.srv.lock:
            p["rev"] += 1
            p["name"] = "Renamed elsewhere"
        pg.fill("#pname", "Mine")
        self.expect(pg.locator("#psave")).to_have_text("Conflict ·")
        self.expect(pg.locator("#preload")).to_be_visible()
        pg.click("#preload")
        self.expect(pg.locator("#pname")).to_have_value("Renamed elsewhere")
        self.expect(pg.locator("#psave")).to_have_text("Saved")
        self.no_problems()

    def test_delete_warns_about_cuts_and_shows_missing(self):
        pg = self.page()
        pg.locator("#strip .thumb img").nth(1).click()
        name = self.srv.recent()[1]["ref"]["name"]
        pg.click("#tocut")
        self.saved(pg)
        pg.click("#del")
        self.expect(pg.locator("#tstatus")).to_contain_text("Deleted")
        self.assertEqual(len(self.dialogs), 1)
        self.assertIn(f"Delete {name} forever? Used in 1 cut; they'll show it as missing.", self.dialogs[0])
        self.expect(pg.locator("#cutstrip .ccard.missing")).to_have_count(1)
        self.expect(pg.locator("#xwarn")).to_contain_text("missing")
        self.assertEqual([r[3] for r in self.srv.requests if r[1] == "/api/delete"],
                         [{"ref": {"root": self.srv_ref_root(name), "name": name}}])

    def srv_ref_root(self, name):
        return "gen" if name.startswith("img_") else "out"

    def test_generate_animate_and_preview(self):
        pg = self.page()
        pg.fill("#prompt", "a lighthouse at night")
        pg.click("#go")
        self.expect(pg.locator("#status")).to_contain_text("seed", timeout=10_000)
        gen = [r for r in self.srv.requests if r[1] == "/api/generate"][0][3]
        self.assertEqual(gen["variant"], "int8")
        self.assertEqual(gen["refs"], [])
        pg.click("#tovideo")
        self.expect(pg.locator("#pane-clip")).to_be_visible()
        pg.fill("#vprompt", "slow push in")
        pg.click("#vgo")
        self.expect(pg.locator("#vstatus")).to_contain_text("Done", timeout=10_000)
        anim = [r for r in self.srv.requests if r[1] == "/api/animate"][0][3]
        self.assertEqual(anim["start"]["root"], "gen")
        self.assertEqual((anim["w"], anim["h"]), (1472, 1104))
        pg.click("#tocut")
        self.saved(pg)
        pg.click("#xpreview")
        self.expect(pg.locator("#xstatus")).to_contain_text("Preview", timeout=10_000)
        self.expect(pg.locator("#stage video")).to_have_attribute("src", re.compile(r"^/m/cache/[0-9a-f]{64}\.mp4$"))
        self.no_problems()

    def test_notice_about_and_model_migration(self):
        pg = self.page(init="try{localStorage.setItem('qs-model','old-int8')}catch(e){}", fresh=True)
        self.assertEqual(pg.evaluate("localStorage.getItem('ml-model')"), "int8")
        self.expect(pg.locator("#model")).to_have_value("int8")
        self.expect(pg.locator("#wizstep")).to_have_text("Step 1 of 3")   # launch UI: a three-step wizard
        for _ in range(3):
            pg.click("#noticeok")
        self.expect(pg.locator("#notice")).to_be_hidden()
        pg.reload()
        self.expect(pg.locator("#strip .thumb").first).to_be_visible()
        self.expect(pg.locator("#notice")).to_be_hidden()
        pg.click("#aboutbtn")
        self.expect(pg.locator("#about")).to_be_visible()
        self.expect(pg.locator("#about")).to_contain_text("Non-commercial research")
        self.expect(pg.locator("#feat")).to_contain_text("Music: ready")
        pg.click("#aboutclose")
        self.no_problems()

    def test_comfy_off_and_music_missing(self):
        self.srv.comfy_ok = False
        pg = self.page()
        self.expect(pg.locator("#dot")).not_to_have_class(re.compile(r"\bok\b"))
        self.expect(pg.locator("#dot")).to_have_attribute("title", "ComfyUI not reachable; editing and export still work")
        pg.fill("#prompt", "x")
        pg.click("#go")
        self.expect(pg.locator("#status")).to_have_text("ComfyUI isn't reachable right now.")
        self.assertEqual([r for r in self.srv.requests if r[1] == "/api/generate"], [])
        self.three_click_export(pg)                                     # editing and export still work
        self.srv.comfy_ok, self.srv.music_ok = True, False
        pg.click("#dot")
        pg.click("#recheck")
        pg.click("#aboutclose")
        pg.select_option("#msel", "make")
        self.expect(pg.locator("#mgo")).to_be_disabled()
        self.expect(pg.locator("#mreason")).to_contain_text("qwen_0.6b_ace15.safetensors")

    def test_token_login(self):
        self.srv.token = "t0ken-for-tests"
        ctx = self.browser.new_context()
        self.addCleanup(ctx.close)
        pg = ctx.new_page()
        pg.goto(self.url)
        self.expect(pg).to_have_url(re.compile(r"/login\?next="))          # the sign-in page (launch UI)
        pg.fill("#token", "wrong")
        pg.click("#signin")
        self.expect(pg.locator("#loginstatus")).to_contain_text("isn't right")
        pg.fill("#token", "t0ken-for-tests")
        pg.click("#signin")
        self.expect(pg.locator("#strip .thumb").first).to_be_visible()
        self.assertFalse(any("t0ken" in r[1] for r in self.srv.requests))   # never in a URL


if __name__ == "__main__":
    unittest.main()
