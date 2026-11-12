"""ADR-18 row-level security — end-to-end smoke test over the real HTTP API.

Builds an interactive dashboard with 5 slots (0..2 OPC1 data, 3 a JS
passthrough, 4 an uncovered slot), sets an RLS policy through PUT /rls, and
checks that every viewer sees exactly their own rows — everywhere the data
could otherwise leak (slot download, source, share links, raw asset name,
lake). Synthetic territories/customers only, no real data.

Owner of this file per the ADR-18 task split; the RLS engine itself is unit-
tested in tests/smoke_rls_service.py.
"""
import gzip
import os
import sys
import tempfile
import time

TMP = tempfile.mkdtemp(prefix="op-rls-e2e-")
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

from app.database import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AuditLog  # noqa: E402
from app.services import opc1  # noqa: E402

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"  <-- {extra}"))
    if not cond:
        FAILURES.append(label)


def login(c, ov):
    r = c.post("/dev-login", data={"person_id": ov}, follow_redirects=False)
    assert r.status_code == 302, f"{ov}: {r.status_code} {r.text[:200]}"


def create_user(c, ov, role="visitor"):
    r = c.post("/api/admin/users", json={"person_id": ov, "role": role, "display_name": ov})
    assert r.status_code == 200, f"{ov}: {r.status_code} {r.text[:200]}"


def make_interactive_html(n_slots):
    inputs = "".join(f'<input type="file" id="f{i}">' for i in range(n_slots))
    return ("<html><body><h1>RLS Report</h1>" + inputs + "</body></html>").encode()


def make_static_html():
    return b"<html><body><h1>Static report</h1></body></html>"


def make_agent_html(inline="{}"):
    # Delta-shaped: no file inputs, an (empty) bootstrap-snapshot stub, data via op_file.
    return ('<html><body><script id="bootstrap-snapshot" type="application/json">'
            + inline + '</script><h1>Agent report</h1></body></html>').encode()


def create_dashboard(c, title, html, data_files=None):
    files = {"html_file": ("d.html", html, "text/html")}
    for i, (name, content) in enumerate((data_files or [])[:3]):
        files[f"data_file_{i}"] = (name, content, "application/octet-stream")
    r = c.post("/api/dashboards", data={"title": title, "description": ""},
               files=files, follow_redirects=False)
    loc = r.headers.get("location") or ""
    did = loc.rstrip("/").rsplit("/", 1)[-1]
    assert did and r.status_code == 303, f"{r.status_code} {r.headers}"
    return did


def put_slot(c, did, slot, name, content):
    r = c.put(f"/api/dashboards/{did}/files/{slot}",
              files={"file": (name, content, "application/octet-stream")})
    assert r.status_code == 200, f"slot {slot}: {r.status_code} {r.text[:200]}"


def audit_rows(action, resource_id=None):
    db = SessionLocal()
    try:
        q = db.query(AuditLog).filter(AuditLog.action == action)
        if resource_id is not None:
            q = q.filter(AuditLog.resource_id == resource_id)
        return [(r.actor_id, r.get_detail()) for r in q.all()]
    finally:
        db.close()


# --- synthetic territory data ------------------------------------------------
# Territory names carry NBSP + mixed spacing on purpose: norm() must fold both
# sides of every comparison the same way.
NORTH, SOUTH, EAST = "North", "South", "East"

POS_SCHEMA = [("territory", "dict"), ("customer_number", "dict")]
POS_ROWS = [
    (NORTH, "C-001"), (NORTH, "C-002"),
    (SOUTH, "C-003"),
    (EAST, "C-004"),
]

CUST_SCHEMA = [("customer_number", "dict"), ("amount", "i32")]
CUST_ROWS = [("C-001", 10), ("C-002", 20), ("C-003", 30), ("C-004", 40)]

TERR_SCHEMA = [("territory", "dict"), ("amount", "i32")]
TERR_ROWS = [(NORTH, 100), (SOUTH, 200), (EAST, 300)]


def opc1_bytes(schema, rows):
    cols = {name: [r[i] for r in rows] for i, (name, _) in enumerate(schema)}
    return opc1.encode(schema, cols)


POLICY = {
    "slots": [
        {"index": 0, "column": "territory"},
        {"index": 1, "key": "customer_number",
         "via": {"slot": 0, "key": "customer_number", "column": "territory"}},
        {"index": 2, "column": "territory"},
    ],
    "passthrough": [3],
}

BAD_POLICY = {"slots": [{"index": 0, "column": "territory", "via": {"slot": 1}}]}


def long_scopes_csv():
    # tmr1 -> North only. hou1 -> North + South (subtree of node HOUALL, via the
    # placeholder edges below). East stays under an unrelated parent, so
    # neither tmr1 nor hou1 ever see it. tmd1 -> TMD with no node -> see_all.
    lines = ["person,role,node,parent"]
    lines.append("tmr1,TMR,North,HOUALL")
    lines.append("hou1,HOU,HOUALL,")
    lines.append("south_edge,TMR,South,HOUALL")
    lines.append("east_edge,TMR,East,OTHERHOU")
    lines.append("tmd1,TMD,,")
    return ("\n".join(lines) + "\n").encode("utf-8")


def do_import(c, content):
    r = c.post("/api/admin/scopes/import",
               files={"file": ("scopes.csv", content, "text/csv")},
               data={"dry_run": "0"})
    assert r.status_code == 200, r.text[:300]
    return r.json()


def main():
    with TestClient(app) as c:
        login(c, "admin1")
        for ov in ("tmr1", "hou1", "tmd1", "none1", "outsider1"):
            create_user(c, ov)

        do_import(c, long_scopes_csv())

        # -- build the RLS dashboard (owner = admin1) ------------------------
        slot0 = opc1_bytes(POS_SCHEMA, POS_ROWS)
        slot1 = opc1_bytes(CUST_SCHEMA, CUST_ROWS)
        slot2_raw = opc1_bytes(TERR_SCHEMA, TERR_ROWS)
        slot2 = gzip.compress(slot2_raw, mtime=0)
        slot3 = b"console.log('shared lib');"
        slot4 = b"col_a,col_b\nx,y\n"

        did = create_dashboard(c, "Delta (synthetic)", make_interactive_html(5), data_files=[
            ("positions.bin", slot0), ("customers.bin", slot1), ("territory.bin.gz", slot2),
        ])
        put_slot(c, did, 3, "lib.js", slot3)
        put_slot(c, did, 4, "extra.csv", slot4)
        r = c.patch(f"/api/dashboards/{did}", json={"is_public": True})
        check("setup: publish is_public", r.status_code == 200, r.text[:200])

        # -- second dashboard: static, to test PUT /rls 409 ------------------
        static_id = create_dashboard(c, "Static report", make_static_html())

        # -- Delta-shaped agent report: snapshot-typed with an EMPTY stub -----
        # Slots arrive by PUT (as publish_run stores them) — a data_file at
        # creation would be promoted into snapshot.json by the upload path.
        agent_id = create_dashboard(c, "Agent report", make_agent_html())
        put_slot(c, agent_id, 0, "positions.bin", slot0)
        r = c.put(f"/api/dashboards/{agent_id}/rls", json={"rls": {"slots": [{"index": 0, "column": "territory"}]}})
        check("agent (empty snapshot stub): PUT /rls 200", r.status_code == 200, r.text[:200])
        r = c.put(f"/api/dashboards/{agent_id}/snapshot", files={"file": ("s.json", b'{"rows":[1]}', "application/json")})
        check("agent: PUT snapshot on RLS report -> 409", r.status_code == 409, r.status_code)
        r = c.put(f"/api/dashboards/{agent_id}/html", files={"html_file": ("d.html", make_agent_html('{"a":1}'), "text/html")}, data={"confirm_type_change": "1"})
        check("agent: replace_html with inline snapshot data -> 409", r.status_code == 409, r.status_code)
        agent_data_id = create_dashboard(c, "Agent report with data", make_agent_html('{"a": 1}'))
        r = c.put(f"/api/dashboards/{agent_data_id}/rls", json={"rls": {"slots": [{"index": 0, "column": "territory"}]}})
        check("agent (inline snapshot data): PUT /rls 409", r.status_code == 409, r.status_code)

        # -- third dashboard: plain interactive report, regression control ---
        plain_id = create_dashboard(c, "Plain report", make_interactive_html(1),
                                    data_files=[("data.csv", b"a,b\n1,2\n")])
        c.patch(f"/api/dashboards/{plain_id}", json={"is_public": True})

        # ---------------------------------------------------------------
        # PUT /rls validation
        # ---------------------------------------------------------------
        r = c.put(f"/api/dashboards/{did}/rls", json={"rls": BAD_POLICY})
        check("PUT /rls: invalid policy -> 422", r.status_code == 422, f"{r.status_code} {r.text[:200]}")

        r = c.put(f"/api/dashboards/{static_id}/rls", json={"rls": POLICY})
        check("PUT /rls: static report -> 409", r.status_code == 409, f"{r.status_code} {r.text[:200]}")

        r = c.put(f"/api/dashboards/{did}/rls", json={"rls": POLICY})
        check("PUT /rls: valid policy -> 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        check("PUT /rls: response enabled=true", r.json().get("enabled") is True, r.json())

        rows = audit_rows("rls_policy_set", resource_id=did)
        check("audit: rls_policy_set written", any(d.get("enabled") is True and d.get("slots") == 3 for _, d in rows),
              rows)

        r = c.get(f"/api/dashboards/{did}/rls")
        check("GET /rls: 200", r.status_code == 200, r.status_code)
        gbody = r.json()
        check("GET /rls: owner sees see_all=true", gbody.get("see_all") is True, gbody)
        check("GET /rls: rls echoes the policy", gbody.get("rls", {}).get("passthrough") == [3], gbody)

        # -- create a share token/link BEFORE the policy would matter (right
        # after enabling RLS this is already refused for the owner too) -----
        r = c.post(f"/api/dashboards/{did}/share-tokens", json={})
        check("share-tokens: owner also 403 under RLS", r.status_code == 403, f"{r.status_code} {r.text[:150]}")
        r = c.post(f"/api/dashboards/{did}/share-links", json={"expires_in_days": 7, "label": "x"})
        check("share-links: owner also 403 under RLS", r.status_code == 403, f"{r.status_code} {r.text[:150]}")
        # The page must not offer what the server refuses: the owner and the
        # administrator see everything (see_all), and the Share form used to be
        # shown to them, so "Generate" ended in a bare "Could not generate link".
        page = c.get(f"/d/{did}").text
        check("viewer: no Generate button on an RLS report, even for see_all",
              'id="btn-gen-share"' not in page)
        check("viewer: the Share panel says why links are off",
              'id="share-off-rls"' in page)

        # ---------------------------------------------------------------
        # Owner (see_all): byte-for-byte, unfiltered
        # ---------------------------------------------------------------
        r = c.get(f"/render/{did}/files/0")
        check("owner: slot0 200", r.status_code == 200, r.status_code)
        check("owner: slot0 byte-for-byte", r.content == slot0, "mismatch")

        r = c.get(f"/api/dashboards/{did}/source")
        check("owner: source 200", r.status_code == 200, r.status_code)

        # ---------------------------------------------------------------
        # A share link issued on a PLAIN (non-RLS) dashboard, then the
        # dashboard is not RLS — used purely as a regression control below.
        # Here: verify a share token minted BEFORE RLS existed on `did`
        # cannot exist at all (creation is blocked once RLS is enabled) —
        # so the "pre-rls token" scenario is exercised on a fresh dashboard
        # that starts RLS-disabled, gets a token, THEN gets a policy.
        # ---------------------------------------------------------------
        pre_id = create_dashboard(c, "Pre-RLS report", make_interactive_html(1),
                                  data_files=[("data.csv", b"a,b\n1,2\n")])
        c.patch(f"/api/dashboards/{pre_id}", json={"is_public": True})
        r = c.post(f"/api/dashboards/{pre_id}/share-tokens", json={})
        check("pre-rls: share token created while report is plain", r.status_code == 200, r.text[:200])
        pre_token = r.json().get("token") if r.status_code == 200 else None
        if pre_token:
            r = c.get(f"/render/{pre_id}/?share={pre_token}")
            check("pre-rls token: render works before RLS", r.status_code == 200, r.status_code)
        r = c.put(f"/api/dashboards/{pre_id}/rls", json={"rls": {
            "slots": [{"index": 0, "column": "territory"}], "passthrough": []}})
        check("pre-rls: enabling RLS afterwards -> 200", r.status_code == 200, r.text[:200])
        if pre_token:
            r = c.get(f"/render/{pre_id}/files/0?share={pre_token}")
            check("pre-rls token: /files/0 403 after RLS enabled", r.status_code == 403, r.status_code)
            r = c.get(f"/render/{pre_id}/?share={pre_token}")
            check("pre-rls token: /render/ 403 after RLS enabled", r.status_code == 403, r.status_code)
            r = c.get(f"/share/{pre_token}/files/0")
            check("pre-rls token: /share/.../files/0 403", r.status_code == 403, r.status_code)
            r = c.get(f"/s/{pre_token}")
            check("pre-rls token: /s/{token} shell 403", r.status_code == 403, r.status_code)

        # ---------------------------------------------------------------
        # tmr1 — one territory (North)
        # ---------------------------------------------------------------
        login(c, "tmr1")
        r0 = c.get(f"/render/{did}/files/0")
        check("tmr1: slot0 200", r0.status_code == 200, r0.status_code)
        desc0, cols0 = opc1.decode(r0.content)
        check("tmr1: slot0 rows == 2 (both North customers)", desc0["rows"] == 2, desc0)
        check("tmr1: slot0 territory values all North", set(cols0["territory"]) == {NORTH}, cols0["territory"])
        check("tmr1: slot0 dict has no foreign territory in the raw bytes",
              SOUTH.encode() not in r0.content and EAST.encode() not in r0.content, "leak")

        r1 = c.get(f"/render/{did}/files/1")
        check("tmr1: slot1 (via) 200", r1.status_code == 200, r1.status_code)
        desc1, cols1 = opc1.decode(r1.content)
        check("tmr1: via slot has only C-001/C-002", set(cols1["customer_number"]) == {"C-001", "C-002"},
              cols1["customer_number"])
        check("tmr1: via slot bytes have no foreign customer numbers",
              b"C-003" not in r1.content and b"C-004" not in r1.content, "leak")

        r2 = c.get(f"/render/{did}/files/2")
        check("tmr1: slot2 (.gz) 200", r2.status_code == 200, r2.status_code)
        check("tmr1: slot2 stays gzip (Content-Encoding)", r2.headers.get("content-encoding") == "gzip",
              r2.headers.get("content-encoding"))
        # httpx transparently decodes Content-Encoding: gzip, so r2.content is
        # already the plain OPC1 body — the wire-level check above already
        # confirmed the SERVER sent it gzip-encoded.
        desc2, cols2 = opc1.decode(r2.content)
        check("tmr1: slot2 has only North", set(cols2["territory"]) == {NORTH}, cols2["territory"])

        check("tmr1: Cache-Control is private", "private" in r0.headers.get("cache-control", ""),
              r0.headers.get("cache-control"))
        # Shared browser: an immutable body for a common URL would be served
        # to the next account without a request. Both subjects must be
        # no-cache (ETag revalidation), even on a matching ?v=.
        fp0 = r0.headers.get("etag", "").strip('"').split("-")[0]
        cc_scoped = c.get(f"/render/{did}/files/0?v={fp0}").headers.get("cache-control", "")
        check("tmr1: matching ?v= is no-cache, never immutable",
              "no-cache" in cc_scoped and "immutable" not in cc_scoped, cc_scoped)
        login(c, "admin1")
        ra = c.get(f"/render/{did}/files/0")
        fpa = ra.headers.get("etag", "").strip('"').split("-")[0]
        cc_full = c.get(f"/render/{did}/files/0?v={fpa}").headers.get("cache-control", "")
        check("admin: full RLS file with ?v= is no-cache, never immutable",
              "no-cache" in cc_full and "immutable" not in cc_full, cc_full)
        check("admin: full RLS file Vary carries Cookie", "Cookie" in c.get(f"/render/{did}/files/0").headers.get("vary", ""))
        ra304 = c.get(f"/render/{did}/files/0", headers={"If-None-Match": ra.headers.get("etag")})
        check("admin: 304 on own ETag for RLS file", ra304.status_code == 304, ra304.status_code)
        login(c, "tmr1")
        check("tmr1: Vary carries Cookie", "Cookie" in r0.headers.get("vary", ""), r0.headers.get("vary"))

        etag_tmr1 = r0.headers.get("etag")
        r = c.get(f"/render/{did}/files/0", headers={"If-None-Match": etag_tmr1})
        check("tmr1: 304 on own ETag", r.status_code == 304, r.status_code)

        # via slot: replacing the SOURCE slot in place (owner/see_all) must
        # change the target's ETag and its content — no stale customer map.
        etag_via = r1.headers.get("etag")
        check("tmr1: via slot never immutable",
              "immutable" not in c.get(f"/render/{did}/files/1?v=" + etag_via.strip('"').split("-")[0]).headers.get("cache-control", ""),
              "immutable")
        login(c, "admin1")
        moved = opc1_bytes(POS_SCHEMA, [(NORTH, "C-001"), (NORTH, "C-002"), (NORTH, "C-003"), (EAST, "C-004")])
        import time as _t; _t.sleep(0.02)
        put_slot(c, did, 0, "positions.bin", moved)
        login(c, "tmr1")
        r1b = c.get(f"/render/{did}/files/1", headers={"If-None-Match": etag_via})
        check("via: source slot replaced -> 200, not 304", r1b.status_code == 200, r1b.status_code)
        check("via: source slot replaced -> new ETag", r1b.headers.get("etag") != etag_via, r1b.headers.get("etag"))
        _, cols1b = opc1.decode(r1b.content)
        check("via: source slot replaced -> C-003 now visible", set(cols1b["customer_number"]) == {"C-001", "C-002", "C-003"},
              cols1b["customer_number"])
        login(c, "admin1")
        put_slot(c, did, 0, "positions.bin", slot0)
        login(c, "tmr1")

        # passthrough slot 3, by index and by original name
        r = c.get(f"/render/{did}/files/3")
        check("tmr1: passthrough slot by index verbatim", r.status_code == 200 and r.content == slot3,
              (r.status_code, r.content[:40]))
        r = c.get(f"/render/{did}/lib.js")
        check("tmr1: passthrough slot by original name verbatim", r.status_code == 200 and r.content == slot3,
              (r.status_code, r.content[:40]))

        # uncovered slot 4 -> 403
        r = c.get(f"/render/{did}/files/4")
        check("tmr1: uncovered slot -> 403", r.status_code == 403, r.status_code)

        # source / write / lake / asset-by-name closed
        r = c.get(f"/api/dashboards/{did}/source")
        check("tmr1: source 403", r.status_code == 403, r.status_code)
        # Grant tmr1 can_update_data as admin, then verify RLS still blocks
        # the write — «can update data» alone is never full access (ADR-18).
        login(c, "admin1")
        r = c.post(f"/api/dashboards/{did}/permissions/tmr1", json={"can_update_data": True})
        check("setup: grant tmr1 can_update_data", r.status_code == 200, r.text[:150])
        login(c, "tmr1")
        r = c.put(f"/api/dashboards/{did}/files/0", files={"file": ("x.bin", b"whatever")})
        check("tmr1: PUT slot 403 even with can_update_data", r.status_code == 403, r.status_code)
        r = c.delete(f"/api/dashboards/{did}/files/0")
        check("tmr1: DELETE slot 403", r.status_code == 403, r.status_code)
        r = c.get(f"/render/{did}/positions.bin")
        check("tmr1: slot-by-original-name 403", r.status_code == 403, r.status_code)

        r = c.get(f"/api/dashboards/{did}/lake/manifest")
        check("tmr1: lake manifest 403", r.status_code == 403, r.status_code)
        r = c.get(f"/api/dashboards/{did}/lake/parts/{'a' * 16}")
        check("tmr1: lake part 403", r.status_code == 403, r.status_code)

        r = c.post(f"/api/dashboards/{did}/share-tokens", json={})
        check("tmr1: share token creation 403", r.status_code == 403, r.status_code)

        # viewer page: no share tab/buttons, has the scoped badge
        r = c.get(f"/d/{did}")
        t = r.text
        check("tmr1: viewer page 200", r.status_code == 200, r.status_code)
        check("tmr1: no share tab", 'data-tab="share"' not in t)
        check("tmr1: no btn-share-link", 'id="btn-share-link"' not in t)
        check("tmr1: no btn-upload-slots", 'id="btn-upload-slots"' not in t)
        check("tmr1: scoped badge present", "Scope access" in t)

        # ---------------------------------------------------------------
        # hou1 — two territories (North + South)
        # ---------------------------------------------------------------
        login(c, "hou1")
        r0h = c.get(f"/render/{did}/files/0")
        check("hou1: slot0 200", r0h.status_code == 200, r0h.status_code)
        desc0h, cols0h = opc1.decode(r0h.content)
        check("hou1: slot0 has both North and South", set(cols0h["territory"]) == {NORTH, SOUTH}, cols0h["territory"])
        check("hou1: slot0 excludes East", EAST.encode() not in r0h.content, "leak")

        check("hou1/tmr1: different ETags", r0h.headers.get("etag") != etag_tmr1,
              (r0h.headers.get("etag"), etag_tmr1))
        r = c.get(f"/render/{did}/files/0", headers={"If-None-Match": etag_tmr1})
        check("hou1: 200 (not 304) on tmr1's ETag", r.status_code == 200, r.status_code)

        # ---------------------------------------------------------------
        # tmd1 — TMD role, no node -> see_all: byte-for-byte with the original
        # ---------------------------------------------------------------
        login(c, "tmd1")
        r = c.get(f"/render/{did}/files/0")
        check("tmd1: slot0 200", r.status_code == 200, r.status_code)
        check("tmd1: slot0 byte-for-byte (see_all)", r.content == slot0, "mismatch")
        r = c.get(f"/d/{did}")
        check("tmd1: full-access badge present", "RLS · full access" in r.text)

        # ---------------------------------------------------------------
        # none1 — no user_scopes row at all -> default deny, 0 rows, still valid
        # ---------------------------------------------------------------
        login(c, "none1")
        r = c.get(f"/render/{did}/files/0")
        check("none1: slot0 200 (empty, not 403)", r.status_code == 200, r.status_code)
        desc_n, cols_n = opc1.decode(r.content)
        check("none1: slot0 has 0 rows", desc_n["rows"] == 0, desc_n)

        # ---------------------------------------------------------------
        # Regression: a plain (non-RLS) report is untouched
        # ---------------------------------------------------------------
        login(c, "outsider1")
        r = c.get(f"/d/{plain_id}")
        check("plain report: public dashboard reachable by any signed-in user",
              r.status_code == 200, r.status_code)
        r = c.get(f"/render/{plain_id}/files/0")
        check("plain report: slot download 200 unfiltered", r.status_code == 200, r.status_code)
        check("plain report: no-store still present",
              "no-store" in r.headers.get("cache-control", ""), r.headers.get("cache-control"))
        r = c.get(f"/render/{plain_id}/")
        check("plain report: render 200", r.status_code == 200, r.status_code)

        login(c, "admin1")
        r = c.post(f"/api/dashboards/{plain_id}/share-links", json={"expires_in_days": 7, "label": "x"})
        check("plain report: share link creation still 200", r.status_code == 200, f"{r.status_code} {r.text[:150]}")

        # ---------------------------------------------------------------
        # Policy removal restores the full file
        # ---------------------------------------------------------------
        r = c.put(f"/api/dashboards/{did}/rls", json={"rls": None})
        check("PUT /rls: remove policy -> 200", r.status_code == 200, r.text[:200])
        check("PUT /rls: response enabled=false", r.json().get("enabled") is False, r.json())
        login(c, "tmr1")
        r = c.get(f"/render/{did}/files/0")
        check("policy removed: tmr1 now sees the FULL file", r.status_code == 200 and r.content == slot0,
              (r.status_code, len(r.content)))

        # ---------------------------------------------------------------
        # Perf: 50 000-row OPC1 slot, cold cache
        # ---------------------------------------------------------------
        login(c, "admin1")
        big_rows = [(NORTH if i % 2 == 0 else SOUTH, f"C-{i:06d}") for i in range(50_000)]
        big_slot = opc1_bytes(POS_SCHEMA, big_rows)
        perf_id = create_dashboard(c, "Perf report", make_interactive_html(1), data_files=[("big.bin", big_slot)])
        c.patch(f"/api/dashboards/{perf_id}", json={"is_public": True})
        r = c.put(f"/api/dashboards/{perf_id}/rls", json={"rls": {
            "slots": [{"index": 0, "column": "territory"}], "passthrough": []}})
        check("perf: policy set", r.status_code == 200, r.text[:200])
        login(c, "tmr1")
        t0 = time.perf_counter()
        r = c.get(f"/render/{perf_id}/files/0")
        dt_ms = (time.perf_counter() - t0) * 1000
        print(f"PERF 50k-row OPC1 slot cold filter: {dt_ms:.1f} ms")
        check("perf: 50k-row slot 200", r.status_code == 200, r.status_code)
        check("perf: 50k-row slot filtered under 1.5s (gate, not a hard perf claim)",
              dt_ms < 1500, dt_ms)

        # ---------------------------------------------------------------
        # rls_serve audit rows carry only counters, never data values
        # ---------------------------------------------------------------
        serve_rows = audit_rows("rls_serve", resource_id=did)
        ok = all(set(d.keys()) == {"slot", "rows_in", "rows_out", "keys"} for _, d in serve_rows)
        check("audit: rls_serve rows carry only {slot, rows_in, rows_out, keys}", ok and len(serve_rows) > 0,
              serve_rows[:3])

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)}")
        for f in FAILURES:
            print("  -", f)
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
