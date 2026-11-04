"""Rules that read a recruiting email: which stage it implies and which job it is about."""
import re
from email.utils import parseaddr

from ..models import CLOSED, PIPELINE

_REJECTED = [
    "unfortunately", "not to move forward", "not be moving forward", "not moving forward",
    "will not be proceeding", "decided to pursue other candidates",
    "move forward with other candidates", "position has been filled",
]
# The subject says the most about where things stand, so it is read first; the body is a
# fallback (bodies carry boilerplate like "thanks for your interest" in every email).
_SUBJECT_RULES: list[tuple[str, list[str]]] = [
    ("offer", ["offer letter", "extend an offer", "offer from", "your offer"]),
    ("interviewing", ["interview", "onsite", "on-site", "final round", "take-home", "take home"]),
    ("screening", ["next step", "phone screen", "recruiter call", "intro call", "introductory call",
                   "looking forward to our conversation", "let's chat", "quick chat"]),
    ("applied", ["thank you for applying", "thanks for applying", "your application",
                 "application received", "application submitted", "received your application",
                 "application has been received", "got your", "thanks for your interest",
                 "thank you for your interest"]),
]
_BODY_RULES: list[tuple[str, list[str]]] = [
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
# Among a company's not-yet-applied jobs, an email is most likely about the one you rated best.
_TRIAGE_PREF = {"ready": 3, "needs": 2, "new": 1, "low": 0, "skip": -1}


def _flat(text: str) -> str:
    return " ".join(text.lower().split())


def _first(text: str, rules: list[tuple[str, list[str]]]) -> str | None:
    for stage, phrases in rules:
        if any(p in text for p in phrases):
            return stage
    return None


def classify(subject: str, body: str) -> str | None:
    subj, text = _flat(subject), _flat(body)
    if any(p in f"{subj} {text}" for p in _REJECTED):
        return "rejected"
    return _first(subj, _SUBJECT_RULES) or _first(text, _BODY_RULES)


# Applicant-tracking and interview-scheduling senders: their mail is never a newsletter.
_ATS_DOMAINS = ("ashbyhq.com", "greenhouse-mail.io", "greenhouse.io", "lever.co", "myworkday.com",
                "workday.com", "smartrecruiters.com", "icims.com", "jobvite.com", "rippling.com",
                "rippling-ats.com", "workablemail.com", "workable.com", "getrounds.co",
                "goodtime.io", "gem.com", "teamtailor.com", "breezy.hr", "bamboohr.com")


def is_ats_sender(sender: str) -> bool:
    domain = parseaddr(sender)[1].rsplit("@", 1)[-1].lower()
    return any(domain == d or domain.endswith("." + d) for d in _ATS_DOMAINS)


_PREFIX = re.compile(r"^(re|fwd?|reminder|following up|great news|invitation|updated invitation"
                     r"(?: with note)?|invitation from an unknown sender)\s*:\s*", re.I)
_DASH = r"\s+[-–—|]\s+"
_SUBJECT_COMPANY = [re.compile(p, re.I) for p in (
    r"interview with (?P<c>.+?)(?:\s*@|\s*[-–—|:,!(]|$)",
    r"(?:applying|applied) (?:to|at) (?P<c>.+?)(?:[-–—|:,!(]|$)",
    r"interest in (?P<c>.+?)(?:[-–—|:,!(]|$)",
    r"(?:for|schedule) your (?P<c>.+?) interview",
    r"^(?P<c>.+?) wants to (?:invite|interview|meet|chat)",
    r"application (?:for|to) (?P<c>[^'’]+?)['’]s\b",
    r"^your (?P<c>.+?) application\b",
    rf"^(?P<c>[^-–—|:@]+?){_DASH}",
)]
_SENDER_SUFFIX = re.compile(r"\s+(hiring team|recruiting( team)?|talent( team| acquisition)?|careers"
                            r"|jobs|people team|team)$", re.I)
_GENERIC = {"no-reply", "noreply", "no reply", "the team", "our team", "us", "you", "team", "rounds",
            "hiring", "recruiting", "your", "the"}


_CONNECTORS = {"&", "and", "of", "de", "for"}


def _clean_company(raw: str) -> str | None:
    name = re.sub(r"[^\w&.'’)]+$", "", raw.strip()).strip()
    name = re.sub(r"^(the|your|my|upcoming)\s+", "", name, flags=re.I).strip()
    if not (2 <= len(name) <= 40) or name.lower() in _GENERIC or "@" in name:
        return None
    # Company names look like names: at most 4 words, each capitalized (connectors aside).
    words = name.split()
    if len(words) > 4 or not all(w[0].isupper() or w[0].isdigit() or w.lower() in _CONNECTORS
                                 for w in words):
        return None
    return name


def extract_company(sender: str, subject: str) -> str | None:
    """Best guess at the company an email is from, for companies the app does not know yet."""
    subj = subject.strip()
    while _PREFIX.match(subj):
        subj = _PREFIX.sub("", subj, count=1)
    for pattern in _SUBJECT_COMPANY:
        m = pattern.search(subj)
        if m and (name := _clean_company(m.group("c"))):
            return name
    display = parseaddr(sender)[0]
    if display and is_ats_sender(sender):
        name = re.sub(r"\s*(?:HR)?\s*Workday$", "", _SENDER_SUFFIX.sub("", display), flags=re.I)
        return _clean_company(name)
    return None


def stage_rank(status: str | None) -> int:
    return _RANK.get(status or "", 0)


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
    return max(candidates, key=lambda j: (_RANK.get(j["status"], 0),
                                          _TRIAGE_PREF.get(j["status"], 0), j["id"]))
