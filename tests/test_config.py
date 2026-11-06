from jobhunt.config import load_settings


def test_env_overrides_dotenv_and_empty_is_unset(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# c\nGEMINI_API_KEY=fromfile\nGEMINI_MODEL=\nEXA_API_KEY='q'\n")
    monkeypatch.setenv("GEMINI_API_KEY", "fromenv")
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    s = load_settings(env)
    assert s.gemini_api_key == "fromenv"
    assert s.gemini_model == "gemini-3.5-flash-lite"
    assert s.exa_api_key == "q"
    assert s.apollo_api_key is None


def test_paths_are_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PROFICIENTLY_DIR", "~/prof")
    monkeypatch.setenv("DB_PATH", "~/x.db")
    s = load_settings(tmp_path / "none")
    assert s.proficiently_dir == tmp_path / "prof" and s.db_path == tmp_path / "x.db"


def test_env_path_in_the_environment_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("ENV_PATH", str(tmp_path / "other.env"))
    s = load_settings(tmp_path / ".env")
    assert s.env_path == tmp_path / ".env"


def test_env_example_has_no_env_path():
    from pathlib import Path
    example = Path(__file__).resolve().parents[1] / ".env.example"
    assert "ENV_PATH" not in example.read_text()


def test_tailor_imports_file_base_name_once():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "src/jobhunt/tailor/__init__.py").read_text()
    assert src.count("file_base_name,") + src.count("file_base_name)") <= 1
