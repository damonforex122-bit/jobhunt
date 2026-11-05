"""Chat revisions: regenerate the tailored resume from the candidate's change requests, under the same checks."""

import re
import sqlite3
from collections.abc import Callable

from pydantic import BaseModel

from .. import db
from ..config import Settings
from ..fit.gemini import _short_error
from ..profile_store import effective_master_path, get_ai_notes
from . import TailorResult, _fail, _finish, agents, resume_from_draft, tailor_job
from .agents import AgentError, Draft, Matches, Requirements
from .check import _CAP_TERM, _SENTENCE_SPLIT
from .master import MasterError, load_master_with_sha
from .model import Resume

_TOKEN = re.compile(r"[\w.+#-]+")


class RevisionResult(BaseModel):
    result: TailorResult
    changes: list[str] = []
    reply: str = ""


def _bullet_texts(resume: Resume) -> list[str]:
    return [b.text for e in resume.experience for b in e.bullets]


def summarize_changes(old: Resume | None, new: Resume) -> list[str]:
    """What differs between two versions, in plain words."""
    changes: list[str] = []
    old_bullets = _bullet_texts(old) if old else []
    new_bullets = _bullet_texts(new)
    if old is not None and old.summary != new.summary:
        changes.append("Summary rewritten")
    changes += [f'Added: "{t[:60]}"' for t in new_bullets if t not in old_bullets]
    changes += [f'Removed: "{t[:60]}"' for t in old_bullets if t not in new_bullets]
    return changes or ["No changes"]


# Sentence-initial words that are phrasing, not a named term the candidate wants added.
_NOT_TERMS = {w.casefold() for w in (
    "Add Make Remove Drop Lead Shorten Mention Use Put Move Keep Rewrite Change Say Show Highlight "
    "Emphasize Focus Cut Replace I Please Can Could Would Also And But Now Then Maybe Thanks The A An "
    "It This That Why How What Let Try Give Include Swap Reword Trim Expand Tighten Start End").split()}


def _missing_terms(instruction: str, master: Resume) -> list[str]:
    """Capitalised terms in the instruction that the master never uses.

    A sentence's first word is skipped when it is just phrasing (a command verb, "I", "please"...),
    but a capitalised first word like "Kubernetes please" counts as a term.
    """
    have = {t.rstrip(".-").casefold() for t in _TOKEN.findall(master.full_text())}
    out: list[str] = []
    for sentence in _SENTENCE_SPLIT.split(instruction):
        for term in _CAP_TERM.findall(sentence):
            term = term.rstrip(".-")
            key = term.casefold()
            if not term or key in have or key in _NOT_TERMS or term in out:
                continue
            out.append(term)
    return out


def revise_job(conn: sqlite3.Connection, job_id: int, settings: Settings, instruction: str, client=None,
               progress: Callable[[str], None] | None = None) -> RevisionResult:
    step = progress or (lambda _msg: None)
    job = db.get_job(conn, job_id)
    if job is None:
        raise ValueError(f"No job with id {job_id}")
    if client is None:
        client = agents.make_client(settings, on_switch=step)
        if client is None:
            res = _fail(conn, job_id, "No AI key set: add GEMINI_API_KEY, ANTHROPIC_API_KEY or OPENROUTER_API_KEY to .env")
            return RevisionResult(result=res, reply=f"I couldn't apply that: {res.error}")

    rec = db.get_resume(conn, job_id)
    if not rec or not rec.get("reqs_json"):
        first = tailor_job(conn, job_id, settings, client=client, progress=progress)
        if first.error:
            return RevisionResult(result=first, reply=f"I couldn't build a first version: {first.error}")
        rec = db.get_resume(conn, job_id) or {}

    # Earlier requests (stored by the caller as chat turns) plus this one, oldest first.
    instructions = [*(t["text"] for t in db.list_chat(conn, job_id) if t["role"] == "user"), instruction]
    try:
        step("Reading your master resume…")
        master, master_sha = load_master_with_sha(conn, effective_master_path(conn, settings))
        reqs = Requirements.model_validate_json(rec["reqs_json"])
        matches = Matches.model_validate_json(rec["matches_json"])
        previous = Draft.model_validate_json(rec["draft_json"]) if rec.get("draft_json") else None
        step("Rewriting with your changes…")
        draft = agents.write(client, settings, master, reqs, matches,
                             previous=previous, instructions=instructions,
                             notes=get_ai_notes(conn))
        result, resume = _finish(conn, job_id, job, settings, master, master_sha, reqs, matches,
                                 draft, client, step)
    except Exception as exc:  # same net as tailor_job: never crash the caller (a web request)
        message = str(exc) if isinstance(exc, (MasterError, AgentError)) else _short_error(exc, settings)
        res = _fail(conn, job_id, message)
        return RevisionResult(result=res, reply=f"I couldn't apply that: {message}")

    old_resume = resume_from_draft(previous, master) if previous is not None else None
    changes = summarize_changes(old_resume, resume)
    lines = ["; ".join(changes)]
    lines += [f"I couldn't apply part of that: {f}" for f in result.fallbacks]
    lines += [f"I couldn't add {t}: it isn't in your master." for t in _missing_terms(instruction, master)]
    return RevisionResult(result=result, changes=changes, reply="\n".join(lines))
