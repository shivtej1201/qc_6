"""QC V6 — site quality platform: coverage crawl, real-visitor review, user journeys, fixes.
Blueprint: docs/blueprint/ARCHITECTURE.md."""
import importlib.util as _ilu
import os as _os
import sys as _sys

__version__ = "6.0.0"

# The coverage check uses the crawl engine shipped in ./engine (package `qcv5`). Make it importable from a checkout.
if _ilu.find_spec("qcv5") is None:
    _root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    for _cand in (_os.path.join(_root, "engine"), _os.path.dirname(_root)):
        if _os.path.isdir(_os.path.join(_cand, "qcv5")):
            _sys.path.insert(0, _cand)
            break
