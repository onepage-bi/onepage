"""ADR-16 × ADR-13: platform reference tables as a SEMANTICS source.

    .venv/bin/python tests/smoke_reference_semantics.py

What this pins, top to bottom:

  * the contract the validator accepts — `sources: [{"reference": slug}]`,
    exclusive of file/object/slot, columns without `source`, one
    `business_key`, text|number|date only;
  * registration against the reference tables that EXIST — a slug nobody
    created registers fine, a schema that disagrees is a 400 naming the diff;
  * the ADR-16 addendum: registering a version CREATES the declared table
    (empty), re-registering never touches its rows, a new column is added
    additively, a dropped column is fine and a changed type is a 400;
  * the snapshot binder — the rows of the CURRENT version travel with the
    semantics, a new import shows up without re-registering the generator,
    and no access / no table degrades to one `unbound` table;
  * the prompt stays values-free with a reference table in the semantics.

Boot idiom from tests/smoke_reference_api.py.
"""
import io
import json
import os
import sys
import tempfile
import zipfile

TMP = tempfile.mkdtemp(prefix="op-ref-sem-")
os.environ.update({
    "OP_DB_PATH": f"{TMP}/onepage.db",
    "OP_UPLOADS_DIR": f"{TMP}/uploads",
    "OP_PERF_DIR": f"{TMP}/perflog",
    "OP_DEV_BYPASS_AUTH": "true",
    "OP_INITIAL_ADMIN": "admin1",
    "OP_CACHE_WARM": "0",
    "OP_EXPLORE_ENABLED": "true",
})
sys.path.insert(0, os.getcwd())
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.services import nl2sql, reference as refsvc, semantics as sem_svc  # noqa: E402
from app.api.reference import VIA_CHAT  # noqa: E402

FAILURES = []
SLUG = "sl_client_map"


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


def login(c, person_id):
    r = c.post("/dev-login", data={"person_id": person_id}, follow_redirects=False)
    assert r.status_code == 302, f"dev-login {person_id}: {r.status_code}"


def sem_doc(name, ref_columns, *, slug=SLUG, extra_tables=None):
    """A minimal but REAL semantics: one file-backed table (so the snapshot
    has something to bind and does not drop the generator), one
    reference-backed table, one view over both, one KPI."""
    tables = {
        "orders": {
            "description": "Orders",
            "sources": [{"file": "SL_*.xlsx", "sheet": "Orders"}],
            "columns": {"client": {"source": "Customer", "type": "text", "role": "dim"},
                        "qty": {"source": "Order, pcs", "type": "number", "role": "measure"}},
        },
        "client_map": {
            "description": "Platform client lookup table",
            "sources": [{"reference": slug}],
            "business_key": "client",
            "columns": ref_columns,
        },
    }
    tables.update(extra_tables or {})
    return {
        "schema_version": 1, "generator": name, "tables": tables,
        "views": {"lines": "SELECT o.qty, coalesce(m.client_display, o.client) AS client_display "
                           "FROM orders o LEFT JOIN client_map m USING (client)"},
        "kpis": {"total": {"name": "Total", "definition": "Σ order", "aggregation": "sum",
                           "cell_sql": "SELECT client_display, SUM(qty) AS value FROM lines GROUP BY 1"}},
    }


REF_COLUMNS = {
    "client": {"type": "text", "role": "key", "description": "Client code"},
    "client_display": {"type": "text", "role": "dim", "description": "How to display"},
    "client_segment": {"type": "text", "role": "dim"},
}


def bundle(name, sem):
    m = {"name": name, "version": "1.0", "entrypoint": {"module": "g.main"},
         "outputs": {"html": True, "slots": []}, "engine": "auto"}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(m))
        z.writestr("g/__init__.py", "")
        z.writestr("g/main.py", "def run(*a, **k):\n    pass\n")
        if sem is not None:
            z.writestr("semantics.json", json.dumps(sem, ensure_ascii=False))
    return buf.getvalue()


def bundle_with_platform(name, sem, slug, platform_columns):
    """A bundle whose MANIFEST also declares the slug as a platform input —
    the case where one registration carries two declarations of one table."""
    m = {"name": name, "version": "1.0", "entrypoint": {"module": "g.main"},
         "outputs": {"html": True, "slots": []}, "engine": "auto",
         "inputs": [
             {"match": "SL_*.xlsx", "role": "data", "required": True,
              "sheets": [{"name": "Orders", "columns": ["Customer"]}]},
             {"match": f"{slug}.xlsx", "role": "reference", "source": "platform",
              "table": slug, "required": False,
              "sheets": [{"name": "data", "columns": platform_columns}]}]}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(m))
        z.writestr("g/__init__.py", "")
        z.writestr("g/main.py", "def run(*a, **k):\n    pass\n")
        if sem is not None:
            z.writestr("semantics.json", json.dumps(sem, ensure_ascii=False))
    return buf.getvalue()


def build_xlsx(header, rows) -> bytes:
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "data"
    ws.append(header)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def do_import(c, slug, content, expected_version):
    return c.post(f"/api/reference/{slug}/import",
                  data={"dry_run": "0", "expected_version": str(expected_version),
                        "delete_missing": "0"},
                  files={"file": ("data.xlsx", content,
                                  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})


# ── the catalogue table the file-backed source binds to (snapshot shape) ──
CATALOG_TABLES = [{
    "name": "sl", "table_id": "t_sl",
    "columns": [{"name": "customer"}, {"name": "order_pcs"}],
    "partitions": [{"file": "SL_1.xlsx", "sheet": "Orders"}],
}]


# ═════════════════════════════════════════════════════════════════════════
# Part 1 — the contract, with no server at all
# ═════════════════════════════════════════════════════════════════════════
check("semantics mirrors reference.SLUG_RE byte for byte",
      sem_svc._SLUG_RE.pattern == refsvc.SLUG_RE.pattern,
      f"{sem_svc._SLUG_RE.pattern} vs {refsvc.SLUG_RE.pattern}")

GOOD = sem_svc.normalize(sem_doc("ref-good", REF_COLUMNS))
check("a reference-backed table validates", sem_svc.validate(GOOD) == [], sem_svc.validate(GOOD))
# The frozen form: the dry run over EMPTY typed tables fills each KPI's dims —
# a reference table is materialised there exactly like an inline one, with no
# row anywhere.
check("dry run over a reference-backed table passes with no rows at all",
      sem_svc.dry_run(GOOD, inplace=True) == [], sem_svc.dry_run(GOOD))


def errs(mutate):
    doc = json.loads(json.dumps(sem_doc("ref-x", REF_COLUMNS)))
    mutate(doc["tables"]["client_map"])
    return sem_svc.validate(sem_svc.normalize(doc))


def bad(label, mutate, needle):
    e = errs(mutate)
    check(label, any(needle in x for x in e), str(e))


bad("reference + file in one source is refused",
    lambda t: t["sources"].__setitem__(0, {"reference": SLUG, "file": "x.xlsx"}),
    "exclusive")
bad("a malformed slug is refused",
    lambda t: t["sources"].__setitem__(0, {"reference": "NOT A SLUG"}),
    "reference table slug")
bad("a per-column `source` header is refused",
    lambda t: t["columns"]["client_display"].__setitem__("source", "Client"),
    "is not allowed on a reference-backed table")
bad("a type a reference table cannot hold is refused",
    lambda t: t["columns"]["client_display"].__setitem__("type", "datetime"),
    "must be one of ['date', 'number', 'text']")
bad("inline rows next to a reference source are refused",
    lambda t: t.__setitem__("rows", [{"client": "acme"}]),
    "must not carry inline rows")
bad("a missing business_key is refused",
    lambda t: t.pop("business_key"),
    "business_key must name the key column")
bad("a second source next to the reference is refused",
    lambda t: t["sources"].append({"file": "SL_*.xlsx"}),
    "exactly one source")

check("reference_sources reads slug/key/columns off the declaration",
      sem_svc.reference_sources(GOOD) == {"client_map": {
          "slug": SLUG, "key": "client",
          "columns": {"client": "text", "client_display": "text", "client_segment": "text"}}},
      str(sem_svc.reference_sources(GOOD)))

bound = sem_svc.bind(GOOD, CATALOG_TABLES)
check("bind: the reference table binds to no catalogue table, and says which slug",
      bound["client_map"] == {"inline": True, "reference": SLUG, "matches": []},
      str(bound["client_map"]))
check("bind: the file-backed table still binds normally",
      bound["orders"]["matches"] and bound["orders"]["matches"][0]["table"] == "sl",
      str(bound["orders"]))

SCHEMA = {SLUG: {"key_column": "client",
                 "columns": {"client": "text", "client_display": "text",
                             "client_segment": "text", "extra": "number"}}}
check("schema check: a table that does not exist is not an error",
      sem_svc.reference_schema_errors(GOOD, {}) == [], "")
check("schema check: a subset of the table's columns is fine",
      sem_svc.reference_schema_errors(GOOD, SCHEMA) == [],
      str(sem_svc.reference_schema_errors(GOOD, SCHEMA)))
WRONG_TYPE = sem_svc.normalize(sem_doc("ref-x", {
    **REF_COLUMNS, "client_segment": {"type": "number", "role": "dim"}}))
e = sem_svc.reference_schema_errors(WRONG_TYPE, SCHEMA)
check("schema check: a type that disagrees is a named diff",
      len(e) == 1 and "client_segment" in e[0] and "'number'" in e[0] and "'text'" in e[0], str(e))
NEW_COLUMN = sem_svc.normalize(sem_doc("ref-x", {
    "client": {"type": "text", "role": "key"}, "nope": {"type": "text", "role": "dim"}}))
check("schema check: a column the table does not have YET is not a conflict "
      "(ADR-16 addendum: it is added additively)",
      sem_svc.reference_schema_errors(NEW_COLUMN, SCHEMA) == [],
      str(sem_svc.reference_schema_errors(NEW_COLUMN, SCHEMA)))
WRONG_KEY = json.loads(json.dumps(GOOD))
WRONG_KEY["tables"]["client_map"]["business_key"] = "client_display"
e = sem_svc.reference_schema_errors(WRONG_KEY, SCHEMA)
check("schema check: a business_key that is not the table's key is a named diff",
      len(e) == 1 and "business_key" in e[0] and "client" in e[0], str(e))

# The intent prompt is built from the FROZEN semantics, which never carry a
# row — but pin it here too, next to the feature that could break it.
prompt = nl2sql.build_intent_prompt(GOOD, None, "how much did Acme order?")
check("intent prompt names the reference column and its author description",
      "client_display" in prompt and "How to display" in prompt, prompt[:600])
check("intent prompt carries NO cell value of the dictionary",
      "Acme Retail" not in prompt and "Bluebell" not in prompt, prompt[:600])
sql_prompt = nl2sql.build_semantic_sql_prompt(
    GOOD, {"tables": {"client_map": "reference", "orders": "bound"}, "views": {"lines": "ok"}},
    "how much did Acme order?")
check("tier-2 prompt describes a reference-backed table like any other",
      "client_map" in sql_prompt and "Acme Retail" not in sql_prompt, sql_prompt[:600])


# ═════════════════════════════════════════════════════════════════════════
# Part 2 — the routes
# ═════════════════════════════════════════════════════════════════════════
def main():
    from app.database import SessionLocal
    from app.models import ReferenceTable, User
    from app.api import explore as explore_api

    with TestClient(app) as c:
        login(c, "admin1")

        # ── registration with a reference nobody has created ───────────
        r = c.post("/api/admin/generators",
                   files={"bundle": ("b.zip", bundle("ref-report", sem_doc("ref-report", REF_COLUMNS)),
                                     "application/zip")})
        check("register: semantics with a `reference` source -> 201",
              r.status_code == 201, f"{r.status_code} {r.text[:300]}")
        gid = r.json()["id"]
        r = c.post("/api/dashboards/generated", json={"title": "Ref Report", "generator_id": gid})
        check("dashboard created", r.status_code == 201, r.text[:200])

        # ADR-16 addendum: registering created the table, empty.
        db = SessionLocal()
        t = db.query(ReferenceTable).filter_by(slug=SLUG).one_or_none()
        check("register: the declared reference table is auto-created, empty",
              t is not None and t.current_version == 0 and t.key_column == "client"
              and [c_["name"] for c_ in t.get_columns()]
              == ["client", "client_display", "client_segment"],
              str(t and t.get_columns()))
        check("register: the auto-created table says where it came from",
              t is not None and "ref-report" in (t.description or ""), str(t and t.description))
        check("register: nobody is made responsible for it",
              t is not None and t.responsible_user_id is None and t.allow_pin is False)
        admin = db.query(User).filter_by(person_id="admin1").first()
        db.close()

        # ── the snapshot: an empty table still binds, at v0 ─────────────
        def snapshot(user_name="admin1"):
            db = SessionLocal()
            try:
                u = db.query(User).filter_by(person_id=user_name).first()
                return explore_api._semantics_snapshot(db, u, CATALOG_TABLES)
            finally:
                db.close()

        def ref_of(entry_list, tname="client_map"):
            for e in entry_list:
                t = (e["semantics"].get("tables") or {}).get(tname)
                if t is not None:
                    return t.get("reference")
            return None

        snap = snapshot()
        check("snapshot: the generator is in it (its file table binds)", len(snap) == 1, str(len(snap)))
        rb = ref_of(snap)
        check("snapshot: an empty reference binds at v0 with no rows",
              rb and rb["status"] == "ok" and rb["version"] == 0 and rb["rows"] == [], str(rb))

        # ── an import shows up WITHOUT re-registering the generator ─────
        content = build_xlsx(["client", "client_display", "client_segment"],
                             [["acme", "Acme Retail", "chains"], ["bluebell", "Bluebell", "chains"]])
        r = do_import(c, SLUG, content, 0)
        check("import v1 -> 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        rb = ref_of(snapshot())
        check("snapshot: the CURRENT version's rows travel with the semantics",
              rb and rb["status"] == "ok" and rb["version"] == 1
              and rb["columns"] == ["client", "client_display", "client_segment"]
              and sorted(rb["rows"]) == sorted([["acme", "Acme Retail", "chains"],
                                                ["bluebell", "Bluebell", "chains"]]), str(rb))
        check("snapshot: the status names the version that answered",
              rb["message"] == f"reference table {SLUG} v1", rb["message"])
        check("snapshot: rows travel as lists, never as objects",
              all(isinstance(row, list) for row in rb["rows"]), str(rb["rows"][:1]))

        content2 = build_xlsx(["client", "client_display", "client_segment"],
                              [["acme", "Acme Retail Group", "chains"], ["nvy", "Novy", "local"]])
        r = do_import(c, SLUG, content2, 1)
        check("import v2 -> 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        rb = ref_of(snapshot())
        check("snapshot: a new version is visible with NO re-registration",
              rb["version"] == 2
              and ["acme", "Acme Retail Group", "chains"] in rb["rows"]
              and ["nvy", "Novy", "local"] in rb["rows"], str(rb))

        # ── a chat consumer is NOT reach (adversarial review, 2026-08-27)
        # Anyone who may register a generator can write `sources:
        # [{"reference": <someone else's slug>}]`. If that widened `_can_read`,
        # publishing a report that merely MENTIONS a dictionary would hand its
        # rows to everyone who can open that report.
        r = c.post("/api/admin/users", json={"person_id": "vis1", "role": "visitor",
                                             "display_name": "Vis One"})
        check("visitor created", r.status_code in (200, 201), r.text[:200])
        db = SessionLocal()
        vis = db.query(User).filter_by(person_id="vis1").first()
        t = db.query(ReferenceTable).filter_by(slug=SLUG).one()
        from app.api import reference as refapi
        check("a chat-only consumer does not give a stranger read access",
              refapi._can_read(db, vis, t) is False
              and refapi._can_write(db, vis, t) is False)
        check("\"in chat\" is still listed as a consumer, it is just not a key",
              any(VIA_CHAT in (c_.get("via") or [])
                  for c_ in refapi._consumers(db, SLUG))
              and refapi._run_consumers(db, SLUG) == [],
              str(refapi._consumers(db, SLUG)))
        db.close()

        # ── the union of the two declarations, not whichever came first ──
        # A manifest and a semantics naming the SAME slug describe one table
        # together: dropping either side's column would answer NULL for it
        # forever with nothing saying why.
        both = sem_doc("ref-both", REF_COLUMNS, slug="union_map")
        r = c.post("/api/admin/generators",
                   files={"bundle": ("b.zip", bundle_with_platform(
                       "ref-both", both, "union_map",
                       [{"name": "client", "type": "text"},
                        {"name": "client_display", "type": "text"}]),
                       "application/zip")})
        check("register: manifest + semantics on one slug -> 201",
              r.status_code == 201, f"{r.status_code} {r.text[:300]}")
        db = SessionLocal()
        t = db.query(ReferenceTable).filter_by(slug="union_map").one_or_none()
        check("union: the semantics-only column is created too",
              t is not None and [c_["name"] for c_ in t.get_columns()]
              == ["client", "client_display", "client_segment"],
              str(t and t.get_columns()))
        db.close()

        # ── the two halves contradicting each other is a 400, not a coin flip
        clash = sem_doc("ref-clash", {**REF_COLUMNS,
                                      "client_segment": {"type": "number", "role": "dim"}},
                        slug="clash_map")
        r = c.post("/api/admin/generators",
                   files={"bundle": ("b.zip", bundle_with_platform(
                       "ref-clash", clash, "clash_map",
                       [{"name": "client", "type": "text"},
                        {"name": "client_segment", "type": "text"}]),
                       "application/zip")})
        check("register: manifest and semantics disagreeing on a type -> 400",
              r.status_code == 400 and "client_segment" in r.text,
              f"{r.status_code} {r.text[:300]}")
        db = SessionLocal()
        check("register: the refused bundle created no dictionary",
              db.query(ReferenceTable).filter_by(slug="clash_map").first() is None)
        db.close()

        # ── a 400 from the reference check must not wedge the generator ──
        # `_write_version` puts bundle.zip on disk; a 400 AFTER it rolls the DB
        # back and leaves the directory, so the next upload picks the same
        # `v{n}` and dies on «versions are immutable».
        db = SessionLocal()
        t = db.query(ReferenceTable).filter_by(slug="union_map").one()
        t.set_columns([{"name": "client", "type": "text"},
                       {"name": "client_display", "type": "number"},
                       {"name": "client_segment", "type": "text"}])
        db.commit()
        db.close()
        gid_union = None
        for g_ in c.get("/api/generators").json():
            if g_["name"] == "ref-both":
                gid_union = g_["id"]
        r = c.post(f"/api/admin/generators/{gid_union}/versions",
                   files={"bundle": ("b.zip", bundle("ref-both", both), "application/zip")})
        check("re-register: a drift against the stored schema -> 400",
              r.status_code == 400, f"{r.status_code} {r.text[:250]}")
        db = SessionLocal()
        t = db.query(ReferenceTable).filter_by(slug="union_map").one()
        t.set_columns([{"name": "client", "type": "text"},
                       {"name": "client_display", "type": "text"},
                       {"name": "client_segment", "type": "text"}])
        db.commit()
        db.close()
        r = c.post(f"/api/admin/generators/{gid_union}/versions",
                   files={"bundle": ("b.zip", bundle("ref-both", both), "application/zip")})
        check("re-register: once the drift is resolved the generator is not wedged",
              r.status_code in (200, 201), f"{r.status_code} {r.text[:250]}")

        # ── no access -> unbound, with a reason ────────────────────────
        db = SessionLocal()
        vis = db.query(User).filter_by(person_id="vis1").first()
        sem = json.loads(json.dumps(sem_doc("ref-report", REF_COLUMNS)))
        explore_api._bind_reference_rows(db, vis, sem, [sem_svc.MAX_REFERENCE_BYTES], {})
        db.close()
        rb = sem["tables"]["client_map"]["reference"]
        check("no access to the dictionary -> unbound with «no access»",
              rb["status"] == "forbidden" and "no access" in rb["message"], str(rb))

        # ── the table gone -> unbound "has not been created" ──────────────────────
        db = SessionLocal()
        sem = json.loads(json.dumps(sem_doc("ref-report", {**REF_COLUMNS})))
        sem["tables"]["client_map"]["sources"] = [{"reference": "nobody_made_this"}]
        explore_api._bind_reference_rows(db, admin, sem, [sem_svc.MAX_REFERENCE_BYTES], {})
        db.close()
        rb = sem["tables"]["client_map"]["reference"]
        check("a reference nobody created -> unbound with «has not been created»",
              rb["status"] == "missing" and "has not been created" in rb["message"], str(rb))

        # ── "Consumers": the chat counts, and says so ──────────────────
        r = c.get(f"/api/reference/{SLUG}/consumers")
        cons = r.json() if r.status_code == 200 else []
        check("consumers: the report that reads it in the chat is listed",
              len(cons) == 1 and cons[0]["generator_name"] == "ref-report"
              and cons[0]["via"] == ["chat"], str(cons))
        r = c.get("/api/reference")
        row = next((x for x in r.json() if x["slug"] == SLUG), None)
        check("list: the row carries its consumer generators for the filter",
              row and [g["name"] for g in row["consumer_generators"]] == ["ref-report"],
              str(row and row.get("consumer_generators")))

        # ── registration against an EXISTING table that disagrees ──────
        bad_sem = sem_doc("ref-bad", {**REF_COLUMNS,
                                      "client_segment": {"type": "number", "role": "dim"}})
        r = c.post("/api/admin/generators",
                   files={"bundle": ("b.zip", bundle("ref-bad", bad_sem), "application/zip")})
        check("register: a type that disagrees with the existing table -> 400 with the diff",
              r.status_code == 400 and "client_segment" in r.text, f"{r.status_code} {r.text[:300]}")
        db = SessionLocal()
        t = db.query(ReferenceTable).filter_by(slug=SLUG).one()
        check("register: the refused bundle left the table untouched",
              t.current_version == 2
              and [c_["type"] for c_ in t.get_columns()] == ["text", "text", "text"],
              str(t.get_columns()))
        db.close()

        # ── re-registering the SAME schema changes nothing ─────────────
        db = SessionLocal()
        t = db.query(ReferenceTable).filter_by(slug=SLUG).one()
        before = (t.current_version, json.dumps(t.get_columns(), ensure_ascii=False),
                  refsvc.rows_sha256(refsvc.current_rows(db, t)), t.responsible_user_id)
        db.close()
        r = c.post(f"/api/admin/generators/{gid}/versions",
                   files={"bundle": ("b.zip", bundle("ref-report", sem_doc("ref-report", REF_COLUMNS)),
                                     "application/zip")})
        check("re-register: same schema -> ok", r.status_code in (200, 201), f"{r.status_code} {r.text[:300]}")
        db = SessionLocal()
        t = db.query(ReferenceTable).filter_by(slug=SLUG).one()
        after = (t.current_version, json.dumps(t.get_columns(), ensure_ascii=False),
                 refsvc.rows_sha256(refsvc.current_rows(db, t)), t.responsible_user_id)
        db.close()
        check("re-register: version, schema, rows and responsible are untouched",
              before == after, f"{before} != {after}")

        # ── a NEW column is added additively, rows survive ─────────────
        WIDER = {**REF_COLUMNS, "client_manager": {"type": "text", "role": "dim"}}
        r = c.post(f"/api/admin/generators/{gid}/versions",
                   files={"bundle": ("b.zip", bundle("ref-report", sem_doc("ref-report", WIDER)),
                                     "application/zip")})
        check("re-register: a new column -> ok", r.status_code in (200, 201), f"{r.status_code} {r.text[:300]}")
        db = SessionLocal()
        t = db.query(ReferenceTable).filter_by(slug=SLUG).one()
        cols = [c_["name"] for c_ in t.get_columns()]
        rows_now = refsvc.current_rows(db, t)
        db.close()
        check("re-register: the new column is appended, key and order kept",
              cols == ["client", "client_display", "client_segment", "client_manager"], str(cols))
        check("re-register: existing rows survive, padded to the new width",
              len(rows_now) == 3 and all(len(row) == 4 for row in rows_now)
              and ["acme", "Acme Retail Group", "chains", None] in rows_now, str(rows_now))
        r = c.get(f"/api/reference/{SLUG}/versions/current/xlsx")
        check("re-register: the export carries the new column",
              r.status_code == 200 and len(r.content) > 0, str(r.status_code))
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(r.content))
        header = [cell.value for cell in wb.active[1]]
        # `_build_xlsx` appends its own `_key_norm` helper column last.
        check("re-register: the exported header IS the widened schema",
              header[:4] == ["client", "client_display", "client_segment", "client_manager"],
              str(header))

        # ── a DROPPED column is not a conflict, the table keeps it ─────
        NARROW = {k: v for k, v in REF_COLUMNS.items() if k != "client_segment"}
        r = c.post(f"/api/admin/generators/{gid}/versions",
                   files={"bundle": ("b.zip", bundle("ref-report", sem_doc("ref-report", NARROW)),
                                     "application/zip")})
        check("re-register: a generator that stops reading a column -> ok",
              r.status_code in (200, 201), f"{r.status_code} {r.text[:300]}")
        db = SessionLocal()
        t = db.query(ReferenceTable).filter_by(slug=SLUG).one()
        check("re-register: the dictionary keeps the column the report dropped",
              [c_["name"] for c_ in t.get_columns()]
              == ["client", "client_display", "client_segment", "client_manager"],
              str(t.get_columns()))
        db.close()

        # ── a version that declares NO reference leaves it alive ───────
        r = c.post(f"/api/admin/generators/{gid}/versions",
                   files={"bundle": ("b.zip", bundle("ref-report", None), "application/zip")})
        check("re-register: a version with no semantics at all -> ok",
              r.status_code in (200, 201), f"{r.status_code} {r.text[:300]}")
        db = SessionLocal()
        t = db.query(ReferenceTable).filter_by(slug=SLUG).one_or_none()
        check("re-register: dropping the reference does NOT delete the table",
              t is not None and t.current_version == 2, str(t and t.current_version))
        db.close()

        # ── deletion is a person's act, and it is guarded ──────────────
        login(c, "vis1")
        r = c.delete(f"/api/reference/{SLUG}")
        check("delete: a user without write access -> 403", r.status_code == 403, str(r.status_code))
        login(c, "admin1")
        r = c.delete(f"/api/reference/{SLUG}")
        check("delete: an admin may remove the table", r.status_code == 204, str(r.status_code))
        db = SessionLocal()
        check("delete: the table and its versions are gone",
              db.query(ReferenceTable).filter_by(slug=SLUG).one_or_none() is None)
        from app.models import AuditLog
        actions = {a.action for a in db.query(AuditLog).all()}
        check("audit: auto-create and delete are both logged",
              {"reference_create", "reference_delete"} <= actions, str(sorted(actions)))
        db.close()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURES: " + ", ".join(FAILURES))
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
