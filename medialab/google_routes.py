"""HTTP routes for Connect Google (medialab/google.py). Installed with `server.install_extension(google_routes)`.

    GET  /api/google/status          configured? connected? email, workspace IDs and links (never a token)
    POST /api/google/connect         -> {url}: the consent URL; sets the one-use binding cookie
    GET  /api/google/callback        Google sends the browser here; validates state, exchanges the code,
                                     then redirects to /#google
    POST /api/google/bootstrap       {name?} create or repair the workspace (idempotent)
    POST /api/google/disconnect      revoke at Google, delete local tokens
    POST /api/google/upload          {ref, folder: "exports" | "rendered"}
    GET  /api/google/inbox           files in Inbox/ that YourContent can see
    POST /api/google/inbox/import    {file_id}
    POST /api/google/manual          {folders: {root, inbox?, ...}, sheet?} pasted IDs (no OAuth)

The callback is a GET that changes state. How the server's guard treats it:
- guard() checks the Host allowlist on every request, GET included, so the callback must arrive on an
  allowed host. It does: the redirect URI is built from the Host the page itself used for POST /connect.
- guard() applies the X-Media-Lab header and Origin checks to POSTs only, so a top-level GET coming back
  from Google is not blocked by them (and could not satisfy them: a redirect carries neither header).
- With MEDIA_LAB_TOKEN set, the session cookie is SameSite=Strict, and a browser does not send Strict cookies
  on a navigation that started on another site (Google's consent page). So routes() adds the callback to
  server.PUBLIC, and the callback protects itself instead: the `state` must match a pending entry (single use,
  10-minute expiry) AND the browser must bring back the binding cookie set on the authenticated POST /connect
  (SameSite=Lax, Path=/api/google/callback, HttpOnly, 10 minutes). Only someone who could already call
  /connect can finish a sign-in, and only in that browser; a forged callback changes nothing.
"""
from __future__ import annotations

import urllib.parse

from . import config as config_mod, google, store

BIND_COOKIE = "ml_gbind"

STATIC = {"/static/google.js": ("google.js", "text/javascript; charset=utf-8"),
          "/static/google.css": ("google.css", "text/css; charset=utf-8")}


def client(h) -> google.Google:
    """The server's Google client (tests may set `srv.google` before the first request)."""
    srv = h.server
    g = getattr(srv, "google", None)
    if g is None:
        with srv.state.lock:
            g = getattr(srv, "google", None)
            if g is None:
                g = srv.google = google.Google()
    return g


def redirect_uri(h) -> str:
    """http://<Host>/api/google/callback. Host already passed guard()'s allowlist. Google's desktop-app
    clients accept any port on a loopback address; off loopback, https is used when a trusted proxy says so."""
    host = (h.headers.get("Host") or "").strip()
    name = host[1:host.find("]")] if host.startswith("[") else host.partition(":")[0]
    scheme = "http"
    if not config_mod.is_loopback(name) and (h.headers.get("X-Forwarded-Proto") or "").lower() == "https":
        scheme = "https"
    return f"{scheme}://{host}{google.CALLBACK_PATH}"


def routes(srv):
    HTTPError, _json, read_json = srv.HTTPError, srv._json, srv.read_json
    srv.PUBLIC.add(("GET", google.CALLBACK_PATH))   # see the module docstring: state + binding cookie guard it

    def r_status(h, m):
        _json(h, 200, client(h).status())

    def r_connect(h, m):
        read_json(h)
        g = client(h)
        ok, why = g.configured()
        if not ok:
            raise HTTPError(409, why or "Google isn't set up", {"setup_doc": google.SETUP_DOC})
        url, binding = g.begin(redirect_uri(h))
        cookie = (f"{BIND_COOKIE}={binding}; HttpOnly; SameSite=Lax; Path={google.CALLBACK_PATH}; "
                  f"Max-Age={google.STATE_TTL}")
        if (h.headers.get("X-Forwarded-Proto") or "").lower() == "https":
            cookie += "; Secure"
        _json(h, 200, {"url": url}, {"Set-Cookie": cookie})

    def r_callback(h, m):
        g = client(h)
        q = urllib.parse.parse_qs(h.path.partition("?")[2], keep_blank_values=True)
        one = lambda k: (q.get(k) or [None])[-1]  # noqa: E731
        try:
            g.finish(one("state"), one("code"), srv._cookie(h, BIND_COOKIE), one("error"))
        except google.GoogleError as e:
            g.last_error = e.message          # a fixed sentence; nothing from the query string is echoed
        clear = f"{BIND_COOKIE}=; HttpOnly; SameSite=Lax; Path={google.CALLBACK_PATH}; Max-Age=0"
        srv._send_raw(h, 302, b"", "text/plain; charset=utf-8",
                      {"Location": "/#google", "Set-Cookie": clear, "Cache-Control": "no-store"})

    def r_bootstrap(h, m):
        body = read_json(h)
        _json(h, 200, {"workspace": client(h).bootstrap(body.get("name"))})

    def r_disconnect(h, m):
        read_json(h)
        _json(h, 200, client(h).disconnect())

    def r_upload(h, m):
        import os
        body = read_json(h)
        ref = store.require_owned(body.get("ref"))
        folder = body.get("folder") or "exports"
        if folder not in google.UPLOAD_FOLDERS:
            raise HTTPError(400, "folder must be exports or rendered")
        path = store.resolve(ref)
        if not os.path.isfile(path):
            raise HTTPError(404, "not found")
        g = client(h)
        out = g.upload(path, ref[1], srv._ctype_for(ref[1]), folder)
        sc = store.sidecar(ref) or {}
        lic = sc.get("most_restrictive") or ",".join(map(str, sc.get("licences") or [])) or ""
        ok = sc.get("commercial_ok")
        try:
            g.append_rows(google.ASSETS_TAB, [[ref[1], sc.get("kind") or store.media_type(ref[1]) or "", folder,
                                               out["id"], lic, "" if ok is None else ("yes" if ok else "no"),
                                               store.now_iso()]])
            out["sheet_row"] = True
        except google.GoogleError:
            out["sheet_row"] = False          # the file is up; the Assets row is a convenience
        _json(h, 200, out)

    def r_inbox(h, m):
        _json(h, 200, {"files": client(h).inbox()})

    def r_import(h, m):
        body = read_json(h)
        _json(h, 200, client(h).import_file(body.get("file_id")))

    def r_manual(h, m):
        body = read_json(h)
        _json(h, 200, {"workspace": google.manual_workspace(body.get("folders"), body.get("sheet"))})

    return [
        ("GET", r"/api/google/status", r_status),
        ("POST", r"/api/google/connect", r_connect),
        ("GET", r"/api/google/callback", r_callback),
        ("POST", r"/api/google/bootstrap", r_bootstrap),
        ("POST", r"/api/google/disconnect", r_disconnect),
        ("POST", r"/api/google/upload", r_upload),
        ("GET", r"/api/google/inbox", r_inbox),
        ("POST", r"/api/google/inbox/import", r_import),
        ("POST", r"/api/google/manual", r_manual),
    ]
