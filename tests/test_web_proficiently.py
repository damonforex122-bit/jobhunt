import os

import pytest
from fastapi.testclient import TestClient

from jobhunt import db
from jobhunt.config import Settings
from jobhunt.models import Fit, Job, Person
from jobhunt.web import app as webapp

SHEET = "# Apply\n\n## NEEDS GEORGIA\n- Upload transcript\n- Answer visa question\n\n## OTHER\n- not this\n"


def tc(app):
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture
def env(tmp_path):
    prof = tmp_path / "prof"
    folder = prof / "jobs" / "acme-role"
    folder.mkdir(parents=True)
    s = Settings(db_path=tmp_path / "w.db", proficiently_dir=prof,
                 master_resume_path=tmp_path / "m.pdf", resume_output_dir=tmp_path / "out")
    files = {"resume.md": "# Resume\nhello", "resume.pdf": "%PDF-1.4 x", "resume.docx": "PK docx",
             "apply-sheet.md": SHEET, "cover-letter.md": "Dear"}
    for n, c in files.items():
        (folder / n).write_text(c)
    conn = db.connect(s.db_path)
    jid, _ = db.upsert_job(conn, Job(company="Acme", title="Role", url="https://x.test/r",
                                     description="d", source="proficiently"))
    kinds = {"resume.md": "resume_md", "resume.pdf": "resume_pdf", "resume.docx": "resume_docx",
             "apply-sheet.md": "apply_sheet", "cover-letter.md": "cover_letter"}
    ids = {}
    for n, k in kinds.items():
        db.save_material(conn, jid, "acme-role", k, str(folder / n), "2026-11-01")
        ids[k] = conn.execute("SELECT id FROM job_materials WHERE path = ?",
                              (str(folder / n),)).fetchone()[0]
    conn.close()
    return s, jid, ids, folder


def _plant(s, jid, path, kind="posting"):
    conn = db.connect(s.db_path)
    db.save_material(conn, jid, "x", kind, str(path), "2026-11-01")
    mid = conn.execute("SELECT id FROM job_materials WHERE path = ?", (str(path),)).fetchone()[0]
    conn.close()
    return mid


def test_job_page_lists_materials_and_needs(env):
    s, jid, ids, _ = env
    html = tc(webapp.create_app(s)).get(f"/jobs/{jid}").text
    assert "Prepared by Proficiently" in html
    for label in ("Resume (PDF)", "Resume (Markdown)", "Resume (Word)", "Cover letter", "Apply sheet"):
        assert label in html
    assert f"/files/{ids['resume_pdf']}" in html
    assert "Upload transcript" in html and "Answer visa question" in html
    assert "not this" not in html


def test_job_page_omits_card_without_materials(env):
    s, jid, _, _ = env
    conn = db.connect(s.db_path)
    j2, _ = db.upsert_job(conn, Job(company="B", title="T", url="https://x.test/2", source="ats"))
    conn.close()
    assert "Prepared by Proficiently" not in tc(webapp.create_app(s)).get(f"/jobs/{j2}").text


def test_job_page_ignores_unreadable_sheet(env):
    s, jid, ids, folder = env
    (folder / "apply-sheet.md").unlink()
    r = tc(webapp.create_app(s)).get(f"/jobs/{jid}")
    assert r.status_code == 200 and "Upload transcript" not in r.text


def test_files_route_serves_recorded_file(env):
    s, jid, ids, _ = env
    c = tc(webapp.create_app(s))
    md = c.get(f"/files/{ids['resume_md']}")
    assert md.status_code == 200 and md.headers["content-type"] == "text/plain; charset=utf-8"
    assert "hello" in md.text
    pdf = c.get(f"/files/{ids['resume_pdf']}")
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.headers["content-disposition"].startswith("inline")
    docx = c.get(f"/files/{ids['resume_docx']}")
    assert docx.status_code == 200 and docx.headers["content-disposition"].startswith("attachment")


def test_files_route_404_for_unknown_id_and_outside_path(env, tmp_path):
    s, jid, _, folder = env
    c = tc(webapp.create_app(s))
    assert c.get("/files/9999").status_code == 404
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    assert c.get(f"/files/{_plant(s, jid, outside)}").status_code == 404
    assert c.get(f"/files/{_plant(s, jid, folder / 'gone.md')}").status_code == 404
    assert c.get(f"/files/{_plant(s, jid, folder)}").status_code == 404  # a directory


def test_files_route_404_for_symlink_escaping_root(env, tmp_path):
    s, jid, _, folder = env
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    link = folder / "link.md"
    os.symlink(outside, link)
    assert tc(webapp.create_app(s)).get(f"/files/{_plant(s, jid, link)}").status_code == 404


def test_digest_route(env, tmp_path):
    s, jid, _, _ = env
    d = s.proficiently_dir / "daily-digest"
    d.mkdir()
    (d / "2026-11-01.md").write_text("# Digest")
    outside = tmp_path / "d.md"
    outside.write_text("x")
    conn = db.connect(s.db_path)
    ok = db.start_run(conn, "digest", str(d / "2026-11-01.md"))
    bad = db.start_run(conn, "digest", str(outside))
    conn.close()
    c = tc(webapp.create_app(s))
    r = c.get(f"/digests/{ok}")
    assert r.status_code == 200 and "# Digest" in r.text
    assert r.headers["content-type"] == "text/plain; charset=utf-8"
    assert c.get(f"/digests/{bad}").status_code == 404
    assert c.get("/digests/9999").status_code == 404


def test_warm_intro_badge(env):
    s, jid, _, _ = env
    conn = db.connect(s.db_path)
    db.upsert_person(conn, Person(company="Acme", job_id=jid, name="Warm Wendy", source="proficiently",
                                  source_url="https://x.test/w", confidence=0.9, kind="warm_intro"))
    db.upsert_person(conn, Person(company="Acme", job_id=jid, name="Cold Carl", source="apollo",
                                  source_url="https://x.test/c", confidence=0.5))
    conn.close()
    html = tc(webapp.create_app(s)).get(f"/jobs/{jid}").text
    assert html.count("warm intro") == 1
    assert html.index("Warm Wendy") < html.index("warm intro") < html.index("Cold Carl")


def test_both_scores_shown(env):
    s, jid, _, _ = env
    conn = db.connect(s.db_path)
    db.save_fit(conn, Fit(job_id=jid, keyword_score=5, llm_score=8, match_pct=70, graded_at="2026-01-01"))
    conn.execute("UPDATE jobs SET prof_score = 9 WHERE id = ?", (jid,))
    conn.commit()
    conn.close()
    c = tc(webapp.create_app(s))
    for url in ("/", f"/jobs/{jid}"):
        html = c.get(url).text
        assert "Fit 8/10" in html and "Proficiently 9" in html
