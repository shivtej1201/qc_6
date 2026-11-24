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
* **Easy to get around**: the menu in four groups (Overview, Work, Explore, Setup), quick jump to any screen, fix or
  page with Ctrl+K (or /), and on phones a top bar, a slide-in menu and bottom tabs. Today opens as a dashboard.
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

### macOS (Apple Silicon or Intel)

1. Install Python 3.11+ and PostgreSQL 16 once. With [Homebrew](https://brew.sh):
   ```bash
   brew install python@3.12 postgresql@16
   ```
   ```bash
   brew services start postgresql@16
   ```
   Or use [Postgres.app](https://postgresapp.com) (open it, press Initialize) and the python.org installer.
   The `python3` that comes with macOS is too old.
2. Unzip QC and **double-click `start.command`** (or run `./start.sh` in Terminal). The first start sets everything up
   (a few minutes), finds your PostgreSQL by itself, creates the `qc6` database and opens QC in your browser.
   Ctrl+C in that Terminal window stops QC.

If macOS says the file can't be opened because it was downloaded, right-click `start.command` → Open once, or run:
```bash
xattr -dr com.apple.quarantine path/to/qc_6
```

### Linux

```bash
./start.sh
```
It finds a local PostgreSQL (asking for the `postgres` password if it needs one) and creates the `qc6` database. To use
another server: `export QC6_DATABASE_URL=postgresql://USER@HOST:5432/qc6` (password in `QC6_DB_PASSWORD`).

### Docker (any system)

`docker compose up --build`, then open http://127.0.0.1:8003.

In the app: **New check**, paste the sitemap address (or one page, or a file), then **Start crawling**. There are no setup screens.

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
tests/          81 tests (real Chromium, real Postgres), including an end-to-end UI walk with axe on every screen,
                quick jump and the phone menu, and every screen at phone width
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
| `QC6_AUTH` | `proxy` (behind oauth2-proxy + Keycloak/Google; `QC6_TRUSTED_PROXIES`, `QC6_AUTH_HEADER`), `oidc` (Bearer JWT: `QC6_OIDC_JWKS_URL`, `QC6_OIDC_ISSUER`, `QC6_OIDC_AUDIENCE`) `local` (QC's own sign-in: e-mail + password, optional two-factor; needs `QC6_SECRET_KEY`) or `dev` (refused when `QC6_ENV=production`) |
| `QC6_COOKIE_SECURE` | `local` sign-in only: set `false` when you open QC over plain `http://` (e.g. `http://localhost:8000`); keep the default (`true`) behind HTTPS |
| `QC6_OLLAMA_URL`, `QC6_AI_MODEL` | AI drafts from a free local model: Ollama address (default `http://127.0.0.1:11434`) and model (default `qwen2.5:3b`) |
| `QC6_MAX_PAGES_PER_FIX` | most affected pages kept per fix (default 20000) |
| `QC6_TLS_VERIFY` | `false` only for a site with a self-made certificate; by default certificates are checked and bad ones reported as `SSL_ERROR` |
| `QC6_RENDER_MAX` | how many JavaScript-built pages a full check opens in a real browser first (default 50) |
| `QC6_KEEP_METRICS_DAYS` | per-minute figures and timelines of finished checks are kept this many days (default 90) |
| `QC6_KEEP_HTML` | `true` keeps every page's HTML, gzipped, under `runs/<id>/html/` (large; off by default) |
| `QC6_SMTP_HOST`, `QC6_SMTP_PORT`, `QC6_SMTP_USER`, `QC6_SMTP_PASSWORD`, `QC6_MAIL_FROM`, `QC6_PUBLIC_URL` | "Forgot your password?" e-mails for `QC6_AUTH=local` (QC's address goes in `QC6_PUBLIC_URL`) |
| `QC6_DOMAIN` | with `docker compose --profile https up`: Caddy serves QC on this name with an automatic certificate |
| `QC6_VISITORS_PER_HOST`, `QC6_VISIT_SHARE` | real-visitor concurrency and their share of a site's speed limit |
| `QC6_NU_URL`, `QC6_LANGUAGETOOL_URL`, `QC6_LIGHTHOUSE_CMD`, `QC6_CHROME_PATH` | optional extra checks (self-hosted Nu HTML checker, LanguageTool, local Lighthouse) |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | GA4-led sampling (site setting `ga4_property_id`) and the V5 Search Console integration |
| `QC6_GITHUB_TOKEN`, `QC6_JIRA_TOKEN` | ticket creation (repository / Jira project set per site with `PATCH /api/v2/sites/{id}`; the Create ticket button appears once set) |
| `QC6_METRICS_TOKEN` | lets a remote Prometheus scrape `/metrics` with `Authorization: Bearer …` (without it, only this machine can) |
| `QC6_SECRET_KEY` | any long random text; encrypts staging logins (set with `PATCH /api/v2/sites/{id}`). Keep it: changing it makes saved logins unreadable |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | Search Console + GA4 for Growth (property entered on the Growth screen). Free; see Growth, Search queries for the steps |
| `QC6_DB_PASSWORD` | database password on its own, so symbols like `@` need no escaping in `QC6_DATABASE_URL` |

Every site runs on standard settings (12 requests a second, 6 visitors, up to 300,000 pages), so there is nothing to set up. To change them, use `PATCH /api/v2/sites/{id}`: speed preset (Careful 8 / Standard 12 / Fast 14 /
Large site 16→18 requests a second, from V5), speed limit, most pages per check, page timeout, retries, visitors, cookie answer, sitemap,
`check_schedule` ("daily 06:30", "weekdays 07:15", "every 6 hours"), Slack/Teams webhooks and which events alert,
GitHub/Jira. Journeys can carry their own `schedule:`.

## New check (the simple way, no setup)

QC opens on **New check**:

1. Choose **Sitemap** and paste its address (a sitemap index is followed to every sitemap it lists), or **One page**,
   or **Upload a sheet** (CSV, TSV, TXT or Excel .xlsx, addresses in any column).
2. QC lists the page addresses, live as it reads them. **Download URLs** as Excel or CSV.
3. Pick a **Speed** (each says how long this list will take) and **Start crawling.** Standard (12 requests a second)
   is picked for lists under 50,000 pages: 10,000 pages in about 16 minutes. Large site (16, rising to 18 while the
   site keeps up) is picked from 50,000: 3 lakh pages in about 5–6 hours instead of 7½. A limit set for the site
   stays its limit. QC slows down by itself when the site answers slowly or says "too many requests".
   Every page is checked; the screen shows pages done, speed and time left, every second.
   **Pause**, **Resume** and **Cancel** work at any point (pause and cancel take effect within seconds; nothing already
   checked is lost).
4. When it finishes: four numbers (pages not OK, broken links, broken images, redirecting links, each with how many
   pages have one), the top problems with what to do, and an **Ops ticket** tab: a ticket for the Ops team, filled in
   from the check (title, suggested priority, summary, every broken link / image / redirect with the pages it is on,
   steps to reproduce, expected and actual), with **Copy title** and **Copy the whole ticket**. Then **Download report** (Excel one sheet, CSV, ZIP, PDF summary).

The site is created from the address the first time; there is nothing to configure and no site to pick: the
screens show the site of the check you opened last. **Your checks** lists every
check with its status. If another check of the same site is still going, the screen says which and offers to
pause or cancel it. If the worker isn't running (for example its window was closed), the screen says so, and a
check left "pausing" by a closed worker becomes "paused" by itself instead of blocking new checks.

## Checking a few pages, reports, history, deleting

* **Check pages** (Today and All pages): paste one or more URLs, or upload a sheet (CSV, TSV, TXT or
  Excel .xlsx; the URLs can be in any column), or check the whole site. A spot check only judges the pages it looked at: it never closes a
  fix for pages it didn't see, and site-wide rules (orphan pages, duplicates across pages) wait for a full check.
* **Seen before**: every issue URL says when it was first found and on which earlier checks ("Also found on 20 Nov,
  21 Nov (first seen 20 Nov, 3 earlier checks)"). In the fix drawer, on the page screen and in the report.
* **Clear all data** (bottom of the menu, owners only; type DELETE to confirm): erases every site, check, URL list,
  report, problem and ticket from the database, their files on disk (crawl data, Excel reports, screenshots, sitemap
  files) and QC's saved data in the browser, then reloads. Sign-in accounts and the audit log (which records the
  clear) stay. Refused while a check is running: pause or cancel it first. It can't be undone.
* **Keep screen awake** (switch at the bottom of the menu): stops the screen from turning off or locking while
  QC is open, until you switch it off. Remembered on this computer. The browser lets go while QC's tab is hidden or
  minimised and QC takes it again when you come back. It works on https or when QC runs on the same computer
  (127.0.0.1 / localhost); on a plain http network address the switch says it can't.
* **Tickets** (menu): **one ticket per issue type**, not per URL: all links that redirect (3xx) are one ticket, all
  broken links another, each kind of page problem (soft 404, structured data missing...) another. Each is numbered
  QC-0001, QC-0002... (the number stays with that issue on that site), written from the site's **ticket template**
  (issue, priority P1-P4, "100 URLs on 10,000 pages", the first 20 addresses with their status, steps to reproduce,
  expected, actual, how to fix), and has a **Download attachment (Excel)** with every affected address, its status,
  where it redirects, on how many pages, example pages, and every page / address pair. Copy the ticket into the Ops
  team's tool, attach the sheet, then type the number it got and **Mark raised**. Views: To raise, Raised, Closed (QC
  closes a ticket when a check finds none of its addresses failing), All. **Download as sheet** lists every ticket
  with its text; the Excel report has a **Tickets** sheet.
* **What is checked:** the pages in the list, and every link and every image on them. CSS, JavaScript, fonts,
  preloads and source maps are not checked (not recorded, not requested), so the requests go to what matters.
* **Management report** (Download report → Management report, first in the list): the file to present. A
  **Dashboard** (KPI tiles with the change since the previous check, key findings in plain words, four native Excel
  charts, next steps), a **Pivot Summary** (status reconciliation, status codes, page type, business, language, page
  type x status matrix, top 20 failed links, colour legend), **Non-200 Responses** (every link that is not a plain 200,
  with its impact, sample page, redirect target and reason), **Tickets**, and **Problem Links** (page -> link pairs for
  the failing links, up to 50,000; all pairs are in the ticket attachments and the CSV ZIP). Healthy links are counted,
  not listed, and each URL is stored once, so it stays small: 1.3 MB for a 10,000-page check where the full workbook is
  5.5 MB.
* **Full audit workbook** (Download report → Full audit workbook) opens with sheets anyone can read in a minute:
  **Summary**, **Ops Ticket** (the same ticket, ready to copy), **Broken Links**, **Broken Images**, **Redirects** and
  **Pages Not OK**, each listing every address, its status in words ("404 Not Found", "301 Moved Permanently -> 200
  OK"), on how many pages it is, an example page and what to do. After them, the detailed QC V5 layout, one sheet each for Executive
  Summary, Sitemap Progress, Business / Language / Page Type, Destination Status, **Status Codes**, Source Page Issues,
  Destination Issues, Broken / Redirect / External URLs, **All URLs (Unique)**, **Images (Unique)**, **Image Issues**,
  Validation Cache Reuse, Sitemap Hreflang, QC Problems and QC Problem Pages.
  * *Status Codes*: every exact code seen (200, 301, 302, 304, 404, 410, 500, 503, timeouts...) for the pages
    checked, the links found on them and the images, with how many unique addresses answered it, how many times
    they're used, and an example. A redirect shows both ends: "301 Moved Permanently -> 200 OK".
  * *All URLs (Unique)* / *Images (Unique)*: each unique address found on the pages is checked once and listed once,
    whatever it answered: first and final status, meaning, final URL, redirect hops and chain, on how many pages it
    is found, the first page and three example pages (images also: pages missing alt text). Problems come first.
  * *Image Issues*: every page -> image pair where the image is broken or redirects.
  "All data as CSVs (ZIP)" has every link-level file.
* **Download report** (Insights, All pages, New check): one file, one sheet: the summary at the top, then one row per problem and
  page with severity, what to do, owner, status and the seen-before remark. CSV, Excel (`.xlsx`) or ZIP (the CSV,
  compressed, about a fortieth of the size; best for big sites). Excel stops at its 1,048,576-row limit and says so.
* **Delete**: a past check (Live, Past checks), a journey (Journeys), or a whole site (New check, open one of its
  checks, Delete … and all its checks; type the site's name). Fixes that have comments or a ticket are kept. Cannot be undone.
* **Product pages**: name, price, currency, availability, identifier (SKU/GTIN/MPN) and image in the page's
  Product data (JSON-LD or microdata), and product pages with no Product data at all.

## Polite crawling, fixes and content (from the CrawlPulse 3 comparison)

* **Crawl-delay**: when a site's robots.txt asks for N seconds between requests (for `QC-Audit` or `*`), QC waits at
  least that long between page fetches, link checks on its own hosts and real-browser visits. The check's timeline says so.
* **More page rules**: missing charset, JSON-LD without `@context` / `@type`, empty pages, pages without `<html>` /
  `<body>`, contradicting index signals, canonicals to another domain.
* **Check this fix now** (in a fix): re-checks just that fix's pages; it's confirmed when every one is clean.
* **Impact** (in a verified fix): Search Console clicks, impressions and rank for the fix's pages 14, 28 and 60 days
  before and after it was confirmed. Needs Search Console; read it as a signal, not proof.
* **Fixes → Board**: drag fixes between Open, In progress, Fixed and Won't fix (or use each card's Move-to menu).
* **Growth**: accept, dismiss or mark done each opportunity (kept as a history); click a score to see how it's made.
* **Content brief** (on a page): intent, audience, target queries, outline, title and description, questions,
  structured data and pages that should link here, written by the local AI. Edit any part or write one part again.
* **Links → Anchor text**: generic anchors ("click here", "read more"), links with no text, the most used anchors.
* **HTML QC received** (on a page): view, search and download the page's HTML when `QC6_KEEP_HTML=true`.

## Where the time goes after the crawl (tickets, report)

Every check now records how long each step after the crawl took: `summary.timings` and lines `[QC6] step …` in the
worker log. Measured on 20,000 pages (860,000 links): report package 56 s (was 64 s; the formula-injection guard now
skips the ~99.9% of rows that have no risky cell — same bytes), link facts 14 s, link intelligence 9 s, problem
grouping 6 s. The Tickets screen lists in 0.1 s; an attachment with 40,000 rows is ready in about 1 s.

* **Ops ticket sync** reads linked tickets 8 at a time over one connection pool (was one by one with a new connection
  each), reads each ticket once, retries a 5xx / 429 / network blip once, and one failure no longer stops the sync
  (the answer says how many couldn't be read).
* **No duplicate Ops tickets**: creating a ticket first claims the fix in one atomic update, so a double click or two
  people at once get "already being created" instead of a second ticket.
## Data explorer (SQL on the crawl data)

**Data explorer** (URL QC menu) runs your own SELECT queries on a check's crawl database (`runs/<check>/coverage.db`):
results table, row count, time taken, pages of 50-500 rows, CSV export (up to 10 lakh rows), example queries and every
table and column one click away. Ready-made views: `pages`, `links` (each link with its target's status), `url_status`
(each unique address, checked once, with `reuse_count`), `images`, `issues`; the raw engine tables (`url_results`,
`on_page_validation_cache`, ...) work too, e.g. `SELECT * FROM url_results WHERE final_status_code = 404`.

Safe and light: the file is opened read-only and SQLite itself refuses anything but reading (no INSERT/UPDATE/DELETE,
ATTACH, PRAGMA, extensions); one statement at a time; 30 s time limit; at most 3 queries at once. The database is in
WAL mode, so a query never blocks a running check. Every query is written to the audit log.
## Speed on slow sites and honest network remarks

* **Requests in flight follow the site's answer time.** Each host's limit is sized as agreed rate x answer time
  (Little's law), so a site that takes 3-6 s per page still gets its full agreed rate instead of about 5 pages a second.
  The rate itself (req/s, Crawl-delay, slow-down on 429/5xx) is unchanged: a slow site is not hit harder.
* **Network failures are retried, then checked a second time.** Connection, DNS and timeout errors are retried at least
  twice with back-off. At the end of the crawl every link or image that still failed for a network reason (or a 502-504)
  is checked again, calmly (8 at a time, longer timeouts). The remark then says *worked on a second check* (with the
  first error) or *confirmed on a second check*. A busy moment no longer shows a working URL as "DNS / network issue".
* **Precise remarks**: DNS temporary failure vs host name not found, connection timeout vs response timeout, connection
  refused, connection reset, SSL certificate vs handshake. When QC's own connection pool is full, the request waits and
  is not counted against the website.

## Email summary after every check (SMTP)

**Email summary** (URL QC menu, owners only) holds the workspace's SMTP settings: host, port, security (STARTTLS,
SSL/TLS or none for a local relay), username, password, sender, recipients (up to 20), on/off, and whether to attach
the Management report. **Send test e-mail** tries the settings before you save them.

When a check completes (not single-URL / spot checks), QC e-mails a summary:
* **Check:** crawl job ID, sitemap / source, start, finish, total time, and an overall status (completed successfully /
  with problems / failed, with what needs attention).
* **Pages:** URLs submitted, duplicates removed from the input, crawled, processed, failed, 200 / 3xx / 4xx / 5xx, no answer.
* **Links:** extracted page-to-URL pairs, unique URLs, duplicate URLs skipped (result reused), 200 / 3xx / 4xx / 5xx,
  DNS / network, timeout and SSL failures, blocked, and links that worked on a second check.
* **Images, report generation status and ticket processing status.**

The Management report is built, then **verified** (file exists, not empty, a complete .xlsx with no damaged parts)
before it is attached. If it is over `QC6_MAIL_MAX_ATTACH_MB` (default 20 MB) the e-mail says so and goes without it.
A temporary SMTP error is retried once. The outcome goes on the check's timeline, and the screen shows the last one.
**Send the summary of check N now** re-sends any check.

Security: the password is encrypted with `QC6_SECRET_KEY` (AES-256-GCM) and never sent to the browser (the screen
only shows that one is saved). Line breaks are refused in every field (no header injection). Only owners can change
or test the settings. Changes are written to the audit log.

## AI drafts (free, on your own machine)

1. Install Ollama from https://ollama.com and start it.
2. `ollama pull qwen2.5:3b` (about 2 GB; any model works, set `QC6_AI_MODEL`).
3. Open a fix; under **Draft with local AI** choose **Write a draft**. You get specific steps, and for title / description problems a proposed
   title and description with their lengths checked. Approve, edit or reject; nothing changes on your site.

The model sees only QC's evidence for that fix and the page's own text, is told not to follow instructions in it,
and nothing leaves your machine.

## QC's own sign-in (no identity provider)

```bash
export QC6_AUTH=local QC6_SECRET_KEY='any long random text'
python -m qc6.admin set-password lead@example.com        # asks for the password (10+ characters)
```

Members sign in with e-mail and password; under **Your account** they change their password, turn on two-factor
(any authenticator app; 10 single-use recovery codes) and sign out. 5 wrong passwords lock an account for 15
minutes. Lost phone: `python -m qc6.admin reset-2fa lead@example.com`. Opening QC over plain http? Also set
`QC6_COOKIE_SECURE=false`.

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
score and buckets (FEAT-15, FEAT-01-04), executive PDF (FEAT-18-02). Also added:
AI recommendations with a free local model (FEAT-16), password sign-in with TOTP (FEAT-20-03) and product-page data
checks (FEAT-08-02).

## Measured

| Run (simulated site, 30 req/s) | Result |
|---|---|
| Coverage, 2,000 pages | 78 s end to end in V6 (crawl, page types, V5 report package, Parquet facts, fixes): 73,503 links, 50,061 images, trust gate PASS |
| Full, 2,000 pages | 224 s: coverage, then 25 real-browser visits at human pace (6 visitors), then journeys |
| **10,000 pages at the default speed (Standard, 12 req/s)**, `scripts/bench_site.py` | **15 min 53 s** end to end (sitemap to report): 429,970 links, 60,000 images, 10,651 unique URLs and 2,000 unique images each checked once (100 x 301 -> 200, 50 x 404 found and placed on their pages); full workbook 11 s |
| Large site speed (16 -> 18 req/s), same stand-in site | held 16.0 req/s steady, no slow-downs; 3 lakh pages at that rate is about 5¾ h (Standard: about 7½ h) |
| **Slow site (every page 3-6 s), 1,000 pages, Standard 12 req/s**, `scripts/bench_site.py --slow 3,6` | before: 5 min 12 s (3.2 pages/s, 7.3 of 12 req/s used); now: **2 min 55 s** (11.7 of 12 req/s), identical results (42,970 links, 6,000 images, 7,196 findings) |
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
