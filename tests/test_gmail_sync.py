import base64

import httpx
import pytest
import respx

from jobhunt import db
from jobhunt.config import Settings
from jobhunt.gmail import auth, sync
from jobhunt.gmail.client import API, GmailClient, GmailError
from jobhunt.models import Job


def b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def message(mid, sender, subject, body, html=False, ms=1793750400000):
    part = {"mimeType": "text/html" if html else "text/plain", "body": {"data": b64(body)}}
    return {"id": mid, "internalDate": str(ms), "snippet": body[:40],
            "payload": {"mimeType": "multipart/alternative",
                        "headers": [{"name": "From", "value": sender},
                                    {"name": "Subject", "value": subject}],
                        "parts": [part]}}


MSGS = {
    "m1": message("m1", "Harvey Recruiting <no-reply@ashbyhq.com>", "Next steps with Harvey",
                  "We'd love to set up a phone screen."),
    "m2": message("m2", "news@example.com", "Weekly digest", "Unfortunately, nothing new."),
    "m3": message("m3", "jane@exa.ai", "Exa onsite", "<p>Agenda for your <b>onsite</b></p>", html=True),
    "m4": message("m4", "jane@exa.ai", "Thanks for applying", "Thank you for applying to Exa"),
}


def mock_gmail(ids=("m1", "m2", "m3", "m4")):
    route = respx.get(f"{API}/messages").respond(json={"messages": [{"id": i} for i in ids]})
    for mid, body in MSGS.items():
        respx.get(f"{API}/messages/{mid}").respond(json=body)
    return route


@pytest.fixture
def jobs(conn):
    h = db.upsert_job(conn, Job(company="Harvey", title="Research Engineer, Data & Evals",
                                url="https://x.test/h", source="ats"))[0]
    e = db.upsert_job(conn, Job(company="Exa", title="Research, Evals", url="https://x.test/e",
                                source="ats"))[0]
    db.set_job_status(conn, h, "applied")
    db.set_job_status(conn, e, "screening")
    return h, e


def gmail():
    return GmailClient("tok", httpx.Client())


@respx.mock
def test_client_reads_headers_body_and_date():
    mock_gmail()
    c = gmail()
    m = c.get("m3")
    assert (m.sender, m.subject, m.date) == ("jane@exa.ai", "Exa onsite", "2026-11-04")
    assert "onsite" in m.body and "<b>" not in m.body
    assert c.list_ids("q", limit=10) == ["m1", "m2", "m3", "m4"]
    req = respx.calls[-1].request
    assert req.headers["authorization"] == "Bearer tok" and "q=q" in str(req.url)


@respx.mock
def test_sync_creates_forward_suggestions_only(conn, jobs):
    h, e = jobs
    route = mock_gmail()
    report = sync.run_sync(conn, Settings(), client=gmail(), days=14)
    assert "newer_than:14d" in route.calls[0].request.url.params["q"]
    assert (report.scanned, report.suggested) == (4, 2)
    sugg = {s["job_id"]: s for s in db.list_gmail_suggestions(conn)}
    assert sugg[h]["suggested_status"] == "screening" and sugg[h]["subject"] == "Next steps with Harvey"
    assert sugg[e]["suggested_status"] == "interviewing"  # "applied" email is backward for Exa
    assert db.get_job(conn, h)["status"] == "applied"  # nothing applied automatically


@respx.mock
def test_sync_skips_messages_already_seen(conn, jobs):
    mock_gmail()
    sync.run_sync(conn, Settings(), client=gmail())
    calls = len(respx.calls)
    report = sync.run_sync(conn, Settings(), client=gmail())
    assert report.scanned == 0 and report.suggested == 0
    assert len(respx.calls) == calls + 1  # only the list call; no message re-fetched


@respx.mock
def test_ignored_messages_store_no_content(conn, jobs):
    mock_gmail()
    sync.run_sync(conn, Settings(), client=gmail())
    row = conn.execute("SELECT * FROM gmail_messages WHERE gmail_id = 'm2'").fetchone()
    assert row["state"] == "ignored" and row["subject"] is None and row["sender"] is None


@respx.mock
def test_accept_moves_job_with_note_and_dismiss_does_not(conn, jobs):
    h, e = jobs
    mock_gmail()
    sync.run_sync(conn, Settings(), client=gmail())
    sugg = {s["job_id"]: s for s in db.list_gmail_suggestions(conn)}
    db.resolve_gmail_suggestion(conn, sugg[h]["id"], accept=True)
    db.resolve_gmail_suggestion(conn, sugg[e]["id"], accept=False)
    assert db.get_job(conn, h)["status"] == "screening"
    assert "Next steps with Harvey" in db.list_events(conn, h)[-1]["note"]
    assert db.get_job(conn, e)["status"] == "screening"
    assert db.list_gmail_suggestions(conn) == []


def test_stale_suggestion_hidden_after_job_moves_past_it(conn, jobs):
    h, _ = jobs
    conn.execute("INSERT INTO gmail_messages (gmail_id, job_id, sender, subject, received, "
                 "suggested_status, state) VALUES ('x', ?, 's', 'subj', '2026-11-01', "
                 "'screening', 'pending')", (h,))
    db.set_job_status(conn, h, "interviewing")
    assert db.list_gmail_suggestions(conn) == []


def test_access_token_without_setup_raises_clear_error(tmp_path):
    s = Settings(gmail_token=tmp_path / "none.json", gmail_client_secret=tmp_path / "none2.json")
    with pytest.raises(auth.GmailNotConnected, match="gmail-auth"):
        auth.access_token(s)


@respx.mock
def test_client_retries_rate_limit_then_succeeds():
    limited = httpx.Response(403, json={"error": {"errors": [{"reason": "rateLimitExceeded"}],
                                                  "message": "Quota exceeded"}})
    route = respx.get(f"{API}/messages").mock(side_effect=[
        limited, httpx.Response(429, headers={"Retry-After": "3"}),
        httpx.Response(200, json={"messages": [{"id": "a"}]})])
    sleeps = []
    c = GmailClient("tok", httpx.Client(), sleep=sleeps.append)
    assert c.list_ids("q") == ["a"]
    assert route.call_count == 3 and sleeps[1] == 3


@respx.mock
def test_client_gives_up_with_google_reason():
    respx.get(f"{API}/messages").respond(403, json={"error": {"message": "Gmail API has not been used",
                                                              "errors": [{"reason": "accessNotConfigured"}]}})
    c = GmailClient("tok", httpx.Client(), sleep=lambda s: None)
    with pytest.raises(GmailError, match="403.*Gmail API has not been used"):
        c.list_ids("q")
