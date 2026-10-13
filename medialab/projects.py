"""Project JSON CRUD, rev check, used_in (§5.3). Owner: WP2.

Files: DATA/projects/p_<12hex>.json, plus .bak = the previous save. Writes are atomic (temp + os.replace).
`save` keeps id, schema and created from the stored copy and bumps rev; the caller (server) has already run
`timeline.validate` and refreshed `snap`. The stored JSON also carries "seconds" (the cut's length when it was
saved) so the project list needs no planning.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import unicodedata

from . import store
from .types import BadRef, Conflict, NotOwned, Ref

PID_RE = re.compile(r"^p_[0-9a-f]{12}$")
SCHEMA = "media-lab/project@1"
MAX_BYTES = 256 * 1024
MAX_NAME = 80
DEFAULT_NAME = "Untitled cut"
DEFAULT_EXPORT = {"preset": "9:16", "fit": "auto", "fps": 24, "fade": 0.5}

_lock = threading.RLock()


class TooLarge(ValueError):
    """The project JSON is over 256 KB. HTTP 413."""
    status = 413


def check_pid(pid: object) -> str:
    if not isinstance(pid, str) or not PID_RE.fullmatch(pid):
        raise BadRef("that project id isn't valid")
    return pid


def clean_name(name: object) -> str:
    """At most 80 characters, control characters stripped, whitespace collapsed; empty -> "Untitled cut"."""
    s = name if isinstance(name, str) else ""
    s = "".join(" " if unicodedata.category(ch)[0] == "C" else ch for ch in s)
    s = " ".join(s.split())[:MAX_NAME].strip()
    return s or DEFAULT_NAME


def _path(pid: str, ext: str = ".json") -> str:
    return store.data_path("projects", check_pid(pid) + ext)


def _read(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    if not isinstance(d, dict):
        raise ValueError("not a project")
    return d


def _encode(project: dict) -> bytes:
    data = json.dumps(project, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_BYTES:
        raise TooLarge("this cut is too big to save (over 256 KB)")
    return data


def create(name: str | None = None) -> dict:
    now = store.now_iso()
    with _lock:
        while True:
            pid = "p_" + secrets.token_hex(6)
            if not os.path.exists(_path(pid)):
                break
        project = {"schema": SCHEMA, "id": pid, "name": clean_name(name), "rev": 1, "created": now,
                   "updated": now, "items": [], "music": None, "export": dict(DEFAULT_EXPORT), "seconds": 0}
        store.atomic_write(_path(pid), _encode(project))
    return project


def load(pid: str) -> dict:
    p = _path(pid)
    try:
        return _read(p)
    except FileNotFoundError:
        raise NotOwned("that cut doesn't exist") from None
    except (OSError, ValueError):
        raise NotOwned("that cut can't be read") from None


def save(pid: str, rev: int, project: dict, seconds: float | None = None) -> int:
    """Returns the new rev. Raises Conflict(current) on a stale rev, TooLarge over 256 KB."""
    if not isinstance(project, dict):
        raise ValueError("project must be an object")
    if isinstance(rev, bool) or not isinstance(rev, int):
        raise ValueError("rev must be a number")
    with _lock:
        current = load(pid)
        if rev != current.get("rev"):
            raise Conflict(current)
        new = {k: v for k, v in project.items() if k not in ("schema", "id", "rev", "created", "updated", "seconds")}
        new["name"] = clean_name(project.get("name", current.get("name")))
        out = {"schema": SCHEMA, "id": current["id"], "name": new.pop("name"), "rev": int(current["rev"]) + 1,
               "created": current.get("created") or store.now_iso(), "updated": store.now_iso()}
        out.update(new)
        out["seconds"] = round(float(seconds), 6) if seconds is not None else current.get("seconds", 0)
        data = _encode(out)
        p = _path(pid)
        try:
            with open(p, "rb") as f:
                old = f.read()
            store.atomic_write(_path(pid, ".json.bak"), old)
        except OSError:
            pass
        store.atomic_write(p, data)
        return out["rev"]


def list_all() -> list[dict]:
    """[{id, name, updated, items, seconds}] newest first."""
    out = []
    d = store.data_path("projects")
    try:
        names = os.listdir(d)
    except OSError:
        return []
    for fn in names:
        if not fn.endswith(".json") or not PID_RE.fullmatch(fn[:-5]):
            continue
        try:
            p = _read(os.path.join(d, fn))
        except (OSError, ValueError):
            continue
        items = p.get("items") if isinstance(p.get("items"), list) else []
        out.append({"id": fn[:-5], "name": str(p.get("name") or DEFAULT_NAME), "updated": str(p.get("updated") or ""),
                    "items": len(items), "seconds": p.get("seconds") or 0})
    out.sort(key=lambda r: (r["updated"], r["id"]), reverse=True)
    return out


def remove(pid: str) -> None:
    """Delete the project JSON and its .bak. Media files are untouched."""
    with _lock:
        p = _path(pid)
        if not os.path.exists(p):
            raise NotOwned("that cut doesn't exist")
        for path in (p, _path(pid, ".json.bak")):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass


def refs_of(project: dict) -> list[Ref]:
    """Every valid ref a project uses (items, then music), in order, without duplicates."""
    out: list[Ref] = []
    srcs = [it.get("src") for it in project.get("items") or [] if isinstance(it, dict)]
    m = project.get("music")
    if isinstance(m, dict):
        srcs.append(m.get("src"))
    for s in srcs:
        try:
            r = store.check_ref(s)
        except BadRef:
            continue
        if r not in out:
            out.append(r)
    return out


def used_in(ref: Ref) -> list[str]:
    """Names of the projects whose items or music use this ref."""
    ref = store.check_ref(ref)
    names = []
    d = store.data_path("projects")
    try:
        files = sorted(os.listdir(d))
    except OSError:
        return []
    for fn in files:
        if not fn.endswith(".json") or not PID_RE.fullmatch(fn[:-5]):
            continue
        try:
            p = _read(os.path.join(d, fn))
        except (OSError, ValueError):
            continue
        if ref in refs_of(p):
            names.append(str(p.get("name") or DEFAULT_NAME))
    return names
