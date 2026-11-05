"""Turn calendar invites in Gmail into interview rows. Invites never create pipeline suggestions.
Email bodies and snippets are never stored."""
from datetime import datetime, timezone
from email.utils import parseaddr

import httpx

from .. import db
from ..config import Settings
from .auth import access_token
from .classify import extract_company, match_job
from .client import GmailClient
from .invites import Invite, local_tz, parse_ics, parse_subject

INVITE_QUERY = 'newer_than:{days}d (filename:ics OR subject:(invitation OR invite))'


def _utc(d: datetime | None) -> str | None:
    return d.astimezone(timezone.utc).isoformat(timespec="seconds") if d else None


def _invites(msg, now: datetime, tz) -> list[Invite]:
    found: list[Invite] = []
    for text in msg.ics:
        found += parse_ics(text, tz)
    if not found:
        one = parse_subject(msg.subject, now, tz)
        found = [one] if one else []
    return found


def run_interview_sync(conn, settings: Settings, client: GmailClient | None = None, days: int = 30,
                       *, tz=None, now: datetime | None = None) -> int:
    if client is None:
        client = GmailClient(access_token(settings), httpx.Client())
    tz = tz or local_tz()
    now = now or datetime.now(tz)
    me = client.profile_email().lower()
    seen = db.interview_seen_ids(conn)
    jobs = db.jobs_for_matching(conn)
    changed = 0
    # Gmail lists newest first; apply oldest first so the latest update of an event wins.
    for mid in reversed(client.list_ids(INVITE_QUERY.format(days=days))):
        if mid in seen:
            continue
        msg = client.get(mid)
        if not (me and parseaddr(msg.sender)[1].lower() == me):
            job = match_job(jobs, msg.sender, msg.subject, msg.body)
            company = job["company"] if job else extract_company(msg.sender, msg.subject)
            for inv in _invites(msg, now, tz):
                db.upsert_interview(conn, {
                    "uid": inv.uid or f"gmail:{mid}", "gmail_id": mid,
                    "job_id": job["id"] if job else None, "company": company,
                    "start": _utc(inv.start), "end": _utc(inv.end), "summary": inv.summary,
                    "location": inv.location, "link": inv.link, "sender": msg.sender,
                    "subject": msg.subject, "status": "cancelled" if inv.cancelled else "scheduled"})
                changed += 1
        db.mark_interview_seen(conn, mid)
    return changed
