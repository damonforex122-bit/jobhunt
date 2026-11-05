"""Verify a tailoring draft against the master resume and assemble the final resume.

Nothing the writer says survives unless it is traceable to the master: a bullet that
fails any check is replaced by the master wording it was derived from.
"""

import re
from collections.abc import Callable

from jobhunt.fit.gemini import _numbers
from jobhunt.tailor.agents import Draft, DraftEntry, Verdicts
from jobhunt.tailor.model import Bullet, Entry, Resume, SkillCategory

_CAP_TERM = re.compile(r"[A-Z][\w.+#-]*")
# Sentence start: ". " but not after "e.g." / "i.e.".
_SENTENCE_SPLIT = re.compile(r"(?<!e\.g)(?<!i\.e)\.\s+")


def _new_terms(text: str, master_text: str) -> list[str]:
    """Capitalized terms (not sentence-initial) that the master never uses."""
    master_terms = {t.rstrip(".-") for t in _CAP_TERM.findall(master_text)}
    missing: list[str] = []
    for sentence in _SENTENCE_SPLIT.split(text):
        words = sentence.split()
        for term in _CAP_TERM.findall(" ".join(words[1:])):
            term = term.rstrip(".-")
            if term and term not in master_terms and term not in missing:
                missing.append(term)
    return missing


def _claims_problem(text: str, allowed_numbers: set[str], master_text: str) -> str | None:
    new_numbers = sorted(_numbers(text) - allowed_numbers)
    if new_numbers:
        return f"new number(s) {', '.join(new_numbers)}"
    new_terms = _new_terms(text, master_text)
    if new_terms:
        return f"new term(s) {', '.join(new_terms)}"
    return None


def _draft_entries(draft: Draft) -> dict[str, DraftEntry]:
    """First draft entry per id."""
    out: dict[str, DraftEntry] = {}
    for de in draft.entries:
        out.setdefault(de.entry_id, de)
    return out


def _flat_bullets(draft: Draft):
    """(flat index, is first occurrence of its entry, entry id, DraftBullet)."""
    seen: set[str] = set()
    i = 0
    for de in draft.entries:
        first = de.entry_id not in seen
        seen.add(de.entry_id)
        for db in de.bullets:
            yield i, first, de.entry_id, db
            i += 1


def deterministic_problems(draft: Draft, master: Resume) -> dict[str, str]:
    master_text = master.full_text()
    master_numbers = _numbers(master_text)
    entries = {e.id: e for e in master.experience}
    problems: dict[str, str] = {}

    reason = _claims_problem(draft.summary, master_numbers, master_text)
    if reason:
        problems["summary"] = reason

    for i, _first, entry_id, db in _flat_bullets(draft):
        entry = entries.get(entry_id)
        if entry is None:
            problems[f"b{i}"] = f"unknown entry {entry_id}"
            continue
        if not db.text.strip():
            problems[f"b{i}"] = "empty bullet"
            continue
        if not db.source_ids:
            problems[f"b{i}"] = "no source bullets"
            continue
        own = {b.id: b for b in entry.bullets}
        bad = [s for s in db.source_ids if s not in own]
        if bad:
            problems[f"b{i}"] = f"source(s) {', '.join(bad)} not in this entry"
            continue
        allowed: set[str] = set()
        for s in db.source_ids:
            allowed |= _numbers(own[s].text)
        reason = _claims_problem(db.text, allowed, master_text)
        if reason:
            problems[f"b{i}"] = reason

    categories = {s.id: s for s in master.skills}
    for ds in draft.skills:
        cat = categories.get(ds.category_id)
        if cat is None:
            problems[f"s{ds.category_id}"] = f"unknown category {ds.category_id}"
            continue
        allowed_items = {x.casefold() for x in cat.items}
        extra = [x for x in ds.items if x.casefold() not in allowed_items]
        if extra:
            problems[f"s{ds.category_id}"] = f"item(s) not in master: {', '.join(extra)}"
        elif not ds.items:
            problems[f"s{ds.category_id}"] = "no items"
    return problems


def _note(text: str, reason: str) -> str:
    return f'Kept your original wording for "{text[:60]}" ({reason})'


def _role_lines(master: Resume) -> list[str]:
    """Each role's own facts (title, org, dates, blurb), so the summary may name them."""
    out = []
    for e in master.experience:
        line = f"{e.title}, {e.org}, {e.location}, {e.dates}"
        out.append(f"{line}. {e.blurb}" if e.blurb else line)
    out += [f"{ed.school}, {ed.detail}, {ed.dates}" for ed in master.education]
    return out


def assemble(
    draft: Draft,
    master: Resume,
    judge_fn: Callable[[list[tuple[str, list[str]]]], Verdicts],
) -> tuple[Resume, list[str], list[str]]:
    problems = deterministic_problems(draft, master)
    fallbacks: list[str] = []
    master_bullets = master.all_bullets()

    # One judge call over the bullets that passed the deterministic checks.
    survivors = [
        (i, db) for i, first, _eid, db in _flat_bullets(draft) if first and f"b{i}" not in problems
    ]
    judge_reasons: dict[int, str] = {}
    pairs = [(db.text, [master_bullets[s].text for s in db.source_ids]) for _, db in survivors]
    summary_pos = None
    if "summary" not in problems:
        summary_pos = len(pairs)
        pairs.append((draft.summary, [master.summary, *_role_lines(master),
                                      *(b.text for b in master_bullets.values())]))
    summary_judge_reason: str | None = None
    if pairs:
        verdicts = judge_fn(pairs)
        by_index: dict[int, list] = {}
        for v in verdicts.verdicts:
            by_index.setdefault(v.index, []).append(v)

        def judged(pos: int) -> str | None:
            vs = by_index.get(pos)
            if not vs:
                return "no verdict from the checker"
            bad = next((v for v in vs if not v.supported), None)
            return (bad.reason or "not supported by the source") if bad else None

        for pos, (i, _db) in enumerate(survivors):
            reason = judged(pos)
            if reason:
                judge_reasons[i] = reason
        if summary_pos is not None:
            summary_judge_reason = judged(summary_pos)

    # Flat index of each first-occurrence draft entry's bullets.
    flat_by_entry: dict[str, list[tuple[int, object]]] = {}
    for i, first, entry_id, db in _flat_bullets(draft):
        if first:
            flat_by_entry.setdefault(entry_id, []).append((i, db))

    final_ids_for_flat: dict[int, list[str]] = {}
    experience: list[Entry] = []
    for me in master.experience:
        out: list[Bullet] = []
        k = 0

        def add(text: str) -> str:
            nonlocal k
            b = Bullet(id=f"{me.id}t{k}", text=text)
            k += 1
            out.append(b)
            return b.id

        own = {b.id: b for b in me.bullets}
        for i, db in flat_by_entry.get(me.id, []):
            reason = problems.get(f"b{i}") or judge_reasons.get(i)
            if reason is None:
                final_ids_for_flat[i] = [add(db.text)]
                continue
            ids: list[str] = []
            for s in db.source_ids:
                src = own.get(s)
                if src is not None and all(b.text != src.text for b in out):
                    ids.append(add(src.text))
            final_ids_for_flat[i] = ids
            fallbacks.append(
                _note(db.text, reason) if ids else f'Dropped "{db.text[:60]}" ({reason})'
            )
        if not out and me.bullets:
            add(me.bullets[0].text)
            fallbacks.append(_note(me.bullets[0].text, "no usable tailored bullets for this role"))
        experience.append(me.model_copy(update={"bullets": out}))

    # Ranking: draft order, then anything unmentioned in document order.
    ranking: list[str] = []
    for idx in draft.ranking:
        for bid in final_ids_for_flat.get(idx, []):
            if bid not in ranking:
                ranking.append(bid)
    for e in experience:
        for b in e.bullets:
            if b.id not in ranking:
                ranking.append(b.id)

    summary = draft.summary
    summary_reason = problems.get("summary") or summary_judge_reason
    if summary_reason:
        fallbacks.append(_note(draft.summary, summary_reason))
        summary = master.summary

    draft_skills: dict[str, list[str]] = {}
    for ds in draft.skills:
        draft_skills.setdefault(ds.category_id, ds.items)
    skills: list[SkillCategory] = []
    for cat in master.skills:
        items = draft_skills.get(cat.id)
        if items is None:
            skills.append(cat)
        elif f"s{cat.id}" in problems:
            fallbacks.append(_note(", ".join(items), problems[f"s{cat.id}"]))
            skills.append(cat)
        else:
            spelling = {x.casefold(): x for x in cat.items}
            kept = list(dict.fromkeys(spelling[x.casefold()] for x in items))
            skills.append(cat.model_copy(update={"items": kept}))

    tailored = master.model_copy(update={"summary": summary, "experience": experience, "skills": skills})
    return tailored, ranking, fallbacks
