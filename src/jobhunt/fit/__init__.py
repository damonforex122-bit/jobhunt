import sqlite3

from .. import db
from ..config import Settings
from ..models import Fit
from ..profile_store import effective_master_path, get_ai_notes
from ..tailor.master import CONFIRM_MESSAGE, MasterError, load_confirmed_master
from .gemini import _now, grade


def grade_job(conn: sqlite3.Connection, job_id: int, settings: Settings, client=None) -> Fit:
    job = db.get_job(conn, job_id)
    if job is None:
        raise ValueError(f"No job with id {job_id}")
    try:
        resume = load_confirmed_master(conn, effective_master_path(conn, settings))[0].full_text()
    except MasterError:  # never grade a different resume than the one chosen
        fit = Fit(job_id=job_id, keyword_score=0, graded_at=_now(), model=settings.gemini_model,
                  error=CONFIRM_MESSAGE)
        return _record(conn, job, fit)
    return _record(conn, job, grade(job, resume, settings, client=client, notes=get_ai_notes(conn)))


def _record(conn: sqlite3.Connection, job: dict, fit: Fit) -> Fit:
    job_id = job["id"]
    old = db.get_fit(conn, job_id)
    if fit.llm_score is None and old is not None and old.llm_score is not None:
        # a failed grade must not wipe an existing score: keep it, surface the error
        fit = old.model_copy(update={
            "keyword_score": fit.keyword_score,
            "missing_keywords": fit.missing_keywords or old.missing_keywords,
            "error": fit.error,
        })
    db.save_fit(conn, fit)
    if fit.llm_score is not None and job["status"] == "new":
        db.set_job_status(conn, job_id, "ready" if fit.llm_score >= 8 else "low",
                          source="grader")
    return fit


def register(sub) -> None:
    from ..config import load_settings

    p = sub.add_parser("fit", help="Grade resume fit for a job")
    p.add_argument("job_id", nargs="?", type=int)
    p.add_argument("--all-new", action="store_true", help="Grade every job with status 'new'")

    def show(job: dict, fit: Fit) -> None:
        print(f"{job['company']} - {job['title']}")
        print(f"  keyword score: {fit.keyword_score}")
        print(f"  missing: {', '.join(fit.missing_keywords) or '-'}")
        if fit.error:
            print(f"  Not graded: {fit.error}")
            return
        print(f"  fit score: {fit.llm_score}/10   match: {fit.match_pct}%")
        print(f"  reasons: {fit.reasons}")
        print(f"  gaps: {fit.gaps}")
        for b in fit.rewritten_bullets:
            print(f"  - {b.get('rewritten')}")

    def handler(args) -> int:
        if args.job_id is None and not args.all_new:
            print("Give a job id or --all-new")
            return 2
        settings = load_settings()
        if not settings.gemini_api_key:
            print("GEMINI_API_KEY not set: showing keyword score only.")
        conn = db.connect(settings.db_path)
        try:
            if args.all_new:
                jobs = db.list_jobs(conn, status="new")
                graded = failed = 0
                for j in jobs:
                    try:
                        fit = grade_job(conn, j["id"], settings)
                    except Exception as exc:  # keep going on a single failure
                        failed += 1
                        print(f"job {j['id']}: {type(exc).__name__}: {str(exc)[:150]}")
                        continue
                    if fit.error:
                        failed += 1
                        print(f"job {j['id']} ({j['company']}): Not graded: {fit.error}")
                    else:
                        graded += 1
                print(f"Graded {graded}, failed {failed}, of {len(jobs)} new jobs")
                return 0
            job = db.get_job(conn, args.job_id)
            if job is None:
                print(f"No job with id {args.job_id}")
                return 1
            show(job, grade_job(conn, args.job_id, settings))
            return 0
        finally:
            conn.close()

    p.set_defaults(handler=handler)
