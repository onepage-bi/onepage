"""The Dev Guide's copyable artifacts — prompts, a manifest, the engine contract.

They are documents, not interface strings: multi-line, meant to be copied into
a model's chat or an editor verbatim, and useless the moment their line breaks
are collapsed. That rules out the string catalogue (`i18n_extract` normalises
whitespace, and a translator chunk is a poor place for a 25-line prompt), so
each one lives as its own text file next to the page's template:

    templates/docs_artifacts/<name>.<lang>.txt

A language with no file of its own falls back to Russian — the same honest
degradation the catalogue gives an untranslated key. Files are read on request
(the page is opened rarely) and cached until their mtime moves, the discipline
`runtime_config` uses for its overlay.
"""
from __future__ import annotations

import os
from pathlib import Path

DIR = Path(__file__).resolve().parents[2] / "templates" / "docs_artifacts"
NAMES = ("base", "bundle", "contract", "manifest", "proto", "diet", "theme")

_cache: dict[tuple[str, str], tuple[float, str]] = {}


def _read(name: str, lang: str) -> str:
    path = DIR / f"{name}.{lang}.txt"
    if not path.exists():
        path = DIR / f"{name}.en.txt"
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return ""
    hit = _cache.get((name, lang))
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        text = path.read_text(encoding="utf-8").rstrip("\n")
    except OSError:
        return ""
    _cache[(name, lang)] = (mtime, text)
    return text


def load(lang: str) -> dict[str, str]:
    """Every artifact for one language, keyed by name for the template."""
    return {name: _read(name, lang) for name in NAMES}
