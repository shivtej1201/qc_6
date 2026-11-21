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
* **Insights**: health over time, fixes new / confirmed / back again per check, time to confirm a fix, page types
  ranked by open fixes, speed and stability per page type, visit outcomes, owners, journeys. Every number says what
  it's based on; every chart has a table view.
* **All pages**: every page of a check, filterable (answer, page type, indexable, canonical, open fixes), with what's
  new, gone or changed since the check before, CSV export, and any two checks side by side.
* **Link intelligence**: clicks from the home page, internal link strength (PageRank), pages too deep or unreachable
  from home, canonicals that point at broken, redirecting, noindex or chained pages, and near-duplicate main text.
* **Growth**: pages ranked by an opportunity score with an action (fix, grow, improve CTR, refresh content, add links,
  consolidate) and the reasons in words; Search Console queries close to page one and pages competing for one query.
* **Executive report**: a one-page PDF summary, printed by the local Chromium.
* **Built to keep running**: a check whose worker dies is picked up again and resumes; crawl scope (include /
  exclude patterns) and logins for pre-production sites, sent only to the site's own hosts.
* **Carries V5 over**: V5's speed presets (with V5's measured time estimate), page limit, timeout and retries per
  site, the site's own `data-pageType` names for page types, and an importer that turns V5 audits into V6 checks.

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
  insights.py     Insights dashboard numbers;  presets.py  V5 speed presets + settings;  import_v5.py  V5 importer
  metrics.py      Prometheus metrics;  inventory.py  All pages + compare;  linkgraph.py  depth, PageRank, canonicals,
                  near-duplicates;  opportunity.py  opportunity score;  search_console.py  GSC/GA4 sync;
                  site_access.py  crawl scope, staging login, site-only headers;  report.py  executive PDF
engine/qcv5/    the crawl engine used by the coverage check (only the modules V6 uses)
web/            React + TypeScript + GSAP web app (source in src/, built app in dist/)
tests/          81 tests (real Chromium, real Postgres), including an end-to-end UI walk with axe on every screen
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
| `QC6_METRICS_TOKEN` | lets a remote Prometheus scrape `/metrics` with `Authorization: Bearer …` (without it, only this machine can) |
| `QC6_SECRET_KEY` | any long random text; encrypts staging logins (Settings, What to check and how to get in). Keep it: changing it makes saved logins unreadable |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | Search Console + GA4 for Growth (property set per site in Settings). Free; see Growth, Search queries for the steps |
| `QC6_DB_PASSWORD` | database password on its own, so symbols like `@` need no escaping in `QC6_DATABASE_URL` |

Site settings (Settings page or `PATCH /api/v2/sites/{id}`): speed preset (Careful 8 / Standard 12 / Fast 14 /
Large site 16→18 requests a second, from V5), speed limit, most pages per check, page timeout, retries, visitors, cookie answer, sitemap,
`check_schedule` ("daily 06:30", "weekdays 07:15", "every 6 hours"), Slack/Teams webhooks and which events alert,
GitHub/Jira. Journeys can carry their own `schedule:`.

## Moving from QC V5

```bash
python -m qc6.admin list-sites
python -m qc6.import_v5 list   path/to/qc_v5/audit_cache.db                     # what's in the V5 database
python -m qc6.import_v5 import path/to/qc_v5/audit_cache.db --site 1 --v5-data path/to/qc_v5/data/onpage
python -m qc6.admin import-v5-config 1 path/to/qc_v5/.env                       # page limit, timeout, retries, rate
```

Each completed V5 audit becomes a V6 check (oldest first), its issues become fixes through the same grouping and
wording a V6 check uses, links and images go into the explorer, and the V5 trust gate is carried over when
`--v5-data` is given. Importing again skips audits already imported; the V5 database is only read. V5 kept no page
HTML, so imported page types come from URL patterns (or the site's `data-pageType`), and imported checks had no
real-browser visits. The check page says so.

## Operations

* `GET /metrics`: Prometheus text: waiting/running checks, oldest waiting check, check ETA, visits and their failure
  share, hosts that paused QC, open fixes, journey outcomes, free disk. Suggested alerts are in `qc6/metrics.py`.
* Every change a person makes is in the `audit_log` table (who, what, when).
* API reads carry an `ETag`; send `If-None-Match` to get `304 Not Modified`. Responses are `no-store`.

## From the CrawlPulse documents

The CrawlPulse 3.0 feature catalog was compared with V6. Already covered by the V5 engine inside V6: redirect chains
and loops, orphan pages, exact duplicate titles / descriptions / content, missing and multiple canonicals, sitemap
lastmod checks, hreflang, thin content, soft 404, structured data. Added: worker heartbeat and recovery (FEAT-19-02),
crawl scope and staging access (FEAT-02-02/03), URL inventory and lifecycle (FEAT-03-02/03), check comparison
(FEAT-17-02), internal PageRank and depth (FEAT-09-02), canonical target validation (FEAT-11-01), near-duplicate
clustering (FEAT-10-02), Search Console / GA4 sync, striking distance and cannibalisation (FEAT-12/13/14), opportunity
score and buckets (FEAT-15, FEAT-01-04), executive PDF (FEAT-18-02). Not added: AI-written recommendations (FEAT-16;
needs a paid API or a local model), password login and TOTP (FEAT-20-03; use your identity provider through OIDC),
PDP price / availability checks beyond the existing structured-data rules (FEAT-08-02).

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
| 6 Hardening | Partly: security headers, SSRF guard on every browser request, webhook/ticket hosts allow-listed, auth tests, dependency audit in CI, Prometheus metrics, audit log of every change, ETag, end-to-end UI test with axe (0 serious/critical on every screen, light and dark). Not done: an external security review, a 300K-page run inside V6, behaviour against a real Akamai/Cloudflare edge |
| Migration (§15) | Done: V5 importer (audits → checks and fixes), V5 presets and settings, V5 `.env` import. V5 retirement is your call after two V6 checks per site match V5 within the trust gate |

Known limits: in a full check the real-visitor stage starts after the coverage stage (the blueprint runs them in
parallel); visits need the page types the coverage stage produces. Live progress for a check started elsewhere
arrives over SSE only while the page is open.
