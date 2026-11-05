"""Gemini agents: read the posting, match it to the master, write a draft, judge the rewrites."""
import json
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel

from ..config import Settings
from .model import Resume
from .providers import AgentError, Chain, Gemini, build_chain

__all__ = ["AgentError"]  # re-exported: callers catch it from here

# What the user is told each call was doing when it failed.
_STEP = {
    "Requirements": "reading the posting",
    "Matches": "matching the posting to your resume",
    "Draft": "writing the resume",
    "Verdicts": "checking the rewrites",
    "ResumeRead": "reading your resume",
}


class Requirement(BaseModel):
    id: str
    text: str
    kind: Literal["must", "nice", "duty"]
    phrases: list[str]


class Requirements(BaseModel):
    requirements: list[Requirement]


class Match(BaseModel):
    requirement_id: str
    evidence_ids: list[str]  # bullet ids, "summary", or "s{n}:{item}"; empty = gap


class Matches(BaseModel):
    matches: list[Match]


class DraftBullet(BaseModel):
    text: str
    source_ids: list[str]


class DraftEntry(BaseModel):
    entry_id: str
    bullets: list[DraftBullet]


class DraftSkills(BaseModel):
    category_id: str
    items: list[str]


class Draft(BaseModel):
    summary: str
    entries: list[DraftEntry]
    skills: list[DraftSkills]
    ranking: list[int]  # indexes into the flattened bullet list, best first


class Verdict(BaseModel):
    index: int
    supported: bool
    reason: str


class Verdicts(BaseModel):
    verdicts: list[Verdict]


def make_client(settings: Settings, on_switch: Callable[[str], None] | None = None) -> Chain | None:
    """Gemini, then Claude, then OpenRouter: every provider with a key. None if no key is set."""
    return build_chain(settings, on_switch)


def _call(client, settings: Settings, prompt: str, schema: type[BaseModel]):
    """`client` is a provider Chain, or a bare Gemini client (wrapped as a one-provider chain)."""
    step = _STEP.get(schema.__name__, "calling the model")
    chain = client if isinstance(client, Chain) else Chain([Gemini(settings, client=client)], settings)
    return chain.generate(prompt, schema, step)


def _master_json(resume: Resume) -> str:
    """The master as IDs and texts only (blurbs included as context)."""
    data = {
        "summary": {"id": "summary", "text": resume.summary},
        "experience": [
            {
                "id": e.id, "org": e.org, "title": e.title, "dates": e.dates, "blurb": e.blurb,
                "bullets": [{"id": b.id, "text": b.text} for b in e.bullets],
            }
            for e in resume.experience
        ],
        "skills": [
            {"id": s.id, "name": s.name, "items": [{"id": f"{s.id}:{i}", "text": i} for i in s.items]}
            for s in resume.skills
        ],
    }
    return json.dumps(data, indent=1, ensure_ascii=False)


def _reqs_json(reqs: Requirements) -> str:
    return json.dumps([r.model_dump() for r in reqs.requirements], indent=1, ensure_ascii=False)


def read_posting(client, settings: Settings, posting: str, company: str, title: str) -> Requirements:
    prompt = f"""You read a job posting and extract what the employer is asking for.

Return at most 15 requirements, most important first. For each give:
- id: "r0", "r1", ... in order
- text: the requirement in one short sentence, in the posting's own terms
- kind: "must" (required or core), "nice" (preferred or bonus), or "duty" (day-to-day responsibility)
- phrases: the exact skill, tool or phrase wording from the posting that a resume should echo

Do not invent requirements that are not in the posting. Ignore benefits, perks, legal boilerplate and company marketing.

Company: {company}
Role: {title}

--- JOB POSTING ---
{posting}
"""
    return _call(client, settings, prompt, Requirements)


def match(client, settings: Settings, resume: Resume, reqs: Requirements) -> Matches:
    prompt = f"""You match job requirements to evidence in a candidate's master resume.

For each requirement, list the evidence_ids of the master items that genuinely support it:
bullet ids (like "e0b1"), "summary", or skill items (like "s0:Python").
Only cite an item when its text really shows that skill or experience. If nothing in the master supports a
requirement, return an empty evidence_ids list: that is a gap, and gaps are fine. Never stretch to fill one.
Return exactly one match per requirement, using its requirement_id.

--- MASTER RESUME (ids and texts) ---
{_master_json(resume)}

--- REQUIREMENTS ---
{_reqs_json(reqs)}
"""
    return _call(client, settings, prompt, Matches)


def _valid_evidence(resume: Resume, ids: list[str]) -> list[str]:
    bullets = resume.all_bullets()
    skills = {s.id: {i.casefold() for i in s.items} for s in resume.skills}
    out = []
    for i in ids:
        cat, sep, item = i.partition(":")
        if i in bullets or i == "summary" or (sep and item.casefold() in skills.get(cat, ())):
            out.append(i)
    return out


def _evidence(resume: Resume, matches: Matches) -> dict[str, list[str]]:
    """Valid evidence ids per requirement id; the last match per requirement wins."""
    last = {m.requirement_id: m.evidence_ids for m in matches.matches}
    return {rid: _valid_evidence(resume, ids) for rid, ids in last.items()}


def supported_requirements(resume: Resume, reqs: list[Requirement], matches: Matches
                           ) -> tuple[list[Requirement], list[Requirement]]:
    """(supported, gaps). Only real evidence ids count, and matches for unknown requirements are ignored."""
    evidence = _evidence(resume, matches)
    return ([r for r in reqs if evidence.get(r.id)], [r for r in reqs if not evidence.get(r.id)])


def write(client, settings: Settings, resume: Resume, reqs: Requirements, matches: Matches,
          previous: Draft | None = None, instructions: list[str] | None = None,
          notes: str = "") -> Draft:
    evidence = _evidence(resume, matches)
    sup, gap_reqs = supported_requirements(resume, reqs.requirements, matches)
    supported = [{**r.model_dump(), "evidence_ids": evidence[r.id]} for r in sup]
    gaps = [r.text for r in gap_reqs]
    supported_json = json.dumps(supported, indent=1, ensure_ascii=False)
    gap_lines = "\n".join(f"- {g}" for g in gaps) or "- (none)"
    revision = ""
    if previous is not None or instructions:
        lines = "\n".join(f"{n}. {t}" for n, t in enumerate(instructions or [], 1)) or "(none)"
        current = previous.model_dump_json(indent=1) if previous is not None else "(none)"
        revision = f"""
--- CURRENT VERSION ---
{current}

--- THE CANDIDATE'S CHANGE REQUESTS (oldest first) ---
{lines}

Apply the candidate's change requests only where the master supports them; never add facts the sources don't state.
"""
    notes_block = f"\nNotes from the candidate (respect these):\n{notes.strip()}\n" if notes.strip() else ""
    prompt = f"""You tailor a candidate's resume to a job posting by selecting and rewording their real master-resume material.

Rules:
- Use only facts in the cited source bullets. Every bullet you write must cite at least one source_id from the same entry (the ids of master bullets in that entry).
- Write each bullet as one or two short, plain sentences that lead with what the candidate did and state the result, e.g. "Built a search index for support tickets. Lookup time dropped from 2 minutes to 5 seconds." Show impact (the X, Y and Z of the work) in natural wording; never use the template phrases "as measured by" or "by doing".
- no new numbers, metrics, tools, domains, employers, dates or outcomes beyond what the cited sources state. Keep every number you use exactly as in the source.
- use the posting's wording only where the source supports it. Never add a skill or claim the sources do not show.
- Fewer honest bullets are better than stretched ones. Drop weak bullets rather than padding.
- Order entries as in the master; entry_id must be a master entry id. Use only bullets from that entry as sources.
- summary: a short rewrite of the master summary that uses only facts from the master.
- skills: keep category names (category_id is the master id, e.g. "s0"); reorder or drop items to foreground those relevant to the posting. Use only items present in the master category.
- Do not write blurbs; they are copied from the master separately.
- ranking: the indexes of your bullets, best first, over the flattened list of draft bullets in entry order (entries as listed in your entries, then each entry's bullets in order, counting from 0).

{notes_block}
--- MASTER RESUME (ids and texts) ---
{_master_json(resume)}

--- REQUIREMENTS WITH SUPPORTING EVIDENCE ---
{supported_json}

--- Gaps (do not write about these) ---
{gap_lines}
{revision}"""
    return _call(client, settings, prompt, Draft)


def judge(client, settings: Settings, pairs: list[tuple[str, list[str]]]) -> Verdicts:
    items = [{"index": i, "rewritten": text, "sources": sources} for i, (text, sources) in enumerate(pairs)]
    prompt = f"""You are a strict fact checker for resume bullets.

For each item, decide whether the rewritten text is fully supported by its source texts.
supported = true only if every claim, number, tool, domain and outcome in the rewritten text is stated in the sources.
supported = false if it adds, inflates or changes anything. Give a short reason either way.
Return one verdict per item, using its index.

{json.dumps(items, indent=1, ensure_ascii=False)}
"""
    return _call(client, settings, prompt, Verdicts)
