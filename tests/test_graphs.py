"""WP3: ComfyUI graph builders (§9, §14 test_graphs). Pure; no ComfyUI, no network."""
from __future__ import annotations

import copy
import json
import os
import unittest

from medialab import graphs, models

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "object_info_0.37.0.json")
GOLDEN = os.path.join(HERE, "fixtures", "golden_prev1_graphs.json")


def load_fixture() -> dict:
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


def stock() -> models.Models:
    return models.load(None)


def with_lora(oi: dict, lora: str) -> dict:
    oi = copy.deepcopy(oi)
    oi["LoraLoaderModelOnly"]["input"]["required"]["lora_name"][0].append(lora)
    return oi


class TestImageGraph(unittest.TestCase):
    def setUp(self):
        self.oi = load_fixture()
        self.v = stock().variant("bf16")

    def test_no_refs(self):
        g = graphs.image_graph(self.v, "a cat", "", 1328, 1328, 30, 42, [], "media_lab/img")
        enc = g["4"]["inputs"]
        self.assertNotIn("vae", enc)
        self.assertFalse([k for k in enc if k.startswith("images.")])
        self.assertEqual(g["8"]["inputs"]["filename_prefix"], "media_lab/img")
        self.assertEqual(g["6"]["inputs"]["model"], ["1", 0])
        self.assertEqual(graphs.validate(g, self.oi), [])

    def test_one_and_six_refs(self):
        for n in (1, 6):
            refs = [f"media_lab/img_{i:05}_.png [output]" for i in range(1, n + 1)]
            g = graphs.image_graph(self.v, "a cat", "", 1104, 1472, 20, 1, refs, "media_lab/img")
            enc = g["4"]["inputs"]
            self.assertEqual(enc["vae"], ["3", 0])
            self.assertEqual(sorted(k for k in enc if k.startswith("images.")),
                             sorted(f"images.image_{i}" for i in range(1, n + 1)))
            for i in range(1, n + 1):
                self.assertEqual(g[f"r{i}"]["class_type"], "LoadImage")
                self.assertEqual(enc[f"images.image_{i}"], [f"r{i}", 0])
            self.assertEqual(graphs.validate(g, self.oi), [])

    def test_refs_capped_at_six(self):
        g = graphs.image_graph(self.v, "x", "", 1328, 1328, 4, 1, [f"a{i}.png" for i in range(9)], "p")
        self.assertEqual(len([k for k in g if k.startswith("r")]), 6)

    def test_lora_rewires_sampler(self):
        v = dict(self.v, lora="test_lora.safetensors")
        g = graphs.image_graph(v, "a cat", "", 1328, 1328, 30, 42, [], "media_lab/img")
        self.assertEqual(g["9"]["class_type"], "LoraLoaderModelOnly")
        self.assertEqual(g["9"]["inputs"]["model"], ["1", 0])
        self.assertEqual(g["6"]["inputs"]["model"], ["9", 0])
        self.assertEqual(graphs.validate(g, with_lora(self.oi, "test_lora.safetensors")), [])
        # without the file in ComfyUI's list, validation names the problem
        errs = graphs.validate(g, self.oi)
        self.assertTrue(any("lora_name" in e for e in errs), errs)


class TestVideoGraph(unittest.TestCase):
    def setUp(self):
        self.oi = load_fixture()
        self.video = stock().video

    def test_experts_and_prefix(self):
        g = graphs.video_graph(self.video, "media_lab/img_00001_.png [output]", "push in", 832, 480, 49, 7,
                               "media_lab/clip")
        hi, lo = g["40"]["inputs"], g["41"]["inputs"]
        self.assertEqual((hi["start_at_step"], hi["end_at_step"]), (0, 2))
        self.assertEqual((lo["start_at_step"], lo["end_at_step"]), (2, 10000))
        self.assertEqual(hi["model"], ["12", 0])
        self.assertEqual(lo["model"], ["22", 0])
        self.assertIn("high_noise", g["10"]["inputs"]["unet_name"])
        self.assertIn("low_noise", g["20"]["inputs"]["unet_name"])
        self.assertEqual(g["43"]["inputs"]["filename_prefix"], "media_lab/clip")
        self.assertEqual(g["43"]["inputs"]["frame_rate"], 16)
        self.assertEqual(g["35"]["inputs"]["length"], 49)
        self.assertEqual(graphs.validate(g, self.oi), [])


class TestGoldenPreV1(unittest.TestCase):
    """The pre-v1 app's graphs, captured with a neutral model table: only prefixes and the table moved out."""

    @classmethod
    def setUpClass(cls):
        with open(GOLDEN, encoding="utf-8") as f:
            cls.golden = json.load(f)
        m = stock()
        cls.v = m.variant("bf16")
        cls.video = m.video

    def test_image_graphs_match(self):
        for key in ("image_0", "image_1", "image_6", "image_lora"):
            meta = self.golden["meta"][key]
            v = dict(self.v, lora=meta.get("lora"))
            p, n, w, h, steps, seed, refs = meta["args"]
            g = graphs.image_graph(v, p, n, w, h, steps, seed, refs, "qwen_studio")
            self.assertEqual(g, self.golden["graphs"][key], key)

    def test_video_graphs_match(self):
        for key in ("video", "video_5s"):
            start, p, w, h, frames, seed = self.golden["meta"][key]["args"]
            g = graphs.video_graph(self.video, start, p, w, h, frames, seed, "qwen_video")
            self.assertEqual(g, self.golden["graphs"][key], key)


class TestMusicGraph(unittest.TestCase):
    def setUp(self):
        self.oi = load_fixture()
        self.music = stock().music

    def test_exact_nodes(self):
        g = graphs.music_graph(self.music, "warm lo-fi", "", 120, 13.72, 123456)
        self.assertEqual(sorted(g, key=int), [str(i) for i in range(1, 11)])
        self.assertEqual({k: v["class_type"] for k, v in g.items()}, {
            "1": "UNETLoader", "2": "DualCLIPLoader", "3": "VAELoader", "4": "ModelSamplingAuraFlow",
            "5": "TextEncodeAceStepAudio1.5", "6": "ConditioningZeroOut", "7": "EmptyAceStep1.5LatentAudio",
            "8": "KSampler", "9": "VAEDecodeAudio", "10": "SaveAudio"})
        self.assertEqual(g["2"]["inputs"], {"clip_name1": "qwen_0.6b_ace15.safetensors",
                                            "clip_name2": "qwen_1.7b_ace15.safetensors", "type": "ace",
                                            "device": "default"})
        self.assertEqual(g["1"]["inputs"]["unet_name"], "acestep_v1.5_turbo.safetensors")
        self.assertEqual(g["3"]["inputs"]["vae_name"], "ace_1.5_vae.safetensors")
        self.assertEqual(g["4"]["inputs"], {"model": ["1", 0], "shift": 3.0})
        ks = g["8"]["inputs"]
        self.assertEqual((ks["steps"], ks["cfg"], ks["sampler_name"], ks["scheduler"], ks["denoise"]),
                         (8, 1.0, "euler", "simple", 1.0))
        self.assertEqual(ks["negative"], ["6", 0])
        self.assertEqual(g["6"]["inputs"], {"conditioning": ["5", 0]})
        enc = g["5"]["inputs"]
        self.assertEqual(enc["lyrics"], "[Instrumental]")
        self.assertEqual(enc["keyscale"], "C major")
        self.assertEqual(enc["timesignature"], "4")
        self.assertEqual(enc["duration"], g["7"]["inputs"]["seconds"])
        self.assertEqual(enc["duration"], 13.72)
        self.assertEqual(g["10"]["inputs"], {"audio": ["9", 0], "filename_prefix": "media_lab/music"})
        self.assertEqual(graphs.validate(g, self.oi), [])

    def test_seconds_quantised_to_latent_frames(self):
        for asked, want in ((13.7, 13.72), (13.666667, 13.68), (5, 5.0), (0.2, 1.0), (240.0, 240.0)):
            g = graphs.music_graph(self.music, "x", "", 120, asked, 1)
            self.assertEqual(g["7"]["inputs"]["seconds"], want)
            self.assertEqual(g["5"]["inputs"]["duration"], want)

    def test_lyrics_and_keyscale(self):
        g = graphs.music_graph(self.music, "x", "  [verse]\nla la ", 90, 10, 1, keyscale="A minor", timesig="3",
                               language="fr")
        enc = g["5"]["inputs"]
        self.assertEqual(enc["lyrics"], "[verse]\nla la")
        self.assertEqual((enc["keyscale"], enc["timesignature"], enc["language"], enc["bpm"]),
                         ("A minor", "3", "fr", 90))
        self.assertEqual(graphs.validate(g, self.oi), [])

    def test_saver_switch(self):
        g = graphs.music_graph(self.music, "x", "", 120, 10, 1, saver="SaveAudioAdvanced")
        self.assertEqual(g["10"]["class_type"], "SaveAudioAdvanced")
        self.assertEqual(g["10"]["inputs"]["format"], "flac")
        self.assertEqual(graphs.validate(g, self.oi), [])
        with self.assertRaises(ValueError):
            graphs.music_graph(self.music, "x", "", 120, 10, 1, saver="SaveAudioMP3")

    def test_keyscales_match_comfyui(self):
        opts = self.oi["TextEncodeAceStepAudio1.5"]["input"]["required"]["keyscale"][1]["options"]
        self.assertEqual(list(graphs.KEYSCALES), opts)
        langs = self.oi["TextEncodeAceStepAudio1.5"]["input"]["required"]["language"][1]["options"]
        self.assertEqual(list(graphs.LANGUAGES), langs)


class TestValidateCatchesMistakes(unittest.TestCase):
    def setUp(self):
        self.oi = load_fixture()
        self.g = graphs.music_graph(stock().music, "x", "", 120, 10, 1)

    def test_unknown_class(self):
        g = copy.deepcopy(self.g)
        g["10"]["class_type"] = "SaveAudioFLAC"
        self.assertTrue(any("not installed" in e for e in graphs.validate(g, self.oi)))

    def test_missing_required_and_unknown_input(self):
        g = copy.deepcopy(self.g)
        del g["5"]["inputs"]["keyscale"]
        g["5"]["inputs"]["mood"] = "happy"
        errs = graphs.validate(g, self.oi)
        self.assertTrue(any("keyscale is missing" in e for e in errs), errs)
        self.assertTrue(any("unknown input mood" in e for e in errs), errs)

    def test_combo_and_range(self):
        g = copy.deepcopy(self.g)
        g["5"]["inputs"]["timesignature"] = "5"
        g["5"]["inputs"]["bpm"] = 400
        errs = graphs.validate(g, self.oi)
        self.assertTrue(any("timesignature" in e for e in errs), errs)
        self.assertTrue(any("bpm" in e and "above" in e for e in errs), errs)

    def test_wrong_link_type(self):
        g = copy.deepcopy(self.g)
        g["8"]["inputs"]["negative"] = ["7", 0]   # a LATENT where CONDITIONING belongs
        self.assertTrue(any("expects CONDITIONING" in e for e in graphs.validate(g, self.oi)))


class TestRequirementsAndFit(unittest.TestCase):
    def test_requirements(self):
        m = stock()
        g = graphs.music_graph(m.music, "x", "", 120, 10, 1)
        r = graphs.requirements(g)
        self.assertIn("TextEncodeAceStepAudio1.5", r["nodes"])
        self.assertEqual(r["models"]["text_encoders"], {"qwen_0.6b_ace15.safetensors", "qwen_1.7b_ace15.safetensors"})
        self.assertEqual(r["models"]["diffusion_models"], {"acestep_v1.5_turbo.safetensors"})
        self.assertEqual(r["models"]["vae"], {"ace_1.5_vae.safetensors"})
        v = graphs.requirements(graphs.video_graph(m.video, "a.png", "x", 832, 480, 49, 1, "p"))
        self.assertEqual(len(v["models"]["diffusion_models"]), 2)
        self.assertEqual(len(v["models"]["loras"]), 2)

    def test_fit(self):
        self.assertEqual(graphs.fit(1328, 1328, graphs.VIDEO_AREA["480p"]), (624, 624))
        self.assertEqual(graphs.fit(1472, 1104, graphs.VIDEO_AREA["480p"]), (736, 544))
        for w, h in ((1104, 1472), (1664, 928), (100, 1000)):
            fw, fh = graphs.fit(w, h, graphs.VIDEO_AREA["720p"])
            self.assertEqual((fw % 16, fh % 16), (0, 0))


if __name__ == "__main__":
    unittest.main()
