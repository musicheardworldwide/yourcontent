"""WP3: feature health (§6 /api/health, §9 Health, §14 test_features). Pure, against the fixture."""
from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest

from medialab import features, media, models

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "fixtures", "object_info_0.37.0.json"), encoding="utf-8") as _f:
    FIXTURE = json.load(_f)
FF_OK = {"version": "7.1.5", "missing_filters": [], "missing_encoders": []}


def drop_file(oi: dict, ct: str, inp: str, name: str) -> dict:
    oi = copy.deepcopy(oi)
    spec = oi[ct]["input"]["required"][inp]
    spec[0].remove(name)
    return oi


class TestFeatures(unittest.TestCase):
    def setUp(self):
        self.m = models.load(None)

    def test_everything_ok_with_the_fixture(self):
        f = features.compute(FIXTURE, self.m, FF_OK)
        self.assertTrue(f["picture"]["ok"], f["picture"])
        self.assertTrue(f["picture"]["variants"]["bf16"]["ok"])
        self.assertTrue(f["clip"]["ok"], f["clip"])
        self.assertTrue(f["extend"]["ok"])
        self.assertTrue(f["music"]["ok"], f["music"])
        self.assertEqual(f["music"]["saver"], "SaveAudio")
        self.assertTrue(f["export"]["ok"])
        self.assertEqual(f["export"]["missing_filters"], [])

    def test_missing_ace_file_disables_music_and_names_it(self):
        oi = drop_file(FIXTURE, "DualCLIPLoader", "clip_name1", "qwen_0.6b_ace15.safetensors")
        f = features.compute(oi, self.m, FF_OK)
        self.assertFalse(f["music"]["ok"])
        self.assertTrue(any("qwen_0.6b_ace15.safetensors" in r for r in f["music"]["missing"]), f["music"])
        self.assertTrue(f["picture"]["ok"])
        oi = drop_file(FIXTURE, "UNETLoader", "unet_name", "acestep_v1.5_turbo.safetensors")
        f = features.compute(oi, self.m, FF_OK)
        self.assertEqual(f["music"]["missing"], ["acestep_v1.5_turbo.safetensors (models/diffusion_models, 4.79 GB)"
                                                 " · get it: python3 -m medialab get-models music"])
        self.assertEqual(features.download_bytes(f["music"]["missing"]), 4787825604)

    def test_missing_ace_encoder_node_gives_version_hint(self):
        oi = {k: v for k, v in FIXTURE.items() if k != "TextEncodeAceStepAudio1.5"}
        f = features.compute(oi, self.m, FF_OK)
        self.assertFalse(f["music"]["ok"])
        self.assertTrue(any("TextEncodeAceStepAudio1.5" in r and "update ComfyUI" in r for r in f["music"]["missing"]))

    def test_saver_fallback(self):
        oi = {k: v for k, v in FIXTURE.items() if k != "SaveAudio"}
        f = features.compute(oi, self.m, FF_OK)
        self.assertTrue(f["music"]["ok"], f["music"])
        self.assertEqual(f["music"]["saver"], "SaveAudioAdvanced")
        oi = {k: v for k, v in oi.items() if k != "SaveAudioAdvanced"}
        f = features.compute(oi, self.m, FF_OK)
        self.assertFalse(f["music"]["ok"])
        self.assertIsNone(f["music"]["saver"])

    def test_missing_vhs_disables_clips_and_extend(self):
        oi = {k: v for k, v in FIXTURE.items() if k != "VHS_VideoCombine"}
        f = features.compute(oi, self.m, FF_OK)
        self.assertFalse(f["clip"]["ok"])
        self.assertFalse(f["extend"]["ok"])
        self.assertTrue(any("VideoHelperSuite" in r for r in f["clip"]["missing"]))
        self.assertTrue(f["picture"]["ok"])

    def test_missing_unet_disables_only_that_variant(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "models.json")
            with open(p, "w") as f:
                json.dump({"image": {"variants": {"fast": {"label": "fast one",
                                                           "unet": "not_downloaded.safetensors"}}}}, f)
            m = models.load(None, local_path=p)
        f = features.compute(FIXTURE, m, FF_OK)
        self.assertTrue(f["picture"]["ok"])
        self.assertTrue(f["picture"]["variants"]["bf16"]["ok"])
        self.assertFalse(f["picture"]["variants"]["fast"]["ok"])
        self.assertEqual(f["picture"]["variants"]["fast"]["missing"],
                         ["not_downloaded.safetensors (models/diffusion_models)"])

    def test_local_models_json_fingerprint(self):
        """The loader reports the sha256 of the models.json bytes it actually read (a stale bind mount shows)."""
        import hashlib
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "models.json")
            raw = b'{"image": {"variants": {}}}'
            with open(p, "wb") as f:
                f.write(raw)
            m = models.load(None, local_path=p)
        self.assertEqual(m.local_file["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(m.local_file["bytes"], len(raw))
        self.assertTrue(m.local_file["mtime"].endswith("Z"))
        self.assertIsNone(models.load(None).local_file)

    def test_comfy_off(self):
        f = features.compute({}, self.m, FF_OK)
        for k in ("picture", "clip", "extend", "music"):
            self.assertFalse(f[k]["ok"], k)
        self.assertEqual(f["clip"]["missing"], [features.OFF])
        self.assertTrue(f["export"]["ok"], "export works without ComfyUI")

    def test_ffmpeg_missing_filter_or_binary(self):
        f = features.compute(FIXTURE, self.m, {"version": "6.0", "missing_filters": ["xfade"],
                                               "missing_encoders": []})
        self.assertFalse(f["export"]["ok"])
        self.assertEqual(f["export"]["missing_filters"], ["xfade"])
        f = features.compute(FIXTURE, self.m, {"version": None, "missing_filters": [], "missing_encoders": []})
        self.assertFalse(f["export"]["ok"])
        self.assertFalse(f["extend"]["ok"])


class TestFfmpegCaps(unittest.TestCase):
    """The caps parse lives in media (WP1); WP3 checks it against the filters the export graphs need."""
    FILTERS = " T.. trim              V->V       Pick one continuous section from the input.\n" \
              " ... xfade             VV->V      Cross fade one video with another.\n" \
              " TSC scale             V->V       Scale the input video size.\n"
    ENCODERS = " V....D libx264              libx264 H.264\n A....D aac                  AAC\n"

    def test_parse(self):
        caps = media.parse_caps("ffmpeg version 7.1.5 Copyright", self.FILTERS, self.ENCODERS)
        self.assertEqual(caps["version"], "7.1.5")
        self.assertNotIn("trim", caps["missing_filters"])
        self.assertNotIn("xfade", caps["missing_filters"])
        self.assertIn("gblur", caps["missing_filters"])
        self.assertNotIn("libx264", caps["missing_encoders"])

    def test_same_filter_list(self):
        self.assertEqual(set(media.REQUIRED_FILTERS), set(features.REQUIRED_FILTERS))


class TestFixtureSubset(unittest.TestCase):
    def test_reduces_model_lists(self):
        oi = copy.deepcopy(FIXTURE)
        oi["UNETLoader"]["input"]["required"]["unet_name"][0].append("someones_private_model.safetensors")
        oi["LoadImage"]["input"]["required"]["image"][0] = ["private_photo.png"]
        oi["SomethingElse"] = {"input": {}}
        sub = features.fixture_subset(oi, set(models.load(None).known_files()))
        text = json.dumps(sub)
        self.assertNotIn("someones_private_model", text)
        self.assertNotIn("private_photo", text)
        self.assertNotIn("SomethingElse", sub)
        self.assertEqual(set(sub), set(FIXTURE))


if __name__ == "__main__":
    unittest.main()
