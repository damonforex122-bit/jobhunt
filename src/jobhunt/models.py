from typing import Literal

from pydantic import BaseModel


class Company(BaseModel):
    id: int | None = None
    name: str
    ats: Literal["greenhouse", "ashby", "lever"] | None = None
    slug: str | None = None
    domain: str | None = None
    last_scanned: str | None = None
    last_error: str | None = None


class Job(BaseModel):
    id: int | None = None
    company: str
    title: str
    url: str
    location: str | None = None
    salary_min: int | None = None
    salary_max: int | None = None
    description: str | None = None
    source: str
    posted_at: str | None = None
    first_seen: str | None = None
    status: str = "new"
    flags: list[str] = []


class Person(BaseModel):
    id: int | None = None
    company: str
    job_id: int | None = None
    name: str
    title: str | None = None
    source: Literal["posting", "apollo", "exa"]
    source_url: str
    linkedin_url: str | None = None
    confidence: float


class Fit(BaseModel):
    job_id: int
    keyword_score: int
    missing_keywords: list[str] = []
    llm_score: int | None = None
    match_pct: int | None = None
    reasons: str | None = None
    gaps: str | None = None
    rewritten_bullets: list[dict] = []
    model: str | None = None
    graded_at: str
    error: str | None = None


class Preferences(BaseModel):
    locations: list[str]
    salary_floor: int
    search_terms: list[str]
    excluded_terms: list[str]
    target_roles: list[str] = []


# Job statuses. Triage statuses come from grading; pipeline and closed ones are set by hand.
TRIAGE = ("new", "ready", "needs", "low", "skip")
PIPELINE = ("applied", "screening", "interviewing", "offer")
CLOSED = ("accepted", "rejected", "withdrawn")
STATUSES = TRIAGE + PIPELINE + CLOSED
