import { useEffect, useState } from "react";
import { api, type InternalTicket, type InternalTickets, type Site } from "../api";
import { useViewMotion } from "../motion";
import { ErrorBox, Icon, Sev, count, day, useLoad, useToast } from "../ui";

/* Tickets: every issue QC finds is a ticket (QC-0001...), written from the site's ticket template, ready to paste into
   the Ops team's tool. Mark it raised with the number it got there; QC closes it when a check confirms the fix. */

const VIEWS: [string, string][] = [["to_raise", "To raise"], ["raised", "Raised"], ["closed", "Closed"], ["all", "All"]];
const STATE_TEXT: Record<InternalTicket["state"], string> = { to_raise: "To raise", raised: "Raised", closed: "Closed" };

async function copyText(text: string, what: string, toast: (m: string) => void) {
  try { await navigator.clipboard.writeText(text); toast(`${what} copied.`); }
  catch { toast("Couldn't copy: select the text and press Ctrl+C."); }
}

export function TicketsView({ site, params }: { site: Site; params: URLSearchParams }) {
  const [view, setView] = useState(() => (VIEWS.some(([k]) => k === params.get("view")) ? params.get("view")! : "to_raise"));
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const [openId, setOpenId] = useState<number | null>(() => Number(params.get("t")) || null);
  const [editing, setEditing] = useState(false);
  const list = useLoad(() => api.internalTickets(site.id, view, query), [site.id, view, query]);
  const ref = useViewMotion<HTMLDivElement>([]);
  useEffect(() => { const t = window.setTimeout(() => setQuery(q.trim()), 300); return () => window.clearTimeout(t); }, [q]);
  const d = list.data;

  return (
    <div className="view" ref={ref}>
      <div className="head">
        <div>
          <h1>Tickets</h1>
          <p className="lede">Every issue QC finds is a ticket here, written from your ticket template. Copy it into the Ops team's tool,
            then mark it raised with the number it got. QC closes it when a check confirms the fix.</p>
        </div>
        <div className="row">
          <a className="btn" href={`/api/v2/sites/${site.id}/internal-tickets.csv?view=${view}`} download><Icon name="page" />Download as sheet</a>
          <button className="btn" aria-expanded={editing} onClick={() => setEditing(!editing)}><Icon name="settings" />Ticket template</button>
        </div>
      </div>

      {editing && d && <TemplateEditor siteId={site.id} data={d} onSaved={() => { list.reload(); }} onClose={() => setEditing(false)} />}

      <div className="toolbar" data-reveal>
        <div className="seg" role="group" aria-label="Show">
          {VIEWS.map(([k, label]) => (
            <button key={k} aria-pressed={view === k} onClick={() => { setView(k); setOpenId(null); }}>
              {label}{d ? ` · ${count(d.counts[k] || 0)}` : ""}
            </button>
          ))}
        </div>
        <label className="sr-only" htmlFor="ticket-q">Find a ticket</label>
        <input id="ticket-q" className="search" placeholder="Find a ticket: QC-0042, a word, an Ops number" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>

      {list.error ? <ErrorBox text={list.error} onRetry={list.reload} />
        : !d ? <p className="faint">Loading…</p>
        : d.tickets.length === 0 ? <p className="good-note">{view === "to_raise" ? "Nothing waiting to be raised." : "No tickets here."}</p>
        : (
          <ul className="ticket-list">
            {d.tickets.map((t) => (
              <TicketRow key={t.id} t={t} open={openId === t.id} onToggle={() => setOpenId(openId === t.id ? null : t.id)} onChanged={list.reload} />
            ))}
          </ul>
        )}
      {d && d.tickets.length >= 200 && <p className="faint">Showing the first 200. Download as sheet has all of them.</p>}
    </div>
  );
}

function TicketRow({ t, open, onToggle, onChanged }: { t: InternalTicket; open: boolean; onToggle: () => void; onChanged: () => void }) {
  const toast = useToast();
  const [refText, setRefText] = useState(t.raised_ref || "");
  const [busy, setBusy] = useState(false);
  const save = async (value: string) => {
    setBusy(true);
    try { await api.markRaised(t.fix_id, value); toast(value ? `${t.ticket_id} marked raised as ${value}.` : `${t.ticket_id} is back in To raise.`); onChanged(); }
    catch (e: any) { toast(e.detail || e.title || e.message); }
    finally { setBusy(false); }
  };
  const panel = `tk-${t.id}`;
  return (
    <li className={`ticket-row ${open ? "open" : ""}`}>
      <button className="ticket-head" aria-expanded={open} aria-controls={panel} onClick={onToggle}>
        <span className="mono ticket-id">{t.ticket_id}</span>
        <Sev s={t.severity} />
        <span className="ticket-title">{t.issue}{t.address && <small className="mono">{t.address}</small>}</span>
        <span className="faint hide-phone">{t.type} · {count(t.pages)} pages</span>
        <span className={`pill ${t.state === "closed" ? "good" : t.state === "raised" ? "new" : "warn"}`}>
          {STATE_TEXT[t.state]}{t.raised_ref ? `: ${t.raised_ref}` : ""}</span>
      </button>
      {open && (
        <div className="ticket-body ticket" id={panel}>
          <p className="faint" style={{ margin: 0 }}>{t.priority} · {t.type} · {count(t.pages)} pages · in QC: {t.qc_status_text}
            {t.regressed ? " · came back after a fix" : ""}{t.raised_at ? ` · raised ${day(t.raised_at)}` : ""}</p>
          <label className="ticket-label" htmlFor={`${panel}-title`}>Title</label>
          <div className="ticket-title"><input id={`${panel}-title`} readOnly value={t.title} onFocus={(e) => e.target.select()} />
            <button className="btn" onClick={() => copyText(t.title, "Title", toast)}>Copy title</button></div>
          <label className="ticket-label" htmlFor={`${panel}-text`}>Ticket</label>
          <textarea id={`${panel}-text`} readOnly rows={16} value={t.text} onFocus={(e) => e.target.select()} />
          <div className="row" style={{ flexWrap: "wrap" }}>
            <button className="btn primary" onClick={() => copyText(`${t.title}\n\n${t.text}`, "Ticket", toast)}><Icon name="page" />Copy the whole ticket</button>
            {t.state !== "closed" && (
              <form className="raise-form" onSubmit={(e) => { e.preventDefault(); save(refText.trim()); }}>
                <label htmlFor={`${panel}-ref`}>Ops ticket number</label>
                <input id={`${panel}-ref`} value={refText} maxLength={120} placeholder="e.g. OPS-1234" onChange={(e) => setRefText(e.target.value)} />
                <button className="btn" disabled={busy || !refText.trim() || refText.trim() === (t.raised_ref || "")}>Mark raised</button>
                {t.raised_ref && <button type="button" className="btn quiet" disabled={busy} onClick={() => { setRefText(""); save(""); }}>Not raised</button>}
              </form>
            )}
          </div>
        </div>
      )}
    </li>
  );
}

function TemplateEditor({ siteId, data, onSaved, onClose }: { siteId: number; data: InternalTickets; onSaved: () => void; onClose: () => void }) {
  const toast = useToast();
  const [title, setTitle] = useState(data.template.title);
  const [body, setBody] = useState(data.template.body);
  const [busy, setBusy] = useState(false);
  const save = async (t: string, b: string, msg: string) => {
    setBusy(true);
    try { const r = await api.saveTicketTemplate(siteId, t, b); setTitle(r.title); setBody(r.body); toast(msg); onSaved(); }
    catch (e: any) { toast(e.detail || e.title || e.message); }
    finally { setBusy(false); }
  };
  return (
    <section className="panel ticket template-editor" aria-labelledby="tpl-h" style={{ marginBottom: 16 }}>
      <div className="panel-head"><h2 id="tpl-h">Ticket template</h2>
        <span className="faint">{data.template.is_default ? "QC's template" : "Your template"} · used for every ticket of this site</span></div>
      <label className="ticket-label" htmlFor="tpl-title">Title</label>
      <input id="tpl-title" value={title} maxLength={300} onChange={(e) => setTitle(e.target.value)} />
      <label className="ticket-label" htmlFor="tpl-body">Ticket text</label>
      <textarea id="tpl-body" rows={18} value={body} onChange={(e) => setBody(e.target.value)} />
      <details className="placeholders"><summary>What you can put in {"{braces}"}</summary>
        <dl>{data.placeholders.map((p) => <div key={p.name}><dt className="mono">{`{${p.name}}`}</dt><dd>{p.means}</dd></div>)}</dl>
      </details>
      <div className="row" style={{ flexWrap: "wrap" }}>
        <button className="btn primary" disabled={busy} onClick={() => save(title, body, "Template saved. Every ticket now uses it.")}>Save template</button>
        {!data.template.is_default && <button className="btn" disabled={busy} onClick={() => save("", "", "Back to QC's template.")}>Use QC's template</button>}
        <button className="btn quiet" onClick={onClose}>Close</button>
      </div>
    </section>
  );
}
