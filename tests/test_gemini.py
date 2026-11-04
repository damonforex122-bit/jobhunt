import pytest

from jobhunt import db
from jobhunt.config import Settings
from jobhunt.fit import grade_job
from jobhunt.fit.gemini import FitResponse, Bullet, build_prompt, grade
from jobhunt.models import Job

NO_FAKE = "Do NOT invent fake experience—reframe my real work using their exact terminology."
FORMULA = "[Action Verb] + [Specific Task] + [Quantified Result]"
JOB = {"id": 1, "company": "AfterQuery", "title": "Applied Researcher",
       "description": "Python, Databricks, AWS and RAG experience required."}
RESUME = "python rag evals"
SECRET = "sekret-key-123"


class FakeModels:
    def __init__(self, parsed=None, exc=None):
        self.parsed, self.exc, self.kwargs = parsed, exc, None

    def generate_content(self, **kwargs):
        self.kwargs = kwargs
        if self.exc:
            raise self.exc
        return type("R", (), {"parsed": self.parsed})()


class FakeClient:
    def __init__(self, **kw):
        self.models = FakeModels(**kw)


def settings(key="k"):
    return Settings(gemini_api_key=key)


def resp(score=8):
    return FitResponse(
        match_pct=72, fit_score=score, missing_keywords=["databricks"],
        rewritten_bullets=[Bullet(original="o", rewritten="r", keywords_used=["aws"])],
        reasons="good", gaps="none",
    )


def test_build_prompt_contains_constraints():
    p = build_prompt(RESUME, "posting text", "AfterQuery", "Applied Researcher")
    assert NO_FAKE in p and FORMULA in p
    assert "building" in p and "funding" in p
    assert "AfterQuery" in p and "Applied Researcher" in p
    assert "8+" in p


def test_grade_success_with_fake_client():
    c = FakeClient(parsed=resp(8))
    fit = grade(JOB, RESUME, settings(), client=c)
    assert (fit.llm_score, fit.match_pct, fit.error) == (8, 72, None)
    assert fit.model == "gemini-2.5-flash"
    assert fit.keyword_score == 50
    assert fit.rewritten_bullets == [{"original": "o", "rewritten": "r", "keywords_used": ["aws"]}]
    assert c.models.kwargs["config"]["response_schema"] is FitResponse
    assert c.models.kwargs["model"] == "gemini-2.5-flash"


def test_grade_no_key():
    fit = grade(JOB, RESUME, settings(key=None))
    assert fit.error == "GEMINI_API_KEY not set"
    assert fit.llm_score is None and fit.keyword_score == 50


def test_grade_api_exception():
    c = FakeClient(exc=RuntimeError(f"boom {SECRET} " + "x" * 500))
    fit = grade(JOB, RESUME, settings(key=SECRET), client=c)
    assert fit.error and len(fit.error) <= 200 and SECRET not in fit.error
    assert fit.llm_score is None and fit.keyword_score == 50


def test_grade_parsed_none():
    fit = grade(JOB, RESUME, settings(), client=FakeClient(parsed=None))
    assert fit.error and fit.llm_score is None and fit.keyword_score == 50


def test_grade_no_posting_text():
    fit = grade({**JOB, "description": ""}, RESUME, settings(), client=FakeClient(parsed=resp()))
    assert fit.error == "No posting text" and fit.keyword_score == 0


@pytest.fixture
def job_id(conn):
    return db.upsert_job(conn, Job(company="AfterQuery", title="R", url="https://x.com/1",
                                   description=JOB["description"], source="manual"))[0]


@pytest.fixture
def s(tmp_path):
    (tmp_path / "profile.md").write_text(RESUME)
    return Settings(gemini_api_key="k", proficiently_dir=tmp_path)


@pytest.mark.parametrize("score,status", [(8, "ready"), (6, "low")])
def test_grade_job_sets_status(conn, job_id, s, score, status):
    fit = grade_job(conn, job_id, s, client=FakeClient(parsed=resp(score)))
    assert fit.llm_score == score
    assert db.get_job(conn, job_id)["status"] == status
    assert db.get_fit(conn, job_id).llm_score == score


def test_grade_job_keeps_applied_status(conn, job_id, s):
    db.set_job_status(conn, job_id, "applied")
    grade_job(conn, job_id, s, client=FakeClient(parsed=resp(9)))
    assert db.get_job(conn, job_id)["status"] == "applied"


def test_grade_job_error_saved_status_unchanged(conn, job_id, s):
    fit = grade_job(conn, job_id, s, client=FakeClient(parsed=None))
    assert fit.error
    assert db.get_fit(conn, job_id).error == fit.error
    assert db.get_job(conn, job_id)["status"] == "new"
