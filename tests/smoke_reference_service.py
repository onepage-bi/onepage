"""Smoke test for ADR-16 reference tables — app/services/reference.py.

Mostly pure-function checks (norm_key, validate_schema, coerce_row,
diff_rows) plus a DB round-trip for the two DB helpers (create_version,
ensure_data_source) via a real Session, following the boot idiom from the
onepage-testing skill (tests/smoke_favorites.py). TestClient(app) is used
only to run the app's lifespan (create_all + migrations) before the DB
helpers touch the tables.
"""
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="op-ref-smoke-")
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
from app.services import reference as ref  # noqa: E402

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


def main():
    # ── norm_key ─────────────────────────────────────────────────────
    check("norm_key: nbsp trimmed", ref.norm_key("\xa0test\xa0") == "test")
    check("norm_key: en dash unified", ref.norm_key("a–b") == "a-b")
    check("norm_key: em dash unified", ref.norm_key("a—b") == "a-b")
    check("norm_key: minus sign unified", ref.norm_key("a−b") == "a-b")
    check("norm_key: double space collapsed", ref.norm_key("a  b") == "a b")
    check("norm_key: casefold", ref.norm_key("ABC") == "abc")
    check("norm_key: 7.0 -> '7'", ref.norm_key(7.0) == "7")
    check("norm_key: leading zeros kept", ref.norm_key("007") == "007")
    check("norm_key: None -> ''", ref.norm_key(None) == "")
    check("norm_key: NaN -> ''", ref.norm_key(float("nan")) == "")
    check("norm_key: trailing tab stripped", ref.norm_key("x\ty\t") == "x y")
    check("norm_key: leading/trailing spaces stripped", ref.norm_key("  trim  ") == "trim")
    check("norm_key: int passthrough", ref.norm_key(42) == "42")

    # ── validate_schema ──────────────────────────────────────────────
    ok_cols = [{"name": "code", "type": "text"}, {"name": "qty", "type": "number"}]
    out = ref.validate_schema(ok_cols)
    check("validate_schema: valid schema passes", out == ok_cols, str(out))

    def expect_err(cols, label):
        try:
            ref.validate_schema(cols)
            check(label, False, "no ValueError raised")
        except ValueError:
            check(label, True)

    expect_err([{"name": "only", "type": "text"}], "validate_schema: <2 columns rejected")
    expect_err([{"name": "n", "type": "number"}, {"name": "n", "type": "number"}],
               "validate_schema: first-column-not-text rejected")
    expect_err([{"name": "code", "type": "text"}, {"name": "code", "type": "number"}],
               "validate_schema: dup name (case-insensitive) rejected")
    expect_err([{"name": "code", "type": "text"}, {"name": "x", "type": "bogus"}],
               "validate_schema: bad type rejected")
    expect_err([{"name": "x" * 65, "type": "text"}, {"name": "y", "type": "text"}],
               "validate_schema: name > 64 chars rejected")
    expect_err("not-a-list", "validate_schema: non-list rejected")

    # dup name differing only by case
    expect_err([{"name": "Code", "type": "text"}, {"name": "code", "type": "text"}],
               "validate_schema: dup name differing only by case rejected")

    # ── coerce_row ───────────────────────────────────────────────────
    cols = [{"name": "code", "type": "text"}, {"name": "qty", "type": "number"},
            {"name": "label", "type": "text"}, {"name": "d", "type": "date"}]

    row, errs = ref.coerce_row(cols, ["A1", 12.0, "Toyota", None])
    check("coerce_row: number col from 12.0 -> 12.0, null date ok", row[1] == 12.0 and row[3] is None and not errs,
          str((row, errs)))

    row, errs = ref.coerce_row([{"name": "code", "type": "text"}, {"name": "label", "type": "text"}],
                                ["A1", 12.0])
    check("coerce_row: text col from 12.0 -> '12'", row[1] == "12" and not errs, str((row, errs)))

    row, errs = ref.coerce_row([{"name": "code", "type": "text"}, {"name": "qty", "type": "number"}],
                                ["A1", "1,5"])
    check("coerce_row: number from '1,5' -> 1.5", row[1] == 1.5 and not errs, str((row, errs)))

    row, errs = ref.coerce_row([{"name": "code", "type": "text"}, {"name": "qty", "type": "number"}],
                                ["A1", "1 234"])
    check("coerce_row: number from '1 234' -> 1234", row[1] == 1234.0 and not errs, str((row, errs)))

    row, errs = ref.coerce_row([{"name": "code", "type": "text"}, {"name": "qty", "type": "number"}],
                                ["A1", "abc"])
    check("coerce_row: number from 'abc' -> error", bool(errs), str((row, errs)))

    from datetime import datetime as _dt
    row, errs = ref.coerce_row([{"name": "code", "type": "text"}, {"name": "d", "type": "date"}],
                                ["A1", _dt(2026, 8, 26)])
    check("coerce_row: date from datetime -> ISO", row[1] == "2026-08-26" and not errs, str((row, errs)))

    row, errs = ref.coerce_row([{"name": "code", "type": "text"}, {"name": "d", "type": "date"}],
                                ["A1", "2026-08-26"])
    check("coerce_row: date from string -> error (by design)", bool(errs), str((row, errs)))

    row, errs = ref.coerce_row([{"name": "code", "type": "text"}, {"name": "x", "type": "text"}],
                                ["", "x"])
    check("coerce_row: empty key -> error", bool(errs) and any(e["column"] == "code" for e in errs), str(errs))

    row, errs = ref.coerce_row([{"name": "code", "type": "text"}, {"name": "x", "type": "text"}],
                                ["k" * 65, "x"])
    check("coerce_row: key > 64 chars -> error", bool(errs), str(errs))

    row, errs = ref.coerce_row([{"name": "code", "type": "text"}, {"name": "x", "type": "text"}],
                                ["k" * 64, "x"])
    check("coerce_row: key == 64 chars -> ok", not errs, str(errs))

    # ── diff_rows ────────────────────────────────────────────────────
    dcols = [{"name": "code", "type": "text"}, {"name": "label", "type": "text"}]
    old = [["A1", "Alpha"], ["A2", "Beta"], ["A3", "Gamma"]]

    # added / changed / removed(kept when delete_missing=False) / unchanged
    new = [["A1", "Alpha"], ["A2", "BETA-changed"], ["A4", "Delta"]]
    d = ref.diff_rows(dcols, old, new, delete_missing=False)
    check("diff_rows: added=1", d["added"] == 1, str(d))
    check("diff_rows: changed=1", d["changed"] == 1, str(d))
    check("diff_rows: unchanged=1", d["unchanged"] == 1, str(d))
    check("diff_rows: removed=0 when delete_missing=False", d["removed"] == 0, str(d))
    check("diff_rows: A3 kept when delete_missing=False",
          any(r[0] == "A3" for r in d["rows"]), str(d["rows"]))
    check("diff_rows: no errors/duplicates on clean input", not d["errors"] and not d["duplicates"], str(d))

    d2 = ref.diff_rows(dcols, old, new, delete_missing=True)
    check("diff_rows: removed=1 when delete_missing=True", d2["removed"] == 1, str(d2))
    check("diff_rows: A3 dropped when delete_missing=True",
          not any(r[0] == "A3" for r in d2["rows"]), str(d2["rows"]))

    # nbsp-variant of an existing key matches the existing row (not "added")
    new_nbsp = [["A1", "Alpha"], ["A2", "Beta"], ["A3", "Gamma"], ["A2\xa0", "Beta"]]
    d3 = ref.diff_rows(dcols, old, new_nbsp, delete_missing=False)
    check("diff_rows: nbsp-variant key matches existing (no spurious add)",
          d3["added"] == 0 and d3["duplicates"] == ["a2"], str(d3))

    # duplicate keys inside new_rows flagged, not merged
    dup_new = [["A1", "Alpha"], ["A1", "Alpha-dup"]]
    d4 = ref.diff_rows(dcols, [], dup_new, delete_missing=False)
    check("diff_rows: duplicate key flagged", d4["duplicates"] == ["a1"], str(d4))
    check("diff_rows: duplicate key not added", d4["added"] == 0, str(d4))

    # ── rows_sha256 / keys_norm ──────────────────────────────────────
    h1 = ref.rows_sha256(old)
    h2 = ref.rows_sha256(list(old))
    check("rows_sha256: deterministic", h1 == h2 and len(h1) == 64, h1)
    h3 = ref.rows_sha256(new)
    check("rows_sha256: differs on different content", h1 != h3)

    kn = ref.keys_norm(dcols, old + [["a1", "dup-of-A1-different-case"]])
    check("keys_norm: sorted unique normalised", kn == sorted({"a1", "a2", "a3"}), str(kn))

    # ── DB round-trip: create_version, ensure_data_source ─────────────
    with TestClient(app) as c:
        c.post("/dev-login", data={"person_id": "admin1"})
        from app.database import SessionLocal
        from app.models import ReferenceTable, User

        db = SessionLocal()
        try:
            admin = db.query(User).filter(User.person_id == "admin1").first()
            table = ReferenceTable(
                slug="nwd_bv_map", title="NWD BV mapping", key_column="code",
            )
            table.set_columns([{"name": "code", "type": "text"}, {"name": "name", "type": "text"}])
            db.add(table)
            db.commit()
            db.refresh(table)

            check("create_version: table starts at version 0", table.current_version == 0)

            v1 = ref.create_version(db, table, [["A1", "Alpha"], ["A2", "Beta"]], admin.id,
                                     {"added": 2, "changed": 0, "removed": 0})
            db.commit()
            db.refresh(table)
            check("create_version: bumps current_version to 1", table.current_version == 1, str(table.current_version))
            check("create_version: row_count matches", v1.row_count == 2, str(v1.row_count))
            check("create_version: updated_at set", table.updated_at is not None)

            v2 = ref.create_version(db, table, [["A1", "Alpha"], ["A2", "Beta"], ["A3", "Gamma"]],
                                     admin.id, {"added": 1, "changed": 0, "removed": 0})
            db.commit()
            db.refresh(table)
            check("create_version: bumps to 2 on second import", table.current_version == 2, str(table.current_version))
            check("create_version: version_number sequential", v2.version_number == 2, str(v2.version_number))

            rows = ref.current_rows(db, table)
            check("current_rows: reflects latest version", len(rows) == 3, str(rows))

            # refuse > MAX_ROWS
            try:
                ref.create_version(db, table, [["k%d" % i, "v"] for i in range(ref.MAX_ROWS + 1)],
                                    admin.id, {"added": 0, "changed": 0, "removed": 0})
                check("create_version: refuses > MAX_ROWS", False, "no ValueError raised")
            except ValueError:
                check("create_version: refuses > MAX_ROWS", True)
            db.rollback()
            db.refresh(table)
            check("create_version: version unchanged after refused import", table.current_version == 2,
                  str(table.current_version))

            # ensure_data_source idempotent
            ds1 = ref.ensure_data_source(db, table)
            db.commit()
            ds2 = ref.ensure_data_source(db, table)
            db.commit()
            check("ensure_data_source: idempotent (same row)", ds1.id == ds2.id, str((ds1.id, ds2.id)))
            check("ensure_data_source: kind/key_name set correctly",
                  ds1.kind == "table" and ds1.key_name == "nwd_bv_map",
                  str((ds1.kind, ds1.key_name)))
        finally:
            db.close()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
