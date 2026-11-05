"""Read calendar invites: .ics VEVENTs, or the Google Calendar subject line as a fallback."""
import re
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel


class Invite(BaseModel):
    uid: str | None = None
    start: datetime  # aware
    end: datetime | None = None
    summary: str = ""
    location: str = ""
    link: str = ""
    cancelled: bool = False


def local_tz() -> tzinfo:
    return datetime.now().astimezone().tzinfo or timezone.utc


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _unescape(v: str) -> str:
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), v)


def _prop(line: str) -> tuple[str, dict[str, str], str] | None:
    if ":" not in line:
        return None
    head, value = line.split(":", 1)
    name, *params = head.split(";")
    pd = {}
    for p in params:
        k, _, v = p.partition("=")
        pd[k.upper()] = v.strip('"')
    return name.upper(), pd, value


def _when(value: str, params: dict[str, str], tz: tzinfo) -> datetime | None:
    value = value.strip()
    try:
        if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
            d = datetime.strptime(value[:8], "%Y%m%d").date()
            return datetime.combine(d, time(9, 0), tzinfo=tz)
        if value.endswith("Z"):
            return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        naive = datetime.strptime(value, "%Y%m%dT%H%M%S")
    except ValueError:
        return None
    if params.get("TZID"):
        try:
            return naive.replace(tzinfo=ZoneInfo(params["TZID"]))
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return naive.replace(tzinfo=tz)


def parse_ics(text: str, tz: tzinfo | None = None) -> list[Invite]:
    tz = tz or local_tz()
    method = ""
    events: list[list[tuple[str, dict[str, str], str]]] = []
    current = None
    for line in _unfold(text):
        p = _prop(line)
        if not p:
            continue
        name, params, value = p
        if name == "BEGIN" and value.strip().upper() == "VEVENT":
            current = []
        elif name == "END" and value.strip().upper() == "VEVENT":
            if current is not None:
                events.append(current)
            current = None
        elif current is not None:
            current.append(p)
        elif name == "METHOD":
            method = value.strip().upper()
    out = []
    for props in events:
        first: dict[str, tuple[dict[str, str], str]] = {}
        for name, params, value in props:
            first.setdefault(name, (params, value))
        if "DTSTART" not in first:
            continue
        start = _when(first["DTSTART"][1], first["DTSTART"][0], tz)
        if start is None:
            continue
        end = _when(first["DTEND"][1], first["DTEND"][0], tz) if "DTEND" in first else None
        uid = first["UID"][1].strip() if "UID" in first else None
        if uid and "RECURRENCE-ID" in first:
            uid += "#" + first["RECURRENCE-ID"][1].strip()
        link = ""
        for key in ("URL", "X-GOOGLE-CONFERENCE"):
            if key in first and first[key][1].strip().startswith("http"):
                link = first[key][1].strip()
                break
        if not link and "DESCRIPTION" in first:
            m = re.search(r"https://[^\s<>\"]+", _unescape(first["DESCRIPTION"][1]))
            link = m.group(0).rstrip(".,;)") if m else ""
        status = first.get("STATUS", ({}, ""))[1].strip().upper()
        out.append(Invite(
            uid=uid or None, start=start, end=end,
            summary=_unescape(first.get("SUMMARY", ({}, ""))[1]).strip(),
            location=_unescape(first.get("LOCATION", ({}, ""))[1]).strip(),
            link=link, cancelled=method == "CANCEL" or status == "CANCELLED"))
    return out


_SUBJECT = re.compile(
    r"^(?:updated invitation|invitation)(?: from google calendar)?:\s*(?P<summary>.+?)\s+@\s+"
    r"(?:(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\s+)?(?P<mon>[A-Za-z]{3})[a-z]*\s+(?P<day>\d{1,2})"
    r"(?:,\s*(?P<year>\d{4}))?\s+(?P<t1>\d{1,2}(?::\d{2})?\s*(?:am|pm)?)"
    r"(?:\s*-\s*(?P<t2>\d{1,2}(?::\d{2})?\s*(?:am|pm)))?", re.I)
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def _clock(t: str, default_ampm: str = "") -> time | None:
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t.strip().lower())
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3) or default_ampm
    if not ap or not 1 <= h <= 12:
        return None
    return time(h % 12 + (12 if ap == "pm" else 0), mi)


def parse_subject(subject: str, now: datetime, tz: tzinfo | None = None) -> Invite | None:
    m = _SUBJECT.match(subject.strip())
    if not m or m.group("mon").lower() not in _MONTHS:
        return None
    tz = tz or local_tz()
    t2 = m.group("t2")
    end_ampm = re.search(r"(am|pm)$", t2.strip().lower()).group(1) if t2 else ""
    t_start = _clock(m.group("t1"), end_ampm)
    if t_start is None:
        return None
    try:
        day = date(int(m.group("year") or now.year), _MONTHS.index(m.group("mon").lower()) + 1,
                   int(m.group("day")))
    except ValueError:
        return None
    start = datetime.combine(day, t_start, tzinfo=tz)
    end = None
    if t2 and (t_end := _clock(t2)):
        end = datetime.combine(day, t_end, tzinfo=tz)
        if end <= start:
            end += timedelta(days=1)
    return Invite(uid=None, start=start, end=end, summary=m.group("summary").strip())
