// Typed client for /api/v2. Errors carry the server's plain-language title/detail.
export class ApiError extends Error {
  constructor(public status: number, public title: string, public detail: string, public data: Record<string, any> = {}) { super(detail || title); }
}

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { "X-QC-Requested-With": "qc6" };      // QC's own sign-in: proves the request is the app's
  const user = localStorageGet("qc6-user");
  if (user) headers["X-QC-User"] = user;
  const form = typeof FormData !== "undefined" && body instanceof FormData;
  if (body !== undefined && !form) headers["Content-Type"] = "application/json";
  const r = await fetch(path, { method, headers, body: body === undefined ? undefined : form ? (body as FormData) : JSON.stringify(body) });
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) {
    const d = (typeof data === "object" && data) || {};
    if (r.status === 401 && (d as any).detail === "auth=local" && !path.startsWith("/api/v2/auth/")) window.location.reload();  // session ended: back to sign-in
    throw new ApiError(r.status, (d as any).title || `Request failed (${r.status})`, (d as any).detail || "", d as Record<string, any>);
  }
  return data as T;
}

/** POST that answers with a file (CSV): returns the file and how many rows it holds. */
export async function postForFile(path: string, body: unknown): Promise<{ blob: Blob; rows: number }> {
  const headers: Record<string, string> = { "X-QC-Requested-With": "qc6", "Content-Type": "application/json" };
  const user = localStorageGet("qc6-user");
  if (user) headers["X-QC-User"] = user;
  const r = await fetch(path, { method: "POST", headers, body: JSON.stringify(body) });
  if (!r.ok) {
    const d = (r.headers.get("content-type") || "").includes("json") ? await r.json() : {};
    throw new ApiError(r.status, d.title || `Request failed (${r.status})`, d.detail || "", d);
  }
  return { blob: await r.blob(), rows: Number(r.headers.get("X-Row-Count") || 0) };
}

export function localStorageGet(k: string): string | null {
  try { return window.localStorage.getItem(k); } catch { return null; }
}
export function localStorageSet(k: string, v: string): void {
  try { window.localStorage.setItem(k, v); } catch { /* storage blocked */ }
}

export interface PipelineSource { rows: number; valid: number; invalid: number; duplicates: number; other_hosts: number; outside_scope: number; added: number }
export interface Pipeline { stage: string; sources: string[]; by_source: Record<string, PipelineSource>; sitemaps: number;
  read: number; valid: number; invalid: number; duplicates: number; other_hosts: number; outside_scope: number; rejected: number;
  queued: number; completed: number; failed: number; remaining: number; links_reused: number }
export interface Job { id: number; site_id: number; source: "sitemap" | "single" | "sheet" | "list" | "mixed"; pipeline?: Pipeline; label: string;
  status: "extracting" | "ready" | "failed" | "cancelled"; url_count: number; error: string | null; primary_host: string;
  progress: { sitemaps?: number; urls?: number; queued?: number; rows?: number; duplicates?: number; rejected?: number; errors?: number };
  note: { duplicates?: number; other_hosts?: number; outside_scope?: number; capped?: boolean };
  created_at: string; updated_at: string; sample?: string[];
  run_id: number | null; run_status: string | null; run_kind: string | null; run_error: string | null;
  run_progress: { stage?: string; pages_done?: number; pages_total?: number; visits_done?: number; visits_total?: number; stages_done?: string[] } | null;
  run_summary: { pages?: number; score?: number | null; visited?: number } | null; run_started: string | null; run_finished: string | null;
  results?: Partial<Record<Severity, { problems: number; pages: number }>>;
  speeds?: { id: string; label: string; rps: number; max_rps: number; minutes: number }[]; speed_default?: string }

export interface ImpactWindow { from: string; to: string; clicks: number; impressions: number; ctr: number | null; position: number | null }
export interface FixImpact { state: "ok" | "not_verified" | "no_search_console"; text: string; verified_at?: string; pages?: number;
  windows: { days: number; ready: boolean; days_so_far?: number; text?: string; before: ImpactWindow; after?: ImpactWindow;
    change?: { clicks: number; clicks_pct: number | null; impressions: number; impressions_pct: number | null; position: number | null } }[] }

export interface Brief { id: number; site_id: number; url: string; model: string; status: string; created_at: string; updated_at: string;
  sections: Record<string, { text: string; edited: boolean; written_at: string; edited_by?: string }>; order: string[]; labels: Record<string, string> }
export interface Anchors { internal_links: number; generic_links: number; generic_share: number; basis: string;
  generic: { anchor: string; links: number; pages: number; example_target: string; example_page: string }[];
  empty: { links: number; pages: number; examples: { page: string; target: string }[] };
  top: { anchor: string; links: number; targets: number }[];
  same_everywhere: { url: string; links: number; anchor: string; share: number }[];
  scattered: { url: string; links: number; variants: number; top_anchor: string; share: number }[] }

export interface QcRow { url: string; status: string; pages: number; example: string; examples: string; scope?: string; final_url?: string; text?: string; detail?: string }
export interface QcBlock { unique: number; pages: number; fix: string; rows: QcRow[] }
export interface OpsTicket { run_id: number; status: string;
  qc: { pages: { total: number; checked: number; ok: number; redirected: number; not_ok: number; rows: QcRow[]; fix: string };
    links: { unique: number }; images: { unique: number }; broken_links: QcBlock; redirects: QcBlock; broken_images: QcBlock };
  ticket: { title: string; priority: string; text: string } }

export interface InternalTicket { id: number; ticket_id: string; group_key: string; issue: string; title: string; text: string;
  priority: string; severity: Severity; type: string; urls: number; pages: number; affected: string; fixes: number;
  state: "to_raise" | "raised" | "closed"; raised_ref: string | null; raised_at: string | null; regressed: boolean; attachment: string;
  template_name?: string; component?: string }
export interface TicketTemplate { title: string; body: string; is_default: boolean }
export interface InternalTickets { tickets: InternalTicket[]; counts: Record<string, number>; template: TicketTemplate;
  placeholders: { name: string; means: string }[]; grouping?: "template_component" | "issue_type" }

export type Severity = "critical" | "high" | "medium" | "low";
export type FixStatus = "open" | "in_progress" | "fixed_pending_check" | "verified" | "wont_fix";
export interface Site { id: number; name: string; base_url: string; primary_host: string; settings: Record<string, any>;
  score?: number | null; open_fixes?: number; last_run?: Run | null }
export interface Fix { id: number; rule: string; severity: Severity; dimension: string; scope: string; title: string; status: FixStatus;
  pages_affected: number; template_name?: string | null; owner_id?: number | null; owner_name?: string | null; regressed: boolean;
  texts: { title: string; why: string; fix: string; verify: string }; example_url?: string | null; source: string;
  ticket_url?: string | null; updated_at: string }
export interface Run { id: number; kind: string; status: string; progress: Record<string, any>; summary: Record<string, any>;
  error?: string | null; created_at: string; started_at?: string | null; finished_at?: string | null; options?: Record<string, any>;
  spot?: { label: string; source: string; file: string | null; count: number } | null }
export interface Member { id: number; name: string; email: string; role: string }
export interface TodayCard { kind: string; title: string; text: string; action: string; fix_id?: number; journey_run_id?: number }
export interface Today { greeting: string; lede: string; cards: TodayCard[]; score: number | null; trend: { score: number; at: string }[];
  checked: string; last_run: Run | null; fixes: { open: number; waiting: number; verified: number }; site: Site;
  kpis: { open_by_severity: Record<Severity, number>; pages: number | null; visited: number | null;
    journeys: { total: number; passed: number }; score_change: number | null };
  flow: { run_id: number; at: string; new: number; verified: number; regressed: number }[] }
export interface Step { idx: number; action: string; label: string; status: string; detail: string; screenshot?: string | null; duration_ms: number }
export interface Journey { id: number; name: string; device: string; enabled: boolean; source_yaml: string;
  recent: { id: number; status: string; failed_step: number | null; duration_ms: number; started_at: string }[] }

export interface Insights {
  runs_considered: number; last_run_id: number | null; headlines: string[];
  score: { points: { run_id: number; at: string; score: number; dimensions: Record<string, number> }[]; dimension_change?: Record<string, number>; basis: string };
  fix_flow: { runs: { run_id: number; at: string; new: number; verified: number; regressed: number }[];
    totals_after_first?: { new: number; verified: number; regressed: number }; basis: string };
  backlog: { by_severity: { severity: Severity; open: number; pages: number }[]; by_status: Record<string, number>; unowned: number; basis: string };
  time_to_verify: { median_days: Record<string, number>; median_hours?: Record<string, number>; count: number; basis: string };
  page_types: { items: { template_id: number; name: string; pages: number; open_fixes: number; worst: Severity | null; visited: number; trouble: number }[]; basis: string };
  vitals: { items: { template_id: number; name: string; device: string; visits: number; lcp: number | null; cls: number | null; inp: number | null;
    ttfb: number | null; rating: Record<string, string | null> }[]; basis: string };
  visits: { by_status: Record<string, number>; total?: number; reasons: { reason: string; visits: number }[]; basis: string };
  crawl: { status_mix: Record<string, number>; pages_by_run: { run_id: number; at: string; pages: number | null; minutes: number | null }[]; basis: string };
  owners: { items: { id: number; name: string; open: number; waiting: number; verified_30d: number }[]; basis: string };
  journeys: { items: { id: number; name: string; runs: number; passed: number; median_s: number | null }[]; basis: string };
  regressions: { items: { id: number; title: string; severity: Severity; pages_affected: number; at: string }[]; basis: string };
}

export interface InvPage { url: string; http_status: number | null; title: string | null; template_name: string | null; noindex?: boolean;
  canonical?: string; canonical_url?: string; depth?: number | null; inlinks?: number | null; authority?: number | null; words?: number | null;
  open_fixes?: number; change: string | null; previous_status: number | null }
export interface Inventory { run_id: number; previous_run_id: number | null; total: number; pages: InvPage[];
  changes: { new?: number; status_changed?: number; gone?: number }; checks: { id: number; finished_at: string; pages: number | null; imported: string | null; spot: string | null }[] }
export interface Compare { a: { id: number; finished_at: string }; b: { id: number; finished_at: string };
  pages: { before: number; after: number; added: number; removed: number; status_changed: number };
  samples: { added: { url: string; http_status: number }[]; removed: { url: string; http_status: number }[];
    status_changed: { url: string; before: number | null; after: number | null }[] };
  fixes: Record<"new" | "confirmed" | "back", { count: number; items: { id: number; title: string; severity: Severity; pages: number }[] }>;
  score: { before: number; after: number; change: number; dimensions: Record<string, number> } | null;
  page_types: { name: string; before: number; after: number }[] }

export interface Opportunity { url: string; template: string; score: number; bucket: string; reasons: string[];
  clicks: number | null; impressions: number | null; sessions: number | null; depth: number | null; authority: number | null;
  parts?: Record<string, number>; multiplier?: number; review?: { status: ReviewStatus; note: string; created_at: string; by: string | null } | null }
export interface Opportunities { run_id: number; total: number; pages: Opportunity[]; buckets: { id: string; label: string; pages: number }[];
  data: { search_console: boolean; ga4: boolean; links: boolean }; confidence: number; basis: string;
  review_counts: Record<string, number>; parts_labels: Record<string, string> }
export type ReviewStatus = "open" | "accepted" | "dismissed" | "done";
export interface SearchData {
  config: { service_account: boolean; service_account_error: string | null; gsc_property: string | null; ga4_property_id: string | null; ready: boolean };
  last_sync: { synced_at: string; start_date: string; end_date: string; gsc_rows: number; ga4_pages: number; truncated: boolean; error: string | null } | null;
  striking: { query: string; page: string; clicks: number; impressions: number; ctr: number; position: number; expected_ctr: number; clicks_at_position_3: number }[];
  cannibalisation: { query: string; impressions: number; clicks: number; pages_top20: number; pages: { page: string; position: number; clicks: number; impressions: number }[] }[];
  thresholds: { striking_min_impressions: number; cannibal_min_impressions: number } }

export interface AIDraft { id: number; kind: "fix_steps" | "title_description"; model: string; page_url: string | null; status: "draft" | "approved" | "rejected";
  evidence: Record<string, any>; draft: AIText; final: AIText | null; decided_by_name?: string | null; created_at: string }
export interface AIText { title?: string; description?: string; steps: string[]; reason: string; check?: string; warnings: string[] }
export interface SpotStarted { run_id: number; urls: number; other_hosts: number; other_examples: string[]; duplicates: number; spot: boolean; label: string }

export const api = {
  me: () => req<{ member: Member; workspace: { id: number; name: string } }>("GET", "/api/v2/me"),
  members: () => req<{ members: Member[] }>("GET", "/api/v2/members"),
  sites: () => req<{ sites: Site[] }>("GET", "/api/v2/sites"),
  createSite: (b: { name: string; base_url: string }) => req<Site>("POST", "/api/v2/sites", b),
  today: (sid: number) => req<Today>("GET", `/api/v2/sites/${sid}/today`),
  insights: (sid: number, runs = 12) => req<Insights>("GET", `/api/v2/sites/${sid}/insights?runs=${runs}`),
  fixes: (sid: number, view: string, q: string, limit = 100) =>
    req<{ fixes: Fix[]; counts: Record<string, number> }>("GET", `/api/v2/sites/${sid}/fixes?${new URLSearchParams({ view, q, limit: String(limit) })}`),
  recheckFix: (id: number) => req<{ run_id: number; pages: number; kind: string }>("POST", `/api/v2/fixes/${id}/recheck`),
  fixImpact: (id: number) => req<FixImpact>("GET", `/api/v2/fixes/${id}/impact`),
  fix: (id: number) => req<{ fix: Fix; pages: string[]; examples: { page_url: string; screenshot: string }[];
    events: { kind: string; detail: any; created_at: string; actor: string | null }[]; page_history?: Record<string, string> }>("GET", `/api/v2/fixes/${id}`),
  patchFix: (id: number, b: Record<string, unknown>) => req<Fix>("PATCH", `/api/v2/fixes/${id}`, b),
  runs: (sid: number) => req<{ runs: Run[] }>("GET", `/api/v2/sites/${sid}/runs`),
  aiStatus: () => req<{ ready: boolean; reachable: boolean; model: string; models: string[]; help: string | null }>("GET", "/api/v2/ai/status"),
  aiDrafts: (fixId: number) => req<{ suggestions: AIDraft[] }>("GET", `/api/v2/fixes/${fixId}/ai`),
  aiDraft: (fixId: number) => req<AIDraft>("POST", `/api/v2/fixes/${fixId}/ai`, {}),
  aiDecide: (id: number, status: "approved" | "rejected", final?: Partial<AIText>) => req<AIDraft>("PATCH", `/api/v2/ai/${id}`, { status, final }),
  deleteRun: (id: number) => req<{ pages: number; fixes_removed: number; screenshots_removed: number }>("DELETE", `/api/v2/runs/${id}`),
  deleteJourney: (id: number) => req<{ runs: number }>("DELETE", `/api/v2/journeys/${id}`),
  clearAllData: (confirm: string) =>
    req<{ sites: number; checks: number; url_lists: number; screenshots_removed: number }>("POST", "/api/v2/workspace/clear-data", { confirm }),
  deleteSite: (sid: number, confirm: string) => req<{ checks: number }>("DELETE", `/api/v2/sites/${sid}`, { confirm }),
  run: (id: number) => req<{ run: Run; recent_visits: any[]; found: { id: number; title: string; severity: Severity; pages: number }[] }>("GET", `/api/v2/runs/${id}`),
  authMode: () => req<{ mode: string; signed_in: boolean | null }>("GET", "/api/v2/auth/mode"),
  login: (email: string, password: string) => req<{ two_factor: boolean }>("POST", "/api/v2/auth/login", { email, password }),
  secondFactor: (code: string) => req<{ ok: boolean; recovery_codes_left: number | null }>("POST", "/api/v2/auth/2fa", { code }),
  forgot: (email: string) => req<{ ok: boolean; mail: boolean }>("POST", "/api/v2/auth/forgot", { email }),
  resetPassword: (token: string, password: string) => req<{ ok: boolean }>("POST", "/api/v2/auth/reset", { token, password }),
  logout: () => req<{ ok: boolean }>("POST", "/api/v2/auth/logout"),
  account: () => req<{ email: string; two_factor: boolean; recovery_codes_left: number }>("GET", "/api/v2/auth/account"),
  changePassword: (current: string, next: string) => req<{ ok: boolean }>("POST", "/api/v2/auth/password", { current, new: next }),
  twoFactorSetup: () => req<{ secret: string; uri: string }>("POST", "/api/v2/auth/2fa/setup"),
  twoFactorEnable: (secret: string, code: string) => req<{ recovery_codes: string[] }>("POST", "/api/v2/auth/2fa/enable", { secret, code }),
  twoFactorDisable: (current: string) => req<{ ok: boolean }>("POST", "/api/v2/auth/2fa/disable", { current, new: "" }),
  checkUrls: (sid: number, text: string, kind: string) =>
    req<SpotStarted>("POST", `/api/v2/sites/${sid}/checks/urls`, { text, kind }),
  checkUpload: (sid: number, file: File, kind: string, wholeSite: boolean) => {
    const f = new FormData();
    f.append("file", file); f.append("kind", kind); f.append("whole_site", String(wholeSite));
    return req<SpotStarted>("POST", `/api/v2/sites/${sid}/checks/upload`, f);
  },
  startRun: (sid: number, kind: string, options: Record<string, unknown> = {}) =>
    req<{ run_id: number }>("POST", `/api/v2/sites/${sid}/runs`, { kind, options }),
  inventory: (sid: number, params: Record<string, string>) =>
    req<Inventory>("GET", `/api/v2/sites/${sid}/inventory?${new URLSearchParams(params)}`),
  inventoryCsv: (sid: number, params: Record<string, string>) => `/api/v2/sites/${sid}/inventory.csv?${new URLSearchParams(params)}`,
  compare: (sid: number, a: number, b: number) => req<Compare>("GET", `/api/v2/sites/${sid}/compare?a=${a}&b=${b}`),
  opportunities: (sid: number, bucket = "", limit = 50, offset = 0, review = "") =>
    req<Opportunities>("GET", `/api/v2/sites/${sid}/growth/opportunities?${new URLSearchParams({ bucket, review, limit: String(limit), offset: String(offset) })}`),
  reviewOpportunity: (sid: number, url: string, status: ReviewStatus, note = "", score?: number) =>
    req<{ id: number }>("POST", `/api/v2/sites/${sid}/growth/opportunities/review`, { url, status, note, score }),
  opportunityHistory: (sid: number, url: string) => req<{ history: { status: string; note: string; score: number | null; created_at: string; by: string | null }[] }>(
    "GET", `/api/v2/sites/${sid}/growth/opportunities/review?${new URLSearchParams({ url })}`),
  searchData: (sid: number) => req<SearchData>("GET", `/api/v2/sites/${sid}/growth/search`),
  searchSync: (sid: number) => req<{ rows: number; ga4_pages: number }>("POST", `/api/v2/sites/${sid}/growth/sync`),
  templates: (sid: number) => req<{ templates: { id: number; name: string; path_pattern: string; page_count: number; visits: number; open_fixes: number }[] }>("GET", `/api/v2/sites/${sid}/templates`),
  pages: (sid: number, q: string) => req<{ pages: { url: string; http_status: number; title: string; template_name: string }[] }>("GET", `/api/v2/sites/${sid}/pages?${new URLSearchParams({ q })}`),
  page: (sid: number, url: string) => req<{ page: any; visit: any; fixes: { id: number; title: string; severity: Severity; status: string }[] }>("GET", `/api/v2/sites/${sid}/page?${new URLSearchParams({ url })}`),
  journeys: (sid: number) => req<{ journeys: Journey[] }>("GET", `/api/v2/sites/${sid}/journeys`),
  saveJourney: (sid: number, yaml: string) => req<{ journey_id: number }>("POST", `/api/v2/sites/${sid}/journeys`, { yaml }),
  journeyRun: (id: number) => req<{ journey_run: any; steps: Step[] }>("GET", `/api/v2/journey-runs/${id}`),
  access: (sid: number) => req<{ header: string; token: string; ua_suffix: string; rules: Record<string, string> }>("GET", `/api/v2/sites/${sid}/access`),
  accessTest: (sid: number) => req<{ ok: boolean; text: string; results: string[] }>("POST", `/api/v2/sites/${sid}/access/test`),
  integrations: () => req<{ integrations: any[]; not_included: { name: string; reason: string }[]; attribution: string }>("GET", "/api/v2/integrations"),
  runIntegration: (id: string, url: string, params: Record<string, unknown> = {}) => req<any>("POST", `/api/v2/integrations/${id}/run`, { url, params }),
  objectUrl: (name: string) => `/api/v2/objects/${encodeURIComponent(name)}`,
  jobs: () => req<{ jobs: Job[] }>("GET", "/api/v2/lists"),
  job: (id: number) => req<Job>("GET", `/api/v2/lists/${id}`),
  newList: (source: "sitemap" | "single" | "list", value: string) =>
    req<Job>("POST", "/api/v2/lists", source === "list" ? { source, text: value } : { source, url: value }),
  uploadList: (files: File[]) => { const f = new FormData(); files.forEach((x) => f.append("file", x)); return req<Job & { skipped_files?: string[] }>("POST", "/api/v2/lists/upload", f); },
  cancelList: (id: number) => req<{ ok: boolean }>("POST", `/api/v2/lists/${id}/cancel`),
  deleteList: (id: number) => req<{ ok: boolean }>("DELETE", `/api/v2/lists/${id}`),
  startList: (id: number, browser: boolean, preset?: string, reuse = true) => req<{ run_id: number }>("POST", `/api/v2/lists/${id}/start`, { browser, preset, reuse }),
  addToList: (id: number, source: "sitemap" | "list", value: string) =>
    req<Job>("POST", `/api/v2/lists/${id}/add`, source === "sitemap" ? { source, url: value } : { source, text: value }),
  addFilesToList: (id: number, files: File[]) => { const f = new FormData(); files.forEach((x) => f.append("file", x)); return req<Job>("POST", `/api/v2/lists/${id}/add-upload`, f); },
  worker: () => req<{ alive: boolean; waiting: number }>("GET", "/api/v2/worker"),
  briefs: (sid: number, url = "") => req<{ briefs: Brief[] }>("GET", `/api/v2/sites/${sid}/briefs?${new URLSearchParams({ url })}`),
  makeBrief: (sid: number, url: string) => req<Brief>("POST", `/api/v2/sites/${sid}/briefs`, { url }),
  editBrief: (id: number, name: string, text: string) => req<Brief>("PUT", `/api/v2/briefs/${id}/sections/${name}`, { text }),
  rewriteBrief: (id: number, name: string) => req<Brief>("POST", `/api/v2/briefs/${id}/sections/${name}/rewrite`),
  deleteBrief: (id: number) => req<{ ok: boolean }>("DELETE", `/api/v2/briefs/${id}`),
  internalTickets: (sid: number, view: string, q = "") =>
    req<InternalTickets>("GET", `/api/v2/sites/${sid}/internal-tickets?view=${view}&q=${encodeURIComponent(q)}`),
  setTicketGrouping: (sid: number, mode: "template_component" | "issue_type") => req<{ mode: string }>("PUT", `/api/v2/sites/${sid}/ticket-grouping`, { mode }),
  saveTicketTemplate: (sid: number, title: string, body: string) => req<TicketTemplate>("PUT", `/api/v2/sites/${sid}/ticket-template`, { title, body }),
  markRaised: (sid: number, tid: number, ref: string) =>
    req<{ ok: boolean; raised_ref: string | null }>("POST", `/api/v2/sites/${sid}/internal-tickets/${tid}/raised`, { ref }),
  opsTicket: (runId: number) => req<OpsTicket>("GET", `/api/v2/runs/${runId}/ops-ticket`),
  anchors: (runId: number) => req<Anchors>("GET", `/api/v2/runs/${runId}/anchors`),
  pageHtml: (runId: number, url: string) => req<string>("GET", `/api/v2/runs/${runId}/html?${new URLSearchParams({ url })}`),
  pauseRun: (id: number) => req<{ status: string }>("POST", `/api/v2/runs/${id}/pause`),
  resumeRun: (id: number) => req<{ status: string }>("POST", `/api/v2/runs/${id}/resume`),
  retryRun: (id: number) => req<{ status: string }>("POST", `/api/v2/runs/${id}/retry`),
  timeline: (id: number) => req<{ events: { kind: string; message: string; data: Record<string, any>; created_at: string }[];
    minutes: { minute: string; pages: number; errors: number; avg_ms: number | null }[] }>("GET", `/api/v2/runs/${id}/timeline`),
  cancelRun: (id: number) => req<{ status: string }>("POST", `/api/v2/runs/${id}/stop`),
  facts: (runId: number, dataset: string, params: Record<string, string | number>) =>
    req<{ total: number; rows: any[]; summary: { rows: number; unique_urls: number; broken: number; redirects: number } }>(
      "GET", `/api/v2/runs/${runId}/facts/${dataset}?${new URLSearchParams(Object.entries(params).map(([k, v]) => [k, String(v)]))}`),
  exploreSchema: (runId: number) => req<ExploreSchema>("GET", `/api/v2/runs/${runId}/explore/schema`),
  explore: (runId: number, sql: string, offset = 0, limit = 100) => req<ExploreResult>("POST", `/api/v2/runs/${runId}/explore`, { sql, offset, limit }),
  mail: () => req<MailSettings>("GET", "/api/v2/mail"),
  saveMail: (b: MailForm) => req<MailSettings>("PUT", "/api/v2/mail", b),
  testMail: (b: MailForm) => req<{ ok: boolean; to: string[] }>("POST", "/api/v2/mail/test", b),
  sendSummary: (runId: number) => req<{ ok: boolean; to: number; subject: string; report_note: string }>("POST", `/api/v2/runs/${runId}/send-summary`),
  runReports: (runId: number) => req<{ reports: RunReport[] }>("GET", `/api/v2/runs/${runId}/reports`),
  buildReport: (runId: number) => req<{ building: boolean }>("POST", `/api/v2/runs/${runId}/reports`),
  queue: (runId: number, state: string, q: string, offset: number, limit = 100) =>
    req<RunQueue>("GET", `/api/v2/runs/${runId}/queue?${new URLSearchParams({ state, q, offset: String(offset), limit: String(limit) })}`),
  retryFailed: (runId: number) => req<{ list_id: number; urls: number }>("POST", `/api/v2/runs/${runId}/queue/retry-failed`),
  factsCsvUrl: (runId: number, dataset: string, filter: string) => `/api/v2/runs/${runId}/export/${dataset}.csv?${new URLSearchParams({ filter })}`,
  presets: (sid: number) => req<{ current: string; rate_rps: number | null; pages: number | null; presets: { id: string; label: string;
    description: string; target_rps: number; max_rps: number; estimate: { minutes: number; pages: number } | null }[] }>("GET", `/api/v2/sites/${sid}/presets`),
  patchSite: (sid: number, b: Record<string, unknown>) => req<Site>("PATCH", `/api/v2/sites/${sid}`, b),
  site: (sid: number) => req<Site>("GET", `/api/v2/sites/${sid}`),
  testAlert: (sid: number) => req<{ sent: { channel: string; ok: boolean; status?: number; error?: string }[] }>("POST", `/api/v2/sites/${sid}/alerts/test`),
  convertRecording: (sid: number, code: string, name: string, device: string) =>
    req<{ yaml: string; skipped: string[]; origin: string }>("POST", `/api/v2/sites/${sid}/journeys/convert`, { code, name, device }),
  createTicket: (fixId: number) => req<{ url: string; key: string }>("POST", `/api/v2/fixes/${fixId}/ticket`),
  syncTickets: (sid: number) => req<{ moved_to_waiting: number }>("POST", `/api/v2/sites/${sid}/tickets/sync`),
};

export interface ExploreSchema { views: { name: string; columns: string[] }[]; tables: { name: string; columns: string[] }[];
  examples: { name: string; sql: string }[]; limits: { timeout_s: number; page_max: number; export_max_rows: number } }
export interface ExploreResult { columns: string[]; rows: (string | number | null)[][]; total: number | null; has_more: boolean;
  offset: number; limit: number; elapsed_ms: number }
export interface MailForm { enabled: boolean; host: string; port: number; security: "starttls" | "ssl" | "none"; username: string;
  password?: string | null; sender: string; recipients: string[]; attach_report: boolean }
export interface MailSettings extends Partial<MailForm> { enabled: boolean; can_edit: boolean; password_set?: boolean;
  secret_key_set?: boolean; max_attach_mb?: number;
  last_result: { ok?: boolean; error?: string; at?: string; run_id?: number; to?: number; report?: string } }

export interface RunReport { id: number; run_id: number; file_name: string; status: "building" | "ready" | "failed" | "replaced";
  size_bytes: number | null; files: number | null; rows: Record<string, number>; seconds: number | null; error: string | null;
  downloads: number; created_at: string | null; finished_at: string | null; download: string | null }

export interface RunQueue { run_status: string; started_at: string | null; finished_at: string | null;
  counts: { QUEUED: number; COMPLETED: number; FAILED: number; EXCLUDED: number; TOTAL: number }; total: number; offset: number; limit: number;
  rows: { id: number; url: string; state: string; attempts: number; status: number | null; ms: number | null; done_at: string | null;
    error: string; sitemap: string; links: number | null; sources: string[] }[] }
