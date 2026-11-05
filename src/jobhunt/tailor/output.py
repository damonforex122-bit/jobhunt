"""Where tailored resumes go: per-company folders, collision-safe names, atomic writes."""

import hashlib
import os
import re
import tempfile
from collections.abc import Callable
from pathlib import Path

_BAD_CHARS = re.compile(r'[/\\:*?"<>|]')
_CORP_SUFFIX = re.compile(r"(,\s*Inc\.?|\s+Inc\.?|,\s*LLC)\s*$", re.IGNORECASE)


def _clean(text: str) -> str:
    text = _BAD_CHARS.sub("", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" .\t")


def resume_base_name(name: str | None) -> str:
    clean = _clean(name or "")
    return f"{clean} - Resume" if clean else "Resume"


def file_base_name(header_name: str | None, saved: str | None = None) -> str:
    """The saved name wins; else the header name without ", <credentials>"."""
    name = saved.strip() if saved and saved.strip() else (header_name or "").split(",")[0]
    return resume_base_name(name)


def company_folder(root: Path, company: str) -> Path:
    name = _clean(_CORP_SUFFIX.sub("", company.strip())) or "Unknown company"
    root = Path(root)
    if root.is_dir():
        for child in sorted(root.iterdir()):
            if child.is_dir() and child.name.casefold() == name.casefold():
                return child
    return root / name


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def reusable_owned(owned: Path | None, recorded_sha: str | None, other_paths: set[str],
                   root: Path) -> Path | None:
    """`owned` if it is still safe to overwrite: nobody else records it, it is inside `root`,
    and it is missing or byte-identical to what we last wrote."""
    if owned is None:
        return None
    owned = Path(owned)
    if str(owned) in other_paths:
        return None
    if not owned.resolve().is_relative_to(Path(root).resolve()):
        return None
    if owned.exists() and (recorded_sha is None or file_sha256(owned) != recorded_sha):
        return None
    return owned


def choose_path(folder: Path, title: str, owned: Path | None, base_name: str) -> Path:
    if owned is not None:
        return Path(owned)
    plain = folder / f"{base_name}.pdf"
    if not plain.exists():
        return plain
    clean_title = _clean(title)[:80] or "Role"
    named = folder / f"{base_name} ({clean_title}).pdf"
    n = 2
    while named.exists():
        named = folder / f"{base_name} ({clean_title}) {n}.pdf"
        n += 1
    return named


def write_atomic(render: Callable[[Path], None], dest: Path) -> None:
    """Render to a temp file beside `dest`, then replace it. A failure leaves `dest` untouched."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=dest.parent, suffix=".pdf")
    os.close(fd)
    tmp = Path(name)
    try:
        render(tmp)
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
