"""export: the pure music mix graph (§7.4, §8.5 mix, §14 WP1). No ffmpeg, no files."""
from __future__ import annotations

import unittest
from dataclasses import replace

from medialab import export, timeline
from medialab.types import MusicPlan
from tests.wp1_fakes import EXAMPLE_PATHS, EXAMPLE_PROBES, EXAMPLE_WAVS, M, example_project

PASS1 = ("[0:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,atrim=start_sample=96000,"
         "asetpts=N/SR/TB,apad=whole_len=656000,atrim=end_sample=656000,afade=t=in:ss=0:ns=24000,"
         "afade=t=out:ss=584000:ns=72000,loudnorm=I=-15.94:TP=-1:LRA=11:print_format=json")

# A pass-1 stderr as ffmpeg 7.1.5 prints it (log level info), with an unrelated brace block before it.
PASS1_STDERR = """Input #0, wav, from '/data/cache/music/7c1e00000000.wav':
  Metadata: { not json }
[Parsed_loudnorm_8 @ 0x55d1c2a3f0c0]
{
	"input_i" : "-21.37",
	"input_tp" : "-6.02",
	"input_lra" : "4.10",
	"input_thresh" : "-31.52",
	"output_i" : "-15.95",
	"output_tp" : "-1.00",
	"output_lra" : "3.90",
	"output_thresh" : "-26.10",
	"normalization_type" : "dynamic",
	"target_offset" : "0.01"
}
[out#0/null @ 0x55d1c2a40a80] video:0KiB audio:5125KiB
"""


def example_plan(**kw):
    return timeline.plan(example_project(**kw), EXAMPLE_PROBES, EXAMPLE_WAVS, paths=EXAMPLE_PATHS)


def mp(**kw) -> MusicPlan:
    base = dict(ref=M, wav="/w.wav", n_samples=1_440_000, skip=0, delay=0, end=656000, fade_in=24000,
                fade_out=72000, volume=1.0, target_i=-14.0)
    base.update(kw)
    return MusicPlan(**base)


class TestChain(unittest.TestCase):
    def test_pass1_example(self):
        pl = example_plan()
        self.assertEqual(export.build_audio_graph(pl.music, pl.T_samples, None), PASS1)

    def test_negative_offset_delays(self):
        p = example_project()
        p["music"]["offset"] = -0.5
        pl = timeline.plan(p, EXAMPLE_PROBES, EXAMPLE_WAVS)
        g = export.build_audio_graph(pl.music, pl.T_samples, None)
        self.assertIn("asetpts=N/SR/TB,adelay=delays=24000S:all=1,apad=whole_len=656000", g)
        self.assertNotIn("atrim=start_sample", g)
        self.assertIn("afade=t=in:ss=24000:ns=24000", g)

    def test_zero_fades_omitted(self):
        g = export.build_audio_graph(mp(fade_in=0, fade_out=0), 656000, None)
        self.assertNotIn("afade", g)
        g = export.build_audio_graph(mp(fade_in=0), 656000, None)
        self.assertNotIn("t=in", g)
        self.assertIn("afade=t=out:ss=584000:ns=72000", g)

    def test_short_song_fades_at_its_end(self):
        g = export.build_audio_graph(mp(n_samples=480000, end=480000), 656000, None)
        self.assertIn("afade=t=out:ss=408000:ns=72000", g)
        self.assertIn("apad=whole_len=656000,atrim=end_sample=656000", g)

    def test_pass2_from_recorded(self):
        pl = example_plan()
        measured = export.parse_loudnorm(PASS1_STDERR)
        self.assertEqual(measured["input_i"], "-21.37")
        g = export.build_audio_graph(pl.music, pl.T_samples, measured)
        self.assertEqual(g, PASS1[: -len(":print_format=json")]
                         + ":measured_I=-21.37:measured_TP=-6.02:measured_LRA=4.10:measured_thresh=-31.52"
                           ":offset=0.01:linear=true:print_format=json,aresample=48000:resampler=soxr,"
                           "apad=whole_len=656000,atrim=end_sample=656000")

    def test_silence_skips_loudnorm(self):
        pl = example_plan()
        for v in ("-inf", "-70.5", "-99"):
            g = export.build_audio_graph(pl.music, pl.T_samples, {"input_i": v, "input_tp": "-inf", "input_lra": "0",
                                                                  "input_thresh": "-inf", "target_offset": "0"})
            self.assertNotIn("loudnorm", g)
            self.assertTrue(g.endswith("apad=whole_len=656000,atrim=end_sample=656000"))

    def test_measured_must_be_numbers(self):
        pl = example_plan()
        bad = {"input_i": "-20", "input_tp": "-1,anullsink", "input_lra": "1", "input_thresh": "-30",
               "target_offset": "0"}
        with self.assertRaises(ValueError):
            export.build_audio_graph(pl.music, pl.T_samples, bad)

    def test_parse_loudnorm_missing(self):
        with self.assertRaises(ValueError):
            export.parse_loudnorm("no json here {a}")

    def test_targets(self):
        self.assertEqual(export.loudnorm_target(1.0), -14.0)
        self.assertEqual(export.loudnorm_target(0.8), -15.94)
        self.assertEqual(export.loudnorm_target(0.05), -40.02)
        self.assertEqual(export.build_audio_graph(mp(target_i=-40.02), 656000, None).count("I=-40.02"), 1)


class TestAudioKey(unittest.TestCase):
    def test_independent_of_resolution(self):
        keys = {export.audio_key(example_plan(preset=p, fit=f)) for p in ("9:16", "16:9", "1:1", "source")
                for f in ("fill", "blur", "auto")}
        self.assertEqual(len(keys), 1)

    def test_changes_with_mix(self):
        base = example_plan()
        k = export.audio_key(base)
        self.assertRegex(k, r"^[0-9a-f]{64}$")
        self.assertNotEqual(k, export.audio_key(replace(base, music=replace(base.music, target_i=-14.0))))
        self.assertNotEqual(k, export.audio_key(replace(base, music=replace(base.music, wav="/x/other.wav"))))
        self.assertNotEqual(k, export.audio_key(example_plan(fade=1.0)))      # T changes
        self.assertEqual(export.audio_key(example_plan(music=False)), "")


if __name__ == "__main__":
    unittest.main()
