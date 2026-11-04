"""Extract hiring-manager hints from a job posting's text."""
import re

from jobhunt.models import Person

_CAP = r"[A-Z][a-z]+(?:[-'][A-Z][a-z]+)?"
_NAME = rf"{_CAP}(?: {_CAP}){{1,2}}"
_TITLE_WORD = r"[A-Z][\w&/-]*"
_TITLE = rf"{_TITLE_WORD}(?: (?:of |and |& )?{_TITLE_WORD}){{0,5}}"

_REPORTS_TO = re.compile(
    rf"\b[Rr]eports? (?:directly )?to (?:the |our )?(?P<title>{_TITLE})"
)
_WORK_WITH = re.compile(
    rf"\b[Yy]ou(?:'ll|’ll| will) (?:work|partner) (?:closely )?with "
    rf"(?P<name>{_NAME}), (?:our |the )?(?P<title>{_TITLE})"
)
_HIRING_MGR = re.compile(rf"\bhiring manager:? (?P<name>{_NAME})", re.I)
_LED_BY = re.compile(rf"\bled by (?P<name>{_NAME})")


def extract_people(description: str, job_url: str, company: str) -> list[Person]:
    text = description or ""
    found: dict[str, Person] = {}

    def add(name: str, title: str | None, confidence: float) -> None:
        name = name.strip()
        if name not in found:
            found[name] = Person(
                company=company, name=name, title=title, source="posting",
                source_url=job_url, confidence=confidence,
            )

    for m in _WORK_WITH.finditer(text):
        add(m["name"], m["title"].strip(), 0.7)
    for m in _HIRING_MGR.finditer(text):
        # re.I would let the name pattern match lowercase words; require capitals
        if re.fullmatch(_NAME, m["name"]):
            add(m["name"], "Hiring Manager", 0.7)
    for m in _LED_BY.finditer(text):
        add(m["name"], None, 0.6)
    for m in _REPORTS_TO.finditer(text):
        title = m["title"].strip()
        add(f"{title} (unnamed)", title, 0.3)
    return list(found.values())


_FALLBACK = ["Head of Research", "Engineering Manager", "CTO", "Co-founder"]


def manager_titles(title: str) -> list[str]:
    t = title.lower()
    out: list[str] = []
    if "eval" in t or "benchmark" in t:
        out += ["Head of Evals", "Evals Lead"]
    if "data" in t:
        out += ["Head of Data", "Data Lead"]
    if "applied" in t:
        out += ["Head of Applied AI", "Applied AI Lead"]
    if "research" in t or "eval" in t or "benchmark" in t:
        out += ["Research Lead", "Head of Research"]
    if "engineer" in t:
        out.append("Engineering Manager")
    if not out:
        return list(_FALLBACK)
    return list(dict.fromkeys(out))
