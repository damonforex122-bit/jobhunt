"""Resume review screen: chat on the left, PDF in the middle, posting on the right.

Revisions run in a background thread (about a minute each); the chat fragment polls while one runs.
"""
import threading
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse

from .. import db
from ..config import Settings, short_error
from ..tailor import tailor_job
from ..tailor.revise import revise_job

# job id -> {"instruction": str, "progress": [str]} for every run in flight.
RUNNING: dict[int, dict] = {}
_LOCK = threading.Lock()

BUSY_REPLY = "Still working on the last change…"
TAILOR_FIRST = "Tailor this resume"


def get_runner() -> Callable[[Callable[[], None]], None]:
    def run(fn: Callable[[], None]) -> None:
        threading.Thread(target=fn, daemon=True).start()
    return run


def get_reviser() -> Callable:
    return revise_job


def get_review_tailor() -> Callable:
    return tailor_job


def _tailor_reply(res) -> str:
    if res.error:
        return f"Sorry, that didn't work: {res.error}"
    parts = ["Tailored your resume."]
    if res.gaps:
        parts.append("Gaps: " + "; ".join(res.gaps))
    if res.fallbacks:
        parts.append("Kept your original wording: " + "; ".join(res.fallbacks))
    return " ".join(parts)


def register(app: FastAPI, settings: Settings, Conn, render: Callable, job_or_404: Callable) -> None:
    Runner = Annotated[Callable, Depends(get_runner)]
    Reviser = Annotated[Callable, Depends(get_reviser)]
    Tailorer = Annotated[Callable, Depends(get_review_tailor)]

    def snapshot(job_id: int) -> dict | None:
        with _LOCK:
            st = RUNNING.get(job_id)
            return None if st is None else {"instruction": st["instruction"],
                                            "progress": list(st["progress"])}

    def chat_ctx(request: Request, conn, job_id: int, extra_app_line: str | None = None) -> dict:
        return dict(job_id=job_id, chat=db.list_chat(conn, job_id),
                    running=snapshot(job_id), extra_app_line=extra_app_line)

    def header_ctx(conn, job: dict) -> dict:
        resume = db.get_resume(conn, job["id"])
        return dict(job=job, resume=resume,
                    has_pdf=bool(resume and resume.get("path")),
                    final=bool(resume and resume.get("final_at")))

    def chat_response(request: Request, conn, job_id: int, trigger: bool = False,
                      extra_app_line: str | None = None) -> HTMLResponse:
        resp = render(request, "_chat.html", **chat_ctx(request, conn, job_id, extra_app_line))
        if trigger:
            resp.headers["HX-Trigger"] = "resume-updated"
        return resp

    def launch(conn, job_id: int, text: str, runner: Callable, reviser: Callable,
               tailor: Callable) -> bool:
        """Start a tailor/revise run in the background; False when one is already running."""
        with _LOCK:
            if job_id in RUNNING:
                return False
            RUNNING[job_id] = {"instruction": text, "progress": []}
        first = not (db.get_resume(conn, job_id) or {}).get("path")

        def progress(line: str) -> None:
            with _LOCK:
                if job_id in RUNNING:
                    RUNNING[job_id]["progress"].append(line)

        def work() -> None:
            bg = None
            try:
                bg = db.connect(settings.db_path, check_same_thread=False)
                try:
                    if first:
                        reply = _tailor_reply(tailor(bg, job_id, settings, progress=progress))
                    else:
                        rev = reviser(bg, job_id, settings, text, progress=progress)
                        reply = rev.reply or "; ".join(rev.changes) or "Done."
                except Exception as e:
                    reply = f"Sorry, that didn't work: {short_error(e)}"
                db.add_chat(bg, job_id, "user", text)
                db.add_chat(bg, job_id, "app", reply)
            except Exception:
                pass  # never let a background failure escape the thread
            finally:
                with _LOCK:
                    RUNNING.pop(job_id, None)
                if bg is not None:
                    bg.close()

        runner(work)
        return True

    @app.get("/jobs/{job_id}/review", response_class=HTMLResponse)
    def review_page(request: Request, job_id: int, conn: Conn, runner: Runner, reviser: Reviser,
                    tailor: Tailorer, start: str = ""):
        job = job_or_404(conn, job_id)
        if start and not (db.get_resume(conn, job_id) or {}).get("path"):
            launch(conn, job_id, TAILOR_FIRST, runner, reviser, tailor)  # no-op if already running
        resume = db.get_resume(conn, job_id)
        return render(request, "review.html", **header_ctx(conn, job),
                      **chat_ctx(request, conn, job_id),
                      version=(resume or {}).get("created_at") or "")

    @app.get("/jobs/{job_id}/review/header", response_class=HTMLResponse)
    def review_header(request: Request, job_id: int, conn: Conn):
        return render(request, "_review_header.html", **header_ctx(conn, job_or_404(conn, job_id)))

    @app.get("/jobs/{job_id}/review/chat", response_class=HTMLResponse)
    def review_chat(request: Request, job_id: int, conn: Conn, poll: str = ""):
        job_or_404(conn, job_id)
        # The poller's last request (run just finished) tells the page to refresh the PDF.
        finished = bool(poll) and snapshot(job_id) is None
        return chat_response(request, conn, job_id, trigger=finished)

    @app.post("/jobs/{job_id}/review/messages", response_class=HTMLResponse)
    def review_send(request: Request, job_id: int, conn: Conn, text: Annotated[str, Form()],
                    runner: Runner, reviser: Reviser, tailor: Tailorer):
        job_or_404(conn, job_id)
        text = text.strip()[:4000]
        if not text:
            return chat_response(request, conn, job_id)
        if not launch(conn, job_id, text, runner, reviser, tailor):
            return chat_response(request, conn, job_id, extra_app_line=BUSY_REPLY)
        # With an inline runner the run is already over; tell the page to refresh the PDF.
        return chat_response(request, conn, job_id, trigger=snapshot(job_id) is None)

    @app.post("/jobs/{job_id}/review/final", response_class=HTMLResponse)
    def review_final(request: Request, job_id: int, conn: Conn):
        job = job_or_404(conn, job_id)
        db.set_final(conn, job_id)
        return render(request, "_review_header.html", **header_ctx(conn, job))
