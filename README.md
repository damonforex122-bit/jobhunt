# jobhunt

A local job-search tool: imports your Proficiently data, scans company ATS boards,
grades fit against your resume, and finds likely hiring managers. FastAPI + SQLite,
served only on `127.0.0.1:8000`.

## Setup

```
uv sync
cp .env.example .env     # then fill in the keys you have
```

## Commands

Run all commands from the repo root (`data/` and `config/` paths are relative to it).

```
uv run jobhunt import    # load jobs/companies/preferences from the Proficiently folder
uv run jobhunt scan      # scan watchlist + known ATS boards (Greenhouse, Ashby, Lever)
uv run jobhunt fit <id> | --all-new   # grade one job, or every 'new' job (keyword score always; Gemini score with a key)
uv run jobhunt people <job_id>   # find likely hiring managers for a job
uv run jobhunt serve     # web UI at http://127.0.0.1:8000
```

To start clean, delete `data/jobhunt.db`, then run `uv run jobhunt import && uv run jobhunt scan`.

Run `uv run jobhunt <command> -h` for options. Tests: `uv run pytest -q` (no network).

## Where data comes from

- **Proficiently export** (`PROFICIENTLY_DIR`, default `~/Documents/Resume/.proficiently`):
  resume, preferences and previously found jobs.
- **ATS boards**: companies in `config/watchlist.yaml` plus those learned from the import.
- **Your database**: `data/jobhunt.db` (git-ignored).

Locations (San Francisco, New York), the $180k base floor and the search terms come from
your preferences. Jobs below the floor are dropped; unclear location/salary is kept and
flagged.

## Read-only guarantee

Nothing in this app writes to, moves or deletes anything under `PROFICIENTLY_DIR`. It is
only read. All state lives in `data/jobhunt.db`.

## API keys (all optional)

A missing key turns that feature off with a banner in the UI; nothing crashes.

| Variable | Turns on |
| --- | --- |
| `GEMINI_API_KEY` (`GEMINI_MODEL`, default `gemini-2.5-flash`) | LLM fit score (1-10), match %, rewritten bullets. Without it you still get the keyword score. |
| `APOLLO_API_KEY` | Apollo people search for hiring-manager candidates (names may be partly obfuscated, shown as-is). |
| `EXA_API_KEY` | Exa web search for hiring-manager candidates. |

People are only stored with a source and source URL. Hiring-manager names found in the
posting itself need no key.

### Apollo needs a company domain

Apollo searches by company domain, so set `domain` on the company in
`config/watchlist.yaml` (same company `name` as in your jobs), e.g.:

```yaml
- {name: Harvey, ats: ashby, slug: harvey, domain: harvey.ai}
```

Without a domain the people panel shows "Skipped apollo: <reason>".
