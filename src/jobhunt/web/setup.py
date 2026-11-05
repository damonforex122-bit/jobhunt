"""First-run setup wizard (/setup): AI key, resume, links, preferences, companies, Gmail, people."""
from collections.abc import Callable
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, urlsplit

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from .. import db, importer, profile_store, urls
from ..config import Settings, reload_settings, short_error
from ..envfile import mask, update_env
from ..gmail import connect
from ..models import Company, Preferences
from ..tailor import agents
from ..tailor.master import MasterError, load_master_with_sha
from ..tailor.providers import has_ai_key

STEPS = ("ai", "resume", "links", "prefs", "companies", "gmail", "people", "done")
LABELS = {"ai": "AI key", "resume": "Resume", "links": "Links", "prefs": "Preferences",
          "companies": "Companies", "gmail": "Gmail", "people": "People", "done": "Done"}
AI_KEYS = ("gemini_api_key", "anthropic_api_key", "openrouter_api_key")
AI_LABELS = {"gemini_api_key": "Gemini", "anthropic_api_key": "Claude (Anthropic)",
             "openrouter_api_key": "OpenRouter"}
AI_LINKS = {"gemini_api_key": "https://aistudio.google.com/apikey",
            "anthropic_api_key": "https://console.anthropic.com/settings/keys",
            "openrouter_api_key": "https://openrouter.ai/keys"}
PEOPLE_KEYS = ("apollo_api_key", "exa_api_key")
PEOPLE_LABELS = {"apollo_api_key": "Apollo", "exa_api_key": "Exa"}
NEEDS_AI = "Add at least one AI key to continue, or choose Skip."


class Ping(BaseModel):
    ok: bool


def get_setup_client(request: Request):
    """The provider chain used by the Test button; None when no key is set. Tests override this."""
    return agents.make_client(request.app.state.settings)


def get_gmail_tester() -> Callable:
    return connect.test_connection


def _csv(value: str) -> list[str]:
    return [p.strip() for p in value.split(",") if p.strip()]


def _slug_name(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").title()


def _answered_by(chain) -> str:
    try:
        return chain.providers[chain._preferred].name
    except (AttributeError, IndexError):
        return "your AI provider"


def register(app: FastAPI, settings: Settings, Conn, render) -> None:
    from .app import get_importer  # imported late: app.py imports this module

    app.state.settings = settings

    def current_step(conn) -> str:
        step = profile_store.get(conn, "setup_step")
        return step if step in STEPS else "ai"

    def go(step: str, msg: str = "") -> RedirectResponse:
        url = f"/setup?step={step}"
        if msg:
            url += f"&msg={quote(msg)}"
        return RedirectResponse(url, status_code=303)

    def advance(conn, step: str) -> RedirectResponse:
        nxt = STEPS[STEPS.index(step) + 1]
        profile_store.put(conn, "setup_step", nxt)
        if nxt == "done":
            profile_store.put(conn, "setup_done", True)
        return go(nxt)

    def master_links(conn) -> profile_store.Links:
        """Header links of the confirmed master, sorted by domain; empty if unreadable."""
        found = profile_store.Links()
        path = profile_store.effective_master_path(conn, settings)
        if path is None:
            return found
        try:
            resume, _ = load_master_with_sha(conn, path)  # cached after the upload; no AI call
        except (MasterError, OSError):
            return found
        for link in resume.header.links:
            host = (urlsplit(link.url).hostname or "").lower()
            if host.endswith("linkedin.com"):
                found.linkedin = found.linkedin or link.url
            elif host.endswith("github.com"):
                found.github = found.github or link.url
            else:
                found.website = found.website or link.url
        return found

    def page(request: Request, conn, step: str, **extra) -> HTMLResponse:
        keys = {f: mask(getattr(settings, f)) for f in AI_KEYS + PEOPLE_KEYS}
        ctx = dict(step=step, steps=STEPS, labels=LABELS, index=STEPS.index(step), keys=keys,
                   ai_keys=AI_KEYS, ai_labels=AI_LABELS, ai_links=AI_LINKS,
                   people_keys=PEOPLE_KEYS, people_labels=PEOPLE_LABELS,
                   error=None, report=None)
        if step == "resume":
            ctx.update(master=profile_store.get_master(conn),
                       master_path=profile_store.effective_master_path(conn, settings),
                       pending=profile_store.get_pending_master(conn),
                       confirm_next="/setup?step=links", cancel_next="/setup?step=resume")
        elif step == "links":
            saved, found = profile_store.get_links(conn), master_links(conn)
            ctx.update(links=profile_store.Links(website=saved.website or found.website,
                                                 github=saved.github or found.github,
                                                 linkedin=saved.linkedin or found.linkedin))
        elif step == "prefs":
            ctx.update(prefs=profile_store.effective_preferences(conn, settings),
                       notes=profile_store.get_ai_notes(conn),
                       output_dir=profile_store.effective_output_dir(conn, settings))
        elif step == "companies":
            pdir = settings.proficiently_dir
            ctx.update(companies=db.list_companies(conn),
                       can_import=pdir is not None and Path(pdir).is_dir())
        elif step == "gmail":
            ctx.update(gmail_address=settings.gmail_address or "",
                       gmail_masked=mask(settings.gmail_app_password))
        ctx.update(extra)
        return render(request, "setup.html", **ctx)

    def save_env(changes: dict[str, str | None]) -> str | None:
        """Write keys to the env file; returns an error message, or None."""
        if not changes:
            return None
        try:
            update_env(settings.env_path, changes)
        except ValueError:
            return "That value contains characters that can't be saved."
        reload_settings(settings)
        return None

    def form_keys(form, fields) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        for f in fields:
            v = form.get(f)
            if isinstance(v, str) and v.strip():
                out[f.upper()] = v.strip()
        return out

    @app.get("/setup", response_class=HTMLResponse)
    def get_setup(request: Request, conn: Conn, step: str = ""):
        return page(request, conn, step if step in STEPS else current_step(conn))

    @app.post("/setup/skip-all")
    def post_skip_all(conn: Conn):
        profile_store.put(conn, "setup_done", True)
        return RedirectResponse("/", status_code=303)

    @app.post("/setup/test-ai", response_class=HTMLResponse)
    def post_test_ai(request: Request, client: Annotated[object, Depends(get_setup_client)]):
        if client is None:
            ok, text = False, "Add an AI key first, then test it."
        else:
            try:
                client.generate('Reply with {"ok": true}.', Ping, "testing the AI key")
                ok, text = True, f"Working — answered by {_answered_by(client)}"
            except Exception as e:  # provider errors are already masked
                ok, text = False, short_error(e)
        return HTMLResponse(f'<div role="alert" class="alert {"alert-success" if ok else "alert-error"}">'
                            f'{_esc(text)}</div>')

    @app.post("/setup/test-gmail", response_class=HTMLResponse)
    def post_test_gmail(tester: Annotated[Callable, Depends(get_gmail_tester)]):
        try:
            ok, text = True, tester(settings)
        except Exception as e:
            text = short_error(e)
            if settings.gmail_app_password:
                text = text.replace(settings.gmail_app_password, "***")
            ok = False
        return HTMLResponse(f'<div role="alert" class="alert {"alert-success" if ok else "alert-error"}">'
                            f'{_esc(text)}</div>')

    @app.post("/setup/import")
    def post_setup_import(conn: Conn, importer_fn: Annotated[Callable, Depends(get_importer)]):
        pdir = settings.proficiently_dir
        if pdir is None or not Path(pdir).is_dir():
            return go("companies", "No Proficiently folder found.")
        try:
            r = importer_fn(conn, pdir)
        except (ValueError, OSError) as e:
            return go("companies", "Import failed: " + short_error(e))
        return go("companies", "Import done: " + ", ".join(f"{k} {v}" for k, v in r.items()))

    @app.post("/setup/{step}/skip")
    def post_skip(step: str, conn: Conn):
        if step not in STEPS[:-1]:
            return go(current_step(conn))
        return advance(conn, step)

    @app.post("/setup/{step}")
    async def post_step(step: str, request: Request, conn: Conn):
        if step not in STEPS[:-1]:
            return go(current_step(conn))
        form = await request.form()

        def get_str(name: str) -> str:
            v = form.get(name)
            return v.strip() if isinstance(v, str) else ""

        if step == "ai":
            err = save_env(form_keys(form, AI_KEYS))
            if err is None and not has_ai_key(settings):
                err = NEEDS_AI
            if err:
                return page(request, conn, "ai", error=err)
        elif step == "links":
            profile_store.save_links(conn, profile_store.Links(
                website=get_str("website"), github=get_str("github"), linkedin=get_str("linkedin")))
        elif step == "prefs":
            roles, locs = _csv(get_str("target_roles")), _csv(get_str("locations"))
            floor = get_str("salary_floor").replace(",", "") or "0"
            folder = get_str("output_dir")
            problem = None
            if not roles:
                problem = "Add at least one target role."
            elif not locs:
                problem = "Add at least one location."
            elif not floor.isdigit():
                problem = "Salary floor must be a whole number."
            elif folder and not Path(folder).expanduser().is_absolute():
                problem = "Give the full path of the folder, for example ~/Documents/jobhunt/Resumes."
            if problem:
                prefs = Preferences(target_roles=roles, locations=locs, salary_floor=0,
                                    search_terms=_csv(get_str("search_terms")),
                                    excluded_terms=_csv(get_str("excluded_terms")))
                return page(request, conn, "prefs", error=problem, prefs=prefs,
                            notes=get_str("notes"), output_dir=folder)
            profile_store.save_preferences(conn, Preferences(
                target_roles=roles, locations=locs, salary_floor=int(floor),
                search_terms=_csv(get_str("search_terms")),
                excluded_terms=_csv(get_str("excluded_terms"))))
            profile_store.save_ai_notes(conn, get_str("notes")[:5000])
            if folder:
                profile_store.put(conn, "resume_output_dir", str(Path(folder).expanduser()))
        elif step == "companies":
            added, unknown, seen = 0, [], set()
            for line in get_str("urls").splitlines():
                url = line.strip()
                if not url or url in seen:
                    continue
                seen.add(url)
                hit = urls.ats_from_url(url)
                if hit is None:
                    unknown.append(url)
                    continue
                ats, slug = hit
                db.upsert_company(conn, Company(name=_slug_name(slug), ats=ats, slug=slug))
                added += 1
            if unknown:
                return page(request, conn, "companies",
                            report=dict(added=added, unknown=unknown))
        elif step == "gmail":
            changes: dict[str, str | None] = {}
            address = get_str("gmail_address")
            if address and address != (settings.gmail_address or ""):
                changes["GMAIL_ADDRESS"] = address
            password = "".join(get_str("gmail_app_password").split())
            if password:
                changes["GMAIL_APP_PASSWORD"] = password
            err = save_env(changes)
            if err:
                return page(request, conn, "gmail", error=err)
        elif step == "people":
            err = save_env(form_keys(form, PEOPLE_KEYS))
            if err:
                return page(request, conn, "people", error=err)
        return advance(conn, step)


def _esc(text: str) -> str:
    from html import escape
    return escape(text)
