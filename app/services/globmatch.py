"""Glob matching with EXACTLY the semantics of the browser engine.

The engine (static/js/gen-engine.js, `globToRe`) decides which files in an
analyst's folder are read. The server has to make the same decision in three
places — validating a manifest's `inputs[].match`, validating an exclusion
override, and attributing observed files to the pattern that admitted them —
and a server that matches differently from the engine would show a lineage
that contradicts what actually ran. So this is a literal port, not `fnmatch`:

  * `**`  → `.*`      crosses separators (kept for forward compatibility; the
                       walk is flat today and patterns with "/" are refused)
  * `*`   → `[^/]*`
  * `?`   → `[^/]`
  * `[`, `]` and every other regex metacharacter are ESCAPED — there are no
    character classes; `[ab].xlsx` matches the literal name "[ab].xlsx"
  * case-insensitive, anchored at both ends

`fnmatch` differs on every one of those points (classes are live, `*` crosses
"/", case folding depends on the OS), which is why it is not used.

Any change here must be mirrored in gen-engine.js and data-sources-edit.js;
tests/smoke_globmatch.py runs one table through all three.
"""
from __future__ import annotations

import re
from functools import lru_cache

MAX_PATTERN_LEN = 128

# Everything the JS version escapes: /[.+^${}()|[\]\\]/
_ESCAPE = set(".+^${}()|[]\\")


@lru_cache(maxsize=1024)
def to_regex(pattern: str) -> re.Pattern:
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                out.append(".*")
                i += 1
            else:
                out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c in _ESCAPE:
            out.append("\\" + c)
        else:
            out.append(c)
        i += 1
    return re.compile("^" + "".join(out) + "$", re.IGNORECASE)


def matches(pattern: str, name: str) -> bool:
    return to_regex(pattern).match(name) is not None


def first_match(patterns, name: str) -> str | None:
    """The first pattern that admits `name`, or None. Same loop as the engine's
    `excludedBy`, so a file is attributed to the same rule on both sides."""
    for p in patterns:
        if to_regex(p).match(name):
            return p
    return None


def validate_pattern(pattern) -> str | None:
    """Return an error message, or None if the pattern is acceptable.

    "/" and "\\" are refused because the engine walks one level: a pattern that
    could only match inside a subfolder would be accepted, shown, and never
    fire — the reviewer's "archive/**" case. Lift this when the walk descends.
    A wildcard-only pattern is refused because as an allow-list entry it is
    meaningless and as an exclusion it empties the folder.
    """
    if not isinstance(pattern, str):
        return "pattern must be a string"
    p = pattern.strip()
    if not p:
        return "pattern is empty"
    if p != pattern:
        return "pattern has leading or trailing whitespace"
    if len(p) > MAX_PATTERN_LEN:
        return f"pattern longer than {MAX_PATTERN_LEN} chars"
    if "/" in p or "\\" in p:
        return "pattern must not contain '/' or '\\' (folders are read one level deep)"
    if ".." in p:
        return "pattern must not contain '..'"
    if p.replace("*", "").replace("?", "") == "":
        return "pattern of only wildcards would match every file"
    return None
