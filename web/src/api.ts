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

export function localStorageGet(k: string): string | null {
  try { return window.localStorage.getItem(k); } catch { return null; }
}
export function localStorageSet(k: string, v: string): void {
  try { window.localStorage.setItem(k, v); } catch { /* storage blocked */ }
}

export interface Job { id: number; site_id: number; source: "sitemap" | "single" | "sheet" | "list"; label: string;
  status: "extracting" | "ready" | "failed" | "cancelled"; url_count: number; error: string | null; primary_host: string;
  progress: { sitemaps?: number; urls?: number; duplicates?: number; rejected?: number; errors?: number };
  note: { duplicates?: number; other_hosts?: number; outside_scope?: number; capped?: boolean };
  created_at: string; updated_at: string; sample?: string[];
  run_id: number | null; run_status: string | null; run_kind: string | null; run_error: string | null;
  run_progress: { stage?: string; pages_done?: number; pages_total?: number; visits_done?: number; visits_total?: number; stages_done?: string[] } | null;
  run_summary: { pages?: number; score?: number | null; visited?: number } | null; run_started: string | null; run_finished: string | null;
  results?: Partial<Record<Severity, { problems: number; pages: number }>> }

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
  clicks: number | null; impressions: number | null; sessions: number | null; depth: number | null; authority: number | null }
export interface Opportunities { run_id: number; total: number; pages: Opportunity[]; buckets: { id: string; label: string; pages: number }[];
  data: { search_console: boolean; ga4: boolean; links: boolean }; confidence: number; basis: string }
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
  fixes: (sid: number, view: string, q: string) =>
    req<{ fixes: Fix[]; counts: Record<string, number> }>("GET", `/api/v2/sites/${sid}/fixes?${new URLSearchParams({ view, q })}`),
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
  opportunities: (sid: number, bucket = "", limit = 50, offset = 0) =>
    req<Opportunities>("GET", `/api/v2/sites/${sid}/growth/opportunities?${new URLSearchParams({ bucket, limit: String(limit), offset: String(offset) })}`),
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
  startList: (id: number, browser: boolean) => req<{ run_id: number }>("POST", `/api/v2/lists/${id}/start`, { browser }),
  worker: () => req<{ alive: boolean; waiting: number }>("GET", "/api/v2/worker"),
  pauseRun: (id: number) => req<{ status: string }>("POST", `/api/v2/runs/${id}/pause`),
  resumeRun: (id: number) => req<{ status: string }>("POST", `/api/v2/runs/${id}/resume`),
  retryRun: (id: number) => req<{ status: string }>("POST", `/api/v2/runs/${id}/retry`),
  timeline: (id: number) => req<{ events: { kind: string; message: string; data: Record<string, any>; created_at: string }[];
    minutes: { minute: string; pages: number; errors: number; avg_ms: number | null }[] }>("GET", `/api/v2/runs/${id}/timeline`),
  cancelRun: (id: number) => req<{ status: string }>("POST", `/api/v2/runs/${id}/stop`),
  facts: (runId: number, dataset: string, params: Record<string, string | number>) =>
    req<{ total: number; rows: any[]; summary: { rows: number; unique_urls: number; broken: number; redirects: number } }>(
      "GET", `/api/v2/runs/${runId}/facts/${dataset}?${new URLSearchParams(Object.entries(params).map(([k, v]) => [k, String(v)]))}`),
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
