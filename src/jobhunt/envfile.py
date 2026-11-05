"""Minimal .env writer. Values are never logged or printed."""
import os
import re
from pathlib import Path

_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]*")
_LINE_SEPARATORS = "\x7f\x85\u2028\u2029"


def _check(key: str, value: str | None) -> None:
    if not _KEY_RE.fullmatch(key):
        raise ValueError("invalid env key")
    if value is None:
        return
    if any(ord(c) < 32 or c in _LINE_SEPARATORS for c in value):
        raise ValueError("invalid env value: control or line-separator character")
    if value != value.strip() or "'" in value or '"' in value:
        raise ValueError("invalid env value: surrounding whitespace or quotes")


def update_env(path: Path, changes: dict[str, str | None]) -> None:
    """Set (str) or remove (None) UPPERCASE keys, keeping comments, other lines and order."""
    path = Path(path)
    for key, value in changes.items():
        _check(key, value)
    lines = path.read_text().splitlines() if path.is_file() else []
    pending = dict(changes)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        key = stripped.partition("=")[0].strip() if "=" in stripped and not stripped.startswith("#") else None
        if key is not None and key in pending:
            value = pending.pop(key)
            if value is not None:
                out.append(f"{key}={value}")
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in pending.items() if v is not None]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(out) + "\n" if out else "")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def mask(value: str | None) -> str:
    return "••••" + value[-4:] if value else ""
