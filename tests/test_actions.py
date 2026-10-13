"""WP3: create/delete flows and the ComfyUI client, against FakeComfy on temp roots (§4, §11, §12.6).

Never a real ComfyUI and never a GPU job. Every test also checks /free was never called.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import threading
import time
import unittest
import wave
from unittest import mock

from medialab import actions, comfy, legacy, media, models, store
from medialab.types import NotOwned
from tests.test_migration import make_mp4
from tests.util import FakeComfy, FakeJob, TempRoots, make_png, requires_ffmpeg

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "fixtures", "object_info_0.37.0.json"), encoding="utf-8") as _f:
    FIXTURE = json.load(_f)


class ActionCase(unittest.TestCase):
    # Most tests read simpler with ComfyUI's plain counters (img_00001_.png); TestNamesNeverRepeat runs the real
    # per-job prefixes.
    plain_prefixes = True

    def setUp(self):
        self.roots = TempRoots()
        self.addCleanup(self.roots.cleanup)
        self.fc = FakeComfy(object_info=FIXTURE, output_dir=self.roots.output, input_dir=self.roots.input).start()
        self.addCleanup(self.fc.stop)
        self.cfg = self.roots.config(comfy_url=self.fc.url)
        store.configure(self.cfg)
        media.configure(self.cfg)
        comfy.configure(self.cfg)
        comfy.POLL = 0.01
        actions.configure(self.cfg, models.load(self.cfg))
        if self.plain_prefixes:
            p = mock.patch.object(actions, "_prefix", lambda kind: f"{self.cfg.output_subdir}/{kind}")
            p.start()
            self.addCleanup(p.stop)
        self.job = FakeJob()

    def tearDown(self):
        self.assertFalse(self.fc.free_called, "ComfyUI /free must never be called")

    def graphs_sent(self):
        return [b["prompt"] for _, _, b in self.fc.calls("POST", "/prompt")]

    def upload_picture(self, name="photo.png") -> dict:
        data = make_png(w=8, h=8)
        return actions.ingest_upload(io.BytesIO(data), len(data), name, "image/png")


class TestNamesNeverRepeat(ActionCase):
    """Audit 2026-10-11 (medium): ComfyUI numbers outputs from the highest file left, so after a delete the next
    job took the deleted file's name and old cuts/calendar entries silently pointed at the new file."""
    plain_prefixes = False

    def setUp(self):
        super().setUp()
        self.fc.counter_from_disk = True

    def test_a_deleted_name_is_never_handed_out_again(self):
        kinds = (("img", lambda: actions.generate({"prompt": "a door"}, self.job)),
                 ("music", lambda: actions.music({"tags": "lofi", "seconds": 10}, self.job)))
        for kind, make in kinds:
            first = make()["ref"]["name"]
            self.assertRegex(first, rf"^{kind}_\d{{8}}-\d{{6}}_[0-9a-f]{{6}}_00001")
            actions.delete(("gen", first))
            again = make()["ref"]["name"]
            self.assertNotEqual(again, first)
            self.assertFalse(store.owned(("gen", first)), "the old ref must stay gone, not resolve to new work")

    def test_clip_and_poster_names_are_new(self):
        pic = actions.generate({"prompt": "a"}, self.job)
        clip = actions.animate({"start": pic["ref"]}, self.job)["ref"]["name"]
        actions.delete(("gen", clip))
        again = actions.animate({"start": pic["ref"]}, self.job)
        self.assertNotEqual(again["ref"]["name"], clip)
        self.assertTrue(again["ref"]["name"].startswith("clip_") and again["poster"].endswith(".png"))


class TestGenerate(ActionCase):
    def test_picture_with_reference(self):
        up = self.upload_picture("My holiday photo.png")
        ref = up["ref"]
        self.assertEqual(ref["root"], "in")
        res = actions.generate({"prompt": "a red door", "variant": "bf16", "refs": [ref], "steps": 8, "seed": 42, "size": "portrait"},
                               self.job)
        g = self.graphs_sent()[-1]
        self.assertEqual(g["8"]["inputs"]["filename_prefix"], "media_lab/img")
        self.assertEqual(g["r1"]["inputs"]["image"], f"media_lab/{ref['name']}")
        self.assertEqual((g["5"]["inputs"]["width"], g["5"]["inputs"]["height"]), (1104, 1472))
        self.assertEqual(res["ref"], {"root": "gen", "name": "img_00001_.png"})
        self.assertEqual(res["type"], "image")
        self.assertEqual(res["url"], "/m/gen/img_00001_.png")
        self.assertEqual((res["seed"], res["size"]), (42, "1104x1472"))
        sc = store.sidecar(("gen", "img_00001_.png"))
        self.assertEqual(sc["kind"], "image")
        self.assertIs(sc["commercial_ok"], False)
        self.assertEqual(sc["most_restrictive"], "qwen-image-nc")
        enc = [m for m in sc["models"] if m["role"] == "text_encoder"]
        self.assertEqual([(m["file"], m["licence"], m["commercial_ok"]) for m in enc],
                         [("qwen3vl_8b_int8_convrot.safetensors", "qwen-image-nc", False)])   # repack: qwen-research
        self.assertEqual(sc["prompt"], "a red door")
        self.assertEqual(sc["seed"], 42)
        self.assertEqual(sc["inputs"][0]["role"], "ref")
        self.assertIsNone(sc["inputs"][0]["commercial_ok"], "an upload's own snapshot is null")
        pid = sc["comfy"]["prompt_ids"][0]
        self.assertEqual(self.job.comfy_pids, [pid])
        self.assertEqual(store.ledger_pids(("gen", "img_00001_.png")), [pid])
        self.assertIn("queued", self.job.phases)

    def test_refuses_unowned_and_lab_refs(self):
        make_png(self.roots.path("out", "qwen_studio_00001_.png"))
        with self.assertRaises(NotOwned):
            actions.generate({"prompt": "x", "variant": "bf16",
                              "refs": [{"root": "out", "name": "qwen_studio_00001_.png"}]}, self.job)
        with self.assertRaises(ValueError):
            actions.load_value(("lab", "up_x.mp4"))
        with self.assertRaises(ValueError):
            actions.generate({"prompt": "   "}, self.job)
        self.assertEqual(self.graphs_sent(), [])

    def test_node_errors_reach_the_user(self):
        self.fc.node_errors = {"1": {"class_type": "UNETLoader", "errors": [
            {"message": "Value not in list", "details": "unet_name: 'x' not in [...]"}]}}
        with self.assertRaises(comfy.ComfyError) as cm:
            actions.generate({"prompt": "x"}, self.job)
        self.assertIn("Value not in list", str(cm.exception))
        self.assertIn("UNETLoader", str(cm.exception))


    def test_unknown_model_or_size_is_refused_not_swapped(self):
        for body in ({"prompt": "a", "variant": "nope"}, {"prompt": "a", "size": "bogus"}):
            with self.assertRaises(ValueError):
                actions.generate(body, self.job)
        self.assertEqual(self.fc.calls("POST", "/prompt"), [], "nothing reaches the GPU")


class TestAnimateAndMusic(ActionCase):
    def test_clip_from_picture(self):
        pic = actions.generate({"prompt": "a red door", "seed": 1, "variant": "bf16"}, self.job)
        res = actions.animate({"start": pic["ref"], "prompt": "slow push in", "seconds": "5", "quality": "480p",
                               "w": 1328, "h": 1328, "seed": 9}, self.job)
        g = self.graphs_sent()[-1]
        self.assertEqual(g["34"]["inputs"]["image"], "media_lab/img_00001_.png [output]")
        self.assertEqual(g["43"]["inputs"]["filename_prefix"], "media_lab/clip")
        self.assertEqual(g["35"]["inputs"]["length"], 81)
        self.assertEqual((g["35"]["inputs"]["width"], g["35"]["inputs"]["height"]), (624, 624))
        self.assertEqual(res["ref"], {"root": "gen", "name": "clip_00001.mp4"})
        self.assertEqual(res["type"], "video")
        self.assertEqual(res["poster"], "/m/gen/clip_00001.png")
        sc = store.sidecar(("gen", "clip_00001.mp4"))
        self.assertEqual(sc["kind"], "clip")
        self.assertEqual(sc["poster"], "clip_00001.png")
        self.assertIs(sc["commercial_ok"], False)
        self.assertEqual(sc["restricted_by"], ["start image img_00001_.png: Qwen-Image 2.1 (non-commercial)"])
        self.assertEqual(sc["params"]["frames"], 81)
        self.assertEqual({m["licence"] for m in sc["models"]}, {"apache-2.0"})

    def test_music(self):
        res = actions.music({"tags": "warm lo-fi", "lyrics": "", "bpm": 90, "seconds": 13.7, "seed": 5}, self.job)
        g = self.graphs_sent()[-1]
        from medialab import graphs
        self.assertEqual(graphs.validate(g, FIXTURE), [])
        self.assertEqual(g["10"]["class_type"], "SaveAudio")
        self.assertEqual(g["10"]["inputs"]["filename_prefix"], "media_lab/music")
        self.assertEqual(g["7"]["inputs"]["seconds"], 13.72)
        self.assertEqual(res["ref"], {"root": "gen", "name": "music_00001.flac"})
        self.assertIs(res["commercial_ok"], True)
        self.assertGreater(res["samples"], 0)
        sc = store.sidecar(("gen", "music_00001.flac"))
        self.assertEqual(sc["kind"], "music")
        self.assertEqual(sc["licences"], ["ace-step"])
        self.assertEqual(sc["params"]["bpm"], 90)
        self.assertEqual(store.tracks()[0]["ref"], res["ref"])

    def test_music_falls_back_to_advanced_saver(self):
        del self.fc.object_info["SaveAudio"]
        comfy.clear_cache()
        actions.music({"tags": "x", "seconds": 10}, self.job)
        g = self.graphs_sent()[-1]
        self.assertEqual(g["10"]["class_type"], "SaveAudioAdvanced")
        self.assertEqual(g["10"]["inputs"]["format"], "flac")

    def test_music_rejects_bad_key(self):
        with self.assertRaises(ValueError):
            actions.music({"tags": "x", "keyscale": "H major"}, self.job)


class TestDelete(ActionCase):
    def test_removes_only_ledger_pids(self):
        pic = actions.generate({"prompt": "a red door"}, self.job)
        ref = ("gen", pic["ref"]["name"])
        ours = store.ledger_pids(ref)
        self.assertEqual(len(ours), 1)
        # another tool's history, including one that names our file in its graph
        self.fc.add_history("other-1", {"9": {"images": [{"filename": "ComfyUI_00001_.png", "subfolder": "",
                                                          "type": "output"}]}})
        self.fc.add_history("other-2", {"9": {"images": [{"filename": "x.png", "subfolder": "", "type": "output"}]}},
                            graph={"1": {"class_type": "LoadImage", "inputs": {"image": f"media_lab/{ref[1]} [output]"}}})
        out = actions.delete(ref)
        self.assertEqual(out["removed"], [ref[1]])
        self.assertEqual(out["history_deleted"], 1)
        self.assertEqual(out["used_in"], [])
        posts = self.fc.calls("POST", "/history")
        self.assertEqual([b for _, _, b in posts], [{"delete": ours}])
        self.assertIn("other-1", self.fc.history)
        self.assertIn("other-2", self.fc.history)
        self.assertNotIn(ours[0], self.fc.history)
        self.assertFalse(os.path.exists(self.roots.path("gen", ref[1])))
        self.assertFalse(store.owned(ref))
        self.assertEqual(store.ledger_pids(ref), [])
        with self.assertRaises(NotOwned):
            actions.delete(ref)

    def test_clip_delete_takes_poster(self):
        pic = actions.generate({"prompt": "a"}, self.job)
        clip = actions.animate({"start": pic["ref"]}, self.job)
        out = actions.delete(("gen", clip["ref"]["name"]))
        self.assertEqual(sorted(out["removed"]), ["clip_00001.mp4", "clip_00001.png"])
        self.assertTrue(os.path.exists(self.roots.path("gen", "img_00001_.png")))

    def test_legacy_without_pid_sends_no_history_delete(self):
        make_png(self.roots.path("out", "qwen_studio_00001_.png"))
        make_png(self.roots.path("out", "qwen2_t2i_00001_.png"))
        legacy.adopt_all(self.cfg, store, None, models=models.load(self.cfg))
        out = actions.delete(("out", "qwen_studio_00001_.png"))
        self.assertEqual(out, {"removed": ["qwen_studio_00001_.png"], "history_deleted": 0, "used_in": []})
        self.assertEqual(self.fc.calls("POST", "/history"), [])
        self.assertTrue(os.path.exists(self.roots.path("out", "qwen2_t2i_00001_.png")))

    def test_unowned_is_never_deleted(self):
        make_png(self.roots.path("gen", "img_00099_.png"))
        with self.assertRaises(NotOwned):
            actions.delete(("gen", "img_00099_.png"))
        self.assertTrue(os.path.exists(self.roots.path("gen", "img_00099_.png")))


class TestUpload(ActionCase):
    def test_picture_goes_to_comfy_input(self):
        res = self.upload_picture("A" * 100 + ".png")
        self.assertEqual(res["type"], "image")
        self.assertEqual(res["ref"]["root"], "in")
        self.assertRegex(res["ref"]["name"], r"^ref_\d{8}-\d{6}_[0-9a-f]{6}\.png$")
        self.assertEqual(len(self.fc.uploads), 1)
        self.assertEqual(self.fc.uploads[0][1], "media_lab")
        sc = store.sidecar(("in", res["ref"]["name"]))
        self.assertEqual(sc["kind"], "upload")
        self.assertIsNone(sc["commercial_ok"])
        self.assertEqual(sc["licences"], ["upload"])
        self.assertLessEqual(len(sc["label"]), 60)
        self.assertNotIn("A" * 61, json.dumps(sc))
        self.assertEqual(os.listdir(store.data_path("tmp")), [])

    def test_picture_upload_when_comfy_is_off(self):
        self.fc.stop()
        res = self.upload_picture()
        self.assertTrue(os.path.isfile(self.roots.path("in", res["ref"]["name"])))
        self.fc.start()   # tearDown checks the (new) fake; nothing called /free

    def test_bad_type_and_size(self):
        exe = b"MZ\x90\x00" + b"\x00" * 200
        with self.assertRaises(actions.BadType):
            actions.ingest_upload(io.BytesIO(exe), len(exe), "virus.png", "image/png")
        png = make_png()
        with self.assertRaises(actions.BadType):
            actions.ingest_upload(io.BytesIO(png), len(png), "x.mp3", "audio/mpeg")
        cfg = self.roots.config(comfy_url=self.fc.url, max_image_mb=0)
        actions.configure(cfg, models.load(cfg))
        with self.assertRaises(actions.TooLarge):
            actions.ingest_upload(io.BytesIO(png), len(png), "x.png", "image/png")
        with self.assertRaises(ValueError):
            actions.ingest_upload(io.BytesIO(png[:10]), len(png), "x.png", "image/png")
        self.assertEqual(os.listdir(store.data_path("tmp")), [])
        self.assertEqual(os.listdir(store.data_path("sidecars", "in")), [])

    @requires_ffmpeg()
    def test_audio_upload(self):
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(48000)
            w.writeframes(b"\x00\x01" * 48000 * 2)
        data = buf.getvalue()
        res = actions.ingest_upload(io.BytesIO(data), len(data), "song.wav", "audio/wav")
        self.assertEqual(res["ref"]["root"], "lab")
        self.assertRegex(res["ref"]["name"], r"^up_\d{8}-\d{6}_[0-9a-f]{6}\.wav$")
        self.assertAlmostEqual(res["seconds"], 2.0, places=2)
        self.assertEqual(store.tracks()[0]["title"], "song.wav")


@requires_ffmpeg()
class TestUploadDemuxer(ActionCase):
    """An ID3 prefix followed by a DASH manifest must not let ffmpeg read a file named inside it (§12)."""

    def test_id3_dash_manifest_refused(self):
        secret = os.path.join(self.roots.base, "owner_secret.m4a")
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=440:d=3", "-c:a",
                        "aac", secret], check=True, timeout=60)
        mpd = ('<?xml version="1.0"?><MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" '
               'mediaPresentationDuration="PT3S" minBufferTime="PT1S" '
               'profiles="urn:mpeg:dash:profile:isoff-on-demand:2011"><Period><AdaptationSet mimeType="audio/mp4">'
               '<Representation id="a" bandwidth="128000" codecs="mp4a.40.2" audioSamplingRate="44100">'
               f'<BaseURL>file://{secret}</BaseURL></Representation></AdaptationSet></Period></MPD>')
        data = b"ID3\x04\x00\x00\x00\x00\x00\x00" + mpd.encode()
        self.assertEqual(media.sniff(data[:64]), "mp3")
        with self.assertRaises(actions.BadType):
            actions.ingest_upload(io.BytesIO(data), len(data), "song.mp3", "audio/mpeg")
        self.assertEqual(os.listdir(self.roots.dir("lab")), [])
        self.assertEqual(os.listdir(store.data_path("tmp")), [])
        # and the whitelist holds for any probe or decode of such a file already on disk
        bad = os.path.join(self.roots.base, "bad.mp3")
        with open(bad, "wb") as f:
            f.write(data)
        with self.assertRaises(media.FFmpegError):
            media.probe(bad)
        with self.assertRaises(media.FFmpegError):
            media.conform_audio(bad)

    def test_wan_size_keeps_animate_sizes(self):
        self.assertEqual(actions._wan_size(1168, 800), (1168, 800))     # graphs.fit of a 2048x1400 picture
        self.assertEqual(actions._wan_size(1080, 1920), (720, 1280))
        self.assertEqual(actions._wan_size(64, 48), (64, 48))


@requires_ffmpeg()
class TestExtend(ActionCase):
    def _clip(self, path, frames=16, color="red"):
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"color=c={color}:s=64x48:r=16", "-frames:v", str(frames), "-c:v", "libx264", "-pix_fmt",
                        "yuv420p", path], check=True, timeout=60)

    def test_segment_of_another_size_is_scaled_to_the_source(self):
        """A 1080x1920-style source gets a Wan-sized segment; the stitch scales it back instead of failing."""
        src_ref = ("lab", "up_20261007-210000_a1b2c4.mp4")
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=72x40:r=16",
                        "-frames:v", "16", "-c:v", "libx264", "-pix_fmt", "yuv420p", self.roots.path(*src_ref)],
                       check=True, timeout=60)
        from medialab import provenance
        store.put_sidecar(src_ref, provenance.make_sidecar("upload", src_ref, models=[],
                                                           inputs=[provenance.upload_entry(("lab", "x"))]))

        def on_prompt(graph, pid):
            self._clip(os.path.join(self.roots.dir("gen"), "seg_00001.mp4"), frames=49, color="blue")
            return {"43": {"gifs": [{"filename": "seg_00001.mp4", "subfolder": "media_lab", "type": "output"}]}}
        self.fc.on_prompt = on_prompt
        res = actions.extend({"src": store.ref_json(src_ref), "seconds": "3"}, self.job)
        p = media.probe(self.roots.path("gen", res["ref"]["name"]))
        self.assertEqual((p.w, p.h, p.frames), (72, 40, 16 + 48))

    def test_cancel_during_stitch_discards_the_result(self):
        """Review r2: the stitch gets the job (a cancel kills it) and a cancel during it discards dest."""
        src_ref = ("lab", "up_20261007-210000_a1b2c5.mp4")
        self._clip(self.roots.path(*src_ref))
        from medialab import jobs, provenance
        store.put_sidecar(src_ref, provenance.make_sidecar("upload", src_ref, models=[],
                                                           inputs=[provenance.upload_entry(("lab", "x"))]))

        def on_prompt(graph, pid):
            self._clip(os.path.join(self.roots.dir("gen"), "seg_00001.mp4"), frames=49, color="blue")
            return {"43": {"gifs": [{"filename": "seg_00001.mp4", "subfolder": "media_lab", "type": "output"}]}}
        self.fc.on_prompt = on_prompt
        real, seen = media.stitch_extension, []

        def stitch(src, seg, dest, job=None):
            seen.append(job)
            real(src, seg, dest, job=job)
            self.job.cancel()           # the owner pressed Cancel while it ran
        media.stitch_extension = stitch
        self.addCleanup(setattr, media, "stitch_extension", real)
        with self.assertRaises(jobs.Cancelled):
            actions.extend({"src": store.ref_json(src_ref), "seconds": "3"}, self.job)
        self.assertEqual(seen, [self.job])
        self.assertEqual([n for n in os.listdir(self.roots.dir("gen")) if n.startswith(("ext_", "seg_"))], [])

    def test_stitch_timeout_scales_with_length(self):
        self.assertEqual(media.stitch_timeout(10, 3), 300)
        self.assertGreaterEqual(media.stitch_timeout(120, 3), 120 / 0.32)   # 2 min 4K at the measured 0.32x
        self.assertGreaterEqual(media.stitch_timeout(600, 5), 600 / 0.32)
        self.assertEqual(media.stitch_timeout(None, None), 300)

    def test_extend_stitches_and_cleans_up(self):
        src = self.roots.path("lab", "up_20261007-210000_a1b2c3.mp4")
        self._clip(src)
        from medialab import provenance
        store.put_sidecar(("lab", "up_20261007-210000_a1b2c3.mp4"),
                          provenance.make_sidecar("upload", ("lab", "up_20261007-210000_a1b2c3.mp4"), models=[],
                                                  inputs=[provenance.upload_entry(("lab", "x"))]))

        def on_prompt(graph, pid):
            name = "seg_00001.mp4"
            self._clip(os.path.join(self.roots.dir("gen"), name), frames=49, color="blue")
            make_png(os.path.join(self.roots.dir("gen"), "seg_00001.png"))
            return {"43": {"gifs": [{"filename": name, "subfolder": "media_lab", "type": "output"}]}}
        self.fc.on_prompt = on_prompt
        self.fc.add_history("someone-else", {"1": {"images": [{"filename": "a.png", "subfolder": "", "type": "output"}]}})
        res = actions.extend({"src": {"root": "lab", "name": "up_20261007-210000_a1b2c3.mp4"}, "seconds": "3"},
                             self.job)
        name = res["ref"]["name"]
        self.assertRegex(name, r"^ext_\d{8}-\d{6}_[0-9a-f]{6}\.mp4$")
        self.assertEqual(res["ref"]["root"], "gen")
        g = self.graphs_sent()[-1]
        self.assertEqual(g["43"]["inputs"]["filename_prefix"], "media_lab/seg")
        self.assertTrue(g["34"]["inputs"]["image"].startswith("media_lab/lastframe_"))
        self.assertEqual((g["35"]["inputs"]["width"], g["35"]["inputs"]["height"]), (64, 48))
        p = media.probe(self.roots.path("gen", name))
        self.assertEqual(p.frames, 16 + 48)
        # temporaries gone, their history gone, other history kept
        self.assertFalse(os.path.exists(self.roots.path("gen", "seg_00001.mp4")))
        self.assertFalse(os.path.exists(self.roots.path("gen", "seg_00001.png")))
        self.assertEqual([n for n in os.listdir(self.roots.dir("in")) if n.startswith("lastframe_")], [])
        seg_pid = self.job.comfy_pids[0]
        self.assertEqual([b for _, _, b in self.fc.calls("POST", "/history")], [{"delete": [seg_pid]}])
        self.assertIn("someone-else", self.fc.history)
        self.assertEqual(store.ledger_pids(("gen", "seg_00001.mp4")), [])
        sc = store.sidecar(("gen", name))
        self.assertEqual(sc["kind"], "extension")
        self.assertIsNone(sc["commercial_ok"], "an upload's restriction carries through")
        self.assertEqual(sc["inputs"][0]["role"], "source")
        self.assertTrue(os.path.isfile(self.roots.path("gen", sc["poster"])))


class TestComfyClient(ActionCase):
    def test_cancel_queued_running_and_foreign(self):
        self.fc.auto_complete = False
        mine = comfy.submit({"1": {"class_type": "SaveImage", "inputs": {}}})
        comfy.cancel(mine)
        self.assertEqual(self.fc.calls("POST", "/queue")[-1][2], {"delete": [mine]})
        running = comfy.submit({"1": {"class_type": "SaveImage", "inputs": {}}})
        self.fc.run(running)
        comfy.cancel(running)
        self.assertEqual(self.fc.calls("POST", "/interrupt")[-1][2], {"prompt_id": running})
        n_posts = len(self.fc.calls("POST"))
        comfy.cancel("not-ours-and-not-queued")
        self.assertEqual(len(self.fc.calls("POST")), n_posts, "a pid that is neither queued nor running is left alone")

    def test_wait_reports_failure_and_cancel(self):
        self.fc.auto_complete = False
        pid = comfy.submit({"1": {"class_type": "SaveImage", "inputs": {}}})
        self.fc.fail(pid, "CUDA out of memory")
        with self.assertRaises(comfy.ComfyError) as cm:
            comfy.wait(pid, 5)
        self.assertIn("out of memory", str(cm.exception))
        pid2 = comfy.submit({"1": {"class_type": "SaveImage", "inputs": {}}})
        job = FakeJob()
        job.cancel()
        with self.assertRaises(comfy.Cancelled):
            comfy.wait(pid2, 5, job=job)
        self.assertEqual(self.fc.calls("POST", "/queue")[-1][2], {"delete": [pid2]})
        with self.assertRaises(comfy.ComfyError):
            comfy.wait(comfy.submit({"1": {"class_type": "SaveImage", "inputs": {}}}), 0.05)

    def test_wait_fails_fast_when_comfyui_restarts_mid_job(self):
        # audit 2026-10-11 (medium): a restart forgets the in-memory queue, and the job used to poll until its
        # 10-30 minute limit, holding the GPU queue, then said "timed out"
        self.fc.auto_complete = False
        pid = comfy.submit({"1": {"class_type": "SaveImage", "inputs": {}}})
        self.fc.run(pid)
        with self.fc.lock:            # the restart: queue and history start empty
            self.fc.running.clear()
        t0 = time.monotonic()
        with self.assertRaises(comfy.ComfyError) as cm:
            comfy.wait(pid, 600)
        self.assertLess(time.monotonic() - t0, 5)
        self.assertIn("no longer knows this job", str(cm.exception))

    def test_wait_keeps_waiting_while_the_prompt_is_queued_or_running(self):
        self.fc.auto_complete = False
        pid = comfy.submit({"1": {"class_type": "SaveImage", "inputs": {}}})
        threading.Timer(0.3, self.fc.run, (pid,)).start()
        threading.Timer(0.6, self.fc.complete, (pid,)).start()
        self.assertEqual(comfy.wait(pid, 10)[0]["kind"], "images")

    def test_wait_gives_up_when_comfyui_stays_unreachable(self):
        self.fc.auto_complete = False
        pid = comfy.submit({"1": {"class_type": "SaveImage", "inputs": {}}})
        self.fc.stop()
        self.addCleanup(setattr, comfy, "DOWN_LIMIT", comfy.DOWN_LIMIT)
        comfy.DOWN_LIMIT = 0.2
        with self.assertRaises(comfy.ComfyError) as cm:
            comfy.wait(pid, 600)
        self.assertIn("stopped answering", str(cm.exception))
        self.fc.start()

    def test_off(self):
        self.fc.stop()
        with self.assertRaises(comfy.ComfyOff):
            comfy.ensure()
        with self.assertRaises(comfy.ComfyOff):
            comfy.submit({})
        self.assertEqual(comfy.forget(["a"]), 0)
        self.assertEqual(comfy.object_infos(["SaveAudio"]), {})
        self.fc.start()

    def test_history_find_output_is_exact(self):
        self.fc.add_history("a", {"1": {"images": [{"filename": "img_00001_.png", "subfolder": "media_lab"}]}})
        self.fc.add_history("b", {"1": {"images": [{"filename": "img_00001_.png.bak", "subfolder": "media_lab"}]}})
        self.assertEqual(comfy.history_find_output("img_00001_.png", "media_lab"), "a")
        self.assertIsNone(comfy.history_find_output("img_00001_.png", ""))
        self.assertIsNone(comfy.history_find_output("img_00001", "media_lab"))

    def test_provisional_ledger_line(self):
        pid = comfy.submit({"1": {"class_type": "SaveImage", "inputs": {}}}, kind="image")
        with open(store.data_path("ledger.jsonl")) as f:
            lines = [json.loads(x) for x in f if x.strip()]
        self.assertEqual(lines[-1]["pid"], pid)
        self.assertEqual(lines[-1]["refs"], [])

    def test_object_info_cache(self):
        comfy.clear_cache()
        comfy.object_info("SaveAudio")
        comfy.object_info("SaveAudio")
        self.assertEqual(len(self.fc.calls("GET", "/object_info/SaveAudio")), 1)


class TestMp4FixtureHelper(unittest.TestCase):
    def test_make_mp4_roundtrip(self):
        with TempRoots() as r:
            p = r.path("out", "t.mp4")
            make_mp4(p, {"prompt": '{"1": {"class_type": "X"}}'})
            self.assertEqual(legacy.read_mp4_prompt(p), {"1": {"class_type": "X"}})


if __name__ == "__main__":
    unittest.main()
