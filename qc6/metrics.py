"""Prometheus metrics (blueprint §13), as plain text at /metrics. Computed from the database on each scrape, so every
API process reports the same numbers and nothing is lost on restart. Cheap queries only (indexed, last 24 h).

Suggested alerts (blueprint §13): qc6_host_paused == 1 on a primary host; qc6_oldest_queued_job_seconds > 900;
qc6_visit_failure_ratio_24h > 0.10; qc6_disk_free_bytes < 30e9.
"""
from __future__ import annotations

import shutil
from typing import Dict, Iterable, List, Tuple

from qc6.settings import settings


def _esc(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _fmt(name: str, help_: str, kind: str, samples: Iterable[Tuple[Dict[str, str], float]]) -> List[str]:
    out = [f"# HELP {name} {help_}", f"# TYPE {name} {kind}"]
    for labels, v in samples:
        lab = ",".join(f'{k}="{_esc(x)}"' for k, x in labels.items())
        out.append(f"{name}{{{lab}}} {float(v):g}" if lab else f"{name} {float(v):g}")
    return out


async def render(pool) -> str:
    lines: List[str] = []
    q = await pool.fetchrow("""SELECT count(*) FILTER (WHERE status = 'queued') AS queued,
                                      count(*) FILTER (WHERE status = 'running') AS running,
                                      coalesce(extract(epoch FROM now() - min(created_at) FILTER (WHERE status = 'queued')), 0) AS oldest
                               FROM runs WHERE status IN ('queued', 'running')""")
    lines += _fmt("qc6_runs_queued", "Checks waiting for a worker", "gauge", [({}, q["queued"])])
    lines += _fmt("qc6_runs_running", "Checks running now", "gauge", [({}, q["running"])])
    lines += _fmt("qc6_oldest_queued_job_seconds", "Age of the oldest waiting check", "gauge", [({}, q["oldest"])])
    rs = await pool.fetch("SELECT status, count(*) AS n FROM runs WHERE created_at > now() - interval '24 hours' GROUP BY status")
    lines += _fmt("qc6_runs_24h", "Checks started in the last 24 hours, by outcome", "gauge", [({"status": r["status"]}, r["n"]) for r in rs])
    running = await pool.fetch("""SELECT r.id, s.primary_host, extract(epoch FROM now() - r.started_at) AS secs,
                                         (r.progress->>'pages_done')::float AS done, (r.progress->>'pages_total')::float AS total
                                  FROM runs r JOIN sites s ON s.id = r.site_id WHERE r.status = 'running' AND r.started_at IS NOT NULL""")
    lines += _fmt("qc6_run_elapsed_seconds", "How long each running check has been going", "gauge",
                  [({"run": str(r["id"]), "host": r["primary_host"]}, r["secs"] or 0) for r in running])
    eta = []
    for r in running:
        if r["done"] and r["total"] and r["done"] < r["total"] and r["secs"]:
            eta.append(({"run": str(r["id"]), "host": r["primary_host"]}, r["secs"] / r["done"] * (r["total"] - r["done"])))
    lines += _fmt("qc6_run_eta_seconds", "Projected time left in the page-check stage (linear)", "gauge", eta)

    vs = await pool.fetch("SELECT status, count(*) AS n FROM visits WHERE started_at > now() - interval '24 hours' GROUP BY status")
    lines += _fmt("qc6_visits_24h", "Real-browser visits in the last 24 hours, by outcome", "gauge", [({"status": r["status"]}, r["n"]) for r in vs])
    tot = sum(r["n"] for r in vs)
    bad = sum(r["n"] for r in vs if r["status"] in ("failed", "challenged"))
    lines += _fmt("qc6_visit_failure_ratio_24h", "Share of visits that failed or met a bot check (last 24 h)", "gauge",
                  [({}, (bad / tot) if tot else 0)])

    hb = await pool.fetch("SELECT host, rate_rps, paused_until > now() AS paused FROM host_budget")
    lines += _fmt("qc6_host_rate_rps", "Agreed request rate per host", "gauge", [({"host": r["host"]}, r["rate_rps"]) for r in hb])
    lines += _fmt("qc6_host_paused", "1 while a host has paused QC (bot check / challenge seen)", "gauge",
                  [({"host": r["host"]}, 1 if r["paused"] else 0) for r in hb])

    fx = await pool.fetch("""SELECT severity, count(*) AS n FROM fixes WHERE status IN ('open', 'in_progress', 'fixed_pending_check')
                             GROUP BY severity""")
    lines += _fmt("qc6_fixes_open", "Open fixes across all sites, by severity", "gauge", [({"severity": r["severity"]}, r["n"]) for r in fx])
    jr = await pool.fetch("SELECT status, count(*) AS n FROM journey_runs WHERE started_at > now() - interval '24 hours' GROUP BY status")
    lines += _fmt("qc6_journey_runs_24h", "Journey runs in the last 24 hours, by outcome", "gauge", [({"status": r["status"]}, r["n"]) for r in jr])
    try:
        free = shutil.disk_usage(settings.data_dir).free
    except OSError:
        free = 0
    lines += _fmt("qc6_disk_free_bytes", "Free disk where QC keeps its data", "gauge", [({}, free)])
    return "\n".join(lines) + "\n"
