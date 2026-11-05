"""Minimal .env writer. Values are never logged or printed."""
import os
from pathlib import Path


def update_env(path: Path, changes: dict[str, str | None]) -> None:
    """Set (str) or remove (None) UPPERCASE keys, keeping comments, other lines and order."""
    path = Path(path)
    for key, value in changes.items():
        if "=" in key or "\n" in key or (value is not None and ("\n" in value or "\r" in value)):
            raise ValueError("invalid env key or value")
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
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(out) + "\n" if out else "")
    os.chmod(path, 0o600)


def mask(value: str | None) -> str:
    return "••••" + value[-4:] if value else ""
