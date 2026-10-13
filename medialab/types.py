"""Shared value types (docs/DESIGN.md §4). Written in WP0 and frozen afterwards.

A contract change goes back to docs/DESIGN.md first; no work package edits this file on its own.
This module imports nothing from the rest of the package and has no side effects.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

Ref = tuple[str, str]
"""A file address: (root, name). JSON form {"root": ..., "name": ...}. Never a path."""


@dataclass(frozen=True)
class Probe:
    """media.probe() result."""
    kind: str               # "image" | "video" | "audio"
    w: int                  # 0 for audio
    h: int                  # 0 for audio
    sar: Fraction           # 0 for audio
    frames: int             # video frames (0 for images/audio)
    rate: Fraction          # e.g. Fraction(16, 1); 0 for images/audio
    seconds: float
    has_audio: bool
    audio_rate: int
    audio_samples: int
    vcodec: str
    acodec: str
    color: str              # "bt709" | "unknown"
    vfr: bool = False       # r_frame_rate != avg_frame_rate (§7: uploads are conformed to CFR)


@dataclass(frozen=True)
class ItemPlan:
    id: str
    kind: str               # "clip" | "still" | "missing"
    ref: Ref
    path: str | None        # None when missing
    src_in: int
    src_out: int
    rate: Fraction
    w: int
    h: int
    sar: Fraction
    D: int                  # output frames
    X_after: int            # crossfade frames into the next item (0 = cut, and on the last item)
    start: int              # output frame where this item starts (S_k)
    fit: str                # "fill" | "blur"
    commercial_ok: bool | None


@dataclass(frozen=True)
class MusicPlan:
    ref: Ref
    wav: str | None         # conformed 48 kHz WAV path; None when not yet conformed / missing
    n_samples: int
    skip: int
    delay: int
    end: int
    fade_in: int
    fade_out: int
    volume: float
    target_i: float


@dataclass(frozen=True)
class Plan:
    F: int
    items: tuple[ItemPlan, ...]
    T: int                  # output frames
    T_samples: int          # T * 48000 / F, always an integer
    preset: str
    fit: str
    music: MusicPlan | None
    warnings: tuple[str, ...]
    missing: tuple[str, ...]  # item ids


class BadRef(ValueError):
    """A ref, id or name that fails validation. HTTP 400."""
    status = 400


class NotOwned(LookupError):
    """A valid ref that YourContent does not own (no sidecar) or that does not exist. HTTP 404."""
    status = 404


class Conflict(Exception):
    """A stale project rev. `current` is what the server holds now. HTTP 409."""
    status = 409

    def __init__(self, current: object = None, message: str = "this cut was changed elsewhere"):
        super().__init__(message)
        self.current = current
