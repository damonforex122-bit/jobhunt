# jobhunt

jobhunt is a job-search helper that runs on your own Mac. Nothing is hosted anywhere.

- It watches the careers pages of companies you pick and collects new openings.
- It reads your resume and grades how well each job fits.
- It rewrites your resume for one job at a time, using only what is already in your resume.
- It tracks every application, from applied to offer.
- If you connect Gmail, it suggests status changes from recruiter emails.

## Install on a Mac

You do this once. It takes about 10 minutes.

A **terminal** is a window where you type commands. Open it with Finder → Applications → Utilities → Terminal.
Paste each command below into it and press Return.

1. Install git, the tool that downloads the app. A window will pop up. Click Install and wait until it finishes.

   ```
   xcode-select --install
   ```

   If it says the tools are already installed, that is fine. Go on.

2. Install uv. uv installs the right Python for you, so you do not need to install Python yourself.

   ```
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

3. Close the Terminal window and open a new one. This lets it find uv.

4. Download the app. The repository is private, so GitHub has to know who you are.
   You need to accept Georgia's invite to the repo first (check your email from GitHub).
   - Install GitHub's command-line tool: go to https://cli.github.com, download the macOS installer and open it.
   - Sign in. Choose GitHub.com, HTTPS, and "Login with a web browser", then follow the code shown:

     ```
     gh auth login
     ```

   - Download the app and go into its folder:

     ```
     gh repo clone georgia-max/jobhunt
     cd jobhunt
     ```

5. Install what the app needs.

   ```
   uv sync
   ```

6. Start the app. It opens in your browser.

   ```
   uv run jobhunt serve --open
   ```

The app only runs while that Terminal window is open. To stop it, press Control-C in the window.
To start it again later, open a new Terminal window and run:

```
cd jobhunt && uv run jobhunt serve --open
```

If you cloned somewhere other than your home folder, `cd` to that folder instead.

### What it costs

The app is free. The AI companies it talks to charge by use, and Gemini has a free tier. You pay each
provider directly, and only for the keys you add. The app calls them only when you grade a job (Gemini only),
tailor a resume, have your resume read, chat in Review, or run the morning step (see Advanced).
It makes no AI calls in the background otherwise.

## The setup wizard

The first time you open the app, it shows a setup wizard. Every step has a Skip button, and **Skip setup**
at the top skips them all. You can change anything later on the **Profile** page. You can also reopen
the wizard at http://127.0.0.1:8000/setup (or your port), or Profile → Run setup again.

1. **AI key.** The app uses an AI model to read your resume, grade jobs and tailor resumes.
   You need at least one key. Paste it, then press **Test** to check that it works.
   A free Gemini key is recommended, because it is the only one that turns on **Grade**.
   With only a Claude or OpenRouter key you get keyword scores instead of AI grades.
   - Gemini has a free tier. Get a key at https://aistudio.google.com/apikey.
   - Claude (Anthropic): https://console.anthropic.com/settings/keys
   - OpenRouter: https://openrouter.ai/keys

   Claude and OpenRouter work as backups for reading your resume, tailoring and the Review chat.
   Gemini is tried first. If it is busy, the app moves to Claude, then OpenRouter. Only providers with a key are used.

2. **Resume.** Upload your resume as a PDF. The app reads it and shows you what it found, with each job and
   its bullets. Check that every bullet sits under the right job. If it looks right, press **Use this resume**.
   If not, press **Try another file**.
   - No PDF handy? Open your LinkedIn profile, then choose More → Save to PDF.
   - The app never invents anything. It only keeps text that is really in your PDF.

3. **Links.** Your LinkedIn, GitHub and website. They go in the header of your tailored resumes.
   They are filled in from your resume when it had them.

4. **Preferences.** Target roles and locations (at least one of each), a salary floor, words to look for in
   job titles, words to avoid, and notes for the AI (one per line, such as "never say I managed a team").
   You can also choose the folder where tailored resumes are saved. The default is `~/Documents/jobhunt/Resumes`.

5. **Companies.** Paste careers-page links, one per line. Greenhouse, Ashby and Lever pages work,
   for example `https://jobs.lever.co/acme`. Links the app does not recognize are listed so you can fix them.

6. **Gmail (optional).** The app reads your recruiting email, read-only, and suggests status changes.
   - Turn on 2-Step Verification for your Google account first.
   - Open https://myaccount.google.com/apppasswords.
   - Name it "jobhunt" and create it. Google shows 16 characters.
   - Paste your Gmail address and those 16 characters, then press **Test connection**.

   The app password lets the app read your mail. You can delete it from the same Google page at any time.

7. **People (optional).** Apollo and Exa keys let the app suggest people to contact at a company.
   Skip this unless you already have those accounts.

When it finishes, press **Scan my companies** to pull in the first jobs.

## Your data

Everything stays on your Mac.

- **`data/jobhunt.db`** is your database (SQLite). The app creates it the first time you run it.
  There is nothing to install.
  It holds your jobs, grades, pipeline history, preferences, and Gmail suggestions.
- **`data/`** also holds the copy of your uploaded resume (`data/master/`), and the morning-run log if you use it.
- **`.env`** holds your keys and passwords. It is readable only by you.
  Never share it or paste it into a message.
- Tailored resumes go to the folder you chose (default `~/Documents/jobhunt/Resumes`), in a folder per company.

**Back up** by copying the `data/` folder and the `.env` file somewhere safe.
**Restore** by copying your backed-up `data/` folder and `.env` file back into the jobhunt folder.
**Start over** by quitting the app and deleting `data/jobhunt.db`. Your keys in `.env` and your uploaded resume
in `data/master/` stay. To wipe everything, delete the whole `data/` folder and `.env`.

**Privacy.** Your resume text and the job posting are sent to the AI provider whose key you added, when you
grade, tailor, or have your resume read. If you connect Gmail, the app stores only the sender, subject,
date and a short snippet, and only for emails that produced a suggestion. It never stores email bodies, and
it cannot send, delete or change email. Nothing else leaves your Mac. The app only listens on your own computer
(127.0.0.1), so nobody else on your network can open it.

## Daily use

Use the menu at the top of the page.

- **Jobs.** Your list of openings. To add one, paste a link in **Add a job by link** and press
  **Add and tailor**. Open a job to press **Grade** (a fit score from 1 to 10, with reasons) or
  **Tailor resume**. Use the status dropdown to move it along, with an optional note.
  A job's page keeps the history of its status changes.
- **Review.** On a job with a tailored resume, press **Review & refine**. Chat to ask for changes
  ("make the first bullet shorter") or press **Tailor now**. Every change is checked against your original
  resume so nothing is invented.
- **Pipeline.** A board of your applications. Drag a card to change its stage. The stages are
  applied, screening, interviewing and offer, then accepted, rejected or withdrawn at the end.
  With Gmail connected, press **Sync Gmail**. Suggested moves appear at the top with **Accept** and **Dismiss**.
  Nothing changes until you accept.
- **Interviews.** Interviews found in your email, in one place.
- **Companies.** The careers pages you watch. Press **Scan now** to look for new jobs.
- **Profile.** Change your resume, links, preferences, notes, output folder and keys at any time.

### Commands

You can do most things from the terminal too. Run these inside the `jobhunt` folder.

```
uv run jobhunt serve --open            # start the app (add --port 8002 to use another port)
uv run jobhunt add <link>              # add a job from a link and tailor your resume to it
uv run jobhunt scan                    # look for new jobs at your companies
uv run jobhunt fit <id>                # grade one job (or: fit --all-new)
uv run jobhunt tailor <id>             # tailor your resume to a job
uv run jobhunt people <id>             # find likely hiring managers for a job
uv run jobhunt status <id> applied     # move a job to a stage (add --note "text")
uv run jobhunt pipeline                # jobs by stage (add --all to include closed ones)
uv run jobhunt gmail-sync              # suggest status changes from recent emails
```

If a posting can't be read from its link, `add` tells you to save the text to a file and rerun it with
`--text-file FILE`. Run `uv run jobhunt <command> -h` to see every option.

## Updating

```
cd jobhunt && git pull && uv sync
```

Then start the app again. Your data stays as it is.

## Troubleshooting

**"Port 8000 is busy."** Another copy of the app, or another program, is using it. Close the other Terminal
window, or use a different port:

```
uv run jobhunt serve --port 8002 --open
```

**Gemini is busy or slow.** The app tries once more, then moves to your next provider (Claude, then
OpenRouter) and uses it for the rest of that task. It tells you when it switches. To have a backup,
add a second key on the Profile page. Without a backup, wait a minute and try again.

**Gmail says the app password was rejected.** Check that 2-Step Verification is on. Make a new app password
at https://myaccount.google.com/apppasswords and paste it again. Spaces are fine. Use the same Gmail address
you made it for. Some work or school Google accounts do not allow app passwords.

**"Couldn't read your resume reliably."** The app checks everything the AI read against the words in your PDF.
If it can't prove the read is faithful, it refuses to use it, so it never works from guesses.
Export your resume as a PDF again from Google Docs or Word, or use LinkedIn → More → Save to PDF. Then upload that file.
Scanned or image-only PDFs have no text to read. Export a text PDF instead.
If the message says to add an AI key, add one on the Setup or Profile page first.

**Gmail suggestions are empty.** Suggestions only come from recruiting emails in the last 30 days that
name a company you track, and only moves a job forward, or closes it (for example a rejection). Check that Gmail is connected (Setup → Gmail, press **Test
connection**), then press **Sync Gmail** on the Pipeline page. Newsletters and your own replies are skipped.

**The page says "Gemini grading off".** Add a Gemini key on the Profile page to turn on grading.

## Advanced

Nothing here is needed to use the app.

### The morning run

One command imports, scans your companies, syncs Gmail and interviews, and grades new jobs (grading needs a Gemini key):

```
uv run jobhunt daily
```

To run it every morning at 08:00 on your Mac (pick another time with `--at HH:MM`, 24-hour):

```
uv run jobhunt daily --install
```

Run it once now, then check it:

```
launchctl kickstart -k gui/$(id -u)/com.jobhunt.daily
uv run jobhunt daily --status
```

`uv run jobhunt daily --uninstall` removes the schedule. The log is `data/daily.log`.
This is the one place the app calls the AI without you pressing a button, so it uses your key every morning.
If a step says "permission denied", macOS is blocking background access to `~/Documents`. Allow Full Disk Access for
the `jobhunt` command, or move the folder it reads to somewhere else.

### Google OAuth for Gmail

The app password in the wizard is the easy way. If you already use a Google Cloud project, you can use
read-only Google sign-in instead. The app uses the app password if both are set up.

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project and enable the **Gmail API**.
2. Under **OAuth consent screen**, choose *External* and add your Gmail address as a **test user**.
3. Under **Credentials**, choose *Create credentials* → *OAuth client ID* → **Desktop app**.
   Download the JSON and save it as `data/gmail_client_secret.json`.
4. Run `uv run jobhunt gmail-auth` and approve read-only access in the browser.
   The token is saved to `data/gmail_token.json`.

While the Google app is in *Testing* mode, Google expires the sign-in after 7 days. When sync says the token expired,
run `gmail-auth` again. Use `GMAIL_CLIENT_SECRET` and `GMAIL_TOKEN` to keep the files elsewhere.

### Proficiently

If you use Proficiently, jobhunt can read its export folder. It never writes to, moves or deletes anything there.

Set `PROFICIENTLY_DIR` in `.env` to the export folder (usually `~/Documents/Resume/.proficiently`). Then either press
**Import from Proficiently** in the wizard's Companies step, or run:

```
uv run jobhunt import
```

This loads your earlier jobs (with their status and score), their companies and the people Proficiently found.
Postings and files Proficiently prepared show on each job's page. Until you save preferences on the Profile page,
the app reads them from this folder.

### Moving over from the original install

`uv run jobhunt migrate-profile` carries an older single-user install's resume path, output folder, Proficiently
folder and notes into the new settings. It is safe to run again, and it never writes inside the old folders.
New users do not need it.

### Settings

Everything can be set in `.env`. The wizard and Profile page write the keys for you. Copy `.env.example`
to `.env` to edit them by hand. Empty values count as unset.

| Variable | What it does | Default |
| --- | --- | --- |
| `GEMINI_API_KEY` | Gemini key. Used first. | none |
| `GEMINI_MODEL` | Gemini model name. | `gemini-3.5-flash-lite` |
| `ANTHROPIC_API_KEY` | Claude key. Backup provider. | none |
| `CLAUDE_MODEL` | Claude model name. | `claude-opus-5-5` |
| `OPENROUTER_API_KEY` | OpenRouter key. Second backup. | none |
| `OPENROUTER_MODEL` | OpenRouter model name. | `openai/gpt-5-mini` |
| `APOLLO_API_KEY` | Apollo people search. It needs a `domain` for the company in `config/watchlist.yaml`. | none |
| `EXA_API_KEY` | Exa web search for hiring-manager candidates. | none |
| `GMAIL_ADDRESS` | Gmail address for the app password. | none |
| `GMAIL_APP_PASSWORD` | Gmail app password (read-only access). | none |
| `GMAIL_CLIENT_SECRET` | Google OAuth client file (Advanced). | `data/gmail_client_secret.json` |
| `GMAIL_TOKEN` | Google OAuth token file (Advanced). | `data/gmail_token.json` |
| `MASTER_RESUME_PATH` | A resume PDF to use, only when no resume has been uploaded. | none |
| `RESUME_OUTPUT_DIR` | Default folder for tailored resumes. A folder saved in the wizard or on the Profile page wins. | `~/Documents/jobhunt/Resumes` |
| `DB_PATH` | The database file. | `data/jobhunt.db` |
| `WATCHLIST_PATH` | Extra companies to scan, as YAML. | `config/watchlist.yaml` |
| `PROFICIENTLY_DIR` | Proficiently export folder (read-only). | none |

Anything you set in the terminal's environment overrides `.env`.

To check the code, run `uv run pytest -q`. The tests use no network.
