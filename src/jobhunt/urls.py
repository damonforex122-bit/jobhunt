from urllib.parse import parse_qs, unquote, urlsplit

_GREENHOUSE_HOSTS = {"job-boards.greenhouse.io", "boards.greenhouse.io"}
_CASE_INSENSITIVE_HOSTS = {"jobs.ashbyhq.com", "jobs.lever.co"}  # path ids/slugs ignore case


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/")
    if path.endswith("/application"):
        path = path[: -len("/application")].rstrip("/")
    host = parts.netloc.lower()
    if host in _CASE_INSENSITIVE_HOSTS:
        path = path.lower()
    elif host == "boards.greenhouse.io":
        host = "job-boards.greenhouse.io"
    query = ""
    gh_jid = parse_qs(parts.query).get("gh_jid")  # custom careers pages: the job id lives here
    if gh_jid and gh_jid[0]:
        query = f"?gh_jid={gh_jid[0]}"
    return f"{parts.scheme.lower()}://{host}{path}{query}"


def ats_from_url(url: str) -> tuple[str, str] | None:
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    segments = [s for s in parts.path.split("/") if s]
    if not segments:
        return None
    slug = unquote(segments[0])
    if host == "jobs.ashbyhq.com":
        return ("ashby", slug)
    if host in _GREENHOUSE_HOSTS:
        return ("greenhouse", slug)
    if host == "jobs.lever.co":
        return ("lever", slug)
    return None
