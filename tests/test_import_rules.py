import json

from jobhunt import db
from jobhunt.importer import map_status, run_import
from jobhunt.models import Fit
from prof_fixture import make_proficiently

H = "https://jobs.lever.co/acme/"


def row(n, status="ready", score=7, **kw):
    d = {"Company": f"Acme{n}", "Role": f"Role {n}", "Score": score, "Job URL": f"{H}{n}",
         "Date": "2026-10-30", "Status": status, "Reason": f"reason {n}"}
    d.update(kw)
    return d


def jid(conn, n):
    return conn.execute("SELECT id FROM jobs WHERE url LIKE ?", (f"%/acme/{n}",)).fetchone()["id"]


def status(conn, n):
    return conn.execute("SELECT status FROM jobs WHERE url LIKE ?", (f"%/acme/{n}",)).fetchone()["status"]


def snapshot(conn):
    return {
        t: [tuple(r) for r in conn.execute(f"SELECT * FROM {q}").fetchall()]
        for t, q in [("jobs", "jobs ORDER BY id"), ("job_events", "job_events ORDER BY id"),
                     ("people", "people ORDER BY id"), ("fits", "fits ORDER BY job_id")]
    }


def test_map_status():
    assert map_status(" Ready ") == ("triage", "ready")
    assert map_status("NEEDS") == ("triage", "needs")
    assert map_status("low") == ("triage", "low")
    assert map_status("Applied") == ("forward", "applied")
    assert map_status("offer") == ("forward", "offer")
    assert map_status("pass") is None and map_status("") is None


def test_import_twice_is_identical(conn, tmp_path):
    root = make_proficiently(
        tmp_path / "p",
        history_rows=[row(1), row(2, "applied")],
        folders={"acme1-role-1-2026-10-30": {
            "posting.md": "text",
            "people.md": "- **Jo Doe**, CTO: https://example.com/jo\n"}},
    )
    first = run_import(conn, root)
    snap1 = snapshot(conn)
    second = run_import(conn, root)
    assert first["new"] == 2 and first["moved"] == 2
    assert second["new"] == 0 and second["moved"] == 0
    assert snapshot(conn) == snap1


def test_triage_only_applies_to_new(conn, tmp_path):
    root = make_proficiently(tmp_path / "p", history_rows=[row(1, "ready")])
    run_import(conn, root)
    db.set_job_status(conn, jid(conn, 1), "applied")
    run_import(conn, root)
    assert status(conn, 1) == "applied"


def test_forward_move_from_triage_logs_event_with_source(conn, tmp_path):
    root = make_proficiently(tmp_path / "p", history_rows=[row(1, "ready")])
    run_import(conn, root)
    make_proficiently(tmp_path / "p", history_rows=[row(1, "interviewing")])
    c = run_import(conn, tmp_path / "p")
    assert status(conn, 1) == "interviewing" and c["moved"] == 1
    ev = conn.execute("SELECT * FROM job_events WHERE job_id = ? ORDER BY id DESC",
                      (jid(conn, 1),)).fetchone()
    assert (ev["status"], ev["note"], ev["source"]) == (
        "interviewing", "From Proficiently (2026-10-30)", "proficiently")


def test_forward_move_never_touches_skip_or_later_stage(conn, tmp_path):
    root = make_proficiently(tmp_path / "p", history_rows=[row(1, "ready"), row(2, "ready")])
    run_import(conn, root)
    db.set_job_status(conn, jid(conn, 1), "skip")
    db.set_job_status(conn, jid(conn, 2), "interviewing")
    make_proficiently(tmp_path / "p", history_rows=[row(1, "applied"), row(2, "applied")])
    c = run_import(conn, tmp_path / "p")
    assert (status(conn, 1), status(conn, 2)) == ("skip", "interviewing")
    assert c["moved"] == 0


def test_unknown_status_counted_not_applied(conn, tmp_path):
    root = make_proficiently(tmp_path / "p", history_rows=[row(1, "Pass"), row(2, "Rejected"), row(3, "")])
    c = run_import(conn, root)
    assert c["unknown_status"] == 2 and c["moved"] == 0
    assert [status(conn, n) for n in (1, 2, 3)] == ["new"] * 3


def test_reads_history_add_and_varied_columns(conn, tmp_path):
    root = make_proficiently(
        tmp_path / "p",
        history_rows=[row(1, "ready")],
        extra_history_rows=[row(2, "low", Source="exa", PhD="yes"), row(1, "needs", score=9)],
    )
    c = run_import(conn, root)
    assert c["jobs"] == 2
    assert status(conn, 2) == "low"
    assert status(conn, 1) == "needs"  # later row (_history-add.md) wins
    assert db.get_job(conn, jid(conn, 1))["prof_score"] == 9


def test_posting_only_fills_empty_description(conn, tmp_path):
    root = make_proficiently(tmp_path / "p", history_rows=[row(1), row(2)],
                             folders={"acme1-role-1-2026-10-30": {"posting.md": "from posting"},
                                      "acme2-role-2-2026-10-30": {"posting.md": "from posting"}})
    run_import(conn, root)
    conn.execute("UPDATE jobs SET description = 'mine' WHERE id = ?", (jid(conn, 1),))
    conn.execute("UPDATE jobs SET description = '' WHERE id = ?", (jid(conn, 2),))
    conn.commit()
    run_import(conn, root)
    assert db.get_job(conn, jid(conn, 1))["description"] == "mine"
    assert db.get_job(conn, jid(conn, 2))["description"] == "from posting"


def test_prof_score_stored_and_gemini_fit_untouched(conn, tmp_path):
    root = make_proficiently(tmp_path / "p", history_rows=[row(1, score=8)])
    run_import(conn, root)
    i = jid(conn, 1)
    db.save_fit(conn, Fit(job_id=i, keyword_score=42, llm_score=3,
                          model="gemini-2.5-flash", graded_at="2026-11-05T00:00:00"))
    make_proficiently(tmp_path / "p", history_rows=[row(1, score=5)])
    run_import(conn, tmp_path / "p")
    j = db.get_job(conn, i)
    assert (j["prof_score"], j["prof_reason"]) == (5, "reason 1")
    fit = db.get_fit(conn, i)
    assert (fit.model, fit.llm_score, fit.keyword_score) == ("gemini-2.5-flash", 3, 42)
    assert conn.execute("SELECT COUNT(*) FROM fits WHERE model = 'proficiently-import'").fetchone()[0] == 0


def test_import_writes_run_row(conn, tmp_path):
    root = make_proficiently(tmp_path / "p", history_rows=[row(1)], careers={"x": 1})
    c = run_import(conn, root)
    r = conn.execute("SELECT * FROM runs WHERE kind = 'import'").fetchall()
    assert len(r) == 1 and r[0]["finished_at"] and r[0]["error"] is None
    assert json.loads(r[0]["counts_json"]) == c
    assert r[0]["path"] == str(root)
