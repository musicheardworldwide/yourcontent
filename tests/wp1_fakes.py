"""WP1 test helpers: probes, the §8.5 worked example, and stand-ins for store/provenance (WP2/WP3 own the real ones).

The fakes are patched in with unittest.mock so the edit engine is tested on its own, against §4 signatures only.
"""
from __future__ import annotations

import itertools
import os
from fractions import Fraction
from unittest import mock

from medialab.types import Probe


def vprobe(w: int, h: int, frames: int, rate=Fraction(16), sar=Fraction(1)) -> Probe:
    return Probe(kind="video", w=w, h=h, sar=sar, frames=frames, rate=Fraction(rate), seconds=frames / float(rate),
                 has_audio=False, audio_rate=0, audio_samples=0, vcodec="h264", acodec="", color="bt709")


def iprobe(w: int, h: int, sar=Fraction(1)) -> Probe:
    return Probe(kind="image", w=w, h=h, sar=sar, frames=0, rate=Fraction(0), seconds=0.0, has_audio=False,
                 audio_rate=0, audio_samples=0, vcodec="png", acodec="", color="unknown")


def aprobe(seconds: float, rate: int = 48000) -> Probe:
    return Probe(kind="audio", w=0, h=0, sar=Fraction(0), frames=0, rate=Fraction(0), seconds=seconds,
                 has_audio=True, audio_rate=rate, audio_samples=int(seconds * rate), vcodec="", acodec="mp3",
                 color="unknown")


A = ("out", "qwen_video_00001.mp4")
B = ("out", "qwen_studio_00009_.png")
C = ("out", "qwen_video_ext_20261008-000542_3cea20.mp4")
D = ("gen", "clip_00001.mp4")
M = ("lab", "up_20261007-210000_a1b2c3.mp3")


def ref(r):
    return {"root": r[0], "name": r[1]}


def example_project(preset="9:16", fit="fill", fps=24, fade=0.5, music=True) -> dict:
    """The §8.5 worked example: 3 clips + 1 still, two crossfades, music."""
    p = {
        "schema": "media-lab/project@1", "id": "p_3f9a0c1b2d4e", "name": "Untitled cut", "rev": 12,
        "items": [
            {"id": "i_9c2a1f", "kind": "clip", "src": ref(A), "in": 0, "out": None, "join": "fade",
             "snap": {"frames": 49, "rate": "16/1", "w": 624, "h": 624, "sar": "1/1", "commercial_ok": False}},
            {"id": "i_0b7e44", "kind": "still", "src": ref(B), "dur": 2.5, "join": "fade",
             "snap": {"w": 1104, "h": 1472, "sar": "1/1", "commercial_ok": False}},
            {"id": "i_51c0aa", "kind": "clip", "src": ref(C), "in": 16, "out": 112, "join": "cut",
             "snap": {"frames": 129, "rate": "16/1", "w": 624, "h": 624, "sar": "1/1", "commercial_ok": False}},
            {"id": "i_d00d01", "kind": "clip", "src": ref(D), "in": 0, "out": 49, "join": "cut",
             "snap": {"frames": 49, "rate": "16/1", "w": 832, "h": 480, "sar": "1/1", "commercial_ok": True}},
        ],
        "music": None,
        "export": {"preset": preset, "fit": fit, "fps": fps, "fade": fade},
    }
    if music:
        p["music"] = {"src": ref(M), "volume": 0.8, "fade_in": 0.5, "fade_out": 1.5, "offset": 2.0}
    return p


EXAMPLE_PROBES = {A: vprobe(624, 624, 49), B: iprobe(1104, 1472), C: vprobe(624, 624, 129), D: vprobe(832, 480, 49)}
EXAMPLE_PATHS = {A: "/output/qwen_video_00001.mp4", B: "/output/qwen_studio_00009_.png",
                 C: "/output/qwen_video_ext_20261008-000542_3cea20.mp4", D: "/output/media_lab/clip_00001.mp4"}
EXAMPLE_WAVS = {M: ("/data/cache/music/7c1e00000000.wav", 1_440_000)}


# --------------------------------------------------------------------------------------------- fake store

class FakeStore:
    """Just enough of medialab.store over a tests.util.TempRoots tree. Owned = has a sidecar here."""

    def __init__(self, roots):
        self.roots = roots
        self.sidecars: dict = {}
        self._n = itertools.count(1)

    def own(self, ref, **side) -> None:
        self.sidecars[tuple(ref)] = {"root": ref[0], "name": ref[1], **side}

    def owned(self, ref) -> bool:
        return tuple(ref) in self.sidecars

    def resolve(self, ref) -> str:
        return self.roots.path(ref[0], ref[1])

    def sidecar(self, ref):
        return self.sidecars.get(tuple(ref))

    def put_sidecar(self, ref, data) -> None:
        self.sidecars[tuple(ref)] = data

    def new_name(self, prefix: str, ext: str) -> str:
        return f"{prefix}_20261007-210000_{next(self._n):06x}.{ext}"

    def tmp_path(self, suffix: str) -> str:
        d = os.path.join(self.roots.data, "tmp")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f".ml_{next(self._n):06x}{suffix}")

    def patch(self):
        return mock.patch.multiple("medialab.store", owned=self.owned, resolve=self.resolve, sidecar=self.sidecar,
                                   put_sidecar=self.put_sidecar, new_name=self.new_name, tmp_path=self.tmp_path)


# --------------------------------------------------------------------------------------------- fake provenance

def _input_entry(role, ref, sidecar, snap_ok=None):
    ok = sidecar.get("commercial_ok", snap_ok) if sidecar else snap_ok
    return {"role": role, "root": ref[0], "name": ref[1], "commercial_ok": ok,
            "licences": (sidecar or {}).get("licences", []), "missing": sidecar is None}


def _aggregate(models, inputs):
    oks = [e.get("commercial_ok") for e in list(models) + list(inputs)]
    ok = False if False in oks else (None if None in oks else True)
    lic = sorted({x for e in list(models) + list(inputs) for x in e.get("licences", [])})
    return {"commercial_ok": ok, "licences": lic, "most_restrictive": lic[0] if lic else None, "restricted_by": []}


def _make_sidecar(kind, ref, **kw):
    return {"schema": "media-lab/asset@1", "root": ref[0], "name": ref[1], "kind": kind,
            "created": "2026-10-07T21:00:00Z", **{k: v for k, v in kw.items() if k != "probe"}}


def fake_provenance():
    return mock.patch.multiple("medialab.provenance", input_entry=_input_entry, aggregate=_aggregate,
                               make_sidecar=_make_sidecar, mp4_comment=lambda agg, name: f"YourContent export {name}",
                               warnings=lambda agg, n: [])
