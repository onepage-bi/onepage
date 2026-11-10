"""Regression: stored data must never break out of the inline <script> seeds.

Both seeds are built by splicing a JSON string straight into a `<script>` body:
`window.__OP_LS__ = {…}` (detector.inject_ls_state) and the bootstrap-snapshot
tag (detector.inject_snapshot). json.dumps escapes neither `<` nor `/`, so a
stored value containing `</script>` used to close the tag and inject markup.

Who could do it: `</script>` is legal inside a JSON string, and both write paths
accept it — PUT /localstorage takes an arbitrary body, PUT /snapshot validates
only that the bytes parse as JSON. Both are open to a can_update_data
collaborator, i.e. a non-owner, non-admin user. The snapshot seed is also served
to anonymous share-link visitors, so the payload would reach them too.

The sibling injectors (inject_textedit, files.snapshot_js_body) already escape
`<` for exactly this reason; this test pins the same guarantee on the other two.
"""
import json
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="op-xss-")
os.environ.update({
    "OP_DB_PATH": f"{TMP}/onepage.db",
    "OP_UPLOADS_DIR": f"{TMP}/uploads",
    "OP_PERF_DIR": f"{TMP}/perflog",
    "OP_DEV_BYPASS_AUTH": "true",
    "OP_INITIAL_ADMIN": "admin1",
    "OP_CACHE_WARM": "0",
    # ADR-17 Quests: harmless for every other check in this file, needed
    # so the router is mounted at all (settings.quests_enabled gates it,
    # canon `app/api/explore.py:55-61`) — must be set before `app.main`
    # imports `app.config.settings`.
    "OP_QUESTS_ENABLED": "true",
})

sys.path.insert(0, os.getcwd())
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.services import quests as qsvc  # noqa: E402

# The daily surprise and the quiz run on working days only: on a weekend the
# test looks at the next working day, otherwise it would fail every Saturday.
if not qsvc.is_working(qsvc.today()):
    qsvc._TODAY_OVERRIDE = qsvc.next_working(qsvc.today())
from app.services import detector  # noqa: E402
from app.services import quests as _qsvc  # noqa: E402
from app.services import quests_quiz as _qquiz  # noqa: E402

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


# The payload closes the seed tag and opens an attacker-controlled one.
BREAKOUT = "</script><img src=x onerror=alert(1)>"

INTERACTIVE = (b"<html><head><title>i</title></head><body>"
               b'<input type="file" id="f"><h1>report</h1></body></html>')
SNAPSHOT = (b"<html><head><title>s</title></head><body>"
            b'<script id="bootstrap-snapshot" type="application/json">{}</script>'
            b"<h1>report</h1></body></html>")


def seed_is_intact(body: str, marker: str) -> bool:
    """The seed script must still be one tag: no raw </script> before its end."""
    start = body.index(marker)
    end = body.index("</script>", start)
    return "<" not in body[start + len(marker):end]


with TestClient(app) as c:
    c.post("/dev-login", data={"person_id": "admin1"}, follow_redirects=False)

    # ---- unit level: the injectors themselves ---------------------------
    ls = detector.inject_ls_state("<html><head></head></html>",
                                  json.dumps({"k": BREAKOUT}))
    check("inject_ls_state escapes <", "</script><img" not in ls, ls[:200])
    check("inject_ls_state seed stays one tag",
          seed_is_intact(ls, "window.__OP_LS__ = "), ls[:200])

    snap_html = SNAPSHOT.decode()
    sn = detector.inject_snapshot(snap_html, json.dumps({"k": BREAKOUT}))
    check("inject_snapshot escapes <", "</script><img" not in sn, sn[:300])
    check("inject_snapshot keeps exactly one </script> for the seed",
          sn.count("</script>") == snap_html.count("</script>"), sn[:300])

    # The escape must not change what JS parses back.
    payload = json.dumps({"k": BREAKOUT})
    escaped = payload.replace("<", "\\u003c")
    check("escape preserves the parsed value", json.loads(escaped) == json.loads(payload))

    # ---- end to end: through the render path ----------------------------
    r = c.post("/api/dashboards",
               data={"title": "interactive", "description": ""},
               files={"html_file": ("i.html", INTERACTIVE, "text/html")},
               follow_redirects=False)
    check("interactive dashboard created", r.status_code == 303, f"{r.status_code} {r.text[:200]}")
    i_id = r.headers["location"].rsplit("/", 1)[1]

    r = c.put(f"/api/dashboards/{i_id}/localstorage", json={"k": BREAKOUT})
    check("localstorage accepts the payload (write path unchanged)",
          r.status_code == 200, f"{r.status_code} {r.text[:200]}")

    body = c.get(f"/render/{i_id}/").text
    check("render: localstorage payload cannot close the seed",
          "</script><img" not in body)
    check("render: seed still holds the value",
          "\\u003c/script>" in body or "\\u003c" in body, body[:400])

    r = c.post("/api/dashboards",
               data={"title": "snapshot", "description": ""},
               files={"html_file": ("s.html", SNAPSHOT, "text/html")},
               follow_redirects=False)
    check("snapshot dashboard created", r.status_code == 303, f"{r.status_code} {r.text[:200]}")
    s_id = r.headers["location"].rsplit("/", 1)[1]

    r = c.put(f"/api/dashboards/{s_id}/snapshot",
              files={"file": ("snap.json", json.dumps({"k": BREAKOUT}).encode(),
                              "application/json")})
    check("snapshot accepts valid JSON carrying </script>",
          r.status_code == 200, f"{r.status_code} {r.text[:200]}")

    body = c.get(f"/render/{s_id}/").text
    check("render: snapshot payload cannot close the seed",
          "</script><img" not in body)

    # A share visitor gets the same body through share_render — same injector.
    r = c.post(f"/api/dashboards/{s_id}/share-links", json={"expires_in": None})
    if r.status_code in (200, 201):
        token = (r.json().get("token")
                 or (r.json().get("url", "").rsplit("/", 1)[-1]))
        if token:
            sr = c.get(f"/share/{token}/render/")
            check("share render: snapshot payload cannot close the seed",
                  sr.status_code != 200 or "</script><img" not in sr.text)

    # ---- the third seed: the presentation builder's #pres-data -----------
    # Same shape as the two above — a JSON blob spliced into a <script> body —
    # so the same payload is aimed at it. Here the escaping comes from Jinja's
    # `| tojson` (htmlsafe_json_dumps) rather than a hand-written injector, and
    # this check is what keeps a future `| safe` from quietly removing it.
    r = c.post("/api/presentations", json={"title": BREAKOUT})
    check("presentation accepts the breakout title (write path unchanged)",
          r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    if r.status_code == 201:
        p_id = r.json()["id"]
        body = c.get(f"/presentations/{p_id}").text
        check("builder: deck title cannot close the #pres-data seed",
              "</script><img" not in body, body[:400])
        check("builder: the #pres-data seed stays one tag",
              seed_is_intact(body, '<script id="pres-data" type="application/json">'),
              body[body.index('id="pres-data"'):][:300])
        check("builder: the title is escaped in the page's own markup too",
              "<img src=x onerror=alert(1)>" not in body)

        # And the presenter bar, which prints the same title on every slide.
        r = c.post("/api/dashboards", data={"title": "pres slide", "description": ""},
                   files={"html_file": ("d.html", INTERACTIVE, "text/html")},
                   follow_redirects=False)
        d_id = r.headers["location"].rsplit("/", 1)[1]
        r = c.post(f"/api/presentations/{p_id}/items",
                   json={"kind": "dashboard", "dashboard_id": d_id})
        check("breakout deck takes a slide", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
        bar = c.get(f"/d/{d_id}?pres={p_id}&i=0").text
        check("show mode: deck title cannot inject markup into the bar",
              "</script><img" not in bar and "<img src=x onerror=alert(1)>" not in bar)

    # ---- ADR-16 reference tables: title baked into data-* attributes -----
    # templates/pages/reference.html and reference_detail.html splice the
    # JSON API's own output into `data-tables`/`data-table` attributes (same
    # idiom as catalog.html's `data-tables`, not a <script> seed) — a title
    # containing the breakout payload must come back HTML-entity-escaped in
    # the attribute, and textContent-only in the rendered row/heading.
    r = c.post("/api/reference", json={
        "slug": "xss_ref_tbl", "title": BREAKOUT,
        "columns": [{"name": "K", "type": "text"}, {"name": "V", "type": "text"}],
    })
    check("reference table accepts the breakout title (write path unchanged)",
          r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    if r.status_code == 201:
        list_body = c.get("/reference").text
        check("reference list: breakout title never appears unescaped",
              "</script><img" not in list_body and "<img src=x onerror=alert(1)>" not in list_body,
              list_body[:400])

        detail_body = c.get("/reference/xss_ref_tbl").text
        check("reference detail: breakout title never appears unescaped",
              "</script><img" not in detail_body and "<img src=x onerror=alert(1)>" not in detail_body,
              detail_body[:400])
        check("reference detail: data-table attribute stays inside one tag",
              "<img src=x onerror=alert(1)>" not in detail_body, detail_body[:400])

    # ---- the CONSUMER identities baked into the same attributes ---------
    # `_table_summary` now ships `consumer_generators[].name` and
    # `consumer_dashboards[].title` so the list page can filter by report.
    # A generator name is charset-constrained by the manifest validator, but
    # a DASHBOARD TITLE is free text its owner types — and it lands in the
    # very same `data-tables` attribute as the table's own title.
    import io as _io, json as _json, zipfile as _zip
    SLUG2 = "xss_ref_consumer"
    _m = {"name": "xssgen", "version": "1.0", "entrypoint": {"module": "g.main"},
          "outputs": {"html": True, "slots": []}, "engine": "auto",
          "inputs": [
              {"match": "SL_*.xlsx", "role": "data", "required": True,
               "sheets": [{"name": "S", "columns": ["A"]}]},
              {"match": f"{SLUG2}.xlsx", "role": "reference", "source": "platform",
               "table": SLUG2, "required": False,
               "sheets": [{"name": "data", "columns": [{"name": "K", "type": "text"},
                                                       {"name": "V", "type": "text"}]}]}]}
    _buf = _io.BytesIO()
    with _zip.ZipFile(_buf, "w", _zip.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", _json.dumps(_m))
        z.writestr("g/__init__.py", "")
        z.writestr("g/main.py", "def run(*a, **k):\n    pass\n")
    r = c.post("/api/admin/generators",
               files={"bundle": ("b.zip", _buf.getvalue(), "application/zip")})
    check("generator with a platform input registers (auto-creates the table)",
          r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    if r.status_code == 201:
        gid = r.json()["id"]
        r = c.post("/api/dashboards/generated",
                   json={"title": BREAKOUT, "generator_id": gid})
        check("a dashboard may be titled with the breakout payload",
              r.status_code == 201, f"{r.status_code} {r.text[:200]}")
        api = c.get("/api/reference").text
        check("consumer identities are in the JSON payload at all",
              SLUG2 in api and "xssgen" in api, api[:300])
        list_body = c.get("/reference").text
        check("reference list: a consumer dashboard title cannot break out of data-tables",
              "</script><img" not in list_body
              and "<img src=x onerror=alert(1)>" not in list_body, list_body[:400])
        detail_body = c.get(f"/reference/{SLUG2}").text
        check("reference detail: a consumer dashboard title cannot break out of data-table",
              "</script><img" not in detail_body
              and "<img src=x onerror=alert(1)>" not in detail_body, detail_body[:400])

    # ---- ADR-17 quests: user text baked into #q-root's data-state -------
    # `/quests` splices the whole page state — including OTHER users'
    # display names in the opted-in leaderboard rows and, indirectly, quiz
    # content imported by an admin — into `data-state="{{ state_json }}"`.
    # That's an HTML *attribute*, not a <script> seed: Jinja's autoescape
    # (not a hand-written injector) is what has to hold here, so this is
    # the same class of check as the ADR-16 `data-tables`/`data-table`
    # block above, aimed at a brand-new template.
    r = c.post("/api/admin/users", json={
        "person_id": "quest_breakout_user", "role": "visitor", "display_name": BREAKOUT,
    })
    check("quests: second user created (write path unchanged)",
          r.status_code == 200, f"{r.status_code} {r.text[:200]}")

    r = c.post("/dev-login", data={"person_id": "quest_breakout_user"}, follow_redirects=False)
    check("quests: dev-login as breakout user", r.status_code == 302, f"{r.status_code}")

    r = c.put("/api/quests/leaderboard-optin", json={"optin": True})
    check("quests: breakout user opts into the leaderboard",
          r.status_code == 200, f"{r.status_code} {r.text[:200]}")

    r = c.post("/dev-login", data={"person_id": "admin1"}, follow_redirects=False)
    check("quests: dev-login back to admin", r.status_code == 302, f"{r.status_code}")

    r = c.put("/api/quests/leaderboard-optin", json={"optin": True})
    check("quests: admin opts into the leaderboard (rows only render when the viewer is in too)",
          r.status_code == 200, f"{r.status_code} {r.text[:200]}")

    r = c.get("/quests")
    check("quests: page renders", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    page_body = r.text
    check("quests: leaderboard name cannot close the data-state attribute",
          "</script><img" not in page_body
          and "<img src=x onerror=alert(1)>" not in page_body, page_body[:400])
    attr_marker = 'id="q-root" data-state="'
    attr_val_start = page_body.find(attr_marker)
    if attr_val_start != -1:
        attr_val_start += len(attr_marker)
    attr_val_end = page_body.find('">', attr_val_start) if attr_val_start != -1 else -1
    state_attr_val = page_body[attr_val_start:attr_val_end] if attr_val_end != -1 else ""
    check("quests: #q-root data-state carries the escaped breakout",
          "&lt;" in state_attr_val and "</script><img" not in state_attr_val,
          state_attr_val[:300] if state_attr_val else "no #q-root data-state found")

    # ---- ADR-17 quiz import: question/option/explanation breakouts ------
    # The quiz bank is admin-imported text (`load_default_bank()` mutated
    # here, not the shipped file) — served to the client as validated JSON
    # (`quests_quiz.public_question`, fine: it's a fetch response, not HTML)
    # but must NEVER leak into the page's own markup via `/quests`.
    bank = _qquiz.load_default_bank()
    # The serving cycle comes from the bank being imported right now (after
    # the import the active days = this bank's days), not from the default
    # constant: with a 30-day bank "today" is a different day than with 15.
    bank_day = _qsvc.quiz_bank_day(_qsvc.today(), _qquiz.bank_days_of(bank))
    idx0 = (bank_day - 1) * _qquiz.PER_DAY  # today's first (lowest-id) question
    bank[idx0]["q"] = BREAKOUT
    bank[idx0]["o"][0] = BREAKOUT
    bank[idx0]["e"] = BREAKOUT

    r = c.post("/api/admin/quests/quiz-import", json={"bank": bank, "dry_run": False})
    check("quests: quiz bank import (breakout question, real import) succeeds",
          r.status_code == 200, f"{r.status_code} {r.text[:400]}")

    r = c.post("/api/quests/quiz/start")
    check("quests: quiz start returns 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    started = r.json() if r.status_code == 200 else {}
    check("quests: quiz start JSON carries the breakout text as plain JSON data (fine)",
          started.get("question", {}).get("q") == BREAKOUT, str(started)[:300])

    r = c.get("/quests")
    check("quests: page still renders after quiz import", r.status_code == 200, f"{r.status_code}")
    page_body = r.text
    check("quests: quiz question/option/explanation never reach the page's own markup",
          "</script><img" not in page_body
          and "<img src=x onerror=alert(1)>" not in page_body, page_body[:400])

    # ---- ADR-17 admin page: no `| safe` opt-out in the template ---------
    # Holidays/season config are admin-typed but not USER text (nobody else
    # can inject through them), so the meaningful pin here is structural:
    # the template must not have grown a `| safe` that would silently
    # disable Jinja's autoescape for anything rendered on this page.
    r = c.get("/admin/quests")
    check("quests: admin page renders", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    admin_tpl_path = os.path.join(os.getcwd(), "templates", "pages", "admin_quests.html")
    with open(admin_tpl_path, "r", encoding="utf-8") as fh:
        admin_tpl_src = fh.read()
    check("quests: admin_quests.html has no `| safe` autoescape opt-out",
          "| safe" not in admin_tpl_src and "|safe" not in admin_tpl_src)

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S): " + ", ".join(FAILURES))
    sys.exit(1)
print("ALL PASS")
