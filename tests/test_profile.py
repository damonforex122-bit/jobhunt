from pathlib import Path

from jobhunt.profile import load_preferences, load_resume_text

FIX = Path(__file__).parent / "fixtures" / "proficiently"


def test_load_preferences():
    p = load_preferences(FIX)
    assert p.salary_floor == 180000
    assert "New York" in p.locations and "San Francisco" in p.locations
    assert "post-training" in p.excluded_terms and "RL" in p.excluded_terms
    assert "evals" in p.search_terms and "human data" in p.search_terms
    assert p.target_roles


def test_load_preferences_falls_back(tmp_path):
    p = load_preferences(tmp_path)
    assert p.salary_floor == 180000
    assert p.locations == ["San Francisco", "New York"]
    assert "structured extraction" in p.search_terms
    assert "frontier lab" in p.excluded_terms


def test_load_resume_text(tmp_path):
    (tmp_path / "profile.md").write_text("PROFILE")
    assert load_resume_text(tmp_path) == "PROFILE"
    assert load_resume_text(FIX).startswith("#")
    assert load_resume_text(tmp_path / "nope") == ""
