"""'Seen before' remarks and the one-sheet report (CSV, Excel, ZIP)."""
import asyncio
import csv
import io
import os
import zipfile

os.environ["QC6_ALLOW_PRIVATE_TARGETS"] = "true"

import pytest  # noqa: E402

from fixture_site import FixtureSite  # noqa: E402
from qc6 import export as ex  # noqa: E402
from qc6 import history, services  # noqa: E402
from qc6.workers.runner import execute_run  # noqa: E402


@pytest.fixture(scope="module")
def two(migrated, tmp_path_factory):
    from qc6.settings import settings
    settings.data_dir = str(tmp_path_factory.mktemp("exp"))
    site = FixtureSite()
    import asyncpg
    from qc6.db.pool import _init

    async def go():
        pool = await asyncpg.create_pool(migrated, min_size=1, max_size=4, init=_init)
        ws = await services.create_workspace(pool, "Export Co", "e@export.test", "Esha")
        sid = await services.create_site(pool, ws["workspace_id"], "Export", site.base, {"rate_rps": 40})
        runs = []
        for kinds in (("product",), ("product", "article")):           # article problems are new in the 2nd check
            rid = await services.start_run(pool, sid, "coverage", {"urls": [f"{site.base}/{k}/{i}" for k in kinds for i in range(3)],
                                                                   "images": False})
            await execute_run(pool, rid)
            runs.append(rid)
        return pool, sid, runs
    loop = asyncio.new_event_loop()
    pool, sid, runs = loop.run_until_complete(go())
    yield loop, pool, sid, runs, site.base
    loop.run_until_complete(pool.close())
    loop.close()
    site.close()


def _site(loop, pool, sid):
    return dict(loop.run_until_complete(pool.fetchrow("SELECT * FROM sites WHERE id = $1", sid)))


def test_history_remarks(two):
    loop, pool, sid, (r1, r2), base = two
    h = loop.run_until_complete(history.for_run(pool, r2))
    product = [k for k in h if "/product/" in k[1]]
    article = loop.run_until_complete(pool.fetch("SELECT fix_id, page_url FROM findings WHERE run_id = $1 AND page_url LIKE '%/article/%'", r2))
    assert product and all(h[k]["checks"] == 1 for k in product)
    assert article and all((a["fix_id"], a["page_url"]) not in h for a in article)        # first time: no history
    first = loop.run_until_complete(pool.fetchval("SELECT finished_at FROM runs WHERE id = $1", r1))
    assert history.remark(h[product[0]]) == f"Also found on {first:%d %b %Y} (first seen {first:%d %b %Y}, 1 earlier check)"
    assert history.remark(None) == "New in this check"


def _csv(loop, pool, site, rid):
    async def collect():
        return b"".join([c async for c in ex.csv_stream(pool, site, rid)]).decode("utf-8-sig")
    return list(csv.reader(io.StringIO(loop.run_until_complete(collect()))))


def test_csv_one_sheet_all_sections(two):
    loop, pool, sid, (r1, r2), base = two
    rows = _csv(loop, pool, _site(loop, pool, sid), r2)
    assert rows[0] == ["QC site quality report"]
    labels = [r[0] for r in rows if r]
    assert "Health score (qcv6-health-1.0)" in labels and "PROBLEMS" in labels and "PAGES AND PROBLEMS" in labels
    d = rows.index(["PAGES AND PROBLEMS"])
    detail = [r for r in rows[d + 2:] if r]
    assert rows[d + 1] == ex.DETAIL_HEADER
    n_find = loop.run_until_complete(pool.fetchval("SELECT count(*) FROM findings WHERE run_id = $1", r2))
    assert len(detail) == n_find
    rem = {r[0].rsplit("/", 2)[-2]: r[-1] for r in detail}
    assert rem["article"] == "New" and rem["product"].startswith("Seen before: ")
    pr = rows.index(ex.PROBLEM_HEADER)
    assert rows[pr + 1][7] and rows[pr + 1][8]                          # why it matters / how to fix: once per problem
    assert ex.cell("=cmd|' /C calc'!A0") == "'=cmd|' /C calc'!A0" and ex.cell("+1") == "'+1"


def test_xlsx_single_sheet_and_row_cap(two, monkeypatch):
    import openpyxl
    loop, pool, sid, (r1, r2), base = two
    site = _site(loop, pool, sid)
    path = loop.run_until_complete(ex.xlsx_file(pool, site, r2))
    try:
        wb = openpyxl.load_workbook(path, read_only=True)
        assert wb.sheetnames == ["QC report"]
        vals = [r[0] if r else None for r in wb.active.iter_rows(values_only=True)]
        assert "PAGES AND PROBLEMS" in vals
    finally:
        os.unlink(path)
    monkeypatch.setattr(ex, "EXCEL_MAX_ROWS", 70)                       # pretend Excel's limit is tiny
    path = loop.run_until_complete(ex.xlsx_file(pool, site, r2))
    try:
        rows = [r for r in openpyxl.load_workbook(path, read_only=True).active.iter_rows(values_only=True)]
        assert str(rows[1][0]).startswith("NOTE: this sheet shows the first") and len(rows) <= 70
    finally:
        os.unlink(path)


def test_zip_and_api(two):
    from fastapi.testclient import TestClient
    from qc6.api.app import app
    loop, pool, sid, (r1, r2), base = two
    with TestClient(app) as c:
        H = {"X-QC-User": "e@export.test"}
        z = c.get(f"/api/v2/sites/{sid}/export.zip", headers=H)
        assert z.status_code == 200 and z.headers["content-type"] == "application/zip"
        names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
        assert len(names) == 1 and names[0].endswith(f"check-{r2}.csv")
        x = c.get(f"/api/v2/sites/{sid}/export.xlsx?run={r1}", headers=H)
        assert x.status_code == 200 and x.content[:2] == b"PK"
        assert c.get(f"/api/v2/sites/{sid}/export.pdf", headers=H).status_code == 404
        fix = loop.run_until_complete(pool.fetchval("SELECT fix_id FROM findings WHERE run_id = $1 AND page_url LIKE '%/product/0'", r2))
        d = c.get(f"/api/v2/fixes/{fix}", headers=H).json()
        assert any(v.startswith("Also found on") for v in d["page_history"].values())
        pg = c.get(f"/api/v2/sites/{sid}/page", params={"url": f"{base}/product/0"}, headers=H).json()
        assert any(f["checks"] == 2 and f["first_seen"] for f in pg["fixes"])
