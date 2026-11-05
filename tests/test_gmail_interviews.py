import base64
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from jobhunt import db
from jobhunt.config import Settings
from jobhunt.gmail.client import API, GmailClient
from jobhunt.gmail.interviews import run_interview_sync
from jobhunt.models import Job

PST = timezone(timedelta(hours=-8))
NOW = datetime(2026, 11, 5, tzinfo=PST)


def b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def invite_msg(mid, subject, start="20261109T191500Z", method="REQUEST", sender="Robyn <r@hippocraticai.com>",
               body="SECRET BODY TEXT"):
    cal = (f"BEGIN:VCALENDAR\r\nMETHOD:{method}\r\nBEGIN:VEVENT\r\nUID:uid-1\r\nDTSTART:{start}\r\n"
           "SUMMARY:Interview with Hippocratic AI\r\nLOCATION:Zoom\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    return {"id": mid, "internalDate": "1793750400000", "snippet": "SECRET SNIPPET",
            "payload": {"mimeType": "multipart/mixed",
                        "headers": [{"name": "From", "value": sender},
                                    {"name": "Subject", "value": subject}],
                        "parts": [{"mimeType": "text/plain", "body": {"data": b64(body)}},
                                  {"mimeType": "text/calendar", "body": {"data": b64(cal)}}]}}


def mock(msgs: dict):
    respx.get(f"{API}/profile").respond(json={"emailAddress": "me@gmail.com"})
    respx.get(f"{API}/messages").respond(json={"messages": [{"id": i} for i in msgs]})
    for mid, body in msgs.items():
        respx.get(f"{API}/messages/{mid}").respond(json=body)


def sync(conn, tmp_path):
    settings = Settings(db_path=tmp_path / "t.db")
    return run_interview_sync(conn, settings, GmailClient("tok", httpx.Client()), tz=PST, now=NOW)


@pytest.fixture
def job(conn):
    return db.upsert_job(conn, Job(company="Hippocratic AI", title="Research Engineer",
                                   url="https://x.test/h", source="ats"))[0]


def listed(conn):
    return db.list_interviews(conn, now="2026-11-05T00:00:00+00:00")


@respx.mock
def test_sync_creates_then_updates_same_uid_without_duplicate(conn, tmp_path, job):
    mock({"m1": invite_msg("m1", "Invitation: Interview with Hippocratic AI @ Mon Nov 9, 2026 11:15am")})
    assert sync(conn, tmp_path) == 1
    [row] = listed(conn)
    assert row["start"] == "2026-11-09T19:15:00+00:00" and row["job_id"] == job
    assert row["job_title"] == "Research Engineer" and row["location"] == "Zoom"
    mock({"m2": invite_msg("m2", "Updated invitation: Interview with Hippocratic AI @ Tue Nov 10, 2026 2pm",
                           start="20261110T220000Z")})
    assert sync(conn, tmp_path) == 1
    [row] = listed(conn)
    assert row["start"] == "2026-11-10T22:00:00+00:00" and row["gmail_id"] == "m2"
    assert conn.execute("SELECT COUNT(*) FROM interviews").fetchone()[0] == 1


@respx.mock
def test_sync_cancellation_hides_row(conn, tmp_path, job):
    mock({"m1": invite_msg("m1", "Invitation: x")})
    sync(conn, tmp_path)
    assert len(listed(conn)) == 1
    mock({"m2": invite_msg("m2", "Canceled event: x", method="CANCEL")})
    sync(conn, tmp_path)
    assert listed(conn) == []
    assert conn.execute("SELECT status FROM interviews").fetchone()[0] == "cancelled"


@respx.mock
def test_sync_skips_seen_and_stores_no_body(conn, tmp_path, job):
    mock({"m1": invite_msg("m1", "Invitation: x"),
          "m2": invite_msg("m2", "Invitation: mine", sender="me@gmail.com")})
    assert sync(conn, tmp_path) == 1  # own invite skipped
    assert db.interview_seen_ids(conn) == {"m1", "m2"}
    cols = [r[1] for r in conn.execute("PRAGMA table_info(interviews)")]
    assert "body" not in cols and "snippet" not in cols
    dump = " ".join(str(v) for r in conn.execute("SELECT * FROM interviews") for v in tuple(r))
    assert "SECRET" not in dump
    gets = [c for c in respx.calls if c.request.url.path.endswith("/messages/m1")]
    assert sync(conn, tmp_path) == 0
    assert [c for c in respx.calls if c.request.url.path.endswith("/messages/m1")] == gets


@respx.mock
def test_subject_fallback_and_unknown_company(conn, tmp_path):
    msg = invite_msg("m1", "Invitation: Interview with Acme @ Mon Nov 9, 2026 11:15am",
                     sender="Jo <jo@acme.com>")
    msg["payload"]["parts"].pop()  # no calendar part
    mock({"m1": msg})
    assert sync(conn, tmp_path) == 1
    [row] = listed(conn)
    assert row["uid"] == "gmail:m1" and row["job_id"] is None
    assert row["start"] == "2026-11-09T19:15:00+00:00"
