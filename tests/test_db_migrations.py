import sqlite3
from datetime import date, datetime, timezone

from fastapi.testclient import TestClient

from jobhunt import db
from jobhunt.config import Settings
from jobhunt.models import Fit, Job
from jobhunt.web import app as webapp

OLD_SCHEMA_JOBS = """
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, company TEXT NOT NULL, title TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE, location TEXT, salary_min INTEGER, salary_max INTEGER,
    description TEXT, source TEXT NOT NULL, posted_at TEXT, first_seen TEXT,
    status TEXT NOT NULL DEFAULT 'new', flags TEXT NOT NULL DEFAULT '[]');
"""


def _old_db(path):
    """A database as it was before this change: no prof columns, a proficiently-import fit."""
    c = sqlite3.connect(path)
    c.executescript(OLD_SCHEMA_JOBS)
    c.executescript(db.SCHEMA.split("CREATE TABLE IF NOT EXISTS jobs")[0])  # companies only
    c.execute("""CREATE TABLE fits (job_id INTEGER PRIMARY KEY, keyword_score INTEGER NOT NULL,
        missing_keywords TEXT NOT NULL DEFAULT '[]', llm_score INTEGER, match_pct INTEGER,
        reasons TEXT, gaps TEXT, rewritten_bullets TEXT NOT NULL DEFAULT '[]', model TEXT,
        graded_at TEXT NOT NULL, error TEXT)""")
    c.execute("""CREATE TABLE job_events (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL,
        status TEXT NOT NULL, note TEXT, at TEXT NOT NULL)""")
    c.execute("""CREATE TABLE people (id INTEGER PRIMARY KEY AUTOINCREMENT, company TEXT NOT NULL,
        job_id INTEGER, name TEXT NOT NULL, title TEXT, source TEXT NOT NULL,
        source_url TEXT NOT NULL, linkedin_url TEXT, confidence REAL NOT NULL,
        UNIQUE(company, name))""")
    c.execute("INSERT INTO jobs (company,title,url,source,first_seen) VALUES "
              "('A','t','https://a.test/1','import','2026-01-01T00:00:00+00:00')")
    c.execute("INSERT INTO jobs (company,title,url,source,first_seen) VALUES "
              "('B','t','https://b.test/1','ats','2026-02-01T00:00:00+00:00')")
    c.execute("INSERT INTO fits VALUES (1,0,'[]',8,NULL,'prof reason',NULL,'[]','proficiently-import','2026-01-01',NULL)")
    c.execute("INSERT INTO fits VALUES (2,3,'[]',9,NULL,'gemini reason',NULL,'[]','gemini-x','2026-01-01',NULL)")
    c.commit()
    c.close()


def test_migration_moves_proficiently_fit_into_job_columns_and_deletes_fit(tmp_path):
    _old_db(tmp_path / "o.db")
    conn = db.connect(tmp_path / "o.db")
    j = db.get_job(conn, 1)
    assert (j["prof_score"], j["prof_reason"]) == (8, "prof reason")
    assert db.get_fit(conn, 1) is None
    conn.close()


def test_migration_keeps_real_gemini_fit(tmp_path):
    _old_db(tmp_path / "o.db")
    conn = db.connect(tmp_path / "o.db")
    f = db.get_fit(conn, 2)
    assert f.llm_score == 9 and f.model == "gemini-x"
    assert db.get_job(conn, 2)["prof_score"] is None
    conn.close()


def test_migration_renames_import_source_and_backfills_added_at(tmp_path):
    _old_db(tmp_path / "o.db")
    conn = db.connect(tmp_path / "o.db")
    a, b = db.get_job(conn, 1), db.get_job(conn, 2)
    assert a["source"] == "proficiently" and b["source"] == "ats"
    assert a["added_at"] == "2026-01-01T00:00:00+00:00"
    assert b["added_at"] == "2026-02-01T00:00:00+00:00"
    conn.close()


def test_migrations_are_idempotent(tmp_path):
    _old_db(tmp_path / "o.db")

    def snapshot(c):
        return (c.execute("SELECT * FROM jobs ORDER BY id").fetchall() and
                [tuple(r) for r in c.execute("SELECT * FROM jobs ORDER BY id")],
                [tuple(r) for r in c.execute("SELECT * FROM fits ORDER BY job_id")])

    c1 = db.connect(tmp_path / "o.db")
    s1 = snapshot(c1)
    c1.close()
    c2 = db.connect(tmp_path / "o.db")
    assert snapshot(c2) == s1
    assert len(s1[1]) == 1 and s1[0][0][list(db.get_job(c2, 1)).index("prof_score")] == 8
    c2.close()


def test_upsert_job_sets_added_at_on_insert_only(conn):
    jid, _ = db.upsert_job(conn, Job(company="A", title="t", url="https://a.test/x", source="ats"))
    first = db.get_job(conn, jid)["added_at"]
    assert first
    conn.execute("UPDATE jobs SET added_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", (jid,))
    db.upsert_job(conn, Job(company="A", title="t2", url="https://a.test/x", source="ats"))
    assert db.get_job(conn, jid)["added_at"] == "2020-01-01T00:00:00+00:00"


def test_set_job_status_records_source(conn):
    jid, _ = db.upsert_job(conn, Job(company="A", title="t", url="https://a.test/x", source="ats"))
    db.set_job_status(conn, jid, "applied", source="user")
    db.set_job_status(conn, jid, "screening")
    rows = [r["source"] for r in conn.execute("SELECT source FROM job_events ORDER BY id")]
    assert rows == ["user", None]


def test_list_jobs_orders_and_filters_by_gemini_then_prof_score(conn):
    def add(n, prof=None, gem=None):
        jid, _ = db.upsert_job(conn, Job(company=n, title="t", url=f"https://{n}.test/1", source="ats"))
        if prof is not None:
            conn.execute("UPDATE jobs SET prof_score = ?, prof_reason = 'r' WHERE id = ?", (prof, jid))
            conn.commit()
        if gem is not None:
            db.save_fit(conn, Fit(job_id=jid, keyword_score=0, llm_score=gem, graded_at="2026-01-01"))
        return jid

    low_prof = add("a", prof=4)
    both = add("b", prof=9, gem=6)   # the Gemini score wins
    high_prof = add("c", prof=7)
    none = add("d")
    assert [j["id"] for j in db.list_jobs(conn)][:3] == [high_prof, both, low_prof]
    assert [j["id"] for j in db.list_jobs(conn, min_score=6)] == [high_prof, both]
    assert db.list_jobs(conn, min_score=7)[0]["prof_reason"] == "r"
    assert none not in [j["id"] for j in db.list_jobs(conn, min_score=1)]


def test_runs_helpers_and_last_runs(conn):
    r1 = db.start_run(conn, "import", path="/x")
    db.finish_run(conn, r1, counts={"jobs": 2})
    r2 = db.start_run(conn, "import")
    db.finish_run(conn, r2, error="boom")
    r3 = db.start_run(conn, "scan")
    last = db.last_runs(conn)
    assert set(last) == {"import", "scan"}
    assert last["import"]["id"] == r2 and last["import"]["error"] == "boom"
    assert last["scan"]["finished_at"] is None
    first = db.last_runs(conn)["import"]
    assert first["finished_at"]
    today = datetime.now(timezone.utc).astimezone().date()
    assert [r["id"] for r in db.runs_on(conn, today)] == [r1, r2, r3]
    assert db.runs_on(conn, date(2000, 1, 1)) == []
    assert db.runs_on(conn, today)[0]["counts"] == {"jobs": 2}


def test_save_material_upserts_by_path(conn):
    jid, _ = db.upsert_job(conn, Job(company="A", title="t", url="https://a.test/x", source="ats"))
    db.save_material(conn, jid, "2026-01-01_a", "resume_pdf", "/p/a.pdf", "m1")
    db.save_material(conn, jid, "2026-01-01_a", "resume_pdf", "/p/a.pdf", "m2")
    db.save_material(conn, jid, "2026-03-01_a", "cover_letter", "/p/c.md", "m3")
    mats = db.list_materials(conn, jid)
    assert [m["path"] for m in mats] == ["/p/c.md", "/p/a.pdf"]  # newest folder first
    assert mats[1]["mtime"] == "m2"
    assert db.material_paths(conn) == {"/p/c.md", "/p/a.pdf"}


def test_web_status_change_logs_user_source(tmp_path):
    s = Settings(db_path=tmp_path / "w.db", proficiently_dir=tmp_path / "prof",
                 master_resume_path=tmp_path / "m.pdf", resume_output_dir=tmp_path / "out")
    c = db.connect(s.db_path)
    jid, _ = db.upsert_job(c, Job(company="A", title="t", url="https://a.test/x", source="ats"))
    c.close()
    client = TestClient(webapp.create_app(s), base_url="http://127.0.0.1")
    r = client.post(f"/jobs/{jid}/status", data={"status": "applied"}, follow_redirects=False)
    assert r.status_code == 303
    c = db.connect(s.db_path)
    try:
        assert c.execute("SELECT source FROM job_events").fetchall()[0][0] == "user"
    finally:
        c.close()
