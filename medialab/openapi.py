"""OpenAPI 3.1 description of the HTTP API, built from the live route table (server.ROUTES) so it cannot drift.

Request schemas come from the MCP tool table where a tool fronts the route (mcp_routes.TOOLS); other routes are
listed with their method and path. Served at GET /api/openapi.json and written by `python3 -m medialab openapi`.
"""
from __future__ import annotations

import re

from . import VERSION, mcp_routes

GROUP = re.compile(r"\((?!\?)[^)]*\)")
# Routes no MCP tool fronts (owner-only or browser-only), keyed "METHOD path".
SUMMARIES = {
    "GET /api/config": "Limits, presets, model choices and licence rows for the page, and how the app is reached: "
                       "listens (address), loopback_only, token_required (no credential needed).",
    "POST /api/login": "Exchange the owner token for a session cookie.",
    "POST /api/logout": "Drop the session cookie.",
    "GET /api/tracks": "Music tracks usable in a cut.",
    "GET /api/info/{p1}/{p2}": "One file's details and licence record (root, name).",
    "POST /api/upload": "Upload a picture, video or audio file: raw body, Content-Type, X-Filename header.",
    "POST /api/delete": "Delete a file YourContent made or adopted, for good (owner only).",
    "GET /api/jobs": "All jobs, newest first.",
    "POST /api/jobs/{p1}/cancel": "Cancel a queued or running job.",
    "POST /api/projects/{p1}/delete": "Delete a cut (owner only).",
    "POST /api/projects/{p1}/preview": "Start a low-resolution preview render of a cut (send its rev).",
    "GET /api/agents": "Agent tokens (without secrets) and the setup-prompt presets (owner only).",
    "POST /api/agents": "Make an agent token and its setup prompt; the token is shown once (owner only).",
    "POST /api/agents/{p1}/revoke": "Revoke an agent token (owner only).",
    "GET /api/openapi.json": "This document.",
    "POST /api/brandkits": "Make a brand kit (name, colours, fonts, logo ref, handle, voice, default styles).",
    "POST /api/brandkits/{p1}": "Replace a brand kit.",
    "POST /api/brandkits/{p1}/delete": "Delete a brand kit (owner only).",
    "GET /api/posts/health": "Whether the post renderer is up, and why not.",
    "GET /api/calendar.ics": "The calendar as an iCalendar file (from, to optional).",
    "GET /api/calendar/feed": "The private subscribe link for calendar apps (owner only).",
    "POST /api/calendar/feed/rotate": "Replace the subscribe link; the old one stops working (owner only).",
    "GET /api/calendar/feed/{p1}.ics": "The calendar feed for calendar apps; the secret in the path is the credential.",
    "POST /api/calendar/{p1}/delete": "Delete a calendar entry (owner only).",
    "GET /api/craft": "Recent craft jobs and the recipe service's state.",
    "POST /api/google/connect": "Start Google sign-in (drive.file access); returns the consent URL (owner only).",
    "GET /api/google/callback": "Google's redirect back after consent (single-use state).",
    "POST /api/google/bootstrap": "Create or repair the workspace folder, subfolders and Content Plan Sheet.",
    "POST /api/google/disconnect": "Revoke Google access and forget the tokens; Drive files stay (owner only).",
    "GET /api/google/inbox": "Files in the workspace's Inbox folder that YourContent can see.",
    "POST /api/google/inbox/import": "Copy an Inbox file into YourContent's uploads.",
    "POST /api/google/manual": "Save pasted folder and Sheet ids as the workspace (links only) (owner only).",
    "POST /mcp": "MCP over Streamable HTTP (JSON-RPC in, JSON out).",
    "GET /mcp": "Not used: 405 (this MCP endpoint has no event stream).",
}


def _path(rx: str) -> tuple[str, list[str]]:
    """'^/api/projects/([^/?#]+)/export$' -> ('/api/projects/{p1}/export', ['p1'])."""
    body = rx.lstrip("^").rstrip("$").replace("\\.", ".")
    names: list[str] = []

    def name(m):
        names.append(f"p{len(names) + 1}")
        return "{" + names[-1] + "}"
    return GROUP.sub(name, body), names


def build(routes) -> dict:
    tools = {(t[3], t[4]): t for t in mcp_routes.TOOLS}
    paths: dict = {}
    for meth, rx, fn in routes:
        path, names = _path(rx.pattern)
        if not path.startswith(("/api/", "/mcp")):
            continue
        tool = None
        for (tm, tp), t in tools.items():
            if tm == meth and re.fullmatch(re.sub(r"\{[^}]+\}", "[^/]+", tp), path.replace("{", "").replace("}", "")):
                tool = t
        op: dict = {"operationId": f"{meth.lower()}_{re.sub(r'[^a-z0-9]+', '_', path.lower()).strip('_')}",
                    "summary": (tool[1] if tool else SUMMARIES.get(f"{meth} {path}")
                                or (fn.__doc__ or "").strip().split("\n")[0]) or path,
                    "responses": {"200": {"description": "JSON"}, "4XX": {"description": "{\"error\": message}"}}}
        if names:
            op["parameters"] = [{"name": n, "in": "path", "required": True, "schema": {"type": "string"}}
                                for n in names]
        if meth == "POST":
            op["parameters"] = op.get("parameters", []) + [
                {"name": "X-Media-Lab", "in": "header", "required": path != "/mcp",
                 "schema": {"const": "1"}, "description": "required on every POST except /mcp"}]
            schema = tool[2] if tool else {"type": "object"}
            op["requestBody"] = {"required": False, "content": {"application/json": {"schema": schema}}}
        paths.setdefault(path, {})[meth.lower()] = op
    return {"openapi": "3.1.0",
            "info": {"title": "YourContent", "version": VERSION,
                     "license": {"name": "PolyForm Small Business 1.0.0",
                                 "url": "https://polyformproject.org/licenses/small-business/1.0.0"}},
            "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer",
                                                          "description": "owner token or mla_ agent token; "
                                                                         "not needed on a loopback-only install"}}},
            "security": [{}, {"bearer": []}],
            "paths": dict(sorted(paths.items()))}


def to_markdown(spec: dict) -> str:
    """docs/API.md: one table row per route, from the same spec."""
    lines = ["# YourContent HTTP API", "",
             "Generated by `python3 -m medialab openapi --md` from the live route table; the machine-readable "
             "version is `docs/openapi.json` (and `GET /api/openapi.json` on a running app).", "",
             "- Every POST needs the header `X-Media-Lab: 1` and a JSON body (except `/mcp`, and `/api/upload`, "
             "which takes the raw file).",
             "- Credentials: `Authorization: Bearer <owner token or mla_ agent token>`, or the page's session "
             "cookie, whenever the app has a token. Agent tokens cannot delete or manage access. Without a token "
             "none is asked for, and every client that can reach the address has the owner's full access, delete "
             "included.",
             "- `GET /api/config` says which applies: `token_required` (bool), `listens` (the address the port is "
             "reachable on; `0.0.0.0` = every address of the machine) and `loopback_only` (only this computer can "
             "connect). Read-only.",
             "- Errors are `{\"error\": \"a plain sentence\"}` with a 4xx/5xx status; a stale `rev` is 409 with "
             "the current record.",
             "- Files are references `{root, name}`, served at `/m/<root>/<name>`.", "",
             "| method | path | what it does |", "|---|---|---|"]
    for path, ops in spec["paths"].items():
        for meth, op in ops.items():
            lines.append(f"| {meth.upper()} | `{path}` | {op['summary'].replace('|', '/')} |")
    return "\n".join(lines) + "\n"


STATIC: dict = {}


def routes(srv):
    def r_openapi(h, m):
        srv._json(h, 200, build(srv.ROUTES))
    return [("GET", r"/api/openapi\.json", r_openapi)]
