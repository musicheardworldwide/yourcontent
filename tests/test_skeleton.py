"""WP0: the package skeleton imports, the shared types are frozen, the page has no inline JS/CSS,
and the shared test helpers work."""
from __future__ import annotations

import dataclasses
import importlib
import json
import os
import re
import unittest
import urllib.error
import urllib.request
import uuid
from fractions import Fraction

from tests.util import ROOT, FakeComfy, FakeJob, TempRoots, make_png

def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


MODULES = ["types", "config", "__main__", "server", "store", "projects", "jobs", "actions", "comfy", "graphs",
           "models", "features", "provenance", "legacy", "timeline", "export", "media"]


class SkeletonTest(unittest.TestCase):
    def test_every_module_imports(self):
        import medialab
        self.assertEqual(medialab.VERSION, "0.2.0")
        for m in MODULES:
            importlib.import_module("medialab." + m)

    def test_types_are_frozen(self):
        from medialab.types import BadRef, Conflict, ItemPlan, MusicPlan, NotOwned, Plan, Probe
        p = Probe("video", 624, 624, Fraction(1), 49, Fraction(16), 3.0625, False, 0, 0, "h264", "", "bt709")
        self.assertFalse(p.vfr)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            p.w = 1
        for cls in (ItemPlan, MusicPlan, Plan):
            self.assertTrue(cls.__dataclass_params__.frozen)
        self.assertTrue(issubclass(BadRef, ValueError) and issubclass(NotOwned, LookupError))
        self.assertEqual(Conflict({"rev": 3}).current, {"rev": 3})

    def test_pure_modules_import_nothing_impure(self):
        for m in ("timeline", "graphs", "provenance", "features"):
            src = _read("medialab", m + ".py")
            for bad in ("import subprocess", "import urllib", "import socket", "from . import store",
                        "from .store", "from . import media", "from .media", "from .comfy"):
                self.assertNotIn(bad, src, f"{m}.py: {bad}")

    def test_page_has_no_inline_js_or_css(self):
        html = _read("static", "index.html")
        self.assertNotRegex(html, r"<script(?![^>]*\bsrc=)[^>]*>")
        self.assertNotIn("<style", html)
        self.assertNotRegex(html, r"\sstyle=")
        self.assertNotRegex(html, r"\son[a-z]+=")
        self.assertIn('src="/static/app.js"', html)
        self.assertIn('href="/static/style.css"', html)
        js = _read("static", "app.js")
        self.assertNotRegex(js, r"""\son[a-z]+=\\?["']""")   # no inline handlers inside built HTML
        self.assertNotIn("style=", js)


class HelpersTest(unittest.TestCase):
    def test_temp_roots(self):
        with TempRoots(input_subdir="qwen_studio") as r:
            for root in ("out", "gen", "in", "lab"):
                self.assertTrue(os.path.isdir(r.dir(root)))
            self.assertTrue(r.dir("gen").startswith(r.dir("out")))
            r.put("out", "a.png", b"x", mtime=1_000_000)
            snap = r.snapshot()
            self.assertEqual(snap["output/a.png"][:2], (1, 1_000_000 * 10**9))
            self.assertEqual(r.config().uploads, r.dir("in"))
            self.assertEqual(r.env["MEDIA_LAB_INPUT_SUBDIR"], "qwen_studio")
            base = r.base
        self.assertFalse(os.path.exists(base))

    def test_make_png_text(self):
        data = make_png(text={"prompt": {"3": {"class_type": "KSampler"}}})
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertIn(b"tEXtprompt\x00", data)

    def test_fake_job(self):
        j = FakeJob()
        j.progress(0.1, "probe"); j.progress(0.5, "video"); j.progress(0.9, "video")
        self.assertEqual(j.phases, ["probe", "video"])
        self.assertFalse(j.cancelled); j.cancel(); self.assertTrue(j.cancelled)


def _req(url, payload=None, raw=None, headers=None):
    data = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
    req = urllib.request.Request(url, data, headers or {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        body = r.read()
        return json.loads(body) if body else None


class FakeComfyTest(unittest.TestCase):
    def test_prompt_history_delete(self):
        with TempRoots() as r, FakeComfy(object_info={"SaveImage": {"input": {}}}, output_dir=r.output,
                                         input_dir=r.input) as fc:
            self.assertEqual(_req(fc.url + "/system_stats")["system"]["comfyui_version"], "0.37.0")
            self.assertIn("SaveImage", _req(fc.url + "/object_info/SaveImage"))
            self.assertEqual(_req(fc.url + "/object_info/Nope"), {})
            graph = {"9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "media_lab/img"}},
                     "10": {"class_type": "SaveAudio", "inputs": {"filename_prefix": "media_lab/music"}}}
            pid = _req(fc.url + "/prompt", {"prompt": graph})["prompt_id"]
            entry = _req(fc.url + "/history/" + pid)[pid]
            self.assertEqual(entry["status"]["status_str"], "success")
            self.assertEqual(entry["outputs"]["9"]["images"][0],
                             {"filename": "img_00001_.png", "subfolder": "media_lab", "type": "output"})
            self.assertEqual(entry["outputs"]["10"]["audio"][0]["filename"], "music_00001.flac")
            self.assertTrue(os.path.isfile(r.path("gen", "img_00001_.png")))
            fc.add_history("other-tool", {"1": {"images": [{"filename": "x.png", "subfolder": "", "type": "output"}]}})
            self.assertIsNone(_req(fc.url + "/history", {"delete": [pid]}))
            self.assertEqual(set(_req(fc.url + "/history")), {"other-tool"})
            self.assertFalse(fc.free_called)

    def test_queue_interrupt_and_errors(self):
        with FakeComfy(auto_complete=False) as fc:
            pid = _req(fc.url + "/prompt", {"prompt": {}})["prompt_id"]
            self.assertEqual(_req(fc.url + "/queue")["queue_pending"][0][1], pid)
            fc.run(pid)
            self.assertEqual(_req(fc.url + "/queue")["queue_running"][0][1], pid)
            _req(fc.url + "/interrupt", {"prompt_id": pid})
            self.assertEqual(_req(fc.url + "/history/" + pid)[pid]["status"]["status_str"], "error")
            fc.node_errors = {"3": {"errors": [{"message": "Value not in list", "details": "unet_name"}]}}
            with self.assertRaises(urllib.error.HTTPError) as cm:
                _req(fc.url + "/prompt", {"prompt": {}})
            self.assertEqual(cm.exception.code, 400)
            self.assertIn("3", json.loads(cm.exception.read())["node_errors"])

    def test_upload_image(self):
        with TempRoots() as r, FakeComfy(input_dir=r.input) as fc:
            b = uuid.uuid4().hex
            png = make_png()
            body = (f'--{b}\r\nContent-Disposition: form-data; name="subfolder"\r\n\r\nmedia_lab\r\n'
                    f'--{b}\r\nContent-Disposition: form-data; name="type"\r\n\r\ninput\r\n'
                    f'--{b}\r\nContent-Disposition: form-data; name="image"; filename="ref_1.png"\r\n'
                    f'Content-Type: image/png\r\n\r\n').encode() + png + f"\r\n--{b}--\r\n".encode()
            out = _req(fc.url + "/upload/image", raw=body,
                       headers={"Content-Type": f"multipart/form-data; boundary={b}"})
            self.assertEqual(out, {"name": "ref_1.png", "subfolder": "media_lab", "type": "input"})
            with open(r.path("in", "ref_1.png"), "rb") as f:
                self.assertEqual(f.read(), png)


if __name__ == "__main__":
    unittest.main()
