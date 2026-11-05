from pathlib import Path

import pytest
from pypdf import PdfReader
from tailor_fakes import FakeGemini, sample_resume

from jobhunt import cli, db
from jobhunt import tailor as tailor_mod
from jobhunt.config import Settings
from jobhunt.models import Job
from jobhunt.tailor import tailor_job
from jobhunt.tailor.agents import (
    AgentError,
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
from jobhunt.tailor.output import choose_path, company_folder, write_atomic
from jobhunt.tailor.render import render_pdf

MASTER = sample_resume()


def fake(raise_on=None, exc=None, matches=None) -> FakeGemini:
    reqs = Requirements(requirements=[
        Requirement(id="r0", text="Build forecasting services", kind="must", phrases=["forecasting"]),
        Requirement(id="r1", text="Run Kubernetes clusters", kind="nice", phrases=["Kubernetes"]),
        Requirement(id="r2", text="Mentor engineers", kind="duty", phrases=["mentor"]),
    ])
    matches = matches or Matches(matches=[
        Match(requirement_id="r0", evidence_ids=["e0b0"]),
        Match(requirement_id="r2", evidence_ids=["e0b2"]),
    ])
    draft = Draft(
        summary=MASTER.summary,
        entries=[
            DraftEntry(entry_id="e0", bullets=[
                DraftBullet(text="Built a forecasting service that reduced stockouts by 18% across 120 warehouses.",
                            source_ids=["e0b0"])]),
            DraftEntry(entry_id="e1", bullets=[
                DraftBullet(text="Lifted retention by 4 points with churn models.", source_ids=["e1b0"])]),
            DraftEntry(entry_id="e2", bullets=[
                DraftBullet(text="Built a text-mining index of survey responses.", source_ids=["e2b0"])]),
        ],
        skills=[],
        ranking=[0, 1, 2],
    )
    verdicts = Verdicts(verdicts=[Verdict(index=i, supported=True, reason="") for i in range(10)])
    return FakeGemini({Requirements: reqs, Matches: matches, Draft: draft, Verdicts: verdicts},
                      exc=exc, raise_on=raise_on)


@pytest.fixture
def settings(tmp_path):
    master = tmp_path / "master.pdf"
    render_pdf(MASTER, master)
    return Settings(db_path=tmp_path / "t.db", proficiently_dir=tmp_path, master_resume_path=master,
                    resume_output_dir=tmp_path / "out", gemini_api_key="k")


def add_job(conn, n=1, company="Acme", title="Evals Engineer", description="Build forecasting. Mentor."):
    j = Job(company=company, title=title, url=f"https://x.test/{n}", source="ats",
            description=description)
    return db.upsert_job(conn, j)[0]


def pages(path: str) -> int:
    return len(PdfReader(path).pages)


def test_tailor_job_writes_pdf_in_company_folder(conn, settings):
    j = add_job(conn)
    status = db.get_job(conn, j)["status"]
    res = tailor_job(conn, j, settings, client=fake())
    assert res.error is None
    assert Path(res.path) == settings.resume_output_dir / "Acme" / "Georgia Liu - Resume.pdf"
    assert Path(res.path).exists() and pages(res.path) <= 2
    rec = db.get_resume(conn, j)
    assert rec["path"] == res.path and rec["master_sha256"] and rec["error"] is None
    assert res.path in db.resume_paths(conn)
    assert db.get_job(conn, j)["status"] == status


def test_second_job_same_company_gets_title_suffix(conn, settings):
    a, b = add_job(conn, 1), add_job(conn, 2, title="Data Lead")
    first = tailor_job(conn, a, settings, client=fake())
    second = tailor_job(conn, b, settings, client=fake())
    assert Path(second.path).name == "Georgia Liu - Resume (Data Lead).pdf"
    assert Path(second.path).parent == Path(first.path).parent


def test_rerun_same_job_overwrites_own_file(conn, settings):
    j = add_job(conn)
    first = tailor_job(conn, j, settings, client=fake())
    second = tailor_job(conn, j, settings, client=fake())
    assert second.path == first.path
    assert len(list(Path(first.path).parent.iterdir())) == 1


def test_handmade_file_never_overwritten(conn, settings):
    folder = settings.resume_output_dir / "Acme"
    folder.mkdir(parents=True)
    handmade = folder / "Georgia Liu - Resume.pdf"
    handmade.write_bytes(b"hand made")
    j = add_job(conn)
    res = tailor_job(conn, j, settings, client=fake())
    assert Path(res.path).name == "Georgia Liu - Resume (Evals Engineer).pdf"
    assert handmade.read_bytes() == b"hand made"


def test_rerun_does_not_take_over_another_jobs_name(conn, settings):
    a, b = add_job(conn, 1), add_job(conn, 2, title="Data Lead")
    first = tailor_job(conn, a, settings, client=fake())
    Path(first.path).unlink()
    other = tailor_job(conn, b, settings, client=fake())
    assert other.path == first.path  # B took the plain name
    before = Path(other.path).read_bytes()
    again = tailor_job(conn, a, settings, client=fake())
    assert again.path != first.path
    assert Path(again.path).name == "Georgia Liu - Resume (Evals Engineer).pdf"
    assert Path(other.path).read_bytes() == before


def test_rerun_never_overwrites_handmade_replacement(conn, settings):
    j = add_job(conn)
    first = tailor_job(conn, j, settings, client=fake())
    Path(first.path).write_bytes(b"hand made")
    again = tailor_job(conn, j, settings, client=fake())
    assert again.path != first.path
    assert Path(first.path).read_bytes() == b"hand made"


def test_rerun_after_output_dir_change_does_not_reuse_old_path(conn, settings, tmp_path):
    j = add_job(conn)
    first = tailor_job(conn, j, settings, client=fake())
    moved = settings.model_copy(update={"resume_output_dir": tmp_path / "elsewhere"})
    again = tailor_job(conn, j, moved, client=fake())
    assert Path(again.path).is_relative_to(tmp_path / "elsewhere")
    assert Path(first.path).exists()


def test_hallucinated_evidence_id_is_a_gap(conn, settings):
    m = Matches(matches=[Match(requirement_id="r0", evidence_ids=["e9b9"]),
                         Match(requirement_id="r2", evidence_ids=["e0b2"])])
    res = tailor_job(conn, add_job(conn), settings, client=fake(matches=m))
    assert res.gaps == ["Build forecasting services", "Run Kubernetes clusters"]


def test_unfittable_resume_notes_page_count(conn, settings, monkeypatch):
    real = tailor_mod.fit_to_pages

    def over(resume, ranking, out, *a, **k):
        fitted, cut, _ = real(resume, ranking, out, *a, **k)
        return fitted, cut, 3

    monkeypatch.setattr(tailor_mod, "fit_to_pages", over)
    j = add_job(conn)
    res = tailor_job(conn, j, settings, client=fake())
    assert res.error is None and Path(res.path).exists()
    assert "Couldn't fit in 2 pages: 3 pages" in res.fallbacks
    assert "Couldn't fit in 2 pages: 3 pages" in db.get_resume(conn, j)["fallbacks"]


def test_save_failure_is_a_short_error_and_says_file_was_written(conn, settings, monkeypatch):
    import sqlite3

    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(tailor_mod.db, "save_resume", boom)
    res = tailor_job(conn, add_job(conn), settings, client=fake())
    assert res.error and "OperationalError" in res.error and "PDF saved" in res.error and "\n" not in res.error
    assert res.path and Path(res.path).exists()


def test_company_folder_sanitises_and_reuses_case(tmp_path):
    root = tmp_path / "r"
    (root / "OPenAI").mkdir(parents=True)
    assert company_folder(root, "Scale AI, Inc.").name == "Scale AI"
    assert company_folder(root, "A/B Labs").name == "AB Labs"
    assert company_folder(root, "Foo LLC Inc").name == "Foo LLC"
    assert company_folder(root, "Bar, LLC").name == "Bar"
    assert company_folder(root, "OpenAI") == root / "OPenAI"


def test_choose_path_numbers_collisions(tmp_path):
    (tmp_path / "Georgia Liu - Resume.pdf").write_bytes(b"x")
    (tmp_path / "Georgia Liu - Resume (A/B).pdf".replace("/", "")).write_bytes(b"x")
    assert choose_path(tmp_path, "A/B", None).name == "Georgia Liu - Resume (AB) 2.pdf"
    owned = tmp_path / "mine.pdf"
    assert choose_path(tmp_path, "A/B", owned) == owned


def test_write_atomic_cleans_up_on_failure(tmp_path):
    dest = tmp_path / "x.pdf"
    dest.write_bytes(b"old")

    def boom(tmp: Path):
        tmp.write_bytes(b"partial")
        raise RuntimeError("render failed")

    with pytest.raises(RuntimeError):
        write_atomic(boom, dest)
    assert dest.read_bytes() == b"old" and [p.name for p in tmp_path.iterdir()] == ["x.pdf"]
    assert not list(tmp_path.glob("*.tmp.pdf"))


def test_agent_failure_writes_nothing(conn, settings):
    j = add_job(conn)
    ok = tailor_job(conn, j, settings, client=fake())
    before = Path(ok.path).read_bytes()
    res = tailor_job(conn, j, settings, client=fake(raise_on=Draft, exc=RuntimeError("boom k")))
    assert res.error and "boom" in res.error
    assert res.path == ok.path and Path(ok.path).read_bytes() == before
    assert not list(Path(ok.path).parent.glob("*.tmp.pdf"))
    rec = db.get_resume(conn, j)
    assert rec["error"] and rec["path"] == ok.path and rec["gaps"] == ok.gaps

    other = add_job(conn, 2, company="Fresh")
    res = tailor_job(conn, other, settings, client=fake(raise_on=Draft, exc=RuntimeError("boom")))
    assert res.error and res.path is None
    assert not (settings.resume_output_dir / "Fresh").exists()


def test_render_failure_is_an_error_not_a_crash(conn, settings, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("render k exploded")

    monkeypatch.setattr(tailor_mod, "fit_to_pages", boom)
    j = add_job(conn)
    res = tailor_job(conn, j, settings, client=fake())
    assert res.error and "***" in res.error and "k exploded" not in res.error.replace("***", "")
    assert not settings.resume_output_dir.exists() or not list(settings.resume_output_dir.rglob("*.pdf"))


def test_agent_error_message_is_reported(conn, settings, monkeypatch):
    def boom(*a, **k):
        raise AgentError("Gemini returned no valid Matches")

    monkeypatch.setattr(tailor_mod.agents, "match", boom)
    res = tailor_job(conn, add_job(conn), settings, client=fake())
    assert res.error == "Gemini returned no valid Matches"


def test_master_error_is_reported(conn, settings):
    settings.master_resume_path.unlink()
    res = tailor_job(conn, add_job(conn), settings, client=fake())
    assert res.error and res.path is None


def test_no_key_and_no_posting_messages(conn, settings):
    s = settings.model_copy(update={"gemini_api_key": None})
    j = add_job(conn)
    assert tailor_job(conn, j, s).error == "GEMINI_API_KEY not set"
    empty = add_job(conn, 2, description="  ")
    assert tailor_job(conn, empty, settings, client=fake()).error == "No posting text"
    assert db.get_resume(conn, empty)["error"] == "No posting text"
    with pytest.raises(ValueError):
        tailor_job(conn, 999, settings, client=fake())


def test_gaps_reported(conn, settings):
    j = add_job(conn)
    res = tailor_job(conn, j, settings, client=fake())
    assert res.gaps == ["Run Kubernetes clusters"]
    assert db.get_resume(conn, j)["gaps"] == res.gaps
    empty = Matches(matches=[Match(requirement_id="r0", evidence_ids=[])])
    res = tailor_job(conn, add_job(conn, 2, company="B"), settings, client=fake(matches=empty))
    assert len(res.gaps) == 3


def test_tailor_cli_prints_path(tmp_path, settings, monkeypatch, capsys):
    monkeypatch.setattr("jobhunt.config.load_settings", lambda: settings)
    monkeypatch.setattr(tailor_mod.agents, "make_client", lambda s: fake())
    conn = db.connect(settings.db_path)
    j = add_job(conn)
    conn.close()
    assert cli.main(["tailor", str(j)]) == 0
    out = capsys.readouterr().out
    assert "Saved: " in out and "Georgia Liu - Resume.pdf" in out
    assert "Gaps:" in out and "Run Kubernetes clusters" in out


def test_tailor_cli_error_returns_1(tmp_path, settings, monkeypatch, capsys):
    monkeypatch.setattr("jobhunt.config.load_settings", lambda: settings)
    conn = db.connect(settings.db_path)
    j = add_job(conn, description="")
    conn.close()
    assert cli.main(["tailor", str(j)]) == 1
    assert "Not tailored: No posting text" in capsys.readouterr().out


STEPS = ["Reading your master resume…", "Reading the posting…", "Matching it to your resume…",
         "Writing the tailored version…", "Checking every rewrite against your master…",
         "Laying out the PDF…"]


def test_progress_reports_each_step_in_order(conn, settings):
    seen = []
    r = tailor_job(conn, add_job(conn), settings, client=fake(), progress=seen.append)
    assert r.error is None and seen == STEPS


def test_progress_stops_at_the_failing_step(conn, settings):
    from jobhunt.tailor.agents import Draft
    seen = []
    r = tailor_job(conn, add_job(conn), settings, client=fake(raise_on=Draft, exc=ValueError("x")),
                   progress=seen.append)
    assert r.error.startswith("Writing the resume failed") and seen == STEPS[:4]


def test_tailor_cli_prints_progress(tmp_path, settings, monkeypatch, capsys):
    monkeypatch.setattr("jobhunt.config.load_settings", lambda: settings)
    monkeypatch.setattr(tailor_mod.agents, "make_client", lambda s: fake())
    conn = db.connect(settings.db_path)
    j = add_job(conn)
    conn.close()
    assert cli.main(["tailor", str(j)]) == 0
    out = capsys.readouterr().out
    assert all(step in out for step in STEPS) and out.index(STEPS[-1]) < out.index("Saved: ")
