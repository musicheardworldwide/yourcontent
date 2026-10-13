"""ffprobe/ffmpeg helpers: argv lists only, never a shell; every call has a timeout (§4). Owner: WP1.

`configure(cfg)` (or the YOURCONTENT_DATA env) tells this module where DATA is, for the probe cache, the
music conform cache and (through export) the render caches.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import struct
import subprocess
import threading
import time
from fractions import Fraction
from typing import Callable

from . import config as _config
from .types import Probe

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
PROBE_TIMEOUT = 20
SR = 48000
PROBE_CACHE_MAX = 2000
STDERR_KEEP = 4 * 1024 * 1024

# What the export and mix graphs (§8) and the helpers here use; one list, kept in the pure features module.
from .features import REQUIRED_FILTERS  # noqa: E402
REQUIRED_ENCODERS: tuple[str, ...] = ("libx264", "aac", "pcm_f32le", "pcm_s16le", "mjpeg", "png")


class FFmpegError(RuntimeError):
    """ffmpeg/ffprobe failed; the message is its last stderr line."""


class Cancelled(Exception):
    """The job was cancelled while ffmpeg ran; the process group was killed."""


# --------------------------------------------------------------------------------------------- DATA dir

_data_dir: str | None = None


def configure(cfg) -> None:
    """Point the caches at DATA: a Config (uses .data) or a directory path."""
    global _data_dir
    _data_dir = os.path.abspath(getattr(cfg, "data", cfg))


def data_dir() -> str:
    d = _data_dir or _config.env("DATA")
    if not d:
        raise RuntimeError("media.configure() was not called and YOURCONTENT_DATA is not set")
    return os.path.abspath(d)


def _cache_dir(*parts: str) -> str | None:
    try:
        d = os.path.join(data_dir(), "cache", *parts)
    except RuntimeError:
        return None
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


# --------------------------------------------------------------------------------------------- running

def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def run_ffmpeg(args: list[str], timeout: float, on_progress: Callable | None = None, job=None) -> str:
    """Run ffmpeg (or ffprobe) from an argv list; returns stderr.

    `args` may start with "ffmpeg"/"ffprobe" or be the arguments alone (ffmpeg is prepended). With
    `-progress pipe:1` in args, `on_progress(out_time_us)` is called as stdout reports it. The process runs in
    its own process group; cancel (job.cancelled) or the timeout kills the whole group. Raises Cancelled,
    FFmpegError (non-zero exit or timeout).
    """
    argv = list(args)
    if not argv or os.path.basename(argv[0]) not in ("ffmpeg", "ffprobe"):
        argv = [FFMPEG, *argv]
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)
    if job is not None:
        job.set_proc(proc)
    err_chunks: list[bytes] = []
    out_tail: list[bytes] = []

    def read_err() -> None:
        size = 0
        for chunk in iter(lambda: proc.stderr.read(65536), b""):
            err_chunks.append(chunk)
            size += len(chunk)
            while size > STDERR_KEEP and len(err_chunks) > 1:
                size -= len(err_chunks.pop(0))

    def read_out() -> None:
        for line in iter(proc.stdout.readline, b""):
            if on_progress is not None and line.startswith(b"out_time_us="):
                try:
                    on_progress(int(line.split(b"=", 1)[1]))
                except (ValueError, TypeError):
                    pass
            elif on_progress is None:
                out_tail.append(line)
                del out_tail[:-200]

    threads = [threading.Thread(target=read_err, daemon=True), threading.Thread(target=read_out, daemon=True)]
    for t in threads:
        t.start()
    deadline = time.monotonic() + timeout
    why = None
    while True:
        try:
            proc.wait(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            pass
        if job is not None and getattr(job, "cancelled", False):
            why = "cancelled"
        elif time.monotonic() > deadline:
            why = "timeout"
        if why:
            _kill(proc)
            proc.wait()
            break
    for t in threads:
        t.join(timeout=5)
    for pipe in (proc.stdout, proc.stderr):
        try:
            pipe.close()
        except OSError:
            pass
    err = b"".join(err_chunks).decode("utf-8", "replace")
    if why == "cancelled":
        raise Cancelled("cancelled")
    if why == "timeout":
        raise FFmpegError(f"{os.path.basename(argv[0])} took longer than {int(timeout)} s")
    if proc.returncode != 0:
        lines = [ln for ln in err.strip().splitlines() if ln.strip()]
        raise FFmpegError(f"{os.path.basename(argv[0])}: " + (" / ".join(lines[-3:]) if lines
                                                               else f"exit {proc.returncode}"))
    return err


# The only demuxers YourContent ever reads with, and only local files. Anything else (dash, hls, concat, ...)
# could follow a path or URL written inside a crafted upload and read a file nobody owns (§12).
SAFE_DEMUXERS = "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,mp3,wav,flac,ogg,aac,image2,png_pipe,jpeg_pipe,webp_pipe"
SAFE_INPUT = ("-protocol_whitelist", "file", "-format_whitelist", SAFE_DEMUXERS)


def inp(path: str) -> list[str]:
    """ffmpeg input arguments for a stored or uploaded file: the demuxer and protocol whitelists, then -i."""
    return [*SAFE_INPUT, "-i", path]


def _ffprobe_json(path: str, *extra: str) -> dict:
    r = subprocess.run([FFPROBE, "-v", "error", "-print_format", "json", *extra, *SAFE_INPUT, path],
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=PROBE_TIMEOUT)
    if r.returncode != 0:
        lines = r.stderr.strip().splitlines()
        raise FFmpegError("ffprobe: " + (lines[-1] if lines else "cannot read the file"))
    return json.loads(r.stdout or "{}")


# --------------------------------------------------------------------------------------------- probe

def _frac(s, default=Fraction(0)) -> Fraction:
    try:
        s = str(s).replace(":", "/")
        q = Fraction(s)
        return q
    except (ValueError, ZeroDivisionError, TypeError):
        return default


def _float(s, default: float = 0.0) -> float:
    try:
        v = float(s)
        return v if v == v and v not in (float("inf"), float("-inf")) else default
    except (TypeError, ValueError):
        return default


def _count_packets(path: str) -> int:
    d = _ffprobe_json(path, "-count_packets", "-select_streams", "v:0", "-show_entries", "stream=nb_read_packets")
    try:
        return int(d["streams"][0]["nb_read_packets"])
    except (KeyError, IndexError, ValueError, TypeError):
        return 0


def _rotation(side_data, tags=None) -> int:
    """Display rotation in degrees from a stream's or frame's side_data_list (or an old `rotate` tag)."""
    for sd in side_data or []:
        if isinstance(sd, dict) and "rotation" in sd:
            try:
                return int(round(float(sd["rotation"])))
            except (TypeError, ValueError):
                return 0
    try:
        return int((tags or {}).get("rotate") or 0)
    except (TypeError, ValueError):
        return 0


def _image_rotation(path: str) -> int:
    """EXIF orientation as decoded by ffmpeg: frame side data of the first frame (images only)."""
    try:
        d = _ffprobe_json(path, "-select_streams", "v:0", "-show_frames", "-read_intervals", "%+#1",
                          "-show_entries", "frame=side_data_list")
        f = (d.get("frames") or [{}])[0]
        return _rotation(f.get("side_data_list"))
    except (FFmpegError, OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return 0


def _probe_uncached(path: str) -> Probe:
    d = _ffprobe_json(path, "-show_format", "-show_streams")
    fmt = d.get("format") or {}
    streams = d.get("streams") or []
    fname = str(fmt.get("format_name", ""))
    v = next((s for s in streams if s.get("codec_type") == "video"
              and not (s.get("disposition") or {}).get("attached_pic")), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    seconds = _float(fmt.get("duration"))
    if v is not None:
        w, h = int(v.get("width") or 0), int(v.get("height") or 0)
        sar = _frac(v.get("sample_aspect_ratio", "1:1"), Fraction(1))
        if sar <= 0:
            sar = Fraction(1)
        cs = str(v.get("color_space") or v.get("color_primaries") or "")
        color = "bt709" if cs == "bt709" else "unknown"
        is_image = fname == "image2" or fname.endswith("_pipe") or fname in ("png", "webp")
        # ffmpeg autorotates on decode, so the graph sees display-oriented frames: report those dimensions
        rot = _image_rotation(path) if is_image else _rotation(v.get("side_data_list"), v.get("tags"))
        if rot % 180 == 90:
            w, h, sar = h, w, 1 / sar
        if is_image:
            kind, frames, rate = "image", 0, Fraction(0)
        else:
            kind = "video"
            rate = _frac(v.get("r_frame_rate"))
            avg = _frac(v.get("avg_frame_rate"))
            try:
                frames = int(v.get("nb_frames"))
            except (TypeError, ValueError):
                frames = _count_packets(path)
            if not seconds:
                seconds = _float(v.get("duration"))
        vfr = kind == "video" and avg > 0 and rate > 0 and avg != rate
        vcodec = str(v.get("codec_name") or "")
    else:
        kind, w, h, sar, frames, rate, color, vfr, vcodec = "audio", 0, 0, Fraction(0), 0, Fraction(0), "unknown", \
            False, ""
    audio_rate, audio_samples, acodec = 0, 0, ""
    if a is not None:
        audio_rate = int(a.get("sample_rate") or 0)
        acodec = str(a.get("codec_name") or "")
        tb = _frac(a.get("time_base"))
        dts = a.get("duration_ts")
        if audio_rate and dts is not None and tb == Fraction(1, audio_rate):
            audio_samples = int(dts)
        else:
            dur = _float(a.get("duration")) or seconds
            audio_samples = int(Fraction(repr(dur)) * audio_rate + Fraction(1, 2)) if audio_rate else 0
        if kind == "audio" and not seconds:
            seconds = _float(a.get("duration"))
    if v is None and a is None:
        raise FFmpegError("no picture or sound in the file")
    return Probe(kind=kind, w=w, h=h, sar=sar, frames=frames, rate=rate, seconds=seconds,
                 has_audio=a is not None, audio_rate=audio_rate, audio_samples=audio_samples, vcodec=vcodec,
                 acodec=acodec, color=color, vfr=vfr)


def probe_to_json(p: Probe) -> dict:
    return {"kind": p.kind, "w": p.w, "h": p.h, "sar": f"{p.sar.numerator}/{p.sar.denominator}",
            "frames": p.frames, "rate": f"{p.rate.numerator}/{p.rate.denominator}", "seconds": p.seconds,
            "has_audio": p.has_audio, "audio_rate": p.audio_rate, "audio_samples": p.audio_samples,
            "vcodec": p.vcodec, "acodec": p.acodec, "color": p.color, "vfr": p.vfr}


def probe_from_json(d: dict) -> Probe:
    return Probe(kind=d["kind"], w=int(d["w"]), h=int(d["h"]), sar=Fraction(d["sar"]), frames=int(d["frames"]),
                 rate=Fraction(d["rate"]), seconds=float(d["seconds"]), has_audio=bool(d["has_audio"]),
                 audio_rate=int(d["audio_rate"]), audio_samples=int(d["audio_samples"]), vcodec=d["vcodec"],
                 acodec=d["acodec"], color=d["color"], vfr=bool(d.get("vfr", False)))


_probe_lock = threading.Lock()


PROBE_VERSION = 2     # 2: w/h/sar are display-oriented (rotation metadata applied)


def _read_cache(path: str) -> dict:
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def probe(path: str) -> Probe:
    """Cached in DATA/cache/probe.json by (path, size, mtime_ns). Raises FFmpegError / OSError."""
    st = os.stat(path)
    cdir = _cache_dir()
    cfile = os.path.join(cdir, "probe.json") if cdir else None
    key = os.path.abspath(path)
    if cfile:
        with _probe_lock:
            hit = _read_cache(cfile).get(key)
        if hit and hit.get("v") == PROBE_VERSION and hit.get("size") == st.st_size \
                and hit.get("mtime_ns") == st.st_mtime_ns:
            try:
                return probe_from_json(hit["probe"])
            except (KeyError, ValueError, TypeError, ZeroDivisionError):
                pass
    p = _probe_uncached(path)
    if cfile:
        with _probe_lock:
            cache = _read_cache(cfile)
            cache.pop(key, None)
            cache[key] = {"v": PROBE_VERSION, "size": st.st_size, "mtime_ns": st.st_mtime_ns, "probe": probe_to_json(p)}
            while len(cache) > PROBE_CACHE_MAX:
                cache.pop(next(iter(cache)))
            tmp = f"{cfile}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp, "w") as f:
                json.dump(cache, f)
            os.replace(tmp, cfile)
    return p


def stream_info(path: str) -> dict:
    """{"frames": video frame count, "audio_seconds": float | None, "pix_fmt", "w", "h"} of a render."""
    d = _ffprobe_json(path, "-show_streams")
    streams = d.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    frames = None
    if v is not None:
        try:
            frames = int(v.get("nb_frames"))
        except (TypeError, ValueError):
            frames = _count_packets(path)
    audio = None
    if a is not None:
        audio = _float(a.get("duration"), None)
    return {"frames": frames, "audio_seconds": audio, "pix_fmt": v.get("pix_fmt") if v else None,
            "w": v.get("width") if v else None, "h": v.get("height") if v else None}


# --------------------------------------------------------------------------------------------- files

def sha256_file(path: str) -> tuple[str, int]:
    h, n = hashlib.sha256(), 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def wav_frames(path: str) -> int:
    """Sample frames in a RIFF/WAVE file, from its header (PCM, float or extensible)."""
    with open(path, "rb") as f:
        head = f.read(12)
        if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            raise ValueError("not a WAV file")
        block = None
        while True:
            ch = f.read(8)
            if len(ch) < 8:
                raise ValueError("WAV file has no data chunk")
            cid, size = ch[:4], struct.unpack("<I", ch[4:])[0]
            if cid == b"fmt ":
                fmt = f.read(size)
                channels, bits = struct.unpack("<H", fmt[2:4])[0], struct.unpack("<H", fmt[14:16])[0]
                block = struct.unpack("<H", fmt[12:14])[0] or channels * bits // 8
                if size % 2:
                    f.read(1)
            elif cid == b"data":
                if block is None:
                    raise ValueError("WAV data before fmt")
                if size == 0xFFFFFFFF or size == 0:
                    size = os.fstat(f.fileno()).st_size - f.tell()
                return size // block
            else:
                f.seek(size + (size % 2), 1)


_sha_cache: dict[tuple[str, int, int], str] = {}


def conform_audio(path: str) -> tuple[str, int]:
    """(48 kHz stereo s16 wav path, n_samples): DATA/cache/music/<sha12>.wav, made once per source sha256."""
    st = os.stat(path)
    k = (os.path.abspath(path), st.st_size, st.st_mtime_ns)
    sha = _sha_cache.get(k)
    if sha is None:
        sha = sha256_file(path)[0]
        _sha_cache[k] = sha
    d = _cache_dir("music")
    if d is None:
        raise RuntimeError("media.configure() was not called")
    wav = os.path.join(d, f"{sha[:12]}.wav")
    meta = os.path.join(d, f"{sha[:12]}.json")
    if os.path.isfile(wav) and os.path.isfile(meta):
        try:
            with open(meta) as f:
                m = json.load(f)
            n = wav_frames(wav)
            if m.get("n_samples") == n and m.get("src") == sha:
                return wav, n
        except (OSError, ValueError):
            pass
    tmp = os.path.join(d, f".{sha[:12]}.{os.getpid()}.{threading.get_ident()}.wav")
    try:
        run_ffmpeg([FFMPEG, "-nostdin", "-y", "-v", "error", *inp(path), "-map", "0:a:0", "-vn",
                    "-af", "aresample=48000:resampler=soxr", "-ac", "2", "-c:a", "pcm_s16le", "-f", "wav", tmp],
                   300)
        n = wav_frames(tmp)
        os.replace(tmp, wav)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    tmpm = meta + ".tmp"
    with open(tmpm, "w") as f:
        json.dump({"src": sha, "n_samples": n}, f)
    os.replace(tmpm, meta)
    return wav, n


def poster(src: str, dest: str, frame: int = 0) -> None:
    """Write frame `frame` of src (a video or an image) to dest (.jpg or .png)."""
    vf = f"trim=start_frame={int(frame)}" if frame > 0 else "null"
    args = [FFMPEG, "-nostdin", "-y", "-v", "error", *inp(src), "-vf", vf, "-frames:v", "1", "-update", "1"]
    if dest.lower().endswith((".jpg", ".jpeg")):
        args += ["-pix_fmt", "yuvj420p", "-q:v", "3"]
    run_ffmpeg(args + [dest], 60)
    if not os.path.isfile(dest):
        raise FFmpegError("no poster frame was written")


def last_frame(src: str, dest: str) -> None:
    """The last frame of a clip as a picture (PNG for ComfyUI's LoadImage)."""
    run_ffmpeg([FFMPEG, "-nostdin", "-y", "-v", "error", "-sseof", "-1", *inp(src), "-update", "1", dest], 60)
    if not os.path.isfile(dest):
        raise FFmpegError("no last frame was written")


def sniff(head: bytes) -> str | None:
    """File type from magic bytes: png jpg webp | mp4 mov webm | mp3 wav m4a, or None."""
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:3] == b"\xff\xd8\xff":
        return "jpg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand == b"qt  ":
            return "mov"
        if brand in (b"M4A ", b"M4B "):
            return "m4a"
        return "mp4"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "webm" if b"webm" in head[:64] else None
    if head[:3] == b"ID3":
        return "mp3"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        layer = (head[1] >> 1) & 3
        version = (head[1] >> 3) & 3
        if layer != 0 and version != 1:     # layer 00 is ADTS AAC; version 01 is reserved
            return "mp3"
    return None


def stitch_timeout(src_seconds: float, seg_seconds: float) -> float:
    """Seconds allowed for a stitch: it re-encodes the whole source (measured 0.32x realtime for 4K on a
    40-core box), so the limit grows with the clip instead of a fixed 300 s that a long 4K upload overruns."""
    return max(300.0, 6.0 * (max(0.0, src_seconds or 0.0) + max(0.0, seg_seconds or 0.0)))


def stitch_extension(src: str, seg: str, dest: str, job=None) -> None:
    """src + seg (minus seg's first frame, which repeats src's last) as one H.264 clip at src's rate.
    A cancel (job.cancelled) kills the encode; the timeout scales with the length (stitch_timeout).

    The segment is scaled to src's exact size and SAR first: Wan renders at multiples of 16 within 720p,
    and concat refuses inputs whose size or SAR differ (a 1080x1920 source gets a 720x1280 segment)."""
    pr = probe(src)
    try:
        seg_seconds = probe(seg).seconds
    except Exception:  # noqa: BLE001 - the stitch itself reports an unreadable segment
        seg_seconds = 0.0
    rate = pr.rate or Fraction(16)
    sar = f"{pr.sar.numerator}/{pr.sar.denominator}" if pr.sar > 0 else "1"
    run_ffmpeg([FFMPEG, "-nostdin", "-y", "-v", "error", *inp(src), *inp(seg), "-filter_complex",
                f"[0:v]setsar={sar}[a];"
                f"[1:v]trim=start_frame=1,setpts=PTS-STARTPTS,scale={pr.w}:{pr.h}:flags=lanczos,setsar={sar}[b];"
                "[a][b]concat=n=2:v=1:a=0[v]",
                "-map", "[v]", "-r", f"{rate.numerator}/{rate.denominator}", "-c:v", "libx264", "-crf", "17",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", dest], stitch_timeout(pr.seconds, seg_seconds), job=job)


# ffprobe format_name for each sniffed upload type (ingest refuses a file some other demuxer claimed)
SNIFF_FORMATS = {"png": {"image2", "png_pipe"}, "jpg": {"image2", "jpeg_pipe"}, "webp": {"image2", "webp_pipe"},
                 "mp4": {"mov,mp4,m4a,3gp,3g2,mj2"}, "mov": {"mov,mp4,m4a,3gp,3g2,mj2"},
                 "m4a": {"mov,mp4,m4a,3gp,3g2,mj2"}, "webm": {"matroska,webm"}, "mp3": {"mp3"}, "wav": {"wav"}}


def format_name(path: str) -> str:
    """ffprobe's format_name for the file (under the demuxer whitelist)."""
    return str((_ffprobe_json(path, "-show_format").get("format") or {}).get("format_name", ""))


def ebur128(path: str) -> dict:
    """{"I": integrated LUFS, "TP": true peak dBFS} (None when silent/unreadable)."""
    err = run_ffmpeg([FFMPEG, "-nostdin", "-hide_banner", "-nostats", *inp(path), "-filter_complex",
                      "ebur128=peak=true", "-f", "null", "-"], 300)
    summary = err[err.rfind("Summary:"):] if "Summary:" in err else err
    out: dict = {"I": None, "TP": None}
    m = re.search(r"I:\s+(-?[\d.]+|-inf)\s+LUFS", summary)
    if m and m.group(1) != "-inf":
        out["I"] = float(m.group(1))
    m = re.search(r"True peak:\s*\n\s*Peak:\s+(-?[\d.]+|-inf)\s+dBFS", summary)
    if m and m.group(1) != "-inf":
        out["TP"] = float(m.group(1))
    return out


_caps: dict | None = None


def ffmpeg_caps() -> dict:
    """{"version", "missing_filters", "missing_encoders"} (cached once per process)."""
    global _caps
    if _caps is not None:
        return _caps
    try:
        ver = subprocess.run([FFMPEG, "-hide_banner", "-version"], capture_output=True, text=True, timeout=10).stdout
        flt = subprocess.run([FFMPEG, "-hide_banner", "-filters"], capture_output=True, text=True, timeout=10).stdout
        enc = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return {"version": None, "missing_filters": list(REQUIRED_FILTERS), "missing_encoders":
                list(REQUIRED_ENCODERS)}
    _caps = parse_caps(ver, flt, enc)
    return _caps


def parse_caps(version_out: str, filters_out: str, encoders_out: str) -> dict:
    m = re.search(r"ffmpeg version n?(\d+(?:\.\d+)*)", version_out)
    filters = {ln.split()[1] for ln in filters_out.splitlines()
               if len(ln.split()) >= 3 and re.match(r"^\s*[T.][S.][C.]?\s", ln)}
    encoders = {ln.split()[1] for ln in encoders_out.splitlines()
                if len(ln.split()) >= 2 and re.match(r"^\s*[VAS][F.][S.][X.][B.][D.]\s", ln)}
    return {"version": m.group(1) if m else None,
            "missing_filters": [f for f in REQUIRED_FILTERS if f not in filters],
            "missing_encoders": [e for e in REQUIRED_ENCODERS if e not in encoders]}
