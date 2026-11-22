# QC V6 against the CrawlPulse 3.0 architecture documents

Checked on 22 Nov 2026 against the five documents: frontend architecture, job orchestration, system architecture,
backend architecture and crawling engine. Every row was checked in the code, and the rows marked **Done** have a test.

Each row has one of four verdicts:
- **Covered**: worked before this round.
- **Done**: was a gap and is closed now.
- **Different on purpose**: QC does the same job another way, for the reason given.
- **Not done**: still open; the reason is given.

## 1. Job orchestration

| Item | Verdict | How / where |
|---|---|---|
| The database is the authority for job state | Covered | `runs` in Postgres. Page-level progress is in each crawl's SQLite file (WAL). |
| Pause, Resume, Cancel; terminal states exclusive | Covered | Pause works from waiting or running. Guarded updates mean completed, failed and cancelled never overwrite each other. |
| `CANCELLING` state | **Done** | Cancelling a running check sets `cancelling` until the worker has stopped at its checkpoint, then `cancelled`. If the worker dies meanwhile, the check is still cancelled. Shown as "Stopping…". Migration 011. |
| FAILED → QUEUED retry by an operator | **Done** | `POST /api/v2/runs/{id}/retry`, plus a "Retry from where it stopped" button on Live and New check. Pages already checked aren't fetched again. |
| Heartbeat every 15 s; worker registry | Covered → **Done** | `workers` now also keeps hostname, PID, status (idle / busy / draining) and start time. |
| Crashed worker: work reclaimed automatically | Covered | Its check goes back in the queue, or to paused / cancelled; after 3 attempts the check is failed with a plain reason. |
| Clean shutdown on SIGTERM / SIGINT | **Done** | The page in hand finishes, the check goes back in the queue, and the worker unregisters. A second signal stops it at once. |
| `job_events` timeline | **Done** | `run_events`: created, picked up by worker X on host Y, started / resumed, each stage, pause / cancel / retry and who asked, worker lost, completed / failed. Shown as a Timeline on Live. |
| `job_minute_metrics` | **Done** | `run_minutes`: pages, errors and average response time per minute. Shown as a "Pages per minute" chart with a table on Live. |
| 50-URL batch leases spread over several workers | Different on purpose | One worker takes the whole check, with a heartbeat, and checkpoints every page, so no page is crawled twice after a crash. Several workers still run different checks in parallel. Splitting one check across machines would add a lot of complexity, and the target scale (300,000 pages) doesn't need it. |

## 2. Crawling engine

| Item | Verdict | How / where |
|---|---|---|
| Async HTTP first, bounded queues, per-host rate limits, 429 / 503 with Retry-After | Covered | |
| Sitemap index recursion, `.xml.gz`, size limits | Covered | |
| Streaming sitemap parse | **Done** | The XML is fed in 64 KB chunks to a pull parser; each entry is dropped once read, and no tree is built. On a 50,000-URL file: 21 MB peak including the result list, versus 30 MB for the old tree alone. |
| `xhtml:link` hreflang in sitemaps | **Done** | Read for every URL and saved with the check. The workbook's "Sitemap Hreflang" sheet lists alternates that are broken, redirected or never checked. |
| URL normalisation; query parameters sorted; tracking parameters removed | **Done** | Repeats are spotted by a key with lowercase host, no default port or #fragment, sorted parameters, and no `utm_*`, `gclid`, `fbclid` and similar. QC still fetches the first address exactly as listed. |
| SSRF: private / loopback / link-local / metadata / CGNAT ranges, every redirect re-checked | Covered | 100.64.0.0/10 was added this round. |
| SSRF: connect only to the checked IP (DNS rebinding) | **Done** | `engine/qcv5/crawler/safe_net.py`: each connection resolves the host once, refuses it if any answer is blocked, and connects to exactly those addresses. TLS still checks the real hostname. Used by the crawler, sitemap reader, robots fetch, extra checks, access test and webhooks. A test simulates the attack. |
| TLS certificates verified | **Done** | On by default; bad certificates become `SSL_ERROR` findings. Turn off with `QC6_TLS_VERIFY=false` (e.g. a staging site with a self-made certificate). |
| Link edges kept when the destination was seen before; anchor text | Covered | |
| `rel=nofollow` on every link edge | **Done** | `rel` and a `nofollow` flag are on every link (Parquet), with a "Nofollow" filter in Links & images and in the exports. |
| Real browser only for JavaScript-built pages | **Done** | Pages flagged as JavaScript shells now go first in the real-browser stage (up to `QC6_RENDER_MAX`, default 50). The rendered page is compared with the raw HTML: content or links that only appear after JavaScript are reported. |
| Raw HTML kept as files | **Done** (opt-in) | `QC6_KEEP_HTML=true` saves each page gzipped under `runs/<id>/html/`, with `index.csv` (url, final url, status, SHA-256, size). Off by default: 300,000 pages is several GB. |
| Memory under 250 MB per worker at 300,000 URLs | Not measured here | Each check records its own peak memory (`rss_peak_mb`). There is no 300,000-page figure against this target from this round. |

## 3. System architecture

| Item | Verdict | How / where |
|---|---|---|
| Modular monolith with separate workers; Docker Compose | Covered | |
| SQLite (WAL) as the main database | Different on purpose | Postgres is the main database (many writers, LISTEN/NOTIFY for live progress). SQLite in WAL mode is each crawl's working file. The document itself plans for Postgres later. |
| Reverse proxy / TLS in front | **Done** | `docker compose --profile https up` with `QC6_DOMAIN`: Caddy with automatic certificates, a request id and unbuffered live progress (`deploy/Caddyfile`). Not started here: Docker isn't available in this environment. |
| Secrets encrypted with AES-256-GCM | **Done** | Key derived from `QC6_SECRET_KEY` by HKDF-SHA256, random 96-bit nonce. Logins saved earlier (Fernet) still open. |
| Retention of metrics and traces | **Done** | Per-minute figures and timeline lines of finished checks are removed after `QC6_KEEP_METRICS_DAYS` (default 90). Checks, pages, problems and reports are never removed automatically. |

## 4. Backend architecture

| Item | Verdict | How / where |
|---|---|---|
| Request ID on every request | **Done** | `X-Request-ID` (kept from a proxy when sane) on every response and log line, and in every error body. |
| Error envelope with a machine code | **Done** | Errors carry `title`, `detail`, `status`, `code` (e.g. `CHECK_NOT_FOUND`) and `request_id`. Unexpected errors answer with a reference instead of a stack trace. |
| Success envelope `{success, data, meta}` | Different on purpose | Success bodies stay plain JSON. Wrapping them would change every endpoint and the app for no gain to users. |
| Passwords hashed with Argon2id | **Done** | 64 MiB, t=3, p=4. Older scrypt hashes still work and are upgraded at the next sign-in. |
| Session cookie, hash stored; TOTP | Covered | SameSite=Strict (stricter than the document), plus a CSRF header check. |
| Forgot password / e-mail | **Done** | With `QC6_SMTP_HOST` and `QC6_PUBLIC_URL` set: "Forgot your password?" sends a one-time link, valid for 30 minutes and stored as a hash only. Every answer looks the same, so nobody can find out which e-mails have accounts. |
| Open self-registration, e-mail verification | Different on purpose | There is no public sign-up for a company QC tool. People are added by whoever runs it (`python -m qc6.admin`), and the reset link proves the e-mail. |
| Repository layer (no SQL in endpoints) | Not done | A large refactor across most files with no change for users. Worth doing only if the database is ever replaced. |

## 5. Frontend architecture

| Item | Verdict | How / where |
|---|---|---|
| React 18 + TypeScript + Vite; WCAG 2.1 AA; reduced motion | Covered | axe runs on every screen in light and dark mode in the tests: 0 serious or critical problems. |
| Live progress during a crawl | Covered | Now with a timeline and per-minute figures. |
| Big tables | Different on purpose | The server sends pages of 100 rows instead of the browser virtualising 100,000. |
| TanStack Query, Zustand, React Router, React Hook Form + Zod, Tailwind + shadcn/ui | Not done | Rebuilding every screen on these libraries would change nothing a user sees. The app works, passes its accessibility checks, and is 112 KB gzipped. |
| Three.js link topology view | Not done | Links are shown as tables and figures (depth, link strength, inlinks). A 3D graph adds a large library for a picture that is hard to read at 300,000 pages. |

## Reports (from the sample V5 workbook)

The **Full audit workbook** (Download report → "Full audit workbook (Excel)") follows the V5 sample:
- the same sheets as the sample: Executive Summary, Sitemap Progress, Business / Language / Page Type Summary, Destination Status, Source Page Issues, Destination Issues, External URLs (Unique), Validation Cache Reuse;
- plus Broken URLs, Redirect URLs, Sitemap Hreflang, QC Problems, and QC Problem Pages with the "seen before" remarks.

Each sheet stops at Excel's row limit and says so. "All data as CSVs (ZIP)" has every link-level file. The one-sheet Excel / CSV / ZIP report is still there for a small file.
