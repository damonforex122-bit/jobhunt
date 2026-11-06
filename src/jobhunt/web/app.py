"""Local web UI. One sqlite connection per request; routes reach the slow actions
(grade, find people, scan, import) through small dependency functions so tests can override them."""
import os
import re
import secrets
from datetime import datetime, timezone
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, unquote, urlsplit

import yaml
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from .. import db, importer, profile_store, scan
from ..config import KEY_FIELDS, Settings, reload_settings, short_error
from ..envfile import mask, update_env
from ..fit import grade_job
from ..gmail import interviews as gmail_interviews
from ..gmail import sync as gmail_sync
from ..gmail.auth import GmailNotConnected
from ..gmail.connect import gmail_connected
from ..gmail.invites import local_tz
from ..models import CLOSED, PIPELINE, STATUSES, TRIAGE, Preferences
from ..people.finder import find_people
from ..sources import by_link
from ..sources.proficiently_files import apply_sheet_needs
from ..tailor import agents, tailor_job
from ..tailor.master_ai import preview_summary, verify_ai_full
from ..tailor.master import MasterError, _pdf_text, load_master_with_sha, master_sha256
from ..tailor.providers import has_ai_key
from . import review, setup

MAX_MASTER_BYTES = 10 * 1024 * 1024
SHA_RE = re.compile(r"[0-9a-f]{64}")
KEY_LABELS = {"gemini_api_key": "Gemini", "anthropic_api_key": "Anthropic",
              "openrouter_api_key": "OpenRouter", "apollo_api_key": "Apollo", "exa_api_key": "Exa",
              "gmail_app_password": "Gmail app password"}

JOB_SOURCES = ("proficiently", "scan", "link", "gmail")

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def local_path(value: str) -> bool:
    """True for a same-site path: starts with one '/', no backslash or control characters,
    also after percent-decoding."""
    for v in (value, unquote(value)):
        if (not v.startswith("/") or v.startswith("//") or "\\" in v
                or any(ord(c) < 32 for c in v)):
            return False
    return True


def safe_url(value: str | None) -> str:
    """Only http(s) URLs may reach an href; anything else (javascript:, data:, ...) becomes ''."""
    v = (value or "").strip()
    return v if re.match(r"https?://", v, re.I) else ""


TEMPLATES.env.filters["safe_url"] = safe_url
TEMPLATES.env.globals["status_groups"] = [("Triage", TRIAGE), ("Pipeline", PIPELINE), ("Closed", CLOSED)]


def _local_when(iso: str | None) -> str:
    """'Mon Nov 9 · 11:15 AM' in the machine's local time; '' when unparseable."""
    try:
        dt = datetime.fromisoformat(iso or "")
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    tz = local_tz()
    dt = dt.astimezone(tz) if tz is not None else dt.astimezone()
    return f"{dt:%a} {dt:%b} {dt.day} · {dt.hour % 12 or 12}:{dt:%M} {'AM' if dt.hour < 12 else 'PM'}"


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


def get_link_adder() -> Callable:
    return by_link.add_job_by_link


def get_gmail_sync() -> Callable:
    return gmail_sync.run_sync


def get_interview_sync() -> Callable:
    return gmail_interviews.run_interview_sync


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


# Small briefcase-style tab icon, so browsers' automatic /favicon.ico request isn't a 404.
_FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect x="3" y="9" width="26" height="19" rx="4" fill="#4f46e5"/>'
    '<rect x="11" y="4" width="10" height="7" rx="2" fill="none" stroke="#4f46e5" stroke-width="2.5"/>'
    '<rect x="3" y="16" width="26" height="2.5" fill="#ffffff" opacity=".8"/></svg>'
)


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

    def setup_pending() -> bool:
        c = db.connect(settings.db_path)
        try:
            return not profile_store.get(c, "setup_done")
        finally:
            c.close()

    def render(request: Request, name: str, **ctx) -> HTMLResponse:
        ctx.update(banner=_banner(settings), msg=request.query_params.get("msg"),
                   setup_pending=False if request.headers.get("HX-Request") else setup_pending())
        return TEMPLATES.TemplateResponse(request, name, ctx)

    def job_or_404(conn, job_id: int) -> dict:
        job = db.get_job(conn, job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        return job

    def panel_ctx(conn, job: dict, **extra) -> dict:
        resume = db.get_resume(conn, job["id"])
        stale = False
        if resume and resume.get("path") and resume.get("master_sha256"):
            current = profile_store.effective_master_path(conn, settings)
            try:
                stale = current is not None and current.is_file() and master_sha256(current) != resume["master_sha256"]
            except OSError:
                stale = False
        return dict(job=job, fit=db.get_fit(conn, job["id"]),
                    people=db.list_people(conn, job["company"]),
                    resume=resume, stale=stale,
                    can_tailor=has_ai_key(settings), output_dir=profile_store.effective_output_dir(conn, settings),
                    **extra)

    def jobs_page(request: Request, conn, status: str = "", min_score: str = "",
                  company: str = "", new: str = "", source: str = "", **extra) -> HTMLResponse:
        jobs = db.list_jobs(
            conn,
            status=status or None,
            min_score=int(min_score) if min_score.strip().isdigit() else None,
            company=company or None,
            source=source if source in JOB_SOURCES else None,
            added_on=datetime.now().astimezone().date() if new == "today" else None,
        )
        return render(request, "jobs.html", jobs=jobs, statuses=STATUSES, status=status,
                      min_score=min_score, company=company, new=new, source=source,
                      sources=JOB_SOURCES,
                      final_ids=db.final_job_ids(conn),
                      **{"add_url": "", "add_needs_text": False, "add_error": None, **extra})

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return Response(_FAVICON_SVG, media_type="image/svg+xml",
                        headers={"Cache-Control": "max-age=86400"})

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, conn: Conn, status: str = "", min_score: str = "",
              company: str = "", new: str = "", source: str = ""):
        if (not profile_store.get(conn, "setup_done")
                and profile_store.effective_master_path(conn, settings) is None):
            return RedirectResponse("/setup", status_code=303)
        return jobs_page(request, conn, status, min_score, company, new, source)

    @app.post("/jobs/add", response_class=HTMLResponse)
    def post_add_job(request: Request, conn: Conn,
                     adder: Annotated[Callable, Depends(get_link_adder)],
                     url: Annotated[str, Form()], text: Annotated[str, Form()] = ""):
        url = url.strip()[:2000]
        if not url:
            return RedirectResponse(f"/?msg={quote('Paste a link to a job posting.')}", status_code=303)
        try:
            job_id = adder(conn, url, settings, text=text.strip()[:200_000] or None)
        except by_link.LinkError as e:
            if e.needs_text:
                return jobs_page(request, conn, add_url=url, add_needs_text=True,
                                 add_error=str(e))
            return RedirectResponse(f"/?msg={quote(str(e))}", status_code=303)
        return RedirectResponse(f"/jobs/{job_id}/review?start=1", status_code=303)

    def prof_file(path_str: str | None) -> Path | None:
        """The recorded path if it is a regular file inside the Proficiently folder, else None."""
        if not path_str or settings.proficiently_dir is None:
            return None
        try:
            path = Path(path_str).resolve()
            if path.is_file() and path.is_relative_to(settings.proficiently_dir.resolve()):
                return path
        except (OSError, RuntimeError, ValueError):
            pass
        return None

    def serve_prof_file(path_str: str | None) -> Response:
        path = prof_file(path_str)
        if path is None:
            raise HTTPException(404, "file not found")
        ext = path.suffix.lower()
        if ext == ".pdf":
            return FileResponse(path, media_type="application/pdf", content_disposition_type="inline",
                                filename=path.name)
        if ext == ".docx":
            return FileResponse(path, content_disposition_type="attachment", filename=path.name,
                                media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        try:
            text = path.read_text(errors="replace")
        except OSError:
            raise HTTPException(404, "file not found")
        return PlainTextResponse(text, media_type="text/plain; charset=utf-8")

    @app.get("/files/{material_id}")
    def get_material(material_id: int, conn: Conn):
        row = conn.execute("SELECT path FROM job_materials WHERE id = ?", (material_id,)).fetchone()
        return serve_prof_file(row["path"] if row else None)

    @app.get("/digests/{run_id}")
    def get_digest(run_id: int, conn: Conn):
        row = conn.execute("SELECT path FROM runs WHERE id = ? AND kind = 'digest'", (run_id,)).fetchone()
        return serve_prof_file(row["path"] if row else None)

    def materials_ctx(conn, job_id: int) -> dict:
        labels = {"resume_pdf": "Resume (PDF)", "resume_md": "Resume (Markdown)",
                  "resume_docx": "Resume (Word)", "cover_letter": "Cover letter",
                  "apply_sheet": "Apply sheet", "warm_intros": "Warm intros", "posting": "Posting"}
        materials, needs = [], []
        for m in db.list_materials(conn, job_id):
            if m["kind"] not in labels:
                continue
            materials.append({**m, "label": labels[m["kind"]]})
            if m["kind"] == "apply_sheet" and not needs:
                sheet = prof_file(m["path"])
                if sheet is not None:
                    try:
                        needs = apply_sheet_needs(sheet.read_text(errors="replace"))
                    except OSError:
                        needs = []
        return dict(materials=materials, needs=needs)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: int, conn: Conn):
        job = job_or_404(conn, job_id)
        return render(request, "job.html", triage=TRIAGE, pipeline=PIPELINE, closed=CLOSED,
                      events=db.list_events(conn, job_id), **materials_ctx(conn, job_id),
                      **panel_ctx(conn, job))

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
            inside = path.resolve().is_relative_to(
                profile_store.effective_output_dir(conn, settings).resolve())
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
    def post_status(request: Request, job_id: int, conn: Conn, status: Annotated[str, Form()],
                    note: Annotated[str, Form()] = ""):
        if status not in STATUSES:
            raise HTTPException(422, f"status must be one of {', '.join(STATUSES)}")
        job_or_404(conn, job_id)
        db.set_job_status(conn, job_id, status, note=note[:1000], source="user")
        if request.headers.get("HX-Request"):  # Jobs-list dropdown or a board drop
            return TEMPLATES.TemplateResponse(request, "_status_select.html",
                                              {"j": job_or_404(conn, job_id)})
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.get("/pipeline", response_class=HTMLResponse)
    def pipeline_page(request: Request, conn: Conn):
        today = datetime.now().astimezone().date()
        summary = db.today_summary(conn, today)
        return render(request, "pipeline.html", board=db.pipeline_board(conn),
                      today=summary, last_run=db.last_morning_run(conn),
                      pipeline=PIPELINE, closed=CLOSED,
                      suggestions=db.list_gmail_suggestions(conn),
                      gmail_connected=gmail_connected(settings),
                      upcoming_interviews=len(db.list_interviews(conn, now=_now_iso())))

    @app.get("/pipeline/board", response_class=HTMLResponse)
    def pipeline_board_fragment(request: Request, conn: Conn):
        return TEMPLATES.TemplateResponse(request, "_board.html", {
            "board": db.pipeline_board(conn), "pipeline": PIPELINE, "closed": CLOSED})

    @app.get("/interviews", response_class=HTMLResponse)
    def interviews_page(request: Request, conn: Conn):
        now = _now_iso()
        rows = db.list_interviews(conn, upcoming_only=False, now=now)
        for r in rows:
            r["when"] = _local_when(r["start"])
            r["closed"] = r.get("job_status") in ("rejected", "withdrawn")
        upcoming = [r for r in rows if (r["start"] or "") >= now]
        past = [r for r in rows if (r["start"] or "") < now][::-1]
        return render(request, "interviews.html", upcoming=upcoming, past=past,
                      gmail_connected=gmail_connected(settings))

    @app.post("/gmail/sync")
    def post_gmail_sync(conn: Conn, syncer: Annotated[Callable, Depends(get_gmail_sync)],
                        interview_syncer: Annotated[Callable, Depends(get_interview_sync)],
                        next: Annotated[str, Form()] = ""):
        dest = next if (next.startswith("/") and not next.startswith("//")
                        and "\\" not in next and "?" not in next) else "/pipeline"
        try:
            r = syncer(conn, settings)
            msg = f"Checked {r.scanned} new emails, {r.suggested} suggested moves"
            try:
                msg += f", {interview_syncer(conn, settings)} interviews updated."
            except Exception as e:  # must not break the suggestion sync
                msg += f", couldn't read interviews: {short_error(e)}."
        except GmailNotConnected as e:
            msg = str(e)
        except Exception as e:  # network/Google errors must not 500 the page
            msg = f"Gmail sync failed: {short_error(e)}"
        return RedirectResponse(f"{dest}?msg={quote(msg)}", status_code=303)

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
        if settings.proficiently_dir is None:
            return RedirectResponse(f"/companies?msg={quote('No Proficiently folder set (PROFICIENTLY_DIR).')}",
                                    status_code=303)
        try:
            r = importer_fn(conn, settings.proficiently_dir)
        except _ACTION_ERRORS as e:
            return RedirectResponse(f"/companies?msg={quote('Import failed: ' + short_error(e))}",
                                    status_code=303)
        msg = "Import done: " + ", ".join(f"{k} {v}" for k, v in r.items())
        return RedirectResponse(f"/companies?msg={quote(msg)}", status_code=303)

    # --- profile ------------------------------------------------------------

    def to_profile(msg: str, next: str = "") -> RedirectResponse:
        dest = next if local_path(next) else "/profile"
        sep = "&" if "?" in dest else "?"
        return RedirectResponse(f"{dest}{sep}msg={quote(msg)}", status_code=303)

    def csv(value: str) -> list[str]:
        return [p.strip() for p in value.split(",") if p.strip()]

    @app.get("/profile", response_class=HTMLResponse)
    def profile_page(request: Request, conn: Conn):
        keys = [dict(field=f, label=KEY_LABELS.get(f, f), masked=mask(getattr(settings, f)),
                     shell=bool(os.environ.get(f.upper()))) for f in KEY_FIELDS]
        return render(request, "profile.html",
                      master=profile_store.get_master(conn),
                      pending=profile_store.get_pending_master(conn),
                      master_path=profile_store.effective_master_path(conn, settings),
                      links=profile_store.get_links(conn),
                      output_dir=profile_store.effective_output_dir(conn, settings),
                      file_name=profile_store.get_file_name(conn) or "",
                      ai_notes=profile_store.get_ai_notes(conn),
                      prefs=profile_store.effective_preferences(conn, settings), keys=keys,
                      gmail_address=settings.gmail_address or "")

    @app.post("/profile/links")
    def post_profile_links(conn: Conn, website: Annotated[str, Form()] = "",
                           github: Annotated[str, Form()] = "",
                           linkedin: Annotated[str, Form()] = ""):
        profile_store.save_links(conn, profile_store.Links(
            website=website.strip(), github=github.strip(), linkedin=linkedin.strip()))
        return to_profile("Links saved.")

    @app.post("/profile/output")
    def post_profile_output(conn: Conn, output_dir: Annotated[str, Form()] = "",
                            file_name: Annotated[str, Form()] = ""):
        folder = output_dir.strip()
        if not folder:
            return to_profile("Give a folder for the tailored resumes.")
        path = Path(folder).expanduser()
        if not path.is_absolute():
            return to_profile("Give the full path of the folder, for example ~/Documents/Resumes.")
        profile_store.put(conn, "resume_output_dir", str(path))
        profile_store.save_file_name(conn, file_name[:100])
        return to_profile("Resume output settings saved.")

    @app.post("/profile/notes")
    def post_profile_notes(conn: Conn, notes: Annotated[str, Form()] = ""):
        profile_store.save_ai_notes(conn, notes[:5000])
        return to_profile("Notes saved.")

    @app.post("/profile/preferences")
    def post_profile_preferences(conn: Conn, target_roles: Annotated[str, Form()] = "",
                                 search_terms: Annotated[str, Form()] = "",
                                 excluded_terms: Annotated[str, Form()] = "",
                                 locations: Annotated[str, Form()] = "",
                                 salary_floor: Annotated[str, Form()] = "0"):
        floor = salary_floor.strip().replace(",", "")
        if not floor.isdigit():
            return to_profile("Salary floor must be a whole number.")
        profile_store.save_preferences(conn, Preferences(
            target_roles=csv(target_roles), search_terms=csv(search_terms),
            excluded_terms=csv(excluded_terms), locations=csv(locations),
            salary_floor=int(floor)))
        return to_profile("Preferences saved.")

    @app.post("/profile/keys")
    async def post_profile_keys(request: Request):
        form = await request.form()
        changes: dict[str, str | None] = {}
        for f in KEY_FIELDS:
            if form.get(f"remove_{f}"):
                changes[f.upper()] = None
            else:
                value = form.get(f)
                value = value.strip() if isinstance(value, str) else value
                if isinstance(value, str) and value:
                    changes[f.upper()] = value
        address = form.get("gmail_address")
        if isinstance(address, str) and address.strip() != (settings.gmail_address or ""):
            changes["GMAIL_ADDRESS"] = address.strip() or None
        if not changes:
            return to_profile("No key changes.")
        try:
            update_env(settings.env_path, changes)
        except ValueError:
            return to_profile("That key contains characters that can't be saved")
        reload_settings(settings)
        return to_profile("Keys updated.")

    master_dir = settings.db_path.parent / "master"

    def pending_file(sha: str) -> Path:
        return master_dir / f".pending-{sha}.pdf"

    def drop_pending(conn) -> None:
        pending = profile_store.get_pending_master(conn)
        if pending and SHA_RE.fullmatch(pending["info"].get("sha256", "")):
            pending_file(pending["info"]["sha256"]).unlink(missing_ok=True)
        profile_store.clear_pending_master(conn)

    def left_out(resume, path: Path) -> list[str]:
        problems, left = verify_ai_full(resume, _pdf_text(path))
        return [] if problems else left[:8]

    @app.post("/profile/master")
    def post_profile_master(conn: Conn, file: Annotated[UploadFile, File()],
                            next: Annotated[str, Form()] = ""):
        data = file.file.read(MAX_MASTER_BYTES + 1)
        if len(data) > MAX_MASTER_BYTES:
            return to_profile("That file is over 10 MB.", next)
        if not data.startswith(b"%PDF-"):
            return to_profile("That file isn't a PDF.", next)
        master_dir.mkdir(parents=True, exist_ok=True)
        tmp = master_dir / f".upload-{secrets.token_hex(8)}.tmp"
        tmp.write_bytes(data)
        try:
            resume, sha = load_master_with_sha(conn, tmp, client=agents.make_client(settings),
                                               settings=settings)
        except MasterError as e:
            tmp.unlink(missing_ok=True)
            return to_profile(str(e), next)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        drop_pending(conn)  # only one upload waits for confirmation at a time
        os.replace(tmp, pending_file(sha))
        profile_store.save_pending_master(conn, profile_store.MasterInfo(
            path=str(master_dir / f"{sha}.pdf"), sha256=sha,
            name=Path(file.filename or "master.pdf").name[:200],
            uploaded_at=datetime.now(timezone.utc).isoformat(timespec="seconds")),
            preview_summary(resume, left_out(resume, master_dir / f".pending-{sha}.pdf")))
        return to_profile("Check how your resume was read, then use it.", next)

    @app.get("/profile/master/preview", response_class=HTMLResponse)
    def get_master_preview(request: Request, conn: Conn):
        return render(request, "_master_preview.html",
                      pending=profile_store.get_pending_master(conn))

    @app.post("/profile/master/confirm")
    def post_master_confirm(conn: Conn, next: Annotated[str, Form()] = ""):
        pending = profile_store.get_pending_master(conn)
        if not pending:
            return to_profile("There is no uploaded resume waiting.", next)
        info = profile_store.MasterInfo(**pending["info"])
        src = pending_file(info.sha256) if SHA_RE.fullmatch(info.sha256) else None
        if src is None or not src.is_file() or master_sha256(src) != info.sha256:
            drop_pending(conn)
            return to_profile("That upload is gone. Please upload your resume again.", next)
        dest = master_dir / f"{info.sha256}.pdf"
        os.replace(src, dest)
        info.path = str(dest)
        info.uploaded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        profile_store.save_master(conn, info)
        profile_store.clear_pending_master(conn)
        return to_profile("Master resume saved.", next)

    @app.post("/profile/master/cancel")
    def post_master_cancel(conn: Conn, cancel_next: Annotated[str, Form()] = ""):
        drop_pending(conn)
        return to_profile("Upload discarded. Choose another file.", cancel_next)

    review.register(app, settings, Conn, render, job_or_404)
    setup.register(app, settings, Conn, render)
    return app
