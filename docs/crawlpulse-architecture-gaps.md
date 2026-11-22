# QC V6 against the CrawlPulse 3.0 architecture documents

Checked on 22 Nov 2026 against the five documents: frontend architecture, job orchestration, system architecture,
backend architecture and crawling engine. Every row below was checked in the code, not from memory.

Each row has one of four verdicts:
- **Covered**: works the way the document asks.
- **Different on purpose**: QC does the same job another way, for a stated reason.
- **Gap**: missing or weaker than the document asks. Each gap has a priority: P1 (security or correctness), P2 (scale or operations) or P3 (nice to have).
- **Closed now**: was a gap and is fixed in this change.

## Closed in this change

| Document | Item | What changed |
|---|---|---|
| Crawling engine §4.4 | Block 100.64.0.0/10 (carrier-grade NAT) | QC's address check now blocks it. Python's `is_private` says False for this range, so it had been allowed. |
| Job orchestration §5.2 | Clean shutdown on SIGTERM / SIGINT | The worker stops taking checks and finishes the page in hand. The check stops at its checkpoint, goes back in the queue for the next worker, and the worker removes itself from `workers`. A second signal stops it at once. |

## 1. Job orchestration

| Item | Verdict | Notes |
|---|---|---|
| Database is the authority for job state | Covered | `runs` in Postgres. Each crawl's page-level progress is in its own SQLite file (WAL), so a resumed crawl never fetches a finished page again. |
| Pause, Resume, Cancel; terminal states exclusive | Covered | Pause works from waiting or running. Cancel works at any point, including while running. Completed, failed and cancelled never overwrite each other (guarded updates). |
| Worker heartbeat every 15 s; registry | Covered | `workers.last_seen` and `runs.heartbeat_at`, every 15 s. |
| Crashed worker: work reclaimed without a person | Covered | A check whose worker went quiet goes back in the queue, and a "pausing" check becomes "paused". After 3 attempts the check is failed with a plain reason. |
| No "stuck" flag from elapsed time alone | Covered | Checks are judged only by missed heartbeats and the worker's last-seen time. |
| 50-URL batch leases with 60 s lease expiry | Different on purpose | One worker leases the whole check, renewed by heartbeat, and the crawl checkpoints every page. Recovery is just as safe: no page is crawled twice. It doesn't spread one check over several workers. Gap P3 if one site must be crawled by several machines at once. |
| `CANCELLING` state | Gap P3 | Cancel goes straight to `cancelled`, and the worker stops within seconds. A separate state would only show "stopping" for those seconds. |
| `job_events` timeline table | Gap P2 | State changes go to `audit_log` (who and when) and to live events, but there is no per-check event timeline (worker joined, timeouts, retries). |
| FAILED → QUEUED retry by an operator | Gap P3 | "Try again" starts a new check from the same URL list, instead of re-queueing the failed one. |
| `job_minute_metrics` time series | Gap P2 | Live progress and `/metrics` (Prometheus) exist. There are no stored per-minute figures (requests, bytes, latency, errors). |
| Worker registry: hostname, PID, IDLE / BUSY / DRAINING | Gap P3 | `workers` keeps only the id, last seen time and current check. |

## 2. Crawling engine

| Item | Verdict | Notes |
|---|---|---|
| HTTPX-first, async, bounded queues | Covered | Pipelined engine with a per-host connection pool and bounded queues. Measured at 300,000 pages. |
| Sitemap index recursion, `.xml.gz`, size limits | Covered | 50 MB per file (the sitemaps.org limit). Gzip is unpacked with a size cap. Loops are detected. |
| Sitemaps parsed in streaming mode (lxml `iterparse`) | Gap P2 | Each file is parsed whole with ElementTree, capped at 50 MB, so the whole file and its tree are in memory at once. A maximum-size sitemap uses several times its size in memory. |
| `<lastmod>` captured | Covered | |
| `xhtml:link` hreflang in sitemaps | Gap P3 | Hreflang is read from the pages, not from sitemaps. |
| Per-host token bucket, 429 / 503 with Retry-After, adaptive backoff | Covered | |
| SSRF: DNS check, private / loopback / link-local / metadata ranges, every redirect re-checked | Covered | Plus 100.64.0.0/10, closed now. |
| SSRF: pin the socket to the checked IP (DNS rebinding) | Gap P1 | The address is checked, then the HTTP client looks it up again. A hostile DNS server could answer differently the second time. Fix: a transport that connects to the checked IP and keeps the Host header and SNI. |
| TLS certificates verified | Gap P1 | The crawler and sitemap reader use `verify=False`, so expired or self-signed certificates don't stop a check. That hides certificate problems from the report and lets an attacker in the network path change what QC sees. Better: verify by default and report certificate errors as a finding. |
| URL normalisation: lowercase, strip fragment, default ports | Covered | |
| Query parameters sorted; tracking parameters (`utm_*`, `gclid`…) removed | Gap P2 | Not done. QC checks each URL exactly as listed. Sorting or stripping changes which URL is fetched, so it should be applied to de-duplication only, not to the request. |
| Link edges kept when the destination was already seen; anchor text | Covered | Every source → destination edge is kept, with anchor text and location (Parquet), and feeds PageRank and depth. |
| `rel=nofollow` per link edge | Gap P3 | Page-level nofollow is checked. Per-link nofollow isn't stored on the edge. |
| Playwright only for JavaScript-rendered pages (hydration heuristics) | Gap P2 | Pages are flagged `js_rendering_suspected`, and sample pages open in a real browser. Flagged pages aren't automatically re-crawled rendered. |
| Raw HTML kept in a file store | Gap P3 | QC keeps facts (Parquet), screenshots and reports, not raw HTML. That keeps storage small, but you can't diff old HTML. |
| Memory under 250 MB per worker | Not measured here | The engine records peak RSS for each check (`rss_peak_mb`), but no 300,000-page figure is recorded against this target. |

## 3. System architecture

| Item | Verdict | Notes |
|---|---|---|
| Modular monolith with separate workers; Docker Compose | Covered | API, worker and database in `docker-compose.yml`. Workers scale with `--scale worker=N`. |
| SQLite in WAL mode as the main database | Different on purpose | Postgres 16 is the main database: many workers write at once, and LISTEN/NOTIFY gives live progress. SQLite in WAL mode is used for each crawl's own working file. The document itself plans for Postgres later. |
| Reverse proxy / TLS in front | Gap P3 | Not in the compose file. Behind your own Nginx or Caddy, set `QC6_COOKIE_SECURE`. |
| Workspace isolation on every query | Covered | Every query filters by workspace. Since this change the screens no longer show workspaces. |
| Secrets encrypted at rest (AES-256-GCM) | Different on purpose | Fernet (AES-128-CBC + HMAC-SHA256) under `QC6_SECRET_KEY`. Authenticated encryption, but a different cipher from the document. |
| Retention and pruning of old metrics and traces | Gap P2 | No automatic cleanup. Old checks are deleted by hand (Live, Past checks). |

## 4. Backend architecture

| Item | Verdict | Notes |
|---|---|---|
| FastAPI, Pydantic v2, problem messages people can act on | Covered | |
| Standard success / error envelope (`success`, `data`, `meta.request_id`, `error.code`) | Different on purpose | Errors use RFC 9457 problem JSON (`title`, `detail`). Success bodies are plain JSON. There are no machine error codes. Gap P3: add `code`. |
| Request ID on every request | Gap P2 | No request ID middleware or header, so logs can't be tied to a request. |
| Repository layer; no SQL in endpoints | Gap P3 | SQL sits in services and some endpoints. It works, but moving to another database would touch many files. |
| `/api/v1` | Different on purpose | `/api/v2`: V1 was the old QC V5 API. |
| Passwords hashed with Argon2id | Different on purpose | scrypt (N=2^15, r=8, p=1) from Python's standard library, a memory-hard hash with no extra dependency. Gap P3 if Argon2id is a hard requirement. |
| Session cookie HttpOnly, Secure, SameSite=Lax; hash stored | Covered | SameSite=Strict, which is stricter, plus a CSRF header check. |
| TOTP two-factor | Covered | With single-use recovery codes. |
| Register, verify e-mail, forgot password | Gap P3 | People are added and reset by `python -m qc6.admin`. There is no e-mail flow, and no mail server is needed. |

## 5. Frontend architecture

| Item | Verdict | Notes |
|---|---|---|
| React 18 + TypeScript + Vite | Covered | |
| Accessible (WCAG 2.1 AA) | Covered | axe runs on every screen in light and dark mode in the test suite: 0 serious or critical problems. Keyboard paths are tested. |
| `prefers-reduced-motion` respected; GSAP only for small motion | Covered | |
| Live progress during a crawl | Covered | Updated every second (polling plus Server-Sent Events). |
| TanStack Query, Zustand, React Router, React Hook Form + Zod | Gap P3 | Not used. The app has a small loader hook, hash routes and plain forms. It works and the bundle is small (112 KB gzipped), but it isn't the document's stack. |
| Tailwind + shadcn/ui (Radix) | Gap P3 | Own CSS with design tokens, light and dark. |
| Virtualised tables for 100,000+ rows | Different on purpose | The server pages them, 100 rows at a time, so the browser never holds more than one page of rows. |
| Web Workers for CSV and graph work | Different on purpose | Exports are built on the server (CSV, one-sheet Excel, ZIP), so the browser does no heavy work. |
| Three.js link topology view | Gap P3 | Links are shown as tables and figures (depth, link strength, inlinks), not as a 3D graph. |
| Route tree `/auth/…`, `/app/w/:workspaceId/…`, projects | Different on purpose | Workspaces and projects are no longer shown. A site is created from the address on New check. |

## What to do next, in order

1. **P1, security**: connect to the IP that was checked (blocks DNS rebinding), and verify TLS by default, reporting certificate errors as findings.
2. **P2, scale**:
   - stream sitemaps with `iterparse`;
   - a request ID on every request;
   - de-duplicate URLs with sorted parameters and tracking parameters removed;
   - re-crawl JavaScript-rendered pages in a browser.
3. **P2, operations**:
   - a per-check event timeline;
   - per-minute metrics;
   - automatic cleanup of old checks.
4. **P3**: everything else marked P3 above.
