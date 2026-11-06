import errno
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from tailor_fakes import sample_resume

from jobhunt import cli, db, profile_store
from jobhunt.config import KEY_FIELDS, Settings
from jobhunt.tailor.providers import AgentError, Chain, ProviderError
from jobhunt.web import setup as setup_mod
from jobhunt.web.app import create_app


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for f in KEY_FIELDS:
        monkeypatch.delenv(f.upper(), raising=False)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    # reload_settings re-reads these from the environment, so pin them there
    monkeypatch.setenv("DB_PATH", str(tmp_path / "data" / "p.db"))
    monkeypatch.setenv("RESUME_OUTPUT_DIR", str(tmp_path / "out"))
    return Settings(db_path=tmp_path / "data" / "p.db", resume_output_dir=tmp_path / "out",
                    env_path=tmp_path / ".env")


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app, base_url="http://127.0.0.1", follow_redirects=False) as c:
        yield c


_OPEN = []


@pytest.fixture(autouse=True)
def _close_conns():
    yield
    while _OPEN:
        _OPEN.pop().close()


def conn_for(settings):
    c = db.connect(settings.db_path)
    _OPEN.append(c)
    return c


def location(r):
    assert r.status_code == 303
    return r.headers["location"]


def test_fresh_install_redirects_home_to_setup(client):
    r = client.get("/")
    assert location(r) == "/setup"


def test_no_redirect_after_setup_done_or_with_master(client, settings):
    conn = conn_for(settings)
    profile_store.put(conn, "setup_done", True)
    assert client.get("/").status_code == 200


def test_no_redirect_with_master_file(settings, tmp_path):
    s = settings.model_copy(update={"master_resume_path": tmp_path / "m.pdf"})
    with TestClient(create_app(s), base_url="http://127.0.0.1", follow_redirects=False) as c:
        assert c.get("/").status_code == 200


def test_other_pages_reachable_and_show_banner(client):
    r = client.get("/profile")
    assert r.status_code == 200
    assert "Finish setting up" in r.text and "Continue setup" in r.text
    assert "Run setup again" in r.text


def test_banner_gone_when_done(client, settings):
    profile_store.put(conn_for(settings), "setup_done", True)
    assert "Finish setting up" not in client.get("/profile").text


def test_setup_renders_steps_and_bad_step_falls_back(client, settings):
    r = client.get("/setup")
    assert r.status_code == 200 and "steps" in r.text and "1. An AI key" in r.text
    profile_store.put(conn_for(settings), "setup_step", "links")
    assert "3. Links" in client.get("/setup?step=bogus").text
    assert "5. Companies" in client.get("/setup?step=companies").text


def test_ai_step_saves_masked_key_and_advances(client, settings):
    r = client.post("/setup/ai", data={"gemini_api_key": "  AIzaSECRETKEY12345  "})
    assert location(r) == "/setup?step=resume"
    assert "AIzaSECRETKEY12345" in settings.env_path.read_text()
    assert settings.gemini_api_key == "AIzaSECRETKEY12345"
    assert profile_store.get(conn_for(settings), "setup_step") == "resume"
    page = client.get("/setup?step=ai").text
    assert "AIzaSECRETKEY12345" not in page


def test_ai_step_blocked_without_key(client, settings):
    r = client.post("/setup/ai", data={})
    assert r.status_code == 200
    assert "Add at least one AI key" in r.text
    assert profile_store.get(conn_for(settings), "setup_step") is None


def test_ai_step_skip_advances(client, settings):
    assert location(client.post("/setup/ai/skip")) == "/setup?step=resume"


def fake_chain(settings, fail=False):
    class P:
        name = "FakeAI"

        def generate(self, prompt, schema):
            assert prompt == 'Reply with {"ok": true}.'
            assert list(schema.model_fields) == ["ok"]
            if fail:
                raise ProviderError("boom key=hidden")
            return schema(ok=True)
    return Chain([P()], settings)


def test_test_ai_ok(app, client, settings):
    app.dependency_overrides[setup_mod.get_setup_client] = lambda: (lambda s: fake_chain(s))
    r = client.post("/setup/test-ai")
    assert "Working — answered by FakeAI" in r.text


def test_test_ai_error_shown(app, client, settings, monkeypatch):
    monkeypatch.setattr("jobhunt.tailor.providers._sleep", lambda s: None)
    app.dependency_overrides[setup_mod.get_setup_client] = lambda: (lambda s: fake_chain(s, fail=True))
    r = client.post("/setup/test-ai")
    assert "FakeAI: returned" not in r.text and "boom" in r.text and "alert-error" in r.text


def test_test_ai_without_key(app, client):
    app.dependency_overrides[setup_mod.get_setup_client] = lambda: (lambda s: None)
    assert "Add an AI key first" in client.post("/setup/test-ai").text


def test_resume_step_shows_upload_and_preview_return_links(client, settings):
    r = client.get("/setup?step=resume")
    assert 'name="next" value="/setup?step=resume"' in r.text
    conn = conn_for(settings)
    profile_store.save_pending_master(conn, profile_store.MasterInfo(
        path="x", sha256="a" * 64, name="m.pdf", uploaded_at="t"),
        {"name": "Alex", "contact": [], "summary": "", "roles": [], "skills": [],
         "education": [], "publications": [], "left_out": []})
    page = client.get("/setup?step=resume").text
    assert 'name="next" value="/setup?step=links"' in page
    assert 'name="cancel_next" value="/setup?step=resume"' in page


def test_master_cancel_and_confirm_return_to_next(client, settings):
    r = client.post("/profile/master/cancel", data={"cancel_next": "/setup?step=resume"})
    assert location(r).startswith("/setup?step=resume&msg=")
    r = client.post("/profile/master/confirm", data={"next": "//evil.com"})
    assert location(r).startswith("/profile?msg=")
    r = client.post("/profile/master/confirm", data={"next": "/setup?step=links"})
    assert location(r).startswith("/setup?step=links&msg=")


def test_links_prefilled_from_master_by_domain(client, settings, tmp_path):
    from jobhunt.tailor.model import Link
    conn = conn_for(settings)
    resume = sample_resume()
    resume.header.links = [Link(label="LinkedIn", url="https://www.linkedin.com/in/alex"),
                           Link(label="Code", url="https://github.com/alex"),
                           Link(label="Site", url="https://alex.dev")]
    pdf = tmp_path / "m.pdf"
    pdf.write_bytes(b"%PDF-1.4 x")
    from jobhunt.tailor.master import PARSER_VERSION, master_sha256
    db.save_master_cache(conn, f"{master_sha256(pdf)}:v{PARSER_VERSION}", resume.model_dump_json())
    profile_store.save_master(conn, profile_store.MasterInfo(
        path=str(pdf), sha256=master_sha256(pdf), name="m.pdf", uploaded_at="t"))
    page = client.get("/setup?step=links").text
    assert 'value="https://www.linkedin.com/in/alex"' in page
    assert 'value="https://github.com/alex"' in page
    assert 'value="https://alex.dev"' in page


def test_links_step_saves(client, settings):
    r = client.post("/setup/links", data={"linkedin": " https://linkedin.com/in/x ",
                                          "github": "", "website": "https://x.dev"})
    assert location(r) == "/setup?step=prefs"
    links = profile_store.get_links(conn_for(settings))
    assert links.linkedin == "https://linkedin.com/in/x" and links.website == "https://x.dev"


def test_prefs_step_saves_everything(client, settings, tmp_path):
    out = tmp_path / "Resumes"
    r = client.post("/setup/prefs", data={
        "target_roles": "Data Scientist, ML Engineer", "locations": "Remote, NYC",
        "salary_floor": "120,000", "search_terms": "ml", "excluded_terms": "intern",
        "notes": "No crypto", "output_dir": str(out)})
    assert location(r) == "/setup?step=companies"
    conn = conn_for(settings)
    p = profile_store.get_preferences(conn)
    assert p.target_roles == ["Data Scientist", "ML Engineer"] and p.locations == ["Remote", "NYC"]
    assert p.salary_floor == 120000 and p.search_terms == ["ml"] and p.excluded_terms == ["intern"]
    assert profile_store.get_ai_notes(conn) == "No crypto"
    assert profile_store.effective_output_dir(conn, settings) == out


@pytest.mark.parametrize("data,expect", [
    ({"target_roles": "", "locations": "Remote"}, "at least one target role"),
    ({"target_roles": "A", "locations": ""}, "at least one location"),
    ({"target_roles": "A", "locations": "B", "salary_floor": "lots"}, "whole number"),
    ({"target_roles": "A", "locations": "B", "output_dir": "relative/dir"}, "full path"),
])
def test_prefs_validation(client, settings, data, expect):
    r = client.post("/setup/prefs", data=data)
    assert r.status_code == 200 and expect in r.text
    assert profile_store.get_preferences(conn_for(settings)) is None


def test_companies_recognises_ats_and_reports_unknown(client, settings):
    urls = "\n".join(["https://job-boards.greenhouse.io/acme-corp", "https://jobs.ashbyhq.com/big_co",
                      "https://jobs.lever.co/zed", "https://jobs.lever.co/zed",
                      "https://example.com/careers"])
    r = client.post("/setup/companies", data={"urls": urls})
    assert r.status_code == 200
    assert "https://example.com/careers" in r.text and "Couldn" in r.text
    got = {c.name: (c.ats, c.slug, c.domain) for c in db.list_companies(conn_for(settings))}
    assert got == {"Acme Corp": ("greenhouse", "acme-corp", None),
                   "Big Co": ("ashby", "big_co", None), "Zed": ("lever", "zed", None)}
    assert location(client.post("/setup/companies/skip")) == "/setup?step=gmail"


def test_companies_all_known_advances(client):
    r = client.post("/setup/companies", data={"urls": "https://jobs.lever.co/zed"})
    assert location(r) == "/setup?step=gmail"


def test_companies_import_button_only_with_proficiently_dir(client, settings, tmp_path):
    assert "Import from Proficiently" not in client.get("/setup?step=companies").text
    d = tmp_path / "prof"
    d.mkdir()
    s = settings.model_copy(update={"proficiently_dir": d})
    app = create_app(s)
    app.dependency_overrides[__import__("jobhunt.web.app", fromlist=["x"]).get_importer] = \
        lambda: (lambda conn, p: {"companies": 3})
    with TestClient(app, base_url="http://127.0.0.1", follow_redirects=False) as c:
        assert "Import from Proficiently" in c.get("/setup?step=companies").text
        loc = location(c.post("/setup/import"))
        assert "Import done: companies 3" in parse_qs(urlsplit(loc).query)["msg"][0]


def test_gmail_step_saves_to_env_and_never_echoes(client, settings):
    r = client.post("/setup/gmail", data={"gmail_address": "me@gmail.com",
                                          "gmail_app_password": "abcd efgh ijkl mnop"})
    assert location(r) == "/setup?step=people"
    env = settings.env_path.read_text()
    assert "GMAIL_ADDRESS=me@gmail.com" in env and "abcdefghijklmnop" in env
    page = client.get("/setup?step=gmail").text
    assert "abcdefghijklmnop" not in page and "me@gmail.com" in page


def test_test_gmail_with_fake(app, client):
    app.dependency_overrides[setup_mod.get_gmail_tester] = lambda: (lambda s: "Connected as me@gmail.com — found 4 job emails")
    assert "Connected as me@gmail.com" in client.post("/setup/test-gmail").text

    def bad(s):
        raise RuntimeError("login failed")
    app.dependency_overrides[setup_mod.get_gmail_tester] = lambda: bad
    r = client.post("/setup/test-gmail")
    assert "login failed" in r.text and "alert-error" in r.text


def test_people_step_saves_keys_and_finishes(client, settings):
    r = client.post("/setup/people", data={"apollo_api_key": "APOLLO-SECRET-1", "exa_api_key": ""})
    assert location(r) == "/setup?step=done"
    assert settings.apollo_api_key == "APOLLO-SECRET-1"
    conn = conn_for(settings)
    assert profile_store.get(conn, "setup_done") is True
    page = client.get("/setup?step=done").text
    assert "Scan my companies" in page and "Add a job by link" in page
    assert "APOLLO-SECRET-1" not in client.get("/setup?step=people").text


def test_skip_all_sets_done_and_goes_home(client, settings):
    assert location(client.post("/setup/skip-all")) == "/"
    assert client.get("/").status_code == 200


def test_cross_site_post_rejected(client):
    r = client.post("/setup/skip-all", headers={"origin": "https://evil.com"})
    assert r.status_code == 403
    r = client.post("/setup/ai", data={"gemini_api_key": "x"}, headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 403


# --- jobhunt serve ---------------------------------------------------------

def test_serve_busy_port_message(monkeypatch, capsys):
    import uvicorn

    def boom(*a, **k):
        raise OSError(errno.EADDRINUSE, "Address already in use")
    monkeypatch.setattr(uvicorn, "run", boom)
    assert cli.main(["serve", "--port", "8123"]) == 1
    out = capsys.readouterr().out
    assert "Port 8123 is busy — try `jobhunt serve --port 8125`" in out


def test_serve_prechecks_port(monkeypatch, capsys):
    import socket

    import uvicorn
    called = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: called.append(1))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        assert cli.main(["serve", "--port", str(port)]) == 1
    assert "is busy" in capsys.readouterr().out and not called


def test_serve_open_starts_timer(monkeypatch):
    import threading
    import webbrowser

    import uvicorn
    timers = []

    class FakeTimer:
        def __init__(self, delay, fn, args):
            timers.append((delay, fn, args))

        def start(self):
            pass
    monkeypatch.setattr(threading, "Timer", FakeTimer)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert cli.main(["serve", "--port", str(port), "--open"]) == 0
    assert timers == [(1.0, webbrowser.open, [f"http://127.0.0.1:{port}"])]


def test_test_ai_uses_typed_key_without_saving_or_echoing(app, client, settings):
    seen = []

    def factory(s):
        seen.append(s.gemini_api_key)
        return fake_chain(s)
    app.dependency_overrides[setup_mod.get_setup_client] = lambda: factory
    r = client.post("/setup/test-ai", data={"gemini_api_key": " TYPED-KEY-999 "})
    assert seen == ["TYPED-KEY-999"] and "Working" in r.text
    assert "TYPED-KEY-999" not in r.text
    assert not settings.env_path.exists() and settings.gemini_api_key is None


def test_test_ai_blank_falls_back_to_saved_key(app, client, settings):
    settings.gemini_api_key = "SAVED-KEY"
    seen = []
    app.dependency_overrides[setup_mod.get_setup_client] = lambda: (lambda s: seen.append(s.gemini_api_key) or fake_chain(s))
    client.post("/setup/test-ai", data={"gemini_api_key": ""})
    assert seen == ["SAVED-KEY"]


def test_test_ai_error_masks_saved_key(app, client, settings, monkeypatch):
    monkeypatch.setattr("jobhunt.tailor.providers._sleep", lambda s: None)
    settings.gemini_api_key = "SAVED-KEY-123"

    class P:
        name = "FakeAI"

        def generate(self, prompt, schema):
            raise ProviderError("bad key SAVED-KEY-123")
    app.dependency_overrides[setup_mod.get_setup_client] = lambda: (lambda s: Chain([P()], s))
    r = client.post("/setup/test-ai")
    assert "SAVED-KEY-123" not in r.text and "alert-error" in r.text


def test_test_gmail_uses_typed_values_without_saving_or_echoing(app, client, settings):
    seen = []

    def tester(s):
        seen.append((s.gmail_address, s.gmail_app_password))
        raise RuntimeError("login failed for " + s.gmail_app_password)
    app.dependency_overrides[setup_mod.get_gmail_tester] = lambda: tester
    r = client.post("/setup/test-gmail", data={"gmail_address": "new@gmail.com",
                                                "gmail_app_password": "abcd efgh ijkl mnop"})
    assert seen == [("new@gmail.com", "abcdefghijklmnop")]
    assert "abcdefghijklmnop" not in r.text and "abcd efgh" not in r.text
    assert not settings.env_path.exists() and settings.gmail_address is None


def test_test_buttons_include_form_fields(client):
    assert 'hx-include="closest form"' in client.get("/setup?step=ai").text
    assert 'hx-include="closest form"' in client.get("/setup?step=gmail").text


@pytest.mark.parametrize("nxt", ["/\\evil.com", "/%09/evil.com", "/%5Cevil.com", "//evil.com"])
def test_next_rejects_tricky_paths(client, nxt):
    r = client.post("/profile/master/confirm", data={"next": nxt})
    assert location(r).startswith("/profile?msg=")


def test_setup_page_not_redirected_or_bannered(client):
    r = client.get("/setup")
    assert r.status_code == 200 and "Finish setting up" not in r.text
    for step in ("ai", "resume", "people", "done"):
        page = client.get(f"/setup?step={step}").text
        assert "Grading is off" not in page and "People search is off" not in page and "alert-warning" not in page


def test_partials_skip_banner_check(client, monkeypatch):
    calls = []
    orig = db.connect
    monkeypatch.setattr(db, "connect", lambda *a, **k: calls.append(1) or orig(*a, **k))
    client.get("/profile/master/preview", headers={"HX-Request": "true"})
    assert calls == [1]  # only the request's own connection


@pytest.mark.parametrize("port", ["0", "70000", "-1"])
def test_serve_rejects_bad_port(capsys, port):
    with pytest.raises(SystemExit):
        cli.main(["serve", "--port", port])
    assert "port" in capsys.readouterr().err.lower()


def test_serve_timer_starts_after_create_app(monkeypatch):
    import threading

    import uvicorn
    order = []
    monkeypatch.setattr(cli, "__name__", cli.__name__)
    monkeypatch.setattr("jobhunt.web.app.create_app", lambda s: order.append("app") or object())
    monkeypatch.setattr(threading, "Timer", type("T", (), {
        "__init__": lambda self, *a: order.append("timer"), "start": lambda self: None}))
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    cli.main(["serve", "--port", str(port), "--open"])
    assert order == ["app", "timer"]
