"""HTTP handler, routes, guard, static files, Range sender, error mapping (§6, §12). Owner: WP2.

Guard order for every request: method (GET/POST only, else 405) -> Host allowlist (403) -> token (401) ->
for POST: X-Media-Lab: 1 and Origin == Host (403) -> body rules (JSON type 415, sizes 413, no chunked 400).
The server never answers OPTIONS and never sends an Access-Control-* header.

Error mapping: HTTPError(code) as given; any exception carrying an int `status` (BadRef 400, NotOwned 404,
Conflict 409, QueueFull 429, projects.TooLarge 413, or a WP3 error with status 415/503) uses it; other
ValueError 400; NotImplementedError 501; anything else 500 with a generic sentence. Messages are scrubbed of
absolute paths.

Other modules may expose `configure(cfg)`; make_server() calls it for comfy, media, export, actions, models,
features, legacy and provenance when present, after store.configure(cfg).
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import sys
import threading
import time
import traceback
import urllib.parse
from collections import deque
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import VERSION, agents, config as config_mod, jobs, projects, store
from .types import BadRef, Conflict, NotOwned


class HTTPError(Exception):
    """Raised by guard() and route code; the handler answers {"error": message} with `code`."""

    def __init__(self, code: int, message: str, extra: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra or {}


STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
# Fixed dict: nothing else under static/ is ever served.
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
# The UI's own fonts (OFL-1.1), copied at image build time from the render service's pinned set (render/fonts.json);
# never committed. A missing file is a 404 and the page falls back to the system font.
FONT_FILES = ("Inter_opsz_wght.ttf", "SpaceGrotesk_wght.ttf", "JetBrainsMono_wght.ttf")
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; "
       "media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
SECURITY_HEADERS = {"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff",
                    "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY"}
CTYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp",
          "mp4": "video/mp4", "mov": "video/quicktime", "webm": "video/webm", "mp3": "audio/mpeg",
          "wav": "audio/wav", "m4a": "audio/mp4", "flac": "audio/flac", "ogg": "audio/ogg", "opus": "audio/ogg",
          "aac": "audio/aac"}

MAX_JSON = 1 << 20          # 1 MiB
MAX_PROMPT = 4000
MAX_TAGS = 1000
MAX_REFS = 6
COOKIE = "ml_session"
SESSION_MSG = b"media-lab-session"
FAIL_DELAY = 1.0            # seconds; a wrong token waits this long
FAIL_LIMIT = 10             # failures per minute per IP
HEALTH_TTL = 30.0
PUBLIC = {("GET", "/"), ("GET", "/static/app.js"), ("GET", "/static/style.css"), ("GET", "/static/tokens.css"),
          ("GET", "/static/theme.js"), ("GET", "/static/icons.svg"), ("GET", "/static/favicon.svg"),
          ("GET", "/api/config"), ("GET", "/login"), ("GET", "/static/login.js"), ("GET", "/static/login.css"),
          ("POST", "/api/login"), ("POST", "/api/logout")}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
PROXY_RE = re.compile(r"^[0-9a-f]{64}$")
CHUNK = 256 * 1024


# --------------------------------------------------------------------------------------------- app state

class State:
    """Per-server state: config, model table, health cache, login failures."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.models = None
        self.models_error: str | None = None
        self.health: tuple[float, dict] | None = None
        self.ffcaps: dict | None = None
        self.fails: dict[str, deque] = {}
        self.session = (hmac.new(cfg.token.encode(), SESSION_MSG, hashlib.sha256).hexdigest()
                        if cfg.token else None)

    def load_models(self, force: bool = False):
        with self.lock:
            if self.models is not None and not force:
                return self.models
        try:
            from . import models
            m = models.load(self.cfg)
            err = None
        except Exception as exc:  # noqa: BLE001 - /api/config still answers; health names the problem
            m, err = None, store.scrub(str(exc) or exc.__class__.__name__)
        with self.lock:
            self.models, self.models_error = m, err
        return m


# --------------------------------------------------------------------------------------------- guard

def _host_ok(cfg, host: str) -> bool:
    host = (host or "").strip().lower()
    if not host or len(host) > 255:
        return False
    if host.startswith("["):
        name = host[: host.find("]") + 1] if "]" in host else host
        port = host[len(name) + 1:] if host[len(name):].startswith(":") else ""
    else:
        name, _, port = host.partition(":")
    if port and not port.isdigit():
        return False
    if name in LOCAL_HOSTS:
        return True
    return host in cfg.allowed_hosts or name in cfg.allowed_hosts


def _client_ip(h) -> str:
    """The socket peer; behind a YOURCONTENT_TRUSTED_PROXY, the address that proxy appended to X-Forwarded-For
    (the last entry: anything to its left was sent by the client and is not trusted)."""
    peer = h.client_address[0] if h.client_address else "?"
    cfg = getattr(getattr(h.server, "state", None), "cfg", None)
    if cfg is not None and peer in getattr(cfg, "trusted_proxies", ()):
        fwd = [p.strip() for p in (h.headers.get("X-Forwarded-For") or "").split(",") if p.strip()]
        if fwd:
            try:
                return str(ipaddress.ip_address(fwd[-1]))
            except ValueError:
                pass
    return peer


def _prune_fails(state: State, ip: str) -> deque:
    """The ip's failures in the last minute (call with state.lock held)."""
    now = time.monotonic()
    q = state.fails.setdefault(ip, deque())
    while q and now - q[0] > 60:
        q.popleft()
    return q


def _check_credential(state: State, ip: str, ok: bool) -> None:
    """Rate limit every credential check, right or wrong (§12.3). Raises HTTPError 429/401.

    While an address is limited, every credential gets an instant 429, the right one too, so a guess earns no
    verdict. Otherwise a wrong one takes its failure slot atomically (check and append under one lock, so
    parallel guesses cannot all slip under the limit), then waits FAIL_DELAY and gets 401."""
    with state.lock:
        q = _prune_fails(state, ip)
        if len(q) >= FAIL_LIMIT:
            raise HTTPError(429, "too many wrong tokens; wait a minute")
        if ok:
            return
        q.append(time.monotonic())
    time.sleep(FAIL_DELAY)
    raise HTTPError(401, "wrong token")


def _cookie(h, name: str) -> str | None:
    raw = h.headers.get("Cookie") or ""
    for part in raw.split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v.strip()
    return None


def _authed(h, state: State) -> bool | None:
    """True = good credential, False = a wrong one was presented, None = none presented."""
    token = state.cfg.token
    h.agent = None
    auth = h.headers.get("Authorization") or ""
    if auth:
        scheme, _, val = auth.partition(" ")
        if scheme.lower() == "bearer" and val:
            val = val.strip()
            if val.startswith(agents.PREFIX):  # a scoped agent token (agents.py), never the owner's
                h.agent = agents.check(val)
                return h.agent is not None
            return hmac.compare_digest(val.encode(), token.encode())
        return False
    c = _cookie(h, COOKIE)
    if c is not None:
        return hmac.compare_digest(c.encode(), state.session.encode())
    return None


def guard(h: BaseHTTPRequestHandler, method: str) -> None:
    """Host allowlist, token, CSRF header, Origin, limits. Raises HTTPError."""
    state: State = h.server.state  # type: ignore[attr-defined]
    cfg = state.cfg
    if method not in ("GET", "POST"):
        raise HTTPError(405, "method not allowed")
    host = h.headers.get("Host") or ""
    if not _host_ok(cfg, host):
        raise HTTPError(403, "this address isn't allowed; add it to YOURCONTENT_ALLOWED_HOSTS")
    path = h.path.partition("?")[0]
    if method == "POST":
        ctype = (h.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        # MCP clients cannot add the header; /mcp takes only JSON, which no cross-site form can send (mcp_routes)
        if h.headers.get("X-Media-Lab") != "1" and not (path == "/mcp" and ctype == "application/json"):
            raise HTTPError(403, "missing the X-Media-Lab: 1 header")
        origin = h.headers.get("Origin")
        if origin is not None:
            o = urllib.parse.urlsplit(origin)
            if o.scheme not in ("http", "https") or o.netloc.lower() != host.strip().lower():
                raise HTTPError(403, "requests from other sites aren't allowed")
        if (h.headers.get("Transfer-Encoding") or "").strip():
            raise HTTPError(400, "send a Content-Length; chunked bodies aren't supported")
    if (cfg.token and (method, path) not in PUBLIC and not path.startswith("/static/")
            and not any(m == method and rx.match(path) for m, rx in PUBLIC_RX)):
        ok = _authed(h, state)
        if ok is None:
            raise HTTPError(401, "log in first")
        if ok and not h.headers.get("Authorization"):
            # a valid session cookie (HMAC of the token, 256 bits) cannot be guessed, so it is never limited:
            # behind NAT or Docker Desktop's port forwarder everyone shares one address, and a guesser there
            # must not lock the owner out (review r2). Bearer tokens and /api/login stay limited.
            return
        _check_credential(state, _client_ip(h), ok)
        if h.agent is not None and not agents.allowed(method, path):
            raise HTTPError(403, "agent tokens can't do that; ask the owner")


# --------------------------------------------------------------------------------------------- sending

def _parse_range(header: str, size: int) -> tuple[int, int] | None | bool:
    """(start, end inclusive), None when no usable Range (send 200), False when unsatisfiable (416)."""
    m = re.fullmatch(r"\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*", header or "")
    if not m or (not m.group(1) and not m.group(2)):
        return None  # absent, malformed or multi-range: whole file
    if m.group(1):
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else size - 1
        if start >= size or end < start:
            return False
        return start, min(end, size - 1)
    n = int(m.group(2))
    if n == 0 or size == 0:
        return False
    return max(0, size - n), size - 1


def send_file(h: BaseHTTPRequestHandler, path: str, ctype: str, range_header: str | None,
              download_name: str | None) -> None:
    try:
        f = open(path, "rb")
    except OSError:
        raise HTTPError(404, "not found") from None
    with f:
        size = os.fstat(f.fileno()).st_size
        rng = _parse_range(range_header, size) if range_header else None
        headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-cache"}
        if download_name:
            headers["Content-Disposition"] = f'attachment; filename="{download_name}"'
        if rng is False:
            headers["Content-Range"] = f"bytes */{size}"
            _send_raw(h, 416, json.dumps({"error": "that range isn't in the file"}).encode(),
                      "application/json", headers)
            return
        if rng:
            start, end = rng
            code = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        else:
            start, end, code = 0, size - 1, 200
        length = max(0, end - start + 1)
        h.send_response(code)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(length))
        for k, v in {**SECURITY_HEADERS, **headers}.items():
            h.send_header(k, v)
        h.end_headers()
        f.seek(start)
        left = length
        try:
            while left > 0:
                chunk = f.read(min(CHUNK, left))
                if not chunk:
                    break
                h.wfile.write(chunk)
                left -= len(chunk)
        except OSError:  # client went away or stalled past the socket timeout
            h.close_connection = True


def _send_raw(h, code: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
    h.send_response(code)
    h.send_header("Content-Type", ctype)
    h.send_header("Content-Length", str(len(body)))
    for k, v in {**SECURITY_HEADERS, **(headers or {})}.items():
        h.send_header(k, v)
    h.end_headers()
    try:
        h.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        h.close_connection = True


def _json(h, code: int, obj, headers: dict | None = None) -> None:
    if code == 204:
        h.send_response(204)
        for k, v in {**SECURITY_HEADERS, **(headers or {})}.items():
            h.send_header(k, v)
        h.end_headers()
        return
    _send_raw(h, code, json.dumps(obj, ensure_ascii=False, default=_jsonable).encode("utf-8"),
              "application/json; charset=utf-8", {"Cache-Control": "no-store", **(headers or {})})


def _jsonable(o):
    if isinstance(o, Fraction):
        return f"{o.numerator}/{o.denominator}"
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    raise TypeError(f"not JSON: {type(o).__name__}")


# --------------------------------------------------------------------------------------------- request helpers

def _query(h) -> dict[str, str]:
    q = urllib.parse.parse_qs(h.path.partition("?")[2], keep_blank_values=True)
    return {k: v[-1] for k, v in q.items()}


def _content_length(h) -> int:
    raw = h.headers.get("Content-Length")
    if raw is None or not raw.strip().isdigit():
        raise HTTPError(400, "send a Content-Length")
    return int(raw)


def read_json(h) -> dict:
    ctype = (h.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    n = _content_length(h) if h.headers.get("Content-Length") is not None else 0
    if n > MAX_JSON:
        raise HTTPError(413, "request too large (1 MB at most)")
    if n == 0:
        return {}
    if ctype != "application/json":
        raise HTTPError(415, "send JSON (Content-Type: application/json)")
    raw = h.rfile.read(n)
    h._body_read = True
    try:
        body = json.loads(raw)
    except ValueError:
        raise HTTPError(400, "that isn't valid JSON") from None
    if not isinstance(body, dict):
        raise HTTPError(400, "send a JSON object")
    return body


def _text(body: dict, key: str, limit: int, default: str = "") -> str:
    v = body.get(key, default)
    if v is None:
        v = default
    if not isinstance(v, str):
        raise HTTPError(400, f"{key} must be text")
    if len(v) > limit:
        raise HTTPError(400, f"{key} is too long ({limit} characters at most)")
    return v


def _num(body: dict, key: str, lo: float, hi: float, default=None, integer: bool = False):
    v = body.get(key, default)
    if v is None or v == "":
        return default
    try:
        n = int(v) if integer else float(v)
    except (TypeError, ValueError):
        raise HTTPError(400, f"{key} must be a number") from None
    if isinstance(v, bool) or not lo <= n <= hi:
        raise HTTPError(400, f"{key} must be between {lo:g} and {hi:g}")
    return n


def _ctype_for(name: str) -> str:
    return CTYPES.get(name.rsplit(".", 1)[-1].lower(), "application/octet-stream")


# --------------------------------------------------------------------------------------------- project resolve

def _probe(ref):
    """media.probe of an owned, present file, else None (rendered as missing)."""
    from . import media
    if not store.owned(ref):
        return None
    try:
        path = store.resolve(ref)
        if not os.path.isfile(path):
            return None
        return media.probe(path)
    except NotImplementedError:
        raise
    except Exception:  # noqa: BLE001 - unreadable media renders as missing
        return None


def _frac(x) -> str:
    f = Fraction(x) if not isinstance(x, Fraction) else x
    return f"{f.numerator}/{f.denominator}"


def refresh_snaps(project: dict, probes: dict) -> None:
    """Rewrite each item's `snap` from its probe and sidecar (§5.3); a missing source keeps its old snap."""
    for it in project.get("items") or []:
        if not isinstance(it, dict):
            continue
        try:
            ref = store.check_ref(it.get("src"))
        except BadRef:
            continue
        p = probes.get(ref)
        if p is None:
            continue
        sc = store.sidecar(ref) or {}
        snap = {"w": p.w, "h": p.h, "sar": _frac(p.sar or 1), "commercial_ok": sc.get("commercial_ok")}
        if it.get("kind") == "clip":
            snap = {"frames": p.frames, "rate": _frac(p.rate or 1), **snap}
        it["snap"] = snap


def licence_summary(project: dict) -> dict:
    from . import provenance
    inputs, uploads = [], 0
    for it in project.get("items") or []:
        if not isinstance(it, dict):
            continue
        try:
            ref = store.check_ref(it.get("src"))
        except BadRef:
            continue
        sc = store.sidecar(ref)
        snap = it.get("snap") if isinstance(it.get("snap"), dict) else {}
        inputs.append(provenance.input_entry("item", ref, sc, snap_ok=snap.get("commercial_ok")))
        uploads += bool(sc and sc.get("kind") == "upload")
    m = project.get("music")
    if isinstance(m, dict):
        try:
            ref = store.check_ref(m.get("src"))
            sc = store.sidecar(ref)
            inputs.append(provenance.input_entry("music", ref, sc))
            uploads += bool(sc and sc.get("kind") == "upload")
        except BadRef:
            pass
    agg = provenance.aggregate([], inputs)
    return {"commercial_ok": agg.get("commercial_ok"), "most_restrictive": agg.get("most_restrictive"),
            "restricted_by": agg.get("restricted_by", []), "uploads": uploads,
            # the same lines the export will report, so the row and the job notes cannot drift apart
            "warnings": provenance.warnings(agg, uploads)}


def probes_for(project: dict) -> dict:
    return {ref: _probe(ref) for ref in projects.refs_of(project)}


def resolve_project(project: dict, probes: dict | None = None) -> tuple[dict, object]:
    """(resolved, plan): timeline.plan_json(plan) plus the licence block (§6)."""
    from . import timeline
    probes = probes_for(project) if probes is None else probes
    plan = timeline.plan(project, probes)
    out = dict(timeline.plan_json(plan))
    posters = item_posters(project)
    out["items"] = [dict(r, poster=posters.get(r.get("id"))) if isinstance(r, dict) else r
                    for r in out.get("items") or []]
    out["licence"] = licence_summary(project)
    return out, plan


def item_posters(project: dict) -> dict:
    """{item id: poster URL or None} for the strip: a still is its own poster; a video uses its sidecar's."""
    out = {}
    for it in project.get("items") or []:
        if not isinstance(it, dict) or not isinstance(it.get("id"), str):
            continue
        url = None
        try:
            ref = store.check_ref(it.get("src"))
            sc = store.sidecar(ref)
            if sc and store.media_type(ref[1]) == "image":
                url = store.url_for(ref)
            elif sc and isinstance(sc.get("poster"), str) and store.NAME_RE.fullmatch(sc["poster"]):
                url = store.url_for((ref[0], sc["poster"]))
        except (BadRef, OSError, ValueError):
            url = None
        out[it["id"]] = url
    return out


def keep_snaps(project: dict, stored: dict) -> None:
    """An item sent without a snap keeps the snap the stored project has for the same item id, so a source
    that went missing between saves still renders at its recorded length with its licence (§5.3)."""
    old = {it.get("id"): it.get("snap") for it in stored.get("items") or [] if isinstance(it, dict)}
    for it in project.get("items") or []:
        if isinstance(it, dict) and not isinstance(it.get("snap"), dict) and isinstance(old.get(it.get("id")), dict):
            it["snap"] = old[it.get("id")]


def _seconds(resolved: dict) -> float:
    try:
        return float(resolved.get("seconds") or 0)
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------------------------- routes

def _signed_in(h) -> bool:
    """For page loads only: does this browser carry a good session cookie? (The guard does the real checks.)"""
    state: State = h.server.state
    if not state.cfg.token:
        return True
    c = _cookie(h, COOKIE)
    return c is not None and hmac.compare_digest(c.encode(), state.session.encode())


def r_index(h, m):
    path = h.path.partition("?")[0]
    # the app page sends a signed-out browser to the sign-in page, and the sign-in page sends a signed-in one back
    if path == "/" and not _signed_in(h):
        return _send_raw(h, 302, b"", "text/plain; charset=utf-8", {"Location": "/login", "Cache-Control": "no-store"})
    if path == "/login" and h.server.state.cfg.token and _signed_in(h):
        return _send_raw(h, 302, b"", "text/plain; charset=utf-8", {"Location": "/", "Cache-Control": "no-store"})
    fname, ctype = STATIC[path]
    try:
        with open(os.path.join(STATIC_DIR, fname), "rb") as f:
            body = f.read()
    except OSError:
        raise HTTPError(404, "not found") from None
    if fname == "index.html":  # feature modules' files, in install order (install_extension)
        css = "".join(f'<link rel="stylesheet" href="{u}">' for u in EXT_STATIC if u.endswith(".css"))
        js = "".join(f'<script src="{u}"></script>' for u in EXT_STATIC if u.endswith(".js"))
        body = body.replace(b"<!--ext-css-->", css.encode()).replace(b"<!--ext-js-->", js.encode())
    _send_raw(h, 200, body, ctype, {"Cache-Control": "no-cache"})


def r_font(h, m):
    if m.group(1) not in FONT_FILES:
        raise HTTPError(404, "not found")
    try:
        with open(os.path.join(STATIC_DIR, "fonts", m.group(1)), "rb") as f:
            body = f.read()
    except OSError:
        raise HTTPError(404, "not found") from None
    _send_raw(h, 200, body, "font/ttf", {"Cache-Control": "max-age=604800"})


def r_login(h, m):
    state: State = h.server.state
    body = read_json(h)
    if not state.cfg.token:
        return _json(h, 204, None)
    tok = body.get("token")
    good = isinstance(tok, str) and hmac.compare_digest(tok.encode(), state.cfg.token.encode())
    _check_credential(state, _client_ip(h), good)
    secure = "; Secure" if (h.headers.get("X-Forwarded-Proto") or "").lower() == "https" else ""
    _json(h, 204, None, {"Set-Cookie": f"{COOKIE}={state.session}; HttpOnly; SameSite=Strict; Path=/{secure}"})


def r_logout(h, m):
    read_json(h)
    _json(h, 204, None, {"Set-Cookie": f"{COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"})


def r_config(h, m):
    state: State = h.server.state
    cfg = state.cfg
    from . import graphs
    models = state.load_models()
    variants, default, aliases, lic, info = {}, None, {}, [], {}
    if models is not None:
        try:
            variants = {vid: v.get("label", vid) for vid, v in models.image_variants().items()}
            for vid, v in models.image_variants().items():
                L = models.licence(v.get("licence")) if callable(getattr(models, "licence", None)) else {}
                info[vid] = {"model": v.get("model") or "Qwen-Image 2.1", "refs": graphs.max_refs(v),
                             "steps": v.get("steps") or 30, "commercial_ok": L.get("commercial_ok")}
            default = models.default_variant
            data = getattr(models, "data", None)
            al = data.get("aliases") if isinstance(data, dict) else getattr(models, "aliases", None)
            aliases = {str(k): str(v) for k, v in al.items()} if isinstance(al, dict) else {}
            if callable(getattr(models, "public_licences", None)):
                lic = list(models.public_licences())
            else:
                lic = [{"model": "YourContent", "licence": "PolyForm Small Business 1.0.0", "commercial_ok": True}]
                rows = [(v.get("label", vid), v.get("licence")) for vid, v in models.image_variants().items()]
                rows += [("Wan 2.2 image-to-video", (models.video or {}).get("licence")),
                         ("ACE-Step 1.5", (models.music or {}).get("licence"))]
                for name, lid in rows:
                    L = models.licence(lid or "unknown")
                    lic.append({"model": name, "licence": L.get("text", lid), "commercial_ok": L.get("commercial_ok")})
        except NotImplementedError:
            pass
    presets = []
    for pid, dims in config_mod.PRESETS.items():
        row = {"id": pid, "label": config_mod.PRESET_LABELS.get(pid, pid)}
        if dims:
            row["w"], row["h"] = dims
        presets.append(row)
    _json(h, 200, {"app": "YourContent", "version": VERSION, "auth": bool(cfg.token), "consent_version": 1,
                   **config_mod.reach(cfg),
                   "variants": variants, "default_variant": default, "aliases": aliases, "variant_info": info,
                   "sizes": list(graphs.SIZES), "presets": presets,
                   "limits": {"image_mb": cfg.max_image_mb, "video_mb": cfg.max_video_mb,
                              "audio_mb": cfg.max_audio_mb},
                   "licences": lic})


def health(state: State, recheck: bool = False) -> dict:
    with state.lock:
        cached = state.health
    if cached and not recheck and time.monotonic() - cached[0] < HEALTH_TTL:
        return cached[1]
    from . import comfy, features, media
    out: dict = {}
    infos: dict = {}
    try:
        stats = comfy.request("/system_stats", timeout=3)
        out["comfy"] = {"ok": True, "version": (stats.get("system") or {}).get("comfyui_version")}
        try:
            infos = comfy.request("/object_info", timeout=15)
        except Exception:  # noqa: BLE001
            infos = {}
    except Exception:  # noqa: BLE001 - off, refused, timed out or not built yet
        out["comfy"] = {"ok": False, "version": None, "error": "ComfyUI not reachable"}
    if state.ffcaps is None or recheck:
        try:
            state.ffcaps = media.ffmpeg_caps()
        except Exception:  # noqa: BLE001
            state.ffcaps = {"version": None, "missing_filters": [], "missing_encoders": [], "error": "ffmpeg not found"}
    models = state.load_models(force=recheck)
    try:
        if models is None:
            raise RuntimeError(state.models_error or "model table not loaded")
        out["features"] = features.compute(infos, models, state.ffcaps)
    except Exception as exc:  # noqa: BLE001 - the page shows the reason; nothing else breaks
        msg = "not ready yet" if isinstance(exc, NotImplementedError) else store.scrub(str(exc))
        out["features"] = {"picture": {"ok": False, "variants": {}, "missing": [msg]},
                           "clip": {"ok": False, "missing": [msg]}, "extend": {"ok": False},
                           "music": {"ok": False, "saver": None, "missing": [msg]},
                           "export": {"ok": False, "missing_filters": []}}
    for name, fn in EXT_FEATURES:  # extension services (posts renderer, craft); each feature() never raises
        try:
            out["features"][name] = fn()
        except Exception as exc:  # noqa: BLE001
            out["features"][name] = {"ok": False, "missing": [store.scrub(str(exc))]}
    out["models_json"] = getattr(models, "local_file", None) or {"loaded": False}
    with state.lock:
        state.health = (time.monotonic(), out)
    return out


def r_health(h, m):
    _json(h, 200, health(h.server.state, _query(h).get("recheck") == "1"))


def r_recent(h, m):
    q = _query(h)
    try:
        limit = max(1, min(int(q.get("limit") or 60), 500))
    except ValueError:
        raise HTTPError(400, "limit must be a number") from None
    kind = q.get("kind") or None
    if kind not in (None, "image", "video", "export"):
        raise HTTPError(400, "kind must be image, video or export")
    _json(h, 200, store.recent(limit, kind))


def r_tracks(h, m):
    _json(h, 200, store.tracks())


def r_info(h, m):
    ref = store.require_owned(store.ref_from_url(m.group(1), m.group(2)))
    sc = store.sidecar(ref) or {}
    sc["used_in"] = projects.used_in(ref)   # for the delete confirm ("Used in 2 cuts")
    _json(h, 200, sc)


def _serve_ref(h, ref):
    target = ref
    if not store.owned(ref) and store.poster_parent(ref) is None:
        raise NotOwned("not found")
    path = store.resolve(target)
    if not os.path.isfile(path):
        raise HTTPError(404, "not found")
    dl = target[1] if _query(h).get("dl") == "1" else None
    send_file(h, path, _ctype_for(target[1]), h.headers.get("Range"), dl)


def r_media(h, m):
    _serve_ref(h, store.ref_from_url(m.group(1), m.group(2)))


def r_alias(h, m):
    _serve_ref(h, store.ref_from_url({"out": "out", "ref": "in"}[m.group(1)], m.group(2)))


def r_proxy(h, m):
    key = m.group(1)
    if not PROXY_RE.fullmatch(key):
        raise HTTPError(404, "not found")
    path = store.data_path("cache", "proxy", key + ".mp4")
    if os.path.islink(path) or not os.path.isfile(path):
        raise HTTPError(404, "not found")
    send_file(h, path, "video/mp4", h.headers.get("Range"), None)


def r_upload(h, m):
    from . import actions
    cfg = h.server.state.cfg
    n = _content_length(h)
    if n <= 0:
        raise HTTPError(400, "the upload is empty")
    ctype = (h.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    caps = {"image": cfg.max_image_mb, "video": cfg.max_video_mb, "audio": cfg.max_audio_mb}
    major = ctype.split("/")[0]
    cap_mb = caps.get(major, max(caps.values()))
    if n > cap_mb * 1024 * 1024:
        what = {"image": "pictures", "video": "videos", "audio": "audio files"}.get(major, "files")
        raise HTTPError(413, f"too big: {what} can be {cap_mb} MB at most")
    try:
        free = store.free_gb("lab")
    except OSError:
        free = None
    if free is not None and free - n / 1e9 < cfg.min_free_gb:
        raise HTTPError(413, f"not enough free disk space (YourContent keeps {cfg.min_free_gb:g} GB free)")
    raw_name = h.headers.get("X-Filename") or ""
    try:
        filename = urllib.parse.unquote(raw_name, errors="replace")
    except Exception:  # noqa: BLE001
        filename = ""
    filename = "".join(ch for ch in filename if ch.isprintable())[:200]
    h._body_read = True  # the action owns the stream now; any error closes the connection
    h.close_connection = True
    h.connection.settimeout(300)
    try:
        out = actions.ingest_upload(h.rfile, n, filename, ctype)
    finally:
        h.connection.settimeout(h.timeout)
    _json(h, 200, out)


def _comfy_up() -> None:
    from . import comfy
    try:
        comfy.ensure()
    except NotImplementedError:
        raise
    except Exception:  # noqa: BLE001
        raise HTTPError(503, "ComfyUI isn't reachable. Start it, then try again.") from None


def _submit(h, kind: str, fn, label: str, queue: str = "gpu", supersede: str | None = None):
    jid = jobs.submit(queue, kind, fn, label=label, supersede=supersede)
    _json(h, 200, {"job": jid})


def r_generate(h, m):
    from . import actions
    body = read_json(h)
    prompt = _text(body, "prompt", MAX_PROMPT)
    if not prompt.strip():
        raise HTTPError(400, "write a prompt first")
    _text(body, "negative", MAX_PROMPT)
    _num(body, "steps", 4, 60, integer=True)
    _num(body, "seed", 0, 2**63, integer=True)
    refs = body.get("refs") or []
    if not isinstance(refs, list) or len(refs) > MAX_REFS:
        raise HTTPError(400, f"at most {MAX_REFS} reference pictures")
    for r in refs:
        store.require_owned(r)
    # refuse before any GPU work: an unknown model id used to run the default model (audit 2026-10-11)
    vid = body.get("variant") or body.get("model")
    models = h.server.state.load_models()
    if models is not None and not models.known(vid):
        raise HTTPError(400, f"unknown model {str(vid)[:60]!r}: use one of {', '.join(models.image_variants())}")
    from . import graphs
    if refs and models is not None and callable(getattr(models, "variant", None)):
        v = models.variant(vid)
        if not graphs.max_refs(v):
            raise HTTPError(400, f"{v['model']} takes no reference pictures: remove them, or pick Qwen-Image 2.1")
    size = body.get("size") or "square"
    if size != "match" and size not in graphs.SIZES:
        raise HTTPError(400, f"size must be one of {', '.join(graphs.SIZES)} or match")
    _comfy_up()
    _submit(h, "generate", lambda job: actions.generate(body, job), prompt.strip()[:60])


def r_animate(h, m):
    from . import actions
    body = read_json(h)
    _text(body, "prompt", MAX_PROMPT)
    store.require_owned(body.get("start"))
    if str(body.get("seconds", "3")) not in ("3", "5"):
        raise HTTPError(400, "seconds must be 3 or 5")
    if body.get("quality", "480p") not in ("480p", "720p"):
        raise HTTPError(400, "quality must be 480p or 720p")
    _num(body, "seed", 0, 2**63, integer=True)
    _comfy_up()
    label = (body.get("prompt") or "").strip()[:60] or "clip"
    _submit(h, "animate", lambda job: actions.animate(body, job), label)


def r_extend(h, m):
    from . import actions
    body = read_json(h)
    _text(body, "prompt", MAX_PROMPT)
    ref = store.require_owned(body.get("src"))
    if store.media_type(ref[1]) != "video":
        raise HTTPError(400, "pick a video to extend")
    if str(body.get("seconds", "3")) not in ("3", "5"):
        raise HTTPError(400, "seconds must be 3 or 5")
    _num(body, "seed", 0, 2**63, integer=True)
    _comfy_up()
    _submit(h, "extend", lambda job: actions.extend(body, job), "extend " + ref[1][:40])


def r_music(h, m):
    from . import actions, timeline
    body = read_json(h)
    tags = _text(body, "tags", MAX_TAGS)
    if not tags.strip():
        raise HTTPError(400, "describe the style first")
    _text(body, "lyrics", MAX_PROMPT)
    _num(body, "bpm", 10, 300, integer=True)
    _num(body, "seconds", 5, 240)
    _num(body, "seed", 0, 2**63, integer=True)
    for k in ("keyscale", "timesig", "language"):
        _text(body, k, 40)
    if body.get("seconds") in (None, ""):
        pid = body.get("project_id")
        seconds = 30.0
        if pid:
            project = projects.load(projects.check_pid(pid))
            _, plan = resolve_project(project)
            seconds = timeline.ace_seconds(plan.T_samples, project.get("music"))
        body["seconds"] = seconds
    _comfy_up()
    _submit(h, "music", lambda job: actions.music(body, job), tags.strip()[:60])


def _audit(h, action: str, what: str, outcome: str) -> None:
    """One stderr line per delete (docker logs keeps it): UTC time, what, the client address, the outcome."""
    when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"yourcontent: audit {when} {action} {what} client={_client_ip(h)} {outcome}", file=sys.stderr, flush=True)


def r_delete(h, m):
    from . import actions
    body = read_json(h)
    ref = store.require_owned(body.get("ref"))
    what = f"{ref[0]}/{ref[1]}"
    used = projects.used_in(ref)
    try:
        out = dict(actions.delete(ref) or {})
    except Exception as exc:
        _audit(h, "delete", what, f"failed ({exc.__class__.__name__})")
        raise
    _audit(h, "delete", what, f"removed={len(out.get('removed') or [])}")
    out.setdefault("used_in", used)
    _json(h, 200, out)


def r_projects_list(h, m):
    _json(h, 200, projects.list_all())


def _project_payload(project: dict) -> dict:
    resolved, _ = resolve_project(project)
    return {"project": project, "resolved": resolved}


def r_projects_create(h, m):
    body = read_json(h)
    name = body.get("name")
    if name is not None and not isinstance(name, str):
        raise HTTPError(400, "name must be text")
    _json(h, 200, _project_payload(projects.create(name)))


def r_project_get(h, m):
    _json(h, 200, _project_payload(projects.load(projects.check_pid(m.group(1)))))


def r_project_save(h, m):
    from . import timeline
    pid = projects.check_pid(m.group(1))
    body = read_json(h)
    rev, project = body.get("rev"), body.get("project")
    if isinstance(rev, bool) or not isinstance(rev, int):
        raise HTTPError(400, "rev must be a number")
    if not isinstance(project, dict):
        raise HTTPError(400, "project must be an object")
    current = projects.load(pid)
    if rev != current.get("rev"):
        raise Conflict(current)
    clean, errors = timeline.validate(project)
    if errors:
        raise HTTPError(400, "; ".join(str(e) for e in errors)[:500])
    keep_snaps(clean, current)
    probes = probes_for(clean)
    refresh_snaps(clean, probes)
    resolved, _ = resolve_project(clean, probes)
    new_rev = projects.save(pid, rev, clean, seconds=_seconds(resolved))
    try:
        saved = projects.load(pid)
    except Exception:  # noqa: BLE001 - the save itself succeeded
        saved = dict(clean, rev=new_rev)
    _json(h, 200, {"rev": new_rev, "resolved": resolved, "project": saved})


def r_project_delete(h, m):
    read_json(h)
    pid = projects.check_pid(m.group(1))
    try:
        projects.remove(pid)
    except Exception as exc:
        _audit(h, "delete-project", pid, f"failed ({exc.__class__.__name__})")
        raise
    _audit(h, "delete-project", pid, "removed")
    _json(h, 200, {"ok": True})


def _render(h, m, mode: str):
    from . import export
    pid = projects.check_pid(m.group(1))
    body = read_json(h)
    rev = body.get("rev")
    if isinstance(rev, bool) or not isinstance(rev, int):
        raise HTTPError(400, "rev must be a number")
    project = projects.load(pid)
    if rev != project.get("rev"):
        raise Conflict(project)
    if not project.get("items"):
        raise HTTPError(400, "add something to the cut first")
    label = ("Preview " if mode == "proxy" else "Export ") + str(project.get("name") or "")[:50]
    sup = f"preview:{pid}" if mode == "proxy" else None
    _submit(h, "preview" if mode == "proxy" else "export",
            lambda job: export.run_render(project, rev, mode, job), label, queue="ffmpeg", supersede=sup)


def r_preview(h, m):
    _render(h, m, "proxy")


def r_export(h, m):
    _render(h, m, "final")


def r_jobs(h, m):
    _json(h, 200, jobs.active())


def r_job(h, m):
    _json(h, 200, jobs.get(m.group(1)))


def r_job_cancel(h, m):
    read_json(h)
    jobs.get(m.group(1))
    _json(h, 200, {"state": jobs.cancel(m.group(1))})


_N = r"([^/?#]+)"
_ROOT = r"(out|gen|in|lab)"
_PID = r"([^/?#]+)"
# (method, compiled path regex, handler). First match wins; paths are matched without the query string.
ROUTES: list[tuple[str, re.Pattern, object]] = [(meth, re.compile("^" + rx + "$"), fn) for meth, rx, fn in [
    ("GET", r"/", r_index),
    ("GET", r"/static/app\.js", r_index),
    ("GET", r"/static/style\.css", r_index),
    ("GET", r"/static/tokens\.css", r_index),
    ("GET", r"/static/theme\.js", r_index),
    ("GET", r"/static/icons\.svg", r_index),
    ("GET", r"/login", r_index),
    ("GET", r"/static/login\.js", r_index),
    ("GET", r"/static/login\.css", r_index),
    ("GET", r"/static/fonts/([A-Za-z0-9_]+\.ttf)", r_font),
    ("POST", r"/api/login", r_login),
    ("POST", r"/api/logout", r_logout),
    ("GET", r"/api/config", r_config),
    ("GET", r"/api/health", r_health),
    ("GET", r"/api/recent", r_recent),
    ("GET", r"/api/tracks", r_tracks),
    ("GET", rf"/api/info/{_ROOT}/{_N}", r_info),
    ("GET", r"/m/cache/([^/?#]+)\.mp4", r_proxy),
    ("GET", rf"/m/{_ROOT}/{_N}", r_media),
    ("GET", rf"/(out|ref)/{_N}", r_alias),
    ("POST", r"/api/upload", r_upload),
    ("POST", r"/api/generate", r_generate),
    ("POST", r"/api/animate", r_animate),
    ("POST", r"/api/extend", r_extend),
    ("POST", r"/api/music", r_music),
    ("POST", r"/api/delete", r_delete),
    ("GET", r"/api/projects", r_projects_list),
    ("POST", r"/api/projects", r_projects_create),
    ("GET", rf"/api/projects/{_PID}", r_project_get),
    ("POST", rf"/api/projects/{_PID}", r_project_save),
    ("POST", rf"/api/projects/{_PID}/delete", r_project_delete),
    ("POST", rf"/api/projects/{_PID}/preview", r_preview),
    ("POST", rf"/api/projects/{_PID}/export", r_export),
    ("GET", r"/api/jobs", r_jobs),
    ("GET", r"/api/jobs/([^/?#]+)", r_job),
    ("POST", r"/api/jobs/([^/?#]+)/cancel", r_job_cancel),
]]


# Extension routes that check their own credential (a secret in the path), so the guard lets them through.
PUBLIC_RX: list[tuple[str, re.Pattern]] = []
EXT_FEATURES: list[tuple[str, object]] = []  # (name, feature()) from modules with FEATURE; shown in /api/health
EXT_STATIC: list[str] = []  # extension static URLs, in install order; index.html gets a tag for each


def install_extension(mod) -> None:
    """Add a feature module's routes and static files (studio merge, 2026-10-10). The module exposes
    `routes(srv) -> [(method, path_regex, handler)]`, where `srv` is this module (use srv._json, srv.read_json,
    srv.HTTPError ...), and optionally `STATIC = {"/static/<file>": (<file in static/>, <content type>)}`.
    Extension routes go after the core ones, so a core route always wins."""
    for meth, rx, fn in mod.routes(sys.modules[__name__]):
        ROUTES.append((meth, re.compile("^" + rx + "$"), fn))
    if getattr(mod, "FEATURE", None):
        EXT_FEATURES.append((mod.FEATURE, mod.feature))
    for meth, rx in getattr(mod, "PUBLIC", ()):
        PUBLIC_RX.append((meth, re.compile("^" + rx + "$")))
    for url, entry in getattr(mod, "STATIC", {}).items():
        if not re.fullmatch(r"/static/[a-z0-9-]+\.(js|css)", url) or url in STATIC:
            raise ValueError(f"bad or duplicate static path {url}")
        STATIC[url] = entry
        EXT_STATIC.append(url)
        ROUTES.append(("GET", re.compile("^" + re.escape(url) + "$"), r_index))


from . import (agent_routes, calendar_routes, google_routes, mcp_routes, openapi,  # noqa: E402 - use the
               plan_sync, render_routes, ui_routes, craft_routes)                   # helpers above

# ui_routes goes last: its studio.js must run after every feature script has defined its window.MediaLab* object.
for _ext in (render_routes, craft_routes, calendar_routes, google_routes, plan_sync, agent_routes, mcp_routes,
             openapi, ui_routes):
    install_extension(_ext)


def match(method: str, path: str):
    """(handler, match) for a request, HTTPError 404 / 405 otherwise."""
    other = False
    for meth, rx, fn in ROUTES:
        mm = rx.match(path)
        if mm:
            if meth == method:
                return fn, mm
            other = True
    raise HTTPError(405 if other else 404, "method not allowed" if other else "not found")


# --------------------------------------------------------------------------------------------- handler

class Handler(BaseHTTPRequestHandler):
    server_version = "ContentCorner"
    sys_version = ""
    timeout = 30
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):  # quiet: errors are printed by _fail; tokens are never logged
        pass

    def _dispatch(self, method: str) -> None:
        self._body_read = False
        try:
            guard(self, method)
            fn, mm = match(method, self.path.partition("?")[0])
            fn(self, mm)
        except Exception as exc:  # noqa: BLE001 - every failure becomes {"error": ...}
            self._fail(exc)

    def _fail(self, exc: Exception) -> None:
        extra: dict = {}
        if isinstance(exc, HTTPError):
            code, msg, extra = exc.code, exc.message, exc.extra
        elif isinstance(exc, Conflict):
            code, msg = 409, str(exc) or "this cut was changed elsewhere"
            cur = exc.current
            if isinstance(cur, dict):
                try:
                    extra = {"current": _project_payload(cur)}
                except Exception:  # noqa: BLE001
                    extra = {"current": {"project": cur, "resolved": None}}
        elif isinstance(getattr(exc, "status", None), int) and 400 <= exc.status <= 599:
            code, msg = exc.status, str(exc) or "request refused"
        elif isinstance(exc, NotImplementedError):
            code, msg = 501, "this feature isn't ready yet"
        elif isinstance(exc, ValueError) and not isinstance(exc, UnicodeError):
            code, msg = 400, str(exc).strip("'\"") or "bad request"
        else:
            code, msg = 500, "something went wrong; the server log has details"
            traceback.print_exc(file=sys.stderr)
        if code >= 500 and code != 501 and not isinstance(exc, HTTPError):
            print(f"yourcontent: {self.command} {self.path.partition('?')[0]} -> {code}", file=sys.stderr, flush=True)
        if not self._body_read and self.command == "POST":
            self.close_connection = True
        headers = {"Allow": "GET, POST"} if code == 405 else None
        try:
            _json(self, code, {"error": store.scrub(msg), **extra}, headers)
        except Exception:  # noqa: BLE001 - client went away
            self.close_connection = True

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_OPTIONS(self) -> None:
        self._dispatch("OPTIONS")

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, state: State):
        self.state = state
        super().__init__(addr, handler)


class StartupError(RuntimeError):
    """The server refuses to start; the message is a plain sentence."""


def prepare(cfg, adopt: bool | None = None) -> State:
    """Everything start-up does before binding: refuse an unsafe bind, set up DATA, configure modules,
    empty DATA/tmp, adopt legacy files (when enabled). Raises StartupError."""
    problem = config_mod.startup_problem(cfg)
    if problem:
        raise StartupError(problem)
    warning = config_mod.startup_warning(cfg)
    if warning:
        print(f"yourcontent: {warning}", file=sys.stderr, flush=True)
    try:
        store.configure(cfg)
    except OSError as exc:
        raise StartupError(str(exc)) from None
    store.clean_tmp()
    from . import actions, comfy, export, features, legacy, media, models, provenance
    for mod in (comfy, media, export, models, features, provenance, legacy, actions):
        fn = getattr(mod, "configure", None)
        if callable(fn):
            fn(cfg)
    state = State(cfg)
    if cfg.adopt_legacy if adopt is None else adopt:
        try:
            res = legacy.adopt_all(cfg, store, comfy)
            n = len(res.get("adopted") or []) if isinstance(res, dict) else 0
            print(f"yourcontent: legacy files adopted this start: {n}", flush=True)
        except NotImplementedError:
            print("yourcontent: legacy adoption not available in this build", file=sys.stderr, flush=True)
        except Exception as exc:  # noqa: BLE001 - never fatal; files stay untouched either way
            print(f"yourcontent: legacy adoption failed: {store.scrub(str(exc))}", file=sys.stderr, flush=True)
    return state


def make_server(cfg, adopt: bool | None = None) -> Server:
    state = prepare(cfg, adopt)
    return Server((cfg.host, cfg.port), Handler, state)


def main() -> None:
    try:
        cfg = config_mod.load()
        srv = make_server(cfg)
    except (config_mod.ConfigError, StartupError) as exc:
        print(f"yourcontent: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(2) from None
    host, port = srv.server_address[:2]
    auth = "token required" if cfg.token else "no token"
    print(f"YourContent {VERSION} listening on {host}:{port} ({auth})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
