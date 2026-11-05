"""Builds a synthetic `.proficiently` tree under a tmp dir. Synthetic content only."""
import json
from pathlib import Path


def _table(rows: list[dict], date: str, label: str) -> str:
    if not rows:
        return ""
    cols = list(rows[0].keys())
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    lines = [f"## {date} - Search: {label}", "", "| " + " | ".join(cols) + " |",
             "|" + "|".join("---" for _ in cols) + "|"]
    for r in rows:
        lines.append("| " + " | ".join(str(r.get(c, "")).replace("|", "\\|") for c in cols) + " |")
    return "\n".join(lines) + "\n\n"


def make_proficiently(
    root: Path,
    *,
    history_rows: list[dict],
    extra_history_rows: list[dict] = (),
    folders: dict[str, dict[str, str]] = {},
    careers: dict = {},
    digests: dict[str, str] = {},
) -> Path:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "job-history.md").write_text("# Job history\n\n" + _table(list(history_rows), "2026-10-30", "synthetic"))
    if extra_history_rows:
        (root / "_history-add.md").write_text(
            "# History add\n\n" + _table(list(extra_history_rows), "2026-11-01", "synthetic extra"))
    for slug, files in folders.items():
        d = root / "jobs" / slug
        d.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            (d / name).write_text(content)
    (root / "company-careers.json").write_text(json.dumps(careers))
    if digests:
        (root / "daily-digest").mkdir(exist_ok=True)
        for date, text in digests.items():
            (root / "daily-digest" / f"{date}.md").write_text(text)
    return root
