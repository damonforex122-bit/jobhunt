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
