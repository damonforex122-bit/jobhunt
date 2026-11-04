"""Local web UI. One sqlite connection per request; routes reach the slow actions
(grade, find people, scan, import) through small dependency functions so tests can override them."""
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import db, importer, scan
from ..config import Settings
from ..fit import grade_job
from ..people.finder import find_people

STATUSES = ("new", "ready", "needs", "low", "applied", "skip")
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def safe_url(value: str | None) -> str:
    """Only http(s) URLs may reach an href; anything else (javascript:, data:, ...) becomes ''."""
    v = (value or "").strip()
    return v if re.match(r"https?://", v, re.I) else ""


TEMPLATES.env.filters["safe_url"] = safe_url


# --- overridable action dependencies ---------------------------------------

def get_grader() -> Callable:
    return grade_job


def get_finder() -> Callable:
    return find_people


def get_scanner() -> Callable:
    return scan.run_scan


def get_importer() -> Callable:
    return importer.run_import


def _banner(s: Settings) -> list[str]:
    msgs = []
    if not s.gemini_api_key:
        msgs.append("Gemini grading off — set GEMINI_API_KEY")
    if not s.apollo_api_key:
        msgs.append("Apollo people search off — set APOLLO_API_KEY")
    if not s.exa_api_key:
        msgs.append("Exa people search off — set EXA_API_KEY")
    return msgs


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="jobhunt")

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
                    people=db.list_people(conn, job["company"]), **extra)

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
        return render(request, "job.html", statuses=STATUSES, **panel_ctx(conn, job))

    @app.post("/jobs/{job_id}/fit", response_class=HTMLResponse)
    def post_fit(request: Request, job_id: int, conn: Conn,
                 grader: Annotated[Callable, Depends(get_grader)]):
        job = job_or_404(conn, job_id)
        grader(conn, job_id, settings)
        return render(request, "_fit.html", **panel_ctx(conn, job))

    @app.post("/jobs/{job_id}/people", response_class=HTMLResponse)
    def post_people(request: Request, job_id: int, conn: Conn,
                    finder: Annotated[Callable, Depends(get_finder)]):
        job = job_or_404(conn, job_id)
        error = None
        try:
            finder(conn, job_id, settings)
        except Exception as e:  # a failing provider must not 500 the panel
            error = str(e)
        return render(request, "_people.html", **panel_ctx(conn, job, people_error=error))

    @app.post("/jobs/{job_id}/status")
    def post_status(job_id: int, conn: Conn, status: Annotated[str, Form()]):
        if status not in STATUSES:
            raise HTTPException(422, f"status must be one of {', '.join(STATUSES)}")
        job_or_404(conn, job_id)
        db.set_job_status(conn, job_id, status)
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.get("/companies", response_class=HTMLResponse)
    def companies(request: Request, conn: Conn):
        counts = {r[0]: r[1] for r in conn.execute(
            "SELECT company, COUNT(*) FROM jobs WHERE status NOT IN ('skip', 'applied') "
            "GROUP BY company")}
        rows = [(c, counts.get(c.name, 0)) for c in db.list_companies(conn)]
        return render(request, "companies.html", rows=rows)

    @app.post("/scan")
    def post_scan(conn: Conn, scanner: Annotated[Callable, Depends(get_scanner)]):
        r = scanner(conn, settings)
        msg = (f"Scanned {r.companies_scanned} companies: {r.jobs_seen} seen, "
               f"{r.jobs_kept} kept, {r.jobs_new} new, {len(r.errors)} errors")
        return RedirectResponse(f"/companies?msg={quote(msg)}", status_code=303)

    @app.post("/import")
    def post_import(conn: Conn, importer_fn: Annotated[Callable, Depends(get_importer)]):
        r = importer_fn(conn, settings.proficiently_dir)
        msg = "Import done: " + ", ".join(f"{k} {v}" for k, v in r.items())
        return RedirectResponse(f"/companies?msg={quote(msg)}", status_code=303)

    return app
