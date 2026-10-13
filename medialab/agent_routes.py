"""Routes for Connect your agent: agent tokens and the setup prompt (agents.py, agent_setup.py).

Owner only: agent tokens are refused on every /api/agents route (agents.AGENT_DENY), so an agent cannot mint
another agent or read the list.
"""
from __future__ import annotations

from . import agent_setup, agents, config as config_mod

STATIC = {"/static/agents.js": ("agents.js", "text/javascript; charset=utf-8"),
          "/static/agents.css": ("agents.css", "text/css; charset=utf-8")}
COLUMNS = ["id", "when", "channel", "title", "caption", "status", "project", "asset", "updated"]


def _columns() -> list[str]:
    try:
        from . import calendar
        return list(calendar.CALENDAR_COLUMNS)
    except (ImportError, AttributeError):
        return COLUMNS


def _google_ctx() -> dict | None:
    try:
        from . import google
    except ImportError:
        return None
    try:
        ws = google.workspace_links()
    except Exception:  # noqa: BLE001 - the prompt then explains how to make one
        return None
    return ws or None


def routes(srv):
    def base_url(h) -> str:
        host = (h.headers.get("Host") or "127.0.0.1").strip()
        proto = "https" if (h.headers.get("X-Forwarded-Proto") or "").lower() == "https" else "http"
        return f"{proto}://{host}"

    def r_list(h, m):
        srv._json(h, 200, {"agents": agents.list_agents(), "presets": agent_setup.PRESET_LABELS})

    def r_create(h, m):
        body = srv.read_json(h)
        preset = body.get("preset") or "generic"
        cfg = h.server.state.cfg
        needs = bool(cfg.token)  # the guard asks for a credential whenever a token is set
        try:
            rec, token = agents.create(srv._text(body, "name", agents.MAX_NAME, "Agent"), preset)
        except ValueError as e:
            raise srv.HTTPError(400, str(e)) from None
        facts = config_mod.reach(cfg)  # what is true for this running config, not what is usual
        prompt = agent_setup.build_prompt(preset, {
            "base_url": base_url(h), "token": token if needs else None, "token_required": needs,
            "listens": facts["listens"], "loopback_only": facts["loopback_only"],
            "agent_name": rec["name"], "google": _google_ctx(), "columns": _columns()})
        srv._json(h, 200, {"agent": rec, "prompt": prompt, "token_shown_once": needs})

    def r_revoke(h, m):
        srv.read_json(h)
        if not agents.revoke(m.group(1)):
            raise srv.HTTPError(404, "no such active agent")
        srv._json(h, 200, {"ok": True})

    return [("GET", r"/api/agents", r_list),
            ("POST", r"/api/agents", r_create),
            ("POST", r"/api/agents/([A-Za-z0-9-]{1,20})/revoke", r_revoke)]
