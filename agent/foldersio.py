"""Writing `folders.ini`: a line-by-line edit of a file written by a HUMAN.

Why a separate module. `agent/folders.py` is frozen by 48 checks and does
exactly one thing: READ the config. Writing is a new ability, and it cannot be
put inside the frozen module: any edit there risks the behaviour that the run
(`runs._resolve_folder_files`), the `GET /folders` handler and the engine page
already rely on. Only writing is here; reading is still the same thing,
`folders.load`.
`folders.load`.

The main decision: we edit LINES, we do not rewrite from the parse
-----------------------------------------------------------
The obvious way is to read with `configparser`, change the structure and
write back with `cp.write()`. It is unusable for two reasons at once:

  1. `cp.write()` throws away ALL of the person's comments. `folders.ini`
     holds lines like "; since May Irkutsk drops its export in another
     folder", the only place that records WHY the paths are what they are.
     Losing them silently would turn the config into a dump of paths in one go.
  2. A repeated key (`dir` three times) collapses in stock `configparser`;
     `folders.load` works around it with `_MultiDict` when reading, but
     `cp.write()` cannot write such a file at all: it would write one key.

So the module keeps the file as a LIST OF LINES and touches exactly the lines
the edit concerns. Comments, blank lines, order, key case, spaces around `=`:
everything the edit does not touch stays as it was, byte for byte. This is
not tidiness for its own sake: the person opens this file in Notepad and must
recognise it after the program has been in it.

Where the line-by-line edit honestly refuses
-----------------------------------------
A refusal is better than quiet damage, so:

  * **Two sections with the same header** (`[folders:X]` twice). When
    reading, `configparser(strict=False)` merges them into one, while a
    line-by-line editor sees two and does not know which to write to. The
    result of the edit would diverge from what is shown on screen:
    `IniError("duplicate_section")`.
  * **A file that does not parse** (`folders.load` returns an empty dict for
    non-empty text). Writing into it means appending lines to a broken file;
    the person must fix it first: `IniError("unparsable")`.
  * **A file larger than `MAX_BYTES`.** A folder config is dozens of lines. A
    megabyte means something else lives under this name, and it must not be
    rewritten.

The module exposes the `comments_preserved` flag as an honest computation
(`will_preserve_comments`), not a promise: the interface must TELL the person
if comments will suffer, not lose them silently.

Atomicity. The write goes to a temporary file IN THE SAME directory and then
`os.replace`; otherwise an interruption (out of space, the process died, the
network drive was pulled) would leave half a config, i.e. a report with half
its sources. The same directory is mandatory: `os.replace` is atomic only
within one file system.

Standard library only: the kit is installed offline (`agent/README.md`).
"""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import folders

# Ceiling on the config size. Not "just in case": the file is read into memory
# whole and rewritten whole, and a folder config is dozens of lines.
MAX_BYTES = 1 * 1024 * 1024

# Ceilings on what the person types in the window. A path longer than `MAX_RAW`
# does not exist on any supported system (Windows 32,767 even with `\\?\`),
# and the mask limit is the same as `folders.MAX_PATTERN_LEN`: a longer mask
# would be truncated on read anyway, and accepting it would promise the
# impossible.
MAX_RAW = 4096
MAX_MASK = folders.MAX_PATTERN_LEN
MAX_SCENARIO = 64

# Keys of section entries, in the SAME order in which `folders.load` lists
# them. The order is not cosmetic here: it is how the entry `index` is
# computed, which the window shows and sends back for deletion. Diverging
# from `folders.load` means deleting the wrong line.
ENTRY_KEYS = ("dir", "file")

# Keys of the section masks.
MASK_KEYS = ("include", "exclude")

# Section header: `[ anything ]`. `configparser` strips the spaces around the
# name itself, so we do too.
_SECTION_RE = re.compile(r"^\[(?P<name>[^\]]*)\]\s*$")

# Option line: a key at the start of the line (no indent), then `=` or `:`.
# The indent is SIGNIFICANT: an indented line is a continuation of the previous
# value, the stock multi-line ini syntax, and `folders.load` reads it on a par
# with a repeated key.
_OPTION_RE = re.compile(r"^(?P<key>[^\s:=\[][^:=]*?)\s*(?P<sep>[:=])(?P<rest>.*)$")

# A comment is only a WHOLE line starting with `;` or `#` (indented or not).
# There are no inline comments in this config, by the decision of
# `folders.load` (`;` and `#` are legal characters in a file name), and we must
# not invent them here: we would cut off a piece of a real path.
_COMMENT_RE = re.compile(r"^\s*[;#]")


class IniError(Exception):
    """A write refusal with a machine-readable reason.

    `code` goes to the interface and is turned into a phrase there: building
    the text here would mean keeping the window's texts in two places.
    """

    def __init__(self, code: str, detail: str = ""):
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail


# --------------------------------------------------------------------------- #
# Splitting text into lines and sections
# --------------------------------------------------------------------------- #

@dataclass
class Value:
    """One option value and the line it physically sits on."""
    text: str
    line: int
    on_key_line: bool       # the value is on the `dir = …` line, not a continuation


@dataclass
class Option:
    """A logical section option: the `key = …` line and its continuations."""
    key: str                # already lower case, as in configparser
    key_line: int
    values: list[Value] = field(default_factory=list)
    last_line: int = 0      # the last physical line of the option


@dataclass
class Section:
    """A `[folders:<id>[:<scenario>]]` section with its line boundaries."""
    name: str               # as written inside the brackets, trimmed
    key: str                # `<id>` or `<id>:<scenario>`, the folders.load key
    dashboard_id: str
    scenario: str
    header: int             # index of the header line
    end: int                # index of the FIRST line that no longer belongs to the section
    options: list[Option] = field(default_factory=list)

    def option(self, key: str) -> Option | None:
        for o in self.options:
            if o.key == key:
                return o
        return None


def split_lines(text: str) -> list[str]:
    """Text -> lines without line breaks. Empty text -> an empty list.

    `splitlines()` on text with a final line break does NOT give an extra empty
    line, which is exactly what is needed: `join` + a final `\\n` returns the
    file to its previous form.
    """
    return text.splitlines()


def _parse_section_name(name: str) -> tuple[str, str] | None:
    """`folders:<id>[:<scenario>]` -> (id, scenario). Otherwise None.

    The parse REPEATS `folders.load` line by line (prefix, `partition` on the
    first colon, `strip` of both halves, an empty id is not a section): two
    different parses of one name would mean the window edits a different
    section from the one the run reads.
    """
    if not name.startswith(folders.SECTION_PREFIX):
        return None
    suffix = name[len(folders.SECTION_PREFIX):].strip()
    if not suffix:
        return None
    dash, _, scen = suffix.partition(":")
    dash, scen = dash.strip(), scen.strip()
    if not dash:
        return None
    return dash, scen


def parse(text: str) -> list[Section]:
    """Config text -> `[folders:…]` sections with their options and boundaries.

    Sections without our prefix do not get into the list, but their LINES stay
    in the file untouched: a foreign section is someone else's business, not
    rubbish.
    """
    lines = split_lines(text)
    out: list[Section] = []
    cur: Section | None = None
    cur_opt: Option | None = None

    def close(at: int) -> None:
        nonlocal cur, cur_opt
        if cur is not None:
            cur.end = at
            out.append(cur)
        cur, cur_opt = None, None

    for i, raw in enumerate(lines):
        m = _SECTION_RE.match(raw)
        if m:
            close(i)
            parsed = _parse_section_name(m.group("name").strip())
            if parsed is not None:
                dash, scen = parsed
                cur = Section(name=m.group("name").strip(),
                              key=f"{dash}:{scen}" if scen else dash,
                              dashboard_id=dash, scenario=scen,
                              header=i, end=len(lines))
            continue
        if cur is None:
            continue
        if _COMMENT_RE.match(raw) or not raw.strip():
            # A comment and an empty line do not close an option:
            # `configparser` skips them too, and a continuation after an empty
            # line is rare but legal. The main thing is that we do not touch
            # their LINES.
            continue
        om = _OPTION_RE.match(raw)
        if om:
            cur_opt = Option(key=om.group("key").strip().lower(), key_line=i,
                             last_line=i)
            val = om.group("rest").strip()
            if val:
                cur_opt.values.append(Value(text=val, line=i, on_key_line=True))
            cur.options.append(cur_opt)
            continue
        if raw[:1].isspace() and cur_opt is not None:
            # Value continuation: one non-empty line = one value
            # (`folders._split_lines` cuts exactly this way).
            cur_opt.values.append(Value(text=raw.strip(), line=i,
                                        on_key_line=False))
            cur_opt.last_line = i
    close(len(lines))
    return out


def raw_sections(text: str) -> list[tuple[str, int, int]]:
    """ALL sections of the file: (name in brackets, header line, line past the end).

    Unlike `parse`, it knows nothing about `[folders:…]`; it is a bare fact of
    ini syntax. It is needed by the program's second ini editor: the bucket
    panel edits `agent.ini` (`agent/s3ui.py`), and the answer to "where does a
    section begin" must be one for both files. Two different answers would
    mean one editor appends a line to a foreign section.
    """
    lines = split_lines(text)
    heads = [(m.group("name").strip(), i)
             for i, raw in enumerate(lines)
             for m in (_SECTION_RE.match(raw),) if m]
    out = []
    for n, (name, at) in enumerate(heads):
        end = heads[n + 1][1] if n + 1 < len(heads) else len(lines)
        out.append((name, at, end))
    return out


def option_key(line: str) -> str | None:
    """The key of a `key = value` line in lower case, otherwise None.

    A line with an INDENT is not a key: it is a continuation of a value (see
    `parse`). Public for the same reason as `raw_sections`.
    """
    if line[:1].isspace() or _COMMENT_RE.match(line):
        return None
    m = _OPTION_RE.match(line)
    return m.group("key").strip().lower() if m else None


def section_of(text: str, key: str) -> Section:
    """The single section with this key. Zero -> `no_section`, two -> a refusal.

    Two sections with one header are read by `folders.load` as ONE (the merge
    in `configparser(strict=False)`) but edited as different ones. This is the
    case where a line-by-line edit cannot be right, and an honest refusal is
    the only answer (see the module header).
    """
    found = [s for s in parse(text) if s.key == key]
    if not found:
        raise IniError("no_section", key)
    if len(found) > 1:
        raise IniError("duplicate_section", key)
    return found[0]


# --------------------------------------------------------------------------- #
# Input checks
# --------------------------------------------------------------------------- #

def check_raw(raw: object) -> str:
    """Is a path that came from the window fit to be written into the ini?

    Exactly what would break the FILE is forbidden, not what we merely dislike:
    a line break (it would become a second entry or a continuation), `=`/`:`/`[`
    at the START (the line would become an empty option or a header), an empty
    string and length over the limit. Everything else, including `%`, `;`, `#`
    and spaces inside, are legal characters of a file name, and rejecting them
    would mean not accepting the person's real path.

    Spaces and tabs AT THE EDGES are trimmed, not rejected: the person copies
    a path from Explorer together with them, a refusal would look like
    nitpicking out of nowhere, and a leading space written as is would turn
    the line into a continuation of the previous option. `folders._split_lines`
    does the same on read, so what is written and what is read coincide.
    """
    if not isinstance(raw, str):
        raise IniError("bad_path", "not a string")
    value = raw.strip("\t ").rstrip("\r")
    if not value:
        raise IniError("bad_path", "empty")
    if "\n" in value or "\r" in value:
        raise IniError("bad_path", "newline")
    if len(value) > MAX_RAW:
        raise IniError("bad_path", f"longer than {MAX_RAW}")
    if value[0] in "=:[":
        raise IniError("bad_path", "starts with = : or [")
    return value


def check_mask(mask: object) -> str:
    """A mask for `include`/`exclude`.

    A comma is forbidden: `folders._split_masks` splits the value on commas,
    and a mask with a comma inside would become TWO masks, i.e. the window
    would show one and the run would read another.
    """
    if not isinstance(mask, str):
        raise IniError("bad_mask", "not a string")
    value = mask.strip()
    if not value:
        raise IniError("bad_mask", "empty")
    if "\n" in value or "\r" in value:
        raise IniError("bad_mask", "newline")
    if "," in value:
        raise IniError("bad_mask", "comma")
    if len(value) > MAX_MASK:
        raise IniError("bad_mask", f"longer than {MAX_MASK}")
    return value


def check_scenario(name: object) -> str:
    """Name of a new scenario.

    `]` would close the header early, `[` would open another, a line break
    would tear it in half: all three give a section OTHER than the one the
    person named. An empty name is the unnamed section, and that is created by
    a separate command, not this one.
    """
    if not isinstance(name, str):
        raise IniError("bad_scenario", "not a string")
    value = name.strip()
    if not value:
        raise IniError("bad_scenario", "empty")
    if len(value) > MAX_SCENARIO:
        raise IniError("bad_scenario", f"longer than {MAX_SCENARIO}")
    if any(c in value for c in "[]\r\n"):
        raise IniError("bad_scenario", "brackets or newline")
    return value


def check_dashboard(value: object) -> str:
    """Report identifier. The same set of prohibitions as for a scenario name,
    plus the colon: `folders.load` uses it to split off the scenario, and an
    `id` with a colon would create a section read as someone else's scenario."""
    out = check_scenario(value)
    if ":" in out:
        raise IniError("bad_dashboard", "colon")
    return out


# --------------------------------------------------------------------------- #
# Edits
# --------------------------------------------------------------------------- #

def entries(section: Section) -> list[tuple[str, Value]]:
    """Section entries in `folders.load` order: all `dir` first, then `file`.

    The order is exactly this and not "as in the file", because it is how
    `Entry.entry_index` is computed, which has already gone into the contract:
    `ResolvedFile.entry_index`, the `dir` field in `input_manifest`, the `dir`
    column in observations. Sorting "more nicely" here would mean deleting the
    wrong entry by index.
    """
    out: list[tuple[str, Value]] = []
    for key in ENTRY_KEYS:
        for opt in section.options:
            if opt.key != key:
                continue
            for val in opt.values:
                out.append((key, val))
    return out


def _owner_option(section: Section, line: int) -> Option:
    for opt in section.options:
        if any(v.line == line for v in opt.values):
            return opt
    raise IniError("internal", f"no option owns line {line}")


def rebuild(lines: list[str], drop: set[int],
             replace: dict[int, str], insert: dict[int, list[str]]) -> str:
    """Assemble the text: drop the lines in `drop`, replace those in `replace`,
    insert `insert` BEFORE the line with that index (the key `len(lines)`
    means the end).

    One assembly point for all edits: otherwise each operation would compute
    the index shift itself, and the first compound edit would shift its
    neighbour.
    """
    out: list[str] = []
    for i, raw in enumerate(lines):
        out.extend(insert.get(i, ()))
        if i in drop:
            continue
        out.append(replace.get(i, raw))
    out.extend(insert.get(len(lines), ()))
    return "\n".join(out) + ("\n" if out else "")


def add_entry(text: str, key: str, kind: str, raw: str) -> str:
    """Append a `dir = …` or `file = …` entry to a section.

    The new line goes RIGHT AFTER the last entry of the same kind, so the
    person finds it where they would look themselves, and a comment written
    before the next block is not torn from its block.
    """
    if kind not in ENTRY_KEYS:
        raise IniError("bad_kind", str(kind))
    value = check_raw(raw)
    section = section_of(text, key)
    lines = split_lines(text)

    same = [o for o in section.options if o.key == kind]
    if same:
        at = same[-1].last_line + 1
    elif section.options:
        at = max(o.last_line for o in section.options) + 1
    else:
        at = section.header + 1
    at = min(at, section.end)
    return rebuild(lines, set(), {}, {at: [f"{kind} = {value}"]})


def remove_entry(text: str, key: str, index: int) -> str:
    """Delete a section entry by its index in `entries()` order.

    Three cases, all of which occur in a live file:
      * the value is on its own continuation line: delete the line;
      * the value is on the `dir = …` line and continuations exist: keep
        `dir =` (otherwise the continuations would be orphaned and become
        keyless rubbish);
      * the value is on the `dir = …` line and is the only one: delete the
        line.
    """
    section = section_of(text, key)
    items = entries(section)
    if not isinstance(index, int) or isinstance(index, bool):
        raise IniError("bad_index", repr(index))
    if index < 0 or index >= len(items):
        raise IniError("bad_index", f"{index} of {len(items)}")
    _, val = items[index]
    opt = _owner_option(section, val.line)
    lines = split_lines(text)
    if not val.on_key_line:
        return rebuild(lines, {val.line}, {}, {})
    if len(opt.values) > 1:
        return rebuild(lines, set(), {opt.key_line: f"{opt.key} ="}, {})
    return rebuild(lines, {opt.key_line}, {}, {})


def set_masks(text: str, key: str, include, exclude) -> str:
    """Rewrite the section masks. An empty list deletes the key entirely.

    The masks are put on ONE line, comma-separated, exactly the form that
    `folders._split_masks` parses and that people write by hand. The old
    continuation lines of the same key go away, comments between them stay:
    a person's comment is not part of the value.
    """
    section = section_of(text, key)
    lines = split_lines(text)
    drop: set[int] = set()
    replace: dict[int, str] = {}
    insert: dict[int, list[str]] = {}

    for mask_key, masks in (("include", include), ("exclude", exclude)):
        if masks is None:
            continue                     # the edit does not touch this key
        if not isinstance(masks, (list, tuple)):
            raise IniError("bad_mask", "not a list")
        values = [check_mask(m) for m in masks]
        opt = section.option(mask_key)
        line = f"{mask_key} = " + ", ".join(values)
        if opt is None:
            if values:
                # The masks apply to the WHOLE section, so we put them on the
                # first line: the person reads "what we take" before the list
                # of folders.
                insert.setdefault(section.header + 1, []).append(line)
            continue
        drop.update(v.line for v in opt.values if not v.on_key_line)
        if values:
            replace[opt.key_line] = line
        else:
            drop.add(opt.key_line)
    return rebuild(lines, drop, replace, insert)


def copy_section(text: str, key: str, scenario: str) -> str:
    """Create `[folders:<id>:<name>]` as a copy of an existing section.

    LINES are copied, i.e. together with the person's comments: the scenario
    "the same but without Irkutsk" starts with the same explanation as the
    original and then lives its own life. The original is not touched at all.
    """
    name = check_scenario(scenario)
    section = section_of(text, key)
    new_key = f"{section.dashboard_id}:{name}"
    if any(s.key == new_key for s in parse(text)):
        raise IniError("scenario_exists", new_key)

    lines = split_lines(text)
    body = lines[section.header + 1:section.end]
    # Trailing empty lines of the source section do not travel into the copy:
    # otherwise every copy of a copy would grow the emptiness.
    while body and not body[-1].strip():
        body.pop()
    header = f"[{folders.SECTION_PREFIX}{new_key}]"
    block = ([""] if lines and lines[-1].strip() else []) + [header] + body
    return rebuild(lines, set(), {}, {len(lines): block})


def create_section(text: str, dashboard_id: str, scenario: str = "") -> str:
    """Create an empty report section. Needed when the ini says NOTHING about the report.

    An empty section is a legal state: `folders.load` keeps it, and `resolve`
    speaks of it out loud (`Missing(reason="not_found")` with the section
    header in `raw`). This is better than creating the section together with
    the first folder: in the window the person first sees the report, then
    fills it.
    """
    dash = check_dashboard(dashboard_id)
    scen = check_scenario(scenario) if scenario else ""
    key = f"{dash}:{scen}" if scen else dash
    if any(s.key == key for s in parse(text)):
        raise IniError("section_exists", key)
    lines = split_lines(text)
    header = f"[{folders.SECTION_PREFIX}{key}]"
    block = ([""] if lines and lines[-1].strip() else []) + [header]
    return rebuild(lines, set(), {}, {len(lines): block})


# --------------------------------------------------------------------------- #
# File
# --------------------------------------------------------------------------- #

def read_text(path) -> str:
    """Read the config. No file -> empty text (it has not been created yet).

    `utf-8-sig`, as in `folders.load`: Windows Notepad puts a BOM, and reading
    it differently in two modules means getting a section that one sees and
    the other does not.
    """
    p = Path(path)
    try:
        if not p.is_file():
            return ""
        size = p.stat().st_size
    except OSError as e:
        raise IniError("read_failed", str(e)) from e
    if size > MAX_BYTES:
        raise IniError("too_large", f"{size} > {MAX_BYTES}")
    try:
        return p.read_text(encoding="utf-8-sig")
    except OSError as e:
        raise IniError("read_failed", str(e)) from e
    except UnicodeDecodeError as e:
        raise IniError("not_utf8", str(e)) from e


def assert_writable(text: str) -> None:
    """Can anything be written into THIS text at all.

    Non-empty text from which `folders.load` did not get a single section is
    either a broken ini or not an ini at all. Appending lines there would
    silently entrench the breakage; the person must open the file and fix it
    themselves.
    """
    if not text.strip():
        return
    # `folders.load` takes a PATH, not text, so suitability is checked by our
    # own line-by-line parse: if at least one `[folders:*]` was found, it is
    # the right file and writing into it is allowed.
    if not parse(text):
        raise IniError("unparsable", "no [folders:*] sections recognised")


def will_preserve_comments(text: str) -> bool:
    """Will the person's comments survive an edit of THIS file.

    The line-by-line edit does not touch them by construction, so the answer is
    always "yes" whenever writing is possible at all. The function exists not
    for today's value but for a promise to the interface: the window shows the
    warning based on THIS answer, not on what we remember about the
    implementation. If a path appears on which comments are lost, it will
    change this answer and the warning will appear by itself.
    """
    try:
        assert_writable(text)
    except IniError:
        return False
    return True


def write_text(path, text: str) -> None:
    """Write the config atomically: a temporary file alongside + `os.replace`.

    Alongside, not in `%TEMP%`: `os.replace` is atomic only within one file
    system, and `folders.ini` lies next to the program, so it may well be on a
    flash drive or a network drive while `%TEMP%` is on the system drive.

    `newline=""` plus explicit `\\n` in the text: without this, on Windows
    Python would turn every `\\n` into `\\r\\n`, and a file read and written
    with no edit at all would differ from itself, so a diff in a foreign
    version control system would show the whole file as edited.
    """
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise IniError("write_failed", str(e)) from e
    fd, tmp = -1, ""
    try:
        fd, tmp = tempfile.mkstemp(prefix=".folders-", suffix=".tmp",
                                   dir=str(p.parent))
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fd = -1
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, str(p))
        tmp = ""
    except OSError as e:
        raise IniError("write_failed", str(e)) from e
    finally:
        if fd >= 0:                                      # pragma: no cover
            os.close(fd)
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:                              # pragma: no cover
                pass


def apply(path, mutate) -> str:
    """Read -> check -> apply the edit -> write atomically.

    One door for all edits: the check "can this file be written at all" must
    stand before EACH of them, and forgetting it in one new operation is a
    matter of time. Returns the new text (the window does not need it, a test
    does).
    """
    text = read_text(path)
    assert_writable(text)
    new = mutate(text)
    if new != text:
        write_text(path, new)
    return new
