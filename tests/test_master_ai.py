"""The AI resume reader: any layout in, verbatim-or-rejected out. No network: FakeGemini."""

import hashlib
from pathlib import Path
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient
from master_layout import render_master_layout
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from tailor_fakes import FakeGemini, sample_resume

from jobhunt import db, profile_store
from jobhunt.config import KEY_FIELDS, Settings
from jobhunt.tailor import agents, master, master_ai
from jobhunt.tailor.master import MasterError, load_master_with_sha
from jobhunt.tailor.master_ai import (
    EducationRead,
    EntryRead,
    LinkRead,
    ResumeRead,
    SkillRead,
    parse_master_ai,
    verify_ai,
)
from jobhunt.web.app import create_app

LINKEDIN = "https://linkedin.example.com/in/sam"
BULLETS = [
    "Cut p95 latency from 900ms to 200ms by caching hot queries.",
    "Moved nightly batch jobs to streaming, saving four hours a day.",
]
LINES = [  # (text, bold, bullet?) -- a layout the strict parser rejects: dash bullets, PROJECTS
    ("Sam Rivera", True), ("Portland, OR | sam@example.com | LinkedIn", False),
    ("PROFILE", True), ("Backend engineer focused on reliable data systems and clear ownership.", False),
    ("WORK HISTORY", True),
    ("Acme Corp, Portland OR", True), ("Senior Engineer, 2021 - Present", False),
    (f"- {BULLETS[0]}", False), (f"- {BULLETS[1]}", False),
    ("Beta Inc, Remote", True), ("Engineer, 2018 - 2021", False),
    ("- Built the billing export used by finance every month.", False),
    ("- Owned the on-call rotation for the payments service for two years.", False),
    ("PROJECTS", True), ("Open Source Tool", True),
    ("- Wrote a log search CLI that reached 300 GitHub stars.", False),
    ("SKILLS", True), ("Languages: Python, Go, SQL", False),
    ("EDUCATION", True), ("State University, BS Computer Science, 2014 - 2018", False),
]


def make_pdf(path):
    c = canvas.Canvas(str(path), pagesize=letter)
    y = 740
    for text, bold in LINES:
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 10)
        c.drawString(72, y, text)
        if text.startswith("Portland"):
            c.linkURL(LINKEDIN, (72 + c.stringWidth("Portland, OR | sam@example.com | ", "Helvetica", 10), y - 2,
                                 72 + c.stringWidth(text, "Helvetica", 10), y + 10))
        y -= 16
    c.save()
    return path


def faithful() -> ResumeRead:
    return ResumeRead(
        name="Sam Rivera", contact=["Portland, OR", "sam@example.com"], links=[LinkRead(label="LinkedIn")],
        summary="Backend engineer focused on reliable data systems and clear ownership.",
        experience=[
            EntryRead(org="Acme Corp", location="Portland OR", title="Senior Engineer", dates="2021 - Present",
                      blurb="", bullets=BULLETS),
            EntryRead(org="Beta Inc", location="Remote", title="Engineer", dates="2018 - 2021", blurb="", bullets=[
                "Built the billing export used by finance every month.",
                "Owned the on-call rotation for the payments service for two years."]),
            EntryRead(org="Open Source Tool", location="", title="", dates="", blurb="",
                      bullets=["Wrote a log search CLI that reached 300 GitHub stars."]),
        ],
        publications=[], skills=[SkillRead(name="Languages", items=["Python", "Go", "SQL"])],
        education=[EducationRead(school="State University", detail="BS Computer Science", dates="2014 - 2018")],
    )


@pytest.fixture
def pdf(tmp_path):
    return make_pdf(tmp_path / "other.pdf")


@pytest.fixture
def settings(tmp_path):
    return Settings(db_path=tmp_path / "data" / "p.db", gemini_api_key="k-test", env_path=tmp_path / ".env",
                    master_resume_path=None, proficiently_dir=None)


def fake(read: ResumeRead) -> FakeGemini:
    return FakeGemini({ResumeRead: read})


def resume_of(read, pdf, settings):
    return parse_master_ai(pdf, fake(read), settings)


def problems(read, pdf, settings):
    return verify_ai(resume_of(read, pdf, settings), master._pdf_text(pdf))


def test_the_other_layout_is_not_strictly_readable(pdf):
    with pytest.raises(Exception):
        master.verify(master.parse_master(pdf), master._pdf_text(pdf))


def test_faithful_read_is_accepted_with_our_ids_and_the_pdfs_link(pdf, settings):
    r = resume_of(faithful(), pdf, settings)
    assert verify_ai(r, master._pdf_text(pdf)) == []
    assert [e.id for e in r.experience] == ["e0", "e1", "e2"]
    assert [b.id for b in r.experience[0].bullets] == ["e0b0", "e0b1"]
    assert r.skills[0].id == "s0"
    assert [(link.label, link.url) for link in r.header.links] == [("LinkedIn", LINKEDIN)]


def test_link_count_mismatch_never_guesses_a_url(pdf, settings):
    read = faithful()
    read.links = [LinkRead(label="LinkedIn"), LinkRead(label="Portfolio")]
    r = resume_of(read, pdf, settings)
    assert r.header.links == [] and "Portfolio" in r.header.contact


def test_reworded_bullet_rejected(pdf, settings):
    read = faithful()
    read.experience[0].bullets[0] = "Slashed p95 latency from 900ms to 200ms through smart caching."
    assert problems(read, pdf, settings)


def test_invented_bullet_rejected(pdf, settings):
    read = faithful()
    read.experience[0].bullets.append("Led a team of 40 engineers.")
    assert "Led a team of 40 engineers." in problems(read, pdf, settings)


def test_merged_bullets_rejected(pdf, settings):
    read = faithful()
    read.experience[0].bullets = [" ".join(BULLETS)]
    assert problems(read, pdf, settings)


def test_out_of_order_rejected(pdf, settings):
    read = faithful()
    read.experience[0].bullets = BULLETS[::-1]
    assert problems(read, pdf, settings)
    read = faithful()
    read.experience[0], read.experience[1] = read.experience[1], read.experience[0]
    assert problems(read, pdf, settings)


def test_dropping_more_than_five_percent_rejected(pdf, settings):
    read = faithful()
    del read.experience[1]
    found = problems(read, pdf, settings)
    assert len(found) == 1 and found[0].startswith("left out ") and found[0].endswith("% of your resume")


def test_dropping_a_little_is_tolerated(pdf, settings):
    text = master._pdf_text(pdf) + "\nPage 1 of 1"  # a page footer the read ignores
    assert verify_ai(resume_of(faithful(), pdf, settings), text) == []


def test_strict_parser_still_wins_without_a_call(conn, tmp_path, settings):
    p = tmp_path / "m.pdf"
    render_master_layout(sample_resume(), p)
    client = FakeGemini({ResumeRead: faithful()})
    resume, _ = load_master_with_sha(conn, p, client=client, settings=settings)
    assert resume.header.name == "Alex Q. Sample, MSc" and client.calls == []


def test_ai_read_is_used_cached_and_reused_without_a_client(conn, pdf, settings):
    client = fake(faithful())
    resume, sha = load_master_with_sha(conn, pdf, client=client, settings=settings)
    assert resume.header.name == "Sam Rivera" and len(client.calls) == 1
    again, sha2 = load_master_with_sha(conn, pdf)  # strict only, but the AI read is cached
    assert again == resume and sha2 == sha and len(client.calls) == 1


def test_unfaithful_ai_read_raises_and_is_not_cached(conn, pdf, settings):
    read = faithful()
    read.summary = "An invented summary."
    with pytest.raises(MasterError, match="Couldn't read your resume reliably"):
        load_master_with_sha(conn, pdf, client=fake(read), settings=settings)
    assert conn.execute("SELECT COUNT(*) FROM master_cache").fetchone()[0] == 0


def test_provider_failure_gives_the_same_message(conn, pdf, settings):
    with pytest.raises(MasterError, match="Save to PDF"):
        load_master_with_sha(conn, pdf, client=FakeGemini(exc=RuntimeError("boom k-test")), settings=settings)


def test_no_ai_key_says_add_one(conn, pdf, settings):
    settings.gemini_api_key = None
    with pytest.raises(MasterError, match="Add an AI key on the Setup page"):
        load_master_with_sha(conn, pdf, settings=settings)


def test_parser_version_bumped():
    assert master.PARSER_VERSION >= 2


# --- upload: preview, then confirm ---------------------------------------------------------

@pytest.fixture
def web(settings, monkeypatch):
    for f in KEY_FIELDS:
        monkeypatch.delenv(f.upper(), raising=False)
    monkeypatch.setattr(agents, "make_client", lambda s, on_switch=None: fake(faithful()))
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as c:
        yield c


def upload(web, pdf, name="Other.pdf"):
    return web.post("/profile/master", files={"file": (name, pdf.read_bytes(), "application/pdf")},
                    follow_redirects=False)


def conn_for(settings):
    return db.connect(settings.db_path)


def test_preview_then_confirm_activates(web, settings, pdf):
    sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
    mdir = settings.db_path.parent / "master"
    assert upload(web, pdf).status_code == 303
    conn = conn_for(settings)
    assert profile_store.get_master(conn) is None
    assert (mdir / f".pending-{sha}.pdf").is_file()
    page = web.get("/profile").text
    for want in ("Sam Rivera", "Acme Corp", "Senior Engineer", "2 bullets", "Languages", "Use this resume",
                 "Try another file", "Save to PDF"):
        assert want in page
    assert "Use this resume" in web.get("/profile/master/preview").text
    assert web.post("/profile/master/confirm", follow_redirects=False).status_code == 303
    info = profile_store.get_master(conn)
    assert info.sha256 == sha and info.name == "Other.pdf"
    assert (mdir / f"{sha}.pdf").is_file() and not (mdir / f".pending-{sha}.pdf").exists()
    assert profile_store.get_pending_master(conn) is None
    resume, _ = load_master_with_sha(conn, mdir / f"{sha}.pdf")  # served from the cache
    assert resume.header.name == "Sam Rivera"
    conn.close()


def test_preview_without_confirm_keeps_the_old_master(web, settings, pdf, tmp_path):
    old = tmp_path / "old.pdf"
    render_master_layout(sample_resume(), old)
    web.post("/profile/master", files={"file": ("old.pdf", old.read_bytes(), "application/pdf")})
    web.post("/profile/master/confirm")
    conn = conn_for(settings)
    before = profile_store.get_master(conn)
    upload(web, pdf)
    assert profile_store.get_master(conn) == before
    assert profile_store.effective_master_path(conn, settings) == Path(before.path)
    conn.close()


def test_try_another_file_deletes_the_pending_upload(web, settings, pdf):
    upload(web, pdf)
    mdir = settings.db_path.parent / "master"
    assert web.post("/profile/master/cancel", follow_redirects=False).status_code == 303
    assert list(mdir.glob("*")) == []
    conn = conn_for(settings)
    assert profile_store.get_pending_master(conn) is None
    conn.close()


def test_only_one_pending_upload_at_a_time(web, settings, pdf, tmp_path):
    upload(web, pdf)
    other = tmp_path / "second.pdf"
    render_master_layout(sample_resume(), other)
    upload(web, other, "second.pdf")
    pending = list((settings.db_path.parent / "master").glob(".pending-*"))
    assert len(pending) == 1 and hashlib.sha256(other.read_bytes()).hexdigest() in pending[0].name


def test_failed_read_leaves_no_files_and_shows_the_message(settings, pdf, monkeypatch):
    read = faithful()
    read.summary = "invented"
    monkeypatch.setattr(agents, "make_client", lambda s, on_switch=None: fake(read))
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as c:
        r = upload(c, pdf)
        assert "reliably" in unquote(r.headers["location"])
    assert list((settings.db_path.parent / "master").glob("*")) == []


def test_preview_escapes_everything(web, settings, pdf, monkeypatch):
    conn = conn_for(settings)
    profile_store.save_pending_master(conn, profile_store.MasterInfo(
        path="x", sha256="0" * 64, name="n", uploaded_at="t"),
        {"name": "<script>alert(1)</script>", "contact": ["<b>x</b>"], "links": ["<i>l</i>"],
         "roles": [{"org": "<img src=x onerror=1>", "title": "t", "bullets": 1}], "skills": ["<u>s</u>"],
         "education": 1})
    conn.close()
    page = web.get("/profile/master/preview").text
    assert "<script>" not in page and "<img" not in page and "<b>x" not in page
    assert "&lt;script&gt;" in page


def test_confirm_without_a_pending_upload_is_harmless(web):
    r = web.post("/profile/master/confirm", follow_redirects=False)
    assert r.status_code == 303 and "no uploaded resume" in unquote(r.headers["location"])


def test_upload_posts_reject_cross_site(web):
    for path in ("/profile/master/confirm", "/profile/master/cancel"):
        assert web.post(path, headers={"Origin": "http://evil.example"}).status_code == 403
