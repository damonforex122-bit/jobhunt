import pytest

from jobhunt import filters
from jobhunt.models import Job, Preferences

PREFS = Preferences(
    locations=["San Francisco", "New York"],
    salary_floor=180000,
    search_terms=["evals", "benchmarks", "human data", "structured extraction", "applied research"],
    excluded_terms=["post-training", "RL", "reinforcement learning", "frontier lab"],
    target_roles=["Applied scientist (evals, structured extraction)", "LLM post-training roles"],
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("$150K – $250K", (150000, 250000)),
        ("$180,000 - $220,000 USD", (180000, 220000)),
        ("150-250k", (150000, 250000)),
        ("$200K", (200000, 200000)),
        ("$150K — $250K • Offers Equity", (150000, 250000)),
        ("$150K to $250K", (150000, 250000)),
        ("competitive", (None, None)),
        ("5 years experience", (None, None)),
        (None, (None, None)),
    ],
)
def test_parse_salary(text, expected):
    assert filters.parse_salary(text) == expected


@pytest.mark.parametrize(
    "loc,expected",
    [
        ("San Francisco, CA; New York, NY", "match"),
        ("SF / NYC / Remote", "match"),
        ("Remote - US", "remote"),
        ("", "unknown"),
        (None, "unknown"),
        ("London", "no"),
        ("London or Remote", "remote"),
        ("New York City", "match"),
    ],
)
def test_location_match(loc, expected):
    assert filters.location_match(loc, PREFS.locations) == expected


def _job(**kw):
    base = dict(company="X", title="Research Engineer, Evals", url="https://x.com/1",
                location="San Francisco, CA", source="ashby")
    return Job(**{**base, **kw})


def test_evaluate_salary_rules():
    keep, flags = filters.evaluate(_job(salary_min=100000, salary_max=170000), PREFS)
    assert keep is False
    keep, flags = filters.evaluate(_job(salary_min=150000, salary_max=250000), PREFS)
    assert keep and flags == ["below_floor_min"]
    keep, flags = filters.evaluate(_job(), PREFS)
    assert keep and flags == []


def test_evaluate_location_flags_and_drop():
    assert filters.evaluate(_job(location="London"), PREFS) == (False, [])
    assert filters.evaluate(_job(location=None), PREFS) == (True, ["location_unknown"])
    assert filters.evaluate(_job(location="Remote - US"), PREFS) == (True, ["remote"])


def test_title_match_ignores_excluded_terms():
    assert filters.title_match("Post-Training RL Engineer", "", PREFS) is False
    assert filters.title_match("Post-Training RL Engineer", None, PREFS) is False
    assert filters.title_match("Research Engineer, Evals", "", PREFS) is True
    assert filters.title_match("Eval Lead", None, PREFS) is True
    assert filters.title_match("Applied Scientist", None, PREFS) is True
    assert filters.title_match("Software Engineer", "We build benchmarks and run evals.", PREFS) is True
    assert filters.title_match("Software Engineer", "We run evals.", PREFS) is False
