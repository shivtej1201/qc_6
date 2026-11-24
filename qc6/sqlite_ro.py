"""Read-only SQLite URIs that work on Windows, macOS and Linux.

`f"file:{path}?mode=ro"` breaks when the path has a space, '#', '%' or '?' (C:\\Users\\First Last\\..., a folder named
"QC #2"), and Windows backslashes aren't URI syntax. pathlib builds a proper file URI (file:///C:/..., percent-encoded),
which SQLite accepts on every system.
"""
from __future__ import annotations

import pathlib


def ro_uri(path: str) -> str:
    return pathlib.Path(path).resolve().as_uri() + "?mode=ro"
