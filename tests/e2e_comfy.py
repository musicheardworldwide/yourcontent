"""Opt-in end-to-end test against a real ComfyUI (§14 e2e_comfy). Never in CI; runs GPU jobs.

    MEDIA_LAB_E2E=1 MEDIA_LAB_COMFY_URL=http://<comfy>:8188 \
    MEDIA_LAB_COMFY_OUTPUT=<ComfyUI output folder> MEDIA_LAB_UPLOADS=<ComfyUI input>/<subdir> \
    MEDIA_LAB_INPUT_SUBDIR=<subdir> [MEDIA_LAB_E2E_VIDEO=1] \
    python3 -m unittest tests.e2e_comfy -v

The file name does not start with "test", so `unittest discover` never picks it up.

What it does: a 512x512 4-step picture; (with MEDIA_LAB_E2E_VIDEO=1) a 480p 3 s clip from it and a 3 s Extend
of that clip; a 30 s ACE-Step track (not silent, 48 kHz, "[Instrumental]" accepted); a SaveAudioAdvanced
`format: "flac"` job; /interrupt by prompt_id (a wrong id leaves ours running, ours stops it); a 9:16 export of
picture + clip + extension + music. MEDIA_LAB_E2E_MODELS=<models.json> uses that model table;
MEDIA_LAB_E2E_REPORT=<file> writes the timings as JSON. Then it deletes everything it made and checks that only its own prompt ids left
ComfyUI's history and that no other file in the output folder changed. DATA is a temp folder, and new outputs go
to their own subfolder (`media_lab_e2e`), so the owner's files are never in play.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest

E2E = os.environ.get("MEDIA_LAB_E2E") == "1" and bool(os.environ.get("MEDIA_LAB_COMFY_URL"))
VIDEO = os.environ.get("MEDIA_LAB_E2E_VIDEO") == "1"
SUBDIR = "media_lab_e2e"


def _tree(top: str, skip: str) -> dict:
    out = {}
    for base, dirs, files in os.walk(top):
        dirs[:] = [d for d in dirs if os.path.join(base, d) != skip]
        for n in files:
            p = os.path.join(base, n)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            out[os.path.relpath(p, top)] = (st.st_size, st.st_mtime_ns)
    return out


@unittest.skipUnless(E2E, "set MEDIA_LAB_E2E=1 and MEDIA_LAB_COMFY_URL to run against a real ComfyUI")
class TestEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from medialab import actions, comfy, config, media, models, store
        cls.tmp = tempfile.mkdtemp(prefix="medialab-e2e-")
        env = dict(os.environ, MEDIA_LAB_DATA=os.path.join(cls.tmp, "data"), MEDIA_LAB_OUTPUT_SUBDIR=SUBDIR,
                   MEDIA_LAB_ADOPT_LEGACY="0")
        cls.cfg = config.load(env)
        if os.environ.get("MEDIA_LAB_E2E_MODELS"):   # e.g. the owner's DATA/config/models.json
            os.makedirs(os.path.join(cls.tmp, "data", "config"), exist_ok=True)
            shutil.copyfile(os.environ["MEDIA_LAB_E2E_MODELS"], os.path.join(cls.tmp, "data", "config", "models.json"))
        store.configure(cls.cfg)
        media.configure(cls.cfg)
        comfy.configure(cls.cfg)
        actions.configure(cls.cfg, models.load(cls.cfg))
        comfy.ensure()
        cls.gen = os.path.join(os.path.abspath(cls.cfg.comfy_output), SUBDIR)
        cls.before_files = _tree(os.path.abspath(cls.cfg.comfy_output), cls.gen)
        cls.before_in = _tree(os.path.abspath(cls.cfg.uploads), "")
        cls.before_history = set(comfy.request("/history", timeout=30))
        cls.made: list = []

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def job(self):
        from tests.util import FakeJob
        return FakeJob()

    def timed(self, key, fn):
        import time
        t0 = time.monotonic()
        out = fn()
        self.timings[key] = round(time.monotonic() - t0, 1)
        return out

    def test_all(self):
        import time
        from medialab import actions, comfy, graphs, media, projects, provenance, store, export
        self.timings: dict = {}
        notes: dict = {}
        ours: set = set()
        # 1. picture, 512x512, 4 steps (the default variant of the model table in use)
        saved = graphs.SIZES["square"]
        graphs.SIZES["square"] = (512, 512)
        try:
            pic = self.timed("picture_512_4steps", lambda: actions.generate(
                {"prompt": "a small red house on a hill, morning light", "steps": 4, "size": "square"},
                self.job()))
        finally:
            graphs.SIZES["square"] = saved
        pref = (pic["ref"]["root"], pic["ref"]["name"])
        self.made.append(pref)
        p = media.probe(store.resolve(pref))
        self.assertEqual((p.w, p.h), (512, 512))
        self.assertIs(store.sidecar(pref)["commercial_ok"], False)
        items = [{"id": "i_000001", "kind": "still", "src": pic["ref"], "dur": 2.0, "join": "fade"}]

        # 2. clip and 3. extend (optional: a couple of minutes of GPU)
        if VIDEO:
            clip = self.timed("clip_3s_480p", lambda: actions.animate(
                {"start": pic["ref"], "seconds": "3", "quality": "480p", "w": 512, "h": 512}, self.job()))
            cref = (clip["ref"]["root"], clip["ref"]["name"])
            self.made.append(cref)
            self.assertEqual(media.probe(store.resolve(cref)).frames, 49)
            self.assertIs(store.sidecar(cref)["commercial_ok"], False)
            items.append({"id": "i_000002", "kind": "clip", "src": clip["ref"], "in": 0, "out": None,
                          "join": "cut"})
            ext = self.timed("extend_3s", lambda: actions.extend(
                {"src": clip["ref"], "seconds": "3", "prompt": "the camera keeps moving slowly"}, self.job()))
            xref = (ext["ref"]["root"], ext["ref"]["name"])
            self.made.append(xref)
            xf = media.probe(store.resolve(xref)).frames
            notes["extend_frames"] = xf
            self.assertEqual(xf, 49 + 49 - 1, "extension = clip + new segment without the repeated frame")
            self.assertIs(store.sidecar(xref)["commercial_ok"], False)
            items.append({"id": "i_000003", "kind": "clip", "src": ext["ref"], "in": 0, "out": None,
                          "join": "cut"})

        # 4. a 30 s ACE-Step track, instrumental (empty lyrics are sent as "[Instrumental]")
        track = self.timed("music_30s", lambda: actions.music(
            {"tags": "warm lo-fi hip hop, mellow piano, no vocals", "lyrics": "", "seconds": 30}, self.job()))
        tref = (track["ref"]["root"], track["ref"]["name"])
        self.made.append(tref)
        self.assertEqual(store.sidecar(tref)["params"]["lyrics"], "")
        tp = media.probe(store.resolve(tref))
        notes["music_rate"], notes["music_samples"], notes["music_codec"] = tp.audio_rate, tp.audio_samples, tp.acodec
        self.assertEqual(tp.audio_rate, 48000)
        self.assertLessEqual(abs(tp.seconds - 30.0), 0.1)
        loud = media.ebur128(store.resolve(tref))
        notes["music_lufs"] = loud.get("I")
        self.assertGreater(loud["I"] or -99, -40, "the track is not silent")

        # 5. SaveAudioAdvanced with format "flac" (§9) is accepted
        m = actions._m()
        g = graphs.music_graph(m.music, "short test tone", "", 120, 5, 7, saver="SaveAudioAdvanced",
                               prefix=f"{SUBDIR}/music")
        pid = comfy.submit(g, kind="music")
        outs = self.timed("music_5s_saveaudioadvanced", lambda: comfy.wait(pid, 600))
        flac = [o for o in outs if o["kind"] == "audio"]
        self.assertTrue(flac and flac[0]["filename"].endswith(".flac"), outs)
        adv = ("gen", flac[0]["filename"])
        store.ledger_add(pid, "music", [adv])
        store.put_sidecar(adv, provenance.make_sidecar("music", adv, models=[], inputs=[], pids=[pid]))
        self.made.append(adv)

        # 6. /interrupt with prompt_id (§12.6): a wrong id leaves our running prompt alone; ours stops it
        v = m.image_variants()[m.default_variant]
        g = graphs.image_graph(v, "a quiet street at dusk", "", 1328, 1328, 40, 11, [], f"{SUBDIR}/irq")
        ipid = comfy.submit(g, kind="image")
        ours.add(ipid)
        deadline = time.monotonic() + 120
        running = lambda: any(len(x) > 1 and x[1] == ipid for x in comfy.request("/queue")["queue_running"])
        while not running() and time.monotonic() < deadline:
            time.sleep(0.25)
        self.assertTrue(running(), "the interrupt test prompt never started")
        comfy.request("/interrupt", {"prompt_id": "00000000-0000-4000-8000-000000000000"})
        time.sleep(3)
        notes["interrupt_wrong_id_left_ours_running"] = running()
        self.assertTrue(running(), "an interrupt naming another prompt id stopped ours")
        t0 = time.monotonic()
        comfy.cancel(ipid)
        with self.assertRaises(comfy.Cancelled):
            comfy.wait(ipid, 120)
        self.timings["interrupt_to_stopped"] = round(time.monotonic() - t0, 1)
        comfy.forget([ipid])

        # 7. a 9:16 auto export of still (+ clip + extension) + music
        proj = projects.create("e2e cut")
        proj["items"] = items
        proj["music"] = {"src": track["ref"], "volume": 1.0, "fade_in": 0.5, "fade_out": 1.0, "offset": 0.0}
        proj["export"] = {"preset": "9:16", "fit": "auto", "fps": 24, "fade": 0.5}
        rev = projects.save(proj["id"], proj["rev"], proj)
        res = self.timed("export_final_9x16", lambda: export.run_render(projects.load(proj["id"]), rev, "final",
                                                                        self.job()))
        eref = (res["ref"]["root"], res["ref"]["name"])
        self.made.append(eref)
        ep = media.probe(store.resolve(eref))
        notes["export_frames"], notes["export_checks"] = ep.frames, res["checks"]
        self.assertEqual((ep.w, ep.h), (1080, 1920))
        self.assertEqual(ep.rate, 24)
        self.assertEqual(ep.acodec, "aac")
        self.assertTrue(res["checks"]["ok"], res["checks"])
        self.assertLessEqual(abs((res["checks"].get("lufs") or 0) + 14), 1.5)
        self.assertIs(res["commercial_ok"], False)
        with open(store.resolve(eref), "rb") as f:
            head = f.read(1 << 16)
        self.assertLess(head.find(b"moov"), head.find(b"mdat") if b"mdat" in head else 1 << 30, "faststart")
        projects.remove(proj["id"])

        # 8. delete everything through the app's delete; only our pids leave /history; nothing else changed
        for ref in reversed(self.made):
            ours.update(store.ledger_pids(ref))
            actions.delete(ref)
        after_history = set(comfy.request("/history", timeout=30))
        self.assertEqual(self.before_history - after_history, set(),
                         "an entry that existed before the test was removed")
        self.assertEqual(after_history & ours, set(), "one of our prompt ids is still in history")
        out = os.path.abspath(self.cfg.comfy_output)
        self.assertEqual(_tree(out, self.gen), self.before_files)
        self.assertEqual(_tree(os.path.abspath(self.cfg.uploads), ""), self.before_in)
        if os.path.isdir(self.gen):
            leftovers = os.listdir(self.gen)
            self.assertEqual(leftovers, [], f"left in {SUBDIR}/: {leftovers}")
            os.rmdir(self.gen)
        report = {"timings_s": self.timings, "notes": notes, "deleted": [list(r) for r in self.made],
                  "history_pids_removed": len(ours)}
        print(json.dumps(report, indent=1, default=str))
        if os.environ.get("MEDIA_LAB_E2E_REPORT"):
            with open(os.environ["MEDIA_LAB_E2E_REPORT"], "w", encoding="utf-8") as f:
                json.dump(report, f, indent=1, default=str)

if __name__ == "__main__":
    unittest.main()
