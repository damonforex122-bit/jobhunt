import json

import httpx
import pytest
from tailor_fakes import FakeGemini

from jobhunt import cli, db
from jobhunt.config import Settings
from jobhunt.sources import by_link
from jobhunt.sources.by_link import ExtractedPosting, LinkError, add_job_by_link, fetch_posting
from jobhunt.tailor import TailorResult

PUBLIC = "93.184.216.34"
DESC = ("We are hiring an engineer to build evaluation pipelines for language models. You will own "
        "datasets, write Python services, partner with researchers and ship dashboards weekly. "
        "Requirements include strong SQL, experience with Postgres and clear written communication.")


@pytest.fixture(autouse=True)
def _no_dns(monkeypatch):
    hosts = {"careers.example.test": [PUBLIC], "evil.example.test": ["10.0.0.7"]}
    monkeypatch.setattr(by_link, "_resolve", lambda h: hosts.get(h, []))


@pytest.fixture
def settings(tmp_path):
    return Settings(db_path=tmp_path / "l.db", gemini_api_key="k")


def mock(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def extraction(**kw) -> FakeGemini:
    data = dict(company="Example Co", title="Evals Engineer", location="Remote", description=DESC)
    data.update(kw)
    return FakeGemini({ExtractedPosting: ExtractedPosting(**data)})


def page(body: str = DESC) -> str:
    return f"<html><head><script>var x=1;</script></head><body><nav>Home</nav><p>{body}</p></body></html>"


def test_greenhouse_link_uses_board_api(settings):
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(200, json={
            "title": "AI Research Engineer", "company_name": "Dscout",
            "location": {"name": "San Francisco, CA"},
            "content": "&lt;p&gt;Build &amp;amp; ship &lt;b&gt;evals&lt;/b&gt;&lt;/p&gt;"})

    job = fetch_posting("https://job-boards.greenhouse.io/dscout/jobs/4472409009", settings,
                        mock(handler))
    assert seen == ["https://boards-api.greenhouse.io/v1/boards/dscout/jobs/4472409009?content=true"]
    assert (job.company, job.title, job.location) == ("Dscout", "AI Research Engineer",
                                                      "San Francisco, CA")
    assert job.description == "Build & ship evals"
    assert job.source == "link"
    assert job.url == "https://job-boards.greenhouse.io/dscout/jobs/4472409009"


def test_ashby_and_lever_links(settings):
    seen = []

    def handler(req):
        seen.append(str(req.url))
        if req.url.host == "api.ashbyhq.com":
            return httpx.Response(200, json={"jobs": [
                {"id": "other", "title": "Nope", "jobUrl": "https://jobs.ashbyhq.com/acme/other",
                 "descriptionPlain": "x"},
                {"id": "ABC-123", "title": "Staff Engineer", "location": "NYC",
                 "jobUrl": "https://jobs.ashbyhq.com/acme/abc-123", "descriptionPlain": "Ashby text"}]})
        return httpx.Response(200, json={
            "text": "Data Lead", "hostedUrl": "https://jobs.lever.co/zeta/xyz",
            "categories": {"location": "Berlin"}, "descriptionPlain": "Lever text"})

    a = fetch_posting("https://jobs.ashbyhq.com/acme/abc-123/application", settings, mock(handler))
    lv = fetch_posting("https://jobs.lever.co/zeta/xyz", settings, mock(handler))
    assert seen == ["https://api.ashbyhq.com/posting-api/job-board/acme?includeCompensation=true",
                    "https://api.lever.co/v0/postings/zeta/xyz"]
    assert (a.title, a.description, a.location, a.source) == ("Staff Engineer", "Ashby text", "NYC", "link")
    assert a.url == "https://jobs.ashbyhq.com/acme/abc-123"
    assert (lv.title, lv.description, lv.location) == ("Data Lead", "Lever text", "Berlin")


LINKEDIN = """<div class="top-card-layout"><h2 class="top-card-layout__title font-sans">Senior ML Engineer</h2>
<a class="topcard__org-name-link topcard__flavor--black-link" href="x">Initech</a>
<span class="topcard__flavor topcard__flavor--bullet">Austin, TX</span>
<div class="show-more-less-html__markup show-more-less-html__markup--clamp-after-5">
<p>Build models.</p><ul><li>Python</li></ul></div></div>"""


def test_linkedin_guest_page_parsed(settings):
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(200, text=LINKEDIN)

    for url in ("https://www.linkedin.com/jobs/view/senior-ml-engineer-at-initech-3812345678?trk=x",
                "https://www.linkedin.com/jobs/collections/recommended/?currentJobId=3812345678"):
        job = fetch_posting(url, settings, mock(handler))
        assert (job.company, job.title, job.location) == ("Initech", "Senior ML Engineer", "Austin, TX")
        assert "Build models." in job.description and "Python" in job.description
        assert job.url == "https://www.linkedin.com/jobs/view/3812345678"
    assert set(seen) == {"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/3812345678"}


def test_linkedin_blocked_needs_text(settings):
    for resp in (httpx.Response(999), httpx.Response(200, text="<html>sign in</html>")):
        with pytest.raises(LinkError) as e:
            fetch_posting("https://www.linkedin.com/jobs/view/3812345678", settings,
                          mock(lambda r, resp=resp: resp))
        assert e.value.needs_text


def test_generic_page_extracted_by_llm(settings):
    fake = extraction()
    got = []

    def handler(req):
        got.append(req.headers.get("user-agent", ""))
        return httpx.Response(200, text=page(), headers={"content-type": "text/html; charset=utf-8"})

    job = fetch_posting("https://careers.example.test/jobs/1?utm=x", settings, mock(handler), fake)
    assert (job.company, job.title, job.location, job.source) == ("Example Co", "Evals Engineer",
                                                                  "Remote", "link")
    assert job.description == DESC
    assert got[0].startswith("Mozilla/5.0")
    prompt = fake.calls[0]["contents"]
    assert "word for word" in prompt and "do not add anything" in prompt
    assert "evaluation pipelines" in prompt and "var x=1" not in prompt


def test_short_or_failed_page_needs_text(settings):
    ok = lambda r: httpx.Response(200, text=page(), headers={"content-type": "text/html"})  # noqa: E731
    url = "https://careers.example.test/jobs/1"
    cases = [
        (ok, extraction(description="Too short.")),
        (lambda r: httpx.Response(403, text="no"), extraction()),
        (lambda r: (_ for _ in ()).throw(httpx.ConnectError("boom")), extraction()),
        (ok, FakeGemini(exc=RuntimeError("model down"))),
        (lambda r: httpx.Response(200, content=b"x" * (by_link.MAX_BYTES + 10),
                                  headers={"content-type": "text/html"}), extraction()),
    ]
    for handler, fake in cases:
        with pytest.raises(LinkError) as e:
            fetch_posting(url, settings, mock(handler), fake)
        assert e.value.needs_text


def test_invented_description_is_rejected(settings):
    made_up = ("Join our team and enjoy unlimited vacation, equity grants, free lunches, a gym "
               "stipend, a rooftop garden and quarterly retreats in exotic locations. " * 2)
    with pytest.raises(LinkError) as e:
        fetch_posting("https://careers.example.test/j", settings,
                      mock(lambda r: httpx.Response(200, text=page())), extraction(description=made_up))
    assert e.value.needs_text


def test_rejects_non_http_and_private_hosts(settings):
    def never(req):
        raise AssertionError("must not fetch")

    for url in ("file:///etc/passwd", "ftp://careers.example.test/x", "http://127.0.0.1:8000/",
                "http://192.168.1.5/", "http://localhost/x", "http://[::1]/",
                "http://169.254.169.254/latest", "http://evil.example.test/x"):
        with pytest.raises(LinkError) as e:
            fetch_posting(url, settings, mock(never), extraction())
        assert not e.value.needs_text, url


def test_redirect_to_private_host_is_rejected_and_public_hops_followed(settings):
    def handler(req):
        if req.url.host == "careers.example.test" and req.url.path == "/start":
            return httpx.Response(302, headers={"location": "/final"})
        if req.url.path == "/final":
            return httpx.Response(200, text=page())
        raise AssertionError(req.url)

    job = fetch_posting("https://careers.example.test/start", settings, mock(handler), extraction())
    assert job.title == "Evals Engineer"

    hit = []

    def evil(req):
        hit.append(str(req.url))
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest"})

    with pytest.raises(LinkError) as e:
        fetch_posting("https://careers.example.test/start", settings, mock(evil), extraction())
    assert not e.value.needs_text and hit == ["https://careers.example.test/start"]

    loop = lambda r: httpx.Response(302, headers={"location": "/again"})  # noqa: E731
    with pytest.raises(LinkError):
        fetch_posting("https://careers.example.test/start", settings, mock(loop), extraction())


def test_posting_from_text_and_no_key(settings):
    job = by_link.posting_from_text("https://careers.example.test/j", DESC + " Extra", settings,
                                    extraction(description=DESC))
    assert job.description == DESC and job.source == "link"
    with pytest.raises(LinkError) as e:
        by_link.posting_from_text("https://careers.example.test/j", DESC, Settings(db_path="x"))
    assert not e.value.needs_text


def test_same_link_twice_returns_same_job(conn, settings):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(200, json={"title": "T", "company_name": "Co", "content": "d"})

    url = "https://boards.greenhouse.io/co/jobs/5"
    a = add_job_by_link(conn, url, settings, http=mock(handler))
    b = add_job_by_link(conn, "https://job-boards.greenhouse.io/co/jobs/5/", settings, http=mock(handler))
    assert a == b and len(calls) == 1
    assert db.get_job(conn, a)["source"] == "link"
    c = add_job_by_link(conn, "https://careers.example.test/x", settings, text=DESC, client=extraction())
    assert c != a


def test_cli_add_prints_and_tailors(tmp_path, settings, monkeypatch, capsys):
    monkeypatch.setattr("jobhunt.config.load_settings", lambda: settings)
    monkeypatch.setattr(by_link, "default_http", lambda: mock(
        lambda r: httpx.Response(200, json={"title": "Evals Eng", "company_name": "Dscout",
                                            "content": "Do things"})))
    ran = []

    def fake_tailor(conn, job_id, settings, progress=None):
        ran.append(job_id)
        progress("Reading the posting…")
        return TailorResult(job_id=job_id, path="/out/r.pdf")

    monkeypatch.setattr("jobhunt.tailor.tailor_job", fake_tailor)
    url = "https://job-boards.greenhouse.io/dscout/jobs/9"
    assert cli.main(["add", url]) == 0
    out = capsys.readouterr().out
    assert "Added job 1: Dscout — Evals Eng" in out and "Reading the posting…" in out
    assert "Saved: /out/r.pdf" in out and ran == [1]
    assert cli.main(["add", url]) == 0
    assert "Already tracked: job 1: Dscout — Evals Eng" in capsys.readouterr().out
    assert cli.main(["add", "file:///etc/passwd"]) == 1
    assert json.dumps(ran) == "[1, 1]"
