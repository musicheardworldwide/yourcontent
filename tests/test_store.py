"""WP2: DATA layout, atomic sidecars, index / recent / tracks, names, ledger, scoped delete (§3, §5)."""
from __future__ import annotations

import json
import os
import re
import stat
import unittest
from unittest import mock

from medialab import store
from medialab.types import NotOwned
from tests.util import TempRoots, make_png


def sc(kind, created, **kw):
    return {"schema": "media-lab/asset@1", "kind": kind, "created": created, "commercial_ok": kw.pop("ok", None),
            **kw}


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.r = TempRoots()
        self.addCleanup(self.r.cleanup)
        store.configure(self.r.config())

    def test_data_layout(self):
        for d in store.DATA_DIRS:
            p = os.path.join(self.r.data, d)
            self.assertTrue(os.path.isdir(p), d)
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode) & 0o077, 0, d)
        with open(os.path.join(self.r.data, "VERSION")) as f:
            self.assertEqual(f.read().strip(), "1")
        self.assertEqual(store.root_dir("lab"), os.path.join(self.r.data, "media"))
        self.assertEqual(store.root_dir("gen"), self.r.dir("gen"))
        store.configure(self.r.config())   # idempotent

    def test_unwritable_data_is_a_plain_error(self):
        with mock.patch("os.makedirs", side_effect=PermissionError(13, "Permission denied")):
            with self.assertRaises(OSError) as cm:
                store.configure(self.r.config(data=os.path.join(self.r.base, "nope")))
        self.assertIn("data folder", str(cm.exception))
        self.assertNotIn(self.r.base, str(cm.exception))

    def test_put_sidecar_is_atomic_and_sets_root_and_name(self):
        ref = ("gen", "img_00001_.png")
        store.put_sidecar(ref, {"kind": "image", "root": "lab", "name": "wrong.png"})
        d = store.data_path("sidecars", "gen")
        self.assertEqual(os.listdir(d), ["img_00001_.png.json"])     # no temp file left behind
        self.assertEqual(store.sidecar(ref)["root"], "gen")
        self.assertEqual(store.sidecar(ref)["name"], "img_00001_.png")
        self.assertTrue(store.owned(ref))
        # a failed write leaves the old sidecar and no temp file
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                store.put_sidecar(ref, {"kind": "image", "label": "new"})
        self.assertNotIn("label", store.sidecar(ref))
        self.assertEqual(os.listdir(d), ["img_00001_.png.json"])

    def test_corrupt_sidecar_reads_as_none(self):
        with open(store.data_path("sidecars", "gen", "x.png.json"), "w") as f:
            f.write("{not json")
        self.assertIsNone(store.sidecar(("gen", "x.png")))
        self.assertEqual(store.index(), [])

    def _assets(self):
        self.r.put("out", "qwen_studio_00001_.png", make_png())
        store.put_sidecar(("out", "qwen_studio_00001_.png"),
                          sc("image", "2026-10-07T10:00:00Z", ok=False, probe={"w": 1328, "h": 1328}))
        self.r.put("gen", "clip_00001.mp4", b"v")
        self.r.put("gen", "clip_00001.png", make_png())
        store.put_sidecar(("gen", "clip_00001.mp4"), sc("clip", "2026-10-07T11:00:00Z", ok=False,
                                                        poster="clip_00001.png", label="push in",
                                                        probe={"w": 832, "h": 480, "seconds": 3.0625}))
        self.r.put("lab", "cut_20261007-120000_abcdef.mp4", b"v")
        store.put_sidecar(("lab", "cut_20261007-120000_abcdef.mp4"), sc("export", "2026-10-07T12:00:00Z"))
        self.r.put("gen", "music_00001.flac", b"fLaC")
        store.put_sidecar(("gen", "music_00001.flac"), sc("music", "2026-10-07T13:00:00Z", ok=True, label="lofi",
                                                          probe={"seconds": 30.0}))
        self.r.put("lab", "up_20261007-140000_abcdef.mp3", b"ID3")
        store.put_sidecar(("lab", "up_20261007-140000_abcdef.mp3"), sc("upload", "2026-10-07T14:00:00Z"))
        # owned but the file is gone: never listed
        store.put_sidecar(("gen", "img_00009_.png"), sc("image", "2026-10-07T15:00:00Z"))

    def test_recent_and_tracks(self):
        self._assets()
        rec = store.recent()
        self.assertEqual([x["ref"]["name"] for x in rec],
                         ["cut_20261007-120000_abcdef.mp4", "clip_00001.mp4", "qwen_studio_00001_.png"])
        clip = rec[1]
        self.assertEqual(clip, {"ref": {"root": "gen", "name": "clip_00001.mp4"}, "type": "video", "kind": "clip",
                                "url": "/m/gen/clip_00001.mp4", "poster": "/m/gen/clip_00001.png",
                                "label": "push in", "w": 832, "h": 480, "seconds": 3.0625,
                                "created": "2026-10-07T11:00:00Z", "commercial_ok": False})
        self.assertEqual(rec[2]["poster"], rec[2]["url"])          # a picture is its own thumbnail
        self.assertEqual([x["kind"] for x in store.recent(kind="image")], ["image"])
        self.assertEqual([x["kind"] for x in store.recent(kind="video")], ["export", "clip"])
        self.assertEqual([x["kind"] for x in store.recent(kind="export")], ["export"])
        self.assertEqual(len(store.recent(limit=1)), 1)
        tr = store.tracks()
        self.assertEqual([t["ref"]["name"] for t in tr], ["up_20261007-140000_abcdef.mp3", "music_00001.flac"])
        self.assertEqual(tr[1]["title"], "lofi")
        self.assertEqual(tr[1]["seconds"], 30.0)
        self.assertTrue(tr[1]["commercial_ok"])
        for row in rec + tr:
            self.assertNotIn(self.r.base, json.dumps(row))

    def test_index_cache_sees_new_and_removed_sidecars(self):
        self.assertEqual(store.index(), [])
        store.put_sidecar(("gen", "a.png"), {"kind": "image"})
        self.assertEqual(len(store.index()), 1)
        os.remove(store.data_path("sidecars", "gen", "a.png.json"))   # changed behind the store's back
        self.assertEqual(store.index(), [])

    def test_new_name_and_tmp_path(self):
        n = store.new_name("cut", "mp4")
        self.assertRegex(n, r"^cut_\d{8}-\d{6}_[0-9a-f]{6}\.mp4$")
        self.assertRegex(store.new_name("up", ".mp3"), r"^up_\d{8}-\d{6}_[0-9a-f]{6}\.mp3$")
        self.assertTrue(store.NAME_RE.fullmatch(n))
        for bad in [("../x", "mp4"), ("cut", "m/p4"), ("", "mp4")]:
            with self.assertRaises(ValueError):
                store.new_name(*bad)
        t = store.tmp_path(".mp4")
        self.assertEqual(os.path.dirname(t), store.data_path("tmp"))
        self.assertTrue(re.fullmatch(r"\.ml_[0-9a-f]{12}\.mp4", os.path.basename(t)))
        self.assertNotEqual(t, store.tmp_path(".mp4"))
        with self.assertRaises(ValueError):
            store.tmp_path("/../../x")

    def test_clean_tmp(self):
        with open(store.tmp_path(".part"), "w") as f:
            f.write("x")
        os.makedirs(store.data_path("tmp", "d"))
        self.assertEqual(store.clean_tmp(), 2)
        self.assertEqual(os.listdir(store.data_path("tmp")), [])

    def test_ledger_add_pids_drop(self):
        a, b = ("gen", "clip_00001.mp4"), ("gen", "img_00001_.png")
        store.ledger_add("p1", "clip", [])           # provisional line, right after /prompt
        store.ledger_add("p1", "clip", [a])
        store.ledger_add("p2", "image", [b])
        store.ledger_add("p3", "segment", [a, b])
        self.assertEqual(store.ledger_pids(a), ["p1", "p3"])
        self.assertEqual(store.ledger_pids(b), ["p2", "p3"])
        self.assertEqual(store.ledger_pids(("gen", "other.png")), [])
        store.ledger_drop(a)
        self.assertEqual(store.ledger_pids(a), [])
        self.assertEqual(store.ledger_pids(b), ["p2", "p3"])
        with open(store.data_path("ledger.jsonl")) as f:
            lines = [json.loads(x) for x in f if x.strip()]
        # p1's line and its provisional line both go (WP5: provisional lines no longer linger)
        self.assertEqual([x["pid"] for x in lines], ["p2", "p3"])
        self.assertEqual(lines[1]["refs"], [["gen", "img_00001_.png"]])
        self.assertRegex(lines[0]["created"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        with self.assertRaises(ValueError):
            store.ledger_add("", "image", [])

    def test_remove_takes_file_poster_and_sidecar_only(self):
        self._assets()
        self.r.put("gen", "clip_00002.mp4", b"other")
        before = self.r.snapshot(self.r.output)
        removed = store.remove(("gen", "clip_00001.mp4"))
        self.assertEqual(removed, ["clip_00001.mp4", "clip_00001.png"])
        self.assertFalse(store.owned(("gen", "clip_00001.mp4")))
        after = self.r.snapshot(self.r.output)
        self.assertEqual(set(before) - set(after),
                         {os.path.join("media_lab", "clip_00001.mp4"), os.path.join("media_lab", "clip_00001.png")})
        for k in after:
            self.assertEqual(after[k], before[k])
        with self.assertRaises(NotOwned):
            store.remove(("gen", "clip_00002.mp4"))
        self.assertTrue(os.path.exists(self.r.path("gen", "clip_00002.mp4")))
        # owned but file already gone: the sidecar still goes
        self.assertEqual(store.remove(("gen", "img_00009_.png")), [])
        self.assertFalse(store.owned(("gen", "img_00009_.png")))

    def test_scrub_removes_paths(self):
        msg = f"can't open {self.r.path('gen', 'a.png')} or /etc/passwd or {self.r.data}/tmp/x"
        out = store.scrub(msg)
        self.assertNotIn(self.r.base, out)
        self.assertNotIn("/etc/passwd", out)
        self.assertEqual(store.scrub("use a picture from Recent"), "use a picture from Recent")
        self.assertEqual(store.scrub("9/16 is tall"), "9/16 is tall")


if __name__ == "__main__":
    unittest.main()
