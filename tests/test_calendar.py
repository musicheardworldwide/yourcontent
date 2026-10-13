"""Studio merge: the content calendar's pure logic and file store (medialab/calendar.py).

Covers: `when` parsing and normal form, field validation, list/get/create/update/delete with per-entry rev
conflicts, the atomic file and its .bak, an unreadable file never being overwritten, iCalendar output checked by
a strict RFC 5545 parser (CRLF, 75-octet folds, TEXT escapes, round-trip), and sheet rows both ways.
"""
from __future__ import annotations

import json
import os
import re
import unittest
from datetime import datetime, timezone
from unittest import mock

from medialab import calendar, store
from medialab.types import BadRef, Conflict, NotOwned
from tests.util import TempRoots

NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------------------------- strict ICS parser

CONTENT_LINE = re.compile(r"^([A-Za-z0-9-]+)((?:;[A-Za-z0-9-]+=(?:\"[^\"]*\"|[^\";:,]*)(?:,(?:\"[^\"]*\"|[^\";:,]*))*)*):(.*)$")


def parse_ics(text: str) -> list[dict]:
    """A deliberately strict reader: raises AssertionError on anything RFC 5545 forbids that we might emit.
    Returns the VEVENTs as {NAME: (params, raw value)} (one value per name; we never repeat a property)."""
    raw = text.encode("utf-8")
    assert raw.endswith(b"\r\n"), "the last line must end with CRLF"
    assert b"\n" not in raw.replace(b"\r\n", b""), "bare LF"
    assert b"\r" not in raw.replace(b"\r\n", b""), "bare CR"
    phys = raw[:-2].split(b"\r\n")
    for ln in phys:
        assert len(ln) <= 75, f"line over 75 octets ({len(ln)}): {ln[:40]!r}"
        ln.decode("utf-8")   # a fold never splits a UTF-8 sequence
        assert not re.search(rb"[\x00-\x08\x0a-\x1f\x7f]", ln), f"control character in {ln[:40]!r}"
    logical: list[str] = []
    for ln in phys:
        s = ln.decode("utf-8")
        if s[:1] in (" ", "\t"):
            assert logical, "a continuation line cannot come first"
            logical[-1] += s[1:]
        else:
            logical.append(s)
    assert logical[0] == "BEGIN:VCALENDAR" and logical[-1] == "END:VCALENDAR"
    stack, events, cal, cur = [], [], {}, None
    for ln in logical:
        m = CONTENT_LINE.match(ln)
        assert m, f"not a content line: {ln!r}"
        name, params, value = m.group(1).upper(), m.group(2), m.group(3)
        if name == "BEGIN":
            stack.append(value)
            if value == "VEVENT":
                assert stack == ["VCALENDAR", "VEVENT"]
                cur = {}
            continue
        if name == "END":
            assert stack and stack.pop() == value, f"END:{value} does not close {stack}"
            if value == "VEVENT":
                events.append(cur)
                cur = None
            continue
        target = cur if cur is not None else cal
        assert name not in target, f"{name} repeated"
        target[name] = (params, value)
        if name in ("SUMMARY", "DESCRIPTION", "X-WR-CALNAME", "CATEGORIES"):
            i = 0
            while i < len(value):   # every ; and , escaped; every backslash starts a known escape
                if value[i] == "\\":
                    assert value[i + 1:i + 2] in ("\\", ";", ",", "n", "N"), f"bad escape in {name}"
                    i += 2
                    continue
                assert value[i] not in ";,", f"unescaped {value[i]!r} in {name}"
                i += 1
    assert not stack
    for req in ("VERSION", "PRODID"):
        assert req in cal, f"VCALENDAR without {req}"
    assert cal["VERSION"][1] == "2.0"
    for ev in events:
        for req in ("UID", "DTSTAMP", "DTSTART"):
            assert req in ev, f"VEVENT without {req}"
        assert re.fullmatch(r"\d{8}T\d{6}Z", ev["DTSTAMP"][1]), "DTSTAMP must be UTC"
    return events


def unescape(v: str) -> str:
    out, i = [], 0
    while i < len(v):
        ch = v[i]
        if ch == "\\":
            nxt = v[i + 1]
            out.append({"n": "\n", "N": "\n", "\\": "\\", ";": ";", ",": ","}[nxt])
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def entry(**kw) -> dict:
    base = {"id": "c_0123456789", "rev": 1, "when": "2026-10-12T09:30", "duration_min": 0, "channel": "",
            "title": "Post", "caption": "", "status": "idea", "project": None, "asset": None,
            "created": "2026-10-07T10:00:00Z", "updated": "2026-10-08T11:00:00Z"}
    base.update(kw)
    return base


# --------------------------------------------------------------------------------------------- pure

class WhenTest(unittest.TestCase):
    def test_normal_forms(self):
        cases = {"2026-10-12": "2026-10-12", "2026-10-12T09:30": "2026-10-12T09:30",
                 "2026-10-12T09:30:00": "2026-10-12T09:30", "2026-10-12T09:30:15": "2026-10-12T09:30:15",
                 "2026-10-12 09:30": "2026-10-12T09:30", "2026-10-12T09:30Z": "2026-10-12T09:30Z",
                 "2026-10-12T09:30+00:00": "2026-10-12T09:30Z", "2026-10-12T09:30-0700": "2026-10-12T09:30-07:00",
                 " 2026-10-12T23:59+05:45 ": "2026-10-12T23:59+05:45"}
        for raw, want in cases.items():
            self.assertEqual(calendar.clean_when(raw), want, raw)

    def test_kinds(self):
        self.assertEqual(calendar.parse_when("2026-10-12")[0], "date")
        self.assertEqual(calendar.parse_when("2026-10-12T09:30")[0], "floating")
        kind, dt = calendar.parse_when("2026-10-12T09:30-07:00")
        self.assertEqual((kind, dt.utcoffset().total_seconds()), ("fixed", -7 * 3600))

    def test_bad_values(self):
        for bad in ("", "tomorrow", "2026-13-01", "2026-02-30", "2026-10-12T25:00", "2026-10-12T09:60",
                    "2026-10-12T09:30+24:00", "12/10/2026", "2026-10-12T09", None, 20261012, "2026-10-12T09:30PST"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                calendar.parse_when(bad)

    def test_day_and_range(self):
        e = entry(when="2026-10-12T23:30-07:00")   # stays on the day it was written for
        self.assertEqual(calendar.day_of(e["when"]), "2026-10-12")
        self.assertTrue(calendar.in_range(e, "2026-10-12", "2026-10-12"))
        self.assertTrue(calendar.in_range(e, None, None))
        self.assertFalse(calendar.in_range(e, "2026-10-13", None))
        self.assertFalse(calendar.in_range(e, None, "2026-10-11"))


class FieldsTest(unittest.TestCase):
    def test_defaults_and_cleaning(self):
        f = calendar.clean_fields({"when": "2026-10-12", "title": "  Two \t spaces\n", "channel": " reel "},
                                  partial=False)
        self.assertEqual(f, {"when": "2026-10-12", "duration_min": 0, "channel": "reel", "title": "Two spaces",
                             "caption": "", "status": "idea", "project": None, "asset": None})
        self.assertEqual(calendar.clean_fields({"when": "2026-10-12", "title": ""}, False)["title"], "Untitled")

    def test_caption_keeps_lines_and_emoji(self):
        cap = "Line one\r\nLine two\x07\n\U0001F468‍\U0001F469‍\U0001F467 #tag"
        out = calendar.clean_fields({"caption": cap}, partial=True)["caption"]
        self.assertEqual(out, "Line one\nLine two \n\U0001F468‍\U0001F469‍\U0001F467 #tag")

    def test_limits_raise_instead_of_truncating(self):
        calendar.clean_fields({"caption": "x" * 2200, "title": "t" * 120, "channel": "c" * 40}, partial=True)
        for key, n in (("caption", 2201), ("title", 121), ("channel", 41)):
            with self.assertRaisesRegex(ValueError, f"{key} is too long"):
                calendar.clean_fields({key: "x" * n}, partial=True)

    def test_bad_inputs(self):
        bad = [{"status": "published"}, {"status": None}, {"duration_min": -1}, {"duration_min": 10081},
               {"duration_min": True}, {"duration_min": 1.5}, {"duration_min": "abc"}, {"title": 5},
               {"project": "nope"}, {"asset": {"root": "etc", "name": "passwd"}},
               {"asset": {"root": "gen", "name": "../x.png"}}, {"asset": "gen"}, {"colour": "red"}]
        for b in bad:
            with self.assertRaises(ValueError, msg=repr(b)):
                calendar.clean_fields(b, partial=True)
        with self.assertRaisesRegex(ValueError, "when is required"):
            calendar.clean_fields({"title": "x"}, partial=False)
        with self.assertRaises(ValueError):
            calendar.clean_fields(["not", "a", "dict"], partial=True)

    def test_good_links_and_read_only_ignored(self):
        f = calendar.clean_fields({"project": "p_0123456789ab", "asset": {"root": "gen", "name": "a.png"},
                                   "status": " Ready ", "duration_min": "30", "id": "x", "rev": 9}, partial=True)
        self.assertEqual(f, {"project": "p_0123456789ab", "asset": {"root": "gen", "name": "a.png"},
                             "status": "ready", "duration_min": 30})
        self.assertEqual(calendar.clean_fields({"asset": "lab/cut_1.mp4"}, True)["asset"],
                         {"root": "lab", "name": "cut_1.mp4"})

    def test_ids(self):
        for good in ("c_0123456789", "row7", "A-b_c"):
            self.assertEqual(calendar.check_id(good), good)
        for bad in ("", "_x", "a.b", "a/b", "x" * 41, "c_0123456789@x", None, 7):
            with self.assertRaises(BadRef, msg=repr(bad)):
                calendar.check_id(bad)
        self.assertRegex(calendar.new_id(), r"^c_[0-9a-f]{10}$")
        self.assertTrue(store.NAME_RE.fullmatch(calendar.new_id()))


class IcsTest(unittest.TestCase):
    def test_the_strict_parser_bites(self):
        good = calendar.to_ics([entry()], "Plan", NOW)
        bad = [good.replace("\r\n", "\n"), good.replace("SUMMARY:Post", "SUMMARY:" + "x" * 80),
               good.replace("SUMMARY:Post", "SUMMARY:a, b"), good.replace("SUMMARY:Post", "SUMMARY:a\\qb"),
               good.replace("DTSTAMP:20261010T120000Z", "DTSTAMP:20261010T120000"),
               good.replace("END:VEVENT\r\n", ""), good.replace("UID:", "X-UID:")]
        for text in bad:
            with self.assertRaises(AssertionError):
                parse_ics(text)

    def test_minimal_calendar_is_valid(self):
        events = parse_ics(calendar.to_ics([], "Plan", NOW))
        self.assertEqual(events, [])

    def test_event_fields(self):
        e = entry(when="2026-10-12T09:30-07:00", duration_min=45, channel="instagram_reel", title="Launch",
                  caption="Hello", status="posted", rev=3)
        text = calendar.to_ics([e], "Plan", NOW)
        self.assertNotIn("\n", text.replace("\r\n", ""))
        (ev,) = parse_ics(text)
        self.assertEqual(ev["UID"][1], "c_0123456789@media-lab")
        self.assertEqual(ev["DTSTAMP"][1], "20261010T120000Z")
        self.assertEqual(ev["DTSTART"], ("", "20261012T163000Z"))      # 09:30 at -07:00 in UTC
        self.assertEqual(ev["DTEND"], ("", "20261012T171500Z"))
        self.assertEqual(unescape(ev["SUMMARY"][1]), "[instagram_reel] Launch")
        self.assertEqual(unescape(ev["DESCRIPTION"][1]), "Hello\n\nStatus: posted")
        self.assertEqual(ev["STATUS"][1], "CONFIRMED")
        self.assertEqual(ev["SEQUENCE"][1], "2")
        self.assertEqual(ev["CREATED"][1], "20261007T100000Z")
        self.assertEqual(ev["LAST-MODIFIED"][1], "20261008T110000Z")

    def test_all_day_and_floating(self):
        evs = parse_ics(calendar.to_ics([entry(id="a", when="2026-12-31"),
                                         entry(id="b", when="2026-10-12", duration_min=1441),
                                         entry(id="c", when="2026-10-12T09:30", duration_min=30),
                                         entry(id="d", when="2026-10-12T10:00")], "Plan", NOW))
        by = {e["UID"][1].split("@")[0]: e for e in evs}
        self.assertEqual(by["a"]["DTSTART"], (";VALUE=DATE", "20261231"))
        self.assertEqual(by["a"]["DTEND"], (";VALUE=DATE", "20270101"))   # exclusive end, across the year
        self.assertEqual(by["b"]["DTEND"], (";VALUE=DATE", "20261014"))   # 1441 minutes covers two days
        self.assertEqual(by["c"]["DTSTART"], ("", "20261012T093000"))     # floating: no Z
        self.assertEqual(by["c"]["DTEND"], ("", "20261012T100000"))
        self.assertNotIn("DTEND", by["d"])                                 # zero length: an instant

    def test_status_mapping(self):
        for status, want in (("idea", "TENTATIVE"), ("draft", "TENTATIVE"), ("ready", "TENTATIVE"),
                             ("posted", "CONFIRMED"), ("skipped", "CANCELLED")):
            (ev,) = parse_ics(calendar.to_ics([entry(status=status)], "Plan", NOW))
            self.assertEqual(ev["STATUS"][1], want, status)

    def test_escaping_and_folding_round_trip(self):
        title = "A, B; C \\ D"
        caption = ("Line 1, with comma; and semicolon\nLine 2 \\ backslash\n"
                   + "\U0001F389 emoji éè \U0001F468‍\U0001F469‍\U0001F467 " * 30).strip()
        e = entry(channel="tiktok", title=title, caption=caption, status="draft")
        text = calendar.to_ics([e], "Studio, plan; 2026", NOW)
        (ev,) = parse_ics(text)   # every physical line <= 75 octets, valid UTF-8 on its own
        self.assertEqual(unescape(ev["SUMMARY"][1]), "[tiktok] " + title)
        self.assertEqual(unescape(ev["DESCRIPTION"][1]), caption + "\n\nStatus: draft")
        self.assertIn("X-WR-CALNAME:Studio\\, plan\\; 2026", text)
        self.assertGreater(text.count("\r\n "), 10)   # it really folded

    def test_fold_unit(self):
        for s in ("x" * 200, "é" * 100, "\U0001F389" * 50, "ab" + "€" * 80, "short"):
            folded = calendar.fold("DESCRIPTION:" + s)
            for ln in folded.split("\r\n"):
                self.assertLessEqual(len(ln.encode("utf-8")), 75)
            self.assertEqual(re.sub(r"\r\n[ \t]", "", folded), "DESCRIPTION:" + s)
        self.assertEqual(calendar.fold("x" * 75), "x" * 75)
        self.assertEqual(calendar.fold("x" * 76), "x" * 75 + "\r\n x")

    def test_escape_drops_controls(self):
        self.assertEqual(calendar.escape_text("a\x00b\x1bc\r\nd\te"), "abc\\nd\te")


class RowsTest(unittest.TestCase):
    def test_header_is_fixed(self):
        self.assertEqual(calendar.CALENDAR_COLUMNS,
                         ["id", "when", "channel", "title", "caption", "status", "project", "asset", "updated"])

    def test_round_trip(self):
        e = entry(channel="instagram_reel", title="T", caption="a\nb, c", status="ready",
                  project="p_0123456789ab", asset={"root": "gen", "name": "pic_1.png"})
        row = calendar.to_row(e)
        self.assertEqual(row, ["c_0123456789", "2026-10-12T09:30", "instagram_reel", "T", "a\nb, c", "ready",
                               "p_0123456789ab", "gen/pic_1.png", "2026-10-08T11:00:00Z"])
        back = calendar.from_row(row, calendar.CALENDAR_COLUMNS)
        self.assertEqual(back, {"id": e["id"], "when": e["when"], "channel": e["channel"], "title": "T",
                                "caption": e["caption"], "status": "ready", "project": e["project"],
                                "asset": e["asset"], "updated": e["updated"]})
        empty = calendar.to_row(entry())
        self.assertEqual(empty[6:8], ["", ""])

    def test_extra_missing_and_reordered_columns(self):
        header = ["Notes", " Title ", "WHEN", "status", "owner"]
        out = calendar.from_row(["ignore me", "Hi", "2026-10-12", "draft", "x"], header)
        self.assertEqual(out, {"title": "Hi", "when": "2026-10-12", "status": "draft"})
        self.assertNotIn("caption", out)   # a column the sheet lacks is left alone on update

    def test_short_rows_and_empty_cells(self):
        # the Sheets API drops trailing empty cells: a short row clears those columns, never errors
        out = calendar.from_row(["", "2026-10-12", "tiktok"], calendar.CALENDAR_COLUMNS)
        self.assertEqual(out, {"when": "2026-10-12", "channel": "tiktok", "title": "Untitled", "caption": "",
                               "project": None, "asset": None})
        self.assertNotIn("id", out)        # empty id = a new row
        self.assertNotIn("status", out)    # empty status keeps the current one (or idea on create)

    def test_numbers_from_the_sheet(self):
        out = calendar.from_row([2026.0, 15], ["title", "channel"])
        self.assertEqual(out, {"title": "2026", "channel": "15"})

    def test_bad_rows_name_the_column(self):
        with self.assertRaisesRegex(ValueError, r"column status: status 'published' isn't valid; use idea"):
            calendar.from_row(["published"], ["status"])
        with self.assertRaisesRegex(ValueError, "column when"):
            calendar.from_row(["next tuesday"], ["when"])
        with self.assertRaisesRegex(BadRef, "column asset"):
            calendar.from_row(["etc/passwd"], ["asset"])
        with self.assertRaisesRegex(BadRef, "column id"):
            calendar.from_row(["bad id!"], ["id"])
        with self.assertRaises(ValueError):
            calendar.from_row("a,b", calendar.CALENDAR_COLUMNS)

    def test_row_feeds_create_and_update(self):
        with TempRoots() as r:
            store.configure(r.config())
            fields = calendar.from_row(["row-7", "2026-10-12", "x", "From sheet", "", "ready"],
                                       calendar.CALENDAR_COLUMNS)
            eid = fields.pop("id")
            fields.pop("updated", None)
            e = calendar.create(fields, eid=eid)
            self.assertEqual((e["id"], e["status"], e["title"]), ("row-7", "ready", "From sheet"))
            patch = calendar.from_row(["row-7", "2026-10-13", "", "", "", "posted", "", "", e["updated"]],
                                      calendar.CALENDAR_COLUMNS)
            e2 = calendar.update(patch.pop("id"), e["rev"], patch)   # id/updated are read-only: ignored
            self.assertEqual((e2["when"], e2["status"], e2["title"], e2["rev"]), ("2026-10-13", "posted", "Untitled", 2))


# --------------------------------------------------------------------------------------------- store

class StoreTest(unittest.TestCase):
    def setUp(self):
        self.r = TempRoots()
        self.addCleanup(self.r.cleanup)
        store.configure(self.r.config())
        self.file = os.path.join(self.r.data, "calendar", "entries.json")

    def test_empty_until_first_write(self):
        self.assertEqual(calendar.list_entries(), [])
        self.assertFalse(os.path.exists(os.path.dirname(self.file)))
        calendar.create({"when": "2026-10-12"})
        self.assertTrue(os.path.isfile(self.file))
        self.assertEqual(os.stat(os.path.dirname(self.file)).st_mode & 0o777, 0o700)
        with open(self.file, encoding="utf-8") as f:
            doc = json.load(f)
        self.assertEqual(doc["schema"], "media-lab/calendar@1")
        self.assertEqual(len(doc["entries"]), 1)

    def test_crud_and_rev(self):
        e = calendar.create({"when": "2026-10-12T09:00", "title": "One", "channel": "reel"})
        self.assertEqual((e["rev"], e["status"], e["created"]), (1, "idea", e["updated"]))
        self.assertEqual(calendar.get(e["id"]), e)
        e2 = calendar.update(e["id"], 1, {"status": "draft", "caption": "Hi"})
        self.assertEqual((e2["rev"], e2["status"], e2["caption"], e2["title"], e2["created"]),
                         (2, "draft", "Hi", "One", e["created"]))
        with self.assertRaises(Conflict) as cm:
            calendar.update(e["id"], 1, {"title": "stale"})
        self.assertEqual(cm.exception.current["rev"], 2)
        self.assertEqual(calendar.get(e["id"])["title"], "One")   # the stale write changed nothing
        with self.assertRaises(Conflict):
            calendar.delete(e["id"], rev=1)
        calendar.delete(e["id"], rev=2)
        with self.assertRaises(NotOwned):
            calendar.get(e["id"])
        with self.assertRaises(NotOwned):
            calendar.update(e["id"], 2, {})
        with self.assertRaises(NotOwned):
            calendar.delete(e["id"])

    def test_other_entries_do_not_conflict(self):
        a = calendar.create({"when": "2026-10-12"})
        b = calendar.create({"when": "2026-10-13"})
        calendar.update(a["id"], 1, {"title": "A"})
        self.assertEqual(calendar.update(b["id"], 1, {"title": "B"})["rev"], 2)

    def test_rev_must_be_an_int(self):
        e = calendar.create({"when": "2026-10-12"})
        for bad in (True, "1", 1.0, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                calendar.update(e["id"], bad, {})

    def test_list_filters_and_sorts(self):
        for w in ("2026-10-14T08:00", "2026-10-12", "2026-10-12T09:00", "2026-11-01", "2026-09-30T23:00"):
            calendar.create({"when": w, "title": w})
        self.assertEqual([e["when"] for e in calendar.list_entries()],
                         ["2026-09-30T23:00", "2026-10-12", "2026-10-12T09:00", "2026-10-14T08:00", "2026-11-01"])
        self.assertEqual([e["when"] for e in calendar.list_entries("2026-10-01", "2026-10-31")],
                         ["2026-10-12", "2026-10-12T09:00", "2026-10-14T08:00"])
        self.assertEqual(len(calendar.list_entries(to="2026-10-12")), 3)
        for bad in ("2026-10", "yesterday", "2026-02-30"):
            with self.assertRaises(ValueError):
                calendar.list_entries(bad)

    def test_backup_and_atomic_write_leave_no_temp_files(self):
        e = calendar.create({"when": "2026-10-12", "title": "v1"})
        calendar.update(e["id"], 1, {"title": "v2"})
        with open(self.file + ".bak", encoding="utf-8") as f:
            self.assertEqual(json.load(f)["entries"][0]["title"], "v1")
        self.assertEqual(sorted(os.listdir(os.path.dirname(self.file))), ["entries.json", "entries.json.bak"])

    def test_unreadable_file_is_never_overwritten(self):
        os.makedirs(os.path.dirname(self.file))
        for junk in (b"{not json", b"[1, 2]", b'{"entries": 5}'):
            with open(self.file, "wb") as f:
                f.write(junk)
            with self.assertRaises(calendar.Unreadable) as cm:
                calendar.create({"when": "2026-10-12"})
            self.assertEqual(cm.exception.status, 503)
            self.assertNotIn(self.r.data, str(cm.exception))
            with open(self.file, "rb") as f:
                self.assertEqual(f.read(), junk)

    def test_create_with_given_id(self):
        calendar.create({"when": "2026-10-12"}, eid="row-1")
        with self.assertRaisesRegex(ValueError, "already used"):
            calendar.create({"when": "2026-10-12"}, eid="row-1")
        with self.assertRaises(BadRef):
            calendar.create({"when": "2026-10-12"}, eid="../x")

    def test_full_calendar(self):
        with mock.patch.object(calendar, "MAX_ENTRIES", 2):
            calendar.create({"when": "2026-10-12"})
            calendar.create({"when": "2026-10-12"})
            with self.assertRaisesRegex(ValueError, "full"):
                calendar.create({"when": "2026-10-12"})

    def test_bad_create_writes_nothing(self):
        with self.assertRaises(ValueError):
            calendar.create({"when": "2026-10-12", "status": "nope"})
        self.assertFalse(os.path.exists(self.file))


if __name__ == "__main__":
    unittest.main()
