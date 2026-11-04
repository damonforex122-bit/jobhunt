from pathlib import Path

from jobhunt import db
from jobhunt.importer import parse_job_dir, parse_job_history, run_import

FIX = Path(__file__).parent / "fixtures" / "proficiently"
AQ_DIR = FIX / "jobs" / "afterquery-research-scientist-frontier-evaluations-2026-10-30"


def test_parse_job_history_both_table_shapes():
    imported = parse_job_history((FIX / "job-history.md").read_text())
    by_title = {i.job.title: i for i in imported}
    seven_col = [i for i in imported if i.job.company in {"Exa", "Variance", "Applied Compute"}]
    phd_col = [i for i in imported if i.job.company in {"AfterQuery", "Snorkel AI", "Mercor"}]
    assert len(seven_col) >= 2 and len(phd_col) >= 2

    exa = by_title["Research, Evals"]
    assert (exa.score, exa.job.status, exa.job.source) == (8, "needs", "import")
    assert exa.job.first_seen == "2026-10-30"
    assert exa.reason and exa.reason.startswith("Strong overlap")

    aq = by_title["Research Scientist - Frontier Evaluations"]
    assert (aq.score, aq.job.status) == (8, "ready")
    assert aq.reason and aq.reason.startswith("Strong overlap")  # not the PhD column


def test_parse_job_history_skips_rows_without_url():
    text = (
        "| Company | Role | Score | Job URL | Date | Status | Reason |\n"
        "|---|---|---|---|---|---|---|\n"
        "| A | Role A | 7 | | 2026-01-01 | low | no url |\n"
        "| B | Role B | x | https://jobs.lever.co/b/1 | 2026-01-01 | weird | ok |\n"
    )
    imported = parse_job_history(text)
    assert [i.job.company for i in imported] == ["B"]
    assert imported[0].score is None and imported[0].job.status == "weird"


def test_parse_people_md():
    posting, people = parse_job_dir(AQ_DIR)
    assert posting
    assert len(people) == 1
    p = people[0]
    assert p.name == "Spencer Mateega"
    assert p.source_url == "https://yespress.io/spencer-mateega.md"
    assert (p.source, p.confidence) == ("exa", 0.5)
    assert p.title.startswith("Co-founder")


def test_parse_job_dir_missing_posting(tmp_path):
    assert parse_job_dir(tmp_path) == (None, [])


def test_run_import_idempotent(conn):
    first = run_import(conn, FIX)
    assert first["jobs"] >= 4 and first["new"] == first["jobs"]
    count = len(db.list_jobs(conn))
    second = run_import(conn, FIX)
    assert second["new"] == 0
    assert len(db.list_jobs(conn)) == count

    aq = next(c for c in db.list_companies(conn) if c.name == "AfterQuery")
    assert (aq.ats, aq.slug) == ("ashby", "AfterQuery")
    job = next(j for j in db.list_jobs(conn) if j["company"] == "AfterQuery")
    assert job["description"]
    fit = db.get_fit(conn, job["id"])
    assert fit.llm_score == 8 and fit.keyword_score == 0 and fit.model == "proficiently-import"
    assert [p.name for p in db.list_people(conn, "AfterQuery")] == ["Spencer Mateega"]


def test_run_import_never_writes(conn):
    def snap():
        return sorted(
            (str(p), p.stat().st_mtime_ns, p.stat().st_size)
            for p in FIX.rglob("*") if p.is_file()
        )
    before = snap()
    run_import(conn, FIX)
    assert snap() == before


def test_reimport_preserves_later_fit_and_description(conn):
    from jobhunt.models import Fit

    run_import(conn, FIX)
    job = next(j for j in db.list_jobs(conn) if j["company"] == "AfterQuery")
    db.save_fit(conn, Fit(job_id=job["id"], keyword_score=42, llm_score=3,
                          model="gemini-2.5-flash", graded_at="2026-11-05T00:00:00"))
    conn.execute("UPDATE jobs SET description = ? WHERE id = ?", ("scanned text", job["id"]))
    conn.commit()
    run_import(conn, FIX)
    fit = db.get_fit(conn, job["id"])
    assert (fit.model, fit.keyword_score, fit.llm_score) == ("gemini-2.5-flash", 42, 3)
    assert db.get_job(conn, job["id"])["description"] == "scanned text"


def test_job_dir_match_requires_exact_date_suffix(conn, tmp_path):
    (tmp_path / "job-history.md").write_text(
        "| Company | Role | Score | Job URL | Date | Status | Reason |\n|---|---|---|---|---|---|---|\n"
        "| Snorkel AI | Research Scientist | 8 | https://job-boards.greenhouse.io/snorkelai/jobs/1 | 2026-10-30 | ready | r |\n"
    )
    jobs = tmp_path / "jobs"
    for name, text in [
        ("snorkel-ai-research-scientist-human-ai-systems-2026-10-30", "WRONG"),
        ("snorkel-ai-research-scientist-2026-10-30", "OLD"),
        ("snorkel-ai-research-scientist-2026-11-02", "NEW"),
    ]:
        (jobs / name).mkdir(parents=True)
        (jobs / name / "posting.md").write_text(text)
    run_import(conn, tmp_path)
    assert db.list_jobs(conn)[0]["description"] == "NEW"


def test_run_import_missing_dir_or_history_raises(conn, tmp_path):
    import pytest

    from jobhunt.importer import ImportSourceError

    with pytest.raises(ImportSourceError, match="nope"):
        run_import(conn, tmp_path / "nope")
    (tmp_path / "empty").mkdir()
    with pytest.raises(ImportSourceError, match="job-history.md"):
        run_import(conn, tmp_path / "empty")


def test_cli_import_missing_dir_prints_error(tmp_path, monkeypatch, capsys):
    from jobhunt.cli import main

    monkeypatch.chdir(tmp_path)
    assert main(["import", "--dir", str(tmp_path / "nope")]) == 1
    out = capsys.readouterr().out
    assert out.startswith("error:") and "nope" in out and "Traceback" not in out
