"""Preference filters: salary parsing, location matching, title relevance."""
import re
from typing import Literal

from .models import Job, Preferences

_AMOUNT = r"(\$)?\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*([kK])?\b"
_RANGE_RE = re.compile(_AMOUNT + r"(?:\s*(?:-|–|—|to)\s*" + _AMOUNT + r")?")


def _value(num: str, k: str | None) -> int:
    return int(float(num.replace(",", "")) * (1000 if k else 1))


def parse_salary(text: str | None) -> tuple[int | None, int | None]:
    """Parse a USD salary string into (min, max); (None, None) when unparseable."""
    if not text:
        return None, None
    for m in _RANGE_RE.finditer(text):
        d1, n1, k1, d2, n2, k2 = m.groups()
        if not (d1 or k1 or d2 or k2):
            continue  # bare number such as "5 years"
        lo = _value(n1, k1)
        hi = _value(n2, k2) if n2 else lo
        if n2 and k2 and not k1 and lo < 1000:
            lo *= 1000  # "150-250k"
        if min(lo, hi) < 1000:
            continue  # hourly rates etc.
        return min(lo, hi), max(lo, hi)
    return None, None


_ALIASES = {
    "san francisco": ["sf"],
    "new york": ["nyc", "new york city"],
}
_PART_SPLIT = re.compile(r"[;/|]|\s+or\s+", re.I)


def _part_matches(part: str, targets: list[str]) -> bool:
    for target in targets:
        names = [target.lower(), *_ALIASES.get(target.lower(), [])]
        for name in names:
            if re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", part):
                return True
    return False


def location_match(
    location: str | None, targets: list[str]
) -> Literal["match", "remote", "unknown", "no"]:
    if not location or not location.strip():
        return "unknown"
    parts = [p.strip().lower() for p in _PART_SPLIT.split(location) if p.strip()]
    if any(_part_matches(p, targets) for p in parts):
        return "match"
    if any("remote" in p for p in parts):
        return "remote"
    return "no"


def _phrase_re(phrase: str) -> re.Pattern:
    return re.compile(rf"(?<![a-z0-9]){re.escape(phrase.lower())}(?![a-z0-9])")


def _contains(text: str, phrase: str) -> bool:
    return bool(_phrase_re(phrase).search(text.lower()))


def _singular_forms(term: str) -> list[str]:
    forms = [term]
    if term.lower().endswith("s") and len(term) > 4:
        forms.append(term[:-1])
    return forms


def _role_keywords(roles: list[str], excluded: list[str]) -> list[str]:
    """Reduce free-text target-role bullets to short phrases usable for title matching."""
    keywords: list[str] = []
    for role in roles:
        for piece in re.split(r"[/,()]|\s+at\s+", role):
            piece = re.sub(r"\s+roles?$", "", piece.strip(), flags=re.I).strip()
            if not piece or any(_contains(piece, x) for x in excluded):
                continue
            keywords.append(piece)
    return keywords


def title_match(title: str, description: str | None, prefs: Preferences) -> bool:
    terms = [t for t in prefs.search_terms if not any(_contains(t, x) for x in prefs.excluded_terms)]
    title_phrases = [f for t in terms for f in _singular_forms(t)]
    title_phrases += _role_keywords(prefs.target_roles, prefs.excluded_terms)
    if any(_contains(title, p) for p in title_phrases):
        return True
    if description:
        hits = sum(any(_contains(description, f) for f in _singular_forms(t)) for t in terms)
        return hits >= 2
    return False


def evaluate(job: Job, prefs: Preferences) -> tuple[bool, list[str]]:
    flags: list[str] = []
    loc = location_match(job.location, prefs.locations)
    if loc == "no":
        return False, []
    if loc == "remote":
        flags.append("remote")
    elif loc == "unknown":
        flags.append("location_unknown")
    if not title_match(job.title, job.description, prefs):
        return False, []
    floor = prefs.salary_floor
    if job.salary_max is not None and job.salary_max < floor:
        return False, []
    if job.salary_min is not None and job.salary_min < floor:
        flags.append("below_floor_min")
    return True, flags
