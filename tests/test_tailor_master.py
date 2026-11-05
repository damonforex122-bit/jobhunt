import pytest
from tailor_fakes import sample_resume

from jobhunt import db
from jobhunt.config import Settings
from jobhunt.tailor import master
from jobhunt.tailor.master import MasterError, load_master, parse_master, verify
from jobhunt.tailor.render import render_pdf


def _pdf_text(path) -> str:
    return master._pdf_text(path)


def test_parse_roundtrip(tmp_path):
    p = tmp_path / "m.pdf"
    render_pdf(sample_resume(), p)
    assert parse_master(p) == sample_resume()


def test_verify_flags_missing_text(tmp_path):
    p = tmp_path / "m.pdf"
    render_pdf(sample_resume(), p)
    r = sample_resume()
    r.experience[0].bullets[1].text = "Invented line"
    assert verify(r, _pdf_text(p)) == ["Invented line"]
    assert verify(sample_resume(), _pdf_text(p)) == []


def test_load_master_caches_by_hash(conn, tmp_path, monkeypatch):
    p = tmp_path / "m.pdf"
    render_pdf(sample_resume(), p)
    first = load_master(conn, p)
    assert first == sample_resume()

    def boom(_):
        raise AssertionError("should have used the cache")

    real = master.parse_master
    monkeypatch.setattr(master, "parse_master", boom)
    assert load_master(conn, p) == first

    calls = []
    monkeypatch.setattr(master, "parse_master", lambda x: calls.append(x) or real(x))
    changed = sample_resume()
    changed.experience[1].bullets[0].text = "Trained a different model that lifted retention by 5 points."
    render_pdf(changed, p)
    assert load_master(conn, p) == changed
    assert len(calls) == 1


def test_load_master_missing_file(conn, tmp_path):
    p = tmp_path / "nope.pdf"
    with pytest.raises(MasterError) as e:
        load_master(conn, p)
    assert str(p) in str(e.value)


def test_load_master_unverifiable(conn, tmp_path, monkeypatch):
    p = tmp_path / "m.pdf"
    render_pdf(sample_resume(), p)
    bad = sample_resume()
    bad.experience[0].bullets[0].text = "A fabricated bullet."
    monkeypatch.setattr(master, "parse_master", lambda _: bad)
    with pytest.raises(MasterError) as e:
        load_master(conn, p)
    assert str(e.value).startswith("Couldn't read the master resume reliably")
    assert "\n" not in str(e.value)
    assert conn.execute("SELECT COUNT(*) FROM master_cache").fetchone()[0] == 0


def test_settings_expand_home():
    s = Settings()
    assert "~" not in str(s.master_resume_path) and "~" not in str(s.resume_output_dir)
    assert s.master_resume_path.name == "Georgia Liu - Master Resume.pdf"
    assert db.get_master_cache is not None
