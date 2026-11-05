"""Where tailored resumes go: per-company folders, collision-safe names, atomic writes."""

import os
import re
from collections.abc import Callable
from pathlib import Path

_BAD_CHARS = re.compile(r'[/\\:*?"<>|]')
_CORP_SUFFIX = re.compile(r"(,\s*Inc\.?|\s+Inc\.?|,\s*LLC)\s*$", re.IGNORECASE)
BASE_NAME = "Georgia Liu - Resume"


def _clean(text: str) -> str:
    text = _BAD_CHARS.sub("", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" .\t")


def company_folder(root: Path, company: str) -> Path:
    name = _clean(_CORP_SUFFIX.sub("", company.strip())) or "Unknown company"
    root = Path(root)
    if root.is_dir():
        for child in sorted(root.iterdir()):
            if child.is_dir() and child.name.casefold() == name.casefold():
                return child
    return root / name


def choose_path(folder: Path, title: str, owned: Path | None) -> Path:
    if owned is not None:
        return Path(owned)
    plain = folder / f"{BASE_NAME}.pdf"
    if not plain.exists():
        return plain
    clean_title = _clean(title)[:80] or "Role"
    named = folder / f"{BASE_NAME} ({clean_title}).pdf"
    n = 2
    while named.exists():
        named = folder / f"{BASE_NAME} ({clean_title}) {n}.pdf"
        n += 1
    return named


def write_atomic(render: Callable[[Path], None], dest: Path) -> None:
    """Render to a temp file beside `dest`, then replace it. A failure leaves `dest` untouched."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp.pdf")
    try:
        render(tmp)
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
