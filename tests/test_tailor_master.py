import pytest
from master_layout import render_master_layout as render_pdf
from tailor_fakes import sample_resume

from jobhunt import db
from jobhunt.config import Settings
from jobhunt.tailor import master
from jobhunt.tailor.master import MasterError, load_master, parse_master, verify


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
    assert "\n" not in str(e.value) and str(p) in str(e.value)
    assert conn.execute("SELECT COUNT(*) FROM master_cache").fetchone()[0] == 0


def _rendered(tmp_path):
    p = tmp_path / "m.pdf"
    render_pdf(sample_resume(), p)
    return _pdf_text(p)


def test_verify_flags_dropped_bullet(tmp_path):
    text = _rendered(tmp_path)
    r = sample_resume()
    del r.experience[0].bullets[2]
    assert verify(r, text)


def test_verify_flags_dropped_entry_and_skill_item(tmp_path):
    text = _rendered(tmp_path)
    r = sample_resume()
    del r.experience[1]
    assert verify(r, text)
    r = sample_resume()
    r.skills[0].items.pop()
    assert verify(r, text)


def test_verify_flags_bullet_moved_to_other_entry(tmp_path):
    text = _rendered(tmp_path)
    r = sample_resume()
    moved = r.experience[0].bullets.pop(1)
    r.experience[1].bullets.append(moved)
    assert verify(r, text)


def test_verify_flags_skill_item_moved_to_other_category(tmp_path):
    text = _rendered(tmp_path)
    r = sample_resume()
    r.skills[1].items.append(r.skills[0].items.pop())
    assert verify(r, text)


def test_verify_flags_swapped_title_and_dates(tmp_path):
    text = _rendered(tmp_path)
    r = sample_resume()
    e = r.experience[0]
    e.title, e.dates = e.dates, e.title
    assert verify(r, text)


def test_load_master_ignores_cache_from_older_parser(conn, tmp_path, monkeypatch):
    p = tmp_path / "m.pdf"
    render_pdf(sample_resume(), p)
    stale = sample_resume()
    stale.summary = "stale cached parse"
    sha = master.master_sha256(p)
    db.save_master_cache(conn, f"{sha}:v{master.PARSER_VERSION - 1}", stale.model_dump_json())
    assert load_master(conn, p) == sample_resume()
    monkeypatch.setattr(master, "PARSER_VERSION", master.PARSER_VERSION + 1)
    assert load_master(conn, p) == sample_resume()  # reparsed under the new version


def test_settings_expand_home():
    s = Settings(master_resume_path="~/x", resume_output_dir="~/y")
    assert "~" not in str(s.master_resume_path) and s.master_resume_path.name == "x"
    assert "~" not in str(s.resume_output_dir) and s.resume_output_dir.name == "y"


def test_load_master_with_sha_returns_the_key_hash(conn, tmp_path):
    p = tmp_path / "m.pdf"
    render_pdf(sample_resume(), p)
    resume, sha = master.load_master_with_sha(conn, p)
    assert resume == sample_resume() and sha == master.master_sha256(p)
