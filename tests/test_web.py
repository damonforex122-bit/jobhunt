import pytest
from fastapi.testclient import TestClient

from jobhunt import db
from jobhunt.config import Settings
from jobhunt.models import Company, Fit, Job, Person
from jobhunt.web import app as webapp


def tc(app):
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture
def settings(tmp_path):
    s = Settings(db_path=tmp_path / "web.db", proficiently_dir=tmp_path / "prof")
    conn = db.connect(s.db_path)
    a, _ = db.upsert_job(conn, Job(company="AfterQuery", title="Evals Lead", url="https://x.test/a",
                                   description="Build <b>evals</b> & benchmarks", source="ats",
                                   flags=["remote"]))
    b, _ = db.upsert_job(conn, Job(company="Acme", title="Researcher", url="https://x.test/b",
                                   description="Research", source="ats"))
    db.save_fit(conn, Fit(job_id=a, keyword_score=5, llm_score=9, match_pct=80,
                          missing_keywords=["rubrics"], reasons="good", gaps="none",
                          rewritten_bullets=[{"original": "old bullet", "rewritten": "new bullet"}],
                          graded_at="2026-01-01"))
    db.save_fit(conn, Fit(job_id=b, keyword_score=2, graded_at="2026-01-01",
                          error="GEMINI_API_KEY not set"))
    db.upsert_person(conn, Person(company="AfterQuery", job_id=a, name="Jane Do***e",
                                  title="Head of Data", source="apollo",
                                  source_url="https://apollo.test/p", confidence=0.7))
    db.upsert_company(conn, Company(name="Acme", ats="greenhouse", slug="acme",
                                    last_error="404 board not found"))
    conn.close()
    return s


@pytest.fixture
def client(settings):
    return tc(webapp.create_app(settings))


def test_index_lists_jobs_sorted_by_score(client):
    html = client.get("/").text
    assert html.index("Evals Lead") < html.index("Researcher")
    assert "remote" in html


def test_index_filter_min_score(client):
    html = client.get("/?min_score=8").text
    assert "Evals Lead" in html and "Researcher" not in html


def test_job_page_shows_not_graded_reason(client):
    html = client.get("/jobs/2").text
    assert "Not graded: GEMINI_API_KEY not set" in html


def test_job_page_shows_fit_people_and_escapes(client):
    html = client.get("/jobs/1").text
    assert "rubrics" in html and "old bullet" in html and "new bullet" in html
    assert "Jane Do***e" in html and "apollo" in html and "https://apollo.test/p" in html
    assert "&lt;b&gt;evals&lt;/b&gt;" in html and "<b>evals</b>" not in html


def test_post_fit_uses_injected_grader(settings):
    app = webapp.create_app(settings)

    def fake(conn, job_id, settings):
        f = Fit(job_id=job_id, keyword_score=1, llm_score=7, graded_at="x", reasons="FAKE-REASON")
        db.save_fit(conn, f)  # the real grade_job persists its result too
        return f

    app.dependency_overrides[webapp.get_grader] = lambda: fake
    r = tc(app).post("/jobs/2/fit")
    assert r.status_code == 200
    assert "FAKE-REASON" in r.text and "7" in r.text


def test_post_people_uses_injected_finder(settings):
    app = webapp.create_app(settings)
    called = []
    app.dependency_overrides[webapp.get_finder] = lambda: lambda conn, job_id, settings: called.append(job_id)
    r = tc(app).post("/jobs/1/people")
    assert r.status_code == 200 and called == [1]


def test_post_status_invalid_422(client):
    assert client.post("/jobs/1/status", data={"status": "bogus"}).status_code == 422


def test_post_status_valid_persists(client, settings):
    r = client.post("/jobs/1/status", data={"status": "applied"}, follow_redirects=False)
    assert r.status_code in (200, 303)
    conn = db.connect(settings.db_path)
    assert db.get_job(conn, 1)["status"] == "applied"
    conn.close()


def test_banner_shows_missing_keys(client):
    html = client.get("/").text
    assert "Gemini grading off" in html and "APOLLO_API_KEY" in html and "EXA_API_KEY" in html


def test_banner_hidden_when_keys_set(settings):
    s = settings.model_copy(update={"gemini_api_key": "k", "apollo_api_key": "k", "exa_api_key": "k"})
    assert "grading off" not in tc(webapp.create_app(s)).get("/").text


def test_companies_page_shows_last_error(client):
    html = client.get("/companies").text
    assert "404 board not found" in html and "Scan now" in html


def test_post_scan_redirects_with_flash(settings):
    from jobhunt.scan import ScanReport

    app = webapp.create_app(settings)
    app.dependency_overrides[webapp.get_scanner] = lambda: lambda conn, s: ScanReport(
        companies_scanned=3, jobs_seen=10, jobs_kept=4, jobs_new=2, errors={"Acme": "x"})
    c = tc(app)
    r = c.post("/scan", follow_redirects=True)
    assert "Scanned 3 companies" in r.text and "2 new" in r.text


def test_post_import_redirects_with_flash(settings):
    app = webapp.create_app(settings)
    app.dependency_overrides[webapp.get_importer] = lambda: lambda conn, d: {"jobs": 5}
    r = tc(app).post("/import", follow_redirects=True)
    assert "Import" in r.text and "jobs" in r.text


@pytest.mark.parametrize("bad", ["javascript:alert(1)", " JavaScript:x", "data:text/html,<script>x</script>",
                                 "java\tscript:alert(1)", "", None])
def test_safe_url_rejects(bad):
    assert webapp.safe_url(bad) == ""


def test_safe_url_accepts():
    assert webapp.safe_url(" https://example.com/a?b=1 ") == "https://example.com/a?b=1"
    assert webapp.safe_url("HTTP://example.com") == "HTTP://example.com"


def test_hostile_urls_not_rendered_in_href(settings):
    import re

    conn = db.connect(settings.db_path)
    db.upsert_person(conn, Person(company="AfterQuery", job_id=1, name="Evil Person", source="exa",
                                  source_url="javascript:alert(1)",
                                  linkedin_url="data:text/html,x", confidence=0.5))
    db.upsert_job(conn, Job(company="Evil", title="Bad", url="javascript:alert(2)", source="ats"))
    conn.close()
    c = tc(webapp.create_app(settings))
    for path in ("/jobs/1", "/jobs/3"):
        html = c.get(path).text
        assert "Evil Person" in html or path == "/jobs/3"
        assert not re.search(r'href="\s*(javascript|data):', html, re.I)


def test_foreign_origin_post_rejected(client):
    r = client.post("/jobs/1/status", data={"status": "applied"},
                    headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    r = client.post("/jobs/1/status", data={"status": "applied"},
                    headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    ok = client.post("/jobs/1/status", data={"status": "applied"}, follow_redirects=False,
                     headers={"Origin": "http://localhost:3000"})
    assert ok.status_code == 303


def test_foreign_host_rejected(settings):
    r = TestClient(webapp.create_app(settings), base_url="http://evil.example").get("/")
    assert r.status_code == 400


def test_post_people_shows_skipped_reasons(settings):
    from jobhunt.people.finder import FinderReport

    app = webapp.create_app(settings)
    app.dependency_overrides[webapp.get_finder] = lambda: lambda conn, job_id, s: FinderReport(
        people=[], skipped={"apollo": "no domain for company", "exa": "EXA_API_KEY not set"})
    html = tc(app).post("/jobs/1/people").text
    assert "Skipped apollo: no domain for company" in html
    assert "Skipped exa: EXA_API_KEY not set" in html


def test_post_scan_and_import_flash_predictable_errors(settings):
    app = webapp.create_app(settings)

    def bad_scan(conn, s):
        from pydantic import ValidationError

        from jobhunt.models import Company

        Company(name="A", ats="greehouse")

    def bad_import(conn, d):
        raise FileNotFoundError("job-history.md not found in /nope")

    app.dependency_overrides[webapp.get_scanner] = lambda: bad_scan
    app.dependency_overrides[webapp.get_importer] = lambda: bad_import
    c = tc(app)
    r = c.post("/scan", follow_redirects=True)
    assert r.status_code == 200 and "Scan failed" in r.text and "ats" in r.text
    r = c.post("/import", follow_redirects=True)
    assert r.status_code == 200 and "Import failed" in r.text and "job-history.md not found" in r.text


def test_imported_fit_shows_blank_keyword_pct(settings):
    conn = db.connect(settings.db_path)
    conn.execute("UPDATE fits SET model = 'proficiently-import', keyword_score = 0 WHERE job_id = 1")
    conn.commit()
    conn.close()
    html = tc(webapp.create_app(settings)).get("/").text
    row = html[html.index("Evals Lead"):html.index("Researcher")]
    assert "<td>0</td>" not in row


def test_post_status_with_note_shows_history(client):
    r = client.post("/jobs/1/status", data={"status": "interviewing", "note": "Onsite Friday"})
    assert r.status_code == 200
    assert "Onsite Friday" in r.text and "History" in r.text


def test_pipeline_board_lists_stage_columns_and_cards(client, settings):
    conn = db.connect(settings.db_path)
    db.set_job_status(conn, 1, "applied", note="via referral")
    db.set_job_status(conn, 2, "offer")
    conn.close()
    r = client.get("/pipeline")
    assert r.status_code == 200
    for stage in ("applied", "screening", "interviewing", "offer", "rejected"):
        assert stage in r.text
    assert "Evals Lead" in r.text and "via referral" in r.text and "Researcher" in r.text
    assert 'href="/pipeline"' in r.text


def test_companies_open_count_excludes_pipeline_jobs(client, settings):
    conn = db.connect(settings.db_path)
    db.set_job_status(conn, 2, "interviewing")
    conn.close()
    r = client.get("/companies")
    assert r.status_code == 200
    row = r.text.split("Acme", 1)[1].split("</tr>", 1)[0]
    assert ">0<" in row
