"""No Georgia-specific defaults: a fresh install starts empty; `migrate-profile` carries hers over."""
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from master_layout import render_master_layout
from tailor_fakes import FakeGemini, sample_resume

from jobhunt import db, profile_store
from jobhunt.cli import main
from jobhunt.config import KEY_FIELDS, Settings, load_settings
from jobhunt.fit import grade_job
from jobhunt.fit.gemini import build_prompt
from jobhunt.migrate import OLD_AI_NOTES
from jobhunt.models import Job
from jobhunt.profile import load_preferences
from jobhunt.tailor import agents
from jobhunt.tailor.output import choose_path, resume_base_name
from jobhunt.web.app import create_app


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for f in KEY_FIELDS:
        monkeypatch.delenv(f.upper(), raising=False)
    for k in ("MASTER_RESUME_PATH", "RESUME_OUTPUT_DIR", "PROFICIENTLY_DIR"):
        monkeypatch.delenv(k, raising=False)


def add(conn):
    return db.upsert_job(conn, Job(company="A", title="T", url="https://x.test/1", source="ats",
                                   description="Need evals"))[0]


def test_settings_defaults_have_no_personal_paths():
    s = Settings()
    assert s.master_resume_path is None and s.proficiently_dir is None
    assert s.resume_output_dir == Path("~/Documents/jobhunt/Resumes").expanduser()


def test_fresh_profile_has_empty_preferences(conn):
    p = profile_store.effective_preferences(conn, Settings())
    assert (p.locations, p.salary_floor, p.search_terms, p.excluded_terms, p.target_roles) == \
        ([], 0, [], [], [])


def test_preferences_come_from_proficiently_folder_when_set(conn, tmp_path):
    (tmp_path / "preferences.md").write_text("## Compensation\n- Floor: $250,000\n")
    p = profile_store.effective_preferences(conn, Settings(proficiently_dir=tmp_path))
    assert p.salary_floor == 250000


def test_missing_proficiently_folder_gives_empty_preferences(conn, tmp_path):
    p = profile_store.effective_preferences(conn, Settings(proficiently_dir=tmp_path / "nope"))
    assert p.locations == [] and p.salary_floor == 0


def test_no_master_means_grading_error_not_empty_text(conn):
    jid = add(conn)
    fit = grade_job(conn, jid, Settings(gemini_api_key="k"), client=object())
    assert fit.error == "Upload and confirm your resume on the Profile page."
    assert fit.llm_score is None


def test_resume_base_name():
    assert resume_base_name("Alex Sample") == "Alex Sample - Resume"
    assert resume_base_name("") == "Resume"
    assert resume_base_name(None) == "Resume"
    assert resume_base_name('A/B: "C"*?<>|\\ D') == "AB C D - Resume"
    assert resume_base_name("/:*") == "Resume"


def test_choose_path_uses_base_name_and_fallback(tmp_path):
    assert choose_path(tmp_path, "T", None, "Resume").name == "Resume.pdf"
    (tmp_path / "Resume.pdf").write_bytes(b"x")
    assert choose_path(tmp_path, "Data Lead", None, "Resume").name == "Resume (Data Lead).pdf"


def test_effective_output_dir_profile_then_settings(conn, tmp_path):
    s = Settings(resume_output_dir=tmp_path / "env")
    assert profile_store.effective_output_dir(conn, s) == tmp_path / "env"
    profile_store.put(conn, "resume_output_dir", str(tmp_path / "chosen"))
    assert profile_store.effective_output_dir(conn, s) == tmp_path / "chosen"


def test_ai_notes_round_trip(conn):
    assert profile_store.get_ai_notes(conn) == ""
    profile_store.save_ai_notes(conn, "  be modest \n")
    assert profile_store.get_ai_notes(conn) == "be modest"


def test_grading_prompt_injects_notes_and_omits_when_empty():
    with_notes = build_prompt("resume", "posting", "Co", "Role", "Never state my salary.")
    assert "Notes from the candidate:" in with_notes and "Never state my salary." in with_notes
    plain = build_prompt("resume", "posting", "Co", "Role", "")
    assert "Notes from the candidate" not in plain
    assert "benchmark" not in plain and "funding" not in plain


def _writer_prompt(notes=""):
    from test_tailor_agents import all_responses, matches, reqs

    client = FakeGemini(all_responses())
    agents.write(client, Settings(gemini_api_key="k"), sample_resume(), reqs(), matches(), notes=notes)
    return client.calls[0]["contents"]


def test_writer_prompt_has_notes_only_when_given():
    assert "Never state my salary." in _writer_prompt("Never state my salary.")
    assert "Notes from the candidate" not in _writer_prompt("")


def test_writer_prompt_has_no_her_or_she():
    import re

    from test_tailor_agents import all_responses, matches, reqs

    from jobhunt.tailor.agents import Draft

    plain = _writer_prompt()
    client = FakeGemini(all_responses())
    agents.write(client, Settings(gemini_api_key="k"), sample_resume(), reqs(), matches(),
                 previous=all_responses()[Draft], instructions=["shorter"])
    revised = client.calls[0]["contents"]
    assert "THE CANDIDATE'S CHANGE REQUESTS" in revised
    for text in (plain, revised):
        assert not re.search(r"\b(her|she)\b", text, re.IGNORECASE)


# --- web ------------------------------------------------------------------------------------

@pytest.fixture
def web(tmp_path):
    s = Settings(db_path=tmp_path / "data" / "w.db", env_path=tmp_path / ".env",
                 resume_output_dir=tmp_path / "out")
    with TestClient(create_app(s), base_url="http://127.0.0.1") as c:
        yield c, s


def test_profile_page_and_output_notes_forms(web):
    c, s = web
    page = c.get("/profile")
    assert page.status_code == 200
    assert "Resume output folder" in page.text and "Notes for the AI" in page.text
    r = c.post("/profile/output", data={"output_dir": "~/somewhere"}, follow_redirects=False)
    assert r.status_code == 303
    r = c.post("/profile/notes", data={"notes": "Say building."}, follow_redirects=False)
    assert r.status_code == 303
    conn = db.connect(s.db_path)
    assert profile_store.effective_output_dir(conn, s) == Path("~/somewhere").expanduser()
    assert profile_store.get_ai_notes(conn) == "Say building."
    conn.close()
    page = c.get("/profile")
    assert "Say building." in page.text and str(Path("~/somewhere").expanduser()) in page.text


def test_web_import_without_folder_flashes(web):
    c, _ = web
    r = c.post("/import", follow_redirects=False)
    assert r.status_code == 303
    assert parse_qs(urlsplit(r.headers["location"]).query)["msg"][0] == \
        "No Proficiently folder set (PROFICIENTLY_DIR)."


def test_cli_import_without_folder(capsys):
    assert main(["import"]) == 1
    assert "No Proficiently folder set (PROFICIENTLY_DIR)." in capsys.readouterr().out


# --- migrate-profile -----------------------------------------------------------------------

@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    resume = h / "Documents" / "Resume"
    (resume / ".proficiently").mkdir(parents=True)
    render_master_layout(sample_resume(), resume / "Georgia Liu - Master Resume.pdf")
    (resume / ".proficiently" / "preferences.md").write_text(
        "## Compensation\n- Floor: $200,000\n## Locations\nBoston\n")
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    return h


def test_migrate_profile_carries_over_and_is_idempotent(home, capsys):
    assert main(["migrate-profile"]) == 0
    out = capsys.readouterr().out
    assert "MASTER_RESUME_PATH" in out and "preferences" not in out

    s = load_settings()
    assert s.master_resume_path == home / "Documents/Resume/Georgia Liu - Master Resume.pdf"
    assert s.resume_output_dir == home / "Documents/Resume"
    assert s.proficiently_dir == home / "Documents/Resume/.proficiently"
    conn = db.connect(s.db_path)
    assert profile_store.get_preferences(conn) is None  # no frozen snapshot: the live folder is used
    prefs = profile_store.effective_preferences(conn, s)
    assert prefs == load_preferences(home / "Documents/Resume/.proficiently")
    assert prefs.salary_floor == 200000 and prefs.locations == ["Boston"]
    assert profile_store.get_ai_notes(conn) == OLD_AI_NOTES
    assert profile_store.get(conn, "setup_done") is True
    conn.close()

    assert main(["migrate-profile"]) == 0
    assert capsys.readouterr().out.strip() == "nothing to change"
    # never wrote inside the legacy folder
    names = {p.name for p in (home / "Documents/Resume").iterdir()}
    assert names == {"Georgia Liu - Master Resume.pdf", ".proficiently"}


def test_migrate_profile_keeps_existing_notes_and_env_values(home, tmp_path, capsys):
    Path(".env").write_text(f"MASTER_RESUME_PATH={tmp_path}/mine.pdf\n")
    s = load_settings()
    s.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(s.db_path)
    profile_store.save_ai_notes(conn, "my own notes")
    conn.close()
    assert main(["migrate-profile"]) == 0
    s = load_settings()
    assert s.master_resume_path == tmp_path / "mine.pdf"
    conn = db.connect(s.db_path)
    assert profile_store.get_ai_notes(conn) == "my own notes"
    conn.close()


# --- review fixes ---------------------------------------------------------------------------

def test_migrate_fills_keys_present_but_empty(home):
    Path(".env").write_text("PROFICIENTLY_DIR=\nMASTER_RESUME_PATH=\n")
    assert main(["migrate-profile"]) == 0
    s = load_settings()
    assert s.proficiently_dir == home / "Documents/Resume/.proficiently"
    assert s.master_resume_path == home / "Documents/Resume/Georgia Liu - Master Resume.pdf"


def test_migrate_saves_file_name_unless_set(home):
    assert main(["migrate-profile"]) == 0
    s = load_settings()
    conn = db.connect(s.db_path)
    assert profile_store.get_file_name(conn) == "Georgia Liu"
    conn.close()


def test_file_name_helpers(conn):
    from jobhunt.tailor.output import file_base_name

    assert profile_store.get_file_name(conn) is None
    profile_store.save_file_name(conn, "  Jane D  ")
    assert profile_store.get_file_name(conn) == "Jane D"
    profile_store.save_file_name(conn, "")
    assert profile_store.get_file_name(conn) is None
    assert file_base_name("Jane Doe, PhD, MBA") == "Jane Doe - Resume"
    assert file_base_name("Jane Doe") == "Jane Doe - Resume"
    assert file_base_name("", saved="Georgia Liu") == "Georgia Liu - Resume"
    assert file_base_name("Jane Doe, PhD", saved="Georgia Liu") == "Georgia Liu - Resume"
    assert file_base_name(None) == "Resume"


def test_tailor_uses_saved_file_name(conn, tmp_path):
    from test_tailor_job import add_job, fake

    from jobhunt.tailor import tailor_job

    pdf = tmp_path / "m.pdf"
    render_master_layout(sample_resume(), pdf)
    s = Settings(db_path=tmp_path / "t.db", master_resume_path=pdf,
                 resume_output_dir=tmp_path / "out", gemini_api_key="k")
    res = tailor_job(conn, add_job(conn), s, client=fake())
    assert Path(res.path).name == "Alex Q. Sample - Resume.pdf"
    profile_store.save_file_name(conn, "Georgia Liu")
    res = tailor_job(conn, add_job(conn, 2, company="Other"), s, client=fake())
    assert Path(res.path).name == "Georgia Liu - Resume.pdf"


def test_profile_file_name_field_and_relative_output_rejected(web):
    c, s = web
    assert "Name on resume files" in c.get("/profile").text
    r = c.post("/profile/output", data={"output_dir": "relative/dir", "file_name": "Jane D"},
               follow_redirects=False)
    assert r.status_code == 303
    msg = parse_qs(urlsplit(r.headers["location"]).query)["msg"][0]
    assert "full path" in msg.lower()
    conn = db.connect(s.db_path)
    assert profile_store.effective_output_dir(conn, s) == s.resume_output_dir
    assert profile_store.get_file_name(conn) is None  # nothing saved on rejection
    conn.close()
    r = c.post("/profile/output", data={"output_dir": "~/ok", "file_name": "Jane D"},
               follow_redirects=False)
    conn = db.connect(s.db_path)
    assert profile_store.get_file_name(conn) == "Jane D"
    conn.close()


# --- README claims match the code ----------------------------------------------------------------

def test_readme_settings_and_import_claims():
    text = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    rows = {line.split("|")[1].strip(" `"): line for line in text.splitlines() if line.startswith("| `")}
    assert "only when no resume has been uploaded" in rows["MASTER_RESUME_PATH"]
    assert "Profile" in rows["RESUME_OUTPUT_DIR"] and "wizard" in rows["RESUME_OUTPUT_DIR"]
    assert "salary floor are dropped" not in text and "plus your preferences" not in text
    assert "output folder, preferences" not in text  # migrate-profile no longer copies preferences
