"""Interface translation (en / ru / zh-HK) — gettext style, key = English source.

Why the key IS the English string instead of a semantic id (`dq.tile.failed`):

  * a missing translation degrades to the English source instead of leaking an
    identifier into the UI, so an incomplete catalogue is "not translated
    yet", never a regression;
  * `en` needs no catalogue at all: `_catalogs["en"]` is empty and the client
    bundle for en is `{}`. English users pay nothing;
  * extraction is mechanical (`'Run'` → `_('Run')`), and the ~2500 keys need
    no invented names;
  * the DOM tests assert on the English text and keep passing unchanged.

History: until 2026-11-03 the key was the RUSSIAN source and `ru` was the
default; `tools/i18n_invert.py` flipped it so the open-source repository
carries no Russian in its code. Russian is now an ordinary translation
(`app/locales/ru.json`).

Homonyms are separated by a context suffix inside the key itself —
`"Close {ctx:verb}"`. `gettext` strips `{ctx:…}` when the key falls through
untranslated, so the suffix never reaches the screen.

Language for the current request lives in a ContextVar, not in a parameter:
rendering is spread across `request.app.state.jinja.get_template(...).render(...)`
in a dozen modules and `HTTPException(detail=…)` deep inside services where no
`request` reaches. The project already uses a ContextVar the same way
(`perflog.request_record`), and anyio copies the context into the threadpool, so
sync routes see it too.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # annotation only; imported for type checkers
    from markupsafe import Markup

import json
import re
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent.parent
LOCALES_DIR = BASE_DIR / "app" / "locales"

# Order here is the order in the language switcher.
LANGS = ("en", "ru", "zh-HK")
DEFAULT = "en"

# Endonyms — a language is named in itself, so they are never translated.
LANG_LABELS = {"en": "English", "ru": "Русский", "zh-HK": "繁體中文（香港）"}

# Short badge for the topbar button.
LANG_SHORT = {"en": "EN", "ru": "RU", "zh-HK": "繁"}

COOKIE_NAME = "op_lang"
COOKIE_MAX_AGE = 365 * 24 * 3600

_CTX_RE = re.compile(r"\s*\{ctx:[^}]*\}")

_catalogs: dict[str, dict[str, str]] = {lang: {} for lang in LANGS}

_lang: ContextVar[str] = ContextVar("op_lang", default=DEFAULT)


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------

def load(base: Optional[Path] = None) -> None:
    """Read ru/zh-HK catalogues once, at startup. `en` is never read: its
    catalogue maps every key to itself, which is exactly what falling through
    does for free."""
    base = base or LOCALES_DIR
    for lang in LANGS:
        if lang == DEFAULT:
            _catalogs[lang] = {}
            continue
        path = base / f"{lang}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError) as e:  # noqa: BLE001 — never block boot
            print(f"[i18n] {path.name}: {e}")
            data = {}
        # A translation equal to its key is the "not translated yet" marker the
        # merge tool writes; dropping it here keeps the lookup dict smaller.
        _catalogs[lang] = {k: v for k, v in data.items()
                           if isinstance(v, str) and v and v != k}
    counts = ", ".join(f"{lang}={len(_catalogs[lang])}" for lang in LANGS)
    print(f"[i18n] catalogues loaded: {counts}")


def catalog(lang: str) -> dict[str, str]:
    return _catalogs.get(lang, {})


# ---------------------------------------------------------------------------
# Current language
# ---------------------------------------------------------------------------

def current_lang() -> str:
    return _lang.get()


def set_lang(lang: Optional[str]) -> Token:
    return _lang.set(lang if lang in LANGS else DEFAULT)


def reset(token: Token) -> None:
    try:
        _lang.reset(token)
    except ValueError:
        # Token from another context (threadpool hop) — the context dies with
        # the request anyway.
        pass


def _match(tag: str) -> Optional[str]:
    """`zh-hk` → `zh-HK`, `en-GB` → `en`, `ru-RU` → `ru`."""
    tag = tag.strip()
    if not tag:
        return None
    low = tag.lower()
    for lang in LANGS:
        if lang.lower() == low:
            return lang
    primary = low.split("-")[0]
    for lang in LANGS:
        if lang.lower().split("-")[0] == primary:
            return lang
    return None


def negotiate(cookie: Optional[str] = None,
              accept_language: Optional[str] = None,
              user_lang: Optional[str] = None) -> str:
    """users.lang → cookie → Accept-Language → en. No I/O, no SQL."""
    for candidate in (user_lang, cookie):
        hit = _match(candidate or "")
        if hit:
            return hit
    for part in (accept_language or "").split(","):
        tag = part.split(";")[0]
        hit = _match(tag)
        if hit:
            return hit
    return DEFAULT


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------

class _SafeDict(dict):
    """`{unknown}` survives formatting instead of raising — a translation that
    invented a placeholder must not 500 the page."""
    def __missing__(self, key):
        return "{" + key + "}"


def strip_ctx(key: str) -> str:
    return _CTX_RE.sub("", key)


def gettext(key: str, **kw) -> str:
    s = _catalogs.get(_lang.get(), {}).get(key)
    if s is None:
        s = strip_ctx(key)
    if kw:
        try:
            s = s.format_map(_SafeDict(kw))
        except (IndexError, ValueError):
            # Stray brace in the text — show it raw rather than fail.
            pass
    return s


def plural_index(n: int, lang: Optional[str] = None) -> int:
    lang = lang or _lang.get()
    if lang == "ru":
        n = abs(int(n))
        if n % 10 == 1 and n % 100 != 11:
            return 0
        if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
            return 1
        return 2
    if lang == "zh-HK":
        return 0
    return 0 if n == 1 else 1


def ngettext(n: int, key: str, **kw) -> str:
    """`key` holds every English form separated by `|`; a translation holds as
    many forms as ITS language needs (ru 3, zh-HK 1)."""
    lang = _lang.get()
    s = _catalogs.get(lang, {}).get(key)
    if s is None:
        # Untranslated: the key IS the source text, so the rule that applies is
        # the rule of the language that WROTE it — and the form count says
        # which that is. English sources carry two forms; a legacy Russian key
        # with three forms (none should remain) follows the Russian rule.
        s = strip_ctx(key)
        lang = "ru" if len(s.split("|")) == 3 else DEFAULT
    forms = s.split("|")
    idx = min(plural_index(n, lang), len(forms) - 1)
    out = forms[idx]
    kw.setdefault("n", n)
    try:
        out = out.format_map(_SafeDict(kw))
    except (IndexError, ValueError):
        pass
    return out


def jinja_ngettext(singular: str, plural: str, n: int) -> str:
    """Jinja's `{% trans %}` calls ngettext(singular, plural, n); our key holds
    every form in one string. This adapter is what create_app installs."""
    return ngettext(n, singular + "|" + plural)


def gettext_html(key: str, **kw) -> "Markup":
    """`_h` in templates: a sentence whose middle carries inline markup.

    Splitting `<b>` out of a sentence would leave the translator three
    fragments to reassemble in an order Chinese does not use, so the tags ride
    the key as ordinary `{name}` placeholders and the result is marked safe.
    Only literal markup from the template is ever passed in — never data."""
    from markupsafe import Markup
    return Markup(gettext(key, **kw))


def N_(s: str) -> str:
    """Extraction marker: "this literal is UI, translate it later".

    A module-level constant (`CARD_META`, `QUESTS`, `LEVELS`) is built once, at
    import time, when no request — and therefore no language — exists yet.
    Calling `_()` there would freeze whatever language happened to be current
    during import into every later response. So the constant keeps the English
    source, `N_()` makes the extractor see it, and the actual `_()` happens in
    the function that serialises the constant into a response.

    It is deliberately the identity function: `N_(x) is x`, so a constant marked
    this way is byte-for-byte what it was before."""
    return s


def translate_marked(text: str, **kw) -> str:
    """The second half of `N_()`: translate a string that came OUT of a marked
    constant. Identical to `gettext`, and a separate name only so the linter
    can tell "a variable, on purpose" from "a variable, by accident" — `_(x)`
    with a non-literal is a bug the extractor cannot see, `_m(x)` is the
    documented way to translate a key some `N_()` already registered."""
    return gettext(text, **kw)


# gettext-style short aliases, used everywhere in app/ and installed into Jinja.
_ = gettext
_n = ngettext
_h = gettext_html
_m = translate_marked
