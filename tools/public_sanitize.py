#!/usr/bin/env python3
"""Gate and scrubber for the public slice of this repository.

Two modes, one rule table, on purpose:

    tools/public_sanitize.py --check <dir>    # detect; exit 1 on any finding
    tools/public_sanitize.py --apply <dir>    # rewrite internal values in place

The table is shared so the two can never drift: anything the scrubber knows how
to replace, the gate knows how to find. A rule with no replacement is
detect-only — it marks content a human has to rewrite (a research document, a
test fixture naming real retailers), because a machine cannot invent a
replacement that keeps the text true.

WHY THIS IS A GATE AND NOT A CHECKLIST
--------------------------------------
Scrubbing by a list of known strings produces a false sense of completion: the
strings on the list disappear, the ones nobody listed stay, and the result
looks clean. So the rules below match CLASSES of leak — any host under the
corporate domain, any address in a private range, any eight-digit number used
as a login — not the particular values that happened to be in the tree the day
this was written. A new colleague's surname in a new test is caught by the
same rule that caught the old ones only if the rule is about shape; keep it
that way when extending this file.

WHAT IS PUBLIC AND WHAT IS NOT
------------------------------
The rules about SHAPE (private address ranges, eight-digit logins, credential
shapes) name no real value and ship with the public tree. The rules about
VALUES (the domain, the surnames, the staff numbers, the warehouse objects, the
customer's brands) are exactly what is being hidden, so they live in
`tools/public_internal.py`, which the slice excludes. The scrubber used to
carry them inline — and was therefore the one file in the slice that spelled
out everything the slice removed.

The escape hatch is `tools/public_allow.txt`, deliberately in the same format
as `tools/i18n_allow.txt`: `<path glob> : <regex>  # why`. Both halves must
match. Every entry is a promise that the match is not a leak; keep it short.

Idempotence is a requirement, not a nicety: `--apply` must produce a tree that
`--check` accepts, and a second `--apply` must change nothing. The synthetic
values below are chosen so they cannot re-trigger the rule that produced them
(documentation IP ranges from RFC 5737, `example.com` from RFC 2606, employee
numbers in a reserved 1000000x block that the allow file exempts).
"""
from __future__ import annotations

import argparse
import fnmatch
import importlib.util
import os
import re
import sys
from dataclasses import dataclass, field

# Directories that never belong to a slice and are expensive to walk. The slice
# itself comes from `git archive`, so these only matter when the tool is run
# against a live working tree.
SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    "dist", "data", "scratchpad", "scratch-ui", ".mypy_cache", ".ruff_cache",
    # Local, not tracked by git (.git/info/exclude) — it cannot get into the slice,
    # which is made from `git archive`, by construction.
    "tools/promo",
    "static/pyodide", "static/duckdb-wasm", "static/duckdb-ext",
    "static/arrow-esm", "static/xlsx-arrow", "engine/xlsx-arrow/target",
}

# Extensions we never read as text. A binary that genuinely carries an internal
# name (a screenshot of a prod URL, say) is a human review item, not a regex
# one — `deploy/public-exclude.txt` is where such a file gets removed.
BINARY_EXT = {
    ".woff2", ".woff", ".ttf", ".otf", ".webp", ".png", ".jpg", ".jpeg",
    ".gif", ".ico", ".mp4", ".webm", ".zip", ".gz", ".br", ".tar", ".whl",
    ".wasm", ".parquet", ".xlsx", ".xlsm", ".xls", ".xlsb", ".db", ".sqlite",
    ".pdf", ".bin", ".so", ".dylib", ".pyc",
}

MAX_BYTES = 8 * 1024 * 1024  # a text file larger than this is a generated blob


@dataclass
class Rule:
    """One class of leak.

    `pattern` is matched against each line. `repl` is either a mapping of exact
    matches to their synthetic replacements, a plain replacement string, or
    None for detect-only. `hint` is printed with every finding and must say
    what the person is supposed to DO — a finding nobody knows how to fix is
    noise that trains people to ignore the gate.
    """

    rid: str
    why: str
    pattern: re.Pattern[str]
    hint: str
    repl: dict[str, str] | str | None = None
    allow: tuple[str, ...] = field(default=())  # inline regexes, always exempt


def _ci(*words: str) -> str:
    return "|".join(re.escape(w) for w in words)


# ── Rules about SHAPE: published with the tree ────────────────────────────
# None of these names a real value, so they travel into the public repository
# and keep catching what has not happened yet: a private address in a new
# config, a staff number in a new test, a key pasted into an example.
RULES: list[Rule] = [
    Rule(
        rid="private-ip",
        why="Addresses inside a private network say where a system actually "
            "lives.",
        pattern=re.compile(
            r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
            r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
            r"|192\.168\.\d{1,3}\.\d{1,3})\b"),
        hint="replace with a documentation address: 192.0.2.x (RFC 5737)",
        repl=None,
        # The docker bridge default is a published fact about docker, not about
        # this company, and FORWARDED_ALLOW_IPS has to name it to be correct.
        allow=(r"172\.18\.0\.0/16",),
    ),
    Rule(
        rid="staff-number-shape",
        why="Any eight-digit number used as a login. This is the rule that "
            "catches the staff number nobody put on a list.",
        pattern=re.compile(r"\b\d{8}@"),
        hint="this looks like a staff number used as a UPN — invent one",
        repl=None,  # cannot be auto-replaced: the surrounding identity matters
        allow=(r"\b1000000\d@",),
    ),
    Rule(
        rid="secret-shape",
        why="Anything shaped like a credential. The tree is clean today; this "
            "rule exists so it stays clean after publication, when a leak is "
            "irreversible.",
        pattern=re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
            r"|\bsk-[A-Za-z0-9]{20,}"
            r"|\bghp_[A-Za-z0-9]{20,}"
            r"|\bxox[baprs]-[A-Za-z0-9-]{10,}"
            r"|\bAKIA[0-9A-Z]{16}\b"),
        hint="a credential must never reach the public tree",
        repl=None,
        # The AWS documentation's own example key, used as a SigV4 test vector.
        allow=(r"AKIAIOSFODNN7EXAMPLE",),
    ),
]

# ── Rules about VALUES: internal repository only ──────────────────────────
# Domains, surnames, staff numbers, warehouse objects, hosts and the customer's
# brands are the very things being hidden, so they live in
# tools/public_internal.py, which deploy/public-exclude.txt keeps out of the
# slice. No file next to this one means this IS the public copy, and the rules
# below simply do not exist there. The import is by path, not by sys.path, so
# the answer does not depend on where the tool was launched from.
_INTERNAL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "public_internal.py")
INTERNAL = None
if os.path.exists(_INTERNAL_PATH):
    _spec = importlib.util.spec_from_file_location("public_internal", _INTERNAL_PATH)
    INTERNAL = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(INTERNAL)

if INTERNAL is not None:
    _I = INTERNAL
    RULES = [r for r in RULES if r.rid != "private-ip"] + [
        Rule(
            rid="internal-domain",
            why="Hosts of the live corporate perimeter. Publishing them is a map "
                "for whoever wants to reach the running system.",
            pattern=re.compile(
                r"\b(?:[A-Za-z0-9][A-Za-z0-9-]*\.)*(?:" + _I.DOMAIN_RE + r")\b",
                re.IGNORECASE),
            hint="replace with an example.com host (RFC 2606)",
            repl=_I.DOMAIN_MAP,
        ),
        Rule(
            rid="private-ip",
            why="Addresses inside the corporate network, plus the public address "
                "of the demo host. Both say where the system actually lives.",
            pattern=re.compile(
                r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
                r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
                r"|192\.168\.\d{1,3}\.\d{1,3}"
                r"|" + _I.IP_EXTRA_RE + r")\b"),
            hint="replace with a documentation address: 192.0.2.x (RFC 5737)",
            repl=_I.IP_MAP,
            allow=(r"172\.18\.0\.0/16",),
        ),
        Rule(
            rid="warehouse-object",
            why="Schema, connection and view names of the customer's warehouse. "
                "Two of them are rendered in the interface and sit in all three "
                "translation catalogues.",
            pattern=re.compile(
                r"\b(?:" + _ci(*sorted(_I.DWH_MAP, key=len, reverse=True)) + r")\b"),
            hint="rename to a neutral warehouse object (warehouse.v_*, analytics_dw)",
            repl=_I.DWH_MAP,
        ),
        Rule(
            rid="person-name",
            why="Surnames of real colleagues. Together with the e-mail and the "
                "staff number next to them this is personal data of third parties.",
            pattern=re.compile(
                r"\b(?:" + _ci(*sorted(_I.PERSON_MAP, key=len, reverse=True)) + r")\b"),
            hint="replace with an invented surname",
            repl=_I.PERSON_MAP,
        ),
        Rule(
            rid="staff-number",
            why="The directory's numeric person id. It is personal data and it is "
                "also a login — SSO sends it as the UPN.",
            pattern=re.compile(r"\b(?:" + _ci(*_I.STAFF_MAP) + r")\b"),
            hint="replace with a number from the reserved 1000000x block",
            repl=_I.STAFF_MAP,
        ),
        Rule(
            rid="infra-name",
            why="Host names, container names and the developer's own machine. "
                "The last one leaks from 808 commit author fields.",
            pattern=re.compile(
                r"\b(?:" + _ci(*sorted(_I.INFRA_MAP, key=len, reverse=True)) + r")\b"),
            hint="replace with a neutral name",
            repl=_I.INFRA_MAP,
        ),
        Rule(
            rid="customer-marker",
            why="Brands, product lines and counterparties of the customer. A "
                "machine cannot rewrite these and keep the sentence true — a "
                "person has to.",
            pattern=re.compile(_I.CUSTOMER_RE),
            hint="rewrite by hand with invented brands, or drop the file",
            repl=None,
        ),
    ]


def load_allow(path: str) -> list[tuple[str, re.Pattern[str]]]:
    """Read `tools/public_allow.txt` — same format as tools/i18n_allow.txt."""
    out: list[tuple[str, re.Pattern[str]]] = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for n, raw in enumerate(fh, 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            if ":" not in line:
                print(f"{path}:{n}: expected '<path glob> : <regex>'", file=sys.stderr)
                sys.exit(2)
            glob, rx = line.split(":", 1)
            try:
                out.append((glob.strip(), re.compile(rx.strip())))
            except re.error as exc:
                print(f"{path}:{n}: bad regex: {exc}", file=sys.stderr)
                sys.exit(2)
    return out


def allowed(rel: str, line: str, allows: list[tuple[str, re.Pattern[str]]]) -> bool:
    for glob, rx in allows:
        if fnmatch.fnmatch(rel, glob) and rx.search(line):
            return True
    return False


def load_excluded(root: str) -> list[str]:
    """Paths deploy/public-slice.sh removes before the slice is published.

    The gate's question is "would the SLICE leak", not "is every file in the
    internal repository clean". Without this it answered the second one and
    went red on CLAUDE.md and the production runbooks — files that never reach
    the public tree — so the only way to a green pipeline would have been to
    stop running the gate. A check nobody can satisfy is a check nobody runs.

    Harmless on a staging tree: those paths are already gone there.
    """
    path = os.path.join(root, "deploy", "public-exclude.txt")
    out: list[str] = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.split("#", 1)[0].strip().rstrip("/")
            if line:
                out.append(line)
    return out


def walk(root: str):
    """Yield (relpath, abspath) for every file worth reading as text."""
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir
        dirnames[:] = [
            d for d in sorted(dirnames)
            if d not in SKIP_DIRS
            and os.path.join(rel_dir, d).replace(os.sep, "/") not in SKIP_DIRS
        ]
        for name in sorted(filenames):
            ext = os.path.splitext(name)[1].lower()
            if ext in BINARY_EXT:
                continue
            abspath = os.path.join(dirpath, name)
            rel = os.path.join(rel_dir, name).replace(os.sep, "/")
            try:
                if os.path.getsize(abspath) > MAX_BYTES:
                    continue
            except OSError:
                continue
            yield rel, abspath


def read_text(path: str) -> str | None:
    try:
        with open(path, "rb") as fh:
            blob = fh.read()
    except OSError:
        return None
    if b"\0" in blob:          # binary with a text-ish extension
        return None
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError:
        return None


def apply_text(text: str, rel: str, allows) -> tuple[str, int]:
    """Scrub one file's text, line by line, honouring the allow file.

    Line granularity is not an optimisation — it is what makes --apply agree
    with --check. The allow file exempts a (path, line) pair, so the scrubber
    has to decide per line too. Before this, --apply ignored the allow file
    entirely and happily rewrote the very things the gate deliberately lets
    through: it turned migration 0012's docstring, which must name BOTH sides
    of the column it renames, into "<new> -> <new>" (a false sentence AND a
    no-op rename), and it rewrote every value inside the rule table, which
    would have shipped a scrubber that no longer knows what to scrub.
    """
    out: list[str] = []
    hits = 0
    for line in text.splitlines(keepends=True):
        if allowed(rel, line, allows):
            out.append(line)
            continue
        for rule in RULES:
            line, n = apply_rule(rule, line)
            hits += n
        out.append(line)
    return "".join(out), hits


def apply_rule(rule: Rule, text: str) -> tuple[str, int]:
    """Replace every match of one rule. Returns (new_text, count)."""
    if rule.repl is None:
        return text, 0
    n = 0

    def _sub(m: re.Match[str]) -> str:
        nonlocal n
        hit = m.group(0)
        for rx in rule.allow:
            if re.fullmatch(rx, hit):
                return hit
        if isinstance(rule.repl, str):
            n += 1
            return rule.repl
        # Longest-key mapping; an unmapped match of a mapped rule is a gap in
        # the table, and staying silent about it would be the whole failure
        # mode this tool exists to prevent.
        repl = rule.repl.get(hit)
        if repl is None:
            for key in sorted(rule.repl, key=len, reverse=True):
                if key.lower() == hit.lower():
                    repl = rule.repl[key]
                    break
        if repl is None:
            return hit
        n += 1
        return repl

    return rule.pattern.sub(_sub, text), n


def scan(root: str, allows) -> list[tuple[str, int, str, str]]:
    """Return findings as (rel, lineno, rule_id, line)."""
    findings = []
    excluded = load_excluded(root)

    def doomed(rel: str) -> bool:
        return any(rel == e or rel.startswith(e + "/") for e in excluded)

    for rel, abspath in walk(root):
        if doomed(rel):
            continue
        text = read_text(abspath)
        if text is None:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for rule in RULES:
                # A line is exempt only when EVERY match on it is exempt: one
                # allowed address on a line does not excuse the one next to it.
                hits = [h.group(0) for h in rule.pattern.finditer(line)]
                if not hits:
                    continue
                if all(any(re.fullmatch(rx, h) for rx in rule.allow) for h in hits):
                    continue
                if allowed(rel, line, allows):
                    continue
                findings.append((rel, lineno, rule.rid, line.strip()[:160]))
                break
    return findings


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("root", nargs="?", default=".", help="tree to inspect")
    ap.add_argument("--check", action="store_true",
                    help="detect only; exit 1 if anything is found")
    ap.add_argument("--apply", action="store_true",
                    help="rewrite internal values in place")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --apply: report what would change, write nothing")
    ap.add_argument("--allow", default=None,
                    help="allow file (default: <root>/tools/public_allow.txt, "
                         "falling back to this script's sibling)")
    args = ap.parse_args(argv)

    if args.check == args.apply:
        ap.error("choose exactly one of --check / --apply")
    if args.apply and INTERNAL is None:
        ap.error("--apply needs tools/public_internal.py next to this script; "
                 "the public copy can only --check")

    root = os.path.abspath(args.root)
    allow_path = args.allow or os.path.join(root, "tools", "public_allow.txt")
    if not os.path.exists(allow_path):
        allow_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "public_allow.txt")
    allows = load_allow(allow_path)
    # Internal exemptions (migrations that must name a column they rename,
    # the internal value table itself) live in a second file that the slice
    # excludes. Absent in the public copy, and then there is nothing to exempt.
    allows += load_allow(os.path.join(os.path.dirname(allow_path),
                                      "public_allow.internal.txt"))

    if args.check:
        findings = scan(root, allows)
        if not findings:
            print("public gate: clean")
            return 0
        by_rule: dict[str, list] = {}
        for rel, lineno, rid, line in findings:
            by_rule.setdefault(rid, []).append((rel, lineno, line))
        rules = {r.rid: r for r in RULES}
        for rid, items in sorted(by_rule.items(), key=lambda kv: -len(kv[1])):
            rule = rules[rid]
            print(f"\n── {rid}: {len(items)} finding(s) "
                  f"in {len({i[0] for i in items})} file(s)")
            print(f"   why:  {rule.why}")
            print(f"   fix:  {rule.hint}")
            for rel, lineno, line in items[:12]:
                print(f"   {rel}:{lineno}: {line}")
            if len(items) > 12:
                print(f"   … and {len(items) - 12} more")
        print(f"\npublic gate: {len(findings)} finding(s) — NOT publishable")
        return 1

    # --apply
    changed_files = 0
    changed_hits = 0
    for rel, abspath in walk(root):
        text = read_text(abspath)
        if text is None:
            continue
        new, hits = apply_text(text, rel, allows)
        if hits and new != text:
            changed_files += 1
            changed_hits += hits
            if args.dry_run:
                print(f"would rewrite {rel}: {hits} replacement(s)")
            else:
                with open(abspath, "w", encoding="utf-8") as fh:
                    fh.write(new)
                print(f"rewrote {rel}: {hits} replacement(s)")
    verb = "would change" if args.dry_run else "changed"
    print(f"public sanitize: {verb} {changed_hits} value(s) in {changed_files} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
