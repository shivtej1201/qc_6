"""Opt-in raw HTML archive (CrawlPulse system architecture §3.4: raw responses kept as files, the database holds only
paths and fingerprints). Off unless QC6_KEEP_HTML=true: a 300,000-page check is several GB even gzipped.

Each page is saved as runs/<id>/html/<sha256 of the URL>.html.gz; index.csv lists url, final url, status, sha256 of the
HTML, file and size. Files go with the check when it is deleted.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import os
import threading
from typing import Optional


def enabled() -> bool:
    return os.environ.get("QC6_KEEP_HTML", "").strip().lower() in ("1", "true", "yes", "on")


class HtmlStore:
    def __init__(self, run_dir: str) -> None:
        self.dir = os.path.join(run_dir, "html")
        os.makedirs(self.dir, exist_ok=True)
        self._lock = threading.Lock()
        new = not os.path.exists(os.path.join(self.dir, "index.csv"))
        self._index = open(os.path.join(self.dir, "index.csv"), "a", encoding="utf-8", newline="")
        self._w = csv.writer(self._index)
        if new:
            self._w.writerow(["url", "final_url", "http_status", "html_sha256", "file", "bytes"])

    def __call__(self, url: str, final_url: str, status: Optional[int], html: str) -> None:
        data = html.encode("utf-8", "replace")
        name = hashlib.sha256(url.encode()).hexdigest()[:40] + ".html.gz"
        with gzip.open(os.path.join(self.dir, name), "wb", compresslevel=6) as f:
            f.write(data)
        with self._lock:
            self._w.writerow([url, final_url, status or "", hashlib.sha256(data).hexdigest(), name, len(data)])

    def close(self) -> None:
        with self._lock:
            self._index.close()
