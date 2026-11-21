import { useState, type ReactNode } from "react";
import { api, type Insights, type Site } from "../api";
import { go } from "../main";
import { useViewMotion } from "../motion";
import { ErrorBox, Sev, count, day, useLoad } from "../ui";

/* Insights (dashboard). Charts follow the data-viz method: one axis, thin marks, legend + direct labels, hover
   tooltip, a table view for every chart, ratings shown as words (never colour alone). Series colours come from
   --viz-new / --viz-done, validated for both themes. */

const RANGES: [number, string][] = [[6, "Last 6 checks"], [12, "Last 12"], [30, "Last 30"]];
const DIM_NAMES: Record<string, string> = { technical: "Technical", links: "Links", content: "Content", experience: "Experience",
  performance: "Performance", security: "Security" };
const RATING: Record<string, [string, string]> = { good: ["good", "Good"], "needs work": ["warn", "Needs work"], poor: ["bad", "Poor"] };

export function InsightsView({ site }: { site: Site; params: URLSearchParams }) {
  const [runs, setRuns] = useState(12);
  const d = useLoad(() => api.insights(site.id, runs), [site.id, runs]);
  const ref = useViewMotion<HTMLDivElement>([!!d.data]);
  if (d.error) return <ErrorBox text={d.error} onRetry={d.reload} />;
  const x = d.data;
  return (
    <div className="view" ref={ref}>
      <div className="head">
        <div>
          <h1>Insights</h1>
          <p className="lede">How {site.primary_host} and the team are trending, from the checks QC has run. Every number says what it's based on.</p>
        </div>
        <div className="seg" role="group" aria-label="How many checks">
          {RANGES.map(([n, label]) => <button key={n} aria-pressed={runs === n} onClick={() => setRuns(n)}>{label}</button>)}
        </div>
      </div>
      {!x ? <p className="faint">Loading…</p> : x.runs_considered === 0 ? (
        <div className="panel empty">No check has finished yet. Insights appear after the first one.
          <div style={{ marginTop: 12 }}><button className="btn primary" onClick={() => go("today")}>Go to Today</button></div></div>
      ) : <Body x={x} />}
    </div>
  );
}

function Body({ x }: { x: Insights }) {
  return (
    <>
      {x.headlines.length > 0 && (
        <div className="panel headlines" data-reveal>
          <p className="section-label">In short</p>
          <ul>{x.headlines.map((h) => <li key={h}>{h}</li>)}</ul>
        </div>
      )}
      <div className="grid g-2" style={{ marginTop: 16 }}>
        <Card title="Health over time" basis={x.score.basis}
          table={<SimpleTable head={["Check", "Date", "Score"]} rows={x.score.points.map((p) => [`#${p.run_id}`, day(p.at), Math.round(p.score)])} />}>
          <ScoreLine points={x.score.points} />
          {Object.values(x.score.dimension_change || {}).some((v) => v !== 0) && (
            <div className="dim-change"><span className="faint" style={{ fontSize: 12 }}>Change since the first check:</span>
              {Object.entries(x.score.dimension_change!).filter(([, v]) => v !== 0).map(([k, v]) => (
                <span key={k} className="chip">{DIM_NAMES[k] || k} <b className={v > 0 ? "up" : v < 0 ? "down" : ""}>{v > 0 ? "+" : ""}{v}</b></span>
              ))}
            </div>
          )}
        </Card>
        <Card title="Fixes: new, confirmed, back again" basis={x.fix_flow.basis}
          table={<SimpleTable head={["Check", "Date", "New", "Confirmed fixed", "Back again"]}
            rows={x.fix_flow.runs.map((r) => [`#${r.run_id}`, day(r.at), r.new, r.verified, r.regressed])} />}>
          <FlowBars runs={x.fix_flow.runs} />
        </Card>
      </div>
      <div className="grid g-3" style={{ marginTop: 16 }}>
        <div className="panel" data-reveal>
          <p className="section-label">Open now, by severity</p>
          <SeverityBars items={x.backlog.by_severity} />
          <p className="basis">{x.backlog.unowned ? `${x.backlog.unowned} with no owner. ` : ""}{x.backlog.basis}</p>
        </div>
        <div className="panel" data-reveal>
          <p className="section-label">Time to confirm a fix</p>
          {x.time_to_verify.count ? (
            <>
              <div className="hero num">{span(x.time_to_verify.median_hours?.all ?? x.time_to_verify.median_days.all * 24)}</div>
              <p className="dim" style={{ margin: "4px 0 10px" }}>median, from first found to confirmed by a check</p>
              <div className="facts">
                {(["critical", "high", "medium", "low"] as const).filter((s) => x.time_to_verify.median_days[s] != null).map((s) => (
                  <div className="fact" key={s}><b className="num">{span(x.time_to_verify.median_hours?.[s] ?? x.time_to_verify.median_days[s] * 24, true)}</b><span><Sev s={s} /></span></div>
                ))}
              </div>
            </>
          ) : <p className="empty" style={{ padding: "16px 0" }}>Nothing confirmed yet. Mark a fix as fixed and the next check confirms it.</p>}
          <p className="basis">{x.time_to_verify.basis}</p>
        </div>
        <div className="panel" data-reveal>
          <p className="section-label">Real-browser visits, latest check</p>
          <Visits v={x.visits} />
        </div>
      </div>
      <Card title="Page types" basis={x.page_types.basis} style={{ marginTop: 16 }}>
        <PageTypes items={x.page_types.items} />
      </Card>
      <Card title="Speed and stability by page type" basis={x.vitals.basis} style={{ marginTop: 16 }}>
        <Vitals items={x.vitals.items} />
      </Card>
      <div className="grid g-2" style={{ marginTop: 16 }}>
        <Card title="What the pages answered" basis={x.crawl.basis}>
          <StatusMix mix={x.crawl.status_mix} />
          {x.crawl.pages_by_run.length > 1 && (
            <SimpleTable head={["Check", "Date", "Pages", "Minutes"]}
              rows={x.crawl.pages_by_run.slice(-6).reverse().map((r) => [`#${r.run_id}`, day(r.at), count(r.pages), r.minutes ?? "—"])} />
          )}
        </Card>
        <Card title="Who's looking after what" basis={x.owners.basis}>
          {x.owners.items.length ? (
            <SimpleTable head={["Owner", "Open", "Waiting", "Confirmed (30 d)"]}
              rows={x.owners.items.map((o) => [o.name, o.open, o.waiting, o.verified_30d])} />
          ) : <p className="empty">Nobody owns a fix yet. Give fixes an owner from the Fixes page.</p>}
        </Card>
      </div>
      <div className="grid g-2" style={{ marginTop: 16 }}>
        <Card title="Journeys" basis={x.journeys.basis}>
          {x.journeys.items.length ? (
            <SimpleTable head={["Journey", "Passed", "Typical time"]}
              rows={x.journeys.items.map((j) => [j.name, j.runs ? `${j.passed} of ${j.runs}` : "Not run yet", j.median_s != null ? `${j.median_s} s` : "—"])} />
          ) : <p className="empty">No journeys yet.</p>}
        </Card>
        <Card title="Came back after being fixed" basis={x.regressions.basis}>
          {x.regressions.items.length ? (
            <ul className="found">{x.regressions.items.map((r) => (
              <li key={r.id} className="regr"><Sev s={r.severity} /><a href={`#/fixes?fix=${r.id}`} style={{ color: "inherit" }}>{r.title}</a>
                <span className="faint num">{day(r.at)}</span></li>
            ))}</ul>
          ) : <p className="empty">Nothing has come back. Good.</p>}
        </Card>
      </div>
    </>
  );
}

function span(hours: number, short = false): ReactNode {
  if (hours < 1) return short ? "< 1 h" : <>&lt; 1<span> hour</span></>;
  if (hours < 48) { const h = Math.round(hours); return short ? `${h} h` : <>{h}<span> hour{h === 1 ? "" : "s"}</span></>; }
  const d = Math.round(hours / 24);
  return short ? `${d} d` : <>{d}<span> days</span></>;
}

function Card({ title, basis, table, children, style }: { title: string; basis: string; table?: ReactNode; children: ReactNode; style?: React.CSSProperties }) {
  const [asTable, setAsTable] = useState(false);
  return (
    <div className="panel" data-reveal style={style}>
      <div className="panel-head">
        <h2>{title}</h2>
        {table && <button className="btn quiet small" aria-pressed={asTable} onClick={() => setAsTable(!asTable)}>{asTable ? "Show chart" : "Show as table"}</button>}
      </div>
      {asTable && table ? table : children}
      <p className="basis">Based on {basis}.</p>
    </div>
  );
}

function SimpleTable({ head, rows }: { head: string[]; rows: (string | number | null)[][] }) {
  return (
    <div className="table-wrap flat">
      <table>
        <thead><tr>{head.map((h, i) => <th key={h} className={i ? "num" : ""}>{h}</th>)}</tr></thead>
        <tbody>{rows.map((r, i) => <tr key={i}>{r.map((c, j) => <td key={j} className={j ? "num nowrap" : "title nowrap"}>{c ?? "—"}</td>)}</tr>)}</tbody>
      </table>
    </div>
  );
}

/* ---------- charts ---------- */
const W = 560, H = 200, PAD = { l: 34, r: 14, t: 14, b: 26 };

function Tip({ at, children }: { at: { x: number; y: number } | null; children: ReactNode }) {
  if (!at) return null;
  return <div className="viz-tip" role="status" style={{ left: `${(at.x / W) * 100}%`, top: `${(at.y / H) * 100}%` }}>{children}</div>;
}

function ScoreLine({ points }: { points: Insights["score"]["points"] }) {
  const [hover, setHover] = useState<number | null>(null);
  if (points.length < 2) return <p className="empty">One check so far: health {Math.round(points[0]?.score ?? 0)}. The trend appears after the next check.</p>;
  const iw = W - PAD.l - PAD.r, ih = H - PAD.t - PAD.b;
  const lo = Math.max(0, Math.floor((Math.min(...points.map((p) => p.score)) - 5) / 10) * 10), hi = 100;
  const X = (i: number) => PAD.l + (i / (points.length - 1)) * iw;
  const Y = (v: number) => PAD.t + ih - ((v - lo) / (hi - lo)) * ih;
  const d = points.map((p, i) => `${i ? "L" : "M"}${X(i).toFixed(1)} ${Y(p.score).toFixed(1)}`).join(" ");
  const ticks = [lo, Math.round((lo + hi) / 2), hi];
  const h = hover != null ? points[hover] : null;
  const last = points[points.length - 1];
  return (
    <div className="viz" onMouseLeave={() => setHover(null)}>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Health score from ${Math.round(points[0].score)} to ${Math.round(last.score)} over ${points.length} checks`}>
        {ticks.map((t) => <g key={t}><line x1={PAD.l} x2={W - PAD.r} y1={Y(t)} y2={Y(t)} className="grid-line" />
          <text x={PAD.l - 6} y={Y(t) + 4} textAnchor="end" className="axis">{t}</text></g>)}
        <path d={d} fill="none" stroke="var(--viz-done)" strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />
        {h && <line x1={X(hover!)} x2={X(hover!)} y1={PAD.t} y2={PAD.t + ih} className="crosshair" />}
        {points.map((p, i) => (
          <circle key={p.run_id} cx={X(i)} cy={Y(p.score)} r={hover === i || i === points.length - 1 ? 4.5 : 0}
            fill="var(--viz-done)" stroke="var(--surface)" strokeWidth="2" />
        ))}
        <text x={X(points.length - 1) - 8} y={Y(last.score) - 10} textAnchor="end" className="label">{Math.round(last.score)}</text>
        <text x={PAD.l} y={H - 6} className="axis">{day(points[0].at)}</text>
        <text x={W - PAD.r} y={H - 6} textAnchor="end" className="axis">{day(last.at)}</text>
        {points.map((p, i) => (
          <rect key={`h${p.run_id}`} x={X(i) - iw / (points.length - 1) / 2} y={PAD.t} width={iw / (points.length - 1)} height={ih}
            fill="transparent" onMouseEnter={() => setHover(i)} onFocus={() => setHover(i)} tabIndex={0}
            aria-label={`Check ${p.run_id}, ${day(p.at)}: ${Math.round(p.score)}`} />
        ))}
      </svg>
      <Tip at={h ? { x: X(hover!), y: Y(h.score) } : null}>
        {h && <><b>Check #{h.run_id}</b> · {day(h.at)}<br />Health <b className="num">{Math.round(h.score)}</b></>}
      </Tip>
    </div>
  );
}

function FlowBars({ runs }: { runs: Insights["fix_flow"]["runs"] }) {
  const [hover, setHover] = useState<number | null>(null);
  if (!runs.length) return <p className="empty">No checks yet.</p>;
  const iw = W - PAD.l - PAD.r, ih = H - PAD.t - PAD.b;
  const max = Math.max(1, ...runs.map((r) => Math.max(r.new, r.verified, r.regressed)));
  const nice = Math.ceil(max / 5) * 5;
  const slot = iw / runs.length, bw = Math.min(16, (slot - 10) / 3);
  const Y = (v: number) => PAD.t + ih - (v / nice) * ih;
  const bar = (x: number, v: number, cls: string) => {
    const y = Y(v), hgt = PAD.t + ih - y;
    if (v <= 0) return null;
    const r = Math.min(4, hgt);           // 4px rounded data-end, square at the baseline
    return <path className={cls} d={`M${x} ${PAD.t + ih} V${y + r} Q${x} ${y} ${x + r} ${y} H${x + bw - r} Q${x + bw} ${y} ${x + bw} ${y + r} V${PAD.t + ih} Z`} />;
  };
  const h = hover != null ? runs[hover] : null;
  const lastI = runs.length - 1;
  return (
    <div className="viz" onMouseLeave={() => setHover(null)}>
      <div className="legend"><span><i className="sw new" />New</span><span><i className="sw done" />Confirmed fixed</span><span><i className="sw back" />Back again</span></div>
      <svg viewBox={`0 0 ${W} ${H}`} role="img"
        aria-label={`New, confirmed and returning fixes per check. Latest: ${runs[lastI].new} new, ${runs[lastI].verified} confirmed, ${runs[lastI].regressed} back again.`}>
        {[0, nice / 2, nice].map((t) => <g key={t}><line x1={PAD.l} x2={W - PAD.r} y1={Y(t)} y2={Y(t)} className="grid-line" />
          <text x={PAD.l - 6} y={Y(t) + 4} textAnchor="end" className="axis">{t}</text></g>)}
        {runs.map((r, i) => {
          const x0 = PAD.l + i * slot + (slot - (bw * 3 + 4)) / 2;
          return (
            <g key={r.run_id} opacity={hover == null || hover === i ? 1 : 0.45}>
              {bar(x0, r.new, "m-new")}{bar(x0 + bw + 2, r.verified, "m-done")}{bar(x0 + 2 * (bw + 2), r.regressed, "m-back")}
              {(i === lastI || hover === i) && ([[r.new, 0], [r.verified, 1], [r.regressed, 2]] as const).map(([v, k]) => v > 0 &&
                <text key={k} x={x0 + k * (bw + 2) + bw / 2} y={Y(v) - 5} textAnchor="middle" className="label">{v}</text>)}
              <rect x={PAD.l + i * slot} y={PAD.t} width={slot} height={ih} fill="transparent" tabIndex={0}
                onMouseEnter={() => setHover(i)} onFocus={() => setHover(i)}
                aria-label={`Check ${r.run_id}: ${r.new} new, ${r.verified} confirmed fixed, ${r.regressed} back again`} />
            </g>
          );
        })}
        <line x1={PAD.l} x2={W - PAD.r} y1={PAD.t + ih} y2={PAD.t + ih} className="baseline" />
        <text x={PAD.l} y={H - 6} className="axis">{day(runs[0].at)}</text>
        <text x={W - PAD.r} y={H - 6} textAnchor="end" className="axis">{day(runs[lastI].at)}</text>
      </svg>
      <Tip at={h ? { x: PAD.l + hover! * slot + slot / 2, y: Y(Math.max(h.new, h.verified, h.regressed)) } : null}>
        {h && <><b>Check #{h.run_id}</b> · {day(h.at)}<br /><i className="sw new" />New <b className="num">{h.new}</b><br />
          <i className="sw done" />Confirmed fixed <b className="num">{h.verified}</b><br /><i className="sw back" />Back again <b className="num">{h.regressed}</b></>}
      </Tip>
    </div>
  );
}

function SeverityBars({ items }: { items: Insights["backlog"]["by_severity"] }) {
  const max = Math.max(1, ...items.map((s) => s.open));
  return (
    <div className="hbars">
      {items.map((s) => (
        <div className="hbar" key={s.severity} title={`${s.open} open, ${count(s.pages)} pages`}>
          <Sev s={s.severity} />
          <div className="track"><i className={`sev-${s.severity}`} style={{ width: `${(s.open / max) * 100}%` }} /></div>
          <b className="num">{s.open}</b>
        </div>
      ))}
    </div>
  );
}

function Visits({ v }: { v: Insights["visits"] }) {
  const ok = v.by_status.ok || 0, total = v.total || 0;
  if (!total) return <p className="empty" style={{ padding: "16px 0" }}>No visits in the latest check.</p>;
  return (
    <>
      <div className="hero num">{Math.round((ok / total) * 100)}<span>% loaded fine</span></div>
      <div className="facts">
        <div className="fact"><b className="num">{ok}</b><span>loaded fine</span></div>
        <div className="fact"><b className="num">{v.by_status.failed || 0}</b><span>couldn't load</span></div>
        <div className="fact"><b className="num">{v.by_status.challenged || 0}</b><span>stopped by a bot check</span></div>
        <div className="fact"><b className="num">{v.by_status.skipped || 0}</b><span>skipped</span></div>
      </div>
      {v.reasons.length > 0 && <>
        <p className="section-label" style={{ marginTop: 14 }}>Why visits failed</p>
        <ul className="reasons">{v.reasons.map((r) => <li key={r.reason}><span>{r.reason}</span><b className="num">{r.visits}</b></li>)}</ul>
      </>}
      <p className="basis">{v.basis}</p>
    </>
  );
}

function PageTypes({ items }: { items: Insights["page_types"]["items"] }) {
  if (!items.length) return <p className="empty">No page types yet.</p>;
  const max = Math.max(1, ...items.map((t) => t.pages));
  return (
    <div className="table-wrap flat">
      <table>
        <thead><tr><th>Page type</th><th>Pages</th><th className="num">Open fixes</th><th>Worst</th><th className="num">Visited</th><th className="num">Visits with trouble</th></tr></thead>
        <tbody>{items.map((t) => (
          <tr key={t.template_id} className="click" tabIndex={0} onClick={() => go("fixes")} onKeyDown={(e) => e.key === "Enter" && go("fixes")}>
            <td className="title">{t.name}</td>
            <td><span className="inline-bar"><i style={{ width: `${(t.pages / max) * 100}%` }} /></span><span className="num">{count(t.pages)}</span></td>
            <td className="num">{t.open_fixes}</td>
            <td>{t.worst ? <Sev s={t.worst} /> : <span className="faint">None</span>}</td>
            <td className="num">{t.visited}</td>
            <td className="num">{t.trouble}</td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  );
}

function Vitals({ items }: { items: Insights["vitals"]["items"] }) {
  if (!items.length) return <p className="empty">No real-browser visits in the latest check.</p>;
  const cell = (v: number | null, rating: string | null | undefined, unit: string) => v == null ? <span className="faint">—</span> : (
    <span className={`pill vital ${rating ? RATING[rating]?.[0] || "" : ""}`}>
      <span className="num">{unit === "" ? v : `${count(v)} ${unit}`}</span>{rating && <>{"\u00a0·\u00a0"}{RATING[rating]?.[1] || rating}</>}</span>
  );
  return (
    <div className="table-wrap flat">
      <table>
        <thead><tr><th>Page type</th><th>Device</th><th className="num">Visits</th><th title="Largest Contentful Paint">Main content shown</th>
          <th title="Cumulative Layout Shift">Layout shift</th><th title="Interaction to Next Paint">Reacts to a tap</th><th title="Time to First Byte">First byte</th></tr></thead>
        <tbody>{items.map((v) => (
          <tr key={`${v.template_id}-${v.device}`}>
            <td className="title">{v.name}</td><td>{v.device === "mobile" ? "Mobile" : "Desktop"}</td><td className="num">{v.visits}</td>
            <td>{cell(v.lcp, v.rating.lcp, "ms")}</td><td>{cell(v.cls, v.rating.cls, "")}</td>
            <td>{cell(v.inp, v.rating.inp, "ms")}</td><td>{cell(v.ttfb, v.rating.ttfb, "ms")}</td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  );
}

const STATUS_ORDER: [string, string, string][] = [["2xx", "Fine (2xx)", "good"], ["3xx", "Redirect (3xx)", "low"], ["4xx", "Not found / refused (4xx)", "high"],
  ["5xx", "Server error (5xx)", "critical"], ["no answer", "No answer", "ink"]];

function StatusMix({ mix }: { mix: Record<string, number> }) {
  const total = Object.values(mix).reduce((a, b) => a + b, 0);
  if (!total) return <p className="empty">No pages in the latest check.</p>;
  const parts = STATUS_ORDER.filter(([k]) => mix[k]);
  return (
    <>
      <div className="stack" role="img" aria-label={parts.map(([k, l]) => `${l}: ${mix[k]}`).join(", ")}>
        {parts.map(([k, l, c]) => <i key={k} className={`st-${c}`} style={{ flexGrow: mix[k] }} title={`${l}: ${count(mix[k])}`} />)}
      </div>
      <ul className="reasons">{parts.map(([k, l, c]) => (
        <li key={k}><span><i className={`sw st-${c}`} />{l}</span><b className="num">{count(mix[k])} <span className="faint">({Math.round((mix[k] / total) * 100)}%)</span></b></li>
      ))}</ul>
    </>
  );
}
