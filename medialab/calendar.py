"""Content calendar: planned posts, per-entry rev check, iCalendar feed, sheet rows (studio merge).

IO: list_entries / get / create / update / delete read and write DATA/calendar/entries.json (created on the
first write, mode 0700; atomic temp + os.replace, previous save kept as entries.json.bak).
PURE: check_id, parse_when, clean_fields, in_range, escape_text, fold, to_ics, to_row, from_row.

File: {"schema": "media-lab/calendar@1", "entries": [entry, ...]}. An entry:
    {"id": "c_<10hex>", "rev": 1, "when": "2026-10-12" | "2026-10-12T09:30" | "2026-10-12T09:30-07:00",
     "duration_min": 0, "channel": "instagram_reel", "title": "...", "caption": "...", "status": "idea",
     "project": "p_<12hex>" | None, "asset": {"root", "name"} | None, "created": iso, "updated": iso}
Each entry has its own rev, so two tabs editing two different entries never collide. A write naming a stale
rev raises Conflict(current entry). `project` and `asset` are checked for shape only: a linked cut or file
may be deleted later and the entry keeps the link (the page shows it as gone).
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import unicodedata
from datetime import date, datetime, timedelta, timezone

from . import projects, store
from .types import BadRef, Conflict, NotOwned

SCHEMA = "media-lab/calendar@1"
STATUSES = ("idea", "draft", "ready", "posted", "skipped")
# A Google Sheet "Content Plan" (later) uses exactly these columns, in this order.
CALENDAR_COLUMNS = ["id", "when", "channel", "title", "caption", "status", "project", "asset", "updated"]
EDITABLE = ("when", "duration_min", "channel", "title", "caption", "status", "project", "asset")
READ_ONLY = ("id", "rev", "created", "updated")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")   # NAME_RE-safe, no dots (it ends up in a UID)
DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
WHEN_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?(Z|[+-]\d{2}:?\d{2})?$")
MAX_CHANNEL, MAX_TITLE, MAX_CAPTION = 40, 120, 2200
MAX_DURATION = 7 * 24 * 60
MAX_ENTRIES = 2000
DEFAULT_TITLE = "Untitled"
PRODID = "-//YourContent//Content calendar//EN"
ICS_STATUS = {"posted": "CONFIRMED", "skipped": "CANCELLED"}   # everything else is TENTATIVE

_lock = threading.RLock()


class Unreadable(RuntimeError):
    """entries.json exists but is not a calendar. Never overwritten; HTTP 503."""
    status = 503


# --------------------------------------------------------------------------------------------- pure: fields

def check_id(eid: object) -> str:
    if not isinstance(eid, str) or not ID_RE.fullmatch(eid):
        raise BadRef("that calendar entry id isn't valid")
    return eid


def new_id() -> str:
    return "c_" + secrets.token_hex(5)


def parse_when(when: object) -> tuple[str, date | datetime]:
    """("date", date) | ("floating", naive datetime) | ("fixed", aware datetime). Raises ValueError.
    Accepts YYYY-MM-DD, or YYYY-MM-DDTHH:MM[:SS] with an optional Z / +HH:MM / -HHMM offset."""
    s = when.strip() if isinstance(when, str) else ""
    m = DATE_RE.fullmatch(s)
    try:
        if m:
            return "date", date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = WHEN_RE.fullmatch(s)
        if m:
            d = date.fromisoformat(m.group(1))
            dt = datetime(d.year, d.month, d.day, int(m.group(2)), int(m.group(3)), int(m.group(4) or 0))
            off = m.group(5)
            if not off:
                return "floating", dt
            if off == "Z":
                return "fixed", dt.replace(tzinfo=timezone.utc)
            sign = -1 if off[0] == "-" else 1
            hh, mm = int(off[1:3]), int(off[-2:])
            if hh > 23 or mm > 59:
                raise ValueError
            return "fixed", dt.replace(tzinfo=timezone(sign * timedelta(hours=hh, minutes=mm)))
    except ValueError:
        pass
    raise ValueError("when must be a date (2026-10-12) or a date and time (2026-10-12T09:30, optionally "
                     "with an offset like -07:00)")


def format_when(kind: str, value: date | datetime) -> str:
    """The stored form: seconds only when not zero; a zero offset is written Z."""
    if kind == "date":
        return value.isoformat()
    s = value.strftime("%Y-%m-%dT%H:%M") + (value.strftime(":%S") if value.second else "")
    if kind == "floating":
        return s
    off = value.utcoffset() or timedelta(0)
    if not off:
        return s + "Z"
    mins = int(off.total_seconds() // 60)
    sign, mins = ("-" if mins < 0 else "+"), abs(mins)
    return f"{s}{sign}{mins // 60:02d}:{mins % 60:02d}"


def clean_when(when: object) -> str:
    return format_when(*parse_when(when))


def day_of(when: str) -> str:
    """The calendar day an entry sits on: the date as written (an offset time stays on its own local day)."""
    return when[:10]


def check_day(s: object, name: str) -> str:
    if not isinstance(s, str) or not DATE_RE.fullmatch(s):
        raise ValueError(f"{name} must be a date like 2026-10-12")
    try:
        date.fromisoformat(s)
    except ValueError:
        raise ValueError(f"{name} must be a date like 2026-10-12") from None
    return s


def _clean_text(v: object, key: str, limit: int, multiline: bool = False) -> str:
    """Text without control characters (newlines kept when multiline) or lone surrogates. Format characters
    stay (emoji sequences need the zero-width joiner). Over the limit raises: a caption is never cut short."""
    if v is None:
        v = ""
    if not isinstance(v, str):
        raise ValueError(f"{key} must be text")
    v = v.replace("\r\n", "\n").replace("\r", "\n").replace(" ", "\n").replace(" ", "\n")
    keep = "\n\t" if multiline else ""
    v = "".join(ch if ch in keep else (" " if unicodedata.category(ch) == "Cc" else ch)
                for ch in v if unicodedata.category(ch) != "Cs")
    v = v.strip() if multiline else " ".join(v.split())
    if len(v) > limit:
        raise ValueError(f"{key} is too long ({limit} characters at most)")
    return v


def _clean_duration(v: object) -> int:
    if v is None or v == "":
        return 0
    if isinstance(v, bool):
        raise ValueError("duration_min must be a whole number of minutes")
    try:
        n = int(v) if not isinstance(v, float) or v.is_integer() else None
    except (TypeError, ValueError):
        n = None
    if n is None or not 0 <= n <= MAX_DURATION:
        raise ValueError(f"duration_min must be a whole number from 0 to {MAX_DURATION}")
    return n


def _clean_status(v: object) -> str:
    s = v.strip().lower() if isinstance(v, str) else v
    if s not in STATUSES:
        raise ValueError(f"status {v!r} isn't valid; use " + ", ".join(STATUSES[:-1]) + f" or {STATUSES[-1]}")
    return s


def _clean_project(v: object) -> str | None:
    if v in (None, ""):
        return None
    return projects.check_pid(v)


def _clean_asset(v: object) -> dict | None:
    if v in (None, ""):
        return None
    if isinstance(v, str):   # the sheet form "root/name"
        root, sep, name = v.strip().partition("/")
        if not sep:
            raise BadRef("asset must look like gen/name.png")
        v = (root, name)
    return store.ref_json(store.check_ref(v))


_CLEANERS = {
    "when": clean_when,
    "duration_min": _clean_duration,
    "channel": lambda v: _clean_text(v, "channel", MAX_CHANNEL),
    "title": lambda v: _clean_text(v, "title", MAX_TITLE) or DEFAULT_TITLE,
    "caption": lambda v: _clean_text(v, "caption", MAX_CAPTION, multiline=True),
    "status": _clean_status,
    "project": _clean_project,
    "asset": _clean_asset,
}


def clean_fields(fields: object, partial: bool) -> dict:
    """Validated editable fields. Read-only keys (id, rev, created, updated) are ignored; an unknown key is an
    error. partial=False fills defaults and requires `when`."""
    if not isinstance(fields, dict):
        raise ValueError("send the entry as an object")
    unknown = sorted(k for k in fields if k not in EDITABLE and k not in READ_ONLY)
    if unknown:
        raise ValueError("unknown field: " + ", ".join(str(k) for k in unknown)[:200])
    out = {k: _CLEANERS[k](fields[k]) for k in EDITABLE if k in fields}
    if not partial:
        if "when" not in out:
            raise ValueError("when is required")
        defaults = {"duration_min": 0, "channel": "", "title": DEFAULT_TITLE, "caption": "", "status": "idea",
                    "project": None, "asset": None}
        out = {**defaults, **out}
    return out


def in_range(entry: dict, from_: str | None, to: str | None) -> bool:
    """from_ and to are inclusive days (YYYY-MM-DD) or None."""
    d = day_of(str(entry.get("when") or ""))
    return (from_ is None or d >= from_) and (to is None or d <= to)


def sort_key(entry: dict) -> tuple:
    return (str(entry.get("when") or ""), str(entry.get("id") or ""))


# --------------------------------------------------------------------------------------------- IO: file

def _path(ext: str = ".json") -> str:
    return store.data_path("calendar", "entries" + ext)


def _load() -> list[dict]:
    try:
        with open(_path(), encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        raise Unreadable("the calendar file (calendar/entries.json in the data folder) can't be read; "
                         "fix or move it") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("entries"), list):
        raise Unreadable("the calendar file (calendar/entries.json in the data folder) isn't a calendar")
    return [e for e in doc["entries"] if isinstance(e, dict) and isinstance(e.get("id"), str)]


def _save(entries: list[dict]) -> None:
    d = store.data_path("calendar")
    os.makedirs(d, mode=0o700, exist_ok=True)
    entries = sorted(entries, key=sort_key)
    data = json.dumps({"schema": SCHEMA, "entries": entries}, ensure_ascii=False, indent=1).encode("utf-8")
    try:
        with open(_path(), "rb") as f:
            store.atomic_write(_path(".json.bak"), f.read())
    except FileNotFoundError:
        pass
    store.atomic_write(_path(), data)


def _find(entries: list[dict], eid: str) -> int:
    for i, e in enumerate(entries):
        if e.get("id") == eid:
            return i
    raise NotOwned("that calendar entry doesn't exist")


def _check_rev(rev: object, current: dict) -> None:
    if isinstance(rev, bool) or not isinstance(rev, int):
        raise ValueError("rev must be a number")
    if rev != current.get("rev"):
        raise Conflict(dict(current), "this entry was changed elsewhere")


# --------------------------------------------------------------------------------------------- IO: CRUD

def list_entries(from_: str | None = None, to: str | None = None) -> list[dict]:
    """Entries on days from_..to (inclusive, either may be None), in time order."""
    if from_ is not None:
        check_day(from_, "from")
    if to is not None:
        check_day(to, "to")
    with _lock:
        entries = _load()
    return sorted((e for e in entries if in_range(e, from_, to)), key=sort_key)


def get(eid: str) -> dict:
    check_id(eid)
    with _lock:
        entries = _load()
    return entries[_find(entries, eid)]


def create(fields: dict, eid: str | None = None) -> dict:
    """A new entry (rev 1). eid lets a sheet import keep its row's id; normally one is made up."""
    clean = clean_fields(fields, partial=False)
    now = store.now_iso()
    with _lock:
        entries = _load()
        if len(entries) >= MAX_ENTRIES:
            raise ValueError(f"the calendar is full ({MAX_ENTRIES} entries); delete old ones first")
        ids = {e.get("id") for e in entries}
        if eid is not None:
            if check_id(eid) in ids:
                raise ValueError("that calendar entry id is already used")
        else:
            eid = new_id()
            while eid in ids:
                eid = new_id()
        entry = {"id": eid, "rev": 1, **clean, "created": now, "updated": now}
        entries.append(entry)
        _save(entries)
    return entry


def update(eid: str, rev: int, patch: dict) -> dict:
    """Merge the given fields into the entry and bump its rev. Raises Conflict(current) on a stale rev."""
    check_id(eid)
    clean = clean_fields(patch, partial=True)
    with _lock:
        entries = _load()
        i = _find(entries, eid)
        _check_rev(rev, entries[i])
        entry = {**entries[i], **clean, "rev": int(entries[i]["rev"]) + 1, "updated": store.now_iso()}
        entries[i] = entry
        _save(entries)
    return entry


def delete(eid: str, rev: int | None = None) -> None:
    """Remove an entry. With rev, a stale rev raises Conflict(current) instead."""
    check_id(eid)
    with _lock:
        entries = _load()
        i = _find(entries, eid)
        if rev is not None:
            _check_rev(rev, entries[i])
        del entries[i]
        _save(entries)


# --------------------------------------------------------------------------------------------- pure: iCalendar

def escape_text(s: object) -> str:
    """RFC 5545 §3.3.11 TEXT: backslash, semicolon, comma and newline escaped; other controls dropped."""
    s = str(s or "").replace("\r\n", "\n").replace("\r", "\n")
    s = "".join(ch for ch in s if ch in "\n\t" or unicodedata.category(ch) not in ("Cc", "Cs"))
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line: str, limit: int = 75) -> str:
    """RFC 5545 §3.1: lines of at most 75 octets, continued with CRLF + one space, never splitting a
    UTF-8 sequence."""
    out, cur, n = [], [], 0
    for ch in line:
        b = len(ch.encode("utf-8"))
        if n + b > limit:
            out.append("".join(cur))
            cur, n = [" "], 1
        cur.append(ch)
        n += b
    out.append("".join(cur))
    return "\r\n".join(out)


def _utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _iso_utc(s: object) -> str | None:
    """"2026-10-07T10:00:00Z" -> "20261007T100000Z"; None when it isn't that shape."""
    try:
        return _utc(datetime.fromisoformat(str(s).replace("Z", "+00:00")))
    except ValueError:
        return None


def _dt_props(entry: dict) -> list[str]:
    kind, start = parse_when(entry.get("when"))
    mins = _clean_duration(entry.get("duration_min"))
    if kind == "date":
        days = max(1, -(-mins // 1440))
        return [f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}",
                f"DTEND;VALUE=DATE:{(start + timedelta(days=days)).strftime('%Y%m%d')}"]
    end = start + timedelta(minutes=mins)
    if kind == "fixed":
        props = [f"DTSTART:{_utc(start)}"] + ([f"DTEND:{_utc(end)}"] if mins else [])
    else:   # floating: the same wall-clock time wherever the subscriber is
        fmt = "%Y%m%dT%H%M%S"
        props = [f"DTSTART:{start.strftime(fmt)}"] + ([f"DTEND:{end.strftime(fmt)}"] if mins else [])
    return props


def _vevent(entry: dict, stamp: str) -> list[str]:
    channel, title = str(entry.get("channel") or ""), str(entry.get("title") or DEFAULT_TITLE)
    status = str(entry.get("status") or "idea")
    caption = str(entry.get("caption") or "")
    desc = (caption + "\n\n" if caption else "") + f"Status: {status}"
    lines = ["BEGIN:VEVENT", f"UID:{check_id(entry.get('id'))}@media-lab", f"DTSTAMP:{stamp}"]
    lines += _dt_props(entry)
    lines += [f"SUMMARY:{escape_text(f'[{channel}] {title}' if channel else title)}",
              f"DESCRIPTION:{escape_text(desc)}",
              f"STATUS:{ICS_STATUS.get(status, 'TENTATIVE')}"]
    rev = entry.get("rev")
    if isinstance(rev, int) and not isinstance(rev, bool) and rev >= 1:
        lines.append(f"SEQUENCE:{rev - 1}")
    for prop, key in (("CREATED", "created"), ("LAST-MODIFIED", "updated")):
        v = _iso_utc(entry.get(key))
        if v:
            lines.append(f"{prop}:{v}")
    if channel:
        lines.append(f"CATEGORIES:{escape_text(channel)}")
    lines.append("END:VEVENT")
    return lines


def to_ics(entries: list[dict], cal_name: str, now: datetime) -> str:
    """A VCALENDAR (RFC 5545) with one VEVENT per entry: CRLF line ends, folded lines. All-day entries are
    VALUE=DATE; times with an offset become UTC (Z); times without one stay floating."""
    stamp = _utc(now)
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
             f"X-WR-CALNAME:{escape_text(cal_name)}"]
    for e in sorted(entries, key=sort_key):
        lines += _vevent(e, stamp)
    lines.append("END:VCALENDAR")
    return "".join(fold(line) + "\r\n" for line in lines)


# --------------------------------------------------------------------------------------------- pure: sheet rows

def to_row(entry: dict) -> list[str]:
    """One sheet row in CALENDAR_COLUMNS order; every cell is text (asset as "root/name")."""
    a = entry.get("asset")
    asset = f"{a.get('root')}/{a.get('name')}" if isinstance(a, dict) and a.get("root") and a.get("name") else ""
    vals = {**entry, "asset": asset, "project": entry.get("project") or ""}
    return ["" if vals.get(c) is None else str(vals.get(c)) for c in CALENDAR_COLUMNS]


def from_row(row: list, header: list) -> dict:
    """Fields from a sheet row, keyed by the header's column names (case and spaces ignored; unknown columns
    skipped; columns the header lacks are left out, so an update keeps them). A cell past the end of a short
    row counts as empty, as the Sheets API drops trailing empty cells. Empty id, when and status are left out
    (a new row; when then comes from elsewhere; status keeps its value or defaults to idea); other empty cells
    clear their field. The result has id and updated when present, plus cleaned editable fields.
    Raises ValueError / BadRef naming the column."""
    if not isinstance(row, (list, tuple)) or not isinstance(header, (list, tuple)):
        raise ValueError("a row and its header must be lists")
    out: dict = {}
    for i, col in enumerate(header):
        key = str(col or "").strip().lower()
        if key not in CALENDAR_COLUMNS or key in out:
            continue
        cell = row[i] if i < len(row) else ""
        cell = "" if cell is None else (str(int(cell)) if isinstance(cell, float) and cell.is_integer()
                                        else str(cell))
        if key in ("id", "when", "status", "updated") and not cell.strip():
            continue
        try:
            if key == "id":
                out["id"] = check_id(cell.strip())
            elif key == "updated":
                out["updated"] = cell.strip()
            else:
                out[key] = _CLEANERS[key](cell)
        except ValueError as exc:   # BadRef is a ValueError too
            raise exc.__class__(f"column {key}: {exc}") from None
    return out
