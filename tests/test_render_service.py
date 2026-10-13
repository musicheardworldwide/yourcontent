"""The render service (render/): spec checks, data files, font fetcher and HTTP side always run; one real
Chromium render runs only where Python Playwright and its Chromium are installed (skipped otherwise)."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import os
import re
import struct
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout

from tests.util import ROOT, make_png

RENDER = os.path.join(ROOT, "render")


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(RENDER, file))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rs = _load("render_service", "server.py")
ff = _load("render_fetch_fonts", "fetch_fonts.py")

PNG = "data:image/png;base64," + base64.b64encode(make_png(w=6, h=8)).decode()
BRAND = {"name": "Test", "handle": "@test", "colors": {"bg": "#101010", "fg": "#fafafa", "accent": "#e8b14a"},
         "fonts": {"display": "Bebas Neue", "body": "Inter"}}


def spec(**over):
    s = {"style": "editorial", "brand": BRAND, "text": {"headline": "Hello"}}
    s.update(over)
    return s


class SpecTest(unittest.TestCase):
    def test_defaults(self):
        s = rs.build_spec(spec())
        self.assertEqual((s["ar"], s["width"], s["type"], s["palette"], s["slide"]), ("4/5", 1080, "png", "brand", 0))
        self.assertTrue(s["safe"] and s["mark"])
        self.assertEqual(s["copy"]["handle"], "@test")                  # the kit's handle when none is typed
        self.assertEqual(s["markText"], "Test")
        t = s["t"]
        self.assertEqual((t["ac"], t["ac2"], t["paper"], t["ink"]), ("#e8b14a", "#e8b14a", "#101010", "#fafafa"))
        self.assertEqual(t["disp"], "'Bebas Neue',sans-serif")
        self.assertEqual(t["serif"], t["disp"])                         # no serif given: the display face
        self.assertEqual(t["mono"], "'JetBrains Mono',monospace")
        self.assertRegex(t["mut"], r"^#[0-9a-f]{6}$")
        self.assertEqual(rs.build_spec(spec(format="1.91:1"))["width"], 1200)

    def test_refusals(self):
        bad = [
            ("unknown style", spec(style="nope")),
            ("no headline", spec(text={"headline": " "})),
            ("long headline", spec(text={"headline": "x" * 301})),
            ("text not text", spec(text={"headline": "x", "body": 3})),
            ("no brand", spec(brand=None)),
            ("bad hex", spec(brand=dict(BRAND, colors={"bg": "black", "fg": "#ffffff", "accent": "#ffffff"}))),
            ("css in a font", spec(brand=dict(BRAND, fonts={"display": "x';}body{x:'", "body": "Inter"}))),
            ("bad format", spec(format="3:2")),
            ("width", spec(width=4000)),
            ("width bool", spec(width=True)),
            ("type", spec(type="gif")),
            ("palette", spec(palette="neon")),
            ("slide on a one-slide style", spec(slide=2)),
            ("too many images", spec(images=[PNG] * 5)),
            ("not a data url", spec(images=["http://example.com/a.png"])),
            ("declared png, bytes jpeg", spec(images=["data:image/png;base64," + base64.b64encode(b"\xff\xd8\xff\xe0" + b"0" * 20).decode()])),
            ("quote in data url", spec(images=['data:image/png;base64,AAAA"onerror="x'])),
            ("svg", spec(images=["data:image/svg+xml;base64,PHN2Zz4="])),
            ("bad logo", spec(brand=dict(BRAND, logo="data:text/html;base64,PGI+"))),
            ("not an object", []),
        ]
        for what, body in bad:
            with self.subTest(what), self.assertRaises(rs.BadRequest):
                rs.build_spec(body)

    def test_carousel_slides_and_images(self):
        s = rs.build_spec(spec(style="carousel", slide=3, images=[PNG, PNG], brand=dict(BRAND, logo=PNG)))
        self.assertEqual((s["slide"], len(s["images"]), s["logo"]), (2, 2, PNG))


class DataFilesTest(unittest.TestCase):
    def test_35_styles_each_with_a_template(self):
        self.assertEqual(len(rs.STYLES), 35)
        with open(os.path.join(RENDER, "styles", "templates.js"), encoding="utf-8") as f:
            ids = re.findall(r'^"([a-z0-9-]+)": c=>', f.read(), re.M)
        self.assertEqual(sorted(ids), sorted(rs.STYLES))
        kinds = {s["kind"] for s in rs.STYLES.values()}
        self.assertEqual(kinds, {"text", "image"})
        for s in rs.STYLES.values():
            self.assertTrue({"id", "name", "kind", "tags", "fb", "mk", "own"} <= set(s), s["id"])
            for v in s["own"].values():
                self.assertRegex(v, r"^#[0-9a-f]{6}$")

    def test_style_versions(self):
        """Every style has its own version; editing one template changes only that style's (visual pass)."""
        v = rs.style_versions()
        self.assertEqual(set(v), set(rs.STYLES))
        self.assertEqual(len(set(v.values())), len(v))
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{12}", x) for x in v.values()))
        self.assertEqual({s["id"]: s["version"] for s in rs.styles_doc()["styles"]}, v)
        other = dict(rs.STYLES, swiss=dict(rs.STYLES["swiss"], tags="changed"))
        v2 = rs.style_versions(other)
        self.assertEqual([k for k in v if v[k] != v2[k]], ["swiss"])

    def test_fonts_manifest(self):
        m = rs.FONTS
        self.assertRegex(m["commit"], r"^[0-9a-f]{40}$")
        self.assertIn(m["commit"], m["base"])
        self.assertTrue(m["base"].startswith("https://"))
        fams = {e["family"] for e in m["fonts"]}
        self.assertEqual(fams, {e["family"] for e in m["licences"]})    # every family ships its licence text
        for e in m["fonts"] + m["licences"]:
            self.assertRegex(e["sha256"], r"^[0-9a-f]{64}$")
            self.assertIn(e["licence"], ("OFL-1.1", "Apache-2.0"))    # redistributable only
            ff.check_entry(e)
        for fam in rs.GENERIC:
            self.assertIn(fam, fams)
        with open(os.path.join(RENDER, "FONTS.md"), encoding="utf-8") as f:
            doc = f.read()
        for fam in fams:
            self.assertIn(f"| {fam} |", doc)

    def test_no_font_binaries_in_the_repo(self):
        for base, _, files in os.walk(RENDER):
            for n in files:
                self.assertFalse(n.lower().endswith((".ttf", ".otf", ".woff", ".woff2")), n)

    def test_assets_are_a_fixed_list(self):
        self.assertIsNotNone(rs.asset("/page.html"))
        self.assertIsNotNone(rs.asset("/styles/templates.js"))
        for p in ("/server.py", "/fonts/../server.py", "/fonts.json", "/styles/styles.json", "/etc/passwd", "/"):
            self.assertIsNone(rs.asset(p), p)


class FetchFontsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src = os.path.join(self.tmp.name, "src")
        os.makedirs(os.path.join(self.src, "licences"))
        self.data = {"A-Regular.ttf": b"font-a", "licences/a-OFL.txt": b"licence"}
        for rel, b in self.data.items():
            with open(os.path.join(self.src, rel), "wb") as f:
                f.write(b)

    def manifest(self, font_hash=None):
        sha = lambda b: hashlib.sha256(b).hexdigest()  # noqa: E731
        m = {"base": "https://example.invalid/", "fonts": [
                {"family": "A Sans", "file": "A-Regular.ttf", "path": "a/A-Regular.ttf", "weight": "100 900",
                 "style": "normal", "sha256": font_hash or sha(b"font-a"), "licence": "OFL-1.1"}],
             "licences": [{"family": "A Sans", "file": "a-OFL.txt", "path": "a/OFL.txt", "sha256": sha(b"licence"),
                           "licence": "OFL-1.1"}]}
        p = os.path.join(self.tmp.name, "m.json")
        with open(p, "w") as f:
            json.dump(m, f)
        return p

    def run_it(self, manifest):
        out = os.path.join(self.tmp.name, "out")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            code = ff.main(["--out", out, "--from", self.src, "--manifest", manifest])
        return code, out, err.getvalue()

    def test_verified_copy_and_css(self):
        code, out, _ = self.run_it(self.manifest())
        self.assertEqual(code, 0)
        with open(os.path.join(out, "fonts.css")) as f:
            css = f.read()
        self.assertIn("@font-face{font-family:'A Sans';font-style:normal;font-weight:100 900;", css)
        self.assertIn("src:url(/fonts/A-Regular.ttf)", css)
        self.assertTrue(os.path.isfile(os.path.join(out, "licences", "a-OFL.txt")))

    def test_hash_mismatch_fails(self):
        code, out, err = self.run_it(self.manifest(font_hash="0" * 64))
        self.assertEqual(code, 1)
        self.assertIn("does not match", err)
        self.assertFalse(os.path.exists(os.path.join(out, "fonts.css")))
        self.assertFalse(os.path.exists(os.path.join(out, "A-Regular.ttf")))

    def test_family_filter(self):
        """--family picks the app's own UI fonts out of the same manifest (visual pass); an unknown name fails."""
        out = os.path.join(self.tmp.name, "out")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            self.assertEqual(ff.main(["--out", out, "--from", self.src, "--manifest", self.manifest(),
                                      "--family", "A Sans"]), 0)
            self.assertEqual(ff.main(["--out", out, "--from", self.src, "--manifest", self.manifest(),
                                      "--family", "B Serif"]), 1)
        self.assertTrue(os.path.isfile(os.path.join(out, "A-Regular.ttf")))
        self.assertIn("no such family", err.getvalue())

    def test_hostile_names_are_refused(self):
        for e in ({"file": "../x.ttf", "sha256": "0" * 64}, {"file": "a.ttf", "sha256": "0" * 64, "family": "x'}"}):
            with self.assertRaises(ValueError):
                ff.check_entry(e)


class HTTPTest(unittest.TestCase):
    """The HTTP side without a browser: every request here is refused before a render starts."""

    @classmethod
    def setUpClass(cls):
        cls.srv = rs.Server(("127.0.0.1", 0), rs.Handler)
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def call(self, method, path, body=None, ctype="application/json"):
        data = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Content-Type": ctype} if data is not None else {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_get(self):
        st, j = self.call("GET", "/styles")
        self.assertEqual(st, 200)
        self.assertEqual(len(j["styles"]), 35)
        self.assertIn("Inter", j["fonts"])
        self.assertEqual(j["formats"], ["1:1", "4:5", "9:16", "1.91:1"])
        st, j = self.call("GET", "/healthz")
        self.assertEqual((st, j["ok"], j["styles"]), (200, True, 35))
        self.assertEqual(self.call("GET", "/nope")[0], 404)
        self.assertEqual(self.call("GET", "/render")[0], 405)

    def test_post_refusals(self):
        self.assertEqual(self.call("POST", "/render", spec(style="nope"))[0], 400)
        self.assertEqual(self.call("POST", "/render", b"{not json")[0], 400)
        self.assertEqual(self.call("POST", "/render", b"x=1", ctype="application/x-www-form-urlencoded")[0], 415)
        self.assertEqual(self.call("POST", "/elsewhere", spec())[0], 404)


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


def _png_size(data: bytes) -> tuple[int, int]:
    return struct.unpack(">II", data[16:24])


@unittest.skipIf(_NO_BROWSER, _NO_BROWSER or "")
class RealRenderTest(unittest.TestCase):
    """One real Chromium render per output type (fonts fall back to system faces unless RENDER_FONT_DIR is set)."""

    def test_png_and_jpeg(self):
        img, meta = rs.RENDERER.render(rs.build_spec(spec(style="scrim", images=[PNG], format="4:5", width=540)))
        self.assertEqual(img[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(_png_size(img), (540, 675))
        self.assertRegex(meta["ver"], r"^[0-9a-f]{8}$")
        img, meta = rs.RENDERER.render(rs.build_spec(spec(style="poster", type="jpg", format="9:16", width=360)))
        self.assertEqual(img[:3], b"\xff\xd8\xff")
        self.assertEqual((meta["w"], meta["h"]), (360, 640))


if __name__ == "__main__":
    unittest.main()
