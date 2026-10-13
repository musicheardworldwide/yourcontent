"""Visual pass (2026-10-11): every screen of the real page, with every feature module, fits the window: no sideways
page scroll at 320, 390, 1024, 1180 and 1440 px, in the light and the dark theme. Runs the real server against the
fake render service from test_render_routes; skipped where Python Playwright and its Chromium are not installed,
like the other browser tests.

    python3 -m unittest tests.test_ui_layout
"""
from __future__ import annotations

import os
import threading
import unittest
from unittest import mock

from medialab import render_client, server
from tests.test_render_routes import FakeRender, install_once
from tests.test_ui import _NO_BROWSER
from tests.util import TempRoots

WIDTHS = (320, 390, 1024, 1180, 1440)
VIEWS = ("home", "posts", "make", "calendar", "library", "connect", "settings")
SEEN = "try{localStorage.setItem('ml-notice-1','1');localStorage.setItem('ml-tour-1','1')}catch(e){}"


@unittest.skipIf(_NO_BROWSER, _NO_BROWSER or "")
class NoSidewaysScrollTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        install_once()
        cls.r = TempRoots()
        cls.fake = FakeRender()
        cls.env = mock.patch.dict(os.environ, {"MEDIA_LAB_RENDER_URL": cls.fake.url})
        cls.env.start()
        render_client._cache.clear()
        cls.srv = server.make_server(cls.r.config(), adopt=False)
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.fake.close()
        cls.env.stop()
        render_client._cache.clear()
        cls.r.cleanup()

    def test_every_screen_fits(self):
        wide = []
        for scheme in ("light", "dark"):
            for w in WIDTHS:
                ctx = self.browser.new_context(viewport={"width": w, "height": 860}, color_scheme=scheme,
                                               is_mobile=w < 800, has_touch=w < 1200)
                ctx.add_init_script(SEEN)
                pg = ctx.new_page()
                for view in VIEWS:
                    pg.goto("about:blank")
                    pg.goto(f"{self.url}/#{view}")
                    pg.wait_for_selector(f"#view-{view}:not([hidden]), #main:not([hidden])")
                    pg.wait_for_timeout(400)          # modules mount and fill after their first fetches
                    sw, cw = pg.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
                    if sw > cw:
                        wide.append(f"{view} at {w} px ({scheme}): {sw} > {cw}")
                ctx.close()
        self.assertEqual(wide, [])

    def test_first_visit_shows_the_welcome_once(self):
        ctx = self.browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        pg = ctx.new_page()
        pg.goto(self.url + "/")
        pg.wait_for_selector("#notice[open]")
        for _ in range(3):                                      # goal (Make posts), GPU, the notice
            pg.click("#noticeok")
        pg.wait_for_selector("#notice:not([open])", state="attached")
        self.assertEqual(pg.evaluate("document.body.dataset.view"), "posts")
        pg.reload()
        pg.wait_for_timeout(500)
        self.assertFalse(pg.evaluate("document.getElementById('notice').open"))
        self.assertIsNone(pg.evaluate("document.querySelector('.tourcard') && 1"))   # no tour unless asked
        ctx.close()

    def test_theme_switch_is_remembered(self):
        ctx = self.browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
        ctx.add_init_script(SEEN)
        pg = ctx.new_page()
        pg.goto(self.url + "/#make")
        self.assertIsNone(pg.evaluate("document.documentElement.getAttribute('data-theme')"))   # system first
        pg.click("#themebtn")
        self.assertEqual(pg.evaluate("document.documentElement.getAttribute('data-theme')"), "light")
        bg = pg.evaluate("getComputedStyle(document.body).backgroundColor")
        self.assertEqual(bg, "rgb(244, 243, 239)")              # light paper although the system is dark
        pg.reload()
        self.assertEqual(pg.evaluate("document.documentElement.getAttribute('data-theme')"), "light")
        self.assertIn("Light", pg.inner_text("#themebtn"))
        ctx.close()


if __name__ == "__main__":
    unittest.main()
