"""Real-ffmpeg checks of the edit engine (§14, WP1). Lavfi stand-ins in temp dirs; skipped without ffmpeg 7.1.

Run in the image (a host ffmpeg older than 7.1 skips these):
    docker run --rm -v "$PWD":/app -w /app media-lab:latest python3 -m unittest tests.test_ffmpeg_exact
"""
from __future__ import annotations

import os
import shutil
import struct
import subprocess
import tempfile
import unittest
from dataclasses import replace
from fractions import Fraction
from unittest import mock

from medialab import export, media, timeline
from medialab.types import Conflict
from tests.util import FakeJob, TempRoots, make_png, requires_ffmpeg
from tests.wp1_fakes import FakeStore, fake_provenance, ref

FF = requires_ffmpeg((7, 1))


def ff(*args: str, timeout: float = 120) -> None:
    r = subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", *args], capture_output=True, text=True,
                       timeout=timeout)
    if r.returncode:
        raise RuntimeError(r.stderr)


def make_clip(path: str, w: int, h: int, frames: int, lum: str, rate: int = 16) -> None:
    """A lossless H.264 clip whose luma is `lum` (a geq expression of N, the frame number)."""
    ff("-f", "lavfi", "-i", f"color=c=black:s={w}x{h}:r={rate}", "-frames:v", str(frames),
       "-vf", f"geq=lum='{lum}':cb=128:cr=128", "-c:v", "libx264", "-qp", "0", "-preset", "ultrafast",
       "-pix_fmt", "yuv420p", path)


def make_song(path: str, seconds: float, rate: int = 44100) -> None:
    ff("-f", "lavfi", "-i", f"anoisesrc=color=pink:amplitude=0.08:sample_rate={rate}:seed=7:duration={seconds}",
       "-ac", "2", "-c:a", "flac", path)


def luma_frames(inputs: list[str], graph: str, W: int = 16, H: int = 16) -> list[float]:
    """Run the graph and return each output frame's mean luma (tv range, unconverted) over a centre crop.

    A crop, not a scale: the test frames are flat, and a crop involves no resampling."""
    g = graph[: -len("[vout]")] + f",crop={W}:{H},format=yuv420p[vout]"
    r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", *inputs, "-filter_complex", g, "-map", "[vout]",
                        "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"],
                       capture_output=True, timeout=120)
    if r.returncode:
        raise RuntimeError(r.stderr.decode())
    size = W * H * 3 // 2
    data = r.stdout
    return [sum(data[i:i + W * H]) / (W * H) for i in range(0, len(data) - size + 1, size)]


@FF
class TestCadence(unittest.TestCase):
    """A luma-coded 16 fps clip through the built graph matches source_frame_at frame by frame."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="medialab-wp1-")
        cls.clip = os.path.join(cls.tmp, "luma.mp4")
        make_clip(cls.clip, 64, 64, 49, "4*N+20")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def check(self, F: int, a: int, b: int):
        from tests.wp1_fakes import vprobe
        r = ("gen", "luma.mp4")
        proj = {"items": [{"id": "i_1", "kind": "clip", "src": ref(r), "in": a, "out": b, "join": "cut"}],
                "export": {"preset": "9:16", "fit": "fill", "fps": F}}
        pl = timeline.plan(proj, {r: vprobe(64, 64, 49)}, paths={r: self.clip})
        W, H, fW, fH = export.output_dims("9:16", pl, 3)
        inputs, graph = export.build_video_graph(pl, W, H, fW, fH)
        lum = luma_frames(inputs, graph)
        self.assertEqual(len(lum), pl.T)
        got = [timeline.RHU(Fraction(round_luma(y) - 20, 4)) for y in lum]
        want = [a + min(b - a - 1, timeline.source_frame_at(k, Fraction(16), F)) for k in range(pl.T)]
        self.assertEqual(got, want)

    def test_24(self):
        self.check(24, 0, 49)

    def test_30(self):
        self.check(30, 0, 49)

    def test_trimmed_24(self):
        self.check(24, 5, 40)


def round_luma(y: float) -> int:
    return timeline.RHU(Fraction(repr(y)))


@FF
class TestWorkedExample(unittest.TestCase):
    """The §8.5 cut: 328 frames, transitions on 62..73 and 110..121, B pure at 74, a hard cut at 254."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="medialab-wp1-")
        p = lambda n: os.path.join(cls.tmp, n)
        make_clip(p("a.mp4"), 624, 624, 49, "60")
        make_png(p("b.png"), 1104, 1472, rgb=(200, 200, 200))
        make_clip(p("c.mp4"), 624, 624, 129, "120")
        make_clip(p("d.mp4"), 832, 480, 49, "30")
        from tests.wp1_fakes import A, B, C, D, EXAMPLE_PROBES, example_project
        cls.paths = {A: p("a.mp4"), B: p("b.png"), C: p("c.mp4"), D: p("d.mp4")}
        cls.plan = timeline.plan(example_project(music=False), EXAMPLE_PROBES, paths=cls.paths)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_frames_and_transitions(self):
        pl = self.plan
        self.assertEqual(pl.T, 328)
        W, H, fW, fH = export.output_dims("9:16", pl, 3)
        inputs, graph = export.build_video_graph(pl, W, H, fW, fH)
        lum = luma_frames(inputs, graph)
        self.assertEqual(len(lum), 328)
        a, b, c, d = lum[0], lum[74], lum[200], lum[300]
        self.assertTrue(abs(a - b) > 20 and abs(b - c) > 20 and abs(c - d) > 20)
        close = lambda x, y: abs(x - y) <= 1.0
        for k in range(0, 63):
            self.assertTrue(close(lum[k], a), (k, lum[k], a))          # 62 is the first transition frame: pure A
        for k in range(63, 74):
            self.assertTrue(min(a, b) + 1 < lum[k] < max(a, b) - 1, (k, lum[k]))
        for k in range(74, 111):
            self.assertTrue(close(lum[k], b), (k, lum[k], b))
        for k in range(111, 122):
            self.assertTrue(min(b, c) + 1 < lum[k] < max(b, c) - 1, (k, lum[k]))
        for k in range(122, 254):
            self.assertTrue(close(lum[k], c), (k, lum[k], c))
        for k in range(254, 328):
            self.assertTrue(close(lum[k], d), (k, lum[k], d))         # the hard cut lands on 254

    def test_blur_16x9_frame_count(self):
        """The golden 16:9 blurred-letterbox graph itself, at the final 1920x1080."""
        from tests.wp1_fakes import EXAMPLE_PROBES, example_project
        pl = timeline.plan(example_project(preset="16:9", fit="blur", music=False), EXAMPLE_PROBES, paths=self.paths)
        W, H, fW, fH = export.output_dims("16:9", pl, 1)
        self.assertEqual((W, H), (1920, 1080))
        inputs, graph = export.build_video_graph(pl, W, H, fW, fH)
        self.assertEqual(len(luma_frames(inputs, graph)), 328)


@FF
class TestMixedJoins(unittest.TestCase):
    """A fade that follows a hard cut: concat's 1/1000000 output must go back on 1/F before the xfade."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="medialab-wp1-")
        cls.refs = [("gen", f"j{i}.mp4") for i in range(4)]
        cls.paths = {}
        for i, r in enumerate(cls.refs):
            cls.paths[r] = os.path.join(cls.tmp, r[1])
            make_clip(cls.paths[r], 64, 64, 37, str(40 + 50 * i))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def check(self, joins: tuple[str, ...], F: int):
        from tests.wp1_fakes import vprobe
        items = [{"id": f"i_{i}", "kind": "clip", "src": ref(r), "in": 0, "out": None,
                  "join": (joins[i] if i < len(joins) else "cut")} for i, r in enumerate(self.refs)]
        proj = {"items": items, "export": {"preset": "9:16", "fit": "fill", "fps": F, "fade": 0.5}}
        pl = timeline.plan(proj, {r: vprobe(64, 64, 37) for r in self.refs}, paths=self.paths)
        W, H, fW, fH = export.output_dims("9:16", pl, 3)
        inputs, graph = export.build_video_graph(pl, W, H, fW, fH)
        lum = luma_frames(inputs, graph)
        self.assertEqual(len(lum), pl.T, (joins, F))
        for i, it in enumerate(pl.items):
            mid = it.start + it.D // 2
            self.assertTrue(abs(lum[mid] - (40 + 50 * i)) <= 2.0, (joins, F, i, mid, lum[mid]))

    def test_cut_fade_fade(self):
        for F in (24, 30):
            self.check(("cut", "fade", "fade"), F)

    def test_fade_cut_fade(self):
        for F in (24, 30):
            self.check(("fade", "cut", "fade"), F)


@FF
class TestRotation(unittest.TestCase):
    """ffmpeg autorotates on decode, so probe must report display-oriented w/h (phone portrait files)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="medialab-rot-")
        p = lambda n: os.path.join(cls.tmp, n)
        ff("-f", "lavfi", "-i", "testsrc=s=64x32:r=16", "-frames:v", "16", "-c:v", "libx264", "-pix_fmt", "yuv420p",
           p("plain.mp4"))
        ff("-display_rotation", "90", "-i", p("plain.mp4"), "-c", "copy", p("rot90.mp4"))
        ff("-f", "lavfi", "-i", "testsrc=s=64x32:r=16", "-frames:v", "1", p("plain.jpg"))
        with open(p("plain.jpg"), "rb") as f:
            jpg = f.read()
        tiff = (b"II*\x00" + struct.pack("<I", 8) + struct.pack("<H", 1)
                + struct.pack("<HHIHH", 0x0112, 3, 1, 6, 0) + struct.pack("<I", 0))
        app1 = b"Exif\x00\x00" + tiff
        with open(p("rot6.jpg"), "wb") as f:
            f.write(jpg[:2] + b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1 + jpg[2:])
        cls.p = staticmethod(p)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def decoded_size(self, path):
        out = self.p("frame.png")
        media.last_frame(path, out) if path.endswith(".mp4") else media.poster(path, out)
        pr = media._probe_uncached(out)
        return pr.w, pr.h

    def test_video_display_rotation(self):
        self.assertEqual((media._probe_uncached(self.p("plain.mp4")).w,
                          media._probe_uncached(self.p("plain.mp4")).h), (64, 32))
        pr = media._probe_uncached(self.p("rot90.mp4"))
        self.assertEqual((pr.w, pr.h), (32, 64))
        self.assertEqual((pr.w, pr.h), self.decoded_size(self.p("rot90.mp4")))

    def test_jpeg_exif_orientation(self):
        pr = media._probe_uncached(self.p("rot6.jpg"))
        self.assertEqual((pr.kind, pr.w, pr.h), ("image", 32, 64))
        self.assertEqual((pr.w, pr.h), self.decoded_size(self.p("rot6.jpg")))
        self.assertEqual((media._probe_uncached(self.p("plain.jpg")).w,
                          media._probe_uncached(self.p("plain.jpg")).h), (64, 32))


@FF
class TestRender(unittest.TestCase):
    """render_mix, run_render (proxy + final) and verify on temp roots, with store/provenance faked."""

    @classmethod
    def setUpClass(cls):
        cls.roots = TempRoots()
        media.configure(cls.roots.data)
        cls.store = FakeStore(cls.roots)
        cls.clip = ("gen", "clip_00001.mp4")
        cls.still = ("in", "ref_20261007-210000_abcdef.png")
        cls.song = ("lab", "up_20261007-210000_a1b2c3.flac")
        make_clip(cls.roots.path(*cls.clip), 96, 64, 49, "4*N+20")
        make_png(cls.roots.path(*cls.still), 64, 80, rgb=(40, 90, 200))
        make_song(cls.roots.path(*cls.song), 12.0)
        cls.store.own(cls.clip, kind="clip", commercial_ok=True, licences=["apache-2.0"])
        cls.store.own(cls.still, kind="upload", commercial_ok=None, licences=["upload"])
        cls.store.own(cls.song, kind="upload", commercial_ok=None, licences=["upload"])

    @classmethod
    def tearDownClass(cls):
        media._data_dir = None
        cls.roots.cleanup()

    def project(self, rev=3, preset="1:1", music=True):
        p = {"schema": "media-lab/project@1", "id": "p_0123456789ab", "name": "a;[b] test", "rev": rev,
             "items": [{"id": "i_1", "kind": "clip", "src": ref(self.clip), "in": 0, "out": None, "join": "fade"},
                       {"id": "i_2", "kind": "still", "src": ref(self.still), "dur": 1.5, "join": "cut"},
                       {"id": "i_3", "kind": "still", "src": {"root": "gen", "name": "gone_00001_.png"}, "dur": 0.5,
                        "join": "cut", "snap": {"w": 10, "h": 10, "sar": "1/1", "commercial_ok": False}}],
             "music": None, "export": {"preset": preset, "fit": "auto", "fps": 24, "fade": 0.5}}
        if music:
            p["music"] = {"src": ref(self.song), "volume": 0.8, "fade_in": 0.5, "fade_out": 1.0, "offset": 1.0}
        return p

    def test_mix_exact_and_loud(self):
        wav, n = media.conform_audio(self.roots.path(*self.song))
        self.assertTrue(abs(n - 12 * 48000) <= 2, n)
        self.assertEqual(media.conform_audio(self.roots.path(*self.song)), (wav, n))      # cached
        proj = self.project()
        probes = {self.clip: media.probe(self.roots.path(*self.clip)),
                  self.still: media.probe(self.roots.path(*self.still))}
        pl = timeline.plan(proj, probes, {self.song: (wav, n)}, paths={k: self.roots.path(*k) for k in probes})
        with self.store.patch():
            mix = export.render_mix(pl, FakeJob())
        self.assertEqual(media.wav_frames(mix["wav"]), pl.T_samples)
        self.assertEqual(mix["normalization_type"], "linear")
        self.assertLessEqual(abs(mix["checks"]["lufs"] - pl.music.target_i), 1.0, mix["checks"])
        self.assertEqual(export.render_mix(pl, FakeJob())["wav"], mix["wav"])               # cache hit

    def test_probe(self):
        p = media.probe(self.roots.path(*self.clip))
        self.assertEqual((p.kind, p.w, p.h, p.frames, p.rate, p.vfr), ("video", 96, 64, 49, Fraction(16), False))
        s = media.probe(self.roots.path(*self.still))
        self.assertEqual((s.kind, s.w, s.h, s.frames), ("image", 64, 80, 0))
        a = media.probe(self.roots.path(*self.song))
        self.assertEqual((a.kind, a.has_audio, a.audio_rate), ("audio", True, 44100))
        self.assertTrue(abs(a.audio_samples - 12 * 44100) <= 1)
        self.assertEqual(media.probe(self.roots.path(*self.clip)), p)                       # from the cache
        self.assertTrue(os.path.isfile(os.path.join(self.roots.data, "cache", "probe.json")))

    def test_proxy_and_final(self):
        proj = self.project()
        job = FakeJob()
        with self.store.patch(), fake_provenance():
            px = export.run_render(proj, 3, "proxy", job)
            self.assertFalse(px["cached"])
            self.assertTrue(px["url"].startswith("/m/cache/") and px["url"].endswith(".mp4"))
            ppath = os.path.join(self.roots.data, "cache", "proxy", px["key"] + ".mp4")
            pinfo = media.stream_info(ppath)
            self.assertEqual(pinfo["frames"], px["frames"])
            self.assertEqual((pinfo["w"], pinfo["h"]), (360, 360))
            self.assertIn("gone_00001_.png is missing; shown as black", px["warnings"])
            self.assertEqual(export.run_render(proj, 3, "proxy", FakeJob())["cached"], True)
            fin = export.run_render(proj, 3, "final", FakeJob())
            with self.assertRaises(Conflict):
                export.run_render(proj, 2, "final", FakeJob())
        self.assertEqual(job.phases[0], "probe")
        self.assertIn("video", job.phases)
        name = fin["ref"]["name"]
        path = self.roots.path("lab", name)
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(os.path.isfile(self.roots.path("lab", name[:-4] + ".jpg")))
        info = media.stream_info(path)
        self.assertEqual(info["frames"], pinfo["frames"])                                   # proxy == final
        self.assertEqual((info["w"], info["h"], info["pix_fmt"]), (1080, 1080, "yuv420p"))
        self.assertEqual(media.probe(path).rate, Fraction(24))
        self.assertLessEqual(abs(info["audio_seconds"] - fin["checks"]["frames_expected"] / 24), 1024 / 48000)
        self.assertTrue(fin["checks"]["ok"], fin["checks"])
        self.assertFalse(fin["commercial_ok"])            # the missing still's snap is False
        with open(path, "rb") as f:
            head = f.read()
        self.assertLess(head.find(b"moov"), head.find(b"mdat"))                             # +faststart
        side = self.store.sidecar(("lab", name))
        self.assertEqual(side["kind"], "export")
        self.assertEqual(side["render"]["frames"], info["frames"])
        self.assertEqual(side["render"]["checks"]["frames_actual"], info["frames"])
        self.assertEqual(side["poster"], name[:-4] + ".jpg")
        tags = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags=title,comment", "-of",
                               "default=nw=1", path], capture_output=True, text=True).stdout
        self.assertIn("title=a;[b] test", tags)
        self.assertIn(f"comment=YourContent export {name}", tags)
        self.assertEqual(os.listdir(os.path.join(self.roots.data, "tmp")), [])               # nothing left behind

    def test_cancel_during_finish_keeps_nothing(self):
        """A cancel after the last ffmpeg step but before the commit discards the render: no cut file, no poster,
        no sidecar (the job then ends "cancelled" truthfully)."""
        from medialab import jobs
        proj = self.project(music=False)
        job = FakeJob()
        before = set(os.listdir(self.roots.dir("lab")))
        real_verify = export.verify

        def verify_then_cancel(path, pl):
            out = real_verify(path, pl)
            job.cancel()
            return out
        with self.store.patch(), fake_provenance(), mock.patch.object(export, "verify", verify_then_cancel):
            with self.assertRaises(jobs.Cancelled):
                export.run_render(proj, 3, "final", job)
        self.assertEqual(set(os.listdir(self.roots.dir("lab"))), before)
        self.assertEqual(os.listdir(os.path.join(self.roots.data, "tmp")), [])

    def test_failure_after_poster_leaves_nothing_in_lab(self):
        """Review r2: the poster goes to a temporary first, so a probe failure leaves no orphan in lab/."""
        proj = self.project(music=False)
        before = set(os.listdir(self.roots.dir("lab")))
        real_probe = media.probe

        def probe(path):
            if path.startswith(os.path.join(self.roots.data, "tmp")) and path.endswith(".mp4"):
                raise media.FFmpegError("ffprobe timed out")
            return real_probe(path)
        with self.store.patch(), fake_provenance(), mock.patch.object(media, "probe", probe):
            with self.assertRaises(media.FFmpegError):
                export.run_render(proj, 3, "final", FakeJob())
        self.assertEqual(set(os.listdir(self.roots.dir("lab"))), before)
        self.assertEqual(os.listdir(os.path.join(self.roots.data, "tmp")), [])

    def test_sidecar_failure_removes_the_cut_and_poster(self):
        """Review r2: a failed sidecar write would leave unowned files in lab/; they are removed instead."""
        proj = self.project(music=False)
        before = set(os.listdir(self.roots.dir("lab")))

        def boom(*a, **k):
            raise OSError(28, "No space left on device")
        with self.store.patch(), fake_provenance(), mock.patch("medialab.store.put_sidecar", boom):
            with self.assertRaises(OSError):
                export.run_render(proj, 3, "final", FakeJob())
        self.assertEqual(set(os.listdir(self.roots.dir("lab"))), before)

    def test_no_music_and_verify_flags(self):
        proj = self.project(music=False)
        with self.store.patch(), fake_provenance():
            px = export.run_render(proj, 3, "proxy", FakeJob())
            path = os.path.join(self.roots.data, "cache", "proxy", px["key"] + ".mp4")
            self.assertIsNone(media.stream_info(path)["audio_seconds"])
            probes = {self.clip: media.probe(self.roots.path(*self.clip)),
                      self.still: media.probe(self.roots.path(*self.still))}
            pl = timeline.plan(proj, probes, paths={k: self.roots.path(*k) for k in probes})
            self.assertTrue(export.verify(path, pl)["ok"])
            bad = export.verify(path, replace(pl, T=pl.T + 1))
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["frames_expected"], pl.T + 1)

    def test_cancel_kills(self):
        job = FakeJob()
        job.cancel()
        with self.assertRaises(media.Cancelled):
            media.run_ffmpeg(["ffmpeg", "-nostdin", "-f", "lavfi", "-i", "color=s=64x64:r=24", "-t", "600", "-f",
                              "null", "-"], 30, job=job)
        self.assertEqual(len(job.procs), 1)
        self.assertIsNotNone(job.procs[0].returncode)

    def test_poster_last_frame_stitch(self):
        clip = self.roots.path(*self.clip)
        out = os.path.join(self.roots.base, "p.jpg")
        media.poster(clip, out, 10)
        self.assertEqual(media.probe(out).kind, "image")
        lf = os.path.join(self.roots.base, "last.png")
        media.last_frame(clip, lf)
        self.assertEqual((media.probe(lf).w, media.probe(lf).h), (96, 64))
        dest = os.path.join(self.roots.base, "ext.mp4")
        media.stitch_extension(clip, clip, dest)
        self.assertEqual(media.probe(dest).frames, 49 + 48)

    def test_stitch_honours_cancel(self):
        clip = self.roots.path(*self.clip)
        job = FakeJob()
        job.cancel()
        with self.assertRaises(media.Cancelled):
            media.stitch_extension(clip, clip, os.path.join(self.roots.base, "ext2.mp4"), job=job)

    def test_caps(self):
        caps = media.ffmpeg_caps()
        self.assertRegex(caps["version"] or "", r"^7\.1")
        self.assertEqual(caps["missing_filters"], [])
        self.assertEqual(caps["missing_encoders"], [])


class TestMediaPure(unittest.TestCase):
    """media helpers that need no ffmpeg."""

    def test_sniff(self):
        cases = {
            b"\x89PNG\r\n\x1a\n" + b"\0" * 8: "png", b"\xff\xd8\xff\xe0" + b"\0" * 8: "jpg",
            b"RIFF\0\0\0\0WEBPVP8 ": "webp", b"RIFF\0\0\0\0WAVEfmt ": "wav",
            b"\0\0\0\x18ftypisom\0\0\0\0": "mp4", b"\0\0\0\x14ftypqt  \0\0\0\0": "mov",
            b"\0\0\0\x18ftypM4A \0\0\0\0": "m4a", b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01\x42\x82\x84webm": "webm",
            b"ID3\x04\0\0\0\0\0\0": "mp3", b"\xff\xfb\x90\x64\0\0": "mp3",
            b"\xff\xf1\x50\x80\0\0": None, b"MZ\x90\0\x03\0\0\0": None, b"": None,
            b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01\x42\x82\x88matroska": None,
        }
        for head, want in cases.items():
            self.assertEqual(media.sniff(head), want, head)

    def test_wav_frames(self):
        import struct
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "f.wav")
            data = b"\0" * (8 * 1000)
            fmt = struct.pack("<HHIIHH", 3, 2, 48000, 48000 * 8, 8, 32)
            with open(p, "wb") as f:
                f.write(b"RIFF" + struct.pack("<I", 4 + 8 + len(fmt) + 8 + 6 + 8 + len(data)) + b"WAVE")
                f.write(b"fmt " + struct.pack("<I", len(fmt)) + fmt)
                f.write(b"LIST" + struct.pack("<I", 6) + b"INFOxx")
                f.write(b"data" + struct.pack("<I", len(data)) + data)
            self.assertEqual(media.wav_frames(p), 1000)

    def test_parse_caps(self):
        ver = "ffmpeg version 7.1.5-0+deb13u1 Copyright (c) 2000-2026 the FFmpeg developers\n"
        flt = " T.C xfade             VV->V      Cross fade one video with another video.\n" \
              " ... trim              V->V       Pick one continuous section from the input.\n"
        enc = " V....D libx264              libx264 H.264\n A....D aac                  AAC\n"
        caps = media.parse_caps(ver, flt, enc)
        self.assertEqual(caps["version"], "7.1.5")
        self.assertNotIn("xfade", caps["missing_filters"])
        self.assertIn("loudnorm", caps["missing_filters"])
        self.assertNotIn("libx264", caps["missing_encoders"])
        self.assertIn("mjpeg", caps["missing_encoders"])

    def test_data_dir_required(self):
        old, oldenv = media._data_dir, os.environ.pop("MEDIA_LAB_DATA", None)
        try:
            media._data_dir = None
            with self.assertRaises(RuntimeError):
                media.data_dir()
            media.configure("/x/y")
            self.assertEqual(media.data_dir(), "/x/y")
        finally:
            media._data_dir = old
            if oldenv is not None:
                os.environ["MEDIA_LAB_DATA"] = oldenv


if __name__ == "__main__":
    unittest.main()
