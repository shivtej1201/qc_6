import { useEffect, useRef, useState } from "react";
import { api, type AIDraft, type AIText, type Fix, type FixImpact, type FixStatus, type Member, type Site } from "../api";
import { go } from "../main";
import { flash, slideIn, useViewMotion } from "../motion";
import { Avatar, ErrorBox, Sev, count, day, statusPill, useLoad, useToast } from "../ui";

const VIEWS: [string, string][] = [["needs", "Needs you"], ["verify", "Waiting for check"], ["verified", "Verified"], ["all", "All"]];

export function FixesView({ site, params }: { site: Site; params: URLSearchParams }) {
  const [view, setView] = useState(() => (VIEWS.some(([k]) => k === params.get("view")) ? params.get("view")! : "needs"));
  const [layout, setLayout] = useState<"list" | "board">(() => (params.get("layout") === "board" ? "board" : "list"));
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const list = useLoad(() => api.fixes(site.id, layout === "board" ? "all" : view, query, layout === "board" ? 500 : 100),
    [site.id, view, query, layout]);
  const members = useLoad(() => api.members(), []);
  const openId = Number(params.get("fix")) || null;
  const ref = useViewMotion<HTMLDivElement>([]);
  useEffect(() => { const t = window.setTimeout(() => setQuery(q.trim()), 300); return () => window.clearTimeout(t); }, [q]);

  return (
    <div className="view" ref={ref}>
      <div className="head">
        <div>
          <h1>Fixes</h1>
          <p className="lede">Each row is one thing to fix, however many pages it touches. Worst first.</p>
        </div>
      </div>
      <div className="toolbar" data-reveal>
        <div className="seg" role="group" aria-label="Layout">
          <button aria-pressed={layout === "list"} onClick={() => setLayout("list")}>List</button>
          <button aria-pressed={layout === "board"} onClick={() => setLayout("board")}>Board</button>
        </div>
        {layout === "list" && <div className="seg" role="group" aria-label="Show">
          {VIEWS.map(([k, label]) => (
            <button key={k} aria-pressed={view === k} onClick={() => setView(k)}>
              {label}{list.data ? ` · ${count(list.data.counts[k])}` : ""}
            </button>
          ))}
        </div>}
        <label className="sr-only" htmlFor="fix-q">Find a fix</label>
        <input id="fix-q" className="search" type="search" placeholder="Find a fix or page type" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      {layout === "board" ? (list.error ? <ErrorBox text={list.error} onRetry={list.reload} />
        : <Board fixes={list.data?.fixes || null} onMoved={list.reload} />) : list.error ? <ErrorBox text={list.error} onRetry={list.reload} /> : (
        <div className="table-wrap" data-reveal>
          <table>
            <thead><tr><th>Severity</th><th>What's wrong</th><th>Page type</th><th className="num">Pages</th><th>Owner</th><th>Status</th></tr></thead>
            <tbody>
              {!list.data && <tr><td colSpan={6} className="faint">Loading…</td></tr>}
              {list.data && list.data.fixes.length === 0 && (
                <tr><td colSpan={6} className="empty">{view === "needs" ? "Nothing needs you here. We'll keep checking." : "Nothing in this list."}</td></tr>
              )}
              {list.data?.fixes.map((f) => (
                <tr key={f.id} className="click" tabIndex={0} data-fix={f.id}
                  onClick={() => go("fixes", { fix: f.id })} onKeyDown={(e) => { if (e.key === "Enter") go("fixes", { fix: f.id }); }}>
                  <td><Sev s={f.severity} /></td>
                  <td className="title">{f.title}<span className="sub">{f.texts.why}</span></td>
                  <td>{f.template_name ? <span className="chip">{f.template_name}</span> : <span className="faint">Site-wide</span>}</td>
                  <td className="num">{count(f.pages_affected)}</td>
                  <td><Avatar name={f.owner_name} /></td>
                  <td>{statusPill(f)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {openId && <FixDrawer tickets={!!site.settings?.tickets?.provider} id={openId} members={members.data?.members || []} onChanged={list.reload} />}
    </div>
  );
}

function FixDrawer({ id, members, onChanged, tickets }: { id: number; members: Member[]; onChanged: () => void; tickets: boolean }) {
  const d = useLoad(() => api.fix(id), [id]);
  const panel = useRef<HTMLElement>(null);
  const back = useRef<HTMLDivElement>(null);
  const closeBtn = useRef<HTMLButtonElement>(null);
  const toast = useToast();
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);
  const close = () => go("fixes");

  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    slideIn(panel.current, back.current);
    closeBtn.current?.focus();
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") close(); };
    document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("keydown", esc); prev?.focus?.(); };
  }, []);

  const change = async (body: Record<string, unknown>, msg: string) => {
    setBusy(true);
    try {
      await api.patchFix(id, body);
      toast(msg);
      d.reload(); onChanged();
      flash(document.querySelector(`tr[data-fix="${id}"]`));
    } catch (e: any) { toast(e.detail || e.message); } finally { setBusy(false); }
  };

  const f: Fix | undefined = d.data?.fix;
  return (
    <>
      <div className="drawer-back" ref={back} onClick={close} />
      <aside className="drawer" ref={panel} role="dialog" aria-modal="true" aria-labelledby="fix-title">
        <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start", flexWrap: "nowrap" }}>
          <div>{f && <Sev s={f.severity} />}<h2 id="fix-title" style={{ marginTop: 6 }}>{f ? f.title : "Loading…"}</h2></div>
          <button className="btn quiet" ref={closeBtn} onClick={close} aria-label="Close">Close</button>
        </div>
        {d.error && <ErrorBox text={d.error} onRetry={d.reload} />}
        {f && d.data && (
          <>
            <div className="row" style={{ marginTop: 12 }}>
              <Avatar name={f.owner_name} />
              <label className="sr-only" htmlFor="owner">Owner</label>
              <select id="owner" className="input" style={{ flex: "0 1 220px" }} value={f.owner_id ?? ""} disabled={busy}
                onChange={(e) => e.target.value ? change({ owner_id: Number(e.target.value) }, `${members.find((m) => m.id === Number(e.target.value))?.name} now owns this fix.`)
                  : change({ unassign: true }, "Nobody owns this fix now.")}>
                <option value="">Unassigned</option>
                {members.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
              </select>
              {statusPill(f)}
            </div>
            <div className="qa">
              <div><h3>What's wrong</h3><p>{f.texts.title}. {count(f.pages_affected)} page{f.pages_affected === 1 ? "" : "s"} affected.</p></div>
              <div><h3>Why it matters</h3><p>{f.texts.why}</p></div>
              <div><h3>How to fix</h3><p>{f.texts.fix}</p></div>
              <div><h3>How we'll know</h3><p>{f.texts.verify}</p></div>
            </div>
            {f.status === "verified" && <ImpactSection fixId={id} />}
            <AISection fixId={id} onChanged={() => d.reload()} />
            <div className="row">
              {f.status !== "in_progress" && f.status !== "verified" && <button className="btn" disabled={busy} onClick={() => change({ status: "in_progress" }, "Marked as in progress.")}>Start working</button>}
              {["open", "in_progress"].includes(f.status) && <button className="btn primary" disabled={busy} onClick={() => change({ status: "fixed_pending_check" }, "Marked fixed. The next check will confirm it.")}>I've fixed it</button>}
              {f.status !== "verified" && f.source !== "journey" && <button className="btn" disabled={busy} onClick={async () => {
                setBusy(true);
                try { const r = await api.recheckFix(id); toast(`Checking ${count(r.pages)} page${r.pages === 1 ? "" : "s"} now. It's confirmed when every one is clean.`); go("live", { run: r.run_id }); }
                catch (e: any) { toast(e.detail || e.title || e.message); } finally { setBusy(false); }
              }}>Check this fix now</button>}
              {f.status !== "wont_fix" && f.status !== "verified" && <button className="btn quiet" disabled={busy} onClick={() => change({ status: "wont_fix" }, "Marked as won't fix.")}>Won't fix</button>}
              {(f.status === "wont_fix" || f.status === "verified") && <button className="btn" disabled={busy} onClick={() => change({ status: "open" }, "Reopened.")}>Reopen</button>}
              {f.ticket_url ? <a className="btn quiet" href={f.ticket_url} target="_blank" rel="noopener noreferrer">Open ticket</a>
                : tickets && <button className="btn quiet" disabled={busy} onClick={async () => {
                    setBusy(true);
                    try { const t = await api.createTicket(id); toast(`Ticket ${t.key} created.`); d.reload(); onChanged(); }
                    catch (e: any) { toast(e.detail || e.message); } finally { setBusy(false); }
                  }}>Create ticket</button>}
            </div>
            {d.data.examples.length > 0 && (
              <>
                <p className="section-label" style={{ marginTop: 20 }}>What a visitor saw</p>
                <div className="thumbs">
                  {d.data.examples.map((x) => <img key={x.screenshot} className="shot" src={api.objectUrl(x.screenshot)} alt={`Screenshot of ${x.page_url}`} loading="lazy" />)}
                </div>
              </>
            )}
            <p className="section-label" style={{ marginTop: 20 }}>Pages ({count(f.pages_affected)})</p>
            <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
              {d.data.pages.slice(0, 8).map((u) => <li key={u}><a href={`#/page?url=${encodeURIComponent(u)}`} className="mono">{u}</a>
                {d.data!.page_history?.[u] && <span className="seen-before">{d.data!.page_history[u]}</span>}</li>)}
            </ul>
            <p className="section-label" style={{ marginTop: 20 }}>History</p>
            <ul className="timeline">
              {d.data.events.map((e, i) => <li key={i}><span className="faint mono">{day(e.created_at)}</span><span>{eventText(e, members)}</span></li>)}
            </ul>
            <form className="row" style={{ marginTop: 16 }} onSubmit={(e) => { e.preventDefault(); if (comment.trim()) { change({ comment }, "Comment added."); setComment(""); } }}>
              <label className="sr-only" htmlFor="comment">Comment</label>
              <input id="comment" className="input" placeholder="Add a comment for the team" value={comment} onChange={(e) => setComment(e.target.value)} />
              <button className="btn" type="submit" disabled={busy || !comment.trim()}>Comment</button>
            </form>
          </>
        )}
      </aside>
    </>
  );
}

function eventText(e: { kind: string; detail: any; actor: string | null }, members: Member[]): string {
  const who = e.actor || "The check";
  switch (e.kind) {
    case "found": return `Found on ${count(e.detail?.pages)} page${e.detail?.pages === 1 ? "" : "s"}`;
    case "assigned": return e.detail?.owner_id ? `${who} gave it to ${members.find((m) => m.id === e.detail.owner_id)?.name || "someone"}` : `${who} unassigned it`;
    case "status": return `${who} moved it to ${String(e.detail?.to || "").replace(/_/g, " ")}`;
    case "regressed": return "Found again after it was verified";
    case "verified": return "The check confirmed it's fixed";
    case "comment": return `${who}: ${e.detail?.text}`;
    case "ticket": return `${who} linked a ticket`;
    default: return e.kind;
  }
}


function AISection({ fixId, onChanged }: { fixId: number; onChanged: () => void }) {
  const st = useLoad(() => api.aiStatus().catch(() => null), []);
  const list = useLoad(() => api.aiDrafts(fixId), [fixId]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [edit, setEdit] = useState<{ id: number; title: string; description: string } | null>(null);
  const toast = useToast();
  const make = async () => {
    setBusy(true); setErr(null);
    try { await api.aiDraft(fixId); list.reload(); } catch (e: any) { setErr(e.detail || e.message); } finally { setBusy(false); }
  };
  const decide = async (a: AIDraft, status: "approved" | "rejected", final?: Partial<AIText>) => {
    try { await api.aiDecide(a.id, status, final); toast(status === "approved" ? "Approved. It's noted on the fix." : "Rejected."); setEdit(null); list.reload(); onChanged(); }
    catch (e: any) { toast(e.detail || e.message); }
  };
  const drafts = list.data?.suggestions || [];
  return (
    <section className="ai-box" aria-labelledby="ai-h">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h3 id="ai-h">Draft with local AI</h3>
        <button className="btn small" disabled={busy || !st.data?.ready} onClick={make}>{busy ? "Writing… (can take a minute)" : drafts.length ? "Another draft" : "Write a draft"}</button>
      </div>
      <p className="faint" style={{ fontSize: 12.5, margin: "4px 0 0" }}>
        {st.data?.ready ? `Uses ${st.data.model} on your own machine through Ollama. A draft is only a suggestion: approve, edit or reject it.`
          : st.data?.help || "Checking for Ollama…"}</p>
      {err && <div className="error-box" style={{ marginTop: 8 }}>{err}</div>}
      {drafts.map((a) => {
        const t = a.final || a.draft;
        const editing = edit?.id === a.id;
        return (
          <div className={`ai-draft ${a.status}`} key={a.id}>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <span className={`pill ${a.status === "approved" ? "good" : a.status === "rejected" ? "bad" : "new"}`}>
                {a.status === "approved" ? `Approved${a.decided_by_name ? ` by ${a.decided_by_name}` : ""}` : a.status === "rejected" ? "Rejected" : "Draft"}</span>
              <span className="faint" style={{ fontSize: 12 }}>{a.model}</span>
            </div>
            {a.kind === "title_description" && (editing ? (
              <div className="grid" style={{ gap: 6, marginTop: 8 }}>
                <label className="grid" style={{ gap: 2 }}>Title <span className="faint">{edit!.title.length} characters (30-60)</span>
                  <input className="input" value={edit!.title} onChange={(e) => setEdit({ ...edit!, title: e.target.value })} /></label>
                <label className="grid" style={{ gap: 2 }}>Description <span className="faint">{edit!.description.length} characters (70-160)</span>
                  <textarea className="input" rows={3} value={edit!.description} onChange={(e) => setEdit({ ...edit!, description: e.target.value })} /></label>
              </div>
            ) : (
              <dl className="ai-td">
                <dt>Title <span className="faint">{(t.title || "").length} characters</span></dt><dd>{t.title}</dd>
                <dt>Description <span className="faint">{(t.description || "").length} characters</span></dt><dd>{t.description}</dd>
              </dl>
            ))}
            {t.warnings?.length > 0 && <ul className="ai-warn">{t.warnings.map((w) => <li key={w}>{w}</li>)}</ul>}
            {t.steps?.length > 0 && <ol className="ai-steps">{t.steps.map((x, i) => <li key={i}>{x}</li>)}</ol>}
            {t.reason && <p className="dim" style={{ fontSize: 13, margin: "6px 0 0" }}>{t.reason}</p>}
            <details style={{ marginTop: 6 }}><summary className="faint" style={{ cursor: "pointer", fontSize: 12.5 }}>What the model was given</summary>
              <pre className="ai-ev">{JSON.stringify(a.evidence, null, 2)}</pre></details>
            {a.status === "draft" && (
              <div className="row" style={{ marginTop: 8 }}>
                {editing ? <button className="btn primary small" onClick={() => decide(a, "approved", { title: edit!.title, description: edit!.description })}>Approve my edit</button>
                  : <button className="btn primary small" onClick={() => decide(a, "approved")}>Approve</button>}
                {a.kind === "title_description" && !editing &&
                  <button className="btn small" onClick={() => setEdit({ id: a.id, title: t.title || "", description: t.description || "" })}>Edit</button>}
                <button className="btn quiet small" onClick={() => decide(a, "rejected")}>Reject</button>
              </div>
            )}
          </div>
        );
      })}
    </section>
  );
}


/* Fix board: one column per status. Drag a card, or use its "Move to" menu (keyboard and screen readers). */
const COLUMNS: [FixStatus, string][] = [["open", "Open"], ["in_progress", "In progress"], ["fixed_pending_check", "Fixed: waiting for a check"],
  ["verified", "Verified"], ["wont_fix", "Won't fix"]];

function Board({ fixes, onMoved }: { fixes: Fix[] | null; onMoved: () => void }) {
  const toast = useToast();
  const [over, setOver] = useState<string | null>(null);
  const move = async (f: Fix, to: FixStatus) => {
    if (f.status === to) return;
    if (to === "verified") { toast("Only a check can verify a fix: use \"Check this fix now\" in the fix."); return; }
    try { await api.patchFix(f.id, { status: to }); toast(`Moved to ${COLUMNS.find((c) => c[0] === to)![1]}.`); onMoved(); }
    catch (e: any) { toast(e.detail || e.title || e.message); }
  };
  if (!fixes) return <p className="faint">Loading…</p>;
  return (
    <div className="board" data-reveal>
      {COLUMNS.map(([st, label]) => {
        const items = fixes.filter((f) => f.status === st);
        return (
          <section key={st} className={`board-col ${over === st ? "over" : ""}`} aria-labelledby={`col-${st}`}
            onDragOver={(e) => { e.preventDefault(); setOver(st); }} onDragLeave={() => setOver(null)}
            onDrop={(e) => { e.preventDefault(); setOver(null); const f = fixes.find((x) => x.id === Number(e.dataTransfer.getData("text/plain"))); if (f) move(f, st); }}>
            <h2 id={`col-${st}`}><span>{label}</span><b>{count(items.length)}</b></h2>
            <ul role="list">
              {items.map((f) => (
                <li key={f.id} className="board-card" draggable onDragStart={(e) => e.dataTransfer.setData("text/plain", String(f.id))}>
                  <Sev s={f.severity} />
                  <a href={`#/fixes?fix=${f.id}&layout=board`} className="board-title">{f.title}</a>
                  <span className="faint">{count(f.pages_affected)} page{f.pages_affected === 1 ? "" : "s"}{f.owner_name ? ` · ${f.owner_name}` : ""}</span>
                  <label className="sr-only" htmlFor={`mv-${f.id}`}>Move fix {f.id} to</label>
                  <select id={`mv-${f.id}`} className="board-move" value={f.status} onChange={(e) => move(f, e.target.value as FixStatus)}>
                    {COLUMNS.map(([k, l]) => <option key={k} value={k} disabled={k === "verified" && f.status !== "verified"}>{l}</option>)}
                  </select>
                </li>
              ))}
              {items.length === 0 && <li className="empty board-empty">Nothing here</li>}
            </ul>
          </section>
        );
      })}
    </div>
  );
}

/* Before / after Search Console figures for a verified fix (14, 28, 60 days). A signal, not proof. */
function ImpactSection({ fixId }: { fixId: number }) {
  const d = useLoad(() => api.fixImpact(fixId), [fixId]);
  const x: FixImpact | null = d.data;
  const signed = (n: number | null | undefined, suffix = "") => (n == null ? "–" : `${n > 0 ? "+" : ""}${n.toLocaleString("en-IN")}${suffix}`);
  return (
    <section className="impact" aria-labelledby="imp-h">
      <h3 id="imp-h">Impact since the fix was confirmed</h3>
      {!x ? <p className="faint">Loading…</p> : x.state !== "ok" ? <p className="faint">{x.text}</p> : (<>
        <div className="stat-row">
          {x.windows.map((w) => (
            <div key={w.days} className="stat">
              <span className="stat-label">{w.days} days</span>
              {w.ready && w.change ? (<>
                <b className={w.change.clicks >= 0 ? "up" : "down"}>{signed(w.change.clicks_pct, "%")}</b>
                <span className="stat-sub">clicks {w.before.clicks.toLocaleString("en-IN")} → {w.after!.clicks.toLocaleString("en-IN")}</span>
                <span className="stat-sub">impressions {signed(w.change.impressions_pct, "%")} · position {w.change.position == null ? "–" : signed(-w.change.position)}</span>
              </>) : (<><b className="muted">…</b><span className="stat-sub">{w.text}</span></>)}
            </div>
          ))}
        </div>
        <p className="faint" style={{ fontSize: 12.5, margin: "8px 0 0" }}>{x.pages} page{x.pages === 1 ? "" : "s"}. {x.text}</p>
      </>)}
    </section>
  );
}
