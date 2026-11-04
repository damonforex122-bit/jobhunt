"""Gemini-backed fit grading with a local keyword score computed first."""
import re
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from ..config import Settings
from ..models import Fit
from .keywords import keyword_fit


class Bullet(BaseModel):
    original: str
    rewritten: str
    keywords_used: list[str]


class FitResponse(BaseModel):
    match_pct: int = Field(ge=0, le=100)
    fit_score: int = Field(ge=1, le=10)
    missing_keywords: list[str]
    rewritten_bullets: list[Bullet]
    reasons: str
    gaps: str


def build_prompt(resume: str, posting: str, company: str, title: str) -> str:
    return f"""Act as a Fortune 500 technical recruiter and ATS algorithm specialist.
Compare my resume/profile to the job description below and respond in the requested JSON schema.

1. Keyword match score: give match_pct, a 0-100 keyword match percentage.
2. List the top 10 hard skills, tools and exact phrases from the posting that are missing from my resume (missing_keywords, at most 10).
3. Rewrite my experience bullets to integrate those exact keywords, using the formula [Action Verb] + [Specific Task] + [Quantified Result].
4. Do NOT invent fake experience—reframe my real work using their exact terminology.

Rules for rewritten_bullets:
- Copy each original bullet verbatim from my resume.
- Use a keyword in a bullet only when the original bullet already describes that work. A keyword I have no evidence for belongs in gaps, not in a bullet.
- Keep every number from the original and add no new numbers, metrics, tools, domains or outcomes.
- Fewer honest bullets are better than more stretched ones.

Also give fit_score, an overall fit from 1 to 10. fit_score 8+ means apply now; 7 or below means low priority.
Briefly explain in reasons (why it fits) and gaps (what is weak or missing).

Profile notes you must respect:
- Say "building", not "built", when describing my benchmark; it is still in progress.
- Never state the funding amount.

Company: {company}
Role: {title}

--- JOB DESCRIPTION ---
{posting}

--- MY RESUME / PROFILE ---
{resume}
"""


def _norm(text: str) -> str:
    return " " + " ".join(re.findall(r"[a-z0-9]+", text.lower())) + " "


_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in _NUMBER.findall(text)}


def check_bullets(bullets: list[Bullet], resume: str) -> tuple[list[Bullet], list[str]]:
    """Keep only bullets whose original is in the resume and that add no new numbers."""
    resume_norm = _norm(resume)
    kept, problems = [], []
    for b in bullets:
        original = _norm(b.original)
        if not original.strip() or original not in resume_norm:
            problems.append(f'"{b.original[:60]}" is not in your resume')
            continue
        known = _numbers(b.original)
        added = [n for n in dict.fromkeys(_NUMBER.findall(b.rewritten)) if n.replace(",", "") not in known]
        if added:
            problems.append(f'"{b.rewritten[:60]}" adds numbers not in the original ({", ".join(added)})')
            continue
        kept.append(b)
    return kept, problems


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _short_error(exc: Exception, settings: Settings) -> str:
    msg = f"{type(exc).__name__}: {exc}".replace("\n", " ")
    if settings.gemini_api_key:
        msg = msg.replace(settings.gemini_api_key, "***")
    return msg[:200]


def grade(job: dict, resume: str, settings: Settings, client=None) -> Fit:
    posting = (job.get("description") or "").strip()
    base = {"job_id": job["id"], "graded_at": _now(), "model": settings.gemini_model}
    if not posting:
        return Fit(keyword_score=0, error="No posting text", **base)
    kw_score, missing = keyword_fit(resume, posting)
    base.update(keyword_score=kw_score, missing_keywords=missing)

    if client is None:
        if not settings.gemini_api_key:
            return Fit(error="GEMINI_API_KEY not set", **base)
        try:
            from google import genai

            client = genai.Client(api_key=settings.gemini_api_key)
        except Exception as exc:
            return Fit(error=_short_error(exc, settings), **base)

    prompt = build_prompt(resume, posting, job.get("company", ""), job.get("title", ""))
    try:
        response = client.models.generate_content(
            model=settings.gemini_model,
            contents=prompt,
            config={
                "response_mime_type": "application/json",
                "response_schema": FitResponse,
                "temperature": 0.2,
            },
        )
        parsed = response.parsed
        if not isinstance(parsed, FitResponse):
            return Fit(error="Gemini returned no valid structured response", **base)
    except Exception as exc:
        return Fit(error=_short_error(exc, settings), **base)

    base["missing_keywords"] = parsed.missing_keywords[:10] or missing
    bullets, problems = check_bullets(parsed.rewritten_bullets, resume)
    gaps = parsed.gaps
    if problems:
        noun = "bullet" if len(problems) == 1 else "bullets"
        gaps = (f"{gaps}\n\nDropped {len(problems)} rewritten {noun} that went beyond "
                "your real experience: " + "; ".join(problems) + ".").strip()
    return Fit(
        llm_score=parsed.fit_score,
        match_pct=parsed.match_pct,
        reasons=parsed.reasons,
        gaps=gaps,
        rewritten_bullets=[b.model_dump() for b in bullets],
        **base,
    )
