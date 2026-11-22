"""Request ids on log lines: every record written while an API request runs carries record.request_id."""
from __future__ import annotations

import contextvars
import logging

REQUEST_ID: contextvars.ContextVar[str] = contextvars.ContextVar("qc6_request_id", default="-")
_old_factory = logging.getLogRecordFactory()


def _factory(*args, **kwargs):
    record = _old_factory(*args, **kwargs)
    record.request_id = REQUEST_ID.get()
    return record


logging.setLogRecordFactory(_factory)
