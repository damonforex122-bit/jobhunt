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


def message(mid, sender, subject, body, html=False, ms=1793750400000, extra=()):
    part = {"mimeType": "text/html" if html else "text/plain", "body": {"data": b64(body)}}
    headers = [{"name": "From", "value": sender}, {"name": "Subject", "value": subject}]
    headers += [{"name": k, "value": v} for k, v in extra]
    return {"id": mid, "internalDate": str(ms), "snippet": body[:40],
            "payload": {"mimeType": "multipart/alternative", "headers": headers, "parts": [part]}}


MSGS = {
    "m1": message("m1", "Harvey Recruiting <no-reply@ashbyhq.com>", "Next steps with Harvey",
                  "We'd love to set up a phone screen."),
    "m2": message("m2", "news@example.com", "Weekly digest", "Unfortunately, nothing new."),
    "m3": message("m3", "jane@exa.ai", "Exa onsite", "<p>Agenda for your <b>onsite</b></p>", html=True),
    "m4": message("m4", "jane@exa.ai", "Thanks for applying", "Thank you for applying to Exa"),
}


def mock_gmail(ids=("m1", "m2", "m3", "m4")):
    respx.get(f"{API}/profile").respond(json={"emailAddress": "me@gmail.com"})
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
    assert c.profile_email() == "me@gmail.com" and m.bulk is False
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
    assert len(respx.calls) == calls + 2  # profile + list; no message re-fetched


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


NEWS = message("n1", "TLDR <dan@tldrnewsletter.com>", "Exa raises; OpenAI news",
               "Unfortunately the market fell. We extend an offer to subscribers.",
               extra=[("List-Unsubscribe", "<mailto:u@tldr.com>")])
ATS_BULK = message("a1", "Harvey Recruiting <no-reply@ashbyhq.com>", "Next steps with Harvey",
                   "phone screen", extra=[("List-Unsubscribe", "<mailto:u@ashbyhq.com>")])
MINE = message("s1", "Alex Sample <me@gmail.com>", "Re: Exa onsite", "See you at the onsite")
NEWCO = message("c1", "Robyn <robyn@hippocraticai.com>",
                "Invitation: Interview with Hippocratic AI @ Mon Nov 9", "calendar")
NEWCO_EARLY = message("c2", "Hippocratic AI Hiring Team <no-reply@ashbyhq.com>",
                      "Thanks for applying to Hippocratic AI!", "", ms=1793500000000)
UNKNOWN_REJECT = message("c3", "QuantCo <no-reply@hire.lever.co>", "Your QuantCo Application",
                         "Unfortunately we will not be moving forward.")


def mock_list(*msgs):
    respx.get(f"{API}/profile").respond(json={"emailAddress": "me@gmail.com"})
    respx.get(f"{API}/messages").respond(json={"messages": [{"id": m["id"]} for m in msgs]})
    for m in msgs:
        respx.get(f"{API}/messages/{m['id']}").respond(json=m)


@respx.mock
def test_newsletters_and_own_replies_are_ignored_but_ats_mail_is_not(conn, jobs):
    h, e = jobs
    mock_list(NEWS, ATS_BULK, MINE)
    report = sync.run_sync(conn, Settings(), client=gmail())
    assert report.suggested == 1
    [s] = db.list_gmail_suggestions(conn)
    assert s["job_id"] == h and s["suggested_status"] == "screening"


@respx.mock
def test_unknown_company_becomes_one_add_suggestion_at_highest_stage(conn, jobs):
    mock_list(NEWCO, NEWCO_EARLY, UNKNOWN_REJECT)
    report = sync.run_sync(conn, Settings(), client=gmail())
    assert report.suggested == 3
    by_co = {s["company"]: s for s in db.list_gmail_suggestions(conn)}
    assert set(by_co) == {"Hippocratic AI", "QuantCo"}
    s = by_co["Hippocratic AI"]
    assert s["job_id"] is None and s["suggested_status"] == "interviewing"
    assert s["current_status"] is None
    assert by_co["QuantCo"]["suggested_status"] == "rejected"  # true outcome, not "applied"


@respx.mock
def test_accepting_new_company_creates_pipeline_job_and_relinks_rest(conn, jobs):
    mock_list(NEWCO, NEWCO_EARLY)
    sync.run_sync(conn, Settings(), client=gmail())
    [s] = db.list_gmail_suggestions(conn)
    db.resolve_gmail_suggestion(conn, s["id"], accept=True)
    job = [j for j in db.list_jobs(conn) if j["company"] == "Hippocratic AI"]
    assert len(job) == 1 and job[0]["status"] == "interviewing" and job[0]["source"] == "gmail"
    assert db.list_gmail_suggestions(conn) == []  # the earlier "applied" email is now stale
    assert conn.execute("SELECT COUNT(*) FROM gmail_messages WHERE job_id IS NULL "
                        "AND state = 'pending'").fetchone()[0] == 0


@respx.mock
def test_dismissing_new_company_dismisses_all_its_emails(conn, jobs):
    mock_list(NEWCO, NEWCO_EARLY)
    sync.run_sync(conn, Settings(), client=gmail())
    [s] = db.list_gmail_suggestions(conn)
    db.resolve_gmail_suggestion(conn, s["id"], accept=False)
    assert db.list_gmail_suggestions(conn) == []
    assert not [j for j in db.list_jobs(conn) if j["company"] == "Hippocratic AI"]


@respx.mock
def test_recheck_reconsiders_ignored_emails(conn, jobs):
    h, _ = jobs
    db.save_gmail_message(conn, "m1")  # marked ignored by an older version of the rules
    mock_gmail(ids=("m1",))
    assert sync.run_sync(conn, Settings(), client=gmail()).scanned == 0
    report = sync.run_sync(conn, Settings(), client=gmail(), recheck=True)
    assert (report.scanned, report.suggested) == (1, 1)
    assert db.list_gmail_suggestions(conn)[0]["job_id"] == h


def test_query_avoids_bare_offer_keyword():
    q = sync.build_query(30)
    assert " offer " not in f" {q} " and '"offer letter"' in q


def test_one_suggestion_per_job_at_furthest_stage(conn, jobs):
    h, _ = jobs
    for gid, stage, day in [("a", "screening", "2026-11-01"), ("b", "interviewing", "2026-11-02"),
                            ("c", "screening", "2026-11-03")]:
        db.save_gmail_message(conn, gid, job_id=h, sender="s", subject=gid, received=day,
                              snippet="", suggested_status=stage)
    [s] = db.list_gmail_suggestions(conn)
    assert s["suggested_status"] == "interviewing" and s["subject"] == "b"
    db.resolve_gmail_suggestion(conn, s["id"], accept=True)
    assert db.list_gmail_suggestions(conn) == []


@respx.mock
def test_recheck_clears_pending_suggestion_the_new_rules_reject(conn, jobs):
    bad = message("b1", "x@gmail.com", "Re: Job description for your review - thoughts?",
                  "Let's meet the team next week")
    db.save_gmail_message(conn, "b1", sender="x", subject="old", received="2026-11-01",
                          snippet="", suggested_status="interviewing",
                          company="Job description for your review")
    mock_list(bad)
    sync.run_sync(conn, Settings(), client=gmail(), recheck=True)
    row = conn.execute("SELECT state, subject, company FROM gmail_messages WHERE gmail_id='b1'").fetchone()
    assert tuple(row) == ("ignored", None, None)
    assert db.list_gmail_suggestions(conn) == []
