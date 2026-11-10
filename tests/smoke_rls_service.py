"""RLS service (ADR-18): norm, policy, slot-file filter, serving.

Unit level: the HTTP routes are pinned by tests/smoke_rls.py, this is the engine itself.
Values are synthetic (three invented territories and four customers);
it checks exactly what ADR-18 exists for: a viewer gets neither
ROWS nor DICTIONARY VALUES of another territory, and a missing
user_scopes entry yields an empty but valid file.
"""
import gzip
import json
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="op-rls-svc-")
os.environ.update({
    "OP_DB_PATH": f"{TMP}/onepage.db",
    "OP_UPLOADS_DIR": f"{TMP}/uploads",
    "OP_PERF_DIR": f"{TMP}/perflog",
    "OP_DEV_BYPASS_AUTH": "true",
    "OP_INITIAL_ADMIN": "admin1",
    "OP_CACHE_WARM": "0",
})
sys.path.insert(0, os.getcwd())
from starlette.requests import Request  # noqa: E402

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models import AuditLog, Dashboard, ROLE_ADMINISTRATOR, ROLE_VISITOR, User, UserScope  # noqa: E402
from app.services import opc1, rls, storage  # noqa: E402

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"  {extra}"))
    if not cond:
        FAILURES.append(label)


# --- synthetic data mart --------------------------------------------------
NORTH, SOUTH, EAST = "Rep North-1", "Rep South-2", "Rep East-3"
SCHEMA = [("territory", "dict"), ("customer_number", "dict"), ("qty", "i32")]
FACTS = [                                   # slot 0: rows by territory
    [NORTH, "C-001", "5"],
    [SOUTH, "C-002", "7"],
    [NORTH, "C-003", "1"],
    [EAST, "C-004", "9"],
]
CUSTOMERS = [                               # slot 1: facts by customer (via)
    ["C-001", "10"],
    ["C-002", "20"],
    ["C-003", "30"],
    ["C-004", "40"],
]
POLICY = {
    "slots": [
        {"index": 0, "column": "territory"},
        {"index": 1, "key": "customer_number",
         "via": {"slot": 0, "key": "customer_number", "column": "territory"}},
        {"index": 2, "column": "territory"},
        {"index": 3, "column": "territory"},
    ],
    "passthrough": [4],
}


def opc1_facts():
    cols = {n: [r[i] for r in FACTS] for i, n in enumerate(("territory", "customer_number", "qty"))}
    return opc1.encode(SCHEMA, cols)


def csv_facts() -> bytes:
    lines = ["territory,customer_number,qty"]
    lines += [",".join(r) for r in FACTS]
    return ("\n".join(lines) + "\n").encode("utf-8")


def json_facts() -> bytes:
    rows = [{"territory": t, "customer_number": c, "qty": int(q)} for t, c, q in FACTS]
    return json.dumps({"rows": rows}, ensure_ascii=False).encode("utf-8")


def customers_csv() -> bytes:
    return ("customer_number,amount\n" + "\n".join(",".join(r) for r in CUSTOMERS) + "\n").encode("utf-8")


def load_slot(index: int):
    """via source for filter_bytes (in production, a file from disk)."""
    if index == 0:
        return "facts.bin", opc1_facts()
    raise AssertionError(f"unexpected via slot {index}")


def make_request(headers=None, query=b""):
    scope = {
        "type": "http", "method": "GET", "path": "/render/x/files/0",
        "query_string": query, "root_path": "", "scheme": "http",
        "server": ("test", 80), "client": ("test", 1),
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    }
    return Request(scope)


def main():
    keys_north = frozenset({rls.norm(NORTH)})
    keys_two = frozenset({rls.norm(NORTH), rls.norm(SOUTH)})

    # --- norm -------------------------------------------------------------
    check("norm: casefold + whitespace collapse",
          rls.norm("  Rep  North-1 ") == rls.norm(NORTH), rls.norm("  Rep  North-1 "))
    check("norm: None -> empty", rls.norm(None) == "" and rls.norm("   ") == "")
    check("norm: case", rls.norm("ABC") == rls.norm("abc") == "abc")

    # --- validate_policy --------------------------------------------------
    check("policy: the pilot is valid", rls.validate_policy(POLICY) == [],
          rls.validate_policy(POLICY))
    negatives = {
        "not an object": "no",
        "empty slots": {"slots": []},
        "no column|via": {"slots": [{"index": 0}]},
        "both column and via": {"slots": [{"index": 0, "column": "a",
                                       "via": {"slot": 1, "key": "k", "column": "c"}}]},
        "unknown slot field": {"slots": [{"index": 0, "colum": "a"}]},
        "unknown top-level field": {"slots": [{"index": 0, "column": "a"}], "extra": 1},
        "duplicate index": {"slots": [{"index": 0, "column": "a"}, {"index": 0, "column": "b"}]},
        "index in passthrough": {"slots": [{"index": 0, "column": "a"}], "passthrough": [0]},
        "negative index": {"slots": [{"index": -1, "column": "a"}]},
        "via to an undeclared slot": {"slots": [
            {"index": 1, "key": "k", "via": {"slot": 9, "key": "k", "column": "c"}}]},
        "via to via (depth 2)": {"slots": [
            {"index": 0, "key": "k", "via": {"slot": 1, "key": "k", "column": "c"}},
            {"index": 1, "key": "k", "via": {"slot": 0, "key": "k", "column": "c"}}]},
        "empty column name": {"slots": [{"index": 0, "column": "  "}]},
        "name longer than 128": {"slots": [{"index": 0, "column": "x" * 129}]},
        "passthrough not a list": {"slots": [{"index": 0, "column": "a"}], "passthrough": 3},
        "key without via": {"slots": [{"index": 0, "column": "a", "key": "k"}]},
    }
    for label, bad in negatives.items():
        check(f"policy negative: {label}", bool(rls.validate_policy(bad)),
              rls.validate_policy(bad))

    # --- OPC1 --------------------------------------------------------------
    raw = opc1_facts()
    out, rin, rout = rls.filter_bytes(POLICY, 0, "facts.bin", raw, keys_north, load_slot)
    desc, cols = opc1.decode(out)
    check("OPC1: own rows only", cols["territory"] == [NORTH, NORTH], cols["territory"])
    check("OPC1: counters", (rin, rout) == (4, 2), (rin, rout))
    check("OPC1: dictionary without other territories",
          desc["cols"][0]["values"] == [NORTH], desc["cols"][0]["values"])
    check("OPC1: other territory absent from the bytes", SOUTH.encode("utf-8") not in out
          and EAST.encode("utf-8") not in out)
    check("OPC1: other customer absent from the bytes", b"C-004" not in out)
    check("OPC1: column types intact", opc1.schema_of(desc) == SCHEMA, opc1.schema_of(desc))

    out2, _, rout2 = rls.filter_bytes(POLICY, 0, "facts.bin", raw, keys_two, load_slot)
    d2, c2 = opc1.decode(out2)
    check("OPC1: two territories", rout2 == 3 and set(c2["territory"]) == {NORTH, SOUTH},
          c2["territory"])

    empty, _, rout0 = rls.filter_bytes(POLICY, 0, "facts.bin", raw, frozenset(), load_slot)
    edesc, ecols = opc1.decode(empty)
    check("OPC1: empty keys -> 0 rows, file valid",
          rout0 == 0 and edesc["rows"] == 0 and ecols["territory"] == [])
    check("OPC1: the empty file contains no territory at all",
          all(t.encode("utf-8") not in empty for t in (NORTH, SOUTH, EAST)))

    gz_in = gzip.compress(raw, mtime=0)
    gz_out, _, gz_rows = rls.filter_bytes(POLICY, 0, "facts.bin.gz", gz_in, keys_north, load_slot)
    check("OPC1 .gz: output is gzip too", gz_out[:2] == b"\x1f\x8b")
    check("OPC1 .gz: content filtered",
          gz_rows == 2 and opc1.decode(gzip.decompress(gz_out))[1]["territory"] == [NORTH, NORTH])
    check("OPC1 .gz: bytes are deterministic (mtime=0)",
          rls.filter_bytes(POLICY, 0, "facts.bin.gz", gz_in, keys_north, load_slot)[0] == gz_out)

    # --- CSV ---------------------------------------------------------------
    cout, crin, crout = rls.filter_bytes(POLICY, 2, "facts.csv", csv_facts(), keys_north, load_slot)
    lines = cout.decode("utf-8").strip().split("\n")
    check("CSV: header preserved", lines[0] == "territory,customer_number,qty", lines[0])
    check("CSV: own rows only", len(lines) == 3 and all(NORTH in l for l in lines[1:]), lines)
    check("CSV: counters", (crin, crout) == (4, 2), (crin, crout))
    cempty, _, _ = rls.filter_bytes(POLICY, 2, "facts.csv", csv_facts(), frozenset(), load_slot)
    check("CSV: empty keys -> header only",
          cempty.decode("utf-8").strip() == "territory,customer_number,qty", cempty)
    cgz, _, _ = rls.filter_bytes(POLICY, 2, "facts.csv.gz", gzip.compress(csv_facts(), mtime=0),
                                 keys_two, load_slot)
    check("CSV .gz: round-trip", gzip.decompress(cgz).decode("utf-8").count("\n") == 4,
          gzip.decompress(cgz))

    # --- JSON --------------------------------------------------------------
    jout, jrin, jrout = rls.filter_bytes(POLICY, 3, "facts.json", json_facts(), keys_north, load_slot)
    doc = json.loads(jout)
    check("JSON: rows wrapper preserved", isinstance(doc, dict) and "rows" in doc, doc)
    check("JSON: own rows only", [r["territory"] for r in doc["rows"]] == [NORTH, NORTH],
          doc["rows"])
    check("JSON: counters", (jrin, jrout) == (4, 2), (jrin, jrout))
    plain = json.dumps([{"territory": t, "customer_number": c} for t, c, _ in FACTS],
                       ensure_ascii=False).encode("utf-8")
    pout, _, _ = rls.filter_bytes(POLICY, 3, "facts.json", plain, keys_two, load_slot)
    check("JSON: a bare array stays an array", isinstance(json.loads(pout), list))

    # --- via ---------------------------------------------------------------
    vout, vrin, vrout = rls.filter_bytes(POLICY, 1, "customers.csv", customers_csv(),
                                         keys_north, load_slot)
    vlines = vout.decode("utf-8").strip().split("\n")
    check("via: customers of own territory only",
          [l.split(",")[0] for l in vlines[1:]] == ["C-001", "C-003"], vlines)
    check("via: counters", (vrin, vrout) == (4, 2), (vrin, vrout))
    vempty, _, _ = rls.filter_bytes(POLICY, 1, "customers.csv", customers_csv(),
                                    frozenset(), load_slot)
    check("via: empty keys -> no customers",
          vempty.decode("utf-8").strip() == "customer_number,amount", vempty)

    # via with an EMPTY key in the source: a source row in my scope but without
    # a customer number must not open every target row with an empty key
    # (regression: the empty string got into allowed and pulled in other rows).
    via_src = ("territory,customer_number\n"
               f"{NORTH},C-001\n{NORTH},\n{SOUTH},C-009\n").encode("utf-8")
    via_tgt = ("customer_number,secret\n"
               "C-001,mine\n,theirs\nC-009,theirs2\n").encode("utf-8")
    via_policy = {"slots": [
        {"index": 0, "column": "territory"},
        {"index": 1, "key": "customer_number",
         "via": {"slot": 0, "key": "customer_number", "column": "territory"}},
    ]}
    bout, brin, brout = rls.filter_bytes(via_policy, 1, "t.csv", via_tgt, keys_north,
                                         lambda i: ("src.csv", via_src))
    btext = bout.decode("utf-8")
    check("via: an empty source key does not open rows with an empty key",
          brout == 1 and "theirs" not in btext and "C-001,mine" in btext,
          (brin, brout, btext))

    # The slot honestly published zero rows: an empty but valid file, not a 403
    # "no column" (columns are not visible at all in an empty JSON array).
    z_arr, zin, zout = rls.filter_bytes(POLICY, 3, "facts.json", b"[]", keys_north, load_slot)
    check("JSON: zero rows -> empty array, not 403",
          json.loads(z_arr) == [] and (zin, zout) == (0, 0), (z_arr, zin, zout))
    z_wrap, _, _ = rls.filter_bytes(POLICY, 3, "facts.json", b'{"meta":1,"rows":[]}',
                                    keys_north, load_slot)
    check("JSON: zero rows in a wrapper -> wrapper intact",
          json.loads(z_wrap) == {"meta": 1, "rows": []}, z_wrap)
    z_csv, _, _ = rls.filter_bytes(POLICY, 2, "facts.csv", b"territory,qty\n",
                                   keys_north, load_slot)
    check("CSV: zero rows -> header only", z_csv.decode("utf-8").strip() == "territory,qty",
          z_csv)
    z_bin, _, _ = rls.filter_bytes(POLICY, 0, "facts.bin",
                                   opc1.encode(SCHEMA, {n: [] for n, _ in SCHEMA}),
                                   keys_north, load_slot)
    check("OPC1: zero rows -> valid empty file", opc1.decode(z_bin)[0]["rows"] == 0, z_bin[:16])

    # --- filter negatives ---------------------------------------------------
    def expect(label, status, fn):
        try:
            fn()
            check(label, False, "no exception was raised")
        except rls.RlsError as e:
            check(label, e.status == status, f"{e.status} {e.detail}")

    expect("no column -> 403", 403, lambda: rls.filter_bytes(
        {"slots": [{"index": 0, "column": "no_such"}]}, 0, "facts.bin", raw,
        keys_north, load_slot))
    expect("slot outside the policy -> 403", 403, lambda: rls.filter_bytes(
        POLICY, 9, "facts.bin", raw, keys_north, load_slot))
    expect("foreign extension -> 415", 415, lambda: rls.filter_bytes(
        POLICY, 0, "facts.xlsx", raw, keys_north, load_slot))
    expect("broken OPC1 -> 415", 415, lambda: rls.filter_bytes(
        POLICY, 0, "facts.bin", b"NOPE" + raw[4:], keys_north, load_slot))
    expect("non-utf8 CSV -> 415", 415, lambda: rls.filter_bytes(
        POLICY, 2, "facts.csv", b"territory\n\xff\xfe\n", keys_north, load_slot))
    expect("JSON not an array -> 415", 415, lambda: rls.filter_bytes(
        POLICY, 3, "facts.json", b'{"a": 1}', keys_north, load_slot))

    pt, _, _ = rls.filter_bytes(POLICY, 4, "lib.js", b"console.log(1)", frozenset(), load_slot)
    check("passthrough: served as is", pt == b"console.log(1)", pt)
    check("passthrough: is_passthrough", rls.is_passthrough(POLICY, 4)
          and not rls.is_passthrough(POLICY, 0))

    # --- subject and serving ---------------------------------------------------
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    owner = User(person_id="owner1", role=ROLE_VISITOR, display_name="owner")
    tmr = User(person_id="tmr1", role=ROLE_VISITOR, display_name="tmr")
    hou = User(person_id="hou1", role=ROLE_VISITOR, display_name="hou")
    none = User(person_id="none1", role=ROLE_VISITOR, display_name="none")
    adm = User(person_id="adm1", role=ROLE_ADMINISTRATOR, display_name="adm")
    db.add_all([owner, tmr, hou, none, adm])
    db.commit()
    db.add_all([
        UserScope(person_id="tmr1", see_all=False, roles="TMR",
                  keys_json=json.dumps([rls.norm(NORTH)]), key_count=1),
        UserScope(person_id="hou1", see_all=False, roles="HOU",
                  keys_json=json.dumps([rls.norm(NORTH), rls.norm(SOUTH)]), key_count=2),
    ])
    dash = Dashboard(id="rls-svc-1", title="RLS", dash_type="interactive",
                     owner_id=owner.id, current_version=1,
                     rls_json=json.dumps(POLICY))
    db.add(dash)
    db.commit()

    s_tmr = rls.subject(db, dash, tmr)
    s_hou = rls.subject(db, dash, hou)
    s_none = rls.subject(db, dash, none)
    check("subject: tmr1 has its own keys", s_tmr.keys == keys_north and not s_tmr.see_all, s_tmr.keys)
    check("subject: hou1 has two keys", s_hou.keys == keys_two, s_hou.keys)
    check("subject: no row -> empty, NOT see_all",
          s_none.keys == frozenset() and not s_none.see_all)
    check("subject: admin see_all", rls.subject(db, dash, adm).see_all)
    check("subject: owner see_all", rls.subject(db, dash, owner).see_all)
    check("subject: keys_hash differs for different keys", s_tmr.keys_hash != s_hou.keys_hash)
    check("subject: see_all -> keys_hash 'all'", rls.subject(db, dash, adm).keys_hash == "all")

    storage.init_dashboard(dash.id)
    storage.save_slot_file(dash.id, 1, 0, "facts.bin.gz", gzip.compress(raw, mtime=0))
    storage.save_slot_file(dash.id, 1, 4, "lib.js", b"console.log(1)")

    rls.cache.clear()
    before = db.query(AuditLog).filter(AuditLog.action == "rls_serve").count()
    req = make_request({"accept-encoding": "gzip"})
    resp = rls.serve_filtered(req, db, dash, 0, s_tmr)
    check("serve: 200", resp.status_code == 200, resp.status_code)
    check("serve: Content-Encoding gzip", resp.headers.get("content-encoding") == "gzip",
          dict(resp.headers))
    body = gzip.decompress(resp.body)
    check("serve: body filtered", opc1.decode(body)[1]["territory"] == [NORTH, NORTH])
    check("serve: other territory absent from the body", SOUTH.encode("utf-8") not in body)
    check("serve: private Cache-Control, revalidation by ETag",
          resp.headers["cache-control"] == "private, no-cache",
          resp.headers.get("cache-control"))
    check("serve: Vary includes Cookie", resp.headers["vary"] == "Accept-Encoding, Cookie",
          resp.headers.get("vary"))
    etag = resp.headers["etag"]
    check("serve: ETag contains keys_hash", s_tmr.keys_hash in etag, etag)
    check("serve: Content-Disposition", "facts.bin.gz" in resp.headers.get("content-disposition", ""),
          resp.headers.get("content-disposition"))
    after = db.query(AuditLog).filter(AuditLog.action == "rls_serve").count()
    check("audit: one row on a miss", after == before + 1, (before, after))
    detail = db.query(AuditLog).filter(AuditLog.action == "rls_serve") \
        .order_by(AuditLog.id.desc()).first().get_detail()
    check("audit: counters only",
          detail == {"slot": 0, "rows_in": 4, "rows_out": 2, "keys": 1}, detail)

    resp2 = rls.serve_filtered(make_request({"accept-encoding": "gzip"}), db, dash, 0, s_tmr)
    check("cache: a hit does not write audit",
          db.query(AuditLog).filter(AuditLog.action == "rls_serve").count() == after)
    check("cache: same ETag", resp2.headers["etag"] == etag)

    resp3 = rls.serve_filtered(make_request({"accept-encoding": "gzip"}), db, dash, 0, s_hou)
    check("another viewer gets another ETag", resp3.headers["etag"] != etag)
    check("another viewer gets their own rows",
          set(opc1.decode(gzip.decompress(resp3.body))[1]["territory"]) == {NORTH, SOUTH})

    r304 = rls.serve_filtered(make_request({"accept-encoding": "gzip", "if-none-match": etag}),
                              db, dash, 0, s_tmr)
    check("304 on matching ETag", r304.status_code == 304, r304.status_code)
    r200 = rls.serve_filtered(make_request({"accept-encoding": "gzip",
                                            "if-none-match": '"foreign-etag"'}), db, dash, 0, s_tmr)
    check("foreign If-None-Match -> 200", r200.status_code == 200, r200.status_code)
    check("viewer hou1's ETag does not give 304 to viewer tmr1",
          rls.serve_filtered(make_request({"accept-encoding": "gzip",
                                           "if-none-match": resp3.headers["etag"]}),
                             db, dash, 0, s_tmr).status_code == 200)

    ident = rls.serve_filtered(make_request({"accept-encoding": "identity"}), db, dash, 0, s_tmr)
    check("a client without gzip gets decompressed bytes",
          ident.headers.get("content-encoding") == "identity"
          and opc1.decode(ident.body)[1]["territory"] == [NORTH, NORTH],
          ident.headers.get("content-encoding"))

    fp = storage.slot_fp(storage.slot_file_path(dash.id, 1, 0).stat())
    imm = rls.serve_filtered(make_request({"accept-encoding": "gzip"},
                                          query=f"v={fp}".encode()), db, dash, 0, s_tmr)
    check("?v= matched -> still no-cache (shared URL, shared browser)",
          imm.headers["cache-control"] == "private, no-cache",
          imm.headers.get("cache-control"))

    pt_resp = rls.serve_filtered(make_request({"accept-encoding": "identity"}), db, dash, 4, s_tmr)
    check("passthrough slot served to the viewer", pt_resp.status_code == 200
          and b"console.log(1)" in pt_resp.body, pt_resp.status_code)

    # A policy change is visible WITHOUT cache.clear(): its fingerprint is part of both the
    # cache key and the ETag (otherwise with OP_WORKERS > 1 the workers that did not serve
    # PUT /rls would keep serving bytes under the old policy).
    before_etag = rls.serve_filtered(make_request(), db, dash, 0, s_tmr).headers["etag"]
    old_policy_json = dash.rls_json
    dash.rls_json = json.dumps({**POLICY, "slots": [
        {"index": 0, "column": "customer_number"},
        *[s for s in POLICY["slots"] if s["index"] != 0]]}, ensure_ascii=False)
    after = rls.serve_filtered(make_request(), db, dash, 0, s_tmr)
    check("policy change: different ETag without clear()", after.headers["etag"] != before_etag,
          (before_etag, after.headers["etag"]))
    check("policy change: bytes re-filtered (0 rows)",
          opc1.decode(after.body)[0]["rows"] == 0, opc1.decode(after.body)[0]["rows"])
    dash.rls_json = old_policy_json

    try:
        rls.serve_filtered(make_request(), db, dash, 9, s_tmr)
        check("uncovered slot -> 403", False, "no exception was raised")
    except rls.RlsError as e:
        check("uncovered slot -> 403", e.status == 403 and "not covered" in e.detail, e.detail)

    db.close()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
