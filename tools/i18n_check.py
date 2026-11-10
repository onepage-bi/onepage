#!/usr/bin/env python3
"""The i18n linter: no Russian UI text in the sources, no broken keys.

    python3 tools/i18n_check.py            # exit 1 on any violation
    python3 tools/i18n_check.py --stats    # also print translation coverage

What it enforces, and why each rule exists:

1. **Cyrillic outside a wrapped call.** The DOM tests cannot catch a MISSED
   string — an unwrapped Russian literal renders exactly like a wrapped one — so
   this rule is the only thing standing between "translated" and "mostly
   translated". It covers `templates/`, `static/js/`, `app/api/` and
   `app/services/` — the last one because a service message reaches the browser
   as JSON data (`{"reason": …}`, `HTTPException(detail=…)`) and so never
   passes through a template. Comments, docstrings, log calls and the key
   literals themselves are removed first;
   whatever Cyrillic is left is a leak. Note that this deliberately still sees
   inside `t('…', {p: '<Russian text>'})`: a parameter is data, and Russian data
   passed into a key is the same leak by another route.
2. **Allow-list** (tools/i18n_allow.txt) for the places where Russian text is
   data, not interface: NLP stems, transliteration tables, LLM prompts.
3. **ru/zh-HK against en**: unknown keys (obsolete), and a coverage figure.
   A key that carries Cyrillic is an error since 2026-11-03: the key IS the
   English source (tools/i18n_invert.py flipped the catalogues).
4. **Formal checks**: a translation may not invent `{placeholders}`, must have
   the number of `|` forms its language uses, `|` may not appear in a key that
   is not a plural, the JSON files are sorted, and static/locales/*.js matches
   the JSON it is generated from.
5. **Non-constant keys**: `t(variable)` is invisible to the extractor.
6. **UI text compared to a Russian literal** (`x.textContent === '<Russian text>'`):
   silently false as soon as the interface is not Russian.

Run it from the repository root.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCALES = ROOT / "app" / "locales"
CLIENT = ROOT / "static" / "locales"
ALLOW_FILE = ROOT / "tools" / "i18n_allow.txt"

sys.path.insert(0, str(ROOT))
from app import i18n  # noqa: E402  (LANGS, plural_index)

CYR = re.compile(r"[\u0400-\u04FF]")

# Files scanned by rule 1. The report generators (`engine/`, `agent/`) are NOT
# in here: they render Russian workbooks, not the platform interface.
SCAN_GLOBS = [
    ("templates", "**/*.html"),
    ("static/js", "**/*.js"),
    ("app/api", "*.py"),
    ("app/services", "*.py"),
]
SKIP_PARTS = {"vendor"}
SKIP_NAMES = {"i18n.js"}


# ---------------------------------------------------------------------------
# A small JS/HTML scanner: enough to know code from string from comment
# ---------------------------------------------------------------------------

def strip_js_comments(src: str) -> str:
    """Blank out // and /* */ without touching strings, templates or regexes."""
    out = list(src)
    i, n = 0, len(src)
    prev = ""
    while i < n:
        c = src[i]
        if c in "\"'":
            q, j = c, i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == q or src[j] == "\n":
                    break
                j += 1
            i = j + 1
            prev = "str"
            continue
        if c == "`":
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == "`":
                    break
                j += 1
            i = j + 1
            prev = "str"
            continue
        if c == "/" and i + 1 < n and src[i + 1] in "/*":
            end = src.find("\n", i) if src[i + 1] == "/" else src.find("*/", i + 2) + 2
            if end <= 0:
                end = n
            for k in range(i, end):
                if out[k] != "\n":
                    out[k] = " "
            i = end
            continue
        if c == "/" and prev in ("(", ",", "=", ":", "[", "!", "&", "|", "?", "{",
                                 "}", ";", "\n", "return", "", "+"):
            j = i + 1
            klass = False
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == "[":
                    klass = True
                elif src[j] == "]":
                    klass = False
                elif src[j] == "/" and not klass:
                    break
                elif src[j] == "\n":
                    break
                j += 1
            for k in range(i, min(j + 1, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j + 1
            prev = "regex"
            continue
        if not c.isspace():
            prev = c
        i += 1
    return "".join(out)


# Wrapped calls whose KEY literal is removed before the Cyrillic hunt. The
# parameter object is deliberately left in place (see rule 1 above).
CALL_KEY = re.compile(
    r"(?<![\w.$])(?:i18n\.)?(?:tinto|tn|t|_n|_h|_m|_|N_|gettext|ngettext|gettext_html"
    r"|translate_marked)\(\s*"
    r"(?:[^'\"`]*?,\s*)?"          # tn()'s count / tinto()'s parent, whatever shape it has
    r"(?P<q>['\"])(?P<s>(?:\\.|(?!(?P=q)).)*)(?P=q)"
    # a key written as several `+`-joined literals (the usual plural shape)
    r"(?:\s*\+\s*(?P=q)(?:\\.|(?!(?P=q)).)*(?P=q))*")

TRANS_BLOCK = re.compile(r"\{%-?\s*trans\b.*?\{%-?\s*endtrans\s*-?%\}", re.S)
JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.S)
PY_COMMENT = re.compile(r"(?m)#.*$")
HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)


def blank(m: re.Match) -> str:
    """Replace a match with same-length whitespace so line numbers survive."""
    return re.sub(r"[^\n]", " ", m.group(0))


def strip_for_scan(path: Path, src: str) -> str:
    suffix = path.suffix
    if suffix == ".js":
        src = strip_js_comments(src)
    elif suffix == ".html":
        src = JINJA_COMMENT.sub(blank, src)
        src = HTML_COMMENT.sub(blank, src)
        src = TRANS_BLOCK.sub(blank, src)
        src = strip_js_comments(src)      # inline <script>
    elif suffix == ".py":
        src = strip_py_logs(strip_py(src))
    return CALL_KEY.sub(blank_keys, src)


LITERAL = re.compile(r"(?P<q>['\"])(?P<s>(?:\\.|(?!(?P=q)).)*)(?P=q)")


def blank_keys(m: re.Match) -> str:
    """Blank every literal of a translated call's key, keeping its length so
    line and column numbers still line up. The PARAMETER object is untouched
    on purpose: Russian text passed in as a parameter is a leak too."""
    call = m.group(0)
    head = call[:call.index(m.group("q"))]
    tail = LITERAL.sub(lambda p: p.group("q") + " " * len(p.group("s")) + p.group("q"),
                       call[len(head):])
    return head + tail


# Diagnostics, not interface: nobody translates a log line, and the operator
# reading `docker logs` is the same person in every locale.
LOG_CALLS = {"print", "debug", "info", "warning", "warn", "error", "exception",
             "critical", "log"}


def strip_py_logs(src: str) -> str:
    """Blank the ARGUMENTS of `print(…)` / `log.info(…)` / `logger.error(…)`.

    Only the arguments: the call itself stays so the line still parses in the
    reader's head, and only these names, so an ordinary `.error(...)` method on
    a domain object is not silently exempted — it has to be one of the logging
    verbs to count."""
    import ast
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return src
    lines = [0]
    for ln in src.splitlines(keepends=True):
        lines.append(lines[-1] + len(ln))
    out = list(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else \
            getattr(node.func, "id", None)
        if name not in LOG_CALLS:
            continue
        first, last = node.args[0], node.args[-1]
        a = lines[first.lineno - 1] + first.col_offset
        b = lines[last.end_lineno - 1] + last.end_col_offset
        for k in range(a, min(b, len(out))):
            if out[k] != "\n":
                out[k] = " "
    return "".join(out)


def strip_py(src: str) -> str:
    """Blank comments and docstrings; a module's prose is not the interface."""
    import io
    import tokenize
    out = list(src)
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, IndentationError):
        return PY_COMMENT.sub(blank, src)
    lines = [0]
    for ln in src.splitlines(keepends=True):
        lines.append(lines[-1] + len(ln))
    prev_type = tokenize.INDENT
    for tok in toks:
        is_doc = tok.type == tokenize.STRING and prev_type in (
            tokenize.INDENT, tokenize.DEDENT, tokenize.NEWLINE, tokenize.NL,
            tokenize.ENCODING)
        if tok.type == tokenize.COMMENT or is_doc:
            a = lines[tok.start[0] - 1] + tok.start[1]
            b = lines[tok.end[0] - 1] + tok.end[1]
            for k in range(a, min(b, len(out))):
                if out[k] != "\n":
                    out[k] = " "
        if tok.type not in (tokenize.COMMENT,):
            prev_type = tok.type
    return "".join(out)


# ---------------------------------------------------------------------------
# Allow-list
# ---------------------------------------------------------------------------

def load_allow() -> list[tuple[str, re.Pattern]]:
    rules = []
    if not ALLOW_FILE.exists():
        return rules
    for raw in ALLOW_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        path_glob, _, pattern = line.partition(":")
        rules.append((path_glob.strip(), re.compile(pattern.strip())))
    return rules


def allowed(rules, rel: str, line: str) -> bool:
    return any(fnmatch.fnmatch(rel, g) and p.search(line) for g, p in rules)


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

NONCONST = re.compile(
    r"(?<![\w.$])(?<!function )(?:t|_)\(\s*(?![\'\"])(?!\s*\))[A-Za-z_$`]")
# A literal locale tag pins the date/number format to one language: invisible
# in ru (it looks right) and wrong everywhere else — the owner saw a Russian
# weekday and date ("Tuesday, 7 September" in Russian) on a zh-HK page. static/js/i18n.js owns the only lang -> BCP-47
# table there is (SKIP_NAMES keeps it out of this scan); everyone else asks
# OP_I18N.fmtDate / OP_I18N.fmtNum, which read the page's own language.
HARD_LOCALE = re.compile(
    r"""['"](?:ru-RU|en-US|en-GB|zh-Hant-HK)['"]"""
    r"""|\.toLocale(?:Date|Time)?String\(\s*['"]""")

COMPARE = re.compile(r"(?:===|!==|==|!=)\s*(['\"])[^'\"]*[\u0400-\u04FF][^'\"]*\1"
                     r"|(['\"])[^'\"]*[\u0400-\u04FF][^'\"]*\2\s*(?:===|!==|==|!=)")


def scan_sources(errors: list[str]) -> None:
    rules = load_allow()
    for base, pattern in SCAN_GLOBS:
        for f in sorted((ROOT / base).glob(pattern)):
            if SKIP_PARTS & set(f.parts) or f.name in SKIP_NAMES:
                continue
            rel = str(f.relative_to(ROOT))
            src = f.read_text(encoding="utf-8")
            stripped = strip_for_scan(f, src)
            for i, line in enumerate(stripped.splitlines(), 1):
                raw = src.splitlines()[i - 1]
                if CYR.search(line) and not allowed(rules, rel, raw):
                    errors.append(f"{rel}:{i}: Russian text outside a translated call: {raw.strip()[:90]}")
                if f.suffix in (".js", ".html") and COMPARE.search(line) and \
                        not allowed(rules, rel, raw):
                    errors.append(f"{rel}:{i}: UI text compared to a Russian literal: {raw.strip()[:90]}")
                if f.suffix in (".js", ".html") and HARD_LOCALE.search(line) and \
                        not allowed(rules, rel, raw):
                    errors.append(f"{rel}:{i}: hard-coded locale — use OP_I18N.fmtDate/"
                                  f"fmtNum: {raw.strip()[:90]}")
            for m in NONCONST.finditer(stripped):
                ln = stripped.count("\n", 0, m.start()) + 1
                raw = src.splitlines()[ln - 1]
                if allowed(rules, rel, raw):
                    continue
                errors.append(f"{rel}:{ln}: translated call with a non-literal key — "
                              f"the extractor cannot see it: {raw.strip()[:90]}")


# ---------------------------------------------------------------------------
# Rule 7: Latin interface text in a template
# ---------------------------------------------------------------------------
# Part of the interface was written in English from the start — «Dashboards»,
# «Workspace», «Audit log» — and a Russian page showed it in English, which
# nobody minded. zh-HK does mind: the owner saw English chrome around Chinese
# content. So an English label is a translatable string like any other, and its
# key is the English source (ru and en map it to itself, so both keep rendering
# byte-for-byte what they rendered before).
#
# Rule 1 cannot see these — they carry no Cyrillic — so this rule reads the
# template text nodes and the four human-readable attributes directly. What is
# NOT interface: a product name, an acronym, a unit, a code. Those are the
# same in every language and stay bare.

PRODUCT_WORDS = {
    # product and vendor names
    "onepage", "op", "greenplum", "duckdb", "excel", "power", "bi", "deepseek",
    "ollama", "sheetjs", "plotly", "echarts", "sqlite", "postgres", "docker",
    # formats, protocols, acronyms
    "json", "csv", "sql", "html", "xlsx", "xls", "css", "js", "api", "url",
    "http", "https", "id", "uuid", "upn", "sso", "adfs", "saml", "rls", "dq",
    "kpi", "utc", "iso", "md5", "sha", "svg", "png", "pdf", "ui", "ux", "db",
    # units and measures
    "ms", "kb", "mb", "gb", "px", "rps", "qps", "p50", "p95", "p99", "v1", "v2",
    "jsonl", "markdown", "mermaid", "rest", "htm", "xlsb", "ods", "esc",
    # `person_id` is the corporate account id — a field name, never a label
    "person_id",
}
# A word made only of digits, punctuation or a single letter is never a label.
# The underscore is part of the word: `person_id`, `dash_type`, `created_at` are ONE
# identifier, not two words. They used to be cut in half, and to let a field
# name through, its halves ("person", "id") would have had to go into PRODUCT_WORDS,
# silencing real labels where those words stand on their own.
LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_'’-]*")
JINJA_EXPR = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.S)
SCRIPT_STYLE = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)
TEXT_NODE = re.compile(r">([^<>]+)<")
HUMAN_ATTR = re.compile(
    r"""\b(?:title|placeholder|aria-label|alt)\s*=\s*(?P<q>["'])(?P<v>[^"'<>]*)(?P=q)""")


ENTITY = re.compile(r"&(?:#\d+|#x[0-9a-fA-F]+|\w+);")

# Markup that says "this is code, not prose": <code>/<pre>/<kbd>, anything the
# stylesheet sets in the mono face, and the docs' key column (`class="k"`, the
# left half of a definition row — an event name, a path, an HTTP verb). Text
# inside these is an identifier by construction and has no translation.
CODE_TAG = re.compile(
    r"<(?:code|pre|kbd|samp|var|textarea)\b"
    r"|<[a-z]+\b[^<>]*\bclass\s*=\s*[\"'][^\"']*\b(?:mono|k)\b")

# templates/pages/viewer.html is the dashboard viewer, and a parallel task owns
# its chat panel right now; wrapping its chrome would collide. It is the one
# file this rule skips, and it comes back the moment that task lands.
LATIN_SKIP = {"templates/pages/viewer.html"}


def in_code(masked: str, pos: int) -> bool:
    """Is the text node starting at `pos` (the `>` of its opening tag) inside
    one of those?"""
    start = masked.rfind("<", 0, pos)
    if start < 0:
        return False
    return bool(CODE_TAG.match(masked[start:pos + 1]))


def latin_label(text: str) -> str:
    """The offending label, or "" when the text is not interface at all."""
    text = ENTITY.sub(" ", text)
    words = LATIN_WORD.findall(text)
    if not words:
        return ""
    # Every word a product name / acronym / unit -> not a sentence, not a label.
    if all(w.lower() in PRODUCT_WORDS for w in words):
        return ""
    # A single word of one or two letters is a code, not a label.
    if len(words) == 1 and len(words[0]) <= 2:
        return ""
    return " ".join(text.split())[:70]


def scan_templates_latin(errors: list[str]) -> None:
    rules = load_allow()
    for f in sorted((ROOT / "templates").rglob("*.html")):
        rel = str(f.relative_to(ROOT))
        if rel in LATIN_SKIP:
            continue
        src = f.read_text(encoding="utf-8")
        raw_lines = src.splitlines()
        # Jinja output is data or an already-translated call; script/style are
        # not markup text. Blank them, keeping the line numbering.
        masked = JINJA_COMMENT.sub(blank, src)
        masked = HTML_COMMENT.sub(blank, masked)
        masked = SCRIPT_STYLE.sub(blank, masked)
        masked = TRANS_BLOCK.sub(blank, masked)
        masked = JINJA_EXPR.sub(blank, masked)
        for m in list(TEXT_NODE.finditer(masked)) + list(HUMAN_ATTR.finditer(masked)):
            is_attr = "v" in (m.groupdict() or {})
            text = m.group("v") if is_attr else m.group(1)
            label = latin_label(text)
            if not label:
                continue
            if not is_attr and in_code(masked, m.start()):
                continue
            # A text node often starts on the line AFTER its opening tag. Point
            # at the text itself, so the report is readable and the allow-list
            # is matched against the line that actually carries the string.
            body = m.start("v") if is_attr else m.start(1)
            body += len(text) - len(text.lstrip())
            ln = masked.count("\n", 0, body) + 1
            raw = raw_lines[ln - 1] if ln <= len(raw_lines) else ""
            if allowed(rules, rel, raw):
                continue
            errors.append(f"{rel}:{ln}: English interface text outside a translated "
                          f"call: {label}")


# ---------------------------------------------------------------------------
# Catalogues
# ---------------------------------------------------------------------------

FORMS = {"ru": 3, "en": 2, "zh-HK": 1}
PARAM = re.compile(r"\{(\w+)\}")


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def check_catalogues(errors: list[str], stats: bool) -> None:
    ru = load_json(LOCALES / (i18n.DEFAULT + ".json"))      # the register, key -> key
    if not ru:
        errors.append("app/locales/en.json is missing or empty — run tools/i18n_extract.py")
        return
    for key in ru:
        if CYR.search(key):
            errors.append(f"en.json: key carries Cyrillic — the key is the English source: {key[:60]}")
    # Which keys are plural is decided by the CALL (tn/_n), not by a stray `|`:
    # a documentation paragraph may legitimately contain a pipe.
    refs = load_json(LOCALES / "_refs.json")
    plural_keys = {k for k, v in refs.items() if isinstance(v, dict) and v.get("plural")}
    for key in plural_keys:
        n = len(key.split("|"))
        # An English source carries two forms — and app/i18n.py reads the
        # count the same way when the key falls through untranslated.
        want = FORMS["en"]
        if n != want:
            errors.append(f"en.json: plural key has {n} forms, English needs "
                          f"{want}: {key[:60]}")
    for lang in i18n.LANGS:
        path = LOCALES / f"{lang}.json"
        if lang == i18n.DEFAULT:
            continue
        data = load_json(path)
        text = path.read_text(encoding="utf-8") if path.exists() else "{}\n"
        want = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if text != want:
            errors.append(f"{path.name}: not sorted/formatted — run tools/i18n_merge.py")
        unknown = [k for k in data if k not in ru]
        for k in unknown[:10]:
            errors.append(f"{path.name}: key is not in en.json (obsolete): {k[:60]}")
        if len(unknown) > 10:
            errors.append(f"{path.name}: … and {len(unknown) - 10} more obsolete keys")
        for key, value in data.items():
            if key not in ru:
                continue
            extra = set(PARAM.findall(value)) - set(PARAM.findall(key))
            if extra:
                errors.append(f"{path.name}: translation invents {sorted(extra)}: {key[:50]}")
            if value == key:
                continue                     # not translated yet — see i18n.load()
            if key in plural_keys:
                n = len(value.split("|"))
                if n != FORMS[lang]:
                    errors.append(f"{path.name}: {n} plural forms, {lang} needs "
                                  f"{FORMS[lang]}: {key[:50]}")
            elif "|" in value and "|" not in key:
                # A pipe the source string never had — likely a plural marker
                # the translator invented. A pipe already in the English
                # source (a literal "|" in prose, a table header) is fine.
                errors.append(f"{path.name}: '|' in a non-plural translation: {key[:50]}")
        translated = sum(1 for k, v in data.items() if k in ru and v and v != k)
        if stats:
            pct = 100.0 * translated / len(ru) if ru else 0.0
            print(f"{lang:6s} {translated:5d}/{len(ru)} translated "
                  f"({pct:5.1f}%), missing {len(ru) - translated}")

    # The client bundles are generated from the JSON; a stale one would ship a
    # catalogue that no longer matches the server's.
    for lang in i18n.LANGS:
        js = CLIENT / f"{lang}.js"
        data = {} if lang == i18n.DEFAULT else {
            k: v for k, v in load_json(LOCALES / f"{lang}.json").items()
            if k in ru and v and v != k}
        want = client_bundle(data)
        if not js.exists() or js.read_text(encoding="utf-8") != want:
            errors.append(f"static/locales/{lang}.js is stale — "
                          f"run tools/i18n_merge.py --client")


def client_bundle(data: dict) -> str:
    body = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return ("/* Generated by tools/i18n_merge.py --client. Do not edit. */\n"
            "window.OP_I18N_CATALOG=" + body + ";\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", action="store_true", help="print coverage per language")
    args = ap.parse_args()

    errors: list[str] = []
    scan_sources(errors)
    scan_templates_latin(errors)
    check_catalogues(errors, args.stats)

    for e in errors:
        print(e)
    if errors:
        print(f"\n{len(errors)} problem(s)")
        return 1
    print("i18n: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
