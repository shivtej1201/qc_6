"""robots.txt matching as RFC 9309 (and Google) define it.

Python's urllib.robotparser takes the FIRST matching rule in file order and doesn't understand '*' / '$' wildcards,
so `Disallow: /` followed by `Allow: /images/` blocked images Google would fetch, and `Disallow: /*?sort=` blocked
nothing. This module:
  * picks the group for our product token (QC-Audit) if there is one, else '*' (groups with the same agent merge);
  * matches with '*' (any characters) and '$' (end of URL);
  * the LONGEST matching rule wins; on a tie Allow wins; an empty Disallow allows everything;
  * /robots.txt itself is always allowed.
`check()` also returns the rule that decided, so a block can be explained in the report.
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Dict, List, Optional, Tuple

Rule = Tuple[bool, str, "re.Pattern[str]"]      # (allow, original path pattern, compiled)


def _compile(pattern: str) -> "re.Pattern[str]":
    end = pattern.endswith("$")
    body = pattern[:-1] if end else pattern
    rx = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
    return re.compile(rx + ("$" if end else ""))


def _norm_path(p: str) -> str:
    # robots paths are compared percent-encoded; normalise the case of %xx escapes
    return re.sub(r"%[0-9a-fA-F]{2}", lambda m: m.group(0).upper(), p)


class Robots:
    def __init__(self, text: str):
        self.groups: Dict[str, List[Rule]] = {}
        self.crawl_delay: Dict[str, float] = {}
        agents: List[str] = []
        in_rules = False
        for raw in (text or "").splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, val = line.split(":", 1)
            key, val = key.strip().lower(), val.strip()
            if key == "user-agent":
                if in_rules:                 # a new group starts
                    agents, in_rules = [], False
                agents.append(val.lower())
                for a in agents:
                    self.groups.setdefault(a, [])
            elif key in ("allow", "disallow"):
                in_rules = True
                if not agents:
                    continue
                if key == "disallow" and not val:
                    continue                 # "Disallow:" allows everything
                pat = _norm_path(val if val.startswith(("/", "*")) else "/" + val)
                for a in agents:
                    self.groups[a].append((key == "allow", pat, _compile(pat)))
            elif key == "crawl-delay":
                in_rules = True
                try:
                    for a in agents:
                        self.crawl_delay[a] = float(val)
                except ValueError:
                    pass

    def _group(self, agent: str) -> Optional[List[Rule]]:
        token = agent.lower().split("/", 1)[0].strip()
        for name, rules in self.groups.items():
            if name != "*" and token and (name == token or token.startswith(name)):
                return rules
        return self.groups.get("*")

    def check(self, url: str, agent: str = "QC-Audit") -> Tuple[bool, str]:
        """(allowed, the deciding rule like 'Disallow: /private/' or '' when no rule matched)."""
        u = urllib.parse.urlsplit(url)
        path = _norm_path((u.path or "/") + (("?" + u.query) if u.query else ""))
        if path == "/robots.txt":
            return True, ""
        rules = self._group(agent)
        if not rules:
            return True, ""
        best: Optional[Tuple[int, bool, str]] = None
        for allow, pat, rx in rules:
            if rx.match(path):
                n = len(pat)
                if best is None or n > best[0] or (n == best[0] and allow and not best[1]):
                    best = (n, allow, pat)
        if best is None:
            return True, ""
        return best[1], ("Allow: " if best[1] else "Disallow: ") + best[2]
