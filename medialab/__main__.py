"""`python3 -m medialab [serve|check|migrate]` (§4, §13.5). Owner: WP2.

    serve                      run the server (the default; same as `python3 app.py`)
    check [--dump-fixture DIR] config (token redacted), ComfyUI, features, missing pieces, legacy scan counts;
                               --dump-fixture writes DIR/object_info_<version>.json (GET requests only)
    migrate --dry-run          print the manifest of legacy files (count, bytes, sha256, mtime), adopting nothing
    migrate --verify FILE      compare the files now with a manifest written by --dry-run; exit 1 on any change
    migrate                    adopt legacy files now (the server also does this at start when enabled)
    get-models [SET] --models-dir DIR [--check|--dry-run|--verify]
                               download the stock model files a set needs (pictures, clips, music, all),
                               pinned and sha256-verified (see medialab/getmodels.py)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

from . import VERSION
from .config import Config, ConfigError, describe, load

# Classes the shipped graphs use (§14); --dump-fixture keeps only these.
FIXTURE_CLASSES = (
    "UNETLoader", "CLIPLoader", "DualCLIPLoader", "VAELoader", "TextEncodeQwenImage21", "EmptyLatentImage",
    "EmptySD3LatentImage", "KSampler", "KSamplerAdvanced", "VAEDecode", "SaveImage", "LoadImage", "LoraLoaderModelOnly",
    "ModelSamplingSD3", "ModelSamplingAuraFlow", "CLIPTextEncode", "WanImageToVideo", "VHS_VideoCombine",
    "TextEncodeAceStepAudio1.5", "EmptyAceStep1.5LatentAudio", "ConditioningZeroOut", "VAEDecodeAudio",
    "SaveAudio", "SaveAudioAdvanced",
)
MODEL_INPUTS = ("unet_name", "clip_name", "clip_name1", "clip_name2", "vae_name", "lora_name")


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _setup(cfg: Config) -> None:
    from . import store
    store.configure(cfg, prepare=False)


# --------------------------------------------------------------------------------------------- check

def cmd_check(cfg: Config, dump_fixture: str | None = None) -> int:
    """Print config (token redacted), ComfyUI reachability, features, missing files/nodes/filters and
    legacy scan counts. `--dump-fixture DIR` writes the object_info subset for tests (GET only).
    Returns 0 when ComfyUI answers and every feature works, else 1."""
    _setup(cfg)
    from . import comfy, features, legacy, media, models as models_mod
    for mod in (comfy, media):
        fn = getattr(mod, "configure", None)
        if callable(fn):
            fn(cfg)
    ok = True
    print(f"YourContent {VERSION}")
    print("config:")
    for k, v in describe(cfg).items():
        print(f"  {k}: {v}")

    infos: dict = {}
    version = None
    try:
        stats = comfy.request("/system_stats", timeout=5)
        version = (stats.get("system") or {}).get("comfyui_version")
        print(f"ComfyUI: reachable, version {version}")
        infos = comfy.request("/object_info", timeout=30)
    except NotImplementedError:
        ok = False
        print("ComfyUI: client not built yet")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"ComfyUI: not reachable ({exc.__class__.__name__})")

    try:
        ff = media.ffmpeg_caps()
        print(f"ffmpeg: {ff.get('version')}; missing filters: {', '.join(ff.get('missing_filters') or []) or 'none'}")
    except Exception as exc:  # noqa: BLE001
        ok = False
        ff = {"version": None, "missing_filters": [], "missing_encoders": []}
        print(f"ffmpeg: not available ({exc.__class__.__name__})")

    models = None
    try:
        models = models_mod.load(cfg)
        lf = models.local_file
        print("models.json: " + (f"read {lf['bytes']} bytes, sha256 {lf['sha256']}, modified {lf['mtime']}"
                                  if lf else "none (shipped defaults only)"))
        feats = features.compute(infos, models, ff)
        print("features:")
        for name, f in feats.items():
            missing = f.get("missing") or f.get("missing_filters") or []
            extra = f" (saver {f['saver']})" if f.get("saver") else ""
            todo = features.download_bytes(missing)
            print(f"  {name}: {'ok' if f.get('ok') else 'NOT available'}{extra}"
                  + (f"; missing: {', '.join(map(str, missing))}" if missing else "")
                  + (f"; {todo / 1e9:.2f} GB still to download" if todo else ""))
            ok = ok and bool(f.get("ok"))
            for vid, v in (f.get("variants") or {}).items():
                vm = v.get("missing") or []
                print(f"    {vid}: {'ok' if v.get('ok') else 'missing ' + ', '.join(map(str, vm))}")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"features: can't compute ({exc.__class__.__name__}: {exc})")

    try:
        found = legacy.scan(cfg)
        counts: dict[str, int] = {}
        for item in found:
            counts[item.kind] = counts.get(item.kind, 0) + 1
        posters = sum(1 for item in found if getattr(item, "poster", None))
        summary = ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "none"
        print(f"legacy files: {summary} ({posters} posters); adoption {'on' if cfg.adopt_legacy else 'off'}")
    except Exception as exc:  # noqa: BLE001
        print(f"legacy files: can't scan ({exc.__class__.__name__})")

    if dump_fixture:
        if not infos:
            print("fixture: nothing to dump (ComfyUI not reachable)")
            return 1
        # Shipped defaults only: the owner's DATA/config/models.json names local files that must not
        # end up in a committed fixture.
        from . import models as models_mod
        path = dump_object_info(infos, models_mod.load(None), dump_fixture, version or "unknown")
        print(f"fixture: wrote {os.path.basename(path)}")
    return 0 if ok else 1


def _model_files(models) -> set[str]:
    """Every model filename the model table names (strings ending .safetensors etc.; {} -> high/low)."""
    found: set[str] = set()

    def walk(x):
        if isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                walk(v)
        elif isinstance(x, str) and x.endswith((".safetensors", ".gguf", ".ckpt", ".pt", ".pth", ".bin")):
            if "{}" in x:
                found.update(x.format(side) for side in ("high", "low"))
            else:
                found.add(x)

    if models is None:
        return found
    for attr in ("image", "video", "music"):
        try:
            walk(getattr(models, attr, None))
        except Exception:  # noqa: BLE001
            pass
    try:
        walk(models.image_variants())
    except Exception:  # noqa: BLE001
        pass
    return found


def dump_object_info(infos: dict, models, out_dir: str, version: str) -> str:
    """Write the object_info subset the tests validate graphs against. Model-file enums are reduced to the
    files the model table names; LoadImage's file list is emptied (it lists the machine's own files)."""
    keep = _model_files(models)
    try:
        keep |= set(models.known_files())
    except Exception:  # noqa: BLE001
        pass
    from . import features
    if callable(getattr(features, "fixture_subset", None)):
        subset = features.fixture_subset(infos, keep)
        return _write_fixture(subset, out_dir, version)
    subset = {}
    for cls in FIXTURE_CLASSES:
        if cls not in infos:
            continue
        info = json.loads(json.dumps(infos[cls]))
        for section in ("required", "optional"):
            inputs = (info.get("input") or {}).get(section) or {}
            for name, spec in inputs.items():
                if not (isinstance(spec, list) and spec and isinstance(spec[0], list)):
                    continue
                if name in MODEL_INPUTS:
                    spec[0] = [v for v in spec[0] if v in keep]
                elif cls == "LoadImage" and name == "image":
                    spec[0] = []
        subset[cls] = info
    return _write_fixture(subset, out_dir, version)


def _write_fixture(subset: dict, out_dir: str, version: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    safe_version = "".join(ch for ch in version if ch.isalnum() or ch in ".-") or "unknown"
    path = os.path.join(out_dir, f"object_info_{safe_version}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(subset, f, indent=1, sort_keys=True)
        f.write("\n")
    return path


# --------------------------------------------------------------------------------------------- migrate

def manifest(cfg: Config) -> dict:
    """Legacy files (posters included) with bytes, sha256 and mtime_ns, plus the output folder's entry count.
    Opens files read-only; changes nothing."""
    from . import legacy
    roots = {"out": os.path.abspath(cfg.comfy_output), "in": os.path.abspath(cfg.uploads)}
    files = []
    seen = set()
    for item in legacy.scan(cfg):
        for name in (item.name, getattr(item, "poster", None)):
            if not name or (item.root, name) in seen:
                continue
            seen.add((item.root, name))
            p = os.path.join(roots[item.root], name)
            st = os.stat(p)
            files.append({"root": item.root, "name": name, "kind": item.kind if name == item.name else "poster",
                          "bytes": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": _sha256(p)})
    try:
        # The app adds its own output subfolder on first start; it is not one of the owner's entries.
        entries = sum(1 for e in os.listdir(roots["out"]) if e != cfg.output_subdir)
    except OSError:
        entries = None
    return {"schema": "media-lab/manifest@1", "files": files, "count": len(files),
            "bytes": sum(f["bytes"] for f in files), "output_entries": entries}


def verify(cfg: Config, old: dict) -> list[str]:
    """Differences between a stored manifest and the files now (empty = identical)."""
    roots = {"out": os.path.abspath(cfg.comfy_output), "in": os.path.abspath(cfg.uploads)}
    problems = []
    for f in old.get("files") or []:
        p = os.path.join(roots.get(f.get("root"), "/nonexistent"), str(f.get("name")))
        try:
            st = os.stat(p)
        except OSError:
            problems.append(f"{f.get('name')}: missing")
            continue
        if st.st_size != f.get("bytes"):
            problems.append(f"{f['name']}: size changed")
        if st.st_mtime_ns != f.get("mtime_ns"):
            problems.append(f"{f['name']}: mtime changed")
        if _sha256(p) != f.get("sha256"):
            problems.append(f"{f['name']}: content changed")
    if old.get("output_entries") is not None:
        try:
            now = sum(1 for e in os.listdir(roots["out"]) if e != cfg.output_subdir)
        except OSError:
            now = None
        if now != old["output_entries"]:
            problems.append(f"output folder entries: {old['output_entries']} before, {now} now")
    return problems


def cmd_migrate(cfg: Config, dry_run: bool, verify_path: str | None) -> int:
    if verify_path:
        with open(verify_path, encoding="utf-8") as f:
            old = json.load(f)
        problems = verify(cfg, old)
        for p in problems:
            print(p)
        print(f"verify: {len(old.get('files') or [])} files checked, {len(problems)} differences")
        return 1 if problems else 0
    if dry_run:
        json.dump(manifest(cfg), sys.stdout, indent=1)
        sys.stdout.write("\n")
        return 0
    from . import comfy, legacy, store
    store.configure(cfg)
    fn = getattr(comfy, "configure", None)
    if callable(fn):
        fn(cfg)
    res = legacy.adopt_all(cfg, store, comfy)
    print(f"adopted {len(res.get('adopted') or [])}, skipped {len(res.get('skipped') or [])}")
    return 0


# --------------------------------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m medialab", description="YourContent")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="run the server (default)")
    c = sub.add_parser("check", help="show config, ComfyUI, features and legacy files")
    c.add_argument("--dump-fixture", metavar="DIR")
    m = sub.add_parser("migrate", help="legacy files: manifest, verify or adopt")
    g = m.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--verify", metavar="MANIFEST")
    oa = sub.add_parser("openapi", help="print the HTTP API as OpenAPI 3.1 JSON (docs/openapi.json)")
    oa.add_argument("--md", action="store_true", help="print docs/API.md instead")
    gm = sub.add_parser("get-models", help="download the stock model files a set needs, sha256-verified")
    gm.add_argument("set", nargs="?", default="all", choices=("pictures", "clips", "music", "all"))
    gm.add_argument("--models-dir", metavar="DIR", help="ComfyUI's models/ folder (or YOURCONTENT_MODELS_DIR)")
    gm.add_argument("--models-json", metavar="FILE", help="a models.json to merge (default: the app's own)")
    gm.add_argument("--variant", metavar="ID", help="picture variant (default: the table's default)")
    gx = gm.add_mutually_exclusive_group()
    gx.add_argument("--check", action="store_true", help="print what is missing; exit 1 if anything is")
    gx.add_argument("--dry-run", action="store_true", help="print the plan and stop")
    gm.add_argument("--verify", action="store_true", help="also re-hash files already present")
    args = ap.parse_args(argv)
    if args.cmd == "openapi":
        import json
        from . import openapi, server
        spec = openapi.build(server.ROUTES)
        print(openapi.to_markdown(spec) if args.md else json.dumps(spec, indent=1), end="" if args.md else "\n")
        return 0
    if args.cmd == "get-models":
        from . import getmodels
        return getmodels.main(args)
    if args.cmd in (None, "serve"):
        from .server import main as serve
        serve()
        return 0
    try:
        cfg = load()
    except ConfigError as exc:
        print(f"yourcontent: {exc}", file=sys.stderr)
        return 2
    if args.cmd == "check":
        return cmd_check(cfg, args.dump_fixture)
    return cmd_migrate(cfg, args.dry_run, args.verify)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
