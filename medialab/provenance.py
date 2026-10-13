"""PURE licence maths and sidecar construction (§5.4, §11). Owner: WP3.

Worst wins: anything non-commercial makes the whole thing non-commercial, anything unknown makes it unknown.
Each input entry carries that input's own aggregate at creation time, so lineage is a one-level read that
survives the input being deleted later. Warn, never block.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from fractions import Fraction

from . import VERSION
from .types import Probe, Ref

_LICENCES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "licences.json")
_TABLE: dict | None = None

ROLE_LABELS = {"start": "start image", "ref": "reference", "source": "source clip", "item": "item",
               "music": "music", "upload": "your upload"}
UNKNOWN_REASON = "made before provenance tracking; source unknown"
# Tie order when several licences share the worst value (§11.2: qwen-image-nc first, then upload/unknown).
PREFERRED = ("qwen-image-nc", "upload", "unknown")


def _table() -> dict:
    """The shipped licences.json (a data file read once; no other I/O)."""
    global _TABLE
    if _TABLE is None:
        with open(_LICENCES_PATH, encoding="utf-8") as f:
            _TABLE = json.load(f)
    return _TABLE


def _ok_of(lid: str, table: dict) -> bool | None:
    entry = table.get(lid)
    if isinstance(entry, dict):
        return entry.get("commercial_ok")
    if isinstance(entry, (bool, type(None))) and lid in table:
        return entry
    return None   # a licence we have never heard of is unknown


def _short(lid: str, table: dict) -> str:
    entry = table.get(lid)
    if isinstance(entry, dict):
        return entry.get("short") or entry.get("text") or lid
    return lid


def _rank(ok: bool | None) -> int:
    return 0 if ok is False else 1 if ok is None else 2


def worst(values) -> bool | None:
    """False if anything is False, else None if anything is None, else True (True for an empty list)."""
    vals = list(values)
    if any(v is False for v in vals):
        return False
    if any(v is None for v in vals):
        return None
    return True


def model_entries(models, used: list[tuple[str, str, str, str]]) -> list:
    """`used` is [(id, role, file, lid)]; `models` is a models.Models (for licence lookups) or None."""
    out = []
    for mid, role, file, lid in used:
        if models is not None and hasattr(models, "licence"):
            ok = models.licence(lid)["commercial_ok"]
        else:
            ok = _ok_of(lid, _table())
        out.append({"id": mid, "role": role, "file": file, "licence": lid, "commercial_ok": ok})
    return out


def input_entry(role: str, ref: Ref, sidecar: dict | None, snap_ok: bool | None = None) -> dict:
    """One input, with that input's own aggregate snapshotted. Without a sidecar the input is missing and
    `snap_ok` (from a project item's snap) is what it contributes."""
    root, name = ref
    if sidecar is not None:
        return {"role": role, "root": root, "name": name, "commercial_ok": sidecar.get("commercial_ok"),
                "licences": list(sidecar.get("licences") or []),
                "most_restrictive": sidecar.get("most_restrictive"), "missing": False}
    return {"role": role, "root": root, "name": name, "commercial_ok": snap_ok, "licences": [],
            "most_restrictive": None, "missing": True}


def upload_entry(ref: Ref) -> dict:
    """An upload's own origin: rights not checked (§11.3)."""
    root, name = ref
    return {"role": "upload", "root": root, "name": name, "commercial_ok": None, "licences": ["upload"],
            "most_restrictive": "upload", "missing": False}


def origin_unknown() -> dict:
    """The origin of a file made before provenance tracking, when nothing about it was recoverable (§11.8)."""
    return {"role": "origin", "root": None, "name": None, "commercial_ok": None, "licences": ["unknown"],
            "most_restrictive": "unknown", "missing": False, "reason": UNKNOWN_REASON}


def _most_restrictive(lids: list[str], table: dict) -> str:
    if not lids:
        return "unknown"
    worst_rank = min(_rank(_ok_of(l, table)) for l in lids)
    cands = [l for l in lids if _rank(_ok_of(l, table)) == worst_rank]
    for p in PREFERRED:
        if p in cands:
            return p
    return sorted(cands)[0]


def aggregate(models: list, inputs: list, table: dict | None = None) -> dict:
    """{"commercial_ok", "licences", "most_restrictive", "restricted_by"}. `table` maps licence id to
    {commercial_ok, text, short} (default: the shipped licences.json)."""
    table = table if table is not None else _table()
    if not models and not inputs:   # nothing in it (an empty cut): nothing restricts it
        return {"commercial_ok": True, "licences": [], "most_restrictive": None, "restricted_by": []}
    lids = set()
    for m in models:
        if m.get("licence"):
            lids.add(m["licence"])
    for i in inputs:
        lids.update(i.get("licences") or [])
    oks = [m.get("commercial_ok") for m in models] + [i.get("commercial_ok") for i in inputs]
    ok = worst(oks)
    licences = sorted(lids)
    restricted: list[str] = []
    if ok is not True:
        for i in inputs:
            if i.get("commercial_ok") is ok:
                restricted.append(_input_line(i, ok, table))
        for m in models:
            if m.get("commercial_ok") is ok:
                line = f"model {m.get('file') or m.get('id')}: {_short(m.get('licence') or 'unknown', table)}"
                if line not in restricted:
                    restricted.append(line)
    most = _most_restrictive(licences, table)
    if ok is False and _ok_of(most, table) is not False:
        # the restriction came from a snapshot whose licence was not recorded (e.g. a missing item)
        most = next((i.get("most_restrictive") for i in inputs
                     if i.get("commercial_ok") is False and i.get("most_restrictive")), most)
    return {"commercial_ok": ok, "licences": licences, "most_restrictive": most, "restricted_by": restricted}


def _input_line(i: dict, ok: bool | None, table: dict) -> str:
    if i.get("reason"):
        return str(i["reason"])
    label = ROLE_LABELS.get(i.get("role"), i.get("role") or "input")
    name = i.get("name") or "?"
    if i.get("role") == "upload":
        return f"{label} {name}: rights not checked"
    lid = i.get("most_restrictive")
    if not lid or _ok_of(lid, table) is not ok:
        cands = [l for l in i.get("licences") or [] if _ok_of(l, table) is ok]
        lid = _most_restrictive(cands, table) if cands else None
    if i.get("missing"):
        what = "not for commercial use (recorded before it went missing)" if ok is False else \
            "license unknown (recorded before it went missing)"
        return f"{label} {name} (missing): {what}"
    if lid:
        return f"{label} {name}: {_short(lid, table)}"
    return f"{label} {name}: {'not for commercial use' if ok is False else 'licence unknown'}"


def probe_dict(probe) -> dict | None:
    """The §5.4 probe block from a types.Probe (or a dict already in that shape)."""
    if probe is None:
        return None
    if isinstance(probe, dict):
        return dict(probe)
    if isinstance(probe, Probe):
        rate = probe.rate if isinstance(probe.rate, Fraction) else Fraction(probe.rate or 0)
        return {"w": probe.w, "h": probe.h, "fps": f"{rate.numerator}/{rate.denominator}" if rate else "0/1",
                "frames": probe.frames, "seconds": probe.seconds, "has_audio": probe.has_audio,
                "audio_rate": probe.audio_rate, "audio_samples": probe.audio_samples,
                "vcodec": probe.vcodec, "acodec": probe.acodec,
                "sar": f"{Fraction(probe.sar).numerator}/{Fraction(probe.sar).denominator}" if probe.sar else "1/1"}
    raise TypeError("probe must be a Probe or a dict")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_sidecar(kind: str, ref: Ref, *, models, inputs, prompt=None, negative=None, seed=None, params=None,
                 pids=(), probe=None, sha256=None, nbytes=None, poster=None, label="", legacy=None,
                 render=None, created: str | None = None, table: dict | None = None) -> dict:
    """The `media-lab/asset@1` sidecar (§5.4). `models` are model_entries(); `inputs` are input_entry()s."""
    root, name = ref
    agg = aggregate(models, inputs, table)
    side = {
        "schema": "media-lab/asset@1", "root": root, "name": name, "kind": kind,
        "created": created or now_iso(), "app": {"name": "YourContent", "version": VERSION},
        "label": clean_label(label), "poster": poster, "sha256": sha256, "bytes": nbytes,
        "probe": probe_dict(probe),
        "models": list(models), "prompt": prompt, "negative": negative, "seed": seed,
        "params": dict(params) if params else {},
        "inputs": list(inputs),
        "commercial_ok": agg["commercial_ok"], "licences": agg["licences"],
        "most_restrictive": agg["most_restrictive"], "restricted_by": agg["restricted_by"],
        "comfy": {"prompt_ids": [p for p in pids if p]},
        "legacy": legacy, "render": render,
    }
    if legacy is not None:
        side["inferred"] = True
    return side


_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def clean_label(text, limit: int = 60) -> str:
    """Control characters stripped, whitespace folded, at most `limit` characters."""
    s = " ".join(_CTRL.sub(" ", str(text or "")).split())
    return s if len(s) <= limit else s[:limit - 1].rstrip() + "…"


def warnings(agg: dict, n_uploads: int) -> list[str]:
    """The licence line(s) for the export row and job results. Never blocking."""
    out = []
    if agg.get("commercial_ok") is False:
        if agg.get("most_restrictive") == "qwen-image-nc" or "qwen-image-nc" in (agg.get("licences") or []):
            out.append("Contains Qwen-Image 2.1 output: non-commercial use only.")
        else:
            out.append("Contains something that is not for commercial use.")
    if n_uploads:
        out.append("Includes uploads: make sure you have the rights.")
    if agg.get("commercial_ok") is None and not n_uploads:
        out.append("License unknown for part of this: check before commercial use.")
    return out


def mp4_comment(agg: dict, name: str) -> str:
    """One-line licence summary for the MP4 comment tag, at most 255 characters, never the prompt."""
    ok = agg.get("commercial_ok")
    table = _table()
    most = agg.get("most_restrictive") or "unknown"
    text_of = str((table.get(most) or {}).get("text", most)).split(" (")[0]
    if ok is True:
        lic = "commercial use OK"
    elif ok is False:
        lic = f"non-commercial ({text_of})"
    else:
        lic = f"unknown ({text_of})"
    safe = re.sub(r"[^A-Za-z0-9._-]", "", str(name or ""))[:80]
    text = f"YourContent export {safe}; license: {lic}"
    return text[:255]
