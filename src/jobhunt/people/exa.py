"""Exa web search for LinkedIn profiles of likely hiring managers."""
from urllib.parse import urlparse

import httpx

from jobhunt.models import Person

from .apollo import SourceError

URL = "https://api.exa.ai/search"


def _host_is_linkedin(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "linkedin.com" or host.endswith(".linkedin.com")


def _parse_title(raw: str) -> tuple[str, str | None] | None:
    """'Name - Title - Company | LinkedIn' -> (name, title)."""
    raw = raw.rsplit("|", 1)[0].strip() if "|" in raw else raw.strip()
    parts = [p.strip() for p in raw.split(" - ")]
    if len(parts) < 2 or not parts[0]:
        return None
    return parts[0], parts[1] or None


def search_people(
    company: str, titles: list[str], api_key: str, client: httpx.Client
) -> list[Person]:
    body = {
        "query": f"{titles[0]} at {company} LinkedIn",
        "numResults": 10,
        "type": "auto",
        "category": "people",
    }
    try:
        resp = client.post(URL, json=body, headers={"x-api-key": api_key})
        resp.raise_for_status()
        data = resp.json()
    except httpx.TimeoutException:
        raise SourceError("timeout") from None
    except httpx.HTTPStatusError as e:
        raise SourceError(f"HTTP {e.response.status_code}") from None
    except httpx.HTTPError as e:
        raise SourceError(f"request failed: {type(e).__name__}") from None
    except ValueError:
        raise SourceError("bad JSON") from None

    people: list[Person] = []
    for r in (data.get("results") or []) if isinstance(data, dict) else []:
        raw, url = r.get("title") or "", r.get("url")
        if not url or company.lower() not in raw.lower():
            continue
        parsed = _parse_title(raw)
        if parsed is None:
            continue
        name, title = parsed
        people.append(
            Person(
                company=company, name=name, title=title, source="exa",
                source_url=url, linkedin_url=url if _host_is_linkedin(url) else None,
                confidence=0.5,
            )
        )
    return people
