"""Read calendar invites: .ics VEVENTs, or the Google Calendar subject line as a fallback."""
import os
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
    tz_unknown: bool = False  # TZID could not be resolved; start/end were read as local time


def local_tz() -> tzinfo | None:
    """The machine's real timezone (so DST is right for any date), or None when it can't be
    named; callers then convert each naive datetime with the system's own rules."""
    name = os.environ.get("TZ", "").lstrip(":")
    if not name:
        try:
            link = os.path.realpath("/etc/localtime")
            name = link.split("zoneinfo/", 1)[1] if "zoneinfo/" in link else ""
        except OSError:
            name = ""
    try:
        return ZoneInfo(name) if name else None
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def _localize(naive: datetime, tz: tzinfo | None) -> datetime:
    return naive.replace(tzinfo=tz) if tz else naive.astimezone()


_WINDOWS_ZONES = {
    "pacific standard time": "America/Los_Angeles", "mountain standard time": "America/Denver",
    "central standard time": "America/Chicago", "eastern standard time": "America/New_York",
    "gmt standard time": "Europe/London", "w. europe standard time": "Europe/Berlin",
    "romance standard time": "Europe/Paris", "central europe standard time": "Europe/Budapest",
}


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


def _when(value: str, params: dict[str, str], tz: tzinfo | None) -> tuple[datetime, bool] | None:
    """Returns (aware datetime, tz_unknown) or None when unreadable."""
    value = value.strip()
    unknown = False
    try:
        if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
            d = datetime.strptime(value[:8], "%Y%m%d").date()
            out = _localize(datetime.combine(d, time(9, 0)), tz)
        elif value.endswith("Z"):
            out = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        else:
            naive = datetime.strptime(value, "%Y%m%dT%H%M%S")
            name = params.get("TZID", "")
            zone = None
            if name:
                for cand in (name, _WINDOWS_ZONES.get(name.lower(), "")):
                    try:
                        zone = ZoneInfo(cand) if cand else None
                    except (ZoneInfoNotFoundError, ValueError, OSError):
                        zone = None
                    if zone:
                        break
                unknown = zone is None
            out = naive.replace(tzinfo=zone) if zone else _localize(naive, tz)
        out.astimezone(timezone.utc)  # raises OverflowError at the calendar's edges
    except (ValueError, OverflowError):
        return None
    return out, unknown


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
        got = _when(first["DTSTART"][1], first["DTSTART"][0], tz)
        if got is None:
            continue
        start, unknown = got
        e = _when(first["DTEND"][1], first["DTEND"][0], tz) if "DTEND" in first else None
        end = e[0] if e else None
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
            link=link, cancelled=method == "CANCEL" or status == "CANCELLED", tz_unknown=unknown))
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
    start = _localize(datetime.combine(day, t_start), tz)
    end = None
    if t2 and (t_end := _clock(t2)):
        end = _localize(datetime.combine(day, t_end), tz)
        if end <= start:
            end = _localize(datetime.combine(day + timedelta(days=1), t_end), tz)
    return Invite(uid=None, start=start, end=end, summary=m.group("summary").strip())
