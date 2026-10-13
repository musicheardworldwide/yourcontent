"""Opt-in end-to-end test: every craft recipe through craftd with the real apps, in throwaway temp folders.

    CRAFT_BIN_DIR=<folder with the four *-cli binaries> python3 -m unittest tests.e2e_craft
    (or CRAFT_SRC=<folder of source checkouts with target/release builds>; needs bubblewrap, ffmpeg and ffprobe)

Not collected by `unittest discover` (the file name does not start with test_). Inputs are generated here with
ffmpeg; every output goes to a temp folder that is removed afterwards. Nothing is written anywhere else.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest

from craft import craftd
from medialab import craft_routes, store
from tests.util import TempRoots

KIT = {"name": "E2E", "colors": {"bg": "#0b1d3a", "fg": "#ffffff", "accent": "#e0b84a"},
       "fonts": {"display": "Inter", "body": "Inter"}, "handle": "@e2e"}


def apps_present() -> bool:
    if not (os.environ.get("CRAFT_BIN_DIR") or os.environ.get("CRAFT_SRC")) or not shutil.which("bwrap"):
        return False
    return all(os.access(craftd.binary(a), os.X_OK) for a in craftd.APPS) and bool(shutil.which("ffprobe"))


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,codec_type,width,height:"
                          "format=duration", "-of", "json", path], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


@unittest.skipUnless(apps_present(), "craft apps not built (set CRAFT_BIN_DIR or CRAFT_SRC), or no bwrap/ffprobe")
class E2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = TempRoots()
        os.environ["CRAFT_WORK_ROOT"] = os.path.join(cls.r.base, "work")
        store.configure(cls.r.config())
        os.makedirs(craftd.queue_dir(), exist_ok=True)
        os.makedirs(craftd.jobs_dir(), exist_ok=True)
        ff = ["ffmpeg", "-loglevel", "error", "-y"]
        subprocess.run(ff + ["-f", "lavfi", "-i", "testsrc2=size=1600x1000", "-frames:v", "1",
                             cls.r.path("in", "source.png")], check=True)
        subprocess.run(ff + ["-f", "lavfi", "-i", "sine=frequency=440:duration=6", cls.r.path("in", "music.wav")],
                       check=True)
        for i, hz in ((1, 330), (2, 550)):   # phone-style clips: picture and sound
            subprocess.run(ff + ["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=2", "-f", "lavfi",
                                 "-i", f"sine=frequency={hz}:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                                 "-c:a", "aac", "-shortest", cls.r.path("in", f"clip{i}.mp4")], check=True)
        shutil.copyfile(cls.r.path("in", "source.png"), cls.r.path("in", "logo.png"))
        cls.src = {"root": "in", "name": "source.png"}

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("CRAFT_WORK_ROOT", None)
        cls.r.cleanup()

    def run_one(self, recipe, params, brand=KIT):
        jid = craft_routes.enqueue({"recipe": recipe, "params": params, "brand": brand})["job"]
        self.assertEqual(craftd.claim(), jid)
        st = craftd.run_job(jid)
        self.assertEqual(st["state"], "done", st.get("error"))
        return {o["role"]: o for o in st["outputs"]}, st

    def path(self, out):
        return store.resolve((out["ref"]["root"], out["ref"]["name"]))

    def test_1_grade(self):
        outs, st = self.run_one("photo.grade", {"image": self.src, "long_edge": 1200})
        self.assertEqual((outs["main"]["w"], outs["main"]["h"]), (1200, 750))
        self.assertIsNone(st["provenance"]["commercial_ok"])   # an upload: rights not checked

    def test_2_finish(self):
        outs, _ = self.run_one("photo.finish", {"image": self.src, "format": "4:5"})
        self.assertEqual((outs["main"]["w"], outs["main"]["h"]), (1080, 1350))
        self.assertGreater(outs["psd"]["bytes"], 100000)

    def test_3_titlecard_and_reel_with_music(self):
        kit = dict(KIT, logo={"root": "in", "name": "logo.png"})
        outs, _ = self.run_one("video.titlecard", {"headline": "Open late", "kicker": "Studio", "seconds": 2,
                                                   "background": self.src}, kit)
        v = probe(self.path(outs["main"]))
        self.assertEqual((v["streams"][0]["codec_name"], v["streams"][0]["width"], v["streams"][0]["height"]),
                         ("h264", 1080, 1920))
        self.assertIn("poster", outs)
        card = outs["main"]["ref"]
        outs, _ = self.run_one("video.reel", {"items": [{"ref": card, "seconds": 2},
                                                        {"ref": self.src, "seconds": 2, "caption": "A caption"}],
                                              "music": {"root": "in", "name": "music.wav"}})
        v = probe(self.path(outs["main"]))
        self.assertAlmostEqual(float(v["format"]["duration"]), 4.0, delta=0.25)
        self.assertIn("audio", [s.get("codec_type") for s in v["streams"]])

    def test_5_reel_of_clips_with_sound_dissolves(self):
        # audit 2026-10-11: any clip with an audio track failed the default dissolve ("no edit point")
        clips = [{"ref": {"root": "in", "name": "clip1.mp4"}}, {"ref": self.src, "seconds": 1},
                 {"ref": {"root": "in", "name": "clip2.mp4"}}, {"ref": {"root": "in", "name": "clip1.mp4"}}]
        outs, _ = self.run_one("video.reel", {"items": clips})
        v = probe(self.path(outs["main"]))
        self.assertAlmostEqual(float(v["format"]["duration"]), 7.0, delta=0.3)
        self.assertIn("audio", [s.get("codec_type") for s in v["streams"]])
        outs, _ = self.run_one("video.reel", {"items": clips[:1], "music": {"root": "in", "name": "music.wav"}})
        self.assertIn("audio", [s.get("codec_type") for s in probe(self.path(outs["main"]))["streams"]])

    def test_4_alpha(self):
        outs, _ = self.run_one("video.titlecard", {"headline": "Alpha", "alpha": True, "seconds": 2})
        self.assertEqual(probe(self.path(outs["main"]))["streams"][0]["codec_name"], "prores")


if __name__ == "__main__":
    unittest.main(verbosity=2)
