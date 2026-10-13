"""`python3 -m medialab get-models [pictures|clips|music|all] --models-dir DIR` (MAP.md: models). Standard library only.

Which files a set needs comes from the set's real graph (the same graphs `features.compute()` checks), built from
the merged model table, so the downloader and the health check can't disagree. `models.manifest.json` only says
where each known file comes from (repo, pinned revision, path), its size and its sha256.

    --check     print what is missing per set, with sizes; exit 1 if anything is missing
    --dry-run   print the plan (sizes, free space) and stop
    --verify    also re-hash files that are already present (reads every byte)

Rules: a file is streamed to DIR/<folder>/<name>.part (resumed with Range), hashed as it is written, and moved into
place only when its byte count and sha256 both match the manifest. A mismatch deletes the .part and exits 1. A
finished file is never deleted or overwritten, nothing is written outside DIR, and ComfyUI is never touched.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request

from . import graphs
from .config import env as config_env

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST_PATH = os.path.join(HERE, "models.manifest.json")
SCHEMA = "media-lab/models-manifest@1"
SETS = ("pictures", "clips", "music")
FEATURE_SET = {"picture": "pictures", "clip": "clips", "extend": "clips", "music": "music"}
FOLDERS = frozenset(graphs.MODEL_INPUTS.values())
CHUNK = 1 << 20
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")


class ManifestError(ValueError):
    pass


class VerifyError(RuntimeError):
    pass


# ------------------------------------------------------------------------------------------------ manifest

def parse_manifest(data: dict) -> dict:
    """Validate a manifest dict and return it. Raises ManifestError naming the first problem."""
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise ManifestError(f"schema must be {SCHEMA}")
    repos = data.get("repos")
    files = data.get("files")
    if not isinstance(repos, dict) or not isinstance(files, dict):
        raise ManifestError("repos and files must be objects")
    for rid, r in repos.items():
        if not isinstance(r, dict) or not isinstance(r.get("id"), str) or not re.fullmatch(r"[\w.-]+/[\w.-]+", r["id"]):
            raise ManifestError(f"repo {rid}: id must be owner/name")
        if not isinstance(r.get("rev"), str) or not re.fullmatch(r"[0-9a-f]{40}", r["rev"]):
            raise ManifestError(f"repo {rid}: rev must be a full 40-hex commit")
    for name, f in files.items():
        if not _NAME.match(name) or ".." in name:
            raise ManifestError(f"{name}: not a plain file name")
        if not isinstance(f, dict):
            raise ManifestError(f"{name}: entry must be an object")
        if f.get("repo") not in repos:
            raise ManifestError(f"{name}: unknown repo {f.get('repo')!r}")
        if f.get("folder") not in FOLDERS:
            raise ManifestError(f"{name}: folder must be one of {sorted(FOLDERS)}")
        path = f.get("path")
        if not isinstance(path, str) or path.startswith("/") or ".." in path.split("/") or path.rsplit("/", 1)[-1] != name:
            raise ManifestError(f"{name}: path must be relative and end in the file name")
        if not isinstance(f.get("bytes"), int) or isinstance(f.get("bytes"), bool) or f["bytes"] <= 0:
            raise ManifestError(f"{name}: bytes must be a positive whole number")
        if not isinstance(f.get("sha256"), str) or not _HEX64.match(f["sha256"]):
            raise ManifestError(f"{name}: sha256 must be 64 lowercase hex digits")
    return data


def load_manifest(path: str | None = None) -> dict:
    with open(path or MANIFEST_PATH, encoding="utf-8") as f:
        try:
            data = json.load(f)
        except ValueError as exc:
            raise ManifestError(f"not JSON: {exc}") from None
    return parse_manifest(data)


@functools.lru_cache(maxsize=1)
def manifest() -> dict:
    """The shipped manifest (cached). {} files when it can't be read, so health checks never break on it."""
    try:
        return load_manifest()
    except (OSError, ManifestError):
        return {"schema": SCHEMA, "repos": {}, "files": {}}


def url_for(man: dict, name: str, endpoint: str | None = None) -> str:
    f = man["files"][name]
    repo = man["repos"][f["repo"]]
    base = (endpoint or os.environ.get("HF_ENDPOINT") or man.get("endpoint") or "https://huggingface.co").rstrip("/")
    return f"{base}/{repo['id']}/resolve/{repo['rev']}/{urllib.parse.quote(f['path'])}"


def gb(n: int) -> str:
    return f"{n / 1e9:.2f} GB"


def hint(name: str, set_name: str | None) -> str | None:
    """The health-check suffix for a known file: ', 14.23 GB) · get it: …'. None for a file not in the manifest."""
    f = manifest()["files"].get(name)
    if not f:
        return None
    cmd = f"python3 -m medialab get-models {set_name}" if set_name else "python3 -m medialab get-models"
    return f"{gb(f['bytes'])}) · get it: {cmd}"


def bytes_of(name: str) -> int:
    f = manifest()["files"].get(name)
    return int(f["bytes"]) if f else 0


# ------------------------------------------------------------------------------------------------ which files

def wanted(models, sets, variant: str | None = None) -> dict[str, dict[str, set[str]]]:
    """{set: {folder: {file}}} from each set's real graph, built from the merged model table."""
    out: dict[str, dict[str, set[str]]] = {}
    for s in sets:
        if s == "pictures":
            if variant and variant not in models.image_variants():
                raise ValueError(f"no picture variant {variant!r} (have: {', '.join(models.image_variants())})")
            vid = variant or models.default_variant
            g = graphs.image_graph(models.variant(vid), "test", "", 1024, 1024, 4, 1, ["example.png"], "yourcontent/img")
        elif s == "clips":
            g = graphs.video_graph(models.video, "example.png", "test", 832, 480, 49, 1, "yourcontent/clip")
        elif s == "music":
            g = graphs.music_graph(models.music, "test", "", 120, 10.0, 1, saver="SaveAudio")
        else:
            raise ValueError(f"unknown set {s!r}")
        out[s] = graphs.requirements(g)["models"]
    return out


def _target(models_dir: str, folder: str, name: str) -> str:
    if folder not in FOLDERS or not _NAME.match(name) or ".." in name:
        raise ValueError(f"refusing path {folder}/{name}")
    root = os.path.realpath(models_dir)
    p = os.path.realpath(os.path.join(root, folder, name))
    if os.path.dirname(os.path.dirname(p)) != root:
        raise ValueError(f"refusing path outside the models folder: {folder}/{name}")
    return p


def plan(man: dict, want: dict[str, dict[str, set[str]]], models_dir: str) -> list[dict]:
    """One row per file: {set, folder, name, bytes, state, have} with state present | partial | missing |
    wrong-size (present but not the manifest's size; never touched) | yours (not in the manifest)."""
    rows, seen = [], set()
    for s, folders in want.items():
        for folder in sorted(folders):
            for name in sorted(folders[folder]):
                if name in seen:
                    continue
                seen.add(name)
                f = man["files"].get(name)
                if not f:
                    rows.append({"set": s, "folder": folder, "name": name, "bytes": 0, "state": "yours", "have": 0})
                    continue
                mfolder = f["folder"]
                p = _target(models_dir, mfolder, name)
                if os.path.isfile(p):
                    size = os.path.getsize(p)
                    state = "present" if size == f["bytes"] else "wrong-size"
                    have = size
                elif os.path.isfile(p + ".part"):
                    state, have = "partial", os.path.getsize(p + ".part")
                else:
                    state, have = "missing", 0
                rows.append({"set": s, "folder": mfolder, "name": name, "bytes": f["bytes"], "state": state, "have": have})
    return rows


def to_fetch(rows: list[dict]) -> int:
    return sum(r["bytes"] - min(r["have"], r["bytes"]) for r in rows if r["state"] in ("missing", "partial"))


def _free(models_dir: str) -> int:
    p = os.path.abspath(models_dir)
    while not os.path.isdir(p):
        p = os.path.dirname(p)
    return shutil.disk_usage(p).free


# ------------------------------------------------------------------------------------------------ fetch

class _NoAuthAcrossHosts(urllib.request.HTTPRedirectHandler):
    """Follow redirects (the hub sends files to a CDN) but never carry the token to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            new.remove_header("Authorization")
        return new


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(man: dict, name: str, models_dir: str, *, endpoint: str | None = None, token: str | None = None,
          progress=None, timeout: float = 60) -> str:
    """Download one manifest file into models_dir/<folder>/<name>. Returns the final path.
    Raises VerifyError (the .part is deleted) on a size or sha256 mismatch; FileExistsError if the file is there."""
    f = man["files"][name]
    final = _target(models_dir, f["folder"], name)
    if os.path.exists(final):
        raise FileExistsError(final)
    os.makedirs(os.path.dirname(final), exist_ok=True)
    part = final + ".part"
    h = hashlib.sha256()
    have = 0
    if os.path.isfile(part):
        have = os.path.getsize(part)
        if have > f["bytes"]:
            os.remove(part)
            have = 0
        else:
            with open(part, "rb") as pf:
                for chunk in iter(lambda: pf.read(CHUNK), b""):
                    h.update(chunk)
    if have < f["bytes"]:
        req = urllib.request.Request(url_for(man, name, endpoint), headers={"User-Agent": "yourcontent-get-models"})
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        if have:
            req.add_header("Range", f"bytes={have}-")
        opener = urllib.request.build_opener(_NoAuthAcrossHosts())
        with opener.open(req, timeout=timeout) as resp:
            mode = "ab"
            if have and getattr(resp, "status", 200) != 206:   # the server ignored Range: start over
                h, have, mode = hashlib.sha256(), 0, "wb"
            with open(part, mode) as out:
                while True:
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    if have + len(chunk) > f["bytes"]:
                        out.close()
                        os.remove(part)
                        raise VerifyError(f"{name}: server sent more than {f['bytes']} bytes")
                    out.write(chunk)
                    h.update(chunk)
                    have += len(chunk)
                    if progress:
                        progress(name, have, f["bytes"])
    if have != f["bytes"]:
        raise VerifyError(f"{name}: got {have} of {f['bytes']} bytes (run again to resume)")
    if h.hexdigest() != f["sha256"]:
        os.remove(part)
        raise VerifyError(f"{name}: sha256 mismatch (got {h.hexdigest()}, want {f['sha256']}); partial file deleted")
    if os.path.exists(final):   # appeared while we downloaded: keep the one that is there
        raise FileExistsError(final)
    os.replace(part, final)
    return final


# ------------------------------------------------------------------------------------------------ command

def _print_plan(rows: list[dict], models_dir: str) -> None:
    for r in rows:
        if r["state"] == "yours":
            print(f"  {r['set']:8} {r['folder']}/{r['name']}: yours, not in the manifest (not downloadable)")
            continue
        extra = f" ({gb(r['have'])} so far)" if r["state"] == "partial" else ""
        if r["state"] == "wrong-size":
            extra = f" (on disk {r['have']} bytes, manifest {r['bytes']}; left as it is)"
        print(f"  {r['set']:8} {r['folder']}/{r['name']}  {gb(r['bytes'])}  {r['state']}{extra}")
    need = to_fetch(rows)
    print(f"to download: {gb(need)}; free in {models_dir}: {gb(_free(models_dir))}")


def run(models, sets, models_dir: str, *, check: bool = False, dry_run: bool = False, verify: bool = False,
        variant: str | None = None, man: dict | None = None, endpoint: str | None = None, out=None) -> int:
    man = man or load_manifest()
    rows = plan(man, wanted(models, sets, variant), models_dir)
    if check:
        missing = [r for r in rows if r["state"] in ("missing", "partial", "wrong-size")]
        for s in sets:
            mine = [r for r in missing if r["set"] == s]
            for r in rows:
                if r["set"] == s and r["state"] == "yours":
                    print(f"{s}: {r['name']} (models/{r['folder']}) is yours, not in the manifest; not checked here")
            if not mine:
                print(f"{s}: all files present")
                continue
            print(f"{s}: missing {len(mine)} files, {gb(to_fetch(mine))} to download")
            for r in mine:
                note = " (wrong size on disk; remove it to re-download)" if r["state"] == "wrong-size" else ""
                print(f"  {r['name']} (models/{r['folder']}, {gb(r['bytes'])}) · get it: "
                      f"python3 -m medialab get-models {s}{note}")
        return 1 if missing else 0
    _print_plan(rows, models_dir)
    if dry_run:
        return 0
    need = to_fetch(rows)
    if need > _free(models_dir):
        print("not enough free space; nothing downloaded", file=sys.stderr)
        return 1
    rc = 0
    if verify:
        for r in rows:
            if r["state"] == "present":
                p = _target(models_dir, r["folder"], r["name"])
                ok = sha256_file(p) == man["files"][r["name"]]["sha256"]
                print(f"verify {r['name']}: {'ok' if ok else 'SHA256 MISMATCH (left as it is)'}")
                rc = rc or (0 if ok else 1)
    token = os.environ.get("HF_TOKEN") or None
    last = {}

    def progress(name, have, total):
        pct = int(have * 100 / total)
        if last.get(name) != pct:
            last[name] = pct
            sys.stdout.write(f"\r  {name}: {pct}%")
            sys.stdout.flush()

    for r in rows:
        if r["state"] not in ("missing", "partial"):
            continue
        try:
            fetch(man, r["name"], models_dir, endpoint=endpoint, token=token,
                  progress=progress if sys.stdout.isatty() else None)
            print(f"\r  {r['name']}: done, sha256 ok")
        except (VerifyError, FileExistsError, urllib.error.URLError, OSError) as exc:
            print(f"\r  {r['name']}: FAILED: {exc}", file=sys.stderr)
            return 1
    return rc


def main(args) -> int:
    from . import models as models_mod
    sets = list(SETS) if args.set in (None, "all") else [args.set]
    models_dir = args.models_dir or config_env("MODELS_DIR")
    if not models_dir:
        print("get-models: --models-dir is required (ComfyUI's models/ folder)", file=sys.stderr)
        return 2
    try:
        if args.models_json:
            models = models_mod.load(None, local_path=args.models_json)
        else:
            cfg = None
            try:
                from .config import load
                cfg = load()
            except Exception:  # noqa: BLE001 - shipped defaults are enough to download
                cfg = None
            models = models_mod.load(cfg)
    except ValueError as exc:
        print(f"get-models: {exc}", file=sys.stderr)
        return 2
    try:
        return run(models, sets, models_dir, check=args.check, dry_run=args.dry_run, verify=args.verify,
                   variant=args.variant)
    except (ManifestError, KeyError, ValueError) as exc:
        print(f"get-models: {exc}", file=sys.stderr)
        return 2
