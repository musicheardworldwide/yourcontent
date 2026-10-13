"""export: the pure video filtergraph and argv builders (§8.1-§8.5, §14 WP1). No ffmpeg, no files."""
from __future__ import annotations

import re
import unittest
from fractions import Fraction

from medialab import export, media, timeline
from medialab.timeline import RHU
from tests.wp1_fakes import (A, B, C, D, EXAMPLE_PATHS, EXAMPLE_PROBES, EXAMPLE_WAVS, example_project, iprobe,
                             ref, vprobe)

SC = ":flags=lanczos:in_color_matrix=auto:out_color_matrix=bt709:out_range=tv"

# §8.5, verbatim (newlines removed, {SC} expanded).
GOLDEN_FILL_9x16 = "".join("""
[0:v]trim=start_frame=0:end_frame=49,setpts=N/(16*TB),scale=1920:1920{SC},crop=1080:1920:420:0,setsar=1,fps=24,tpad=stop_mode=clone:stop=24,trim=end_frame=74,settb=1/24,setpts=N,fps=24,format=yuv444p[v0];
[1:v]scale=1440:1920{SC},crop=1080:1920:180:0,setsar=1,loop=loop=59:size=1:start=0,settb=1/24,setpts=N,fps=24,format=yuv444p[v1];
[2:v]trim=start_frame=16:end_frame=112,setpts=N/(16*TB),scale=1920:1920{SC},crop=1080:1920:420:0,setsar=1,fps=24,tpad=stop_mode=clone:stop=24,trim=end_frame=144,settb=1/24,setpts=N,fps=24,format=yuv444p[v2];
[3:v]trim=start_frame=0:end_frame=49,setpts=N/(16*TB),scale=3328:1920{SC},crop=1080:1920:1124:0,setsar=1,fps=24,tpad=stop_mode=clone:stop=24,trim=end_frame=74,settb=1/24,setpts=N,fps=24,format=yuv444p[v3];
[v0][v1]xfade=transition=fade:duration=0.500000:offset=2.583333[x1];
[x1][v2]xfade=transition=fade:duration=0.500000:offset=4.583333[x2];
[x2][v3]concat=n=2:v=1:a=0,format=yuv420p[vout]
""".split("\n")).replace("{SC}", SC)

GOLDEN_BLUR_16x9 = "".join("""
[0:v]trim=start_frame=0:end_frame=49,setpts=N/(16*TB),split=2[f0][b0];[b0]scale=80:80{SC},crop=80:46:0:16,gblur=sigma=3,scale=1920:1080:flags=bicubic,setsar=1[bg0];[f0]scale=1080:1080{SC},setsar=1[fg0];[bg0][fg0]overlay=x=420:y=0:eof_action=pass,fps=24,tpad=stop_mode=clone:stop=24,trim=end_frame=74,settb=1/24,setpts=N,fps=24,format=yuv444p[v0];
[1:v]split=2[f1][b1];[b1]scale=80:108{SC},crop=80:46:0:30,gblur=sigma=3,scale=1920:1080:flags=bicubic,setsar=1[bg1];[f1]scale=810:1080{SC},setsar=1[fg1];[bg1][fg1]overlay=x=554:y=0:eof_action=pass,loop=loop=59:size=1:start=0,settb=1/24,setpts=N,fps=24,format=yuv444p[v1];
[2:v]trim=start_frame=16:end_frame=112,setpts=N/(16*TB),split=2[f2][b2];[b2]scale=80:80{SC},crop=80:46:0:16,gblur=sigma=3,scale=1920:1080:flags=bicubic,setsar=1[bg2];[f2]scale=1080:1080{SC},setsar=1[fg2];[bg2][fg2]overlay=x=420:y=0:eof_action=pass,fps=24,tpad=stop_mode=clone:stop=24,trim=end_frame=144,settb=1/24,setpts=N,fps=24,format=yuv444p[v2];
[3:v]trim=start_frame=0:end_frame=49,setpts=N/(16*TB),split=2[f3][b3];[b3]scale=80:48{SC},crop=80:46:0:0,gblur=sigma=3,scale=1920:1080:flags=bicubic,setsar=1[bg3];[f3]scale=1872:1080{SC},setsar=1[fg3];[bg3][fg3]overlay=x=24:y=0:eof_action=pass,fps=24,tpad=stop_mode=clone:stop=24,trim=end_frame=74,settb=1/24,setpts=N,fps=24,format=yuv444p[v3];
[v0][v1]xfade=transition=fade:duration=0.500000:offset=2.583333[x1];
[x1][v2]xfade=transition=fade:duration=0.500000:offset=4.583333[x2];
[x2][v3]concat=n=2:v=1:a=0,format=yuv420p[vout]
""".split("\n")).replace("{SC}", SC)

SIZES = [(624, 624), (832, 480), (480, 832), (1104, 1472), (1328, 1328), (1664, 928)]
PRESETS = ["9:16", "4:5", "1:1", "16:9", "source"]


def plan_of(project, probes=EXAMPLE_PROBES, paths=EXAMPLE_PATHS, wavs=EXAMPLE_WAVS):
    return timeline.plan(project, probes, wavs, paths=paths)


def graph_of(pl, div=1):
    W, H, fW, fH = export.output_dims(pl.preset, pl, div)
    return export.build_video_graph(pl, W, H, fW, fH)


def branches(graph: str) -> list[str]:
    """The per-item branch text, up to and including its [vN] label."""
    return re.findall(r"\[\d+:v\].*?\[v\d+\]", graph)


def strip_allowed_slashes(graph: str) -> str:
    g = re.sub(r"settb=1/\d+", "", graph)
    g = re.sub(r"setpts=N(\*\d+)?/\(\d+\*TB\)", "", g)
    return g


class TestGolden(unittest.TestCase):
    def test_fill_9x16(self):
        inputs, graph = graph_of(plan_of(example_project()))
        self.assertEqual(graph, GOLDEN_FILL_9x16)
        self.assertEqual(inputs, [a for r in (A, B, C, D) for a in media.inp(EXAMPLE_PATHS[r])])
        self.assertEqual(media.inp("x")[-2:], ["-i", "x"])
        self.assertIn("-format_whitelist", media.inp("x"))

    def test_blur_16x9(self):
        _, graph = graph_of(plan_of(example_project(preset="16:9", fit="blur")))
        self.assertEqual(graph, GOLDEN_BLUR_16x9)

    def test_example_geometry_tables(self):
        g = export.geometry(624, 624, Fraction(1), 1080, 1920, "fill", 1080, 1920)
        self.assertEqual((g.SW, g.SH, g.CX, g.CY), (1920, 1920, 420, 0))
        g = export.geometry(1104, 1472, Fraction(1), 1080, 1920, "fill", 1080, 1920)
        self.assertEqual((g.SW, g.SH, g.CX, g.CY), (1440, 1920, 180, 0))
        g = export.geometry(832, 480, Fraction(1), 1080, 1920, "fill", 1080, 1920)
        self.assertEqual((g.SW, g.SH, g.CX, g.CY), (3328, 1920, 1124, 0))
        rows = {(624, 624): (1080, 1080, 420, 0, 80, 80, 0, 16), (1104, 1472): (810, 1080, 554, 0, 80, 108, 0, 30),
                (832, 480): (1872, 1080, 24, 0, 80, 48, 0, 0)}
        for (w, h), want in rows.items():
            g = export.geometry(w, h, Fraction(1), 1920, 1080, "blur", 1920, 1080)
            self.assertEqual((g.FW, g.FH, g.OX, g.OY, g.bw, g.bh, g.bx, g.by), want)
            self.assertEqual((g.BW, g.BH), (80, 46))


class TestShapes(unittest.TestCase):
    def test_one_item(self):
        pl = timeline.plan({"items": [{"id": "a", "kind": "clip", "src": ref(A), "join": "fade"}],
                            "export": {"preset": "1:1", "fit": "fill"}}, EXAMPLE_PROBES, paths=EXAMPLE_PATHS)
        inputs, graph = graph_of(pl)
        self.assertEqual(graph, f"[0:v]trim=start_frame=0:end_frame=49,setpts=N/(16*TB),scale=1080:1080{SC},"
                                "crop=1080:1080:0:0,setsar=1,fps=24,tpad=stop_mode=clone:stop=24,trim=end_frame=74,"
                                "settb=1/24,setpts=N,fps=24,format=yuv420p[v0];[v0]format=yuv420p[vout]")
        self.assertEqual(inputs, media.inp(EXAMPLE_PATHS[A]))

    def test_all_cuts(self):
        p = example_project()
        for it in p["items"]:
            it["join"] = "cut"
        _, graph = graph_of(plan_of(p))
        self.assertNotIn("xfade", graph)
        self.assertNotIn("yuv444p", graph)
        for b in branches(graph):
            self.assertTrue(b.endswith(",settb=1/24,setpts=N,fps=24,format=yuv420p" + b[b.rfind("["):]))
        self.assertTrue(graph.endswith("[v0][v1]concat=n=2:v=1:a=0[x1];[x1][v2]concat=n=2:v=1:a=0[x2];"
                                       "[x2][v3]concat=n=2:v=1:a=0,format=yuv420p[vout]"))

    def test_cut_then_fade_retimes_concat(self):
        """concat outputs on 1/1000000; a following xfade needs 1/F at F, so the concat is put back on it."""
        p = example_project()
        joins = ["cut", "fade", "cut", "cut"]
        for it, j in zip(p["items"], joins):
            it["join"] = j
        _, graph = graph_of(plan_of(p))
        self.assertIn("[v0][v1]concat=n=2:v=1:a=0,settb=1/24,setpts=N,fps=24[x1];[x1][v2]xfade=", graph)
        self.assertIn("[x2][v3]concat=n=2:v=1:a=0,format=yuv420p[vout]", graph)

    def test_missing_item(self):
        probes = dict(EXAMPLE_PROBES)
        probes[C] = None
        pl = plan_of(example_project(), probes)
        inputs, graph = graph_of(pl)
        k = 2 * len(media.inp("x"))
        self.assertEqual(inputs[k:k + 4], ["-f", "lavfi", "-i", "color=c=black:s=1080x1920:r=24"])
        self.assertIn("[2:v]trim=end_frame=144,settb=1/24,setpts=N,fps=24,format=yuv444p[v2]", graph)
        self.assertNotIn(EXAMPLE_PATHS[C], inputs)
        pin, _ = graph_of(pl, 3)
        self.assertIn("color=c=black:s=360x640:r=24", pin)

    def test_branch_tails(self):
        for p in (example_project(), example_project(preset="16:9", fit="blur"), example_project(fps=30)):
            pl = plan_of(p)
            _, graph = graph_of(pl)
            PF = "yuv444p"
            bs = branches(graph)
            self.assertEqual(len(bs), 4)
            for b in bs:
                self.assertRegex(b, rf",settb=1/{pl.F},setpts=N,fps={pl.F},format={PF}\[v\d+\]$")

    def test_offsets_exact(self):
        for fps in (24, 30):
            for fade in (0.25, 0.5, 1.0):
                pl = plan_of(example_project(fps=fps, fade=fade))
                _, graph = graph_of(pl)
                offs = [Fraction(x) for x in re.findall(r"offset=([\d.]+)", graph)]
                durs = [Fraction(x) for x in re.findall(r"duration=([\d.]+)", graph)]
                xf = [it for it in pl.items[:-1] if it.X_after > 0]
                self.assertEqual(len(offs), len(xf))
                for o, d, it in zip(offs, durs, xf):
                    nxt = pl.items[pl.items.index(it) + 1]
                    self.assertEqual(RHU(o * pl.F), nxt.start)
                    self.assertEqual(RHU(d * pl.F), it.X_after)

    def test_labels_unique_and_inputs_match(self):
        for p in (example_project(), example_project(preset="16:9", fit="blur")):
            inputs, graph = graph_of(plan_of(p))
            produced = re.findall(r"\[([a-z]+\d*)\](?=;|$)", graph)
            self.assertEqual(len(produced), len(set(produced)), produced)
            for lab in re.findall(r"\[([a-z]+\d*)\]", graph):
                self.assertEqual(graph.count(f"[{lab}]"), 1 if lab == "vout" else 2, lab)   # made once, used once
            idx = {int(i) for i in re.findall(r"\[(\d+):v\]", graph)}
            self.assertEqual(idx, set(range(inputs.count("-i"))))

    def test_no_paths_in_graph(self):
        for p in (example_project(), example_project(preset="16:9", fit="blur")):
            inputs, graph = graph_of(plan_of(p))
            for path in EXAMPLE_PATHS.values():
                self.assertNotIn(path, graph)
            self.assertNotIn("/", strip_allowed_slashes(graph))
            self.assertNotIn("'", graph)

    def test_proxy_equals_final_but_size(self):
        for p in (example_project(), example_project(preset="16:9", fit="blur"), example_project(preset="4:5")):
            pl = plan_of(p)
            _, final = graph_of(pl)
            _, proxy = graph_of(pl, 3)
            self.assertEqual(re.sub(r"\d+", "#", final), re.sub(r"\d+", "#", proxy))
            timing = r"(trim=[^,;\[]*|loop=loop=\d+|xfade=[^\[]*|tpad=[^,]*|fps=\d+|settb=[^,]*)"
            self.assertEqual(re.findall(timing, final), re.findall(timing, proxy))
            bg = re.findall(r"\[b\d+\]scale=\d+:\d+" + re.escape(SC) + r",crop=[\d:]+,gblur=sigma=3", final)
            self.assertEqual(bg, re.findall(r"\[b\d+\]scale=\d+:\d+" + re.escape(SC) + r",crop=[\d:]+,gblur=sigma=3",
                                            proxy))

    def test_proxy_dims(self):
        pl = plan_of(example_project())
        for preset, want in (("9:16", (360, 640)), ("4:5", (360, 450)), ("1:1", (360, 360)), ("16:9", (640, 360))):
            self.assertEqual(export.output_dims(preset, pl, 3)[:2], want)
        for preset, want in (("9:16", (46, 80)), ("4:5", (46, 56)), ("1:1", (46, 46)), ("16:9", (80, 46))):
            W, H, fW, fH = export.output_dims(preset, pl, 3)
            g = export.geometry(624, 624, Fraction(1), W, H, "blur", fW, fH)
            self.assertEqual((g.BW, g.BH), want)


class TestGeometry(unittest.TestCase):
    def test_even_and_centred(self):
        for (w, h) in SIZES:
            for preset in PRESETS:
                pl = timeline.plan({"items": [{"id": "a", "kind": "still", "src": ref(B)}],
                                    "export": {"preset": preset}}, {B: iprobe(w, h)}, paths={B: "/x"})
                for div in (1, 3):
                    W, H, fW, fH = export.output_dims(preset, pl, div)
                    self.assertEqual((W % 2, H % 2), (0, 0), (preset, w, h))
                    for fit in ("fill", "blur"):
                        g = export.geometry(w, h, Fraction(1), W, H, fit, fW, fH)
                        for v in (g.SW, g.SH, g.CX, g.CY, g.FW, g.FH, g.OX, g.OY, g.bw, g.bh, g.BW, g.BH, g.bx, g.by):
                            self.assertEqual(v % 2, 0, (preset, w, h, fit, g))
                        if fit == "fill":
                            self.assertTrue(g.SW >= W and g.SH >= H)
                            self.assertTrue(0 <= g.SW - W - 2 * g.CX <= 3 and 0 <= g.SH - H - 2 * g.CY <= 3, g)
                            # aspect kept within a pixel of rounding
                            self.assertLessEqual(abs(Fraction(g.SW, g.SH) - Fraction(w, h)), Fraction(2, min(g.SW, g.SH)) * 2)
                        else:
                            self.assertTrue(g.FW <= W and g.FH <= H and (g.FW >= W - 2 or g.FH >= H - 2), g)
                            self.assertTrue(0 <= W - g.FW - 2 * g.OX <= 3 and 0 <= H - g.FH - 2 * g.OY <= 3, g)
                            self.assertTrue(g.bw >= g.BW and g.bh >= g.BH)
                            self.assertTrue(g.bx + g.BW <= g.bw and g.by + g.BH <= g.bh)

    def test_source_odd_dims(self):
        pl = timeline.plan({"items": [{"id": "a", "kind": "clip", "src": ref(A)}], "export": {"preset": "source"}},
                           {A: vprobe(1665, 929, 49)}, paths={A: "/x"})
        W, H, fW, fH = export.output_dims("source", pl)
        self.assertEqual((W, H), (1666, 930))
        _, graph = graph_of(pl)
        self.assertIn("crop=1666:930:", graph)
        pW, pH, _, _ = export.output_dims("source", pl, 3)
        self.assertEqual((pW, pH), (556, 310))

    def test_anamorphic(self):
        g = export.geometry(720, 480, Fraction(32, 27), 1920, 1080, "fill", 1920, 1080)
        self.assertEqual((g.SW, g.SH), (1920, 1080))


class TestArgv(unittest.TestCase):
    def test_encode_final(self):
        a = export.encode_args("final", 24, 4, "licence: non-commercial")
        self.assertEqual(a[:4], ["-map", "[vout]", "-map", "4:a"])
        s = " ".join(a)
        for part in ("-c:v libx264 -preset slow -crf 18 -maxrate 8M -bufsize 16M -profile:v high -pix_fmt yuv420p",
                     "-colorspace bt709 -color_primaries bt709 -color_trc bt709 -color_range tv -g 48",
                     "-c:a aac -b:a 192k -ar 48000 -movflags +faststart", "-fps_mode passthrough"):
            self.assertIn(part, s)
        self.assertEqual(a[-2:], ["-metadata", "comment=licence: non-commercial"])
        self.assertNotIn("-shortest", a)
        self.assertNotIn("-t", a)

    def test_encode_proxy_no_audio(self):
        a = export.encode_args("proxy", 30, None, "")
        s = " ".join(a)
        self.assertIn("-preset veryfast -crf 28", s)
        self.assertIn("-g 30", s)
        self.assertNotIn("maxrate", s)
        self.assertNotIn("-c:a", a)
        self.assertEqual(a.count("-map"), 1)
        self.assertIn("-b:a 128k", " ".join(export.encode_args("proxy", 24, 4, "")))

    def test_hostile_name_stays_out_of_graph(self):
        pl = plan_of(example_project())
        name = "a;[b]"
        argv = export.render_argv(pl, "final", "/data/cache/audio/k.wav", "/data/tmp/.ml_x.mp4", comment="c",
                                  title=name)
        graph = argv[argv.index("-filter_complex") + 1]
        self.assertNotIn(name, graph)
        self.assertEqual([x for x in argv if name in x], [f"title={name}"])
        self.assertEqual(argv[argv.index(f"title={name}") - 1], "-metadata")
        self.assertEqual(argv[-1], "/data/tmp/.ml_x.mp4")
        self.assertEqual(argv[argv.index("/data/cache/audio/k.wav") - 1], "-i")
        self.assertIn("4:a", argv)

    def test_proxy_key_stable(self):
        pl = plan_of(example_project())
        self.assertEqual(export.proxy_key(pl, 360, 640), export.proxy_key(pl, 360, 640))
        other = plan_of(example_project(fade=1.0))
        self.assertNotEqual(export.proxy_key(pl, 360, 640), export.proxy_key(other, 360, 640))
        self.assertRegex(export.proxy_key(pl, 360, 640), r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
