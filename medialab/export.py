"""PURE filtergraph / argv builders plus the impure render runner (§8). Owner: WP1.

The builders take everything as arguments and return strings; only render_mix, run_render, verify (and the
stat() inside proxy_key) touch files. Only integers we computed and our own labels go into a filtergraph:
paths appear solely as `-i` arguments, user text solely as separate `-metadata` argv values.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
from dataclasses import dataclass
from fractions import Fraction

from . import jobs, timeline
from .timeline import RHU, SR, dec, even
from .types import Conflict, MusicPlan, Plan

SC = ":flags=lanczos:in_color_matrix=auto:out_color_matrix=bt709:out_range=tv"
PROXY_DIV = 3
PROXY_CACHE_BYTES = 2 * 1024**3
LOUD_TP, LOUD_LRA = -1, 11
SILENCE_I = -70.0
_MEASURE_KEYS = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
_NUM_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+|inf)$")


# --------------------------------------------------------------------------------------------- sizes

def output_dims(preset: str, plan: Plan, scale_div: int = 1) -> tuple[int, int, int, int]:
    """(W, H, final_W, final_H): the render size for this mode and the final size it derives from (§8.1)."""
    fW, fH = timeline.preset_dims(preset, plan.items)
    if scale_div == 1:
        return fW, fH, fW, fH
    return max(2, even(Fraction(fW, scale_div))), max(2, even(Fraction(fH, scale_div))), fW, fH


# --------------------------------------------------------------------------------------------- geometry

@dataclass(frozen=True)
class Geom:
    """Integer geometry for one item at (W, H) (§8.2). Every number is a computed int."""
    fit: str                       # "fill" | "blur"
    W: int
    H: int
    # fill: scale to SW x SH, crop W x H at (CX, CY). blur: also the foreground size/offset.
    SW: int = 0
    SH: int = 0
    CX: int = 0
    CY: int = 0
    FW: int = 0
    FH: int = 0
    OX: int = 0
    OY: int = 0
    # blur background: cover-scale to bw x bh, crop BW x BH at (bx, by), blur, scale up to W x H.
    bw: int = 0
    bh: int = 0
    BW: int = 0
    BH: int = 0
    bx: int = 0
    by: int = 0

    def text(self, i: int) -> str:
        """The GEOM filter text for input/branch i (labels f{i} b{i} bg{i} fg{i} when blurred)."""
        if self.fit == "fill":
            return f"scale={self.SW}:{self.SH}{SC},crop={self.W}:{self.H}:{self.CX}:{self.CY},setsar=1"
        return (f"split=2[f{i}][b{i}];"
                f"[b{i}]scale={self.bw}:{self.bh}{SC},crop={self.BW}:{self.BH}:{self.bx}:{self.by},"
                f"gblur=sigma=3,scale={self.W}:{self.H}:flags=bicubic,setsar=1[bg{i}];"
                f"[f{i}]scale={self.FW}:{self.FH}{SC},setsar=1[fg{i}];"
                f"[bg{i}][fg{i}]overlay=x={self.OX}:y={self.OY}:eof_action=pass")

    @property
    def filter(self) -> str:
        return self.text(0)


def _cover(dw: Fraction, dh: Fraction, W: int, H: int) -> tuple[int, int, int, int]:
    s = max(Fraction(W) / dw, Fraction(H) / dh)
    SW, SH = 2 * timeline.ceil_q(dw * s / 2), 2 * timeline.ceil_q(dh * s / 2)
    SW, SH = max(SW, W), max(SH, H)
    return SW, SH, ((SW - W) // 2) // 2 * 2, ((SH - H) // 2) // 2 * 2


def geometry(sw: int, sh: int, sar: Fraction, W: int, H: int, fit: str, final_W: int, final_H: int) -> Geom:
    sar = Fraction(sar) if sar else Fraction(1)
    if sar <= 0:
        sar = Fraction(1)
    if sw <= 0 or sh <= 0:
        sw, sh, sar = W, H, Fraction(1)
    dw, dh = Fraction(sw) * sar, Fraction(sh)
    if fit == "fill":
        SW, SH, CX, CY = _cover(dw, dh, W, H)
        return Geom("fill", W, H, SW=SW, SH=SH, CX=CX, CY=CY)
    if fit != "blur":
        raise ValueError(f"unknown fit {fit!r}")
    s = min(Fraction(W) / dw, Fraction(H) / dh)
    FW, FH = max(2, 2 * ((dw * s / 2) // 1)), max(2, 2 * ((dh * s / 2) // 1))
    FW, FH = min(int(FW), W), min(int(FH), H)
    OX, OY = ((W - FW) // 2) // 2 * 2, ((H - FH) // 2) // 2 * 2
    BW, BH = max(2, even(Fraction(final_W, 24))), max(2, even(Fraction(final_H, 24)))
    bw, bh, bx, by = _cover(dw, dh, BW, BH)
    return Geom("blur", W, H, FW=FW, FH=FH, OX=OX, OY=OY, bw=bw, bh=bh, BW=BW, BH=BH, bx=bx, by=by)


# --------------------------------------------------------------------------------------------- video graph

def _setpts_for(rate: Fraction) -> str:
    rate = Fraction(rate)
    if rate.denominator == 1:
        return f"setpts=N/({rate.numerator}*TB)"
    return f"setpts=N*{rate.denominator}/({rate.numerator}*TB)"


def build_video_graph(plan: Plan, W: int, H: int, final_W: int, final_H: int) -> tuple[list[str], str]:
    """(input_args, filter_complex graph) for the plan at W x H (§8.3, §8.4). Input i is item i."""
    from . import media
    F = plan.F
    if not plan.items:
        raise ValueError("the cut is empty")
    PF = "yuv444p" if any(it.X_after > 0 for it in plan.items) else "yuv420p"
    tail = f"settb=1/{F},setpts=N,fps={F},format={PF}"
    inputs: list[str] = []
    parts: list[str] = []
    for i, it in enumerate(plan.items):
        if it.kind == "missing":
            inputs += ["-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r={F}"]
            parts.append(f"[{i}:v]trim=end_frame={it.D},{tail}[v{i}]")
            continue
        if not it.path:
            raise ValueError(f"item {i + 1} has no file path")
        g = geometry(it.w, it.h, it.sar, W, H, it.fit, final_W, final_H).text(i)
        if it.kind == "clip":
            inputs += media.inp(it.path)
            parts.append(f"[{i}:v]trim=start_frame={it.src_in}:end_frame={it.src_out},{_setpts_for(it.rate)},"
                         f"{g},fps={F},tpad=stop_mode=clone:stop={F},trim=end_frame={it.D},{tail}[v{i}]")
        elif it.kind == "still":
            inputs += media.inp(it.path)
            parts.append(f"[{i}:v]{g},loop=loop={it.D - 1}:size=1:start=0,{tail}[v{i}]")
        else:
            raise ValueError(f"unknown item kind {it.kind!r}")
    n = len(plan.items)
    if n == 1:
        parts.append("[v0]format=yuv420p[vout]")
        return inputs, ";".join(parts)
    acc = "v0"
    for k in range(n - 1):
        X = plan.items[k].X_after
        out = f"x{k + 1}"
        last = k == n - 2
        if X > 0:
            S = plan.items[k + 1].start
            stage = (f"[{acc}][v{k + 1}]xfade=transition=fade:duration={dec(Fraction(X, F), 6)}"
                     f":offset={dec(Fraction(S, F), 6)}")
        else:
            stage = f"[{acc}][v{k + 1}]concat=n=2:v=1:a=0"
            if not last and plan.items[k + 1].X_after > 0:
                # concat outputs on 1/1000000 with no frame rate; xfade needs both inputs on 1/F at F (§8.4)
                stage += f",settb=1/{F},setpts=N,fps={F}"
        parts.append(stage + (",format=yuv420p[vout]" if last else f"[{out}]"))
        acc = out
    return inputs, ";".join(parts)


# --------------------------------------------------------------------------------------------- audio graph

def loudnorm_target(volume: float) -> float:
    """-14 + 20*log10(clamp(volume, 0.05, 1.0)) LUFS, to 0.01 (100% = -14 LUFS)."""
    return timeline.target_i(volume)


def _is_silent(measured: dict | None) -> bool:
    if not measured:
        return False
    v = str(measured.get("input_i", "")).strip().lower()
    if v in ("-inf", "inf", "nan", ""):
        return True
    try:
        return float(v) < SILENCE_I
    except ValueError:
        return True


def _mnum(v) -> str:
    s = str(v).strip()
    if not _NUM_RE.match(s.lower()):
        raise ValueError(f"loudnorm value is not a number: {s!r}")
    return s


def _audio_chain(m: MusicPlan, T_samples: int) -> str:
    c = ["[0:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"]
    if m.skip > 0:
        c.append(f"atrim=start_sample={m.skip}")
    c.append("asetpts=N/SR/TB")
    if m.delay > 0:
        c.append(f"adelay=delays={m.delay}S:all=1")
    c.append(f"apad=whole_len={T_samples}")
    c.append(f"atrim=end_sample={T_samples}")
    if m.fade_in > 0:
        c.append(f"afade=t=in:ss={m.delay}:ns={m.fade_in}")
    if m.fade_out > 0:
        c.append(f"afade=t=out:ss={m.end - m.fade_out}:ns={m.fade_out}")
    return ",".join(c)


def build_audio_graph(m: MusicPlan, T_samples: int, measured: dict | None) -> str:
    """Pass 1 (measured None: measure only) or pass 2 (linear loudnorm from the pass-1 numbers) (§8.5)."""
    chain = _audio_chain(m, T_samples)
    ti = dec(Fraction(m.target_i), 2)
    base = f"loudnorm=I={ti}:TP={LOUD_TP}:LRA={LOUD_LRA}"
    pin = f"apad=whole_len={T_samples},atrim=end_sample={T_samples}"
    if measured is None:
        return f"{chain},{base}:print_format=json"
    if _is_silent(measured):
        return f"{chain},{pin}"
    vals = {k: _mnum(measured[k]) for k in _MEASURE_KEYS}
    return (f"{chain},{base}:measured_I={vals['input_i']}:measured_TP={vals['input_tp']}"
            f":measured_LRA={vals['input_lra']}:measured_thresh={vals['input_thresh']}"
            f":offset={vals['target_offset']}:linear=true:print_format=json,"
            f"aresample=48000:resampler=soxr,{pin}")


def parse_loudnorm(stderr: str) -> dict:
    """The last {...} JSON block on loudnorm's stderr."""
    end = stderr.rfind("}")
    while end != -1:
        start = stderr.rfind("{", 0, end)
        if start == -1:
            break
        try:
            d = json.loads(stderr[start:end + 1])
            if isinstance(d, dict) and "input_i" in d:
                return d
        except ValueError:
            pass
        end = stderr.rfind("}", 0, start)
    raise ValueError("loudnorm printed no measurement")


# --------------------------------------------------------------------------------------------- encode

def encode_args(mode: str, F: int, audio_index: int | None, comment: str) -> list[str]:
    """Everything after -filter_complex up to (not including) the output path (§8.5)."""
    if mode not in ("final", "proxy"):
        raise ValueError(f"unknown mode {mode!r}")
    a = ["-map", "[vout]"]
    if audio_index is not None:
        a += ["-map", f"{int(audio_index)}:a"]
    # §8.5 writes "-fps_mode passthrough -r F"; ffmpeg 7.1.5 refuses that pair ("-r ... together with a non-CFR
    # -fps_mode ... contradictory"), so -r is dropped. The graph already emits exactly T frames at pts N on 1/F,
    # so passthrough keeps every frame and the muxer derives F (r_frame_rate F/1, measured).
    a += ["-fps_mode", "passthrough", "-c:v", "libx264"]
    if mode == "final":
        a += ["-preset", "slow", "-crf", "18", "-maxrate", "8M", "-bufsize", "16M"]
    else:
        a += ["-preset", "veryfast", "-crf", "28"]
    a += ["-profile:v", "high", "-pix_fmt", "yuv420p", "-colorspace", "bt709", "-color_primaries", "bt709",
          "-color_trc", "bt709", "-color_range", "tv", "-g", str(2 * F if mode == "final" else F)]
    if audio_index is not None:
        a += ["-c:a", "aac", "-b:a", "192k" if mode == "final" else "128k", "-ar", "48000"]
    a += ["-movflags", "+faststart"]
    if comment:
        a += ["-metadata", f"comment={comment}"]
    return a


def render_argv(plan: Plan, mode: str, audio_wav: str | None, out: str, *, comment: str = "",
                title: str = "") -> list[str]:
    """The full ffmpeg argv for a render (pure). `audio_wav` is the pre-rendered mix, or None."""
    from . import media
    W, H, fW, fH = output_dims(plan.preset, plan, PROXY_DIV if mode == "proxy" else 1)
    inputs, graph = build_video_graph(plan, W, H, fW, fH)
    argv = ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-v", "error", "-progress", "pipe:1", *inputs]
    ai = None
    if audio_wav is not None:
        ai = len(plan.items)
        argv += media.inp(audio_wav)
    argv += ["-filter_complex", graph, *encode_args(mode, plan.F, ai, comment)]
    if title:
        argv += ["-metadata", f"title={title}"]
    return argv + [out]


def audio_key(plan: Plan) -> str:
    """sha256(audio chain + music source + T_samples + target_i): independent of the video size."""
    m = plan.music
    if m is None:
        return ""
    src = os.path.basename(m.wav) if m.wav else f"{m.ref[0]}/{m.ref[1]}"
    blob = "\n".join([build_audio_graph(m, plan.T_samples, None), src, str(m.n_samples), str(plan.T_samples),
                      dec(Fraction(m.target_i), 2)])
    return hashlib.sha256(blob.encode()).hexdigest()


def _stat(path: str | None) -> tuple[int, int]:
    try:
        st = os.stat(path) if path else None
    except OSError:
        st = None
    return (st.st_size, st.st_mtime_ns) if st else (0, 0)


def proxy_key(plan: Plan, W: int, H: int) -> str:
    """sha256 of the proxy's ffmpeg argv (minus the output path) + every input's (size, mtime_ns)."""
    akey = audio_key(plan)
    argv = render_argv(plan, "proxy", f"<audio:{akey}>" if akey else None, "<out>")[:-1]
    stats = [_stat(it.path) for it in plan.items]
    blob = json.dumps({"argv": argv, "W": W, "H": H, "stats": stats, "audio": akey}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def plan_sha(plan: Plan) -> str:
    return hashlib.sha256(json.dumps(timeline.plan_json(plan), sort_keys=True).encode()).hexdigest()


# --------------------------------------------------------------------------------------------- impure runner

def _data(*parts: str) -> str:
    from . import media
    return os.path.join(media.data_dir(), *parts)


def _mkdirs(path: str) -> str:
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def _write_json(path: str, data: dict) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, sort_keys=True)
    os.replace(tmp, path)


def _progress(job, p, phase) -> None:
    if job is not None:
        job.progress(p, phase)


def render_mix(plan: Plan, job) -> dict | None:
    """Pre-render the music to DATA/cache/audio/<akey>.wav (pcm_f32le, exactly T_samples).

    Returns {"wav", "key", "measured", "normalization_type", "checks": {"lufs", "true_peak"}, "warnings"},
    or None when the cut has no music. Cached by audio_key: the proxy and the final share one WAV.
    """
    from . import media, store
    m = plan.music
    if m is None:
        return None
    if not m.wav:
        raise ValueError("music is not conformed")
    key = audio_key(plan)
    wav = os.path.join(_mkdirs(_data("cache", "audio")), f"{key}.wav")
    meta_path = os.path.join(_mkdirs(_data("cache", "loud")), f"{key}.json")
    if os.path.isfile(wav) and os.path.isfile(meta_path):
        try:
            with open(meta_path) as f:
                meta = json.load(f)
            if media.wav_frames(wav) == plan.T_samples:
                return {**meta, "wav": wav, "key": key}
        except (OSError, ValueError):
            pass
    warnings: list[str] = []
    timeout = 300
    pass1 = build_audio_graph(m, plan.T_samples, None)
    err = media.run_ffmpeg(["ffmpeg", "-nostdin", "-hide_banner", "-nostats", *media.inp(m.wav),
                            "-filter_complex", pass1, "-f", "null", "-"], timeout, job=job)
    measured = parse_loudnorm(err)
    silent = _is_silent(measured)
    if silent:
        warnings.append("the music is silent; loudness not adjusted")
    _progress(job, 0.12, "mix")
    pass2 = build_audio_graph(m, plan.T_samples, measured)
    tmp = store.tmp_path(".wav")
    try:
        err2 = media.run_ffmpeg(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-nostats", *media.inp(m.wav),
                                 "-filter_complex", pass2, "-c:a", "pcm_f32le", "-f", "wav", tmp],
                                timeout, job=job)
        ntype = None
        if not silent:
            try:
                ntype = parse_loudnorm(err2).get("normalization_type")
            except ValueError:
                ntype = None
        if ntype == "dynamic":
            warnings.append("loudness had to be limited; may pump")
        os.replace(tmp, wav)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    loud = media.ebur128(wav)
    meta = {"measured": measured, "normalization_type": ntype, "target_i": m.target_i,
            "checks": {"lufs": loud.get("I"), "true_peak": loud.get("TP")}, "warnings": warnings,
            "samples": media.wav_frames(wav)}
    _write_json(meta_path, meta)
    return {**meta, "wav": wav, "key": key}


def verify(path: str, plan: Plan) -> dict:
    """ffprobe the render: frame count must equal T; audio within 1024/SR of T_samples/SR (§8.6 step 4)."""
    from . import media
    info = media.stream_info(path)
    frames = info.get("frames")
    checks: dict = {"frames_expected": plan.T, "frames_actual": frames, "audio_seconds": info.get("audio_seconds")}
    warns: list[str] = []
    ok = frames == plan.T
    if not ok:
        warns.append(f"the export has {frames} frames, expected {plan.T}")
    if plan.music is not None:
        a = info.get("audio_seconds")
        want = Fraction(plan.T_samples, SR)
        if a is None or abs(Fraction(repr(float(a))) - want) > Fraction(1024, SR):
            ok = False
            warns.append("the sound length does not match the picture")
    elif info.get("audio_seconds") is not None:
        ok = False
        warns.append("the export has sound it should not have")
    checks["ok"] = ok
    checks["warnings"] = warns
    return checks


def _prune_proxies(keep: str) -> None:
    d = _data("cache", "proxy")
    try:
        files = [(e.stat().st_mtime_ns, e.stat().st_size, e.path) for e in os.scandir(d)
                 if e.is_file() and e.name.endswith(".mp4")]
    except OSError:
        return
    total = sum(f[1] for f in files)
    for _, size, p in sorted(files):
        if total <= PROXY_CACHE_BYTES:
            break
        if os.path.abspath(p) == os.path.abspath(keep):
            continue
        try:
            os.remove(p)
            total -= size
        except OSError:
            pass


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _gather(clean: dict):
    """Probe every source the cut uses: (probes, paths). Unowned, unsafe or vanished files count as missing."""
    from . import media, store
    refs = [timeline._key(it["src"]) for it in clean["items"]]
    if clean.get("music"):
        refs.append(timeline._key(clean["music"]["src"]))
    probes, paths = {}, {}
    for ref in dict.fromkeys(refs):
        try:
            if not store.owned(ref):
                probes[ref] = None
                continue
            p = store.resolve(ref)
            if not os.path.isfile(p):
                probes[ref] = None
                continue
            probes[ref] = media.probe(p)
            paths[ref] = p
        except (LookupError, ValueError, OSError, RuntimeError):
            probes[ref] = None
    return probes, paths


def run_render(project: dict, rev: int, mode: str, job) -> dict:
    """Render the saved project: mode "proxy" (cached preview) or "final" (an export in root lab) (§8.6).

    `project` is the stored project; its rev must equal `rev` (else Conflict). Proxy result:
    {"url", "frames", "seconds", "key", "cached", "warnings"}. Final result: a Recent item plus
    {"checks", "commercial_ok", "licences", "warnings"}.
    """
    from . import media, store
    if mode not in ("proxy", "final"):
        raise ValueError(f"unknown mode {mode!r}")
    if not isinstance(project, dict) or project.get("rev") != rev:
        raise Conflict(project)
    _progress(job, 0.0, "probe")
    clean, errors = timeline.validate(project)
    if errors:
        raise ValueError(errors[0])
    if not clean["items"]:
        raise ValueError("add something to the cut first")
    probes, paths = _gather(clean)
    wavs = {}
    if clean.get("music"):
        mref = timeline._key(clean["music"]["src"])
        if mref in paths:
            _progress(job, 0.05, "mix")
            wavs[mref] = media.conform_audio(paths[mref])
    pl = timeline.plan(clean, probes, wavs, paths=paths)
    if pl.T <= 0:
        raise ValueError("the cut is empty")
    _progress(job, 0.08, "mix")
    mix = render_mix(pl, job)
    warnings = list(pl.warnings) + (mix["warnings"] if mix else [])
    seconds = float(Fraction(pl.T, pl.F))
    W, H, fW, fH = output_dims(pl.preset, pl, PROXY_DIV if mode == "proxy" else 1)
    timeout = max(300, 20 * seconds) if mode == "final" else max(120, 6 * seconds)
    total_us = pl.T * 1_000_000 / pl.F

    def on_progress(out_us: int) -> None:
        _progress(job, 0.2 + 0.7 * min(1.0, max(0.0, out_us / total_us)), "video")

    if mode == "proxy":
        key = proxy_key(pl, W, H)
        dest = os.path.join(_mkdirs(_data("cache", "proxy")), f"{key}.mp4")
        cached = os.path.isfile(dest)
        if not cached:
            tmp = store.tmp_path(".mp4")
            try:
                _progress(job, 0.2, "video")
                media.run_ffmpeg(render_argv(pl, "proxy", mix["wav"] if mix else None, tmp), timeout,
                                 on_progress=on_progress, job=job)
                _progress(job, 0.92, "verify")
                checks = verify(tmp, pl)
                warnings += checks["warnings"]
                os.replace(tmp, dest)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
            _prune_proxies(dest)
        else:
            os.utime(dest)
        _progress(job, 1.0, "done")
        return {"url": f"/m/cache/{key}.mp4", "key": key, "frames": pl.T, "seconds": seconds,
                "cached": cached, "warnings": warnings}

    # final
    from . import provenance
    inputs, n_uploads, seen = [], 0, set()
    for it in pl.items:
        if it.ref in seen:
            continue
        seen.add(it.ref)
        side = None if it.kind == "missing" else store.sidecar(it.ref)
        if side and side.get("kind") == "upload":
            n_uploads += 1
        inputs.append(provenance.input_entry("item", it.ref, side, snap_ok=it.commercial_ok))
    if pl.music is not None:
        side = store.sidecar(pl.music.ref)
        if side and side.get("kind") == "upload":
            n_uploads += 1
        inputs.append(provenance.input_entry("music", pl.music.ref, side))
    agg = provenance.aggregate([], inputs)
    name = store.new_name("cut", "mp4")
    poster_name = name[:-4] + ".jpg"
    comment = provenance.mp4_comment(agg, name)
    title = clean.get("name") or "Untitled cut"
    tmp = store.tmp_path(".mp4")
    tmp_poster = store.tmp_path(".jpg")
    dest = store.resolve(("lab", name))
    poster_dest = store.resolve(("lab", poster_name))
    try:
        _progress(job, 0.2, "video")
        media.run_ffmpeg(render_argv(pl, "final", mix["wav"] if mix else None, tmp, comment=comment,
                                     title=title), timeout, on_progress=on_progress, job=job)
        _progress(job, 0.92, "verify")
        checks = verify(tmp, pl)
        warnings += checks.pop("warnings")
        checks["lufs"] = mix["checks"]["lufs"] if mix else None
        checks["true_peak"] = mix["checks"]["true_peak"] if mix else None
        _progress(job, 0.95, "finish")
        # everything goes to temporaries first: nothing lands in lab/ until the job has committed
        media.poster(tmp, tmp_poster, RHU(Fraction(pl.T, 3)))
        sha, nbytes = media.sha256_file(tmp)
        pr = media.probe(tmp)
        jobs.commit(job)       # a cancel that came first discards the render; a later one cannot
        os.replace(tmp_poster, poster_dest)
        os.replace(tmp, dest)
    except BaseException:
        # dest and poster_dest are fresh names from store.new_name: nothing else owns them
        for p in (poster_dest, dest):
            if os.path.exists(p):
                os.remove(p)
        raise
    finally:
        for p in (tmp, tmp_poster):
            if os.path.exists(p):
                os.remove(p)
    warnings += provenance.warnings(agg, n_uploads)
    render = {
        "project": clean.get("id"), "rev": rev, "snapshot": project, "preset": pl.preset, "fit": pl.fit,
        "fps": pl.F, "W": W, "H": H, "frames": pl.T, "samples": pl.T_samples, "plan_sha": plan_sha(pl),
        "ffmpeg": media.ffmpeg_caps().get("version"),
        "loudnorm": ({"input_i": mix["measured"].get("input_i"), "target_i": pl.music.target_i,
                      "normalization_type": mix.get("normalization_type")} if mix else None),
        "checks": checks,
    }
    try:
        side = provenance.make_sidecar("export", ("lab", name), models=[], inputs=inputs, probe=pr, sha256=sha,
                                       nbytes=nbytes, poster=poster_name, label=title, render=render)
        store.put_sidecar(("lab", name), side)
    except BaseException:
        # without a sidecar the files are unowned: never listed, never deletable through the app
        for p in (dest, poster_dest):
            try:
                os.remove(p)
            except OSError:
                pass
        raise
    _progress(job, 1.0, "done")
    return {"ref": {"root": "lab", "name": name}, "type": "video", "kind": "export",
            "url": f"/m/lab/{name}", "poster": f"/m/lab/{poster_name}", "label": title, "w": W, "h": H,
            "seconds": seconds, "created": side.get("created") or _now(),
            "commercial_ok": agg.get("commercial_ok"), "licences": agg.get("licences", []),
            "checks": checks, "warnings": warnings}
