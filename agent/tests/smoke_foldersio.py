"""Smoke test of agent/foldersio.py: line-by-line writing of folders.ini.

Run:  python -m agent.tests.smoke_foldersio   (from the OnePage repo root)

The main test of the file is not "a line was added" but the TWO invariants
for which the module is written exactly this way:
  * the person's comments and everything the edit does not touch survive the
    write byte for byte: the file is opened in Notepad, and after the program
    it must remain recognisable;
  * after any edit `folders.load` reads exactly what we meant to write,
    otherwise the window shows one thing and the run reads another, which is
    the worst possible outcome (RFC_DESKTOP_MIRROR, section "Window and
    folders").
Standard library only.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from agent import folders, foldersio

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


def raises(label, code, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except foldersio.IniError as e:
        check(label, e.code == code, f"code {e.code!r}, expected {code!r}")
    except Exception as e:                      # noqa: BLE001
        check(label, False, f"not an IniError: {e!r}")
    else:
        check(label, False, "did not refuse at all")


SAMPLE = """\
; Report folders. Lines with ; are my notes, the program does not touch them.

[folders:17]
; since May Irkutsk drops its export in another folder
dir = C:\\Reports\\Ventes_été
dir = D:\\Shared\\exports
exclude = ~$*, *.tmp
file = E:\\manual\\référence.xlsx
# a foreign section is not our business
[security]
allowed_origins = https://example.invalid

[folders:42:without-north]
dir = C:\\Reports\\Ventes_été
exclude = *North*
"""


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="op-foldersio-test-"))
    try:
        # ---------------------------------------------------------------- #
        # 1. Parsing: sections, boundaries, options, order of entries.
        # ---------------------------------------------------------------- #
        secs = foldersio.parse(SAMPLE)
        check("both our sections are visible and no foreign one",
              [s.key for s in secs] == ["17", "42:without-north"],
              [s.key for s in secs])
        s17 = secs[0]
        check("a section ends BEFORE a foreign header",
              SAMPLE.splitlines()[s17.end].startswith("[security]"),
              SAMPLE.splitlines()[s17.end])
        check("the scenario is parsed as in folders.load",
              (secs[1].dashboard_id, secs[1].scenario) == ("42", "without-north"),
              secs[1])
        order = [(k, v.text) for k, v in foldersio.entries(s17)]
        check("the order of entries is the same as in folders.load (dir first, then file)",
              [k for k, _ in order] == ["dir", "dir", "file"], order)
        loaded = folders.load(_w(tmp / "a.ini", SAMPLE))
        check("entry indexes coincide with folders.load one to one",
              [e.raw for e in loaded["17"].entries] == [v for _, v in order],
              (order, [e.raw for e in loaded["17"].entries]))

        # ---------------------------------------------------------------- #
        # 2. Comments and foreign sections survive an edit.
        # ---------------------------------------------------------------- #
        added = foldersio.add_entry(SAMPLE, "17", "dir", "F:\\new")
        check("the person's comment is in place after the addition",
              "; since May Irkutsk drops its export in another folder" in added)
        check("the foreign section is untouched", "[security]" in added
              and "allowed_origins = https://example.invalid" in added)
        check("the new folder went right after the last dir",
              added.splitlines()[6] == "dir = F:\\new", added.splitlines()[:9])
        check("the file entry stayed below, the order of kinds is preserved",
              added.index("dir = F:\\new") < added.index("file = E:"))
        check("what is read coincides with what was intended",
              [e.raw for e in folders.load(_w(tmp / "b.ini", added))["17"].entries]
              == ["C:\\Reports\\Ventes_été", "D:\\Shared\\exports", "F:\\new",
                  "E:\\manual\\référence.xlsx"])

        # ---------------------------------------------------------------- #
        # 3. Deletion by index, of three kinds.
        # ---------------------------------------------------------------- #
        gone = foldersio.remove_entry(SAMPLE, "17", 1)
        check("exactly the second folder was deleted",
              [e.raw for e in folders.load(_w(tmp / "c.ini", gone))["17"].entries]
              == ["C:\\Reports\\Ventes_été", "E:\\manual\\référence.xlsx"])
        check("the deletion did not touch the comment",
              "; since May Irkutsk" in gone)
        gone_file = foldersio.remove_entry(SAMPLE, "17", 2)
        check("the file entry was deleted (the index is counted after all dirs)",
              "file =" not in gone_file and "dir = D:" in gone_file)

        multi = ("[folders:9]\ndir = A:\\one\n\tA:\\two\n\tA:\\three\n")
        m0 = foldersio.remove_entry(multi, "9", 0)
        check("the value from the `dir = …` line is removed, continuations did not become orphans",
              [e.raw for e in folders.load(_w(tmp / "d.ini", m0))["9"].entries]
              == ["A:\\two", "A:\\three"], m0)
        m1 = foldersio.remove_entry(multi, "9", 1)
        check("a continuation was deleted, the others are in place",
              [e.raw for e in folders.load(_w(tmp / "e.ini", m1))["9"].entries]
              == ["A:\\one", "A:\\three"], m1)
        only = foldersio.remove_entry("[folders:9]\ndir = A:\\one\n", "9", 0)
        check("the last entry removes the key line itself too",
              "dir" not in only and "[folders:9]" in only, only)
        raises("index out of range: refusal", "bad_index",
               foldersio.remove_entry, SAMPLE, "17", 9)
        raises("index is not a number: refusal", "bad_index",
               foldersio.remove_entry, SAMPLE, "17", "1")

        # ---------------------------------------------------------------- #
        # 4. Masks.
        # ---------------------------------------------------------------- #
        masks = foldersio.set_masks(SAMPLE, "17", ["*.xlsx", "*.xlsb"], ["~$*"])
        got = folders.load(_w(tmp / "f.ini", masks))["17"].entries[0]
        check("masks are rewritten and read by both halves",
              got.include == ("*.xlsx", "*.xlsb") and got.exclude == ("~$*",),
              (got.include, got.exclude))
        check("include went to the first line of the section, before the list of folders",
              masks.index("include =") < masks.index("dir = C:"))
        dropped = foldersio.set_masks(SAMPLE, "17", None, [])
        check("an empty list deletes the key entirely",
              "~$*" not in dropped
              and "exclude" not in dropped[:dropped.index("[security]")], dropped)
        check("the exclude of a FOREIGN section is untouched",
              "exclude = *North*" in dropped)
        check("include was not touched, so it did not appear",
              "include" not in dropped)
        raises("a mask with a comma: refusal (it would become two)", "bad_mask",
               foldersio.set_masks, SAMPLE, "17", ["a,b"], None)

        # ---------------------------------------------------------------- #
        # 5. A scenario as a copy of a section.
        # ---------------------------------------------------------------- #
        copied = foldersio.copy_section(SAMPLE, "17", "plan-B")
        cl = folders.load(_w(tmp / "g.ini", copied))
        check("the copy of the section is read as a scenario of the same report",
              "17:plan-B" in cl
              and [e.raw for e in cl["17:plan-B"].entries]
              == [e.raw for e in cl["17"].entries], sorted(cl))
        check("the person's comment travelled into the copy together with the entries",
              copied.count("; since May Irkutsk") == 2)
        check("the source section did not change",
              cl["17"].entries == loaded["17"].entries)
        raises("a scenario with this name already exists: refusal", "scenario_exists",
               foldersio.copy_section, SAMPLE, "42:without-north", "without-north")
        raises("a bracket in the scenario name: refusal", "bad_scenario",
               foldersio.copy_section, SAMPLE, "17", "ba]d")

        # ---------------------------------------------------------------- #
        # 6. A new section.
        # ---------------------------------------------------------------- #
        made = foldersio.create_section(SAMPLE, "77")
        check("the empty section appeared and is read (resolve will speak of it aloud)",
              "77" in folders.load(_w(tmp / "h.ini", made)), made[-60:])
        res = folders.resolve(folders.load(_w(tmp / "h.ini", made))["77"])
        check("the empty section does not stay silent: files is empty, missing is not",
              not res.files and len(res.missing) == 1, res)
        raises("the report already exists: refusal", "section_exists",
               foldersio.create_section, SAMPLE, "17")
        raises("a colon in the identifier: refusal", "bad_dashboard",
               foldersio.create_section, SAMPLE, "7:7")

        # ---------------------------------------------------------------- #
        # 7. The refusals the module was written for.
        # ---------------------------------------------------------------- #
        dup = SAMPLE + "\n[folders:17]\ndir = Z:\\more\n"
        raises("two sections with one header: a refusal, not a blind edit",
               "duplicate_section", foldersio.add_entry, dup, "17", "dir", "Q:\\x")
        raises("no such section: refusal", "no_section",
               foldersio.add_entry, SAMPLE, "999", "dir", "Q:\\x")
        raises("a file without a single folders section: writing is impossible", "unparsable",
               foldersio.assert_writable, "just some text\n")
        check("writing to an empty file is allowed (the first section creates it too)",
              foldersio.will_preserve_comments("") is True)
        check("we do not write into a broken file and give no promise about comments",
              foldersio.will_preserve_comments("garbage") is False)
        trimmed = foldersio.add_entry(SAMPLE, "17", "dir", "  Q:\\x\t")
        check("spaces at the edges of a path are trimmed and do not make a continuation line",
              "dir = Q:\\x\n" in trimmed
              and [e.raw for e in folders.load(_w(tmp / "i.ini", trimmed))["17"].entries]
              [-2] == "Q:\\x", trimmed)
        for bad, why in (("", "empty"), ("=start", "empty option"),
                         ("a\nb", "line break"), ("x" * 5000, "length")):
            raises(f"path rejected ({why})", "bad_path",
                   foldersio.add_entry, SAMPLE, "17", "dir", bad)
        raises("unknown kind of entry: refusal", "bad_kind",
               foldersio.add_entry, SAMPLE, "17", "folder", "Q:\\x")

        # ---------------------------------------------------------------- #
        # 8. The file: atomic write, BOM, line break, size limit.
        # ---------------------------------------------------------------- #
        target = tmp / "live" / "folders.ini"
        foldersio.write_text(target, SAMPLE)
        check("the write creates the directory and the file", target.is_file())
        check("what was read is exactly what was written",
              foldersio.read_text(target) == SAMPLE)
        check("no temporary files remain after the write",
              [p.name for p in target.parent.iterdir()] == ["folders.ini"],
              [p.name for p in target.parent.iterdir()])
        check("there is no \\r in the file: the line break is not rewritten for Windows",
              b"\r" not in target.read_bytes())
        bom = tmp / "bom.ini"
        bom.write_bytes("\ufeff[folders:5]\ndir = Q:\\x\n".encode("utf-8"))
        check("the Notepad BOM is read the same way folders.load reads it",
              foldersio.read_text(bom).startswith("[folders:5]"))
        big = tmp / "big.ini"
        big.write_text("x" * (foldersio.MAX_BYTES + 1), encoding="utf-8")
        raises("a file that is too large is not rewritten", "too_large",
               foldersio.read_text, big)
        nonutf = tmp / "cp.ini"
        nonutf.write_bytes("[folders:5]\ndir = é\n".encode("cp1252"))
        raises("a file not in UTF-8: refusal, not corrupted letters", "not_utf8",
               foldersio.read_text, nonutf)

        # `apply` is the only door: reads, checks, writes atomically.
        out = foldersio.apply(target, lambda t: foldersio.add_entry(
            t, "17", "file", "G:\\more\\book.xlsx"))
        check("apply wrote the file and returned exactly what is in the file",
              out == foldersio.read_text(target)
              and "G:\\more\\book.xlsx" in out,
              out[-80:])
        before = target.read_bytes()
        foldersio.apply(target, lambda t: t)
        check("an edit with no change does not touch the file", target.read_bytes() == before)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'ALL PASS' if _FAIL == 0 else 'FAILURES: ' + str(_FAIL)}  ({_PASS} passed)")
    return 1 if _FAIL else 0


def _w(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


if __name__ == "__main__":
    sys.exit(main())
