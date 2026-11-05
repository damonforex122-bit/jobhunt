"""Tailor the master resume to a job posting."""

import sqlite3
from collections.abc import Callable
from functools import partial
from pathlib import Path

from pydantic import BaseModel

from .. import db
from ..config import Settings
from ..fit.gemini import _short_error
from . import agents
from .agents import AgentError
from .check import assemble
from .master import MasterError, load_master_with_sha
from .output import choose_path, company_folder, file_sha256, reusable_owned, write_atomic
from .render import fit_to_pages

MAX_PAGES = 2


class TailorResult(BaseModel):
    job_id: int
    path: str | None = None
    gaps: list[str] = []
    fallbacks: list[str] = []
    cut: list[str] = []
    error: str | None = None


def _fail(conn: sqlite3.Connection, job_id: int, message: str) -> TailorResult:
    """Record an error without touching the earlier file, path or gaps."""
    old = db.get_resume(conn, job_id) or {}
    db.save_resume(conn, {**old, "job_id": job_id, "error": message})
    return TailorResult(job_id=job_id, path=old.get("path"), gaps=old.get("gaps", []),
                        fallbacks=old.get("fallbacks", []), error=message)


def tailor_job(conn: sqlite3.Connection, job_id: int, settings: Settings, client=None,
               progress: Callable[[str], None] | None = None) -> TailorResult:
    """`progress` is told each step as it starts (the CLI prints them; runs take about a minute)."""
    step = progress or (lambda _msg: None)
    job = db.get_job(conn, job_id)
    if job is None:
        raise ValueError(f"No job with id {job_id}")
    posting = (job.get("description") or "").strip()
    if not posting:
        return _fail(conn, job_id, "No posting text")
    if client is None:
        client = agents.make_client(settings, on_switch=step)  # Gemini, then Claude, then OpenRouter
        if client is None:
            return _fail(conn, job_id, "No AI key set: add GEMINI_API_KEY, ANTHROPIC_API_KEY or OPENROUTER_API_KEY to .env")

    try:
        step("Reading your master resume…")
        master, master_sha = load_master_with_sha(conn, settings.master_resume_path)
        step("Reading the posting…")
        reqs = agents.read_posting(client, settings, posting, job["company"], job["title"])
        step("Matching it to your resume…")
        matches = agents.match(client, settings, master, reqs)
        step("Writing the tailored version…")
        draft = agents.write(client, settings, master, reqs, matches)
        step("Checking every rewrite against your master…")
        judge_fn = partial(agents.judge, client, settings)
        resume, ranking, fallbacks = assemble(draft, master, judge_fn)

        _, gap_reqs = agents.supported_requirements(master, reqs.requirements, matches)
        gaps = [r.text for r in gap_reqs]

        old = db.get_resume(conn, job_id)
        owned = Path(old["path"]) if old and old.get("path") else None
        owned = reusable_owned(owned, old.get("file_sha256") if old else None,
                               db.resume_paths(conn, exclude_job=job_id), settings.resume_output_dir)
        folder = company_folder(settings.resume_output_dir, job["company"])
        dest = choose_path(folder, job["title"], owned)
        fitted: dict = {}
        step("Laying out the PDF…")

        def render(tmp: Path) -> None:
            fitted["resume"], fitted["cut"], fitted["pages"] = fit_to_pages(resume, ranking, tmp)
            fitted["sha"] = file_sha256(tmp)

        write_atomic(render, dest)
        written = dest  # from here the PDF exists; only recording it can still fail
        if fitted["pages"] > MAX_PAGES:
            fallbacks = [*fallbacks, f"Couldn't fit in {MAX_PAGES} pages: {fitted['pages']} pages"]
        try:
            db.save_resume(conn, {"job_id": job_id, "path": str(written), "master_sha256": master_sha,
                                  "file_sha256": fitted["sha"], "gaps": gaps, "fallbacks": fallbacks,
                                  "error": None})
        except Exception as exc:
            message = f"PDF saved to {written} but couldn't record it: {_short_error(exc, settings)}"
            return TailorResult(job_id=job_id, path=str(written), gaps=gaps, fallbacks=fallbacks,
                                cut=fitted["cut"], error=message)
    except (MasterError, AgentError) as exc:
        return _fail(conn, job_id, str(exc))
    except Exception as exc:  # never crash the caller (or a web request) on a rendering/agent bug
        return _fail(conn, job_id, _short_error(exc, settings))

    return TailorResult(job_id=job_id, path=str(written), gaps=gaps, fallbacks=fallbacks,
                        cut=fitted["cut"])


def register(sub) -> None:
    from ..config import load_settings

    p = sub.add_parser("tailor", help="Tailor the master resume to a job")
    p.add_argument("job_id", type=int)

    def handler(args) -> int:
        settings = load_settings()
        conn = db.connect(settings.db_path)
        try:
            result = tailor_job(conn, args.job_id, settings,
                                progress=lambda msg: print(msg, flush=True))
        except ValueError as exc:
            print(exc)
            return 1
        finally:
            conn.close()
        if result.error:
            print(f"Not tailored: {result.error}")
            return 1
        print(f"Saved: {result.path}")
        if result.gaps:
            print("Gaps:")
            for g in result.gaps:
                print(f"  - {g}")
        if result.fallbacks:
            print("Kept original wording:")
            for f in result.fallbacks:
                print(f"  - {f}")
        return 0

    p.set_defaults(handler=handler)
