"""Minimal read-only Gmail REST client (messages.list and messages.get)."""
import base64
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from ..sources.ats import _strip_html

API = "https://gmail.googleapis.com/gmail/v1/users/me"


@dataclass
class Message:
    id: str
    sender: str
    subject: str
    date: str  # YYYY-MM-DD (UTC)
    snippet: str
    body: str


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
    def __init__(self, token: str, http: httpx.Client):
        self.http = http
        self.headers = {"Authorization": f"Bearer {token}"}

    def _get(self, url: str, params: dict | None = None) -> dict:
        r = self.http.get(url, params=params, headers=self.headers, timeout=20)
        r.raise_for_status()
        return r.json()

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
                       date=date, snippet=d.get("snippet", ""), body=_body(payload))
