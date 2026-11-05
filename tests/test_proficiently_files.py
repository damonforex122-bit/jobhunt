import json
import os

from jobhunt import db
from jobhunt.importer import run_import
from jobhunt.models import Company, Person
from jobhunt.sources.proficiently_files import (
    apply_sheet_needs, import_careers, import_digests, import_materials, parse_warm_intros,
)
from prof_fixture import make_proficiently

H = "https://jobs.lever.co/acme/"


def row(n, status="ready"):
    return {"Company": f"Acme{n}", "Role": f"Role {n}", "Score": 8, "Job URL": f"{H}{n}",
            "Date": "2026-10-30", "Status": status, "Reason": "r"}


def job_id(conn, n):
    return conn.execute("SELECT id FROM jobs WHERE url LIKE ?", (f"%/acme/{n}",)).fetchone()["id"]


def test_materials_matched_by_posting_url(conn, tmp_path):
    root = make_proficiently(tmp_path / "p", history_rows=[row(1)], folders={
        "some-odd-folder-name": {"posting.md": f"# Role\n\nApply: {H}1?utm_source=x\n",
                                 "resume.pdf": "pdf", "resume.md": "md", "cover-letter.md": "c",
                                 "apply-sheet.md": "a", "warm-intros.md": "# Warm intros: Acme1\n",
                                 "resume-v2.md": "ignored", "resume.docx": "d"}})
    run_import(conn, root)
    mats = db.list_materials(conn, job_id(conn, 1))
    assert {m["kind"] for m in mats} == {"resume_pdf", "resume_md", "resume_docx", "cover_letter",
                                         "apply_sheet", "warm_intros", "posting"}
    assert all(m["folder"] == "some-odd-folder-name" and m["mtime"] for m in mats)


def test_materials_fall_back_to_slug_and_prefer_newest_folder(conn, tmp_path):
    root = make_proficiently(tmp_path / "p", history_rows=[row(1)], folders={
        "acme1-role-1-2026-10-30": {"resume.md": "old"},
        "acme1-role-1-2026-11-02": {"resume.md": "new", "posting.md": "no url here"}})
    c = run_import(conn, root)
    mats = db.list_materials(conn, job_id(conn, 1))
    assert c["materials"]["matched"] == 2
    assert mats[0]["folder"] == "acme1-role-1-2026-11-02"


def test_tmp_and_symlink_outside_root_skipped(conn, tmp_path):
    outside = tmp_path / "secret.md"
    outside.write_text("secret")
    root = make_proficiently(tmp_path / "p", history_rows=[row(1)], folders={
        "acme1-role-1-2026-10-30": {"x.tmp": "t", "resume.md": "ok"}})
    folder = root / "jobs" / "acme1-role-1-2026-10-30"
    os.symlink(outside, folder / "cover-letter.md")
    (folder / "resume.pdf").mkdir()
    c = run_import(conn, root)
    kinds = {m["kind"] for m in db.list_materials(conn, job_id(conn, 1))}
    assert kinds == {"resume_md"} and c["materials"]["files"] == 1


def test_unmatched_folder_counted(conn, tmp_path):
    root = make_proficiently(tmp_path / "p", history_rows=[row(1)], folders={
        "nobody-nothing-2026-10-30": {"resume.md": "x"}})
    c = run_import(conn, root)
    assert c["materials"] == {"folders": 1, "matched": 0, "unmatched_folders": 1, "files": 0}


def test_warm_intros_parsed_with_and_without_linkedin():
    text = (
        "# Warm intros: Acme\n\nSome prose about connections.\n\n"
        "- **Ada Lovelace**, Staff Engineer: https://www.linkedin.com/in/ada-l\n"
        "- **Bob Byte** (1st degree), Recruiter\n"
        "\n| Name | Title | LinkedIn |\n|---|---|---|\n"
        "| Cy Code | Manager | https://linkedin.com/in/cy |\n| Di Dee | | |\n"
    )
    people = {p.name: p for p in parse_warm_intros(text, "Acme")}
    assert set(people) == {"Ada Lovelace", "Bob Byte", "Cy Code", "Di Dee"}
    ada = people["Ada Lovelace"]
    assert (ada.kind, ada.source, ada.confidence) == ("warm_intro", "proficiently", 1.0)
    assert ada.linkedin_url == "https://www.linkedin.com/in/ada-l" and ada.title == "Staff Engineer"
    assert ada.source_url == ada.linkedin_url
    bob = people["Bob Byte"]
    assert bob.linkedin_url is None and bob.title == "Recruiter"
    assert bob.source_url == "proficiently:Acme:bob byte"
    assert people["Cy Code"].linkedin_url == "https://linkedin.com/in/cy"
    assert parse_warm_intros("# Warm intros: X\n\nNo connections at X.\n", "X") == []


def test_warm_intro_dedupes_with_existing_person(conn, tmp_path):
    db.upsert_person(conn, Person(company="Acme1", name="Ada Lovelace", title="Eng", source="exa",
                                  source_url="https://x/ada", confidence=0.5))
    root = make_proficiently(tmp_path / "p", history_rows=[row(1)], folders={
        "acme1-role-1-2026-10-30": {"warm-intros.md": "- **Ada Lovelace**, Staff: https://linkedin.com/in/ada\n"}})
    run_import(conn, root)
    run_import(conn, root)
    rows = conn.execute("SELECT * FROM people WHERE company = 'Acme1'").fetchall()
    assert len(rows) == 1 and rows[0]["kind"] == "warm_intro" and rows[0]["source"] == "proficiently"
    # a later found contact does not downgrade it
    db.upsert_person(conn, Person(company="Acme1", name="Ada Lovelace", source="exa",
                                  source_url="https://x/ada", confidence=0.5))
    r = conn.execute("SELECT * FROM people WHERE company = 'Acme1'").fetchone()
    assert (r["kind"], r["source"], r["confidence"]) == ("warm_intro", "proficiently", 1.0)


def test_apply_sheet_needs_section():
    text = ("# Apply\n\n## Answers\n- not this\n\n## NEEDS GEORGIA\n- Why us?\n* Upload transcript\n"
            "plain line\n\n## Other\n- nor this\n")
    assert apply_sheet_needs(text) == ["- Why us?", "* Upload transcript"]
    assert apply_sheet_needs("nothing") == []


def test_careers_adds_known_ats_and_reports_conflict(conn, tmp_path):
    db.upsert_company(conn, Company(name="Has", ats="greenhouse", slug="old"))
    db.upsert_company(conn, Company(name="Same", ats="lever", slug="same"))
    careers = {
        "New": {"careers_url": "https://jobs.lever.co/newco", "type": "ats"},
        "Has": {"careers_url": "https://jobs.lever.co/hasco"},
        "Same": {"careers_url": "https://jobs.lever.co/same"},
        "Null": {"careers_url": None},
        "Own": {"careers_url": "https://example.com/careers"},
    }
    root = make_proficiently(tmp_path / "p", history_rows=[row(1)], careers=careers)
    assert import_careers(conn, root) == {"added": 1, "conflicts": 1, "skipped": 3}
    new = db.get_company(conn, "New")
    assert (new.ats, new.slug) == ("lever", "newco")
    assert db.get_company(conn, "Has").slug == "old"


def test_digest_rows_once_per_file(conn, tmp_path):
    d = ("# Daily digest 2026-11-02\n\nNew jobs found: 15 (3 from the network scan).\n\n"
         "| Company | Role | Score | Status |\n|---|---|---|---|\n"
         "| A | R | 8 | ready |\n| B | R | 9 | needs |\n| C | R | 7 | low |\n")
    root = make_proficiently(tmp_path / "p", history_rows=[row(1)],
                             digests={"2026-11-02": d, "2026-10-30": "# Daily digest\n\nAll jobs.\n"})
    assert import_digests(conn, root) == 2
    assert import_digests(conn, root) == 0
    r = conn.execute("SELECT * FROM runs WHERE path LIKE '%2026-11-02.md'").fetchone()
    assert r["kind"] == "digest" and r["finished_at"]
    assert json.loads(r["counts_json"]) == {"found": 15, "high": 2}
    assert r["started_at"].startswith("2026-11-02")
    old = conn.execute("SELECT counts_json FROM runs WHERE path LIKE '%2026-10-30.md'").fetchone()
    assert json.loads(old["counts_json"]) == {"found": 0, "high": 0}


def test_full_import_rerun_adds_no_duplicate_materials_people_or_digests(conn, tmp_path):
    root = make_proficiently(
        tmp_path / "p", history_rows=[row(1)],
        folders={"acme1-role-1-2026-10-30": {
            "posting.md": f"{H}1", "resume.md": "r",
            "people.md": "- **Jo Doe**, CTO: https://example.com/jo\n",
            "warm-intros.md": "- **Ada L**, Eng: https://linkedin.com/in/ada\n"}},
        careers={"New": {"careers_url": "https://jobs.lever.co/newco"}},
        digests={"2026-11-02": "# d\n\nNew jobs found: 1\n"})
    first = run_import(conn, root)
    def snap():
        return [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("job_materials", "people", "runs", "companies")]
    s1 = snap()
    second = run_import(conn, root)
    s2 = snap()
    assert first["materials"]["files"] == 3 and first["careers"]["added"] == 1 and first["digests"] == 1
    assert second["digests"] == 0 and second["careers"]["added"] == 0
    assert s2[:2] == s1[:2] and s2[3] == s1[3]
    assert s2[2] == s1[2] + 1  # only the new import run row
    assert conn.execute("SELECT COUNT(*) FROM runs WHERE kind='import'").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM runs WHERE kind='digest'").fetchone()[0] == 1


def test_invalid_digest_date_skipped_not_fatal(conn, tmp_path):
    root = make_proficiently(tmp_path / "p", history_rows=[row(1)], digests={
        "2026-13-45": "# d\n\nNew jobs found: 9\n", "2026-11-02": "# d\n\nNew jobs found: 1\n"})
    c = run_import(conn, root)
    assert c["digests"] == 1 and c["digests_skipped"] == 1
    assert conn.execute("SELECT COUNT(*) FROM runs WHERE kind='digest'").fetchone()[0] == 1
    run_import(conn, root)  # later imports keep working


def test_symlinked_posting_outside_root_not_read(conn, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text(f"{H}1\n")
    root = make_proficiently(tmp_path / "p", history_rows=[row(1)], folders={"odd-folder": {}})
    os.symlink(outside, root / "jobs" / "odd-folder" / "posting.md")
    c = run_import(conn, root)
    assert c["materials"]["unmatched_folders"] == 1 and c["materials"]["files"] == 0
