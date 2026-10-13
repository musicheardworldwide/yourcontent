"""Agent tokens: scoped credentials for AI agents that drive YourContent (studio merge, Connect your agent).

An agent token is a Bearer credential that can make and arrange things but not destroy them or change who has
access: delete, Google connect/disconnect and the token routes themselves are refused (AGENT_DENY). Only the
SHA-256 of a token is stored (`DATA/config/agents.json`); the token is shown once, when it is made.

Tokens matter only when the server requires a token (YOURCONTENT_TOKEN set). On a loopback-only install with no
token, every local process already has full access; the setup prompt says so instead of pretending otherwise.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

from . import store

PREFIX = "mla_"
TOKEN_RE = re.compile(r"^mla_[A-Za-z0-9_-]{43}$")
MAX_AGENTS = 20
MAX_NAME = 60
PRESETS = ("claude", "chatgpt", "hermes", "generic")

# (method, path regex) an agent token may never use. Everything else follows the normal guard.
AGENT_DENY: tuple[tuple[str, re.Pattern], ...] = tuple((m, re.compile("^" + rx + "$")) for m, rx in (
    ("POST", r"/api/delete"),
    ("POST", r"/api/projects/[^/]+/delete"),
    ("POST", r"/api/calendar/[^/]+/delete"),
    ("GET", r"/api/calendar/feed"),
    ("POST", r"/api/calendar/feed/rotate"),
    ("POST", r"/api/brandkits/[^/]+/delete"),
    ("POST", r"/api/agents(/.*)?"),
    ("GET", r"/api/agents(/.*)?"),
    ("POST", r"/api/google/(connect|disconnect|bootstrap|manual)"),
    ("GET", r"/api/google/callback"),
    ("POST", r"/api/login"),
))

_lock = threading.Lock()


def _path() -> str:
    return store.data_path("config", "agents.json")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _load() -> list[dict]:
    try:
        with open(_path(), encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    return [a for a in data.get("agents", []) if isinstance(a, dict)]


def _save(agents: list[dict]) -> None:
    p = _path()
    tmp = p + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"agents": agents}, f, indent=1)
    os.replace(tmp, p)


def public(a: dict) -> dict:
    """An agent record without its hash."""
    return {k: a.get(k) for k in ("id", "name", "preset", "created", "last_used", "revoked")}


def list_agents() -> list[dict]:
    with _lock:
        return [public(a) for a in _load()]


def create(name: str, preset: str) -> tuple[dict, str]:
    """(record, token). The token is returned only here."""
    name = " ".join(str(name or "").split())[:MAX_NAME] or "Agent"
    if preset not in PRESETS:
        raise ValueError(f"preset must be one of {', '.join(PRESETS)}")
    token = PREFIX + secrets.token_urlsafe(32)
    rec = {"id": "ag-" + secrets.token_hex(4), "name": name, "preset": preset, "created": int(time.time()),
           "last_used": None, "revoked": False, "sha256": _hash(token)}
    with _lock:
        agents = _load()
        if sum(1 for a in agents if not a.get("revoked")) >= MAX_AGENTS:
            raise ValueError(f"at most {MAX_AGENTS} active agents; revoke one first")
        agents.append(rec)
        _save(agents)
    return public(rec), token


def revoke(agent_id: str) -> bool:
    with _lock:
        agents = _load()
        for a in agents:
            if a.get("id") == agent_id and not a.get("revoked"):
                a["revoked"] = True
                a.pop("sha256", None)
                _save(agents)
                return True
    return False


def check(token: str) -> dict | None:
    """The active agent this token belongs to (and marks it used), or None. Constant-time per record."""
    if not TOKEN_RE.fullmatch(token or ""):
        return None
    want = _hash(token)
    with _lock:
        agents = _load()
        hit = None
        for a in agents:
            if not a.get("revoked") and hmac.compare_digest(a.get("sha256") or "", want):
                hit = a
        if hit is None:
            return None
        now = int(time.time())
        if not hit.get("last_used") or now - int(hit["last_used"]) > 60:
            hit["last_used"] = now
            _save(agents)
        return public(hit)


def allowed(method: str, path: str) -> bool:
    """PURE: may an agent token use this route?"""
    return not any(m == method and rx.match(path) for m, rx in AGENT_DENY)
