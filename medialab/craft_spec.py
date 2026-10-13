"""Craft jobs: recipes, their parameters, brand kits and input references. Shared by the app and the craft runner.

The app validates a request with `validate()` before it queues a job; the runner (craft/craftd.py) validates the
queued job again before it runs it. Nothing here runs anything or touches the network: it checks shapes, ranges
and names, and resolves input references through the store (`resolve()`), refusing anything that is not a plain
file inside its own root.

Inputs are store refs {root, name}. An owned ref (one with a sidecar) is accepted from any root; a file in the
uploads root ("in") without a sidecar is accepted too and counts as an upload whose rights were not checked.

Recipes (each runs one of the four craft apps inside the bubblewrap sandbox, see craft/README.md):
  photo.grade      LightCraft  : a look (brand-derived, a preset, or explicit develop values) on one photo -> JPEG/PNG
  photo.finish     PhotoCraft  : crop to a post format around a focus point, resize, light adjustments -> JPEG (+ PSD)
  video.titlecard  EffectCraft : animated title card in the brand kit's colours, fonts, logo and handle -> MP4 or alpha .mov
  video.reel       FilmCraft   : stills and clips in order, captions, dissolves, a music file, loudness -> MP4 + cover
"""
from __future__ import annotations

import colorsys
import os
import re

from . import config, store
from .types import BadRef

RECIPES = ("photo.grade", "photo.finish", "video.titlecard", "video.reel")
APP_OF = {"photo.grade": "lightcraft", "photo.finish": "photocraft", "video.titlecard": "effectcraft",
          "video.reel": "filmcraft"}
APP_NAMES = {"photocraft": "PhotoCraft", "lightcraft": "LightCraft", "filmcraft": "FilmCraft",
             "effectcraft": "EffectCraft"}
APP_LICENCE = "MIT OR Apache-2.0"
APP_SOURCE = "https://github.com/storytold"
SAM_LICENCE = ("SAM License (Meta), https://github.com/facebookresearch/sam3/blob/main/LICENSE: a custom license, "
               "not open source; the weights are never shipped and load only when you turn AI masks on")

JOB_ID = re.compile(r"c-\d{14}-[0-9a-f]{6}")
# Names a recipe may leave in its sandbox out/ folder (the runner copies nothing else back).
OUT_FILE = re.compile(r"[a-z][a-z0-9-]{0,40}\.(jpg|png|psd|mp4|mov)")
MIME = {"jpg": "image/jpeg", "png": "image/png", "psd": "image/vnd.adobe.photoshop", "mp4": "video/mp4",
        "mov": "video/quicktime"}
STILL = ("jpg", "jpeg", "png", "webp")
VIDEO = ("mp4", "mov", "webm")
AUDIO = ("mp3", "wav", "m4a", "flac", "ogg", "aac")
IMAGE_FOR_LOGO = ("png", "jpg", "jpeg", "webp")
SIZES = {"1:1": (1080, 1080), "4:5": (1080, 1350), "9:16": (1080, 1920), "1.91:1": (1200, 628)}
MAX_REEL_SECONDS = 90
MAX_ITEMS = 20

# Preset looks for photo.grade (LightCraft develop control ids, checked against its control list at run time).
# "brand" derives one from the brand kit's colours (brand_look); "custom" uses only the request's `values`.
LOOKS = {
    "neutral": {},
    "natural": {"light.highlights": -15, "light.shadows": 12, "color.vibrance": 8, "effects.clarity": 4},
    "warm": {"light.exposure": 0.05, "light.highlights": -18, "light.shadows": 12, "color.vibrance": 8,
             "grading.highlights.hue": 40, "grading.highlights.sat": 18, "grading.shadows.hue": 28,
             "grading.shadows.sat": 8, "vignette.amount": -10},
    "cool": {"light.contrast": 10, "light.highlights": -15, "color.saturation": -6, "grading.shadows.hue": 210,
             "grading.shadows.sat": 16, "grading.highlights.hue": 190, "grading.highlights.sat": 6},
    "matte": {"light.contrast": -8, "light.blacks": 18, "light.highlights": -20, "color.saturation": -10,
              "grain.amount": 10},
    "punchy": {"light.contrast": 25, "light.blacks": -15, "color.vibrance": 18, "effects.clarity": 14,
               "vignette.amount": -15},
    "mono": {"color.saturation": -100, "light.contrast": 20, "effects.clarity": 10, "grain.amount": 12},
    "film": {"light.contrast": 12, "light.highlights": -25, "light.shadows": 15, "color.saturation": -8,
             "grading.shadows.hue": 200, "grading.shadows.sat": 12, "grading.highlights.hue": 42,
             "grading.highlights.sat": 16, "vignette.amount": -18, "grain.amount": 16, "grain.size": 25},
}
CONTROL = re.compile(r"[a-z]+(\.[a-zA-Z]+){1,2}")

# Used when a recipe needs colours or fonts and the request has no brand kit.
NEUTRAL_KIT = {"name": "", "bg": "#111111", "fg": "#ffffff", "ac": "#ffffff", "ac2": None, "mut": "#cccccc",
               "display": "Inter", "sans": "Inter", "logo": None, "handle": ""}
HEX = re.compile(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})")
FAMILY = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._'-]{0,63}")
COLOR_KEYS = {"bg": ("bg", "background"), "fg": ("fg", "text", "foreground"),
              "ac": ("accent", "ac", "primary"), "ac2": ("accent2", "ac2", "secondary"), "mut": ("muted", "mut")}
FONT_KEYS = {"display": ("display", "heading", "headline", "title"), "sans": ("body", "sans", "text")}


class SpecError(ValueError):
    """A request the craft service will not run. HTTP 400."""
    status = 400


def ref_key(ref) -> str:
    """A string key for a ref ("root/name"); names never contain a slash."""
    return f"{ref[0]}/{ref[1]}"


def ai_masks_enabled(env=None) -> bool:
    """AI masks (LightCraft's SAM 3 object selection) are off unless YOURCONTENT_CRAFT_AI_MASKS=1 (or MEDIA_LAB_CRAFT_AI_MASKS=1)."""
    env = os.environ if env is None else env
    return str(config.env("CRAFT_AI_MASKS", env) or "0").strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------------------------- refs

def resolve(obj, what: str = "input"):
    """(ref, path, ext, owned) for an input reference, or SpecError. Only regular files inside their root."""
    try:
        ref = store.check_ref(obj)
    except BadRef as exc:
        raise SpecError(f"{what}: {exc}") from None
    owned = store.owned(ref)
    if not owned and ref[0] != "in":
        raise SpecError(f"{what}: {ref[1]} not found")
    try:
        path = store.resolve(ref)
    except BadRef as exc:
        raise SpecError(f"{what}: {exc}") from None
    if os.path.islink(path) or not os.path.isfile(path):
        raise SpecError(f"{what}: {ref[1]} not found")
    ext = ref[1].rsplit(".", 1)[-1].lower() if "." in ref[1] else ""
    return ref, path, ext, owned


# --------------------------------------------------------------------------------------------- brand kits

def _hex(v, what):
    if v in (None, ""):
        return None
    if not isinstance(v, str) or not HEX.fullmatch(v.strip()):
        raise SpecError(f"brand {what} must be a color like #1a2b3c")
    v = v.strip().lower()
    return "#" + "".join(c * 2 for c in v[1:]) if len(v) == 4 else v


def _family(v, what):
    if isinstance(v, dict):
        v = v.get("family") or v.get("name")
    if v in (None, ""):
        return None
    if not isinstance(v, str) or not FAMILY.fullmatch(v.strip()):
        raise SpecError(f"brand {what} font must be a family name (letters, digits, spaces; at most 64)")
    return v.strip()


def brand_kit(obj):
    """Normalise a brand kit {colors, fonts, logo, handle} (as stored by medialab.brandkits, or sent inline).

    colors: {bg|background, fg|text|foreground, accent|primary, accent2|secondary, muted} as #rrggbb, or a list
            [bg, fg, accent, accent2, muted] (strings, or objects with hex/value)
    fonts:  {display|heading, body|sans} family names (or {family}), or a list [display, body]
    logo:   a store ref {root, name} to a PNG/JPEG/WebP (optional)
    handle: a short one-line text such as @name (optional)
    Missing values fall back to NEUTRAL_KIT. Returns None for None; raises SpecError on anything malformed."""
    if obj is None:
        return None
    if not isinstance(obj, dict):
        raise SpecError("brand must be an object {colors, fonts, logo, handle}")
    kit = dict(NEUTRAL_KIT)
    kit["name"] = _text(obj, "name", 60)
    given: set[str] = set()
    colors = obj.get("colors") if obj.get("colors") is not None else obj.get("colours")
    if isinstance(colors, list):
        if len(colors) > 5:
            raise SpecError("brand colors: at most 5 in a list [bg, fg, accent, accent2, muted]")
        vals = [(c.get("hex") or c.get("value")) if isinstance(c, dict) else c for c in colors]
        for key, v in zip(("bg", "fg", "ac", "ac2", "mut"), vals):
            h = _hex(v, key)
            if h:
                kit[key] = h
                given.add(key)
    elif isinstance(colors, dict):
        for key, names in COLOR_KEYS.items():
            for n in names:
                if colors.get(n) not in (None, ""):
                    kit[key] = _hex(colors[n], n)
                    given.add(key)
                    break
    elif colors is not None:
        raise SpecError("brand colors must be an object or a list")
    if "ac" not in given:
        kit["ac"] = kit["fg"]
    if "mut" not in given and "fg" in given:
        kit["mut"] = kit["fg"]
    fonts = obj.get("fonts")
    if isinstance(fonts, list):
        if len(fonts) > 2:
            raise SpecError("brand fonts: at most 2 in a list [display, body]")
        for key, v in zip(("display", "sans"), fonts):
            f = _family(v, key)
            if f:
                kit[key] = f
                given.add(key)
    elif isinstance(fonts, dict):
        for key, names in FONT_KEYS.items():
            for n in names:
                if fonts.get(n) not in (None, ""):
                    kit[key] = _family(fonts[n], n)
                    given.add(key)
                    break
    elif fonts is not None:
        raise SpecError("brand fonts must be an object or a list")
    if "display" in given and "sans" not in given:
        kit["sans"] = kit["display"]
    logo = obj.get("logo")
    if logo not in (None, ""):
        ref, _, ext, _ = resolve(logo, "brand logo")
        if ext not in IMAGE_FOR_LOGO:
            raise SpecError(f"brand logo: {ref[1]} is a .{ext}; this needs {'/'.join(IMAGE_FOR_LOGO)}")
        kit["logo"] = list(ref)
    kit["handle"] = _text(obj, "handle", 40)
    return kit


def _hsl(hexv):
    r, g, b = (int(hexv[i:i + 2], 16) / 255 for i in (1, 3, 5))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    return h * 360, s, l


def luminance(hexv) -> float:
    r, g, b = (int(hexv[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def brand_look(kit) -> dict:
    """LightCraft develop values derived from a brand kit's colours: highlights tinted toward the accent,
    shadows toward the background (or second accent), contrast and vignette from how dark the palette is."""
    dark = luminance(kit["bg"]) < 0.35
    vals = {"light.contrast": 18 if dark else 8, "light.highlights": -20, "light.shadows": 10 if dark else 18,
            "color.vibrance": 10, "effects.clarity": 6, "vignette.amount": -18 if dark else -8}
    h_ac, s_ac, _ = _hsl(kit["ac"])
    if s_ac >= 0.15:
        vals["grading.highlights.hue"] = round(h_ac)
        vals["grading.highlights.sat"] = round(8 + 14 * s_ac)
    shadow = next((c for c in (kit["bg"], kit.get("ac2")) if c and _hsl(c)[1] >= 0.15), None)
    if shadow:
        h, s, _ = _hsl(shadow)
        vals["grading.shadows.hue"] = round(h)
        vals["grading.shadows.sat"] = round(6 + 12 * s)
    if all(_hsl(c)[1] < 0.08 for c in (kit["bg"], kit["fg"], kit["ac"])):
        vals["color.saturation"] = -12
    return vals


# --------------------------------------------------------------------------------------------- fields

def _num(f, key, lo, hi, default=None, integer=False):
    v = f.get(key, default)
    if v is None:
        return None
    if isinstance(v, bool):
        raise SpecError(f"{key} must be a number")
    try:
        v = int(v) if integer else float(v)
    except (TypeError, ValueError):
        raise SpecError(f"{key} must be a number") from None
    if not lo <= v <= hi:
        raise SpecError(f"{key} must be {lo}..{hi}")
    return v


def _text(f, key, limit, required=False):
    v = f.get(key)
    v = "" if v is None else str(v)
    v = " ".join("".join(ch for ch in v if ch.isprintable() or ch.isspace()).split())  # one line, no control chars
    if required and not v:
        raise SpecError(f"{key} is required")
    if len(v) > limit:
        raise SpecError(f"{key} must be at most {limit} characters")
    return v


def _choice(f, key, options, default):
    v = str(f.get(key) or default)
    if v not in options:
        raise SpecError(f"{key} must be one of: {', '.join(options)}")
    return v


def _bool(f, key, default):
    v = f.get(key, default)
    if v is None:
        return default
    if not isinstance(v, bool):
        raise SpecError(f"{key} must be true or false")
    return v


# --------------------------------------------------------------------------------------------- validate

def validate(req) -> dict:
    """Check a job request {recipe, params, brand?}. Returns the normalised job
    {recipe, params, brand, inputs: [{role, ref: [root, name]}]} or raises SpecError."""
    if not isinstance(req, dict):
        raise SpecError("body must be a JSON object")
    recipe = str(req.get("recipe") or "")
    if recipe not in RECIPES:
        raise SpecError(f"recipe must be one of: {', '.join(RECIPES)}")
    f = req.get("params") or {}
    if not isinstance(f, dict):
        raise SpecError("params must be an object")
    kit = brand_kit(req.get("brand"))
    p: dict = {}
    inputs: list[dict] = []

    def add(role, ref):
        if all(i["ref"] != list(ref) for i in inputs):
            inputs.append({"role": role, "ref": list(ref)})

    def ref(key, kinds, role, required=True):
        r = f.get(key)
        if r in (None, ""):
            if required:
                raise SpecError(f"{key} is required")
            return None
        rr, _, ext, _ = resolve(r, key)
        if ext not in kinds:
            raise SpecError(f"{key}: {rr[1]} is a .{ext}; this needs {'/'.join(kinds)}")
        add(role, rr)
        return list(rr)

    if recipe == "photo.grade":
        p["image"] = ref("image", STILL, "source")
        p["look"] = _choice(f, "look", tuple(LOOKS) + ("brand", "custom"), "brand" if kit else "neutral")
        if p["look"] == "brand" and not kit:
            raise SpecError("look brand needs a brand kit (send brand {colors, fonts, logo, handle})")
        vals = f.get("values") or {}
        if not isinstance(vals, dict) or len(vals) > 60:
            raise SpecError("values must be an object of at most 60 LightCraft control ids")
        for k, v in vals.items():
            if not CONTROL.fullmatch(str(k)) or isinstance(v, bool) or not isinstance(v, (int, float)):
                raise SpecError(f"values: {k!r} must be a control id like light.exposure with a number")
        if p["look"] == "custom" and not vals:
            raise SpecError("look custom needs values")
        p["values"] = {str(k): float(v) for k, v in vals.items()}
        base = brand_look(kit) if p["look"] == "brand" else dict(LOOKS.get(p["look"], {}))
        p["develop"] = {**base, **p["values"]}
        p["format"] = _choice(f, "format", ("jpeg", "png"), "jpeg")
        p["long_edge"] = _num(f, "long_edge", 320, 4096, 2048, integer=True)
        p["quality"] = _num(f, "quality", 50, 100, 88, integer=True)
    elif recipe == "photo.finish":
        p["image"] = ref("image", STILL, "source")
        p["format"] = _choice(f, "format", tuple(SIZES), "4:5")
        focus = f.get("focus") or [0.5, 0.5]
        if not (isinstance(focus, list) and len(focus) == 2 and all(
                isinstance(x, (int, float)) and not isinstance(x, bool) and 0 <= x <= 1 for x in focus)):
            raise SpecError("focus must be [x, y] with each 0..1")
        p["focus"] = [float(focus[0]), float(focus[1])]
        p["width"] = _num(f, "width", 320, 4096, SIZES[p["format"]][0], integer=True)
        p["contrast"] = _choice(f, "contrast", ("none", "soft", "strong"), "soft")
        p["vibrance"] = _num(f, "vibrance", -100, 100, 10, integer=True)
        p["warmth"] = _num(f, "warmth", -50, 50, 0, integer=True)
        p["psd"] = _bool(f, "psd", True)
        p["quality"] = _num(f, "quality", 50, 100, 88, integer=True)
    elif recipe == "video.titlecard":
        p["kicker"] = _text(f, "kicker", 60)
        p["headline"] = _text(f, "headline", 80, required=True)
        p["sub"] = _text(f, "sub", 120)
        p["background"] = ref("background", STILL, "source", required=False)
        p["motion"] = _choice(f, "motion", ("push", "pan", "still"), "push")
        p["format"] = _choice(f, "format", ("9:16", "4:5", "1:1"), "9:16")
        p["seconds"] = _num(f, "seconds", 2, 10, 4)
        p["fps"] = int(_choice(f, "fps", ("24", "30"), "30"))
        p["alpha"] = _bool(f, "alpha", False)
        if p["alpha"] and p["background"]:
            raise SpecError("alpha renders text only; drop background or alpha")
        p["logo"] = _bool(f, "logo", bool(kit and kit["logo"]))
        if p["logo"]:
            if not (kit and kit["logo"]):
                raise SpecError("logo needs a brand kit with a logo")
            add("ref", kit["logo"])
        p["handle"] = _bool(f, "handle", bool(kit and kit["handle"]))
        if p["handle"] and not (kit and kit["handle"]):
            raise SpecError("handle needs a brand kit with a handle")
    elif recipe == "video.reel":
        items = f.get("items")
        if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
            raise SpecError(f"items must be a list of 1..{MAX_ITEMS} {{ref, seconds?, caption?}}")
        p["items"], total = [], 0.0
        for i, it in enumerate(items, 1):
            if not isinstance(it, dict):
                raise SpecError(f"items[{i}] must be an object")
            rr, _, ext, _ = resolve(it.get("ref"), f"items[{i}]")
            if ext not in STILL + VIDEO:
                raise SpecError(f"items[{i}]: {rr[1]} is not an image or a video")
            add("item", rr)
            secs = _num(it, "seconds", 0.5, 15, 3 if ext in STILL else None)
            cap = _text(it, "caption", 80)
            p["items"].append({"ref": list(rr), "seconds": secs, "caption": cap,
                               "kind": "still" if ext in STILL else "video"})
            total += secs or 15
        p["music"] = ref("music", AUDIO + VIDEO, "music", required=False)
        p["loudness"] = _num(f, "loudness", -23, -9, -14)
        p["transition"] = _choice(f, "transition", ("dissolve", "cut"), "dissolve")
        p["transition_frames"] = _num(f, "transition_frames", 4, 24, 10, integer=True)
        p["format"] = _choice(f, "format", ("9:16", "4:5", "1:1"), "9:16")
        p["fps"] = int(_choice(f, "fps", ("24", "30"), "30"))
        p["bitrate_kbps"] = _num(f, "bitrate_kbps", 2000, 20000, 8000, integer=True)
        if total > MAX_REEL_SECONDS:
            raise SpecError(f"reel is at most {MAX_REEL_SECONDS} s (items add up to {total:g} s; clips count "
                            "up to 15 s each)")
    return {"recipe": recipe, "params": p, "brand": kit, "inputs": inputs}


def recipes_doc() -> dict:
    """What GET /api/craft/recipes shows callers."""
    ref = "a file reference {root, name}"
    return {
        "photo.grade": {"app": "LightCraft", "params": {
            "image": f"required: {ref} to a JPEG/PNG/WebP",
            "look": f"brand (default with a brand kit: derived from its colors) | {' | '.join(LOOKS)} | custom",
            "values": "optional {control: number} on top of the look, e.g. {\"light.exposure\": 0.3}",
            "format": "jpeg | png", "long_edge": "320-4096 (2048)", "quality": "50-100 (88)"},
            "out": ["the graded picture (JPEG or PNG)"]},
        "photo.finish": {"app": "PhotoCraft", "params": {
            "image": "required", "format": "1:1 | 4:5 | 9:16 | 1.91:1 (4:5)",
            "focus": "[x, y] 0..1, the point the crop keeps (center)", "width": "320-4096 (format width)",
            "contrast": "none | soft | strong", "vibrance": "-100..100 (10)", "warmth": "-50..50 (0)",
            "psd": "true: also a layered PSD", "quality": "50-100"},
            "out": ["the finished JPEG", "a layered PSD (download)"]},
        "video.titlecard": {"app": "EffectCraft", "params": {
            "headline": "required, <= 80 chars", "kicker": "<= 60", "sub": "<= 120",
            "background": "optional still (else the brand's background color)", "motion": "push | pan | still",
            "format": "9:16 | 4:5 | 1:1", "seconds": "2-10 (4)", "fps": "24 | 30",
            "alpha": "true: text only on transparency, ProRes 4444 .mov for compositing over video",
            "logo": "true (default when the brand kit has a logo): the logo at the top",
            "handle": "true (default when the brand kit has a handle): the handle at the bottom"},
            "out": ["the title card (MP4, or .mov with alpha)", "a poster frame"]},
        "video.reel": {"app": "FilmCraft", "params": {
            "items": f"1-{MAX_ITEMS} of {{ref, seconds (stills 0.5-15, default 3; clips trimmed to it), caption "
                     f"(<= 80)}}; total <= {MAX_REEL_SECONDS} s",
            "music": f"optional music file ({'/'.join(AUDIO)}) or a video whose audio becomes the bed",
            "loudness": "-23..-9 LUFS (-14)", "transition": "dissolve | cut", "transition_frames": "4-24 (10)",
            "format": "9:16 | 4:5 | 1:1", "fps": "24 | 30", "bitrate_kbps": "2000-20000 (8000)"},
            "out": ["the reel (H.264 MP4)", "a cover frame"]},
    }
