import pytest

from jobhunt import cli, db, pipeline
from jobhunt.config import Settings
from jobhunt.models import Job


def add_job(conn, n=1, company="Acme"):
    return db.upsert_job(conn, Job(company=company, title=f"Evals {n}", url=f"https://x.test/{n}",
                                   source="ats"))[0]


def test_stages_split_triage_and_pipeline():
    assert pipeline.PIPELINE == ("applied", "screening", "interviewing", "offer")
    assert pipeline.CLOSED == ("accepted", "rejected", "withdrawn")
    assert set(pipeline.STATUSES) == set(pipeline.TRIAGE + pipeline.PIPELINE + pipeline.CLOSED)
    assert "skip" in pipeline.TRIAGE and "new" in pipeline.TRIAGE


def test_status_change_records_event_with_note(conn):
    j = add_job(conn)
    db.set_job_status(conn, j, "applied", note="Referred by Sam")
    db.set_job_status(conn, j, "screening")
    events = db.list_events(conn, j)
    assert [(e["status"], e["note"]) for e in events] == [("applied", "Referred by Sam"),
                                                          ("screening", None)]
    assert all(e["at"] for e in events)
    assert db.get_job(conn, j)["status"] == "screening"


def test_same_status_without_note_records_nothing(conn):
    j = add_job(conn)
    db.set_job_status(conn, j, "applied")
    db.set_job_status(conn, j, "applied")
    assert len(db.list_events(conn, j)) == 1


def test_same_status_with_note_records_note(conn):
    j = add_job(conn)
    db.set_job_status(conn, j, "interviewing")
    db.set_job_status(conn, j, "interviewing", note="Onsite on Friday")
    assert [e["note"] for e in db.list_events(conn, j)] == [None, "Onsite on Friday"]


def test_board_groups_pipeline_jobs_with_stage_date_and_last_note(conn):
    a, b, c = add_job(conn, 1), add_job(conn, 2, "Beta"), add_job(conn, 3)
    db.set_job_status(conn, a, "applied", note="via site")
    db.set_job_status(conn, b, "applied")
    db.set_job_status(conn, b, "interviewing", note="Panel next week")
    db.set_job_status(conn, c, "ready")
    board = db.pipeline_board(conn)
    assert list(board) == list(pipeline.PIPELINE + pipeline.CLOSED)
    assert [j["id"] for j in board["applied"]] == [a]
    card = board["interviewing"][0]
    assert card["id"] == b and card["last_note"] == "Panel next week" and card["stage_since"]
    assert card["applied_at"]
    assert all(c not in [j["id"] for j in col] for col in board.values())


def test_cli_status_and_pipeline(tmp_path, monkeypatch, capsys):
    s = Settings(db_path=tmp_path / "c.db", proficiently_dir=tmp_path)
    monkeypatch.setattr(pipeline, "load_settings", lambda: s)
    conn = db.connect(s.db_path)
    j = add_job(conn)
    conn.close()
    assert cli.main(["status", str(j), "applied", "--note", "Sent resume"]) == 0
    assert cli.main(["pipeline"]) == 0
    out = capsys.readouterr().out
    assert "applied" in out and "Acme" in out and "Sent resume" in out


def test_cli_status_rejects_unknown_stage(tmp_path, monkeypatch):
    s = Settings(db_path=tmp_path / "c.db", proficiently_dir=tmp_path)
    monkeypatch.setattr(pipeline, "load_settings", lambda: s)
    with pytest.raises(SystemExit):
        cli.main(["status", "1", "bogus"])


def test_cli_status_unknown_job(tmp_path, monkeypatch, capsys):
    s = Settings(db_path=tmp_path / "c.db", proficiently_dir=tmp_path)
    monkeypatch.setattr(pipeline, "load_settings", lambda: s)
    assert cli.main(["status", "99", "applied"]) == 1
    assert "not found" in capsys.readouterr().err


def test_note_does_not_reset_stage_since_but_new_stage_does(conn, monkeypatch):
    times = iter(["2026-10-30T00:00:00+00:00",  # first_seen, set by upsert_job
                  "2026-11-01T00:00:00+00:00", "2026-11-03T00:00:00+00:00",
                  "2026-11-05T00:00:00+00:00"])
    monkeypatch.setattr(db, "_now", lambda: next(times))
    j = add_job(conn)
    db.set_job_status(conn, j, "applied")
    db.set_job_status(conn, j, "applied", note="Followed up")
    assert db.pipeline_board(conn)["applied"][0]["stage_since"].startswith("2026-11-01")
    db.set_job_status(conn, j, "screening")
    card = db.pipeline_board(conn)["screening"][0]
    assert card["stage_since"].startswith("2026-11-05") and card["applied_at"].startswith("2026-11-01")
    assert card["last_note"] == "Followed up"
