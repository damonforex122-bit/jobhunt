from urllib.parse import unquote, urlsplit

_GREENHOUSE_HOSTS = {"job-boards.greenhouse.io", "boards.greenhouse.io"}


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/")
    if path.endswith("/application"):
        path = path[: -len("/application")].rstrip("/")
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{path}"


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
