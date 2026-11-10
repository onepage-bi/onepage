"""Smoke test for presentations v1 — CRUD, slides, order, check, show context.

The invariants worth pinning here are the ones a UI cannot be trusted to keep:

  * a presentation is owner-only, and a non-owner gets 404 — not 403, which
    would confirm the id exists;
  * a dashboard slide passes the SAME access rule as opening the report, so a
    presentation is never a side door into someone else's dashboard;
  * a note longer than the column's contract is a 422, not a silent truncation;
  * deleting removes the bytes from disk, not just the row.

Uploads (sniffing, caps, serving) live in tests/smoke_presentation_uploads.py.
"""
import json
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="op-pres-")
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


HTML = b"<html><head><title>d</title></head><body><h1>report</h1></body></html>"
PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)


def login(c, person_id):
    r = c.post("/dev-login", data={"person_id": person_id}, follow_redirects=False)
    assert r.status_code == 302, f"dev-login {person_id}: {r.status_code}"


def make_dashboard(c, title):
    r = c.post("/api/dashboards", data={"title": title, "description": ""},
               files={"html_file": ("d.html", HTML, "text/html")},
               follow_redirects=False)
    assert r.status_code == 303, f"create dashboard: {r.status_code} {r.text[:200]}"
    return r.headers["location"].rsplit("/", 1)[1]


with TestClient(app) as c:
    # A second superuser, so "someone else's private dashboard" is a real case.
    from app.database import SessionLocal
    from app.models import Dashboard, GenerationRun, Presentation, PresentationItem, User
    _db = SessionLocal()
    _db.add(User(person_id="anna", role="superuser"))
    _db.commit()
    _db.close()

    # anna's own private dashboard — never shared with admin1.
    login(c, "anna")
    anna_dash = make_dashboard(c, "anna private")
    r = c.post("/api/presentations", json={"title": "anna deck"})
    check("anna: create presentation -> 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    anna_pres = r.json()["id"]

    login(c, "admin1")
    dash_a = make_dashboard(c, "Report A")
    dash_b = make_dashboard(c, "Report B")

    # ---- CRUD -----------------------------------------------------------
    r = c.post("/api/presentations", json={"title": "Q3 review"})
    check("create -> 201 with id", r.status_code == 201 and r.json().get("id"),
          f"{r.status_code} {r.text[:200]}")
    pid = r.json()["id"]

    r = c.get("/api/presentations")
    mine = r.json()
    check("list returns own presentations only",
          r.status_code == 200 and [p["id"] for p in mine] == [pid],
          f"{r.status_code} {r.text[:200]}")
    check("list carries items_count and updated_at",
          mine[0]["items_count"] == 0 and mine[0]["updated_at"], mine[0])

    r = c.patch(f"/api/presentations/{pid}", json={"title": "Q3 review (final)"})
    check("patch title -> 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    check("patch persisted",
          c.get(f"/api/presentations/{pid}").json()["title"] == "Q3 review (final)")

    # ---- owner-only ------------------------------------------------------
    login(c, "anna")
    for label, resp in (
        ("GET", c.get(f"/api/presentations/{pid}")),
        ("PATCH", c.patch(f"/api/presentations/{pid}", json={"title": "hijack"})),
        ("DELETE", c.delete(f"/api/presentations/{pid}")),
        ("items", c.post(f"/api/presentations/{pid}/items",
                         json={"kind": "dashboard", "dashboard_id": dash_a})),
        ("order", c.put(f"/api/presentations/{pid}/order", json={"ids": []})),
        ("check", c.get(f"/api/presentations/{pid}/check")),
    ):
        check(f"non-owner {label} -> 404 (existence not leaked)", resp.status_code == 404,
              f"{resp.status_code} {resp.text[:150]}")
    check("non-owner list does not show it",
          all(p["id"] != pid for p in c.get("/api/presentations").json()))

    # ---- items -----------------------------------------------------------
    login(c, "admin1")
    r = c.post(f"/api/presentations/{pid}/items",
               json={"kind": "dashboard", "dashboard_id": dash_a, "note": "opening"})
    check("add dashboard slide -> 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    item_a = r.json()["id"]
    check("slide position starts at 0", r.json()["position"] == 0, r.json())

    r = c.post(f"/api/presentations/{pid}/items",
               json={"kind": "dashboard", "dashboard_id": dash_b,
                     "start_section": "kpi"})
    check("second slide -> position 1", r.status_code == 201 and r.json()["position"] == 1,
          f"{r.status_code} {r.text[:200]}")
    item_b = r.json()["id"]

    body = c.get(f"/api/presentations/{pid}").json()
    check("GET joins dashboard_title",
          [i["dashboard_title"] for i in body["items"]] == ["Report A", "Report B"],
          body["items"])
    check("dashboard slide has no file_url",
          all(i["file_url"] is None for i in body["items"]))
    check("note and start_section round-trip",
          body["items"][0]["note"] == "opening" and body["items"][1]["start_section"] == "kpi",
          body["items"])

    # Access rule: anna's private dashboard is not addable by admin... except
    # admin1 IS an administrator, who may view any dashboard. Use a visitor to
    # prove the rule bites.
    _db = SessionLocal()
    _db.add(User(person_id="vera", role="superuser"))
    _db.commit()
    _db.close()
    login(c, "vera")
    r = c.post("/api/presentations", json={"title": "vera deck"})
    vera_pres = r.json()["id"]
    r = c.post(f"/api/presentations/{vera_pres}/items",
               json={"kind": "dashboard", "dashboard_id": anna_dash})
    check("slide onto someone else's private dashboard -> 403/404",
          r.status_code in (403, 404), f"{r.status_code} {r.text[:200]}")
    r = c.post(f"/api/presentations/{vera_pres}/items",
               json={"kind": "dashboard", "dashboard_id": "no-such-dashboard"})
    check("slide onto a missing dashboard -> 404", r.status_code == 404,
          f"{r.status_code} {r.text[:200]}")

    login(c, "admin1")
    long_note = "x" * (2000 + 1)
    r = c.post(f"/api/presentations/{pid}/items",
               json={"kind": "dashboard", "dashboard_id": dash_a, "note": long_note})
    check("2001-char note -> 422", r.status_code == 422, f"{r.status_code} {r.text[:150]}")
    r = c.patch(f"/api/presentations/{pid}/items/{item_a}", json={"note": long_note})
    check("2001-char note on PATCH -> 422", r.status_code == 422, f"{r.status_code} {r.text[:150]}")
    r = c.patch(f"/api/presentations/{pid}/items/{item_a}", json={"note": "x" * 2000})
    check("2000-char note accepted", r.status_code == 200, f"{r.status_code} {r.text[:150]}")

    r = c.patch(f"/api/presentations/{pid}/items/{item_a}",
                json={"note": None, "start_section": "totals"})
    check("PATCH clears note and sets start_section",
          r.status_code == 200 and r.json()["note"] is None
          and r.json()["start_section"] == "totals", f"{r.status_code} {r.text[:200]}")

    # ---- order -----------------------------------------------------------
    r = c.put(f"/api/presentations/{pid}/order",
              json={"ids": [item_b, item_a, "ghost-id"]})
    check("reorder -> 200 (unknown ids ignored)", r.status_code == 200,
          f"{r.status_code} {r.text[:200]}")
    order = [i["id"] for i in c.get(f"/api/presentations/{pid}").json()["items"]]
    check("reorder applied", order == [item_b, item_a], order)

    r = c.put(f"/api/presentations/{pid}/order", json={"ids": []})
    order2 = [i["id"] for i in c.get(f"/api/presentations/{pid}").json()["items"]]
    check("empty order keeps existing relative order", order2 == order, order2)

    # ---- an image slide, so delete has a file to remove ------------------
    r = c.post(f"/api/presentations/{pid}/upload",
               files={"file": ("shot.png", PNG, "image/png")})
    check("png slide uploaded", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    img_item = r.json()["id"]
    _db = SessionLocal()
    rel = _db.query(PresentationItem).filter(PresentationItem.id == img_item).first().file_path
    _db.close()
    disk = os.path.join(f"{TMP}/uploads", rel)
    check("uploaded slide is on disk", os.path.exists(disk), disk)

    # ---- check endpoint --------------------------------------------------
    r = c.get(f"/api/presentations/{pid}/check")
    check("check -> 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    slides = r.json()["slides"]
    check("check reports one row per slide", len(slides) == 3, slides)
    dash_rows = [s for s in slides if s["kind"] == "dashboard"]
    check("dashboard row carries position/title/pub_at",
          all(s.get("title") and s.get("pub_at") and "stale" in s for s in dash_rows),
          dash_rows)
    check("a dashboard with no runs is not stale",
          all(s["stale"] is False for s in dash_rows), dash_rows)
    img_rows = [s for s in slides if s["kind"] == "image"]
    check("image slide is never stale",
          len(img_rows) == 1 and img_rows[0]["stale"] is False, img_rows)

    # A generator-backed dashboard with no observed data is stale by the rule.
    # A REAL generator and version: since foreign_keys=ON (2026-09-06) the
    # dashboard's generator_id and the runs below can no longer point at ids
    # that were never inserted.
    _db = SessionLocal()
    from app.models import Generator, GeneratorVersion
    _gen = Generator(name="pres-smoke-generator")
    _db.add(_gen)
    _db.flush()
    _gver = GeneratorVersion(generator_id=_gen.id, version_number=1, source_type="bundle",
                             bundle_path="b", manifest_json="{}", checksum_sha256="x")
    _db.add(_gver)
    _db.flush()
    gen_id, gver_id = _gen.id, _gver.id
    _d = _db.query(Dashboard).filter(Dashboard.id == dash_a).first()
    _d.generator_id = gen_id
    _db.commit()
    _db.close()
    slides = c.get(f"/api/presentations/{pid}/check").json()["slides"]
    row = [s for s in slides if s.get("title") == "Report A"][0]
    check("generator-backed dashboard with no data is stale", row["stale"] is True, row)

    # An old run's newest input mtime is what makes it stale, not the run row.
    import json as _json
    import time as _time
    _db = SessionLocal()
    old_ms = int((_time.time() - 30 * 86400) * 1000)
    _db.add(GenerationRun(
        dashboard_id=dash_a, generator_version_id=gver_id, engine="browser",
        status="success",
        input_manifest=_json.dumps({"files": [{"name": "a.xlsx", "size": 1,
                                              "mtime_ms": old_ms}]}),
    ))
    _db.commit()
    _db.close()
    row = [s for s in c.get(f"/api/presentations/{pid}/check").json()["slides"]
           if s.get("title") == "Report A"][0]
    check("data_at comes from the run's input manifest", row["data_at"] is not None, row)
    check("30-day-old data is stale", row["stale"] is True, row)

    _db = SessionLocal()
    fresh_ms = int(_time.time() * 1000)
    _db.add(GenerationRun(
        dashboard_id=dash_a, generator_version_id=gver_id, engine="browser",
        status="success",
        input_manifest=_json.dumps({"files": [{"name": "a.xlsx", "size": 1,
                                              "mtime_ms": fresh_ms}]}),
    ))
    _db.commit()
    _db.close()
    row = [s for s in c.get(f"/api/presentations/{pid}/check").json()["slides"]
           if s.get("title") == "Report A"][0]
    check("fresh data is not stale", row["stale"] is False, row)

    # Access lost after the slide was added -> {"access": false}
    _db = SessionLocal()
    _admin = _db.query(User).filter(User.person_id == "admin1").first()
    _anna = _db.query(User).filter(User.person_id == "anna").first()
    _pres = _db.query(Presentation).filter(Presentation.id == vera_pres).first()
    _pres.owner_id = _admin.id  # hand vera's deck to admin1 for the next check
    _db.add(PresentationItem(presentation_id=vera_pres, position=0,
                             kind="dashboard", dashboard_id=anna_dash))
    _db.commit()
    _db.close()
    slides = c.get(f"/api/presentations/{vera_pres}/check").json()["slides"]
    check("admin sees anna's dashboard (administrator reaches everything)",
          slides[0].get("access") is not False, slides)

    login(c, "vera")
    _db = SessionLocal()
    _vera = _db.query(User).filter(User.person_id == "vera").first()
    _pres = _db.query(Presentation).filter(Presentation.id == vera_pres).first()
    _pres.owner_id = _vera.id
    _db.commit()
    _db.close()
    slides = c.get(f"/api/presentations/{vera_pres}/check").json()["slides"]
    check("unreachable dashboard -> {'access': false}",
          slides[0].get("access") is False, slides)

    # ---- show_context ----------------------------------------------------
    login(c, "admin1")
    from app.api import presentations as pres_api
    _db = SessionLocal()
    _admin = _db.query(User).filter(User.person_id == "admin1").first()
    _anna = _db.query(User).filter(User.person_id == "anna").first()

    ctx0 = pres_api.show_context(_db, _admin, pid, 0)
    ctx1 = pres_api.show_context(_db, _admin, pid, 1)
    ctx2 = pres_api.show_context(_db, _admin, pid, 2)
    check("show_context: n is the slide count", ctx0["n"] == 3, ctx0)
    check("show_context: first slide has no prev", ctx0["prev_url"] is None, ctx0)
    check("show_context: last slide has no next", ctx2["next_url"] is None, ctx2)
    check("show_context: dashboard next_url routes to /d/ with pres+i",
          ctx0["next_url"] == f"/d/{dash_a}?pres={pid}&i=1", ctx0)
    check("show_context: middle slide has both edges",
          ctx1["prev_url"] and ctx1["next_url"], ctx1)
    check("show_context: image slide url is the slide route",
          ctx2["item"]["kind"] == "image" and ctx1["next_url"].endswith(f"/presentations/{pid}/slide/2"),
          (ctx1, ctx2))
    check("show_context: builder_url points at the presentation",
          ctx0["builder_url"] == f"/presentations/{pid}", ctx0)
    check("show_context: out of range -> None",
          pres_api.show_context(_db, _admin, pid, 3) is None
          and pres_api.show_context(_db, _admin, pid, -1) is None)
    check("show_context: non-owner -> None",
          pres_api.show_context(_db, _anna, pid, 0) is None)
    check("show_context: unknown presentation -> None",
          pres_api.show_context(_db, _admin, "no-such-id", 0) is None)
    _db.close()

    # ---- pages: builder, show mode, slide --------------------------------
    # The pages are where "owner only" stops being an API property and starts
    # being something a browser could stumble into, so each one is pinned here.
    r = c.get(f"/presentations/{pid}")
    check("builder page -> 200 for owner", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    body = r.text
    check("builder ships its payload as #pres-data", 'id="pres-data"' in body)
    check("builder loads presentations.js", "/static/js/presentations.js" in body)
    check("builder page has the picker and the add buttons",
          'id="pres-picker"' in body and 'id="pres-add-dash"' in body
          and 'id="pres-add-image"' in body and 'id="pres-add-pdf"' in body)
    check("builder page has the check button", 'id="pres-check-btn"' in body)

    login(c, "anna")
    check("builder page -> 404 for a non-owner",
          c.get(f"/presentations/{pid}").status_code == 404)
    check("slide page -> 404 for a non-owner",
          c.get(f"/presentations/{pid}/slide/0").status_code == 404)
    login(c, "admin1")

    r = c.get("/presentations")
    check("presentations list page -> 200", r.status_code == 200, r.status_code)
    check("list page shows the deck", "Q3 review" in r.text)

    # Show mode on a report: the bar rides above the untouched viewer.
    slide0_dash = ctx0["item"]["dashboard_id"]
    r = c.get(f"/d/{slide0_dash}?pres={pid}&i=0")
    check("show mode -> 200", r.status_code == 200, r.status_code)
    bar = r.text
    check('show mode renders id="pres-bar"', 'id="pres-bar"' in bar)
    # `&` is `&amp;` in an href attribute — that IS the correct rendering.
    check("show mode links the next slide",
          f'href="{ctx0["next_url"].replace("&", "&amp;")}"' in bar, ctx0["next_url"])
    check("show mode links back to the builder", f'href="/presentations/{pid}"' in bar)
    check("show mode turns on the chrome-hiding body classes",
          "'op-present', 'op-pres'" in bar)
    check("show mode loads presentations.js", "/static/js/presentations.js" in bar)

    r = c.get(f"/d/{slide0_dash}?pres={pid}&i=99")
    check("an out-of-range i draws no bar",
          r.status_code == 200 and 'id="pres-bar"' not in r.text, r.status_code)
    r = c.get(f"/d/{slide0_dash}?pres=no-such-deck&i=0")
    check("an unknown pres draws no bar",
          r.status_code == 200 and 'id="pres-bar"' not in r.text, r.status_code)
    r = c.get(f"/d/{slide0_dash}")
    check("a plain /d/{id} is untouched",
          r.status_code == 200 and 'id="pres-bar"' not in r.text, r.status_code)

    # Image slide page + the file it points at.
    r = c.get(f"/presentations/{pid}/slide/2")
    check("image slide page -> 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    img_page = r.text
    check("image slide page embeds the file url",
          f'/presentations/{pid}/file/{img_item}' in img_page, img_page[:200])
    check("image slide page carries the same bar", 'id="pres-bar"' in img_page)
    r = c.get(f"/presentations/{pid}/file/{img_item}")
    check("slide file -> 200 with nosniff",
          r.status_code == 200 and r.headers.get("x-content-type-options") == "nosniff",
          f'{r.status_code} {r.headers.get("x-content-type-options")}')
    check("slide file keeps the sniffed type", r.headers["content-type"] == "image/png",
          r.headers.get("content-type"))

    # A pdf slide renders the iframe + the "Open PDF" fallback link.
    r = c.post(f"/api/presentations/{pid}/upload",
               files={"file": ("deck.pdf", b"%PDF-1.4\n" + b"\x00" * 64, "application/pdf")})
    check("pdf slide uploaded", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    pdf_item = r.json()["id"]
    r = c.get(f"/presentations/{pid}/slide/3")
    pdf_page = r.text
    check("pdf slide page -> 200", r.status_code == 200, r.status_code)
    check("pdf slide page draws the page with pdf.js (canvas on the file url) and keeps the viewer iframe as a lazy fallback",
          f'<canvas id="pres-pdf-canvas" data-src="/presentations/{pid}/file/{pdf_item}"' in pdf_page
          and f'<iframe id="pres-pdf" data-src="/presentations/{pid}/file/{pdf_item}#toolbar=0&navpanes=0&scrollbar=0&view=FitH"' in pdf_page
          and '/static/vendor/pdfjs/pdf.min.mjs' in pdf_page)
    check("pdf slide page has no sandbox on that iframe",
          'sandbox' not in pdf_page.split('<iframe id="pres-pdf"', 1)[1].split('>', 1)[0])
    check("pdf slide page offers the fallback link", "Open PDF" in pdf_page)
    r = c.delete(f"/api/presentations/{pid}/items/{pdf_item}")
    check("pdf slide removed again", r.status_code == 200, r.status_code)

    # A dashboard slide reached through the slide route lands on the report.
    # The show shell: one document that hosts the slides in an iframe so that
    # fullscreen survives "Next" (a per-slide navigation dropped it).
    r = c.get(f"/presentations/{pid}/show?i=0")
    check("show shell -> 200 with the first slide in its frame",
          r.status_code == 200 and 'id="pres-buf-a"' in r.text and f"?pres={pid}&amp;i=0" in r.text, r.status_code)
    check("show shell: out of range -> 404", c.get(f"/presentations/{pid}/show?i=99").status_code == 404)
    r = c.get(f"/presentations/{pid}/slide/0", follow_redirects=False)
    check("slide route redirects a report slide to its own viewer",
          r.status_code == 307 and r.headers["location"] == f"/d/{slide0_dash}?pres={pid}&i=0",
          f'{r.status_code} {r.headers.get("location")}')

    # ---- home payload ----------------------------------------------------
    r = c.get("/")
    check("home -> 200", r.status_code == 200, r.status_code)
    import re as _re
    m = _re.search(r'<script id="dash-data" type="application/json">(.*?)</script>', r.text, _re.S)
    check("home ships #dash-data", m is not None)
    payload = json.loads(m.group(1)) if m else {}
    decks = payload.get("presentations") or []
    check("home payload carries the presentation",
          any(d["id"] == pid and d["items_count"] == 3 for d in decks), decks)
    check("home payload carries a slide count, not a per-deck query",
          all("items_count" in d and "updated_at" in d for d in decks), decks)

    # ---- XSS: a hostile title never becomes markup -----------------------
    r = c.post("/api/presentations", json={"title": "<script>alert(1)</script>"})
    xss_pid = r.json()["id"]
    r = c.post(f"/api/presentations/{xss_pid}/items",
               json={"kind": "dashboard", "dashboard_id": slide0_dash})
    check("hostile-titled deck takes a slide", r.status_code == 201, r.status_code)
    b = c.get(f"/presentations/{xss_pid}").text
    check("builder escapes a hostile deck title",
          "<script>alert(1)</script>" not in b and "&lt;script&gt;alert(1)&lt;/script&gt;" in b)
    b = c.get(f"/d/{slide0_dash}?pres={xss_pid}&i=0").text
    check("the presenter bar escapes a hostile deck title",
          "<script>alert(1)</script>" not in b and "&lt;script&gt;alert(1)&lt;/script&gt;" in b)
    c.delete(f"/api/presentations/{xss_pid}")

    # ---- delete ----------------------------------------------------------
    r = c.delete(f"/api/presentations/{pid}/items/{item_b}")
    check("delete slide -> 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    check("slide gone",
          all(i["id"] != item_b for i in c.get(f"/api/presentations/{pid}").json()["items"]))

    r = c.delete(f"/api/presentations/{pid}")
    check("delete presentation -> 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    check("presentation gone", c.get(f"/api/presentations/{pid}").status_code == 404)
    check("its file is off disk", not os.path.exists(disk), disk)
    check("its directory is gone",
          not os.path.exists(os.path.join(f"{TMP}/uploads", "_presentations", pid)))

    _db = SessionLocal()
    left = _db.query(PresentationItem).filter(
        PresentationItem.presentation_id == pid).count()
    _db.close()
    check("its slides are gone from the DB (cascade)", left == 0, left)

    # ---- audit -----------------------------------------------------------
    _db = SessionLocal()
    from app.models import AuditLog
    actions = {a for (a,) in _db.query(AuditLog.action).filter(
        AuditLog.resource_type == "presentation").all()}
    _db.close()
    check("every mutation is audited",
          {"presentation_create", "presentation_update", "presentation_delete",
           "presentation_item_add", "presentation_item_upload",
           "presentation_item_update", "presentation_item_delete",
           "presentation_reorder"} <= actions, sorted(actions))

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S): " + ", ".join(FAILURES))
    sys.exit(1)
print("ALL PASS")
