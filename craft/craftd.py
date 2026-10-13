"""craftd: runs craft jobs one at a time, each app inside the bubblewrap sandbox (craft-mcp).

The app validates a request and writes DATA/craft/queue/<id>.json. craftd polls that folder, claims a job by
renaming it to DATA/craft/jobs/<id>/job.json, validates it again (medialab.craft_spec), copies its inputs into a
fresh sandbox folder DATA/craft/work/<id>/in under digit-free names, runs the recipe (recipes.py) with the app
inside craft-mcp (no network, only that folder writable), and moves the allowed outputs into the "lab" root with
a provenance sidecar each (medialab.provenance: the job inherits the worst licence of its inputs; an upload with
no sidecar counts as "rights not checked"). It writes DATA/craft/jobs/<id>/status.json and removes the sandbox
folder. It opens no port: the app and craftd share only the data folder.

Run:  python3 -m craft.craftd            (the craft container's command)
      python3 -m craft.craftd --once     (run what is queued, then exit)
      python3 -m craft.craftd --check    (exit 0 if the heartbeat is fresh: the container health check)
Env:  MEDIA_LAB_DATA / MEDIA_LAB_COMFY_OUTPUT / MEDIA_LAB_UPLOADS (as the app), CRAFT_BIN_DIR or CRAFT_SRC (where
      the <app>-cli binaries are), CRAFT_VERSIONS_DIR (the pinned commits, written at image build),
      CRAFT_WORK_ROOT (sandbox folders, default DATA/craft/work), MEDIA_LAB_CRAFT_AI_MASKS=1 and
      MEDIA_LAB_CRAFT_SAM3_DIR (opt-in: LightCraft's SAM 3 model folder; Meta's SAM License, off by default).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from medialab import config as config_mod, craft_spec, provenance, store

APPS = ("photocraft", "lightcraft", "filmcraft", "effectcraft")
PREFIX = {"photo.grade": "grade", "photo.finish": "finish", "video.titlecard": "title", "video.reel": "reel"}
LABEL = {"photo.grade": "Graded photo", "photo.finish": "Finished photo", "video.titlecard": "Title card",
         "video.reel": "Reel"}
KEEP_JOBS = 500          # job folders (status + job.json) kept; finished files live in the lab root, not here
POLL = 1.0
HEARTBEAT_EVERY = 10
FRESH = 45               # seconds: an older heartbeat means the service is down


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------------------------- places

def craft_dir() -> Path:
    return Path(store.data_path("craft"))


def queue_dir() -> Path:
    return craft_dir() / "queue"


def jobs_dir() -> Path:
    return craft_dir() / "jobs"


def fonts_dir() -> Path:
    return craft_dir() / "fonts"


def work_root() -> Path:
    return Path(os.environ.get("CRAFT_WORK_ROOT") or craft_dir() / "work")


def binary(app: str) -> Path:
    if os.environ.get("CRAFT_BIN_DIR"):
        return Path(os.environ["CRAFT_BIN_DIR"]) / f"{app}-cli"
    if os.environ.get("CRAFT_SRC"):
        return Path(os.environ["CRAFT_SRC"]) / app / "target" / "release" / f"{app}-cli"
    return Path("/opt/craft/bin") / f"{app}-cli"


def _commit(app: str) -> str | None:
    vdir = os.environ.get("CRAFT_VERSIONS_DIR")
    if vdir:
        try:
            with open(os.path.join(vdir, f"{app}.json"), encoding="utf-8") as f:
                return str(json.load(f).get(app) or "")[:7] or None
        except (OSError, ValueError, AttributeError):
            return None
    src = os.environ.get("CRAFT_SRC")
    if src:
        head = Path(src) / app / ".git" / "HEAD"
        try:
            ref = head.read_text().strip()
            return ((head.parent / ref[5:]).read_text().strip() if ref.startswith("ref: ") else ref)[:7] or None
        except OSError:
            return None
    return None


def app_versions() -> dict:
    return {a: {"built": os.access(binary(a), os.X_OK), "commit": _commit(a)} for a in APPS}


def masks_settings() -> dict:
    on = craft_spec.ai_masks_enabled()
    sam = os.environ.get("MEDIA_LAB_CRAFT_SAM3_DIR") or None
    present = bool(on and sam and os.path.isfile(os.path.join(sam, "model.safetensors")))
    return {"enabled": on, "model_present": present, "sam3_dir": sam if on else None,
            "licence": craft_spec.SAM_LICENCE}


def write_json(path, obj):
    """Write in place, not tmp + rename: with some Docker file sharing a rename over an existing name keeps serving
    the old file to the other side, so status.json and the heartbeat would look frozen. A reader can catch a
    half-written file; the app retries on bad JSON."""
    data = json.dumps(obj, indent=1).encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        os.write(fd, data)
        os.ftruncate(fd, len(data))
        os.fsync(fd)
    finally:
        os.close(fd)


def heartbeat(state, current=None):
    m = masks_settings()
    fonts = fonts_dir()
    n_fonts = sum(1 for p in fonts.iterdir() if p.suffix.lower() in (".ttf", ".otf")) if fonts.is_dir() else 0
    write_json(craft_dir() / "craftd.json", {
        "pid": os.getpid(), "at": now(), "epoch": time.time(), "state": state, "job": current,
        "apps": app_versions(), "fonts": n_fonts,
        "ai_masks": {"enabled": m["enabled"], "model_present": m["model_present"], "licence": m["licence"]}})


# --------------------------------------------------------------------------------------------- provenance

def input_entries(inputs) -> tuple[list[dict], int]:
    """provenance input entries for a job's inputs, and how many of them are uploads (rights not checked)."""
    entries = []
    for i in inputs:
        ref = (i["ref"][0], i["ref"][1])
        sc = store.sidecar(ref) if store.owned(ref) else None
        if sc is None:   # an upload with no sidecar (only the "in" root allows this): rights not checked
            entries.append(provenance.upload_entry(ref))
        else:
            entries.append(provenance.input_entry(i["role"], ref, sc))
    return entries, sum(1 for e in entries if "upload" in (e.get("licences") or []))


def _sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _publish(src: Path, ref) -> None:
    """Copy a finished file into its root: temp file in DATA/tmp, then rename (same disk as the lab root)."""
    tmp = store.tmp_path(src.suffix)
    shutil.copyfile(src, tmp)
    os.replace(tmp, store.resolve(ref))


# --------------------------------------------------------------------------------------------- one job

def run_job(jid, runner=None):
    jdir = jobs_dir() / jid
    status = {"id": jid, "state": "running", "startedAt": now(), "warnings": [], "outputs": [], "steps": []}
    work = work_root() / jid
    try:
        raw = json.loads((jdir / "job.json").read_text())
        status.update(recipe=raw.get("recipe"), queuedAt=raw.get("queuedAt"), by=raw.get("by"))
        write_json(jdir / "status.json", status)
        job = craft_spec.validate({k: raw.get(k) for k in ("recipe", "params", "brand")})
        job["id"] = jid
        app = craft_spec.APP_OF[job["recipe"]]
        if not os.access(binary(app), os.X_OK):
            raise RuntimeError(f"{craft_spec.APP_NAMES[app]} is not installed in the craft service "
                               "(build the craft image, see craft/README.md)")
        shutil.rmtree(work, ignore_errors=True)
        (work / "in").mkdir(parents=True)
        (work / "out").mkdir()
        inp, letters = {}, "abcdefghijklmnopqrstuvwxyz"
        for n, i in enumerate(job["inputs"]):
            ref, path, ext, _ = craft_spec.resolve(i["ref"], i["role"])
            name = f"in-{letters[n // 26] if n >= 26 else ''}{letters[n % 26]}.{ext}"  # no digits: see recipes.py
            shutil.copyfile(path, work / "in" / name)
            inp[craft_spec.ref_key(ref)] = work / "in" / name
        masks = masks_settings()
        fonts = fonts_dir()
        ctx = SimpleNamespace(work=work, inp=inp, out=work / "out", brand=job["brand"] or dict(craft_spec.NEUTRAL_KIT),
                              fonts=fonts if fonts.is_dir() else None, warn=status["warnings"], sessions=[], meta={},
                              ai_masks=masks["enabled"], sam3_dir=masks["sam3_dir"])
        entries, n_up = input_entries(job["inputs"])
        agg = provenance.aggregate([], entries)
        t0 = time.monotonic()
        if runner is None:
            from . import recipes
            runner = recipes.RUN[job["recipe"]]
        try:
            files = runner(job, ctx)
        finally:
            for s in ctx.sessions:
                status["steps"] += s.log
                s.close(kill=True)
        status["outputs"] = finish_outputs(jid, job, ctx, files, entries)
        status.update(state="done", seconds=round(time.monotonic() - t0, 1), finishedAt=now(),
                      provenance={k: agg[k] for k in ("commercial_ok", "most_restrictive", "restricted_by")})
        status["warnings"] = provenance.warnings(agg, n_up) + status["warnings"]
    except Exception as e:  # noqa: BLE001 - a job failure is a status, not a crash
        status.update(state="failed", error=store.scrub(f"{type(e).__name__}: {e}")[:600], finishedAt=now())
        if not isinstance(e, (craft_spec.SpecError, RuntimeError)):
            status["trace"] = store.scrub(traceback.format_exc()[-1500:])
    finally:
        shutil.rmtree(work, ignore_errors=True)
    write_json(jdir / "status.json", status)
    return status


def finish_outputs(jid, job, ctx, files, entries) -> list[dict]:
    """Check the recipe's outputs, move them into the lab root and write their sidecars. Main file first; a PNG
    after a video is its poster (same stem); a PSD is its own downloadable file with the same provenance."""
    if not files:
        raise RuntimeError("the recipe produced no output")
    for name in files:
        src = ctx.out / name
        if not isinstance(name, str) or not craft_spec.OUT_FILE.fullmatch(name) or src.is_symlink() or not src.is_file():
            raise RuntimeError(f"recipe output {name} is missing or not allowed")
    main = files[0]
    main_ext = main.rsplit(".", 1)[1]
    ref = ("lab", store.new_name(PREFIX[job["recipe"]], main_ext))
    stem = ref[1].rsplit(".", 1)[0]
    is_video = main_ext in ("mp4", "mov")
    plan = [(main, ref, "main")]
    for name in files[1:]:
        ext = name.rsplit(".", 1)[1]
        role = "poster" if is_video and ext == "png" else "psd" if ext == "psd" else None
        if role is None:
            raise RuntimeError(f"recipe output {name} is not expected after {main}")
        plan.append((name, ("lab", f"{stem}.{ext}"), role))
    poster = next((r[1] for _, r, role in plan if role == "poster"), None)
    app = craft_spec.APP_OF[job["recipe"]]
    tool = {"app": craft_spec.APP_NAMES[app], "licence": craft_spec.APP_LICENCE, "source": craft_spec.APP_SOURCE,
            "commit": _commit(app), "recipe": job["recipe"], "job": jid}
    params = {"recipe": job["recipe"], **{k: v for k, v in job["params"].items() if k not in ("items",)}}
    if job["recipe"] == "video.reel":
        params["items"] = len(job["params"]["items"])
    if job.get("brand"):
        params["brand"] = job["brand"].get("name") or "inline"
    out = []
    for name, r, role in plan:
        src = ctx.out / name
        _publish(src, r)
        ext = name.rsplit(".", 1)[1]
        info = {"ref": store.ref_json(r), "url": store.url_for(r), "role": role, "mime": craft_spec.MIME[ext],
                "bytes": src.stat().st_size}
        probe = None
        if ext in ("jpg", "png"):
            from .recipes import image_size
            size = image_size(src)
            if size:
                info["w"], info["h"] = size
                probe = {"w": size[0], "h": size[1]}
        elif name in ctx.meta:
            probe = {k: ctx.meta[name][k] for k in ("w", "h", "seconds") if k in ctx.meta[name]}
            info.update(probe)
        if role != "poster":
            side = provenance.make_sidecar(
                "craft", r, models=[], inputs=entries, params=params, sha256=_sha256(src), nbytes=info["bytes"],
                probe=probe, poster=poster if role == "main" else None,
                label=f"{LABEL[job['recipe']]}{' (layered PSD)' if role == 'psd' else ''}")
            side["tool"] = tool
            store.put_sidecar(r, side)
        out.append(info)
    return out


# --------------------------------------------------------------------------------------------- queue

def claim():
    """Oldest queued job, moved to jobs/<id>/job.json (atomic rename), or None."""
    q_dir = queue_dir()
    if not q_dir.is_dir():
        return None
    for q in sorted(q_dir.glob("c-*.json"), key=lambda p: p.stat().st_mtime):
        jid = q.stem
        if not craft_spec.JOB_ID.fullmatch(jid) or q.is_symlink():
            q.unlink(missing_ok=True)
            continue
        jdir = jobs_dir() / jid
        try:
            jdir.mkdir(parents=True)
        except FileExistsError:
            q.unlink(missing_ok=True)
            continue
        try:
            os.replace(q, jdir / "job.json")
        except FileNotFoundError:
            shutil.rmtree(jdir, ignore_errors=True)
            continue
        return jid
    return None


def prune():
    done = []
    for d in jobs_dir().iterdir() if jobs_dir().is_dir() else []:
        try:
            st = json.loads((d / "status.json").read_text())
        except (OSError, ValueError):
            continue
        if st.get("state") in ("done", "failed"):
            done.append((d.stat().st_mtime, d))
    done.sort()
    while len(done) > KEEP_JOBS:
        shutil.rmtree(done.pop(0)[1], ignore_errors=True)


def recover():
    """A job left 'running' by a crash or restart is marked failed (it is never re-run silently)."""
    for st in jobs_dir().glob("*/status.json") if jobs_dir().is_dir() else []:
        try:
            s = json.loads(st.read_text())
        except (OSError, ValueError):
            continue
        if s.get("state") == "running":
            s.update(state="failed", error="the craft service restarted while this job was running", finishedAt=now())
            write_json(st, s)
    shutil.rmtree(work_root(), ignore_errors=True)


def configure(env=None):
    cfg = config_mod.load(os.environ if env is None else env)
    store.configure(cfg)
    for d in (queue_dir(), jobs_dir()):
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
    work_root().mkdir(parents=True, exist_ok=True, mode=0o700)
    return cfg


def check() -> int:
    """0 when the heartbeat is fresh (the container health check), 1 otherwise."""
    try:
        configure_store_only()
        with open(craft_dir() / "craftd.json", encoding="utf-8") as f:
            hb = json.load(f)
        return 0 if time.time() - float(hb.get("epoch") or 0) < FRESH else 1
    except Exception:  # noqa: BLE001
        return 1


def configure_store_only():
    store.configure(config_mod.load(os.environ), prepare=False)


def main(once=False):
    configure()
    recover()
    print(f"craftd: data {store.scrub(str(craft_dir()))}, apps {app_versions()}, "
          f"AI masks {'on' if craft_spec.ai_masks_enabled() else 'off'}", flush=True)
    last_beat = 0.0
    while True:
        if time.time() - last_beat > HEARTBEAT_EVERY:
            heartbeat("idle")
            last_beat = time.time()
        jid = claim()
        if jid:
            heartbeat("running", jid)
            st = run_job(jid)
            print(f"craftd: {jid} {st.get('recipe')} {st['state']} {st.get('seconds', '')} {st.get('error', '')}",
                  flush=True)
            prune()
            last_beat = 0.0
            continue
        if once:
            heartbeat("idle")
            return
        time.sleep(POLL)


if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(check())
    main(once="--once" in sys.argv)
