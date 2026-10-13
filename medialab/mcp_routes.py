"""MCP over HTTP at POST /mcp: YourContent's API as tools for AI agents (studio merge, Connect your agent).

Streamable HTTP transport, JSON responses only (no SSE stream): one JSON-RPC request in, one JSON response out;
a notification gets 202 and no body. Each tool is a thin call to YourContent's own HTTP API on the loopback address,
carrying the caller's Authorization header, so a tool gets exactly the validation and the permissions of the route
behind it (an agent token still cannot delete; see agents.AGENT_DENY). Tools whose route is not installed (a
feature module that is absent) are left out of tools/list.

The guard exempts /mcp from the X-Media-Lab header because MCP clients cannot add it; it still demands
`Content-Type: application/json` (which a cross-site form cannot send without a CORS preflight that this server
refuses) and the same Host and Origin checks as every POST.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import VERSION

PROTOCOL = "2025-06-18"
JOB_WAIT_MAX = 110  # seconds a job_status call may wait; MCP clients time out around 2 minutes
INSTRUCTIONS = (
    "YourContent makes pictures, clips and music on the owner's GPU, styles posts with brand kits, edits photos and "
    "video, cuts reels and keeps a content calendar. Generation runs as jobs: submit, then call job_status with "
    "wait_seconds until the job is done. Every file carries a license label; check commercial_ok before suggesting "
    "commercial use. Nothing here posts to social networks: put finished work on the calendar as 'ready' and let "
    "the owner publish. Read /docs/AGENTS.md in the YourContent repository for the full guide.")


def _obj(props: dict, required: tuple = (), extra: bool = False) -> dict:
    s = {"type": "object", "properties": props, "additionalProperties": extra}
    if required:
        s["required"] = list(required)
    return s


REF = {"type": "object", "description": "a file reference as returned by list_media: {root, name}",
       "properties": {"root": {"type": "string"}, "name": {"type": "string"}}, "required": ["root", "name"]}
S, I, N = {"type": "string"}, {"type": "integer"}, {"type": "number"}
CAL_FIELDS = {"when": {"type": ["string", "null"], "description": "YYYY-MM-DD (all day) or YYYY-MM-DDTHH:MM[+offset]; "
                                                                "null leaves it unscheduled"},
              "duration_min": I, "channel": {"type": "string", "description": "e.g. instagram_reel"},
              "title": S, "caption": S,
              "status": {"enum": ["idea", "draft", "ready", "posted", "skipped"]},
              "project": S, "asset": REF,
              "note": {"type": "string", "description": "why the status changed (kept in the entry's history; 500 chars)"}}

# name, description, input schema, method, path template ({key} taken from the arguments), body keys
# (None = every remaining argument), read-only hint.
TOOLS: list[tuple] = [
    ("app_status", "Which features work right now (models present, ComfyUI up) and why not.",
     _obj({}), "GET", "/api/health", (), True),
    ("list_media", "Recent pictures, clips, music and exports, newest first, with license labels.",
     _obj({}), "GET", "/api/recent", (), True),
    ("generate_picture", "Start a picture job from a prompt. The default model, Qwen-Image-2512, is text-to-image "
     "and commercial OK; reference pictures (up to 6) need variant \"bf16\" (Qwen-Image 2.1, non-commercial).",
     _obj({"prompt": S, "negative": S, "steps": I, "seed": I, "variant": S, "refs": {"type": "array", "items": REF}},
          ("prompt",)), "POST", "/api/generate", None, False),
    ("animate_picture", "Start a clip job that animates a picture (3 or 5 seconds).",
     _obj({"start": REF, "prompt": S, "seconds": {"enum": [3, 5]}, "quality": {"enum": ["480p", "720p"]},
           "seed": I}, ("start",)), "POST", "/api/animate", None, False),
    ("extend_clip", "Start a job that adds 3 or 5 seconds to the end of a clip.",
     _obj({"src": REF, "prompt": S, "seconds": {"enum": [3, 5]}, "seed": I}, ("src",)),
     "POST", "/api/extend", None, False),
    ("make_music", "Start a music job from style tags (and optional lyrics; '[Instrumental]' for none).",
     _obj({"tags": S, "lyrics": S, "bpm": I, "seconds": N, "project_id": S}, ("tags",)),
     "POST", "/api/music", None, False),
    ("job_status", "A job's state and result. wait_seconds (up to 110) waits until it finishes.",
     _obj({"job": S, "wait_seconds": I}, ("job",)), "GET", "/api/jobs/{job}", (), True),
    ("list_projects", "Cuts (timeline projects).", _obj({}), "GET", "/api/projects", (), True),
    ("create_project", "Make an empty cut.", _obj({"name": S}), "POST", "/api/projects", None, False),
    ("get_project", "A cut with its items, music and resolved timing.", _obj({"id": S}, ("id",)),
     "GET", "/api/projects/{id}", (), True),
    ("save_project", "Replace a cut (send the rev you read; a stale rev is refused).",
     _obj({"id": S, "rev": I, "project": {"type": "object"}}, ("id", "rev", "project")),
     "POST", "/api/projects/{id}", ("rev", "project"), False),
    ("export_project", "Start the final MP4 export of a cut.", _obj({"id": S, "rev": I}, ("id", "rev")),
     "POST", "/api/projects/{id}/export", ("rev",), False),
    ("calendar_list", "Content calendar entries between two dates (YYYY-MM-DD), or the unscheduled ones with "
     "unscheduled='1'.", _obj({"from": S, "to": S, "unscheduled": S}),
     "GET", "/api/calendar", (), True),
    ("calendar_add", "Add a calendar entry. Put finished work on it with status 'ready' and the file as asset.",
     _obj(CAL_FIELDS, ("title",)), "POST", "/api/calendar", None, False),
    ("calendar_update", "Change a calendar entry: send its rev and only the fields that change.",
     _obj({"id": S, "rev": I, **CAL_FIELDS}, ("id", "rev")), "POST", "/api/calendar/{id}", None, False),
    ("list_styles", "Post styles available for render_post.", _obj({}), "GET", "/api/styles", (), True),
    ("list_brandkits", "The owner's brand kits (colors, fonts, logo, voice).", _obj({}),
     "GET", "/api/brandkits", (), True),
    ("render_post", "Render a styled post image from a brand kit, a style (list_styles), text and optional "
     "pictures. Saved to the library with a license label; the file is item.ref in the reply.",
     _obj({"kit": S, "style": S, "format": {"enum": ["4:5", "1:1", "9:16", "1.91:1"]}, "type": {"enum": ["png", "jpg"]},
           "image": REF, "images": {"type": "array", "items": REF},
           "text": _obj({"headline": S, "kicker": S, "body": S, "detail": S, "handle": S, "label": S}, ("headline",)),
           "caption": S}, ("kit", "style", "text")), "POST", "/api/posts/render", None, False),
    ("craft_recipes", "Photo and video recipes (grade, finish, title card, reel) and their parameters.",
     _obj({}), "GET", "/api/craft/recipes", (), True),
    ("craft_run", "Start a photo/video recipe job; params per recipe are listed by craft_recipes.",
     _obj({"recipe": {"enum": ["photo.grade", "photo.finish", "video.titlecard", "video.reel"]},
           "params": {"type": "object"}, "brand_kit": S}, ("recipe", "params")), "POST", "/api/craft", None, False),
    ("craft_status", "A craft job's state and outputs.", _obj({"job": S}, ("job",)),
     "GET", "/api/craft/{job}", (), True),
    ("google_status", "Whether Google Drive is connected, and the workspace folder and Sheet links.",
     _obj({}), "GET", "/api/google/status", (), True),
    ("plan_sync", "Sync the calendar with the Content Plan Sheet both ways (the side that changed wins).",
     _obj({}), "POST", "/api/google/sync", None, False),
    ("google_upload", "Copy a finished file to the Google workspace (folder: rendered or exports).",
     _obj({"ref": REF, "folder": {"enum": ["rendered", "exports"]}}, ("ref", "folder")),
     "POST", "/api/google/upload", None, False),
]
BY_NAME = {t[0]: t for t in TOOLS}


class ToolError(Exception):
    pass


def _fill(template: str, args: dict) -> tuple[str, set]:
    used = set()
    out = template
    for key in [p[1:-1] for p in template.split("/") if p.startswith("{")]:
        val = args.get(key)
        if not isinstance(val, str) or not val or "/" in val:
            raise ToolError(f"{key} is required")
        out = out.replace("{" + key + "}", urllib.parse.quote(val, safe=""))
        used.add(key)
    return out, used


def available(srv, method: str, path: str) -> bool:
    """Is a route installed for this tool? (404 = no, anything else = yes)."""
    probe = path.replace("{", "x").replace("}", "")
    try:
        srv.match(method, probe)
    except srv.HTTPError as e:
        return e.code != 404
    return True


def _call(base: str, auth: str | None, method: str, path: str, body: dict | None, timeout: float = 60):
    headers = {"Accept": "application/json"}
    if auth:
        headers["Authorization"] = auth
    data = None
    if method == "POST":
        headers.update({"X-Media-Lab": "1", "Content-Type": "application/json"})
        data = json.dumps(body or {}).encode()
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            msg = json.loads(raw).get("error") or ""
        except ValueError:
            msg = ""
        raise ToolError(f"{e.code}: {msg or raw[:200].decode(errors='replace')}") from None
    except urllib.error.URLError as e:
        raise ToolError(f"YourContent did not answer ({e.reason})") from None
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {"text": raw[:2000].decode(errors="replace")}


def call_tool(base: str, auth: str | None, name: str, args: dict):
    t = BY_NAME.get(name)
    if not t:
        raise ToolError(f"unknown tool {name}")
    _, _, _, method, template, body_keys, _ = t
    if not isinstance(args, dict):
        raise ToolError("arguments must be an object")
    path, used = _fill(template, args)
    if method == "GET":
        q = {k: v for k, v in args.items() if k not in used and k != "wait_seconds" and isinstance(v, (str, int))}
        if q:
            path += "?" + urllib.parse.urlencode(q)
        out = _call(base, auth, "GET", path, None)
        wait = args.get("wait_seconds")
        if name == "job_status" and isinstance(wait, int) and wait > 0:
            deadline = time.monotonic() + min(wait, JOB_WAIT_MAX)
            while (isinstance(out, dict) and out.get("state") in ("queued", "running")
                   and time.monotonic() < deadline):
                time.sleep(2)
                out = _call(base, auth, "GET", path, None)
        return out
    rest = {k: v for k, v in args.items() if k not in used}
    body = rest if body_keys is None else {k: rest[k] for k in body_keys if k in rest}
    return _call(base, auth, "POST", path, body, timeout=120)


def handle(srv, h, msg: dict):
    """One JSON-RPC message -> result dict, or None for a notification. Raises KeyError for unknown methods."""
    method = msg.get("method")
    if method == "initialize":
        want = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL
        return {"protocolVersion": want if want <= PROTOCOL else PROTOCOL, "capabilities": {"tools": {}},
                "serverInfo": {"name": "yourcontent", "title": "YourContent", "version": VERSION},
                "instructions": INSTRUCTIONS}
    if method == "ping":
        return {}
    if method == "tools/list":
        out = []
        for name, desc, schema, meth, template, _, ro in TOOLS:
            if available(srv, meth, template):
                out.append({"name": name, "description": desc, "inputSchema": schema,
                            "annotations": {"readOnlyHint": ro, "destructiveHint": False}})
        return {"tools": out}
    if method == "tools/call":
        p = msg.get("params") or {}
        host, port = h.server.server_address[:2]
        host = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
        base = f"http://{'[' + host + ']' if ':' in host else host}:{port}"
        try:
            out = call_tool(base, h.headers.get("Authorization"), p.get("name"), p.get("arguments") or {})
        except ToolError as e:
            return {"content": [{"type": "text", "text": str(e)}], "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False, indent=1)}],
                "structuredContent": out if isinstance(out, dict) else {"items": out}, "isError": False}
    if msg.get("id") is None:
        return None
    raise KeyError(method)


def routes(srv):
    def r_mcp(h, m):
        msg = srv.read_json(h)
        if msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            return srv._json(h, 400, {"jsonrpc": "2.0", "id": None,
                                      "error": {"code": -32600, "message": "invalid request"}})
        try:
            res = handle(srv, h, msg)
        except KeyError:
            return srv._json(h, 200, {"jsonrpc": "2.0", "id": msg.get("id"),
                                      "error": {"code": -32601, "message": f"method not found: {msg['method']}"}})
        if msg.get("id") is None:
            return srv._send_raw(h, 202, b"", "application/json")
        srv._json(h, 200, {"jsonrpc": "2.0", "id": msg["id"], "result": res})

    def r_mcp_get(h, m):
        raise srv.HTTPError(405, "this MCP endpoint answers POST with JSON; it has no event stream")

    return [("POST", r"/mcp", r_mcp), ("GET", r"/mcp", r_mcp_get)]
