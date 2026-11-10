"""Smoke test for the /data-sources lineage UI (task C2).

Two halves, because the page has two failure modes that cannot see each other:

  * the SERVER half — the template must ship the #lineage stage, both scripts
    (dagre before lineage.js) and the dev flag driven by ?lineage=sample, and
    must no longer reference the canvas that lineage.js replaced;
  * the BROWSER half — `node tests/lineage_dom.js`, which runs the real
    lineage.js against a minimal DOM and checks the things that only exist at
    runtime: edge contraction when a layer is folded, the writes_back loop
    staying out of dagre's ranking, focus closure, the ds:* event contract with
    data-sources-edit.js, and the 300-nodes-under-200 ms budget.

The events are the contract worth pinning: data-sources-edit.js was NOT changed
by this task, so ds:filter / ds:open / ds:saved must keep working exactly as
they did against the file lineage.js replaced.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import zipfile

TMP = tempfile.mkdtemp(prefix="op-lineage-ui-")
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


def make_bundle(name):
    m = {
        "name": name,
        "version": "1.0",
        "entrypoint": {"module": "gen.main"},
        "outputs": {"html": True, "slots": []},
        "engine": "browser",
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(m))
        z.writestr("gen/__init__.py", "")
        z.writestr("gen/main.py", "def begin(*a, **k):\n    pass\n")
    return buf.getvalue()


with TestClient(app) as c:
    r = c.post("/dev-login", data={"person_id": "admin1"}, follow_redirects=False)
    assert r.status_code == 302, r.status_code
    r = c.post("/api/admin/generators",
               files={"bundle": ("b.zip", make_bundle("rdcstock"), "application/zip")})
    assert r.status_code == 201, r.text[:300]
    gid = r.json()["id"]
    r = c.post("/api/dashboards/generated", json={"title": "RDC Stock Health", "generator_id": gid})
    assert r.status_code == 201, r.text[:300]

    page = c.get("/data-sources")
    check("page renders", page.status_code == 200, str(page.status_code))
    h = page.text

    check("nav-item Data Quality present", 'href="/data-quality"' in h and 'Data Quality' in h)
    check("dq-bell present and hidden by default", 'id="dq-bell"' in h and 'data-dq-badge hidden' in h)

    # The badge markup switched from `.nav-meta` (no `display`) to the shared
    # `.badge` component (`display: inline-flex`), which beats the UA's
    # `[hidden] { display: none }` rule. Pages that include topbar.html (this
    # one among them) have no page-local [hidden] reset, so main.css must
    # carry one scoped to the badge itself or it renders as a permanent
    # empty pill whenever main.js leaves it [hidden] (0 open findings).
    css = c.get("/static/css/main.css")
    check("main.css resets [hidden] on the topbar dq badge",
          css.status_code == 200
          and "[data-dq-badge][hidden]" in css.text
          and "display: none" in css.text.split("[data-dq-badge][hidden]", 1)[1][:60])

    check("lineage section is present", 'id="lineage"' in h)
    check("stage carries the viewport/world/edges/cards hooks",
          all(k in h for k in ("data-lin-viewport", "data-lin-world",
                               "data-lin-edges", "data-lin-cards")))
    check("skeleton and error states exist",
          "data-lin-skeleton" in h and "data-lin-error" in h and "data-lin-msg" in h)
    check("toolbar has detail/fit/reset", 'data-lin-detail' in h
          and 'data-lin-act="fit"' in h and 'data-lin-act="reset"' in h)
    check("toolbar has zoom in/out/100 and a clear-focus action", all(k in h for k in (
          'data-lin-zoom="in"', 'data-lin-zoom="out"', 'data-lin-zoom="100"',
          'data-lin-zoom-pct', 'data-lin-act="unfocus"')))
    check("clear-focus button starts hidden", 'data-lin-act="unfocus" hidden' in h)
    check("hint documents the new wheel/zoom/pan gestures without repeating 'pan' twice",
          "scroll to pan" in h and "to zoom" in h and "drag" in h
          and h.count("to pan") == 1)
    check("hint documents the +/-/0 keyboard shortcuts the toolbar buttons advertise",
          "+/" in h and "0 to zoom" in h)
    check("dagre loads before lineage.js",
          0 < h.index("vendor/dagre.min.js") < h.index("js/lineage.js"))
    check("the replaced canvas is gone from the template",
          "ds-canvas" not in h and "data-sources.js" not in h and "graph_json" not in h)
    check("data-sources.js is deleted from disk",
          not os.path.exists("static/js/data-sources.js"))
    check("the filter/drawer script is untouched and still loaded",
          "data-sources-edit.js" in h)
    # This workspace has no run yet, so the table body is empty — the row hook
    # the graph listens on is asserted on the template instead of the render.
    tpl = open("templates/pages/data_sources.html", encoding="utf-8").read()
    check("source rows still carry the key the graph highlights by",
          'data-src-row="{{ s.key }}"' in tpl)

    check("dev fixture is off by default", 'data-lineage-dev="0"' in h)
    h2 = c.get("/data-sources?lineage=sample").text
    check("?lineage=sample turns the dev fixture on", 'data-lineage-dev="1"' in h2)

    # The endpoint is A7's; until it lands the page must say so rather than
    # showing an empty rectangle. All this test can assert here is that the
    # route the drawing calls exists at all.
    r = c.get("/api/data-sources/lineage")
    check("lineage endpoint answers (501 until A7 lands)",
          r.status_code in (200, 501), str(r.status_code))

print("--- node tests/lineage_dom.js ---")
p = subprocess.run(["node", "tests/lineage_dom.js"], capture_output=True, text=True)
print(p.stdout.strip())
if p.stderr.strip():
    print(p.stderr.strip())
check("browser-side harness", p.returncode == 0)


# gen-engine.js enumerate(): required_files-derived inputs must never form an
# allow-list on their own (regression 2026-08-21, ops-rdc-health got 1 of 16
# files). Pinned as a source check because enumerate() needs a browser.
_src = (__import__("pathlib").Path(__file__).resolve().parents[1] / "static/js/gen-engine.js").read_text()
check("gen-engine allow-list is gated on an author-declared input",
      "inputsDecl.some(inp => !inp.from_required_files)" in _src)

print("FAILURES: " + ", ".join(FAILURES) if FAILURES else "ALL PASS")
sys.exit(1 if FAILURES else 0)
