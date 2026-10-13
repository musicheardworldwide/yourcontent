"""Connect Google (medialab/google.py + google_routes.py) against an in-process fake Google. No real network.

Covers: PKCE, state single use / expiry / browser binding, token file permissions, refresh and revocation,
bootstrap idempotency (and repair after a crash or a lost record), streamed resumable upload, Inbox listing
and import, Sheet rows, error mapping, the manual fallback, and the routes through the real server (guard,
token mode, callback redirect). No token, secret, code or verifier may appear in any response or log line.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import http.client
import io
import json
import os
import re
import secrets
import stat
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from medialab import google, google_routes, jobs, server, store
from tests.util import TempRoots, make_png, requires_ffmpeg

CLIENT_ID = "1234-test.apps.googleusercontent.com"
SECRET = "test-client-secret-" + secrets.token_hex(4)
ENV = {google.ENV_ID: CLIENT_ID, google.ENV_SECRET: SECRET}
REDIRECT = "http://127.0.0.1:8190/api/google/callback"
MY_DRIVE = "0AmyDriveRootFolderXX"

if "/static/google.js" not in server.STATIC:
    server.install_extension(google_routes)


# --------------------------------------------------------------------------------------------- fake Google

class FakeGoogle:
    """OAuth token/revoke, Drive v3 (about, files create/list/get/media/patch, resumable upload) and Sheets v4
    (create, values get/update/append), with the shapes YourContent reads. Every API call needs the current
    access token. `fail` holds one-shot (method, path prefix, status, json) answers."""

    def __init__(self):
        self.lock = threading.RLock()
        self.files: dict[str, dict] = {}
        self.sheets: dict[str, dict] = {}
        self.codes: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}
        self.access: str | None = None
        self.refresh: str | None = None
        self.issued: list[str] = []          # every token / code handed out (for the leak checks)
        self.revoked: list[bytes] = []
        self.requests: list[tuple[str, str, dict, bytes]] = []
        self.fail: list[tuple[str, str, int, dict]] = []
        self.grant_scope = google.SCOPE
        self.email = "person@example.com"
        self._n = 0

    def start(self):
        fake = self

        class H(_Handler):
            owner = fake

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    @property
    def endpoints(self):
        b = self.base
        return google.Endpoints(auth=b + "/o/oauth2/v2/auth", token=b + "/token", revoke=b + "/revoke",
                                drive=b + "/drive/v3", upload=b + "/upload/drive/v3", sheets=b + "/v4/spreadsheets")

    def new_id(self, prefix="f"):
        self._n += 1
        return f"{prefix}{self._n:04d}{secrets.token_hex(8)}"

    def authorize(self, url: str) -> tuple[str, str]:
        """What the person's browser does at Google: returns (code, state)."""
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        code = "4/FAKE-CODE-" + secrets.token_hex(8)
        with self.lock:
            self.codes[code] = q
            self.issued.append(code)
        return code, q["state"]

    def add_file(self, name, parent, data=b"", mime="image/png", app=False):
        fid = self.new_id()
        with self.lock:
            self.files[fid] = {"id": fid, "name": name, "mimeType": mime, "parents": [parent], "trashed": False,
                               "appProperties": {"mediaLab": "upload"} if app else {}, "data": data}
        return fid

    def creates(self):
        return [r for r in self.requests if r[0] == "POST" and r[1].split("?")[0] in ("/drive/v3/files", "/v4/spreadsheets")]

    def calls(self, method, prefix):
        return [r for r in self.requests if r[0] == method and r[1].startswith(prefix)]


class _Handler(BaseHTTPRequestHandler):
    owner: FakeGoogle
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, obj=None, raw=None, headers=None, ctype="application/json"):
        body = raw if raw is not None else (json.dumps(obj).encode() if obj is not None else b"")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        out = b""
        while len(out) < n:
            b = self.rfile.read(min(65536, n - len(out)))
            if not b:
                break
            out += b
        return out

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    def do_PATCH(self):
        self._route("PATCH")

    def _route(self, method):
        g = self.owner
        raw = self._body()
        path, _, query = self.path.partition("?")
        q = dict(urllib.parse.parse_qsl(query))
        with g.lock:
            g.requests.append((method, self.path, dict(self.headers.items()), raw))
            for i, (m, pre, code, obj) in enumerate(g.fail):
                if m == method and path.startswith(pre):
                    del g.fail[i]
                    return self._send(code, obj)
        if path == "/token":
            return self._token(dict(urllib.parse.parse_qsl(raw.decode())))
        if path == "/revoke":
            with g.lock:
                g.revoked.append(raw)
                g.access = g.refresh = None
            return self._send(200, {})
        if self.headers.get("Authorization") != f"Bearer {g.access}" or not g.access:
            return self._send(401, {"error": {"code": 401, "message": "Request had invalid authentication credentials."}})
        with g.lock:
            if path == "/drive/v3/about":
                return self._send(200, {"user": {"emailAddress": g.email, "displayName": "Person"}})
            if path == "/drive/v3/files" and method == "POST":
                meta = json.loads(raw)
                fid = g.new_id()
                g.files[fid] = {"id": fid, "name": meta["name"], "mimeType": meta["mimeType"], "trashed": False,
                                "parents": meta.get("parents") or [MY_DRIVE],
                                "appProperties": meta.get("appProperties") or {}, "data": b""}
                return self._send(200, {"id": fid})
            if path == "/drive/v3/files" and method == "GET":
                return self._send(200, {"files": [self._meta(f) for f in self._query(q.get("q", ""))]})
            m = re.fullmatch(r"/drive/v3/files/([^/]+)", path)
            if m:
                f = g.files.get(m.group(1))
                if not f:
                    return self._send(404, {"error": {"code": 404, "message": "File not found."}})
                if method == "GET" and q.get("alt") == "media":
                    return self._send(200, raw=f["data"], ctype=f["mimeType"])
                if method == "GET":
                    return self._send(200, self._meta(f))
                if method == "PATCH":
                    meta = json.loads(raw or b"{}")
                    rm = set((q.get("removeParents") or "").split(",")) - {""}
                    f["parents"] = [p for p in f["parents"] if p not in rm]
                    if q.get("addParents") and q["addParents"] not in f["parents"]:
                        f["parents"].append(q["addParents"])
                    f["appProperties"].update(meta.get("appProperties") or {})
                    return self._send(200, {"id": f["id"], "parents": f["parents"]})
            if path == "/upload/drive/v3/files" and method == "POST" and q.get("uploadType") == "resumable":
                sid = secrets.token_hex(6)
                g.sessions[sid] = {"meta": json.loads(raw), "type": self.headers.get("X-Upload-Content-Type"),
                                   "length": int(self.headers.get("X-Upload-Content-Length"))}
                loc = f"{g.base}/upload/drive/v3/files?uploadType=resumable&upload_id={sid}"
                return self._send(200, raw=b"", headers={"Location": loc})
            if path == "/upload/drive/v3/files" and method == "PUT":
                s = g.sessions.pop(q.get("upload_id"), None)
                if not s or len(raw) != s["length"]:
                    return self._send(400, {"error": {"code": 400, "message": "bad upload"}})
                fid = g.new_id()
                g.files[fid] = {"id": fid, "name": s["meta"]["name"], "mimeType": s["type"], "trashed": False,
                                "parents": s["meta"]["parents"], "appProperties": s["meta"].get("appProperties") or {},
                                "data": raw}
                return self._send(200, {"id": fid, "name": s["meta"]["name"],
                                        "webViewLink": f"https://drive.google.com/file/d/{fid}/view"})
            if path == "/v4/spreadsheets" and method == "POST":
                body = json.loads(raw)
                sid = g.new_id("s")
                g.sheets[sid] = {"title": body["properties"]["title"],
                                 "tabs": {s["properties"]["title"]: [] for s in body["sheets"]}}
                g.files[sid] = {"id": sid, "name": body["properties"]["title"], "trashed": False,
                                "mimeType": google.SHEET_MIME, "parents": [MY_DRIVE], "appProperties": {}, "data": b""}
                return self._send(200, {"spreadsheetId": sid})
            m = re.fullmatch(r"/v4/spreadsheets/([^/]+)/values/([^/]+?)(:append)?", path)
            if m:
                sh = g.sheets.get(m.group(1))
                rng = urllib.parse.unquote(m.group(2))
                mm = re.fullmatch(r"'([^']+)'!A(\d*)(?::([A-Z]))?", rng)
                if not sh or not mm or mm.group(1) not in sh["tabs"]:
                    return self._send(400, {"error": {"code": 400, "message": "Unable to parse range"}})
                rows = sh["tabs"][mm.group(1)]
                if method == "GET":
                    width = ord(mm.group(3) or "A") - ord("A") + 1   # 'Tab'!A:A or 'Tab'!A:I
                    return self._send(200, {"values": [list(r[:width]) for r in rows]})
                vals = json.loads(raw)["values"]
                if m.group(3):
                    rows.extend(vals)
                    return self._send(200, {"updates": {"updatedRows": len(vals)}})
                start = int(mm.group(2) or 1) - 1
                for i, v in enumerate(vals):
                    while len(rows) <= start + i:
                        rows.append([])
                    rows[start + i] = v
                return self._send(200, {"updatedRows": len(vals)})
        return self._send(404, {"error": {"code": 404, "message": "no such fake route"}})

    def _token(self, form):
        g = self.owner
        with g.lock:
            if form.get("client_id") != CLIENT_ID or form.get("client_secret") != SECRET:
                return self._send(401, {"error": "invalid_client"})
            if form.get("grant_type") == "authorization_code":
                req = g.codes.pop(form.get("code"), None)
                ch = base64.urlsafe_b64encode(hashlib.sha256(form.get("code_verifier", "").encode()).digest()).rstrip(b"=").decode()
                if not req or req["code_challenge"] != ch or req["redirect_uri"] != form.get("redirect_uri"):
                    return self._send(400, {"error": "invalid_grant"})
                g.access = "ya29.FAKE-ACCESS-" + secrets.token_hex(12)
                g.refresh = "1//FAKE-REFRESH-" + secrets.token_hex(12)
                g.issued += [g.access, g.refresh]
                return self._send(200, {"access_token": g.access, "expires_in": 3599, "refresh_token": g.refresh,
                                        "scope": g.grant_scope, "token_type": "Bearer"})
            if form.get("grant_type") == "refresh_token":
                if not g.refresh or form.get("refresh_token") != g.refresh:
                    return self._send(400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
                g.access = "ya29.FAKE-ACCESS-" + secrets.token_hex(12)
                g.issued.append(g.access)
                return self._send(200, {"access_token": g.access, "expires_in": 3599, "scope": g.grant_scope})
        return self._send(400, {"error": "unsupported_grant_type"})

    def _meta(self, f):
        out = {k: v for k, v in f.items() if k != "data"}
        out["size"] = str(len(f["data"]))
        return out

    def _query(self, q):
        g = self.owner
        out = []
        for f in g.files.values():
            if "trashed=false" in q and f["trashed"]:
                continue
            m = re.search(r"'([^']+)' in parents", q)
            if m and m.group(1) not in f["parents"]:
                continue
            m = re.search(r"value='([^']+)'", q)
            if m and f["appProperties"].get("mediaLab") != m.group(1):
                continue
            m = re.search(r"mimeType='([^']+)'", q)
            if m and f["mimeType"] != m.group(1):
                continue
            out.append(f)
        return out


# --------------------------------------------------------------------------------------------- unit tests

class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class GoogleCase(unittest.TestCase):
    def setUp(self):
        self.r = TempRoots()
        self.addCleanup(self.r.cleanup)
        store.configure(self.r.config())
        self.fake = FakeGoogle().start()
        self.addCleanup(self.fake.stop)
        self.clock = Clock()
        self.ingested: list[tuple[bytes, int, str, str]] = []
        self.g = google.Google(endpoints=self.fake.endpoints, env=dict(ENV), clock=self.clock, ingest=self._ingest)

    def _ingest(self, stream, length, name, mime):
        data = stream.read(length)
        self.ingested.append((data, length, name, mime))
        return {"ref": {"root": "in", "name": "ref_x.png"}, "label": name}

    def connect(self):
        url, binding = self.g.begin(REDIRECT)
        code, state = self.fake.authorize(url)
        return self.g.finish(state, code, binding)

    def secrets_seen(self):
        return [s for s in self.fake.issued + [SECRET] if s]


class TestOAuth(GoogleCase):
    def test_pkce_pair_is_s256(self):
        v, c = google.pkce_pair()
        self.assertRegex(v, r"^[A-Za-z0-9_-]{43,128}$")
        self.assertEqual(c, base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode())
        self.assertNotEqual(google.pkce_pair()[0], v)

    def test_consent_url_asks_for_drive_file_only(self):
        url, _ = self.g.begin(REDIRECT)
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        self.assertEqual(q["scope"], "https://www.googleapis.com/auth/drive.file")
        self.assertEqual(q["code_challenge_method"], "S256")
        self.assertEqual(q["redirect_uri"], REDIRECT)
        self.assertEqual(q["client_id"], CLIENT_ID)
        self.assertEqual(q["access_type"], "offline")
        self.assertNotIn("include_granted_scopes", q)
        self.assertGreaterEqual(len(q["state"]), 32)
        self.assertNotIn(SECRET, url)

    def test_connect_stores_tokens_0600_and_status_has_none(self):
        st = self.connect()
        self.assertTrue(st["connected"])
        self.assertEqual(st["email"], "person@example.com")
        p = google.tokens_path()
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        with open(p) as f:
            saved = json.load(f)
        self.assertEqual(saved["refresh_token"], self.fake.refresh)
        blob = json.dumps(self.g.status())
        for s in self.secrets_seen():
            self.assertNotIn(s, blob)

    def test_state_is_single_use(self):
        url, binding = self.g.begin(REDIRECT)
        code, state = self.fake.authorize(url)
        self.g.finish(state, code, binding)
        with self.assertRaises(google.GoogleError) as cm:
            self.g.finish(state, code, binding)
        self.assertEqual(cm.exception.status, 400)
        self.assertIn("already used", cm.exception.message)

    def test_state_expires_after_ten_minutes(self):
        url, binding = self.g.begin(REDIRECT)
        code, state = self.fake.authorize(url)
        self.clock.t += google.STATE_TTL + 1
        with self.assertRaises(google.GoogleError) as cm:
            self.g.finish(state, code, binding)
        self.assertIn("expired", cm.exception.message)
        self.assertEqual(self.g.pending, {})
        self.assertFalse(self.fake.calls("POST", "/token"))

    def test_state_is_bound_to_the_browser(self):
        url, _ = self.g.begin(REDIRECT)
        code, state = self.fake.authorize(url)
        for wrong in (None, "", "someone-else"):
            url, binding = self.g.begin(REDIRECT)
            code, state = self.fake.authorize(url)
            with self.assertRaises(google.GoogleError) as cm:
                self.g.finish(state, code, wrong)
            self.assertIn("same browser", cm.exception.message)
        self.assertFalse(self.g.connected())

    def test_unknown_state_and_cancel(self):
        with self.assertRaises(google.GoogleError):
            self.g.finish("made-up", "code", "b")
        url, binding = self.g.begin(REDIRECT)
        _, state = self.fake.authorize(url)
        with self.assertRaises(google.GoogleError) as cm:
            self.g.finish(state, None, binding, "access_denied")
        self.assertIn("cancelled", cm.exception.message)

    def test_unticked_drive_box_is_refused(self):
        self.fake.grant_scope = "openid"
        with self.assertRaises(google.GoogleError) as cm:
            self.connect()
        self.assertIn("tick", cm.exception.message)
        self.assertFalse(os.path.exists(google.tokens_path()))

    def test_not_configured(self):
        g = google.Google(endpoints=self.fake.endpoints, env={})
        st = g.status()
        self.assertFalse(st["configured"])
        self.assertIn("docs/GOOGLE.md", st["reason"])
        self.assertIn(google.ENV_ID, st["reason"])
        with self.assertRaises(google.NotConfigured):
            g.begin(REDIRECT)
        bad = google.Google(endpoints=self.fake.endpoints, env={google.ENV_ID: "nope"})
        self.assertFalse(bad.configured()[0])

    def test_refresh_when_expired(self):
        self.connect()
        first = self.fake.access
        self.clock.t += 4000
        self.g.bootstrap()
        self.assertNotEqual(self.fake.access, first)
        self.assertEqual(len([r for r in self.fake.calls("POST", "/token") if b"refresh_token" in r[3]]), 1)
        with open(google.tokens_path()) as f:
            self.assertEqual(json.load(f)["access_token"], self.fake.access)

    def test_revoked_refresh_disconnects(self):
        self.connect()
        self.fake.refresh = None
        self.clock.t += 4000
        with self.assertRaises(google.NotConnected) as cm:
            self.g.bootstrap()
        self.assertEqual(cm.exception.status, 409)
        self.assertFalse(os.path.exists(google.tokens_path()))

    def test_disconnect_revokes_in_the_body(self):
        self.connect()
        rt = self.fake.refresh
        out = self.g.disconnect()
        self.assertEqual(out, {"connected": False, "revoked": True})
        req = self.fake.calls("POST", "/revoke")[0]
        self.assertEqual(req[1], "/revoke")                    # nothing in the URL
        self.assertIn(urllib.parse.quote(rt, safe=""), req[3].decode())
        self.assertFalse(os.path.exists(google.tokens_path()))
        self.assertFalse(self.g.status()["connected"])

    def test_disconnect_still_forgets_when_revoke_fails(self):
        self.connect()
        self.fake.fail.append(("POST", "/revoke", 500, {}))
        self.assertEqual(self.g.disconnect()["revoked"], False)
        self.assertFalse(os.path.exists(google.tokens_path()))


class TestWorkspace(GoogleCase):
    def setUp(self):
        super().setUp()
        self.connect()

    def tree(self):
        ws = self.g.workspace()
        return ws, self.fake.files

    def test_bootstrap_builds_the_workspace(self):
        out = self.g.bootstrap("Studio Files")
        ws, files = self.tree()
        root = ws["folders"]["root"]
        self.assertEqual(files[root]["name"], "Studio Files")
        self.assertEqual(files[root]["mimeType"], google.FOLDER_MIME)
        for key, title in google.SUBFOLDERS:
            f = files[ws["folders"][key]]
            self.assertEqual((f["name"], f["parents"]), (title, [root]))
        sid = ws["sheet"]
        self.assertEqual(files[sid]["parents"], [root])          # moved out of My Drive
        self.assertEqual(files[sid]["appProperties"], {"mediaLab": "sheet"})
        tabs = self.fake.sheets[sid]["tabs"]
        self.assertEqual(tabs["Content Plan"], [["id", "when", "channel", "title", "caption", "status", "project",
                                                 "asset", "updated"]])
        self.assertEqual(tabs["Assets"], [["name", "kind", "folder", "drive_file_id", "licence", "commercial_ok",
                                           "created"]])
        self.assertTrue(out["complete"])
        self.assertEqual(out["sheet"]["url"], f"https://docs.google.com/spreadsheets/d/{sid}/edit")
        self.assertEqual(stat.S_IMODE(os.stat(google.workspace_path()).st_mode), 0o600)

    def test_bootstrap_is_idempotent(self):
        self.g.bootstrap()
        n = len(self.fake.creates())
        self.assertEqual(n, 6)
        before = self.g.workspace()
        self.g.bootstrap()
        self.g.bootstrap("Another name")
        self.assertEqual(len(self.fake.creates()), n)
        self.assertEqual(self.g.workspace()["folders"], before["folders"])
        self.assertEqual(len(self.fake.sheets[before["sheet"]]["tabs"]["Content Plan"]), 1)

    def test_lost_record_is_found_again(self):
        self.g.bootstrap()
        before = self.g.workspace()
        n = len(self.fake.creates())
        os.remove(google.workspace_path())
        self.g.bootstrap()
        self.assertEqual(len(self.fake.creates()), n)
        after = self.g.workspace()
        self.assertEqual((after["folders"], after["sheet"]), (before["folders"], before["sheet"]))

    def test_trashed_folder_is_recreated_alone(self):
        self.g.bootstrap()
        ws = self.g.workspace()
        self.fake.files[ws["folders"]["exports"]]["trashed"] = True
        n = len(self.fake.creates())
        self.g.bootstrap()
        self.assertEqual(len(self.fake.creates()), n + 1)
        self.assertNotEqual(self.g.workspace()["folders"]["exports"], ws["folders"]["exports"])

    def test_crash_mid_bootstrap_resumes_without_duplicates(self):
        self.fake.fail.append(("PUT", "/v4/spreadsheets/", 503, {"error": {"code": 503, "message": "backend"}}))
        with self.assertRaises(google.GoogleError) as cm:
            self.g.bootstrap()
        self.assertEqual(cm.exception.status, 503)
        ws = self.g.workspace()
        self.assertTrue(ws["sheet"])
        self.assertFalse(ws["headers_ok"])
        self.g.bootstrap()
        self.assertEqual(len(self.fake.sheets), 1)
        ws = self.g.workspace()
        self.assertTrue(ws["headers_ok"] and ws["moved"])

    def test_bad_names(self):
        for bad in ("x" * 101, "   ", 5):
            with self.assertRaises(google.GoogleError):
                self.g.bootstrap(bad)

    def test_upload_needs_a_workspace(self):
        p = self.r.put("lab", "a.mp4", b"x" * 10)
        with self.assertRaises(google.GoogleError) as cm:
            self.g.upload(p, "a.mp4", "video/mp4", "exports")
        self.assertEqual(cm.exception.status, 409)


class TestTransfer(GoogleCase):
    def setUp(self):
        super().setUp()
        self.connect()
        self.g.bootstrap()
        self.ws = self.g.workspace()

    def test_upload_streams_in_chunks(self):
        data = os.urandom(100_000)
        p = self.r.put("lab", "cut.mp4", data)
        readers = []
        real = google._Reader

        class Spy(real):
            def __init__(self, *a):
                super().__init__(*a)
                readers.append(self)

        with mock.patch.object(google, "CHUNK", 4096), mock.patch.object(google, "_Reader", Spy):
            out = self.g.upload(p, "cut.mp4", "video/mp4", "exports")
        f = self.fake.files[out["id"]]
        self.assertEqual(f["data"], data)
        self.assertEqual(f["parents"], [self.ws["folders"]["exports"]])
        self.assertEqual(f["mimeType"], "video/mp4")
        self.assertEqual(out["bytes"], len(data))
        self.assertLessEqual(readers[0].max_read, 4096)
        self.assertTrue(out["url"].startswith("https://drive.google.com/file/d/"))
        init = self.fake.calls("POST", "/upload/drive/v3/files")[0]
        self.assertEqual(init[2].get("X-Upload-Content-Length"), str(len(data)))

    def test_upload_limits(self):
        p = self.r.put("lab", "big.mp4", b"x" * 2048)
        with mock.patch.object(google.Google, "max_bytes", lambda self: 1024):
            with self.assertRaises(google.GoogleError) as cm:
                self.g.upload(p, "big.mp4", "video/mp4", "rendered")
        self.assertEqual(cm.exception.status, 413)
        with self.assertRaises(google.GoogleError) as cm:
            self.g.upload(p, "big.mp4", "video/mp4", "inbox")
        self.assertEqual(cm.exception.status, 400)
        empty = self.r.put("lab", "e.mp4", b"")
        with self.assertRaises(google.GoogleError):
            self.g.upload(empty, "e.mp4", "video/mp4", "exports")

    def test_upload_refuses_a_foreign_session_address(self):
        p = self.r.put("lab", "c.mp4", b"x" * 100)
        orig = self.g.t.request

        def evil(method, url, headers=None, body=None, **kw):
            r = orig(method, url, headers, body, **kw)
            if "uploadType=resumable" in url and method == "POST":
                r.headers["location"] = "https://attacker.example/upload?id=1"
            return r

        with mock.patch.object(self.g.t, "request", evil):
            with self.assertRaises(google.GoogleError) as cm:
                self.g.upload(p, "c.mp4", "video/mp4", "exports")
        self.assertEqual(cm.exception.status, 502)
        self.assertFalse(self.fake.calls("PUT", "/upload"))

    def test_inbox_list_and_import(self):
        png = make_png(w=4, h=4)
        fid = self.fake.add_file("holiday.png", self.ws["folders"]["inbox"], png, app=True)
        self.fake.add_file("notes", self.ws["folders"]["inbox"], b"", mime="application/vnd.google-apps.document")
        self.fake.add_file("elsewhere.png", MY_DRIVE, png)
        files = self.g.inbox()
        self.assertEqual({f["name"] for f in files}, {"holiday.png", "notes"})
        row = next(f for f in files if f["id"] == fid)
        self.assertEqual((row["size"], row["importable"]), (len(png), True))
        out = self.g.import_file(fid)
        self.assertEqual(out["label"], "holiday.png")
        self.assertEqual(self.ingested, [(png, len(png), "holiday.png", "image/png")])

    def test_import_refusals(self):
        other = self.fake.add_file("x.png", MY_DRIVE, b"abc")
        doc = self.fake.add_file("d", self.ws["folders"]["inbox"], b"", mime="application/vnd.google-apps.document")
        big = self.fake.add_file("big.mp4", self.ws["folders"]["inbox"], b"x" * 2048, mime="video/mp4")
        cases = [(other, 400), (doc, 415), ("../etc", 400), ("short", 400), (None, 400)]
        for fid, code in cases:
            with self.assertRaises(google.GoogleError) as cm:
                self.g.import_file(fid)
            self.assertEqual(cm.exception.status, code, fid)
        with mock.patch.object(google.Google, "max_bytes", lambda self: 1024):
            with self.assertRaises(google.GoogleError) as cm:
                self.g.import_file(big)
        self.assertEqual(cm.exception.status, 413)
        self.assertEqual(self.ingested, [])

    def test_sheet_rows(self):
        sid = self.ws["sheet"]
        self.assertEqual(self.g.append_rows("Content Plan", [["c1", "2026-10-11", "instagram", "Title"]]), 1)
        out = self.g.upsert_row("Content Plan", ["c1", "2026-10-12", "instagram", "New title"])
        self.assertEqual(out, {"row": 2, "updated": True})
        out = self.g.upsert_row("Content Plan", ["c2", "2026-10-13"])
        self.assertEqual(out, {"row": 3, "updated": False})
        rows = self.fake.sheets[sid]["tabs"]["Content Plan"]
        self.assertEqual(rows[1], ["c1", "2026-10-12", "instagram", "New title"])
        self.assertEqual(rows[2], ["c2", "2026-10-13"])
        with self.assertRaises(google.GoogleError):
            self.g.append_rows("Secrets", [["x"]])
        with self.assertRaises(google.GoogleError):
            self.g.append_rows("Assets", [list("abcdefgh")])

    def test_error_mapping(self):
        cases = [(403, {"error": {"code": 403, "message": "The user does not have sufficient permissions."}}, 502),
                 (404, {"error": {"code": 404}}, 404), (500, {}, 503), (429, {}, 503), (400, {"error": "x"}, 502)]
        for status, body, want in cases:
            self.fake.fail.append(("GET", "/drive/v3/files", status, body))
            with self.assertRaises(google.GoogleError) as cm:
                self.g.inbox()
            self.assertEqual(cm.exception.status, want, status)
            self.assertNotEqual(cm.exception.status, 401)
        self.assertIn("sufficient permissions",
                      self._msg(403, {"error": {"code": 403, "message": "The user does not have sufficient permissions."}}))

    def _msg(self, status, body):
        self.fake.fail.append(("GET", "/drive/v3/files", status, body))
        try:
            self.g.inbox()
        except google.GoogleError as e:
            return e.message
        return ""

    def test_401_retries_once_with_a_fresh_token(self):
        self.fake.fail.append(("GET", "/drive/v3/files", 401, {"error": {"code": 401}}))
        self.assertEqual(self.g.inbox(), [])
        self.fake.fail += [("GET", "/drive/v3/files", 401, {}), ("GET", "/drive/v3/files", 401, {})]
        with self.assertRaises(google.NotConnected):
            self.g.inbox()

    def test_unreachable_google_is_503(self):
        dead = google.Endpoints(**{k: "http://127.0.0.1:9/x" for k in google.Endpoints.__dataclass_fields__})
        g = google.Google(endpoints=dead, env=dict(ENV), clock=self.clock)
        url, binding = g.begin(REDIRECT)
        _, state = self.fake.authorize(url)
        with self.assertRaises(google.GoogleError) as cm:
            g.finish(state, "4/code", binding)
        self.assertEqual(cm.exception.status, 503)
        self.assertNotIn("127.0.0.1", cm.exception.message)


class TestManual(GoogleCase):
    def test_validate_manual_is_pure_and_strict(self):
        good = {"root": "1AbCdEfGhIjKlMnOpQrStU", "inbox": "1ZyXwVuTsRqPoNmLkJiHg"}
        ws = google.validate_manual(good, "1SheetIdAbCdEfGhIjKlMnOp")
        self.assertEqual(ws["source"], "manual")
        self.assertFalse(os.path.exists(google.workspace_path()))
        for folders, sheet in [({}, None), ({"inbox": good["inbox"]}, None), ({"root": "x/../y"}, None),
                               ({"root": good["root"], "evil": good["root"]}, None), ([], None),
                               ({"root": good["root"]}, "no spaces allowed")]:
            with self.assertRaises(google.GoogleError):
                google.validate_manual(folders, sheet)

    def test_manual_workspace_saves(self):
        out = google.manual_workspace({"root": "1AbCdEfGhIjKlMnOpQrStU"}, "1SheetIdAbCdEfGhIjKlMnOp")
        self.assertEqual(out["folders"]["root"]["url"], "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStU")
        self.assertFalse(out["complete"])
        self.assertEqual(self.g.status()["workspace"]["source"], "manual")


# --------------------------------------------------------------------------------------------- routes

class RouteCase(unittest.TestCase):
    TOKEN = None

    def setUp(self):
        self.r = TempRoots()
        self.addCleanup(self.r.cleanup)
        self.fake = FakeGoogle().start()
        self.addCleanup(self.fake.stop)
        jobs.reset()
        self.addCleanup(jobs.reset)
        cfg = self.r.config(token=self.TOKEN) if self.TOKEN else self.r.config()
        self.srv = server.make_server(cfg, adopt=False)
        self.ingested = []
        self.srv.google = google.Google(endpoints=self.fake.endpoints, env=dict(ENV), ingest=self._ingest)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.bodies: list[bytes] = []
        self.err = io.StringIO()
        ctx = contextlib.redirect_stderr(self.err)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)

    def _ingest(self, stream, length, name, mime):
        self.ingested.append(stream.read(length))
        return {"ref": {"root": "in", "name": "ref_1.png"}, "label": name}

    def req(self, method, path, body=None, headers=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        data = None
        if method == "POST":
            h["X-Media-Lab"] = "1"
            if body is not None:
                data = json.dumps(body).encode()
                h["Content-Type"] = "application/json"
        h.update(headers or {})
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        c.close()
        self.bodies.append(raw + json.dumps(hdrs).encode())
        try:
            j = json.loads(raw) if raw else None
        except ValueError:
            j = None
        return r.status, hdrs, j

    def connect(self, extra=None):
        st, hdrs, j = self.req("POST", "/api/google/connect", {}, extra)
        self.assertEqual(st, 200, j)
        cookie = hdrs["set-cookie"].split(";")[0]
        self.assertIn("SameSite=Lax", hdrs["set-cookie"])
        self.assertIn("Path=/api/google/callback", hdrs["set-cookie"])
        self.assertIn("HttpOnly", hdrs["set-cookie"])
        code, state = self.fake.authorize(j["url"])
        q = urllib.parse.urlencode({"state": state, "code": code, "scope": google.SCOPE})
        return cookie, q, j["url"]

    def assert_no_secrets(self):
        blob = b"\n".join(self.bodies).decode("utf-8", "replace") + self.err.getvalue()
        for s in self.fake.issued + [SECRET]:
            self.assertNotIn(s, blob)


class TestRoutes(RouteCase):
    def test_static_files_are_served(self):
        for path, ctype in (("/static/google.js", "text/javascript"), ("/static/google.css", "text/css")):
            st, hdrs, _ = self.req("GET", path)
            self.assertEqual(st, 200)
            self.assertTrue(hdrs["content-type"].startswith(ctype))
            self.assertIn("script-src 'self'", hdrs["content-security-policy"])

    def test_status_unconfigured(self):
        self.srv.google.env = {}
        st, _, j = self.req("GET", "/api/google/status")
        self.assertEqual(st, 200)
        self.assertFalse(j["configured"])
        self.assertEqual(j["setup_doc"], "docs/GOOGLE.md")
        st, _, j = self.req("POST", "/api/google/connect", {})
        self.assertEqual(st, 409)
        self.assertIn("docs/GOOGLE.md", j["error"])

    def test_full_flow(self):
        cookie, q, url = self.connect()
        self.assertIn(urllib.parse.quote(f"http://127.0.0.1:{self.port}/api/google/callback", safe=""), url)
        st, hdrs, _ = self.req("GET", "/api/google/callback?" + q, headers={"Cookie": cookie})
        self.assertEqual(st, 302)
        self.assertEqual(hdrs["location"], "/#google")
        self.assertIn("Max-Age=0", hdrs["set-cookie"])
        st, _, j = self.req("GET", "/api/google/status")
        self.assertTrue(j["connected"])
        self.assertEqual(j["email"], "person@example.com")
        self.assertIsNone(j["last_error"])

        st, _, j = self.req("POST", "/api/google/bootstrap", {"name": "My Lab"})
        self.assertEqual(st, 200, j)
        self.assertTrue(j["workspace"]["complete"])
        ws = self.srv.google.workspace()

        data = os.urandom(5000)
        self.r.put("lab", "cut_1.mp4", data)
        store.put_sidecar(("lab", "cut_1.mp4"), {"kind": "export", "commercial_ok": False,
                                                 "most_restrictive": "qwen-image-nc"})
        st, _, j = self.req("POST", "/api/google/upload", {"ref": {"root": "lab", "name": "cut_1.mp4"},
                                                           "folder": "exports"})
        self.assertEqual(st, 200, j)
        self.assertEqual(self.fake.files[j["id"]]["data"], data)
        self.assertTrue(j["sheet_row"])
        assets = self.fake.sheets[ws["sheet"]]["tabs"]["Assets"]
        self.assertEqual(assets[1][:6], ["cut_1.mp4", "export", "exports", j["id"], "qwen-image-nc", "no"])

        st, _, j = self.req("POST", "/api/google/upload", {"ref": {"root": "lab", "name": "nope.mp4"}})
        self.assertEqual(st, 404)
        st, _, j = self.req("POST", "/api/google/upload", {"ref": {"root": "lab", "name": "cut_1.mp4"},
                                                           "folder": "calendar"})
        self.assertEqual(st, 400)

        fid = self.fake.add_file("in.png", ws["folders"]["inbox"], make_png(), app=True)
        st, _, j = self.req("GET", "/api/google/inbox")
        self.assertEqual([f["id"] for f in j["files"]], [fid])
        st, _, j = self.req("POST", "/api/google/inbox/import", {"file_id": fid})
        self.assertEqual(st, 200, j)
        self.assertEqual(self.ingested, [make_png()])

        st, _, j = self.req("POST", "/api/google/disconnect", {})
        self.assertEqual(j, {"connected": False, "revoked": True})
        st, _, j = self.req("GET", "/api/google/status")
        self.assertFalse(j["connected"])
        self.assertTrue(j["workspace"]["complete"])
        st, _, j = self.req("GET", "/api/google/inbox")
        self.assertEqual(st, 409)
        self.assert_no_secrets()

    def test_callback_without_binding_cookie_changes_nothing(self):
        _, q, _ = self.connect()
        st, hdrs, _ = self.req("GET", "/api/google/callback?" + q)
        self.assertEqual((st, hdrs["location"]), (302, "/#google"))
        _, _, j = self.req("GET", "/api/google/status")
        self.assertFalse(j["connected"])
        self.assertIn("same browser", j["last_error"])
        self.assertFalse(self.fake.calls("POST", "/token"))

    def test_callback_replay_is_refused(self):
        cookie, q, _ = self.connect()
        self.req("GET", "/api/google/callback?" + q, headers={"Cookie": cookie})
        self.req("POST", "/api/google/disconnect", {})
        st, _, _ = self.req("GET", "/api/google/callback?" + q, headers={"Cookie": cookie})
        _, _, j = self.req("GET", "/api/google/status")
        self.assertFalse(j["connected"])
        self.assertIn("already used", j["last_error"])

    def test_forged_callback_does_not_echo_input(self):
        st, _, _ = self.req("GET", "/api/google/callback?state=%3Cscript%3E&code=x&error=%3Cb%3E")
        self.assertEqual(st, 302)
        _, _, j = self.req("GET", "/api/google/status")
        self.assertNotIn("<", j["last_error"])

    def test_guard_still_applies(self):
        st, _, _ = self.req("POST", "/api/google/connect", {}, headers={"X-Media-Lab": "0"})
        self.assertEqual(st, 403)
        st, _, _ = self.req("POST", "/api/google/connect", {}, headers={"Origin": "https://evil.example"})
        self.assertEqual(st, 403)
        st, _, _ = self.req("GET", "/api/google/callback?state=a&code=b", host="evil.example")
        self.assertEqual(st, 403)

    def test_manual_route(self):
        st, _, j = self.req("POST", "/api/google/manual", {"folders": {"root": "1AbCdEfGhIjKlMnOpQrStU"}})
        self.assertEqual(st, 200)
        self.assertEqual(j["workspace"]["source"], "manual")
        st, _, j = self.req("POST", "/api/google/manual", {"folders": {"root": "../x"}})
        self.assertEqual(st, 400)

    def test_google_errors_never_answer_401(self):
        cookie, q, _ = self.connect()
        self.req("GET", "/api/google/callback?" + q, headers={"Cookie": cookie})
        self.req("POST", "/api/google/bootstrap", {})
        self.fake.fail += [("GET", "/drive/v3/files", 401, {}), ("GET", "/drive/v3/files", 401, {})]
        st, _, j = self.req("GET", "/api/google/inbox")
        self.assertEqual(st, 409)
        self.assertIn("Connect", j["error"])
        self.assert_no_secrets()

    @requires_ffmpeg()
    def test_import_through_the_real_ingest(self):
        self.srv.google._ingest = None
        cookie, q, _ = self.connect()
        self.req("GET", "/api/google/callback?" + q, headers={"Cookie": cookie})
        self.req("POST", "/api/google/bootstrap", {})
        ws = self.srv.google.workspace()
        png = make_png(w=8, h=8)
        fid = self.fake.add_file("drop.png", ws["folders"]["inbox"], png, app=True)
        st, _, j = self.req("POST", "/api/google/inbox/import", {"file_id": fid})
        self.assertEqual(st, 200, j)
        ref = store.check_ref(j["ref"])
        self.assertTrue(store.owned(ref))
        with open(store.resolve(ref), "rb") as f:
            self.assertEqual(f.read(), png)


class TestRoutesWithToken(RouteCase):
    TOKEN = "correct-horse-battery"

    def test_connect_needs_the_token_but_the_callback_does_not(self):
        st, _, _ = self.req("POST", "/api/google/connect", {})
        self.assertEqual(st, 401)
        st, _, _ = self.req("GET", "/api/google/status")
        self.assertEqual(st, 401)
        auth = {"Authorization": "Bearer " + self.TOKEN}
        cookie, q, _ = self.connect(auth)
        # the browser comes back from Google without the Strict session cookie, only the Lax binding cookie
        st, hdrs, _ = self.req("GET", "/api/google/callback?" + q, headers={"Cookie": cookie})
        self.assertEqual((st, hdrs.get("location")), (302, "/#google"))
        st, _, j = self.req("GET", "/api/google/status", headers=auth)
        self.assertTrue(j["connected"])
        self.assert_no_secrets()

    def test_callback_without_binding_is_inert_even_when_public(self):
        auth = {"Authorization": "Bearer " + self.TOKEN}
        _, q, _ = self.connect(auth)
        st, _, _ = self.req("GET", "/api/google/callback?" + q)
        self.assertEqual(st, 302)
        _, _, j = self.req("GET", "/api/google/status", headers=auth)
        self.assertFalse(j["connected"])


if __name__ == "__main__":
    unittest.main()
