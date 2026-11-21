"""Insight headlines only say what the numbers say."""
from qc6.insights import headlines


def _d(scores, dims=None, flow=None, crit=0, pages=0, unowned=0, totals=None):
    pts = [{"score": s, "dimensions": {}} for s in scores]
    return {"score": {"points": pts, "dimension_change": dims or {}},
            "fix_flow": {"runs": flow or [], "totals_after_first": totals or {}},
            "backlog": {"by_severity": [{"severity": "critical", "open": crit, "pages": pages}], "unowned": unowned}}


def test_up_and_shrinking():
    flow = [{"new": 40, "verified": 0, "regressed": 0}, {"new": 1, "verified": 5, "regressed": 0}, {"new": 2, "verified": 4, "regressed": 1}]
    h = headlines(_d([60, 64, 71], flow=flow, totals={"new": 3, "verified": 9, "regressed": 1}))
    assert h[0] == "Health is up 11 points over the last 3 checks."
    assert h[1] == "Since the first of these checks, 9 fixes were confirmed and 3 new problems found. The list is shrinking."  # 40 = baseline
    assert h[2] == "1 fix came back after a check had confirmed it."


def test_down_names_the_dimension_and_criticals():
    h = headlines(_d([80, 70], dims={"links": -12.0, "content": 1.0}, crit=2, pages=1340))
    assert h[0] == "Health is down 10 points over the last 2 checks."
    assert h[1] == "Most of the drop is in links: down 12 points."
    assert h[-1] == "2 critical fixes open, touching 1,340 pages."


def test_single_check_and_unowned_singular():
    h = headlines(_d([55], unowned=1))
    assert h == ["1 open fix has nobody looking after it."]


def test_nothing_to_say():
    assert headlines(_d([])) == []


def test_comebacks_stop_the_list_from_shrinking():
    flow = [{"new": 33, "verified": 0, "regressed": 0}] * 4
    h = headlines(_d([56, 56, 56, 56], flow=flow, totals={"new": 0, "verified": 12, "regressed": 12}))
    assert h[1] == "Since the first of these checks, 12 fixes were confirmed and 0 new problems found."
    assert h[2] == "12 fixes came back after a check had confirmed them."
