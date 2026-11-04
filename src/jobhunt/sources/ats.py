"""Public job-board fetchers for Greenhouse, Ashby and Lever."""
import html
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml

from jobhunt.models import Company, Job


class FetchError(Exception):
    def __init__(self, company: str, reason: str):
        self.company = company
        self.reason = reason
        super().__init__(f"{company}: {reason}")


_BLOCK_TAGS = re.compile(r"</?(p|br|div|li|ul|ol|h[1-6]|tr)\b[^>]*>", re.I)
_TAGS = re.compile(r"<[^>]+>")


def _strip_html(text: str | None) -> str:
    if not text:
        return ""
    text = html.unescape(text)
    text = _BLOCK_TAGS.sub("\n", text)
    text = _TAGS.sub("", text)
    text = html.unescape(text).replace("\xa0", " ")
    lines = (re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


# One malformed posting must not sink the board: it is skipped and counted.
_BAD_POSTING = (KeyError, TypeError, AttributeError, ValueError)  # ValueError incl. ValidationError


def _greenhouse_job(co: Company, j) -> Job:
    return Job(
        company=co.name,
        title=j["title"],
        url=j["absolute_url"],
        location=(j.get("location") or {}).get("name"),
        description=_strip_html(j.get("content")),
        source="greenhouse",
        posted_at=j.get("updated_at"),
    )


def _ashby_job(co: Company, j) -> Job:
    locs = [j.get("location")] + [s.get("location") for s in j.get("secondaryLocations") or []]
    location = "; ".join(l for l in locs if l) or None
    desc = j.get("descriptionPlain") or ""
    summary = (j.get("compensation") or {}).get("compensationTierSummary")
    if summary:
        desc = f"Compensation: {summary}\n\n{desc}"
    return Job(
        company=co.name,
        title=j["title"],
        url=j["jobUrl"],
        location=location,
        description=desc,
        source="ashby",
        posted_at=j.get("publishedAt"),
    )


def _lever_job(co: Company, j) -> Job:
    sr = j.get("salaryRange") or {}
    created = j.get("createdAt")
    posted = (
        datetime.fromtimestamp(created / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        if created
        else None
    )
    return Job(
        company=co.name,
        title=j["text"],
        url=j["hostedUrl"],
        location=(j.get("categories") or {}).get("location"),
        salary_min=int(sr["min"]) if sr.get("min") is not None else None,
        salary_max=int(sr["max"]) if sr.get("max") is not None else None,
        description=j.get("descriptionPlain"),
        source="lever",
        posted_at=posted,
        flags=["non_usd"] if sr.get("currency") and sr["currency"].upper() != "USD" else [],
    )


# (url template, function selecting the posting list from the response, per-posting parser)
_ENDPOINTS = {
    "greenhouse": ("https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
                   lambda d: d["jobs"], _greenhouse_job),
    "ashby": ("https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true",
              lambda d: d["jobs"], _ashby_job),
    "lever": ("https://api.lever.co/v0/postings/{slug}?mode=json", lambda d: list(d), _lever_job),
}


def fetch_jobs(company: Company, client: httpx.Client, stats: dict | None = None) -> list[Job]:
    """`stats["skipped"]` is incremented for each malformed posting that was dropped."""
    if company.ats not in _ENDPOINTS or not company.slug:
        raise FetchError(company.name, "no ATS/slug configured")
    template, select, parse_one = _ENDPOINTS[company.ats]
    try:
        resp = client.get(template.format(slug=company.slug))
        resp.raise_for_status()
        data = resp.json()
    except httpx.TimeoutException:
        raise FetchError(company.name, "timeout") from None
    except httpx.HTTPStatusError as e:
        raise FetchError(company.name, f"HTTP {e.response.status_code}") from None
    except httpx.HTTPError as e:
        raise FetchError(company.name, f"request failed: {e}") from None
    except ValueError:
        raise FetchError(company.name, "bad JSON") from None
    try:
        items = select(data)
    except (KeyError, TypeError, AttributeError, ValueError) as e:
        raise FetchError(company.name, f"unexpected response shape: {e!r}") from None
    jobs = []
    for item in items:
        try:
            jobs.append(parse_one(company, item))
        except _BAD_POSTING:
            if stats is not None:
                stats["skipped"] = stats.get("skipped", 0) + 1
    return jobs


def load_watchlist(path: Path) -> list[Company]:
    path = Path(path)
    if not path.exists():
        return []
    entries = yaml.safe_load(path.read_text()) or []
    return [Company(**e) for e in entries]
