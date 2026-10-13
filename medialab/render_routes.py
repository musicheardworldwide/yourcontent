"""Posts: brand kits + the render service, as a server extension (`server.install_extension(render_routes)`).

    GET  /api/styles                  style list and font list from the render service, plus {"render": feature}
    GET  /api/posts/health            {"ok", "styles", "missing": [reason]} (never an error when the service is off)
    GET  /api/brandkits               {"kits": [...]}
    POST /api/brandkits               create a kit (409 if the id exists)
    POST /api/brandkits/<id>          replace a kit (404 if it doesn't exist)
    POST /api/brandkits/<id>/delete   delete a kit
    POST /api/posts/render            draw a post; preview=true answers the image, otherwise it is saved to the
                                      "lab" root with a sidecar and the answer is JSON. Previews 480 px wide or less
                                      are kept in DATA/cache/thumbs, keyed on the style's version (X-Thumb-Cache)

A saved post's sidecar follows provenance.py: its inputs are the pictures it was drawn from (and the kit's logo),
so it inherits the most restrictive licence among them; a text-only post has no inputs and is commercial OK; an
input whose licence is unknown (an upload, say) makes the post unknown (null). Fonts are OFL/Apache and do not
restrict the output.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os

from . import brandkits, media, provenance, render_client, store
from .render_client import RenderFailed, RenderOff

FEATURE = "posts"  # /api/health shows render_client.feature() under this name (server.EXT_FEATURES)
feature = render_client.feature
STATIC = {"/static/posts.js": ("posts.js", "text/javascript; charset=utf-8"),
          "/static/posts.css": ("posts.css", "text/css; charset=utf-8")}
MAX_IMAGES = 4
MAX_IMAGE_MB = 16
TEXT_LIMITS = {"headline": 300, "kicker": 120, "body": 600, "detail": 80, "handle": 80, "label": 60}
MAX_CAPTION = 2200
MIME = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}
FALLBACK_FORMATS = ["1:1", "4:5", "9:16", "1.91:1"]
THUMB_MAX_W = 480        # previews this wide or narrower (the gallery's 320 px) are cached on disk
THUMB_MAX = 3000         # files kept in DATA/cache/thumbs (about 50 KB each)
THUMB_SCHEMA = 1
_thumb_writes = 0


def _data_url(srv, ref) -> tuple[str, tuple]:
    """An owned picture as a data: URL for the render service, and its ref."""
    ref = store.require_owned(ref)
    if store.media_type(ref[1]) != "image":
        raise srv.HTTPError(400, "pick a picture, not a video or sound")
    path = store.resolve(ref)
    try:
        size = os.path.getsize(path)
        if size > MAX_IMAGE_MB << 20:
            raise srv.HTTPError(413, f"that picture is too large for a post ({MAX_IMAGE_MB} MB at most)")
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        raise srv.HTTPError(404, "that picture's file is missing") from None
    kind = media.sniff(data[:16])
    if kind not in MIME:
        raise srv.HTTPError(400, "that picture isn't a PNG, JPEG or WebP")
    return f"data:{MIME[kind]};base64,{base64.b64encode(data).decode('ascii')}", ref


def _text(srv, body: dict) -> dict:
    t = body.get("text") or {}
    if not isinstance(t, dict):
        raise srv.HTTPError(400, "text must be an object")
    return {k: srv._text(t, k, lim) for k, lim in TEXT_LIMITS.items()}


def _kit_logo_ok(srv, kit: dict) -> None:
    if kit.get("logo"):
        ref = store.check_ref(kit["logo"])
        if not store.owned(ref):
            raise srv.HTTPError(400, "logo: pick one of your pictures")


def _thumb_key(spec: dict, kit: dict, refs: list) -> str | None:
    """Disk-cache key of a gallery thumbnail: the style's version from the render service (a hash of its template
    and shared assets), the kit as stored, the words and options, and each picture's identity (ref, size, mtime).
    None when the service reports no version (an older service): such pictures are not cached."""
    try:
        ver = next((s.get("version") for s in render_client.styles().get("styles", []) if s.get("id") == spec["style"]), None)
    except (RenderOff, RenderFailed):
        return None
    if not ver:
        return None
    pics = []
    for r in refs + ([kit["logo"]] if kit.get("logo") else []):
        try:
            ref = store.check_ref(r)
            st = os.stat(store.resolve(ref))
            pics.append([ref[0], ref[1], st.st_size, st.st_mtime_ns])
        except Exception:  # noqa: BLE001 - a missing file is part of the key, not an error here
            pics.append(["missing", str(r)])
    doc = {k: v for k, v in spec.items() if k not in ("brand", "images")}
    raw = json.dumps([THUMB_SCHEMA, ver, kit, doc, pics], sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _thumb_path(key: str) -> str:
    return store.data_path("cache", "thumbs", key[:2], key + ".png")


def _thumb_get(key: str | None) -> bytes | None:
    if not key:
        return None
    try:
        with open(_thumb_path(key), "rb") as f:
            return f.read()
    except OSError:
        return None


def _thumb_put(key: str, data: bytes) -> None:
    """Best effort: a full disk or a read-only DATA only means no cache. Past THUMB_MAX files the oldest go."""
    path = _thumb_path(key)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        store.atomic_write(path, data)
        global _thumb_writes
        _thumb_writes += 1
        if _thumb_writes % 100 == 0:
            base = store.data_path("cache", "thumbs")
            files = [os.path.join(d, n) for d, _, ns in os.walk(base) for n in ns if n.endswith(".png")]
            if len(files) > THUMB_MAX:
                files.sort(key=lambda p: os.stat(p).st_mtime)
                for old in files[:len(files) - THUMB_MAX]:
                    os.unlink(old)
    except OSError:
        pass


def render_post(srv, h, body: dict) -> None:
    kit = brandkits.get(body.get("kit") if isinstance(body.get("kit"), str) else "")
    style = body.get("style")
    if not isinstance(style, str) or not brandkits.STYLE_RE.fullmatch(style):
        raise srv.HTTPError(400, "pick a style")
    text = _text(srv, body)
    if not text["headline"].strip():
        raise srv.HTTPError(400, "a headline is needed")
    caption = srv._text(body, "caption", MAX_CAPTION)
    refs = []
    if body.get("image"):
        refs.append(body["image"])
    extra = body.get("images") or []
    if not isinstance(extra, list):
        raise srv.HTTPError(400, "images must be a list of pictures")
    refs += extra
    if len(refs) > MAX_IMAGES:
        raise srv.HTTPError(400, f"{MAX_IMAGES} pictures at most")
    images, inputs, used, warnings = [], [], [], []
    for r in refs:
        url, ref = _data_url(srv, store.check_ref(r))
        if ref in used:
            continue
        used.append(ref)
        images.append(url)
        inputs.append(provenance.input_entry("image", ref, store.sidecar(ref)))
    brand = brandkits.render_brand(kit)
    if kit.get("logo"):
        try:
            logo_url, logo_ref = _data_url(srv, store.check_ref(kit["logo"]))
            brand["logo"] = logo_url
            inputs.append(provenance.input_entry("logo", logo_ref, store.sidecar(logo_ref)))
        except Exception:  # noqa: BLE001 - a deleted logo must not block the post
            warnings.append("The kit's logo is gone; drawn with its name instead.")
    preview = body.get("preview") is True
    spec = {"style": style, "brand": brand, "images": images,
            "text": {k: text[k] for k in ("headline", "kicker", "body", "detail", "handle", "label")},
            "format": body.get("format") or "4:5", "type": "jpg" if body.get("type") == "jpg" else "png",
            "palette": body.get("palette") or "brand", "safe_area": body.get("safe_area") is not False,
            "mark": body.get("mark") is not False, "slide": srv._num(body, "slide", 1, 20, 1, integer=True)}
    width = srv._num(body, "width", 320, 2160, None, integer=True)
    if width is not None:
        spec["width"] = width
    elif preview:
        spec["width"] = 540
    key = _thumb_key(spec, kit, refs) if preview and spec.get("width", 999) <= THUMB_MAX_W else None
    hit = _thumb_get(key)
    if hit is not None:
        srv._send_raw(h, 200, hit, "image/png", {"Cache-Control": "no-store", "X-Thumb-Cache": "hit"})
        return
    data, info, ctype = render_client.render(spec)
    if preview:
        if key and ctype == "image/png":
            _thumb_put(key, data)
        srv._send_raw(h, 200, data, ctype, {"Cache-Control": "no-store", "X-Render-Info": json.dumps(info),
                                            "X-Thumb-Cache": "miss" if key else "off"})
        return
    cfg = h.server.state.cfg
    try:
        if store.free_gb("lab") - len(data) / 1e9 < cfg.min_free_gb:
            raise srv.HTTPError(413, f"not enough free disk space (YourContent keeps {cfg.min_free_gb:g} GB free)")
    except OSError:
        pass
    ext = "jpg" if ctype == "image/jpeg" else "png"
    ref = ("lab", store.new_name("post", ext))
    store.atomic_write(store.resolve(ref), data)
    params = {"kit": kit["id"], "style": style, "format": spec["format"], "palette": spec["palette"],
              "text": spec["text"], "caption": caption}
    sc = provenance.make_sidecar(
        "post", ref, models=[], inputs=inputs, params=params, probe={"w": info.get("w"), "h": info.get("h")},
        sha256=hashlib.sha256(data).hexdigest(), nbytes=len(data), label=text["headline"] or "post",
        render={"service": "posts", "style": style, "style_version": info.get("version"),
                "format": spec["format"], "w": info.get("w"), "h": info.get("h"), "kit": kit["id"]})
    store.put_sidecar(ref, sc)
    n_up = sum(1 for i in inputs if "upload" in (i.get("licences") or []))
    agg = {"commercial_ok": sc["commercial_ok"], "most_restrictive": sc["most_restrictive"],
           "licences": sc["licences"]}
    srv._json(h, 200, {"item": store.item(sc), "info": info, "commercial_ok": sc["commercial_ok"],
                       "restricted_by": sc["restricted_by"], "caption": caption,
                       "warnings": warnings + provenance.warnings(agg, n_up)})


def routes(srv):
    def r_styles(h, m):
        try:
            doc = dict(render_client.styles())
            doc["render"] = {"ok": True, "missing": []}
        except (RenderOff, RenderFailed) as exc:
            doc = {"styles": [], "fonts": [], "formats": FALLBACK_FORMATS,
                   "render": {"ok": False, "missing": [store.scrub(str(exc))]}}
        srv._json(h, 200, doc)

    def r_health(h, m):
        srv._json(h, 200, render_client.feature())

    def r_kits(h, m):
        srv._json(h, 200, {"kits": brandkits.list_kits()})

    def r_kit_create(h, m):
        body = srv.read_json(h)
        _kit_logo_ok(srv, brandkits.validate(body))
        srv._json(h, 200, brandkits.create(body))

    def r_kit_save(h, m):
        body = srv.read_json(h)
        _kit_logo_ok(srv, brandkits.validate(body, kit_id=m.group(1)))
        srv._json(h, 200, brandkits.update(m.group(1), body))

    def r_kit_delete(h, m):
        srv.read_json(h)
        brandkits.delete(m.group(1))
        srv._json(h, 200, {"deleted": m.group(1)})

    def r_render(h, m):
        render_post(srv, h, srv.read_json(h))

    return [
        ("GET", r"/api/styles", r_styles),
        ("GET", r"/api/posts/health", r_health),
        ("GET", r"/api/brandkits", r_kits),
        ("POST", r"/api/brandkits", r_kit_create),
        ("POST", r"/api/brandkits/([^/?#]+)", r_kit_save),
        ("POST", r"/api/brandkits/([^/?#]+)/delete", r_kit_delete),
        ("POST", r"/api/posts/render", r_render),
    ]
