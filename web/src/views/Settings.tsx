import { useEffect, useState } from "react";
import { api, type Site } from "../api";
import { useViewMotion } from "../motion";
import { ErrorBox, useLoad, useToast } from "../ui";

export function SettingsView({ site }: { site: Site; params: URLSearchParams }) {
  const s = useLoad(() => api.site(site.id), [site.id]);
  const pr = useLoad(() => api.presets(site.id), [site.id]);
  const ref = useViewMotion<HTMLDivElement>([!!s.data]);
  const toast = useToast();
  const [form, setForm] = useState<Record<string, any>>({});
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!s.data) return;
    const st = s.data.settings || {};
    setForm({
      name: s.data.name, preset: st.preset ?? "target", rate_rps: st.rate_rps ?? 12, visitors: st.visitors ?? 6, consent: st.consent ?? "accept",
      max_pages: st.max_pages ?? "", read_timeout: st.read_timeout ?? "", max_retries: st.max_retries ?? "",
      gsc_property: st.gsc_property ?? "", ga4_property_id: st.ga4_property_id ?? "",
      include: (st.scope?.include || []).join("\n"), exclude: (st.scope?.exclude || []).join("\n"),
      stg_user: st.staging?.username ?? "", stg_pass: "",
      stg_headers: (st.staging?.header_names || []).map((h: string) => `${h}: •••`).join("\n"),
      check_schedule: st.check_schedule ?? "", sitemap: st.sitemap ?? "",
      slack: st.alerts?.slack_webhook ?? "", teams: st.alerts?.teams_webhook ?? "",
      on: st.alerts?.on ?? ["summary", "regressed", "journey_failed", "access"],
      provider: st.tickets?.provider ?? "", repo: st.tickets?.repo ?? "", base_url: st.tickets?.base_url ?? "",
      project: st.tickets?.project ?? "", email: st.tickets?.email ?? "",
    });
  }, [s.data]);
  const set = (k: string) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setForm({ ...form, [k]: e.target.value });
  const setT = (k: string) => (e: React.ChangeEvent<HTMLTextAreaElement>) => setForm({ ...form, [k]: e.target.value });

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true); setErr(null);
    const tickets = form.provider === "github" ? { provider: "github", repo: form.repo }
      : form.provider === "jira" ? { provider: "jira", base_url: form.base_url, project: form.project, email: form.email } : { provider: "none" };
    try {
      await api.patchSite(site.id, {
        name: form.name, preset: form.preset, rate_rps: Number(form.rate_rps), visitors: Number(form.visitors), consent: form.consent,
        max_pages: form.max_pages === "" ? undefined : Number(form.max_pages),
        read_timeout: form.read_timeout === "" ? undefined : Number(form.read_timeout),
        max_retries: form.max_retries === "" ? undefined : Number(form.max_retries),
        scope: { include: lines(form.include), exclude: lines(form.exclude) },
        gsc_property: form.gsc_property.trim(), ga4_property_id: form.ga4_property_id.trim(),
        staging: stagingBody(form, s.data?.settings?.staging),
        check_schedule: form.check_schedule, sitemap: form.sitemap || undefined,
        alerts: { slack_webhook: form.slack, teams_webhook: form.teams, on: form.on }, tickets,
      });
      toast("Settings saved.");
      s.reload();
    } catch (x: any) { setErr(x.detail || x.title || x.message); } finally { setBusy(false); }
  };
  const testAlert = async () => {
    try {
      const r = await api.testAlert(site.id);
      toast(r.sent.map((x) => `${x.channel === "slack" ? "Slack" : "Teams"}: ${x.ok ? "delivered" : "failed"}`).join(" · "));
    } catch (x: any) { toast(x.detail || x.message); }
  };

  if (s.error) return <ErrorBox text={s.error} onRetry={s.reload} />;
  if (!s.data) return <p className="faint">Loading…</p>;
  const toggle = (k: string) => setForm({ ...form, on: form.on.includes(k) ? form.on.filter((x: string) => x !== k) : [...form.on, k] });
  return (
    <div className="view" ref={ref} style={{ maxWidth: 860 }}>
      <div className="head"><div><h1>Settings</h1><p className="lede">How QC checks {site.primary_host}, when, and who hears about it.</p></div></div>
      <form className="grid" onSubmit={save}>
        <section className="panel grid" data-reveal>
          <h2>Checking</h2>
          <Field id="s-name" label="Name"><input id="s-name" className="input" value={form.name || ""} onChange={set("name")} /></Field>
          <fieldset className="presets">
            <legend><b>Speed</b> <span className="faint" style={{ fontSize: 13 }}>
              {pr.data?.pages ? `Times are for this site's ${pr.data.pages.toLocaleString("en-IN")} pages, from QC V5's measured request model.` : "Times appear after the first check."}</span></legend>
            {(pr.data?.presets || []).map((p) => (
              <label key={p.id} className={`preset ${form.preset === p.id ? "on" : ""}`}>
                <input type="radio" name="preset" value={p.id} checked={form.preset === p.id}
                  onChange={() => setForm({ ...form, preset: p.id, rate_rps: p.target_rps })} />
                <span className="preset-head"><b>{p.label}</b><span className="mono">{p.target_rps} req/s</span></span>
                <span className="faint">{p.description}</span>
                {p.estimate && <span className="preset-time num">{p.estimate.minutes < 1 ? "under a minute" : p.estimate.minutes < 90
                  ? `about ${Math.round(p.estimate.minutes)} min` : `about ${(p.estimate.minutes / 60).toFixed(1)} h`}</span>}
              </label>
            ))}
          </fieldset>
          <Field id="s-rate" label="Speed limit" hint="Requests per second to this site, shared by every check. A number you type here is never exceeded. Ask the site's operators what the CDN allows.">
            <input id="s-rate" className="input" type="number" min={0.5} max={200} step={0.5} value={form.rate_rps ?? ""} onChange={set("rate_rps")} /></Field>
          <Field id="s-vis" label="Visitors at a time" hint="People-like browser visits in parallel (1–20). Each reads one page at a time.">
            <input id="s-vis" className="input" type="number" min={1} max={20} value={form.visitors ?? ""} onChange={set("visitors")} /></Field>
          <Field id="s-consent" label="Cookie banner" hint="What visitors answer when a site asks about cookies.">
            <select id="s-consent" className="input" value={form.consent} onChange={set("consent")}><option value="accept">Accept</option><option value="reject">Reject</option></select></Field>
          <Field id="s-sched" label="Check automatically" hint='For example "daily 06:30", "weekdays 07:15", "mondays 09:00", "every 6 hours". Leave empty for manual checks only.'>
            <input id="s-sched" className="input" placeholder="daily 06:30" value={form.check_schedule || ""} onChange={set("check_schedule")} /></Field>
          <Field id="s-sitemap" label="Sitemap" hint="Leave empty to find it from robots.txt.">
            <input id="s-sitemap" className="input" placeholder={`${site.base_url}/sitemap.xml`} value={form.sitemap || ""} onChange={set("sitemap")} /></Field>
          <details>
            <summary style={{ cursor: "pointer" }}>More: page limit, timeout, retries</summary>
            <div className="grid g-3" style={{ marginTop: 12 }}>
              <Field id="s-max" label="Most pages per check" hint="Empty: 300,000.">
                <input id="s-max" className="input" type="number" min={1} max={1000000} value={form.max_pages} onChange={set("max_pages")} /></Field>
              <Field id="s-rto" label="Wait for a page (seconds)" hint="Empty: 15, as in V5.">
                <input id="s-rto" className="input" type="number" min={2} max={120} value={form.read_timeout} onChange={set("read_timeout")} /></Field>
              <Field id="s-ret" label="Retries" hint="Empty: 1, as in V5.">
                <input id="s-ret" className="input" type="number" min={0} max={5} value={form.max_retries} onChange={set("max_retries")} /></Field>
            </div>
            <p className="faint" style={{ fontSize: 13, margin: "10px 0 0" }}>Moving from QC V5? <span className="mono">python -m qc6.admin import-v5-config {site.id} path/to/.env</span> copies these from your V5 settings.</p>
          </details>
        </section>
        <section className="panel grid" data-reveal>
          <h2>What to check, and how to get in</h2>
          <div className="grid g-2">
            <Field id="s-inc" label="Only these pages" hint="One pattern per line, e.g. /loans/*. Empty: every page in the sitemap.">
              <textarea id="s-inc" className="input" rows={3} value={form.include} onChange={setT("include")} placeholder="/loans/*" /></Field>
            <Field id="s-exc" label="Never these pages" hint="One per line, e.g. *?sort=* or /archive/*.">
              <textarea id="s-exc" className="input" rows={3} value={form.exclude} onChange={setT("exclude")} placeholder="*?sort=*" /></Field>
          </div>
          <p className="faint" style={{ fontSize: 13, margin: 0 }}>For a pre-production site behind a login. QC sends these only to {site.primary_host} and its
            own subdomains, never to other websites, and keeps them encrypted.</p>
          <div className="grid g-2">
            <Field id="s-su" label="Login name"><input id="s-su" className="input" autoComplete="off" value={form.stg_user} onChange={set("stg_user")} /></Field>
            <Field id="s-sp" label="Password" hint={s.data?.settings?.staging?.has_password ? "Saved. Type a new one to change it." : undefined}>
              <input id="s-sp" className="input" type="password" autoComplete="new-password" value={form.stg_pass} onChange={set("stg_pass")} /></Field>
          </div>
          <Field id="s-sh" label="Extra headers" hint="One per line as Name: value. Saved values show as •••; leave them to keep them, delete the line to remove one.">
            <textarea id="s-sh" className="input" rows={2} value={form.stg_headers} onChange={setT("stg_headers")} placeholder="X-Env: staging" /></Field>
          {s.data?.settings?.staging && <div><button type="button" className="btn quiet" onClick={async () => {
            await api.patchSite(site.id, { staging: { clear: true } }); toast("Staging login removed."); s.reload(); }}>Remove the staging login</button></div>}
        </section>
        <section className="panel grid" data-reveal>
          <h2>Search Console and GA4</h2>
          <p className="faint" style={{ fontSize: 13, margin: 0 }}>Free. QC reads your own numbers with a Google service account; see Growth, Search queries for the steps.</p>
          <div className="grid g-2">
            <Field id="s-gsc" label="Search Console property" hint={`For example sc-domain:${site.primary_host.replace(/^www\./, "")}`}>
              <input id="s-gsc" className="input" value={form.gsc_property} onChange={set("gsc_property")} /></Field>
            <Field id="s-ga4" label="GA4 property id" hint="Optional. A number, from GA4 Admin, Property details.">
              <input id="s-ga4" className="input" inputMode="numeric" value={form.ga4_property_id} onChange={set("ga4_property_id")} /></Field>
          </div>
        </section>
        <section className="panel grid" data-reveal>
          <h2>Alerts</h2>
          <Field id="s-slack" label="Slack incoming webhook"><input id="s-slack" className="input" placeholder="https://hooks.slack.com/services/…" value={form.slack || ""} onChange={set("slack")} /></Field>
          <Field id="s-teams" label="Microsoft Teams incoming webhook"><input id="s-teams" className="input" placeholder="https://….webhook.office.com/…" value={form.teams || ""} onChange={set("teams")} /></Field>
          <fieldset className="row" style={{ border: 0, padding: 0, margin: 0 }}>
            <legend className="section-label">Send a message when</legend>
            {[["summary", "a check finishes"], ["regressed", "a fixed problem comes back"], ["journey_failed", "a journey fails"], ["access", "the CDN blocks QC"]].map(([k, l]) => (
              <label key={k} className="row" style={{ gap: 6 }}><input type="checkbox" checked={form.on?.includes(k) || false} onChange={() => toggle(k)} />{l}</label>
            ))}
          </fieldset>
          <div><button type="button" className="btn" onClick={testAlert}>Send a test message</button></div>
        </section>
        <section className="panel grid" data-reveal>
          <h2>Tickets</h2>
          <Field id="s-prov" label="Create tickets in"><select id="s-prov" className="input" value={form.provider} onChange={set("provider")}>
            <option value="">Nowhere</option><option value="github">GitHub Issues</option><option value="jira">Jira Cloud</option></select></Field>
          {form.provider === "github" && <Field id="s-repo" label="Repository" hint="owner/name. The token goes in QC6_GITHUB_TOKEN on the server."><input id="s-repo" className="input" value={form.repo || ""} onChange={set("repo")} /></Field>}
          {form.provider === "jira" && <>
            <Field id="s-jurl" label="Jira address"><input id="s-jurl" className="input" placeholder="https://acme.atlassian.net" value={form.base_url || ""} onChange={set("base_url")} /></Field>
            <Field id="s-jproj" label="Project key"><input id="s-jproj" className="input" placeholder="WEB" value={form.project || ""} onChange={set("project")} /></Field>
            <Field id="s-jmail" label="Account e-mail" hint="The API token goes in QC6_JIRA_TOKEN on the server."><input id="s-jmail" className="input" value={form.email || ""} onChange={set("email")} /></Field>
          </>}
        </section>
        {err && <ErrorBox text={err} />}
        <div><button className="btn primary" type="submit" disabled={busy}>Save settings</button></div>
      </form>
    </div>
  );
}

function lines(v: string): string[] { return (v || "").split("\n").map((x) => x.trim()).filter(Boolean); }

function stagingBody(form: Record<string, any>, current: any) {
  const headers: Record<string, string> = {};
  for (const l of lines(form.stg_headers)) {
    const i = l.indexOf(":");
    if (i > 0) headers[l.slice(0, i).trim()] = l.slice(i + 1).trim();
  }
  for (const h of current?.header_names || []) if (!(h in headers)) headers[h] = "";     // line deleted: remove it
  if (!form.stg_user && !form.stg_pass && !Object.keys(headers).length && !current) return undefined;
  return { username: form.stg_user, password: form.stg_pass, headers };
}

function Field({ id, label, hint, children }: { id: string; label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="grid" style={{ gap: 4 }}>
      <label htmlFor={id}><b>{label}</b></label>
      {children}
      {hint && <span className="faint" style={{ fontSize: 13 }}>{hint}</span>}
    </div>
  );
}
