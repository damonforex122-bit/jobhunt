import pytest
from master_layout import render_master_layout
from tailor_fakes import sample_resume

from jobhunt import db
from jobhunt.config import Settings
from jobhunt.fit import grade_job
from jobhunt.models import Fit, Job


@pytest.fixture
def setup(conn, tmp_path):
    master = tmp_path / "master.pdf"
    render_master_layout(sample_resume(), master)
    jid, _ = db.upsert_job(conn, Job(company="A", title="T", url="https://x.test/1",
                                     source="import", description="Need evals and rubrics"))
    db.save_fit(conn, Fit(job_id=jid, keyword_score=0, llm_score=8, reasons="imported",
                          model="proficiently-import", graded_at="2026-01-01"))
    return jid, Settings(master_resume_path=master, db_path=tmp_path / "t.db")


class Boom:
    class models:
        @staticmethod
        def generate_content(**kw):
            raise RuntimeError("api down")


def _check(conn, jid):
    f = db.get_fit(conn, jid)
    assert f.llm_score == 8 and f.reasons == "imported"
    assert f.model == "proficiently-import" and f.graded_at == "2026-01-01"
    assert f.error
    return f


def test_no_key_keeps_imported_score(conn, setup):
    jid, s = setup
    grade_job(conn, jid, s)
    f = _check(conn, jid)
    assert "GEMINI_API_KEY" in f.error
    assert f.keyword_score > 0 or f.missing_keywords is not None


def test_api_error_keeps_imported_score(conn, setup):
    jid, s = setup
    grade_job(conn, jid, s.model_copy(update={"gemini_api_key": "k"}), client=Boom())
    assert "api down" in _check(conn, jid).error


def test_no_posting_keeps_imported_score(conn, setup):
    jid, s = setup
    conn.execute("UPDATE jobs SET description = '' WHERE id = ?", (jid,))
    conn.commit()
    grade_job(conn, jid, s)
    assert "No posting text" in _check(conn, jid).error
