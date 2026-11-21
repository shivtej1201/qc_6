"""Executive summary report (CrawlPulse FEAT-18-02): one self-contained HTML page, printed to PDF by the same local
Chromium QC already uses. No external services; no scripts in the page; the browser may not load anything.

Contents: health score and its trend, open fixes by severity, the ten fixes that matter most, what changed since the
check before, the pages worth working on first, page types, real-browser visits and journeys. Every number comes from
QC's own data and says what it is based on.
"""
from __future__ import annotations

import asyncio
import html
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

SEV_ORDER = ["critical", "high", "medium", "low"]
SEV_COLOR = {"critical": "#B42318", "high": "#C4320A", "medium": "#B54708", "low": "#3E6A8A"}
_pdf_lock = asyncio.Semaphore(2)               # at most two PDFs rendering at once per API process


def e(v: Any) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def n(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return f"{int(v):,}" if float(v).is_integer() else f"{v:,.1f}"


def _span(ttv: Dict[str, Any]) -> str:
    h = (ttv.get("median_hours") or {}).get("all")
    if h is None:
        h = (ttv.get("median_days") or {}).get("all", 0) * 24
    if h < 1:
        return "under an hour"
    return f"{round(h)} hours" if h < 48 else f"{round(h / 24)} days"


def _trend_svg(points: List[Dict[str, Any]], w: int = 520, h: int = 120) -> str:
    if len(points) < 2:
        return ""
    vals = [p["score"] for p in points]
    lo = max(0, min(vals) - 5)
    xs = [20 + i * (w - 40) / (len(vals) - 1) for i in range(len(vals))]
    ys = [h - 20 - (v - lo) / max(1, 100 - lo) * (h - 40) for v in vals]
    d = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f} {y:.1f}" for i, (x, y) in enumerate(zip(xs, ys)))
    return (f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" aria-label="Health score over the last checks">'
            f'<line x1="20" x2="{w - 20}" y1="{h - 20}" y2="{h - 20}" stroke="#D8E0DE"/>'
            f'<path d="{d}" fill="none" stroke="#008C83" stroke-width="2"/>'
            f'<circle cx="{xs[-1]:.1f}" cy="{ys[-1]:.1f}" r="4" fill="#008C83"/>'
            f'<text x="{xs[-1] - 6:.1f}" y="{ys[-1] - 8:.1f}" text-anchor="end" font-size="11" font-weight="600">{round(vals[-1])}</text>'
            f'<text x="20" y="{h - 4}" font-size="10" fill="#5E6D74">{e(str(points[0]["at"])[:10])}</text>'
            f'<text x="{w - 20}" y="{h - 4}" font-size="10" fill="#5E6D74" text-anchor="end">{e(str(points[-1]["at"])[:10])}</text></svg>')


async def gather(pool, site: Dict[str, Any]) -> Dict[str, Any]:
    from qc6.insights import site_insights
    from qc6.inventory import checks_with_pages, compare
    from qc6.opportunity import explain, page_opportunities
    ins = await site_insights(pool, site["id"], 12)
    checks = await checks_with_pages(pool, site["id"])
    cmp = None
    if len(checks) >= 2:
        cmp = await compare(pool, site["id"], checks[1]["id"], checks[0]["id"], sample=5)
    opps = (await page_opportunities(pool, site["id"], checks[0]["id"], with_reasons=0)) if checks else None
    if opps:
        opps["pages"] = explain(opps, [p for p in opps["pages"] if p["bucket"] != "monitor"][:8])
    fixes = await pool.fetch("""SELECT f.title, f.severity, f.pages_affected, f.status, m.name AS owner, f.texts->>'fix' AS how
                                FROM fixes f LEFT JOIN members m ON m.id = f.owner_id
                                WHERE f.site_id = $1 AND f.status IN ('open', 'in_progress', 'fixed_pending_check')
                                ORDER BY CASE f.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
                                         f.pages_affected DESC LIMIT 10""", site["id"])
    run = await pool.fetchrow("SELECT id, finished_at, summary FROM runs WHERE id = $1", ins["last_run_id"]) if ins["last_run_id"] else None
    return {"site": site, "insights": ins, "compare": cmp, "opps": opps, "fixes": [dict(r) for r in fixes],
            "run": dict(run) if run else None}


def render_html(d: Dict[str, Any]) -> str:
    s, ins, run = d["site"], d["insights"], d["run"]
    pts = ins["score"]["points"]
    score = pts[-1]["score"] if pts else None
    sev = {x["severity"]: x for x in ins["backlog"]["by_severity"]}
    now = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
    parts: List[str] = []
    parts.append(f"""<header><div><p class="kicker">Site quality report</p><h1>{e(s['name'])}</h1>
      <p class="sub">{e(s['base_url'])} · check #{e(run['id']) if run else '—'}, finished {e(str(run['finished_at'])[:16]) if run else '—'} · made {e(now)}</p></div>
      <div class="score"><b>{round(score) if score is not None else '—'}</b><span>health out of 100</span></div></header>""")
    if ins["headlines"]:
        parts.append("<section><h2>In short</h2><ul class='lead'>" + "".join(f"<li>{e(h)}</li>" for h in ins["headlines"]) + "</ul></section>")
    dims = (pts[-1]["dimensions"] if pts else {}) or {}
    parts.append("<section class='two'><div><h2>Health over time</h2>" + (_trend_svg(pts) or "<p class='muted'>The trend appears after the second check.</p>")
                 + "<table class='dims'>" + "".join(f"<tr><td>{e(k.title())}</td><td class='num'>{n(v)}</td></tr>" for k, v in dims.items())
                 + "</table></div><div><h2>Open fixes</h2><table class='sev'>"
                 + "".join(f"<tr><td><i style='background:{SEV_COLOR[k]}'></i>{k.title()}</td><td class='num'>{n(sev.get(k, {}).get('open', 0))}</td>"
                           f"<td class='num muted'>{n(sev.get(k, {}).get('pages', 0))} pages</td></tr>" for k in SEV_ORDER)
                 + f"</table><p class='muted'>{n(ins['backlog']['unowned'])} open fixes have no owner. "
                 + (f"Median time to confirm a fix: {_span(ins['time_to_verify'])}." if ins["time_to_verify"]["count"] else "")
                 + "</p></div></section>")
    if d["fixes"]:
        parts.append("<section><h2>The fixes that matter most</h2><table class='grid'><tr><th>Severity</th><th>What's wrong</th><th class='num'>Pages</th>"
                     "<th>Owner</th><th>Status</th></tr>"
                     + "".join(f"<tr><td><span class='sev-tag' style='color:{SEV_COLOR[f['severity']]}'>{e(f['severity'].title())}</span></td>"
                               f"<td><b>{e(f['title'])}</b><br><span class='muted'>{e((f['how'] or '')[:220])}</span></td>"
                               f"<td class='num'>{n(f['pages_affected'])}</td><td>{e(f['owner'] or 'Nobody')}</td>"
                               f"<td>{e(f['status'].replace('_', ' ').replace('fixed pending check', 'waiting for the next check'))}</td></tr>"
                               for f in d["fixes"]) + "</table></section>")
    c = d["compare"]
    if c:
        fx = c["fixes"]
        parts.append(f"""<section><h2>Since check #{e(c['a']['id'])}</h2><div class="facts">
          <div><b>{n(c['pages']['after'])}</b><span>pages now ({n(c['pages']['before'])} before)</span></div>
          <div><b>{n(c['pages']['added'])}</b><span>new pages</span></div><div><b>{n(c['pages']['removed'])}</b><span>pages gone</span></div>
          <div><b>{n(fx['new']['count'])}</b><span>new problems</span></div><div><b>{n(fx['confirmed']['count'])}</b><span>confirmed fixed</span></div>
          <div><b>{n(fx['back']['count'])}</b><span>came back</span></div></div></section>""")
    o = d["opps"]
    if o and o["pages"]:
        top = [p for p in o["pages"] if p["bucket"] != "monitor"][:8]
        if top:
            label = {b["id"]: b["label"] for b in o["buckets"]}
            parts.append("<section><h2>Worth doing first</h2><table class='grid'><tr><th class='num'>Score</th><th>Page</th><th>What to do</th><th>Why</th></tr>"
                         + "".join(f"<tr><td class='num'><b>{round(p['score'])}</b></td><td class='mono'>{e(p['url'])}</td><td>{e(label.get(p['bucket']))}</td>"
                                   f"<td>{e(' '.join(p['reasons'])[:300])}</td></tr>" for p in top)
                         + f"</table><p class='muted'>{e(o['basis'])} Based on {round(o['confidence'] * 100)}% of the model.</p></section>")
    pt = ins["page_types"]["items"][:10]
    if pt:
        parts.append("<section><h2>Page types</h2><table class='grid'><tr><th>Page type</th><th class='num'>Pages</th><th class='num'>Open fixes</th>"
                     "<th>Worst</th><th class='num'>Visited</th></tr>"
                     + "".join(f"<tr><td>{e(t['name'])}</td><td class='num'>{n(t['pages'])}</td><td class='num'>{n(t['open_fixes'])}</td>"
                               f"<td>{e((t['worst'] or 'none').title())}</td><td class='num'>{n(t['visited'])}</td></tr>" for t in pt) + "</table></section>")
    v, j = ins["visits"], ins["journeys"]["items"]
    if v.get("total") or j:
        parts.append("<section class='two'><div><h2>Real-browser visits</h2>"
                     + (f"<p>{n(v['by_status'].get('ok', 0))} of {n(v['total'])} loaded fine. "
                        + ", ".join(f"{n(r['visits'])} {e(r['reason'])}" for r in v["reasons"]) + "</p>" if v.get("total") else "<p class='muted'>No visits in the latest check.</p>")
                     + "</div><div><h2>Journeys</h2>"
                     + ("<table class='grid'>" + "".join(f"<tr><td>{e(x['name'])}</td><td class='num'>{n(x['passed'])} of {n(x['runs'])} passed</td></tr>" for x in j)
                        + "</table>" if j else "<p class='muted'>No journeys yet.</p>") + "</div></section>")
    parts.append("<footer>QC V6 · every number comes from QC's own checks of this site"
                 + (" and the site's own Search Console / GA4 data" if o and (o["data"]["search_console"] or o["data"]["ga4"]) else "")
                 + ". The health score is qcv6-health-1.0; the opportunity score is QC's prioritisation, not a Google ranking score.</footer>")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{e(s['name'])}: site quality report</title><style>
@page {{ size: A4; margin: 16mm 14mm; }}
* {{ box-sizing: border-box; }}
body {{ font: 10.5pt/1.45 "Segoe UI", system-ui, -apple-system, "Helvetica Neue", Arial, sans-serif; color: #14212B; margin: 0; }}
header {{ display: flex; justify-content: space-between; align-items: flex-end; border-bottom: 2px solid #008C83; padding-bottom: 10px; margin-bottom: 14px; }}
h1 {{ font-size: 22pt; margin: 0; line-height: 1.1; }} h2 {{ font-size: 12.5pt; margin: 0 0 8px; }}
.kicker {{ text-transform: uppercase; letter-spacing: .08em; font-size: 8pt; color: #5E6D74; margin: 0 0 4px; }}
.sub, .muted {{ color: #4A5A63; font-size: 9pt; margin: 4px 0 0; }}
.score {{ text-align: right; }} .score b {{ display: block; font-size: 34pt; line-height: 1; color: #008C83; }} .score span {{ font-size: 8.5pt; color: #4A5A63; }}
section {{ margin: 0 0 16px; page-break-inside: avoid; }}
.two {{ display: grid; grid-template-columns: 1.2fr 1fr; gap: 22px; }}
.lead {{ margin: 0; padding-left: 18px; font-size: 11pt; }}
table {{ border-collapse: collapse; width: 100%; }}
.grid th {{ text-align: left; font-size: 8pt; text-transform: uppercase; letter-spacing: .05em; color: #5E6D74; border-bottom: 1px solid #D8E0DE; padding: 4px 6px; }}
.grid td {{ border-bottom: 1px solid #EEF2F1; padding: 5px 6px; vertical-align: top; }}
.dims td, .sev td {{ padding: 3px 4px; border-bottom: 1px solid #EEF2F1; }}
.sev td:first-child {{ white-space: nowrap; }}
.sev i {{ display: inline-block; width: 8px; height: 8px; border-radius: 2px; margin-right: 6px; }}
.num {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }}
.mono {{ font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 8.5pt; word-break: break-all; }}
.sev-tag {{ font-weight: 600; }}
.facts {{ display: grid; grid-template-columns: repeat(6, 1fr); gap: 8px; }}
.facts div {{ border: 1px solid #D8E0DE; border-radius: 6px; padding: 8px; }} .facts b {{ display: block; font-size: 15pt; }} .facts span {{ font-size: 8.5pt; color: #4A5A63; }}
footer {{ border-top: 1px solid #D8E0DE; padding-top: 8px; color: #5E6D74; font-size: 8pt; }}
</style></head><body>{''.join(parts)}</body></html>"""


async def to_pdf(html_doc: str) -> bytes:
    """Print with local Chromium. Scripts off, and every request the page tries to make is refused."""
    from playwright.async_api import async_playwright
    async with _pdf_lock:
        async with async_playwright() as p:
            b = await p.chromium.launch()
            try:
                ctx = await b.new_context(java_script_enabled=False)
                await ctx.route("**/*", lambda route: route.abort())
                page = await ctx.new_page()
                await page.set_content(html_doc, wait_until="load")
                return await page.pdf(format="A4", print_background=True, margin={"top": "16mm", "bottom": "16mm", "left": "14mm", "right": "14mm"})
            finally:
                await b.close()
