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


def test_description_route_requires_technical_title():
    desc = "We build evals and benchmarks."
    assert filters.title_match("Marketing Lead", desc, PREFS) is False
    assert filters.title_match("Recruiter", desc, PREFS) is False
    assert filters.title_match("Software Engineer", desc, PREFS) is True


def test_excluded_term_flagged_not_dropped():
    keep, flags = filters.evaluate(_job(title="Research Engineer, RL Evals"), PREFS)
    assert keep and "excluded_term" in flags
    keep, flags = filters.evaluate(_job(title="Research Engineer, Control Evals"), PREFS)
    assert keep and "excluded_term" not in flags
    keep, flags = filters.evaluate(_job(title="Research Engineer, URL Evals"), PREFS)
    assert "excluded_term" not in flags


def test_non_usd_skips_floor():
    keep, flags = filters.evaluate(_job(salary_min=50000, salary_max=90000, flags=["non_usd"]), PREFS)
    assert keep and flags == ["non_usd"]


@pytest.mark.parametrize("title", [
    "Senior Technical Recruiter - Engineering",
    "Recruiter (Research)",
    "Strategic Sourcing Lead, Professional Services – Human Data",
    "Senior Product Marketing Manager, Labs & Research",
    "Senior IT Platform and Automation Engineer",
    "Account Executive, Evals Platform",
])
def test_non_technical_titles_dropped_even_with_matching_terms(title):
    desc = "We build evals and benchmarks with human data."
    assert filters.title_match(title, desc, PREFS) is False


@pytest.mark.parametrize("title", [
    "Data Operations Manager, Human Data",
    "Research Engineer, Benchmarks & Evaluation",
    "Software Engineer, Research - Human Data",
    "Staff Software Engineer, GTM AI Engineering",
    "Member of Technical Staff - Evals Infra",
])
def test_technical_titles_with_lookalike_words_still_match(title):
    assert filters.title_match(title, "We build evals and benchmarks.", PREFS) is True
