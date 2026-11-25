"""Non-Domain / environment URL audit: host classification by policy, and the separate report built only from what
the crawl recorded (existing data untouched)."""
import os
import sqlite3

import openpyxl

from qc6 import host_audit as ha


def test_classification_policy():
    p = ha.Policy("www.bajajfinserv.in")
    assert p.domain == "bajajfinserv.in"
    cases = {"www.bajajfinserv.in": "Primary", "bajajfinserv.in": "Primary", "bmqa.bajajfinserv.in": "QA",
             "bmuat.bajajfinserv.in": "UAT", "qa-www.bajajfinserv.in": "QA", "uat2.bajajfinserv.in": "UAT",
             "dev.bajajfinserv.in": "Dev / staging", "staging.bajajfinserv.in": "Dev / staging",
             "preprod.bajajfinserv.in": "Dev / staging", "api.bajajfinserv.in": "Other subdomain",
             "10.2.3.4": "Dev / staging", "localhost": "Dev / staging", "example.com": "External",
             "aqua.bajajfinserv.in": "Other subdomain"}
    for host, want in cases.items():
        assert p.classify(host) == want, (host, p.classify(host))
    q = ha.Policy("www.bajajfinserv.in", {"allowed_subdomains": ["api", "cdn*"], "external_allowed": ["youtube.com"],
                                          "qa_patterns": ["*test*"]})
    assert q.classify("api.bajajfinserv.in") == "Allowed subdomain" and q.classify("cdn2.bajajfinserv.in") == "Allowed subdomain"
    assert q.classify("www.youtube.com") == "External (allowed)" and q.classify("bmtest.bajajfinserv.in") == "QA"
    assert q.classify("bmqa.bajajfinserv.in") == "Other subdomain"                 # patterns replaced by the site's own
    assert ha.Policy("shop.example.co.in").domain == "example.co.in"


def _db(path):
    c = sqlite3.connect(path)
    c.executescript("""
      CREATE TABLE on_page_source_pages (job_id, source_page_url, page_type);
      CREATE TABLE on_page_link_relationships (id INTEGER PRIMARY KEY, job_id, source_page_url, found_url, normalized_found_url,
        found_url_type, found_url_scope, representative_location, occurrence_count);
      CREATE TABLE on_page_validation_cache (job_id, normalized_url, http_status, status_bucket, final_url, first_seen_source_url);
      CREATE TABLE on_page_image_relationships (id INTEGER PRIMARY KEY, job_id, source_page_url, image_url, normalized_image_url,
        image_type, representative_location, occurrence_count);
      CREATE TABLE on_page_image_validation_cache (job_id, normalized_url, http_status, status_bucket, final_url, first_seen_source_url);
    """)
    j = "RUN7"
    rel, val = [], []
    for i in range(1, 6):
        page = f"https://www.bajajfinserv.in/clp/{i}"
        c.execute("INSERT INTO on_page_source_pages VALUES (?, ?, 'CLP')", (j, page))
        rel += [(j, page, "https://bmqa.bajajfinserv.in/x", "https://bmqa.bajajfinserv.in/x", "ANCHOR", "INTERNAL", "Breadcrumb", 1),
                (j, page, "https://www.bajajfinserv.in/ok", "https://www.bajajfinserv.in/ok", "ANCHOR", "INTERNAL", "Main content", 2)]
    page = "https://www.bajajfinserv.in/pdp/1"
    c.execute("INSERT INTO on_page_source_pages VALUES (?, ?, 'PDP')", (j, page))
    rel += [(j, page, "http://bmuat.bajajfinserv.in/p/1", "http://bmuat.bajajfinserv.in/p/1", "CANONICAL", "INTERNAL", "Head", 1),
            (j, page, "https://example.com/a", "https://example.com/a", "ANCHOR", "EXTERNAL", "Footer", 1)]
    c.executemany("INSERT INTO on_page_link_relationships (job_id, source_page_url, found_url, normalized_found_url, found_url_type, "
                  "found_url_scope, representative_location, occurrence_count) VALUES (?,?,?,?,?,?,?,?)", rel)
    c.executemany("INSERT INTO on_page_validation_cache VALUES (?,?,?,?,?,?)", [
        (j, "https://bmqa.bajajfinserv.in/x", 200, "2xx", "https://bmqa.bajajfinserv.in/x", "https://www.bajajfinserv.in/clp/1"),
        (j, "http://bmuat.bajajfinserv.in/p/1", 301, "3xx", "https://www.bajajfinserv.in/p/1", page),
        (j, "https://example.com/a", 200, "2xx", "https://example.com/a", page)])
    c.execute("INSERT INTO on_page_image_relationships (job_id, source_page_url, image_url, normalized_image_url, image_type, "
              "representative_location, occurrence_count) VALUES (?,?,?,?,?,?,?)",
              (j, page, "https://staging.bajajfinserv.in/i.jpg", "https://staging.bajajfinserv.in/i.jpg", "HTTPS_IMAGE", "Main content", 1))
    c.commit()
    c.close()


def test_report_aggregates_by_host_template_component(tmp_path, monkeypatch):
    from qc6.settings import settings
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    os.makedirs(tmp_path / "runs" / "7")
    db = str(tmp_path / "runs" / "7" / "coverage.db")
    _db(db)
    before = open(db, "rb").read()
    site = {"primary_host": "www.bajajfinserv.in", "settings": {}}
    out = str(tmp_path / "nd.xlsx")
    res = ha.write_report(7, site, {}, out)
    assert open(db, "rb").read() == before                         # the crawl data isn't touched
    assert res["counts"] == {"QA": 5, "UAT": 1, "External": 1, "Dev / staging": 1} and res["issues"] == 3
    wb = openpyxl.load_workbook(out, read_only=True)
    assert wb.sheetnames[:3] == ["Summary", "Issues", "Hosts"] and "All non-domain URLs" in wb.sheetnames
    issues = [r for r in wb["Issues"].iter_rows(values_only=True)]
    qa = next(r for r in issues[1:] if r[0] == "QA")
    assert qa[1:6] == ("QA URL found", "bmqa.bajajfinserv.in", "CLP", "Breadcrumb", 5)          # one row = one ticket
    uat = next(r for r in issues[1:] if r[0] == "UAT")
    assert uat[3:5] == ("PDP", "Canonical") and uat[8] == 1                                     # http://, canonical
    rows = [r for r in wb["All non-domain URLs"].iter_rows(values_only=True)]
    assert len(rows) == 1 + 8 and not any("www.bajajfinserv.in/ok" in str(r[5]) for r in rows)
    u = next(r for r in rows if r[8] == "UAT")
    assert u[11] == 301 and "redirects to www.bajajfinserv.in" in u[17] and "http://" in u[17]
    st = next(r for r in rows if r[3] == "image")
    assert st[8] == "Dev / staging" and st[11] is None and "not validated" in st[17]
    f = ha.findings(db, "RUN7", ha.policy_for(site))
    assert {(r, h) for _, r, h, _, _ in f} == {("ENV_URL_QA", "bmqa.bajajfinserv.in"), ("ENV_URL_UAT", "bmuat.bajajfinserv.in"),
                                              ("ENV_URL_DEV", "staging.bajajfinserv.in")}
