"""All pages: filters, new / gone / changed since the check before, CSV, and comparing two checks."""
import asyncio
import os

os.environ["QC6_ALLOW_PRIVATE_TARGETS"] = "true"

import pytest  # noqa: E402

from fixture_site import FixtureSite  # noqa: E402
from qc6 import inventory as I  # noqa: E402
from qc6 import services  # noqa: E402
from qc6.workers.runner import execute_run  # noqa: E402


@pytest.fixture(scope="module")
def two_checks(migrated, tmp_path_factory):
    from qc6.settings import settings
    settings.data_dir = str(tmp_path_factory.mktemp("inv"))
    site = FixtureSite()
    import asyncpg
    from qc6.db.pool import _init

    async def go():
        pool = await asyncpg.create_pool(migrated, min_size=1, max_size=4, init=_init)
        ws = await services.create_workspace(pool, "Inv Co", "i@inv.test", "Indu")
        sid = await services.create_site(pool, ws["workspace_id"], "Inv", site.base, {"rate_rps": 40})
        u = lambda kinds: [f"{site.base}/{k}/{i}" for k in kinds for i in range(3)]
        runs = []
        for kinds in (("product", "city"), ("product", "article")):        # city pages leave, article pages arrive
            rid = await services.start_run(pool, sid, "coverage", {"urls": u(kinds) + [f"{site.base}/missing-page"], "images": False})
            await execute_run(pool, rid)
            runs.append(rid)
        return pool, sid, runs
    loop = asyncio.new_event_loop()
    pool, sid, runs = loop.run_until_complete(go())
    yield loop, pool, sid, runs
    loop.run_until_complete(pool.close())
    loop.close()
    site.close()


def test_changes_and_filters(two_checks):
    loop, pool, sid, (r1, r2) = two_checks
    run = lambda c: loop.run_until_complete(c)
    all_ = run(I.list_pages(pool, sid, r2, {}))
    assert all_["previous_run_id"] == r1 and all_["total"] == 7
    assert all_["changes"] == {"new": 3, "status_changed": 0, "gone": 3}
    new = run(I.list_pages(pool, sid, r2, {"change": "new"}))
    assert sorted(p["url"].rsplit("/", 2)[-2] for p in new["pages"]) == ["article"] * 3
    gone = run(I.list_pages(pool, sid, r2, {"change": "gone"}))
    assert gone["total"] == 3 and all("/city/" in p["url"] and p["change"] == "gone" for p in gone["pages"])
    four = run(I.list_pages(pool, sid, r2, {"status": "4xx"}))
    assert [p["url"].rsplit("/", 1)[-1] for p in four["pages"]] == ["missing-page"]
    q = run(I.list_pages(pool, sid, r2, {"q": "EMIs"}))                  # title search
    assert q["total"] == 3
    with_fixes = run(I.list_pages(pool, sid, r2, {"fixes": "yes"}, sort="fixes", desc=True))
    assert with_fixes["total"] > 0 and with_fixes["pages"][0]["open_fixes"] >= with_fixes["pages"][-1]["open_fixes"] > 0
    page2 = run(I.list_pages(pool, sid, r2, {}, limit=3, offset=3))
    assert len(page2["pages"]) == 3 and page2["total"] == 7
    first = run(I.list_pages(pool, sid, r1, {}))
    assert first["previous_run_id"] is None and all(p["change"] is None for p in first["pages"])


def test_csv_streams_every_matching_page(two_checks):
    loop, pool, sid, (r1, r2) = two_checks

    async def collect(f):
        return "".join([chunk async for chunk in I.csv_rows(pool, sid, r2, f)])
    text = loop.run_until_complete(collect({"change": "new"}))
    lines = text.strip().splitlines()
    assert lines[0].startswith("url,http_status,template_name") and len(lines) == 4
    gone = loop.run_until_complete(collect({"change": "gone"})).strip().splitlines()
    assert len(gone) == 4 and all("/city/" in l for l in gone[1:])
    assert I._cell("=HYPERLINK(1)") == "'=HYPERLINK(1)" and I._cell("-1") == "'-1" and I._cell(None) == ""


def test_compare(two_checks):
    loop, pool, sid, (r1, r2) = two_checks
    d = loop.run_until_complete(I.compare(pool, sid, r2, r1))            # order doesn't matter: older -> newer
    assert d["a"]["id"] == r1 and d["b"]["id"] == r2
    assert d["pages"] == {"before": 7, "after": 7, "added": 3, "removed": 3, "status_changed": 0}
    assert {x["url"].rsplit("/", 2)[-2] for x in d["samples"]["added"]} == {"article"}
    assert d["fixes"]["new"]["count"] > 0                                 # article-page problems first found in check 2
    assert d["score"] is not None and "dimensions" in d["score"]
    types = {t["name"]: (t["before"], t["after"]) for t in d["page_types"]}
    assert types["Product pages"] == (3, 3)
    with pytest.raises(ValueError):
        loop.run_until_complete(I.compare(pool, sid, r1, r1))
    with pytest.raises(LookupError):
        loop.run_until_complete(I.compare(pool, sid, r1, 999999))
