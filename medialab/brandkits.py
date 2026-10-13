"""Brand kits for posts: validation (PURE) and a small JSON store under DATA/brandkits/.

A brand kit is what a post is drawn with: {id, name, colors{bg, fg, accent, accent2?, muted?, paper?, ink?},
fonts{display, body, serif?, mono?, hand?}, logo (an owned picture ref, optional), handle, voice (a short note on
tone, for whoever writes the copy), default_styles[]}. The render service re-checks colours and fonts; this module
is the gate for what gets stored.

The first time the store is read it is seeded with the neutral examples in brandkits.example.json; after that they
are ordinary kits (edit or delete them freely). One file per kit: DATA/brandkits/<id>.json, written atomically.
"""
from __future__ import annotations

import json
import os
import re
import threading

from . import store
from .types import BadRef

EXAMPLES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brandkits.example.json")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
STYLE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
FONT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ]{0,47}$")
COLOR_REQUIRED = ("bg", "fg", "accent")
COLOR_OPTIONAL = ("accent2", "muted", "paper", "ink")
FONT_REQUIRED = ("display", "body")
FONT_OPTIONAL = ("serif", "mono", "hand")
LIMITS = {"name": 60, "handle": 80, "voice": 500}
MAX_STYLES = 12
MAX_KITS = 200
_lock = threading.RLock()


class KitError(ValueError):
    """A kit that can't be stored (400), or a store conflict carrying its own status."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------------------------- PURE

def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")[:40].strip("-")
    return s or "kit"


def _str(obj: dict, key: str, required: bool = False) -> str:
    v = obj.get(key, "")
    if v is None:
        v = ""
    if not isinstance(v, str):
        raise KitError(f"{key} must be text")
    v = " ".join(v.split()) if key != "voice" else v.strip()
    if required and not v:
        raise KitError(f"{key} is required")
    if len(v) > LIMITS[key]:
        raise KitError(f"{key} is too long ({LIMITS[key]} characters at most)")
    if any(ord(ch) < 32 and ch not in "\n\t" for ch in v):
        raise KitError(f"{key} has control characters")
    return v


def validate(obj: object, kit_id: str | None = None) -> dict:
    """The stored shape of a kit, or KitError. Unknown keys are dropped. `kit_id` (from the URL) wins over the
    body's id; without either the id is made from the name."""
    if not isinstance(obj, dict):
        raise KitError("send the brand kit as a JSON object")
    name = _str(obj, "name", required=True)
    kid = kit_id or obj.get("id") or slug(name)
    if not isinstance(kid, str) or not ID_RE.fullmatch(kid):
        raise KitError("id may use lowercase letters, digits and dashes (40 at most)")
    colors_in = obj.get("colors")
    if not isinstance(colors_in, dict):
        raise KitError("colors must be an object with bg, fg and accent")
    colors = {}
    for k in COLOR_REQUIRED + COLOR_OPTIONAL:
        v = colors_in.get(k)
        if v in (None, ""):
            if k in COLOR_REQUIRED:
                raise KitError(f"colors.{k} is required (#rrggbb)")
            continue
        if not isinstance(v, str) or not HEX_RE.fullmatch(v):
            raise KitError(f"colors.{k} must be #rrggbb")
        colors[k] = v.lower()
    fonts_in = obj.get("fonts")
    if not isinstance(fonts_in, dict):
        raise KitError("fonts must be an object with display and body")
    fonts = {}
    for k in FONT_REQUIRED + FONT_OPTIONAL:
        v = fonts_in.get(k)
        if v in (None, ""):
            if k in FONT_REQUIRED:
                raise KitError(f"fonts.{k} is required")
            continue
        if not isinstance(v, str) or not FONT_RE.fullmatch(v.strip()):
            raise KitError(f"fonts.{k}: letters, digits and spaces only (48 at most)")
        fonts[k] = v.strip()
    logo = obj.get("logo")
    if logo in (None, "", {}):
        logo = None
    else:
        try:
            logo = store.ref_json(store.check_ref(logo))
        except BadRef as exc:
            raise KitError(f"logo: {exc}") from None
        if store.media_type(logo["name"]) != "image":
            raise KitError("logo must be a picture (PNG, JPEG or WebP)")
    styles = obj.get("default_styles") or []
    if not isinstance(styles, list) or len(styles) > MAX_STYLES:
        raise KitError(f"default_styles must be a list of at most {MAX_STYLES} style ids")
    out_styles: list[str] = []
    for s in styles:
        if not isinstance(s, str) or not STYLE_RE.fullmatch(s):
            raise KitError("default_styles holds style ids (lowercase letters, digits, dashes)")
        if s not in out_styles:
            out_styles.append(s)
    return {"id": kid, "name": name, "colors": colors, "fonts": fonts, "logo": logo,
            "handle": _str(obj, "handle"), "voice": _str(obj, "voice"), "default_styles": out_styles}


def render_brand(kit: dict) -> dict:
    """The `brand` block the render service takes (without the logo, which the caller attaches as data)."""
    return {"name": kit["name"], "handle": kit.get("handle") or "", "colors": dict(kit["colors"]),
            "fonts": dict(kit["fonts"])}


# --------------------------------------------------------------------------------------------- store

def _dir() -> str:
    d = store.data_path("brandkits")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def _path(kid: str) -> str:
    if not isinstance(kid, str) or not ID_RE.fullmatch(kid):
        raise KitError("not found", 404)
    return os.path.join(_dir(), kid + ".json")


def _write(kit: dict) -> None:
    store.atomic_write(_path(kit["id"]), json.dumps(kit, ensure_ascii=False, indent=1).encode("utf-8"))


def load_examples(path: str = EXAMPLES) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    return [validate(k) for k in doc["kits"]]


def _seed() -> None:
    marker = os.path.join(_dir(), ".seeded")
    if os.path.exists(marker):
        return
    for kit in load_examples():
        if not os.path.exists(_path(kit["id"])):
            _write(dict(kit, updated=store.now_iso()))
    store.atomic_write(marker, b"1\n")


def list_kits() -> list[dict]:
    """Every kit, by name."""
    with _lock:
        _seed()
        out = []
        for fn in sorted(os.listdir(_dir())):
            if not fn.endswith(".json") or not ID_RE.fullmatch(fn[:-5]):
                continue
            p = os.path.join(_dir(), fn)
            if os.path.islink(p):
                continue
            try:
                with open(p, encoding="utf-8") as f:
                    kit = json.load(f)
            except (OSError, ValueError):
                continue
            if isinstance(kit, dict) and kit.get("id") == fn[:-5]:
                out.append(kit)
    return sorted(out, key=lambda k: (str(k.get("name", "")).lower(), k["id"]))


def get(kid: str) -> dict:
    with _lock:
        _seed()
        p = _path(kid)
        try:
            if os.path.islink(p):
                raise OSError
            with open(p, encoding="utf-8") as f:
                kit = json.load(f)
        except (OSError, ValueError):
            raise KitError("no brand kit with that id", 404) from None
    if not isinstance(kit, dict):
        raise KitError("no brand kit with that id", 404)
    return kit


def create(obj: object) -> dict:
    kit = validate(obj)
    with _lock:
        _seed()
        if os.path.exists(_path(kit["id"])):
            raise KitError("a brand kit with that id already exists", 409)
        if len(list_kits()) >= MAX_KITS:
            raise KitError(f"{MAX_KITS} brand kits at most", 409)
        kit["updated"] = store.now_iso()
        _write(kit)
    return kit


def update(kid: str, obj: object) -> dict:
    with _lock:
        get(kid)                          # 404 when it doesn't exist
        kit = validate(obj, kit_id=kid)
        kit["updated"] = store.now_iso()
        _write(kit)
    return kit


def delete(kid: str) -> None:
    with _lock:
        get(kid)
        os.remove(_path(kid))
