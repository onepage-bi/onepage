"""Folder run under the R2 contract: folders.ini -> resolve -> r2host -> outputs.

This pins the JOINT, not the halves: `smoke_folders` checks ini parsing,
`smoke_r2host` - the host protocol, and both were green at the moment when the
folder branch in `run_generator` did not exist at all and a bundle without GreenPlum got an
empty inbox. This file checks what is between them.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from agent import runs  # noqa: E402

FAILS: list[str] = []


def check(label: str, ok: bool, got: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + label + ("" if ok else f"  -> {got}"))
    if not ok:
        FAILS.append(label)


GEN = '''
import os, json
def begin(out_dir, cache_dir, schema_version, progress):
    return S(out_dir)
class S:
    def __init__(self, out): self.out, self.rows = out, []
    def add_file(self, name, mtime_ms, size, path):
        self.rows.append((name, size))
        return "parsed"
    def finish(self):
        open(os.path.join(self.out, "dashboard.html"), "w", encoding="utf-8").write(
            json.dumps({"files": [r[0] for r in self.rows],
                        "bytes": sum(r[1] for r in self.rows)}, ensure_ascii=False))
        return {"html": "dashboard.html", "slots": {}, "write_back": {}}
'''

MANIFEST = {"name": "fakefolder", "version": "1.0",
            "entrypoint": {"module": "fakefolder.main"},
            "outputs": {"html": True, "slots": []},
            "inputs": [{"match": "SL_*.xlsx", "role": "data", "required": True}],
            "engine": "browser"}


def make_bundle() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", json.dumps(MANIFEST, ensure_ascii=False))
        z.writestr("fakefolder/__init__.py", "")
        z.writestr("fakefolder/main.py", GEN)
    return buf.getvalue()


def make_gp_bundle() -> bytes:
    """The same bundle, but with a `sources` block - as after the generator was
    re-registered onto GreenPlum."""
    m = dict(MANIFEST, name="fakegp", entrypoint={"module": "fakefolder.main"},
             sources={"type": "greenplum", "connection": "analytics_dw",
                      "queries": [{"sql_file": "sql/01.sql",
                                   "output_name": "slot_0.csv.gz",
                                   "format": "csv_gz"}]})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", json.dumps(m, ensure_ascii=False))
        z.writestr("fakefolder/__init__.py", "")
        z.writestr("fakefolder/main.py", GEN)
    return buf.getvalue()


def run(work: Path, bundle: bytes, dash: str, scenario: str = "",
        mode: str | None = None) -> dict:
    r = runs.Run("r", work / ("w-" + str(abs(hash((dash, scenario, mode))))[:8]))
    meta = {
        "checksum_sha256": hashlib.sha256(bundle).hexdigest(),
        "run_id": "r1", "generator": "fakefolder", "version": "1.0",
        "dashboard_id": dash, "scenario": scenario,
    }
    if mode is not None:
        meta["mode"] = mode
    runs.run_generator(r, bundle, meta, config=None, cred_store=None)
    return {"run": r, "term": r._events[-1], "events": r._events}


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="smoke-r2folders-"))
    data = work / "Donn\u00e9es_\u00e9t\u00e9"
    data.mkdir()
    for nm, body in [("SL_01.xlsx", b"a" * 100), ("SL_02.xlsx", b"b" * 250),
                     ("autre.txt", "not data".encode("utf-8"))]:
        (data / nm).write_bytes(body)
    other = work / "Seconde"
    other.mkdir()
    (other / "SL_01.xlsx").write_bytes(b"c" * 7)      # the same NAME, another folder

    ini = work / "folders.ini"
    ini.write_text(
        f"[folders:dash-42]\ndir = {data}\ninclude = SL_*.xlsx\n\n"
        f"[folders:dash-42:premi\u00e8re-seule]\nfile = {data / 'SL_01.xlsx'}\n\n"
        f"[folders:dash-dup]\ndir = {data}\ndir = {other}\ninclude = SL_*.xlsx\n\n"
        f"[folders:dash-empty]\ndir = {work / 'dossier-absent'}\n",
        encoding="utf-8")
    os.environ["OP_FOLDERS_INI"] = str(ini)
    bundle = make_bundle()

    r = run(work, bundle, "dash-42")
    ok = r["term"].get("t") == "done"
    check("folder run reaches done", ok, str(r["term"])[:160])
    if ok:
        got = json.loads(r["run"].output_paths["dashboard.html"].read_text("utf-8"))
        check("the section mask cut off the non-data",
              got["files"] == ["SL_01.xlsx", "SL_02.xlsx"], str(got))
        check("the generator read the real bytes", got["bytes"] == 350, str(got))
    check("the analyst's files are untouched",
          sorted(p.name for p in data.iterdir()) == ["SL_01.xlsx", "SL_02.xlsx", "autre.txt"],
          str(sorted(p.name for p in data.iterdir())))

    r = run(work, bundle, "dash-42", "premi\u00e8re-seule")
    got = json.loads(r["run"].output_paths["dashboard.html"].read_text("utf-8")) \
        if r["term"].get("t") == "done" else {}
    check("a scenario gives its own set", got.get("files") == ["SL_01.xlsx"], str(got))

    r = run(work, bundle, "absent")
    check("no section -> folders_no_section, not an empty run",
          r["term"].get("kind") == "folders_no_section", str(r["term"])[:160])

    r = run(work, bundle, "dash-42", "invent\u00e9")
    t = r["term"]
    check("no scenario -> folders_no_scenario", t.get("kind") == "folders_no_scenario",
          str(t)[:160])
    check("the refusal lists the existing scenarios",
          "premi\u00e8re-seule" in str(t.get("detail", "")), str(t.get("detail")))

    r = run(work, bundle, "dash-dup")
    t = r["term"]
    check("the same names from different folders -> refusal",
          t.get("kind") == "folders_duplicate_names", str(t)[:160])
    check("the refusal names BOTH paths",
          str(data) in str(t.get("detail", "")) and str(other) in str(t.get("detail", "")),
          str(t.get("detail"))[:200])

    r = run(work, bundle, "dash-empty")
    t = r["term"]
    check("an empty folder -> refusal, not a report from nothing",
          t.get("kind") == "folders_empty", str(t)[:160])
    check("the loss is declared by a warning",
          any(e.get("t") == "warning" and str(e.get("kind", "")).startswith("folder_")
              for e in r["events"]),
          str([e for e in r["events"] if e.get("t") == "warning"])[:200])

    # K3: the page's `mode` against the bundle manifest. A divergence is not theory:
    # the generator could have been re-registered onto GreenPlum between the folder probe and
    # the button press, and then the person would get a report from the database instead of the
    # folder whose file list they had just seen on screen.
    r = run(work, make_gp_bundle(), "dash-42", mode="folders")
    t = r["term"]
    check("mode=folders against a manifest with sources -> mode_mismatch",
          t.get("kind") == "mode_mismatch", str(t)[:200])
    check("the refusal names BOTH sides of the divergence",
          "folder" in str(t.get("message", "")) and "sources" in str(t.get("message", "")),
          str(t.get("message"))[:200])
    # Indirect but strict: `cred_store=None`, so it was impossible to reach the password prompt -
    # the refusal came EARLIER, not after going for the credential.
    check("the refusal comes BEFORE the password prompt (not AttributeError on cred_store=None)",
          t.get("t") == "error" and t.get("kind") == "mode_mismatch", str(t)[:200])

    # The flip side: a folder run without a named report is loud too.
    r = run(work, bundle, "", mode="folders")
    check("mode=folders without dashboard_id -> folders_no_dashboard",
          r["term"].get("kind") == "folders_no_dashboard", str(r["term"])[:160])

    # ── Lineage: observations reach the run's event stream ──────────────
    # `observe` frames are taken by the host (with the same probe as the browser worker), and
    # the agent must carry them to the page UNTOUCHED: it sends them to the same
    # POST …/runs/{id}/observations. This is checked here, not in smoke_r2host,
    # because here the WHOLE chain passes: runs -> generator -> r2host and
    # back.
    r = run(work, bundle, "dash-42")
    obs = [e for e in r["events"] if e.get("t") == "observe"]
    check("observations reached the run events", bool(obs),
          str([e.get("t") for e in r["events"]])[:200])
    seen = {f.get("name"): f for fr in obs for f in (fr.get("files") or [])}
    check("an observation for every file read",
          sorted(seen) == ["SL_01.xlsx", "SL_02.xlsx"], str(sorted(seen)))
    one = seen.get("SL_01.xlsx") or {}
    check("the observation has the REAL path of the analyst's file",
          one.get("path") == str(data / "SL_01.xlsx"), str(one.get("path")))
    check("and the section entry number (dir), by which the page finds the root",
          one.get("dir") == 0, str(one.get("dir")))
    # The test files are not workbooks, so the schema must arrive WITH A REASON, not
    # empty: "not observed" and "observed and failed" are distinguished on the lineage
    # screen, and this is exactly the defect that was fixed in the browser.
    check("an unreadable workbook gives a schema with a REASON, not silence",
          isinstance(one.get("schema"), dict)
          and one["schema"].get("sheets") == []
          and bool(one["schema"].get("error")), str(one.get("schema"))[:200])
    # Into the run log (a file on the analyst's machine that travels to
    # support) only the COUNT of an observation goes: the frame carries sheet names
    # and columns, while "how many observations went out" answers the only question
    # asked of the log - why the lineage is empty.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        runs.Run("log", work / "w-log")._log(
            {"t": "observe",
             "files": [{"name": "SL_01.xlsx",
                        "schema": {"sheets": [{"name": "Feuille Secr\u00e8te"}]}}],
             "stages": []})
    line = buf.getvalue()
    check("the observe event in the log is a count, not the frame content",
          "files=1" in line and "Feuille Secr\u00e8te" not in line
          and "SL_01.xlsx" not in line, line.strip()[:200])

    # `matched_input` is computed by the agent (one globmatch port, `folders.matches`),
    # and computed as the browser does: the first template that fits.
    files = runs._resolve_folder_files(
        runs.Run("m", work / "w-match"), {"dashboard_id": "dash-42"},
        [{"match": "SL_0*.xlsx"}, {"match": "SL_*.xlsx"}])
    check("matched_input is the FIRST template that fits, as in the browser",
          [f.get("matched_input") for f in files] == ["SL_0*.xlsx", "SL_0*.xlsx"],
          str([f.get("matched_input") for f in files]))
    check("every file has dir = the section entry number",
          [f.get("dir") for f in files] == [0, 0], str([f.get("dir") for f in files]))
    files = runs._resolve_folder_files(
        runs.Run("m2", work / "w-match2"), {"dashboard_id": "dash-42"},
        [{"match": "aucun-match.xlsx"}])
    # A file recognised by no template is still READ: the set was chosen by the person in folders.ini,
    # and dropping a file they named would silently compute over half.
    check("a file recognised by no template stays in the set without a mark",
          len(files) == 2 and all("matched_input" not in f for f in files),
          str(files)[:200])

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): " + ", ".join(FAILS))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
