"""HTTP client for the post render service (render/ in this repository, its own container).

URL from YOURCONTENT_RENDER_URL (or MEDIA_LAB_RENDER_URL) (default http://render:8191, the compose service name). Plain http on the internal
network only, no proxies, no redirects. When the service is off, `feature()` says so in one short reason (the way
features.py reports a missing ComfyUI) and calls raise RenderOff (503); nothing here crashes the app.
"""
from __future__ import annotations

import http.client
import json
import os
import threading
import time
import urllib.parse

from . import config

DEFAULT_URL = "http://render:8191"
OFF = "the render service is not reachable"
STYLES_TTL = 30.0
_cache: dict = {}
_lock = threading.Lock()


class RenderOff(Exception):
    """The render service can't be reached (503)."""
    status = 503


class RenderFailed(Exception):
    """The render service answered with an error; carries its status (400s pass through, others become 502)."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def base_url() -> str:
    return (config.env("RENDER_URL") or DEFAULT_URL).rstrip("/")


def _target() -> tuple[str, int]:
    u = urllib.parse.urlsplit(base_url())
    if u.scheme != "http" or not u.hostname or u.path not in ("", "/") or u.query or u.username:
        raise RenderOff("YOURCONTENT_RENDER_URL must be http://host:port")
    return u.hostname, u.port or 80


def _request(method: str, path: str, body: bytes | None = None, timeout: float = 10.0):
    """(status, headers, body bytes). Raises RenderOff when the service can't be reached."""
    host, port = _target()
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        headers = {"Accept": "*/*"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=body, headers=headers)
        r = conn.getresponse()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
    except (OSError, http.client.HTTPException) as exc:
        raise RenderOff(OFF) from exc
    finally:
        conn.close()


def _error_of(data: bytes, status: int) -> str:
    try:
        msg = json.loads(data).get("error")
    except (ValueError, AttributeError):
        msg = None
    return str(msg or f"the render service answered {status}")[:300]


def styles(fresh: bool = False) -> dict:
    """GET /styles (cached for 30 s): {"styles": [...], "fonts": [...], "formats": [...], ...}."""
    with _lock:
        hit = _cache.get("styles")
    if hit and not fresh and time.monotonic() - hit[0] < STYLES_TTL and hit[1] == base_url():
        return hit[2]
    status, _, data = _request("GET", "/styles", timeout=5)
    if status != 200:
        raise RenderFailed(_error_of(data, status))
    try:
        doc = json.loads(data)
    except ValueError:
        raise RenderFailed("the render service sent something that isn't JSON") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("styles"), list):
        raise RenderFailed("the render service sent an unexpected style list")
    with _lock:
        _cache["styles"] = (time.monotonic(), base_url(), doc)
    return doc


def feature() -> dict:
    """{"ok": bool, "missing": [reason]} for /api/health-style reporting. Never raises."""
    try:
        doc = styles()
        n = len(doc.get("styles") or [])
        return {"ok": n > 0, "styles": n, "missing": [] if n else ["the render service has no styles"]}
    except RenderOff:
        return {"ok": False, "styles": 0, "missing": [OFF]}
    except RenderFailed as exc:
        return {"ok": False, "styles": 0, "missing": [str(exc)]}


def render(spec: dict, timeout: float = 120.0) -> tuple[bytes, dict, str]:
    """POST /render. Returns (image bytes, X-Render-Info dict, content type). Raises RenderOff / RenderFailed."""
    status, headers, data = _request("POST", "/render", json.dumps(spec).encode("utf-8"), timeout=timeout)
    if status != 200:
        raise RenderFailed(_error_of(data, status), status if status in (400, 413, 503) else 502)
    ctype = headers.get("content-type", "")
    if ctype not in ("image/png", "image/jpeg") or not data:
        raise RenderFailed("the render service sent something that isn't a picture")
    try:
        info = json.loads(headers.get("x-render-info") or "{}")
    except ValueError:
        info = {}
    return data, info if isinstance(info, dict) else {}, ctype
