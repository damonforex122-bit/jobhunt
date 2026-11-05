import pytest
from fastapi.testclient import TestClient

from jobhunt import db
from jobhunt.config import Settings
from jobhunt.models import Job
from jobhunt.tailor import TailorResult
from jobhunt.tailor.revise import RevisionResult
from jobhunt.web import app as webapp
from jobhunt.web import review


@pytest.fixture
def settings(tmp_path):
    s = Settings(db_path=tmp_path / "r.db", proficiently_dir=tmp_path / "prof",
                 master_resume_path=tmp_path / "master.pdf", resume_output_dir=tmp_path / "out")
    conn = db.connect(s.db_path)
    db.upsert_job(conn, Job(company="Acme", title="Lead", url="https://x.test/a",
                            description="We need <Kubernetes> people", source="ats"))
    conn.close()
    return s


@pytest.fixture(autouse=True)
def _clear_running():
    review.RUNNING.clear()
    yield
    review.RUNNING.clear()


class Fakes:
    def __init__(self):
        self.revised = []
        self.tailored = []

    def reviser(self, conn, job_id, settings, instruction, progress=None):
        self.revised.append(instruction)
        if progress:
            progress("Checking")
        db.save_resume(conn, {"job_id": job_id, "path": "x.pdf"})  # a save clears final_at
        return RevisionResult(result=TailorResult(job_id=job_id, path="x.pdf"),
                              reply="Done, shortened it.", changes=["Summary rewritten"])

    def tailor(self, conn, job_id, settings, progress=None):
        self.tailored.append(job_id)
        db.save_resume(conn, {"job_id": job_id, "path": "x.pdf"})
        return TailorResult(job_id=job_id, path="x.pdf", gaps=["Rust"], fallbacks=["bullet 2"])


@pytest.fixture
def fakes():
    return Fakes()


@pytest.fixture
def client(settings, fakes):
    app = webapp.create_app(settings)
    app.dependency_overrides[review.get_runner] = lambda: (lambda fn: fn())
    app.dependency_overrides[review.get_reviser] = lambda: fakes.reviser
    app.dependency_overrides[review.get_review_tailor] = lambda: fakes.tailor
    return TestClient(app, base_url="http://127.0.0.1")


def with_resume(settings, final=False):
    conn = db.connect(settings.db_path)
    db.save_resume(conn, {"job_id": 1, "path": "x.pdf"})
    if final:
        db.set_final(conn, 1)
    conn.close()


def test_review_page_has_three_panels_and_posting_text(client, settings):
    with_resume(settings)
    html = client.get("/jobs/1/review").text
    assert 'id="chat-panel"' in html and "<iframe" in html and 'id="posting-panel"' in html
    assert "/jobs/1/resume.pdf?v=" in html
    assert "<pre" in html and "We need &lt;Kubernetes&gt; people" in html
    assert "Show posting" in html and "localStorage" in html
    assert client.get("/jobs/99/review").status_code == 404


def test_review_page_empty_state_offers_tailor_now(client):
    html = client.get("/jobs/1/review").text
    assert "Tailor now" in html and "<iframe" not in html


def test_send_message_stores_turns_and_reply(client, settings, fakes):
    with_resume(settings)
    r = client.post("/jobs/1/review/messages", data={"text": "shorten the summary"})
    assert r.status_code == 200
    assert fakes.revised == ["shorten the summary"]
    assert "shorten the summary" in r.text and "Done, shortened it." in r.text
    assert r.headers["HX-Trigger"] == "resume-updated"
    conn = db.connect(settings.db_path)
    assert [(c["role"], c["text"]) for c in db.list_chat(conn, 1)] == [
        ("user", "shorten the summary"), ("app", "Done, shortened it.")]
    conn.close()
    assert review.RUNNING == {}


def test_first_message_without_resume_tailors(client, fakes):
    r = client.post("/jobs/1/review/messages", data={"text": "Tailor this resume"})
    assert fakes.tailored == [1] and fakes.revised == []
    assert "Tailored your resume." in r.text and "Rust" in r.text and "bullet 2" in r.text


def test_reviser_error_is_stored_as_app_turn(client, settings):
    with_resume(settings)

    def boom(*a, **k):
        raise RuntimeError("model fell over")
    client.app.dependency_overrides[review.get_reviser] = lambda: boom
    r = client.post("/jobs/1/review/messages", data={"text": "x y"})
    assert "didn&#39;t work: model fell over" in r.text
    conn = db.connect(settings.db_path)
    assert [c["role"] for c in db.list_chat(conn, 1)] == ["user", "app"]
    conn.close()
    assert review.RUNNING == {}


def test_second_send_while_running_is_refused(client, settings, fakes):
    with_resume(settings)
    review.RUNNING[1] = {"instruction": "first", "progress": ["Drafting"]}
    r = client.post("/jobs/1/review/messages", data={"text": "second"})
    assert "Still working on the last change" in r.text
    assert fakes.revised == []
    conn = db.connect(settings.db_path)
    assert db.list_chat(conn, 1) == []
    conn.close()
    assert 1 in review.RUNNING


def test_chat_polls_only_while_running(client, settings):
    with_resume(settings)
    assert "every 1s" not in client.get("/jobs/1/review/chat").text
    review.RUNNING[1] = {"instruction": "shorten it", "progress": ["Drafting"]}
    busy = client.get("/jobs/1/review/chat")
    assert 'hx-trigger="every 1s"' in busy.text
    assert "shorten it" in busy.text and "Drafting" in busy.text
    review.RUNNING.clear()
    done = client.get("/jobs/1/review/chat?poll=1")
    assert "every 1s" not in done.text and done.headers["HX-Trigger"] == "resume-updated"


def test_final_button_sets_badge_and_regenerate_clears_it(client, settings):
    with_resume(settings)
    assert "Final ✓" not in client.get("/jobs/1/review").text
    assert "Final ✓" in client.post("/jobs/1/review/final").text
    assert "Final ✓" in client.get("/jobs/1/review").text
    client.post("/jobs/1/review/messages", data={"text": "change something"})
    assert "Final ✓" not in client.get("/jobs/1/review/header").text


def test_job_list_shows_final_badge(client, settings):
    assert "Final ✓" not in client.get("/").text
    with_resume(settings, final=True)
    assert "Final ✓" in client.get("/").text
    page = client.get("/jobs/1").text
    assert "Final ✓" in page and "/jobs/1/review" in page


def test_review_post_rejects_cross_site(client, fakes):
    for path, data in (("/jobs/1/review/messages", {"text": "hi"}), ("/jobs/1/review/final", {})):
        r = client.post(path, data=data, headers={"Origin": "http://evil.test"})
        assert r.status_code == 403
    assert fakes.revised == [] and fakes.tailored == []


def test_runner_that_raises_does_not_stick_the_job(client, settings, fakes):
    def bad_runner(fn):
        raise RuntimeError("no threads")
    client.app.dependency_overrides[review.get_runner] = lambda: bad_runner
    with pytest.raises(RuntimeError):
        client.post("/jobs/1/review/messages", data={"text": "Tailor this resume"})
    assert review.RUNNING == {}
    client.app.dependency_overrides[review.get_runner] = lambda: (lambda fn: fn())
    r = client.post("/jobs/1/review/messages", data={"text": "Tailor this resume"})
    assert "Tailored your resume." in r.text and fakes.tailored == [1]


def test_cross_site_start_does_not_tailor(client, fakes):
    client.get("/jobs/1/review?start=1", headers={"Sec-Fetch-Site": "cross-site"})
    assert fakes.tailored == []
    client.get("/jobs/1/review?start=1", headers={"Sec-Fetch-Site": "same-origin"})
    assert fakes.tailored == [1]


def test_start_without_fetch_site_header_tailors(client, fakes):
    client.get("/jobs/1/review?start=1")
    assert fakes.tailored == [1]


def test_first_instruction_without_pdf_goes_to_reviser(client, fakes):
    client.post("/jobs/1/review/messages", data={"text": "lead with evals"})
    assert fakes.revised == ["lead with evals"] and fakes.tailored == []
