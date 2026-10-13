"""Craft routes: queue a photo/video recipe for the craft service, report its status and the service's health.

The craft service (craft/craftd.py, its own container) runs the jobs. This module never runs an app and opens no
connection: it writes queue files under DATA/craft/queue and reads what the service writes back (status.json per
job, craftd.json as its heartbeat). Finished files land in the "lab" root with sidecars, so they are served,
listed and deleted like any other YourContent file (/m/lab/<name>, /api/recent).

GET  /api/craft/recipes    recipes, params, looks, the service's health, the AI masks setting
POST /api/craft            {recipe, params, brand?: {colors, fonts, logo, handle} | brand_kit?: <id>} -> 202 queued
GET  /api/craft/<job>      queued / running / done (outputs as refs + URLs, licence summary) / failed (reason)
GET  /api/craft            the service's health, queued and recent jobs

Install with server.install_extension(craft_routes); report health with feature() (never raises).
"""
from __future__ import annotations

import json
import os
import secrets
import time
from datetime import datetime, timezone

from . import craft_spec, store

QUEUE_MAX = 20
FRESH = 45  # seconds; must match craft/craftd.py
NOT_STARTED = "the craft service has not started (run the craft container, see craft/README.md)"


class QueueFull(Exception):
    status = 429


def craft_dir() -> str:
    return store.data_path("craft")


def queue_dir() -> str:
    return os.path.join(craft_dir(), "queue")


def jobs_dir() -> str:
    return os.path.join(craft_dir(), "jobs")


def read_json(path: str, tries: int = 4):
    """The service rewrites status.json and craftd.json in place, so a read can land mid-write: retry."""
    for i in range(tries):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except ValueError:
            if i == tries - 1:
                raise
            time.sleep(0.05)


# --------------------------------------------------------------------------------------------- health

def runner() -> dict:
    """The craft service's state from its heartbeat. Never raises: a problem becomes `reason`."""
    try:
        hb = read_json(os.path.join(craft_dir(), "craftd.json"))
        if not isinstance(hb, dict):
            raise ValueError("not an object")
    except FileNotFoundError:
        return {"up": False, "reason": NOT_STARTED}
    except Exception as exc:  # noqa: BLE001 - unreadable, half-written, wrong shape, store not configured
        return {"up": False, "reason": f"the craft service's heartbeat can't be read ({type(exc).__name__})"}
    try:
        age = time.time() - float(hb.get("epoch") or 0)
    except (TypeError, ValueError):
        age = float("inf")
    out = {"up": age < FRESH, "lastSeen": hb.get("at"), "state": hb.get("state"), "job": hb.get("job"),
           "apps": hb.get("apps") if isinstance(hb.get("apps"), dict) else {}, "fonts": hb.get("fonts"),
           "ai_masks": hb.get("ai_masks") if isinstance(hb.get("ai_masks"), dict) else {"enabled": False}}
    if not out["up"]:
        secs = "a long time" if age == float("inf") else f"{int(age)} s"
        out["reason"] = f"no heartbeat from the craft service for {secs}: is the craft container running?"
    return out


STATIC = {"/static/craft.js": ("craft.js", "text/javascript; charset=utf-8"),
          "/static/craft.css": ("craft.css", "text/css; charset=utf-8")}
FEATURE = "craft"  # /api/health shows feature() under this name (server.EXT_FEATURES)


def feature() -> dict:
    """{ok, reason, recipes: {recipe: {ok, reason}}, ai_masks} for /api/health. Never raises."""
    try:
        run = runner()
        recipes = {}
        for recipe, app in craft_spec.APP_OF.items():
            built = bool((run.get("apps") or {}).get(app, {}).get("built")) if run.get("up") else False
            reason = None if built else (run.get("reason") or f"{craft_spec.APP_NAMES[app]} is not installed in the "
                                                              "craft service")
            recipes[recipe] = {"ok": built, "reason": reason}
        ok = run.get("up", False) and all(r["ok"] for r in recipes.values())
        reason = None if ok else run.get("reason") or next(r["reason"] for r in recipes.values() if not r["ok"])
        masks = dict(run.get("ai_masks") or {"enabled": False})
        masks.setdefault("licence", craft_spec.SAM_LICENCE)
        return {"ok": ok, "reason": reason, "recipes": recipes, "ai_masks": masks}
    except Exception as exc:  # noqa: BLE001 - health must answer
        return {"ok": False, "reason": f"craft status unavailable ({type(exc).__name__})", "recipes": {},
                "ai_masks": {"enabled": False, "licence": craft_spec.SAM_LICENCE}}


# --------------------------------------------------------------------------------------------- queue

def _brand_from_kit(kid):
    """A stored brand kit by id, through medialab.brandkits when this build has it."""
    try:
        from . import brandkits  # noqa: PLC0415 - optional module
    except ImportError:
        raise craft_spec.SpecError("brand kits are not available in this build; send brand {colors, fonts, logo, "
                                   "handle} instead") from None
    getter = getattr(brandkits, "get", None) or getattr(brandkits, "load", None)
    if not callable(getter):
        raise craft_spec.SpecError("brand kits can't be read in this build; send brand inline")
    try:
        kit = getter(kid)
    except (LookupError, ValueError) as exc:
        raise craft_spec.SpecError(f"brand_kit: {exc}") from None
    if not isinstance(kit, dict):
        raise craft_spec.SpecError("brand_kit: not found")
    return kit


def enqueue(body: dict, who: str = "api") -> dict:
    if not isinstance(body, dict):
        raise craft_spec.SpecError("body must be a JSON object")
    req = {"recipe": body.get("recipe"), "params": body.get("params") or {}, "brand": body.get("brand")}
    if req["brand"] is None and body.get("brand_kit") not in (None, ""):
        if not isinstance(body["brand_kit"], str) or len(body["brand_kit"]) > 128:
            raise craft_spec.SpecError("brand_kit must be a brand kit id")
        req["brand"] = _brand_from_kit(body["brand_kit"])
    job = craft_spec.validate(req)
    os.makedirs(queue_dir(), mode=0o700, exist_ok=True)
    waiting = [n for n in os.listdir(queue_dir()) if n.startswith("c-") and n.endswith(".json")]
    if len(waiting) >= QUEUE_MAX:
        raise QueueFull(f"{len(waiting)} craft jobs are waiting; try again later")
    jid = f"c-{datetime.now(timezone.utc):%Y%m%d%H%M%S}-{secrets.token_hex(3)}"
    rec = {"recipe": job["recipe"], "params": req["params"], "brand": req["brand"],
           "queuedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"), "by": who}
    tmp = os.path.join(queue_dir(), f".{jid}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f)
    os.replace(tmp, os.path.join(queue_dir(), f"{jid}.json"))
    out = {"job": jid, "state": "queued", "position": len(waiting) + 1, "status": f"/api/craft/{jid}",
           "inputs": [i["ref"] for i in job["inputs"]]}
    run = runner()
    if not run.get("up"):
        out["warning"] = f"{run.get('reason')}; the job waits in the queue until it runs"
    return out


def status(jid: str) -> dict | None:
    if not craft_spec.JOB_ID.fullmatch(jid or ""):
        return None
    q = os.path.join(queue_dir(), f"{jid}.json")
    if os.path.isfile(q) and not os.path.islink(q):
        try:
            mine = os.stat(q).st_mtime
            ahead = sum(1 for n in os.listdir(queue_dir()) if n.startswith("c-") and n.endswith(".json")
                        and os.stat(os.path.join(queue_dir(), n)).st_mtime < mine)
            rec = read_json(q)
            return {"id": jid, "state": "queued", "position": ahead + 1, "recipe": rec.get("recipe"),
                    "queuedAt": rec.get("queuedAt")}
        except (OSError, ValueError):
            pass   # claimed by the service between the two reads: fall through to its status
    try:
        st = read_json(os.path.join(jobs_dir(), jid, "status.json"))
    except (OSError, ValueError):
        if os.path.isfile(os.path.join(jobs_dir(), jid, "job.json")):
            return {"id": jid, "state": "running"}
        return None
    if not isinstance(st, dict):
        return None
    st.pop("trace", None)
    if st.get("error"):
        st["error"] = store.scrub(st["error"])
    return st


def listing(n: int = 20) -> dict:
    jobs = []
    names = sorted((d for d in os.listdir(jobs_dir()) if craft_spec.JOB_ID.fullmatch(d)), reverse=True)[:n] \
        if os.path.isdir(jobs_dir()) else []
    for d in names:
        try:
            st = read_json(os.path.join(jobs_dir(), d, "status.json"))
        except (OSError, ValueError):
            continue
        jobs.append({k: st.get(k) for k in ("id", "recipe", "state", "seconds", "finishedAt", "error")}
                    | {"outputs": [o.get("ref") for o in st.get("outputs") or []],
                       "commercial_ok": (st.get("provenance") or {}).get("commercial_ok")})
    queued = []
    if os.path.isdir(queue_dir()):
        names = [x for x in os.listdir(queue_dir()) if x.startswith("c-") and x.endswith(".json")]
        queued = [x[:-5] for x in sorted(names, key=lambda x: os.stat(os.path.join(queue_dir(), x)).st_mtime)]
    return {"queued": queued, "recent": jobs}


def doc() -> dict:
    return {"recipes": craft_spec.recipes_doc(), "looks": list(craft_spec.LOOKS) + ["brand", "custom"],
            "formats": list(craft_spec.SIZES), "service": feature(),
            "brand": "optional {colors: {bg, fg, accent, accent2, muted}, fonts: {display, body}, logo: {root, "
                     "name}, handle}; or brand_kit: <id>",
            "how": "POST /api/craft {recipe, params, brand}; poll GET /api/craft/<job>. Finished files are in the "
                   "lab folder with a sidecar (license of the inputs carried through), usable as inputs to "
                   "other recipes and to cuts.",
            "tools": {name: {"licence": craft_spec.APP_LICENCE, "source": f"{craft_spec.APP_SOURCE}/{app}"}
                      for app, name in craft_spec.APP_NAMES.items()}}


# --------------------------------------------------------------------------------------------- routes

def routes(srv):
    def spec_errors(fn):
        def wrapped(h, m):
            try:
                return fn(h, m)
            except craft_spec.SpecError as exc:
                raise srv.HTTPError(400, str(exc)) from None
            except QueueFull as exc:
                raise srv.HTTPError(429, str(exc)) from None
        return wrapped

    def r_recipes(h, m):
        srv._json(h, 200, doc())

    def r_list(h, m):
        srv._json(h, 200, {"service": feature(), **listing()})

    @spec_errors
    def r_submit(h, m):
        body = srv.read_json(h)
        srv._json(h, 202, enqueue(body, "api"))

    def r_status(h, m):
        st = status(m.group(1))
        if st is None:
            raise srv.HTTPError(404, "no such craft job")
        srv._json(h, 200, st)

    return [("GET", r"/api/craft", r_list),
            ("GET", r"/api/craft/recipes", r_recipes),
            ("POST", r"/api/craft", r_submit),
            ("GET", r"/api/craft/(c-\d{14}-[0-9a-f]{6})", r_status)]
