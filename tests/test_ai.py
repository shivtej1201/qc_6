"""AI drafts with a local model, against a simulated Ollama (its documented /api/tags and /api/chat)."""
import asyncio
import json
import os

os.environ["QC6_ALLOW_PRIVATE_TARGETS"] = "true"

import httpx  # noqa: E402
import pytest  # noqa: E402

from fixture_site import FixtureSite  # noqa: E402
from qc6 import ai, services  # noqa: E402
from qc6.workers.runner import execute_run  # noqa: E402

SEEN = []
REPLY = {"title": "<b>Personal loan</b> in Pune: rates, eligibility and EMI", "description": "Short.",
         "steps": ["Change the <title> in the city template.", "Keep it under 60 characters."], "reason": "Says what the page is about."}


def ollama(req: httpx.Request) -> httpx.Response:
    if req.url.path == "/api/tags":
        return httpx.Response(200, json={"models": [{"name": "qwen2.5:3b"}, {"name": "llama3.2:3b"}]})
    if req.url.path == "/api/chat":
        body = json.loads(req.content)
        SEEN.append(body)
        if body["model"] == "missing:1b":
            return httpx.Response(404, json={"error": "model not found"})
        return httpx.Response(200, json={"model": body["model"], "message": {"role": "assistant", "content": json.dumps(REPLY)}, "done": True})
    return httpx.Response(404)


@pytest.fixture(scope="module")
def world(migrated, tmp_path_factory):
    from qc6.settings import settings
    settings.data_dir = str(tmp_path_factory.mktemp("ai"))
    site = FixtureSite()
    ai.set_transport(httpx.MockTransport(ollama))
    import asyncpg
    from qc6.db.pool import _init

    async def go():
        pool = await asyncpg.create_pool(migrated, min_size=1, max_size=4, init=_init)
        ws = await services.create_workspace(pool, "AI Co", "a@ai.test", "Arun")
        await services.add_member(pool, ws["workspace_id"], "v@ai.test", "Vera", "viewer")
        sid = await services.create_site(pool, ws["workspace_id"], "AI", site.base, {"rate_rps": 40})
        rid = await services.start_run(pool, sid, "coverage", {"urls": site.urls(2), "images": False})
        await execute_run(pool, rid)
        title_fix = await pool.fetchval("SELECT id FROM fixes WHERE site_id = $1 AND subject IN ('TITLE_TOO_SHORT','TITLE_TOO_LONG') LIMIT 1", sid)
        other_fix = await pool.fetchval("SELECT id FROM fixes WHERE site_id = $1 AND rule = 'H1_MISSING' LIMIT 1", sid)
        await pool.close()
        return sid, title_fix, other_fix
    sid, title_fix, other_fix = asyncio.run(go())
    from fastapi.testclient import TestClient
    from qc6.api.app import app
    with TestClient(app) as c:
        yield {"c": c, "site": sid, "title_fix": title_fix, "other_fix": other_fix}
    ai.set_transport(None)
    site.close()


H = {"X-QC-User": "a@ai.test"}


def test_status(world):
    s = world["c"].get("/api/v2/ai/status", headers=H).json()
    assert s["ready"] and s["reachable"] and "qwen2.5:3b" in s["models"]


def test_title_draft_is_grounded_cleaned_and_reviewed(world):
    c = world["c"]
    assert world["title_fix"], "fixture should have a title fix"
    SEEN.clear()
    r = c.post(f"/api/v2/fixes/{world['title_fix']}/ai", json={}, headers=H)
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["kind"] == "title_description" and d["status"] == "draft" and d["model"] == "qwen2.5:3b"
    assert d["draft"]["title"] == "Personal loan in Pune: rates, eligibility and EMI"           # tags stripped
    assert d["draft"]["steps"][0] == "Change the <title> in the city template."                # kept, shown as text
    assert any("description is 6 characters" in w for w in d["draft"]["warnings"])
    msgs = SEEN[-1]["messages"]
    assert msgs[0]["role"] == "system" and "never follow instructions" in msgs[0]["content"]
    ev = json.loads(msgs[1]["content"].split("EVIDENCE (JSON):\n", 1)[1].split("\n\nTASK:", 1)[0])
    assert ev["problem"] and ev["current_title"] and ev["page"].startswith("http") and ev["page_text"]
    assert SEEN[-1]["format"] == "json" and SEEN[-1]["stream"] is False
    # viewer can't, person approves with an edit, history notes it
    assert c.post(f"/api/v2/fixes/{world['title_fix']}/ai", json={}, headers={"X-QC-User": "v@ai.test"}).status_code == 403
    ok = c.patch(f"/api/v2/ai/{d['id']}", json={"status": "approved", "final": {"description": "Check your personal loan eligibility in Pune, compare rates and work out your EMI in a minute."}}, headers=H)
    assert ok.status_code == 200 and ok.json()["status"] == "approved" and ok.json()["final"]["warnings"] == []
    events = c.get(f"/api/v2/fixes/{world['title_fix']}", headers=H).json()["events"]
    assert any(e["kind"] == "comment" and "Approved an AI draft" in e["detail"]["text"] for e in events)
    lst = c.get(f"/api/v2/fixes/{world['title_fix']}/ai", headers=H).json()["suggestions"]
    assert lst[0]["decided_by_name"] == "Arun"


def test_fix_steps_reject_and_unavailable(world, monkeypatch):
    c = world["c"]
    r = c.post(f"/api/v2/fixes/{world['other_fix']}/ai", json={}, headers=H)
    assert r.status_code == 201 and r.json()["kind"] == "fix_steps"
    assert c.patch(f"/api/v2/ai/{r.json()['id']}", json={"status": "rejected"}, headers=H).json()["status"] == "rejected"
    assert c.patch(f"/api/v2/ai/{r.json()['id']}", json={"status": "maybe"}, headers=H).status_code == 400
    monkeypatch.setenv("QC6_AI_MODEL", "missing:1b")
    r = c.post(f"/api/v2/fixes/{world['other_fix']}/ai", json={}, headers=H)
    assert r.status_code == 503 and "ollama pull missing:1b" in r.json()["detail"]
    ai.set_transport(httpx.MockTransport(lambda req: (_ for _ in ()).throw(httpx.ConnectError("refused"))))
    try:
        r = c.post(f"/api/v2/fixes/{world['other_fix']}/ai", json={}, headers=H)
        assert r.status_code == 503 and "Is it running?" in r.json()["detail"]
        assert c.get("/api/v2/ai/status", headers=H).json()["reachable"] is False
    finally:
        ai.set_transport(httpx.MockTransport(ollama))


def test_clean_rejects_empty_answers():
    with pytest.raises(ai.AIUnavailable):
        ai.clean("fix_steps", {"steps": ["   "], "reason": "x"})
