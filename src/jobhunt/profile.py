"""Read-only loaders for the user's preferences and resume text."""
import re
from pathlib import Path

from .models import Preferences

DEFAULT_LOCATIONS = ["San Francisco", "New York"]
DEFAULT_SALARY_FLOOR = 180000
DEFAULT_SEARCH_TERMS = [
    "evals", "benchmarks", "human data", "structured extraction", "applied research",
]
DEFAULT_EXCLUDED_TERMS = ["post-training", "RL", "reinforcement learning", "frontier lab"]

_SEPARATOR = "\n\n---\n\n"


def _sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            current = sections.setdefault(line[3:].strip().lower(), [])
        elif current is not None:
            current.append(line)
    return sections


def _find(sections: dict[str, list[str]], prefix: str) -> list[str] | None:
    for name, lines in sections.items():
        if name.startswith(prefix):
            return lines
    return None


def _bullets(lines: list[str]) -> list[str]:
    return [m.group(1).strip() for line in lines if (m := re.match(r"\s*[-*]\s+(.+)", line))]


def _parse_locations(lines: list[str]) -> list[str]:
    text = " ".join(line.strip() for line in lines if line.strip())
    parts = re.split(r"\s+or\s+|\s+and\s+|[,/;]", text)
    return [p.strip() for p in parts if p.strip()]


def _parse_salary(lines: list[str]) -> int | None:
    text = " ".join(lines)
    if m := re.search(r"\$\s*(\d[\d,]*(?:\.\d+)?)\s*([kK])?", text):
        value = float(m.group(1).replace(",", ""))
        return int(value * 1000) if m.group(2) else int(value)
    return None


def _parse_search_terms(lines: list[str]) -> list[str]:
    for line in lines:
        if "around:" in line:
            raw = line.split("around:", 1)[1].strip().rstrip(".")
            terms = [re.sub(r"^and\s+", "", t.strip()) for t in raw.split(",")]
            return [t for t in terms if t]
    return []


def _parse_excluded(lines: list[str]) -> list[str]:
    terms: list[str] = []
    for line in lines:
        if re.search(r"do not use|don't use|avoid|never", line, re.I):
            terms += re.findall(r'"([^"]+)"', line)
    return terms


def load_preferences(dir: Path) -> Preferences:
    path = Path(dir) / "preferences.md"
    sections = _sections(path.read_text()) if path.is_file() else {}

    loc_lines = _find(sections, "location")
    comp_lines = _find(sections, "compensation")
    focus_lines = _find(sections, "search focus") or []
    roles_lines = _find(sections, "target roles")

    return Preferences(
        locations=(_parse_locations(loc_lines) if loc_lines else []) or list(DEFAULT_LOCATIONS),
        salary_floor=(_parse_salary(comp_lines) if comp_lines else None) or DEFAULT_SALARY_FLOOR,
        search_terms=_parse_search_terms(focus_lines) or list(DEFAULT_SEARCH_TERMS),
        excluded_terms=_parse_excluded(focus_lines) or list(DEFAULT_EXCLUDED_TERMS),
        target_roles=_bullets(roles_lines) if roles_lines else [],
    )


def load_resume_text(dir: Path) -> str:
    dir = Path(dir)
    chunks: list[str] = []
    profile = dir / "profile.md"
    if profile.is_file():
        chunks.append(profile.read_text())
    resume_dir = dir / "resume"
    if resume_dir.is_dir():
        from pypdf import PdfReader

        for pdf in sorted(resume_dir.glob("*.pdf")):
            try:
                text = "\n".join(page.extract_text() or "" for page in PdfReader(pdf).pages)
            except Exception:
                continue  # an unreadable PDF should not block grading
            if text.strip():
                chunks.append(text.strip())
    return _SEPARATOR.join(chunks)
