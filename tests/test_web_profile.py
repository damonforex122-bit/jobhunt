import hashlib
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from tailor_fakes import sample_resume

from jobhunt import db, profile_store
from jobhunt.config import KEY_FIELDS, Settings
from jobhunt.models import Preferences
from jobhunt.web.app import create_app
from master_layout import render_master_layout


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for f in KEY_FIELDS:
        monkeypatch.delenv(f.upper(), raising=False)


@pytest.fixture
def settings(tmp_path):
    prof = tmp_path / "prof"
    prof.mkdir()
    (prof / "preferences.md").write_text("")  # loader decides; see first-time test
    return Settings(db_path=tmp_path / "data" / "p.db", proficiently_dir=prof,
                    master_resume_path=tmp_path / "nomaster.pdf",
                    resume_output_dir=tmp_path / "out", env_path=tmp_path / ".env")


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as c:
        yield c


def conn_for(settings):
    return db.connect(settings.db_path)


def msg_of(r):
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith("/profile?msg=")
    return parse_qs(urlsplit(loc).query)["msg"][0]


def make_pdf(path, text="hello"):
    c = canvas.Canvas(str(path), pagesize=letter)
    c.drawString(72, 700, text)
    c.save()
    return path


def test_profile_page_prefills_preferences_from_loader_first_time(client, settings, monkeypatch):
    monkeypatch.setattr(profile_store, "load_preferences", lambda d: Preferences(
        locations=["Remote"], salary_floor=123000, search_terms=["evals"],
        excluded_terms=["intern"], target_roles=["Lead"]))
    r = client.get("/profile")
    assert r.status_code == 200
    for needle in ("Master resume", "Links", "Job preferences", "API keys", "123000", "Remote",
                   "evals", "intern", "Lead"):
        assert needle in r.text


def test_save_preferences_then_page_shows_saved(client, settings):
    r = client.post("/profile/preferences", data={
        "target_roles": "A, B", "search_terms": "x,  y ,", "excluded_terms": "z",
        "locations": "NYC, Remote", "salary_floor": "150000"}, follow_redirects=False)
    assert r.status_code == 303
    conn = conn_for(settings)
    p = profile_store.get_preferences(conn)
    conn.close()
    assert p.target_roles == ["A", "B"] and p.search_terms == ["x", "y"]
    assert p.locations == ["NYC", "Remote"] and p.salary_floor == 150000
    page = client.get("/profile").text
    assert "150000" in page and "NYC, Remote" in page


def test_save_links(client, settings):
    r = client.post("/profile/links", data={"website": "https://me.test", "github": "https://github.com/me",
                                            "linkedin": "https://linkedin.com/in/me"},
                    follow_redirects=False)
    assert r.status_code == 303
    conn = conn_for(settings)
    links = profile_store.get_links(conn)
    conn.close()
    assert links.github == "https://github.com/me" and links.website == "https://me.test"
    assert "https://linkedin.com/in/me" in client.get("/profile").text


def test_save_key_writes_env_masks_and_reloads(client, settings):
    r = client.post("/profile/keys", data={"gemini_api_key": "newkey1234"}, follow_redirects=False)
    assert r.status_code == 303
    assert "GEMINI_API_KEY=newkey1234" in settings.env_path.read_text()
    assert oct(settings.env_path.stat().st_mode & 0o777) == "0o600"
    page = client.get("/profile").text
    assert "••••1234" in page and "newkey1234" not in page
    assert settings.gemini_api_key == "newkey1234"
    assert "newkey1234" not in r.headers["location"]


def test_blank_key_keeps_existing_and_remove_clears(client, settings):
    client.post("/profile/keys", data={"gemini_api_key": "newkey1234", "exa_api_key": "exakey5678"})
    client.post("/profile/keys", data={"gemini_api_key": "", "exa_api_key": ""})
    text = settings.env_path.read_text()
    assert "GEMINI_API_KEY=newkey1234" in text and "EXA_API_KEY=exakey5678" in text
    client.post("/profile/keys", data={"remove_gemini_api_key": "on"})
    text = settings.env_path.read_text()
    assert "GEMINI_API_KEY" not in text and "EXA_API_KEY=exakey5678" in text
    assert settings.gemini_api_key is None and settings.exa_api_key == "exakey5678"


def test_bad_key_value_rejected_without_echo(client, settings):
    r = client.post("/profile/keys", data={"gemini_api_key": "bad key'x"}, follow_redirects=False)
    assert "can't be saved" in msg_of(r)
    assert "bad key" not in r.headers["location"]
    assert not settings.env_path.exists()


def test_shell_key_shows_override_note(client, settings, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "shell9999")
    settings.gemini_api_key = "shell9999"  # what load_settings did at startup
    page = client.get("/profile").text
    assert "set in your shell" in page and "••••9999" in page and "shell9999" not in page


def test_upload_valid_master_saves_under_data_and_activates(client, settings, tmp_path):
    src = tmp_path / "m.pdf"
    render_master_layout(sample_resume(), src)
    data = src.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    r = client.post("/profile/master", files={"file": ("My Master.pdf", data, "application/pdf")},
                    follow_redirects=False)
    assert r.status_code == 303
    dest = settings.db_path.parent / "master" / f"{sha}.pdf"
    assert dest.read_bytes() == data
    assert list(dest.parent.glob("*")) == [dest]
    conn = conn_for(settings)
    info = profile_store.get_master(conn)
    eff = profile_store.effective_master_path(conn, settings)
    conn.close()
    assert info.sha256 == sha and info.name == "My Master.pdf" and info.path == str(dest)
    assert eff == dest
    assert "My Master.pdf" in client.get("/profile").text


def test_upload_invalid_pdf_rejected_keeps_old_master(client, settings, tmp_path):
    src = tmp_path / "m.pdf"
    render_master_layout(sample_resume(), src)
    client.post("/profile/master", files={"file": ("a.pdf", src.read_bytes(), "application/pdf")})
    conn = conn_for(settings)
    before = profile_store.get_master(conn)
    conn.close()
    bad = make_pdf(tmp_path / "bad.pdf", "arbitrary text")
    r = client.post("/profile/master", files={"file": ("bad.pdf", bad.read_bytes(), "application/pdf")},
                    follow_redirects=False)
    assert "Couldn't read" in msg_of(r)
    conn = conn_for(settings)
    assert profile_store.get_master(conn) == before
    conn.close()
    assert len(list((settings.db_path.parent / "master").glob("*"))) == 1  # no temp leftovers


def test_upload_rejects_non_pdf_and_oversize(client, settings):
    r = client.post("/profile/master", files={"file": ("x.pdf", b"not a pdf", "application/pdf")},
                    follow_redirects=False)
    assert "PDF" in msg_of(r)
    big = b"%PDF-" + b"0" * (10 * 1024 * 1024)
    r = client.post("/profile/master", files={"file": ("x.pdf", big, "application/pdf")},
                    follow_redirects=False)
    assert "10 MB" in msg_of(r)
    conn = conn_for(settings)
    assert profile_store.get_master(conn) is None
    conn.close()


@pytest.mark.parametrize("path,data", [
    ("/profile/links", {"website": "x"}), ("/profile/preferences", {"salary_floor": "1"}),
    ("/profile/keys", {"gemini_api_key": "k1234"}),
])
def test_profile_posts_reject_cross_site(client, path, data):
    r = client.post(path, data=data, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    r = client.post("/profile/master", files={"file": ("a.pdf", b"%PDF-", "application/pdf")},
                    headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


def test_pasted_key_is_stripped(client, settings):
    client.post("/profile/keys", data={"gemini_api_key": "  newkey1234\n"})
    assert "GEMINI_API_KEY=newkey1234\n" in settings.env_path.read_text()
    assert settings.gemini_api_key == "newkey1234"
