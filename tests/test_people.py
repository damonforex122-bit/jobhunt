import json

import httpx
import respx

from jobhunt import db
from jobhunt.config import Settings
from jobhunt.models import Company, Job
from jobhunt.people import apollo, exa, finder, posting

APOLLO = "https://api.apollo.io/api/v1/mixed_people/api_search"
EXA = "https://api.exa.ai/search"
URL = "https://boards.example.com/acme/1"


def test_extract_reports_to():
    ps = posting.extract_people("This role reports to the Head of Research.", URL, "Acme")
    assert len(ps) == 1
    assert ps[0].title == "Head of Research"
    assert ps[0].name == "Head of Research (unnamed)"
    assert ps[0].confidence == 0.3
    assert ps[0].source == "posting" and ps[0].source_url == URL


def test_extract_named_partner():
    ps = posting.extract_people("You'll work closely with Jane Doe, our Evals Lead.", URL, "Acme")
    assert [(p.name, p.title) for p in ps] == [("Jane Doe", "Evals Lead")]


def test_extract_hiring_manager_and_led_by():
    ps = posting.extract_people("Hiring manager: Sam Lee. The team is led by Ana Ruiz.", URL, "Acme")
    assert {p.name for p in ps} == {"Sam Lee", "Ana Ruiz"}


def test_extract_none():
    assert posting.extract_people("We build things. Apply today.", URL, "Acme") == []


def test_manager_titles_evals():
    assert "Head of Evals" in posting.manager_titles("Research Engineer, Evals")
    assert posting.manager_titles("Office Chef") == [
        "Head of Research", "Engineering Manager", "CTO", "Co-founder"
    ]


@respx.mock
def test_apollo_maps_people():
    route = respx.post(APOLLO).mock(return_value=httpx.Response(200, json={"people": [
        {"id": "a1", "first_name": "Jane", "last_name": "Doe", "title": "Head of Evals"},
        {"id": "a2", "first_name": "Bob", "last_name": "", "last_name_obfuscated": "Sm***h"},
    ]}))
    co = Company(name="Acme", domain="acme.com")
    with httpx.Client() as c:
        ps = apollo.search_people(co, ["Head of Evals"], "k", c)
    assert len(ps) == 1
    p = ps[0]
    assert p.source == "apollo" and p.name == "Jane Doe"
    assert p.source_url == "https://app.apollo.io/#/people/a1"
    req = route.calls[0].request
    assert "acme.com" in str(req.url) + req.content.decode()
    assert req.headers["x-api-key"] == "k"


@respx.mock
def test_exa_filters_by_company():
    respx.post(EXA).mock(return_value=httpx.Response(200, json={"results": [
        {"title": "Jane Doe - Head of Evals - Acme | LinkedIn", "url": "https://www.linkedin.com/in/jane"},
        {"title": "Joe Roe - Research Lead - Acme Corp | LinkedIn", "url": "https://linkedin.com/in/joe"},
        {"title": "Max Poe - Lead - Other Inc | LinkedIn", "url": "https://linkedin.com/in/max"},
    ]}))
    with httpx.Client() as c:
        ps = exa.search_people("Acme", ["Head of Evals"], "k", c)
    assert [p.name for p in ps] == ["Jane Doe", "Joe Roe"]
    assert ps[0].linkedin_url == "https://www.linkedin.com/in/jane"
    assert ps[0].title == "Head of Evals" and ps[0].confidence == 0.5


def _job(conn, desc="Reports to the Head of Research."):
    db.upsert_company(conn, Company(name="Acme", domain="acme.com"))
    jid, _ = db.upsert_job(conn, Job(company="Acme", title="Research Engineer, Evals",
                                     url=URL, description=desc, source="greenhouse"))
    return jid


def test_find_people_skips_missing_keys(conn):
    jid = _job(conn)
    rep = finder.find_people(conn, jid, Settings())
    assert [p.source for p in rep.people] == ["posting"]
    assert "not set" in rep.skipped["apollo"] and "not set" in rep.skipped["exa"]


@respx.mock
def test_find_people_apollo_500_still_runs_exa(conn):
    jid = _job(conn)
    respx.post(APOLLO).mock(return_value=httpx.Response(500))
    respx.post(EXA).mock(return_value=httpx.Response(200, json={"results": [
        {"title": "Jane Doe - Head of Evals - Acme | LinkedIn", "url": "https://linkedin.com/in/jane"}]}))
    rep = finder.find_people(conn, jid, Settings(apollo_api_key="SECRET1", exa_api_key="SECRET2"))
    assert "500" in rep.skipped["apollo"] and "SECRET" not in rep.skipped["apollo"]
    assert "exa" not in rep.skipped
    assert [p.name for p in rep.people] == ["Jane Doe", "Head of Research (unnamed)"]
    assert all(p.source_url for p in rep.people)
