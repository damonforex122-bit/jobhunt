import stat
from pathlib import Path

from jobhunt import profile_store
from jobhunt.config import Settings, load_settings, reload_settings
from jobhunt.envfile import mask, update_env
from jobhunt.models import Preferences


def test_update_env_preserves_other_lines_and_sets_600(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# comment\nA=1\n\nGEMINI_API_KEY=old\nB=2\n")
    update_env(p, {"GEMINI_API_KEY": "new", "EXA_API_KEY": "x"})
    assert p.read_text() == "# comment\nA=1\n\nGEMINI_API_KEY=new\nB=2\nEXA_API_KEY=x\n"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_update_env_creates_missing_file_600(tmp_path):
    p = tmp_path / "sub" / ".env"
    update_env(p, {"A": "1"})
    assert p.read_text() == "A=1\n"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_update_env_none_removes_key(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# c\nA=1\nB=2\n")
    update_env(p, {"A": None, "MISSING": None})
    assert p.read_text() == "# c\nB=2\n"


def test_mask():
    assert mask("abcdef1234") == "••••1234"
    assert mask(None) == ""
    assert mask("") == ""


def test_reload_settings_updates_same_instance(tmp_path, monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.delenv("DB_PATH", raising=False)
    env = tmp_path / ".env"
    env.write_text("A=1\n")
    s = load_settings(env)
    assert s.env_path == env
    assert s.exa_api_key is None
    update_env(env, {"EXA_API_KEY": "k1234", "DB_PATH": "~/x.db"})
    reload_settings(s)
    assert s.exa_api_key == "k1234"
    assert s.db_path == Path("~/x.db").expanduser()
    assert s.env_path == env
    update_env(env, {"EXA_API_KEY": None})
    reload_settings(s)
    assert s.exa_api_key is None


def test_saved_preferences_win_and_empty_lists_stay_empty(conn, tmp_path):
    s = Settings(proficiently_dir=tmp_path)
    assert profile_store.get_preferences(conn) is None
    prefs = Preferences(locations=[], salary_floor=1, search_terms=[], excluded_terms=[])
    profile_store.save_preferences(conn, prefs)
    assert profile_store.get_preferences(conn) == prefs
    assert profile_store.effective_preferences(conn, s) == prefs


def test_effective_preferences_falls_back_to_loader_when_unsaved(conn, tmp_path):
    (tmp_path / "preferences.md").write_text("## Compensation\n\n$250k base\n")
    s = Settings(proficiently_dir=tmp_path)
    assert profile_store.effective_preferences(conn, s).salary_floor == 250000


def test_effective_master_path_prefers_upload(conn, tmp_path):
    s = Settings(master_resume_path=tmp_path / "m.pdf")
    assert profile_store.get_master(conn) is None
    assert profile_store.effective_master_path(conn, s) == tmp_path / "m.pdf"
    info = profile_store.MasterInfo(path=str(tmp_path / "up.pdf"), sha256="ab", name="r.pdf",
                                    uploaded_at="2026-11-05")
    profile_store.save_master(conn, info)
    assert profile_store.get_master(conn) == info
    assert profile_store.effective_master_path(conn, s) == tmp_path / "up.pdf"


def test_links_roundtrip(conn):
    assert profile_store.get_links(conn) == profile_store.Links()
    links = profile_store.Links(website="https://a.io", github="gh", linkedin="li")
    profile_store.save_links(conn, links)
    assert profile_store.get_links(conn) == links


def test_get_put(conn):
    assert profile_store.get(conn, "k", 5) == 5
    profile_store.put(conn, "k", {"a": [1]})
    profile_store.put(conn, "k", {"a": [2]})
    assert profile_store.get(conn, "k") == {"a": [2]}


import pytest


@pytest.mark.parametrize("key,value", [
    ("EXA_API_KEY", "k APOLLO_API_KEY=evil"),
    ("EXA_API_KEY", "k x"),
    ("EXA_API_KEY", "k\x85x"),
    ("EXA_API_KEY", "k\x0bAPOLLO_API_KEY=evil"),
    ("EXA_API_KEY", "k\x1cx"),
    ("EXA_API_KEY", "a\nb"),
    ("EXA_API_KEY", '"quoted'),
    ("EXA_API_KEY", " lead"),
    ("EXA_API_KEY", "trail "),
    ("A=B", "x"),
    ("lower", "x"),
])
def test_update_env_rejects_unsafe_and_leaves_file_unchanged(tmp_path, key, value):
    p = tmp_path / ".env"
    p.write_text("A=1\n")
    with pytest.raises(ValueError):
        update_env(p, {key: value})
    assert p.read_text() == "A=1\n"
    assert [f.name for f in tmp_path.iterdir()] == [".env"]


def test_update_env_tightens_existing_644_without_temp_leftovers(tmp_path):
    p = tmp_path / ".env"
    p.write_text("A=1\n")
    p.chmod(0o644)
    update_env(p, {"EXA_API_KEY": "secret"})
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert [f.name for f in tmp_path.iterdir()] == [".env"]
