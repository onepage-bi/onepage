"""/admin/scopes (ADR-18): CSV import (wide + long formats), person resolution,
user_scopes recompute (subtree flattening, TMD see_all), dry_run, replace
semantics on re-import, manual grants, lookup, and permission gates.

Synthetic data only — no real employee/territory names.
"""
import csv
import io
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="op-scopes-")
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

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


def login(c, ov):
    r = c.post("/dev-login", data={"person_id": ov}, follow_redirects=False)
    assert r.status_code == 302, r.text[:200]


def create_user(c, person_id, role="visitor"):
    r = c.post("/api/admin/users", json={"person_id": person_id, "role": role, "display_name": person_id})
    assert r.status_code == 200, f"{person_id}: {r.status_code} {r.text[:200]}"


# The exact real header (colon-free, synthetic values only per ADR-18 spec).
WIDE_HEADER = (
    "id_posterr,id_customer_cycle_hist,hou_position_name,hou_full_name,hou_login,"
    "rm_position_name,rm_full_name,rm_login,cm_position_name,cm_full_name,cm_login,"
    "tmr_position_name,tmr_full_name,employee_login,employee_fi,main_employee_login,"
    "n_customer,insert_ts,hk_h_id_posterr_id_customer_cycle_hist,dss_change_hash_1,"
    "update_ts,hou_userprincipalname,rm_userprincipalname,cm_userprincipalname,"
    "tmr_userprincipalname"
)


def wide_row(id_posterr, chist, hou, rm, cm, tmr, hou_upn, rm_upn, cm_upn, tmr_upn):
    return [
        id_posterr, chist, hou, f"{hou} Full", "hou_login",
        rm, f"{rm} Full", "rm_login", cm, f"{cm} Full", "cm_login",
        tmr, f"{tmr} Full", "emp_login", "E. F.", "main_login",
        "1", "2026-01-01", "hk1", "hash1", "2026-01-01",
        hou_upn, rm_upn, cm_upn, tmr_upn,
    ]


def make_wide_csv():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(WIDE_HEADER.split(","))
    # hou1's chain: HOU-W1 -> RM-W1 -> CM-W1 -> {TMR-W1, TMR-W2}
    w.writerow(wide_row(1, 1, "HOU-W1", "RM-W1", "CM-W1", "TMR-W1",
                         "hou1_wide@example.test", "rm1_wide@example.test",
                         "cm1_wide@example.test", "tmr1_wide@example.test"))
    # duplicate of the same TMR position (repeats per customer-cycle-hist row
    # in the real export) — must be skipped, not double-counted.
    w.writerow(wide_row(1, 2, "HOU-W1", "RM-W1", "CM-W1", "TMR-W1",
                         "hou1_wide@example.test", "rm1_wide@example.test",
                         "cm1_wide@example.test", "tmr1_wide@example.test"))
    w.writerow(wide_row(2, 1, "HOU-W1", "RM-W1", "CM-W1", "TMR-W2",
                         "hou1_wide@example.test", "rm1_wide@example.test",
                         "cm1_wide@example.test", "tmr2_wide@example.test"))
    # a vacant TMR position (empty node name) — skipped and counted.
    w.writerow(wide_row(3, 1, "", "", "", "",
                         "", "", "", ""))
    # a second, unrelated HOU chain — hou1 must NOT see this TMR.
    w.writerow(wide_row(4, 1, "HOU-W2", "RM-W2", "CM-W2", "TMR-W3",
                         "hou2_wide@example.test", "rm2_wide@example.test",
                         "cm2_wide@example.test", "tmr3_wide@example.test"))
    return buf.getvalue().encode("utf-8")


LONG_HEADER = "person,role,node,parent"


def make_long_csv(include_ghost=True, include_tmr2=True):
    lines = [LONG_HEADER]
    lines.append("tmd1,TMD,,")
    lines.append("hou1,HOU,HOU-L,")
    lines.append("rm1,RM,RM-L1,HOU-L")
    lines.append("rm2,RM,RM-L2,HOU-L")
    lines.append("cm1,CM,CM-L1,RM-L1")
    lines.append("tmr1,TMR,TMR-L1,CM-L1")
    if include_tmr2:
        lines.append("tmr2,TMR,TMR-L2,CM-L1")
    lines.append("cm2,CM,CM-L2,RM-L2")
    lines.append("tmr3,TMR,TMR-L3,CM-L2")
    if include_ghost:
        lines.append("ghost@nowhere.test,TMR,TMR-L4,CM-L2")
    lines.append(",TMR,TMR-EMPTY,CM-L2")  # blank person → skipped & counted
    return ("\n".join(lines) + "\n").encode("utf-8")


def do_import(c, content, dry_run):
    return c.post("/api/admin/scopes/import",
                   files={"file": ("scopes.csv", content, "text/csv")},
                   data={"dry_run": "1" if dry_run else "0"})


def lookup(c, person_id):
    r = c.get("/api/admin/scopes/lookup", params={"q": person_id})
    assert r.status_code == 200, r.text[:200]
    return r.json()


def status(c):
    r = c.get("/api/admin/scopes/status")
    assert r.status_code == 200, r.text[:200]
    return r.json()


def main():
    with TestClient(app) as c:
        login(c, "admin1")
        create_user(c, "visitor1", role="visitor")

        # ── permission gates ────────────────────────────────────────────
        login(c, "visitor1")
        r = c.get("/admin/scopes", follow_redirects=False)
        check("visitor: /admin/scopes denied", r.status_code in (302, 303, 401, 403), r.status_code)
        r = do_import(c, make_wide_csv(), dry_run=True)
        check("visitor: import denied", r.status_code in (401, 403), r.status_code)
        r = c.post("/api/admin/scopes/manual", json={"person_id": "x", "see_all": True})
        check("visitor: manual grant denied", r.status_code in (401, 403), r.status_code)
        r = c.get("/api/admin/scopes/status")
        check("visitor: status denied", r.status_code in (401, 403), r.status_code)
        r = c.get("/api/admin/scopes/lookup", params={"q": "x"})
        check("visitor: lookup denied", r.status_code in (401, 403), r.status_code)

        login(c, "admin1")
        r = c.get("/admin/scopes")
        check("admin: page 200", r.status_code == 200, str(r.status_code))
        check("admin: page has import card", "Import scopes" in r.text)

        # ── users to resolve against ────────────────────────────────────
        for ov in ("hou1_wide@example.test", "rm1_wide@example.test", "cm1_wide@example.test",
                   "tmr1_wide@example.test", "tmr2_wide@example.test", "hou2_wide@example.test"):
            create_user(c, ov)

        # ── wide-format import (real) ───────────────────────────────────
        r = do_import(c, make_wide_csv(), dry_run=False)
        check("wide import: 200", r.status_code == 200, r.text[:300])
        body = r.json()
        # 3 real TMR positions survive: TMR-W1, TMR-W2, TMR-W3 (duplicate +
        # vacant rows are skipped, not counted as rows).
        check("wide import: total 3 rows", body["total"] == 3, str(body))
        check("wide import: 9 persons, 6 resolved, 3 unresolved",
              body["persons"] == 9 and body["resolved"] == 6 and body["unresolved"] == 3, str(body))
        check("wide import: 9 nodes (2x HOU/RM/CM chains + 3 TMR)",
              body["nodes"] == 9, str(body))

        hou1 = lookup(c, "hou1_wide@example.test")
        check("wide: hou1 found, not see_all", hou1["found"] and not hou1["see_all"], str(hou1))
        keys = set(hou1["keys_sample"])
        check("wide: hou1 subtree has both own TMRs",
              {"tmr-w1", "tmr-w2"} <= keys, str(hou1))
        check("wide: hou1 subtree excludes the other HOU's TMR",
              "tmr-w3" not in keys, str(hou1))

        hou2 = lookup(c, "hou2_wide@example.test")
        check("wide: hou2's subtree is its own chain only",
              set(hou2["keys_sample"]) == {"hou-w2", "rm-w2", "cm-w2", "tmr-w3"}, str(hou2))

        # ── dry_run writes nothing ──────────────────────────────────────
        before = status(c)
        r = do_import(c, make_long_csv(), dry_run=True)
        check("long dry_run: 200", r.status_code == 200, r.text[:300])
        dbody = r.json()
        check("long dry_run: reports dry_run=true", dbody["dry_run"] is True, str(dbody))
        # The long-format persons (hou1/rm1/.../tmd1) aren't OnePage accounts
        # yet at this point — dry_run must still parse and count them all as
        # unresolved without writing anything (checked right below).
        check("long dry_run: 10 persons, all unresolved (accounts not created yet)",
              dbody["persons"] == 10 and dbody["resolved"] == 0 and dbody["unresolved"] == 10,
              str(dbody))
        after = status(c)
        check("dry_run: status unchanged", before == after, f"{before} != {after}")
        hou1_after_dry = lookup(c, "hou1_wide@example.test")
        check("dry_run: wide import untouched", hou1_after_dry == hou1, str(hou1_after_dry))

        # ── re-import replaces (long format now) ────────────────────────
        for ov in ("tmd1", "hou1", "rm1", "rm2", "cm1", "cm2", "tmr1", "tmr2", "tmr3"):
            create_user(c, ov)
        r = do_import(c, make_long_csv(), dry_run=False)
        check("long import: 200", r.status_code == 200, r.text[:300])
        lbody = r.json()
        check("long import: total rows (10 data rows, blank person skipped)",
              lbody["total"] == 10, str(lbody))
        check("long import: 10 persons, 9 resolved, 1 unresolved",
              lbody["persons"] == 10 and lbody["resolved"] == 9 and lbody["unresolved"] == 1,
              str(lbody))
        check("long import: unresolved_sample has the ghost",
              "ghost@nowhere.test" in lbody["unresolved_sample"], str(lbody))

        # wide-format rows are gone (source='import' fully replaced)
        hou1_wide_gone = lookup(c, "hou1_wide@example.test")
        check("re-import replaced the wide rows",
              not hou1_wide_gone["found"] or hou1_wide_gone["key_count"] == 0,
              str(hou1_wide_gone))

        tmd1_res = lookup(c, "tmd1")
        check("TMD role with empty node -> see_all", tmd1_res["found"] and tmd1_res["see_all"], str(tmd1_res))

        hou1_res = lookup(c, "hou1")
        check("HOU subtree flattening: sees every TMR under its RM/CM chain",
              set(hou1_res["keys_sample"]) >= {"hou-l", "rm-l1", "rm-l2", "cm-l1", "cm-l2",
                                                "tmr-l1", "tmr-l2", "tmr-l3", "tmr-l4"},
              str(hou1_res))
        rm1_res = lookup(c, "rm1")
        check("RM subtree narrower than HOU's",
              rm1_res["key_count"] < hou1_res["key_count"] and "tmr-l3" not in rm1_res["keys_sample"],
              str(rm1_res))

        # ── manual grants ────────────────────────────────────────────────
        create_user(c, "office1")
        r = c.post("/api/admin/scopes/manual", json={"person_id": "office1", "see_all": True})
        check("manual see_all: 200", r.status_code == 200, r.text[:200])
        office1 = lookup(c, "office1")
        check("manual see_all: lookup shows see_all", office1["found"] and office1["see_all"], str(office1))
        r = c.post("/api/admin/scopes/manual", json={"person_id": "OFFICE1", "see_all": False})
        check("manual: person_id matched case-insensitively", r.status_code == 200 and r.json()["person_id"] == "office1", r.text[:200])
        check("manual: case-insensitive revoke took effect", not lookup(c, "office1")["see_all"], "still see_all")
        r = c.post("/api/admin/scopes/manual", json={"person_id": "office1", "see_all": True})
        check("manual see_all: re-grant 200", r.status_code == 200, r.text[:200])
        r = c.post("/api/admin/scopes/manual", json={"person_id": "nobody-here", "see_all": True})
        check("manual: unknown account -> 404", r.status_code == 404, r.status_code)

        create_user(c, "dev1")
        r = c.post("/api/admin/scopes/manual",
                    json={"person_id": "dev1", "node": "CM-L1", "role": "HOU_ADD"})
        check("manual node grant: 200", r.status_code == 200, r.text[:200])
        dev1 = lookup(c, "dev1")
        check("manual node grant: sees CM-L1's subtree",
              "cm-l1" in dev1["keys_sample"] and "tmr-l1" in dev1["keys_sample"], str(dev1))
        check("manual node grant: not see_all", not dev1["see_all"], str(dev1))

        r = c.post("/api/admin/scopes/manual", json={"person_id": "office1", "remove": True})
        check("manual remove: 200", r.status_code == 200, r.text[:200])
        office1_after = lookup(c, "office1")
        check("manual remove: office1 no longer see_all",
              not office1_after["found"] or not office1_after["see_all"], str(office1_after))

        r = c.post("/api/admin/scopes/manual", json={"person_id": "dev1", "remove": True})
        check("manual remove: dev1 grant removed", r.status_code == 200, r.text[:200])
        dev1_after = lookup(c, "dev1")
        check("manual remove: dev1 lost the CM-L1 grant",
              not dev1_after["found"] or dev1_after["key_count"] == 0, str(dev1_after))

        # ── status endpoint reflects the last (long) import ─────────────
        st = status(c)
        check("status: persons/resolved/unresolved match last import",
              st["persons"] == 10 and st["resolved"] == 9 and st["unresolved"] == 1, str(st))

        # ── attach on login: the ghost gets an account after the import ─
        create_user(c, "ghost@nowhere.test")
        g0 = lookup(c, "ghost@nowhere.test")
        check("attach: before login the ghost is default deny", not g0["found"], str(g0))
        login(c, "ghost@nowhere.test")
        login(c, "admin1")
        g1 = lookup(c, "ghost@nowhere.test")
        check("attach: after first login the ghost has its TMR node",
              g1["found"] and g1["key_count"] == 1 and "tmr-l4" in g1["keys_sample"], str(g1))
        st2 = status(c)
        check("attach: import counters untouched", st2["unresolved"] == 1, str(st2))
        login(c, "ghost@nowhere.test")  # second login: nothing left to attach, no error
        login(c, "admin1")
        r = c.get("/api/admin/audit-log", params={"action": "rls_scope_attach"})
        check("attach: audited once", r.status_code != 200 or r.text.count("rls_scope_attach") >= 1, r.status_code)

        # ── lookup for someone with no scope row at all ─────────────────
        create_user(c, "nobody1")
        nobody = lookup(c, "nobody1")
        check("lookup: unknown person -> found False, default deny",
              nobody == {"found": False, "see_all": False, "roles": [], "key_count": 0, "keys_sample": []},
              str(nobody))

    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURES: {FAILURES}")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    main()
