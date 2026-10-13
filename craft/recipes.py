"""The four craft recipes. Each takes (job, ctx) and returns the output file names it left in ctx.out, main first.

ctx: work (the job's sandbox folder, the only place the apps can write), inp (ref key "root/name" -> copied input
path, named without digits so EffectCraft does not read a still as an image sequence), out (= work/out), brand
(a normalised brand kit, craft_spec.NEUTRAL_KIT when the request had none), fonts (a TTF/OTF folder or None),
warn (list of warnings), sessions (open app sessions, closed by the runner), meta (output name -> {w, h, seconds}),
ai_masks / sam3_dir (the opt-in SAM 3 settings, LightCraft only). Every path handed to an app is inside ctx.work.
"""
from __future__ import annotations

import struct
from pathlib import Path

from medialab.craft_spec import SIZES, ref_key

from .mcp_client import Session

TPS = 254016000000  # FilmCraft ticks per second


def image_size(path):
    """(w, h) of a JPEG, PNG or WebP from its header, or None."""
    b = Path(path).read_bytes()[:65536]
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", b[16:24])
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        if b[12:16] == b"VP8X":
            return 1 + int.from_bytes(b[24:27], "little"), 1 + int.from_bytes(b[27:30], "little")
        if b[12:16] == b"VP8 ":
            w, h = struct.unpack("<HH", b[26:30])
            return w & 0x3FFF, h & 0x3FFF
        if b[12:16] == b"VP8L":
            bits = int.from_bytes(b[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if b[:2] == b"\xff\xd8":
        data = Path(path).read_bytes()
        i = 2
        while i < len(data) - 9:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h
            i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    return None


def _session(app, ctx):
    s = Session(app, ctx.work, ctx.fonts, ai_masks=getattr(ctx, "ai_masks", False),
                sam3_dir=getattr(ctx, "sam3_dir", None))
    ctx.sessions.append(s)
    return s


def _inp(ctx, ref):
    return ctx.inp[ref_key(ref)]


# --------------------------------------------------------------------------------------------- photo.grade (LightCraft)

def photo_grade(job, ctx):
    p = job["params"]
    src = _inp(ctx, p["image"])
    ext = "jpg" if p["format"] == "jpeg" else "png"
    with _session("lightcraft", ctx) as s:
        imp, _ = s.call("import", {"paths": [str(src)]}, "import")
        pid = (imp.get("imported") or [None])[0] if isinstance(imp, dict) else None
        if pid is None:
            raise RuntimeError(f"LightCraft did not import the image: {imp}")
        controls, _ = s.call("list_controls", {}, "controls")
        items = controls if isinstance(controls, list) else controls.get("controls", [])
        ranges = {c["id"]: (c.get("min"), c.get("max")) for c in items if isinstance(c, dict) and c.get("id")}
        clean = {}
        for k, v in p["develop"].items():
            if k not in ranges:
                ctx.warn.append(f"LightCraft has no control {k}; skipped")
                continue
            lo, hi = ranges[k]
            clean[k] = min(max(v, lo), hi) if lo is not None and hi is not None else v
        if clean:
            s.call("set_develop", {"id": pid, "label": f"look {p['look']}", "values": clean}, "develop")
        s.call("export", {"id": pid, "path": str(ctx.out / f"graded.{ext}"), "format": p["format"],
                          "quality": p["quality"], "longEdge": p["long_edge"], "dontEnlarge": True,
                          "colorSpace": "srgb"}, "export")
    return [f"graded.{ext}"]


# --------------------------------------------------------------------------------------------- photo.finish (PhotoCraft)

CURVES = {"soft": [[0, 4], [64, 58], [192, 200], [255, 252]], "strong": [[0, 10], [64, 50], [192, 208], [255, 248]]}


def photo_finish(job, ctx):
    p = job["params"]
    src = _inp(ctx, p["image"])

    def rel(path):  # PhotoCraft takes paths relative to its write root
        return str(Path(path).relative_to(ctx.work))

    tw, th = SIZES[p["format"]]
    ratio = tw / th
    with _session("photocraft", ctx) as s:
        doc, _ = s.call("doc_open", {"path": rel(src)}, "open")
        w, h = doc["width"], doc["height"]
        if w / h > ratio:
            cw, ch = round(h * ratio), h
        else:
            cw, ch = w, round(w / ratio)
        x = min(max(round(p["focus"][0] * w - cw / 2), 0), w - cw)
        y = min(max(round(p["focus"][1] * h - ch / 2), 0), h - ch)
        s.call("command_run", {"id": "image.crop", "params": {"x": x, "y": y, "width": cw, "height": ch}}, "crop")
        width = p["width"]
        s.call("command_run", {"id": "image.imageSize", "params": {"width": width, "height": round(width / ratio),
                                                                     "resample": "bicubic"}}, "resize")
        if width > cw:
            ctx.warn.append(f"upscaled {cw}px -> {width}px; the source is smaller than the output")
        if p["contrast"] != "none":
            s.call("command_run", {"id": "layer.newAdjustmentLayer.curves",
                                   "params": {"points": CURVES[p["contrast"]]}}, "curves")
        if p["vibrance"]:
            s.call("command_run", {"id": "layer.newAdjustmentLayer.vibrance",
                                   "params": {"vibrance": p["vibrance"]}}, "vibrance")
        if p["warmth"]:
            k = p["warmth"]
            s.call("command_run", {"id": "layer.newAdjustmentLayer.colorBalance",
                                   "params": {"midtones": [round(k * 0.5), 0, round(-k * 0.6)],
                                              "highlights": [round(k * 0.3), 0, round(-k * 0.4)]}}, "warmth")
        outs = []
        if p["psd"]:
            s.call("doc_save", {"path": rel(ctx.out / "finished.psd")}, "psd")
            outs.append("finished.psd")
        s.call("doc_export", {"path": rel(ctx.out / "finished.jpg"), "quality": p["quality"]}, "jpeg")
    return ["finished.jpg"] + outs


# --------------------------------------------------------------------------------------------- video.titlecard (EffectCraft)

def _lines(text, size, width):
    if not text:
        return 0
    per = max(4, int(width / (size * 0.52)))
    n, cur = 1, 0
    for word in text.split():
        if cur and cur + 1 + len(word) > per:
            n, cur = n + 1, len(word)
        else:
            cur += (1 if cur else 0) + len(word)
    return n


def _import_layer(s, ctx, ex, src, secs, what):
    """Import a still into EffectCraft and place it as the newest layer ("#1"). Returns its pixel size."""
    iw, ih = image_size(src) or (0, 0)
    imp = ex("file.import", {"paths": [str(src)]}, f"import {what}")
    item = (imp.get("items") or [None])[0] if isinstance(imp, dict) else None
    if item is None:
        raise RuntimeError(f"EffectCraft did not import the {what}: {imp}")
    ex("layer.addItem", {"item": item, "time": 0, "duration": secs}, f"place {what}")
    return iw, ih


def titlecard(job, ctx):
    p, b = job["params"], ctx.brand
    W, H = SIZES[p["format"]]
    secs, fps, k = p["seconds"], p["fps"], W / 1080
    margin = round(90 * k)
    box_w = W - 2 * margin
    head = p["headline"]
    hs = round((112 if len(head) <= 18 else 96 if len(head) <= 30 else 80 if len(head) <= 48 else 66) * k)
    ks, ss, hds = round(34 * k), round(42 * k), round(30 * k)
    nh, ns = _lines(head, hs, box_w), _lines(p["sub"], ss, box_w)
    block = (ks * 1.4 + 30 * k if p["kicker"] else 0) + nh * hs * 1.12 + (24 * k + ns * ss * 1.3 if p["sub"] else 0)
    top = (H - block) / 2 - (H * 0.03 if p["format"] == "9:16" else 0)  # a little high of centre, clear of the UI
    fade_out = max(secs - 0.5, 1.9)
    outro = [{"time": fade_out, "value": 100}, {"time": secs, "value": 0}]

    with _session("effectcraft", ctx) as s:
        def ex(cmd, params, note=""):
            return s.call("execute_command", {"command": cmd, "params": params}, note or cmd)[0]

        ex("comp.new", {"name": "Title", "width": W, "height": H, "frameRate": fps, "duration": secs,
                        "background": b["bg"]}, "comp")
        if not p["alpha"]:
            ex("layer.newSolid", {"name": "Base", "color": b["bg"], "width": W, "height": H}, "base")
        if p["background"]:
            src = _inp(ctx, p["background"])
            iw, ih = _import_layer(s, ctx, ex, src, secs, "background")
            iw, ih = iw or W, ih or H
            bg = "#1"  # the newest layer sits on top
            cover = max(W / iw, H / ih) * 100 * (1.12 if p["motion"] == "pan" else 1.0)
            if p["motion"] == "push":
                s.call("add_keyframe", {"layer": bg, "path": "transform/scale", "interpolation": "easyEase",
                                        "keys": [{"time": 0, "value": [cover, cover]},
                                                 {"time": secs, "value": [cover * 1.08, cover * 1.08]}]}, "push")
            else:
                s.call("set_property", {"layer": bg, "path": "transform/scale", "value": [cover, cover]}, "cover")
            if p["motion"] == "pan":
                shift = (iw * cover / 100 - W) / 2 * 0.9
                s.call("add_keyframe", {"layer": bg, "path": "transform/position", "interpolation": "easyEase",
                                        "keys": [{"time": 0, "value": [W / 2 + shift, H / 2]},
                                                 {"time": secs, "value": [W / 2 - shift, H / 2]}]}, "pan")
            s.call("add_effect", {"layer": bg, "effect": "Gaussian Blur", "values": {"blurriness": round(5 * k)}}, "blur")
            ex("layer.newSolid", {"name": "Shade", "color": b["bg"], "width": W, "height": H}, "shade")
            s.call("set_property", {"layer": "Shade", "path": "transform/opacity", "value": 55}, "shade 55%")
        if p["logo"]:
            iw, ih = _import_layer(s, ctx, ex, _inp(ctx, b["logo"]), secs, "logo")
            target_h = round(110 * k)
            scale = min(target_h / ih if ih else 1.0, (box_w * 0.5) / iw if iw else 1.0) * 100
            s.call("set_property", {"layer": "#1", "path": "transform/scale", "value": [scale, scale]}, "logo size")
            s.call("set_property", {"layer": "#1", "path": "transform/position",
                                    "value": [W / 2, round(margin + target_h / 2)]}, "logo pos")
            s.call("add_keyframe", {"layer": "#1", "path": "transform/opacity", "interpolation": "easyEase",
                                    "keys": [{"time": 0.0, "value": 0}, {"time": 0.6, "value": 100}] + outro}, "logo fade")
        y = top
        if p["kicker"]:
            ex("layer.newText", {"text": p["kicker"].upper(), "name": "Kicker",
                                 "box": [margin, round(y), box_w, round(ks * 1.6)], "font": b["sans"], "size": ks,
                                 "fill": b["ac"], "justify": "center", "tracking": 120}, "kicker")
            s.call("add_keyframe", {"layer": "Kicker", "path": "transform/opacity", "interpolation": "easyEase",
                                    "keys": [{"time": 0.1, "value": 0}, {"time": 0.7, "value": 100}] + outro},
                   "kicker fade")
            y += ks * 1.4 + 10 * k
            ex("layer.newSolid", {"name": "Rule", "color": b["ac"], "width": round(120 * k),
                                  "height": max(2, round(5 * k))}, "rule")
            s.call("set_property", {"layer": "Rule", "path": "transform/position",
                                    "value": [W / 2, round(y + 10 * k)]}, "rule pos")
            s.call("add_keyframe", {"layer": "Rule", "path": "transform/scale", "interpolation": "easyEase",
                                    "keys": [{"time": 0.3, "value": [0, 100]}, {"time": 0.9, "value": [100, 100]}]},
                   "rule grow")
            s.call("add_keyframe", {"layer": "Rule", "path": "transform/opacity", "interpolation": "linear",
                                    "keys": outro}, "rule out")
            y += 30 * k
        ex("layer.newText", {"text": head, "name": "Headline",
                             "box": [margin, round(y), box_w, round(nh * hs * 1.2 + hs * 0.3)],
                             "font": b["display"], "size": hs, "fill": b["fg"], "justify": "center"}, "headline")
        s.call("add_keyframe", {"layer": "Headline", "path": "transform/opacity", "interpolation": "easyEase",
                                "keys": [{"time": 0.4, "value": 0}, {"time": 1.3, "value": 100}] + outro},
               "headline fade")
        pos, _ = s.call("get_property", {"layer": "Headline", "path": "transform/position"}, "headline pos")
        px, py = (pos.get("value") or [W / 2, H / 2])[:2] if isinstance(pos, dict) else (W / 2, H / 2)
        s.call("add_keyframe", {"layer": "Headline", "path": "transform/position", "interpolation": "easyEase",
                                "keys": [{"time": 0.4, "value": [px, py + 40 * k]}, {"time": 1.3, "value": [px, py]}]},
               "headline rise")
        y += nh * hs * 1.12 + 24 * k
        if p["sub"]:
            ex("layer.newText", {"text": p["sub"], "name": "Sub",
                                 "box": [margin, round(y), box_w, round(ns * ss * 1.4 + ss * 0.3)],
                                 "font": b["sans"], "size": ss, "fill": b["mut"] or b["fg"], "justify": "center"}, "sub")
            s.call("add_keyframe", {"layer": "Sub", "path": "transform/opacity", "interpolation": "easyEase",
                                    "keys": [{"time": 0.9, "value": 0}, {"time": 1.8, "value": 100}] + outro}, "sub fade")
        if p["handle"]:
            hy = H - margin - round(hds * 1.6) - (round(H * 0.12) if p["format"] == "9:16" else 0)
            ex("layer.newText", {"text": b["handle"], "name": "Handle", "box": [margin, hy, box_w, round(hds * 1.6)],
                                 "font": b["sans"], "size": hds, "fill": b["mut"] or b["fg"], "justify": "center"},
               "handle")
            s.call("add_keyframe", {"layer": "Handle", "path": "transform/opacity", "interpolation": "easyEase",
                                    "keys": [{"time": 1.0, "value": 0}, {"time": 1.8, "value": 100}] + outro},
                   "handle fade")
        name = "titlecard.mov" if p["alpha"] else "titlecard.mp4"
        q = {"format": "prores", "proresProfile": "4444", "channels": "rgba"} if p["alpha"] else {"format": "h264"}
        ex("renderQueue.add", dict(q, output=str(ctx.out / name), audio="off"), "queue")
        ex("renderQueue.render", {"wait": True}, "render")
        s.call("render_frame", {"time": min(1.9, secs * 0.6), "path": str(ctx.out / "poster.png"), "inline": False,
                                "max_side": 0}, "poster")
    ctx.meta[name] = {"w": W, "h": H, "seconds": secs}
    return [name, "poster.png"]


# --------------------------------------------------------------------------------------------- video.reel (FilmCraft)

def reel(job, ctx):
    p, b = job["params"], ctx.brand
    W, H = SIZES[p["format"]]
    k = W / 1080
    with _session("filmcraft", ctx) as s:
        def cmd(i, params, note=""):
            return s.call("command_run", {"id": i, "params": params}, note or i)[0]

        cmd("file.newProject", {"name": f"craft {job['id']}"})
        paths = [str(_inp(ctx, it["ref"])) for it in p["items"]]
        music = str(_inp(ctx, p["music"])) if p["music"] else None
        imp = cmd("file.import", {"paths": paths + ([music] if music else []), "imageSequence": False}, "import")
        items = (imp.get("items") or []) if isinstance(imp, dict) else []
        if (isinstance(imp, dict) and imp.get("errors")) or len(items) != len(paths) + (1 if music else 0):
            raise RuntimeError(f"FilmCraft import: {imp.get('errors') if isinstance(imp, dict) else imp}")
        proj, _ = s.call("project_inspect", {}, "durations")
        children = (proj.get("root") or {}).get("children", []) if isinstance(proj, dict) else []
        dur = {c.get("item"): c.get("duration") for c in children if isinstance(c, dict)}
        cmd("file.newSequence", {"name": "Reel", "width": W, "height": H, "fps": p["fps"]}, "sequence")
        # timeline.place returns the picture clip first, then any sound clips linked to it (a phone clip or an
        # upload with an audio track). Transitions are per track kind: a video transition on a sound clip fails
        # with "no edit point" (audit 2026-10-11), so the two are kept apart.
        t, clips, sounds, caps = 0, [], [], []
        for it, item in zip(p["items"], items):
            label = it["ref"][1]
            if it["kind"] == "video":
                have = (dur.get(item) or 15 * TPS)
                length = min(have, round((it["seconds"] or 15) * TPS))
                if it["seconds"] and it["seconds"] * TPS > have + TPS / 10:
                    ctx.warn.append(f"{label} is {have / TPS:.1f} s; used all of it, not {it['seconds']} s")
            else:
                length = round(it["seconds"] * TPS)
            r = cmd("timeline.place", {"item": item, "track": "V1", "time": t, "insert": False, "duration": length},
                    f"place {label}")
            placed = r.get("clips") or []
            if not placed:
                raise RuntimeError(f"FilmCraft placed nothing for {label}")
            clips.append(placed[0])
            sounds.append(placed[1:])
            if it["caption"]:
                caps.append((it["caption"], t, length))
            t += length
        total = t / TPS
        cmd("clip.fillFrame", {"clips": clips}, "fill frame")
        if p["transition"] == "dissolve":
            for c in clips[1:]:
                cmd("sequence.applyVideoTransition", {"clip": c, "effect": "cross_dissolve",
                                                      "frames": p["transition_frames"], "edge": "in"}, "dissolve")
            if not music:  # with music the track replaces the clips' own sound (below), so nothing to cross
                for before, now in zip(sounds, sounds[1:]):
                    # crossfade only where two pieces of sound meet; sound after a still just starts at the cut
                    if before and now:
                        cmd("sequence.applyAudioTransition", {"clip": now[0], "effect": "constant_power",
                                                              "frames": p["transition_frames"], "edge": "in"},
                            "crossfade")
        margin = round(80 * k)
        top = round(H * (0.70 if p["format"] == "9:16" else 0.78))  # clear of the platform UI (bottom 20%)
        for text, start, length in caps:
            # paragraph text in a frame-wide box, so long captions wrap instead of running off the edges
            r = cmd("graphics.newText", {"text": text, "position": [margin, top], "box": [W - 2 * margin, round(H * 0.16)],
                                         "size": round(58 * k), "font": b["sans"], "seconds": length / TPS,
                                         "time": start}, "caption")
            s.call("command_run", {"id": "graphics.set", "params": {"clip": r.get("clip"), "layer": 0, "props": {
                "align": "center", "fill_color": b["fg"], "stroke": True, "stroke_width": max(2, round(4 * k))}}},
                "caption style", check=False)
        if music:
            have = dur.get(items[-1]) or t
            cmd("timeline.place", {"item": items[-1], "track": "A1", "time": 0, "insert": False,
                                   "duration": min(have, t)}, "music")
            if have < t:
                ctx.warn.append(f"the music is {have / TPS:.1f} s, shorter than the reel ({total:.1f} s)")
        cmd("file.exportMedia", {"path": str(ctx.out / "reel.mp4"), "format": "h264", "width": W, "height": H,
                                 "fps": p["fps"], "bitrateKbps": p["bitrate_kbps"], "bitrateMode": "vbr1Pass",
                                 "loudnessLufs": p["loudness"], "wait": True}, "export")
        _, imgs = s.call("render_frame", {"seconds": min(2.0, total * 0.3), "max_side": max(W, H)}, "cover")
        if imgs:
            (ctx.out / "cover.png").write_bytes(imgs[0])
    ctx.meta["reel.mp4"] = {"w": W, "h": H, "seconds": round(total, 3)}
    return ["reel.mp4", "cover.png"] if imgs else ["reel.mp4"]


RUN = {"photo.grade": photo_grade, "photo.finish": photo_finish, "video.titlecard": titlecard, "video.reel": reel}
