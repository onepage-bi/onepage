"""Folders and files of the app, by PATH rather than by browser handle.

The contract was declared in advance in `docs/RFC_DESKTOP_MIRROR.md`,
section "Window and folders: an app instead of an appendage (12.10.2026)",
and is executed here literally: `load` / `Source` / `Entry` / `resolve` /
`Resolved` / `ResolvedFile` / `Missing` / `identity`. The window
(`agent/appwindow.py`) and the server half (`path`/`root_path`/`root_id` in
observations) are written in parallel and rely on these names and on these
four reasons for a missing path, so none of them is renamed and there is no
fifth reason here, even where one would be welcome (see "A deliberate stretch"
below).

Why the module exists at all. In the browser a folder is available only as a
File System Access API handle: there is fundamentally no path, the folder is
recognised by its name plus a fingerprint of the set of names, every
permission grant needs a human gesture, and the C.6 limit (500 files / 500 MB
/ 35 MB) exists precisely because this is a tab. The app reads a path as TEXT,
and all of that goes away at once, including the main practical gain: the
person sees WHICH EXACT file was read, not "a folder with that name".

Standard library only: the kit is installed offline, no new wheels will appear
(`agent/README.md`).

Two things that make this file written this way and not the obvious way
-----------------------------------------------------------------------

**1. Masks have ONE semantics with the browser.** `_to_regex` is a line-by-line
port of `app/services/globmatch.py` (itself a port of `globToRe` from
`static/js/gen-engine.js`). This is not whim: the same question "what was
read" is asked in four places (the engine in the browser, the server when
parsing the manifest, the lineage page, and now this app), and two different
answers mean a lineage that contradicts what was actually computed. `fnmatch`
fails on every count: it has live character classes, `*` crosses `/`, and case
sensitivity depends on the OS.

The only divergence from `globmatch.py`: there is a `validate_pattern` that
REFUSES an obviously meaningless pattern (with `/`, with `..`, made only of
wildcards). Here there is nobody to refuse and nothing to refuse with:
`Missing` has no "broken mask" reason, and inventing one would break the
contract. So a bad mask stays as written and, by construction, is NOT SILENT:
a pattern with `/` cannot match any flat name (`*` expands to `[^/]*`), the
folder yields zero files and the person gets `Missing(..., "no_match")`. The
refusal turns into an honest "missing", not a quiet empty run.

**2. Several `dir`/`file` keys in one section.** On its own `configparser`
OVERWRITES a second key of the same name (and with `strict=True` raises
`DuplicateOptionError`), so a naive parse would read the last of three folders
and the rest would vanish silently, exactly the outcome this module must
rule out. Parsing goes through `_MultiDict`: while reading the file
`configparser` holds an option value as a LIST of lines and, on meeting the
key again, reassigns it. The substituted `dict_type` does not reassign at that
moment but APPENDS the lines to the list already accumulated; then the stock
`_join_multiline_values` joins the list with `\\n`, and `cp.get()` returns a
multi-line value that we split by lines. A side benefit: indented
continuation lines look the same, so both ways of writing the ini work
identically and nobody has to explain the difference to the person.

A deliberate stretch
--------------------
There are exactly four reasons for a missing path, and "this is not a file" is
not among them. So a `file = ...` entry pointing at a DIRECTORY also yields
`not_a_dir`: here the reason reads as "the type of the path is not the
declared one". A fifth reason would break the contract for the parallel
implementers, and staying silent about such an entry is not allowed.
"""
from __future__ import annotations

import configparser
import errno
import hashlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Config name. It lives NEXT TO THE PROGRAM, not in the profile: the folder is
# portable and moves as a whole, together with which reports read which folders.
INI_NAME = "folders.ini"

# Section prefix: [folders:<dashboard_id>].
SECTION_PREFIX = "folders:"

# Mask length limit, the same as `globmatch.MAX_PATTERN_LEN`. A longer one is
# not rejected (there is nothing to reject with, see the header) but truncated
# to the limit, so that a string of arbitrary length from the file does not
# end up in the compiled regexp.
MAX_PATTERN_LEN = 128

# Exactly what the JS version escapes: /[.+^${}()|[\]\\]/. There are NO
# character classes in this semantics: `[ab].xlsx` matches the literal name
# "[ab].xlsx".
_ESCAPE = set(".+^${}()|[]\\")

# Separator of the mask list in the ini. A comma, as in the manifest and in the
# override UI.
_MASK_SEP = ","


def _to_regex(pattern: str) -> re.Pattern:
    """Line-by-line port of `globmatch.to_regex` (and, through it, `globToRe`).

    Any change here must go into `app/services/globmatch.py`,
    `static/js/gen-engine.js` and `static/js/data-sources-edit.js` at once.
    """
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                out.append(".*")        # `**` crosses the separator
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
    """Whether a file NAME (not a path) matches the mask."""
    return _to_regex(pattern[:MAX_PATTERN_LEN]).match(name) is not None


@dataclass(frozen=True)
class Entry:
    """One section entry: a folder OR a specific file.

    `include`/`exclude` apply only to LISTING a folder. With a `file = ...`
    entry the person has already named exactly one file, which is its own
    filter, and a section mask that dropped the named file would give a silent
    empty run instead of a read. Of the two non-obvious behaviours the one
    that does not lose data is chosen.
    """
    raw: str
    kind: str                       # "dir" | "file"
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class Source:
    """All entries of one report. `dashboard_id` is the section text as written.

    `scenario` is a named VARIANT of the input file set of the same report:
    section `[folders:<id>:<name>]` versus the unnamed `[folders:<id>]`. It is
    needed for scenario planning ("the same data but without Irkutsk",
    "plan B"): one report, several sets, results compared through the
    platform's ordinary versions. There is no selection UI yet, only the key
    and the field are laid down (owner decision of 19.10), so `scenario` here
    and `scenario` in `input_manifest` must appear TOGETHER, otherwise a run
    cannot say which set it was built from and there is nothing to compare.

    A colon as separator is safe: the report identifier is a UUID, which has
    no colons by construction.
    """
    dashboard_id: str
    entries: tuple[Entry, ...] = ()
    scenario: str = ""              # "" is the default set

    @property
    def key(self) -> str:
        """Section key as written in the ini: `<id>` or `<id>:<scenario>`."""
        return f"{self.dashboard_id}:{self.scenario}" if self.scenario else self.dashboard_id


@dataclass(frozen=True)
class ResolvedFile:
    path: str                       # absolute normalised path
    name: str                       # file name; masks work on it too
    size: int
    mtime_ms: int                   # ms, as in the engine contract (`add_file`)
    entry_index: int                # which section entry produced it


@dataclass(frozen=True)
class Missing:
    raw: str
    reason: str                     # "not_found" | "denied" | "not_a_dir" | "no_match"


@dataclass(frozen=True)
class Resolved:
    files: tuple[ResolvedFile, ...] = ()
    missing: tuple[Missing, ...] = ()


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

class _MultiDict(dict):
    """`dict_type` for configparser that does not lose a repeated key.

    It fires in exactly one place: while `_read` accumulates option values they
    are LISTS of lines, and a repeated key arrives as a reassignment. We
    concatenate the lists, and `_join_multiline_values` then joins them with
    `\\n` by itself. Non-list assignments (sections, DEFAULTS) pass as usual,
    otherwise we would break the parser's internals instead of fixing the
    parsing.
    """

    def __setitem__(self, key, value):
        if isinstance(value, list) and isinstance(self.get(key), list):
            super().__setitem__(key, self[key] + value)
        else:
            super().__setitem__(key, value)


def default_ini_path() -> Path:
    """`folders.ini` next to the program.

    The order is the same as in `agent/mirror/assets.py`: first an explicit
    `OP_FOLDERS_INI` (used by tests and manual runs), then the directory of
    the launched file (in the kit that is the folder with `op_agent.py`), then
    the current directory, the last one only for development.
    """
    env = os.environ.get("OP_FOLDERS_INI")
    if env:
        return Path(env)
    try:
        return Path(sys.argv[0]).resolve().parent / INI_NAME
    except (OSError, IndexError, ValueError):   # pragma: no cover
        return Path(INI_NAME)


def _split_lines(value: str) -> list[str]:
    """Lines of a multi-line value, without empty ones and surrounding spaces."""
    return [ln.strip() for ln in value.splitlines() if ln.strip()]


def _split_masks(value: str) -> tuple[str, ...]:
    """Masks from one or more `include`/`exclude` keys, comma-separated."""
    out: list[str] = []
    for line in _split_lines(value):
        for part in line.split(_MASK_SEP):
            part = part.strip()
            if part:
                out.append(part[:MAX_PATTERN_LEN])
    return tuple(out)


def load(ini_path) -> dict[str, Source]:
    """Read `folders.ini` -> {dashboard_id: Source}.

    Lenient about a missing or broken file: no file, or it does not parse,
    gives an empty dict rather than an exception. The reason is not
    indulgence but start-up order: the folder config is edited by a person in
    Notepad, and a typo in it must not stop the program from opening and
    saying so; an agent that crashed at start cannot complain to anyone.
    """
    path = Path(ini_path)
    try:
        if not path.is_file():
            return {}
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return {}

    # `interpolation=None` is mandatory: paths contain `%` (`%USERNAME%`,
    # export names like `80%.xlsx`), and the stock interpolation fails on it.
    # `strict=False`: repeated keys are NORMAL here, `_MultiDict` collects
    # them. There are no inline comments by default, which is convenient:
    # `;` and `#` are legal characters in a file name.
    cp = configparser.ConfigParser(dict_type=_MultiDict, strict=False,
                                   interpolation=None)
    try:
        cp.read_string(text, source=str(path))
    except configparser.Error:
        return {}

    out: dict[str, Source] = {}
    for section in cp.sections():
        if not section.startswith(SECTION_PREFIX):
            continue
        suffix = section[len(SECTION_PREFIX):].strip()
        if not suffix:
            continue
        # `<id>` or `<id>:<scenario>`. Split on the FIRST colon: the scenario
        # name is written by a person, and forbidding a colon inside the name
        # would mean rejecting a section they have already written, and there
        # is nothing to reject with here (`Missing` has no such reason, see
        # the module header).
        dash, _, scen = suffix.partition(":")
        dash, scen = dash.strip(), scen.strip()
        if not dash:
            continue

        include = _split_masks(cp.get(section, "include", fallback=""))
        exclude = _split_masks(cp.get(section, "exclude", fallback=""))

        entries: list[Entry] = []
        for kind, key in (("dir", "dir"), ("file", "file")):
            for raw in _split_lines(cp.get(section, key, fallback="")):
                entries.append(Entry(raw=raw, kind=kind,
                                     include=include, exclude=exclude))

        # An empty section DOES get into the dict: `resolve` must say so out
        # loud, and a section dropped here would become a report without a
        # source that nobody would say anything about.
        src = Source(dashboard_id=dash, entries=tuple(entries), scenario=scen)
        out[src.key] = src
    return out


def scenarios_for(sources: dict, dashboard_id: str) -> list[str]:
    """Scenario names of this report, the default set first.

    An empty string in the list means the unnamed section `[folders:<id>]`.
    The list is empty if the ini says nothing about the report, and these are
    DIFFERENT things: the list `[""]` means "a set is declared, and it is the
    only one", an empty list means "no section at all", and the latter must
    lead to a refusal with a clear phrase, not to an empty run.
    """
    names = [s.scenario for s in sources.values() if s.dashboard_id == dashboard_id]
    return sorted(names, key=lambda n: (n != "", n.casefold()))


# --------------------------------------------------------------------------- #
# Paths and the stable folder key
# --------------------------------------------------------------------------- #

def _is_unc(raw: str) -> bool:
    """UNC is a path starting with two BACKSLASHES.

    `//` is deliberately excluded: on POSIX it is a legal absolute path, and
    taking it for a network share would break an ordinary path. A leading
    `\\\\`, however, cannot be a POSIX absolute path by construction.
    """
    return raw[:2] == "\\\\" and len(raw) > 2


def _norm(raw: str) -> str:
    """Normalised absolute path, the same for every spelling of one folder.

    UNC is handled SEPARATELY, not for the sake of a special branch in the
    logic (the RFC forbids that) but for one fact: `os.path.abspath` on POSIX
    considers `\\\\server\\share` RELATIVE and glues the current directory
    onto it, so the `identity` of one and the same share would depend on where
    the program is launched from. `os.path.normpath` keeps the leading
    `\\\\` (on Windows because it knows about UNC, on POSIX because it does
    not treat `\\` as a separator), but it never gets there if the path has
    already been spoiled by abspath.
    """
    raw = str(raw)
    if _is_unc(raw):
        body = raw[2:].replace("/", "\\")
        parts = [p for p in body.split("\\") if p and p != "."]
        # We collapse `..` inside UNC ourselves: normpath on POSIX will not,
        # and two spellings of one share must give one key.
        stack: list[str] = []
        for p in parts:
            if p == ".." and stack:
                stack.pop()
            elif p != "..":
                stack.append(p)
        return "\\\\" + "\\".join(stack)
    return os.path.normpath(os.path.abspath(raw))


def identity(path) -> str:
    """Folder key: the first 16 hex of sha256 of the normalised absolute path.

    The platform uses this key to tell TWO FOLDERS WITH THE SAME NAME apart,
    which the browser path cannot do at all (it identifies by name plus a
    fingerprint of the set of file names). So the key must be stable across
    spellings: the separator (`/` versus `\\`), a trailing slash, `.`/`..`
    inside, and on Windows the case, because NTFS and SMB do not distinguish
    case. On POSIX case is SIGNIFICANT, and casefold there would merge two
    different folders into one key.
    """
    norm = _norm(path)
    if os.name == "nt":
        # On Windows we bring the separator to `\` before case folding:
        # normpath has already done that, but the path may not have come
        # through it.
        norm = norm.replace("/", "\\")
        key = norm.casefold()
    else:
        key = norm
    return hashlib.sha256(key.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _reason_for(exc: OSError) -> str:
    """Reason for a missing path by errno. An unknown error is "not found", not "denied".

    This is more honest for a network folder: `EIO`/`ESTALE`/"host not
    responding" mean to a person "the folder is not there right now", while
    the word "denied" would send them to sort out permissions they never lost.
    """
    code = getattr(exc, "errno", None)
    if code in (errno.EACCES, errno.EPERM):
        return "denied"
    if code == errno.ENOTDIR:
        return "not_a_dir"
    return "not_found"


def _admits(name: str, include: tuple[str, ...], exclude: tuple[str, ...]) -> bool:
    """Whether the mask set admits this name. Empty `include` = everything is admitted."""
    if include and not any(matches(p, name) for p in include):
        return False
    return not any(matches(p, name) for p in exclude)


def _resolve_file(entry: Entry, index: int,
                  files: list[ResolvedFile], missing: list[Missing]) -> None:
    path = _norm(entry.raw)
    try:
        st = os.stat(path)
    except OSError as exc:
        missing.append(Missing(raw=entry.raw, reason=_reason_for(exc)))
        return
    if not os.path.isdir(path):
        files.append(ResolvedFile(path=path, name=os.path.basename(path),
                                  size=int(st.st_size),
                                  mtime_ms=int(st.st_mtime * 1000),
                                  entry_index=index))
        return
    # The named file turned out to be a directory: see "A deliberate stretch" in
    # the module header.
    missing.append(Missing(raw=entry.raw, reason="not_a_dir"))


def _resolve_dir(entry: Entry, index: int,
                 files: list[ResolvedFile], missing: list[Missing]) -> None:
    path = _norm(entry.raw)
    found: list[ResolvedFile] = []
    try:
        # The walk is FLAT, one level, exactly like the engine in the browser.
        # Descending would make masks with `/` meaningful here and
        # meaningless there, i.e. it would bring back two answers to one
        # question.
        with os.scandir(path) as it:
            for de in it:
                try:
                    if de.is_dir():
                        continue
                    if not _admits(de.name, entry.include, entry.exclude):
                        continue
                    st = de.stat()
                except OSError:
                    # One inaccessible entry does not cancel the folder: there
                    # is nothing to report it with (`Missing` carries the
                    # folder path, not a file's), but the listing must not be
                    # dropped because of it either.
                    continue
                found.append(ResolvedFile(path=_norm(de.path), name=de.name,
                                          size=int(st.st_size),
                                          mtime_ms=int(st.st_mtime * 1000),
                                          entry_index=index))
    except NotADirectoryError:
        missing.append(Missing(raw=entry.raw, reason="not_a_dir"))
        return
    except OSError as exc:
        reason = _reason_for(exc)
        # The path exists but is a file: on some systems scandir gives ENOTDIR
        # already on open, on others ENOENT on a component. We check
        # ourselves, otherwise "a file was supplied instead of a folder" would
        # look like "no such folder" and the person would go looking for a
        # problem that does not exist.
        try:
            if reason == "not_found" and os.path.exists(path) and not os.path.isdir(path):
                reason = "not_a_dir"
        except OSError:                          # pragma: no cover
            pass
        missing.append(Missing(raw=entry.raw, reason=reason))
        return

    if not found:
        # The folder exists and is readable, but nothing fell under the masks.
        # This is NOT a success: a silent empty run is the worst possible
        # outcome (RFC section 2).
        missing.append(Missing(raw=entry.raw, reason="no_match"))
        return
    files.extend(sorted(found, key=lambda f: f.name.casefold()))


def resolve(source: Source) -> Resolved:
    """Resolve a report's entries into files on disk.

    Two invariants, both pinned by a test:
      * NEVER raises: a missing folder is an answer, not a failure; raising
        here would mean the window does not show the person the reason;
      * NEVER returns an empty `files` without a non-empty `missing`,
        otherwise a run starts on an empty place and the report quietly
        recomputes over nothing.
    """
    files: list[ResolvedFile] = []
    missing: list[Missing] = []

    for index, entry in enumerate(getattr(source, "entries", ()) or ()):
        try:
            if entry.kind == "file":
                _resolve_file(entry, index, files, missing)
            else:
                _resolve_dir(entry, index, files, missing)
        except Exception as exc:                 # noqa: BLE001 — last line of defence
            # What we did not foresee ends up here (a broken locale of a name,
            # network FS exotica). The second invariant matters more than the
            # precision of the reason.
            missing.append(Missing(raw=getattr(entry, "raw", ""),
                                   reason=_reason_for(exc) if isinstance(exc, OSError)
                                   else "not_found"))

    if not files and not missing:
        # A section without a single `dir`/`file` entry. What is missing is
        # not a path but the section's content, hence its header goes into
        # `raw`: the person must be shown exactly the place in the file where
        # there is nothing to read.
        missing.append(Missing(raw=f"[{SECTION_PREFIX}{source.dashboard_id}]",
                               reason="not_found"))

    return Resolved(files=tuple(files), missing=tuple(missing))
