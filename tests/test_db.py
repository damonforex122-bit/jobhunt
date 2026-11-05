from jobhunt import db
from jobhunt.models import Fit, Job, Person

URL = "https://jobs.ashbyhq.com/AfterQuery/a565ac3b"


def job(**kw):
    base = dict(company="AfterQuery", title="Evals Eng", url=URL, source="import")
    base.update(kw)
    return Job(**base)


def test_upsert_job_idempotent(conn):
    id1, new1 = db.upsert_job(conn, job())
    id2, new2 = db.upsert_job(conn, job(url=URL + "/application"))
    assert new1 is True and new2 is False and id1 == id2
    assert conn.execute("select count(*) from jobs").fetchone()[0] == 1


def test_upsert_job_keeps_status(conn):
    jid, _ = db.upsert_job(conn, job(status="applied"))
    db.upsert_job(conn, job(status="new"))
    assert db.get_job(conn, jid)["status"] == "applied"


def test_save_and_get_fit_roundtrip(conn):
    jid, _ = db.upsert_job(conn, job())
    f = Fit(
        job_id=jid, keyword_score=7, missing_keywords=["evals", "sql"], llm_score=8,
        match_pct=80, reasons="r", gaps="g", rewritten_bullets=[{"old": "a", "new": "b"}],
        model="m", graded_at="2026-01-01T00:00:00", error=None,
    )
    db.save_fit(conn, f)
    assert db.get_fit(conn, jid) == f
    assert db.list_jobs(conn, min_score=8)[0]["id"] == jid
    assert db.list_jobs(conn, min_score=9) == []


def test_upsert_person_dedup(conn):
    p = Person(company="Acme", job_id=None, name="Jane", source="exa",
               source_url="https://x.com", confidence=0.5)
    id1 = db.upsert_person(conn, p)
    id2 = db.upsert_person(conn, p)
    assert id1 == id2
    assert len(db.list_people(conn, "Acme")) == 1


def test_scan_upsert_clears_stale_flags(conn):
    jid, _ = db.upsert_job(conn, job(source="ashby", flags=["remote"]))
    db.upsert_job(conn, job(source="ashby", flags=[]))
    assert db.get_job(conn, jid)["flags"] == []


def test_import_upsert_keeps_flags(conn):
    jid, _ = db.upsert_job(conn, job(source="ashby", flags=["remote"]))
    db.upsert_job(conn, job(source="proficiently", flags=[]))
    assert db.get_job(conn, jid)["flags"] == ["remote"]
