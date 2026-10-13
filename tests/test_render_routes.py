"""Posts extension (render_routes + render_client + brand kits) through the real HTTP server, against an in-process
fake render service (like FakeComfy). No network beyond 127.0.0.1; no Playwright needed."""
from __future__ import annotations

import base64
import http.client
import json
import os
import re
import shutil
import subprocess
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from medialab import render_client, render_routes, server, store
from tests.util import ROOT, TempRoots, make_png

STYLES = {"styles": [{"id": "editorial", "name": "Editorial Serif", "kind": "text", "tags": "calm", "slides": 1,
                      "version": "e1"},
                     {"id": "scrim", "name": "Full-Bleed + Scrim", "kind": "image", "tags": "clean", "slides": 1,
                      "version": "s1"}],
          "fonts": ["Inter", "Space Grotesk"], "formats": ["1:1", "4:5", "9:16", "1.91:1"]}


class FakeRender:
    """POST /render answers a small PNG (or the error set in .fail); GET /styles answers STYLES."""

    def __init__(self):
        self.calls: list[dict] = []
        self.fail: tuple[int, str] | None = None
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype, extra=None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/styles":
                    return self._send(200, json.dumps(STYLES).encode(), "application/json")
                self._send(404, b"{}", "application/json")

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.calls.append(body)
                if fake.fail:
                    return self._send(fake.fail[0], json.dumps({"error": fake.fail[1]}).encode(), "application/json")
                png = make_png(w=8, h=10, rgb=(10, 200, 30))
                info = {"style": body["style"], "version": "abcd1234", "w": 1080, "h": 1350, "format": body["format"]}
                self._send(200, png, "image/png", {"X-Render-Info": json.dumps(info)})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def install_once():
    if "/static/posts.js" not in server.STATIC:
        server.install_extension(render_routes)


class PostsCase(unittest.TestCase):
    def setUp(self):
        install_once()
        self.r = TempRoots()
        self.addCleanup(self.r.cleanup)
        self.fake = FakeRender()
        self.addCleanup(self.fake.close)
        env = mock.patch.dict(os.environ, {"MEDIA_LAB_RENDER_URL": self.fake.url})
        env.start()
        self.addCleanup(env.stop)
        render_client._cache.clear()
        self.addCleanup(render_client._cache.clear)
        self.srv = server.make_server(self.r.config(), adopt=False)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def req(self, method, path, body=None, csrf=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": f"127.0.0.1:{self.port}"}
        data = None
        if method == "POST":
            if csrf:
                h["X-Media-Lab"] = "1"
            data = json.dumps(body if body is not None else {}).encode()
            h["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=h)
        r = conn.getresponse()
        out, hdrs = r.read(), {k.lower(): v for k, v in r.getheaders()}
        conn.close()
        j = json.loads(out) if hdrs.get("content-type", "").startswith("application/json") else None
        return r.status, j, out, hdrs

    def picture(self, name, ok, licences, root="gen", kind="image"):
        """An owned picture with a given licence aggregate."""
        make_png(self.r.path(root, name), w=4, h=4)
        store.put_sidecar((root, name), {"schema": "media-lab/asset@1", "kind": kind, "created": store.now_iso(),
                                         "commercial_ok": ok, "licences": licences,
                                         "most_restrictive": licences[0] if licences else None})
        return {"root": root, "name": name}

    def post(self, **over):
        body = {"kit": "paper-studio", "style": "editorial", "text": {"headline": "Hello", "kicker": "K"},
                "format": "4:5"}
        body.update(over)
        return self.req("POST", "/api/posts/render", body)


class StylesAndHealthTest(PostsCase):
    def test_styles_from_the_service(self):
        st, j, _, _ = self.req("GET", "/api/styles")
        self.assertEqual(st, 200)
        self.assertEqual([s["id"] for s in j["styles"]], ["editorial", "scrim"])
        self.assertEqual(j["render"], {"ok": True, "missing": []})
        st, j, _, _ = self.req("GET", "/api/posts/health")
        self.assertEqual(j, {"ok": True, "styles": 2, "missing": []})

    def test_service_off_is_a_reason_not_a_crash(self):
        self.fake.close()
        render_client._cache.clear()
        st, j, _, _ = self.req("GET", "/api/styles")
        self.assertEqual(st, 200)
        self.assertEqual(len(j["styles"]), 35)                        # listed from styles.json, drawn as swatches
        self.assertEqual(j["render"], {"ok": False, "missing": ["the render service is not reachable"]})
        st, j, _, _ = self.req("GET", "/api/posts/health")
        self.assertEqual((st, j["ok"], j["missing"]), (200, False, ["the render service is not reachable"]))
        st, j, _, _ = self.post()
        self.assertEqual(st, 503)
        self.assertIn("not reachable", j["error"])
        self.assertEqual(os.listdir(self.r.dirs["lab"]), [])

    def test_bad_render_url_is_refused(self):
        with mock.patch.dict(os.environ, {"MEDIA_LAB_RENDER_URL": "file:///etc/passwd"}):
            render_client._cache.clear()
            st, j, _, _ = self.req("GET", "/api/styles")
        self.assertEqual(j["render"]["ok"], False)
        self.assertEqual(self.fake.calls, [])

    def test_static_files_are_served(self):
        for path, ctype in (("/static/posts.js", "text/javascript"), ("/static/posts.css", "text/css")):
            st, _, body, h = self.req("GET", path)
            self.assertEqual(st, 200)
            self.assertTrue(h["content-type"].startswith(ctype))
            self.assertIn("content-security-policy", h)


class BrandKitRoutesTest(PostsCase):
    def test_list_create_update_delete(self):
        st, j, _, _ = self.req("GET", "/api/brandkits")
        self.assertEqual(st, 200)
        self.assertEqual(len(j["kits"]), 3)
        new = {"name": "Corner Shop", "colors": {"bg": "#ffffff", "fg": "#111111", "accent": "#0055ff"},
               "fonts": {"display": "Inter", "body": "Inter"}}
        self.assertEqual(self.req("POST", "/api/brandkits", new, csrf=False)[0], 403)
        st, j, _, _ = self.req("POST", "/api/brandkits", new)
        self.assertEqual((st, j["id"]), (200, "corner-shop"))
        self.assertEqual(self.req("POST", "/api/brandkits", new)[0], 409)
        st, j, _, _ = self.req("POST", "/api/brandkits/corner-shop", dict(new, handle="@corner"))
        self.assertEqual((st, j["handle"]), (200, "@corner"))
        self.assertEqual(self.req("POST", "/api/brandkits/nope", new)[0], 404)
        st, j, _, _ = self.req("POST", "/api/brandkits", dict(new, name="X", colors={"bg": "red"}))
        self.assertEqual(st, 400)
        self.assertIn("colors", j["error"])
        self.assertEqual(self.req("POST", "/api/brandkits/corner-shop/delete")[0], 200)
        self.assertEqual(self.req("POST", "/api/brandkits/corner-shop/delete")[0], 404)

    def test_logo_must_be_an_owned_picture(self):
        new = {"name": "Logo Kit", "colors": {"bg": "#ffffff", "fg": "#111111", "accent": "#0055ff"},
               "fonts": {"display": "Inter", "body": "Inter"}, "logo": {"root": "in", "name": "nope.png"}}
        st, j, _, _ = self.req("POST", "/api/brandkits", new)
        self.assertEqual(st, 400)
        new["logo"] = self.picture("logo.png", True, ["apache-2.0"])
        self.assertEqual(self.req("POST", "/api/brandkits", new)[0], 200)


class RenderRouteTest(PostsCase):
    def test_text_only_post_is_saved_commercial_ok(self):
        st, j, _, _ = self.post(caption="Out now.", palette="invert", width=1080)
        self.assertEqual(st, 200, j)
        sent = self.fake.calls[-1]
        self.assertEqual(sent["style"], "editorial")
        self.assertEqual(sent["images"], [])
        self.assertEqual(sent["brand"]["colors"]["accent"], "#b5452b")       # from the example kit
        self.assertEqual(sent["brand"]["handle"], "paperstudio.example")
        self.assertEqual((sent["palette"], sent["width"]), ("invert", 1080))
        self.assertNotIn("logo", sent["brand"])
        self.assertTrue(j["commercial_ok"])
        ref = j["item"]["ref"]
        self.assertEqual(ref["root"], "lab")
        self.assertRegex(ref["name"], r"^post_\d{8}-\d{6}_[0-9a-f]{6}\.png$")
        self.assertTrue(os.path.isfile(self.r.path("lab", ref["name"])))
        sc = store.sidecar(("lab", ref["name"]))
        self.assertEqual((sc["kind"], sc["inputs"], sc["commercial_ok"]), ("post", [], True))
        self.assertEqual(sc["params"]["caption"], "Out now.")
        self.assertEqual(sc["render"]["style_version"], "abcd1234")
        self.assertEqual((sc["probe"]["w"], sc["probe"]["h"]), (1080, 1350))
        recent = self.req("GET", "/api/recent?kind=image")[1]
        self.assertIn(ref, [r["ref"] for r in recent])

    def test_a_post_inherits_its_pictures_licence(self):
        nc = self.picture("nc.png", False, ["qwen-image-nc"])
        st, j, _, _ = self.post(style="scrim", image=nc)
        self.assertEqual(st, 200, j)
        self.assertIs(j["commercial_ok"], False)
        self.assertTrue(any("nc.png" in r for r in j["restricted_by"]))
        img = self.fake.calls[-1]["images"][0]
        self.assertTrue(img.startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(img.split(",", 1)[1])[:8], b"\x89PNG\r\n\x1a\n")
        sc = store.sidecar(("lab", j["item"]["ref"]["name"]))
        self.assertEqual(sc["inputs"][0]["name"], "nc.png")
        self.assertEqual(sc["most_restrictive"], "qwen-image-nc")

    def test_an_unknown_licence_makes_the_post_unknown(self):
        up = self.picture("ref_up.png", None, ["upload"], root="in")
        ok = self.picture("ok.png", True, ["apache-2.0"])
        st, j, _, _ = self.post(style="scrim", image=up, images=[ok, up])
        self.assertEqual(st, 200, j)
        self.assertIsNone(j["commercial_ok"])
        self.assertEqual(len(self.fake.calls[-1]["images"]), 2)        # the repeated picture is sent once
        self.assertTrue(any("rights" in w for w in j["warnings"]))

    def test_the_kits_logo_is_an_input_too(self):
        logo = self.picture("logo.png", False, ["qwen-image-nc"])
        kit = next(k for k in self.req("GET", "/api/brandkits")[1]["kits"] if k["id"] == "paper-studio")
        self.assertEqual(self.req("POST", "/api/brandkits/paper-studio", dict(kit, logo=logo))[0], 200)
        st, j, _, _ = self.post()
        self.assertTrue(self.fake.calls[-1]["brand"]["logo"].startswith("data:image/png;base64,"))
        self.assertIs(j["commercial_ok"], False)
        store.remove(("gen", "logo.png"))
        st, j, _, _ = self.post()
        self.assertEqual(st, 200, j)
        self.assertNotIn("logo", self.fake.calls[-1]["brand"])
        self.assertTrue(j["commercial_ok"])
        self.assertTrue(any("logo is gone" in w for w in j["warnings"]))

    def test_preview_is_not_saved(self):
        st, j, body, h = self.post(preview=True)
        self.assertEqual(st, 200)
        self.assertEqual(h["content-type"], "image/png")
        self.assertEqual(body[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(json.loads(h["x-render-info"])["version"], "abcd1234")
        self.assertEqual(self.fake.calls[-1]["width"], 540)
        self.assertEqual(os.listdir(self.r.dirs["lab"]), [])

    def test_thumbnails_are_cached_on_disk_by_style_version(self):
        n = len(self.fake.calls)
        st, _, body, h = self.post(preview=True, width=320)
        self.assertEqual((st, h["x-thumb-cache"], len(self.fake.calls)), (200, "miss", n + 1))
        st, _, again, h = self.post(preview=True, width=320)
        self.assertEqual((st, h["x-thumb-cache"], len(self.fake.calls), again), (200, "hit", n + 1, body))
        self.assertEqual(self.post(preview=True, width=320, text={"headline": "Other"})[3]["x-thumb-cache"], "miss")
        self.assertEqual(self.post(preview=True, width=540)[3]["x-thumb-cache"], "off")    # the large preview
        STYLES["styles"][0]["version"] = "e2"                       # the template changed: drawn again
        self.addCleanup(STYLES["styles"][0].__setitem__, "version", "e1")
        render_client._cache.clear()
        self.assertEqual(self.post(preview=True, width=320)[3]["x-thumb-cache"], "miss")
        pic = self.picture("p.png", True, ["apache-2.0"])            # a picture's identity is in the key too
        self.assertEqual(self.post(style="scrim", image=pic, preview=True, width=320)[3]["x-thumb-cache"], "miss")
        self.assertEqual(self.post(style="scrim", image=pic, preview=True, width=320)[3]["x-thumb-cache"], "hit")
        make_png(self.r.path("gen", "p.png"), w=6, h=6)
        os.utime(self.r.path("gen", "p.png"), ns=(1, 1))
        self.assertEqual(self.post(style="scrim", image=pic, preview=True, width=320)[3]["x-thumb-cache"], "miss")
        self.assertTrue(os.listdir(store.data_path("cache", "thumbs")))

    def test_style_thumbnail_endpoint_is_cached_by_style_kit_format_palette(self):
        q = "/api/posts/thumb?kit=paper-studio&style=editorial&format=4:5&palette=brand&v=x"
        n = len(self.fake.calls)
        st, _, body, h = self.req("GET", q)
        self.assertEqual((st, h["content-type"], h["x-thumb-cache"]), (200, "image/png", "miss"))
        self.assertEqual(body[:8], b"\x89PNG\r\n\x1a\n")
        self.assertIn("max-age=86400", h["cache-control"])
        call = self.fake.calls[-1]
        self.assertEqual((call["width"], call["format"], call["palette"], call["images"]), (320, "4:5", "brand", []))
        self.assertEqual(call["text"]["headline"], "Your headline goes here")       # sample words, never the user's
        st, _, again, h = self.req("GET", q.replace("v=x", "v=y"))                    # v= is only for the browser
        self.assertEqual((st, h["x-thumb-cache"], again, len(self.fake.calls)), (200, "hit", body, n + 1))
        self.assertEqual(self.req("GET", q.replace("4:5", "9:16"))[3]["x-thumb-cache"], "miss")
        self.assertEqual(self.req("GET", q.replace("brand", "own"))[3]["x-thumb-cache"], "miss")
        self.assertEqual(self.req("GET", q.replace("editorial", "scrim"))[3]["x-thumb-cache"], "miss")
        self.assertEqual(self.req("GET", q.replace("paper-studio", "market-day"))[3]["x-thumb-cache"], "miss")

    def test_style_thumbnail_refusals_and_service_off(self):
        base = "/api/posts/thumb?kit=paper-studio&style=editorial"
        self.assertEqual(self.req("GET", base.replace("paper-studio", "nope"))[0], 404)
        self.assertEqual(self.req("GET", base.replace("editorial", "<x>"))[0], 400)
        self.assertEqual(self.req("GET", base + "&format=2:1")[0], 400)
        self.assertEqual(self.req("GET", base + "&palette=neon")[0], 400)
        self.assertEqual(self.fake.calls, [])
        with mock.patch.dict(os.environ, {"MEDIA_LAB_RENDER_URL": "http://127.0.0.1:9"}):
            render_client._cache.clear()
            self.assertEqual(self.req("GET", base)[0], 503)

    def test_styles_carry_their_own_colours_and_list_locally_when_the_service_is_off(self):
        st, j, _, _ = self.req("GET", "/api/styles")
        ed = next(s for s in j["styles"] if s["id"] == "editorial")
        self.assertRegex(ed["own"]["bg"], r"^#[0-9a-f]{6}$")                         # from render/styles/styles.json
        self.assertEqual(ed["version"], "e1")                                         # the service's fields win
        with mock.patch.dict(os.environ, {"MEDIA_LAB_RENDER_URL": "http://127.0.0.1:9"}):
            render_client._cache.clear()
            st, j, _, _ = self.req("GET", "/api/styles")
        self.assertFalse(j["render"]["ok"])
        self.assertEqual(len(j["styles"]), 35)                                        # swatch cards still show
        self.assertEqual(render_routes.local_styles("/nonexistent/styles.json"), [])

    def test_refusals(self):
        self.assertEqual(self.post(kit="nope")[0], 404)
        self.assertEqual(self.post(style="<x>")[0], 400)
        self.assertEqual(self.post(text={"headline": "  "})[0], 400)
        self.assertEqual(self.post(text={"headline": "x" * 301})[0], 400)
        self.assertEqual(self.post(image={"root": "gen", "name": "missing.png"})[0], 404)
        self.assertEqual(self.post(image={"root": "gen", "name": "../x.png"})[0], 400)
        clip = self.r.path("lab", "clip.mp4")
        with open(clip, "wb") as f:
            f.write(b"\x00\x00\x00\x18ftypisom")
        store.put_sidecar(("lab", "clip.mp4"), {"kind": "upload", "commercial_ok": None})
        self.assertEqual(self.post(image={"root": "lab", "name": "clip.mp4"})[0], 400)
        self.assertEqual(self.post(images=[self.picture(f"p{i}.png", True, []) for i in range(5)])[0], 400)
        self.assertEqual(self.req("POST", "/api/posts/render", {"kit": "paper-studio"}, csrf=False)[0], 403)
        self.assertEqual(self.fake.calls, [])

    def test_service_errors_pass_through(self):
        self.fake.fail = (400, "unknown style; GET /styles lists them")
        st, j, _, _ = self.post()
        self.assertEqual((st, j["error"]), (400, "unknown style; GET /styles lists them"))
        self.fake.fail = (500, "render failed: boom")
        st, j, _, _ = self.post()
        self.assertEqual(st, 502)
        self.assertEqual(os.listdir(self.r.dirs["lab"]), [])


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
class PostsBrowserTest(PostsCase):
    """posts.js mounted into the real page, with the real CSP, at phone width, against the fake render service."""

    def test_phone_preview_render_and_kit_editor(self):
        from playwright.sync_api import expect, sync_playwright
        self.picture("pic.png", True, ["apache-2.0"])
        with sync_playwright() as p:
            b = p.chromium.launch()
            try:
                self._drive(b, expect)
            finally:
                b.close()

    def _drive(self, b, expect):
        pg = b.new_page(viewport={"width": 375, "height": 800})
        pg.add_init_script("try{localStorage.setItem('ml-notice-1','1')}catch(e){}")   # no first-run welcome
        problems = []
        pg.on("console", lambda m: problems.append(m.text) if "Content Security Policy" in m.text else None)
        pg.on("pageerror", lambda e: problems.append(str(e)))
        pg.goto(f"http://127.0.0.1:{self.port}/#make")                 # the page opens on Posts otherwise
        pg.add_style_tag(url="/static/posts.css")
        pg.add_script_tag(url="/static/posts.js")
        pg.evaluate("() => { const d = document.createElement('div'); d.id = 'pp'; document.body.prepend(d);"
                    " window.MediaLabPosts.mount(d); }")
        # composer (2026-10-12): kit chips, a gallery of style cards (lazy <img> from /api/posts/thumb over a swatch),
        # a live preview beside (or, on a phone, after) the fields, Preview scrolls to it, Render and save under it
        expect(pg.locator(".mlp-kits .chip")).to_have_count(3)
        expect(pg.locator(".mlp-pic")).to_have_count(2)                # "No picture" + pic.png
        expect(pg.locator(".mlp-card")).to_have_count(2)                # one card per style
        expect(pg.locator(".mlp-card .mlp-swatch")).to_have_count(2)
        expect(pg.locator(".mlp-card .mlp-shot").first).to_have_attribute("loading", "lazy")
        expect(pg.locator(".mlp-card .mlp-shot").first).to_have_attribute("src", re.compile(r"^/api/posts/thumb\?kit="))
        expect(pg.locator(".mlp-card[aria-pressed=true]")).to_have_count(1)
        expect(pg.locator(".mlp-pimg")).to_have_attribute("src", re.compile(r"^blob:"))    # a live sample at once
        live = [c for c in self.fake.calls if c.get("width") == 540][-1]
        self.assertEqual(live["text"]["headline"], "Your headline goes here")              # sample words until you type
        pg.locator('.mlp-card[data-style="editorial"]').focus()                            # arrows move between cards
        pg.keyboard.press("ArrowRight")
        self.assertEqual(pg.evaluate("document.activeElement.dataset.style"), "scrim")
        pg.keyboard.press("Enter")
        expect(pg.locator('.mlp-card[data-style="scrim"]')).to_have_attribute("aria-pressed", "true")
        pg.get_by_label("Search styles").fill("zzz")
        expect(pg.locator(".mlp-gallery")).to_contain_text("No styles match.")
        pg.get_by_role("button", name="Clear filters").click()
        expect(pg.locator(".mlp-card")).to_have_count(2)
        pg.locator(".mlp-pic").nth(1).click()
        pg.get_by_label("Headline", exact=True).fill("Fresh today")
        pg.get_by_role("button", name="Preview", exact=True).click()
        for _ in range(60):
            if any(c.get("width") == 540 and c["text"]["headline"] == "Fresh today" for c in self.fake.calls):
                break
            pg.wait_for_timeout(50)
        else:
            self.fail("the preview did not re-draw")
        top = pg.evaluate("document.querySelector('.mlp-preview').getBoundingClientRect().top")
        self.assertTrue(-1 <= top < 800, top)                          # Preview brought it into view (audit P1)
        big = [c for c in self.fake.calls if c.get("width") == 540][-1]
        self.assertEqual((big["style"], big["text"]["headline"], len(big["images"])), ("scrim", "Fresh today", 1))
        pg.get_by_role("button", name="Render and save").click()
        expect(pg.locator(".mlp-result")).to_contain_text("Saved as post_")
        expect(pg.locator(".mlp-result")).to_contain_text("Commercial use OK.")
        expect(pg.locator(".mlp-result").get_by_role("link", name="Download")).to_be_visible()
        self.assertLessEqual(pg.evaluate("document.documentElement.scrollWidth"), 375)
        pg.get_by_role("button", name="New kit").click()
        pg.locator(".mlp-kitform").get_by_label("Name").fill("Phone Kit")
        pg.get_by_role("button", name="Create kit").click()
        expect(pg.locator(".mlp-kits .chip")).to_have_count(4)
        self.assertLessEqual(pg.evaluate("document.documentElement.scrollWidth"), 375)
        pg.set_viewport_size({"width": 1280, "height": 800})          # wide: the preview sticks beside the editor
        pg.evaluate("window.scrollTo(0, 900)")
        top = pg.evaluate("document.querySelector('.mlp-preview').getBoundingClientRect().top")
        self.assertAlmostEqual(top, 16, delta=1)
        self.assertEqual(problems, [])


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


class PostsPageTest(unittest.TestCase):
    """posts.js keeps the page's CSP rules: no inline handlers or styles, no HTML strings, CSRF header on POST."""

    JS, CSS = _read("static", "posts.js"), _read("static", "posts.css")

    def test_csp_safe(self):
        for bad in ("style=", "setAttribute(\"style\"", ".style.", "innerHTML", "outerHTML", "insertAdjacentHTML",
                    "eval(", "new Function", "document.write", "javascript:"):
            self.assertFalse(bad in self.JS, bad)
        self.assertNotRegex(self.CSS, r"@import|url\(")

    def test_every_post_sends_the_header(self):
        self.assertEqual(len(re.findall(r"\bfetch\(", self.JS)), 1)      # one fetch, inside api()
        self.assertIn('opt.headers["X-Media-Lab"] = "1"', self.JS)
        self.assertIn("window.MediaLabPosts = {mount}", self.JS)

    def test_phone_width(self):
        self.assertIn("@media (max-width:760px)", self.CSS)
        self.assertIn("@media (max-width:900px)", self.CSS)            # one column: the preview after the fields
        self.assertIn(".mlp-preview{position:sticky;top:16px", self.CSS)
        for w in re.findall(r"(?<![-\w])(?:width|min-width):\s*(\d+)px", self.CSS):
            self.assertLessEqual(int(w), 343, "a fixed width wider than a 375px screen minus gutters")

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_script_parses(self):
        out = subprocess.run(["node", "--check", os.path.join(ROOT, "static", "posts.js")], capture_output=True,
                             text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)


if __name__ == "__main__":
    unittest.main()
