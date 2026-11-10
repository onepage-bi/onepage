"""Where the mirror's resources live: `strings.json` and the `web/` pages.

One function for the whole package, because the resources live in TWO
different places and the code should not have to know about it:

  * **Running from the package** (development, tests) -- next to this file:
    `agent/mirror/strings.json`, `agent/mirror/web/library.html`.
  * **Single-file build** (`deploy/build_agent_single.py` glues the modules
    into `op_agent.py`) -- the gluing carries only CODE. The kit builder puts
    the resources next to `op_agent.py`: `op_agent.py` + `mirror/strings.json`
    + `mirror/web/*`. Then `__file__` points at `op_agent.py` itself, and
    `Path(__file__).with_name("strings.json")` finds nothing -- this is how
    the mirror in the kit would silently end up without texts and pages.

That is why the path is searched through a list of candidates rather than
computed. A missing resource gives `None`, not an exception: missing texts
must not bring down the tray icon (`schedule.strings()` on an empty dict
shows the keys, and that is visible immediately).

**A resource name is not a path.** `res_path` accepts only simple names from
a whitelist alphabet: the resources are listed in code, but the page name
comes from the ADDRESS (`/library/common.js`), and one missed `..` here means
reading any file on the person's machine over loopback. We validate rather
than repair.

Standard library only (agent/ is Python 3.12, stdlib).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# Name of the directory with resources next to the single-file `op_agent.py`.
# The kit builder uses the same name -- change it in both places at once.
RES_DIRNAME = "mirror"

WEB_DIRNAME = "web"
STRINGS_NAME = "strings.json"

# Resource name: letters, digits, dot, hyphen, underscore. No slashes, no
# colon (NTFS stream), no empty name, no name starting with a dot.
_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$")


def _roots() -> tuple[Path, ...]:
    """Directories where resources may live, in descending order of confidence."""
    here = Path(__file__).resolve().parent
    out: list[Path] = [here, here / RES_DIRNAME]
    try:
        out.append(Path(sys.argv[0]).resolve().parent / RES_DIRNAME)
    except (OSError, IndexError, ValueError):   # pragma: no cover
        pass
    return tuple(out)


def res_path(name: str, *, web: bool = False) -> Path | None:
    """Path to a resource, or None if it is under no root.

    `web=True` -- a page from `web/`. The name is validated, not normalized:
    an unusable name is None, i.e. a 404 for the caller.
    """
    if not isinstance(name, str) or not _NAME_RE.match(name):
        return None
    for root in _roots():
        p = (root / WEB_DIRNAME / name) if web else (root / name)
        try:
            if p.is_file():
                return p
        except OSError:                         # pragma: no cover
            continue
    return None


def strings_path() -> Path | None:
    """Path to `strings.json`, or None."""
    return res_path(STRINGS_NAME)
