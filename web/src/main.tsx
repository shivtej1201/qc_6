import { StrictMode, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles/app.css";
import "./styles/glass.css";
import { api, localStorageGet, localStorageSet, type Site } from "./api";
import { WAKE_TEXT, useScreenAwake } from "./wakeLock";
import { ErrorBox, Icon, ToastProvider, useLoad } from "./ui";
import { Palette, type Screen } from "./palette";
import { InsightsView } from "./views/Insights";
import { GrowthView } from "./views/Growth";
import { TodayView } from "./views/Today";
import { PagesView } from "./views/Pages";
import { LiveView } from "./views/Live";
import { FixesView } from "./views/Fixes";
import { PageView } from "./views/Page";
import { JourneysView } from "./views/Journeys";
import { LinksView } from "./views/Links";
import { AccountView, ResetPassword, SignIn } from "./views/Account";
import { StartView } from "./views/Start";

export type Route = { view: string; params: URLSearchParams };
function parseHash(): Route {
  const h = window.location.hash.replace(/^#\/?/, "");
  const [view, q] = h.split("?");
  return { view: view || "start", params: new URLSearchParams(q || "") };
}
export function go(view: string, params: Record<string, string | number> = {}) {
  const q = new URLSearchParams(Object.entries(params).map(([k, v]) => [k, String(v)])).toString();
  window.location.hash = `/${view}${q ? "?" + q : ""}`;
}

const NAV_GROUPS: { label: string; more?: boolean; items: [string, string, string, string][] }[] = [
  { label: "URL QC", items: [["start", "play", "New check", "start crawl sitemap upload sheet url jobs results ticket"], ["fixes", "fix", "Problems", "issues problems fixes inbox"],
    ["links", "link", "Links & images", "broken redirects images"]] },
  { label: "More tools", more: true, items: [["today", "today", "Today", "home dashboard needs"], ["insights", "up", "Insights", "trends charts"],
    ["growth", "growth", "Growth", "opportunities search console queries"], ["pages", "layers", "All pages", "inventory urls compare"],
    ["journeys", "journey", "Journeys", "flows paths"], ["live", "live", "Live run", "check running progress"]] },
];
const NAV = NAV_GROUPS.flatMap((g) => g.items);
const SCREEN_VIEWS: Record<string, true> = Object.fromEntries([...NAV.map((n) => n[0]), "page"].map((v) => [v, true]));
const SCREENS: Screen[] = NAV.map(([view, icon, label, words]) => ({ view, icon, label, words }));
const TABS: [string, string, string][] = [["start", "play", "Check"], ["fixes", "fix", "Problems"], ["links", "link", "Links"]];
const TITLES: Record<string, string> = { ...Object.fromEntries(NAV.map((n) => [n[0], n[2]])), page: "Page", account: "Your account" };

function App({ local }: { local: boolean }) {
  const [route, setRoute] = useState<Route>(parseHash);
  useEffect(() => {
    const on = () => setRoute(parseHash());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  const sites = useLoad(() => api.sites(), []);
  const [siteId, setSiteId] = useState<number | null>(() => Number(localStorageGet("qc6-site")) || null);
  const site: Site | undefined = useMemo(() => {
    const list = sites.data?.sites || [];
    return list.find((s) => s.id === siteId) || list[0];
  }, [sites.data, siteId]);
  const [theme, setTheme] = useState<string | null>(() => localStorageGet("qc6-theme"));
  const awake = useScreenAwake();
  useEffect(() => {
    if (theme) document.documentElement.dataset.theme = theme;
    else delete document.documentElement.dataset.theme;
  }, [theme]);
  useEffect(() => { document.title = `QC V6 · ${TITLES[route.view] || "New check"}`; }, [route.view]);
  const [palette, setPalette] = useState(false);
  const [menu, setMenu] = useState(false);
  useEffect(() => { setMenu(false); }, [route.view, route.params.toString()]);
  useEffect(() => {                                       // a modal open: everything behind it is inert
    const phoneMenu = menu && window.matchMedia("(max-width: 900px)").matches;
    const behind = palette ? ["main", ".rail", ".topbar", ".tabbar", ".skip"] : phoneMenu ? ["main", ".topbar", ".tabbar", ".skip"] : [];
    document.querySelectorAll("main, .rail, .topbar, .tabbar, .skip").forEach((el) => el.removeAttribute("inert"));
    behind.forEach((sel) => document.querySelector(sel)?.setAttribute("inert", ""));
  }, [palette, menu, route.view]);
  const wasOpen = useRef(false);
  useEffect(() => {                                       // phone menu: focus goes in when it opens, back when it closes
    if (!window.matchMedia("(max-width: 900px)").matches) return;
    if (menu) (document.querySelector(".rail-close") as HTMLElement | null)?.focus();
    else if (wasOpen.current) (document.querySelector('.topbar .icon-btn[aria-controls="rail"]') as HTMLElement | null)?.focus({ preventScroll: true });
    wasOpen.current = menu;
  }, [menu]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const typing = /^(INPUT|TEXTAREA|SELECT)$/.test((e.target as HTMLElement)?.tagName) || (e.target as HTMLElement)?.isContentEditable;
      if ((e.key === "k" || e.key === "K") && (e.ctrlKey || e.metaKey)) { e.preventDefault(); setPalette(true); }
      else if (e.key === "/" && !typing && !palette) { e.preventDefault(); setPalette(true); }
      else if (e.key === "Escape" && menu) setMenu(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [palette, menu]);

  const toggleTheme = () => {
    const cur = theme || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const next = cur === "dark" ? "light" : "dark";
    setTheme(next);
    localStorageSet("qc6-theme", next);
  };

  const pickSite = (v: number) => { setSiteId(v); localStorageSet("qc6-site", String(v)); };
  let body;
  if (sites.error) body = <ErrorBox text={sites.error} onRetry={sites.reload} />;
  else if (!sites.data) body = <p className="faint">Loading…</p>;
  else if (route.view === "account" && local) body = <AccountView />;
  else if (route.view === "start" || !site || !(route.view in SCREEN_VIEWS))   // no setup screens: anything else lands on New check
    body = <StartView params={route.params} onSite={(id) => { if (id !== site?.id) { sites.reload(); pickSite(id); } }}
      onSitesChanged={() => sites.reload()} />;
  else {
    const p = { site, params: route.params };
    body = {
      today: <TodayView {...p} />, insights: <InsightsView {...p} />, growth: <GrowthView {...p} />, live: <LiveView {...p} />, fixes: <FixesView {...p} />, pages: <PagesView {...p} />, page: <PageView {...p} />,
      journeys: <JourneysView {...p} />, links: <LinksView {...p} />,
    }[route.view] || <TodayView {...p} />;
  }

  const current = route.view === "page" ? "pages" : route.view;
  return (
    <div className={`app ${menu ? "menu-open" : ""}`}>
      <a className="skip" href="#main">Skip to content</a>
      <header className="topbar">
        <button className="icon-btn" aria-label="Open the menu" aria-expanded={menu} aria-controls="rail" onClick={() => setMenu(true)}><Icon name="menu" /></button>
        <div className="topbar-title"><b>{TITLES[route.view] || "New check"}</b>{site && <span>{site.primary_host}</span>}</div>
        <button className="icon-btn" aria-label="Quick jump" onClick={() => setPalette(true)}><Icon name="search" /></button>
      </header>
      {menu && <div className="rail-back" onClick={() => setMenu(false)} />}
      <aside className="rail" id="rail" aria-label="Main">
        <div className="logo"><div className="logo-mark">QC</div><div><b>QC V6</b><span>Site quality</span></div>
          <button className="icon-btn rail-close" aria-label="Close the menu" onClick={() => setMenu(false)}><Icon name="close" /></button></div>
        {site && <p className="site-now" title="Shown for the check you opened last. Start a new check from a sitemap, a sheet or one URL.">Showing <b>{site.primary_host}</b></p>}
        <button className="jump" onClick={() => setPalette(true)}><Icon name="search" /><span>Jump to…</span><kbd>Ctrl K</kbd></button>
        <nav aria-label="Sections">
          {NAV_GROUPS.map((g) => {
            const items = g.items.map(([v, icon, label]) => (
              <a key={v} className="nav" href={`#/${v}`} aria-current={current === v ? "page" : undefined}>
                <Icon name={icon} />{label}
                {v === "fixes" && site?.open_fixes ? <span className="count" aria-label={`${site.open_fixes} open`}>{site.open_fixes}</span> : null}
              </a>
            ));
            return g.more ? (
              <details className="nav-group nav-more" key={g.label} open={g.items.some(([v]) => v === current) || undefined}>
                <summary className="nav-label">{g.label}</summary>{items}
              </details>
            ) : (
              <div className="nav-group" key={g.label} role="group" aria-labelledby={`ng-${g.label.replace(/\W/g, "")}`}>
                <p className="nav-label" id={`ng-${g.label.replace(/\W/g, "")}`}>{g.label}</p>{items}
              </div>
            );
          })}
        </nav>
        <div className="foot">
          {local && <a className="nav" href="#/account" style={{ padding: "4px 0" }} aria-current={current === "account" ? "page" : undefined}>Your account</a>}
          <button className="awake-switch" role="switch" aria-checked={awake.wanted} onClick={awake.toggle}
            disabled={awake.state === "unsupported"} title={WAKE_TEXT[awake.state]}>
            <span className="awake-track" aria-hidden="true"><i /></span>
            <span><b>Keep screen awake</b><small>{WAKE_TEXT[awake.state]}</small></span>
          </button>
          <button className="btn quiet" onClick={toggleTheme} style={{ justifyContent: "flex-start", padding: 0, minHeight: 0 }}>Switch light / dark</button>
        </div>
      </aside>
      <main id="main" key={`${route.view}-${site?.id}`} tabIndex={-1}>{body}</main>
      <nav className="tabbar" aria-label="Main sections">
        {TABS.map(([v, icon, label]) => (
          <a key={v} href={`#/${v}`} aria-current={current === v ? "page" : undefined}><Icon name={icon} /><span>{label}</span></a>
        ))}
        <button aria-expanded={menu} aria-controls="rail" onClick={() => setMenu(true)}><Icon name="more" /><span>More</span></button>
      </nav>
      {palette && <Palette site={site} screens={SCREENS} onClose={() => setPalette(false)} />}
    </div>
  );
}

/* With QC's own sign-in (QC6_AUTH=local) nothing else loads until you're signed in. */
function Root() {
  const mode = useLoad(() => api.authMode(), []);
  if (mode.error) return <main id="main" className="signin"><ErrorBox text={mode.error} onRetry={mode.reload} /></main>;
  if (!mode.data) return null;
  const local = mode.data.mode === "local";
  const r = parseHash();
  if (local && r.view === "reset" && r.params.get("token")) return <ResetPassword token={r.params.get("token")!} />;
  if (local && !mode.data.signed_in) return <SignIn onDone={() => window.location.reload()} />;
  return <App local={local} />;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode><ToastProvider><Root /></ToastProvider></StrictMode>,
);
