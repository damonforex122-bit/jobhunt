import base64
from datetime import datetime, timedelta, timezone

import httpx
import respx

from jobhunt.gmail.client import API, GmailClient
from jobhunt.gmail.invites import parse_ics, parse_subject

PST = timezone(timedelta(hours=-8))
NOW = datetime(2026, 11, 5, tzinfo=PST)


def ics(*events, method="REQUEST"):
    return "BEGIN:VCALENDAR\r\nMETHOD:" + method + "\r\n" + "".join(
        "BEGIN:VEVENT\r\n" + "\r\n".join(e) + "\r\nEND:VEVENT\r\n" for e in events) + "END:VCALENDAR\r\n"


def test_parse_ics_utc_tzid_and_floating():
    text = ics(
        ["UID:a", "DTSTART:20261109T191500Z", "DTEND:20261109T201500Z", "SUMMARY:Utc call",
         "LOCATION:Zoom"],
        ["UID:b", "DTSTART;TZID=America/New_York:20261109T100000", "SUMMARY:NY call"],
        ["UID:c", "DTSTART:20261109T100000", "SUMMARY:Floating"],
        ["UID:d", "DTSTART;VALUE=DATE:20261110", "SUMMARY:All day"])
    a, b, c, d = parse_ics(text, tz=PST)
    assert a.start == datetime(2026, 11, 9, 19, 15, tzinfo=timezone.utc)
    assert a.end == datetime(2026, 11, 9, 20, 15, tzinfo=timezone.utc) and a.location == "Zoom"
    assert b.start.astimezone(timezone.utc).hour == 15  # 10:00 EST
    assert c.start.astimezone(timezone.utc).hour == 18  # 10:00 PST
    assert d.start == datetime(2026, 11, 10, 9, tzinfo=PST)
    assert not a.cancelled and a.uid == "a"


def test_parse_ics_folded_lines_and_conference_link():
    text = ics(["UID:x", "DTSTART:20261109T191500Z", "SUMMARY:Interview with Hippo",
                " cratic AI", "X-GOOGLE-CONFERENCE:https://meet.google.com/abc-defg-hij"])
    [i] = parse_ics(text, tz=PST)
    assert i.summary == "Interview with Hippocratic AI"
    assert i.link == "https://meet.google.com/abc-defg-hij"
    [j] = parse_ics(ics(["DTSTART:20261109T191500Z", "DESCRIPTION:Join at https://zoom.us/j/1\\, ok"]),
                    tz=PST)
    assert j.link == "https://zoom.us/j/1" and j.uid is None


def test_parse_ics_cancel():
    [i] = parse_ics(ics(["UID:a", "DTSTART:20261109T191500Z"], method="CANCEL"), tz=PST)
    assert i.cancelled
    [j] = parse_ics(ics(["UID:a", "DTSTART:20261109T191500Z", "STATUS:CANCELLED"]), tz=PST)
    assert j.cancelled


def test_parse_subject_google_invite_formats():
    i = parse_subject("Invitation: Interview with Hippocratic AI @ Mon Nov 9, 2026 11:15am", NOW,
                      tz=PST)
    assert i.summary == "Interview with Hippocratic AI" and i.uid is None
    assert i.start == datetime(2026, 11, 9, 11, 15, tzinfo=PST) and i.end is None
    u = parse_subject("Updated invitation: Interview with Hippocratic AI @ Thu Nov 12, 2026 "
                      "2pm - 3pm (PST) (me@x.com)", NOW, tz=PST)
    assert u.start == datetime(2026, 11, 12, 14, tzinfo=PST)
    assert u.end == datetime(2026, 11, 12, 15, tzinfo=PST)
    r = parse_subject("Invitation: Chat @ Mon Nov 9, 2026 11:15am - 12pm", NOW, tz=PST)
    assert r.end == datetime(2026, 11, 9, 12, tzinfo=PST)
    y = parse_subject("Invitation: Chat @ Mon Nov 9 11am", NOW, tz=PST)
    assert y.start.year == 2026
    assert parse_subject("Re: your application", NOW, tz=PST) is None


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


@respx.mock
def test_client_get_fetches_ics_attachment():
    inline = ics(["UID:i", "DTSTART:20261109T191500Z"])
    attached = ics(["UID:a", "DTSTART:20261110T191500Z"])
    respx.get(f"{API}/messages/m1").respond(json={
        "id": "m1", "internalDate": "1793750400000", "snippet": "s",
        "payload": {"mimeType": "multipart/mixed",
                    "headers": [{"name": "From", "value": "a@b.com"}],
                    "parts": [
                        {"mimeType": "text/plain", "body": {"data": b64(b"hi")}},
                        {"mimeType": "text/calendar", "body": {"data": b64(inline.encode())}},
                        {"mimeType": "application/octet-stream", "filename": "invite.ics",
                         "body": {"attachmentId": "att1"}}]}})
    route = respx.get(f"{API}/messages/m1/attachments/att1").respond(
        json={"data": b64(attached.encode())})
    msg = GmailClient("tok", httpx.Client()).get("m1")
    assert route.called and msg.ics == [inline, attached]
