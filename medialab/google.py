"""Connect Google: OAuth (installed app, PKCE S256 + state), a Drive workspace, uploads, Inbox import, Sheet rows.

Scope: only `drive.file` (SCOPE). With it YourContent sees the files it created itself, plus files a person opens
with it through the Google Picker; nothing else in their Drive. That is why the app builds its own workspace
(root folder, Inbox / Rendered / Exports / Calendar, the "Content Plan" Sheet) instead of using existing folders.
See docs/GOOGLE.md for the 5-minute client setup and the limits of drive.file.

Secrets: the OAuth client comes from YOURCONTENT_GOOGLE_CLIENT_ID / YOURCONTENT_GOOGLE_CLIENT_SECRET (or the older MEDIA_LAB_* names) (read when
needed, never returned). Tokens live in DATA/config/google.json, mode 0600. No function here logs a token or
returns one; status() is the only view of the connection and it carries none.

Every HTTP call goes through one transport (`Transport.request`), so tests run against an in-process fake.
Errors are GoogleError with an HTTP `status` the server maps directly; none of them is 401, because the page
treats 401 as "log in to YourContent".
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import BinaryIO, Callable, Mapping

from . import config, store

SCOPE = "https://www.googleapis.com/auth/drive.file"
ENV_ID = "YOURCONTENT_GOOGLE_CLIENT_ID"
ENV_SECRET = "YOURCONTENT_GOOGLE_CLIENT_SECRET"
SETUP_DOC = "docs/GOOGLE.md"
CALLBACK_PATH = "/api/google/callback"

try:
    from .calendar import CALENDAR_COLUMNS  # type: ignore[attr-defined]
except ImportError:  # TODO: import from medialab.calendar once that module lands (studio merge)
    CALENDAR_COLUMNS = ("id", "when", "channel", "title", "caption", "status", "project", "asset", "updated")
ASSET_COLUMNS = ("name", "kind", "folder", "drive_file_id", "licence", "commercial_ok", "created")

PLAN_TAB = "Content Plan"
ASSETS_TAB = "Assets"
TABS = {PLAN_TAB: tuple(CALENDAR_COLUMNS), ASSETS_TAB: ASSET_COLUMNS}
SUBFOLDERS = (("inbox", "Inbox"), ("rendered", "Rendered"), ("exports", "Exports"), ("calendar", "Calendar"))
UPLOAD_FOLDERS = ("exports", "rendered")
FOLDER_MIME = "application/vnd.google-apps.folder"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"
ROLE_KEY = "mediaLab"          # appProperties key on everything the app creates (lets bootstrap find it again)

STATE_TTL = 600                # seconds a consent link stays valid
MAX_PENDING = 16
REFRESH_SKEW = 60
TIMEOUT = 60
CHUNK = 256 * 1024
MAX_ERROR = 200

ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,200}$")
NAME_MAX = 100


@dataclass(frozen=True)
class Endpoints:
    auth: str = "https://accounts.google.com/o/oauth2/v2/auth"
    token: str = "https://oauth2.googleapis.com/token"
    revoke: str = "https://oauth2.googleapis.com/revoke"
    drive: str = "https://www.googleapis.com/drive/v3"
    upload: str = "https://www.googleapis.com/upload/drive/v3"
    sheets: str = "https://sheets.googleapis.com/v4/spreadsheets"


GOOGLE = Endpoints()


class GoogleError(Exception):
    """A plain sentence for the page plus the HTTP status the server answers with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class NotConfigured(GoogleError):
    def __init__(self, message: str = "Google isn't set up on this YourContent yet; see " + SETUP_DOC):
        super().__init__(409, message)


class NotConnected(GoogleError):
    def __init__(self, message: str = "Connect Google first."):
        super().__init__(409, message)


# --------------------------------------------------------------------------------------------- transport

@dataclass
class Response:
    status: int
    headers: dict          # lower-case names
    body: bytes = b""
    raw: BinaryIO | None = None   # set when stream=True; the caller closes it

    def json(self) -> dict:
        try:
            d = json.loads(self.body or b"{}")
        except ValueError:
            return {}
        return d if isinstance(d, dict) else {}

    def close(self) -> None:
        if self.raw is not None:
            try:
                self.raw.close()
            except OSError:
                pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):  # a redirect comes back as a response; callers decide (no auth leak)
        return None


class Transport:
    """urllib, no automatic redirects. `body` may be bytes or a file object (sent in blocks, needs a
    Content-Length header). Network failures become GoogleError 503 without the URL or any header."""

    def __init__(self):
        self.opener = urllib.request.build_opener(_NoRedirect)

    def request(self, method: str, url: str, headers: Mapping[str, str] | None = None, body=None,
                timeout: float = TIMEOUT, stream: bool = False) -> Response:
        req = urllib.request.Request(url, data=body, method=method, headers=dict(headers or {}))
        try:
            r = self.opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            try:
                data = e.read(64 * 1024)
            except OSError:
                data = b""
            finally:
                e.close()
            return Response(e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, data)
        except (OSError, ValueError):
            raise GoogleError(503, "YourContent can't reach Google right now; check the internet connection.") from None
        hdrs = {k.lower(): v for k, v in r.headers.items()}
        if stream:
            return Response(r.status, hdrs, b"", r)
        try:
            return Response(r.status, hdrs, r.read())
        finally:
            r.close()


# --------------------------------------------------------------------------------------------- pure helpers

def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def pkce_pair() -> tuple[str, str]:
    """(code_verifier, S256 code_challenge). The verifier is 64 URL-safe characters (RFC 7636: 43-128)."""
    verifier = secrets.token_urlsafe(48)
    return verifier, b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def folder_url(fid: str) -> str:
    return f"https://drive.google.com/drive/folders/{fid}"


def sheet_url(sid: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sid}/edit"


def check_id(v: object, what: str = "id") -> str:
    if not isinstance(v, str) or not ID_RE.fullmatch(v.strip()):
        raise GoogleError(400, f"that {what} doesn't look like a Google Drive id")
    return v.strip()


def clean_name(v: object) -> str:
    if v is None or v == "":
        return "YourContent"
    if not isinstance(v, str):
        raise GoogleError(400, "the folder name must be text")
    s = " ".join("".join(ch for ch in v if ch.isprintable()).split())
    if not s or len(s) > NAME_MAX:
        raise GoogleError(400, f"the folder name must be 1 to {NAME_MAX} characters")
    return s


def _q(s: str) -> str:
    """A value for a Drive query string literal."""
    return s.replace("\\", "\\\\").replace("'", "\\'")


def _range(tab: str, cells: str) -> str:
    return urllib.parse.quote(f"'{tab}'!{cells}", safe="")


def validate_manual(folder_ids: object, sheet_id: object) -> dict:
    """PURE. The workspace record for IDs a person pasted (no OAuth). folder_ids: {"root", "inbox", "rendered",
    "exports", "calendar"} (root required, the rest optional); sheet_id optional. Raises GoogleError 400."""
    if not isinstance(folder_ids, dict):
        raise GoogleError(400, "folders must be an object of folder ids")
    keys = {"root"} | {k for k, _ in SUBFOLDERS}
    extra = set(folder_ids) - keys
    if extra:
        raise GoogleError(400, "unknown folder: " + ", ".join(sorted(map(str, extra)))[:80])
    if not folder_ids.get("root"):
        raise GoogleError(400, "paste the id of the root folder at least")
    folders = {k: check_id(v, f"{k} folder id") for k, v in folder_ids.items() if v}
    sheet = check_id(sheet_id, "Sheet id") if sheet_id else None
    return {"source": "manual", "name": None, "folders": folders, "sheet": sheet,
            "headers_ok": False, "moved": False, "created": store.now_iso()}


def write_private(path: str, obj: dict) -> None:
    """Atomic JSON write with mode 0600 from the first byte (never world-readable, even for a moment)."""
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    tmp = os.path.join(d, f".{os.path.basename(path)}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(json.dumps(obj, indent=1).encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _read(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def tokens_path() -> str:
    return store.data_path("config", "google.json")


def workspace_path() -> str:
    return store.data_path("config", "google_workspace.json")


def manual_workspace(folder_ids: object, sheet_id: object = None) -> dict:
    """Validate pasted IDs (validate_manual) and save them as the workspace. Returns the public view."""
    ws = validate_manual(folder_ids, sheet_id)
    write_private(workspace_path(), ws)
    return public_workspace(ws)


def public_workspace(ws: dict | None) -> dict | None:
    if not ws:
        return None
    folders = ws.get("folders") or {}
    out = {"name": ws.get("name"), "source": ws.get("source"),
           "folders": {k: {"id": v, "url": folder_url(v)} for k, v in folders.items() if isinstance(v, str)},
           "sheet": {"id": ws["sheet"], "url": sheet_url(ws["sheet"])} if isinstance(ws.get("sheet"), str) else None,
           "complete": bool(folders.get("root") and all(folders.get(k) for k, _ in SUBFOLDERS) and ws.get("sheet")
                            and ws.get("headers_ok") and ws.get("moved"))}
    return out


def workspace_links() -> dict | None:
    """The workspace as the agent setup prompt wants it (agent_setup.build_prompt ctx["google"]), or None."""
    ws = public_workspace(_read(workspace_path()))
    if not ws or not ws.get("sheet"):
        return None
    folders = ws["folders"]
    return {"root_url": (folders.get("root") or {}).get("url", ""), "sheet_url": ws["sheet"]["url"],
            "sheet_id": ws["sheet"]["id"],
            "folders": {name: folders[key]["url"] for key, name in SUBFOLDERS if key in folders}}


class _Reader:
    """A file wrapper that never hands out more than CHUNK bytes per read, and counts what was sent."""

    def __init__(self, f: BinaryIO, limit: int):
        self.f, self.left, self.sent, self.max_read = f, limit, 0, 0

    def read(self, n: int = -1) -> bytes:
        n = CHUNK if n is None or n < 0 else min(n, CHUNK)
        n = min(n, self.left)
        if n <= 0:
            return b""
        b = self.f.read(n)
        self.left -= len(b)
        self.sent += len(b)
        self.max_read = max(self.max_read, len(b))
        return b


# --------------------------------------------------------------------------------------------- client

class Google:
    """One per server. `transport`, `endpoints`, `env`, `clock` and `ingest` are injectable for tests."""

    def __init__(self, transport: Transport | None = None, endpoints: Endpoints = GOOGLE,
                 env: Mapping[str, str] | None = None, clock: Callable[[], float] = time.time,
                 ingest: Callable | None = None):
        self.t = transport or Transport()
        self.ep = endpoints
        self.env = env
        self.clock = clock
        self._ingest = ingest
        self.lock = threading.RLock()
        self.pending: dict[str, dict] = {}
        self.last_error: str | None = None

    # ---- settings
    def _client(self) -> tuple[str | None, str | None]:
        env = os.environ if self.env is None else self.env
        cid = (config.env(ENV_ID, env) or "").strip() or None
        sec = (config.env(ENV_SECRET, env) or "").strip() or None
        return cid, sec

    def configured(self) -> tuple[bool, str | None]:
        cid, _ = self._client()
        if not cid:
            return False, (f"Google isn't set up: {ENV_ID} is not set. Create your own OAuth client (about five "
                           f"minutes) as {SETUP_DOC} explains, then restart YourContent.")
        if not cid.endswith(".apps.googleusercontent.com"):
            return False, f"{ENV_ID} doesn't look like a Google OAuth client id (it ends in .apps.googleusercontent.com)."
        return True, None

    # ---- consent
    def begin(self, redirect_uri: str) -> tuple[str, str]:
        """(consent URL, binding). The binding goes into a cookie on the person's browser; the callback must
        bring it back with the state, so a consent link only completes in the browser that asked for it."""
        ok, why = self.configured()
        if not ok:
            raise NotConfigured(why or "Google isn't set up")
        cid, _ = self._client()
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(32)
        binding = secrets.token_urlsafe(32)
        now = self.clock()
        with self.lock:
            for k in [k for k, v in self.pending.items() if now - v["at"] > STATE_TTL]:
                del self.pending[k]
            while len(self.pending) >= MAX_PENDING:
                del self.pending[min(self.pending, key=lambda k: self.pending[k]["at"])]
            self.pending[_sha(state)] = {"at": now, "verifier": verifier, "redirect_uri": redirect_uri,
                                         "binding": _sha(binding)}
        q = {"client_id": cid, "redirect_uri": redirect_uri, "response_type": "code", "scope": SCOPE,
             "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
             "access_type": "offline", "prompt": "consent"}
        return self.ep.auth + "?" + urllib.parse.urlencode(q), binding

    def take_state(self, state: object, binding: object) -> dict:
        """Single use: the pending entry is removed whatever the outcome. Raises GoogleError 400."""
        if not isinstance(state, str) or not state or len(state) > 200:
            raise GoogleError(400, "that sign-in link isn't valid; press Connect again")
        with self.lock:
            p = self.pending.pop(_sha(state), None)
        if p is None:
            raise GoogleError(400, "that sign-in link isn't valid or was already used; press Connect again")
        if self.clock() - p["at"] > STATE_TTL:
            raise GoogleError(400, "that sign-in link expired (10 minutes); press Connect again")
        if not isinstance(binding, str) or not hmac.compare_digest(_sha(binding), p["binding"]):
            raise GoogleError(400, "finish signing in with the same browser that pressed Connect")
        return p

    def finish(self, state: object, code: object, binding: object, error: object = None) -> dict:
        """Validate the callback and exchange the code. Returns the status view."""
        p = self.take_state(state, binding)
        if error:
            raise GoogleError(400, "Google sign-in was cancelled" if error == "access_denied"
                              else "Google sign-in didn't finish; press Connect again")
        if not isinstance(code, str) or not code or len(code) > 2048:
            raise GoogleError(400, "Google didn't send a sign-in code; press Connect again")
        cid, sec = self._client()
        if not cid:
            raise NotConfigured()
        form = {"code": code, "client_id": cid, "redirect_uri": p["redirect_uri"],
                "grant_type": "authorization_code", "code_verifier": p["verifier"]}
        if sec:
            form["client_secret"] = sec
        r = self._token_call(form)
        tok = r.json()
        if not tok.get("access_token"):
            raise GoogleError(502, "Google didn't send an access token")
        granted = str(tok.get("scope") or "").split()
        if SCOPE not in granted:
            raise GoogleError(400, "Google Drive access wasn't granted (the box on Google's consent screen was "
                                   "unticked). Press Connect again and tick it.")
        old = _read(tokens_path()) or {}
        data = {"access_token": tok["access_token"],
                "expires_at": self.clock() + float(tok.get("expires_in") or 3600),
                "refresh_token": tok.get("refresh_token") or old.get("refresh_token"),
                "scope": " ".join(granted), "connected_at": store.now_iso(), "email": None}
        with self.lock:
            write_private(tokens_path(), data)
        email = self._email(tok)
        if email:
            with self.lock:
                cur = _read(tokens_path()) or data
                cur["email"] = email
                write_private(tokens_path(), cur)
        self.last_error = None
        return self.status()

    def _email(self, tok: dict) -> str | None:
        """The account's address: from an id_token when Google sent one (only with openid/email scopes, which
        YourContent does not ask for), else Drive about.get?fields=user (allowed with drive.file)."""
        idt = tok.get("id_token")
        if isinstance(idt, str) and idt.count(".") == 2:
            try:  # received straight from the token endpoint over TLS (OIDC §3.1.3.7 allows skipping the check)
                pl = json.loads(base64.urlsafe_b64decode(idt.split(".")[1] + "==="))
                if isinstance(pl.get("email"), str):
                    return pl["email"][:200]
            except (ValueError, TypeError):
                pass
        try:
            u = self._api("GET", self.ep.drive + "/about", params={"fields": "user(emailAddress,displayName)"}).json()
        except GoogleError:
            return None
        e = (u.get("user") or {}).get("emailAddress")
        return e[:200] if isinstance(e, str) else None

    def _token_call(self, form: dict) -> Response:
        body = urllib.parse.urlencode(form).encode()
        r = self.t.request("POST", self.ep.token, {"Content-Type": "application/x-www-form-urlencoded"}, body)
        if r.status == 200:
            return r
        err = str(r.json().get("error") or "")
        if err == "invalid_grant":
            raise GoogleError(400, "Google refused the sign-in (expired or revoked); press Connect again")
        if err in ("invalid_client", "unauthorized_client"):
            raise GoogleError(502, f"Google refused this OAuth client; check {ENV_ID} / {ENV_SECRET} ({SETUP_DOC})")
        if r.status >= 500 or r.status == 429:
            raise GoogleError(503, "Google is busy; try again in a minute")
        raise GoogleError(502, "Google refused the sign-in" + (f" ({err[:60]})" if err else ""))

    # ---- tokens
    def connected(self) -> bool:
        t = _read(tokens_path())
        return bool(t and (t.get("refresh_token") or t.get("access_token")))

    def access_token(self, force: bool = False) -> str:
        with self.lock:
            t = _read(tokens_path())
            if not t or not (t.get("refresh_token") or t.get("access_token")):
                raise NotConnected()
            if not force and t.get("access_token") and float(t.get("expires_at") or 0) - REFRESH_SKEW > self.clock():
                return t["access_token"]
            if not t.get("refresh_token"):
                self._forget()
                raise NotConnected("Google access ended; press Connect again.")
            cid, sec = self._client()
            if not cid:
                raise NotConfigured()
            form = {"grant_type": "refresh_token", "refresh_token": t["refresh_token"], "client_id": cid}
            if sec:
                form["client_secret"] = sec
            try:
                tok = self._token_call(form).json()
            except GoogleError as e:
                if e.status == 400:  # invalid_grant: revoked in the Google account, or expired
                    self._forget()
                    raise NotConnected("Google access ended (revoked or expired); press Connect again.") from None
                raise
            if not tok.get("access_token"):
                raise GoogleError(502, "Google didn't send an access token")
            t["access_token"] = tok["access_token"]
            t["expires_at"] = self.clock() + float(tok.get("expires_in") or 3600)
            if tok.get("refresh_token"):
                t["refresh_token"] = tok["refresh_token"]
            write_private(tokens_path(), t)
            return t["access_token"]

    def _forget(self) -> None:
        try:
            os.remove(tokens_path())
        except FileNotFoundError:
            pass

    def disconnect(self) -> dict:
        """Revoke at Google (best effort) and delete the local tokens. The workspace record stays."""
        with self.lock:
            t = _read(tokens_path()) or {}
            tok = t.get("refresh_token") or t.get("access_token")
            revoked = False
            if tok:
                try:
                    r = self.t.request("POST", self.ep.revoke, {"Content-Type": "application/x-www-form-urlencoded"},
                                       urllib.parse.urlencode({"token": tok}).encode())
                    revoked = r.status == 200
                except GoogleError:
                    revoked = False
            self._forget()
        return {"connected": False, "revoked": revoked}

    # ---- status
    def status(self) -> dict:
        ok, why = self.configured()
        t = _read(tokens_path()) or {}
        conn = bool(t.get("refresh_token") or t.get("access_token"))
        return {"configured": ok, "reason": why, "setup_doc": SETUP_DOC, "scope": SCOPE,
                "connected": conn, "email": t.get("email") if conn else None,
                "workspace": public_workspace(_read(workspace_path())), "last_error": self.last_error}

    # ---- API plumbing
    def _api(self, method: str, url: str, params: dict | None = None, json_body=None, headers: dict | None = None,
             body=None, stream: bool = False, ok=(200,)) -> Response:
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        hdrs = dict(headers or {})
        data = body
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            hdrs["Content-Type"] = "application/json; charset=UTF-8"
        for attempt in (0, 1):
            hdrs["Authorization"] = "Bearer " + self.access_token(force=attempt == 1)
            r = self.t.request(method, url, hdrs, data, stream=stream)
            if r.status in (301, 302, 303, 307, 308) and method == "GET" and r.headers.get("location"):
                r.close()
                loc = urllib.parse.urljoin(url, r.headers["location"])
                same = urllib.parse.urlsplit(loc).netloc == urllib.parse.urlsplit(url).netloc
                if urllib.parse.urlsplit(loc).scheme != "https" and not same:
                    raise GoogleError(502, "Google sent an unexpected redirect")
                h2 = {k: v for k, v in hdrs.items() if k != "Authorization" or same}
                r = self.t.request("GET", loc, h2, None, stream=stream)
            if r.status == 401 and attempt == 0 and not hasattr(body, "read"):
                r.close()
                continue
            break
        if r.status in ok:
            return r
        if stream and r.raw is not None:
            try:
                r.body = r.raw.read(64 * 1024)
            except OSError:
                r.body = b""
            r.close()
        raise self._error(r)

    def _error(self, r: Response) -> GoogleError:
        err = r.json().get("error")
        msg = ""
        if isinstance(err, dict):
            msg = str(err.get("message") or "")
        elif isinstance(err, str):
            msg = err
        msg = " ".join(msg.split())[:MAX_ERROR]
        if r.status == 401:
            self._forget()
            return NotConnected("Google access ended; press Connect again.")
        if r.status == 404:
            return GoogleError(404, "that item isn't in Google Drive any more, or YourContent can't see it")
        if r.status == 429 or r.status >= 500:
            return GoogleError(503, "Google is busy; try again in a minute")
        if r.status == 403:
            return GoogleError(502, "Google refused" + (f": {msg}" if msg else ""))
        return GoogleError(502, "Google said no" + (f": {msg}" if msg else f" ({r.status})"))

    # ---- workspace
    def workspace(self) -> dict:
        return _read(workspace_path()) or {}

    def _save_ws(self, ws: dict) -> None:
        write_private(workspace_path(), ws)

    def _alive(self, fid: str) -> bool:
        try:
            d = self._api("GET", f"{self.ep.drive}/files/{urllib.parse.quote(fid)}",
                          params={"fields": "id,trashed"}).json()
        except GoogleError as e:
            if e.status == 404:
                return False
            raise
        return not d.get("trashed")

    def _find_role(self, role: str, parent: str | None, mime: str | None) -> str | None:
        q = f"appProperties has {{ key='{ROLE_KEY}' and value='{_q(role)}' }} and trashed=false"
        if mime:
            q += f" and mimeType='{_q(mime)}'"
        if parent:
            q += f" and '{_q(parent)}' in parents"
        files = self._api("GET", self.ep.drive + "/files",
                          params={"q": q, "fields": "files(id)", "pageSize": "10", "spaces": "drive"}).json()
        rows = files.get("files") or []
        return rows[0]["id"] if rows and isinstance(rows[0].get("id"), str) else None

    def _ensure_folder(self, ws: dict, key: str, title: str, parent: str | None) -> str:
        fid = (ws.get("folders") or {}).get(key)
        if fid and self._alive(fid):
            return fid
        fid = self._find_role(key, parent, FOLDER_MIME)
        if not fid:
            meta = {"name": title, "mimeType": FOLDER_MIME, "appProperties": {ROLE_KEY: key}}
            if parent:
                meta["parents"] = [parent]
            fid = self._api("POST", self.ep.drive + "/files", params={"fields": "id"}, json_body=meta).json().get("id")
            if not isinstance(fid, str):
                raise GoogleError(502, "Google didn't return the new folder")
        ws.setdefault("folders", {})[key] = fid
        self._save_ws(ws)
        return fid

    def _ensure_sheet(self, ws: dict, root: str) -> str:
        sid = ws.get("sheet")
        if not (sid and self._alive(sid)):
            found = self._find_role("sheet", None, SHEET_MIME)
            if found:   # the role is set by the last step (the move), so headers and folder were done then
                ws.update(sheet=found, headers_ok=True, moved=True)
            else:
                body = {"properties": {"title": PLAN_TAB},
                        "sheets": [{"properties": {"title": t}} for t in TABS]}
                sid = self._api("POST", self.ep.sheets, json_body=body).json().get("spreadsheetId")
                if not isinstance(sid, str):
                    raise GoogleError(502, "Google didn't return the new Sheet")
                ws.update(sheet=sid, headers_ok=False, moved=False)
            sid = ws["sheet"]
            self._save_ws(ws)
        if not ws.get("headers_ok"):
            for tab, cols in TABS.items():
                self._api("PUT", f"{self.ep.sheets}/{sid}/values/{_range(tab, 'A1')}",
                          params={"valueInputOption": "RAW"},
                          json_body={"range": f"'{tab}'!A1", "majorDimension": "ROWS", "values": [list(cols)]})
            ws["headers_ok"] = True
            self._save_ws(ws)
        if not ws.get("moved"):
            cur = self._api("GET", f"{self.ep.drive}/files/{sid}", params={"fields": "parents"}).json()
            old = [p for p in cur.get("parents") or [] if p != root]
            params = {"addParents": root, "fields": "id,parents"}
            if old:
                params["removeParents"] = ",".join(old)
            self._api("PATCH", f"{self.ep.drive}/files/{sid}", params=params,
                      json_body={"appProperties": {ROLE_KEY: "sheet"}})
            ws["moved"] = True
            self._save_ws(ws)
        return sid

    def bootstrap(self, name: object = None) -> dict:
        """Create (or repair) the workspace. Idempotent: recorded IDs that still exist are reused, lost
        records are found again by their appProperties, and each ID is saved the moment it is created."""
        title = clean_name(name)
        with self.lock:
            ws = self.workspace()
            if ws.get("source") != "app":
                ws = {"source": "app", "name": title, "folders": {}, "created": store.now_iso()}
            root = self._ensure_folder(ws, "root", ws.get("name") or title, None)
            for key, sub in SUBFOLDERS:
                self._ensure_folder(ws, key, sub, root)
            self._ensure_sheet(ws, root)
            return public_workspace(ws) or {}

    def _folder(self, key: str) -> str:
        fid = (self.workspace().get("folders") or {}).get(key)
        if not fid:
            raise GoogleError(409, "create the Google workspace first")
        return fid

    # ---- upload
    def max_bytes(self) -> int:
        try:
            cfg = store.config()
            return max(cfg.max_image_mb, cfg.max_video_mb, cfg.max_audio_mb) * 1024 * 1024
        except RuntimeError:
            return 500 * 1024 * 1024

    def upload(self, path: str, name: str, mime: str, folder: str) -> dict:
        """Stream one file into Exports/ or Rendered/ with a resumable upload (two requests: start, then the
        bytes in CHUNK-sized reads). Returns {id, name, url, folder}."""
        if folder not in UPLOAD_FOLDERS:
            raise GoogleError(400, "folder must be exports or rendered")
        parent = self._folder(folder)
        size = os.path.getsize(path)
        if size <= 0:
            raise GoogleError(400, "that file is empty")
        if size > self.max_bytes():
            raise GoogleError(413, f"too big to send to Google ({self.max_bytes() // (1024 * 1024)} MB at most)")
        meta = {"name": name, "parents": [parent], "appProperties": {ROLE_KEY: "upload"}}
        r = self._api("POST", self.ep.upload + "/files",
                      params={"uploadType": "resumable", "fields": "id,name,webViewLink"},
                      json_body=meta, headers={"X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(size)})
        loc = r.headers.get("location") or ""
        want, got = urllib.parse.urlsplit(self.ep.upload), urllib.parse.urlsplit(loc)
        if (got.scheme, got.netloc) != (want.scheme, want.netloc) or not got.path.startswith(want.path):
            raise GoogleError(502, "Google sent an unexpected upload address")
        with open(path, "rb") as f:
            rd = _Reader(f, size)
            r2 = self._api("PUT", loc, headers={"Content-Type": mime, "Content-Length": str(size)}, body=rd,
                           ok=(200, 201))
        d = r2.json()
        fid = d.get("id")
        if not isinstance(fid, str):
            raise GoogleError(502, "Google didn't confirm the upload")
        return {"id": fid, "name": d.get("name") or name, "folder": folder, "bytes": rd.sent,
                "url": d.get("webViewLink") or f"https://drive.google.com/file/d/{fid}/view"}

    # ---- inbox
    def inbox(self) -> list[dict]:
        """Files in Inbox/ that YourContent can see (with drive.file: ones it created there, or ones opened with
        it through the Picker; see docs/GOOGLE.md)."""
        fid = self._folder("inbox")
        d = self._api("GET", self.ep.drive + "/files",
                      params={"q": f"'{_q(fid)}' in parents and trashed=false", "pageSize": "100",
                              "orderBy": "modifiedTime desc", "spaces": "drive",
                              "fields": "files(id,name,mimeType,size,modifiedTime)"}).json()
        out = []
        for f in d.get("files") or []:
            mime = str(f.get("mimeType") or "")
            if mime == FOLDER_MIME:
                continue
            out.append({"id": f.get("id"), "name": str(f.get("name") or "")[:200], "mime": mime,
                        "size": int(f["size"]) if str(f.get("size") or "").isdigit() else None,
                        "modified": f.get("modifiedTime"),
                        "importable": mime.split("/")[0] in ("image", "video", "audio")})
        return out

    def import_file(self, file_id: object) -> dict:
        """Download one Inbox file straight into YourContent's uploads (actions.ingest_upload: the same name
        rules, type sniffing and size limits as a browser upload)."""
        fid = check_id(file_id, "file id")
        inbox = self._folder("inbox")
        meta = self._api("GET", f"{self.ep.drive}/files/{fid}",
                         params={"fields": "id,name,mimeType,size,parents,trashed"}).json()
        if meta.get("trashed") or inbox not in (meta.get("parents") or []):
            raise GoogleError(400, "only files in the Inbox folder can be imported")
        mime = str(meta.get("mimeType") or "")
        if mime.split("/")[0] not in ("image", "video", "audio"):
            raise GoogleError(415, "only pictures, videos and audio can be imported")
        size = int(meta["size"]) if str(meta.get("size") or "").isdigit() else None
        if size is not None and size > self.max_bytes():
            raise GoogleError(413, f"too big to import ({self.max_bytes() // (1024 * 1024)} MB at most)")
        r = self._api("GET", f"{self.ep.drive}/files/{fid}", params={"alt": "media"}, stream=True)
        try:
            cl = r.headers.get("content-length") or ""
            length = int(cl) if cl.isdigit() else size
            if not length:
                raise GoogleError(502, "Google didn't say how big the file is")
            if length > self.max_bytes():
                raise GoogleError(413, "too big to import")
            ingest = self._ingest
            if ingest is None:
                from . import actions
                ingest = actions.ingest_upload
            return ingest(r.raw, length, str(meta.get("name") or "drive file"), mime)
        finally:
            r.close()

    # ---- sheet rows
    def _sheet(self) -> str:
        sid = self.workspace().get("sheet")
        if not sid:
            raise GoogleError(409, "create the Google workspace first")
        return sid

    @staticmethod
    def _row(tab: str, row) -> list[str]:
        if tab not in TABS:
            raise GoogleError(400, "unknown tab")
        if not isinstance(row, (list, tuple)) or len(row) > len(TABS[tab]):
            raise GoogleError(400, f"a {tab} row has at most {len(TABS[tab])} cells")
        return ["" if v is None else str(v)[:5000] for v in row]

    def append_rows(self, tab: str, rows) -> int:
        vals = [self._row(tab, r) for r in rows]
        if not vals:
            return 0
        self._api("POST", f"{self.ep.sheets}/{self._sheet()}/values/{_range(tab, 'A1')}:append",
                  params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
                  json_body={"majorDimension": "ROWS", "values": vals})
        return len(vals)

    def upsert_row(self, tab: str, row) -> dict:
        """Update the row whose first cell (id / name) matches, else append it."""
        vals = self._row(tab, row)
        if not vals or not vals[0]:
            raise GoogleError(400, "the first cell (the id) is required")
        sid = self._sheet()
        col = self._api("GET", f"{self.ep.sheets}/{sid}/values/{_range(tab, 'A:A')}").json().get("values") or []
        for i, cell in enumerate(col):
            if i > 0 and cell and cell[0] == vals[0]:
                self._api("PUT", f"{self.ep.sheets}/{sid}/values/{_range(tab, f'A{i + 1}')}",
                          params={"valueInputOption": "RAW"}, json_body={"majorDimension": "ROWS", "values": [vals]})
                return {"row": i + 1, "updated": True}
        self.append_rows(tab, [vals])
        return {"row": len(col) + 1, "updated": False}
