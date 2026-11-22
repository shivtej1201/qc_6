import { useState } from "react";
import { api, type Site } from "../api";
import { go } from "../main";
import { useViewMotion } from "../motion";
import { ErrorBox, ago, count, useLoad, useToast } from "../ui";

/* Growth: pages worth working on first (opportunity score) and what Search Console says about queries. */

export function GrowthView({ site, params }: { site: Site; params: URLSearchParams }) {
  const [tab, setTab] = useState(params.get("tab") === "search" ? "search" : "opps");
  const ref = useViewMotion<HTMLDivElement>([tab]);
  return (
    <div className="view" ref={ref}>
      <div className="head">
        <div>
          <h1>Growth</h1>
          <p className="lede">Which pages are worth working on first, and why. Uses the latest check, plus Search Console and GA4 when they're connected.</p>
        </div>
        <div className="seg" role="group" aria-label="Show">
          <button aria-pressed={tab === "opps"} onClick={() => setTab("opps")}>Opportunities</button>
          <button aria-pressed={tab === "search"} onClick={() => setTab("search")}>Search queries</button>
        </div>
      </div>
      {tab === "opps" ? <Opps site={site} /> : <Search site={site} />}
    </div>
  );
}

function Opps({ site }: { site: Site }) {
  const [bucket, setBucket] = useState("");
  const [offset, setOffset] = useState(0);
  const d = useLoad(() => api.opportunities(site.id, bucket, 50, offset), [site.id, bucket, offset]);
  if (d.error) return <ErrorBox text={d.error} onRetry={d.reload} />;
  const x = d.data;
  if (!x) return <p className="faint">Loading…</p>;
  const missing = [!x.data.search_console && "Search Console", !x.data.ga4 && "GA4"].filter(Boolean);
  return (
    <>
      {missing.length > 0 && (
        <div className="panel" data-reveal style={{ marginBottom: 16 }}>
          <p style={{ margin: 0 }}>Ranked on {Math.round(x.confidence * 100)}% of the model: {missing.join(" and ")} {missing.length > 1 ? "aren't" : "isn't"} connected,
            so search demand{x.data.ga4 ? "" : " and visits"} aren't counted yet. </p>
          <GoogleIds site={site} onSaved={() => window.location.reload()} />
        </div>
      )}
      <div className="row" style={{ marginBottom: 14 }} data-reveal>
        <button className={`btn ${bucket === "" ? "primary" : ""}`} onClick={() => { setBucket(""); setOffset(0); }}>All</button>
        {x.buckets.filter((b) => b.pages > 0).map((b) => (
          <button key={b.id} className={`btn ${bucket === b.id ? "primary" : ""}`} onClick={() => { setBucket(b.id); setOffset(0); }}>
            {b.label} <span className="num">{count(b.pages)}</span></button>
        ))}
      </div>
      <div className="table-wrap" tabIndex={0} role="region" aria-label="Opportunities" data-reveal>
        <table>
          <thead><tr><th className="num">Score</th><th>Page</th><th>What to do</th><th>Why</th>
            {x.data.search_console && <th className="num">Clicks</th>}{x.data.search_console && <th className="num">Impressions</th>}
            {x.data.ga4 && <th className="num">Visits</th>}</tr></thead>
          <tbody>
            {x.pages.length === 0 && <tr><td colSpan={7} className="empty">No pages in this group.</td></tr>}
            {x.pages.map((p) => (
              <tr key={p.url} className="click" tabIndex={0} onClick={() => go("page", { url: p.url })} onKeyDown={(e) => e.key === "Enter" && go("page", { url: p.url })}>
                <td className="num"><b className="score-cell">{p.score.toFixed(0)}</b></td>
                <td className="title"><span className="mono">{path(p.url)}</span>{p.template && <span className="sub">{p.template}</span>}</td>
                <td><span className="chip">{x.buckets.find((b) => b.id === p.bucket)?.label}</span></td>
                <td className="reasons-cell">{p.reasons.length ? <ul>{p.reasons.map((r) => <li key={r}>{r}</li>)}</ul> : <span className="faint">Nothing stands out.</span>}</td>
                {x.data.search_console && <td className="num">{count(p.clicks)}</td>}
                {x.data.search_console && <td className="num">{count(p.impressions)}</td>}
                {x.data.ga4 && <td className="num">{count(p.sessions)}</td>}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="row" style={{ justifyContent: "space-between", marginTop: 12 }}>
        <span className="basis" style={{ margin: 0 }}>{x.basis}</span>
        {x.total > 50 && <span className="row">
          <button className="btn" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}>Previous</button>
          <span className="dim">{count(offset + 1)}–{count(Math.min(offset + 50, x.total))} of {count(x.total)}</span>
          <button className="btn" disabled={offset + 50 >= x.total} onClick={() => setOffset(offset + 50)}>Next</button></span>}
      </div>
    </>
  );
}

function Search({ site }: { site: Site }) {
  const d = useLoad(() => api.searchData(site.id), [site.id]);
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  if (d.error) return <ErrorBox text={d.error} onRetry={d.reload} />;
  const x = d.data;
  if (!x) return <p className="faint">Loading…</p>;
  const sync = async () => {
    setBusy(true);
    try { const r = await api.searchSync(site.id); toast(`Synced ${count(r.rows)} query rows${r.ga4_pages ? ` and ${count(r.ga4_pages)} GA4 pages` : ""}.`); d.reload(); }
    catch (e: any) { toast(e.detail || e.title || e.message); } finally { setBusy(false); }
  };
  if (!x.config.ready) return (
    <div className="panel" data-reveal>
      <h2>Connect Search Console</h2>
      <ol className="steps">
        <li>In Google Cloud, create a service account and enable the Search Console API (and the Analytics Data API for GA4). It's free.</li>
        <li>In Search Console, add the service account's e-mail as a user of your property (and in GA4 as a viewer).</li>
        <li>On the QC server, set <span className="mono">GOOGLE_SERVICE_ACCOUNT_FILE</span> to the key file. {x.config.service_account
          ? <b>Done.</b> : x.config.service_account_error ? <b>That file can't be used: {x.config.service_account_error}</b> : <b>Not set yet.</b>}</li>
        <li>Enter the property below, for example <span className="mono">sc-domain:{site.primary_host.replace(/^www\./, "")}</span>. {x.config.gsc_property ? <b>Done.</b> : <b>Not set yet.</b>}</li>
      </ol>
      <GoogleIds site={site} onSaved={() => window.location.reload()} />
    </div>
  );
  const s = x.last_sync;
  return (
    <>
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 14 }} data-reveal>
        <span className="dim">{s && s.synced_at ? `Last 28 days to ${s.end_date}, synced ${ago(s.synced_at)}: ${count(s.gsc_rows)} query rows${s.truncated ? " (the first 200,000)" : ""}.` : "Not synced yet."}
          {s?.error && <> <b style={{ color: "var(--critical)" }}>Last sync failed: {s.error}</b></>}</span>
        <button className="btn primary" disabled={busy} onClick={sync}>{busy ? "Syncing…" : "Sync now"}</button>
      </div>
      <div className="panel" data-reveal style={{ marginBottom: 16 }}>
        <div className="panel-head"><h2>Close to page one</h2>
          <span className="faint" style={{ fontSize: 13 }}>Position 4–15, at least {count(x.thresholds.striking_min_impressions)} impressions</span></div>
        {x.striking.length === 0 ? <p className="empty">No query qualifies yet.</p> : (
          <div className="table-wrap flat" tabIndex={0} role="region" aria-label="Queries close to page one">
            <table><thead><tr><th>Query</th><th>Page</th><th className="num">Position</th><th className="num">Impressions</th><th className="num">Clicks</th>
              <th className="num">Clicks at position 3</th></tr></thead>
              <tbody>{x.striking.map((r) => <tr key={r.query + r.page}><td className="title nowrap">{r.query}</td>
                <td><a className="mono" href={`#/page?url=${encodeURIComponent(r.page)}`}>{path(r.page)}</a></td>
                <td className="num">{r.position.toFixed(1)}</td><td className="num">{count(r.impressions)}</td><td className="num">{count(r.clicks)}</td>
                <td className="num">about {count(r.clicks_at_position_3)}</td></tr>)}</tbody></table>
          </div>)}
        <p className="basis">"Clicks at position 3" uses the usual click-through rate for that position (about 11%): the size of the prize, not a promise.</p>
      </div>
      <div className="panel" data-reveal>
        <div className="panel-head"><h2>Pages competing for the same query</h2>
          <span className="faint" style={{ fontSize: 13 }}>Two or more pages in the top 20, at least {count(x.thresholds.cannibal_min_impressions)} impressions</span></div>
        {x.cannibalisation.length === 0 ? <p className="empty">None found. Each query has one clear page.</p> : (
          <ul className="cannibal">{x.cannibalisation.map((q) => (
            <li key={q.query}><b>{q.query}</b> <span className="faint">{count(q.impressions)} impressions, {q.pages_top20} pages</span>
              <ul>{q.pages.map((p) => <li key={p.page}><a className="mono" href={`#/page?url=${encodeURIComponent(p.page)}`}>{path(p.page)}</a>
                <span className="num"> position {p.position}, {count(p.clicks)} clicks</span></li>)}</ul></li>))}
          </ul>)}
        <p className="basis">Pick one page to rank for the query, make the others link to it, and change their titles to target something else.</p>
      </div>
    </>
  );
}

function path(u: string) {
  try { const x = new URL(u); return x.pathname + x.search; } catch { return u; }
}


/* The two Google ids Growth needs, right where they're used (there is no settings screen). */
function GoogleIds({ site, onSaved }: { site: Site; onSaved: () => void }) {
  const st: any = site.settings || {};
  const [gsc, setGsc] = useState<string>(st.gsc_property || "");
  const [ga4, setGa4] = useState<string>(st.ga4_property_id || "");
  const [msg, setMsg] = useState<string | null>(null);
  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    try { await api.patchSite(site.id, { gsc_property: gsc.trim(), ga4_property_id: ga4.trim() }); setMsg("Saved."); onSaved(); }
    catch (x: any) { setMsg(x.detail || x.title || x.message); }
  };
  return (
    <form className="row google-ids" onSubmit={save} style={{ flexWrap: "wrap", marginTop: 10 }}>
      <label className="grid" style={{ gap: 4 }}>Search Console property
        <input className="input" value={gsc} onChange={(e) => setGsc(e.target.value)} placeholder={`sc-domain:${site.primary_host.replace(/^www\./, "")}`} /></label>
      <label className="grid" style={{ gap: 4 }}>GA4 property id (optional)
        <input className="input" inputMode="numeric" value={ga4} onChange={(e) => setGa4(e.target.value)} placeholder="123456789" /></label>
      <button className="btn" type="submit" style={{ alignSelf: "end" }}>Save</button>
      {msg && <span className="faint" role="status" style={{ alignSelf: "end" }}>{msg}</span>}
    </form>
  );
}
