from jobhunt.config import load_settings


def test_env_overrides_dotenv_and_empty_is_unset(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# c\nGEMINI_API_KEY=fromfile\nGEMINI_MODEL=\nEXA_API_KEY='q'\n")
    monkeypatch.setenv("GEMINI_API_KEY", "fromenv")
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    s = load_settings(env)
    assert s.gemini_api_key == "fromenv"
    assert s.gemini_model == "gemini-2.5-flash"
    assert s.exa_api_key == "q"
    assert s.apollo_api_key is None


def test_paths_are_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PROFICIENTLY_DIR", "~/prof")
    monkeypatch.setenv("DB_PATH", "~/x.db")
    s = load_settings(tmp_path / "none")
    assert s.proficiently_dir == tmp_path / "prof" and s.db_path == tmp_path / "x.db"
