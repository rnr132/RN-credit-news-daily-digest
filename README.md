# The Watchlist — Daily Credit News Digest

An automated daily digest of industry and company news for a defined credit coverage
universe, published as a static, self-updating web page. No server, no ongoing cost
beyond API usage.

Every day, a GitHub Actions workflow runs a script that:

1. Optionally reads a dedicated inbox (newsletters/alerts) over IMAP.
2. Calls Claude (via the Anthropic API, using the `web_search` tool) to find and
   categorise credit-relevant news across your configured sectors and companies.
3. Renders **`index.html`** — today's edition only, in two columns (Industry News /
   Company News), with a small colour-coded flag per item (Positive / Negative /
   Watch / Neutral) showing the likely credit direction.
4. Renders **`archive.html`** — every past edition, as a flat, searchable list with
   a text search box and clickable tag chips (one per sector/company, plus one per
   credit-flag) so you can filter down to, say, everything tagged "Vodafone" or
   everything flagged "Negative", entirely client-side.
5. Commits both pages, which GitHub Pages then serves automatically.

Rendered examples with sample headlines are in `preview.html` (today's-edition view)
and `preview-archive.html` (the searchable archive) — open either in a browser to see
the design before doing any setup.

---

## 1. What's in this project

```
config.json                      Sectors, companies, sources, email settings (no secrets)
digest_history.json              Full history of past editions (auto-updated, feeds archive.html)
requirements.txt                 Python dependencies
scripts/generate_digest.py       The core script
scripts/template.html            Today's-edition page design (parchment/broadsheet style)
scripts/archive_template.html    Searchable archive page design
.github/workflows/daily-digest.yml   Runs the script daily and commits the result
preview.html                     Sample rendered "today" page, for a look before setup
preview-archive.html             Sample rendered archive page, for a look before setup
index.html                       The live "today" page (created after your first run)
archive.html                     The live searchable archive (created after your first run)
```

---

## 2. One-time setup

### Step 1 — Create a GitHub account and repository

1. If you don't already have one, sign up at [github.com](https://github.com).
2. Click the **+** icon (top right) → **New repository**.
3. Name it something like `credit-news-digest`. Set it to **Public** (GitHub Pages'
   free tier requires a public repo unless you're on a paid plan). Don't initialise
   with a README (you already have one).
4. Upload the contents of this project to the new repository. The simplest way if
   you're not familiar with git:
   - On the repo page, click **Add file → Upload files**.
   - Drag in all the files and folders from this project, keeping the folder
     structure intact (`scripts/`, `.github/workflows/`, etc).
   - Commit directly to the `main` branch.

   (If you are comfortable with git/command line, a normal `git init`, `git add .`,
   `git commit`, `git remote add origin ...`, `git push` works just as well.)

### Step 2 — Get an Anthropic API key

1. Go to [console.anthropic.com](https://console.anthropic.com) and sign in or sign up.
2. Navigate to **API Keys** and create a new key.
3. Copy it somewhere safe — you won't be able to see it again after this point.
4. Note: this uses paid API usage (there's typically a small free credit for new
   accounts, but ongoing use has a cost). One digest run — a handful of web searches
   plus a short completion — costs a small fraction of a cent to a few cents per day
   with Claude Sonnet, depending on how much searching the model does.

### Step 3 — Add your API key as a repository secret

1. In your GitHub repo, go to **Settings → Secrets and variables → Actions**.
2. Click **New repository secret**.
3. Name: `ANTHROPIC_API_KEY`. Value: paste the key from Step 2.
4. Save.

### Step 4 — Enable GitHub Pages

1. In your repo, go to **Settings → Pages**.
2. Under **Build and deployment → Source**, choose **Deploy from a branch**.
3. Branch: `main`, folder: `/ (root)`. Save.
4. GitHub will give you a URL, typically `https://<your-username>.github.io/<repo-name>/`.
   It can take a minute or two to go live after the first successful workflow run.

### Step 5 — Run the workflow for the first time

1. In your repo, go to the **Actions** tab.
2. You should see the **Daily Watchlist Digest** workflow. Click it, then
   **Run workflow → Run workflow** to trigger it manually (don't wait for the
   schedule the first time).
3. Watch the run. If it succeeds, `index.html` and `digest_history.json` will be
   committed automatically, and your Pages URL will show the first live edition
   shortly after.
4. From then on, it runs automatically once a day (see the `cron` schedule in
   `.github/workflows/daily-digest.yml` — defaults to 06:15 UTC; edit the cron
   expression if you'd like a different time).

That's it for the core setup — the page will now update itself every day.

---

## 3. Editing your coverage

Everything about *what* the digest tracks lives in `config.json`, and none of it is
secret, so you can edit it directly in the GitHub web UI (click the file → pencil icon
→ edit → commit) at any time:

- `sectors` — list of sector names.
- `companies` — list of named companies.
- `geography` — a short free-text description of geographic focus (used directly in
  the prompt, so plain English is fine).
- `preferred_sources` — starts empty. Add trade press, agency names, or specific
  sites you trust (e.g. `"S&P Global Ratings"`, `"Upstream Online"`) as you notice
  gaps or want to steer sourcing. The model will still use general web search
  alongside these.
- `history_editions_to_keep` — how many past daily editions are kept in
  `digest_history.json` and shown in the searchable archive before the oldest ones
  roll off (default 90, roughly 3 months). Raise or lower it freely; `index.html`
  always shows only the single latest edition regardless of this setting, so it's
  really just controlling how far back `archive.html` reaches.

Changes take effect on the next scheduled or manually triggered run.

---

## 4. Adding the email inbox integration (optional, do this later)

The digest can fold in newsletters or sector alert emails from a **dedicated** inbox
— deliberately not your work mailbox, so nothing touching corporate IT policy or
sensitive work email is ever involved.

### Step 1 — Create a dedicated Gmail inbox

1. Create a new, separate Gmail account just for this (e.g.
   `yourname.creditwatch@gmail.com`). Subscribe it to whatever sector newsletters or
   alert emails you'd like folded into the digest.

### Step 2 — Create an app password

Gmail app passwords let a script log in via IMAP without using your main password,
and can be revoked independently at any time.

1. On the dedicated Google account, turn on **2-Step Verification** (Google Account →
   Security) — this is required before app passwords are available.
2. Go to **Google Account → Security → 2-Step Verification → App passwords**.
3. Create a new app password (name it something like "Watchlist digest"). Copy the
   16-character password shown.

### Step 3 — Add the email secrets and settings

1. In your GitHub repo: **Settings → Secrets and variables → Actions → New repository
   secret**.
   - Name: `EMAIL_APP_PASSWORD`. Value: the app password from Step 2.
2. Edit `config.json` and update the `email` section:
   ```json
   "email": {
     "enabled": true,
     "imap_host": "imap.gmail.com",
     "imap_port": 993,
     "username": "yourname.creditwatch@gmail.com",
     "folder": "INBOX",
     "lookback_hours": 26,
     "mark_as_read": false
   }
   ```
   The username isn't sensitive on its own (it's just an address), so it lives in
   `config.json` rather than as a secret — only the password is a secret.
3. Commit the change. The next run will pull messages from the inbox received in the
   last `lookback_hours` and fold anything credit-relevant into the digest, tagged by
   sender.

Nothing about this step touches your real email account — it's fully isolated in a
separate inbox you control.

---

## 5. How the design works

`scripts/template.html` contains the page's HTML/CSS shell with two placeholders,
`{{EDITIONS}}` and `{{GENERATED_AT}}`, which `generate_digest.py` fills in on every
run. The design is a parchment-toned, serif-headline broadsheet look, with monospace
used for tags/metadata and a small coloured flag per item:

| Flag | Meaning |
|---|---|
| 🟩 Positive | Likely credit-positive development |
| 🟥 Negative | Likely credit-negative development |
| 🟨 Watch | Worth monitoring, direction unclear yet |
| ⬜ Neutral | Informational, limited credit impact expected |

If you want to change the look (colours, fonts, layout), edit the `<style>` block in
`scripts/template.html` directly — the generator script doesn't need to change.

---

## 6. Alternatives that were considered

For reference, in case costs or preferences change later:

- **RSS-only, no AI**: pull sector/company RSS feeds directly and list headlines with
  no summarisation or categorisation. Free, but loses the categorisation, credit-flag
  triage, and inbox-folding that make this useful at a glance.
- **Free-tier AI (Gemini / Groq)**: swap the Anthropic API call for a free-tier model
  from Google or Groq. Feasible, but free tiers typically lack a hosted web-search
  tool as capable as Claude's, so search quality and the categorisation step would
  need more custom engineering (e.g. wiring in a separate news API).

The Claude API was chosen as the final approach for the built-in `web_search` tool
and the quality of categorisation/summarisation in a single call.

---

## 7. Troubleshooting

- **Workflow fails on the Claude API call**: check the Action's log for the error.
  Most commonly this is a missing/incorrect `ANTHROPIC_API_KEY` secret, or a billing
  issue on the Anthropic account (check console.anthropic.com).
- **Workflow fails on the email step**: the script is written to fail *gracefully* —
  if IMAP login fails, it logs a warning and continues without email content rather
  than failing the whole run. Check the Action log for the specific IMAP error
  (usually an incorrect app password or 2-Step Verification not being enabled yet).
- **Page not updating**: confirm the workflow actually ran (Actions tab) and that
  GitHub Pages is set to deploy from `main` / root (Settings → Pages).
- **Want a different run time**: edit the `cron` line in
  `.github/workflows/daily-digest.yml` (values are in UTC).
