"""Minimal read-only Gmail REST client (messages.list and messages.get)."""
import base64
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from ..sources.ats import _strip_html

API = "https://gmail.googleapis.com/gmail/v1/users/me"
_RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}


class GmailError(Exception):
    """A Gmail API failure, with Google's status code and message."""


@dataclass
class Message:
    id: str
    sender: str
    subject: str
    date: str  # YYYY-MM-DD (UTC)
    snippet: str
    body: str
    bulk: bool = False  # has List-Unsubscribe / List-Id: newsletter-style mail


def _decode(data: str | None) -> str:
    if not data:
        return ""
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")


def _body(payload: dict) -> str:
    """Prefer text/plain anywhere in the MIME tree; fall back to stripped text/html."""
    plain, html = [], []
    stack = [payload]
    while stack:
        part = stack.pop()
        stack.extend(reversed(part.get("parts") or []))
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if mime == "text/plain":
            plain.append(_decode(data))
        elif mime == "text/html":
            html.append(_decode(data))
    if plain:
        return "\n".join(plain)
    return _strip_html("\n".join(html))


class GmailClient:
    def __init__(self, token: str, http: httpx.Client, sleep: Callable[[float], None] = time.sleep,
                 retries: int = 5):
        self.http = http
        self.headers = {"Authorization": f"Bearer {token}"}
        self.sleep, self.retries = sleep, retries

    @staticmethod
    def _error(r: httpx.Response) -> tuple[str, set[str]]:
        try:
            err = r.json().get("error") or {}
        except ValueError:
            return r.text[:150], set()
        return str(err.get("message", ""))[:150], {e.get("reason") for e in err.get("errors") or []}

    def _get(self, url: str, params: dict | None = None) -> dict:
        for attempt in range(self.retries + 1):
            r = self.http.get(url, params=params, headers=self.headers, timeout=20)
            if r.status_code < 400:
                return r.json()
            message, reasons = self._error(r)
            limited = r.status_code == 429 or (r.status_code == 403 and reasons & _RATE_REASONS)
            if not limited or attempt == self.retries:
                raise GmailError(f"Gmail API {r.status_code}: {message}")
            retry_after = r.headers.get("retry-after", "")
            self.sleep(float(retry_after) if retry_after.isdigit() else min(2 ** (attempt + 1), 60))
        raise AssertionError("unreachable")

    def list_ids(self, query: str, limit: int = 200) -> list[str]:
        ids, token = [], None
        while len(ids) < limit:
            params = {"q": query, "maxResults": min(100, limit - len(ids))}
            if token:
                params["pageToken"] = token
            data = self._get(f"{API}/messages", params)
            ids += [m["id"] for m in data.get("messages") or []]
            token = data.get("nextPageToken")
            if not token:
                break
        return ids[:limit]

    def get(self, mid: str) -> Message:
        d = self._get(f"{API}/messages/{mid}", {"format": "full"})
        payload = d.get("payload") or {}
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers") or []}
        ms = int(d.get("internalDate") or 0)
        date = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        return Message(id=mid, sender=headers.get("from", ""), subject=headers.get("subject", ""),
                       date=date, snippet=d.get("snippet", ""), body=_body(payload),
                       bulk="list-unsubscribe" in headers or "list-id" in headers)

    def profile_email(self) -> str:
        return self._get(f"{API}/profile").get("emailAddress", "")
