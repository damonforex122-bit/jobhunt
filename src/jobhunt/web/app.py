"""Local web UI. One sqlite connection per request; routes reach the slow actions
(grade, find people, scan, import) through small dependency functions so tests can override them."""
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, urlsplit

import yaml
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import db, importer, scan
from ..config import Settings, short_error
from ..fit import grade_job
from ..gmail import sync as gmail_sync
from ..gmail.auth import GmailNotConnected
from ..models import CLOSED, PIPELINE, STATUSES, TRIAGE
from ..people.finder import find_people
from ..tailor import tailor_job

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def safe_url(value: str | None) -> str:
    """Only http(s) URLs may reach an href; anything else (javascript:, data:, ...) becomes ''."""
    v = (value or "").strip()
    return v if re.match(r"https?://", v, re.I) else ""


TEMPLATES.env.filters["safe_url"] = safe_url


# --- overridable action dependencies ---------------------------------------

def get_grader() -> Callable:
    return grade_job


def get_tailor() -> Callable:
    return tailor_job


def get_finder() -> Callable:
    return find_people


def get_scanner() -> Callable:
    return scan.run_scan


def get_importer() -> Callable:
    return importer.run_import


def get_gmail_sync() -> Callable:
    return gmail_sync.run_sync


def _banner(s: Settings) -> list[str]:
    msgs = []
    if not s.gemini_api_key:
        msgs.append("Gemini grading off — set GEMINI_API_KEY")
    if not s.apollo_api_key:
        msgs.append("Apollo people search off — set APOLLO_API_KEY")
    if not s.exa_api_key:
        msgs.append("Exa people search off — set EXA_API_KEY")
    return msgs


LOCAL_HOSTS = ("127.0.0.1", "localhost")
# Predictable failures from scan/import (bad watchlist, missing Proficiently dir, ...)
_ACTION_ERRORS = (ValueError, OSError, yaml.YAMLError)


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="jobhunt")

    @app.middleware("http")
    async def reject_cross_site_posts(request: Request, call_next):
        if request.method == "POST":
            origin = request.headers.get("origin")
            cross = request.headers.get("sec-fetch-site", "").lower() == "cross-site"
            if origin is not None and (urlsplit(origin).hostname or "") not in LOCAL_HOSTS:
                cross = True
            if cross:
                return PlainTextResponse("cross-site request rejected", status_code=403)
        return await call_next(request)

    # Added last, so it runs first: blocks DNS-rebinding requests with a foreign Host header.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(LOCAL_HOSTS))

    def get_conn() -> Iterator:
        # Sync routes/dependencies may run on different threadpool threads, so the
        # per-request connection is opened with check_same_thread=False.
        conn = db.connect(settings.db_path, check_same_thread=False)
        try:
            yield conn
        finally:
            conn.close()

    Conn = Annotated[object, Depends(get_conn)]

    def render(request: Request, name: str, **ctx) -> HTMLResponse:
        ctx.update(banner=_banner(settings), msg=request.query_params.get("msg"))
        return TEMPLATES.TemplateResponse(request, name, ctx)

    def job_or_404(conn, job_id: int) -> dict:
        job = db.get_job(conn, job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        return job

    def panel_ctx(conn, job: dict, **extra) -> dict:
        return dict(job=job, fit=db.get_fit(conn, job["id"]),
                    people=db.list_people(conn, job["company"]),
                    resume=db.get_resume(conn, job["id"]),
                    can_tailor=bool(settings.gemini_api_key), output_dir=settings.resume_output_dir,
                    **extra)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, conn: Conn, status: str = "", min_score: str = "",
              company: str = ""):
        jobs = db.list_jobs(
            conn,
            status=status or None,
            min_score=int(min_score) if min_score.strip().isdigit() else None,
            company=company or None,
        )
        return render(request, "jobs.html", jobs=jobs, statuses=STATUSES, status=status,
                      min_score=min_score, company=company)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: int, conn: Conn):
        job = job_or_404(conn, job_id)
        return render(request, "job.html", triage=TRIAGE, pipeline=PIPELINE, closed=CLOSED,
                      events=db.list_events(conn, job_id), **panel_ctx(conn, job))

    @app.post("/jobs/{job_id}/fit", response_class=HTMLResponse)
    def post_fit(request: Request, job_id: int, conn: Conn,
                 grader: Annotated[Callable, Depends(get_grader)]):
        job = job_or_404(conn, job_id)
        grader(conn, job_id, settings)
        return render(request, "_fit.html", **panel_ctx(conn, job))

    @app.post("/jobs/{job_id}/resume", response_class=HTMLResponse)
    def post_resume(request: Request, job_id: int, conn: Conn,
                    tailor: Annotated[Callable, Depends(get_tailor)]):
        job = job_or_404(conn, job_id)
        tailor(conn, job_id, settings)  # records its own result or error; never raises
        return render(request, "_resume.html", **panel_ctx(conn, job))

    @app.get("/jobs/{job_id}/resume.pdf")
    def get_resume_pdf(job_id: int, conn: Conn):
        rec = db.get_resume(conn, job_id)
        if rec is None or not rec.get("path"):
            raise HTTPException(404, "no tailored resume")
        path = Path(rec["path"])
        try:
            inside = path.resolve().is_relative_to(settings.resume_output_dir.resolve())
        except OSError:
            inside = False
        if not inside or not path.is_file():
            raise HTTPException(404, "no tailored resume")
        return FileResponse(path, media_type="application/pdf", content_disposition_type="inline")

    @app.post("/jobs/{job_id}/people", response_class=HTMLResponse)
    def post_people(request: Request, job_id: int, conn: Conn,
                    finder: Annotated[Callable, Depends(get_finder)]):
        job = job_or_404(conn, job_id)
        error, skipped = None, {}
        try:
            report = finder(conn, job_id, settings)
            skipped = getattr(report, "skipped", None) or {}
        except Exception as e:  # a failing provider must not 500 the panel
            error = str(e)
        return render(request, "_people.html",
                      **panel_ctx(conn, job, people_error=error, skipped=skipped))

    @app.post("/jobs/{job_id}/status")
    def post_status(job_id: int, conn: Conn, status: Annotated[str, Form()],
                    note: Annotated[str, Form()] = ""):
        if status not in STATUSES:
            raise HTTPException(422, f"status must be one of {', '.join(STATUSES)}")
        job_or_404(conn, job_id)
        db.set_job_status(conn, job_id, status, note=note[:1000])
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.get("/pipeline", response_class=HTMLResponse)
    def pipeline_page(request: Request, conn: Conn):
        return render(request, "pipeline.html", board=db.pipeline_board(conn),
                      pipeline=PIPELINE, closed=CLOSED,
                      suggestions=db.list_gmail_suggestions(conn),
                      gmail_connected=settings.gmail_token.exists())

    @app.post("/gmail/sync")
    def post_gmail_sync(conn: Conn, syncer: Annotated[Callable, Depends(get_gmail_sync)]):
        try:
            r = syncer(conn, settings)
            msg = f"Checked {r.scanned} new emails, {r.suggested} suggested moves."
        except GmailNotConnected as e:
            msg = str(e)
        except Exception as e:  # network/Google errors must not 500 the page
            msg = f"Gmail sync failed: {short_error(e)}"
        return RedirectResponse(f"/pipeline?msg={quote(msg)}", status_code=303)

    @app.post("/gmail/suggestions/{suggestion_id}/{action}")
    def post_suggestion(suggestion_id: int, action: str, conn: Conn):
        if action not in ("accept", "dismiss"):
            raise HTTPException(404, "unknown action")
        if not db.resolve_gmail_suggestion(conn, suggestion_id, accept=action == "accept"):
            raise HTTPException(404, "suggestion not found")
        return RedirectResponse("/pipeline", status_code=303)

    @app.get("/companies", response_class=HTMLResponse)
    def companies(request: Request, conn: Conn):
        counts = {r[0]: r[1] for r in conn.execute(
            "SELECT company, COUNT(*) FROM jobs WHERE status IN ({}) GROUP BY company".format(
                ",".join("?" * (len(TRIAGE) - 1))), [t for t in TRIAGE if t != "skip"])}
        rows = [(c, counts.get(c.name, 0)) for c in db.list_companies(conn)]
        return render(request, "companies.html", rows=rows)

    @app.post("/scan")
    def post_scan(conn: Conn, scanner: Annotated[Callable, Depends(get_scanner)]):
        try:
            r = scanner(conn, settings)
        except _ACTION_ERRORS as e:
            return RedirectResponse(f"/companies?msg={quote('Scan failed: ' + short_error(e))}",
                                    status_code=303)
        msg = (f"Scanned {r.companies_scanned} companies: {r.jobs_seen} seen, "
               f"{r.jobs_kept} kept, {r.jobs_new} new, {len(r.errors)} errors")
        return RedirectResponse(f"/companies?msg={quote(msg)}", status_code=303)

    @app.post("/import")
    def post_import(conn: Conn, importer_fn: Annotated[Callable, Depends(get_importer)]):
        try:
            r = importer_fn(conn, settings.proficiently_dir)
        except _ACTION_ERRORS as e:
            return RedirectResponse(f"/companies?msg={quote('Import failed: ' + short_error(e))}",
                                    status_code=303)
        msg = "Import done: " + ", ".join(f"{k} {v}" for k, v in r.items())
        return RedirectResponse(f"/companies?msg={quote(msg)}", status_code=303)

    return app
