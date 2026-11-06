"""Tailor the master resume to a job posting."""

import re
import sqlite3
from collections.abc import Callable
from functools import partial
from pathlib import Path

from pydantic import BaseModel

from .. import db
from ..config import Settings
from ..fit.gemini import _short_error
from ..profile_store import (Links, effective_master_path, effective_output_dir,
                             get_ai_notes, get_file_name, get_links)
from . import agents
from .agents import AgentError, Draft, DraftBullet, DraftEntry, DraftSkills
from .check import assemble
from .master import MasterError, load_confirmed_master
from .model import Bullet, Link, Resume
from .output import (choose_path, company_folder, file_base_name, file_sha256, reusable_owned,
                     write_atomic)
from .render import fit_to_pages

MAX_PAGES = 2


def _http_url(value: str) -> str:
    v = (value or "").strip()
    return v if re.match(r"https?://", v, re.I) else ""


def apply_profile_links(resume: Resume, links: Links) -> Resume:
    """A copy whose header links carry the profile's Website, LinkedIn and GitHub URLs.

    A non-blank http(s) profile link replaces the URL of the header link with the same label
    (case-insensitive) or is appended when there's none; blank or non-http(s) ones keep the master's.
    """
    header_links = [lk.model_copy() for lk in resume.header.links]
    for label, raw in (("Website", links.website), ("LinkedIn", links.linkedin), ("GitHub", links.github)):
        url = _http_url(raw)
        if not url:
            continue
        for lk in header_links:
            if lk.label.strip().lower() == label.lower():
                lk.url = url
                break
        else:
            header_links.append(Link(label=label, url=url))
    header = resume.header.model_copy(update={"links": header_links})
    return resume.model_copy(update={"header": header})


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


def _draft_from_resume(resume: Resume, ranking: list[str], master: Resume, source: Draft) -> Draft:
    """The checked resume as a Draft: what the PDF says, so a revision starts from it.

    An accepted bullet keeps the source_ids the Writer cited (found by entry and text); a bullet that
    fell back to master wording is traced to its master bullet by text.
    """
    cited = {(de.entry_id, b.text): b.source_ids for de in source.entries for b in de.bullets}
    flat: list[str] = []
    entries = []
    for e in resume.experience:
        own = {b.text: b.id for b in next((m for m in master.experience if m.id == e.id), e).bullets}
        bullets = []
        for b in e.bullets:
            flat.append(b.id)
            ids = cited.get((e.id, b.text)) or ([own[b.text]] if b.text in own else [])
            bullets.append(DraftBullet(text=b.text, source_ids=list(ids)))
        entries.append(DraftEntry(entry_id=e.id, bullets=bullets))
    order = [flat.index(i) for i in ranking if i in flat]
    return Draft(summary=resume.summary, entries=entries,
                 skills=[DraftSkills(category_id=s.id, items=list(s.items)) for s in resume.skills],
                 ranking=order)


def resume_from_draft(draft: Draft, master: Resume) -> Resume:
    """The master with a stored draft's summary and bullet texts (to compare what changed)."""
    by_entry = {de.entry_id: de for de in draft.entries}
    experience = []
    for e in master.experience:
        de = by_entry.get(e.id)
        if de is not None:
            e = e.model_copy(update={"bullets": [Bullet(id=f"{e.id}t{k}", text=b.text)
                                                 for k, b in enumerate(de.bullets)]})
        experience.append(e)
    return master.model_copy(update={"summary": draft.summary, "experience": experience})


def _finish(conn: sqlite3.Connection, job_id: int, job: dict, settings: Settings, master: Resume,
            master_sha: str, reqs: agents.Requirements, matches: agents.Matches, draft: Draft, client,
            step: Callable[[str], None]) -> tuple[TailorResult, Resume]:
    """Everything after the Writer: the checks, profile links, PDF and the saved record.

    Shared by tailor_job and revise_job so a revised draft passes exactly the same checks.
    Returns the result and the checked resume; raises if rendering fails.
    """
    step("Checking every rewrite against your master…")
    judge_fn = partial(agents.judge, client, settings)
    resume, ranking, fallbacks = assemble(draft, master, judge_fn)
    resume = apply_profile_links(resume, get_links(conn))

    _, gap_reqs = agents.supported_requirements(master, reqs.requirements, matches)
    gaps = [r.text for r in gap_reqs]

    old = db.get_resume(conn, job_id)
    owned = Path(old["path"]) if old and old.get("path") else None
    out_dir = effective_output_dir(conn, settings)
    owned = reusable_owned(owned, old.get("file_sha256") if old else None,
                           db.resume_paths(conn, exclude_job=job_id), out_dir)
    folder = company_folder(out_dir, job["company"])
    dest = choose_path(folder, job["title"], owned, file_base_name(resume.header.name, get_file_name(conn)))
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
                              "error": None, "reqs_json": reqs.model_dump_json(),
                              "matches_json": matches.model_dump_json(),
                              "draft_json": _draft_from_resume(fitted["resume"], ranking, master, draft).model_dump_json()})
    except Exception as exc:
        message = f"PDF saved to {written} but couldn't record it: {_short_error(exc, settings)}"
        return TailorResult(job_id=job_id, path=str(written), gaps=gaps, fallbacks=fallbacks,
                            cut=fitted["cut"], error=message), fitted["resume"]
    return TailorResult(job_id=job_id, path=str(written), gaps=gaps, fallbacks=fallbacks,
                        cut=fitted["cut"]), fitted["resume"]


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
        master, master_sha = load_confirmed_master(conn, effective_master_path(conn, settings))
        step("Reading the posting…")
        reqs = agents.read_posting(client, settings, posting, job["company"], job["title"])
        step("Matching it to your resume…")
        matches = agents.match(client, settings, master, reqs)
        step("Writing the tailored version…")
        draft = agents.write(client, settings, master, reqs, matches, notes=get_ai_notes(conn))
        result, _resume = _finish(conn, job_id, job, settings, master, master_sha, reqs, matches,
                                  draft, client, step)
        return result
    except (MasterError, AgentError) as exc:
        return _fail(conn, job_id, str(exc))
    except Exception as exc:  # never crash the caller (or a web request) on a rendering/agent bug
        return _fail(conn, job_id, _short_error(exc, settings))


from .revise import RevisionResult, revise_job, summarize_changes  # noqa: E402,F401  (revise imports this module)


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
