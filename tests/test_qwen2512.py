"""Qwen-Image-2512 as the shipped picture default (plan D1, 2026-10-12): Apache-2.0, so its pictures are labelled
commercial OK; Qwen-Image 2.1 stays selectable and non-commercial, and anything holding a 2.1 picture stays
non-commercial."""
from __future__ import annotations

import json
import os
import unittest

from medialab import actions, getmodels, graphs, models, provenance as pv, store
from tests.test_actions import ActionCase

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "fixtures", "object_info_0.37.0.json"), encoding="utf-8") as _f:
    FIXTURE = json.load(_f)


class TestTable(unittest.TestCase):
    def setUp(self):
        self.m = models.load(None)

    def test_default_is_2512_and_2_1_stays_selectable(self):
        self.assertEqual(self.m.default_variant, "2512")
        v = self.m.variant(None)
        self.assertEqual((v["graph"], v["unet"], v["clip"], v["vae"]),
                         ("qwen-image-2512", "qwen_image_2512_fp8_e4m3fn.safetensors",
                          "qwen_2.5_vl_7b_fp8_scaled.safetensors", "qwen_image_vae.safetensors"))
        self.assertEqual((v["steps"], v["cfg"], graphs.max_refs(v)), (50, 4.0, 0))
        old = self.m.variant("bf16")
        self.assertEqual((old["graph"], old["licence"], graphs.max_refs(old)), ("qwen-image-2.1", "qwen-image-nc", 6))
        self.assertEqual(self.m.alias("qwen-image-2.1"), "bf16")

    def test_licence_labels(self):
        self.assertIs(self.m.licence("qwen-image-2512")["commercial_ok"], True)
        for f in ("qwen_image_2512_fp8_e4m3fn.safetensors", "qwen_2.5_vl_7b_fp8_scaled.safetensors",
                  "qwen_image_vae.safetensors", "qwen-image-2512-Q8_0.gguf"):   # the last: an owner's own quant
            self.assertIs(self.m.licence(self.m.licence_for_file(f))["commercial_ok"], True, f)
        for f in ("qwen_image_2.1_bf16.safetensors", "qwen-image-2.1-Q8_0.gguf",
                  "qwen3vl_8b_int8_convrot.safetensors"):
            self.assertIs(self.m.licence(self.m.licence_for_file(f))["commercial_ok"], False, f)
        rows = {r["model"]: r["commercial_ok"] for r in self.m.public_licences()}
        self.assertIs(rows["Qwen-Image-2512"], True)
        self.assertIs(rows["Qwen-Image 2.1"], False)

    def test_provenance_of_each_model(self):
        agg = pv.aggregate(pv.model_entries(self.m, self.m.used_image("2512")), [])
        self.assertIs(agg["commercial_ok"], True)
        self.assertEqual(agg["restricted_by"], [])
        ids = [e[0] for e in self.m.used_image("2512")]
        self.assertEqual(ids, ["qwen-image-2512:2512", "qwen2.5-vl-7b", "qwen-image-vae"])
        self.assertIs(pv.aggregate(pv.model_entries(self.m, self.m.used_image("bf16")), [])["commercial_ok"], False)

    def test_manifest_has_the_2512_files_from_the_comfy_org_repack(self):
        man = getmodels.load_manifest()
        self.assertEqual(man["repos"]["qwen2512"], {"id": "Comfy-Org/Qwen-Image_ComfyUI",
                                                     "rev": "1f12b17be14c89b026c51a91d67c32f84bb047bc"})
        f = man["files"]
        self.assertEqual(f["qwen_image_2512_fp8_e4m3fn.safetensors"]["bytes"], 20430679144)
        self.assertEqual(f["qwen_2.5_vl_7b_fp8_scaled.safetensors"]["bytes"], 9384670680)
        self.assertEqual(f["qwen_image_vae.safetensors"]["bytes"], 253806246)
        self.assertTrue(getmodels.url_for(man, "qwen_image_vae.safetensors").endswith(
            "/Comfy-Org/Qwen-Image_ComfyUI/resolve/1f12b17be14c89b026c51a91d67c32f84bb047bc/split_files/vae/"
            "qwen_image_vae.safetensors"))


class TestGraph(unittest.TestCase):
    def test_matches_comfyuis_template_and_validates(self):
        m = models.load(None)
        g = graphs.image_graph(m.variant("2512"), "a lighthouse", "", 1328, 1328, 50, 7, ["ignored.png"], "yc/img")
        self.assertEqual(graphs.validate(g, FIXTURE), [])
        types = sorted(n["class_type"] for n in g.values())
        self.assertEqual(types, sorted(["UNETLoader", "CLIPLoader", "VAELoader", "CLIPTextEncode", "CLIPTextEncode",
                                        "ModelSamplingAuraFlow", "EmptySD3LatentImage", "KSampler", "VAEDecode",
                                        "SaveImage"]))   # no LoadImage: 2512 is text-to-image only
        ks = g["8"]["inputs"]
        self.assertEqual((ks["steps"], ks["cfg"], ks["sampler_name"], ks["scheduler"]), (50, 4.0, "euler", "simple"))
        self.assertEqual(g["6"]["inputs"]["shift"], 3.1)
        self.assertEqual(g["2"]["inputs"]["type"], "qwen_image")
        self.assertEqual(g["5"]["inputs"]["text"], graphs.QWEN_2512_NEGATIVE)   # empty negative: the model's own
        self.assertEqual(graphs.requirements(g)["models"], {
            "diffusion_models": {"qwen_image_2512_fp8_e4m3fn.safetensors"},
            "text_encoders": {"qwen_2.5_vl_7b_fp8_scaled.safetensors"}, "vae": {"qwen_image_vae.safetensors"}})

    def test_2_1_graph_is_unchanged(self):
        m = models.load(None)
        g = graphs.image_graph(m.variant("bf16"), "a", "", 1024, 1024, 30, 1, ["r.png"], "yc/img")
        self.assertEqual(g["4"]["class_type"], "TextEncodeQwenImage21")
        self.assertEqual(g["r1"]["inputs"]["image"], "r.png")
        self.assertEqual(graphs.validate(g, FIXTURE), [])


class TestGenerate2512(ActionCase):
    def test_picture_is_commercial_ok_and_a_clip_from_it_too(self):
        pic = actions.generate({"prompt": "a red door", "seed": 3}, self.job)
        g = self.graphs_sent()[-1]
        self.assertEqual(g["8"]["inputs"]["steps"], 50)            # the variant's recommended steps
        sc = store.sidecar(("gen", pic["ref"]["name"]))
        self.assertIs(sc["commercial_ok"], True)
        self.assertEqual(sc["params"]["variant"], "2512")
        self.assertEqual({m["licence"] for m in sc["models"]}, {"qwen-image-2512", "apache-2.0"})
        clip = actions.animate({"start": pic["ref"], "seconds": "3", "quality": "480p", "w": 1328, "h": 1328},
                               self.job)
        self.assertIs(store.sidecar(("gen", clip["ref"]["name"]))["commercial_ok"], True)

    def test_references_are_refused_before_the_gpu(self):
        up = self.upload_picture()
        with self.assertRaises(ValueError) as cm:
            actions.generate({"prompt": "x", "refs": [up["ref"]]}, self.job)
        self.assertIn("Qwen-Image 2.1", str(cm.exception))
        self.assertEqual(self.graphs_sent(), [])

    def test_anything_holding_a_2_1_picture_stays_non_commercial(self):
        new = actions.generate({"prompt": "a"}, self.job)
        old = actions.generate({"prompt": "b", "variant": "bf16"}, self.job)
        self.assertIs(store.sidecar(("gen", old["ref"]["name"]))["commercial_ok"], False)
        ins = [pv.input_entry("item", ("gen", r["ref"]["name"]), store.sidecar(("gen", r["ref"]["name"])))
               for r in (new, old)]
        agg = pv.aggregate([], ins)
        self.assertIs(agg["commercial_ok"], False)
        self.assertEqual(agg["most_restrictive"], "qwen-image-nc")
        self.assertIs(pv.aggregate([], ins[:1])["commercial_ok"], True)
        clip = actions.animate({"start": old["ref"], "seconds": "3", "quality": "480p", "w": 1328, "h": 1328},
                               self.job)
        self.assertIs(store.sidecar(("gen", clip["ref"]["name"]))["commercial_ok"], False)


if __name__ == "__main__":
    unittest.main()
