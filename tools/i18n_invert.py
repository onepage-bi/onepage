#!/usr/bin/env python3
"""One-shot inversion of the i18n catalogues: the key becomes the ENGLISH
source string, Russian becomes an ordinary translation.

Before (2026-11-03): key = Russian source, `ru` is the default language with
an empty catalogue, `en.json` / `zh-HK.json` translate Russian keys.
After: key = English source, `en` is the default with an empty catalogue,
`ru.json` / `zh-HK.json` translate English keys.

    python3 tools/i18n_invert.py --plan     # report only: collisions, gaps
    python3 tools/i18n_invert.py --apply    # rewrite sources + catalogues

What --apply does, in order:
  1. builds old-key → new-key from app/locales/en.json (a Russian key whose
     English value is missing or equal to the key is a hard error — fill it
     first); a `{ctx:…}` suffix on the old key is carried onto the new one;
  2. rewrites every wrapped literal in app/**/*.py, static/js/**/*.js and
     templates/**/*.html, using the same recognisers as tools/i18n_extract.py
     (so what the extractor sees is exactly what gets rewritten);
  3. writes app/locales/en.json (register, key→key), ru.json (new key →
     Russian text, ctx stripped) and zh-HK.json (new key → Chinese or the
     untranslated marker);
  4. best-effort: replaces exact old keys quoted inside tests/**/*.{py,js}.
The code of app/i18n.py, static/js/i18n.js and the i18n tools is adjusted by
hand in the same commit — this script only moves strings.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import i18n_extract as X  # noqa: E402  — regexes and py_literal

LOCALES = ROOT / "app" / "locales"
CYR = re.compile(r"[\u0400-\u04FF]")
CTX = re.compile(r"\s*(\{ctx:[^}]*\})")
PARAM = re.compile(r"\{(\w+)\}")


def load(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def dump(p: Path, d: dict) -> None:
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                 encoding="utf-8")


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------

def build_map(ru: dict, en: dict, overrides: dict) -> tuple[dict, list[str]]:
    """old key → new key. Problems are returned, not raised, so --plan can
    list them all at once."""
    mapping: dict[str, str] = {}
    problems: list[str] = []
    for old in ru:
        if not CYR.search(old):
            mapping[old] = old            # already an English key
            continue
        new = overrides.get(old) or en.get(old)
        if not new or new == old:
            problems.append(f"untranslated: {old[:90]!r}")
            continue
        if CYR.search(new):
            problems.append(f"english value still has Cyrillic: {old[:60]!r} -> {new[:60]!r}")
            continue
        m = CTX.search(old)
        if m and "{ctx:" not in new:
            new = new + " " + m.group(1)
        # placeholders: every {param} of the old key should survive, or the
        # Russian translation would "invent" one from the new key's viewpoint
        lost = set(PARAM.findall(old)) - set(PARAM.findall(new)) - {"ctx"}
        lost = {p for p in lost if not p.startswith("ctx")}
        if lost:
            problems.append(f"placeholders lost {sorted(lost)}: {old[:60]!r} -> {new[:60]!r}")
        mapping[old] = new
    # Collisions. Two sources can land on one English key:
    #   * an English-chrome key that already IS that string plus ONE Russian
    #     key that translates to it — merging them is the point of the
    #     inversion (both rendered the same English; ru keeps the Russian);
    #   * several Russian keys with one English rendering (three Russian
    #     verbs for "cancel" all became Cancel). Russian grammar differs per
    #     place, so each extra one keeps its own key with a `{ctx:N}` suffix —
    #     invisible on screen (gettext strips it), distinct in the catalogue.
    by_new: dict[str, list[str]] = {}
    for old, new in mapping.items():
        by_new.setdefault(new, []).append(old)
    for new, olds in by_new.items():
        rus = sorted(o for o in olds if CYR.search(o))
        if len(rus) <= 1:
            continue
        for i, old in enumerate(rus[1:], 2):
            mapping[old] = f"{new} {{ctx:{i}}}"
    return mapping, problems


# ---------------------------------------------------------------------------
# Literal writers
# ---------------------------------------------------------------------------

def py_lit(s: str, q: str) -> str:
    out = s.replace("\\", "\\\\").replace(q, "\\" + q).replace("\n", "\\n").replace("\t", "\\t")
    return q + out + q


def js_lit(s: str, q: str) -> str:
    out = s.replace("\\", "\\\\").replace(q, "\\" + q).replace("\n", "\\n")
    return q + out + q


def tpl_lit(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


# ---------------------------------------------------------------------------
# Rewriters — each returns (new_text, replaced, skipped)
# ---------------------------------------------------------------------------

def offsets(src: str) -> list[int]:
    off = [0]
    for line in src.splitlines(keepends=True):
        off.append(off[-1] + len(line))
    return off


def rewrite_python(src: str, mapping: dict) -> tuple[str, int, list[str]]:
    """ast reports col_offset / end_col_offset in UTF-8 BYTES, so the slicing
    is done on the encoded source — a Russian comment earlier on the line
    would otherwise shift every cut (it did, on the first run)."""
    tree = ast.parse(src)
    raw = src.encode("utf-8")
    off = [0]
    for line in raw.splitlines(keepends=True):
        off.append(off[-1] + len(line))
    edits: list[tuple[int, int, str]] = []
    skipped: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
        if name in X.PY_FUNCS and node.args:
            arg = node.args[0]
        elif name in X.PY_NFUNCS and len(node.args) >= 2:
            arg = node.args[1]
        else:
            continue
        lit = X.py_literal(arg)
        if lit is None or lit not in mapping or mapping[lit] == lit:
            continue
        a = off[arg.lineno - 1] + arg.col_offset
        b = off[arg.end_lineno - 1] + arg.end_col_offset
        seg = raw[a:b].decode("utf-8")
        body = seg.lstrip("rbfuRBFU")
        q = '"' if body.startswith('"') else "'"
        edits.append((a, b, py_lit(mapping[lit], q)))
    edits.sort(key=lambda e: e[0])
    out: list[bytes] = []
    pos = 0
    n = 0
    last_end = -1
    for a, b, rep in edits:
        if a < last_end:
            skipped.append(f"overlap at byte {a}")
            continue
        out.append(raw[pos:a])
        out.append(rep.encode("utf-8"))
        pos = b
        last_end = b
        n += 1
    out.append(raw[pos:])
    return b"".join(out).decode("utf-8"), n, skipped


def apply_edits(src: str, edits: list[tuple[int, int, str]], skipped: list[str]) -> tuple[str, int, list[str]]:
    edits.sort(key=lambda e: e[0])
    out: list[str] = []
    pos = 0
    n = 0
    last_end = -1
    for a, b, rep in edits:
        if a < last_end:
            skipped.append(f"overlap at {a}")
            continue
        out.append(src[pos:a])
        out.append(rep)
        pos = b
        last_end = b
        n += 1
    out.append(src[pos:])
    return "".join(out), n, skipped


def js_edits(src: str, mapping: dict) -> list[tuple[int, int, str]]:
    edits = []
    for m in X.JS_CALL.finditer(src):
        fn = m.group("fn")
        if fn in ("tn", "tinto") and m.group("n") is None:
            continue
        if fn == "t" and m.group("n") is not None:
            continue
        key = X.js_key(m)
        if key not in mapping or mapping[key] == key:
            continue
        a = m.start("q")
        b = m.end("more") if m.group("more") else m.end("s") + 1
        edits.append((a, b, js_lit(mapping[key], m.group("q"))))
    return edits


def rewrite_js(src: str, mapping: dict) -> tuple[str, int, list[str]]:
    return apply_edits(src, js_edits(src, mapping), [])


def rewrite_template(src: str, mapping: dict) -> tuple[str, int, list[str]]:
    edits = []
    for m in X.TPL_CALL.finditer(src):
        key = X.jinja_unescape(m.group(2))
        if key not in mapping or mapping[key] == key:
            continue
        edits.append((m.start(2), m.end(2), tpl_lit(mapping[key])))
    edits += js_edits(src, mapping)
    return apply_edits(src, edits, [])


def rewrite_tests(src: str, mapping: dict) -> tuple[str, int, list[str]]:
    """Exact quoted old keys inside tests → new keys. Substring assertions are
    left for a human; the run shows them."""
    n = 0
    for old in sorted((k for k, v in mapping.items() if k != v), key=len, reverse=True):
        if "\n" in old:
            continue
        for q in ("'", '"', "`"):
            lit = q + old + q
            if lit in src:
                src = src.replace(lit, q + mapping[old].replace(q, "\\" + q) + q)
                n += 1
    return src, n, []


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--overrides", default=str(LOCALES / "_invert_overrides.json"),
                    help="JSON {old key: english key} for the gaps --plan reports")
    ap.add_argument("--python-only", action="store_true",
                    help="redo the app/**/*.py rewrite only, mapping taken from the "
                         "catalogues at git HEAD; catalogues are not rewritten")
    args = ap.parse_args()
    if not (args.plan or args.apply):
        ap.print_help()
        return 2

    if args.python_only:
        import subprocess
        def head(name: str) -> dict:
            return json.loads(subprocess.check_output(
                ["git", "-C", str(ROOT), "show", f"HEAD:app/locales/{name}"]).decode("utf-8"))
        ru, en, zh = head("ru.json"), head("en.json"), head("zh-HK.json")
    else:
        ru = load(LOCALES / "ru.json")
        en = load(LOCALES / "en.json")
        zh = load(LOCALES / "zh-HK.json")
    overrides = load(Path(args.overrides))
    mapping, problems = build_map(ru, en, overrides)
    changed = sum(1 for k, v in mapping.items() if k != v)
    print(f"{len(ru)} keys, {changed} to invert, {len(ru) - changed} already English, "
          f"{len(problems)} problems")
    for p in problems:
        print("  ! " + p)
    if args.plan or problems:
        if problems and args.apply:
            print("refusing to apply with problems — fix them (overrides file) first")
            return 1
        if args.plan:
            return 0

    total = 0
    skipped_all: list[str] = []
    files: list[tuple[Path, str]] = []
    for d in X.PY_DIRS:
        for f in sorted((ROOT / d).rglob("*.py")):
            if "migrations" in f.parts or f.name == "i18n.py":
                continue
            files.append((f, "py"))
    if not args.python_only:
        for f in sorted(X.JS_DIR.rglob("*.js")):
            if X.SKIP_JS_PARTS & set(f.parts) or f.name in X.SKIP_JS_NAMES:
                continue
            files.append((f, "js"))
        for f in sorted(X.TPL_DIR.rglob("*.html")):
            files.append((f, "tpl"))
        for f in sorted((ROOT / "tests").rglob("*.py")) + sorted((ROOT / "tests").rglob("*.js")):
            files.append((f, "tests"))

    for f, kind in files:
        src = f.read_text(encoding="utf-8")
        fn = {"py": rewrite_python, "js": rewrite_js, "tpl": rewrite_template,
              "tests": rewrite_tests}[kind]
        new, n, skipped = fn(src, mapping)
        skipped_all += [f"{f.relative_to(ROOT)}: {s}" for s in skipped]
        if n and new != src:
            f.write_text(new, encoding="utf-8")
            total += n
            print(f"  {f.relative_to(ROOT)}: {n}")
    for s in skipped_all:
        print("  ! skipped " + s)

    if args.python_only:
        print(f"{total} literals rewritten in Python; catalogues untouched")
        return 0
    # catalogues
    new_en = {v: v for v in mapping.values()}
    new_ru = {}
    new_zh = {}
    for old, new in mapping.items():
        if old != new:
            new_ru[new] = X.jinja_unescape(re.sub(r"\s*\{ctx:[^}]*\}", "", old))
        else:
            new_ru[new] = new                 # English chrome stays English in ru
        z = zh.get(old)
        new_zh[new] = z if z and z != old else new
    dump(LOCALES / "en.json", new_en)
    dump(LOCALES / "ru.json", new_ru)
    dump(LOCALES / "zh-HK.json", new_zh)
    print(f"{total} literals rewritten; catalogues written "
          f"(en {len(new_en)}, ru {sum(1 for k, v in new_ru.items() if k != v)} translated, "
          f"zh-HK {sum(1 for k, v in new_zh.items() if k != v)} translated)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
