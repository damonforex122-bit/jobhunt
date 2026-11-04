import json
import re
from pathlib import Path

import httpx
import pytest
import respx

from jobhunt.models import Company
from jobhunt.sources import ats

FIX = Path(__file__).parent / "fixtures" / "ats"


def _fx(name):
    return json.loads((FIX / f"{name}.json").read_text())


def _client():
    return httpx.Client(timeout=20.0, headers={"User-Agent": "jobhunt/0.1"})


@respx.mock
def test_greenhouse_parses():
    respx.get("https://boards-api.greenhouse.io/v1/boards/anthropic/jobs").respond(json=_fx("greenhouse"))
    co = Company(name="Anthropic", ats="greenhouse", slug="anthropic")
    jobs = ats.fetch_jobs(co, _client())
    assert len(jobs) == 3
    for j in jobs:
        assert j.source == "greenhouse"
        assert j.company == "Anthropic"
        assert j.url.startswith("https://")
        assert j.description and "<" not in j.description
        assert "&lt;" not in j.description
    assert jobs[0].location
    assert jobs[0].posted_at


@respx.mock
def test_ashby_parses_secondary_locations():
    data = _fx("ashby")
    respx.get("https://api.ashbyhq.com/posting-api/job-board/harvey").respond(json=data)
    co = Company(name="Harvey", ats="ashby", slug="harvey")
    jobs = ats.fetch_jobs(co, _client())
    assert len(jobs) == 3 and all(j.source == "ashby" for j in jobs)
    multi = [j for j in jobs if "; " in (j.location or "")]
    assert multi
    summary = data["jobs"][0]["compensation"]["compensationTierSummary"]
    assert f"Compensation: {summary}" in jobs[0].description
    assert "Compensation:" not in jobs[2].description


@respx.mock
def test_lever_parses_salary_range():
    respx.get("https://api.lever.co/v0/postings/weride").respond(json=_fx("lever"))
    co = Company(name="WeRide", ats="lever", slug="weride")
    jobs = ats.fetch_jobs(co, _client())
    assert len(jobs) == 3
    assert jobs[0].salary_min == 120000 and jobs[0].salary_max == 165000
    assert jobs[2].salary_min is None
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", jobs[0].posted_at)


@respx.mock
def test_fetch_404_raises_fetcherror():
    respx.get("https://api.ashbyhq.com/posting-api/job-board/nope").respond(404)
    co = Company(name="Nope", ats="ashby", slug="nope")
    with pytest.raises(ats.FetchError) as e:
        ats.fetch_jobs(co, _client())
    assert e.value.company == "Nope"
    assert "404" in e.value.reason and "Nope" in str(e.value)


@respx.mock
def test_fetch_timeout_raises_fetcherror():
    respx.get("https://api.lever.co/v0/postings/slow").mock(side_effect=httpx.ReadTimeout("t"))
    co = Company(name="Slow", ats="lever", slug="slow")
    with pytest.raises(ats.FetchError) as e:
        ats.fetch_jobs(co, _client())
    assert "timeout" in e.value.reason.lower()


@respx.mock
def test_fetch_bad_json_raises_fetcherror():
    respx.get("https://api.lever.co/v0/postings/bad").respond(200, text="<html>")
    with pytest.raises(ats.FetchError):
        ats.fetch_jobs(Company(name="Bad", ats="lever", slug="bad"), _client())


def test_load_watchlist_missing_file_returns_empty(tmp_path):
    assert ats.load_watchlist(tmp_path / "nope.yaml") == []


def test_load_watchlist_seed():
    cos = ats.load_watchlist(Path(__file__).parent.parent / "config" / "watchlist.yaml")
    assert [c.name for c in cos] == ["Anthropic", "Harvey", "Mercor"]
    assert cos[0].ats == "greenhouse" and cos[0].domain == "anthropic.com"


@respx.mock
def test_lever_non_usd_flag():
    data = _fx("lever")
    data[0]["salaryRange"]["currency"] = "GBP"
    respx.get("https://api.lever.co/v0/postings/weride").respond(json=data)
    jobs = ats.fetch_jobs(Company(name="WeRide", ats="lever", slug="weride"), _client())
    assert jobs[0].flags == ["non_usd"]
    assert jobs[1].flags == []


@respx.mock
@pytest.mark.parametrize("ats_name,url,payload", [
    ("greenhouse", "https://boards-api.greenhouse.io/v1/boards/x/jobs",
     {"jobs": [{"title": None, "absolute_url": "https://a.test/1"},
               {"title": "Good", "absolute_url": "https://a.test/2"}]}),
    ("ashby", "https://api.ashbyhq.com/posting-api/job-board/x",
     {"jobs": [{"title": None, "jobUrl": "https://a.test/1"},
               {"title": "Good", "jobUrl": "https://a.test/2"}]}),
    ("lever", "https://api.lever.co/v0/postings/x",
     [{"text": None, "hostedUrl": "https://a.test/1"},
      {"text": "Bad salary", "hostedUrl": "https://a.test/3", "salaryRange": {"min": "abc"}},
      {"text": "Good", "hostedUrl": "https://a.test/2"}]),
])
def test_bad_posting_is_skipped_not_fatal(ats_name, url, payload):
    respx.get(re.compile(re.escape(url))).respond(json=payload)
    stats = {}
    jobs = ats.fetch_jobs(Company(name="X", ats=ats_name, slug="x"), _client(), stats)
    assert [j.title for j in jobs] == ["Good"]
    assert stats["skipped"] == (2 if ats_name == "lever" else 1)


@respx.mock
def test_unknown_slug_404_and_unexpected_shape():
    respx.get("https://api.lever.co/v0/postings/nope").respond(404)
    with pytest.raises(ats.FetchError, match="HTTP 404"):
        ats.fetch_jobs(Company(name="X", ats="lever", slug="nope"), _client())
    respx.get("https://api.ashbyhq.com/posting-api/job-board/x").respond(json={"wrong": 1})
    with pytest.raises(ats.FetchError, match="unexpected response shape"):
        ats.fetch_jobs(Company(name="X", ats="ashby", slug="x"), _client())


@respx.mock
def test_titles_and_locations_are_unescaped_and_trimmed():
    data = {"jobs": [{"title": "  Research Scientist, Human Data &amp; Evaluation ",
                      "jobUrl": "https://jobs.ashbyhq.com/contra/1",
                      "location": "London &amp;  San Francisco", "descriptionPlain": "x"}]}
    respx.get("https://api.ashbyhq.com/posting-api/job-board/contra").respond(json=data)
    gh = {"jobs": [{"title": " Evals &amp; Benchmarks", "absolute_url": "https://x.test/1",
                    "location": {"name": "NY &amp; SF "}, "content": ""}]}
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").respond(json=gh)
    lv = [{"text": "Evals\u00a0Lead &amp; Co ", "hostedUrl": "https://jobs.lever.co/a/1",
           "categories": {"location": " New York "}}]
    respx.get("https://api.lever.co/v0/postings/lv").respond(json=lv)
    a = ats.fetch_jobs(Company(name="Contra", ats="ashby", slug="contra"), _client())[0]
    g = ats.fetch_jobs(Company(name="Acme", ats="greenhouse", slug="acme"), _client())[0]
    l = ats.fetch_jobs(Company(name="Lv", ats="lever", slug="lv"), _client())[0]
    assert (a.title, a.location) == ("Research Scientist, Human Data & Evaluation", "London & San Francisco")
    assert (g.title, g.location) == ("Evals & Benchmarks", "NY & SF")
    assert (l.title, l.location) == ("Evals Lead & Co", "New York")
