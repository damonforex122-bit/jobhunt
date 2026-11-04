import pytest

from jobhunt import db


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _isolate_from_real_files(tmp_path, monkeypatch):
    """Default settings paths (data/gmail_token.json, data/jobhunt.db, .env) are relative to the
    working directory. Run every test from an empty temp dir so they never reach real files."""
    monkeypatch.chdir(tmp_path)
