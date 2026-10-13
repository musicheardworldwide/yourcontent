"""WP5: the seams between packages, with the real timeline, provenance, store and projects modules.

Only media.probe is faked (so this runs without ffmpeg 7.1); RealFlowTest in test_server.py covers the same
flow end to end with real ffmpeg in the container.
"""
from __future__ import annotations

import http.client
import json
import threading
import time
import unittest
from fractions import Fraction
from unittest import mock

from medialab import __main__ as cli, features, jobs, media, server, store
from medialab.types import Probe
from tests.util import TempRoots


def fake_probe(path):
    if path.endswith(".png"):
        return Probe("image", 1104, 1472, Fraction(1), 0, Fraction(0), 0.0, False, 0, 0, "png", "", "unknown")
    return Probe("video", 624, 624, Fraction(1), 49, Fraction(16), 3.0625, False, 0, 0, "h264", "", "bt709")


class SeamTest(unittest.TestCase):
    def setUp(self):
        self.r = TempRoots()
        self.addCleanup(self.r.cleanup)
        p = mock.patch.object(media, "probe", fake_probe)
        p.start()
        self.addCleanup(p.stop)
        jobs.reset()
        self.addCleanup(jobs.reset)
        self.srv = server.make_server(self.r.config(), adopt=False)
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.base = f"127.0.0.1:{self.srv.server_address[1]}"

    def req(self, method, path, body=None):
        conn = http.client.HTTPConnection(self.base, timeout=10)
        h = {"X-Media-Lab": "1", "Content-Type": "application/json"} if method == "POST" else {}
        conn.request(method, path, json.dumps(body).encode() if body is not None else None, h)
        resp = conn.getresponse()
        out = resp.read()
        conn.close()
        return resp.status, (json.loads(out) if out else None)

    def own(self, root, name, **sc):
        self.r.put(root, name, b"x")
        store.put_sidecar((root, name), {"schema": "media-lab/asset@1", "root": root, "name": name,
                                         "created": "2026-10-07T10:00:00Z", "commercial_ok": False,
                                         "licences": ["qwen-image-nc"], **sc})

    def test_save_returns_project_snaps_and_item_posters(self):
        self.own("out", "qwen_studio_00009_.png", kind="image")
        self.own("out", "qwen_video_00001.mp4", kind="clip", poster="qwen_video_00001.png")
        self.r.put("out", "qwen_video_00001.png", b"p")
        code, out = self.req("POST", "/api/projects", {"name": "Seams"})
        self.assertEqual(code, 200, out)
        proj = out["project"]
        proj["items"] = [
            {"id": "i_000001", "kind": "clip", "src": {"root": "out", "name": "qwen_video_00001.mp4"},
             "in": 0, "out": None, "join": "fade"},
            {"id": "i_000002", "kind": "still", "src": {"root": "out", "name": "qwen_studio_00009_.png"},
             "dur": 2.5, "join": "cut"}]
        code, saved = self.req("POST", f"/api/projects/{proj['id']}", {"rev": 1, "project": proj})
        self.assertEqual(code, 200, saved)
        # the server's snaps come back, so the page can keep them
        snaps = {it["id"]: it["snap"] for it in saved["project"]["items"]}
        self.assertEqual(snaps["i_000001"]["frames"], 49)
        self.assertIs(snaps["i_000002"]["commercial_ok"], False)
        # real planner: 49 f @ 16 -> 74 at 24 fps; 2.5 s still -> 60; one 12-frame fade
        items = {it["id"]: it for it in saved["resolved"]["items"]}
        self.assertEqual((items["i_000001"]["frames"], items["i_000002"]["frames"]), (74, 60))
        self.assertEqual(saved["resolved"]["frames"], 74 + 60 - 12)
        self.assertEqual(items["i_000001"]["poster"], "/m/out/qwen_video_00001.png")
        self.assertEqual(items["i_000002"]["poster"], "/m/out/qwen_studio_00009_.png")
        self.assertIs(saved["resolved"]["licence"]["commercial_ok"], False)

        # the clip disappears; the page sends the item without a snap: the stored snap is kept
        store.remove(("out", "qwen_video_00001.mp4"))
        for it in proj["items"]:
            it.pop("snap", None)
        code, again = self.req("POST", f"/api/projects/{proj['id']}", {"rev": 2, "project": proj})
        self.assertEqual(code, 200, again)
        clip = {it["id"]: it for it in again["project"]["items"]}["i_000001"]
        self.assertEqual(clip["snap"]["frames"], 49)
        res = {it["id"]: it for it in again["resolved"]["items"]}["i_000001"]
        self.assertEqual((res["frames"], res["missing"], res["poster"]), (74, True, None))
        self.assertTrue(any("missing" in w for w in again["resolved"]["warnings"]))
        self.assertIs(again["resolved"]["licence"]["commercial_ok"], False)

    def test_info_has_used_in(self):
        self.own("out", "qwen_studio_00009_.png", kind="image")
        _, out = self.req("POST", "/api/projects", {"name": "Uses it"})
        proj = out["project"]
        proj["items"] = [{"id": "i_000001", "kind": "still",
                          "src": {"root": "out", "name": "qwen_studio_00009_.png"}, "dur": 1.0, "join": "cut"}]
        self.req("POST", f"/api/projects/{proj['id']}", {"rev": 1, "project": proj})
        code, info = self.req("GET", "/api/info/out/qwen_studio_00009_.png")
        self.assertEqual((code, info["used_in"]), (200, ["Uses it"]))


class LedgerTest(unittest.TestCase):
    def test_drop_removes_provisional_line_of_same_pid(self):
        r = TempRoots()
        self.addCleanup(r.cleanup)
        store.configure(r.config())
        store.ledger_add("pid-a", "image", [])                 # comfy.submit's provisional line
        store.ledger_add("pid-a", "image", [("gen", "img_00001_.png")])
        store.ledger_add("pid-b", "image", [])                 # someone else's provisional line stays
        store.ledger_drop(("gen", "img_00001_.png"))
        with open(store.data_path("ledger.jsonl"), encoding="utf-8") as f:
            pids = [json.loads(line)["pid"] for line in f if line.strip()]
        self.assertEqual(pids, ["pid-b"])


class JobsCancelTest(unittest.TestCase):
    def test_media_cancelled_maps_to_cancelled(self):
        jobs.reset()
        self.addCleanup(jobs.reset)

        def fn(job):
            raise media.Cancelled("stopped")
        jid = jobs.submit("ffmpeg", "export", fn, label="x")
        deadline = time.time() + 5
        while jobs.get(jid)["state"] in ("queued", "running") and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(jobs.get(jid)["state"], "cancelled")


class OneListTest(unittest.TestCase):
    def test_required_filters_single_source(self):
        self.assertIs(media.REQUIRED_FILTERS, features.REQUIRED_FILTERS)

    def test_fixture_dump_uses_shipped_models_only(self):
        # the owner's DATA/config/models.json must never reach a committed fixture
        with open(cli.__file__, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("models_mod.load(None)", src)
        self.assertNotIn("dump_object_info(infos, models, dump_fixture", src)

if __name__ == "__main__":
    unittest.main()
