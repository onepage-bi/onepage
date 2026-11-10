#!/usr/bin/env python3
"""Teaching workbook for the Anonymizer lesson: a "bad file" of the common kind.

    tools/learn_fixture_anon.py [--check]

Why a separate file. The "Delta" export is deliberately sterile: only numbers,
store codes and item names, so there is nothing to show the tool on, because
there is nothing to show in substance either. The lesson has to show exactly
what happens in real life: surnames and tax ids in columns, a hidden sheet with
a lookup list, a named range, a link to someone else's workbook on a network
drive, a colleague's comment and the author's name in the properties.

All values are INVENTED. This is a matter of principle: the file lives in the
repository, ends up in screenshots and is downloaded by people, so no real data
can be here.

Deterministic: `--check` rebuilds the workbook and compares sha256, so that the
lesson's screenshots and text do not drift away from the file.
"""
import os as _os

# The XML serializer is pinned EXPLICITLY, and this is not a style nitpick.
# openpyxl writes the workbook through lxml if it is installed and through the
# standard ElementTree if not, and the two differ in markup details:
# `<numFmt .../>` vs `<numFmt ... />`, attribute order, sheet size (15.6 MB vs
# 11.7 on the same export). So the fixture's reproducibility depended on
# whether lxml was present in the environment.
#
# It depended silently: lxml came in as a dependency of python3-saml. When SAML
# moved to a separate requirements-saml.txt, the base install stopped pulling
# it in, and CI began "failing to reproduce" a file built on a machine where
# lxml remained. Reproducibility that rests on an accidental transitive
# dependency is not reproducibility.
#
# Set BEFORE importing openpyxl: the flag is evaluated once, at import time.
_os.environ.setdefault("OPENPYXL_LXML", "False")

import argparse
import hashlib
import os
import sys
from datetime import date, datetime, timedelta

MODIFIED = datetime(2026, 3, 17, 18, 30, 0)

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "static", "learn", "files", "anon-demo.xlsx")

# Invented companies and people. Not one name is taken from life: the set is
# assembled from neutral words so that nobody recognises their own client in it.
# Tax ids keep the 10-digit (company) / 12-digit (sole trader) shape the
# anonymizer detects; phones use the fictional 555 range.
CLIENTS = [
    ("Breezeway Ltd", "Irene Somers", "7701234501", "+1 555 123-45-01"),
    ("Noonday Ltd", "Arthur Gaines", "7701234502", "+1 555 123-45-02"),
    ("Ceramo Ltd", "Olivia Larkin", "7701234503", "+1 555 123-45-03"),
    ("P. Sutton (sole trader)", "Peter Sutton", "770123450412", "+1 555 123-45-04"),
    ("Quietwater Ltd", "Karen Abbott", "7701234505", "+1 555 123-45-05"),
    ("Farfield Ltd", "Nadia Yorke", "7701234506", "+1 555 123-45-06"),
    ("Blackcurrant Ltd", "Michael Katz", "7701234507", "+1 555 123-45-07"),
    ("A. Vale (sole trader)", "Alia Vale", "770123450813", "+1 555 123-45-08"),
]
POINTS = ["ST-01", "ST-02", "ST-03", "ST-04", "ST-05", "ST-06", "ST-07", "ST-08"]


def build(path: str) -> None:
    import openpyxl
    from openpyxl.comments import Comment
    from openpyxl.workbook.defined_name import DefinedName

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Shipments"
    ws.append(["date", "store", "client", "contact person", "tax id", "phone",
               "item", "shipped", "amount"])
    start = date(2026, 3, 3)
    for day in range(14):                       # two weeks, an even series
        for i, (client, person, inn, phone) in enumerate(CLIENTS):
            ws.append([
                start + timedelta(days=day), POINTS[i], client, person, inn, phone,
                "drink 0.5", 100 + i * 7 + day, (100 + i * 7 + day) * 120,
            ])
    # A formula referencing someone else's workbook is the least visible leak
    # channel: the path alone tells about the project, the drive and sometimes
    # a surname.
    ws["K2"] = "='[C:\\exchange\\Pricelist_2026_final.xlsx]Prices'!B7"
    ws["L2"] = "=SUM(I2:I100)"
    ws["C2"].comment = Comment("check with Somers: discount until the end of the quarter", "Parker")
    ws.row_dimensions[5].hidden = True
    ws.column_dimensions["E"].hidden = True

    ref = wb.create_sheet("Discount list")
    ref.append(["client", "discount, %"])
    for client, *_ in CLIENTS:
        ref.append([client, 5])
    ref.sheet_state = "hidden"

    tech = wb.create_sheet("service")
    tech.append(["parameter", "value"])
    tech.append(["exported from", "old version of the report"])
    tech.sheet_state = "veryHidden"

    wb.defined_names.add(DefinedName("ClientDiscounts",
                                     attr_text="'Discount list'!$A$1:$B$9"))
    wb.properties.creator = "Helen V. Parker"
    wb.properties.lastModifiedBy = "S. A. Gurney"
    wb.properties.company = "Breezeway Ltd"
    # Without this the workbook is not deterministic: openpyxl writes the
    # MOMENT of the build into the properties, and sha256 changes from run to
    # run. Confirmed by the very first `--check`. The date is fixed and
    # obviously a "teaching" one.
    wb.properties.created = datetime(2026, 3, 3, 9, 0, 0)
    wb.properties.modified = MODIFIED   # openpyxl overwrites this; see _pin_modified
    wb.save(path)
    _pin_modified(path)


def _pin_modified(path: str) -> None:
    """Pin the modification date inside an already saved workbook.

    openpyxl OVERWRITES `wb.properties.modified` with the current time on save,
    whatever was put there. Because of that the workbook was not reproducible
    for a single day: `--check` rebuilt it and honestly reported "DIFFERS", and
    the only working state was the hour the file was committed. A check that is
    bound to fail tomorrow checks nothing.

    We patch the zip archive rather than going through the API: the API is what
    overwrites us. openpyxl writes the other parts of the workbook stably
    (compared byte for byte: exactly one line in docProps/core.xml differed).
    """
    import re
    import shutil
    import zipfile

    with zipfile.ZipFile(path) as zf:
        entries = [(zi, zf.read(zi.filename)) for zi in zf.infolist()]

    pinned = MODIFIED.strftime("%Y-%m-%dT%H:%M:%SZ")
    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:
        for zi, data in entries:
            if zi.filename == "docProps/core.xml":
                data = re.sub(rb"(<dcterms:modified[^>]*>)[^<]*(</dcterms:modified>)",
                              rb"\g<1>" + pinned.encode() + rb"\g<2>", data)
            # The entry date inside the archive must be constant too, otherwise
            # the bytes would differ for another reason.
            new = zipfile.ZipInfo(zi.filename, date_time=(2026, 3, 3, 9, 0, 0))
            new.compress_type = zi.compress_type
            new.external_attr = zi.external_attr
            out.writestr(new, data)
    shutil.move(tmp, path)


def content_sha256(path) -> str:
    """Fingerprint of the CONTENT, not of the packaging.

    For zip containers (xlsx is a zip) the sha256 of the file itself cannot be
    compared: the bytes depend on the zlib version, which differs between the
    developer's machine and the CI runner. The same content is packed
    differently, and the reproducibility check starts complaining about the
    platform instead of the data; exactly this happened on 03.11.2026, when CI
    went red on two fixtures in a row.

    We hash entry names and their UNCOMPRESSED bytes in sorted order: that is
    exactly the property we promise, "regeneration gives the same file".
    """
    import zipfile
    path = str(path)
    h = hashlib.sha256()
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            for name in sorted(z.namelist()):
                h.update(name.encode("utf-8"))
                h.update(b"\0")
                h.update(z.read(name))
                h.update(b"\0")
        return h.hexdigest()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="rebuild into a temporary file and compare sha256")
    args = ap.parse_args()
    try:
        import openpyxl                                     # noqa: F401
    except Exception:
        print("openpyxl is required", file=sys.stderr)
        return 2

    if args.check:
        if not os.path.exists(OUT):
            print("file is missing:", OUT, file=sys.stderr)
            return 1
        tmp = OUT + ".check"
        build(tmp)
        same = content_sha256(tmp) == content_sha256(OUT)
        os.unlink(tmp)
        print(("ok    " if same else "DIFFERS ") + os.path.basename(OUT) + " " + content_sha256(OUT))
        return 0 if same else 1

    build(OUT)
    print("built", OUT)
    print("  size: %.1f KB" % (os.path.getsize(OUT) / 1024))
    print("  inside: a hidden sheet, a \"very hidden\" sheet, a named range,")
    print("          a link to another workbook, a formula, a comment, a hidden row and column,")
    print("          names, tax ids and phones, all invented")
    print("  sha256:", content_sha256(OUT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
