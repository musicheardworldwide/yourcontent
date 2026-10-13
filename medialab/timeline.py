"""PURE validator and planner: integer frames and samples, Fraction maths, round-half-up (§7). Owner: WP1.

Nothing here touches files, the clock or the network: every input arrives as an argument. All timing is
done in `fractions.Fraction`; `RHU` (round half up) is the only rounding, Python's `round()` is never used.
"""
from __future__ import annotations

import math
import re
from dataclasses import replace
from fractions import Fraction
from typing import Mapping

from .types import ItemPlan, MusicPlan, Plan, Probe, Ref

SR = 48000
MAX_ITEMS = 60
MAX_NAME = 80
FPS_CHOICES = (24, 30)
FADE_CHOICES = (Fraction(1, 4), Fraction(1, 2), Fraction(1))
PRESET_DIMS: dict[str, tuple[int, int]] = {
    "9:16": (1080, 1920), "4:5": (1080, 1350), "1:1": (1080, 1080), "16:9": (1920, 1080),
}
PRESETS = ("9:16", "4:5", "1:1", "16:9", "source")
FITS = ("auto", "fill", "blur")
SOURCE_LONG_SIDE = 1920
STILL_DEFAULT = Fraction(5, 2)
STILL_MIN, STILL_MAX = Fraction(1, 2), Fraction(30)
FILL_TOLERANCE = Fraction(6, 5)          # auto fit: fill when the aspect ratios are within 1.2x

# A ref name, as store.NAME_RE (duplicated so this module stays pure and import-free).
_ROOTS = ("out", "gen", "in", "lab")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
_ITEM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_PID_RE = re.compile(r"^p_[0-9a-f]{12}$")
_RATE_RE = re.compile(r"^\d{1,9}(/\d{1,9})?$")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


# --------------------------------------------------------------------------------------------- numbers

def RHU(x) -> int:
    """Round half up: floor(x + 1/2). Python's round() is never used."""
    return math.floor(_q(x) + Fraction(1, 2))


def even(x) -> int:
    """2 * RHU(x / 2)."""
    return 2 * RHU(_q(x) / 2)


def _q(x) -> Fraction:
    """A Fraction from an int, Fraction, decimal-looking float or "a/b" / "1.5" string (exactly)."""
    if isinstance(x, Fraction):
        return x
    if isinstance(x, bool):
        raise TypeError("bool is not a number")
    if isinstance(x, int):
        return Fraction(x)
    if isinstance(x, float):
        if not math.isfinite(x):
            raise ValueError("not a finite number")
        return Fraction(repr(x))          # the decimal the JSON wrote, not the binary approximation
    if isinstance(x, str):
        return Fraction(x.strip())
    raise TypeError(f"not a number: {type(x).__name__}")


def ceil_q(x: Fraction) -> int:
    return -((-_q(x)) // 1)


def dec(x, places: int) -> str:
    """Fixed-point text of x with `places` decimals, rounded half up (never round(), never float format)."""
    x = _q(x)
    scale = 10 ** places
    n = RHU(x * scale)
    sign = "-" if n < 0 else ""
    n = abs(n)
    if places == 0:
        return f"{sign}{n}"
    return f"{sign}{n // scale}.{n % scale:0{places}d}"


def rate_str(r: Fraction) -> str:
    r = _q(r)
    return f"{r.numerator}/{r.denominator}"


def target_i(volume) -> float:
    """Loudnorm target for a volume: -14 + 20*log10(clamp(volume, 0.05, 1.0)), to 0.01 LU (half up)."""
    v = min(max(float(volume), 0.05), 1.0)
    return float(Fraction(RHU(Fraction(-14 + 20 * math.log10(v)) * 100), 100))


# --------------------------------------------------------------------------------------------- validate

def _num(v, default, lo, hi):
    """A clamped Fraction from a JSON number, or the default when it is not a usable number."""
    try:
        if v is None or isinstance(v, (bool, str)):
            raise TypeError
        q = _q(v)
    except (TypeError, ValueError, ZeroDivisionError):
        return _q(default)
    return min(max(q, _q(lo)), _q(hi))


def _int(v, default, lo, hi) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return default
    try:
        n = math.floor(_q(v))
    except (TypeError, ValueError):
        return default
    return min(max(n, lo), hi)


def _jnum(q: Fraction):
    """Back to a JSON number: int when whole, else float."""
    return int(q) if q.denominator == 1 else float(q)


def _ref(obj) -> Ref | None:
    if isinstance(obj, dict):
        root, name = obj.get("root"), obj.get("name")
    elif isinstance(obj, (list, tuple)) and len(obj) == 2:
        root, name = obj
    else:
        return None
    if root not in _ROOTS or not isinstance(name, str) or ".." in name or not _NAME_RE.match(name):
        return None
    return (root, name)


def _ref_json(ref: Ref) -> dict:
    return {"root": ref[0], "name": ref[1]}


def _clean_text(s, limit: int) -> str:
    if not isinstance(s, str):
        return ""
    return _CTRL_RE.sub("", s)[:limit]


def _snap(s) -> dict | None:
    if not isinstance(s, dict):
        return None
    out: dict = {}
    for k in ("frames", "w", "h"):
        v = s.get(k)
        if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 10_000_000:
            out[k] = v
    for k in ("rate", "sar"):
        v = s.get(k)
        if isinstance(v, str) and _RATE_RE.match(v):
            try:
                if _q(v) > 0:
                    out[k] = rate_str(_q(v))
            except (ValueError, ZeroDivisionError):
                pass
    ok = s.get("commercial_ok")
    out["commercial_ok"] = ok if isinstance(ok, bool) else None
    return out


def _fade_choice(v) -> Fraction:
    q = _num(v, Fraction(1, 2), Fraction(1, 4), Fraction(1))
    return min(FADE_CHOICES, key=lambda c: (abs(c - q), c))


def validate(project: dict) -> tuple[dict, list[str]]:
    """Clamp a project to the §5.3 rules and drop unknown keys.

    Returns (clean project, errors). Errors are only for unusable input (an item that cannot be used, more
    than 60 items, a project that is not an object); everything else is clamped silently.
    """
    errors: list[str] = []
    if not isinstance(project, dict):
        project, errors = {}, ["a cut must be a JSON object"]
    clean: dict = {"schema": "media-lab/project@1"}
    pid = project.get("id")
    if isinstance(pid, str) and _PID_RE.match(pid):
        clean["id"] = pid
    clean["name"] = _clean_text(project.get("name"), 10 * MAX_NAME).strip()[:MAX_NAME].strip() or "Untitled cut"
    rev = project.get("rev")
    if isinstance(rev, int) and not isinstance(rev, bool) and rev >= 0:
        clean["rev"] = rev
    for k in ("created", "updated"):
        if isinstance(project.get(k), str):
            clean[k] = _clean_text(project[k], 40)

    raw_items = project.get("items", [])
    if not isinstance(raw_items, list):
        errors.append("items must be a list")
        raw_items = []
    if len(raw_items) > MAX_ITEMS:
        errors.append(f"a cut can hold at most {MAX_ITEMS} items")
        raw_items = raw_items[:MAX_ITEMS]
    items, seen = [], set()
    for n, it in enumerate(raw_items, 1):
        if not isinstance(it, dict):
            errors.append(f"item {n} is not usable")
            continue
        kind = it.get("kind")
        ref = _ref(it.get("src"))
        iid = it.get("id")
        if kind not in ("clip", "still") or ref is None:
            errors.append(f"item {n} is not usable")
            continue
        if not (isinstance(iid, str) and _ITEM_ID_RE.match(iid)) or iid in seen:
            iid = f"i_{n:06x}"
            while iid in seen:
                iid += "x"
        seen.add(iid)
        c: dict = {"id": iid, "kind": kind, "src": _ref_json(ref)}
        if kind == "clip":
            c["in"] = _int(it.get("in"), 0, 0, 10_000_000)
            out = it.get("out")
            c["out"] = None if out is None else max(_int(out, c["in"] + 2, 0, 10_000_002), c["in"] + 2)
        else:
            c["dur"] = _jnum(_num(it.get("dur"), STILL_DEFAULT, STILL_MIN, STILL_MAX))
        c["join"] = it.get("join") if it.get("join") in ("cut", "fade") else "cut"
        snap = _snap(it.get("snap"))
        if snap is not None:
            c["snap"] = snap
        items.append(c)
    clean["items"] = items

    music = project.get("music")
    clean["music"] = None
    if music is not None:
        ref = _ref(music.get("src")) if isinstance(music, dict) else None
        if ref is None:
            errors.append("the music track is not usable")
        else:
            m: dict = {"src": _ref_json(ref),
                       "volume": float(_num(music.get("volume"), 1, Fraction(1, 20), 1)),
                       "fade_in": _jnum(_num(music.get("fade_in"), Fraction(1, 2), 0, 10)),
                       "fade_out": _jnum(_num(music.get("fade_out"), Fraction(3, 2), 0, 10)),
                       "offset": _jnum(_num(music.get("offset"), 0, -600, 600))}
            gen = music.get("gen")
            if isinstance(gen, dict):
                g: dict = {}
                for k, lim in (("tags", 1000), ("lyrics", 4000), ("keyscale", 40), ("timesig", 4),
                               ("language", 16)):
                    if isinstance(gen.get(k), str):
                        g[k] = _clean_text(gen[k], lim) if k != "lyrics" else gen[k][:lim]
                if "bpm" in gen:
                    g["bpm"] = _int(gen.get("bpm"), 120, 10, 300)
                if "seconds" in gen:
                    g["seconds"] = float(_num(gen.get("seconds"), 5, 1, 1000))
                if isinstance(gen.get("seed"), int) and not isinstance(gen.get("seed"), bool):
                    g["seed"] = min(max(gen["seed"], 0), 2**64 - 1)
                m["gen"] = g
            clean["music"] = m

    ex = project.get("export") if isinstance(project.get("export"), dict) else {}
    fps = ex.get("fps")
    clean["export"] = {
        "preset": ex.get("preset") if ex.get("preset") in PRESETS else "9:16",
        "fit": ex.get("fit") if ex.get("fit") in FITS else "auto",
        "fps": fps if fps in FPS_CHOICES and not isinstance(fps, bool) else 24,
        "fade": float(_fade_choice(ex.get("fade"))),
    }
    return clean, errors


# --------------------------------------------------------------------------------------------- pieces

def item_frames(n_src: int, rate: Fraction, F: int) -> int:
    """Output frames for n source frames at source rate r: RHU(n*F/r)."""
    return RHU(Fraction(n_src) * F / _q(rate))


def still_frames(dur, F: int) -> int:
    return max(1, RHU(_q(dur) * F))


def clamp_xfades(D: list[int], X: list[int]) -> tuple[list[int], list[str]]:
    """Clamp joins left to right: X_k = min(X_k, D_k-1-X_{k-1}, D_{k+1}-1); below 2 frames becomes a cut.

    X[k] is the join after item k; the last one is always 0. Warnings name items 1-based.
    """
    n = len(D)
    X = list(X) + [0] * max(0, n - len(X))
    out: list[int] = []
    warns: list[str] = []
    prev = 0
    for k in range(n):
        want = X[k] if k < n - 1 else 0
        x = want
        if want > 0:
            x = min(want, D[k] - 1 - prev, D[k + 1] - 1)
            if x < 2:
                x = 0
                warns.append(f"crossfade after item {k + 1} became a cut (item too short)")
            elif x < want:
                warns.append(f"crossfade after item {k + 1} shortened to {x} frames (item too short)")
        out.append(x)
        prev = x
    return out[:n], warns


def music_window(music: dict, n_m: int, T_samples: int, F: int) -> tuple[dict | None, list[str]]:
    """Sample window of the music on the cut (§7.4).

    Returns (fields, warnings): fields are the MusicPlan values {skip, delay, end, fade_in, fade_out, volume,
    target_i}, or None when the music would not be heard at all.
    """
    warns: list[str] = []
    offset = _num(music.get("offset"), 0, -600, 600)
    if offset >= 0:
        skip, delay = RHU(offset * SR), 0
    else:
        skip, delay = 0, RHU(-offset * SR)
    end = min(T_samples, delay + n_m - skip)
    region = end - delay
    if region <= 0:
        return None, ["music is not heard (start is past the end)"]
    fi = RHU(_num(music.get("fade_in"), Fraction(1, 2), 0, 10) * SR)
    fo = RHU(_num(music.get("fade_out"), Fraction(3, 2), 0, 10) * SR)
    if fi + fo > region:
        tot = fi + fo
        fi, fo = fi * region // tot, fo * region // tot
        warns.append("music fades shortened to fit the music")
    if end < T_samples - SR // 2:
        warns.append(f"music ends {dec(Fraction(T_samples - end, SR), 1)} s before the cut")
    volume = float(_num(music.get("volume"), 1, Fraction(1, 20), 1))
    return ({"skip": skip, "delay": delay, "end": end, "fade_in": fi, "fade_out": fo,
             "volume": volume, "target_i": target_i(volume)}, warns)


def ace_seconds(T_samples: int, music: dict | None) -> float:
    """Default generated track length: the cut plus any skipped intro, minus any delay, to 0.04 s, 5..240 s."""
    music = music or {}
    offset = _num(music.get("offset"), 0, -600, 600)
    skip = RHU(offset * SR) if offset >= 0 else 0
    delay = RHU(-offset * SR) if offset < 0 else 0
    need = Fraction(T_samples - delay + skip, SR)
    secs = Fraction(ceil_q(need * 25), 25)
    return float(min(max(secs, Fraction(5)), Fraction(240)))


def auto_fit(sw: int, sh: int, sar: Fraction, W: int, H: int) -> str:
    """"fill" when |ln((sw*sar/sh)/(W/H))| <= ln(1.2), else "blur" (exact, no logs)."""
    sar = _q(sar) if sar else Fraction(1)
    if sw <= 0 or sh <= 0 or sar <= 0:
        return "fill"
    r = Fraction(sw) * sar * H / (Fraction(sh) * W)
    return "fill" if 1 / FILL_TOLERANCE <= r <= FILL_TOLERANCE else "blur"


def source_frame_at(k: int, rate: Fraction, F: int) -> int:
    """The source frame the fps filter shows at output frame k: max i with RHU(i*F/r) <= k."""
    # RHU(y) <= k  <=>  y < k + 1/2  <=>  i < (k + 1/2) * r / F
    return ceil_q((Fraction(k) + Fraction(1, 2)) * _q(rate) / F) - 1


def preset_dims(preset: str, items) -> tuple[int, int]:
    """Final (W, H) for a preset. `source` uses the first non-missing item's display size, long side <= 1920."""
    if preset in PRESET_DIMS:
        return PRESET_DIMS[preset]
    first = next((it for it in items if it.kind != "missing" and it.w > 0 and it.h > 0), None)
    if first is None:
        first = next((it for it in items if it.w > 0 and it.h > 0), None)
    if first is None:
        return PRESET_DIMS["9:16"]
    sar = _q(first.sar) if first.sar else Fraction(1)
    dw, dh = Fraction(first.w) * sar, Fraction(first.h)
    s = min(Fraction(1), Fraction(SOURCE_LONG_SIDE) / max(dw, dh))
    return max(2, even(dw * s)), max(2, even(dh * s))


# --------------------------------------------------------------------------------------------- plan

def _key(ref_obj) -> Ref:
    r = _ref(ref_obj)
    assert r is not None
    return r


def plan(project: dict, probes: Mapping[Ref, Probe | None], wavs: Mapping[Ref, tuple[str, int]] = {},
         paths: Mapping[Ref, str] | None = None) -> Plan:
    """Plan a cut in integer output frames and samples (§7).

    probes: a Probe per ref, or None (or absent) when the source is missing; a missing item renders from its
    `snap` as black. wavs: conformed music (wav path, n_samples) per ref. paths: the resolved file path per
    ref (WP1 addition, optional); ItemPlan.path is taken from it and is None for a missing item.
    """
    clean, _errors = validate(project)
    paths = paths or {}
    ex = clean["export"]
    F = ex["fps"]
    fade = _q(ex["fade"])
    warnings: list[str] = []
    missing: list[str] = []

    pre: list[dict] = []
    for it in clean["items"]:
        ref = _key(it["src"])
        pr = probes.get(ref)
        snap = it.get("snap") or {}
        name = ref[1]
        entry = {"id": it["id"], "ref": ref, "join": it["join"], "commercial_ok": snap.get("commercial_ok")}
        usable = pr is not None and (
            (it["kind"] == "clip" and pr.kind == "video" and pr.frames >= 1 and pr.rate and pr.rate > 0)
            or (it["kind"] == "still" and pr.kind == "image" and pr.w > 0 and pr.h > 0))
        if usable:
            sar = pr.sar if pr.sar and pr.sar > 0 else Fraction(1)
            if it["kind"] == "clip":
                n_src = pr.frames
                a = min(it["in"], max(0, n_src - 2))
                b = n_src if it["out"] is None else min(it["out"], n_src)
                b = max(b, min(a + 2, n_src))
                rate = _q(pr.rate)
                entry.update(kind="clip", src_in=a, src_out=b, rate=rate, D=item_frames(b - a, rate, F))
            else:
                entry.update(kind="still", src_in=0, src_out=1, rate=Fraction(0),
                             D=still_frames(it["dur"], F))
            entry.update(w=pr.w, h=pr.h, sar=sar, path=paths.get(ref))
        else:
            missing.append(it["id"])
            warnings.append(f"{name} is missing; shown as black")
            sar = _q(snap["sar"]) if snap.get("sar") else Fraction(1)
            entry.update(kind="missing", w=snap.get("w", 0), h=snap.get("h", 0), sar=sar, path=None)
            if it["kind"] == "clip":
                rate = _q(snap["rate"]) if snap.get("rate") else Fraction(16)
                n_src = snap.get("frames")
                if n_src is None:
                    n_src = it["out"] if it["out"] is not None else it["in"] + 2
                a = min(it["in"], max(0, n_src - 2))
                b = n_src if it["out"] is None else min(it["out"], n_src)
                b = max(b, min(a + 2, n_src), a + 1)
                entry.update(src_in=a, src_out=b, rate=rate, D=item_frames(b - a, rate, F))
            else:
                entry.update(src_in=0, src_out=1, rate=Fraction(0), D=still_frames(it["dur"], F))
        pre.append(entry)

    n = len(pre)
    D = [e["D"] for e in pre]
    X0 = [RHU(fade * F) if (k < n - 1 and pre[k]["join"] == "fade") else 0 for k in range(n)]
    X, xw = clamp_xfades(D, X0)
    warnings += xw
    starts, s = [], 0
    for k in range(n):
        starts.append(s)
        s += D[k] - X[k]
    T = sum(D) - sum(X)
    T_samples = T * SR // F

    preset, fit_req = ex["preset"], ex["fit"]
    stub = [ItemPlan(id=e["id"], kind=e["kind"], ref=e["ref"], path=e["path"], src_in=e["src_in"],
                     src_out=e["src_out"], rate=e["rate"], w=e["w"], h=e["h"], sar=e["sar"], D=e["D"],
                     X_after=X[k], start=starts[k], fit="fill", commercial_ok=e["commercial_ok"])
            for k, e in enumerate(pre)]
    W, H = preset_dims(preset, stub)
    items = []
    for it in stub:
        if it.kind == "missing":
            f = "fill"
        elif fit_req in ("fill", "blur"):
            f = fit_req
        else:
            f = auto_fit(it.w, it.h, it.sar, W, H)
        items.append(replace(it, fit=f))

    mplan = None
    m = clean.get("music")
    if m is not None and T_samples > 0:
        mref = _key(m["src"])
        wav, n_m = None, None
        if mref in wavs:
            wav, n_m = wavs[mref]
        else:
            mp = probes.get(mref)
            if mp is not None and (mp.audio_samples or mp.seconds):
                rate = mp.audio_rate or SR
                n_m = (mp.audio_samples * SR // rate) if mp.audio_samples else RHU(_q(mp.seconds) * SR)
        if n_m is None:
            warnings.append(f"{mref[1]} is missing; the cut has no music")
        else:
            fields, mw = music_window(m, n_m, T_samples, F)
            warnings += mw
            if fields is not None:
                mplan = MusicPlan(ref=mref, wav=wav, n_samples=n_m, **fields)

    return Plan(F=F, items=tuple(items), T=T, T_samples=T_samples, preset=preset, fit=fit_req, music=mplan,
                warnings=tuple(warnings), missing=tuple(missing))


def plan_json(plan: Plan) -> dict:
    """What the UI gets (the `resolved` shape of §6, minus licence)."""
    F = plan.F
    W, H = preset_dims(plan.preset, plan.items)
    out = {
        "fps": F, "frames": plan.T, "seconds": dec(Fraction(plan.T, F), 6), "samples": plan.T_samples,
        "preset": plan.preset, "fit": plan.fit, "w": W, "h": H,
        "items": [{"id": it.id, "kind": it.kind, "start": it.start, "frames": it.D, "xfade_after": it.X_after,
                   "src_in": it.src_in, "src_out": it.src_out, "seconds": dec(Fraction(it.D, F), 6),
                   "fit": it.fit, "missing": it.kind == "missing"} for it in plan.items],
        "music": None,
        "warnings": list(plan.warnings),
    }
    m = plan.music
    if m is not None:
        out["music"] = {"skip": m.skip, "delay": m.delay, "end": m.end, "fade_in": m.fade_in,
                        "fade_out": m.fade_out, "seconds": float(Fraction(m.n_samples, SR)),
                        "volume": m.volume, "target_i": m.target_i}
    return out
