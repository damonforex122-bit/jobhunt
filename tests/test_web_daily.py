from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from jobhunt import db
from jobhunt.config import Settings
from jobhunt.models import Job
from jobhunt.web import app as webapp

NY = ZoneInfo("America/New_York")
TODAY = date(2026, 11, 5)


def tc(app):
    return TestClient(app, base_url="http://127.0.0.1")


def _job(conn, n, source="scan", added_at=None):
    jid, _ = db.upsert_job(conn, Job(company=f"Co{n}", title=f"Role{n}", url=f"https://x.test/{n}",
                                     description="d", source=source))
    if added_at:
        conn.execute("UPDATE jobs SET added_at = ? WHERE id = ?", (added_at, jid))
        conn.commit()
    return jid


def _event(conn, jid, status, at, source=None):
    conn.execute("INSERT INTO job_events (job_id, status, at, source) VALUES (?, ?, ?, ?)",
                 (jid, status, at, source))
    conn.commit()


def test_new_today_counts_only_rows_inserted_today_after_old_history_reimport(conn):
    # an old row re-imported today keeps its old added_at; first_seen was reset to an old date
    old = _job(conn, 1, "proficiently", added_at="2026-10-01T15:00:00+00:00")
    # 2026-11-06T03:30Z is Nov 5 22:30 in New York -> today
    late = _job(conn, 2, "scan", added_at="2026-11-06T03:30:00+00:00")
    # 2026-11-05T03:30Z is Nov 4 22:30 in New York -> yesterday
    early = _job(conn, 3, "scan", added_at="2026-11-05T03:30:00+00:00")
    got = [j["id"] for j in db.list_jobs(conn, added_on=TODAY, tz=NY)]
    assert got == [late]
    assert old not in got and early not in got


def test_source_filter(conn):
    a = _job(conn, 1, "proficiently")
    _job(conn, 2, "scan")
    ids = [j["id"] for j in db.list_jobs(conn, source="proficiently")]
    assert ids == [a]
    assert len(db.list_jobs(conn)) == 2


def test_today_summary_moved_excludes_triage(conn):
    now = "2026-11-05T15:00:00+00:00"
    _job(conn, 1, "proficiently", added_at=now)
    _job(conn, 2, "proficiently", added_at=now)
    _job(conn, 3, "scan", added_at=now)
    j = _job(conn, 4, "gmail", added_at="2026-10-01T00:00:00+00:00")
    _event(conn, j, "ready", now, "proficiently")      # triage: no
    _event(conn, j, "low", now, "grader")              # triage: no
    _event(conn, j, "applied", now, "proficiently")    # forward move: yes
    _event(conn, j, "rejected", now, "gmail")          # closed: yes
    _event(conn, j, "interviewing", "2026-11-04T15:00:00+00:00", "user")  # yesterday: no
    s = db.today_summary(conn, TODAY, tz=NY)
    assert s["new_by_source"] == {"proficiently": 2, "scan": 1}
    assert s["moved"] == 2
    assert s["interviews_upcoming"] == 0
    assert s["digest_run_id"] is None


def _client(tmp_path):
    s = Settings(db_path=tmp_path / "w.db", proficiently_dir=tmp_path / "prof",
                 master_resume_path=tmp_path / "m.pdf", resume_output_dir=tmp_path / "out")
    return s, tc(webapp.create_app(s))


def test_pipeline_strip_and_digest_link(tmp_path):
    s, client = _client(tmp_path)
    conn = db.connect(s.db_path)
    _job(conn, 1, "proficiently")
    _job(conn, 2, "proficiently")
    _job(conn, 3, "scan")
    j = _job(conn, 4, "link", added_at="2026-10-01T00:00:00+00:00")
    db.set_job_status(conn, j, "applied")
    today = datetime.now().astimezone().date()
    rid = db.start_run(conn, "digest", str(tmp_path / "prof" / "daily-digest" / f"{today.isoformat()}.md"))
    other = db.start_run(conn, "digest", str(tmp_path / "prof" / "daily-digest" / "2020-01-01.md"))
    conn.close()
    html = client.get("/pipeline").text
    assert "Today: 3 new (Proficiently 2" in html
    assert "Scan 1" in html
    assert "1 moved" in html
    assert f'href="/digests/{rid}"' in html
    assert f"/digests/{other}" not in html


def test_last_run_status_line(tmp_path):
    s, client = _client(tmp_path)
    conn = db.connect(s.db_path)
    assert "Last morning run" not in client.get("/pipeline").text
    r1 = db.start_run(conn, "import")
    db.finish_run(conn, r1, {"n": 1}, error="skipped: no gmail token")
    r2 = db.start_run(conn, "scan")
    db.finish_run(conn, r2, {"n": 1})
    assert "Last morning run" in client.get("/pipeline").text
    assert "✓" in client.get("/pipeline").text.split("Last morning run")[1][:60]
    r3 = db.start_run(conn, "gmail")
    db.finish_run(conn, r3, None, error="boom")
    conn.close()
    assert "✗" in client.get("/pipeline").text.split("Last morning run")[1][:60]


def test_jobs_page_new_today_and_source_controls(tmp_path):
    s, client = _client(tmp_path)
    conn = db.connect(s.db_path)
    _job(conn, 1, "proficiently")
    _job(conn, 2, "scan", added_at="2026-01-01T00:00:00+00:00")
    conn.close()
    html = client.get("/?new=today").text
    assert "Co1" in html and "Co2" not in html
    assert 'name="new"' in html and 'name="source"' in html
    html = client.get("/?source=scan").text
    assert "Co2" in html and "Co1" not in html
