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
    verify_ai_full,
    to_resume,
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
    del read.experience[2]  # the last role: nothing matched after it, so only the 95% rule sees it
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


# --- boundaries, attribution, section order, left-out lines (the verbatim guarantee) --------------

def lines_pdf(path, lines):
    c = canvas.Canvas(str(path), pagesize=letter)
    y = 760
    for t in lines:
        c.setFont("Helvetica", 9)
        c.drawString(40, y, t)
        y -= 12
    c.save()
    return master._pdf_text(path)


def verdict(read, text):
    return verify_ai(to_resume(read, []), text)


def plain(**kw):
    base = dict(name="Pat Lee", contact=["pat@example.com"], links=[], summary="", experience=[],
                publications=[], skills=[], education=[])
    return ResumeRead(**{**base, **kw})


def role(org, bullets, title="Engineer", dates="2019 - 2020", location="", blurb=""):
    return EntryRead(org=org, location=location, title=title, dates=dates, blurb=blurb, bullets=bullets)


def test_a_cut_off_number_is_rejected(pdf, settings):
    read = faithful()
    read.experience[2].bullets[0] = "Wrote a log search CLI that reached 30"
    assert problems(read, pdf, settings)


def test_a_mid_sentence_fragment_is_rejected(pdf, settings):
    read = faithful()
    read.experience[0].bullets[0] = "p95 latency from 900ms to 200ms"
    assert problems(read, pdf, settings)
    read = faithful()
    read.experience[1].bullets[1] = "Owned the on-call rotation for the payments service"
    assert problems(read, pdf, settings)


@pytest.mark.parametrize("fake_skill", ["C", "R", "AI", "Go", "S", "E", "Py"])
def test_invented_short_skill_is_rejected(pdf, settings, fake_skill):
    read = faithful()
    read.skills[0].items.append(fake_skill)
    assert fake_skill in problems(read, pdf, settings)


def test_partial_title_is_rejected(pdf, settings):
    read = faithful()
    read.experience[0].title = "Engineer"  # the PDF says Senior Engineer
    assert problems(read, pdf, settings)


def test_skills_carved_out_of_words_are_rejected(tmp_path):
    text = lines_pdf(tmp_path / "s.pdf", ["Pat Lee", "pat@example.com", "SKILLS",
                                          "Languages: JavaScript, TypeScript, PostgreSQL", "Cloud: Google Cloud, Azure"])
    good = [SkillRead(name="Languages", items=["JavaScript", "TypeScript", "PostgreSQL"]),
            SkillRead(name="Cloud", items=["Google Cloud", "Azure"])]
    assert verdict(plain(skills=good), text) == []
    carved = [SkillRead(name="Languages", items=["Java", "Script", "TypeScript", "Postgre", "SQL"]),
              SkillRead(name="Cloud", items=["Google", "Cloud", "Azure"])]
    assert verdict(plain(skills=carved), text)
    moved = [SkillRead(name="Languages", items=["JavaScript", "TypeScript"]),
             SkillRead(name="Cloud", items=["PostgreSQL", "Google Cloud", "Azure"])]
    assert verdict(plain(skills=moved), text)  # a skill moved to another category
    moved = [SkillRead(name="Languages", items=["JavaScript", "TypeScript", "PostgreSQL", "Google Cloud"]),
             SkillRead(name="Cloud", items=["Azure"])]
    assert verdict(plain(skills=moved), text)


def test_bullet_moved_to_the_next_role_is_rejected(pdf, settings):
    read = faithful()
    b = read.experience[0].bullets.pop()
    read.experience[1].bullets.insert(0, b)
    assert problems(read, pdf, settings)
    read = faithful()
    read.experience[0].bullets.append(read.experience[1].bullets.pop(0))
    assert problems(read, pdf, settings)


def test_bullet_turned_into_a_blurb_or_location_is_rejected(pdf, settings):
    read = faithful()
    read.experience[1].blurb = read.experience[0].bullets.pop()
    assert problems(read, pdf, settings)
    read = faithful()
    read.experience[1].location = read.experience[0].bullets.pop()
    assert problems(read, pdf, settings)


def test_dropped_role_header_with_merged_bullets_is_rejected(tmp_path):
    lines, roles = ["Pat Lee", "pat@example.com", "EXPERIENCE"], []
    for i in range(4):
        bs = [f"Delivered project number {i} {k} with measurable impact across several teams." for k in range(2)]
        lines += [f"Company{i} Ltd", "Engineer", "2019 - 2020", *("- " + b for b in bs)]
        roles.append(role(f"Company{i} Ltd", bs))
    text = lines_pdf(tmp_path / "r.pdf", lines)
    assert verdict(plain(experience=roles), text) == []
    merged = [*roles[:2], role("Company2 Ltd", roles[2].bullets + roles[3].bullets)]
    assert verdict(plain(experience=merged), text)
    assert verdict(plain(experience=[*roles[:1], *roles[2:]]), text)  # a whole middle role dropped


def test_education_before_experience_is_accepted(tmp_path):
    text = lines_pdf(tmp_path / "o.pdf", [
        "Pat Lee", "pat@example.com", "EDUCATION", "State University, BS CS, 2016 - 2020", "EXPERIENCE",
        "Acme", "Engineer", "2019 - 2020", "- Built a thing that mattered a lot."])
    read = plain(experience=[role("Acme", ["Built a thing that mattered a lot."])],
                 education=[EducationRead(school="State University", detail="BS CS", dates="2016 - 2020")])
    assert verdict(read, text) == []


def test_sidebar_first_layout_is_accepted(tmp_path):
    text = lines_pdf(tmp_path / "l.pdf", [
        "Contact", "pat@example.com", "Top Skills", "Python", "Go", "Pat Lee", "Tinkerer and coffee enthusiast",
        "Experience", "Acme", "Engineer", "2019 - 2020", "Built a thing that mattered a lot to the team."])
    read = plain(skills=[SkillRead(name="Top Skills", items=["Python", "Go"])],
                 summary="Tinkerer and coffee enthusiast",
                 experience=[role("Acme", ["Built a thing that mattered a lot to the team."])])
    assert verdict(read, text) == []


def test_a_caps_company_name_is_not_mistaken_for_a_heading(tmp_path):
    text = lines_pdf(tmp_path / "h.pdf", ["Pat Lee", "pat@example.com", "EXPERIENCE", "ACME TRAINING CORP",
                                          "Engineer", "2019 - 2020", "- Built a thing."])
    dropped = plain(experience=[role("", ["Built a thing."])])
    assert verdict(dropped, text)


def test_small_trailing_role_left_out_is_reported(tmp_path):
    lines = ["Pat Lee", "pat@example.com", "EXPERIENCE"]
    roles = []
    for i in range(8):
        bs = [f"Delivered project number {i} {k} with measurable impact across several teams." for k in range(3)]
        lines += [f"Company{i} Ltd", "Engineer", "2019 - 2020", *("- " + b for b in bs)]
        roles.append(role(f"Company{i} Ltd", bs))
    lines += ["Intern Co", "Engineer", "2019 - 2020", "- Fixed bugs."]
    text = lines_pdf(tmp_path / "e.pdf", lines)
    found, left = verify_ai_full(to_resume(plain(experience=roles), []), text)
    assert found == [] and any("Intern Co" in t for t in left)


def test_preview_lists_what_was_left_out(settings, pdf, monkeypatch):
    read = faithful()
    read.experience[0].bullets = BULLETS  # whole PDF read: nothing left out
    monkeypatch.setattr(agents, "make_client", lambda s, on_switch=None: fake(read))
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as c:
        upload(c, pdf)
        assert "Not included from your PDF" not in c.get("/profile").text


def test_header_links_are_only_looked_for_in_the_first_lines(tmp_path):
    p = tmp_path / "x.pdf"
    c = canvas.Canvas(str(p), pagesize=letter)
    for k in range(12):
        c.drawString(40, 760 - 14 * k, "LinkedIn" if k == 10 else f"line {k}")
    c.linkURL("https://elsewhere.example.com", (40, 760 - 140 - 2, 100, 760 - 140 + 10))
    c.save()
    assert master_ai._header_urls(master.PdfReader(p), ["LinkedIn"]) == []


def four_roles(tmp_path):
    lines, roles = ["Pat Lee", "pat@example.com", "EXPERIENCE"], []
    titles = ["Engineer", "Senior Engineer", "Staff Engineer", "Principal Engineer"]
    for i in range(4):
        bs = [f"Delivered project number {i} {k} with measurable impact across several teams." for k in range(2)]
        lines += [f"Company{i} Ltd", titles[i], f"201{i} - 201{i + 1}", *("- " + b for b in bs)]
        roles.append(role(f"Company{i} Ltd", bs, title=titles[i], dates=f"201{i} - 201{i + 1}"))
    return roles, titles, lines_pdf(tmp_path / "r4.pdf", lines)


def merged_without_third(roles, titles):
    return [*roles[:2], role("Company2 Ltd", roles[2].bullets + roles[3].bullets, title=titles[2], dates="2012 - 2013")]


def test_dropped_role_header_laundered_into_education_or_contact_is_rejected(tmp_path):
    roles, titles, text = four_roles(tmp_path)
    assert verdict(plain(experience=roles), text) == []
    merged = merged_without_third(roles, titles)
    edu = [EducationRead(school="Company3 Ltd", detail=titles[3], dates="2013 - 2014")]
    assert verdict(plain(experience=merged, education=edu), text)
    contact = ["pat@example.com", "Company3 Ltd", titles[3], "2013 - 2014"]
    assert verdict(plain(contact=contact, experience=merged), text)


def test_a_bullet_moved_into_the_summary_is_rejected(tmp_path):
    roles, titles, text = four_roles(tmp_path)
    moved = [roles[0], role("Company1 Ltd", roles[1].bullets[1:], title=titles[1], dates="2011 - 2012"), *roles[2:]]
    assert verdict(plain(experience=moved, summary=roles[1].bullets[0]), text)


GLYPHLESS = ["Pat Lee", "pat@example.com", "Experience", "Acme", "Engineer", "2019 - 2020",
             "Built a thing that mattered a lot to the team.", "Shipped the payments migration with zero downtime.",
             "Beta", "Staff Engineer", "2017 - 2019", "Ran the platform group of eight people."]


def glyphless_roles(**beta):
    return [role("Acme", GLYPHLESS[6:8]),
            role("Beta", [GLYPHLESS[11]], title="Staff Engineer", dates="2017 - 2019", **beta)]


def test_glyphless_layout_faithful_read_is_accepted_and_moves_are_rejected(tmp_path):
    text = lines_pdf(tmp_path / "g.pdf", GLYPHLESS)
    assert verdict(plain(experience=glyphless_roles()), text) == []
    moved = [role("Acme", GLYPHLESS[6:7]), glyphless_roles(location=GLYPHLESS[7])[1]]
    assert verdict(plain(experience=moved), text)  # A's last bullet became B's location
    absorbed = [role("Acme", GLYPHLESS[6:8] + GLYPHLESS[8:12])]
    assert verdict(plain(experience=absorbed), text)  # B's heading lines read as A's bullets


def test_a_location_must_be_short_and_next_to_the_role_heading(tmp_path):
    lines = ["Pat Lee", "pat@example.com", "Acme", "Engineer", "2019 - 2020", "Built things that mattered a lot", "Remote"]
    text = lines_pdf(tmp_path / "l.pdf", lines)
    far = plain(experience=[role("Acme", ["Built things that mattered a lot"], location="Remote")])
    assert verdict(far, text)  # 'Remote' is not beside the heading and the heading lines are not consecutive


def test_contact_items_must_be_whole_segments(pdf, settings):
    read = faithful()
    read.contact = ["Portland, OR", "sam@example"]
    assert problems(read, pdf, settings)


def test_a_split_wrapped_bullet_is_rejected(tmp_path):
    text = lines_pdf(tmp_path / "w.pdf", ["Pat Lee", "pat@example.com", "EXPERIENCE", "Acme", "Engineer", "2019 - 2020",
                                          "- Led the review that decided not", "  to ship the risky launch."])
    whole = plain(experience=[role("Acme", ["Led the review that decided not to ship the risky launch."])])
    assert verdict(whole, text) == []
    split = plain(experience=[role("Acme", ["Led the review that decided not", "to ship the risky launch."])])
    assert verdict(split, text)


def test_numbered_bullets_are_accepted(tmp_path):
    text = lines_pdf(tmp_path / "n.pdf", ["Pat Lee", "pat@example.com", "EXPERIENCE", "Acme", "Engineer", "2019 - 2020",
                                          "1. Built the first thing for the team.", "2) Built the second thing too."])
    read = plain(experience=[role("Acme", ["Built the first thing for the team.", "Built the second thing too."])])
    assert verdict(read, text) == []


def test_a_heading_in_the_middle_of_experience_is_not_a_skipped_line(tmp_path):
    lines = ["Pat Lee", "pat@example.com", "EXPERIENCE", "Acme", "Engineer", "2019 - 2020", "- Built the first thing.",
             "ADDITIONAL EXPERIENCE", "Beta", "Engineer", "2017 - 2019", "- Built the other thing."]
    text = lines_pdf(tmp_path / "a.pdf", lines)
    read = plain(experience=[role("Acme", ["Built the first thing."]),
                             role("Beta", ["Built the other thing."], dates="2017 - 2019")])
    assert verdict(read, text) == []


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
         "education": 1, "left_out": ["<em>Intern Co</em> fixed bugs for a summer"]})
    conn.close()
    page = web.get("/profile/master/preview").text
    assert "Not included from your PDF" in page and "&lt;em&gt;Intern Co" in page
    assert "<script>" not in page and "<img" not in page and "<b>x" not in page
    assert "&lt;script&gt;" in page


def test_confirm_without_a_pending_upload_is_harmless(web):
    r = web.post("/profile/master/confirm", follow_redirects=False)
    assert r.status_code == 303 and "no uploaded resume" in unquote(r.headers["location"])


def test_upload_posts_reject_cross_site(web):
    for path in ("/profile/master/confirm", "/profile/master/cancel"):
        assert web.post(path, headers={"Origin": "http://evil.example"}).status_code == 403
