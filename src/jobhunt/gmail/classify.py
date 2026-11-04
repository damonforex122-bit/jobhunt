"""Rules that read a recruiting email: which stage it implies and which job it is about."""
import re
from email.utils import parseaddr

from ..models import CLOSED, PIPELINE

# Checked in this order: a rejection that mentions "interview" is still a rejection.
_RULES: list[tuple[str, list[str]]] = [
    ("rejected", [
        "unfortunately", "not to move forward", "not be moving forward", "not moving forward",
        "will not be proceeding", "decided to pursue other candidates",
        "move forward with other candidates", "position has been filled",
    ]),
    ("offer", ["extend an offer", "pleased to offer", "offer letter", "verbal offer",
               "offer of employment"]),
    ("interviewing", ["onsite", "on-site", "final round", "panel interview", "technical interview",
                      "interview loop", "take-home", "take home assignment",
                      "meet the team", "hiring manager interview", "interview with the team"]),
    ("screening", ["phone screen", "recruiter call", "intro call", "introductory call",
                   "initial call", "screening call", "schedule a call", "schedule an interview",
                   "set up a call", "first interview", "availability for a call"]),
    ("applied", ["thank you for applying", "thanks for applying", "received your application",
                 "application received", "application has been received",
                 "thanks for your interest", "thank you for your interest"]),
]

_RANK = {s: i + 1 for i, s in enumerate(PIPELINE)}  # applied=1 ... offer=4; triage = 0


def classify(subject: str, body: str) -> str | None:
    text = " ".join(f"{subject} {body}".lower().split())
    for stage, phrases in _RULES:
        if any(p in text for p in phrases):
            return stage
    return None


def should_suggest(current: str, suggested: str) -> bool:
    """Only moves that go forward, or that close a job that is still open."""
    if current in CLOSED:
        return False
    if suggested in CLOSED:
        return True
    return _RANK.get(suggested, 0) > _RANK.get(current, 0)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _name_in(text: str, company: str) -> bool:
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(company.lower())}(?![a-z0-9])", text.lower()))


def _domain_matches(address: str, company: str) -> bool:
    domain = address.rsplit("@", 1)[-1].lower()
    labels = domain.split(".")[:-1]  # drop the TLD
    name = _norm(company)
    short = re.sub(r"(ai|labs|inc|hq)$", "", name)
    return any(label.replace("-", "") in {name, short} for label in labels if label)


def match_job(jobs: list[dict], sender: str, subject: str, body: str) -> dict | None:
    """Pick the job an email is about. The company must appear in the sender or subject."""
    display, address = parseaddr(sender)
    header = f"{display} {subject}"
    companies = {j["company"] for j in jobs}
    hits = [c for c in companies if _name_in(header, c) or _domain_matches(address, c)]
    if not hits:
        return None
    # Several names can hit ("Scale" and "Scale AI"): keep the longest if it contains the rest.
    company = max(hits, key=len)
    if not all(h.lower() in company.lower() for h in hits):
        return None
    candidates = [j for j in jobs if j["company"] == company]
    text = f"{subject} {body}".lower()
    titled = [j for j in candidates if j["title"].lower() in text]
    if titled:
        return max(titled, key=lambda j: len(j["title"]))
    return max(candidates, key=lambda j: (_RANK.get(j["status"], 0), j["id"]))
