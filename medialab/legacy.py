"""Idempotent adoption of pre-v1 files (§13). Owner: WP3. Opens owner files read-only; never moves them.

Adoption only adds sidecars (and ledger lines) under DATA. Nothing in ComfyUI's folders is renamed, moved,
rewritten or deleted, and file mtimes are untouched. It runs at every start when YOURCONTENT_ADOPT_LEGACY=1 and
skips anything that already has a sidecar, so running it twice changes nothing.

Lineage comes from the graph ComfyUI embedded in each file: a PNG tEXt/iTXt `prompt` chunk, an MP4 `prompt`
metadata tag, or (for an extension, whose mp4 has none) the poster's PNG chunk. A clip's start picture is
resolved to that picture's own sidecar, so pictures are adopted before clips.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import struct
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone

from . import provenance

# (pattern id, root, regex, kind), in adoption (dependency) order: uploads and pictures before the clips that
# may start from them. §13.3.
PATTERNS: tuple[tuple[str, str, re.Pattern, str], ...] = (
    ("ref", "in", re.compile(r"^ref_\d{8}-\d{6}_[0-9a-f]{6}\.(png|jpe?g|webp)$"), "upload"),
    ("qwen_studio", "out", re.compile(r"^qwen_studio_\d{5}_\.png$"), "image"),
    ("qwen_video", "out", re.compile(r"^qwen_video_\d{5}\.mp4$"), "clip"),
    ("qwen_video_ext", "out", re.compile(r"^qwen_video_ext_\d{8}-\d{6}_[0-9a-f]{6}\.mp4$"), "extension"),
)
LEDGER_KIND = {"image": "image", "clip": "clip"}
MAX_PNG_TEXT = 8 << 20      # ignore absurd text chunks
MAX_MP4_META = 16 << 20     # the moov box we are willing to read


@dataclass(frozen=True)
class Legacy:
    """One scanned legacy file."""
    pattern: str            # PATTERNS id
    root: str
    name: str
    kind: str
    poster: str | None      # same-stem .png for clips/extensions, when present


def _root_dirs(cfg) -> dict[str, str]:
    return {"out": os.path.abspath(cfg.comfy_output), "in": os.path.abspath(cfg.uploads)}


def _regular(path: str) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode)


def scan(cfg) -> list[Legacy]:
    """Every legacy file the strict patterns match, in adoption order. Read-only: lists folders, nothing else.
    Symlinks and folders are ignored; a poster .png is never a picture of its own."""
    dirs = _root_dirs(cfg)
    listing: dict[str, list[str]] = {}
    for root, d in dirs.items():
        try:
            listing[root] = sorted(os.listdir(d))
        except OSError:
            listing[root] = []
    out: list[Legacy] = []
    for pid, root, rx, kind in PATTERNS:
        names = set(listing[root])
        for name in listing[root]:
            if not rx.fullmatch(name) or not _regular(os.path.join(dirs[root], name)):
                continue
            poster = None
            if kind in ("clip", "extension"):
                cand = name[:-4] + ".png"
                if cand in names and _regular(os.path.join(dirs[root], cand)):
                    poster = cand
            out.append(Legacy(pid, root, name, kind, poster))
    return out


# --------------------------------------------------------------------------------------------- metadata

def _png_chunks(f):
    if f.read(8) != b"\x89PNG\r\n\x1a\n":
        return
    while True:
        hdr = f.read(8)
        if len(hdr) < 8:
            return
        n, kind = struct.unpack(">I4s", hdr)
        if kind in (b"tEXt", b"iTXt", b"zTXt", b"IHDR") and n <= MAX_PNG_TEXT:
            data = f.read(n)
            f.seek(4, 1)
            yield kind, data
        else:
            f.seek(n + 4, 1)
        if kind == b"IEND":
            return


def _png_texts(path: str) -> dict[str, str]:
    texts: dict[str, str] = {}
    with open(path, "rb") as f:
        for kind, data in _png_chunks(f):
            try:
                if kind == b"tEXt":
                    k, _, v = data.partition(b"\x00")
                    texts.setdefault(k.decode("latin-1"), v.decode("latin-1"))
                elif kind == b"zTXt":
                    k, _, rest = data.partition(b"\x00")
                    texts.setdefault(k.decode("latin-1"), zlib.decompress(rest[1:]).decode("latin-1"))
                elif kind == b"iTXt":
                    k, _, rest = data.partition(b"\x00")
                    comp, _method = rest[0], rest[1]
                    _lang, _, rest = rest[2:].partition(b"\x00")
                    _tk, _, text = rest.partition(b"\x00")
                    if comp:
                        text = zlib.decompress(text)
                    texts.setdefault(k.decode("latin-1"), text.decode("utf-8"))
            except (ValueError, IndexError, zlib.error, UnicodeError):
                continue
    return texts


def png_size(path: str) -> tuple[int, int] | None:
    try:
        with open(path, "rb") as f:
            for kind, data in _png_chunks(f):
                if kind == b"IHDR" and len(data) >= 8:
                    return struct.unpack(">II", data[:8])
                break
    except OSError:
        return None
    return None


def _graph(text: str | None) -> dict | None:
    if not text:
        return None
    try:
        g = json.loads(text)
    except ValueError:
        return None
    return g if isinstance(g, dict) and g else None


def read_png_prompt(path: str) -> dict | None:
    """The ComfyUI graph in a PNG's `prompt` text chunk (tEXt, zTXt or iTXt), or None."""
    try:
        return _graph(_png_texts(path).get("prompt"))
    except OSError:
        return None


def _boxes(buf: bytes, off: int, end: int):
    while off + 8 <= end:
        size, typ = struct.unpack(">I4s", buf[off:off + 8])
        hdr = 8
        if size == 1:
            if off + 16 > end:
                return
            size = struct.unpack(">Q", buf[off + 8:off + 16])[0]
            hdr = 16
        elif size == 0:
            size = end - off
        if size < hdr or off + size > end:
            return
        yield typ, off + hdr, off + size
        off += size


def _meta_tags(buf: bytes, start: int, end: int) -> dict[str, str]:
    """Tags in a `meta` box: mdta keys + ilst (what ffmpeg writes for custom keys), and plain ilst atoms."""
    tags: dict[str, str] = {}
    # a meta box is a full box (4 bytes version/flags) in ISO files; QuickTime's is not
    child = (b"hdlr", b"keys", b"ilst")
    body = start if buf[start + 4:start + 8] in child else start + 4
    keys: list[str] = []
    ilst = None
    for typ, s, e in _boxes(buf, body, end):
        if typ == b"keys" and s + 8 <= e:
            count = struct.unpack(">I", buf[s + 4:s + 8])[0]
            p = s + 8
            for _ in range(count):
                if p + 8 > e:
                    break
                ksize = struct.unpack(">I", buf[p:p + 4])[0]
                if ksize < 8:
                    break
                keys.append(buf[p + 8:p + ksize].decode("utf-8", "replace"))
                p += ksize
        elif typ == b"ilst":
            ilst = (s, e)
    if ilst:
        for typ, s, e in _boxes(buf, *ilst):
            idx = struct.unpack(">I", typ)[0]
            name = keys[idx - 1] if 1 <= idx <= len(keys) else typ.decode("latin-1")
            for dtyp, ds, de in _boxes(buf, s, e):
                if dtyp == b"data" and ds + 8 <= de:
                    tags.setdefault(name, buf[ds + 8:de].decode("utf-8", "replace"))
    return tags


def mp4_tags(path: str) -> dict[str, str]:
    tags: dict[str, str] = {}
    with open(path, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        off = 0
        while off + 8 <= size:
            f.seek(off)
            hdr = f.read(16)
            if len(hdr) < 8:
                break
            bsize, typ = struct.unpack(">I4s", hdr[:8])
            if bsize == 1 and len(hdr) >= 16:
                bsize = struct.unpack(">Q", hdr[8:16])[0]
            elif bsize == 0:
                bsize = size - off
            if bsize < 8:
                break
            if typ == b"moov":
                if bsize > MAX_MP4_META:
                    break
                f.seek(off)
                buf = f.read(bsize)
                _collect_moov(buf, tags)
                break
            off += bsize
    return tags


def _collect_moov(buf: bytes, tags: dict[str, str]) -> None:
    for typ, s, e in _boxes(buf, 8, len(buf)):
        if typ == b"meta":
            tags.update({k: v for k, v in _meta_tags(buf, s, e).items() if k not in tags})
        elif typ == b"udta":
            for t2, s2, e2 in _boxes(buf, s, e):
                if t2 == b"meta":
                    tags.update({k: v for k, v in _meta_tags(buf, s2, e2).items() if k not in tags})


def read_mp4_prompt(path: str) -> dict | None:
    """The ComfyUI graph in an MP4's `prompt` metadata tag (VHS writes it), or None."""
    try:
        return _graph(mp4_tags(path).get("prompt"))
    except (OSError, struct.error, ValueError):
        return None


# --------------------------------------------------------------------------------------------- lineage

_LOADERS = (("UNETLoader", "unet_name", "unet"), ("LoraLoaderModelOnly", "lora_name", "lora"),
            ("CLIPLoader", "clip_name", "text_encoder"), ("DualCLIPLoader", "clip_name1", "text_encoder"),
            ("DualCLIPLoader", "clip_name2", "text_encoder"), ("VAELoader", "vae_name", "vae"))


def models_used(graph: dict, models) -> list[tuple[str, str, str, str]]:
    """[(id, role, file, licence id)] for every model file the graph loads; unknown files get `unknown`."""
    used, seen = [], set()
    for nid in sorted(graph, key=lambda k: (len(str(k)), str(k))):
        node = graph[nid] if isinstance(graph[nid], dict) else {}
        for ct, inp, role in _LOADERS:
            if node.get("class_type") != ct:
                continue
            f = (node.get("inputs") or {}).get(inp)
            if not isinstance(f, str) or not f:
                continue
            if role in ("unet", "lora"):
                if "high_noise" in f:
                    role = f"{role}_high"
                elif "low_noise" in f:
                    role = f"{role}_low"
            if (f, role) in seen:
                continue
            seen.add((f, role))
            lid = (models.licence_for_file(f) if models is not None else None) or "unknown"
            mid = models.id_for_file(f) if models is not None else f
            used.append((mid, role, f, lid))
    return used


def _link(graph: dict, value) -> dict | None:
    if isinstance(value, list) and value and str(value[0]) in graph:
        n = graph[str(value[0])]
        return n if isinstance(n, dict) else None
    return None


def _text_of(graph: dict, value, want: int = 0) -> str | None:
    n = _link(graph, value)
    if not n:
        return None
    ct, inp = n.get("class_type"), n.get("inputs") or {}
    if ct == "CLIPTextEncode":
        return inp.get("text") if isinstance(inp.get("text"), str) else None
    if ct == "TextEncodeQwenImage21":
        key = "prompt" if (value[1] if len(value) > 1 else 0) == 0 else "negative_prompt"
        return inp.get(key) if isinstance(inp.get(key), str) else None
    if ct == "WanImageToVideo":
        idx = value[1] if len(value) > 1 else 0
        return _text_of(graph, inp.get("positive" if idx == 0 else "negative"))
    return None


def prompt_of(graph: dict) -> tuple[str | None, str | None, int | None]:
    """(prompt, negative, seed) from the first sampler in the graph."""
    for nid in sorted(graph, key=lambda k: (len(str(k)), str(k))):
        node = graph[nid] if isinstance(graph[nid], dict) else {}
        if node.get("class_type") in ("KSampler", "KSamplerAdvanced"):
            inp = node.get("inputs") or {}
            seed = inp.get("seed", inp.get("noise_seed"))
            return (_text_of(graph, inp.get("positive")), _text_of(graph, inp.get("negative")),
                    seed if isinstance(seed, int) else None)
    return None, None, None


def load_ref(value: str, cfg) -> tuple[str, str] | None:
    """A LoadImage value back to a ref: "<name> [output]" -> out, "<OUTPUT_SUBDIR>/<name> [output]" -> gen,
    "<INPUT_SUBDIR>/<name>" -> in. Anything else (another tool's file) -> None."""
    if not isinstance(value, str):
        return None
    v = value.strip()
    kind = "input"
    m = re.fullmatch(r"(.*?)\s*\[(output|input|temp)\]", v)
    if m:
        v, kind = m.group(1), m.group(2)
    sub, _, name = v.rpartition("/")
    if not name or "\\" in v:
        return None
    if kind == "output":
        if sub == "":
            return ("out", name)
        if sub == cfg.output_subdir or sub == "media_lab":   # media_lab: the output folder before YourContent
            return ("gen", name)
        return None
    if kind == "input" and sub == cfg.input_subdir:
        return ("in", name)
    return None


def start_images(graph: dict, cfg) -> list[tuple[str, tuple[str, str]]]:
    """[(role, ref)]: the LoadImage feeding WanImageToVideo.start_image is "start", others are "ref"."""
    starts = set()
    for node in graph.values():
        if isinstance(node, dict) and node.get("class_type") == "WanImageToVideo":
            v = (node.get("inputs") or {}).get("start_image")
            if isinstance(v, list) and v:
                starts.add(str(v[0]))
    out = []
    for nid in sorted(graph, key=lambda k: (len(str(k)), str(k))):
        node = graph[nid] if isinstance(graph[nid], dict) else {}
        if node.get("class_type") != "LoadImage":
            continue
        ref = load_ref((node.get("inputs") or {}).get("image"), cfg)
        if ref:
            out.append(("start" if str(nid) in starts else "ref", ref))
    return out


# --------------------------------------------------------------------------------------------- adoption

def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _file_facts(path: str) -> dict:
    st = os.stat(path)
    return {"bytes": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": _sha(path)}


def _probe(path: str, kind: str):
    try:
        from . import media
        return provenance.probe_dict(media.probe(path))
    except Exception:  # noqa: BLE001 - adoption never fails on a probe
        pass
    if path.lower().endswith(".png"):
        size = png_size(path)
        if size:
            return {"w": size[0], "h": size[1], "fps": "0/1", "frames": 0, "seconds": 0, "has_audio": False,
                    "audio_rate": 0, "vcodec": "png", "acodec": ""}
    return None


def _history_index(comfy) -> dict | None:
    """{(filename, subfolder): pid} from one read of ComfyUI's /history (None when unavailable)."""
    if comfy is None:
        return None
    fn = getattr(comfy, "history_index", None)
    if callable(fn):
        try:
            return fn()
        except Exception:  # noqa: BLE001
            return None
    return None


def adopt_all(cfg, store, comfy=None, models=None) -> dict:
    """Adopt every legacy file that has no sidecar yet. Returns
    {"adopted": [names], "skipped": [names already owned], "manifest": [per-file facts, posters included]}."""
    if models is None:
        from . import models as models_mod
        models = models_mod.load(cfg)
    dirs = _root_dirs(cfg)
    table = getattr(models, "licences", None)
    hist = _history_index(comfy)
    adopted, skipped, manifest = [], [], []
    for it in scan(cfg):
        path = os.path.join(dirs[it.root], it.name)
        try:
            facts = _file_facts(path)
            poster_facts = _file_facts(os.path.join(dirs[it.root], it.poster)) if it.poster else None
        except OSError:
            continue
        manifest.append({"root": it.root, "name": it.name, "kind": it.kind, "pattern": it.pattern, **facts,
                         "poster": {"name": it.poster, **poster_facts} if it.poster else None})
        ref = (it.root, it.name)
        if store.owned(ref):
            skipped.append(it.name)
            continue
        side = _sidecar_for(it, path, facts, cfg, store, models, table, comfy, hist)
        store.put_sidecar(ref, side)
        for pid in side["comfy"]["prompt_ids"]:
            try:
                store.ledger_add(pid, LEDGER_KIND.get(it.kind, it.kind), [ref])
            except Exception:  # noqa: BLE001
                pass
        adopted.append(it.name)
    return {"adopted": adopted, "skipped": skipped, "manifest": manifest}


def _sidecar_for(it: Legacy, path: str, facts: dict, cfg, store, models, table, comfy, hist) -> dict:
    ref = (it.root, it.name)
    dirs = _root_dirs(cfg)
    graph, source = None, "none"
    if it.kind == "image":
        graph = read_png_prompt(path)
        source = "png:prompt" if graph else "none"
    elif it.kind in ("clip", "extension"):
        graph = read_mp4_prompt(path)
        source = "mp4:prompt" if graph else "none"
        if graph is None and it.poster:
            graph = read_png_prompt(os.path.join(dirs[it.root], it.poster))
            source = "poster:prompt" if graph else "none"
    used: list = []
    inputs: list = []
    prompt = negative = seed = None
    if it.kind == "upload":
        inputs = [provenance.upload_entry(ref)]
    elif not graph:
        inputs = [provenance.origin_unknown()]
    else:
        used = models_used(graph, models)
        prompt, negative, seed = prompt_of(graph)
        for role, r in start_images(graph, cfg):
            if r == ref:
                continue
            inputs.append(provenance.input_entry(role, r, store.sidecar(r) if store.owned(r) else None))
    pids = []
    if it.kind in LEDGER_KIND and it.root == "out":
        pid = None
        if hist is not None:
            pid = hist.get((it.name, ""))
        elif comfy is not None:
            try:
                pid = comfy.history_find_output(it.name, "")
            except Exception:  # noqa: BLE001
                pid = None
        if pid:
            pids.append(pid)
    try:
        created = _iso(os.stat(path).st_mtime)
    except OSError:
        created = None
    label = prompt or it.name
    return provenance.make_sidecar(
        it.kind, ref, models=provenance.model_entries(models, used), inputs=inputs, prompt=prompt,
        negative=negative, seed=seed, params={}, pids=pids, probe=_probe(path, it.kind),
        sha256=facts["sha256"], nbytes=facts["bytes"], poster=it.poster, label=label,
        legacy={"adopted": provenance.now_iso(), "pattern": it.pattern, "recovered_from": source},
        created=created, table=table)
