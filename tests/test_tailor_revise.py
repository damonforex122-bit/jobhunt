from pathlib import Path

import pytest
from master_layout import render_master_layout
from pypdf import PdfReader
from tailor_fakes import FakeGemini, sample_resume

from jobhunt import db
from jobhunt.config import Settings
from jobhunt.models import Job
from jobhunt.tailor import revise_job, summarize_changes, tailor_job
from jobhunt.tailor.agents import (
    Draft,
    DraftBullet,
    DraftEntry,
    Match,
    Matches,
    Requirement,
    Requirements,
    Verdict,
    Verdicts,
)

MASTER = sample_resume()
REQS = Requirements(requirements=[
    Requirement(id="r0", text="Build forecasting services", kind="must", phrases=["forecasting"]),
    Requirement(id="r1", text="Mentor engineers", kind="duty", phrases=["mentor"]),
])
MATCHES = Matches(matches=[Match(requirement_id="r0", evidence_ids=["e0b0"]),
                           Match(requirement_id="r1", evidence_ids=["e0b2"])])
VERDICTS = Verdicts(verdicts=[Verdict(index=i, supported=True, reason="") for i in range(20)])


def draft(e0_text="Built a forecasting service that reduced stockouts by 18% across 120 warehouses.",
          summary=None) -> Draft:
    return Draft(
        summary=summary or MASTER.summary,
        entries=[
            DraftEntry(entry_id="e0", bullets=[DraftBullet(text=e0_text, source_ids=["e0b0"])]),
            DraftEntry(entry_id="e1", bullets=[
                DraftBullet(text="Lifted retention by 4 points with churn models.", source_ids=["e1b0"])]),
        ],
        skills=[], ranking=[0, 1])


def fake(d: Draft) -> FakeGemini:
    return FakeGemini({Requirements: REQS, Matches: MATCHES, Draft: d, Verdicts: VERDICTS})


@pytest.fixture
def settings(tmp_path):
    master = tmp_path / "master.pdf"
    render_master_layout(MASTER, master)
    return Settings(db_path=tmp_path / "t.db", proficiently_dir=tmp_path, master_resume_path=master,
                    resume_output_dir=tmp_path / "out", gemini_api_key="k")


def add_job(conn):
    return db.upsert_job(conn, Job(company="Acme", title="Evals Engineer", url="https://x.test/1",
                                   source="ats", description="Build forecasting. Mentor."))[0]


def prompt_of(client: FakeGemini, schema=Draft) -> str:
    return [c for c in client.calls if c["config"]["response_schema"] is schema][-1]["contents"]


def pdf_text(path: str) -> str:
    return "\n".join(p.extract_text() for p in PdfReader(path).pages)


def test_revise_passes_previous_draft_and_all_instructions_in_order(conn, settings):
    j = add_job(conn)
    tailor_job(conn, j, settings, client=fake(draft()))
    db.add_chat(conn, j, "user", "make it shorter")
    db.add_chat(conn, j, "app", "Summary rewritten")
    db.add_chat(conn, j, "user", "lead with forecasting")
    client = fake(draft())
    revise_job(conn, j, settings, "mention mentoring", client=client)
    p = prompt_of(client)
    assert "--- CURRENT VERSION ---" in p and "Lifted retention by 4 points" in p
    assert "--- HER CHANGE REQUESTS (oldest first) ---" in p
    assert p.index("make it shorter") < p.index("lead with forecasting") < p.index("mention mentoring")
    assert "Summary rewritten" not in p.split("CHANGE REQUESTS")[1]
    assert "never add facts the sources don't state" in p


def test_revise_replaces_pdf_in_place_and_clears_final(conn, settings):
    j = add_job(conn)
    first = tailor_job(conn, j, settings, client=fake(draft()))
    db.set_final(conn, j)
    assert j in db.final_job_ids(conn)
    new = draft("Built a forecasting service. Stockouts fell 18% across 120 warehouses.")
    out = revise_job(conn, j, settings, "tighten the first bullet", client=fake(new))
    assert out.result.error is None and out.result.path == first.path
    assert len(list(Path(first.path).parent.iterdir())) == 1
    assert "Stockouts fell 18%" in pdf_text(first.path)
    assert db.get_resume(conn, j)["final_at"] is None and j not in db.final_job_ids(conn)
    assert any(c.startswith('Added: "Built a forecasting service. Stockouts') for c in out.changes)
    assert any(c.startswith("Removed:") for c in out.changes)
    assert out.reply.startswith("Added:")


def test_revise_runs_full_tailor_when_none_saved(conn, settings):
    j = add_job(conn)
    client = fake(draft())
    out = revise_job(conn, j, settings, "add more detail", client=client)
    assert out.result.error is None and Path(out.result.path).exists()
    rec = db.get_resume(conn, j)
    assert rec["reqs_json"] and rec["matches_json"] and rec["draft_json"]
    drafts = [c for c in client.calls if c["config"]["response_schema"] is Draft]
    assert len(drafts) == 2  # the tailor run, then the revision
    assert "CURRENT VERSION" not in drafts[0]["contents"] and "CURRENT VERSION" in drafts[1]["contents"]


def test_unsupported_instruction_is_refused_in_reply_and_not_in_pdf(conn, settings):
    j = add_job(conn)
    tailor_job(conn, j, settings, client=fake(draft()))
    bad = draft("Built a forecasting service on Kubernetes that reduced stockouts by 18%.")
    out = revise_job(conn, j, settings, "add Kubernetes", client=fake(bad))
    assert "couldn't add Kubernetes" in out.reply
    assert "I couldn't apply part of that:" in out.reply
    assert out.result.fallbacks
    assert "Kubernetes" not in pdf_text(out.result.path)


def test_revise_failure_keeps_previous_row_and_final(conn, settings):
    j = add_job(conn)
    tailor_job(conn, j, settings, client=fake(draft()))
    db.set_final(conn, j)
    before = db.get_resume(conn, j)
    out = revise_job(conn, j, settings, "x", client=FakeGemini({Draft: None}, exc=RuntimeError("boom"),
                                                               raise_on=Draft))
    assert out.result.error and out.reply.startswith("I couldn't apply that")
    after = db.get_resume(conn, j)
    assert after["path"] == before["path"] and after["draft_json"] == before["draft_json"]
    assert after["final_at"] == before["final_at"] and after["error"]


def test_summarize_changes():
    a = sample_resume()
    assert summarize_changes(a, a) == ["No changes"]
    b = a.model_copy(deep=True)
    b.summary = "New summary."
    b.experience[0].bullets[0].text = "X" * 70
    del b.experience[1].bullets[0]
    ch = summarize_changes(a, b)
    assert ch[0] == "Summary rewritten"
    assert f'Added: "{"X" * 60}"' in ch
    assert f'Removed: "{a.experience[0].bullets[0].text[:60]}"' in ch
    assert f'Removed: "{a.experience[1].bullets[0].text[:60]}"' in ch
    assert summarize_changes(None, a)[0].startswith("Added:")


def test_set_final_and_final_job_ids(conn, settings):
    j, k = add_job(conn), db.upsert_job(conn, Job(company="B", title="T", url="https://x.test/2",
                                                  source="ats", description="d"))[0]
    tailor_job(conn, j, settings, client=fake(draft()))
    assert db.final_job_ids(conn) == set()
    db.set_final(conn, j)
    assert db.final_job_ids(conn) == {j}
    db.set_final(conn, k)  # no resume row: nothing to mark
    assert db.final_job_ids(conn) == {j}
    db.add_chat(conn, j, "user", "hi")
    assert [(c["role"], c["text"]) for c in db.list_chat(conn, j)] == [("user", "hi")]
