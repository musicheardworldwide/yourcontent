"""ComfyUI HTTP client (§4, §12.6). Owner: WP3. Never calls /free.

ComfyUI is shared with other tools, so this client only ever touches prompt ids YourContent ran (or adopted by an
exact output-filename match): history is deleted by id, a queued job is removed by id, and a running job is
interrupted only after /queue shows it is ours.

The base URL comes from `configure(cfg)` (server start) or, until then, YOURCONTENT_COMFY_URL (or MEDIA_LAB_COMFY_URL) / COMFY_URL.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Callable

from . import config

DEFAULT_URL = "http://127.0.0.1:8188"
CLIENT_ID = "media-lab"
OUTPUT_KEYS = ("images", "gifs", "audio")
POLL = 1.0          # seconds between /history polls (tests lower it)
LOST_POLLS = 3      # polls in a row on which ComfyUI answers but has the prompt in neither /history nor /queue
DOWN_LIMIT = 60.0   # seconds ComfyUI may stay unreachable during a job before the job is given up
INFO_TTL = 30.0     # object_info cache lifetime

_base = (config.env("COMFY_URL") or os.environ.get("COMFY_URL") or DEFAULT_URL).rstrip("/")
_info_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()
# Called as on_submit(pid, kind) right after /prompt returns, so a crash still leaves the pid in the ledger
# (§5.6). actions wires it to store.ledger_add; kept as a hook so this module does not import the store.
on_submit: Callable[[str, str], None] | None = None


class ComfyOff(RuntimeError):
    """ComfyUI is not reachable. HTTP 503."""
    status = 503


class ComfyError(RuntimeError):
    """ComfyUI refused or failed a job; the message is ComfyUI's own reason."""
    status = 502


class Cancelled(RuntimeError):
    """The job was cancelled while it waited on ComfyUI."""
    status = 409


def configure(cfg_or_url) -> None:
    """Point the client at a ComfyUI (a Config with .comfy_url, or a URL string)."""
    global _base
    url = getattr(cfg_or_url, "comfy_url", cfg_or_url)
    _base = str(url or DEFAULT_URL).rstrip("/")
    clear_cache()


def base_url() -> str:
    return _base


def clear_cache() -> None:
    with _lock:
        _info_cache.clear()


def request(path: str, payload: dict | None = None, timeout: float = 30) -> dict:
    """GET (payload None) or POST JSON; returns the parsed body ({} for an empty one).
    urllib errors propagate (HTTPError keeps ComfyUI's body)."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(_base + path, data, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
    return json.loads(body) if body.strip() else {}


def ensure() -> None:
    """Fail fast with a plain answer when ComfyUI is off."""
    try:
        request("/system_stats", timeout=3)
    except Exception:  # noqa: BLE001 - any failure means "not up"
        raise ComfyOff("ComfyUI is not reachable. Start it, then try again.") from None


def version() -> str | None:
    try:
        return request("/system_stats", timeout=3).get("system", {}).get("comfyui_version")
    except Exception:  # noqa: BLE001
        return None


def _node_errors(detail: dict) -> str:
    errs = []
    for nid, n in (detail.get("node_errors") or {}).items():
        for e in n.get("errors", []):
            msg = " ".join(x for x in (e.get("message", ""), e.get("details", "")) if x).strip()
            errs.append(f"{n.get('class_type', 'node ' + str(nid))}: {msg}" if msg else str(nid))
    return "; ".join(errs) or (detail.get("error") or {}).get("message", "") or "ComfyUI refused the job"


def submit(graph: dict, kind: str | None = None) -> str:
    """POST /prompt; returns the prompt id. Raises ComfyError with ComfyUI's node errors.
    With `kind`, `on_submit(pid, kind)` records the pid at once (provisional ledger line)."""
    try:
        res = request("/prompt", {"prompt": graph, "client_id": CLIENT_ID}, timeout=30)
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read() or b"{}")
        except ValueError:
            detail = {}
        raise ComfyError(_node_errors(detail)) from None
    except (urllib.error.URLError, OSError):
        raise ComfyOff("ComfyUI is not reachable. Start it, then try again.") from None
    pid = res.get("prompt_id")
    if not pid:
        raise ComfyError(_node_errors(res))
    if kind and on_submit is not None:
        try:
            on_submit(pid, kind)
        except Exception:  # noqa: BLE001 - the ledger must never fail a job
            pass
    return pid


def _outputs(entry: dict) -> list[dict]:
    out = []
    for nid, node_out in (entry.get("outputs") or {}).items():
        for key in OUTPUT_KEYS:
            for item in node_out.get(key, []) or []:
                if isinstance(item, dict) and item.get("filename"):
                    out.append({**item, "node": nid, "kind": key})
    return out


def wait(pid: str, limit: float, on_progress: Callable | None = None, job=None) -> list[dict]:
    """Outputs from `images`, `gifs` and `audio` of the finished prompt:
    [{"filename", "subfolder", "type", "node", "kind"}]. Raises ComfyError on failure or timeout, and within a
    few polls when ComfyUI has forgotten the prompt (restarted) or after DOWN_LIMIT seconds without an answer;
    Cancelled when `job.cancelled` turns true (the prompt is then removed or interrupted)."""
    t0 = time.monotonic()
    phase = None
    # ComfyUI keeps its queue in memory: a restart mid-job forgets the prompt, which then never shows up in
    # /history. Without these checks the job polled until its 10-30 minute limit and said "timed out", holding
    # the GPU queue all that time (functional audit 2026-10-11).
    lost, down_since = 0, None
    while True:
        if job is not None and getattr(job, "cancelled", False):
            try:
                cancel(pid)
            except Exception:  # noqa: BLE001
                pass
            raise Cancelled("cancelled")
        try:
            entry = request(f"/history/{urllib.parse.quote(pid)}", timeout=10).get(pid)
            down_since = None
        except (urllib.error.URLError, OSError, ValueError):
            entry = None
            down_since = down_since if down_since is not None else time.monotonic()
            if time.monotonic() - down_since >= DOWN_LIMIT:
                raise ComfyError(f"ComfyUI stopped answering for {int(DOWN_LIMIT)} seconds in the middle of this "
                                 "job (it crashed or restarted), so the job is lost. Start ComfyUI, then try "
                                 "again.") from None
        if entry:
            status = entry.get("status") or {}
            if status.get("status_str") not in (None, "success"):
                msgs = [m for m in status.get("messages", []) if m and m[0] in ("execution_error",
                                                                                 "execution_interrupted")]
                if msgs and msgs[0][0] == "execution_interrupted":
                    raise Cancelled("cancelled")
                raise ComfyError(msgs[0][1].get("exception_message", "render failed") if msgs else "render failed")
            if status.get("completed", True) or entry.get("outputs"):
                return _outputs(entry)
        where = _queue_state(pid) if down_since is None and not entry else None
        new = where if where in ("running", "queued") else None
        if where is not None:
            # answered, but not finished: it must still be queued or running, else ComfyUI has forgotten it.
            # Several polls in a row, so a prompt that finished between the two reads is not mistaken for lost.
            if where == "absent":
                lost += 1
                if lost >= LOST_POLLS:
                    raise ComfyError("ComfyUI no longer knows this job: it restarted (or its queue was cleared) "
                                     "while the job ran. Try again.")
            else:
                lost = 0
        if on_progress is not None:
            if new and new != phase:
                phase = new
                try:
                    on_progress(None, phase)
                except Exception:  # noqa: BLE001
                    pass
        if time.monotonic() - t0 >= limit:
            raise ComfyError(f"timed out after {max(1, int(limit / 60))} minutes")
        time.sleep(POLL)


def _queue_state(pid: str) -> str | None:
    """"running", "queued", "absent" (ComfyUI answered and has no such prompt queued), or None (no answer)."""
    try:
        q = request("/queue", timeout=5)
    except Exception:  # noqa: BLE001
        return None
    if any(len(x) > 1 and x[1] == pid for x in q.get("queue_running", [])):
        return "running"
    if any(len(x) > 1 and x[1] == pid for x in q.get("queue_pending", [])):
        return "queued"
    return "absent"


def upload_image(data: bytes, name: str, subfolder: str) -> str:
    """POST /upload/image into input/<subfolder>; returns the stored name (ComfyUI may rename on a clash)."""
    boundary = uuid.uuid4().hex
    safe_name = name.replace('"', "").replace("\r", "").replace("\n", "")
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="subfolder"\r\n\r\n{subfolder}\r\n'.encode(),
        f'--{boundary}\r\nContent-Disposition: form-data; name="type"\r\n\r\ninput\r\n'.encode(),
        f'--{boundary}\r\nContent-Disposition: form-data; name="overwrite"\r\n\r\nfalse\r\n'.encode(),
        (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{safe_name}"\r\n'
         f'Content-Type: application/octet-stream\r\n\r\n').encode() + data + b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    req = urllib.request.Request(_base + "/upload/image", b"".join(parts),
                                 {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)["name"]
    except urllib.error.HTTPError as exc:
        raise ComfyError(f"ComfyUI refused the picture ({exc.code})") from None
    except (urllib.error.URLError, OSError):
        raise ComfyOff("ComfyUI is not reachable. Start it, then try again.") from None


def forget(pids: list[str]) -> int:
    """POST /history {"delete": pids}; only pids from our ledger. Returns how many were sent (0 when off)."""
    pids = [p for p in dict.fromkeys(pids or []) if isinstance(p, str) and p]
    if not pids:
        return 0
    try:
        request("/history", {"delete": pids}, timeout=10)
    except Exception:  # noqa: BLE001 - ComfyUI off: its in-memory history is gone anyway
        return 0
    return len(pids)


def cancel(pid: str) -> None:
    """Remove our queued prompt, or interrupt it only when /queue shows ours is the one running (§12.6)."""
    q = request("/queue", timeout=5)
    if any(len(x) > 1 and x[1] == pid for x in q.get("queue_pending", [])):
        request("/queue", {"delete": [pid]}, timeout=5)
        return
    if any(len(x) > 1 and x[1] == pid for x in q.get("queue_running", [])):
        # 0.37.0 honours prompt_id (e2e 2026-10-07: a wrong id leaves ours running); still only sent while
        # /queue shows ours running.
        request("/interrupt", {"prompt_id": pid}, timeout=5)


def history_find_output(filename: str, subfolder: str) -> str | None:
    """Structured exact match on outputs[*].images|gifs|audio[*].filename and subfolder (read-only)."""
    try:
        hist = request("/history", timeout=15)
    except Exception:  # noqa: BLE001
        return None
    for pid, entry in hist.items():
        for item in _outputs(entry):
            if item.get("filename") == filename and (item.get("subfolder") or "") == (subfolder or ""):
                return pid
    return None


def history_index() -> dict[tuple[str, str], str]:
    """{(filename, subfolder): pid} for every output in ComfyUI's /history, from one read (read-only).
    Raises ComfyOff when ComfyUI is off."""
    try:
        hist = request("/history", timeout=30)
    except Exception:  # noqa: BLE001
        raise ComfyOff("ComfyUI is not reachable") from None
    out: dict[tuple[str, str], str] = {}
    for pid, entry in hist.items():
        for item in _outputs(entry):
            out.setdefault((item.get("filename"), item.get("subfolder") or ""), pid)
    return out


def object_info(cls: str) -> dict:
    """GET /object_info/<cls>, cached 30 s. Returns that class's info ({} when ComfyUI lacks it).
    Raises ComfyOff when ComfyUI is off."""
    now = time.monotonic()
    with _lock:
        hit = _info_cache.get(cls)
        if hit and now - hit[0] < INFO_TTL:
            return hit[1]
    try:
        info = request(f"/object_info/{urllib.parse.quote(cls)}", timeout=10).get(cls) or {}
    except (urllib.error.URLError, OSError, ValueError):
        raise ComfyOff("ComfyUI is not reachable") from None
    with _lock:
        _info_cache[cls] = (now, info)
    return info


def object_infos(classes) -> dict:
    """{cls: info} for every class ComfyUI has, among `classes` ({} when ComfyUI is off)."""
    out = {}
    try:
        for cls in classes:
            info = object_info(cls)
            if info:
                out[cls] = info
    except ComfyOff:
        return {}
    return out
