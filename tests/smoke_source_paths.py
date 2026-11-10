"""File and root paths sent by the desktop app
(RFC_DESKTOP_MIRROR, "Window and folders", decision 3; migration 0009).

The goal is practical: a person sees WHICH EXACT file was read. The test pins:
  * intake: a file has an optional `path`, a source has `root_path`/`root_id`
    (in the start manifest AND in the observation batch); an old client
    without these fields works as before — the columns stay NULL, pages intact;
  * parsing is silent: length is cut to 512 / 32, a non-string and junk
    `roots` are ignored without a 400 (a 400 on the final batch would lose the
    whole run's observations); a missing field does NOT erase a known value;
  * display: the sources page, provenance (page and JSON) and the lineage graph
    carry the root and file path and a "Copy path" button; the right is the
    same as on those pages (can_update_dashboard_data) — a report reader does
    not see paths;
  * the "folders are matched by name" caveat is narrowed and promises no more
    than it does;
  * boundaries: a path never reaches model prompts (nl2sql, explore, catalogue),
    never appears on share surfaces, and file values do not travel to the
    server (extra batch fields do not settle in source_files).

Synthetic data: no real folders, no real people.
"""
import io
import json
import os
import sys
import tempfile
import zipfile

TMP = tempfile.mkdtemp(prefix="op-smoke-source-paths-")
os.environ.update({
    "OP_DB_PATH": f"{TMP}/onepage.db",
    "OP_UPLOADS_DIR": f"{TMP}/uploads",
    "OP_PERF_DIR": f"{TMP}/perflog",
    "OP_DEV_BYPASS_AUTH": "true",
    "OP_INITIAL_ADMIN": "admin1",
    "OP_CACHE_WARM": "0",
})

sys.path.insert(0, os.getcwd())
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    DashboardPermission, DataSource, SourceFile, SourceObservation, User,
)

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"  <-- {extra}"))
    if not cond:
        FAILURES.append(label)


def login(c, ov):
    r = c.post("/dev-login", data={"person_id": ov}, follow_redirects=False)
    assert r.status_code == 302, f"{ov}: {r.status_code} {r.text[:200]}"


def create_user(c, ov):
    r = c.post("/api/admin/users", json={"person_id": ov, "role": "visitor", "display_name": ov})
    assert r.status_code == 200, f"{ov}: {r.status_code} {r.text[:200]}"


MARK = "ZZPATHMARK"          # appears ONLY in paths — leaks are searched by it
FOLDER = "PathTest_Folder"
ROOT = "\\\\fileserver\\" + MARK + "_OPS\\" + FOLDER
FILE_NAME = "PT_2026_09.xlsx"
FILE_PATH_START = ROOT + "\\" + FILE_NAME            # from the start manifest
FILE_PATH_BATCH = ROOT + "\\sub\\" + FILE_NAME       # from the batch: it wins
ROOT_ID = "a1b2c3d4e5f60718"
VALUE_MARK = "ZZVALUEMARK"   # a "value from the file" — must never be on the server

MANIFEST = {
    "name": "paths-gen", "version": "1.0",
    "entrypoint": {"module": "pathtest.main"},
    "outputs": {"html": True},
    "inputs": [{"match": "PT_*.xlsx", "role": "data",
                "sheets": [{"name": "Sheet", "columns": [{"name": "Code", "type": "text"}]}]}],
    "deps": [{"name": "pandas", "version": "2.2.0"}],
}


def make_bundle(manifest):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest))
        z.writestr("pathtest/__init__.py", "")
        z.writestr("pathtest/main.py", "def run(file_iter, out_dir, progress):\n    pass\n")
    return buf.getvalue()


_TICK = [0]


def _space_out(run_id):
    """An observation's created_at is CURRENT_TIMESTAMP with one-second
    resolution: two test runs in a row get ONE stamp, and "the latest
    observation" for datasources.build becomes a coin toss. In real life runs
    are minutes apart; the test spaces them out by hand, not with sleep."""
    from datetime import datetime, timedelta
    _TICK[0] += 1
    db = SessionLocal()
    try:
        (db.query(SourceObservation).filter(SourceObservation.run_id == run_id)
         .update({"created_at": datetime(2026, 10, 1) + timedelta(minutes=_TICK[0])}))
        db.commit()
    finally:
        db.close()


def start_run(c, dash_id, file_extra=None, roots=None, name=FILE_NAME):
    r = _start_run(c, dash_id, file_extra, roots, name)
    if r.status_code == 201:
        _space_out(r.json()["run_id"])
    return r


def _start_run(c, dash_id, file_extra=None, roots=None, name=FILE_NAME):
    f = {"name": name, "dir": 0, "size": 1024, "mtime_ms": 1_700_000_000_000}
    f.update(file_extra or {})
    im = {"dirs": [FOLDER], "files": [f], "skipped": [], "total_bytes": 1024, "count": 1}
    if roots is not None:
        im["roots"] = roots
    return c.post(f"/api/dashboards/{dash_id}/generator/runs",
                  json={"engine": "browser", "input_manifest": im})


def post_obs(c, dash_id, run_id, file_extra=None, roots=None, name=FILE_NAME, final=True):
    f = {"dir": 0, "name": name, "kind": "parsed"}
    f.update(file_extra or {})
    body = {"seq": 1, "files": [f], "final": final}
    if roots is not None:
        body["roots"] = roots
    return c.post(f"/api/dashboards/{dash_id}/generator/runs/{run_id}/observations", json=body)


def publish(c, dash_id, run_id):
    return c.post(f"/api/dashboards/{dash_id}/generator/runs/{run_id}/publish",
                  files={"html_file": ("dashboard.html", b"<html><body>ready</body></html>", "text/html")})


def db_state():
    """(DataSource, [SourceFiles of the latest observation]) straight from the DB."""
    db = SessionLocal()
    try:
        ds = db.query(DataSource).filter_by(kind="folder", key_name=FOLDER).one()
        obs = (db.query(SourceObservation).filter_by(data_source_id=ds.id)
               .order_by(SourceObservation.id.desc()).first())
        files = db.query(SourceFile).filter_by(observation_id=obs.id).all() if obs else []
        return (ds.root_path, ds.root_id), [(f.name, f.path) for f in files]
    finally:
        db.close()


with TestClient(app) as c:
    login(c, "admin1")
    r = c.post("/api/admin/generators",
               files={"bundle": ("bundle.zip", make_bundle(MANIFEST), "application/zip")})
    assert r.status_code == 201, r.text[:300]
    gid = r.json()["id"]
    r = c.post("/api/dashboards/generated", json={"title": "Paths report", "generator_id": gid})
    assert r.status_code == 201, r.text[:300]
    dash = r.json()["id"]

    # ── 1. old client: none of the new fields ──────────────────────────
    r = start_run(c, dash)
    check("old client: run start 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    run_a = r.json()["run_id"]
    r = post_obs(c, dash, run_a)
    check("old client: observations 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    root, files = db_state()
    check("old client: root_path/root_id stay NULL", root == (None, None), str(root))
    check("old client: file path stays NULL", files == [(FILE_NAME, None)], str(files))
    check("old client: publish ok", publish(c, dash, run_a).status_code == 200)
    check("old client: /data-sources 200", c.get("/data-sources").status_code == 200)
    r = c.get(f"/d/{dash}/provenance")
    check("old client: provenance page 200, no path block content",
          r.status_code == 200 and "data-copy-path" not in r.text, str(r.status_code))
    pj = c.get(f"/api/dashboards/{dash}/provenance").json()
    so = pj.get("source_observations") or []
    check("old client: provenance carries null/empty path fields",
          len(so) == 1 and so[0]["root_path"] is None and so[0]["file_paths"] == [], str(so))

    # ── 2. the app: path in the start manifest, the batch path wins ────
    r = start_run(c, dash, {"path": FILE_PATH_START}, [{"dir": 0, "root_path": ROOT, "root_id": ROOT_ID}])
    check("app: run start 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    run_b = r.json()["run_id"]
    root, files = db_state()
    check("app: root stored from the start manifest", root == (ROOT, ROOT_ID), str(root))
    check("app: file path stored from the start manifest", files == [(FILE_NAME, FILE_PATH_START)], str(files))
    # a "value from the file" in an extra batch field: the platform does not accept it
    r = post_obs(c, dash, run_b, {"path": FILE_PATH_BATCH, "values": [VALUE_MARK], "cells": VALUE_MARK})
    check("app: batch with path 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    root, files = db_state()
    check("app: batch path overrides the manifest one", files == [(FILE_NAME, FILE_PATH_BATCH)], str(files))
    check("app: root survives a batch without roots", root == (ROOT, ROOT_ID), str(root))
    _db = SessionLocal()
    blob = json.dumps([[getattr(f, col) for col in ("name", "path", "kind", "matched_input",
                                                    "schema_json", "consumed_json")]
                       for f in _db.query(SourceFile).all()], ensure_ascii=False)
    _db.close()
    check("boundary: a value-like extra field of a batch is not stored", VALUE_MARK not in blob)
    check("app: publish ok", publish(c, dash, run_b).status_code == 200)

    # ── 3. display to whoever has the right ───────────────────────────
    r = c.get("/data-sources")
    check("display: /data-sources 200", r.status_code == 200, str(r.status_code))
    check("display: sources page shows the root path", MARK in r.text and "_OPS" in r.text)
    check("display: sources page has a copy button",
          'data-copy-path' in r.text and "source-paths.js" in r.text)
    check("display: sources page shows the file path", FILE_PATH_BATCH in r.text, "file path not on the page")
    check("display: new caveat names the browser case and the root id",
          "Folders read in the browser are matched by name" in r.text
          and "compare the root id" in r.text, "caveat")
    check("display: old over-broad caveat is gone", "Folder sources are matched by name, not verified" not in r.text)
    check("display: no 'by name' chip for a source with a root id",
          "path reported" in r.text)

    r = c.get(f"/d/{dash}/provenance")
    check("display: provenance page shows root and file paths",
          r.status_code == 200 and MARK in r.text and ROOT_ID in r.text and "data-copy-path" in r.text
          and "source-paths.js" in r.text, str(r.status_code))
    pj = c.get(f"/api/dashboards/{dash}/provenance").json()
    so = pj.get("source_observations") or []
    check("display: provenance JSON carries root_path/root_id/file_paths",
          len(so) == 1 and so[0]["root_path"] == ROOT and so[0]["root_id"] == ROOT_ID
          and so[0]["file_paths"] == [FILE_PATH_BATCH], str(so))

    g = c.get("/api/data-sources/lineage").json()
    folder = [n for n in g["nodes"] if n["kind"] == "folder" and n["label"] == FOLDER]
    d = folder[0]["data"] if folder else {}
    check("display: lineage node carries root_path/root_id/file_paths",
          d.get("root_path") == ROOT and d.get("root_id") == ROOT_ID
          and d.get("file_paths") == [FILE_PATH_BATCH], str(d))
    check("display: lineage 'certain' lifts for a source with a root id", d.get("certain") is True, str(d))
    for js in ("lineage.js", "data-sources-edit.js", "source-paths.js"):
        check(f"display: {js} is served",
              c.get(f"/static/js/{js}").status_code == 200)

    # ── 4. the right is the same as on those pages ─────────────────────
    create_user(c, "reader1")
    _db = SessionLocal()
    reader = _db.query(User).filter_by(person_id="reader1").first()
    boss = _db.query(User).filter_by(person_id="admin1").first()
    _db.add(DashboardPermission(dashboard_id=dash, user_id=reader.id,
                                granted_by=boss.id, can_update_data=False))
    _db.commit()
    _db.close()
    login(c, "reader1")
    r = c.get(f"/d/{dash}/provenance")
    check("rights: reader sees the provenance page but no path", r.status_code == 200 and MARK not in r.text
          and ROOT_ID not in r.text, str(r.status_code))
    rj = c.get(f"/api/dashboards/{dash}/provenance")
    check("rights: reader's provenance JSON has no path", MARK not in rj.text and ROOT_ID not in rj.text)
    r = c.get("/data-sources")
    check("rights: reader's /data-sources has no path", MARK not in r.text and ROOT_ID not in r.text,
          str(r.status_code))
    r = c.get("/api/data-sources/lineage")
    check("rights: reader's lineage has no path", MARK not in r.text and ROOT_ID not in r.text)
    check("rights: reader cannot post observations to somebody else's run",
          post_obs(c, dash, run_b, {"path": MARK + "x"}).status_code in (403, 409))
    login(c, "admin1")

    # ── 5. boundaries: share, prompts ──────────────────────────────────
    r = c.post(f"/api/dashboards/{dash}/share-links", json={"expires_in_days": 7, "label": "x"})
    check("share: link created", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    tok = r.json().get("id")
    anon = TestClient(app)
    leaked = []
    for url in (f"/s/{tok}", f"/share/{tok}/render/"):
        body = anon.get(url).text
        if MARK in body or ROOT_ID in body:
            leaked.append(url)
    check("share: no path or root id on share surfaces", not leaked, str(leaked))

    def src(rel):
        with open(os.path.join(os.getcwd(), rel), encoding="utf-8") as fh:
            return fh.read()
    FORBIDDEN = ("root_path", "root_id", "file_paths", "SourceFile.path", ".declared_path")
    for rel in ("app/services/nl2sql.py", "app/api/explore.py", "app/services/catalog.py",
                "app/api/share.py", "app/api/catalog.py", "app/services/semantics.py"):
        text = src(rel)
        hits = [w for w in FORBIDDEN if w in text]
        # declared_path in catalog.py is a pre-existing catalogue field, not ours
        if rel == "app/services/catalog.py":
            hits = [w for w in hits if w != ".declared_path"]
        check(f"boundary: {rel} never touches paths", not hits, str(hits))
    nl = src("app/services/nl2sql.py")
    check("boundary: nl2sql does not import the source models",
          "SourceFile" not in nl and "DataSource" not in nl and "app.models" not in nl)
    for url in ("/api/explore/catalog", "/api/catalog"):
        rr = c.get(url)
        check(f"boundary: {url} carries no path", MARK not in rr.text and ROOT_ID not in rr.text,
              str(rr.status_code))

    # ── 6. silent parsing: junk, length, absence does not erase ─────────
    garbage_roots = [1, "x", {"dir": "a"}, {"dir": 99, "root_path": "z"}, {"dir": True, "root_path": "t"},
                     {"dir": 0, "root_path": 5, "root_id": ["q"]}]
    r = start_run(c, dash, {"path": 123}, garbage_roots)
    check("garbage: run start with junk path/roots still 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    run_c = r.json()["run_id"]
    root, files = db_state()
    check("garbage: junk does not touch the known root", root == (ROOT, ROOT_ID), str(root))
    check("garbage: non-string file path ignored", files == [(FILE_NAME, None)], str(files))
    r = post_obs(c, dash, run_c, {"path": {"x": 1}}, "junk")
    check("garbage: batch with junk path/roots still 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    r = post_obs(c, dash, run_c, {"path": ["a"]}, garbage_roots)
    check("garbage: list path + junk roots 200", r.status_code in (200, 409), f"{r.status_code} {r.text[:200]}")
    root, files = db_state()
    check("garbage: still no path stored", files == [(FILE_NAME, None)], str(files))
    check("garbage: publish ok", publish(c, dash, run_c).status_code == 200)

    long_path = "\\\\h\\" + "x" * 700
    r = start_run(c, dash, {"path": long_path}, [{"dir": 0, "root_path": long_path, "root_id": "f" * 80}])
    check("long: run start 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    run_d = r.json()["run_id"]
    root, files = db_state()
    check("long: root_path cut to 512, root_id cut to 32",
          root[0] == long_path[:512] and root[1] == "f" * 32, f"{len(root[0] or '')} {root[1]}")
    check("long: file path cut to 512", files and files[0][1] == long_path[:512],
          str([len(p or '') for _, p in files]))
    r = post_obs(c, dash, run_d, {"path": "  " + long_path + "  "})
    check("long: batch path cut to 512", r.status_code == 200 and db_state()[1][0][1] == long_path[:512],
          f"{r.status_code}")
    publish(c, dash, run_d)

    # a weak check that both new columns really are in the schema
    _db = SessionLocal()
    cols = {row[1] for row in _db.execute(__import__("sqlalchemy").text("PRAGMA table_info(source_files)"))}
    cols2 = {row[1] for row in _db.execute(__import__("sqlalchemy").text("PRAGMA table_info(data_sources)"))}
    _db.close()
    check("schema: source_files.path and data_sources.root_path/root_id exist",
          "path" in cols and {"root_path", "root_id"} <= cols2)

print()
print("ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILED: {FAILURES}")
sys.exit(1 if FAILURES else 0)
