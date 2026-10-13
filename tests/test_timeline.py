"""timeline: the pure planner (§7, §14 WP1). No ffmpeg, no files."""
from __future__ import annotations

import unittest
from fractions import Fraction

from medialab import timeline as tl
from medialab.timeline import RHU
from tests.wp1_fakes import (A, B, C, D, EXAMPLE_PATHS, EXAMPLE_PROBES, EXAMPLE_WAVS, M, aprobe, example_project,
                             iprobe, ref, vprobe)


def clip(iid, r, a=0, b=None, join="cut", **kw):
    return {"id": iid, "kind": "clip", "src": ref(r), "in": a, "out": b, "join": join, **kw}


def still(iid, r, dur=2.5, join="cut", **kw):
    return {"id": iid, "kind": "still", "src": ref(r), "dur": dur, "join": join, **kw}


def proj(items, fps=24, fade=0.5, music=None, preset="9:16", fit="auto"):
    return {"items": items, "music": music, "export": {"preset": preset, "fit": fit, "fps": fps, "fade": fade}}


class TestRounding(unittest.TestCase):
    def test_rhu_is_half_up_never_bankers(self):
        self.assertEqual(RHU(Fraction(1, 2)), 1)
        self.assertEqual(RHU(Fraction(5, 2)), 3)        # round() would give 2
        self.assertEqual(RHU(Fraction(147, 2)), 74)     # 73.5
        self.assertEqual(RHU(Fraction(-1, 2)), 0)
        self.assertEqual(RHU(Fraction(-3, 2)), -1)
        self.assertEqual(RHU(Fraction(7, 3)), 2)
        self.assertEqual(RHU(2.5), 3)
        self.assertEqual(RHU("0.5"), 1)

    def test_even(self):
        self.assertEqual(tl.even(Fraction(1080, 3)), 360)
        self.assertEqual(tl.even(Fraction(1350, 3)), 450)
        self.assertEqual(tl.even(1665), 1666)           # 832.5 -> 833
        self.assertEqual(tl.even(Fraction(1920, 24)), 80)
        self.assertEqual(tl.even(Fraction(1080, 24)), 46)  # 22.5 -> 23

    def test_dec(self):
        self.assertEqual(tl.dec(Fraction(62, 24), 6), "2.583333")
        self.assertEqual(tl.dec(Fraction(110, 24), 6), "4.583333")
        self.assertEqual(tl.dec(Fraction(1, 2), 6), "0.500000")
        self.assertEqual(tl.dec(Fraction(5, 1000000) / 10 * 5, 6), "0.000003")  # 0.0000025 -> half up
        self.assertEqual(tl.dec(Fraction(-1594, 100), 2), "-15.94")
        self.assertEqual(tl.dec(Fraction(176000, 48000), 1), "3.7")


class TestItemFrames(unittest.TestCase):
    def test_clips(self):
        r = Fraction(16)
        self.assertEqual([tl.item_frames(n, r, 24) for n in (49, 46, 96)], [74, 69, 144])
        self.assertEqual([tl.item_frames(n, r, 30) for n in (49, 46)], [92, 86])

    def test_stills(self):
        self.assertEqual(tl.still_frames(2.5, 24), 60)
        self.assertEqual(tl.still_frames(2.5, 30), 75)
        p = tl.plan(proj([still("s", B, 2.5)], fps=30), {B: iprobe(10, 10)})
        self.assertEqual(p.items[0].D, 75)

    def test_reorder_keeps_lengths(self):
        items = [clip("a", A, join="fade"), still("b", B, join="fade"), clip("c", C, 16, 112), clip("d", D)]
        p1 = tl.plan(proj(items), EXAMPLE_PROBES)
        p2 = tl.plan(proj([items[3], items[1], items[0], items[2]]), EXAMPLE_PROBES)
        self.assertEqual({i.id: i.D for i in p1.items}, {i.id: i.D for i in p2.items})

    def test_trim_clamps_to_source(self):
        p = tl.plan(proj([clip("a", A, 40, 400)]), {A: vprobe(624, 624, 49)})
        self.assertEqual((p.items[0].src_in, p.items[0].src_out), (40, 49))
        p = tl.plan(proj([clip("a", A, 48, None)]), {A: vprobe(624, 624, 49)})
        self.assertEqual((p.items[0].src_in, p.items[0].src_out), (47, 49))   # keeps >= 2 frames


class TestCrossfades(unittest.TestCase):
    def test_clamp_never_overlaps(self):
        # a 0.5 s still (12 frames) between two 0.5 s fades
        X, w = tl.clamp_xfades([74, 12, 74], [12, 12, 0])
        self.assertEqual(X, [11, 0, 0])
        self.assertEqual(len(w), 2)
        for D, want in (([74, 12, 74], [12, 12, 0]), ([30, 30, 30, 30], [24, 24, 24, 0]),
                        ([5, 5, 5], [12, 12, 0]), ([74, 60, 144, 74], [12, 12, 0, 0])):
            X, _ = tl.clamp_xfades(D, want)
            for k in range(len(D)):
                prev = X[k - 1] if k else 0
                self.assertLessEqual(prev + X[k], D[k] - 1, (D, X))
                if k < len(D) - 1:
                    self.assertLessEqual(X[k], D[k + 1] - 1)
            self.assertEqual(X[-1], 0)

    def test_short_fade_becomes_cut(self):
        X, w = tl.clamp_xfades([2, 3], [12, 0])
        self.assertEqual(X, [0, 0])
        self.assertEqual(w, ["crossfade after item 1 became a cut (item too short)"])
        X, w = tl.clamp_xfades([3, 3], [12, 0])
        self.assertEqual(X, [2, 0])
        self.assertIn("shortened", w[0])

    def test_unclamped_example(self):
        X, w = tl.clamp_xfades([74, 60, 144, 74], [12, 12, 0, 0])
        self.assertEqual((X, w), ([12, 12, 0, 0], []))

    def test_plan_still_between_fades(self):
        p = tl.plan(proj([clip("a", A, join="fade"), still("b", B, 0.5, join="fade"), clip("c", C)]),
                    EXAMPLE_PROBES)
        self.assertEqual([i.X_after for i in p.items], [11, 0, 0])
        self.assertTrue(any("item 2 became a cut" in w for w in p.warnings))


class TestWorkedExample(unittest.TestCase):
    def setUp(self):
        self.p = tl.plan(example_project(), EXAMPLE_PROBES, EXAMPLE_WAVS, paths=EXAMPLE_PATHS)

    def test_frames(self):
        p = self.p
        self.assertEqual([i.D for i in p.items], [74, 60, 144, 74])
        self.assertEqual([i.start for i in p.items], [0, 62, 110, 254])
        self.assertEqual([i.X_after for i in p.items], [12, 12, 0, 0])
        self.assertEqual((p.T, p.T_samples, p.F), (328, 656000, 24))
        self.assertEqual([i.kind for i in p.items], ["clip", "still", "clip", "clip"])
        self.assertEqual(p.items[2].src_in, 16)
        self.assertEqual(p.items[0].path, EXAMPLE_PATHS[A])
        self.assertEqual(p.warnings, ())

    def test_music(self):
        m = self.p.music
        self.assertEqual((m.skip, m.delay, m.end, m.fade_in, m.fade_out), (96000, 0, 656000, 24000, 72000))
        self.assertEqual(m.end - m.fade_out, 584000)
        self.assertEqual(m.target_i, -15.94)
        self.assertEqual(m.wav, EXAMPLE_WAVS[M][0])

    def test_offsets_round_trip(self):
        for it in self.p.items[1:]:
            self.assertEqual(RHU(Fraction(tl.dec(Fraction(it.start, 24), 6)) * 24), it.start)

    def test_auto_fit_blurs_all_at_9x16(self):
        p = tl.plan(example_project(fit="auto"), EXAMPLE_PROBES, EXAMPLE_WAVS)
        self.assertEqual({i.fit for i in p.items}, {"blur"})

    def test_plan_json(self):
        j = tl.plan_json(self.p)
        self.assertEqual((j["fps"], j["frames"], j["seconds"], j["samples"]), (24, 328, "13.666667", 656000))
        self.assertEqual(j["items"][0], {"id": "i_9c2a1f", "kind": "clip", "start": 0, "frames": 74,
                                         "xfade_after": 12, "src_in": 0, "src_out": 49, "seconds": "3.083333",
                                         "fit": "fill", "missing": False})
        self.assertEqual(j["music"]["skip"], 96000)
        self.assertEqual(j["music"]["seconds"], 30.0)
        self.assertNotIn("path", str(j))


class TestFiveItems(unittest.TestCase):
    """Hand-worked at 30 fps, fade 0.25 s (X = RHU(7.5) = 8)."""

    def test_case(self):
        E = ("gen", "clip_00002.mp4")
        probes = {A: vprobe(624, 624, 49), B: iprobe(1104, 1472), C: vprobe(624, 624, 129), E: vprobe(832, 480, 96)}
        items = [clip("a", A, join="fade"), still("b", B, 1.0, join="fade"), clip("c", C, 0, 46),
                 still("d", B, 0.5, join="fade"), clip("e", E)]
        p = tl.plan(proj(items, fps=30, fade=0.25), probes)
        self.assertEqual([i.D for i in p.items], [92, 30, 86, 15, 180])
        self.assertEqual([i.X_after for i in p.items], [8, 8, 0, 8, 0])
        self.assertEqual([i.start for i in p.items], [0, 84, 106, 192, 199])
        self.assertEqual((p.T, p.T_samples), (379, 606400))
        last = p.items[-1]
        self.assertEqual(last.start + last.D, p.T)

    def test_samples_integer(self):
        for fps in (24, 30):
            for n in range(2, 60):
                p = tl.plan(proj([clip("a", A, 0, n)], fps=fps), {A: vprobe(10, 10, 60)})
                self.assertEqual(p.T_samples * fps, p.T * 48000)


class TestMissing(unittest.TestCase):
    def test_missing_uses_snap(self):
        snap = {"frames": 49, "rate": "16/1", "w": 624, "h": 624, "sar": "1/1", "commercial_ok": False}
        p = tl.plan(proj([clip("a", A, snap=snap), still("b", B, 2.0, snap={"w": 5, "h": 5})]), {A: None})
        a, b = p.items
        self.assertEqual((a.kind, a.D, a.path, a.fit, a.commercial_ok), ("missing", 74, None, "fill", False))
        self.assertEqual((b.kind, b.D), ("missing", 48))
        self.assertEqual(p.missing, ("a", "b"))
        self.assertIn("qwen_video_00001.mp4 is missing; shown as black", p.warnings)

    def test_wrong_kind_is_missing(self):
        p = tl.plan(proj([clip("a", B)]), {B: iprobe(10, 10)})
        self.assertEqual(p.items[0].kind, "missing")

    def test_missing_trim_clamped_to_snap(self):
        snap = {"frames": 30, "rate": "16/1"}
        p = tl.plan(proj([clip("a", A, 10, 60, snap=snap)]), {})
        self.assertEqual((p.items[0].src_in, p.items[0].src_out, p.items[0].D), (10, 30, 30))


class TestMusicWindow(unittest.TestCase):
    T = 656000

    def m(self, **kw):
        base = {"volume": 1.0, "fade_in": 0.5, "fade_out": 1.5, "offset": 0.0}
        base.update(kw)
        return base

    def test_positive_offset(self):
        f, w = tl.music_window(self.m(offset=2.0, volume=0.8), 1_440_000, self.T, 24)
        self.assertEqual((f["skip"], f["delay"], f["end"]), (96000, 0, 656000))
        self.assertEqual(w, [])

    def test_negative_offset(self):
        f, w = tl.music_window(self.m(offset=-1.0), 1_440_000, self.T, 24)
        self.assertEqual((f["skip"], f["delay"], f["end"]), (0, 48000, 656000))

    def test_song_shorter_than_cut(self):
        f, w = tl.music_window(self.m(), 480000, self.T, 24)
        self.assertEqual(f["end"], 480000)
        self.assertEqual(f["end"] - f["fade_out"], 408000)
        self.assertEqual(w, ["music ends 3.7 s before the cut"])
        f, w = tl.music_window(self.m(), self.T - 23999, self.T, 24)   # under half a second short: no warning
        self.assertEqual(w, [])

    def test_fades_scaled(self):
        f, w = tl.music_window(self.m(fade_in=1.0, fade_out=3.0), 48000, self.T, 24)
        self.assertEqual((f["fade_in"], f["fade_out"]), (12000, 36000))
        self.assertIn("music fades shortened to fit the music", w)
        f, w = tl.music_window(self.m(fade_in=1.0, fade_out=1.0), 47999, self.T, 24)
        self.assertLessEqual(f["fade_in"] + f["fade_out"], 47999)

    def test_not_heard(self):
        f, w = tl.music_window(self.m(offset=40.0), 1_440_000, self.T, 24)
        self.assertIsNone(f)
        self.assertEqual(w, ["music is not heard (start is past the end)"])
        f, w = tl.music_window(self.m(offset=-20.0), 1_440_000, self.T, 24)
        self.assertIsNone(f)

    def test_target(self):
        self.assertEqual(tl.target_i(1.0), -14.0)
        self.assertEqual(tl.target_i(0.8), -15.94)
        self.assertEqual(tl.target_i(0.05), -40.02)
        self.assertEqual(tl.target_i(0.01), -40.02)    # clamped

    def test_plan_without_wav_uses_probe(self):
        p = tl.plan(example_project(), {**EXAMPLE_PROBES, M: aprobe(30.0)})
        self.assertIsNone(p.music.wav)
        self.assertEqual(p.music.n_samples, 1_440_000)

    def test_plan_music_missing(self):
        p = tl.plan(example_project(), EXAMPLE_PROBES)
        self.assertIsNone(p.music)
        self.assertTrue(any("no music" in w for w in p.warnings))


class TestAceSeconds(unittest.TestCase):
    def test_values(self):
        self.assertEqual(tl.ace_seconds(656000, None), 13.68)          # 341.67 -> 342 latent frames
        self.assertEqual(tl.ace_seconds(656000, {"offset": 2.0}), 15.68)
        self.assertEqual(tl.ace_seconds(656000, {"offset": -1.0}), 12.68)
        self.assertEqual(tl.ace_seconds(48000, None), 5.0)
        self.assertEqual(tl.ace_seconds(48000 * 600, None), 240.0)
        self.assertEqual(tl.ace_seconds(48000 * 10, None), 10.0)       # exact: no extra frame


class TestCadence(unittest.TestCase):
    def runs(self, F, n=40):
        seq = [tl.source_frame_at(k, Fraction(16), F) for k in range(n)]
        out, cur = [], 1
        for a, b in zip(seq, seq[1:]):
            if a == b:
                cur += 1
            else:
                self.assertEqual(b, a + 1)
                out.append(cur)
                cur = 1
        return out

    def test_24(self):
        self.assertEqual(self.runs(24)[:8], [2, 1, 2, 1, 2, 1, 2, 1])

    def test_30(self):
        self.assertEqual(self.runs(30)[:8], [2, 2, 2, 2, 1, 2, 2, 2])

    def test_definition(self):
        for F in (24, 30):
            for r in (Fraction(16), Fraction(30000, 1001), Fraction(25)):
                for k in range(100):
                    i = tl.source_frame_at(k, r, F)
                    self.assertLessEqual(RHU(Fraction(i) * F / r), k)
                    self.assertGreater(RHU(Fraction(i + 1) * F / r), k)


class TestAutoFit(unittest.TestCase):
    def test_fit(self):
        self.assertEqual(tl.auto_fit(624, 624, Fraction(1), 1080, 1920), "blur")
        self.assertEqual(tl.auto_fit(624, 624, Fraction(1), 1080, 1080), "fill")
        self.assertEqual(tl.auto_fit(1104, 1472, Fraction(1), 1080, 1350), "fill")   # 0.75 vs 0.8
        self.assertEqual(tl.auto_fit(832, 480, Fraction(1), 1920, 1080), "fill")
        self.assertEqual(tl.auto_fit(1200, 1000, Fraction(1), 1000, 1000), "fill")   # exactly 1.2
        self.assertEqual(tl.auto_fit(1201, 1000, Fraction(1), 1000, 1000), "blur")
        self.assertEqual(tl.auto_fit(720, 1080, Fraction(3, 2), 1080, 1080), "fill")  # display 1080x1080


class TestValidate(unittest.TestCase):
    def test_clamps_and_drops(self):
        raw = {"schema": "x", "id": "p_3f9a0c1b2d4e", "name": "  a\x00b\x1f" + "n" * 100, "rev": 4, "evil": 1,
               "items": [{"id": "i_1", "kind": "clip", "src": ref(A), "in": -5, "out": 1, "join": "wipe", "x": 2},
                         {"id": "i_2", "kind": "still", "src": ref(B), "dur": 99},
                         {"id": "i_3", "kind": "still", "src": ref(B), "dur": "2"},
                         {"id": "i_1", "kind": "still", "src": ref(B), "dur": 0.1}],
               "music": {"src": ref(M), "volume": 7, "fade_in": -1, "fade_out": 50, "offset": -900, "junk": 1},
               "export": {"preset": "21:9", "fit": "stretch", "fps": 60, "fade": 0.4, "x": 1}}
        c, errs = tl.validate(raw)
        self.assertEqual(errs, [])
        self.assertEqual(set(c), {"schema", "id", "name", "rev", "items", "music", "export"})
        self.assertEqual(c["schema"], "media-lab/project@1")
        self.assertEqual(len(c["name"]), 80)
        self.assertTrue(c["name"].startswith("ab"))
        i1, i2, i3, i4 = c["items"]
        self.assertEqual(i1, {"id": "i_1", "kind": "clip", "src": ref(A), "in": 0, "out": 2, "join": "cut"})
        self.assertEqual((i2["dur"], i3["dur"], i4["dur"]), (30, 2.5, 0.5))
        self.assertNotEqual(i4["id"], "i_1")
        self.assertEqual(c["music"], {"src": ref(M), "volume": 1.0, "fade_in": 0, "fade_out": 10, "offset": -600})
        self.assertEqual(c["export"], {"preset": "9:16", "fit": "auto", "fps": 24, "fade": 0.5})

    def test_unusable(self):
        c, errs = tl.validate({"items": [{"kind": "clip", "src": {"root": "etc", "name": "passwd"}},
                                         {"kind": "clip", "src": {"root": "out", "name": "../x"}},
                                         {"kind": "video", "src": ref(A)}, "x"],
                               "music": {"src": {"root": "lab", "name": "a/b"}}})
        self.assertEqual(c["items"], [])
        self.assertEqual(c["music"], None)
        self.assertEqual(len(errs), 5)
        c, errs = tl.validate([])
        self.assertTrue(errs)
        self.assertEqual(c["items"], [])

    def test_item_cap(self):
        items = [still(f"i_{n}", B) for n in range(61)]
        c, errs = tl.validate({"items": items})
        self.assertEqual(len(c["items"]), 60)
        self.assertIn("a cut can hold at most 60 items", errs)
        c, errs = tl.validate({"items": items[:60]})
        self.assertEqual((len(c["items"]), errs), (60, []))

    def test_numbers_not_bools(self):
        c, _ = tl.validate({"items": [clip("a", A, True, None)], "export": {"fps": True}})
        self.assertEqual(c["items"][0]["in"], 0)
        self.assertEqual(c["export"]["fps"], 24)

    def test_idempotent(self):
        c, _ = tl.validate(example_project())
        self.assertEqual(tl.validate(c)[0], c)


class TestSourcePreset(unittest.TestCase):
    def test_dims(self):
        p = tl.plan(proj([clip("a", A)], preset="source"), {A: vprobe(1665, 929, 49)})
        self.assertEqual(tl.preset_dims("source", p.items), (1666, 930))
        p = tl.plan(proj([clip("a", A)], preset="source"), {A: vprobe(3001, 1001, 49)})
        W, H = tl.preset_dims("source", p.items)
        self.assertEqual(W, 1920)
        self.assertEqual(H % 2, 0)
        p = tl.plan(proj([clip("a", A)], preset="source"), {A: vprobe(720, 480, 49, sar=Fraction(32, 27))})
        self.assertEqual(tl.preset_dims("source", p.items), (854, 480))
        p = tl.plan(proj([clip("x", C), clip("a", A)], preset="source"), {A: vprobe(625, 351, 49)})
        self.assertEqual(tl.preset_dims("source", p.items), (626, 352))   # first non-missing item


if __name__ == "__main__":
    unittest.main()
