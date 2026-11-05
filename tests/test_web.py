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
    s = Settings(db_path=tmp_path / "web.db", proficiently_dir=tmp_path / "prof",
                 master_resume_path=tmp_path / "master.pdf", resume_output_dir=tmp_path / "out")
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


def _suggest(settings, job_id=1, stage="screening", subject="Next steps with AfterQuery"):
    conn = db.connect(settings.db_path)
    db.set_job_status(conn, job_id, "applied")
    db.save_gmail_message(conn, f"g{job_id}{stage}", job_id=job_id, sender="r@afterquery.com",
                          subject=subject, received="2026-11-04", snippet="phone screen",
                          suggested_status=stage)
    sid = db.list_gmail_suggestions(conn)[0]["id"]
    conn.close()
    return sid


def test_pipeline_shows_gmail_suggestions_and_accept_moves_job(client, settings):
    sid = _suggest(settings)
    page = client.get("/pipeline").text
    assert "Suggested from Gmail" in page and "Next steps with AfterQuery" in page
    assert f"/gmail/suggestions/{sid}/accept" in page
    r = client.post(f"/gmail/suggestions/{sid}/accept", follow_redirects=False)
    assert r.status_code == 303
    conn = db.connect(settings.db_path)
    assert db.get_job(conn, 1)["status"] == "screening"
    assert db.list_gmail_suggestions(conn) == []
    conn.close()


def test_dismiss_keeps_stage(client, settings):
    sid = _suggest(settings)
    client.post(f"/gmail/suggestions/{sid}/dismiss")
    conn = db.connect(settings.db_path)
    assert db.get_job(conn, 1)["status"] == "applied" and db.list_gmail_suggestions(conn) == []
    conn.close()


def test_unknown_suggestion_404(client):
    assert client.post("/gmail/suggestions/999/accept").status_code == 404


def test_gmail_sync_not_connected_flashes_setup_hint(client):
    r = client.post("/gmail/sync")
    assert r.status_code == 200 and "gmail-auth" in r.text


def test_gmail_sync_reports_counts(settings):
    from jobhunt.gmail.sync import SyncReport

    app = webapp.create_app(settings)
    app.dependency_overrides[webapp.get_gmail_sync] = lambda: lambda conn, s: SyncReport(7, 2)
    r = tc(app).post("/gmail/sync")
    assert "7 new emails" in r.text and "2 suggested" in r.text


def test_pipeline_shows_new_company_suggestion(client, settings):
    conn = db.connect(settings.db_path)
    db.save_gmail_message(conn, "n1", sender="Robyn <r@hippocraticai.com>",
                          subject="Interview with Hippocratic AI", received="2026-11-03",
                          snippet="", suggested_status="interviewing", company="Hippocratic AI")
    conn.close()
    page = client.get("/pipeline").text
    assert "Hippocratic AI" in page and "not tracked" in page and "/jobs/None" not in page


def _tailor_settings(settings, tmp_path, key="k"):
    return settings.model_copy(update={"gemini_api_key": key,
                                       "resume_output_dir": tmp_path / "out"})


def test_tailor_button_calls_tailor_and_renders_panel(settings, tmp_path):
    s = _tailor_settings(settings, tmp_path)
    app = webapp.create_app(s)

    def fake(conn, job_id, settings):
        db.save_resume(conn, {"job_id": job_id, "path": str(tmp_path / "out" / "r.pdf"),
                              "gaps": ["no Rust experience"], "fallbacks": ["Summary line"]})

    app.dependency_overrides[webapp.get_tailor] = lambda: fake
    r = tc(app).post("/jobs/1/resume")
    assert r.status_code == 200
    assert "Open PDF" in r.text and "/jobs/1/resume.pdf" in r.text
    assert "no Rust experience" in r.text and "Kept your original wording" in r.text
    assert "Summary line" in r.text and str(tmp_path / "out") in r.text


def test_resume_pdf_route_serves_only_recorded_file(settings, tmp_path):
    s = _tailor_settings(settings, tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    pdf = out / "r.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    outside = tmp_path / "secret.pdf"
    outside.write_bytes(b"%PDF-1.4 secret")
    conn = db.connect(s.db_path)
    db.save_resume(conn, {"job_id": 1, "path": str(pdf)})
    db.save_resume(conn, {"job_id": 2, "path": str(outside)})
    conn.close()
    c = tc(webapp.create_app(s))
    r = c.get("/jobs/1/resume.pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")
    assert c.get("/jobs/2/resume.pdf").status_code == 404  # outside the output dir
    assert c.get("/jobs/999/resume.pdf").status_code == 404  # no record
    pdf.unlink()
    assert c.get("/jobs/1/resume.pdf").status_code == 404  # file deleted


def test_tailor_disabled_without_key(client):
    html = client.get("/jobs/1").text
    assert "Set a Gemini, Claude or OpenRouter key to tailor" in html and "disabled" in html
    assert 'hx-post="/jobs/1/resume"' not in html


def test_tailor_button_enabled_and_no_posting_text(settings, tmp_path):
    s = _tailor_settings(settings, tmp_path)
    conn = db.connect(s.db_path)
    conn.execute("UPDATE jobs SET description = '' WHERE id = 2")
    conn.commit()
    conn.close()
    c = tc(webapp.create_app(s))
    ok = c.get("/jobs/1").text
    assert 'hx-post="/jobs/1/resume"' in ok and "Tailoring" in ok
    assert "No posting text" in c.get("/jobs/2").text


def test_tailor_error_shown(settings, tmp_path):
    s = _tailor_settings(settings, tmp_path)
    app = webapp.create_app(s)

    def fake(conn, job_id, settings):
        db.save_resume(conn, {"job_id": job_id, "error": "model said <no>"})

    app.dependency_overrides[webapp.get_tailor] = lambda: fake
    r = tc(app).post("/jobs/1/resume")
    assert '<p class="text-error">Not tailored: model said &lt;no&gt;</p>' in r.text
    assert "Open PDF" not in r.text


def test_base_uses_daisyui_light_theme(client):
    import re
    html = client.get("/").text
    assert 'data-theme="light"' in html
    assert 'content="light only"' in html
    assert re.search(r"daisyui@5\.\d+\.\d+", html)
    assert re.search(r"@tailwindcss/browser@4\.\d+\.\d+", html)


def test_nav_links(client):
    html = client.get("/").text
    assert 'href="/interviews"' in html and 'href="/profile"' in html


def test_resume_panel_notes_older_master(settings, tmp_path):
    s = _tailor_settings(settings, tmp_path)
    s.master_resume_path.parent.mkdir(parents=True, exist_ok=True)
    s.master_resume_path.write_bytes(b"%PDF current master")
    conn = db.connect(s.db_path)
    db.save_resume(conn, {"job_id": 1, "path": str(tmp_path / "out" / "r.pdf"),
                          "master_sha256": "0" * 64})
    conn.close()
    assert "Made from an older master" in tc(webapp.create_app(s)).get("/jobs/1").text
    conn = db.connect(s.db_path)
    from jobhunt.tailor.master import master_sha256
    db.save_resume(conn, {"job_id": 1, "path": str(tmp_path / "out" / "r.pdf"),
                          "master_sha256": master_sha256(s.master_resume_path)})
    conn.close()
    assert "Made from an older master" not in tc(webapp.create_app(s)).get("/jobs/1").text
    s.master_resume_path.unlink()
    conn = db.connect(s.db_path)
    db.save_resume(conn, {"job_id": 1, "path": str(tmp_path / "out" / "r.pdf"),
                          "master_sha256": "0" * 64})
    conn.close()
    assert "Made from an older master" not in tc(webapp.create_app(s)).get("/jobs/1").text
