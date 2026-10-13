"""Roots, ref checks, sidecar index, ledger, names, scoped delete (§3.1, §5.2, §5.6). Owner: WP2.

Module state: call `configure(cfg)` once at start (server.main, `python3 -m medialab`, tests). Every other
function reads the roots set there. A ref is (root, name); clients never send paths.

A ref is **owned** iff DATA/sidecars/<root>/<name>.json exists. Only owned refs are listed, served, used or
deleted. The one exception is a poster an owned sidecar names in `poster`: `poster_parent()` finds that parent.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import threading
import time
import urllib.parse
from datetime import datetime, timezone

from .types import BadRef, NotOwned, Ref

ROOTS = ("out", "gen", "in", "lab")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")

IMAGE_EXT = {"png", "jpg", "jpeg", "webp"}
VIDEO_EXT = {"mp4", "mov", "webm"}
AUDIO_EXT = {"mp3", "wav", "m4a", "flac", "ogg", "opus", "aac"}

# DATA layout (§5.2). Created at configure(), mode 0700.
DATA_DIRS = ("config", "media", "projects", "cache", "cache/music", "cache/loud", "cache/audio", "cache/proxy",
             "tmp") + tuple("sidecars/" + r for r in ROOTS)

_cfg = None
_dirs: dict[str, str] = {}
_data: str = ""
_lock = threading.RLock()
_ledger_lock = threading.Lock()
_index_cache: tuple[tuple, dict] | None = None


# --------------------------------------------------------------------------------------------- setup

def configure(cfg, prepare: bool = True) -> None:
    """Point the store at cfg's folders. With prepare, create the DATA layout (0700) and DATA/VERSION.
    Raises OSError with a plain message when DATA can't be written."""
    global _cfg, _dirs, _data, _index_cache
    data = os.path.abspath(cfg.data)
    out = os.path.abspath(cfg.comfy_output)
    dirs = {"out": out, "gen": os.path.join(out, cfg.output_subdir), "in": os.path.abspath(cfg.uploads),
            "lab": os.path.join(data, "media")}
    with _lock:
        _cfg, _dirs, _data, _index_cache = cfg, dirs, data, None
    if prepare:
        try:
            for d in ("",) + DATA_DIRS:
                p = os.path.join(data, d) if d else data
                os.makedirs(p, mode=0o700, exist_ok=True)
                if os.stat(p).st_mode & 0o077:
                    os.chmod(p, 0o700)
            vp = os.path.join(data, "VERSION")
            if not os.path.exists(vp):
                _atomic_write(vp, b"1\n")
        except OSError as exc:
            raise OSError(f"YourContent can't write to its data folder ({exc.strerror or exc}). Create it and "
                          "make it writable by the user the app runs as.") from None
        try:  # ComfyUI also creates this on its first save; a read-only output mount is not fatal here
            os.makedirs(dirs["gen"], exist_ok=True)
        except OSError:
            pass


def config():
    if _cfg is None:
        raise RuntimeError("store.configure(cfg) was not called")
    return _cfg


def data_path(*parts: str) -> str:
    """A path under DATA (e.g. data_path("cache", "proxy", key + ".mp4")). Parts are fixed by the caller."""
    config()
    return os.path.join(_data, *parts)


def root_dir(root: str) -> str:
    config()
    if root not in _dirs:
        raise BadRef("unknown folder")
    return _dirs[root]


def clean_tmp() -> int:
    """Empty DATA/tmp (done at start). Returns how many entries were removed."""
    tmp = data_path("tmp")
    n = 0
    for e in os.listdir(tmp):
        p = os.path.join(tmp, e)
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p)
            else:
                os.remove(p)
            n += 1
        except OSError:
            pass
    return n


_PATHISH = re.compile(r"(?:[A-Za-z]:)?(?:[\\/][^\s'\"\\/:,;)]+){2,}[\\/]?")


def scrub(msg: str) -> str:
    """Remove absolute paths from a message before it reaches a response (§6: responses never contain
    absolute paths). Configured roots become their root name; any other path-like run becomes "…"."""
    s = str(msg)
    for root, d in sorted(_dirs.items(), key=lambda kv: -len(kv[1])):
        s = s.replace(d + os.sep, "").replace(d, root)
    if _data:
        s = s.replace(_data + os.sep, "").replace(_data, "data")
    return _PATHISH.sub("…", s)


def free_gb(root: str = "lab") -> float:
    """Free space on the disk holding a root, in GB (10^9)."""
    return shutil.disk_usage(root_dir(root)).free / 1e9


# --------------------------------------------------------------------------------------------- refs

def check_name(name: object) -> str:
    if not isinstance(name, str) or "\x00" in name or ".." in name or not NAME_RE.fullmatch(name):
        raise BadRef("that file name isn't allowed")
    return name


def check_ref(obj: object) -> Ref:
    """Validate a client ref ({"root","name"} or a pair). Raises BadRef."""
    if isinstance(obj, dict):
        root, name = obj.get("root"), obj.get("name")
    elif isinstance(obj, (list, tuple)) and len(obj) == 2:
        root, name = obj
    else:
        raise BadRef("expected a file reference {root, name}")
    if not isinstance(root, str) or root not in ROOTS:
        raise BadRef("unknown folder")
    return (root, check_name(name))


def ref_from_url(root: str, quoted_name: str) -> Ref:
    """A ref from a URL path segment: percent-decoded once, then checked (so %2e%2e%2f is refused)."""
    try:
        name = urllib.parse.unquote(quoted_name, errors="strict") if "%" in quoted_name else quoted_name
    except UnicodeError:
        raise BadRef("that file name isn't allowed") from None
    return check_ref((root, name))


def ref_json(ref: Ref) -> dict:
    return {"root": ref[0], "name": ref[1]}


def resolve(ref: Ref) -> str:
    """Absolute path of a ref inside its root (no symlinks, no escape). Raises BadRef.
    The file need not exist yet (callers use it for new files too)."""
    root, name = check_ref(ref)
    base = root_dir(root)
    path = os.path.join(base, name)
    if os.path.realpath(os.path.dirname(path)) != os.path.realpath(base):
        raise BadRef("that file is outside YourContent's folders")
    try:
        if os.path.islink(path):
            raise BadRef("links are not followed")
    except OSError:
        raise BadRef("that file can't be read") from None
    return path


def _sidecar_path(ref: Ref) -> str:
    root, name = check_ref(ref)
    return data_path("sidecars", root, name + ".json")


def owned(ref: Ref) -> bool:
    try:
        p = _sidecar_path(ref)
    except BadRef:
        return False
    return os.path.isfile(p) and not os.path.islink(p)


def require_owned(ref: Ref) -> Ref:
    """check_ref + owned, else NotOwned (404)."""
    ref = check_ref(ref)
    if not owned(ref):
        raise NotOwned("not found")
    return ref


def sidecar(ref: Ref) -> dict | None:
    try:
        p = _sidecar_path(ref)
    except BadRef:
        return None
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def put_sidecar(ref: Ref, data: dict) -> None:
    """Atomic write of DATA/sidecars/<root>/<name>.json. root and name are set from the ref."""
    global _index_cache
    ref = check_ref(ref)
    doc = dict(data)
    doc["root"], doc["name"] = ref
    _atomic_write(_sidecar_path(ref), json.dumps(doc, ensure_ascii=False, indent=1).encode("utf-8"))
    with _lock:
        _index_cache = None


def poster_parent(ref: Ref) -> Ref | None:
    """The owned ref (same root) whose sidecar names this file as its poster, if any."""
    root, name = check_ref(ref)
    stem = name.rsplit(".", 1)[0]
    # Posters are same-stem by construction (§3.2); try that first, then scan.
    for ext in sorted(VIDEO_EXT):
        cand = (root, f"{stem}.{ext}")
        if cand[1] != name:
            sc = sidecar(cand) if NAME_RE.fullmatch(cand[1]) else None
            if sc and sc.get("poster") == name:
                return cand
    for sc in index():
        if sc.get("root") == root and sc.get("poster") == name and sc.get("name") != name:
            return (root, sc["name"])
    return None


# --------------------------------------------------------------------------------------------- index

def index() -> list[dict]:
    """Every sidecar (each with root and name), unordered. Parsed sidecars are cached per file and reused
    while the file's (inode, mtime, size) is unchanged, so a changed or removed sidecar is always seen."""
    global _index_cache
    with _lock:
        old = _index_cache[1] if _index_cache else {}
    cache: dict = {}
    out = []
    for r in ROOTS:
        d = data_path("sidecars", r)
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            fn = e.name
            if not fn.endswith(".json") or fn.startswith(".") or not NAME_RE.fullmatch(fn[:-5]):
                continue
            try:
                if e.is_symlink():
                    continue
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            key = (st.st_ino, st.st_mtime_ns, st.st_size)
            hit = old.get(e.path)
            if hit and hit[0] == key:
                sc = hit[1]
            else:
                try:
                    with open(e.path, encoding="utf-8") as f:
                        sc = json.load(f)
                except (OSError, ValueError):
                    continue
                if not isinstance(sc, dict):
                    continue
                sc["root"], sc["name"] = r, fn[:-5]
            cache[e.path] = (key, sc)
            out.append(dict(sc))
    with _lock:
        _index_cache = ((), cache)
    return out


def media_type(name: str) -> str | None:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    return None


def url_for(ref: Ref) -> str:
    return f"/m/{ref[0]}/{urllib.parse.quote(ref[1])}"


def _created(sc: dict) -> str:
    return str(sc.get("created") or "")


def _exists(sc: dict) -> bool:
    try:
        p = resolve((sc["root"], sc["name"]))
    except (BadRef, KeyError):
        return False
    return os.path.isfile(p)


def item(sc: dict) -> dict:
    """The page's view of one visual asset (an entry of /api/recent)."""
    ref = (sc["root"], sc["name"])
    t = media_type(ref[1])
    pr = sc.get("probe") if isinstance(sc.get("probe"), dict) else {}
    poster = sc.get("poster")
    if isinstance(poster, str) and NAME_RE.fullmatch(poster):
        poster_url = url_for((ref[0], poster))
    else:
        poster_url = url_for(ref) if t == "image" else ""
    return {"ref": ref_json(ref), "type": t, "kind": sc.get("kind"), "url": url_for(ref), "poster": poster_url,
            "label": str(sc.get("label") or ""), "w": pr.get("w"), "h": pr.get("h"),
            "seconds": pr.get("seconds"), "created": _created(sc), "commercial_ok": sc.get("commercial_ok")}


def recent(limit: int = 60, kind: str | None = None) -> list[dict]:
    """Owned pictures and videos, newest first; music and audio uploads excluded.
    kind: None | "image" | "video" (clips, extensions, video uploads, exports) | "export"."""
    rows = []
    for sc in index():
        t = media_type(sc["name"])
        if t not in ("image", "video"):
            continue
        if kind == "image" and t != "image":
            continue
        if kind == "video" and t != "video":
            continue
        if kind == "export" and sc.get("kind") != "export":
            continue
        if not _exists(sc):
            continue
        rows.append(sc)
    rows.sort(key=lambda s: (_created(s), s["name"]), reverse=True)
    return [item(s) for s in rows[:max(0, int(limit))]]


def tracks() -> list[dict]:
    """Music (generated) and audio uploads, newest first: [{ref, url, seconds, title, commercial_ok}]."""
    rows = [sc for sc in index() if media_type(sc["name"]) == "audio" and _exists(sc)]
    rows.sort(key=lambda s: (_created(s), s["name"]), reverse=True)
    out = []
    for sc in rows:
        ref = (sc["root"], sc["name"])
        pr = sc.get("probe") if isinstance(sc.get("probe"), dict) else {}
        out.append({"ref": ref_json(ref), "url": url_for(ref), "seconds": pr.get("seconds"),
                    "title": str(sc.get("label") or sc["name"]), "kind": sc.get("kind"),
                    "commercial_ok": sc.get("commercial_ok")})
    return out


# --------------------------------------------------------------------------------------------- names

def stamp() -> str:
    """<YYYYmmdd-HHMMSS>_<6hex> (UTC), as in §3.2 names."""
    return f"{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}_{secrets.token_hex(3)}"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_name(prefix: str, ext: str) -> str:
    """e.g. new_name("cut", "mp4") -> "cut_20261007-210000_a1b2c3.mp4". Unique among existing sidecars."""
    ext = ext.lstrip(".")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,15}", prefix) or not re.fullmatch(r"[A-Za-z0-9]{1,8}", ext):
        raise ValueError("bad name prefix or extension")
    while True:
        name = f"{prefix}_{stamp()}.{ext}"
        if not any(os.path.exists(data_path("sidecars", r, name + ".json")) for r in ROOTS):
            return name


def tmp_path(suffix: str) -> str:
    """A fresh path in DATA/tmp (same mount as DATA/media, so os.replace into it is atomic)."""
    if not re.fullmatch(r"[A-Za-z0-9._-]{0,32}", suffix or ""):
        raise ValueError("bad temp suffix")
    return data_path("tmp", f".ml_{secrets.token_hex(6)}{suffix}")


def _atomic_write(path: str, data: bytes) -> None:
    d = os.path.dirname(path)
    tmp = os.path.join(d, f".{os.path.basename(path)}.{secrets.token_hex(4)}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


atomic_write = _atomic_write


# --------------------------------------------------------------------------------------------- ledger

def _ledger_file() -> str:
    return data_path("ledger.jsonl")


def _ledger_lines() -> list[dict]:
    try:
        with open(_ledger_file(), encoding="utf-8") as f:
            raw = f.readlines()
    except OSError:
        return []
    out = []
    for line in raw:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict) and isinstance(d.get("pid"), str):
            out.append(d)
    return out


def ledger_add(pid: str, kind: str, refs: list[Ref]) -> None:
    """Append {"pid","kind","refs":[[root,name],…],"created"} (refs may be empty for a provisional line)."""
    if not isinstance(pid, str) or not pid or len(pid) > 128:
        raise ValueError("bad prompt id")
    rows = [list(check_ref(r)) for r in refs]
    line = json.dumps({"pid": pid, "kind": kind, "refs": rows, "created": now_iso()}, ensure_ascii=False)
    with _ledger_lock:
        with open(_ledger_file(), "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())


def ledger_pids(ref: Ref) -> list[str]:
    """Prompt ids (first-seen order, unique) whose ledger lines name this ref."""
    ref = check_ref(ref)
    seen: list[str] = []
    with _ledger_lock:
        lines = _ledger_lines()
    for d in lines:
        refs = d.get("refs") or []
        if any(isinstance(r, list) and tuple(r) == ref for r in refs) and d["pid"] not in seen:
            seen.append(d["pid"])
    return seen


def ledger_drop(ref: Ref) -> None:
    """Rewrite the ledger without this ref; a line left with no refs is dropped (temp file + os.replace)."""
    ref = check_ref(ref)
    with _ledger_lock:
        lines = _ledger_lines()
        out, gone = [], set()
        for d in lines:
            refs = d.get("refs") or []
            keep = [r for r in refs if not (isinstance(r, list) and tuple(r) == ref)]
            if len(keep) != len(refs) and not keep:
                gone.add(d.get("pid"))
                continue
            d["refs"] = keep
            out.append(d)
        # comfy.submit's provisional line (no refs) for a pid whose real line just went is dropped too.
        out = [json.dumps(d, ensure_ascii=False) for d in out if d.get("refs") or d.get("pid") not in gone]
        _atomic_write(_ledger_file(), ("\n".join(out) + "\n" if out else "").encode("utf-8"))


# --------------------------------------------------------------------------------------------- delete

def remove(ref: Ref) -> list[str]:
    """Delete file + poster + sidecar of an owned ref; returns the removed file names (sidecar not listed).
    Raises NotOwned for anything without a sidecar. Never touches a file that is not owned."""
    global _index_cache
    ref = require_owned(ref)
    sc = sidecar(ref) or {}
    removed = []
    targets = [ref]
    poster = sc.get("poster")
    if isinstance(poster, str) and NAME_RE.fullmatch(poster) and poster != ref[1]:
        targets.append((ref[0], poster))
    for t in targets:
        p = resolve(t)
        if os.path.isfile(p) and not os.path.islink(p):
            os.remove(p)
            removed.append(t[1])
    os.remove(_sidecar_path(ref))
    with _lock:
        _index_cache = None
    return removed
