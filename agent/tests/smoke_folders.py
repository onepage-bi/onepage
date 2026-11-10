"""Smoke test of agent/folders.py: folders.ini parsing, masks, missing folders,
identity stability, UNC. Standard library only.

Run:  python -m agent.tests.smoke_folders   (from the OnePage repo root)

The main test of the file is not "files were found" but "a folder does not
stay silent": no branch of `resolve` returns an empty `files` without
`missing`, and each of the four reasons is reachable. A silent successful run
over an empty place is the worst possible outcome (RFC_DESKTOP_MIRROR,
section "Window and folders"), and it must be caught by a test, not by the
user.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

from agent import folders

_PASS = 0
_FAIL = 0


def check(label, cond, extra=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"PASS {label}")
    else:
        _FAIL += 1
        print(f"FAIL {label}  {extra}")


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _reasons(res) -> set[str]:
    return {m.reason for m in res.missing}


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="op-folders-test-"))
    try:
        # ---------------------------------------------------------------- #
        # 1. Config parsing: several dir/file in one section.
        # ---------------------------------------------------------------- #
        d1 = tmp / "reports2026"
        d2 = tmp / "share"
        d1.mkdir()
        d2.mkdir()
        _write(d1 / "sl.xlsx", "x")
        _write(d1 / "~$sl.xlsx", "x")          # Excel junk
        _write(d1 / "notes.txt", "x")
        _write(d2 / "extra.csv", "x")
        named = _write(tmp / "named.xlsx", "x")

        ini = _write(tmp / "folders.ini", f"""
[folders:17]
dir = {d1}
dir = {d2}
file = {named}
include = *.xlsx, *.csv
exclude = ~$*, *.tmp

[folders:  42  ]
dir = {d1}
""")
        srcs = folders.load(ini)
        check("one section per report: both read", set(srcs) == {"17", "42"}, sorted(srcs))
        s17 = srcs["17"]
        check("several dirs in one section do not overwrite each other",
              len([e for e in s17.entries if e.kind == "dir"]) == 2,
              [e.raw for e in s17.entries])
        check("file next to dir is parsed",
              [e.raw for e in s17.entries if e.kind == "file"] == [str(named)])
        check("include parsed as comma-separated", s17.entries[0].include == ("*.xlsx", "*.csv"),
              s17.entries[0].include)
        check("exclude parsed as comma-separated", s17.entries[0].exclude == ("~$*", "*.tmp"),
              s17.entries[0].exclude)

        # ---------------------------------------------------------------- #
        # 2. Masks: one semantics with app/services/globmatch.py.
        # ---------------------------------------------------------------- #
        res = folders.resolve(s17)
        names = sorted(f.name for f in res.files)
        check("include admits only its own extensions",
              names == ["extra.csv", "named.xlsx", "sl.xlsx"], names)
        check("exclude drops ~$ junk", "~$sl.xlsx" not in names)
        check("entry_index points to its own entry",
              {f.name: f.entry_index for f in res.files}
              == {"sl.xlsx": 0, "extra.csv": 1, "named.xlsx": 2},
              [(f.name, f.entry_index) for f in res.files])
        check("size and mtime_ms are filled",
              all(f.size > 0 and f.mtime_ms > 0 for f in res.files))
        check("path in ResolvedFile is absolute", all(os.path.isabs(f.path) for f in res.files))
        check("mask is case-insensitive (as globToRe)",
              folders.matches("*.XLSX", "sl.xlsx") and folders.matches("*.xlsx", "SL.XLSX"))
        check("no character classes: [ab].xlsx is literal",
              folders.matches("[ab].xlsx", "[ab].xlsx")
              and not folders.matches("[ab].xlsx", "a.xlsx"))
        check("? is exactly one character", folders.matches("sl?.xlsx", "sl1.xlsx")
              and not folders.matches("sl?.xlsx", "sl12.xlsx"))

        # A named file is read even if the section masks would drop it: the
        # filter here is the name itself, a dropped file would be a silent loss.
        only_file = folders.Source("x", (folders.Entry(raw=str(tmp / "named.xlsx"),
                                                       kind="file",
                                                       include=("*.csv",)),))
        check("a named file is not filtered out by section masks",
              len(folders.resolve(only_file).files) == 1)

        # ---------------------------------------------------------------- #
        # 3. A missing folder of each kind. The MAIN test of the file.
        # ---------------------------------------------------------------- #
        gone = folders.Source("g", (folders.Entry(raw=str(tmp / "no-such-dir"), kind="dir"),))
        r = folders.resolve(gone)
        check("not_found: no such folder", _reasons(r) == {"not_found"} and not r.files, r.missing)

        empty_dir = tmp / "empty"
        empty_dir.mkdir()
        r = folders.resolve(folders.Source("e", (folders.Entry(raw=str(empty_dir), kind="dir"),)))
        check("no_match: folder exists, no suitable files",
              _reasons(r) == {"no_match"} and not r.files, r.missing)

        r = folders.resolve(folders.Source("n", (folders.Entry(raw=str(named), kind="dir"),)))
        check("not_a_dir: a file supplied instead of a folder",
              _reasons(r) == {"not_a_dir"} and not r.files, r.missing)

        r = folders.resolve(folders.Source("fd", (folders.Entry(raw=str(d1), kind="file"),)))
        check("not_a_dir: a file entry points to a directory",
              _reasons(r) == {"not_a_dir"} and not r.files, r.missing)

        r = folders.resolve(folders.Source("gf", (folders.Entry(raw=str(tmp / "nope.xlsx"),
                                                                kind="file"),)))
        check("not_found: the named file does not exist", _reasons(r) == {"not_found"}, r.missing)

        denied = tmp / "denied"
        denied.mkdir()
        _write(denied / "a.xlsx", "x")
        os.chmod(denied, 0o000)
        try:
            r = folders.resolve(folders.Source("d", (folders.Entry(raw=str(denied), kind="dir"),)))
            if os.geteuid() == 0:               # root cannot be denied
                check("denied: no rights to the folder (skipped under root)", True)
            else:
                check("denied: no rights to the folder",
                      _reasons(r) == {"denied"} and not r.files, r.missing)
        finally:
            os.chmod(denied, 0o700)

        # The invariant the module was written for.
        for label, src in (("no folder", gone),
                           ("zero matches", folders.Source(
                               "e", (folders.Entry(raw=str(empty_dir), kind="dir"),))),
                           ("empty section", folders.Source("z", ()))):
            rr = folders.resolve(src)
            check(f"empty files NEVER occurs without missing: {label}",
                  bool(rr.missing) or bool(rr.files), rr)

        r = folders.resolve(folders.Source("z", ()))
        check("an empty section is named by the section header",
              r.missing and r.missing[0].raw == "[folders:z]", r.missing)

        # A folder present + a folder missing in one section: there are files AND a complaint.
        mixed = folders.Source("m", (folders.Entry(raw=str(d1), kind="dir"),
                                     folders.Entry(raw=str(tmp / "nope"), kind="dir")))
        r = folders.resolve(mixed)
        check("the loss of one entry does not hide the files of another",
              r.files and _reasons(r) == {"not_found"}, r)

        # resolve does not raise even on junk instead of a Source.
        try:
            r = folders.resolve(folders.Source("junk", (folders.Entry(raw="\x00/bad", kind="dir"),)))
            check("resolve does not raise on a bad path", bool(r.missing), r)
        except Exception as exc:                 # noqa: BLE001
            check("resolve does not raise on a bad path", False, repr(exc))

        # ---------------------------------------------------------------- #
        # 4. identity: the key by which the platform tells same-named folders apart.
        # ---------------------------------------------------------------- #
        base = tmp / "idtest"
        base.mkdir()
        i0 = folders.identity(str(base))
        check("identity: 16 hex", len(i0) == 16 and all(c in "0123456789abcdef" for c in i0), i0)
        check("identity is stable against a trailing slash",
              folders.identity(str(base) + os.sep) == i0)
        check("identity is stable against './' inside",
              folders.identity(str(base.parent) + os.sep + "." + os.sep + base.name) == i0)
        check("identity is stable against '..' inside",
              folders.identity(str(base / "sub" / "..")) == i0)
        check("two different folders: different keys",
              folders.identity(str(tmp / "a")) != folders.identity(str(tmp / "b")))
        if os.name == "nt":
            check("identity on Windows does not distinguish case",
                  folders.identity(str(base).upper()) == folders.identity(str(base).lower()))
        else:
            check("identity on POSIX DISTINGUISHES case (it is significant there)",
                  folders.identity("/data/Reports") != folders.identity("/data/reports"))

        # ---------------------------------------------------------------- #
        # 5. UNC is an ordinary path, but the leading \\ are not eaten.
        # ---------------------------------------------------------------- #
        unc = r"\\server\share\Reports\2026"
        check("os.path.normpath keeps the leading \\\\",
              os.path.normpath(r"\\server\share") .startswith("\\\\")
              if os.name == "nt" else True)
        check("UNC normalisation keeps the leading \\\\",
              folders._norm(unc).startswith("\\\\") and not folders._norm(unc).startswith("\\\\\\"),
              folders._norm(unc))
        check("UNC identity does not depend on the current directory",
              folders.identity(unc) == folders.identity(unc + "\\"))
        check("UNC identity is stable against the separator",
              folders.identity(unc) == folders.identity("//server/share/Reports/2026"
                                                        if os.name == "nt" else unc))
        check("UNC and a local path with the same tail: different keys",
              folders.identity(unc) != folders.identity(r"\\server\share\Reports\2025"))
        unc_src = folders.load(_write(tmp / "unc.ini", f"""
[folders:7]
dir = {unc}
"""))
        check("UNC in the ini reaches Entry as is",
              unc_src["7"].entries[0].raw == unc, unc_src["7"].entries)
        r = folders.resolve(unc_src["7"])
        check("UNC without a special branch: an unreachable share = an ordinary loss",
              not r.files and _reasons(r) <= {"not_found", "denied", "not_a_dir"}, r.missing)

        # ---------------------------------------------------------------- #
        # 6. Empty section and broken config: do not crash.
        # ---------------------------------------------------------------- #
        e = folders.load(_write(tmp / "empty.ini", "[folders:5]\ninclude = *.xlsx\n"))
        check("an empty section is read and gets into the dict",
              "5" in e and e["5"].entries == (), e)

        # A broken config is checked with a catch: a `load` that RAISES is also a
        # check failure, not a test crash (an agent that crashed at start
        # cannot complain to anyone).
        try:
            broken = folders.load(_write(tmp / "broken.ini",
                                         "[folders:1\ndir = C:\\x\nnot an option\n"))
            check("broken config: empty dict, no exception", broken == {}, broken)
        except Exception as exc:                 # noqa: BLE001
            check("broken config: empty dict, no exception", False, repr(exc))
        check("no file: empty dict", folders.load(tmp / "absent.ini") == {})
        check("directory instead of a file: empty dict", folders.load(tmp) == {})
        pct = folders.load(_write(tmp / "pct.ini", "[folders:9]\ndir = C:\\Exports\\80%\n"))
        check("'%' in a path does not break parsing (interpolation is off)",
              pct and pct["9"].entries[0].raw.endswith("80%"), pct)

        semi = folders.load(_write(tmp / "semi.ini", "[folders:9]\ndir = C:\\a;b\n"))
        check("';' in a path is not treated as a comment",
              semi["9"].entries[0].raw == "C:\\a;b", semi)

        cont = folders.load(_write(tmp / "cont.ini",
                                   f"[folders:3]\ndir = {d1}\n\t{d2}\n"))
        check("an indented continuation line = a second folder",
              len(cont["3"].entries) == 2, cont["3"].entries)

        nosec = folders.load(_write(tmp / "other.ini", "[security]\nallowed_origins = x\n"))
        check("foreign sections do not become sources", nosec == {}, nosec)
        # The kit's example must parse: it lands on a person's machine, and a
        # typo in it looks like "the app does not see the folders".
        example = Path(__file__).resolve().parents[2] / "deploy/agent_tools/folders.ini.example"
        ex = folders.load(example)
        check("the folders.ini.example parses",
              # The named scenario appeared in the example together with the
              # `GET /folders` handler (contract K1): a person will not learn
              # about `scenario` from the code, only from this file.
              set(ex) == {"17", "42", "42:without-north"}
              and [e.kind for e in ex["17"].entries] == ["dir", "dir", "file"]
              and ex["17"].entries[0].exclude == ("~$*", "*.tmp")
              and ex["42:without-north"].scenario == "without-north"
              and ex["42:without-north"].entries[0].exclude == ("*North*",),
              {k: v.entries for k, v in ex.items()})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'ALL PASS' if _FAIL == 0 else 'FAILURES: ' + str(_FAIL)}  ({_PASS} passed)")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
