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


@respx.mock
def test_bad_message_is_skipped_and_marked_seen_good_one_lands(conn, tmp_path, job):
    mock({"good": invite_msg("good", "Invitation: x"),
          "bad": invite_msg("bad", "Invitation: y", start="99991231T235959")})
    assert sync(conn, tmp_path) == 1
    assert len(listed(conn)) == 1
    assert db.interview_seen_ids(conn) == {"good", "bad"}


@respx.mock
def test_transient_fetch_error_skips_without_marking_seen(conn, tmp_path, job):
    mock({"good": invite_msg("good", "Invitation: x"), "flaky": invite_msg("flaky", "Invitation: y")})
    respx.get(f"{API}/messages/flaky").respond(status_code=500, json={"error": {"message": "boom"}})
    assert sync(conn, tmp_path) == 1
    assert db.interview_seen_ids(conn) == {"good"}


@respx.mock
def test_update_without_job_match_keeps_job_and_company(conn, tmp_path, job):
    mock({"m1": invite_msg("m1", "Invitation: x")})
    sync(conn, tmp_path)
    mock({"m2": invite_msg("m2", "Updated invitation: x", sender="Someone <s@unrelated.example>")})
    sync(conn, tmp_path)
    [row] = listed(conn)
    assert row["gmail_id"] == "m2" and row["job_id"] == job and row["company"] == "Hippocratic AI"


@respx.mock
def test_unknown_tzid_falls_back_to_subject_time(conn, tmp_path, job):
    msg = invite_msg("m1", "Invitation: x @ Mon Nov 9, 2026 11:15am")
    cal = msg["payload"]["parts"][1]
    cal["body"]["data"] = b64("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:u\r\n"
                              "DTSTART;TZID=Mars/Olympus:20261109T050000\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    mock({"m1": msg})
    sync(conn, tmp_path)
    assert listed(conn)[0]["start"] == "2026-11-09T19:15:00+00:00"


def test_cli_interview_failure_still_prints_suggestion_summary(tmp_path, monkeypatch, capsys):
    import argparse

    from jobhunt.gmail import interviews, sync

    settings = Settings(db_path=tmp_path / "c.db")
    monkeypatch.setattr("jobhunt.config.load_settings", lambda: settings)
    monkeypatch.setattr(sync, "run_sync", lambda *a, **k: sync.SyncReport(4, 1))

    def boom(*a, **k):
        raise RuntimeError("calendar broke")

    monkeypatch.setattr(interviews, "run_interview_sync", boom)
    p = argparse.ArgumentParser()
    sync.register(p.add_subparsers())
    args = p.parse_args(["gmail-sync"])
    assert args.handler(args) == 0
    out = capsys.readouterr().out
    assert "Checked 4 new emails, 1 suggested" in out and "Couldn't read interviews" in out
