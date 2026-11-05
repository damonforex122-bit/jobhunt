from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from jobhunt import db
from jobhunt.config import Settings
from jobhunt.models import Job
from jobhunt.web import app as webapp


@pytest.fixture
def settings(tmp_path):
    s = Settings(db_path=tmp_path / "web.db", proficiently_dir=tmp_path / "prof",
                 master_resume_path=tmp_path / "master.pdf", resume_output_dir=tmp_path / "out")
    conn = db.connect(s.db_path)
    db.upsert_job(conn, Job(company="Acme", title="Researcher", url="https://x.test/a",
                            description="d", source="ats"))
    db.upsert_job(conn, Job(company="Globex", title="Analyst", url="https://x.test/b",
                            description="d", source="ats"))
    conn.close()
    return s


@pytest.fixture
def client(settings, monkeypatch):
    monkeypatch.setattr(webapp, "local_tz", lambda: ZoneInfo("America/Los_Angeles"))
    return TestClient(webapp.create_app(settings), base_url="http://127.0.0.1")


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat(timespec="seconds")


def _add(settings, uid, start, **kw):
    conn = db.connect(settings.db_path)
    db.upsert_interview(conn, {"uid": uid, "start": start, "status": "scheduled", **kw})
    conn.commit()
    conn.close()


def test_interviews_page_lists_upcoming_soonest_first(client, settings):
    _add(settings, "late", _iso(timedelta(days=5)), job_id=1, company="Acme", summary="Later chat")
    _add(settings, "soon", _iso(timedelta(days=1)), job_id=2, company="Globex", summary="Sooner chat")
    r = client.get("/interviews")
    assert r.status_code == 200
    assert r.text.index("Sooner chat") < r.text.index("Later chat")
    assert 'href="/jobs/1"' in r.text and "Researcher" in r.text and "Globex" in r.text


def test_local_time_format(client, settings):
    _add(settings, "t", "2030-11-11T19:15:00+00:00", company="Acme", summary="Fixed time")
    assert "Mon Nov 11 · 11:15 AM" in client.get("/interviews").text


def test_past_interviews_collapsed(client, settings):
    _add(settings, "old", _iso(-timedelta(days=3)), company="Acme", summary="Old chat")
    _add(settings, "older", _iso(-timedelta(days=9)), company="Acme", summary="Older chat")
    html = client.get("/interviews").text
    assert "Past interviews" in html
    assert "<details" in html and "<details open" not in html
    assert html.index("Old chat") < html.index("Older chat")
    assert html.index("Past interviews") < html.index("Old chat")
    assert "No upcoming interviews found" in html


def test_closed_job_rows_greyed(client, settings):
    conn = db.connect(settings.db_path)
    db.set_job_status(conn, 1, "rejected")
    conn.close()
    _add(settings, "a", _iso(timedelta(days=1)), job_id=1, company="Acme", summary="Closed one")
    _add(settings, "b", _iso(timedelta(days=2)), job_id=2, company="Globex", summary="Open one")
    html = client.get("/interviews").text
    closed_row = html[html.rindex("<tr", 0, html.index("Closed one")):html.index("Closed one")]
    open_row = html[html.rindex("<tr", 0, html.index("Open one")):html.index("Open one")]
    assert "opacity-50" in closed_row and "opacity-50" not in open_row


def test_join_link_uses_safe_url(client, settings):
    _add(settings, "bad", _iso(timedelta(days=1)), company="Acme", summary="Evil",
         link="javascript:alert(1)", location="Room 4")
    _add(settings, "good", _iso(timedelta(days=2)), company="Acme", summary="Fine",
         link="https://meet.example/abc")
    html = client.get("/interviews").text
    assert "javascript:" not in html
    assert 'href="https://meet.example/abc"' in html and "Join" in html
    assert "Room 4" in html


def test_empty_state(client):
    assert "No upcoming interviews found in Gmail invites." in client.get("/interviews").text


def test_sync_button_returns_to_interviews(client):
    html = client.get("/interviews").text
    assert 'action="/gmail/sync"' in html and 'name="next" value="/interviews"' in html
    r = client.post("/gmail/sync", data={"next": "/interviews"}, follow_redirects=False)
    assert r.headers["location"].startswith("/interviews?msg=")


@pytest.mark.parametrize("bad", ["//evil.test", "https://evil.test", "/\\evil.test", ""])
def test_sync_rejects_unsafe_next(client, bad):
    r = client.post("/gmail/sync", data={"next": bad}, follow_redirects=False)
    assert r.headers["location"].startswith("/pipeline?msg=")


def test_pipeline_links_upcoming_interviews(client, settings):
    assert "upcoming interview" not in client.get("/pipeline").text
    _add(settings, "a", _iso(timedelta(days=1)), company="Acme")
    _add(settings, "b", _iso(timedelta(days=2)), company="Acme")
    html = client.get("/pipeline").text
    assert 'href="/interviews"' in html and "2 upcoming interviews" in html
