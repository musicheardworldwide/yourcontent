"""Two-way sync between the content calendar and the Content Plan tab of the Google workspace Sheet.

`reconcile()` is PURE: given the Sheet's rows, the calendar entries and what the last sync saw, it decides which
entries to create or update and which rows to write. Per entry, the side that changed since the last sync wins;
when both changed, the calendar wins (the Sheet row is rewritten) and the result counts a conflict. A Sheet row
with no id becomes a new entry (it needs a `when`) and gets the id written back. A row whose entry was deleted in
YourContent stays as it is (`orphans`); YourContent never deletes Sheet rows. Rows it cannot read are left alone and
reported. `sync(g)` does the IO: one read of the tab, the calendar changes, at most one write of the tab.

What the last sync saw lives in DATA/config/plan_sync.json: per id, the hash of the row it wrote and the entry's
`updated` stamp.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading

from . import calendar, google, store

_lock = threading.Lock()
COLS = list(calendar.CALENDAR_COLUMNS)


def _pad(row: list) -> list[str]:
    cells = ["" if c is None else str(c) for c in (row or [])][:len(COLS)]
    return cells + [""] * (len(COLS) - len(cells))


def row_hash(row: list) -> str:
    return hashlib.sha256(json.dumps(_pad(row)).encode()).hexdigest()[:32]


def _editable(fields: dict) -> dict:
    return {k: v for k, v in fields.items() if k not in ("id", "updated")}


def reconcile(values: list, entries: list[dict], last: dict) -> dict:
    """PURE. values = the tab's rows (row 0 is the header). Returns {"creates": [(row_index, fields, id|None)],
    "updates": [(id, rev, fields)], "rows": {row_index: entry_id} (rows to rewrite from the final entry),
    "append": [entry_id], "orphans": n, "conflicts": n, "errors": [str]}. Raises ValueError on a wrong header."""
    header = [str(c).strip().lower() for c in (values[0] if values else [])]
    if values and header[:len(COLS)] != COLS:
        raise ValueError("the Content Plan header was changed; row 1 must be: " + ", ".join(COLS))
    by_id = {e["id"]: e for e in entries}
    out: dict = {"creates": [], "updates": [], "rows": {}, "append": [], "orphans": 0, "conflicts": 0,
                 "errors": []}
    seen: set[str] = set()
    for i, row in enumerate(values[1:], start=1):
        if not any(str(c).strip() for c in row or []):
            continue
        try:
            fields = calendar.from_row(list(row), COLS)
        except ValueError as exc:
            out["errors"].append(f"row {i + 1}: {exc}")
            continue
        rid = fields.get("id")
        if rid and rid in seen:
            out["errors"].append(f"row {i + 1}: id {rid} appears twice")
            continue
        if rid:
            seen.add(rid)
        e = by_id.get(rid) if rid else None
        if e is None:
            if rid and rid in last:
                out["orphans"] += 1          # deleted in YourContent; the row stays
                continue
            if not fields.get("when"):
                out["errors"].append(f"row {i + 1}: a new row needs a when")
                continue
            out["creates"].append((i, _editable(fields), rid))
            continue
        prev = last.get(rid) or {}
        app_changed = e.get("updated") != prev.get("updated")
        sheet_changed = (row_hash(row) != prev["row"]) if prev.get("row") else (
            _pad(row) != _pad(calendar.to_row(e)))
        if sheet_changed and not app_changed:
            patch = _editable(fields)
            out["updates"].append((rid, e["rev"], patch))
        elif sheet_changed and app_changed:
            out["conflicts"] += 1
        out["rows"][i] = rid
    out["append"] = [e["id"] for e in entries if e["id"] not in seen]
    return out


def _state_path() -> str:
    return store.data_path("config", "plan_sync.json")


def _load_state() -> dict:
    try:
        with open(_state_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    p = _state_path()
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(p + ".tmp", p)


def sync(g: google.Google) -> dict:
    """Read the tab, apply the plan to the calendar, write the tab once. Returns counts."""
    with _lock:
        sid = g._sheet()
        tab = google._range(google.PLAN_TAB, "A:" + chr(ord("A") + len(COLS) - 1))
        values = g._api("GET", f"{g.ep.sheets}/{sid}/values/{tab}").json().get("values") or [COLS]
        last = _load_state()
        plan = reconcile(values, calendar.list_entries(), last)
        final: dict[str, dict] = {}
        for rid, rev, patch in plan["updates"]:
            final[rid] = calendar.update(rid, rev, patch)
        created_rows: dict[int, str] = {}
        for i, fields, rid in plan["creates"]:
            e = calendar.create(fields, eid=rid)
            final[e["id"]] = e
            created_rows[i] = e["id"]
        entries = {e["id"]: e for e in calendar.list_entries()}
        grid = [COLS] + [_pad(r) for r in values[1:]]
        for i, rid in {**plan["rows"], **created_rows}.items():
            grid[i] = calendar.to_row(entries[rid])
        for rid in plan["append"]:
            grid.append(calendar.to_row(entries[rid]))
        before = [COLS] + [_pad(r) for r in values[1:]]
        wrote = grid != before
        if wrote:
            g._api("PUT", f"{g.ep.sheets}/{sid}/values/{google._range(google.PLAN_TAB, 'A1')}",
                   params={"valueInputOption": "RAW"},
                   json_body={"range": f"'{google.PLAN_TAB}'!A1", "majorDimension": "ROWS", "values": grid})
        state = {}
        for row in grid[1:]:
            rid = row[0]
            if rid in entries:
                state[rid] = {"row": row_hash(row), "updated": entries[rid].get("updated")}
        _save_state(state)
        return {"from_sheet": len(plan["updates"]) + len(plan["creates"]), "to_sheet": len(plan["append"])
                + sum(1 for i in plan["rows"] if grid[i] != before[i]), "conflicts": plan["conflicts"],
                "orphans": plan["orphans"], "errors": plan["errors"][:20], "wrote": wrote}


def routes(srv):
    from . import google_routes

    def r_sync(h, m):
        srv.read_json(h)
        try:
            out = sync(google_routes.client(h))
        except ValueError as exc:
            raise srv.HTTPError(409, str(exc)) from None
        srv._json(h, 200, out)

    return [("POST", r"/api/google/sync", r_sync)]
