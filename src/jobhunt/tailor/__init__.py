"""Tailor the master resume to a job posting."""

import sqlite3
from functools import partial
from pathlib import Path

from pydantic import BaseModel

from .. import db
from ..config import Settings
from ..fit.gemini import _short_error
from . import agents
from .agents import AgentError
from .check import assemble
from .master import MasterError, load_master, master_sha256
from .output import choose_path, company_folder, write_atomic
from .render import fit_to_pages


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


def tailor_job(conn: sqlite3.Connection, job_id: int, settings: Settings, client=None) -> TailorResult:
    job = db.get_job(conn, job_id)
    if job is None:
        raise ValueError(f"No job with id {job_id}")
    posting = (job.get("description") or "").strip()
    if not posting:
        return _fail(conn, job_id, "No posting text")
    if client is None:
        if not settings.gemini_api_key:
            return _fail(conn, job_id, "GEMINI_API_KEY not set")
        client = agents.make_client(settings)

    try:
        master = load_master(conn, settings.master_resume_path)
        reqs = agents.read_posting(client, settings, posting, job["company"], job["title"])
        matches = agents.match(client, settings, master, reqs)
        draft = agents.write(client, settings, master, reqs, matches)
        judge_fn = partial(agents.judge, client, settings)
        resume, ranking, fallbacks = assemble(draft, master, judge_fn)

        supported = {m.requirement_id for m in matches.matches if m.evidence_ids}
        gaps = [r.text for r in reqs.requirements if r.id not in supported]

        old = db.get_resume(conn, job_id)
        owned = Path(old["path"]) if old and old.get("path") else None
        folder = company_folder(settings.resume_output_dir, job["company"])
        dest = choose_path(folder, job["title"], owned)
        fitted: dict = {}

        def render(tmp: Path) -> None:
            fitted["resume"], fitted["cut"] = fit_to_pages(resume, ranking, tmp)

        write_atomic(render, dest)
        sha = master_sha256(settings.master_resume_path)
    except (MasterError, AgentError) as exc:
        return _fail(conn, job_id, str(exc))
    except Exception as exc:  # never crash the caller (or a web request) on a rendering/agent bug
        return _fail(conn, job_id, _short_error(exc, settings))

    db.save_resume(conn, {"job_id": job_id, "path": str(dest), "master_sha256": sha,
                          "gaps": gaps, "fallbacks": fallbacks, "error": None})
    return TailorResult(job_id=job_id, path=str(dest), gaps=gaps, fallbacks=fallbacks,
                        cut=fitted["cut"])


def register(sub) -> None:
    from ..config import load_settings

    p = sub.add_parser("tailor", help="Tailor the master resume to a job")
    p.add_argument("job_id", type=int)

    def handler(args) -> int:
        settings = load_settings()
        conn = db.connect(settings.db_path)
        try:
            result = tailor_job(conn, args.job_id, settings)
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
