# QC V6

Site quality checks that work the way a careful person would:

* **Every page** in the sitemap, every link, image and SEO signal on it (crawl engine proven at 300,000 pages in
  3 h 22 min).
* **Real visitors**: a sample covering every page type, opened in real Chromium by visitors who wait for the page,
  answer the cookie banner, read, scroll, then measure what a person experienced (speed, layout shift, accessibility,
  content that only appears after JavaScript, browser errors) and take a screenshot.
* **Journeys**: paths customers take, walked at a person's pace; never presses Submit or Pay.
* **Fixes**, not rows: one fix per root cause, in plain language (what's wrong, why it matters, how to fix, how we'll
  know), owned by a person, verified by the next check. Alerts to Slack/Teams, tickets in GitHub/Jira.

QC identifies itself honestly on every request (`QC-Audit/6.0` in the user agent and an `X-QC-Audit` token). It never
disguises itself or gets around bot protection: if a CDN challenges it, it stops and gives you the allow rule.

## Start it

### Windows

1. Install **Python 3.11+** from https://www.python.org/downloads/ and tick **"Add python.exe to PATH"**.
2. Install **PostgreSQL 16** from https://www.postgresql.org/download/windows/ and remember the password you set for
   the `postgres` user (keep the default port 5432).
3. Double-click **start.bat** (or run `.\start.bat` in PowerShell). It asks for that password once, creates the `qc6`
   database, installs everything on the first start (a few minutes) and opens http://127.0.0.1:8003.

To skip the password question, set it first (any characters work, including `@`). PowerShell: `$env:QC6_DB_PASSWORD = "your password"`.
Or set the full address. PowerShell: `$env:QC6_DATABASE_URL = "postgresql://postgres:PASSWORD@127.0.0.1:5432/qc6"`
· Command Prompt: `set QC6_DATABASE_URL=postgresql://postgres:PASSWORD@127.0.0.1:5432/qc6`

### Linux / macOS

```bash
export QC6_DATABASE_URL=postgresql://postgres:PASSWORD@127.0.0.1:5432/qc6     # the database is created for you
./start.sh
```

### Docker (any system)

`docker compose up --build`, then open http://127.0.0.1:8003.

In the app: **Add a site**, enter the site's address, press **Check the site now**.

Try it on a built-in demo site (local testing only; never set this for a real site):

```bash
export QC6_ALLOW_PRIVATE_TARGETS=true
.venv/bin/python scripts/demo.py --port 8892      # seeds a demo workspace + site, then serves the site
```

## Layout

```
qc6/            the application
  api/app.py      REST API /api/v2 + live events (SSE) + serves the web app
  workers/        run orchestration, coverage, real visitors, journeys, extra checks
  domain/         page types (SimHash), plain-language rules, fixes, score, visual change
  crawl/          human pacing, shared per-site speed limit, bot-challenge detection
  auth.py, scheduler.py, notify.py, tickets.py, recorder.py, ga4.py, facts.py
  db/             Postgres pool + migrations
engine/qcv5/    the crawl engine used by the coverage check (only the modules V6 uses)
web/            React + TypeScript + GSAP web app (source in src/, built app in dist/)
tests/          37 tests (real Chromium, real Postgres)
docs/           architecture blueprint, design spec, clickable prototype, screenshots
scripts/demo.py local demo site + seed data
```

## Rebuild the web app (only if you change it)

```bash
cd web && npm ci && npm run build
```

## Tests

```bash
export QC6_TEST_DATABASE_URL=postgresql://postgres:YOUR_PASSWORD@127.0.0.1:5432/qc6_test     # an empty database
python -m pytest -q
```

## Configuration (environment)

| Variable | Purpose |
|---|---|
| `QC6_DATABASE_URL` | Postgres connection |
| `QC6_DATA_DIR` | screenshots, facts (Parquet), V5 report ZIPs |
| `QC6_AUTH` | `proxy` (behind oauth2-proxy + Keycloak/Google; `QC6_TRUSTED_PROXIES`, `QC6_AUTH_HEADER`), `oidc` (Bearer JWT: `QC6_OIDC_JWKS_URL`, `QC6_OIDC_ISSUER`, `QC6_OIDC_AUDIENCE`) or `dev` (refused when `QC6_ENV=production`) |
| `QC6_VISITORS_PER_HOST`, `QC6_VISIT_SHARE` | real-visitor concurrency and their share of a site's speed limit |
| `QC6_NU_URL`, `QC6_LANGUAGETOOL_URL`, `QC6_LIGHTHOUSE_CMD`, `QC6_CHROME_PATH` | optional extra checks (self-hosted Nu HTML checker, LanguageTool, local Lighthouse) |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | GA4-led sampling (site setting `ga4_property_id`) and the V5 Search Console integration |
| `QC6_GITHUB_TOKEN`, `QC6_JIRA_TOKEN` | ticket creation (repository / Jira site set per site in Settings) |

Site settings (Settings page or `PATCH /api/v2/sites/{id}`): speed limit, visitors, cookie answer, sitemap,
`check_schedule` ("daily 06:30", "weekdays 07:15", "every 6 hours"), Slack/Teams webhooks and which events alert,
GitHub/Jira. Journeys can carry their own `schedule:`.

## Measured

| Run (simulated site, 30 req/s) | Result |
|---|---|
| Coverage, 2,000 pages | 78 s end to end in V6 (crawl, page types, V5 report package, Parquet facts, fixes): 73,503 links, 50,061 images, trust gate PASS |
| Full, 2,000 pages | 224 s: coverage, then 25 real-browser visits at human pace (6 visitors), then journeys |
| V5 engine alone, 300,000 pages | 3 h 22 min end to end (V5 benchmark) |

## Status against the delivery plan (blueprint §16)

| Phase | State |
|---|---|
| 0 Foundations | Done: schema + migrations, API, web app, compose file, CI workflow (GitHub Actions), sign-in (trusted proxy or OIDC bearer). Keycloak itself is deployed by you (any OIDC provider works) |
| 1 Coverage on new storage | Done: V5 engine inside V6, state in Postgres, link/image facts in Parquet + DuckDB (explorer, CSV export), live progress over SSE, pause/resume |
| 2 Fixes and inbox | Done |
| 3 Real-visitor review | Done, including visual change between checks |
| 4 Journeys | Done: YAML, runner, filmstrip, irreversible-action guard, schedules, recorder (Playwright codegen → steps), Slack/Teams alerts |
| 5 Integrations | Done: GA4-led sampling, Nu HTML checker, LanguageTool, local Lighthouse, GitHub/Jira tickets with status sync, V5 free integrations |
| 6 Hardening | Partly: security headers, SSRF guard on every browser request, webhook/ticket hosts allow-listed, auth tests, dependency audit in CI. Not done: an external security review, a 300K-page run inside V6, behaviour against a real Akamai/Cloudflare edge |

Known limits: in a full check the real-visitor stage starts after the coverage stage (the blueprint runs them in
parallel); visits need the page types the coverage stage produces. Live progress for a check started elsewhere
arrives over SSE only while the page is open.
