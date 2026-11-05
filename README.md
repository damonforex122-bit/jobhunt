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
uv run jobhunt tailor <id>   # tailor the master resume PDF to a job (needs GEMINI_API_KEY)
uv run jobhunt people <job_id>   # find likely hiring managers for a job
uv run jobhunt status <id> applied --note "Referred by Sam"   # move a job to a pipeline stage
uv run jobhunt pipeline [--all]   # jobs by stage (--all adds accepted/rejected/withdrawn)
uv run jobhunt gmail-auth   # one-time: connect Gmail, read-only (see "Gmail setup")
uv run jobhunt gmail-sync [--days 30]   # suggest pipeline moves from recent recruiting emails
uv run jobhunt serve     # web UI at http://127.0.0.1:8000
```

### Daily morning run

`uv run jobhunt daily --install [--at HH:MM]` schedules the morning run with launchd. After installing, run it once with
`launchctl kickstart -k gui/$(id -u)/com.jobhunt.daily` and check `uv run jobhunt daily --status`. If the import step
says "permission denied", macOS is blocking background access to `~/Documents`: allow Full Disk Access for the
`jobhunt` entry point, or move `PROFICIENTLY_DIR` somewhere else.

### Application pipeline

Stages: `applied` → `screening` → `interviewing` → `offer`, closed as `accepted`, `rejected` or `withdrawn`.
Change a job's stage on its page (with an optional note) or with `jobhunt status`. Every change is
logged with its date; the job page shows the history and the **Pipeline** page shows a board.
Grading, import and scan never move a job that is already in the pipeline.

### Tailored resumes

`jobhunt tailor <id>` reads the job posting, matches it to your master resume
(`~/Documents/Resume/Georgia Liu - Master Resume.pdf`, override with `MASTER_RESUME_PATH`), and renders a
2-page PDF in the master's layout. Files go to `~/Documents/Resume/<Company>/` (override with
`RESUME_OUTPUT_DIR`) as `Georgia Liu - Resume.pdf`. If that name is taken by a file you made, it is never
overwritten: the new one is `Georgia Liu - Resume (<Job title>).pdf`. Re-running a job replaces only its own file.

**Providers.** Tailoring tries Gemini first, then Claude (`ANTHROPIC_API_KEY`, model `CLAUDE_MODEL`, default
`claude-opus-5-5`), then OpenRouter (`OPENROUTER_API_KEY`, model `OPENROUTER_MODEL`, default `openai/gpt-5-mini`).
Only providers with a key are used. A timeout (90s; 180s for Claude) or an overloaded/rate-limited reply is
retried once, then the next provider takes over and is kept for the rest of the run. The command prints each
step and any switch, e.g. "Gemini is unavailable — trying Claude…".

**No invention.** The model may only select and reword what is in your master. Every rewritten bullet is
checked for new numbers and new capitalised terms, then by a second model pass; anything that fails keeps
your original wording. The command lists the requirements your master doesn't support (**Gaps**) and the
bullets where it **kept your original wording**. If anything goes wrong, no file is written.
Tailoring never changes the job's status.

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
| `GEMINI_API_KEY` (`GEMINI_MODEL`, default `gemini-3.5-flash-lite`) | LLM fit score (1-10), match %, rewritten bullets. Without it you still get the keyword score. |
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

## Gmail setup (interview tracking)

`gmail-sync` reads recent recruiting emails (read-only) and suggests pipeline moves such as
*applied → screening* when a recruiter asks for a phone screen. Suggestions appear at the top of the
**Pipeline** page with Accept / Dismiss buttons. **Nothing changes until you accept.** An accepted move
is logged in the job's history with the email subject and date.

How it decides:
- **Stage:** fixed phrases. "phone screen" or "recruiter call" → screening; "onsite", "final round" or
  "technical interview" → interviewing; "extend an offer" → offer; "unfortunately… not moving forward"
  → rejected; "thank you for applying" → applied.
- **Job:** the company name must appear in the sender or the subject, or be the sender's domain.
  If the email names the job title, that job is used. Otherwise it picks the company's job that is furthest along.
- **Which moves:** only moves forward, or moves that close an open job. Each email is checked once.

Only the sender, subject, date and a 300-character snippet are stored, and only for emails that
produced a suggestion. Email bodies are never stored.

One-time setup (about 10 minutes):
1. In [Google Cloud Console](https://console.cloud.google.com/), create a project and enable the **Gmail API**.
2. Under **OAuth consent screen**, choose *External*. Add your Gmail address as a **test user**.
3. Under **Credentials** → *Create credentials* → *OAuth client ID* → **Desktop app**. Download the
   JSON and save it as `data/gmail_client_secret.json`. `data/` is git-ignored.
4. Run `uv run jobhunt gmail-auth` and approve read-only access in the browser. The token is saved to
   `data/gmail_token.json`, readable only by you.
5. Run `uv run jobhunt gmail-sync`, or click **Sync Gmail** on the Pipeline page.

While the Google app stays in *Testing* mode, Google expires the sign-in after 7 days. When sync
says the token expired, run `gmail-auth` again.
To use other file locations, set `GMAIL_CLIENT_SECRET` and `GMAIL_TOKEN`.

