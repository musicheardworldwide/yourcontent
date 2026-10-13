"""HTTP routes and static files for the content calendar (studio merge). IO: wires medialab.calendar to the
server through server.install_extension(calendar_routes).

    GET  /api/calendar?from=YYYY-MM-DD&to=YYYY-MM-DD   {"entries": [...], "statuses": [...]}
    POST /api/calendar                                 create; body = entry fields -> the entry
    POST /api/calendar/<id>                            update; body = {"rev": n, ...fields} -> the entry
    POST /api/calendar/<id>/delete                     body = {"rev": n} (rev optional) -> {"ok": true}
    GET  /api/calendar.ics?from=&to=                   text/calendar feed of the same entries

A stale rev answers 409 {"error", "current": <the entry as stored now>}.

    GET  /api/calendar/feed                            {"url", "path"}: the subscribe link (owner only)
    POST /api/calendar/feed/rotate                     new secret; the old link stops working (owner only)
    GET  /api/calendar/feed/<secret>.ics               the feed for calendar apps (no credential but the secret)

/api/calendar.ics sits behind the guard like every route. Calendar apps cannot send a Bearer header, so they
subscribe to the secret feed link instead: 256 random bits in the path, kept in DATA/config/calendar_feed.json
(0600), checked in constant time; a wrong secret answers 404. The guard lets that one path through (PUBLIC).
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
from datetime import datetime, timezone

from . import calendar, store
from .types import Conflict

PUBLIC = [("GET", r"/api/calendar/feed/[A-Za-z0-9_-]{43}\.ics")]


def _feed_path() -> str:
    return store.data_path("config", "calendar_feed.json")


def feed_secret(rotate: bool = False) -> str:
    """The feed secret, made on first use; rotate=True replaces it."""
    p = _feed_path()
    if not rotate:
        try:
            with open(p, encoding="utf-8") as f:
                s = json.load(f).get("secret")
            if isinstance(s, str) and len(s) == 43:
                return s
        except (FileNotFoundError, ValueError):
            pass
    s = secrets.token_urlsafe(32)
    tmp = p + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"secret": s}, f)
    os.replace(tmp, p)
    return s

STATIC = {"/static/calendar.js": ("calendar.js", "text/javascript; charset=utf-8"),
          "/static/calendar.css": ("calendar.css", "text/css; charset=utf-8")}
CAL_NAME = "YourContent content plan"


def routes(srv) -> list[tuple[str, str, object]]:
    """[(method, path regex, handler)] for server.install_extension; `srv` is medialab.server."""

    def _range(h) -> tuple[str | None, str | None]:
        q = srv._query(h)
        return q.get("from") or None, q.get("to") or None

    def _conflict(exc: Conflict):
        return srv.HTTPError(409, str(exc) or "this entry was changed elsewhere", {"current": exc.current})

    def r_list(h, m):
        _json_ok(h, {"entries": calendar.list_entries(*_range(h)), "statuses": list(calendar.STATUSES)})

    def r_create(h, m):
        body = srv.read_json(h)
        _json_ok(h, calendar.create(body))

    def r_update(h, m):
        eid = calendar.check_id(m.group(1))
        body = srv.read_json(h)
        rev = body.pop("rev", None)
        if isinstance(rev, bool) or not isinstance(rev, int):
            raise srv.HTTPError(400, "rev must be a number")
        try:
            _json_ok(h, calendar.update(eid, rev, body))
        except Conflict as exc:
            raise _conflict(exc) from None

    def r_delete(h, m):
        eid = calendar.check_id(m.group(1))
        body = srv.read_json(h)
        rev = body.get("rev")
        if rev is not None and (isinstance(rev, bool) or not isinstance(rev, int)):
            raise srv.HTTPError(400, "rev must be a number")
        try:
            calendar.delete(eid, rev)
        except Conflict as exc:
            raise _conflict(exc) from None
        _json_ok(h, {"ok": True})

    def r_ics(h, m):
        # Same guard as every route; see the module docstring for what that means for subscribers.
        body = calendar.to_ics(calendar.list_entries(*_range(h)), CAL_NAME, datetime.now(timezone.utc))
        srv._send_raw(h, 200, body.encode("utf-8"), "text/calendar; charset=utf-8",
                      {"Cache-Control": "no-cache",
                       "Content-Disposition": 'inline; filename="yourcontent-calendar.ics"'})

    def _feed_info(h, secret):
        host = (h.headers.get("Host") or "127.0.0.1").strip()
        proto = "https" if (h.headers.get("X-Forwarded-Proto") or "").lower() == "https" else "http"
        path = f"/api/calendar/feed/{secret}.ics"
        return {"url": f"{proto}://{host}{path}", "path": path}

    def r_feed(h, m):
        srv._json(h, 200, _feed_info(h, feed_secret()))

    def r_feed_rotate(h, m):
        srv.read_json(h)
        srv._json(h, 200, _feed_info(h, feed_secret(rotate=True)))

    def r_feed_ics(h, m):
        if not hmac.compare_digest(m.group(1).encode(), feed_secret().encode()):
            raise srv.HTTPError(404, "not found")
        body = calendar.to_ics(calendar.list_entries(None, None), CAL_NAME, datetime.now(timezone.utc))
        srv._send_raw(h, 200, body.encode("utf-8"), "text/calendar; charset=utf-8", {"Cache-Control": "no-cache"})

    def _json_ok(h, obj):
        srv._json(h, 200, obj)

    return [("GET", r"/api/calendar/feed", r_feed),
            ("POST", r"/api/calendar/feed/rotate", r_feed_rotate),
            ("GET", r"/api/calendar/feed/([A-Za-z0-9_-]{43})\.ics", r_feed_ics),
            ("GET", r"/api/calendar", r_list),
            ("POST", r"/api/calendar", r_create),
            ("GET", r"/api/calendar\.ics", r_ics),
            ("POST", r"/api/calendar/([^/?#]+)", r_update),
            ("POST", r"/api/calendar/([^/?#]+)/delete", r_delete)]
