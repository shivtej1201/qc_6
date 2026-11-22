"""A check's timeline (CrawlPulse job orchestration §4.3): what happened to it and when, in plain words.
Written next to every state change; read by the Live screen. Never fails the thing it records."""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("qc6.events")


async def record(pool, run_id: int, kind: str, message: str, /, **data: Any) -> None:
    try:
        await pool.execute("INSERT INTO run_events (run_id, kind, message, data) VALUES ($1, $2, $3, $4)",
                           run_id, kind, message[:500], data)
    except Exception as e:                       # a missing timeline line must never stop a check
        log.warning("could not record %s for run %s: %s", kind, run_id, e)
