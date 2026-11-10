#!/usr/bin/env python3
"""Collect every translatable key into app/locales/en.json.

It does NOT rewrite code — it only reads calls that are already wrapped:

    Python    _("…"), _n(n, "…"), _h("…"), i18n.gettext("…")
    Jinja     {{ _('…') }}, {{ _h('…') }}, {{ _n(n, '…') }}, {% trans %}…{% endtrans %}
    JS        t('…'), tn(n, '…'), tinto(el, '…')

`en.json` maps every key to itself. That file is the register of source strings:
`i18n_check.py` measures ru/zh-HK against it, `i18n_merge.py --split` cuts the
translator chunks from it, and nothing reads it at runtime (an English page falls
through to the key, which IS the English text).

    python3 tools/i18n_extract.py            # rewrite en.json + _refs.json
    python3 tools/i18n_extract.py --check    # exit 1 if they are out of date

Run it from the repository root.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCALES = ROOT / "app" / "locales"

PY_DIRS = ["app"]
JS_DIR = ROOT / "static" / "js"
TPL_DIR = ROOT / "templates"

SKIP_JS_PARTS = {"vendor"}
SKIP_JS_NAMES = {"i18n.js"}


# ---------------------------------------------------------------------------
# String literal decoding
# ---------------------------------------------------------------------------

_JS_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f",
               "v": "\v", "0": "\0", "\\": "\\", "'": "'", '"': '"', "`": "`",
               "\n": ""}


def js_unescape(s: str) -> str:
    out = []
    i = 0
    while i < len(s):
        c = s[i]
        if c != "\\" or i + 1 >= len(s):
            out.append(c)
            i += 1
            continue
        nxt = s[i + 1]
        if nxt == "u":
            if i + 2 < len(s) and s[i + 2] == "{":
                end = s.index("}", i + 2)
                out.append(chr(int(s[i + 3:end], 16)))
                i = end + 1
            else:
                out.append(chr(int(s[i + 2:i + 6], 16)))
                i += 6
            continue
        if nxt == "x":
            out.append(chr(int(s[i + 2:i + 4], 16)))
            i += 4
            continue
        out.append(_JS_ESCAPES.get(nxt, nxt))
        i += 2
    return "".join(out)


# ---------------------------------------------------------------------------
# Collectors
# ---------------------------------------------------------------------------

class Keys:
    def __init__(self):
        self.refs: dict[str, list[str]] = {}
        self.plural: set[str] = set()
        self.errors: list[str] = []

    def add(self, key: str, ref: str, plural: bool = False) -> None:
        self.refs.setdefault(key, []).append(ref)
        if plural:
            # Only a key reached through tn()/_n() may carry `|` separators;
            # anywhere else a pipe is just a character (i18n_check.py checks it).
            self.plural.add(key)


# `N_` marks a literal that lives in a module constant and is translated later,
# at the point the constant is serialised. For the extractor it is a key like
# any other — that is the whole purpose of the marker.
PY_FUNCS = {"_", "gettext", "_h", "gettext_html", "N_"}
PY_NFUNCS = {"_n", "ngettext"}


def py_literal(node: ast.AST) -> str | None:
    """The string a key argument spells out, or None if it is not a literal.

    Adjacent literals over several lines are already one `Constant` (Python
    folds implicit concatenation), but `"a" + "b"` is not — and a key written
    that way is still perfectly readable, so it is folded here too. An f-string
    is deliberately NOT a literal: see `scan_python`."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = py_literal(node.left), py_literal(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def scan_python(keys: Keys) -> None:
    for d in PY_DIRS:
        for f in sorted((ROOT / d).rglob("*.py")):
            # The implementation of _() itself, and the revisions.
            if "migrations" in f.parts or f.name == "i18n.py":
                continue
            try:
                tree = ast.parse(f.read_text(encoding="utf-8"))
            except SyntaxError as e:
                keys.errors.append(f"{f}: {e}")
                continue
            rel = str(f.relative_to(ROOT))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = node.func.attr if isinstance(node.func, ast.Attribute) else \
                    getattr(node.func, "id", None)
                if name in PY_FUNCS and node.args:
                    arg, plural = node.args[0], False
                elif name in PY_NFUNCS and len(node.args) >= 2:
                    arg, plural = node.args[1], True
                else:
                    continue
                literal = py_literal(arg)
                if literal is not None:
                    keys.add(literal, f"{rel}:{node.lineno}", plural)
                elif isinstance(arg, ast.JoinedStr):
                    # An f-string bakes the value INTO the key, so every run
                    # produces a key no catalogue can ever hold. The whole
                    # sentence belongs in the key with `{name}` placeholders,
                    # and the values go in as keyword arguments.
                    keys.errors.append(
                        f"{rel}:{node.lineno}: {name}() called with an f-string — "
                        f"pass the value as {name}('… {{x}} …', x=…) instead")
                else:
                    keys.errors.append(
                        f"{rel}:{node.lineno}: {name}() called with a non-literal key")


# `_('…')`, `_h('…')` and `_n(n, '…')` in templates. The key is always a
# single-quoted literal — that is what the extraction produced and what
# i18n_check.py enforces.
TPL_CALL = re.compile(r"\b(_h|_n|_)\(\s*(?:[^,()'\"]+,\s*)?'((?:\\.|[^'])*)'")
TPL_TRANS = re.compile(r"\{%-?\s*trans\b[^%]*%\}(.*?)\{%-?\s*endtrans\s*-?%\}", re.S)


def jinja_unescape(s: str) -> str:
    return s.replace("\\'", "'").replace('\\\\', '\\')


def scan_templates(keys: Keys) -> None:
    for f in sorted(TPL_DIR.rglob("*.html")):
        src = f.read_text(encoding="utf-8")
        rel = str(f.relative_to(ROOT))
        # inline <script> is JS, and is scanned by scan_js's regexes below
        for m in TPL_CALL.finditer(src):
            keys.add(jinja_unescape(m.group(2)),
                     f"{rel}:{src.count(chr(10), 0, m.start()) + 1}", m.group(1) == "_n")
        for m in TPL_TRANS.finditer(src):
            body = re.sub(r"\s+", " ", m.group(1)).strip()
            if body:
                keys.add(body, f"{rel}:{src.count(chr(10), 0, m.start()) + 1}")
        for m in JS_CALL.finditer(src):
            keys.add(js_key(m), f"{rel}:{src.count(chr(10), 0, m.start()) + 1}",
                     m.group("fn") == "tn")


JS_CALL = re.compile(
    r"(?<![\w.$])(?P<fn>tinto|tn|t)\(\s*"
    r"(?:(?P<n>[^,'\"`()]+|\([^()]*\))\s*,\s*)?"
    r"(?P<q>['\"])(?P<s>(?:\\.|(?!(?P=q)).)*)(?P=q)"
    # A key split over several lines with `+` — how a three-form plural is
    # usually written. The pieces belong to ONE key.
    r"(?P<more>(?:\s*\+\s*(?P=q)(?:\\.|(?!(?P=q)).)*(?P=q))*)")


PIECE = re.compile(r"(?P<q>['\"])(?P<s>(?:\\.|(?!(?P=q)).)*)(?P=q)")


def js_key(m: re.Match) -> str:
    """The key of one t()/tn() match, `+`-joined pieces included."""
    parts = [m.group("s")] + [p.group("s") for p in PIECE.finditer(m.group("more") or "")]
    return js_unescape("".join(parts))


def scan_js(keys: Keys) -> None:
    for f in sorted(JS_DIR.rglob("*.js")):
        if SKIP_JS_PARTS & set(f.parts) or f.name in SKIP_JS_NAMES:
            continue
        src = f.read_text(encoding="utf-8")
        rel = str(f.relative_to(ROOT))
        for m in JS_CALL.finditer(src):
            fn = m.group("fn")
            if fn in ("tn", "tinto") and m.group("n") is None:
                keys.errors.append(f"{rel}: {fn}() without its leading argument")
                continue
            if fn == "t" and m.group("n") is not None:
                continue        # not our t(): something else took the name
            keys.add(js_key(m), f"{rel}:{src.count(chr(10), 0, m.start()) + 1}",
                     fn == "tn")


def collect() -> Keys:
    keys = Keys()
    scan_python(keys)
    scan_templates(keys)
    scan_js(keys)
    return keys


def write(keys: Keys) -> tuple[str, str]:
    catalog = {k: k for k in sorted(keys.refs)}
    refs = {k: {"refs": keys.refs[k], "plural": k in keys.plural}
            for k in sorted(keys.refs)}
    return (json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            json.dumps(refs, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="do not write; fail if en.json/_refs.json are stale")
    args = ap.parse_args()

    keys = collect()
    catalog_text, refs_text = write(keys)
    ru = LOCALES / "en.json"
    refs = LOCALES / "_refs.json"

    if args.check:
        stale = []
        for path, text in ((ru, catalog_text), (refs, refs_text)):
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                stale.append(path.name)
        for e in keys.errors:
            print(f"error: {e}")
        if stale:
            print(f"error: stale {', '.join(stale)} — run tools/i18n_extract.py")
        return 1 if stale or keys.errors else 0

    LOCALES.mkdir(parents=True, exist_ok=True)
    ru.write_text(catalog_text, encoding="utf-8")
    refs.write_text(refs_text, encoding="utf-8")
    for e in keys.errors:
        print(f"error: {e}")
    print(f"{len(keys.refs)} keys -> {ru.relative_to(ROOT)}")
    return 1 if keys.errors else 0


if __name__ == "__main__":
    sys.exit(main())
