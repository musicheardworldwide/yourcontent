#!/usr/bin/env python3
"""Post render service: draws a branded post (one of 35 styles) with a headless Chromium and returns the image.

Runs in its own container on YourContent's internal compose network; nothing publishes its port. Stateless: it keeps
no files, holds no secrets and makes no network requests (the page's own requests are answered from this folder,
everything else is aborted). Stdlib HTTP server; Playwright is imported only when the first render starts.

    GET  /healthz   {"ok": true, "styles": 35, "fonts": 29, "browser": false}
    GET  /styles    {"styles": [{id, name, kind, tags, slides, version}], "fonts": [family...], "formats": [...], ...}
    POST /render    JSON spec (below) -> the PNG or JPEG bytes; X-Render-Info: {"style", "version", "w", "h", ...}

Spec: {"style": "editorial",
       "brand": {"name", "colors": {"bg", "fg", "accent", "accent2"?, "muted"?, "paper"?, "ink"?},
                 "fonts": {"display", "body", "serif"?, "mono"?, "hand"?}, "handle"?, "logo"?: data URL},
       "text": {"headline" (required), "kicker", "body", "detail", "handle", "label"},
       "images": [data URL, ...] (0-4; the first is the main picture),
       "format": "1:1" | "4:5" | "9:16" | "1.91:1", "width": 320-2160, "type": "png" | "jpg",
       "palette": "brand" | "invert" | "own", "safe_area": true (9:16 only), "mark": true, "slide": 1}
Errors are JSON {"error": "..."}: 400 bad spec, 404, 405, 413 too large, 415 not JSON, 503 busy, 500 render failed.

Env: RENDER_HOST (default 127.0.0.1; the container sets 0.0.0.0), RENDER_PORT (8191), RENDER_FONT_DIR (./fonts).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import queue
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
STYLES_FILE = os.path.join(HERE, "styles", "styles.json")
FONTS_FILE = os.path.join(HERE, "fonts.json")
PAGE_ORIGIN = "http://render.invalid"     # never resolved: every request of the page is answered in-process
MAX_BODY = 64 << 20
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 16 << 20
QUEUE_MAX = 8
JOB_TIMEOUT = 90

FORMATS = {"1:1": "1/1", "4:5": "4/5", "9:16": "9/16", "1.91:1": "1.91/1"}
PALETTES = ("brand", "invert", "own")
TYPES = {"png": "image/png", "jpg": "image/jpeg"}
TEXT_LIMITS = {"headline": 300, "kicker": 120, "body": 600, "detail": 80, "handle": 80, "label": 60}
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
FAMILY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ]{0,47}$")
DATA_URL = re.compile(r"^data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/]*={0,2})$")
COLOR_KEYS = {"bg": "bg", "fg": "fg", "accent": "ac", "accent2": "ac2", "muted": "mut", "paper": "paper", "ink": "ink"}
GENERIC = {"Caveat": "cursive", "Permanent Marker": "cursive", "Monoton": "cursive",
           "JetBrains Mono": "monospace", "Geist Mono": "monospace",
           "Cormorant Garamond": "serif", "DM Serif Display": "serif", "Libre Caslon Text": "serif",
           "Old Standard TT": "serif", "Playfair Display": "serif", "Source Serif 4": "serif", "Limelight": "serif"}
FONT_DEFAULTS = {"display": "Space Grotesk", "body": "Inter", "mono": "JetBrains Mono"}


class BadRequest(Exception):
    def __init__(self, msg: str, code: int = 400):
        super().__init__(msg)
        self.code = code


class Busy(Exception):
    pass


# --------------------------------------------------------------------------------------------- data files

def load_styles(path: str = STYLES_FILE) -> dict[str, dict]:
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    return {s["id"]: s for s in doc["styles"]}


def load_fonts(path: str = FONTS_FILE) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


STYLES = load_styles()
FONTS = load_fonts()
FONT_FILES = {e["file"] for e in FONTS["fonts"]}
FAMILIES = sorted({e["family"] for e in FONTS["fonts"]})


def style_versions(styles: dict[str, dict] = STYLES) -> dict[str, str]:
    """{style id: 12 hex}: a hash of the style's own template, its styles.json entry and everything every post
    shares (engine.js, base.css, page.html, fonts.json). It changes exactly when that style can draw differently,
    so a client may cache a picture under it (the app's thumbnail cache does)."""
    shared = hashlib.sha256()
    for name in ("engine.js", "base.css", "page.html", "fonts.json"):
        with open(os.path.join(HERE, name), "rb") as f:
            shared.update(name.encode() + b"\0" + f.read() + b"\0")
    with open(os.path.join(HERE, "styles", "templates.js"), encoding="utf-8") as f:
        src = f.read()
    # each template starts on a line of its own: "id": c=>...
    starts = [(m.group(1), m.start()) for m in re.finditer(r'^"([a-z0-9-]+)":', src, re.M)]
    blocks = {sid: src[a:(starts[i + 1][1] if i + 1 < len(starts) else len(src))] for i, (sid, a) in enumerate(starts)}
    out = {}
    for sid, meta in styles.items():
        h = shared.copy()
        h.update(blocks.get(sid, "").encode() + b"\0" + json.dumps(meta, sort_keys=True).encode())
        out[sid] = h.hexdigest()[:12]
    return out


VERSIONS = style_versions()


def font_dir() -> str:
    return os.environ.get("RENDER_FONT_DIR") or os.path.join(HERE, "fonts")


def fonts_installed() -> int:
    d = font_dir()
    return sum(1 for f in FONT_FILES if os.path.isfile(os.path.join(d, f)))


def styles_doc() -> dict:
    return {"styles": [{"id": s["id"], "name": s["name"], "kind": s["kind"], "tags": s.get("tags", ""),
                        "slides": s.get("slides", 1), "version": VERSIONS.get(s["id"], "")} for s in STYLES.values()],
            "fonts": FAMILIES, "formats": list(FORMATS), "palettes": list(PALETTES), "types": list(TYPES),
            "max_images": MAX_IMAGES}


# --------------------------------------------------------------------------------------------- PURE: spec checks

def _mix(a: str, b: str, t: float) -> str:
    A = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    B = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(A, B))


def css_family(name: str) -> str:
    """'Family',generic. The name is checked first, so it can't close the quote or the CSS declaration."""
    if not isinstance(name, str) or not FAMILY.fullmatch(name):
        raise BadRequest("a font name may use letters, digits and spaces only (48 at most)")
    return f"'{name}',{GENERIC.get(name, 'sans-serif')}"


def brand_tokens(brand: object) -> tuple[dict, str, str]:
    """(tokens for engine.js, wordmark text, handle) from a brand kit. Raises BadRequest."""
    if not isinstance(brand, dict):
        raise BadRequest("brand must be an object")
    colors = brand.get("colors")
    if not isinstance(colors, dict):
        raise BadRequest("brand.colors must be an object")
    c = {}
    for key, tok in COLOR_KEYS.items():
        v = colors.get(key)
        if v in (None, ""):
            if key in ("bg", "fg", "accent"):
                raise BadRequest(f"brand.colors.{key} is required (#rrggbb)")
            continue
        if not isinstance(v, str) or not HEX.fullmatch(v):
            raise BadRequest(f"brand.colors.{key} must be #rrggbb")
        c[tok] = v.lower()
    c.setdefault("ac2", c["ac"])
    c.setdefault("mut", _mix(c["fg"], c["bg"], .45))
    c.setdefault("paper", c["bg"])
    c.setdefault("ink", c["fg"])
    fonts = brand.get("fonts") or {}
    if not isinstance(fonts, dict):
        raise BadRequest("brand.fonts must be an object")
    f = {k: fonts.get(k) or FONT_DEFAULTS.get(k) for k in ("display", "body", "serif", "mono", "hand")}
    f["serif"] = f["serif"] or f["display"]
    f["hand"] = f["hand"] or f["display"]
    c.update({"disp": css_family(f["display"]), "sans": css_family(f["body"]), "serif": css_family(f["serif"]),
              "mono": css_family(f["mono"]), "hand": css_family(f["hand"])})
    name = brand.get("name") or ""
    handle = brand.get("handle") or ""
    if not isinstance(name, str) or len(name) > 60 or not isinstance(handle, str) or len(handle) > 80:
        raise BadRequest("brand.name is 60 characters at most, brand.handle 80")
    return c, name, handle


def sniff(data: bytes) -> str | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def check_image(s: object, what: str = "image") -> str:
    """A data: URL of a PNG, JPEG or WebP whose bytes match its declared type. Raises BadRequest."""
    if not isinstance(s, str):
        raise BadRequest(f"{what} must be a data: URL")
    m = DATA_URL.fullmatch(s)
    if not m:
        raise BadRequest(f"{what} must be a base64 data: URL of a PNG, JPEG or WebP")
    if len(m.group(2)) * 3 // 4 > MAX_IMAGE_BYTES:
        raise BadRequest(f"{what} is too large ({MAX_IMAGE_BYTES >> 20} MB at most)", 413)
    try:
        raw = base64.b64decode(m.group(2), validate=True)
    except (binascii.Error, ValueError):
        raise BadRequest(f"{what} is not valid base64") from None
    if sniff(raw[:16]) != m.group(1):
        raise BadRequest(f"{what}: the bytes are not a {m.group(1).split('/')[1].upper()}")
    return s


def _text(text: dict, key: str) -> str:
    v = text.get(key, "")
    if v is None:
        v = ""
    if not isinstance(v, str):
        raise BadRequest(f"text.{key} must be text")
    if len(v) > TEXT_LIMITS[key]:
        raise BadRequest(f"text.{key} is too long ({TEXT_LIMITS[key]} characters at most)")
    return v.replace("\r\n", "\n").strip()


def _int(body: dict, key: str, lo: int, hi: int, default: int) -> int:
    v = body.get(key, default)
    if v is None:
        return default
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise BadRequest(f"{key} must be a whole number")
    try:
        n = int(v)
    except ValueError:
        raise BadRequest(f"{key} must be a whole number") from None
    if not lo <= n <= hi:
        raise BadRequest(f"{key} must be {lo}-{hi}")
    return n


def build_spec(body: object) -> dict:
    """Request JSON -> what engine.js renderPost() takes (plus the output type). Raises BadRequest."""
    if not isinstance(body, dict):
        raise BadRequest("send a JSON object")
    sid = body.get("style")
    if sid not in STYLES:
        raise BadRequest("unknown style; GET /styles lists them")
    meta = STYLES[sid]
    t, name, kit_handle = brand_tokens(body.get("brand"))
    text = body.get("text") or {}
    if not isinstance(text, dict):
        raise BadRequest("text must be an object")
    copy = {k: _text(text, k) for k in ("kicker", "headline", "body", "detail", "handle")}
    label = _text(text, "label")
    if not copy["headline"]:
        raise BadRequest("text.headline is required")
    copy["handle"] = copy["handle"] or kit_handle
    images = body.get("images") or []
    if not isinstance(images, list) or len(images) > MAX_IMAGES:
        raise BadRequest(f"images must be a list of at most {MAX_IMAGES}")
    images = [check_image(x, f"images[{i}]") for i, x in enumerate(images)]
    logo = body.get("brand", {}).get("logo")
    logo = check_image(logo, "brand.logo") if logo else None
    fmt = body.get("format") or "4:5"
    if fmt not in FORMATS:
        raise BadRequest("format must be one of " + ", ".join(FORMATS))
    ar = FORMATS[fmt]
    width = _int(body, "width", 320, 2160, 1200 if fmt == "1.91:1" else 1080)
    typ = body.get("type") or "png"
    if typ not in TYPES:
        raise BadRequest("type must be png or jpg")
    palette = body.get("palette") or "brand"
    if palette not in PALETTES:
        raise BadRequest("palette must be brand, invert or own")
    slides = int(meta.get("slides", 1))
    slide = _int(body, "slide", 1, slides, 1)
    return {"meta": meta, "t": t, "palette": palette, "ar": ar, "safe": body.get("safe_area", True) is not False,
            "mark": body.get("mark", True) is not False, "markText": name, "logo": logo, "name": name,
            "label": label, "images": images, "slide": slide - 1, "width": width,
            "copy": {"kicker": copy["kicker"], "head": copy["headline"], "body": copy["body"],
                     "detail": copy["detail"], "handle": copy["handle"]},
            "type": typ, "format": fmt}


# --------------------------------------------------------------------------------------------- the browser

ASSETS = {"/page.html": ("page.html", "text/html; charset=utf-8"),
          "/engine.js": ("engine.js", "text/javascript; charset=utf-8"),
          "/base.css": ("base.css", "text/css; charset=utf-8"),
          "/styles/templates.js": (os.path.join("styles", "templates.js"), "text/javascript; charset=utf-8")}


def asset(path: str) -> tuple[bytes, str] | None:
    """The bytes the render page may load, by URL path; None for anything else (the request is aborted)."""
    if path in ASSETS:
        rel, ctype = ASSETS[path]
        with open(os.path.join(HERE, rel), "rb") as f:
            return f.read(), ctype
    if path == "/fonts.css":
        p = os.path.join(font_dir(), "fonts.css")
        return (open(p, "rb").read() if os.path.isfile(p) else b""), "text/css; charset=utf-8"
    if path.startswith("/fonts/") and path[7:] in FONT_FILES:
        p = os.path.join(font_dir(), path[7:])
        if os.path.isfile(p):
            with open(p, "rb") as f:
                return f.read(), "font/ttf"
    return None


class Renderer:
    """One warm Chromium on one worker thread (Playwright's sync API is not thread-safe); jobs queue up to 8."""

    def __init__(self):
        self.jobs: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)
        self.thread: threading.Thread | None = None
        self.lock = threading.Lock()
        self.started = False

    def _ensure(self) -> None:
        with self.lock:
            if not self.thread or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._run, name="render", daemon=True)
                self.thread.start()

    def render(self, spec: dict) -> tuple[bytes, dict]:
        """(image bytes, meta). Raises Busy when the queue is full, RuntimeError when the render fails."""
        self._ensure()
        done, box = threading.Event(), {}
        try:
            self.jobs.put_nowait((spec, box, done))
        except queue.Full:
            raise Busy("the render queue is full; try again shortly") from None
        if not done.wait(JOB_TIMEOUT):
            raise RuntimeError("the render timed out")
        if "error" in box:
            raise RuntimeError(box["error"])
        return box["img"], box["meta"]

    @staticmethod
    def _route(route) -> None:
        u = urlsplit(route.request.url)
        hit = asset(u.path) if f"{u.scheme}://{u.netloc}" == PAGE_ORIGIN else None
        if hit is None:
            route.abort()
        else:
            route.fulfill(status=200, body=hit[0], content_type=hit[1])

    def _open(self, browser):
        ctx = browser.new_context(viewport={"width": 2200, "height": 2400}, device_scale_factor=1,
                                  service_workers="block")
        ctx.route("**/*", self._route)
        page = ctx.new_page()
        page.goto(PAGE_ORIGIN + "/page.html", wait_until="load")
        page.wait_for_function("window.engineReady === true && !!window.TEMPLATES", timeout=30000)
        return ctx, page

    def _run(self) -> None:
        from playwright.sync_api import sync_playwright  # only here: the HTTP side runs without Playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--disable-dev-shm-usage", "--font-render-hinting=none"])
            ctx, page = self._open(browser)
            self.started = True
            while True:
                spec, box, done = self.jobs.get()
                try:
                    js = {k: v for k, v in spec.items() if k not in ("type", "format")}
                    meta = page.evaluate("spec => window.renderPost(spec)", js)
                    if meta.get("error"):
                        box["error"] = meta["error"]
                    else:
                        el = page.locator("#root > .post")
                        kw = {"type": "jpeg", "quality": 90} if spec["type"] == "jpg" else {"type": "png"}
                        box["img"] = el.screenshot(animations="disabled", timeout=30000, **kw)
                        box["meta"] = meta
                except Exception as exc:  # noqa: BLE001 - a crashed or wedged page: report it, start a fresh one
                    box["error"] = f"render failed: {exc}".splitlines()[0][:300]
                    try:
                        ctx.close()
                    except Exception:  # noqa: BLE001
                        pass
                    ctx, page = self._open(browser)
                finally:
                    done.set()


RENDERER = Renderer()


# --------------------------------------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "media-lab-render"
    sys_version = ""
    timeout = 120

    def log_message(self, fmt, *args):  # quiet; one line per render is printed by do_POST
        pass

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self) -> None:
        path = self.path.partition("?")[0]
        if path == "/healthz":
            return self._json(200, {"ok": True, "styles": len(STYLES), "fonts": fonts_installed(),
                                    "browser": RENDERER.started})
        if path == "/styles":
            return self._json(200, styles_doc())
        if path == "/render":
            return self._json(405, {"error": "POST a JSON spec"})
        return self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path.partition("?")[0] != "/render":
            return self._json(404, {"error": "not found"})
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            return self._json(415, {"error": "send JSON (Content-Type: application/json)"})
        raw_len = self.headers.get("Content-Length") or ""
        if not raw_len.isdigit() or not 0 < int(raw_len) <= MAX_BODY:
            self.close_connection = True
            return self._json(413, {"error": f"send a Content-Length of 1 byte to {MAX_BODY >> 20} MB"})
        t0 = time.monotonic()
        try:
            try:
                body = json.loads(self.rfile.read(int(raw_len)))
            except ValueError:
                raise BadRequest("that isn't valid JSON") from None
            spec = build_spec(body)
            img, meta = RENDERER.render(spec)
        except BadRequest as exc:
            return self._json(exc.code, {"error": str(exc)})
        except Busy as exc:
            return self._json(503, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            print(f"render: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            return self._json(500, {"error": str(exc)[:300] or "render failed"})
        info = {"style": spec["meta"]["id"], "version": meta.get("ver"), "w": meta.get("w"), "h": meta.get("h"),
                "format": spec["format"], "palette": spec["palette"], "type": spec["type"],
                "slide": spec["slide"] + 1}
        print(f"render {info['style']} {info['format']} {info['w']}x{info['h']} {len(img)} B "
              f"{(time.monotonic() - t0) * 1000:.0f} ms", flush=True)
        self._send(200, img, TYPES[spec["type"]], {"X-Render-Info": json.dumps(info)})

    def _no(self) -> None:
        self._json(405, {"error": "method not allowed"})

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _no


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    host = os.environ.get("RENDER_HOST") or "127.0.0.1"
    port = int(os.environ.get("RENDER_PORT") or 8191)
    srv = Server((host, port), Handler)
    print(f"render: {len(STYLES)} styles, {fonts_installed()}/{len(FONT_FILES)} font files, "
          f"listening on {host}:{srv.server_address[1]}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
