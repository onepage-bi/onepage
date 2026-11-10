#!/usr/bin/env python3
"""Chunks for translators, and the client bundles.

    python3 tools/i18n_merge.py --split          # en.json -> app/locales/chunks/en-NN.json
    python3 tools/i18n_merge.py --merge ru       # ru-NN.json -> app/locales/ru.json
    python3 tools/i18n_merge.py --init ru        # ru.json = en.json (untranslated seed)
    python3 tools/i18n_merge.py --client         # *.json -> static/locales/*.js

`--split` cuts ~150 keys per chunk along FILE boundaries taken from
`_refs.json`, so a translator sees one screen at a time and can hold its
terminology. A chunk carries the source file list and a per-key reference, plus
a `note` the extractor's operator can fill in for anything ambiguous.

`--merge` refuses a chunk that renames a key, invents a placeholder or returns
the wrong number of plural forms; a value still equal to its key is reported as
"not translated yet", not as an error — that is the deliberate degradation
path (an untranslated string shows the English source).

`--client` writes `static/locales/<lang>.js`, a single assignment to
`window.OP_I18N_CATALOG`. It is JS rather than JSON so the page needs no fetch,
and it is a separate file rather than inline markup so a browser caches it once
(`?v=` + immutable) instead of re-downloading ~20 KB with every page. `en` gets
an empty object: the English UI never consults a catalogue.

Run it from the repository root.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCALES = ROOT / "app" / "locales"
CHUNKS = LOCALES / "chunks"
CLIENT = ROOT / "static" / "locales"

sys.path.insert(0, str(ROOT))
from app import i18n  # noqa: E402

CHUNK_SIZE = 150
FORMS = {"ru": 3, "en": 2, "zh-HK": 1}
PARAM = re.compile(r"\{(\w+)\}")


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def dump(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")


# ---------------------------------------------------------------------------
# --split
# ---------------------------------------------------------------------------

def split() -> None:
    ru = load(LOCALES / (i18n.DEFAULT + ".json"))
    refs = load(LOCALES / "_refs.json")
    if not ru:
        sys.exit("app/locales/en.json is empty — run tools/i18n_extract.py first")

    # Group by source file (the first reference wins), so one screen's strings
    # travel together; files are ordered by their own path for stable chunks.
    by_file: dict[str, list[str]] = {}
    for key in ru:
        entry = refs.get(key) or {}
        first = (entry.get("refs") or ["misc:0"])[0]
        by_file.setdefault(first.rsplit(":", 1)[0], []).append(key)

    chunks: list[tuple[list[str], list[str]]] = []
    cur_keys: list[str] = []
    cur_files: list[str] = []
    for path in sorted(by_file):
        keys = sorted(by_file[path])
        # A file bigger than a chunk gets chunks of its own.
        while keys:
            room = CHUNK_SIZE - len(cur_keys)
            if room <= 0:
                chunks.append((cur_files, cur_keys))
                cur_keys, cur_files = [], []
                continue
            take, keys = keys[:room], keys[room:]
            cur_keys += take
            if path not in cur_files:
                cur_files.append(path)
        if len(cur_keys) >= CHUNK_SIZE:
            chunks.append((cur_files, cur_keys))
            cur_keys, cur_files = [], []
    if cur_keys:
        chunks.append((cur_files, cur_keys))

    for old in CHUNKS.glob(f"{i18n.DEFAULT}-*.json"):
        old.unlink()
    for n, (files, keys) in enumerate(chunks, 1):
        strings = {}
        for k in keys:
            ref = refs.get(k) or {}
            entry = {"ref": (ref.get("refs") or ["?"])[0]}
            if ref.get("plural"):
                entry["plural"] = True
            strings[k] = entry
        dump(CHUNKS / f"{i18n.DEFAULT}-{n:02d}.json", {
            "_meta": {"chunk": f"{n:02d}", "lang": i18n.DEFAULT,
                      "source_files": files, "count": len(keys)},
            "strings": strings,
        })
    print(f"{len(chunks)} chunks in {CHUNKS.relative_to(ROOT)} "
          f"({len(ru)} keys, ~{CHUNK_SIZE} per chunk)")


# ---------------------------------------------------------------------------
# --merge
# ---------------------------------------------------------------------------

def merge(lang: str) -> int:
    ru = load(LOCALES / (i18n.DEFAULT + ".json"))
    refs = load(LOCALES / "_refs.json")
    plural_keys = {k for k, v in refs.items() if isinstance(v, dict) and v.get("plural")}
    out: dict[str, str] = {}
    problems: list[str] = []
    untranslated = 0
    files = sorted(CHUNKS.glob(f"{lang}-*.json"))
    if not files:
        sys.exit(f"no {lang}-NN.json in {CHUNKS.relative_to(ROOT)}")
    for f in files:
        data = load(f)
        for key, value in (data.get("strings") or {}).items():
            if isinstance(value, dict):        # an untouched source chunk
                value = key
            if key not in ru:
                problems.append(f"{f.name}: unknown key {key[:60]!r}")
                continue
            extra = set(PARAM.findall(value)) - set(PARAM.findall(key))
            if extra:
                problems.append(f"{f.name}: {key[:40]!r} invents {sorted(extra)}")
                continue
            if value == key:
                # Not translated yet — the deliberate degradation path, never
                # an error, even for a plural key whose English form count
                # (2) never matches ru/zh-HK's (3/1).
                untranslated += 1
                out[key] = value
                continue
            if key in plural_keys and len(value.split("|")) != FORMS[lang]:
                problems.append(f"{f.name}: {key[:40]!r} has "
                                f"{len(value.split('|'))} forms, {lang} needs {FORMS[lang]}")
                continue
            out[key] = value
    # A chunk that never shipped (a translator hasn't reached it, or its
    # chunk was rejected above) must not shrink the catalogue's key set: the
    # key set of app/locales/{lang}.json always equals en.json's, filled with
    # the English source where no translation exists yet. i18n.load() drops
    # value==key entries at read time, so this is a no-op at runtime and only
    # keeps "same key set as ru.json" true for the merge's own contract.
    missing = 0
    for key, value in ru.items():
        if key not in out:
            out[key] = value
            missing += 1
    for p in problems:
        print("error: " + p)
    dump(LOCALES / f"{lang}.json", out)
    print(f"{len(out)} keys -> app/locales/{lang}.json "
          f"({untranslated + missing} still untranslated: "
          f"{untranslated} in shipped chunks, {missing} in chunks not yet delivered)")
    return 1 if problems else 0


# ---------------------------------------------------------------------------
# --init / --client
# ---------------------------------------------------------------------------

def init(lang: str) -> None:
    """Seed a language file with the English source as its own value — the
    shape a translator receives, and a no-op at runtime (i18n.load() drops
    values equal to their key)."""
    ru = load(LOCALES / (i18n.DEFAULT + ".json"))
    dump(LOCALES / f"{lang}.json", dict(ru))
    print(f"{len(ru)} keys -> app/locales/{lang}.json (seeded from {i18n.DEFAULT})")


def client_bundle(data: dict) -> str:
    body = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return ("/* Generated by tools/i18n_merge.py --client. Do not edit. */\n"
            "window.OP_I18N_CATALOG=" + body + ";\n")


def client() -> None:
    ru = load(LOCALES / (i18n.DEFAULT + ".json"))
    CLIENT.mkdir(parents=True, exist_ok=True)
    for lang in i18n.LANGS:
        if lang == i18n.DEFAULT:
            data: dict[str, str] = {}
        else:
            data = {k: v for k, v in load(LOCALES / f"{lang}.json").items()
                    if k in ru and v and v != k}
        path = CLIENT / f"{lang}.js"
        path.write_text(client_bundle(data), encoding="utf-8")
        print(f"static/locales/{lang}.js  {len(data)} keys, {path.stat().st_size} bytes")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", action="store_true")
    ap.add_argument("--merge", metavar="LANG")
    ap.add_argument("--init", metavar="LANG")
    ap.add_argument("--client", action="store_true")
    args = ap.parse_args()
    rc = 0
    if args.split:
        split()
    if args.init:
        init(args.init)
    if args.merge:
        rc |= merge(args.merge)
    if args.client:
        client()
    if not any((args.split, args.merge, args.init, args.client)):
        ap.print_help()
    return rc


if __name__ == "__main__":
    sys.exit(main())
