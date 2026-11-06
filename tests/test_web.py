import re

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
    assert "Grading is off — add a Gemini key on the Profile page" in html
    assert "People search is off — add an Apollo key on the Profile page (optional)" in html
    assert "People search is off — add an Exa key on the Profile page (optional)" in html
    assert "set GEMINI_API_KEY" not in html and "set APOLLO_API_KEY" not in html and "set EXA_API_KEY" not in html


def test_banner_hidden_when_keys_set(settings):
    s = settings.model_copy(update={"gemini_api_key": "k", "apollo_api_key": "k", "exa_api_key": "k"})
    assert "is off" not in tc(webapp.create_app(s)).get("/").text


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
    assert r.status_code == 200 and "Connect Gmail on the Setup page" in r.text


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


def test_resume_panel_notes_older_uploaded_master(settings, tmp_path):
    from jobhunt import profile_store
    s = _tailor_settings(settings, tmp_path)
    up = tmp_path / "up.pdf"
    up.write_bytes(b"%PDF uploaded master")
    conn = db.connect(s.db_path)
    profile_store.save_master(conn, profile_store.MasterInfo(
        path=str(up), sha256="x", name="up.pdf", uploaded_at="2026-01-01"))
    db.save_resume(conn, {"job_id": 1, "path": str(tmp_path / "out" / "r.pdf"),
                          "master_sha256": "0" * 64})
    conn.close()
    assert "Made from an older master" in tc(webapp.create_app(s)).get("/jobs/1").text


def test_gmail_sync_interview_failure_keeps_suggestion_flash(settings):
    from jobhunt.gmail.sync import SyncReport

    def boom(conn, s):
        raise RuntimeError("calendar broke")

    app = webapp.create_app(settings)
    app.dependency_overrides[webapp.get_gmail_sync] = lambda: lambda conn, s: SyncReport(7, 2)
    app.dependency_overrides[webapp.get_interview_sync] = lambda: boom
    r = tc(app).post("/gmail/sync")
    assert "7 new emails" in r.text and "couldn" in r.text and "read interviews" in r.text


def test_gmail_sync_reports_interviews_updated(settings):
    from jobhunt.gmail.sync import SyncReport

    app = webapp.create_app(settings)
    app.dependency_overrides[webapp.get_gmail_sync] = lambda: lambda conn, s: SyncReport(1, 0)
    app.dependency_overrides[webapp.get_interview_sync] = lambda: lambda conn, s: 3
    assert "3 interviews updated" in tc(app).post("/gmail/sync").text


# --- add a job by link ---------------------------------------------------------

def _link_app(settings, adder=None, tailored=None):
    import httpx
    from tailor_fakes import FakeGemini

    from jobhunt.sources import by_link
    from jobhunt.tailor import TailorResult
    from jobhunt.web import review

    review.RUNNING.clear()
    desc = ("We are hiring an engineer to build evaluation pipelines for language models and ship "
            "dashboards weekly, working with researchers on datasets and Python services every day.")
    fake = FakeGemini({by_link.ExtractedPosting: by_link.ExtractedPosting(
        company="Example Co", title="Evals Engineer", description=desc)})

    def handler(req):
        if req.url.host == "boards-api.greenhouse.io":
            return httpx.Response(200, json={"title": "AI Eng", "company_name": "Dscout",
                                             "content": "&lt;p&gt;Do things&lt;/p&gt;"})
        return httpx.Response(200, text="<html><body>Sign in to continue</body></html>")

    def add(conn, url, settings, text=None):
        return by_link.add_job_by_link(conn, url, settings, text=text, client=fake,
                                       http=httpx.Client(transport=httpx.MockTransport(handler)))

    def fake_tailor(conn, job_id, settings, progress=None):
        (tailored if tailored is not None else []).append(job_id)
        db.save_resume(conn, {"job_id": job_id, "path": "x.pdf"})
        return TailorResult(job_id=job_id, path="x.pdf")

    app = webapp.create_app(settings)
    app.dependency_overrides[webapp.get_link_adder] = lambda: adder or add
    app.dependency_overrides[review.get_runner] = lambda: (lambda fn: fn())
    app.dependency_overrides[review.get_review_tailor] = lambda: fake_tailor
    return tc(app), desc


def test_jobs_page_has_add_form(client):
    html = client.get("/").text
    assert 'action="/jobs/add"' in html and "Add a job by link" in html
    assert "Paste the posting text" not in html


def test_web_add_redirects_to_review_and_starts_tailoring(settings, monkeypatch):
    monkeypatch.setattr("jobhunt.sources.by_link._resolve", lambda h: ["93.184.216.34"])
    tailored = []
    c, _ = _link_app(settings, tailored=tailored)
    r = c.post("/jobs/add", data={"url": "https://job-boards.greenhouse.io/dscout/jobs/4472409009"},
               follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.endswith("/review?start=1")
    page = c.get(loc)
    assert page.status_code == 200 and tailored == [int(loc.split("/")[2])]
    assert "Tailored your resume." in page.text and "Tailor this resume" in page.text
    c.get(loc)  # reloading with a resume in place doesn't tailor again
    assert len(tailored) == 1
    assert "Dscout" in c.get("/").text


def test_review_start_skips_a_running_job(settings):
    from jobhunt.web import review
    c, _ = _link_app(settings)
    review.RUNNING[1] = {"instruction": "x", "progress": []}
    try:
        assert c.get("/jobs/1/review?start=1").status_code == 200
    finally:
        review.RUNNING.clear()


def test_web_add_needs_text_shows_textarea_then_accepts_text(settings, monkeypatch):
    monkeypatch.setattr("jobhunt.sources.by_link._resolve", lambda h: ["93.184.216.34"])
    c, desc = _link_app(settings)
    url = "https://careers.example.test/jobs/7"
    r = c.post("/jobs/add", data={"url": url}, follow_redirects=False)
    assert r.status_code == 200
    assert "Paste the posting text" in r.text and f'value="{url}"' in r.text
    r = c.post("/jobs/add", data={"url": url, "text": desc}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/review?start=1")


def test_web_add_other_errors_redirect_with_msg(settings):
    c, _ = _link_app(settings)
    r = c.post("/jobs/add", data={"url": "http://127.0.0.1:8000/"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/?msg=")
    assert "private network" in c.get(r.headers["location"]).text


def test_add_post_rejects_cross_site(settings):
    c, _ = _link_app(settings)
    for headers in ({"origin": "https://evil.example"}, {"sec-fetch-site": "cross-site"}):
        r = c.post("/jobs/add", data={"url": "https://job-boards.greenhouse.io/a/jobs/1"},
                   headers=headers, follow_redirects=False)
        assert r.status_code == 403


def test_add_bad_url_redirects_not_500(settings):
    c, _ = _link_app(settings)
    r = c.post("/jobs/add", data={"url": "http://[zz/job"}, follow_redirects=False)
    assert r.status_code == 303 and "valid" in r.headers["location"]


def test_favicon_served_not_404(tmp_path):
    from fastapi.testclient import TestClient
    from jobhunt.config import Settings
    from jobhunt.web.app import create_app
    client = TestClient(create_app(Settings(db_path=tmp_path / "t.db")), base_url="http://127.0.0.1")
    r = client.get("/favicon.ico")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    assert 'rel="icon"' in client.get("/").text


# --- quick status changes: Jobs list dropdown and Pipeline drag-and-drop ------

HX = {"HX-Request": "true"}


def _status_and_events(settings, job_id):
    conn = db.connect(settings.db_path)
    try:
        return db.get_job(conn, job_id)["status"], len(db.list_events(conn, job_id))
    finally:
        conn.close()


def test_jobs_list_has_inline_status_select(client):
    html = client.get("/").text
    assert 'hx-post="/jobs/1/status"' in html and 'hx-trigger="change"' in html
    for group in ("Triage", "Pipeline", "Closed"):
        assert f'<optgroup label="{group}">' in html


def test_inline_status_change_returns_select_and_logs_event(client, settings):
    before = _status_and_events(settings, 1)[1]
    r = client.post("/jobs/1/status", data={"status": "applied"}, headers=HX)
    assert r.status_code == 200 and "<select" in r.text and "<html" not in r.text  # a fragment
    assert '<option value="applied" selected>' in r.text
    status, events = _status_and_events(settings, 1)
    assert status == "applied" and events == before + 1


def test_same_status_again_logs_nothing(client, settings):
    client.post("/jobs/1/status", data={"status": "applied"}, headers=HX)
    events = _status_and_events(settings, 1)[1]
    client.post("/jobs/1/status", data={"status": "applied"}, headers=HX)
    assert _status_and_events(settings, 1)[1] == events


def test_non_htmx_status_post_still_redirects(client):
    r = client.post("/jobs/1/status", data={"status": "screening"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/jobs/1"


def test_bad_status_is_rejected(client, settings):
    r = client.post("/jobs/1/status", data={"status": "hired!"}, headers=HX)
    assert r.status_code == 422 and _status_and_events(settings, 1)[0] != "hired!"


def test_board_fragment_has_columns_closed_targets_and_draggable_cards(client, settings):
    conn = db.connect(settings.db_path)
    db.set_job_status(conn, 1, "applied")
    db.set_job_status(conn, 2, "rejected")
    conn.close()
    html = client.get("/pipeline/board").text
    for stage in ("applied", "screening", "interviewing", "offer", "rejected", "withdrawn", "accepted"):
        assert f'data-status="{stage}"' in html
    assert 'data-job-id="1"' in html and 'data-job-id="2"' in html
    rejected = html[html.index('data-status="rejected"'):html.index('data-status="withdrawn"')]
    assert 'data-job-id="2"' in rejected   # closed jobs are cards in their closed column


def test_pipeline_page_includes_board_and_sortable(client):
    html = client.get("/pipeline").text
    assert 'id="board"' in html and re.search(r"sortablejs@\d+\.\d+\.\d+/", html)


def test_drop_on_board_changes_status_seen_by_jobs_list(client, settings):
    client.post("/jobs/1/status", data={"status": "interviewing"}, headers=HX)
    jobs = client.get("/").text
    row = jobs[jobs.index("Evals Lead"):]
    assert '<option value="interviewing" selected>' in row[:row.index("</tr>")]
    board = client.get("/pipeline/board").text
    col = board[board.index('data-status="interviewing"'):board.index('data-status="offer"')]
    assert 'data-job-id="1"' in col


def test_pages_refresh_when_tab_regains_focus(client):
    assert 'data-refresh-on-focus="#jobs-table"' in client.get("/").text
    assert 'data-refresh-on-focus="#board"' in client.get("/pipeline").text


def test_profile_keys_save_gmail_app_password_and_address(client, settings):
    page = client.get("/profile").text
    assert 'name="gmail_app_password"' in page and 'name="gmail_address"' in page
    client.post("/profile/keys", data={"gmail_address": "me@gmail.com", "gmail_app_password": "abcdpw"})
    env = settings.env_path.read_text()
    assert "GMAIL_ADDRESS=me@gmail.com" in env and "GMAIL_APP_PASSWORD=abcdpw" in env
    page = client.get("/profile").text
    assert "abcdpw" not in page and 'value="me@gmail.com"' in page
    assert "connect Gmail" not in client.get("/pipeline").text


def test_not_connected_links_to_setup(client):
    assert 'href="/setup?step=gmail">connect Gmail</a>' in client.get("/interviews").text
    assert 'href="/setup?step=gmail">connect Gmail</a>' in client.get("/pipeline").text
