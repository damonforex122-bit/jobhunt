"""Apollo People API Search (mixed_people/api_search)."""
import httpx

from jobhunt.models import Company, Person

URL = "https://api.apollo.io/api/v1/mixed_people/api_search"


class SourceError(Exception):
    pass


def search_people(
    company: Company, titles: list[str], api_key: str, client: httpx.Client
) -> list[Person]:
    if not company.domain:
        # Apollo's API search has no organization-name filter; without a domain
        # the results would not be scoped to this company.
        raise SourceError("no company domain known for Apollo search")
    # Apollo documents these as query-string parameters (array keys use "[]").
    params: list[tuple[str, str]] = [("q_organization_domains_list[]", company.domain)]
    params += [("person_titles[]", t) for t in titles]
    params.append(("per_page", "10"))
    try:
        resp = client.post(URL, params=params, headers={"x-api-key": api_key})
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
    for p in (data.get("people") or []) if isinstance(data, dict) else []:
        first = (p.get("first_name") or "").strip()
        pid = p.get("id")
        if not first or not pid:
            continue
        last = (p.get("last_name") or "").strip()
        full = bool(last) and "*" not in last
        if not full:
            last = (p.get("last_name_obfuscated") or "").strip()
        linkedin = p.get("linkedin_url") or None
        people.append(
            Person(
                company=company.name, name=f"{first} {last}".strip(), title=p.get("title"),
                source="apollo", source_url=f"https://app.apollo.io/#/people/{pid}",
                linkedin_url=linkedin, confidence=0.8 if full else 0.6,
            )
        )
    return people
