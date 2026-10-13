"""PURE: the "Connect your agent" setup prompt (studio merge).

`build_prompt(preset, ctx)` returns the text the owner pastes into their AI agent. It tells the agent how to reach
YourContent (MCP at /mcp, or the HTTP API), which credential to use, where the Google workspace is (or how to make
one when the app has no Google connection) and the house rules: make and schedule, never post or delete.

ctx keys: base_url (str), token (str | None: a fresh agent token, None when the server needs none),
token_required (bool), listens (str: the address the port is reachable on, config.reach), loopback_only (bool;
derived from listens, else from base_url, when missing), agent_name (str), google (dict | None: {"root_url", "folders": {name: url}, "sheet_url",
"sheet_id"}), columns (list[str]: the Content Plan header).
"""
from __future__ import annotations

import json
import urllib.parse

from .config import is_loopback

PRESET_LABELS = {
    "claude": "Claude (Claude Code, Claude Desktop)",
    "chatgpt": "ChatGPT",
    "hermes": "Hermes Agent",
    "generic": "Any agent with HTTP or MCP",
}

FOLDERS = ("Inbox", "Rendered", "Exports", "Calendar")

RULES = """House rules:
- Make, arrange and schedule. Never post to a social network yourself, and never delete anything{why}.
  Finished work goes on the calendar with status "ready"; the owner publishes.
- Generation is slow. Start a job, then call job_status with wait_seconds until it is done. Don't start the same
  job twice.
- Every file has a licence label. If commercial_ok is false, say so before suggesting the file for paid or
  business use. If it is null, the rights were not checked.
- Keep the credential below secret: don't print it back, don't put it in files, captions or the Sheet."""


def _mcp_block(preset: str, url: str, token: str | None) -> str:
    auth = f"Authorization: Bearer {token}" if token else None
    if preset == "claude":
        cmd = f"claude mcp add --transport http yourcontent {url}"
        if auth:
            cmd += f' --header "{auth}"'
        args = ["-y", "mcp-remote", url] + (["--allow-http"] if url.startswith("http://") else [])
        if token:
            args += ["--header", f"Authorization: Bearer {token}"]
        desktop = json.dumps({"mcpServers": {"yourcontent": {"command": "npx", "args": args}}})
        return ("Connect over MCP (preferred).\n"
                f"- Claude Code: run\n    {cmd}\n"
                "- Claude Desktop (needs Node.js; mcp-remote bridges it to the HTTP server): add this to "
                f"claude_desktop_config.json, then restart:\n    {desktop}")
    if preset == "chatgpt":
        return ("ChatGPT reaches MCP servers only over public HTTPS. If the owner has published YourContent behind "
                f"HTTPS, add a connector with the URL {url}" + (" and the bearer token below" if token else "")
                + ". Otherwise use the HTTP API below from a code tool that can reach the address.")
    if preset == "hermes":
        return ("Add YourContent as an MCP server in your Hermes configuration (an HTTP server entry):\n"
                f"    url: {url}" + (f"\n    headers: {{Authorization: Bearer {token}}}" if token else "")
                + "\nUse your configuration command rather than editing the file by hand, then reload.")
    return (f"MCP (Streamable HTTP, JSON responses): POST JSON-RPC to {url}"
            + (f" with the header '{auth}'." if auth else "."))


def _http_block(base: str, token: str | None) -> str:
    h = f" -H 'Authorization: Bearer {token}'" if token else ""
    return ("HTTP API (for agents without MCP). Every POST needs the header 'X-Media-Lab: 1' and a JSON body.\n"
            f"    curl -s{h} {base}/api/health\n"
            f"    curl -s{h} -H 'X-Media-Lab: 1' -H 'Content-Type: application/json' \\\n"
            f"      -d '{{\"prompt\": \"a lighthouse at dusk, film grain\"}}' {base}/api/generate\n"
            f"    curl -s{h} {base}/api/jobs/<job id>\n"
            "The full route list is in docs/API.md and docs/openapi.json in the YourContent repository.")


def _google_block(google: dict | None, columns: list[str]) -> str:
    cols = ", ".join(columns)
    if google and google.get("sheet_url"):
        lines = ["Google workspace (YourContent made it and keeps it in sync):",
                 f"- Folder: {google.get('root_url', '')}"]
        for name in FOLDERS:
            url = (google.get("folders") or {}).get(name)
            if url:
                lines.append(f"- {name}/: {url}")
        lines.append(f"- Content Plan Sheet: {google['sheet_url']} (columns: {cols})")
        lines.append("Drop source photos and clips in Inbox/. Plan posts as Content Plan rows, or better, with "
                     "calendar_add, which YourContent syncs to the Sheet.")
        return "\n".join(lines)
    return ("Google workspace: not connected yet. YourContent uses Google's drive.file access, so it can only see "
            "folders and Sheets it made itself; one you make by hand would stay invisible to it. If the owner wants "
            "Drive, ask them to open YourContent > Connect > Google and press Connect, then Create workspace. After "
            f"that, google_status gives you the folder and Content Plan links (columns: {cols}). Until then the "
            "calendar in YourContent works without Google.")


def build_prompt(preset: str, ctx: dict) -> str:
    if preset not in PRESET_LABELS:
        raise ValueError(f"preset must be one of {', '.join(PRESET_LABELS)}")
    base = str(ctx.get("base_url") or "").rstrip("/")
    token = ctx.get("token")
    name = ctx.get("agent_name") or "Agent"
    parts = [
        f"You are connecting to YourContent as \"{name}\". YourContent runs on the owner's computer and GPU: it makes "
        "pictures, clips and music, styles posts with the owner's brand kits, grades photos, cuts reels and keeps "
        "a content calendar.",
        f"Address: {base}",
    ]
    listens = str(ctx.get("listens") or urllib.parse.urlsplit(base).hostname or "")
    local = ctx.get("loopback_only")
    local = is_loopback(listens) if local is None else bool(local)
    scoped = bool(token or ctx.get("token_required"))
    if token:
        parts.append(f"Credential (agent token, scoped: it can make and schedule, not delete): {token}")
    elif ctx.get("token_required"):
        parts.append("Credential: ask the owner to make an agent token in YourContent > Connect your agent. "
                     "Agent tokens can make and schedule, not delete.")
    elif local:
        parts.append(f"Credential: none needed. This YourContent listens only on this computer ({listens}), so "
                     "you must run on the same computer. Without a token every program here has the owner's full "
                     "access, including delete.")
    else:
        where = "every network address of its computer" if listens in ("0.0.0.0", "::", "[::]") else listens
        parts.append(f"Credential: none needed. This YourContent listens on {where} without a token, so anything "
                     "that can reach that address, not only this computer, has the owner's full access, including "
                     "delete.")
    why = (": deleting is refused for agent tokens anyway" if scoped
           else ": with no token nothing stops a delete here, so this rule is yours to keep")
    parts += [_mcp_block(preset, base + "/mcp", token), _http_block(base, token),
              _google_block(ctx.get("google"), list(ctx.get("columns") or [])), RULES.format(why=why),
              "First step: call app_status (or GET /api/health) and tell the owner which features are ready."]
    return "\n\n".join(parts) + "\n"
