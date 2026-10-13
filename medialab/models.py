"""Load models.default.json + licences.json, deep-merge DATA/config/models.json (§5.5). Owner: WP3.

The shipped files name only stock public model files. The owner's own variants (and their real filenames) live
in DATA/config/models.json, which is never committed. In that local file a key set to null removes the default
entry (for example `"image": {"variants": {"bf16": null}}`).

`Models` is plain data once built, so the pure modules (graphs, provenance, features) may take it as an argument.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(HERE, "models.default.json")
LICENCES_PATH = os.path.join(HERE, "licences.json")

# Model ids and roles used in sidecars (§5.4, §9). Ids are stable names, not filenames.
VIDEO_IDS = {"unet": "wan2.2-i2v-a14b", "lora": "wan2.2-lightx2v-4step", "clip": "umt5-xxl", "vae": "wan2.1-vae"}
MUSIC_IDS = {"unet": "ace-step-1.5-turbo", "clip1": "qwen3-0.6b-ace15", "clip2": "qwen3-1.7b-ace15",
             "vae": "ace-1.5-vae"}
IMAGE_IDS = {"clip": "qwen3vl-8b", "vae": "qwen-image-2.1-vae"}


def _merge(base: dict, over: dict) -> dict:
    """Deep merge: dicts merge, None deletes, anything else replaces."""
    out = dict(base)
    for k, v in over.items():
        if v is None:
            out.pop(k, None)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _read_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{os.path.basename(path)} must hold a JSON object")
    return data


class Models:
    """The model table and the licence table, merged."""

    def __init__(self, data: dict, licences: dict):
        self.data = data
        self.local_file: dict | None = None   # sha256/bytes/mtime of the DATA/config/models.json read, if any
        self.licences = licences
        img = data.get("image") or {}
        self._image = img
        self._variants: dict[str, dict] = {}
        for vid, spec in (img.get("variants") or {}).items():
            if not isinstance(spec, dict) or not spec.get("unet"):
                continue
            self._variants[str(vid)] = {
                "id": str(vid),
                "label": str(spec.get("label") or vid),
                "unet": spec["unet"],
                "lora": spec.get("lora") or None,
                "licence": spec.get("licence") or img.get("licence") or "unknown",
                "clip": spec.get("clip") or img.get("clip"),
                "vae": spec.get("vae") or img.get("vae"),
                "encoder_licence": img.get("encoder_licence") or "unknown",
            }
        patterns = []
        for item in data.get("file_patterns") or []:
            try:
                patterns.append((re.compile(item[0]), str(item[1])))
            except (re.error, IndexError, TypeError):
                continue
        self._patterns = patterns

    # image
    def image_variants(self) -> dict[str, dict]:
        """{id: {label, unet, lora, licence, clip, vae, encoder_licence}} (a superset of §4's keys)."""
        return {k: dict(v) for k, v in self._variants.items()}

    @property
    def default_variant(self) -> str:
        d = self._image.get("default")
        if d in self._variants:
            return d
        return next(iter(self._variants), "")

    def alias(self, id: str | None) -> str:
        """Map an old or unknown variant id to a current one (the default when nothing matches)."""
        id = str(id or "")
        if id in self._variants:
            return id
        mapped = (self.data.get("aliases") or {}).get(id)
        if mapped in self._variants:
            return mapped
        return self.default_variant

    def known(self, id: str | None) -> bool:
        """Whether a client-sent variant id names a variant here (directly or through an alias). Empty = default."""
        if id is None or id == "":
            return True
        return isinstance(id, str) and (id in self._variants or (self.data.get("aliases") or {}).get(id) in self._variants)

    def variant(self, id: str | None, strict: bool = False) -> dict:
        """The full spec of a variant (after alias). strict: refuse an unknown id instead of using the default."""
        if strict and not self.known(id):
            raise ValueError(f"unknown model {str(id)[:60]!r}: use one of {', '.join(self._variants)}")
        vid = self.alias(id)
        if vid not in self._variants:
            raise ValueError("no picture model is set up")
        return dict(self._variants[vid])

    @property
    def video(self) -> dict:
        return dict(self.data.get("video") or {})

    @property
    def music(self) -> dict:
        return dict(self.data.get("music") or {})

    # licences
    def licence(self, lid: str | None) -> dict:
        """{text, commercial_ok} (plus `short`); an unknown id gives the `unknown` entry."""
        entry = self.licences.get(lid or "") or self.licences.get("unknown") or {"text": "unknown",
                                                                                 "commercial_ok": None}
        return {"text": entry.get("text", lid or "unknown"), "short": entry.get("short") or entry.get("text", ""),
                "commercial_ok": entry.get("commercial_ok")}

    def licence_table(self) -> dict[str, bool | None]:
        return {k: v.get("commercial_ok") for k, v in self.licences.items() if isinstance(v, dict)}

    def licence_for_file(self, filename: str) -> str | None:
        """The licence id of a model file we know, by exact name first, then by the shipped name patterns."""
        name = os.path.basename(str(filename or ""))
        if not name:
            return None
        known = self.known_files()
        if name in known:
            return known[name][1]
        for rx, lid in self._patterns:
            if rx.search(name):
                return lid
        return None

    def id_for_file(self, filename: str) -> str:
        known = self.known_files()
        name = os.path.basename(str(filename or ""))
        if name in known:
            return known[name][0]
        return re.sub(r"\.(safetensors|gguf|ckpt|pt|pth|bin)$", "", name) or "unknown"

    def known_files(self) -> dict[str, tuple[str, str]]:
        """{filename: (model id, licence id)} for every file the table names."""
        out: dict[str, tuple[str, str]] = {}
        for fname, lid in (self.data.get("files") or {}).items():
            if isinstance(lid, str):
                out[fname] = (re.sub(r"\.safetensors$", "", fname), lid)
        img = self._image
        for vid, v in self._variants.items():
            out[v["unet"]] = (f"qwen-image-2.1:{vid}", v["licence"])
            if v["lora"]:
                out[v["lora"]] = (f"qwen-image-2.1-lora:{vid}", v["licence"])
            if v["clip"]:
                out.setdefault(v["clip"], (IMAGE_IDS["clip"], v["encoder_licence"]))
            if v["vae"]:
                out.setdefault(v["vae"], (IMAGE_IDS["vae"], img.get("licence") or v["licence"]))
        vid_ = self.video
        vl = vid_.get("licence") or "unknown"
        for key in ("unet", "lora"):
            if vid_.get(key):
                for side in ("high", "low"):
                    out[vid_[key].format(side)] = (VIDEO_IDS[key], vl)
        for key in ("clip", "vae"):
            if vid_.get(key):
                out[vid_[key]] = (VIDEO_IDS[key], vl)
        mus = self.music
        ml = mus.get("licence") or "unknown"
        for key in ("unet", "clip1", "clip2", "vae"):
            if mus.get(key):
                out[mus[key]] = (MUSIC_IDS[key], ml)
        return out

    # what each generator uses: [(id, role, file, licence id)] for provenance.model_entries
    def used_image(self, variant_id: str | None) -> list[tuple[str, str, str, str]]:
        v = self.variant(variant_id)
        used = [(f"qwen-image-2.1:{v['id']}", "unet", v["unet"], v["licence"])]
        if v["lora"]:
            used.append((f"qwen-image-2.1-lora:{v['id']}", "lora", v["lora"], v["licence"]))
        if v["clip"]:
            used.append((IMAGE_IDS["clip"], "text_encoder", v["clip"], v["encoder_licence"]))
        if v["vae"]:
            used.append((IMAGE_IDS["vae"], "vae", v["vae"], self._image.get("licence") or v["licence"]))
        return used

    def used_video(self) -> list[tuple[str, str, str, str]]:
        v = self.video
        lid = v.get("licence") or "unknown"
        used = []
        for side in ("high", "low"):
            used.append((VIDEO_IDS["unet"], f"unet_{side}", v.get("unet", "").format(side), lid))
            used.append((VIDEO_IDS["lora"], f"lora_{side}", v.get("lora", "").format(side), lid))
        used.append((VIDEO_IDS["clip"], "text_encoder", v.get("clip", ""), lid))
        used.append((VIDEO_IDS["vae"], "vae", v.get("vae", ""), lid))
        return used

    def used_music(self) -> list[tuple[str, str, str, str]]:
        m = self.music
        lid = m.get("licence") or "unknown"
        return [(MUSIC_IDS["unet"], "unet", m.get("unet", ""), lid),
                (MUSIC_IDS["clip1"], "text_encoder", m.get("clip1", ""), lid),
                (MUSIC_IDS["clip2"], "text_encoder", m.get("clip2", ""), lid),
                (MUSIC_IDS["vae"], "vae", m.get("vae", ""), lid)]

    def public_licences(self) -> list[dict]:
        """For GET /api/config: [{model, licence, commercial_ok}]."""
        rows = [{"model": "YourContent", "licence": "PolyForm Small Business 1.0.0", "commercial_ok": True}]
        seen = set()
        for v in self._variants.values():
            key = ("Qwen-Image 2.1", v["licence"])
            if key not in seen:
                seen.add(key)
                rows.append({"model": key[0], "licence": self.licence(v["licence"])["text"],
                             "commercial_ok": self.licence(v["licence"])["commercial_ok"]})
        for model, lid in (("Wan 2.2, lightx2v, umt5", self.video.get("licence")),
                           ("ACE-Step 1.5", self.music.get("licence"))):
            if lid:
                rows.append({"model": model, "licence": self.licence(lid)["text"],
                             "commercial_ok": self.licence(lid)["commercial_ok"]})
        return rows


def load(cfg=None, *, local_path: str | None = None) -> Models:
    """Shipped defaults, deep-merged with DATA/config/models.json when it exists.

    A broken local file is an error the owner should see, so it raises ValueError naming the file.
    """
    data = _read_json(DEFAULT_PATH)
    licences = _read_json(LICENCES_PATH)
    path = local_path
    if path is None and cfg is not None and getattr(cfg, "data", None):
        path = os.path.join(cfg.data, "config", "models.json")
    seen = None
    if path and os.path.isfile(path):
        try:
            with open(path, "rb") as f:
                raw = f.read()
            mtime = os.stat(path).st_mtime
            local = json.loads(raw.decode("utf-8"))
            if not isinstance(local, dict):
                raise ValueError("models.json must hold a JSON object")
        except (OSError, ValueError) as exc:
            raise ValueError(f"config/models.json could not be read: {exc}") from None
        # what this process actually read: compare with `sha256sum data/lab/config/models.json` on the host
        # (a bind mount can keep showing the old file after an editor's rename-save, §13.2)
        seen = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                "mtime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(mtime))}
        if isinstance(local.get("licences"), dict):
            licences = _merge(licences, local.pop("licences"))
        data = _merge(data, local)
    m = Models(data, licences)
    m.local_file = seen
    return m
