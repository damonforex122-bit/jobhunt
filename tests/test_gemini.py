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
        rewritten_bullets=[Bullet(original=RESUME, rewritten="r", keywords_used=["aws"])],
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
    assert fit.model == "gemini-3.5-flash-lite"
    assert fit.keyword_score == 50
    assert fit.rewritten_bullets == [{"original": RESUME, "rewritten": "r", "keywords_used": ["aws"]}]
    assert c.models.kwargs["config"]["response_schema"] is FitResponse
    assert c.models.kwargs["model"] == "gemini-3.5-flash-lite"


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


def test_response_schema_has_no_defaults_or_max_items():
    import json

    schema = json.dumps(FitResponse.model_json_schema())
    assert '"default"' not in schema and "maxItems" not in schema


REAL = "Cut analysis time from 5 hours to 30 minutes across 1,000 transcripts."


def bullets_resp(*bullets):
    return FitResponse(match_pct=70, fit_score=8, missing_keywords=["calibration"],
                       rewritten_bullets=list(bullets), reasons="ok", gaps="No calibration work.")


def test_prompt_forbids_unsupported_keywords_and_new_numbers():
    p = build_prompt(RESUME, "posting", "Co", "Role")
    assert "verbatim" in p
    assert "belongs in gaps" in p
    assert "no new numbers" in p


def test_grade_keeps_bullet_grounded_in_resume():
    b = Bullet(original=REAL, rewritten="Reduced eval analysis time from 5 hours to 30 minutes "
               "across 1,000 transcripts.", keywords_used=["eval"])
    fit = grade(JOB, "Intro.\n" + REAL + "\nMore.", settings(), client=FakeClient(parsed=bullets_resp(b)))
    assert len(fit.rewritten_bullets) == 1
    assert fit.gaps == "No calibration work."


def test_grade_drops_bullet_whose_original_is_not_in_resume():
    b = Bullet(original="Built fraud risk workflows for banks.", rewritten="Built risk workflows.",
               keywords_used=["risk workflows"])
    fit = grade(JOB, REAL, settings(), client=FakeClient(parsed=bullets_resp(b)))
    assert fit.rewritten_bullets == []
    assert "1 rewritten bullet" in fit.gaps and "not in your resume" in fit.gaps


def test_grade_drops_bullet_that_adds_numbers():
    b = Bullet(original=REAL, rewritten="Measured calibration across 2,000 outputs, cutting time "
               "from 5 hours to 30 minutes across 1,000 transcripts.", keywords_used=["calibration"])
    fit = grade(JOB, REAL, settings(), client=FakeClient(parsed=bullets_resp(b)))
    assert fit.rewritten_bullets == []
    assert "2,000" in fit.gaps
