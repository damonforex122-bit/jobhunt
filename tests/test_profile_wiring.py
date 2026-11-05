import httpx
import respx
from master_layout import render_master_layout
from pypdf import PdfReader
from tailor_fakes import sample_resume

from jobhunt import db, profile_store, scan
from jobhunt.config import Settings
from jobhunt.fit import grade_job
from jobhunt.models import Company, Fit, Job, Preferences
from jobhunt.profile_store import Links, MasterInfo
from jobhunt.tailor import apply_profile_links
from jobhunt.tailor.master import master_sha256

ASHBY = "https://api.ashbyhq.com/posting-api/job-board/harvey"


def _payload():
    def job(i, title, loc):
        return {"id": i, "title": title, "location": loc, "secondaryLocations": [],
                "jobUrl": f"https://jobs.ashbyhq.com/harvey/{i}", "descriptionPlain": "Evals.",
                "publishedAt": "2026-10-30T12:00:00.000+00:00"}
    return {"jobs": [job("a1", "Evals Engineer", "Austin"), job("a2", "Evals Engineer", "San Francisco")]}


@respx.mock
def test_scan_uses_saved_preferences(conn, tmp_path):
    pdir = tmp_path / "prof"
    pdir.mkdir()
    s = Settings(proficiently_dir=pdir, watchlist_path=tmp_path / "w.yaml")
    db.upsert_company(conn, Company(name="A", ats="ashby", slug="harvey"))
    profile_store.save_preferences(conn, Preferences(
        locations=["Austin"], salary_floor=0, search_terms=["evals"], excluded_terms=[]))
    respx.get(ASHBY).respond(json=_payload())
    with httpx.Client() as client:
        scan.run_scan(conn, s, client)
    locs = [j["location"] for j in db.list_jobs(conn)]
    assert locs == ["Austin"]


def _grade_setup(conn, tmp_path, monkeypatch):
    pdir = tmp_path / "prof"
    pdir.mkdir()
    (pdir / "profile.md").write_text("proficiently resume text")
    jid, _ = db.upsert_job(conn, Job(company="A", title="T", url="https://x.test/1",
                                     source="import", description="Need evals"))
    seen = {}

    def fake_grade(job, resume, settings, client=None):
        seen["resume"] = resume
        return Fit(job_id=job["id"], keyword_score=0, llm_score=5, reasons="r", model="m",
                   graded_at="2026-01-01")

    monkeypatch.setattr("jobhunt.fit.grade", fake_grade)
    return jid, Settings(proficiently_dir=pdir, db_path=tmp_path / "t.db"), seen


def test_grading_uses_uploaded_master_text(conn, tmp_path, monkeypatch):
    jid, s, seen = _grade_setup(conn, tmp_path, monkeypatch)
    master = sample_resume()
    pdf = tmp_path / "up.pdf"
    render_master_layout(master, pdf)
    profile_store.save_master(conn, MasterInfo(path=str(pdf), sha256=master_sha256(pdf), name="up.pdf",
                                               uploaded_at="2026-01-01"))
    grade_job(conn, jid, s)
    assert seen["resume"] == master.full_text()


def test_grading_without_upload_reads_proficiently(conn, tmp_path, monkeypatch):
    jid, s, seen = _grade_setup(conn, tmp_path, monkeypatch)
    grade_job(conn, jid, s)
    assert "proficiently resume text" in seen["resume"]


def test_apply_profile_links_replaces_appends_and_copies():
    master = sample_resume()
    before = master.model_copy(deep=True)
    out = apply_profile_links(master, Links(github="https://gh.example/me", linkedin="javascript:x"))
    urls = {lk.label: lk.url for lk in out.header.links}
    assert urls["GitHub"] == "https://gh.example/me"
    assert urls["LinkedIn"] == "https://linkedin.example.com/in/alex"  # non-http ignored
    assert master == before  # never mutated
    bare = master.model_copy(update={"header": master.header.model_copy(update={"links": []})})
    out = apply_profile_links(bare, Links(website="https://w.example", linkedin="https://l.example",
                                          github="https://g.example"))
    assert [lk.label for lk in out.header.links] == ["Website", "LinkedIn", "GitHub"]
    assert apply_profile_links(master, Links()) == master


def test_tailor_uses_uploaded_master_and_profile_links(conn, tmp_path):
    from test_tailor_job import add_job, fake
    from jobhunt.tailor import tailor_job

    master = sample_resume()
    pdf = tmp_path / "up.pdf"
    render_master_layout(master, pdf)
    profile_store.save_master(conn, MasterInfo(path=str(pdf), sha256=master_sha256(pdf), name="up.pdf",
                                               uploaded_at="2026-01-01"))
    profile_store.save_links(conn, Links(github="https://github.com/new-me"))
    s = Settings(db_path=tmp_path / "t.db", proficiently_dir=tmp_path,
                 master_resume_path=tmp_path / "missing.pdf", resume_output_dir=tmp_path / "out",
                 gemini_api_key="k")
    res = tailor_job(conn, add_job(conn), s, client=fake())
    assert res.error is None
    uris = {a.get_object()["/A"]["/URI"] for p in PdfReader(res.path).pages
            for a in p.get("/Annots", []) if "/A" in a.get_object()}
    assert "https://github.com/new-me" in uris
    assert "https://github.example.com/alex" not in uris
