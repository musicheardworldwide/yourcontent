"""WP2: project CRUD, atomic saves with .bak, rev conflicts, list, used_in (§5.3)."""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from medialab import projects, store
from medialab.types import BadRef, Conflict, NotOwned
from tests.util import TempRoots


def clip(name, root="out"):
    return {"id": "i_" + name[-6:].replace(".", "x"), "kind": "clip", "src": {"root": root, "name": name},
            "in": 0, "out": None, "join": "cut"}


class ProjectsTest(unittest.TestCase):
    def setUp(self):
        self.r = TempRoots()
        self.addCleanup(self.r.cleanup)
        store.configure(self.r.config())

    def test_create_and_load(self):
        p = projects.create(None)
        self.assertRegex(p["id"], r"^p_[0-9a-f]{12}$")
        self.assertEqual((p["schema"], p["name"], p["rev"], p["items"], p["music"]),
                         ("media-lab/project@1", "Untitled cut", 1, [], None))
        self.assertEqual(p["export"], {"preset": "9:16", "fit": "auto", "fps": 24, "fade": 0.5})
        self.assertEqual(projects.load(p["id"]), p)
        self.assertEqual(os.listdir(store.data_path("projects")), [p["id"] + ".json"])
        with self.assertRaises(NotOwned):
            projects.load("p_000000000000")
        with self.assertRaises(BadRef):
            projects.load("../../etc/passwd")

    def test_names_are_cleaned(self):
        self.assertEqual(projects.clean_name("  My\x00 cut\n\tone ‮"), "My cut one")
        self.assertEqual(projects.clean_name("x" * 200), "x" * 80)
        self.assertEqual(projects.clean_name(""), "Untitled cut")
        self.assertEqual(projects.clean_name(None), "Untitled cut")
        self.assertEqual(projects.create("a;[b]")["name"], "a;[b]")   # stored as text; never reaches ffmpeg graphs

    def test_save_bumps_rev_keeps_identity_and_bak(self):
        p = projects.create("One")
        pid = p["id"]
        edit = dict(p, name="Two", items=[clip("qwen_video_00001.mp4")], id="p_ffffffffffff", rev=99,
                    created="1999-01-01T00:00:00Z", schema="other")
        self.assertEqual(projects.save(pid, 1, edit, seconds=3.0833333), 2)
        cur = projects.load(pid)
        self.assertEqual((cur["id"], cur["rev"], cur["name"], cur["schema"], cur["created"]),
                         (pid, 2, "Two", "media-lab/project@1", p["created"]))
        self.assertEqual(cur["seconds"], 3.083333)
        self.assertEqual(len(cur["items"]), 1)
        with open(store.data_path("projects", pid + ".json.bak")) as f:
            self.assertEqual(json.load(f)["rev"], 1)
        self.assertEqual(sorted(os.listdir(store.data_path("projects"))), [pid + ".json", pid + ".json.bak"])

    def test_stale_rev_conflicts_with_current(self):
        p = projects.create("One")
        projects.save(p["id"], 1, dict(p, name="Two"))
        with self.assertRaises(Conflict) as cm:
            projects.save(p["id"], 1, dict(p, name="Three"))
        self.assertEqual(cm.exception.current["name"], "Two")
        self.assertEqual(cm.exception.current["rev"], 2)
        self.assertEqual(cm.exception.status, 409)
        self.assertEqual(projects.load(p["id"])["name"], "Two")
        for bad in ("2", True, None, 2.0):
            with self.subTest(rev=bad), self.assertRaises(ValueError):
                projects.save(p["id"], bad, p)

    def test_too_large_is_refused_and_nothing_written(self):
        p = projects.create("Big")
        big = dict(p, items=[dict(clip("qwen_video_00001.mp4"), pad="x" * 300_000)])
        with self.assertRaises(projects.TooLarge) as cm:
            projects.save(p["id"], 1, big)
        self.assertEqual(cm.exception.status, 413)
        self.assertEqual(projects.load(p["id"])["rev"], 1)

    def test_failed_write_keeps_the_old_file(self):
        p = projects.create("Keep")
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                projects.save(p["id"], 1, dict(p, name="Lost"))
        self.assertEqual(projects.load(p["id"])["name"], "Keep")
        self.assertEqual([n for n in os.listdir(store.data_path("projects")) if n.endswith(".tmp")], [])

    def test_list_remove_used_in(self):
        a = projects.create("Alpha")
        b = projects.create("Beta")
        projects.save(a["id"], 1, dict(a, items=[clip("qwen_video_00001.mp4")]), seconds=3.0)
        projects.save(b["id"], 1, dict(b, items=[clip("clip_00001.mp4", "gen")],
                                       music={"src": {"root": "gen", "name": "music_00001.flac"}, "volume": 1.0}))
        rows = projects.list_all()
        self.assertEqual({r["name"] for r in rows}, {"Alpha", "Beta"})
        alpha = next(r for r in rows if r["name"] == "Alpha")
        self.assertEqual(alpha, {"id": a["id"], "name": "Alpha", "updated": alpha["updated"], "items": 1,
                                 "seconds": 3.0})
        self.assertEqual(projects.used_in(("out", "qwen_video_00001.mp4")), ["Alpha"])
        self.assertEqual(projects.used_in(("gen", "music_00001.flac")), ["Beta"])
        self.assertEqual(projects.used_in(("gen", "nothing.png")), [])
        projects.remove(a["id"])
        self.assertFalse(any(n.startswith(a["id"]) for n in os.listdir(store.data_path("projects"))))
        self.assertEqual(projects.used_in(("out", "qwen_video_00001.mp4")), [])
        with self.assertRaises(NotOwned):
            projects.remove(a["id"])

    def test_refs_of_skips_bad_refs(self):
        p = {"items": [clip("ok.mp4"), {"src": {"root": "out", "name": "../x"}}, clip("ok.mp4"), "junk"],
             "music": {"src": {"root": "lab", "name": "up_1.mp3"}}}
        self.assertEqual(projects.refs_of(p), [("out", "ok.mp4"), ("lab", "up_1.mp3")])


if __name__ == "__main__":
    unittest.main()
