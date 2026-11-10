"""GET /docs — the Dev Guide.

Pins:
  * the page renders for an ordinary signed-in user (it is documentation, not
    an admin tool) and carries the track markup the page's JS binds to;
  * every copyable artifact reaches the page through `docs_artifacts`, with
    its line breaks intact — a prompt collapsed into one line is not copyable;
  * an English request gets the English artifacts and no Russian text left in
    the guide's own body;
  * a language with no artifact file of its own falls back to Russian rather
    than to an empty block;
  * every screenshot the page references exists in static/img/docs.

Synthetic data only — no real folder/file names, no people.
"""
import os
import re
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="op-smoke-docs-")
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
from app.services import docs_artifacts  # noqa: E402

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"  <-- {extra}"))
    if not cond:
        FAILURES.append(label)


def login(c, ov):
    r = c.post("/dev-login", data={"person_id": ov}, follow_redirects=False)
    assert r.status_code == 302, f"{ov}: {r.status_code} {r.text[:200]}"


def body(html):
    """The guide's own markup, without the chrome around it."""
    return html.split('id="dg-root"', 1)[1] if 'id="dg-root"' in html else ""


with TestClient(app) as c:
    login(c, "admin1")
    c.post("/api/admin/users", json={"person_id": "reader1", "role": "visitor",
                                     "display_name": "reader1"})
    login(c, "reader1")
    r = c.get("/docs")
    check("/docs renders for an ordinary user", r.status_code == 200, r.status_code)
    ru = r.text
    check("the page carries the track root", 'id="dg-root"' in ru and 'data-track=' in ru)
    check("both tracks are on the page",
          'data-track-btn="analyst"' in ru and 'data-track-btn="author"' in ru)
    check("the map nodes point at steps", ru.count("data-goto=") >= 5, ru.count("data-goto="))

    # Artifacts: present, multi-line, and copyable.
    for name in docs_artifacts.NAMES:
        block = re.search(r'<pre id="dg-code-%s">(.*?)</pre>' % name, ru, re.S)
        check(f"artifact {name} is on the page", bool(block))
        if block:
            text = block.group(1)
            check(f"artifact {name} keeps its line breaks", text.count("\n") >= 3,
                  text[:60])
            check(f"artifact {name} escaped its markup", "<script>" not in text)

    # Screenshots are real files, not broken references.
    shots = set(re.findall(r'/static/img/docs/([a-z_]+\.webp)', ru))
    check("the page references screenshots", len(shots) >= 8, sorted(shots))
    missing = [s for s in shots if not os.path.exists(os.path.join("static", "img", "docs", s))]
    check("every referenced screenshot exists", not missing, missing)

    # English.
    r = c.get("/docs", headers={"Cookie": f"op_session={c.cookies.get('op_session')}; op_lang=en"})
    en = r.text
    check("/docs renders in English", r.status_code == 200, r.status_code)
    check("the English guide has no Russian left",
          not re.search(r"[\u0410-\u044f]{4,}", body(en)),
          re.findall(r"[\u0410-\u044f]{4,}", body(en))[:6])
    check("the English guide ships the English artifacts",
          "Write a self-contained HTML file" in en)

# A language with no artifact file of its own still gets a usable block.
zh = docs_artifacts.load("zh-HK")
check("an untranslated language falls back to Russian artifacts",
      all(zh[n] for n in docs_artifacts.NAMES) and "HTML" in zh["base"])

print()
if FAILURES:
    print("FAILURES: %d" % len(FAILURES))
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("smoke_docs: all checks passed")
