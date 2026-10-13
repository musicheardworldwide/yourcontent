"""WP3: adoption of pre-v1 files (§13, §14 test_migration). Everything runs on temp copies.

The owner's real files are never touched: the optional real-sample test (MEDIA_LAB_LEGACY_SAMPLE=<folder>)
copies a few of them into a temp tree first and checks the originals are byte- and mtime-identical after.
"""
from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import unittest

from medialab import legacy, media, models, store
from tests.util import FakeComfy, TempRoots, make_png, requires_ffmpeg, sha256_file

PIC_UNET = "qwen-image-2.1-local-int8.safetensors"   # a local Qwen-Image variant: licence by name pattern


def image_graph(seed=1215546937, refs=()):
    g = {"1": {"class_type": "UNETLoader", "inputs": {"unet_name": PIC_UNET, "weight_dtype": "default"}},
         "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_8b_int8_convrot.safetensors",
                                                      "type": "qwen_image", "device": "default"}},
         "3": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_2.1_vae_bf16.safetensors"}},
         "4": {"class_type": "TextEncodeQwenImage21", "inputs": {"clip": ["2", 0], "prompt": "a red door",
                                                                "negative_prompt": "blurry", "resolution": 1328}},
         "6": {"class_type": "KSampler", "inputs": {"model": ["1", 0], "positive": ["4", 0], "negative": ["4", 1],
                                                    "seed": seed, "steps": 30}},
         "8": {"class_type": "SaveImage", "inputs": {"images": ["7", 0], "filename_prefix": "qwen_studio"}}}
    for i, r in enumerate(refs, 1):
        g[f"r{i}"] = {"class_type": "LoadImage", "inputs": {"image": r}}
        g["4"]["inputs"][f"images.image_{i}"] = [f"r{i}", 0]
    return g


def clip_graph(start="qwen_studio_00005_.png [output]"):
    g = {}
    for side, nid in (("high", 10), ("low", 20)):
        g[str(nid)] = {"class_type": "UNETLoader",
                       "inputs": {"unet_name": f"wan2.2_i2v_{side}_noise_14B_fp8_scaled.safetensors"}}
        g[str(nid + 1)] = {"class_type": "LoraLoaderModelOnly", "inputs": {
            "model": [str(nid), 0], "lora_name": f"wan2.2_i2v_lightx2v_4steps_lora_v1_{side}_noise.safetensors"}}
    g.update({
        "30": {"class_type": "CLIPLoader", "inputs": {"clip_name": "umt5_xxl_fp8_e4m3fn_scaled.safetensors"}},
        "31": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["30", 0], "text": "slow push in"}},
        "32": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["30", 0], "text": "static"}},
        "33": {"class_type": "VAELoader", "inputs": {"vae_name": "wan_2.1_vae.safetensors"}},
        "34": {"class_type": "LoadImage", "inputs": {"image": start}},
        "35": {"class_type": "WanImageToVideo", "inputs": {"positive": ["31", 0], "negative": ["32", 0],
                                                          "start_image": ["34", 0]}},
        "40": {"class_type": "KSamplerAdvanced", "inputs": {"positive": ["35", 0], "negative": ["35", 1],
                                                           "noise_seed": 1013216627}},
    })
    return g


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def make_mp4(path: str, tags: dict[str, str] | None = None) -> None:
    """A tiny MP4 box structure with ffmpeg-style mdta metadata (no media; enough for the tag reader)."""
    body = b""
    if tags:
        keys = b"".join(_box(b"mdta", k.encode()) for k in tags)
        keys_box = _box(b"keys", b"\x00\x00\x00\x00" + struct.pack(">I", len(tags)) + keys)
        items = b"".join(_box(struct.pack(">I", i), _box(b"data", b"\x00\x00\x00\x01\x00\x00\x00\x00" + v.encode()))
                         for i, v in enumerate(tags.values(), 1))
        hdlr = _box(b"hdlr", b"\x00" * 8 + b"mdta" + b"\x00" * 13)
        meta = _box(b"meta", b"\x00\x00\x00\x00" + hdlr + keys_box + _box(b"ilst", items))
        body = _box(b"udta", meta)
    data = _box(b"ftyp", b"isom\x00\x00\x02\x00isomiso2avc1mp41") + _box(b"mdat", b"\x00" * 32) + \
        _box(b"moov", _box(b"mvhd", b"\x00" * 100) + body)
    with open(path, "wb") as f:
        f.write(data)


class MigrationCase(unittest.TestCase):
    def setUp(self):
        self.roots = TempRoots(input_subdir="qwen_studio")
        self.addCleanup(self.roots.cleanup)
        self.cfg = self.roots.config()
        store.configure(self.cfg)
        media.configure(self.cfg)
        self.models = models.load(self.cfg)
        r = self.roots
        t0 = 1_760_000_000
        # legacy
        make_png(r.path("out", "qwen_studio_00001_.png"), w=8, h=6, text={"prompt": image_graph()})
        make_png(r.path("out", "qwen_studio_00005_.png"), w=4, h=4,
                 text={"prompt": image_graph(seed=5, refs=["qwen_studio_00001_.png [output]",
                                                            "qwen_studio/ref_20261008-004506_debc46.png"])})
        make_png(r.path("out", "qwen_studio_00009_.png"), w=4, h=4)               # no graph at all
        make_mp4(r.path("out", "qwen_video_00001.mp4"), {"prompt": json.dumps(clip_graph()), "encoder": "Lavf"})
        make_png(r.path("out", "qwen_video_00001.png"), text={"prompt": clip_graph()})
        make_mp4(r.path("out", "qwen_video_ext_20261008-000542_3cea20.mp4"), {"encoder": "Lavf61"})
        shutil.copyfile(r.path("out", "qwen_video_00001.png"), r.path("out", "qwen_video_ext_20261008-000542_3cea20.png"))
        make_png(r.path("in", "ref_20261008-004506_debc46.png"))
        # neighbours that must never be adopted
        for n in ("qwen2_t2i_00001_.png", "oub_qwen2_00001_.png", "qwen_studio_x.png", "qwen_video_00002.png",
                  "qwen_studio_00001_.png.bak"):
            make_png(r.path("out", n))
        os.makedirs(r.path("out", "wan22_i2v"))
        make_png(os.path.join(r.path("out", "wan22_i2v"), "qwen_studio_00002_.png"))
        os.symlink(r.path("out", "qwen_studio_00001_.png"), r.path("out", "qwen_studio_00003_.png"))
        make_png(r.path("in", "lastframe_20261008-000000_abcdef.png"))
        for d in (r.output, r.uploads):
            for base, _, files in os.walk(d):
                for n in files:
                    p = os.path.join(base, n)
                    if not os.path.islink(p):
                        os.utime(p, (t0, t0))
        self.before = r.snapshot(r.output)
        self.before_in = r.snapshot(r.input)

    def assertUntouched(self):
        self.assertEqual(self.roots.snapshot(self.roots.output), self.before)
        self.assertEqual(self.roots.snapshot(self.roots.input), self.before_in)

    def adopt(self, comfy=None):
        return legacy.adopt_all(self.cfg, store, comfy, models=self.models)


class TestScan(MigrationCase):
    def test_only_legacy_files(self):
        found = [(i.root, i.name, i.kind, i.poster) for i in legacy.scan(self.cfg)]
        self.assertEqual(found, [
            ("in", "ref_20261008-004506_debc46.png", "upload", None),
            ("out", "qwen_studio_00001_.png", "image", None),
            ("out", "qwen_studio_00005_.png", "image", None),
            ("out", "qwen_studio_00009_.png", "image", None),
            ("out", "qwen_video_00001.mp4", "clip", "qwen_video_00001.png"),
            ("out", "qwen_video_ext_20261008-000542_3cea20.mp4", "extension",
             "qwen_video_ext_20261008-000542_3cea20.png"),
        ])

    def test_metadata_readers(self):
        g = legacy.read_png_prompt(self.roots.path("out", "qwen_studio_00001_.png"))
        self.assertEqual(g["6"]["inputs"]["seed"], 1215546937)
        g = legacy.read_mp4_prompt(self.roots.path("out", "qwen_video_00001.mp4"))
        self.assertEqual(g["34"]["inputs"]["image"], "qwen_studio_00005_.png [output]")
        self.assertIsNone(legacy.read_mp4_prompt(self.roots.path("out", "qwen_video_ext_20261008-000542_3cea20.mp4")))
        self.assertIsNone(legacy.read_png_prompt(self.roots.path("out", "qwen_studio_00009_.png")))
        self.assertEqual(legacy.png_size(self.roots.path("out", "qwen_studio_00001_.png")), (8, 6))

    def test_load_ref(self):
        c = self.cfg
        self.assertEqual(legacy.load_ref("qwen_studio_00005_.png [output]", c), ("out", "qwen_studio_00005_.png"))
        self.assertEqual(legacy.load_ref("media_lab/img_00001_.png [output]", c), ("gen", "img_00001_.png"))
        self.assertEqual(legacy.load_ref("qwen_studio/ref_x.png", c), ("in", "ref_x.png"))
        self.assertIsNone(legacy.load_ref("oub/ref.png", c))
        self.assertIsNone(legacy.load_ref("other/x.png [output]", c))


class TestAdopt(MigrationCase):
    def test_adoption(self):
        res = self.adopt()
        self.assertEqual(sorted(res["adopted"]), sorted([
            "ref_20261008-004506_debc46.png", "qwen_studio_00001_.png", "qwen_studio_00005_.png",
            "qwen_studio_00009_.png", "qwen_video_00001.mp4", "qwen_video_ext_20261008-000542_3cea20.mp4"]))
        self.assertEqual(res["skipped"], [])
        self.assertEqual(len(res["manifest"]), 6)
        self.assertUntouched()
        # neighbours stay unowned
        for n in ("qwen2_t2i_00001_.png", "oub_qwen2_00001_.png", "qwen_studio_x.png", "qwen_video_00002.png",
                  "qwen_studio_00003_.png"):
            self.assertFalse(store.owned(("out", n)), n)
        self.assertFalse(store.owned(("in", "lastframe_20261008-000000_abcdef.png")))
        self.assertFalse(store.owned(("out", "qwen_video_00001.png")), "a poster is never a picture")

        pic = store.sidecar(("out", "qwen_studio_00001_.png"))
        self.assertEqual(pic["kind"], "image")
        self.assertIs(pic["commercial_ok"], False)
        self.assertEqual(pic["most_restrictive"], "qwen-image-nc")
        self.assertEqual(pic["seed"], 1215546937)
        self.assertEqual(pic["prompt"], "a red door")
        self.assertEqual(pic["negative"], "blurry")
        self.assertEqual(pic["legacy"]["recovered_from"], "png:prompt")
        self.assertEqual(pic["legacy"]["pattern"], "qwen_studio")
        self.assertTrue(pic["inferred"])
        self.assertEqual(pic["sha256"], sha256_file(self.roots.path("out", "qwen_studio_00001_.png")))
        self.assertEqual((pic["probe"]["w"], pic["probe"]["h"]), (8, 6))
        self.assertEqual(pic["created"], "2025-10-09T08:53:20Z", "created is the file's own mtime")
        self.assertIn(PIC_UNET, [m["file"] for m in pic["models"]])

        five = store.sidecar(("out", "qwen_studio_00005_.png"))
        roles = {(i["role"], i["name"]) for i in five["inputs"]}
        self.assertEqual(roles, {("ref", "qwen_studio_00001_.png"), ("ref", "ref_20261008-004506_debc46.png")})
        self.assertIs(five["commercial_ok"], False)

        bare = store.sidecar(("out", "qwen_studio_00009_.png"))
        self.assertIsNone(bare["commercial_ok"])
        self.assertEqual(bare["legacy"]["recovered_from"], "none")
        self.assertEqual(bare["restricted_by"], ["made before provenance tracking; source unknown"])

        clip = store.sidecar(("out", "qwen_video_00001.mp4"))
        self.assertEqual(clip["kind"], "clip")
        self.assertEqual(clip["poster"], "qwen_video_00001.png")
        self.assertEqual(clip["legacy"]["recovered_from"], "mp4:prompt")
        self.assertEqual([(i["role"], i["name"], i["commercial_ok"]) for i in clip["inputs"]],
                         [("start", "qwen_studio_00005_.png", False)])
        self.assertIs(clip["commercial_ok"], False, "clip lineage resolves to false through its start picture")
        self.assertEqual(clip["restricted_by"], ["start image qwen_studio_00005_.png: Qwen-Image 2.1 (non-commercial)"])
        self.assertEqual(clip["seed"], 1013216627)
        self.assertEqual(clip["prompt"], "slow push in")

        ext = store.sidecar(("out", "qwen_video_ext_20261008-000542_3cea20.mp4"))
        self.assertEqual(ext["kind"], "extension")
        self.assertEqual(ext["legacy"]["recovered_from"], "poster:prompt")
        self.assertEqual(ext["poster"], "qwen_video_ext_20261008-000542_3cea20.png")
        self.assertIs(ext["commercial_ok"], False)

        up = store.sidecar(("in", "ref_20261008-004506_debc46.png"))
        self.assertEqual(up["kind"], "upload")
        self.assertIsNone(up["commercial_ok"])
        self.assertEqual(up["most_restrictive"], "upload")

        # what the page lists: every adopted visual asset; posters reachable only through their parent
        recent = {r["ref"]["name"] for r in store.recent()}
        self.assertEqual(recent, set(res["adopted"]))
        self.assertEqual(store.poster_parent(("out", "qwen_video_00001.png")), ("out", "qwen_video_00001.mp4"))

    def test_idempotent(self):
        first = self.adopt()
        sidecars = {n: store.sidecar(("in" if n.startswith("ref_") else "out", n)) for n in first["adopted"]}
        second = self.adopt()
        self.assertEqual(second["adopted"], [])
        self.assertEqual(sorted(second["skipped"]), sorted(first["adopted"]))
        self.assertEqual(second["manifest"], first["manifest"])
        for n, sc in sidecars.items():
            self.assertEqual(store.sidecar(("in" if n.startswith("ref_") else "out", n)), sc)
        self.assertUntouched()

    def test_history_pids_by_exact_match_read_only(self):
        oi = {}
        with FakeComfy(object_info=oi) as fc:
            fc.add_history("pid-ours", {"8": {"images": [{"filename": "qwen_studio_00001_.png", "subfolder": "",
                                                          "type": "output"}]}})
            fc.add_history("pid-other-tool", {"8": {"images": [{"filename": "qwen_studio_00001_.png",
                                                                "subfolder": "elsewhere", "type": "output"}]}})
            fc.add_history("pid-substring", {"9": {"images": [{"filename": "xqwen_video_00001.mp4",
                                                               "subfolder": "", "type": "output"}]},
                                             "1": {"text": ["qwen_video_00001.mp4"]}})
            from medialab import comfy
            comfy.configure(fc.url)
            self.adopt(comfy)
            self.assertEqual(store.ledger_pids(("out", "qwen_studio_00001_.png")), ["pid-ours"])
            self.assertEqual(store.ledger_pids(("out", "qwen_video_00001.mp4")), [])
            pic = store.sidecar(("out", "qwen_studio_00001_.png"))
            self.assertEqual(pic["comfy"]["prompt_ids"], ["pid-ours"])
            self.assertEqual(fc.calls("POST"), [], "adoption only reads ComfyUI")
            self.assertFalse(fc.free_called)
            self.assertEqual(len(fc.history), 3)
        self.assertUntouched()


@requires_ffmpeg()
class TestRealMp4Tag(MigrationCase):
    """The prompt tag as ffmpeg itself writes it (VHS passes -metadata prompt=...)."""

    def test_ffmpeg_written_tag(self):
        p = self.roots.path("out", "qwen_video_00007.mp4")
        g = clip_graph()
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "color=c=black:s=32x32:r=16", "-frames:v", "4", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-movflags", "use_metadata_tags", "-metadata", "prompt=" + json.dumps(g), p],
                       check=True, timeout=60)
        self.assertEqual(legacy.read_mp4_prompt(p), g)


SAMPLE = os.environ.get("MEDIA_LAB_LEGACY_SAMPLE")


@unittest.skipUnless(SAMPLE and os.path.isdir(SAMPLE), "set MEDIA_LAB_LEGACY_SAMPLE to a pre-v1 output folder")
class TestRealSampleCopy(unittest.TestCase):
    """A few real pre-v1 files, copied to a temp tree. The originals are only read (and checked after)."""

    def test_real_copy(self):
        names = sorted(n for n in os.listdir(SAMPLE) if legacy.PATTERNS[1][2].fullmatch(n)
                       or legacy.PATTERNS[2][2].fullmatch(n) or legacy.PATTERNS[3][2].fullmatch(n))
        picked = [n for n in names if not n.endswith(".png") or n.startswith("qwen_studio_")][:12]
        posters = [n[:-4] + ".png" for n in picked if n.endswith(".mp4") and os.path.isfile(os.path.join(SAMPLE, n[:-4] + ".png"))]
        originals = {}
        for n in picked + posters:
            p = os.path.join(SAMPLE, n)
            st = os.stat(p)
            originals[n] = (st.st_size, st.st_mtime_ns, sha256_file(p))
        with TempRoots(input_subdir="qwen_studio") as roots:
            for n in picked + posters:
                shutil.copy2(os.path.join(SAMPLE, n), roots.path("out", n))
            before = roots.snapshot(roots.output)
            cfg = roots.config()
            store.configure(cfg)
            media.configure(cfg)
            res = legacy.adopt_all(cfg, store, None, models=models.load(cfg))
            self.assertEqual(roots.snapshot(roots.output), before)
            self.assertEqual(len(res["adopted"]), len(picked))
            for n in picked:
                sc = store.sidecar(("out", n))
                self.assertIsNotNone(sc, n)
                if n.startswith("qwen_studio_"):
                    self.assertEqual(sc["legacy"]["recovered_from"], "png:prompt", n)
                    self.assertIs(sc["commercial_ok"], False, n)
                else:
                    self.assertIn(sc["legacy"]["recovered_from"], ("mp4:prompt", "poster:prompt"), n)
                    starts = [i for i in sc["inputs"] if i["role"] == "start"]
                    if starts and not starts[0]["missing"]:
                        self.assertIs(sc["commercial_ok"], False, n)
        for n, facts in originals.items():
            p = os.path.join(SAMPLE, n)
            st = os.stat(p)
            self.assertEqual((st.st_size, st.st_mtime_ns, sha256_file(p)), facts, f"original {n} changed")


if __name__ == "__main__":
    unittest.main()
