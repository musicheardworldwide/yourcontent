"""WP3: licence maths and sidecar shape (§5.4, §11, §14 test_provenance). Pure."""
from __future__ import annotations

import itertools
import unittest
from fractions import Fraction

from medialab import models, provenance as pv
from medialab.types import Probe


def M():
    return models.load(None)


def entry(ok, lid=None, role="start", name="x.png", missing=False):
    return {"role": role, "root": "gen", "name": name, "commercial_ok": ok,
            "licences": [lid] if lid else [], "most_restrictive": lid, "missing": missing}


class TestAggregate(unittest.TestCase):
    def test_truth_table_worst_wins(self):
        for combo in itertools.product((True, None, False), repeat=3):
            inputs = [entry(v, {True: "apache-2.0", None: "upload", False: "qwen-image-nc"}[v], name=f"i{k}.png")
                      for k, v in enumerate(combo)]
            got = pv.aggregate([], inputs)["commercial_ok"]
            want = False if False in combo else None if None in combo else True
            self.assertIs(got, want, combo)

    def test_models_count_too(self):
        m = M()
        nc = pv.model_entries(m, m.used_image("bf16"))
        agg = pv.aggregate(nc, [])
        self.assertIs(agg["commercial_ok"], False)
        self.assertEqual(agg["most_restrictive"], "qwen-image-nc")
        # the int8 text encoder comes from the qwen-research repack, so it is qwen-image-nc too
        self.assertEqual(agg["licences"], ["qwen-image-nc"])
        self.assertTrue(all(l.startswith("model ") for l in agg["restricted_by"]))
        self.assertTrue(any("non-commercial" in l for l in agg["restricted_by"]))

    def test_all_apache_true(self):
        m = M()
        agg = pv.aggregate(pv.model_entries(m, m.used_video()), [])
        self.assertIs(agg["commercial_ok"], True)
        self.assertEqual(agg["licences"], ["apache-2.0"])
        self.assertEqual(agg["most_restrictive"], "apache-2.0")
        self.assertEqual(agg["restricted_by"], [])
        music = pv.aggregate(pv.model_entries(m, m.used_music()), [])
        self.assertIs(music["commercial_ok"], True)
        self.assertEqual(music["licences"], ["ace-step"])

    def test_upload_is_null_with_its_line(self):
        up = pv.upload_entry(("lab", "up_20261007-210000_a1b2c3.mp3"))
        agg = pv.aggregate([], [up])
        self.assertIsNone(agg["commercial_ok"])
        self.assertEqual(agg["most_restrictive"], "upload")
        self.assertEqual(agg["restricted_by"], ["your upload up_20261007-210000_a1b2c3.mp3: rights not checked"])

    def test_empty_is_unrestricted_and_unknown_origin_is_null(self):
        agg = pv.aggregate([], [])
        self.assertIs(agg["commercial_ok"], True)
        self.assertEqual(agg["restricted_by"], [])
        agg = pv.aggregate([], [pv.origin_unknown()])
        self.assertIsNone(agg["commercial_ok"])
        self.assertEqual(agg["most_restrictive"], "unknown")
        self.assertEqual(agg["restricted_by"], [pv.UNKNOWN_REASON])

    def test_qwen_wins_tie_order(self):
        inputs = [entry(None, "unknown", name="a.png"), entry(False, "qwen-image-nc", name="b.png"),
                  pv.upload_entry(("lab", "up_x.mp4"))]
        agg = pv.aggregate([], inputs)
        self.assertIs(agg["commercial_ok"], False)
        self.assertEqual(agg["most_restrictive"], "qwen-image-nc")
        self.assertEqual(agg["restricted_by"], ["start image b.png: Qwen-Image 2.1 (non-commercial)"])


class TestChains(unittest.TestCase):
    """A Qwen picture -> clip -> extension -> export stays false at every step (§11.4)."""

    def test_chain_stays_false(self):
        m = M()
        pic = pv.make_sidecar("image", ("gen", "img_00001_.png"), models=pv.model_entries(m, m.used_image("bf16")),
                              inputs=[])
        self.assertIs(pic["commercial_ok"], False)
        clip = pv.make_sidecar("clip", ("gen", "clip_00001.mp4"), models=pv.model_entries(m, m.used_video()),
                               inputs=[pv.input_entry("start", ("gen", "img_00001_.png"), pic)])
        self.assertIs(clip["commercial_ok"], False)
        self.assertEqual(clip["most_restrictive"], "qwen-image-nc")
        self.assertEqual(clip["restricted_by"], ["start image img_00001_.png: Qwen-Image 2.1 (non-commercial)"])
        ext = pv.make_sidecar("extension", ("gen", "ext_x.mp4"), models=pv.model_entries(m, m.used_video()),
                              inputs=[pv.input_entry("source", ("gen", "clip_00001.mp4"), clip)])
        self.assertIs(ext["commercial_ok"], False)
        self.assertEqual(ext["restricted_by"], ["source clip clip_00001.mp4: Qwen-Image 2.1 (non-commercial)"])
        clean = pv.make_sidecar("clip", ("gen", "clip_00002.mp4"), models=pv.model_entries(m, m.used_video()),
                                inputs=[])
        self.assertIs(clean["commercial_ok"], True)
        items = [pv.input_entry("item", ("gen", "clip_00002.mp4"), clean) for _ in range(9)]
        items.append(pv.input_entry("item", ("gen", "ext_x.mp4"), ext))
        export = pv.make_sidecar("export", ("lab", "cut_x.mp4"), models=[], inputs=items)
        self.assertIs(export["commercial_ok"], False)
        self.assertEqual(export["licences"], ["apache-2.0", "qwen-image-nc"])
        self.assertEqual(len(export["restricted_by"]), 1)

    def test_snapshot_survives_deleted_input(self):
        """The export's input entry holds the input's own aggregate; deleting the input's sidecar later
        cannot change it (the entry is a copy, not a reference)."""
        m = M()
        pic = pv.make_sidecar("image", ("gen", "img_1.png"), models=pv.model_entries(m, m.used_image("bf16")),
                              inputs=[])
        e = pv.input_entry("item", ("gen", "img_1.png"), pic)
        pic.clear()   # the input's sidecar goes away
        self.assertIs(e["commercial_ok"], False)
        self.assertEqual(e["licences"], ["qwen-image-nc"])
        self.assertIs(pv.aggregate([], [e])["commercial_ok"], False)

    def test_missing_item_uses_snap(self):
        gone = pv.input_entry("item", ("out", "qwen_video_00001.mp4"), None, snap_ok=False)
        self.assertTrue(gone["missing"])
        agg = pv.aggregate([], [gone, pv.input_entry("item", ("gen", "c.mp4"), {"commercial_ok": True,
                                                                               "licences": ["apache-2.0"]})])
        self.assertIs(agg["commercial_ok"], False)
        self.assertEqual(agg["restricted_by"],
                         ["item qwen_video_00001.mp4 (missing): not for commercial use (recorded before it went "
                          "missing)"])
        unknown = pv.input_entry("item", ("out", "x.mp4"), None)
        self.assertIsNone(pv.aggregate([], [unknown])["commercial_ok"])


class TestSidecarShape(unittest.TestCase):
    def test_fields_for_downstream_tools(self):
        m = M()
        probe = Probe(kind="video", w=832, h=480, sar=Fraction(1), frames=49, rate=Fraction(16), seconds=3.0625,
                      has_audio=False, audio_rate=0, audio_samples=0, vcodec="h264", acodec="", color="bt709")
        sc = pv.make_sidecar("clip", ("gen", "clip_00004.mp4"), models=pv.model_entries(m, m.used_video()),
                             inputs=[], prompt="slow push in", negative="static", seed=123456,
                             params={"w": 832}, pids=["abc"], probe=probe, sha256="0" * 64, nbytes=10,
                             poster="clip_00004.png", label="slow push in\n\x07 and more", created="2026-10-07T21:03:11Z")
        for k in ("schema", "root", "name", "kind", "created", "app", "label", "poster", "sha256", "bytes", "probe",
                  "models", "prompt", "negative", "seed", "params", "inputs", "commercial_ok", "licences",
                  "most_restrictive", "restricted_by", "comfy", "legacy", "render"):
            self.assertIn(k, sc)
        self.assertEqual(sc["schema"], "media-lab/asset@1")
        self.assertEqual(sc["app"]["name"], "YourContent")
        self.assertEqual(sc["probe"]["fps"], "16/1")
        self.assertEqual(sc["probe"]["frames"], 49)
        self.assertEqual(sc["comfy"], {"prompt_ids": ["abc"]})
        self.assertEqual(sc["label"], "slow push in and more")
        self.assertNotIn("inferred", sc)
        self.assertEqual({e["id"] for e in sc["models"]},
                         {"wan2.2-i2v-a14b", "wan2.2-lightx2v-4step", "umt5-xxl", "wan2.1-vae"})
        self.assertTrue(all(e["licence"] == "apache-2.0" and e["commercial_ok"] is True for e in sc["models"]))
        leg = pv.make_sidecar("image", ("out", "a.png"), models=[], inputs=[],
                              legacy={"adopted": "x", "pattern": "qwen_studio", "recovered_from": "none"})
        self.assertTrue(leg["inferred"])

    def test_label_truncated(self):
        self.assertEqual(len(pv.clean_label("x" * 200)), 60)


class TestTexts(unittest.TestCase):
    def test_warnings(self):
        nc = {"commercial_ok": False, "most_restrictive": "qwen-image-nc", "licences": ["qwen-image-nc"]}
        self.assertEqual(pv.warnings(nc, 0), ["Contains Qwen-Image 2.1 output: non-commercial use only."])
        self.assertEqual(pv.warnings({"commercial_ok": None, "most_restrictive": "upload"}, 2),
                         ["Includes uploads: make sure you have the rights."])
        self.assertEqual(pv.warnings({"commercial_ok": True, "most_restrictive": "apache-2.0"}, 0), [])

    def test_mp4_comment(self):
        nc = {"commercial_ok": False, "most_restrictive": "qwen-image-nc"}
        c = pv.mp4_comment(nc, "cut_20261007-210000_a1b2c3.mp4")
        self.assertEqual(c, "YourContent export cut_20261007-210000_a1b2c3.mp4; license: non-commercial "
                            "(Qwen-Image 2.1 license)")
        long = pv.mp4_comment({"commercial_ok": True}, "a;[b]" * 200)
        self.assertLessEqual(len(long), 255)
        self.assertNotIn(";[", long)
        # the prompt is never an argument, so it can never be in the comment
        self.assertNotIn("prompt", pv.mp4_comment.__code__.co_varnames)


if __name__ == "__main__":
    unittest.main()
