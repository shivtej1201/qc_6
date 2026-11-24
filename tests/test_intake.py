"""One intake for every URL source: normalize, global dedup across sources (keeping every source), rows that can't be
used kept with the reason, big sheets read in chunks."""
import asyncio
import time
import tracemalloc

from qc6 import intake


def test_global_dedup_across_sources_keeps_every_source():
    it = intake.Intake("www.example.com")
    intake.feed_text(it, "https://www.example.com/page-a\nhttps://www.example.com/page-b#top\nexample.com/page-c\nnot a url")
    intake.feed_sitemap_entries(it, ["https://www.example.com/page-a", "https://www.example.com/page-d"],
                                ["https://www.example.com/sitemap-1.xml"], [0, 0], "Sitemap: https://www.example.com/sitemap.xml")
    for i, u in enumerate(["https://WWW.example.com/page-a?utm_source=x", "https://www.example.com/page-b",
                           "https://other.test/x", "https://www.example.com/page-e"], 2):
        it.add(u, "Sheet: a.xlsx", {"row": i})
    # page-a from all three sources: one queue entry, three sources
    assert it.urls[:2] == ["https://www.example.com/page-a", "https://www.example.com/page-b"]
    a = it.urls.index("https://www.example.com/page-a")
    assert it.sources[a] == ["Manual entry", "Sitemap: https://www.example.com/sitemap.xml", "Sheet: a.xlsx"]
    assert "https://example.com/page-c" in it.urls                       # typed without https://, bare host of the site
    s = it.summary()
    assert s["queued"] == len(it.urls) == 5
    assert s["by_source"]["Sheet: a.xlsx"] == {"rows": 4, "valid": 4, "invalid": 0, "duplicates": 2, "other_hosts": 1,
                                               "outside_scope": 0, "added": 1}
    assert s["by_source"]["Manual entry"]["invalid"] == 3                 # "not", "a", "url": not addresses
    assert any(why.startswith("Another site") and v == "https://other.test/x" for _, _, v, why in it.rejects)
    names, idx = it.sitemap_view()                                       # the report's per-source breakdown
    assert names[idx[it.urls.index("https://www.example.com/page-d")]] == "https://www.example.com/sitemap-1.xml"
    assert names[idx[it.urls.index("https://www.example.com/page-e")]] == "Sheet: a.xlsx"


def test_scope_and_preload():
    it = intake.Intake("example.com", {"include": ["/blog/*"]})
    it.preload([("https://example.com/blog/1", ["Manual entry"], {"line": 1})])
    assert it.add("https://example.com/blog/1?gclid=1", "Sheet: s.csv") == "duplicate"
    assert it.add("https://example.com/shop/1", "Sheet: s.csv") == "outside_scope"
    assert it.sources[0] == ["Manual entry", "Sheet: s.csv"]


def test_big_sheet_read_in_chunks(tmp_path):
    """1.25 lakh rows with repeats and junk: read in 5,000-row chunks with progress after each, memory bounded."""
    path = tmp_path / "big.csv"
    with open(path, "w", encoding="utf-8") as f:
        f.write("Sr,Page URL,Owner\n")
        for i in range(125_000):
            u = f"https://www.example.com/p/{i % 118_500}" if i % 50 else "n/a"
            f.write(f"{i},{u},team-{i % 7}\n")
    it = intake.Intake("www.example.com")
    calls = []

    async def progress(s):
        calls.append(s["rows"])
    tracemalloc.start()
    t0 = time.perf_counter()
    asyncio.run(intake.read_sheet(it, str(path), "big.csv", "Sheet: big.csv", progress))
    took = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    s = it.summary()
    unique = len({i % 118_500 for i in range(125_000) if i % 50})
    assert s["rows"] == 125_000 and s["invalid"] == 2_500 and s["queued"] == unique and s["duplicates"] == 122_500 - unique
    assert len(calls) >= 25 and calls == sorted(calls)                    # progress after every chunk
    assert it.refs[0]["row"] == 3 and it.refs[0]["cells"] == ["1", "https://www.example.com/p/1", "team-1"]
    assert it.refs[0]["header"] == ["Sr", "Page URL", "Owner"]
    print(f"125,000 rows in {took:.1f} s, peak {peak / 1e6:.0f} MB")
    assert took < 30


def test_xlsx_without_header_takes_every_http_cell(tmp_path):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["https://example.com/a", "note", "https://example.com/b"])
    ws.append(["", None, ""])
    ws.append(["https://example.com/a", "dup", None])
    wb.save(tmp_path / "s.xlsx")
    it = intake.Intake("example.com")
    asyncio.run(intake.read_sheet(it, str(tmp_path / "s.xlsx"), "s.xlsx", "Sheet: s.xlsx"))
    assert it.urls == ["https://example.com/a", "https://example.com/b"] and it.summary()["duplicates"] == 1
