"""Non-Domain / environment URL audit: an extra report, nothing else changes.

It only READS what the crawl already recorded (every reportable URL and image found on each page, with its page
component and validation status) and classifies the host of each one against the site's host policy:

  Primary              the crawl's own host, the primary domain and www.<primary domain>
  Allowed subdomain    a subdomain of the primary domain listed in allowed_subdomains (e.g. api, images, cdn*)
  QA / UAT / Dev / staging
                       a subdomain whose labels match qa / uat / dev-staging patterns (bmqa, bmuat, qa-www, dev2,
                       staging, preprod, sit...), or localhost / an IP address
  Other subdomain      any other subdomain of the primary domain (listed for review; add it to allowed_subdomains
                       when it's a real production host)
  External             any other domain

Everything that isn't Primary goes in Non_Domain_URL_Audit.xlsx (in the report ZIP, and on its own):
  Summary, Issues (QA / UAT / Dev URLs aggregated by Classification x Host x Template x Component, with the affected
  pages and examples: one row = one ticket), Hosts, and every page -> URL row with its status.
QA / UAT / Dev hosts also become problems (ENV_URL_QA / _UAT / _DEV) grouped by Template + Component like every
other issue, so the Ops team gets one ticket per logical issue. They're left out of the health score, so the
existing score doesn't move. The exclusions (JS, CSS, tag managers, tracking...) still apply: those URLs aren't
audited here.

Site setting `host_policy` (all optional):
  {"primary_domain": "bajajfinserv.in",
   "allowed_subdomains": ["api", "images", "cdn*"],
   "qa_patterns": [...], "uat_patterns": [...], "dev_patterns": [...],     # globs on each subdomain label
   "flag": ["QA", "UAT", "Dev / staging"],                                  # which classes become problems
   "external_allowed": ["facebook.com"]}                                    # reported as "External (allowed)"
"""
from __future__ import annotations

import fnmatch
import ipaddress
import os
import sqlite3
import urllib.parse
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from qc6.components import component as component_of
from qc6.settings import settings
from qc6.sqlite_ro import ro_uri

QA = ["qa", "qa-*", "qa[0-9]*", "*-qa", "*-qa-*", "*qa", "*qa[0-9]", "*qa[0-9][0-9]", "sqa*", "*-sqa"]
UAT = ["uat", "uat-*", "uat[0-9]*", "*-uat", "*-uat-*", "*uat", "*uat[0-9]", "*uat[0-9][0-9]"]
DEV = ["dev", "dev-*", "dev[0-9]*", "*-dev", "*-dev-*", "*dev", "*dev[0-9]", "develop*", "stag", "stage", "stage-*",
       "stage[0-9]*", "*-stage", "*stage", "staging*", "*-staging", "*staging", "stg", "stg-*", "*-stg", "*stg", "stg[0-9]*",
       "preprod*", "*-preprod", "*preprod", "pre-prod*", "sit", "sit[0-9]*", "sit-*", "*-sit", "sandbox*", "*-sandbox",
       "test", "test-*", "test[0-9]*", "*-test", "*-test-*", "demo", "demo-*", "local", "localhost", "int", "int-*", "*-int"]
ISSUE = {"QA": "QA URL found", "UAT": "UAT URL found", "Dev / staging": "Development / staging URL found",
         "Other subdomain": "Subdomain not in the allowed list", "Allowed subdomain": "Allowed subdomain",
         "External": "External URL", "External (allowed)": "External URL (allowed)"}
RULE = {"QA": "ENV_URL_QA", "UAT": "ENV_URL_UAT", "Dev / staging": "ENV_URL_DEV", "Other subdomain": "ENV_URL_OTHER_SUBDOMAIN"}
REMARK = {
    "QA": "A QA environment URL on a production page: replace it with the production URL",
    "UAT": "A UAT environment URL on a production page: replace it with the production URL",
    "Dev / staging": "A development / staging URL on a production page: replace it with the production URL",
    "Other subdomain": "A subdomain of the primary domain that isn't in the allowed list: check it's meant to be linked",
    "Allowed subdomain": "Allowed production subdomain",
    "External": "External URL", "External (allowed)": "External URL on the allowed list",
}


def registrable(host: str) -> str:
    h = (host or "").lower().split(":")[0].strip(".")
    parts = h.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "gov", "edu", "ac") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else h


class Policy:
    def __init__(self, primary_host: str, cfg: Optional[Dict[str, Any]] = None):
        cfg = dict(cfg or {})
        self.primary_host = (primary_host or "").lower().split(":")[0]
        self.domain = str(cfg.get("primary_domain") or registrable(self.primary_host)).lower().strip(".")
        self.allowed = [str(x).lower().strip(".") for x in cfg.get("allowed_subdomains") or []]
        self.qa = [str(x).lower() for x in cfg.get("qa_patterns") or QA]
        self.uat = [str(x).lower() for x in cfg.get("uat_patterns") or UAT]
        self.dev = [str(x).lower() for x in cfg.get("dev_patterns") or DEV]
        self.flag = [str(x) for x in cfg.get("flag") or ["QA", "UAT", "Dev / staging"]]
        self.ext_ok = [str(x).lower().strip(".") for x in cfg.get("external_allowed") or []]
        self._cache: Dict[str, str] = {}

    def describe(self) -> Dict[str, Any]:
        return {"primary_domain": self.domain, "primary_host": self.primary_host, "allowed_subdomains": self.allowed,
                "qa_patterns": self.qa, "uat_patterns": self.uat, "dev_patterns": self.dev, "flag": self.flag,
                "external_allowed": self.ext_ok}

    @staticmethod
    def _any(label: str, pats: Sequence[str]) -> bool:
        return any(fnmatch.fnmatchcase(label, p) for p in pats)

    def classify(self, host: str) -> str:
        h = (host or "").lower().split(":")[0].strip(".")
        c = self._cache.get(h)
        if c is not None:
            return c
        c = self._classify(h)
        if len(self._cache) < 100_000:
            self._cache[h] = c
        return c

    def _classify(self, h: str) -> str:
        if not h:
            return "External"
        if h in (self.primary_host, self.domain, "www." + self.domain):
            return "Primary"
        try:
            ipaddress.ip_address(h)
            return "Dev / staging"                   # a bare IP address on a production page
        except ValueError:
            pass
        if h == "localhost" or h.endswith((".local", ".localhost", ".internal")):
            return "Dev / staging"
        if h.endswith("." + self.domain):
            sub = h[: -(len(self.domain) + 1)]
            labels = sub.split(".")
            if self._any(sub, self.allowed) or any(self._any(l, self.allowed) for l in labels[:1]):
                return "Allowed subdomain"
            for name, pats in (("QA", self.qa), ("UAT", self.uat), ("Dev / staging", self.dev)):
                if any(self._any(l, pats) for l in labels):
                    return name
            return "Other subdomain"
        if any(h == d or h.endswith("." + d) for d in self.ext_ok):
            return "External (allowed)"
        return "External"


def policy_for(site: Dict[str, Any]) -> Policy:
    return Policy(site.get("primary_host") or "", (site.get("settings") or {}).get("host_policy"))


def _host(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _rows(c: sqlite3.Connection, job: str, pol: Policy, templates: Dict[str, str]):
    """Every non-primary page -> URL / image placement: (page, template, component, kind, found, url, host, class,
    scheme, status, bucket, final_url, first_seen, occurrences, url_type)."""
    tpl_cache: Dict[str, str] = {}
    page_type = dict(c.execute("SELECT source_page_url, COALESCE(NULLIF(page_type, ''), '') FROM on_page_source_pages WHERE job_id = ?",
                               (job,)).fetchall())

    def tpl(page: str) -> str:
        t = tpl_cache.get(page)
        if t is None:
            t = templates.get(page) or page_type.get(page) or "Unclassified"
            if len(tpl_cache) < 200_000:
                tpl_cache[page] = t
        return t
    for page, found, url, ut, loc, occ, st, bk, fin, first in c.execute("""
            SELECT r.source_page_url, r.found_url, r.normalized_found_url, r.found_url_type, COALESCE(r.representative_location, ''),
                   r.occurrence_count, c.http_status, c.status_bucket, c.final_url, c.first_seen_source_url
              FROM on_page_link_relationships r LEFT JOIN on_page_validation_cache c
                ON c.job_id = r.job_id AND c.normalized_url = r.normalized_found_url
             WHERE r.job_id = ? AND r.found_url_scope IN ('INTERNAL', 'EXTERNAL') ORDER BY r.id""", (job,)):
        h = _host(url)
        cls = pol.classify(h)
        if cls == "Primary":
            continue
        yield (page, tpl(page), component_of("", ut, loc), "link", found, url, h, cls, url.split(":", 1)[0].lower(),
               st, bk, fin, first, occ, ut)
    if c.execute("SELECT 1 FROM sqlite_master WHERE name = 'on_page_image_relationships'").fetchone():
        for page, found, url, loc, occ, st, bk, fin, first in c.execute("""
                SELECT i.source_page_url, i.image_url, i.normalized_image_url, COALESCE(i.representative_location, ''),
                       i.occurrence_count, c.http_status, c.status_bucket, c.final_url, c.first_seen_source_url
                  FROM on_page_image_relationships i LEFT JOIN on_page_image_validation_cache c
                    ON c.job_id = i.job_id AND c.normalized_url = i.normalized_image_url
                 WHERE i.job_id = ? AND i.image_type IN ('HTTP_IMAGE', 'HTTPS_IMAGE') ORDER BY i.id""", (job,)):
            h = _host(url)
            cls = pol.classify(h)
            if cls == "Primary":
                continue
            yield (page, tpl(page), f"Images · {loc}" if loc else "Images", "image", found, url, h, cls,
                   url.split(":", 1)[0].lower(), st, bk, fin, first, occ, "IMAGE")


def _remark(cls: str, scheme: str, st: Any, fin: Any, url: str) -> str:
    r = REMARK.get(cls, cls)
    if scheme == "http":
        r += "; uses http://"
    if fin and fin != url:
        fh = _host(str(fin))
        r += f"; redirects to {fh}" if fh and fh != _host(url) else "; redirects"
    if st is None:
        r += "; not validated (not requested or pending)"
    return r


def write_report(run_id: int, site: Dict[str, Any], templates: Dict[str, str], path: str,
                 pol: Optional[Policy] = None) -> Dict[str, Any]:
    """Non_Domain_URL_Audit.xlsx (blocking; run in a thread). Returns counts per classification and issue rows."""
    from qc6.report_package import EXAMPLES, _Xlsx
    pol = pol or policy_for(site)
    db = os.path.join(settings.data_dir, "runs", str(int(run_id)), "coverage.db")
    job = f"RUN{run_id}"
    counts: Dict[str, int] = {}
    hosts: Dict[Tuple[str, str], Dict[str, Any]] = {}
    issues: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
    x = _Xlsx(path)
    if not os.path.exists(db):
        x.sheet("Summary", ["Item", "Value"], [["Primary domain", pol.domain], ["Note", "This check has no crawl data."]], [30, 80])
        x.close()
        return {"counts": counts, "issues": 0, "rows": 0}
    c = sqlite3.connect(ro_uri(db), uri=True, timeout=30)
    try:
        def detail():
            for (page, tmpl, comp, kind, found, url, h, cls, scheme, st, bk, fin, first, occ, ut) in _rows(c, job, pol, templates):
                counts[cls] = counts.get(cls, 0) + 1
                hk = (h, cls)
                hr = hosts.get(hk)
                if hr is None:
                    hr = hosts[hk] = {"pages": set(), "urls": set(), "occ": 0, "status": {}}
                if len(hr["pages"]) < 1_000_000:
                    hr["pages"].add(page)
                if len(hr["urls"]) < 200_000:
                    hr["urls"].add(url)
                hr["occ"] += int(occ or 1)
                sk = str(st if st is not None else "-")
                hr["status"][sk] = hr["status"].get(sk, 0) + 1
                if cls in RULE:
                    ik = (cls, h, tmpl, comp)
                    it = issues.get(ik)
                    if it is None:
                        it = issues[ik] = {"pages": set(), "urls": set(), "occ": 0, "ex_pages": [], "ex_urls": [], "http": 0}
                    if page not in it["pages"]:
                        it["pages"].add(page)
                        if len(it["ex_pages"]) < EXAMPLES:
                            it["ex_pages"].append(page)
                    if url not in it["urls"]:
                        it["urls"].add(url)
                        if len(it["ex_urls"]) < EXAMPLES:
                            it["ex_urls"].append(url)
                    it["occ"] += int(occ or 1)
                    it["http"] += scheme == "http"
                yield [page, tmpl, comp, kind, found, url, h, pol.domain, cls, ISSUE.get(cls, cls), scheme.upper(),
                       st, bk, fin or "", first or "", int(occ or 1), ut, _remark(cls, scheme, st, fin, url)]
        # the long sheet first (streamed); summary / issues / hosts are small and follow
        n = x.sheet("All non-domain URLs", ["Source page", "Template", "Component", "Kind", "Found URL (as written)",
                                            "Normalized URL", "Host", "Primary domain", "Classification", "Issue", "HTTP/HTTPS",
                                            "Status", "Status bucket", "Redirects to", "First seen on", "Occurrences",
                                            "URL type", "Remarks"], detail(),
                    [60, 22, 22, 7, 60, 60, 30, 18, 18, 30, 10, 8, 12, 50, 50, 10, 12, 60],
                    "No URL on another host than the primary domain was found.")
        order = ["QA", "UAT", "Dev / staging", "Other subdomain", "Allowed subdomain", "External", "External (allowed)"]
        rank = {k: i for i, k in enumerate(order)}
        x.sheet("Issues", ["Classification", "Issue", "Host", "Template", "Component", "Affected pages", "Different URLs",
                           "Occurrences", "Using http://"] + [f"Example page {i}" for i in range(1, EXAMPLES + 1)]
                + [f"Example URL {i}" for i in range(1, EXAMPLES + 1)],
                ([k[0], ISSUE[k[0]], k[1], k[2], k[3], len(v["pages"]), len(v["urls"]), v["occ"], v["http"]]
                 + (v["ex_pages"] + [""] * EXAMPLES)[:EXAMPLES] + (v["ex_urls"] + [""] * EXAMPLES)[:EXAMPLES]
                 for k, v in sorted(issues.items(), key=lambda kv: (rank.get(kv[0][0], 9), -len(kv[1]["pages"])))),
                [16, 30, 30, 22, 22, 14, 14, 12, 12] + [60] * (2 * EXAMPLES),
                "No QA, UAT or development / staging URL on any page.")
        x.sheet("Hosts", ["Host", "Classification", "Pages", "Different URLs", "Occurrences", "Statuses"],
                ([h, cls, len(v["pages"]), len(v["urls"]), v["occ"],
                  ", ".join(f"{k}: {n_}" for k, n_ in sorted(v["status"].items(), key=lambda kv: -kv[1])[:6])]
                 for (h, cls), v in sorted(hosts.items(), key=lambda kv: (rank.get(kv[0][1], 9), -len(kv[1]["pages"])))),
                [40, 18, 10, 14, 12, 40])
        d = pol.describe()
        x.sheet("Summary", ["Item", "Value"], [
            ["Primary domain", d["primary_domain"]], ["Crawled host", d["primary_host"]],
            ["Allowed subdomains", ", ".join(d["allowed_subdomains"]) or "(none set: every other subdomain is listed for review)"],
            ["Flagged as issues", ", ".join(d["flag"])],
            ["QA patterns", ", ".join(d["qa_patterns"])], ["UAT patterns", ", ".join(d["uat_patterns"])],
            ["Dev / staging patterns", ", ".join(d["dev_patterns"])],
            ["External allowed", ", ".join(d["external_allowed"]) or "-"],
            ["Page -> URL rows (not on the primary domain)", n]] +
            [[f"Rows: {k}", counts.get(k, 0)] for k in order] +
            [["Hosts", len(hosts)], ["Aggregated issues (one row = one ticket)", len(issues)],
             ["Note", "Only URLs the crawl reports are audited: JS, CSS, tag managers, analytics and tracking URLs stay "
                      "excluded. Set host_policy in the site's settings to change the domain, allowed subdomains or patterns."]],
            [44, 100])
    finally:
        c.close()
    x.order(["Summary", "Issues", "Hosts"])
    x.close()
    return {"counts": counts, "issues": len(issues), "rows": sum(counts.values()), "hosts": len(hosts)}


def findings(db_path: str, job: str, pol: Policy) -> List[Tuple[str, str, str, str, int]]:
    """(page, rule, host, component, occurrences) for the flagged classes, for the problems / tickets."""
    out: List[Tuple[str, str, str, str, int]] = []
    if not os.path.exists(db_path):
        return out
    c = sqlite3.connect(ro_uri(db_path), uri=True, timeout=30)
    try:
        for (page, _t, comp, _k, _f, url, h, cls, _s, _st, _bk, _fin, _first, occ, _ut) in _rows(c, job, pol, {}):
            if cls in pol.flag and cls in RULE:
                out.append((page, RULE[cls], h, comp, int(occ or 1)))
    finally:
        c.close()
    return out
