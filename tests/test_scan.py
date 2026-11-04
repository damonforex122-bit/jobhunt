import httpx
import respx

from jobhunt import db, scan
from jobhunt.config import Settings
from jobhunt.models import Company, Job, Preferences

ASHBY = "https://api.ashbyhq.com/posting-api/job-board/harvey"
GH = "https://boards-api.greenhouse.io/v1/boards/ghost/jobs"


def _settings(tmp_path):
    pdir = tmp_path / "prof"
    pdir.mkdir()
    (pdir / "preferences.md").write_text("")  # falls back to defaults
    return Settings(proficiently_dir=pdir, watchlist_path=tmp_path / "watchlist.yaml")


def _ashby_payload():
    return {"jobs": [
        {"id": "a1", "title": "Member of Technical Staff, Evals", "location": "San Francisco",
         "secondaryLocations": [], "jobUrl": "https://jobs.ashbyhq.com/harvey/a565ac3b",
         "descriptionPlain": "Build evals.", "publishedAt": "2026-10-30T12:00:00.000+00:00",
         "compensation": {"compensationTierSummary": "$230K – $340K"}},
        {"id": "a2", "title": "Account Executive", "location": "San Francisco",
         "secondaryLocations": [], "jobUrl": "https://jobs.ashbyhq.com/harvey/zzz",
         "descriptionPlain": "Sell.", "publishedAt": "2026-10-30T12:00:00.000+00:00"},
        {"id": "a3", "title": "Evals Engineer", "location": "London",
         "secondaryLocations": [], "jobUrl": "https://jobs.ashbyhq.com/harvey/lon",
         "descriptionPlain": "Evals.", "publishedAt": "2026-10-30T12:00:00.000+00:00"},
    ]}


def _setup(conn):
    db.upsert_company(conn, Company(name="A", ats="ashby", slug="harvey"))
    db.upsert_company(conn, Company(name="B", ats="greenhouse", slug="ghost"))


@respx.mock
def test_scan_continues_after_company_error(conn, tmp_path):
    _setup(conn)
    respx.get(ASHBY).respond(json=_ashby_payload())
    respx.get(GH).respond(404)
    report = scan.run_scan(conn, _settings(tmp_path))
    assert set(report.errors) == {"B"}
    assert report.companies_scanned == 2
    assert report.jobs_seen == 3 and report.jobs_kept == 1 and report.jobs_new == 1
    jobs = db.list_jobs(conn)
    assert [j["title"] for j in jobs] == ["Member of Technical Staff, Evals"]
    assert jobs[0]["salary_min"] == 230000 and jobs[0]["salary_max"] == 340000
    assert jobs[0]["posted_at"] == "2026-10-30"
    cos = {c.name: c for c in db.list_companies(conn)}
    assert cos["B"].last_error == "HTTP 404"
    assert cos["A"].last_error is None and cos["A"].last_scanned


@respx.mock
def test_scan_dedups_with_import(conn, tmp_path):
    _setup(conn)
    db.upsert_job(conn, Job(company="A", title="MTS Evals", source="import",
                            url="https://jobs.ashbyhq.com/harvey/a565ac3b/application"))
    respx.get(ASHBY).respond(json=_ashby_payload())
    respx.get(GH).respond(404)
    report = scan.run_scan(conn, _settings(tmp_path))
    assert report.jobs_kept == 1 and report.jobs_new == 0
    assert len(db.list_jobs(conn)) == 1


@respx.mock
def test_scan_second_run_no_new(conn, tmp_path):
    _setup(conn)
    respx.get(ASHBY).respond(json=_ashby_payload())
    respx.get(GH).respond(404)
    s = _settings(tmp_path)
    assert scan.run_scan(conn, s).jobs_new == 1
    assert scan.run_scan(conn, s).jobs_new == 0
    assert len(db.list_jobs(conn)) == 1


@respx.mock
def test_scan_uses_watchlist_and_injected_client(conn, tmp_path):
    s = _settings(tmp_path)
    s.watchlist_path.write_text("- {name: A, ats: ashby, slug: harvey}\n")
    respx.get(ASHBY).respond(json=_ashby_payload())
    with httpx.Client() as client:
        report = scan.run_scan(conn, s, client)
    assert report.companies_scanned == 1 and report.jobs_new == 1


@respx.mock
def test_scan_survives_bad_posting_and_unexpected_exception(conn, tmp_path, monkeypatch):
    _setup(conn)
    payload = _ashby_payload()
    payload["jobs"].insert(0, {"id": "bad", "title": None, "jobUrl": "https://jobs.ashbyhq.com/harvey/bad"})
    respx.get(ASHBY).respond(json=payload)
    respx.get(GH).respond(json={"jobs": []})
    s = _settings(tmp_path)
    report = scan.run_scan(conn, s)
    assert report.jobs_new == 1 and report.jobs_skipped == 1 and not report.errors

    real = scan.ats.fetch_jobs

    def flaky(co, client, stats=None):
        if co.name == "A":
            raise RuntimeError("boom")
        return real(co, client, stats)

    monkeypatch.setattr(scan.ats, "fetch_jobs", flaky)
    report = scan.run_scan(conn, s)
    assert "boom" in report.errors["A"] and "B" not in report.errors
    assert report.companies_scanned == 2
    assert {c.name: c.last_error for c in db.list_companies(conn)}["A"].startswith("unexpected error")
