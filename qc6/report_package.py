"""The complete report of a check as ONE ZIP, stored locally, with its metadata in Postgres.

A multi-GB single workbook comes from repeating the same link once per page ("Redirect URLs": a redirecting header
link on 1.25 lakh pages = 1.25 lakh rows). Here every Excel sheet is aggregated (one row per unique URL, or per
URL x page component with the page count and example pages) and the page-by-page detail goes to CSV inside the ZIP,
where it compresses about 10x. Nothing is lost; each file is small enough to open.

    QC-report-<host>-check<id>.zip
      Crawl_Summary.xlsx           dashboard, key findings, charts (the Management report)
      Issue_Summary.xlsx           Template x Component x Issue with affected pages and examples; issues by type
      URL_Audit.xlsx               every unique URL with its status; problem links grouped by URL x component
      Image_Audit.xlsx             every unique image; problem images grouped by image x component
      Schema_Audit.xlsx            per page: title, description, H1, canonical, indexability, structured data
      Non_Domain_URL_Audit.xlsx    URLs on other hosts than the primary domain: QA / UAT / dev / other subdomains, external
      data/Pages.csv               every page crawled
      data/All_Links.csv           every page -> URL placement with status and component
      data/All_Images.csv          every page -> image placement with status
      data/Excluded_URLs.csv       URLs found but not reported (JS, CSS, tag managers, tracking...) with the reason
      seed/                        what the check was given: Input_URLs.csv (with sources), sitemaps, uploaded files
      README.txt

Written with XlsxWriter in constant-memory mode and CSV streaming: memory stays flat at any size. Sheets split at
Excel's row limit (…_2, …_3). Built after the check completes (in the worker); also on demand.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import re
import shutil
import sqlite3
import time
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from qc6.components import component
from qc6.settings import settings
from qc6.sqlite_ro import ro_uri

log = logging.getLogger("qc6.report_package")
XL_MAX = 1_000_000
EXAMPLES = 3
BROKEN = ("(c.status_bucket IN ('4xx','5xx','Timeout','DNS/Network','SSL','Robots blocked','WAF blocked','Other errors') "
          "OR (c.status_bucket = '3xx' AND (c.http_status IS NULL OR c.http_status >= 400)))")
PROBLEM = f"({BROKEN} OR COALESCE(c.redirect_count, 0) > 0 OR c.normalized_url LIKE 'http://%')"


def run_dir(run_id: int) -> str:
    return os.path.join(settings.data_dir, "runs", str(int(run_id)))


def seed_dir(list_id: int) -> str:
    return os.path.join(settings.data_dir, "lists", str(int(list_id)), "seed")


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:80] or "site"


# ------------------------------------------------------------------ writers
class _Xlsx:
    def __init__(self, path: str):
        import xlsxwriter
        self.wb = xlsxwriter.Workbook(path, {"constant_memory": True, "strings_to_urls": False,
                                             "strings_to_formulas": False, "strings_to_numbers": False})
        self.head = self.wb.add_format({"bold": True, "bg_color": "#DDF1EF", "border": 1, "text_wrap": True})
        self.rows = 0

    def sheet(self, name: str, header: Sequence[str], rows: Iterable[Sequence[Any]], widths: Sequence[int],
              empty_note: str = "") -> int:
        """Writes rows, starting a new sheet (name_2, name_3...) at Excel's limit. Returns the number of rows."""
        part, n, total, ws = 1, 0, 0, None

        def new(p: int):
            w = self.wb.add_worksheet((name if p == 1 else f"{name[:27]}_{p}")[:31])
            for i, wd in enumerate(widths):
                w.set_column(i, i, wd)
            w.write_row(0, 0, list(header), self.head)
            w.freeze_panes(1, 0)
            w.autofilter(0, 0, 0, len(header) - 1)
            return w
        ws = new(1)
        for row in rows:
            if n >= XL_MAX:
                part += 1
                ws, n = new(part), 0
            n += 1
            total += 1
            ws.write_row(n, 0, ["" if v is None else (v if isinstance(v, (int, float)) else str(v)[:32000]) for v in row])
        if total == 0 and empty_note:
            ws.write(1, 0, empty_note)
        self.rows += total
        return total

    def order(self, first: Sequence[str]) -> None:
        """Put these sheets first (written last because they're small summaries of the long sheet)."""
        objs = self.wb.worksheets_objs
        front = [w for n in first for w in objs if w.name == n]
        rest = [w for w in objs if w not in front]
        self.wb.worksheets_objs = front + rest
        for i, w in enumerate(self.wb.worksheets_objs):
            w.index = i
        if front:
            front[0].activate()

    def close(self) -> None:
        self.wb.close()


def _csv(path: str, header: Sequence[str], rows: Iterable[Sequence[Any]]) -> int:
    n = 0
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow(["" if v is None else v for v in r])
            n += 1
    return n


def _examples(s: Optional[str]) -> List[str]:
    out = [x for x in (s or "").split("\x1f") if x][:EXAMPLES]
    return out + [""] * (EXAMPLES - len(out))


# ------------------------------------------------------------------ the package
def build_files(run_id: int, host: str, templates: Dict[str, str], out_dir: str, summary_xlsx: Optional[str],
                seed: Optional[Dict[str, Any]] = None, site_for_audit: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Write every file of the package into out_dir (blocking; run in a thread)."""
    os.makedirs(os.path.join(out_dir, "data"), exist_ok=True)
    db = os.path.join(run_dir(run_id), "coverage.db")
    files: Dict[str, int] = {}
    if summary_xlsx and os.path.exists(summary_xlsx):
        shutil.copyfile(summary_xlsx, os.path.join(out_dir, "Crawl_Summary.xlsx"))
        files["Crawl_Summary.xlsx"] = 1
    if not os.path.exists(db):
        files.update(_seed(out_dir, seed))
        _readme(out_dir, host, run_id, files)
        return files
    c = sqlite3.connect(ro_uri(db), uri=True, timeout=30)
    job = f"RUN{run_id}"
    try:
        c.execute("CREATE TEMP TABLE tpl (url TEXT PRIMARY KEY, name TEXT)")
        c.executemany("INSERT OR IGNORE INTO temp.tpl VALUES (?, ?)", templates.items())
        has = lambda t: bool(c.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (t,)).fetchone())  # noqa: E731
        tpl_sql = "COALESCE(t.name, NULLIF(s.page_type, ''), 'Unclassified')"

        # ---------------- Issue_Summary.xlsx: Template x Component x Issue
        from qc6.internal_tickets import humanize
        agg: Dict[tuple, Dict[str, Any]] = {}
        for rule, sev, cat, et, loc, tmpl, page, found in c.execute(f"""
                SELECT i.rule_code, i.severity, i.category, i.element_type, COALESCE(i.location, ''), {tpl_sql}, i.page_url,
                       COALESCE(i.found_url, '')
                  FROM on_page_issues i LEFT JOIN on_page_source_pages s ON s.job_id = i.job_id AND s.source_page_url = i.page_url
                  LEFT JOIN temp.tpl t ON t.url = i.page_url WHERE i.job_id = ?""", (job,)):
            comp = component(rule, et, loc)
            k = (tmpl, comp, rule)
            a = agg.get(k)
            if a is None:
                a = agg[k] = {"sev": sev, "cat": cat, "pages": set(), "occ": 0, "urls": set(), "ex_pages": [], "ex_urls": []}
            a["occ"] += 1
            if page not in a["pages"]:
                a["pages"].add(page)
                if len(a["ex_pages"]) < EXAMPLES:
                    a["ex_pages"].append(page)
            if found and found not in a["urls"]:
                if len(a["urls"]) < 100_000:
                    a["urls"].add(found)
                if len(a["ex_urls"]) < EXAMPLES:
                    a["ex_urls"].append(found)
        if has("qc_schema_checks"):                    # schema + EMI problems (qc6.schema_check), same grouping
            for page, tmpl, probs, emi, emi_note in c.execute(f"""
                    SELECT q.page_url, {tpl_sql}, q.problems, q.emi_status, q.emi_note FROM qc_schema_checks q
                      LEFT JOIN on_page_source_pages s ON s.job_id = q.job_id AND s.source_page_url = q.page_url
                      LEFT JOIN temp.tpl t ON t.url = q.page_url WHERE q.job_id = ?""", (job,)):
                entries = []
                for part in [x for x in (probs or "").split("; ") if x]:
                    path, _, rest = part.partition(" ")
                    kind = rest.split(" ", 1)[0]
                    entries.append(("SCHEMA_FIELD_MISSING" if kind == "missing" else "SCHEMA_VALUE_INVALID",
                                    f"{path.split('.', 1)[0]} schema", path, "MEDIUM"))
                if emi in ("missing", "invalid"):
                    entries.append((f"EMI_{emi.upper()}", "EMI", emi_note or "", "MEDIUM"))
                for rule, comp, found, sev in entries:
                    k = (tmpl, comp, rule if not found or rule.startswith("EMI") else f"{rule}:{found}")
                    a = agg.get(k)
                    if a is None:
                        a = agg[k] = {"sev": sev, "cat": "SCHEMA" if rule.startswith("SCHEMA") else "EMI", "pages": set(),
                                      "occ": 0, "urls": set(), "ex_pages": [], "ex_urls": []}
                    a["occ"] += 1
                    if page not in a["pages"]:
                        a["pages"].add(page)
                        if len(a["ex_pages"]) < EXAMPLES:
                            a["ex_pages"].append(page)
        sev_rank = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
        rows = sorted(agg.items(), key=lambda kv: (sev_rank.get(str(kv[1]["sev"]).upper(), 9), -len(kv[1]["pages"])))
        x = _Xlsx(os.path.join(out_dir, "Issue_Summary.xlsx"))
        files["Issue_Summary.xlsx"] = x.sheet(
            "Template-wise issues", ["Template", "Component", "Issue", "Rule", "Severity", "Category", "Affected pages",
                                     "Occurrences", "Different URLs"] + [f"Example page {i}" for i in range(1, EXAMPLES + 1)]
            + [f"Example URL {i}" for i in range(1, EXAMPLES + 1)],
            ([k[0], k[1], humanize(k[2].split(":")[0]) + (f": {k[2].split(':', 1)[1]}" if ":" in k[2] else ""), k[2], v["sev"],
              v["cat"], len(v["pages"]), v["occ"], len(v["urls"])]
             + (v["ex_pages"] + [""] * EXAMPLES)[:EXAMPLES] + (v["ex_urls"] + [""] * EXAMPLES)[:EXAMPLES] for k, v in rows),
            [26, 26, 34, 26, 10, 14, 14, 12, 14] + [60] * (2 * EXAMPLES), "No issues found.")
        by_rule: Dict[str, Dict[str, Any]] = {}
        for (tmpl, comp, rule), v in agg.items():
            b = by_rule.setdefault(rule, {"sev": v["sev"], "cat": v["cat"], "pages": set(), "occ": 0, "tpl": set(), "comp": set()})
            b["pages"] |= v["pages"]
            b["occ"] += v["occ"]
            b["tpl"].add(tmpl)
            b["comp"].add(comp)
        x.sheet("Issues by type", ["Issue", "Rule", "Severity", "Category", "Affected pages", "Occurrences", "Templates", "Components"],
                ([humanize(r), r, v["sev"], v["cat"], len(v["pages"]), v["occ"], ", ".join(sorted(v["tpl"]))[:500],
                  ", ".join(sorted(v["comp"]))[:500]]
                 for r, v in sorted(by_rule.items(), key=lambda kv: (sev_rank.get(str(kv[1]["sev"]).upper(), 9), -len(kv[1]["pages"])))),
                [34, 26, 10, 14, 14, 12, 50, 50])
        x.close()
        del agg, by_rule

        # ---------------- URL_Audit.xlsx
        x = _Xlsx(os.path.join(out_dir, "URL_Audit.xlsx"))
        files["URL_Audit.xlsx"] = x.sheet(
            "Unique URLs", ["URL", "Scope", "First status", "Final status", "Status bucket", "Final URL", "Redirects",
                            "Remark", "Error", "Response ms", "Robots", "Found on (pages)", "First found on", "Checked"],
            c.execute("""SELECT c.normalized_url, c.url_scope, c.initial_http_status, c.http_status, c.status_bucket, c.final_url,
                                c.redirect_count, c.remark, c.error_message, ROUND(c.response_time_ms), c.robots_status,
                                (SELECT COUNT(*) FROM on_page_link_relationships r INDEXED BY idx_oplr_dest
                                  WHERE r.job_id = c.job_id AND r.normalized_found_url = c.normalized_url),
                                c.first_seen_source_url, CASE WHEN c.seeded_by = 'PREVIOUS_CHECK' THEN 'reused from previous check' ELSE 'checked' END
                           FROM on_page_validation_cache c WHERE c.job_id = ? AND c.referenced = 1
                          ORDER BY c.status_bucket = '2xx', c.normalized_url""", (job,)),
            [70, 10, 10, 10, 12, 60, 9, 40, 40, 11, 12, 14, 60, 18])
        x.sheet("Problem links by component", ["URL", "Status", "Status bucket", "Final URL", "Template", "Component",
                                               "Link type", "Pages"] + [f"Example page {i}" for i in range(1, EXAMPLES + 1)],
                ([u, st, bk, fu, tm, comp, lt, n] + _examples(ex) for u, st, bk, fu, tm, comp, lt, n, ex in c.execute(f"""
                    SELECT r.normalized_found_url, c.http_status, c.status_bucket, c.final_url, {tpl_sql},
                           COALESCE(NULLIF(r.representative_location, ''), 'Content Section'), r.found_url_type,
                           COUNT(DISTINCT r.source_page_url), substr(GROUP_CONCAT(r.source_page_url, char(31)), 1, 3000)
                      FROM on_page_link_relationships r JOIN on_page_validation_cache c
                        ON c.job_id = r.job_id AND c.normalized_url = r.normalized_found_url
                      LEFT JOIN on_page_source_pages s ON s.job_id = r.job_id AND s.source_page_url = r.source_page_url
                      LEFT JOIN temp.tpl t ON t.url = r.source_page_url
                     WHERE r.job_id = ? AND r.found_url_scope IN ('INTERNAL','EXTERNAL') AND {PROBLEM}
                     GROUP BY 1, 5, 6, 7 ORDER BY 8 DESC""", (job,))),
                [70, 9, 12, 60, 24, 24, 12, 10] + [60] * EXAMPLES, "No broken, redirecting or http:// links.")
        x.close()

        # ---------------- Image_Audit.xlsx
        x = _Xlsx(os.path.join(out_dir, "Image_Audit.xlsx"))
        img_ok = has("on_page_image_validation_cache")
        files["Image_Audit.xlsx"] = x.sheet(
            "Unique images", ["Image URL", "Type", "Scope", "First status", "Final status", "Status bucket", "Content type",
                              "Bytes", "Image issue", "Remark", "Robots", "Response ms", "First found on"],
            c.execute("""SELECT normalized_url, image_type, internal_external, initial_http_status, http_status, status_bucket,
                                content_type, content_length, image_issue, remark, robots_status, ROUND(response_time_ms),
                                first_seen_source_url FROM on_page_image_validation_cache WHERE job_id = ?
                          ORDER BY status_bucket = '2xx', normalized_url""", (job,)) if img_ok else [],
            [70, 12, 14, 10, 10, 12, 18, 10, 24, 40, 12, 11, 60])
        if img_ok:
            x.sheet("Problem images by component", ["Image URL", "Status", "Status bucket", "Image issue", "Template",
                                                    "Component", "Pages"] + [f"Example page {i}" for i in range(1, EXAMPLES + 1)],
                    ([u, st, bk, iss, tm, comp, n] + _examples(ex) for u, st, bk, iss, tm, comp, n, ex in c.execute(f"""
                        SELECT i.normalized_image_url, c.http_status, c.status_bucket, c.image_issue, {tpl_sql},
                               COALESCE(NULLIF(i.representative_location, ''), 'Content Section'),
                               COUNT(DISTINCT i.source_page_url), substr(GROUP_CONCAT(i.source_page_url, char(31)), 1, 3000)
                          FROM on_page_image_relationships i JOIN on_page_image_validation_cache c
                            ON c.job_id = i.job_id AND c.normalized_url = i.normalized_image_url
                          LEFT JOIN on_page_source_pages s ON s.job_id = i.job_id AND s.source_page_url = i.source_page_url
                          LEFT JOIN temp.tpl t ON t.url = i.source_page_url
                         WHERE i.job_id = ? AND ({BROKEN} OR COALESCE(c.redirect_count, 0) > 0 OR COALESCE(c.image_issue, '') != ''
                               OR c.normalized_url LIKE 'http://%')
                         GROUP BY 1, 5, 6 ORDER BY 7 DESC""", (job,))),
                    [70, 9, 12, 24, 24, 24, 10] + [60] * EXAMPLES, "No broken, redirecting or http:// images.")
        x.close()

        # ---------------- Schema_Audit.xlsx (per page)
        x = _Xlsx(os.path.join(out_dir, "Schema_Audit.xlsx"))
        if has("on_page_seo_pages"):
            cols = {r[1] for r in c.execute("PRAGMA table_info(on_page_seo_pages)")}
            want = [("title", "Title"), ("title_length", "Title length"), ("meta_description_length", "Description length"),
                    ("h1_count", "H1 count"), ("canonical_href", "Canonical"), ("indexable", "Indexable"),
                    ("meta_robots", "Meta robots"), ("schema_types", "Schema types"), ("jsonld_blocks", "JSON-LD blocks"),
                    ("jsonld_invalid", "Invalid JSON-LD"), ("og_title", "OG title"), ("og_image", "OG image"),
                    ("hreflang_count", "Hreflang"), ("word_count", "Words"), ("insecure_resources", "http:// resources")]
            want += [(k, k.replace("_", " ").capitalize()) for k in sorted(cols) if k.startswith(("product_", "emi_", "schema_check"))]
            have = [(k, h) for k, h in want if k in cols]
            files["Schema_Audit.xlsx"] = x.sheet(
                "Pages", ["Page", "Template"] + [h for _, h in have],
                c.execute(f"""SELECT p.source_page_url, {tpl_sql}, {', '.join('p.' + k for k, _ in have)}
                                FROM on_page_seo_pages p LEFT JOIN on_page_source_pages s
                                  ON s.job_id = p.job_id AND s.source_page_url = p.source_page_url
                                LEFT JOIN temp.tpl t ON t.url = p.source_page_url WHERE p.job_id = ?
                               ORDER BY p.source_page_url""", (job,)),
                [70, 24] + [18] * len(have))
        else:
            files["Schema_Audit.xlsx"] = x.sheet("Pages", ["Page"], [], [70], "No page facts in this check.")
        if has("qc_schema_checks"):
            x.sheet("Schema & EMI", ["Page", "Template", "Schema types", "Product", "Offer", "Price", "Currency",
                                     "Schema problems", "Problems", "EMI", "EMI found in", "EMI amount", "EMI note"],
                    c.execute(f"""SELECT q.page_url, {tpl_sql}, q.schema_types, CASE q.product WHEN 1 THEN 'yes' ELSE 'no' END,
                                         CASE q.offer WHEN 1 THEN 'yes' ELSE 'no' END, q.price, q.currency, q.problem_count,
                                         q.problems, q.emi_status, q.emi_sources, q.emi_amount, q.emi_note
                                    FROM qc_schema_checks q LEFT JOIN on_page_source_pages s
                                      ON s.job_id = q.job_id AND s.source_page_url = q.page_url
                                    LEFT JOIN temp.tpl t ON t.url = q.page_url WHERE q.job_id = ?
                                   ORDER BY q.problem_count DESC, q.page_url""", (job,)),
                    [70, 24, 40, 9, 8, 12, 9, 10, 80, 14, 30, 12, 40])
        x.close()

        # ---------------- Non_Domain_URL_Audit.xlsx (additive: QA / UAT / dev / other hosts found on the pages)
        if site_for_audit is not None:
            from qc6 import host_audit
            res = host_audit.write_report(run_id, site_for_audit, templates, os.path.join(out_dir, "Non_Domain_URL_Audit.xlsx"))
            files["Non_Domain_URL_Audit.xlsx"] = res["rows"]

        # ---------------- CSV detail
        d = os.path.join(out_dir, "data")
        files["data/Pages.csv"] = _csv(os.path.join(d, "Pages.csv"), [
            "page", "template", "state", "first_status", "final_status", "final_url", "redirects", "title", "response_ms",
            "links_found", "links_reported", "links_excluded", "images_found", "images_reported", "error", "remark", "checked_at"],
            c.execute(f"""SELECT s.source_page_url, {tpl_sql}, s.crawl_state, s.initial_http_status, s.http_status, s.final_url,
                                 s.redirect_count, s.page_title, ROUND(s.response_time_ms),
                                 {"COALESCE(s.discovered_url_count, 0)" if _col(c, "on_page_source_pages", "discovered_url_count") else "''"},
                                 s.raw_occurrences_count,
                                 {"COALESCE(s.excluded_url_occurrences, 0)" if _col(c, "on_page_source_pages", "excluded_url_occurrences") else "''"},
                                 {"COALESCE(s.discovered_image_count, 0)" if _col(c, "on_page_source_pages", "discovered_image_count") else "''"},
                                 s.raw_image_occurrences_count, s.error_message, s.remark, s.checked_at
                            FROM on_page_source_pages s LEFT JOIN temp.tpl t ON t.url = s.source_page_url
                           WHERE s.job_id = ? ORDER BY s.source_page_id""", (job,)))
        files["data/All_Links.csv"] = _csv(os.path.join(d, "All_Links.csv"), [
            "page", "url", "url_as_written", "type", "scope", "component", "anchor_text", "occurrences", "status",
            "status_bucket", "final_url", "redirects", "validation"],
            c.execute("""SELECT r.source_page_url, r.normalized_found_url, r.found_url, r.found_url_type, r.found_url_scope,
                                r.representative_location, r.anchor_text, r.occurrence_count, c.http_status, c.status_bucket,
                                c.final_url, c.redirect_count, r.validation_source
                           FROM on_page_link_relationships r LEFT JOIN on_page_validation_cache c
                             ON c.job_id = r.job_id AND c.normalized_url = r.normalized_found_url
                          WHERE r.job_id = ? ORDER BY r.id""", (job,)))
        if has("on_page_image_relationships"):
            files["data/All_Images.csv"] = _csv(os.path.join(d, "All_Images.csv"), [
                "page", "image_url", "image_as_written", "type", "scope", "component", "alt_present", "alt_text", "occurrences",
                "status", "status_bucket", "image_issue"],
                c.execute("""SELECT i.source_page_url, i.normalized_image_url, i.image_url, i.image_type, i.internal_external,
                                    i.representative_location, i.alt_present, i.alt_text, i.occurrence_count, c.http_status,
                                    c.status_bucket, c.image_issue
                               FROM on_page_image_relationships i LEFT JOIN on_page_image_validation_cache c
                                 ON c.job_id = i.job_id AND c.normalized_url = i.normalized_image_url
                              WHERE i.job_id = ? ORDER BY i.id""", (job,)))
        if has("on_page_excluded_urls"):
            files["data/Excluded_URLs.csv"] = _csv(os.path.join(d, "Excluded_URLs.csv"), [
                "page", "kind", "url_as_written", "url", "type", "reason", "occurrences", "component"],
                c.execute("""SELECT source_page_url, kind, found_url, normalized_url, url_type, reason, occurrence_count, location
                               FROM on_page_excluded_urls WHERE job_id = ? ORDER BY id""", (job,)))
    finally:
        c.close()
    files.update(_seed(out_dir, seed))
    _readme(out_dir, host, run_id, files)
    return files


def _col(c: sqlite3.Connection, table: str, col: str) -> bool:
    return col in {r[1] for r in c.execute(f"PRAGMA table_info({table})")}


def _seed(out_dir: str, seed: Optional[Dict[str, Any]]) -> Dict[str, int]:
    """The check's input: every queued URL with its sources, the sitemaps, and the original uploaded files / text."""
    if not seed:
        return {}
    d = os.path.join(out_dir, "seed")
    os.makedirs(d, exist_ok=True)
    files: Dict[str, int] = {}
    files["seed/Input_URLs.csv"] = _csv(os.path.join(d, "Input_URLs.csv"), ["#", "url", "sources", "first_seen_at"], seed.get("rows") or [])
    if seed.get("sitemaps"):
        with open(os.path.join(d, "sitemaps.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(seed["sitemaps"]) + "\n")
        files["seed/sitemaps.txt"] = len(seed["sitemaps"])
    src = seed.get("dir")
    if src and os.path.isdir(src):
        for name in sorted(os.listdir(src)):
            p = os.path.join(src, name)
            if os.path.isfile(p):
                shutil.copyfile(p, os.path.join(d, name))
                files[f"seed/{name}"] = 1
    return files


def _readme(out_dir: str, host: str, run_id: int, files: Dict[str, int]) -> None:
    lines = [f"QC report for {host}, check {run_id}", f"Made {datetime.now(timezone.utc):%d %b %Y %H:%M UTC}", "",
             "Start with Crawl_Summary.xlsx (dashboard) and Issue_Summary.xlsx (Template x Component x Issue).",
             "Excel sheets are grouped (one row per URL or per URL x page component, with the page count and examples);",
             "every page-by-page row is in data/*.csv. seed/ is what the check was given.", "", "Files (rows):"]
    lines += [f"  {k}: {v:,}" for k, v in sorted(files.items())]
    with open(os.path.join(out_dir, "README.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def zip_dir(src: str, dest: str) -> int:
    tmp = dest + ".part"
    n = 0
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as z:
        top = os.path.basename(src.rstrip(os.sep))
        for root, _, names in os.walk(src):
            for name in sorted(names):
                p = os.path.join(root, name)
                # xlsx are zips already: store them, deflate the CSV / text (about 10x)
                z.write(p, os.path.join(top, os.path.relpath(p, src)),
                        compress_type=zipfile.ZIP_STORED if name.endswith(".xlsx") else zipfile.ZIP_DEFLATED)
                n += 1
    with zipfile.ZipFile(tmp) as z:                       # complete before it's offered
        bad = z.testzip()
        if bad:
            raise RuntimeError(f"the ZIP is damaged ({bad})")
    os.replace(tmp, dest)
    return n


# ------------------------------------------------------------------ orchestration + metadata
async def build_for_run(pool, run_id: int, site: Dict[str, Any]) -> Dict[str, Any]:
    """Build the package of a check, store it under runs/<id>/package/ and record it in `reports`."""
    import asyncio
    host = site.get("primary_host") or "site"
    base = f"QC-report-{_safe(host)}-check{run_id}"
    pkg_root = os.path.join(run_dir(run_id), "package")
    out_dir = os.path.join(pkg_root, base)
    zip_path = os.path.join(pkg_root, base + ".zip")
    rid = await pool.fetchval("""INSERT INTO reports (run_id, site_id, kind, file_name, path, status)
                                 VALUES ($1, $2, 'package', $3, $4, 'building') RETURNING id""",
                              run_id, site["id"], base + ".zip", zip_path)
    t0 = time.perf_counter()
    summary = None
    try:
        templates = {r["url"]: r["name"] for r in await pool.fetch(
            "SELECT p.url, t.name FROM pages p JOIN templates t ON t.id = p.template_id WHERE p.run_id = $1", run_id)}
        try:
            from qc6 import smt_report
            summary = await smt_report.report_file(pool, site, run_id)
        except Exception:                                   # noqa: BLE001 - the rest of the package still helps
            log.warning("management report for run %s failed", run_id, exc_info=True)
        seed = await seed_for_run(pool, run_id)
        if os.path.isdir(out_dir):
            shutil.rmtree(out_dir)
        files = await asyncio.to_thread(build_files, run_id, host, templates, out_dir, summary, seed, site)
        n = await asyncio.to_thread(zip_dir, out_dir, zip_path)
        size = os.path.getsize(zip_path)
        await pool.execute("""UPDATE reports SET status = 'ready', size_bytes = $2, files = $3, compressed = true, finished_at = now(),
                              detail = $4 WHERE id = $1""", rid, size, n,
                           {"rows": files, "seconds": round(time.perf_counter() - t0, 1), "folder": out_dir})
        await pool.execute("UPDATE reports SET status = 'replaced' WHERE run_id = $1 AND kind = 'package' AND id < $2 AND status = 'ready'",
                           run_id, rid)
        return {"id": rid, "path": zip_path, "size": size, "files": n, "rows": files}
    except Exception as e:
        log.exception("report package for run %s failed", run_id)
        await pool.execute("UPDATE reports SET status = 'failed', error = $2, finished_at = now() WHERE id = $1",
                           rid, f"{type(e).__name__}: {e}"[:500])
        raise
    finally:
        if summary and os.path.exists(summary):
            os.unlink(summary)


async def seed_for_run(pool, run_id: int) -> Optional[Dict[str, Any]]:
    row = await pool.fetchrow("SELECT id, urls, sitemaps, label, source FROM url_lists WHERE run_id = $1 ORDER BY id DESC LIMIT 1", run_id)
    if row is None:
        opts = await pool.fetchval("SELECT options FROM runs WHERE id = $1", run_id) or {}
        urls = (opts or {}).get("urls") or []
        return {"rows": [[i, u, "", ""] for i, u in enumerate(urls, 1)]} if urls else None
    from qc6 import jobs
    rows = await jobs.url_rows(pool, row["id"], dict(row))
    sitemaps = [s for s in (row["sitemaps"] or []) if str(s).startswith(("http://", "https://"))]
    if row["source"] in ("sitemap", "mixed") and str(row["label"]).startswith(("http://", "https://")):
        sitemaps = list(dict.fromkeys([row["label"]] + sitemaps))
    return {"rows": rows, "sitemaps": sitemaps, "dir": seed_dir(row["id"])}


async def latest(pool, run_id: int) -> Optional[Dict[str, Any]]:
    r = await pool.fetchrow("""SELECT * FROM reports WHERE run_id = $1 AND kind = 'package' AND status IN ('ready', 'building')
                               ORDER BY id DESC LIMIT 1""", run_id)
    return dict(r) if r else None
