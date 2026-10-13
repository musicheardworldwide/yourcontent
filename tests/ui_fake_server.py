"""A tiny fake of the YourContent HTTP API (§6) for developing and testing the page (WP4). Stdlib only.

It serves the real static/ files with the §12 headers and answers every §6 route from memory, with the same
JSON shapes. Jobs finish after a short delay. Nothing touches ComfyUI, ffmpeg (except to make one sample
clip in a temp folder, when ffmpeg exists) or any real media folder.

    python3 tests/ui_fake_server.py [--port 18191] [--job-seconds 1.5] [--token T] [--comfy-off]

From a test:

    srv = FakeUI(job_seconds=0.3); url = srv.start(); ...; srv.stop()
    srv.requests  -> [(method, path, headers, body)]  every request the page made
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import hmac
import io
import json
import math
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import wave
import zlib
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/static/style.css": ("style.css", "text/css; charset=utf-8"),
          "/static/tokens.css": ("tokens.css", "text/css; charset=utf-8"),
          "/static/theme.js": ("theme.js", "text/javascript; charset=utf-8"),
          "/static/icons.svg": ("icons.svg", "image/svg+xml"),
          "/static/favicon.svg": ("favicon.svg", "image/svg+xml"),
          "/login": ("login.html", "text/html; charset=utf-8"),
          "/static/login.js": ("login.js", "text/javascript; charset=utf-8"),
          "/static/login.css": ("login.css", "text/css; charset=utf-8")}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; "
       "media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
SR = 48000
PRESETS = [{"id": "9:16", "label": "9:16 Reels / Stories", "w": 1080, "h": 1920},
           {"id": "4:5", "w": 1080, "h": 1350}, {"id": "1:1", "w": 1080, "h": 1080},
           {"id": "16:9", "w": 1920, "h": 1080}, {"id": "source"}]


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def hex_(n: int) -> str:
    return "".join(random.choice("0123456789abcdef") for _ in range(n))


def RHU(x) -> int:
    return math.floor(Fraction(x) + Fraction(1, 2))


def png(w: int, h: int, rgb: tuple[int, int, int]) -> bytes:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def silent_wav(seconds: float, rate: int = 8000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


def sample_mp4(tmp: str, frames: int = 49) -> bytes:
    """A small 16 fps test clip, made with ffmpeg when it exists; else empty bytes (playback just fails)."""
    if not shutil.which("ffmpeg"):
        return b""
    out = os.path.join(tmp, "sample.mp4")
    try:
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-v", "error", "-f", "lavfi", "-i",
                        "testsrc=size=208x208:rate=16", "-frames:v", str(frames), "-pix_fmt", "yuv420p",
                        "-c:v", "libx264", "-preset", "ultrafast", "-movflags", "+faststart", out],
                       check=True, timeout=60, capture_output=True)
        with open(out, "rb") as f:
            return f.read()
    except (OSError, subprocess.SubprocessError):
        return b""


class FakeUI:
    def __init__(self, job_seconds: float = 0.4, token: str | None = None, comfy_ok: bool = True,
                 music_ok: bool = True):
        self.job_seconds = job_seconds
        self.token = token
        self.comfy_ok = comfy_ok
        self.music_ok = music_ok
        self.lock = threading.RLock()
        self.requests: list[tuple[str, str, dict, object]] = []
        self.assets: dict[tuple[str, str], dict] = {}     # (root, name) -> sidecar-ish record
        self.media: dict[tuple[str, str], tuple[bytes, str]] = {}
        self.projects: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}
        self.cache: dict[str, bytes] = {}
        self.counter = 0
        self.tmp = tempfile.mkdtemp(prefix="medialab-ui-fake-")
        self.mp4 = sample_mp4(self.tmp)
        self.httpd: ThreadingHTTPServer | None = None
        self._seed()

    # ----------------------------------------------------------------------------------------- assets
    def _add(self, root, name, kind, data, ctype, *, w=0, h=0, frames=0, rate="0", seconds=0.0,
             has_audio=False, commercial_ok=None, licences=(), restricted_by=(), poster=None, label="",
             prompt=None, seed=None, params=None, legacy=None, pids=()):
        self.counter += 1
        rec = {"schema": "media-lab/asset@1", "root": root, "name": name, "kind": kind,
               "created": f"2026-10-07T21:{self.counter // 60:02d}:{self.counter % 60:02d}Z",
               "order": self.counter, "app": {"name": "YourContent", "version": "0.2.0"}, "label": label,
               "poster": poster, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
               "probe": {"w": w, "h": h, "fps": rate, "frames": frames, "seconds": seconds,
                         "has_audio": has_audio, "audio_rate": 48000 if has_audio else 0,
                         "vcodec": "h264" if frames else ("png" if kind in ("image", "upload") else ""),
                         "acodec": "aac" if has_audio else ""},
               "models": [], "prompt": prompt, "negative": None, "seed": seed, "params": params or {},
               "inputs": [], "commercial_ok": commercial_ok, "licences": list(licences),
               "most_restrictive": (licences[0] if licences else "unknown"), "restricted_by": list(restricted_by),
               "comfy": {"prompt_ids": list(pids)}, "legacy": legacy, "render": None}
        self.assets[(root, name)] = rec
        self.media[(root, name)] = (data, ctype)
        if poster:
            self.media[(root, poster)] = (png(208, 208, (60, 90, 140)), "image/png")
        return rec

    def _seed(self):
        nc = dict(commercial_ok=False, licences=("qwen-image-nc",),
                  restricted_by=("Qwen-Image 2.1 (non-commercial)",))
        leg = {"adopted": now(), "pattern": "qwen_studio", "recovered_from": "png:prompt"}
        self._add("out", "qwen_studio_00001_.png", "image", png(176, 132, (170, 110, 60)), "image/png",
                  w=1472, h=1104, label="a fox in a library", prompt="a fox in a library", seed=1215546937,
                  legacy=leg, **nc)
        self._add("out", "qwen_studio_00009_.png", "image", png(132, 176, (80, 140, 90)), "image/png",
                  w=1104, h=1472, label="portrait by a window", legacy=leg, **nc)
        self._add("out", "qwen_video_00001.mp4", "clip", self.mp4, "video/mp4", w=624, h=624, frames=49,
                  rate="16/1", seconds=3.0625, poster="qwen_video_00001.png", label="slow push in",
                  legacy=dict(leg, pattern="qwen_video"), **nc)
        self._add("gen", "img_00001_.png", "image", png(160, 160, (120, 80, 160)), "image/png", w=1328, h=1328,
                  label="city at dusk", prompt="city at dusk", seed=42, pids=("p1",), **nc)
        self._add("lab", "up_20261007-210000_a1b2c3.mp3", "upload", silent_wav(12.0), "audio/wav", seconds=12.0,
                  has_audio=True, commercial_ok=None, licences=("upload",),
                  restricted_by=("upload song.mp3: your upload, rights not checked",), label="song.mp3")

    def item(self, rec: dict) -> dict:
        """The §6 /api/recent item for an asset record."""
        root, name = rec["root"], rec["name"]
        vid = rec["probe"]["frames"] > 0 or name.endswith(".mp4")
        return {"ref": {"root": root, "name": name}, "type": "video" if vid else "image", "kind": rec["kind"],
                "url": f"/m/{root}/{name}",
                "poster": f"/m/{root}/{rec['poster']}" if rec.get("poster") else None,
                "label": rec["label"], "w": rec["probe"]["w"], "h": rec["probe"]["h"],
                "seconds": rec["probe"]["seconds"], "created": rec["created"], "commercial_ok": rec["commercial_ok"]}

    def is_audio(self, rec: dict) -> bool:
        return rec["kind"] == "music" or (rec["kind"] == "upload" and rec["probe"]["has_audio"]
                                          and not rec["probe"]["frames"])

    def recent(self, limit=60, kind=None):
        recs = sorted(self.assets.values(), key=lambda r: -r["order"])
        out = []
        for r in recs:
            if self.is_audio(r):
                continue
            it = self.item(r)
            if kind == "image" and it["type"] != "image" or kind == "video" and it["type"] != "video" \
                    or kind == "export" and r["kind"] != "export":
                continue
            out.append(it)
        return out[:limit]

    def tracks(self):
        recs = sorted(self.assets.values(), key=lambda r: -r["order"])
        return [{"ref": {"root": r["root"], "name": r["name"]}, "url": f"/m/{r['root']}/{r['name']}",
                 "seconds": r["probe"]["seconds"], "title": r["label"] or r["name"],
                 "commercial_ok": r["commercial_ok"]} for r in recs if self.is_audio(r)]

    # ----------------------------------------------------------------------------------------- planner (simplified §7)
    def resolve(self, p: dict) -> dict:
        ex = p.get("export") or {}
        F = int(ex.get("fps") or 24)
        fade = Fraction(str(ex.get("fade") or 0.5))
        items, Ds, Xs, warns = [], [], [], []
        lic = {"commercial_ok": True, "most_restrictive": "apache-2.0", "restricted_by": [], "uploads": 0}

        def take(ok, why, licence):
            if ok is False:
                if lic["commercial_ok"] is not False:
                    lic["restricted_by"] = []
                lic["commercial_ok"] = False
                lic["most_restrictive"] = licence
                lic["restricted_by"].append(why)
            elif ok is None and lic["commercial_ok"] is True:
                lic["commercial_ok"] = None
                lic["most_restrictive"] = licence
                lic["restricted_by"].append(why)
            elif ok is None and lic["commercial_ok"] is None:
                lic["restricted_by"].append(why)

        for it in p.get("items", []):
            src = it.get("src") or {}
            rec = self.assets.get((src.get("root"), src.get("name")))
            snap = it.get("snap") or {}
            missing = rec is None
            if it.get("kind") == "clip":
                n_src = rec["probe"]["frames"] if rec else int(snap.get("frames") or 49)
                rate = Fraction(rec["probe"]["fps"] if rec else (snap.get("rate") or "16/1"))
                i_in = max(0, min(int(it.get("in") or 0), n_src - 2))
                i_out = it.get("out")
                i_out = n_src if i_out is None else max(i_in + 2, min(int(i_out), n_src))
                D = RHU((i_out - i_in) * F / rate)
            else:
                i_in, i_out = 0, 0
                D = max(1, RHU(Fraction(str(it.get("dur") or 2.5)) * F))
            if missing:
                warns.append(f"{src.get('name', '?')} is missing; shown as black")
            ok = rec["commercial_ok"] if rec else snap.get("commercial_ok")
            if rec and rec["kind"] == "upload":
                lic["uploads"] += 1
            take(ok, f"item {src.get('name')}: " + ("Qwen-Image 2.1 (non-commercial)" if ok is False
                                                   else "your upload: rights not checked"),
                 (rec["licences"][0] if rec and rec["licences"] else ("qwen-image-nc" if ok is False else "upload")))
            Ds.append(D)
            Xs.append(RHU(fade * F) if it.get("join") == "fade" else 0)
            items.append({"id": it.get("id"), "src_in": i_in, "src_out": i_out, "missing": missing,
                          "fit": "fill" if missing else ("fill" if ex.get("fit") == "fill" else "blur")})
        if Xs:
            Xs[-1] = 0
        for k in range(len(Xs) - 1):
            prev = Xs[k - 1] if k else 0
            x = min(Xs[k], Ds[k] - 1 - prev, Ds[k + 1] - 1)
            if x != Xs[k]:
                warns.append(f"crossfade after item {k + 1} " + ("became a cut (item too short)" if x < 2
                                                                  else "was shortened"))
            Xs[k] = x if x >= 2 else 0
        S, s = [], 0
        for D, X in zip(Ds, Xs):
            S.append(s)
            s += D - X
        T = s
        Ts = T * SR // F
        for i, it in enumerate(items):
            it.update({"start": S[i], "frames": Ds[i], "xfade_after": Xs[i], "seconds": f"{Ds[i] / F:.6f}"})
        music = None
        m = p.get("music")
        if m and m.get("src"):
            rec = self.assets.get((m["src"].get("root"), m["src"].get("name")))
            n_m = int((rec["probe"]["seconds"] if rec else 0) * SR)
            off = float(m.get("offset") or 0)
            skip, delay = (RHU(Fraction(str(off)) * SR), 0) if off >= 0 else (0, RHU(Fraction(str(-off)) * SR))
            end = min(Ts, delay + n_m - skip)
            if end - delay <= 0:
                warns.append("music is not heard (start is past the end)")
            else:
                if end < Ts - SR // 2:
                    warns.append(f"music ends {(Ts - end) / SR:.1f} s before the cut")
                music = {"skip": skip, "delay": delay, "end": end, "fade_in": RHU(Fraction(str(m.get("fade_in", 0.5))) * SR),
                         "fade_out": RHU(Fraction(str(m.get("fade_out", 1.5))) * SR), "seconds": n_m / SR}
                if rec:
                    take(rec["commercial_ok"], "music " + rec["name"] + ": your upload: rights not checked"
                         if rec["kind"] == "upload" else "music", rec["licences"][0] if rec["licences"] else "unknown")
                    if rec["kind"] == "upload":
                        lic["uploads"] += 1
        if not items:
            lic = {"commercial_ok": None, "most_restrictive": "unknown", "restricted_by": [], "uploads": 0}
        return {"fps": F, "frames": T, "seconds": f"{T / F:.6f}", "samples": Ts, "items": items, "music": music,
                "warnings": warns, "licence": lic}

    def snap_items(self, p: dict) -> None:
        for it in p.get("items", []):
            src = it.get("src") or {}
            rec = self.assets.get((src.get("root"), src.get("name")))
            if rec:
                pr = rec["probe"]
                snap = {"w": pr["w"], "h": pr["h"], "sar": "1/1", "commercial_ok": rec["commercial_ok"]}
                if it.get("kind") == "clip":
                    snap.update({"frames": pr["frames"], "rate": pr["fps"]})
                it["snap"] = snap

    # ----------------------------------------------------------------------------------------- jobs
    def submit(self, kind: str, label: str, finish, phases=("working",)) -> str:
        jid = "j_" + hex_(12)
        job = {"id": jid, "kind": kind, "state": "queued", "phase": "queued", "progress": None, "label": label,
               "result": None, "error": None}
        self.jobs[jid] = job

        def run():
            time.sleep(min(0.1, self.job_seconds / 4))
            with self.lock:
                if job["state"] == "cancelled":
                    return
                job["state"] = "running"
            steps = 6
            for i in range(steps):
                time.sleep(self.job_seconds / steps)
                with self.lock:
                    if job["state"] == "cancelled":
                        return
                    job["phase"] = phases[min(len(phases) - 1, i * len(phases) // steps)]
                    job["progress"] = round((i + 1) / steps, 3) if kind in ("preview", "export") else None
            with self.lock:
                if job["state"] == "cancelled":
                    return
                try:
                    job["result"] = finish()
                    job["state"] = "done"
                except Exception as exc:  # noqa: BLE001 - shown to the page
                    job["state"], job["error"] = "error", str(exc)
        threading.Thread(target=run, daemon=True).start()
        return jid

    # ----------------------------------------------------------------------------------------- server
    def start(self, port: int = 0, host: str = "127.0.0.1") -> str:
        fake = self

        class H(Handler):
            pass
        H.fake = fake
        self.httpd = _QuietServer((host, port), H)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return f"http://{host}:{self.httpd.server_address[1]}"

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)


class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):  # a browser dropping a media stream is normal
            super().handle_error(request, client_address)


class HTTPErr(Exception):
    def __init__(self, code, message, extra=None):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra or {}


def need(cond, code, message):
    if not cond:
        raise HTTPErr(code, message)


class Handler(BaseHTTPRequestHandler):
    fake: FakeUI
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet
        pass

    # ---- plumbing
    def send(self, code, body: bytes, ctype: str, headers: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, obj, code=200, headers=None):
        self.send(code, json.dumps(obj).encode(), "application/json", headers)

    def authed(self) -> bool:
        t = self.fake.token
        if not t:
            return True
        if hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + t):
            return True
        want = hmac.new(t.encode(), b"media-lab-session", hashlib.sha256).hexdigest()
        cookie = self.headers.get("Cookie", "")
        return any(c.strip() == "ml_session=" + want for c in cookie.split(";"))

    def record(self, body):
        hdr = {k: self.headers.get(k) for k in ("X-Media-Lab", "Content-Type", "X-Filename", "Origin")
               if self.headers.get(k) is not None}
        with self.fake.lock:
            self.fake.requests.append((self.command, self.path, hdr, body))

    def do_PUT(self):
        self.json({"error": "method not allowed"}, 405)

    do_DELETE = do_PATCH = do_OPTIONS = do_PUT

    def do_GET(self):
        path, _, query = self.path.partition("?")
        q = urllib.parse.parse_qs(query)
        self.record(None)
        try:
            if path in STATIC:
                fname, ctype = STATIC[path]
                with open(os.path.join(ROOT, "static", fname), "rb") as f:
                    body = f.read()
                return self.send(200, body, ctype, {"Content-Security-Policy": CSP, "Cache-Control": "no-store"})
            if path == "/api/config":
                return self.json(self.config())
            need(self.authed(), 401, "Sign in first.")
            with self.fake.lock:
                return self.get_api(path, q)
        except HTTPErr as e:
            self.json(dict({"error": e.message}, **e.extra), e.code)

    def get_api(self, path, q):
        f = self.fake
        if path == "/api/health":
            return self.json(self.health())
        if path == "/api/recent":
            return self.json(f.recent(int((q.get("limit") or ["60"])[0]), (q.get("kind") or [None])[0]))
        if path == "/api/tracks":
            return self.json(f.tracks())
        m = re.fullmatch(r"/api/info/(out|gen|in|lab)/([A-Za-z0-9][A-Za-z0-9._-]{0,159})", path)
        if m:
            rec = f.assets.get((m.group(1), m.group(2)))
            need(rec, 404, "Not found.")
            return self.json({k: v for k, v in rec.items() if k != "order"})
        if path == "/api/projects":
            return self.json([{"id": p["id"], "name": p["name"], "updated": p["updated"], "items": len(p["items"]),
                               "seconds": float(f.resolve(p)["seconds"])}
                              for p in sorted(f.projects.values(), key=lambda p: p["updated"], reverse=True)])
        m = re.fullmatch(r"/api/projects/(p_[0-9a-f]{12})", path)
        if m:
            p = f.projects.get(m.group(1))
            need(p, 404, "That cut is gone.")
            return self.json({"project": p, "resolved": f.resolve(p)})
        if path == "/api/jobs":
            return self.json([j for j in f.jobs.values() if j["state"] in ("queued", "running")])
        m = re.fullmatch(r"/api/jobs/(j_[0-9a-f]{12})", path)
        if m:
            j = f.jobs.get(m.group(1))
            need(j, 404, "No such job.")
            return self.json(j)
        m = re.fullmatch(r"/m/cache/([0-9a-f]{64})\.mp4", path)
        if m:
            need(m.group(1) in f.cache, 404, "Not found.")
            return self.media_bytes(f.cache[m.group(1)], "video/mp4", None)
        m = re.fullmatch(r"/(?:m/(out|gen|in|lab)|(out|ref))/([A-Za-z0-9][A-Za-z0-9._-]{0,159})", path)
        if m:
            root = m.group(1) or {"out": "out", "ref": "in"}[m.group(2)]
            got = f.media.get((root, m.group(3)))
            need(got, 404, "Not found.")
            return self.media_bytes(got[0], got[1], m.group(3) if "dl=1" in self.path else None)
        raise HTTPErr(404, "Not found.")

    def media_bytes(self, data: bytes, ctype: str, dl: str | None):
        extra = {"Accept-Ranges": "bytes"}
        if dl:
            extra["Content-Disposition"] = f'attachment; filename="{dl}"'
        rng = self.headers.get("Range")
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng or "")
        if m and data:
            a = int(m.group(1)) if m.group(1) else max(0, len(data) - int(m.group(2) or 0))
            b = int(m.group(2)) if m.group(1) and m.group(2) else len(data) - 1
            b = min(b, len(data) - 1)
            if a > b:
                return self.send(416, b"", ctype, {"Content-Range": f"bytes */{len(data)}"})
            extra["Content-Range"] = f"bytes {a}-{b}/{len(data)}"
            return self.send(206, data[a:b + 1], ctype, extra)
        self.send(200, data, ctype, extra)

    def config(self):
        return {"app": "YourContent", "version": "0.2.0", "auth": bool(self.fake.token), "consent_version": 1,
                "variants": {"int8": "int8 · balanced", "fp4": "NVFP4 · fastest", "bf16-lora": "bf16 + LoRA · best quality"},
                "default_variant": "int8", "aliases": {"old-int8": "int8"},
                "sizes": ["square", "portrait", "landscape", "wide", "tall"], "presets": PRESETS,
                "limits": {"image_mb": 30, "video_mb": 500, "audio_mb": 100},
                "licences": [{"model": "Qwen-Image 2.1", "licence": "qwen-image-nc", "commercial_ok": False},
                             {"model": "Wan 2.2", "licence": "apache-2.0", "commercial_ok": True},
                             {"model": "ACE-Step 1.5", "licence": "ace-step", "commercial_ok": True}]}

    def health(self):
        f = self.fake
        ok = f.comfy_ok
        return {"comfy": {"ok": ok, "version": "0.37.0"} if ok else {"ok": False, "version": None,
                                                                     "error": "ComfyUI not reachable"},
                "features": {"picture": {"ok": ok, "variants": {"int8": {"ok": ok, "missing": []},
                                                                "fp4": {"ok": ok, "missing": []},
                                                                "bf16-lora": {"ok": False, "missing": ["lora.safetensors"]}}},
                             "clip": {"ok": ok, "missing": []}, "extend": {"ok": ok},
                             "music": {"ok": ok and f.music_ok, "saver": "SaveAudio",
                                       "missing": [] if f.music_ok else ["qwen_0.6b_ace15.safetensors"]},
                             "export": {"ok": True, "missing_filters": []}}}

    def do_POST(self):
        f = self.fake
        path = self.path.partition("?")[0]
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            if self.headers.get("Transfer-Encoding"):
                self.record(None)
                raise HTTPErr(411, "Content-Length is required.")
            if self.headers.get("X-Media-Lab") != "1":
                self.record(None)
                raise HTTPErr(403, "Missing X-Media-Lab header.")
            if path == "/api/upload":
                self.record({"bytes": len(raw)})
                need(self.authed(), 401, "Sign in first.")
                with f.lock:
                    return self.json(self.upload(raw))
            need((self.headers.get("Content-Type") or "").startswith("application/json"), 415,
                 "Send JSON.")
            body = json.loads(raw or b"{}")
            self.record(body)
            if path == "/api/login":
                if f.token and hmac.compare_digest(str(body.get("token", "")), f.token):
                    want = hmac.new(f.token.encode(), b"media-lab-session", hashlib.sha256).hexdigest()
                    return self.send(204, b"", "text/plain",
                                     {"Set-Cookie": f"ml_session={want}; HttpOnly; SameSite=Strict; Path=/"})
                time.sleep(0.2)
                raise HTTPErr(401, "That token is not right.")
            if path == "/api/logout":
                return self.send(204, b"", "text/plain", {"Set-Cookie": "ml_session=; Max-Age=0; Path=/"})
            need(self.authed(), 401, "Sign in first.")
            with f.lock:
                return self.post_api(path, body)
        except HTTPErr as e:
            self.json(dict({"error": e.message}, **e.extra), e.code)
        except (ValueError, KeyError, TypeError) as e:
            self.json({"error": f"Bad request: {e}"}, 400)

    def ref_of(self, obj) -> tuple[str, str]:
        need(isinstance(obj, dict) and obj.get("root") in ("out", "gen", "in", "lab")
             and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}", str(obj.get("name", ""))), 400, "Bad ref.")
        key = (obj["root"], obj["name"])
        need(key in self.fake.assets, 404, "Not found.")
        return key

    def upload(self, raw: bytes):
        f = self.fake
        ctype = self.headers.get("Content-Type") or ""
        label = urllib.parse.unquote(self.headers.get("X-Filename") or "")[:60]
        need(raw, 400, "Empty file.")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        if ctype.startswith("image/"):
            ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(ctype)
            need(ext, 415, "Pictures must be PNG, JPEG or WebP.")
            name = f"ref_{stamp}_{hex_(6)}.{ext}"
            rec = f._add("in", name, "upload", raw, ctype, w=512, h=512, commercial_ok=None, licences=("upload",),
                         restricted_by=(f"upload {label}: your upload, rights not checked",), label=label)
            return {"ref": {"root": "in", "name": name}, "type": "image", "url": f"/m/in/{name}", "poster": None,
                    "probe": rec["probe"]}
        if ctype.startswith("video/"):
            name = f"up_{stamp}_{hex_(6)}.mp4"
            rec = f._add("lab", name, "upload", f.mp4 or raw, "video/mp4", w=208, h=208, frames=49, rate="16/1",
                         seconds=3.0625, poster=name[:-4] + ".jpg", commercial_ok=None, licences=("upload",),
                         restricted_by=(f"upload {label}: your upload, rights not checked",), label=label)
            return {"ref": {"root": "lab", "name": name}, "type": "video", "url": f"/m/lab/{name}",
                    "poster": f"/m/lab/{rec['poster']}", "seconds": 3.0625, "probe": rec["probe"]}
        if ctype.startswith("audio/"):
            name = f"up_{stamp}_{hex_(6)}.wav"
            rec = f._add("lab", name, "upload", silent_wav(8.0), "audio/wav", seconds=8.0, has_audio=True,
                         commercial_ok=None, licences=("upload",),
                         restricted_by=(f"upload {label}: your upload, rights not checked",), label=label)
            return {"ref": {"root": "lab", "name": name}, "type": "audio", "url": f"/m/lab/{name}", "poster": None,
                    "seconds": 8.0, "probe": rec["probe"]}
        raise HTTPErr(415, "That kind of file is not supported.")

    def project_of(self, pid) -> dict:
        p = self.fake.projects.get(pid)
        need(p, 404, "That cut is gone.")
        return p

    def post_api(self, path, body):
        f = self.fake
        if path == "/api/generate":
            need(str(body.get("prompt", "")).strip(), 400, "Write a prompt first.")
            need(body.get("variant") in ("int8", "fp4", "bf16-lora"), 400, "Unknown model.")
            for r in body.get("refs", []):
                self.ref_of(r)
            seed = body.get("seed") or random.randint(1, 2 ** 31)

            def done():
                name = f"img_{f.counter:05d}_.png"
                rec = f._add("gen", name, "image", png(160, 120, (200, 150, 90)), "image/png", w=1472, h=1104,
                             label=body["prompt"][:60], prompt=body["prompt"], seed=seed, commercial_ok=False,
                             licences=("qwen-image-nc",), restricted_by=("Qwen-Image 2.1 (non-commercial)",))
                return dict(f.item(rec), seed=seed, seconds=round(f.job_seconds, 1), size="1472×1104")
            return self.json({"job": f.submit("generate", "Picture", done, ("loading", "painting"))})
        if path in ("/api/animate", "/api/extend"):
            src = self.ref_of(body.get("start") if path == "/api/animate" else body.get("src"))
            need(path == "/api/extend" or f.item(f.assets[src])["type"] == "image", 400, "use a picture from Recent")

            def done():
                stem = f"clip_{f.counter:05d}" if path == "/api/animate" else f"ext_{time.strftime('%Y%m%d-%H%M%S')}_{hex_(6)}"
                rec = f._add("gen", stem + ".mp4", "clip" if path == "/api/animate" else "extension", f.mp4,
                             "video/mp4", w=832, h=480, frames=49, rate="16/1", seconds=3.0625, poster=stem + ".png",
                             label=str(body.get("prompt", ""))[:60], prompt=body.get("prompt"), seed=7,
                             commercial_ok=f.assets[src]["commercial_ok"], licences=tuple(f.assets[src]["licences"]),
                             restricted_by=tuple(f.assets[src]["restricted_by"]))
                return dict(f.item(rec), seconds=round(f.job_seconds, 1), size="832×480")
            kind = path.rsplit("/", 1)[1]
            return self.json({"job": f.submit(kind, kind.title(), done, ("loading", "sampling", "saving"))})
        if path == "/api/music":
            need(f.music_ok, 503, "Music needs files that are missing.")
            need(str(body.get("tags", "")).strip(), 400, "Describe the music style first.")
            secs = body.get("seconds")
            if secs is None:
                p = f.projects.get(body.get("project_id") or "")
                T = Fraction(f.resolve(p)["samples"], SR) if p else Fraction(10)
                secs = max(5.0, min(240.0, math.ceil(T * 25) / 25))
            seed = body.get("seed") or random.randint(1, 2 ** 31)

            def done():
                name = f"music_{f.counter:05d}.flac"
                params = {k: body.get(k) for k in ("tags", "lyrics", "bpm", "keyscale", "timesig", "language")}
                params["seconds"] = secs
                rec = f._add("gen", name, "music", silent_wav(float(secs)), "audio/wav", seconds=float(secs),
                             has_audio=True, commercial_ok=True, licences=("ace-step",),
                             label=str(body["tags"])[:60], prompt=body["tags"], seed=seed, params=params)
                t = [x for x in f.tracks() if x["ref"]["name"] == name][0]
                return dict(t, samples=int(secs * SR), seconds=float(secs))
            return self.json({"job": f.submit("music", "Music", done, ("loading", "composing"))})
        if path == "/api/delete":
            key = self.ref_of(body.get("ref"))
            rec = f.assets.pop(key)
            f.media.pop(key, None)
            removed = [key[1]]
            if rec.get("poster"):
                f.media.pop((key[0], rec["poster"]), None)
                removed.append(rec["poster"])
            used = [p["name"] for p in f.projects.values()
                    if any((i.get("src") or {}).get("name") == key[1] and (i.get("src") or {}).get("root") == key[0]
                           for i in p["items"])
                    or ((p.get("music") or {}).get("src") or {}) == {"root": key[0], "name": key[1]}]
            return self.json({"removed": removed, "history_deleted": len(rec["comfy"]["prompt_ids"]),
                              "used_in": used})
        if path == "/api/projects":
            pid = "p_" + hex_(12)
            name = re.sub(r"[\x00-\x1f\x7f]", "", str(body.get("name") or "Untitled cut"))[:80]
            p = {"schema": "media-lab/project@1", "id": pid, "name": name, "rev": 1, "created": now(),
                 "updated": now(), "items": [], "music": None,
                 "export": {"preset": "9:16", "fit": "auto", "fps": 24, "fade": 0.5}}
            f.projects[pid] = p
            return self.json({"project": p, "resolved": f.resolve(p)})
        m = re.fullmatch(r"/api/projects/(p_[0-9a-f]{12})(/delete|/preview|/export)?", path)
        if m:
            p = self.project_of(m.group(1))
            act = m.group(2)
            if act == "/delete":
                del f.projects[p["id"]]
                return self.json({"ok": True})
            if body.get("rev") != p["rev"]:
                raise HTTPErr(409, "This cut was changed somewhere else.",
                              {"current": {"project": p, "resolved": f.resolve(p)}})
            if act is None:
                newp = body.get("project")
                need(isinstance(newp, dict), 400, "Missing project.")
                need(len(newp.get("items", [])) <= 60, 400, "A cut can hold 60 items at most.")
                for it in newp.get("items", []):
                    need(it.get("kind") in ("clip", "still"), 400, "Bad item kind.")
                    need(re.fullmatch(r"i_[0-9a-f]{6}", str(it.get("id", ""))), 400, "Bad item id.")
                    need(it.get("join") in ("cut", "fade"), 400, "Bad join.")
                keep = {k: newp[k] for k in ("name", "items", "music", "export") if k in newp}
                keep["name"] = re.sub(r"[\x00-\x1f\x7f]", "", str(keep.get("name", p["name"])))[:80]
                p.update(copy.deepcopy(keep))
                p["rev"] += 1
                p["updated"] = now()
                f.snap_items(p)
                return self.json({"rev": p["rev"], "resolved": f.resolve(p)})
            res = f.resolve(p)
            need(res["frames"] > 0, 400, "The cut is empty.")
            if act == "/preview":
                pkey = hashlib.sha256(json.dumps(p, sort_keys=True).encode()).hexdigest()
                if pkey in f.cache:
                    jid = "j_" + hex_(12)
                    f.jobs[jid] = {"id": jid, "kind": "preview", "state": "done", "phase": "done", "progress": 1,
                                   "label": "Preview", "error": None,
                                   "result": {"url": f"/m/cache/{pkey}.mp4", "frames": res["frames"],
                                              "seconds": float(res["seconds"])}}
                    return self.json({"job": jid})

                def done_preview():
                    f.cache[pkey] = f.mp4
                    return {"url": f"/m/cache/{pkey}.mp4", "frames": res["frames"], "seconds": float(res["seconds"])}
                return self.json({"job": f.submit("preview", "Preview", done_preview, ("probe", "mix", "video", "verify"))})
            snap = copy.deepcopy(p)

            def done_export():
                stem = f"cut_{time.strftime('%Y%m%d-%H%M%S')}_{hex_(6)}"
                lic = res["licence"]
                rec = f._add("lab", stem + ".mp4", "export", f.mp4, "video/mp4", w=1080, h=1920, frames=res["frames"],
                             rate=f"{res['fps']}/1", seconds=float(res["seconds"]), has_audio=bool(snap.get("music")),
                             poster=stem + ".jpg", label=snap["name"], commercial_ok=lic["commercial_ok"],
                             licences=(lic["most_restrictive"],), restricted_by=tuple(lic["restricted_by"]))
                checks = {"frames_expected": res["frames"], "frames_actual": res["frames"],
                          "audio_seconds": float(res["seconds"]), "lufs": -14.0, "true_peak": -1.2, "ok": True}
                return dict(f.item(rec), checks=checks, commercial_ok=lic["commercial_ok"],
                            licences=[lic["most_restrictive"]], warnings=res["warnings"])
            return self.json({"job": f.submit("export", "Export", done_export,
                                              ("probe", "mix", "video", "verify", "finish"))})
        m = re.fullmatch(r"/api/jobs/(j_[0-9a-f]{12})/cancel", path)
        if m:
            j = f.jobs.get(m.group(1))
            need(j, 404, "No such job.")
            if j["state"] in ("queued", "running"):
                j["state"] = "cancelled"
            return self.json({"state": j["state"]})
        raise HTTPErr(404, "Not found.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Fake YourContent API for UI work")
    ap.add_argument("--port", type=int, default=18191)
    ap.add_argument("--job-seconds", type=float, default=1.5)
    ap.add_argument("--token")
    ap.add_argument("--comfy-off", action="store_true")
    ap.add_argument("--no-music", action="store_true")
    a = ap.parse_args(argv)
    srv = FakeUI(job_seconds=a.job_seconds, token=a.token, comfy_ok=not a.comfy_off, music_ok=not a.no_music)
    print("fake YourContent on", srv.start(a.port), flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
