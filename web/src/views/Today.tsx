import { useEffect, useRef, useState } from "react";
import { api, type Site, type TodayCard } from "../api";
import { go } from "../main";
import { countTo, useViewMotion } from "../motion";
import { ErrorBox, Icon, Spark, copyText, count, useLoad, useToast } from "../ui";
import { FlowBars } from "./Insights";
import { SpotCheckButton } from "./SpotCheck";

const CARD_ICON: Record<string, [string, boolean]> = {
  regressed: ["up", true], journey: ["journey", true], access: ["shield", false], unassigned: ["fix", false],
};

export function TodayView({ site }: { site: Site; params: URLSearchParams }) {
  const t = useLoad(() => api.today(site.id), [site.id]);
  const ref = useViewMotion<HTMLDivElement>([!!t.data]);
  const toast = useToast();
  const [starting, setStarting] = useState(false);

  if (t.error) return <ErrorBox text={t.error} onRetry={t.reload} />;
  if (!t.data) return <p className="faint">Loading…</p>;
  const d = t.data;
  const running = d.last_run && ["queued", "running", "pausing", "paused"].includes(d.last_run.status);
  const changedTpl: any[] = d.last_run?.summary?.templates_changed || [];
  const change = d.kpis.score_change == null ? null : Math.round(d.kpis.score_change);

  const act = async (c: TodayCard) => {
    if (c.kind === "journey") go("journeys", { run: c.journey_run_id ?? "" });
    else if (c.kind === "access") {
      const a = await api.access(site.id).catch(() => null);
      const ok = a && (await copyText(a.rules.akamai));
      toast(ok ? "Rule copied. Send it to your CDN team." : "Open Crawler access to copy the rule.");
      if (!ok) go("access");
    } else if (c.fix_id) go("fixes", { fix: c.fix_id });
  };
  const start = async () => {
    setStarting(true);
    try {
      const r = await api.startRun(site.id, "full");
      toast("Check started. You can watch it live.");
      go("live", { run: r.run_id });
    } catch (e: any) { toast(e.detail || e.message); setStarting(false); }
  };

  return (
    <div className="view" ref={ref}>
      <div className="head">
        <div>
          <h1>{d.greeting}</h1>
          <p className="lede">{d.lede}</p>
        </div>
        {running ? <button className="btn primary" onClick={() => go("live", { run: d.last_run!.id })}><Icon name="live" />{d.last_run!.status === "paused" ? "Resume the check" : "Watch the check"}</button>
          : <div className="row"><SpotCheckButton site={site} /><button className="btn primary" disabled={starting} onClick={start}><Icon name="play" />Check the site now</button></div>}
      </div>
      <div className="kpis" data-reveal>
        <Kpi label="Site health" value={d.score} format={(n) => n.toFixed(0)} sub={change == null ? "out of 100"
          : change === 0 ? "same as the check before" : `${change > 0 ? "up" : "down"} ${Math.abs(change)} since the check before`}
          tone={change != null && change < 0 ? "down" : change ? "up" : undefined} onClick={() => go("insights")}
          spark={d.trend.map((x) => x.score)} />
        <Kpi label="Open fixes" value={d.fixes.open} sub={d.kpis.open_by_severity.critical + d.kpis.open_by_severity.high > 0
          ? `${d.kpis.open_by_severity.critical} critical, ${d.kpis.open_by_severity.high} high` : "none critical or high"}
          tone={d.kpis.open_by_severity.critical ? "down" : undefined} onClick={() => go("fixes")} />
        <Kpi label="Pages checked" value={d.kpis.pages} sub={d.kpis.visited ? `${count(d.kpis.visited)} visited in a real browser` : "in the last check"} onClick={() => go("pages")} />
        <Kpi label="Journeys passing" value={d.kpis.journeys.total ? d.kpis.journeys.passed : null}
          suffix={d.kpis.journeys.total ? ` of ${d.kpis.journeys.total}` : ""} sub={d.kpis.journeys.total ? "last run of each" : "none set up yet"}
          tone={d.kpis.journeys.passed < d.kpis.journeys.total ? "down" : undefined} onClick={() => go("journeys")} />
        <Kpi label="Confirmed fixed" value={d.fixes.verified} sub={`${count(d.fixes.waiting)} waiting for the next check`} onClick={() => go("fixes", { view: "verified" })} />
      </div>
      <div className="grid g-main">
        <div className="grid">
          <div className="panel" data-reveal>
            <p className="section-label">Needs you</p>
            {d.cards.length === 0 && changedTpl.length === 0 && (
              <div className="all-clear"><Icon name="fix" /><div><h3>All clear</h3><p>Nothing needs you right now. We'll keep checking.</p></div></div>
            )}
            {changedTpl.slice(0, 2).map((t) => (
              <div className="need" key={`v${t.template_id}`}>
                <div className="icon"><Icon name="page" /></div>
                <div><h3>{t.template} look different since the last check</h3>
                  <p>{Math.round(t.changed * 100)}% of the page changed on the {t.pages_compared} pages compared. If that wasn't a planned release, have a look.</p></div>
                <button className="btn" onClick={() => go("fixes")}>See fixes</button>
              </div>
            ))}
            {d.cards.map((c, i) => {
              const [icon, alert] = CARD_ICON[c.kind] || ["fix", false];
              return (
                <div className="need" key={i}>
                  <div className={`icon ${alert ? "alert" : ""}`}><Icon name={icon} /></div>
                  <div><h3>{c.title}</h3><p>{c.text}</p></div>
                  <button className={`btn ${i === 0 ? "primary" : ""}`} onClick={() => act(c)}>{c.action}</button>
                </div>
              );
            })}
          </div>
          {d.flow.length > 0 && (
            <div className="panel" data-reveal>
              <div className="panel-head"><h2>Fixes over the last checks</h2>
                <button className="btn quiet small" onClick={() => go("insights")}>More in Insights</button></div>
              <FlowBars runs={d.flow} />
            </div>
          )}
        </div>
        <div className="grid">
          <TopOpportunities site={site} />
          <div className="panel" data-reveal>
            <p className="section-label">What we checked</p>
            <p style={{ margin: 0 }}>{d.checked}</p>
          </div>
        </div>
      </div>
    </div>
  );
}

function Kpi({ label, value, sub, suffix = "", tone, format = (n: number) => count(Math.round(n)), onClick, spark }: {
  label: string; value: number | null | undefined; sub: string; suffix?: string; tone?: "up" | "down"; format?: (n: number) => string;
  onClick?: () => void; spark?: number[] }) {
  const ref = useRef<HTMLElement>(null);
  useEffect(() => { if (value != null) countTo(ref.current, value, format); }, [value]);
  return (
    <button className="kpi" onClick={onClick}>
      <span className="kpi-label">{label}</span>
      <span className="kpi-value"><b ref={ref} className="num">{value == null ? "—" : format(value)}</b>{suffix && <small>{suffix}</small>}</span>
      <span className={`kpi-sub ${tone || ""}`}>{sub}</span>
      {spark && spark.length > 1 && <span className="kpi-spark"><Spark values={spark} /></span>}
    </button>
  );
}

function TopOpportunities({ site }: { site: Site }) {
  const o = useLoad(() => api.opportunities(site.id, "", 3).catch(() => null), [site.id]);
  const top = (o.data?.pages || []).filter((p) => p.bucket !== "monitor");
  if (!top.length) return null;
  return (
    <div className="panel" data-reveal>
      <div className="panel-head"><p className="section-label" style={{ margin: 0 }}>Worth doing first</p>
        <button className="btn quiet" onClick={() => go("growth")}>All opportunities</button></div>
      <ul className="opp-list">{top.map((p) => <li key={p.url}><b className="num">{p.score.toFixed(0)}</b>
        <span><a className="mono" href={`#/page?url=${encodeURIComponent(p.url)}`}>{(() => { try { return new URL(p.url).pathname; } catch { return p.url; } })()}</a>
        <span className="faint">{o.data?.buckets.find((b) => b.id === p.bucket)?.label}</span></span></li>)}</ul>
    </div>
  );
}
