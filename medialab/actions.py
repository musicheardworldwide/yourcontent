"""Create/delete flows tying comfy, graphs, provenance and store together (§4, §11). Owner: WP3.

Every create path writes a sidecar before it returns (that sidecar is what makes the file YourContent's own),
and records the ComfyUI prompt id in the ledger, so delete can later remove exactly our history entries.
Licences are recorded and warned about, never enforced.

`configure(cfg)` is called once at start (server.prepare does it for every module that has one).
"""
from __future__ import annotations

import os
import random
import shutil
import time
from fractions import Fraction
from math import floor
from typing import BinaryIO

from . import comfy, graphs, jobs, media, provenance, store
from . import models as models_mod
from .types import NotOwned, Ref

CHUNK = 1 << 20
MAX_PROMPT = 4000
MAX_TAGS = 1000
DEFAULT_ANIMATE = "natural subtle motion, gentle camera movement"
DEFAULT_EXTEND = "the motion continues naturally, same camera movement"
TIMEOUT_PICTURE = 600
TIMEOUT_CLIP = 1800
TIMEOUT_MUSIC = 900
MAX_VIDEO_SECONDS = 600
MAX_VIDEO_PX = 4096

_cfg = None
_models = None


class TooLarge(ValueError):
    status = 413


class BadType(ValueError):
    status = 415


def configure(cfg, models=None) -> None:
    """Remember the Config; models load lazily from it unless given. Wires comfy.submit's provisional ledger
    line (§5.6) to the store."""
    global _cfg, _models
    _cfg, _models = cfg, models
    comfy.on_submit = _provisional


def _provisional(pid: str, kind: str) -> None:
    store.ledger_add(pid, kind, [])


def _config():
    return _cfg if _cfg is not None else store.config()


def _m():
    global _models
    if _models is None:
        _models = models_mod.load(_config())
    return _models


def _rhu(x: Fraction) -> int:
    return floor(x + Fraction(1, 2))


# --------------------------------------------------------------------------------------------- helpers

def _seed(body: dict) -> int:
    s = body.get("seed")
    if s in (None, "", 0, "0"):
        return random.randint(1, 2**31)
    s = int(s)
    if not 0 <= s < 2**63:
        raise ValueError("seed is out of range")
    return s


def _text(body: dict, key: str, limit: int) -> str:
    v = body.get(key) or ""
    if not isinstance(v, str):
        raise ValueError(f"{key} must be text")
    v = v.strip()
    if len(v) > limit:
        raise ValueError(f"{key} is too long (at most {limit} characters)")
    return v


def _owned(obj) -> Ref:
    ref = store.check_ref(obj)
    if not store.owned(ref):
        raise NotOwned("not found")
    return ref


def load_value(ref: Ref) -> str:
    """The LoadImage value for a picture ref (§3.1)."""
    cfg = _config()
    root, name = ref
    if root == "out":
        return f"{name} [output]"
    if root == "gen":
        return f"{cfg.output_subdir}/{name} [output]"
    if root == "in":
        return f"{cfg.input_subdir}/{name}"
    raise ValueError("use a picture from Recent")


def _picture(obj) -> tuple[Ref, dict]:
    ref = _owned(obj)
    if ref[0] == "lab":
        raise ValueError("use a picture from Recent")
    sc = store.sidecar(ref) or {}
    if store.media_type(ref[1]) != "image":
        raise ValueError("that isn't a picture")
    return ref, sc


def _progress(job):
    def cb(p, phase):
        if job is not None:
            job.progress(p, phase)
    return cb


def _run(graph: dict, kind: str, limit: float, job) -> tuple[str, list[dict], float]:
    """Queue a graph and wait for it: (prompt id, outputs, seconds)."""
    t0 = time.monotonic()
    if job is not None:
        job.progress(None, "queued")
    pid = comfy.submit(graph, kind=kind)
    if job is not None:
        job.set_comfy(pid)
    outs = comfy.wait(pid, limit, on_progress=_progress(job), job=job)
    return pid, outs, round(time.monotonic() - t0, 1)


def _prefix(kind: str) -> str:
    """A filename_prefix no earlier job used: <subdir>/<kind>_<UTC stamp>_<6 hex>.

    ComfyUI numbers outputs from the highest file left under a prefix, so a shared prefix ("img") hands a deleted
    file's name to the next job, and a cut, calendar entry or Sheet row still holding the old ref silently points
    at new content (functional audit 2026-10-11). A fresh prefix per job makes every name new; names already on
    disk (img_00005_.png) keep working."""
    return f"{_config().output_subdir}/{kind}_{store.stamp()}"


def _pick(outs: list[dict], kind: str) -> str:
    """The first output of `kind` that landed in our gen folder."""
    sub = _config().output_subdir
    for o in outs:
        if o.get("kind") == kind and (o.get("subfolder") or "") == sub and o.get("type", "output") == "output":
            name = o["filename"]
            store.check_ref(("gen", name))
            return name
    raise comfy.ComfyError("ComfyUI finished but saved no file where YourContent expected it")


def _facts(path: str) -> tuple[object, str | None, int | None]:
    """(probe or None, sha256, bytes) of a file we just made."""
    try:
        sha, n = media.sha256_file(path)
    except OSError:
        sha, n = None, None
    try:
        pr = media.probe(path)
    except Exception:  # noqa: BLE001 - a sidecar without a probe is still a valid sidecar
        pr = None
    return pr, sha, n


def _probe_or(pr, **fallback) -> dict | None:
    d = provenance.probe_dict(pr)
    if d is None and fallback:
        d = dict(fallback)
    return d


def _save(kind: str, ref: Ref, *, used, inputs, pids, path, poster=None, label="", prompt=None, negative=None,
          seed=None, params=None, fallback_probe=None, job=None) -> dict:
    # the file is already made: from here a cancel cannot stop anything, so the job ends "done" (it is kept)
    jobs.commit(job, can_discard=False)
    m = _m()
    pr, sha, n = _facts(path)
    sc = provenance.make_sidecar(kind, ref, models=provenance.model_entries(m, used), inputs=inputs,
                                 prompt=prompt, negative=negative, seed=seed, params=params, pids=pids,
                                 probe=_probe_or(pr, **(fallback_probe or {})), sha256=sha, nbytes=n,
                                 poster=poster, label=label, table=_table())
    store.put_sidecar(ref, sc)
    sc = store.sidecar(ref) or {**sc, "root": ref[0], "name": ref[1]}
    return sc


def _table() -> dict:
    """Licence table for aggregate: the merged one (owner's models.json may add licences)."""
    return _m().licences


def _poster_name(name: str) -> str:
    return name.rsplit(".", 1)[0] + ".png"


def _remove_unowned(ref: Ref) -> None:
    """Delete a temporary file of ours that has no sidecar (never an owned file)."""
    try:
        if store.owned(ref):
            return
        p = store.resolve(ref)
        if os.path.isfile(p) and not os.path.islink(p):
            os.remove(p)
    except Exception:  # noqa: BLE001 - cleanup is best effort
        pass


# --------------------------------------------------------------------------------------------- generate

def generate(body: dict, job) -> dict:
    """A picture from a prompt (Qwen-Image 2.1), optionally with up to 6 reference pictures."""
    cfg, m = _config(), _m()
    prompt = _text(body, "prompt", MAX_PROMPT)
    if not prompt:
        raise ValueError("write a prompt first")
    negative = _text(body, "negative", MAX_PROMPT)
    variant = m.variant(body.get("variant") or body.get("model"), strict=True)
    raw_refs = body.get("refs") or []
    if not isinstance(raw_refs, list) or len(raw_refs) > graphs.MAX_REFS:
        raise ValueError(f"at most {graphs.MAX_REFS} reference pictures")
    refs = [_picture(r) for r in raw_refs]
    seed = _seed(body)
    steps = max(4, min(int(body.get("steps") or 30), 60))
    size = body.get("size") or "square"
    if size != "match" and size not in graphs.SIZES:
        raise ValueError(f"size must be one of {', '.join(graphs.SIZES)} or match")
    if size == "match" and body.get("ref_w") and body.get("ref_h"):
        sq = graphs.SIZES["square"]
        w, h = graphs.fit(int(body["ref_w"]), int(body["ref_h"]), sq[0] * sq[1])
    else:
        w, h = graphs.SIZES.get(size, graphs.SIZES["square"])
    graph = graphs.image_graph(variant, prompt, negative, w, h, steps, seed, [load_value(r) for r, _ in refs],
                               _prefix("img"))
    pid, outs, secs = _run(graph, "image", TIMEOUT_PICTURE, job)
    name = _pick(outs, "images")
    ref = ("gen", name)
    store.ledger_add(pid, "image", [ref])
    if job is not None:
        job.progress(1.0, "saving")
    inputs = [provenance.input_entry("ref", r, sc or None) for r, sc in refs]
    sc = _save("image", ref, job=job, used=m.used_image(variant["id"]), inputs=inputs, pids=[pid],
               path=store.resolve(ref), label=prompt, prompt=prompt, negative=negative, seed=seed,
               params={"w": w, "h": h, "steps": steps, "size": size, "variant": variant["id"]},
               fallback_probe={"w": w, "h": h, "seconds": 0})
    return {**store.item(sc), "seed": seed, "seconds": secs, "size": f"{w}x{h}", "variant": variant["id"]}


# --------------------------------------------------------------------------------------------- animate

def animate(body: dict, job) -> dict:
    """A 3 or 5 s clip from a picture (Wan 2.2 14B image-to-video, lightx2v 4-step)."""
    cfg, m = _config(), _m()
    start, start_sc = _picture(body.get("start"))
    prompt = _text(body, "prompt", MAX_PROMPT) or DEFAULT_ANIMATE
    seed = _seed(body)
    quality = body.get("quality") if body.get("quality") in graphs.VIDEO_AREA else "480p"
    secs_key = str(body.get("seconds") or "3")
    frames = graphs.VIDEO_FRAMES.get(secs_key, graphs.VIDEO_FRAMES["3"])
    pr = start_sc.get("probe") if isinstance(start_sc.get("probe"), dict) else {}
    sw = int(body.get("w") or pr.get("w") or 832)
    sh = int(body.get("h") or pr.get("h") or 480)
    if sw <= 0 or sh <= 0:
        sw, sh = 832, 480
    w, h = graphs.fit(sw, sh, graphs.VIDEO_AREA[quality])
    graph = graphs.video_graph(m.video, load_value(start), prompt, w, h, frames, seed, _prefix("clip"))
    pid, outs, secs = _run(graph, "clip", TIMEOUT_CLIP, job)
    name = _pick(outs, "gifs")
    ref = ("gen", name)
    store.ledger_add(pid, "clip", [ref])
    if job is not None:
        job.progress(1.0, "saving")
    poster = _poster_name(name)
    if not os.path.isfile(store.resolve(("gen", poster))):
        try:
            media.poster(store.resolve(ref), store.resolve(("gen", poster)))
        except Exception:  # noqa: BLE001
            poster = None
    sc = _save("clip", ref, job=job, used=m.used_video(), inputs=[provenance.input_entry("start", start, start_sc or None)],
               pids=[pid], path=store.resolve(ref), poster=poster, label=prompt, prompt=prompt,
               negative=graphs.WAN_NEGATIVE, seed=seed,
               params={"w": w, "h": h, "frames": frames, "fps": graphs.VIDEO_FPS, "steps": graphs.VIDEO_STEPS,
                       "quality": quality, "seconds": secs_key, "variant": None},
               fallback_probe={"w": w, "h": h, "frames": frames, "fps": f"{graphs.VIDEO_FPS}/1",
                               "seconds": frames / graphs.VIDEO_FPS})
    return {**store.item(sc), "seed": seed, "elapsed": secs, "size": f"{w}x{h}"}


# --------------------------------------------------------------------------------------------- extend

def _wan_size(w: int, h: int) -> tuple[int, int]:
    """Wan wants multiples of 16 and a sane area: keep a Wan-made size, else fit into at most 720p."""
    cap = graphs.VIDEO_AREA["720p"]
    # graphs.fit rounds each side to 16, so a size animate made can be up to 8 px per side over the area cap
    if w > 0 and h > 0 and w % 16 == 0 and h % 16 == 0 and w * h <= cap + 8 * (w + h) + 64:
        return w, h
    return graphs.fit(max(w, 16), max(h, 16), min(max(w * h, graphs.VIDEO_AREA["480p"]), cap))


def extend(body: dict, job) -> dict:
    """Continue any owned video from its last frame, then stitch the two into a new, longer clip.
    The original stays. The segment and the last-frame picture are removed, and the segment's ComfyUI
    history entry is deleted by its prompt id."""
    cfg, m = _config(), _m()
    src = _owned(body.get("src"))
    if store.media_type(src[1]) != "video":
        raise ValueError("pick a video to extend")
    src_sc = store.sidecar(src) or {}
    src_path = store.resolve(src)
    pr = media.probe(src_path)
    w, h = _wan_size(pr.w, pr.h)
    prompt = _text(body, "prompt", MAX_PROMPT) or DEFAULT_EXTEND
    seed = _seed(body)
    secs_key = str(body.get("seconds") or "3")
    frames = graphs.VIDEO_FRAMES.get(secs_key, graphs.VIDEO_FRAMES["3"])

    if job is not None:
        job.progress(None, "last frame")
    tmp = store.tmp_path(".png")
    lastframe_ref = None
    seg_ref = None
    pid = None
    dest = f"ext_{store.stamp()}.mp4"
    dest_ref = ("gen", dest)
    dest_path = store.resolve(dest_ref)
    ok = False
    try:
        media.last_frame(src_path, tmp)
        with open(tmp, "rb") as f:
            data = f.read()
        stored = comfy.upload_image(data, f"lastframe_{store.stamp()}.png", cfg.input_subdir)
        lastframe_ref = store.check_ref(("in", stored))
        graph = graphs.video_graph(m.video, load_value(lastframe_ref), prompt, w, h, frames, seed,
                                   _prefix("seg"))
        pid, outs, secs = _run(graph, "segment", TIMEOUT_CLIP, job)
        seg_ref = ("gen", _pick(outs, "gifs"))
        store.ledger_add(pid, "segment", [seg_ref])
        if job is not None:
            job.progress(None, "stitching")
        media.stitch_extension(src_path, store.resolve(seg_ref), dest_path, job=job)
        # a cancel that came during the stitch discards the result (the finally removes dest)
        jobs.commit(job)
        poster = _poster_name(dest)
        src_poster = src_sc.get("poster")
        try:
            if isinstance(src_poster, str) and os.path.isfile(store.resolve((src[0], src_poster))):
                shutil.copyfile(store.resolve((src[0], src_poster)), store.resolve(("gen", poster)))
            else:
                media.poster(dest_path, store.resolve(("gen", poster)))
        except Exception:  # noqa: BLE001
            poster = None
        if job is not None:
            job.progress(1.0, "saving")
        sc = _save("extension", dest_ref, job=job, used=m.used_video(),
                   inputs=[provenance.input_entry("source", src, src_sc or None)], pids=[pid], path=dest_path,
                   poster=poster, label=prompt, prompt=prompt, negative=graphs.WAN_NEGATIVE, seed=seed,
                   params={"w": w, "h": h, "frames": frames, "fps": graphs.VIDEO_FPS, "steps": graphs.VIDEO_STEPS,
                           "seconds": secs_key, "source": {"root": src[0], "name": src[1]}})
        ok = True
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
        if lastframe_ref:
            _remove_unowned(lastframe_ref)
        if seg_ref:
            _remove_unowned(seg_ref)
            _remove_unowned(("gen", _poster_name(seg_ref[1])))
            try:
                store.ledger_drop(seg_ref)
            except Exception:  # noqa: BLE001
                pass
        if pid:
            comfy.forget([pid])
        if not ok:
            _remove_unowned(dest_ref)
            _remove_unowned(("gen", _poster_name(dest)))
    return {**store.item(sc), "seed": seed, "elapsed": secs, "size": f"{w}x{h}"}


# --------------------------------------------------------------------------------------------- music

def music(body: dict, job) -> dict:
    """A music track (ACE-Step 1.5 turbo), saved as lossless FLAC."""
    cfg, m = _config(), _m()
    tags = _text(body, "tags", MAX_TAGS)
    if not tags:
        raise ValueError("describe the style first")
    lyrics = _text(body, "lyrics", MAX_PROMPT)
    bpm = max(10, min(int(body.get("bpm") or 120), 300))
    seconds = graphs.quantise_seconds(max(5.0, min(float(body.get("seconds") or 30.0), 240.0)))
    keyscale = body.get("keyscale") or "C major"
    if keyscale not in graphs.KEYSCALES:
        raise ValueError("unknown key")
    timesig = str(body.get("timesig") or "4")
    if timesig not in graphs.TIMESIGS:
        raise ValueError("time signature must be 2, 3, 4 or 6")
    language = body.get("language") or "en"
    if language not in graphs.LANGUAGES:
        raise ValueError("unknown language")
    seed = _seed(body)
    from . import features
    saver = features.pick_saver({s: comfy.object_info(s) for s in graphs.SAVERS})
    if saver is None:
        raise comfy.ComfyError("ComfyUI has no audio saver node (SaveAudio or SaveAudioAdvanced)")
    graph = graphs.music_graph(m.music, tags, lyrics, bpm, seconds, seed, keyscale=keyscale, timesig=timesig,
                               language=language, saver=saver, prefix=_prefix("music"))
    pid, outs, secs = _run(graph, "music", TIMEOUT_MUSIC, job)
    name = _pick(outs, "audio")
    ref = ("gen", name)
    store.ledger_add(pid, "music", [ref])
    if job is not None:
        job.progress(1.0, "saving")
    sc = _save("music", ref, job=job, used=m.used_music(), inputs=[], pids=[pid], path=store.resolve(ref), label=tags,
               prompt=tags, seed=seed,
               params={"tags": tags, "lyrics": lyrics, "bpm": bpm, "seconds": seconds, "keyscale": keyscale,
                       "timesig": timesig, "language": language, "seed": seed, "saver": saver},
               fallback_probe={"seconds": seconds, "audio_rate": 48000, "has_audio": True})
    pr = sc.get("probe") or {}
    got = float(pr.get("seconds") or seconds)
    samples = int(pr.get("audio_samples") or 0) or _rhu(Fraction(str(got)) * 48000)
    return {"ref": store.ref_json(ref), "url": store.url_for(ref), "seconds": got, "title": sc.get("label") or name,
            "kind": "music", "commercial_ok": sc.get("commercial_ok"), "samples": samples, "elapsed": secs,
            "seed": seed}


# --------------------------------------------------------------------------------------------- upload

KIND_OF = {"png": "image", "jpg": "image", "webp": "image", "mp4": "video", "mov": "video", "webm": "video",
           "mp3": "audio", "wav": "audio", "m4a": "audio"}


def ingest_upload(stream: BinaryIO, length: int, filename: str, ctype: str) -> dict:
    """Stream an upload to DATA/tmp with a running cap, check its magic bytes and ffprobe it, then store it
    under a server-made name. Pictures go to ComfyUI's input folder (root "in"); video and audio to "lab".
    The original filename is used only as the label."""
    cfg = _config()
    caps = {"image": cfg.max_image_mb, "video": cfg.max_video_mb, "audio": cfg.max_audio_mb}
    cap_all = max(caps.values()) * 1024 * 1024
    length = int(length or 0)
    if length <= 0:
        raise ValueError("the upload is empty")
    if length > cap_all:
        raise TooLarge(f"too big: files can be {max(caps.values())} MB at most")
    tmp = store.tmp_path(".part")
    try:
        got = 0
        head = b""
        with open(tmp, "wb") as f:
            while got < length:
                chunk = stream.read(min(CHUNK, length - got))
                if not chunk:
                    break
                if len(head) < 64:
                    head += chunk[:64 - len(head)]
                got += len(chunk)
                if got > cap_all:
                    raise TooLarge("too big")
                f.write(chunk)
        if got != length:
            raise ValueError("the upload was cut short; try again")
        ext = media.sniff(head)
        kind = KIND_OF.get(ext or "")
        if not kind:
            raise BadType("that file type isn't supported (use PNG, JPEG, WebP, MP4, MOV, WebM, MP3, WAV or M4A)")
        declared = (ctype or "").split(";")[0].strip().lower().split("/")[0]
        if declared in caps and declared != kind:
            raise BadType("the file's contents don't match its type")
        if got > caps[kind] * 1024 * 1024:
            raise TooLarge(f"too big: {kind} files can be {caps[kind]} MB at most")
        try:
            pr = media.probe(tmp)
        except Exception:  # noqa: BLE001
            raise BadType("YourContent couldn't read that file") from None
        try:
            fmt = media.format_name(tmp)
        except Exception:  # noqa: BLE001
            fmt = ""
        if fmt not in media.SNIFF_FORMATS.get(ext, ()):
            raise BadType("the file's contents don't match its type")
        if kind == "image" and pr.kind != "image":
            raise BadType("YourContent couldn't read that picture")
        if kind == "video":
            if pr.kind != "video" or pr.frames < 1:
                raise BadType("YourContent couldn't read that video")
            if pr.seconds > MAX_VIDEO_SECONDS or max(pr.w, pr.h) > MAX_VIDEO_PX:
                raise BadType("videos can be 10 minutes and 4096 pixels at most")
        if kind == "audio":
            if pr.kind != "audio" or not pr.has_audio or not 1 <= pr.seconds <= 1200:
                raise BadType("audio must be between 1 second and 20 minutes")
        label = provenance.clean_label(filename or "upload")
        stamp = store.stamp()
        poster = None
        if kind == "image":
            name = f"ref_{stamp}.{ext}"
            with open(tmp, "rb") as f:
                data = f.read()
            try:
                stored = comfy.upload_image(data, name, cfg.input_subdir)
            except comfy.ComfyOff:
                stored = name
                shutil.copyfile(tmp, store.resolve(("in", name)))
            ref = store.check_ref(("in", stored))
            path = store.resolve(ref)
        elif kind == "video":
            if pr.vfr:
                fps = max(1, _rhu(Fraction(pr.frames) / Fraction(str(pr.seconds)))) if pr.seconds else \
                    max(1, _rhu(Fraction(pr.rate)))
                name = f"up_{stamp}_cfr.mp4"
                ref = ("lab", name)
                path = store.resolve(ref)
                media.run_ffmpeg(["ffmpeg", "-nostdin", "-y", "-v", "error", *media.inp(tmp), "-vf", f"fps={fps}",
                                  "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                                  "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", path], 1800)
            else:
                name = f"up_{stamp}.{ext}"
                ref = ("lab", name)
                path = store.resolve(ref)
                os.replace(tmp, path)
            poster = name.rsplit(".", 1)[0] + ".jpg"
            try:
                media.poster(path, store.resolve(("lab", poster)))
            except Exception:  # noqa: BLE001
                poster = None
        else:
            name = f"up_{stamp}.{ext}"
            ref = ("lab", name)
            path = store.resolve(ref)
            os.replace(tmp, path)
        try:
            pr = media.probe(path)
        except Exception:  # noqa: BLE001 - keep the first probe (e.g. ComfyUI's input is not mounted here)
            pass
        try:
            sha, n = media.sha256_file(path)
        except OSError:
            sha, n = None, got
        sc = provenance.make_sidecar("upload", ref, models=[], inputs=[provenance.upload_entry(ref)],
                                     probe=pr, sha256=sha, nbytes=n, poster=poster, label=label,
                                     params={"type": kind}, table=_table())
        store.put_sidecar(ref, sc)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    item_ = store.item(store.sidecar(ref) or sc)
    out = {"ref": store.ref_json(ref), "type": kind, "url": store.url_for(ref), "poster": item_.get("poster", ""),
           "probe": provenance.probe_dict(pr), "label": label, "commercial_ok": None}
    if kind in ("video", "audio"):
        out["seconds"] = pr.seconds
    return out


# --------------------------------------------------------------------------------------------- delete

def delete(ref: Ref) -> dict:
    """Delete an owned file for good (plus its poster and sidecar) and remove exactly the ComfyUI history
    entries our ledger recorded for it. Projects that used it show it as missing."""
    ref = _owned(ref)
    try:
        from . import projects
        used = projects.used_in(ref)
    except Exception:  # noqa: BLE001
        used = []
    pids = store.ledger_pids(ref)
    removed = store.remove(ref)
    n = comfy.forget(pids) if pids else 0
    store.ledger_drop(ref)
    return {"removed": removed, "history_deleted": n, "used_in": used}
