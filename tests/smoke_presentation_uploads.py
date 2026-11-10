"""Regression: what a presentation will accept as a slide file, and how it
serves it back.

A slide image is server-stored content of the same class as a dashboard slot
file, so the same three defences apply and each is pinned here:

  * **sniffing** — the verdict comes from the first bytes, never from the
    declared Content-Type or the extension. An SVG renamed
    `.png` would be script running in OUR origin if it were ever served back
    with its stored type, so it is refused at the door;
  * **a hard cap enforced during the read** — 5 MB image / 20 MB pdf, decided
    AFTER the sniff, aborted before the body is fully buffered and long before
    anything reaches the disk. The test asserts the directory stays empty;
  * **the stored name is built from ids** — a filename carrying `../` cannot
    steer the path, because no part of the client's name is used to build it.

Serving adds `nosniff` + `inline` + `private, no-store`, and is owner-only.
The last case is the OriginGuard one: a browser client MUST send FormData, and
an untyped body from a foreign origin is refused before the route is reached.
"""
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="op-presup-")
os.environ.update({
    "OP_DB_PATH": f"{TMP}/onepage.db",
    "OP_UPLOADS_DIR": f"{TMP}/uploads",
    "OP_PERF_DIR": f"{TMP}/perflog",
    "OP_DEV_BYPASS_AUTH": "true",
    "OP_INITIAL_ADMIN": "admin1",
    "OP_CACHE_WARM": "0",
    # OriginGuard needs a name of our own to compare against (smoke_csrf_origin).
    "OP_SAML_SP_BASE_URL": "http://testserver",
})

sys.path.insert(0, os.getcwd())
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


SELF = "http://testserver"
EVIL = "https://evil.example"

# Real leading bytes + padding: enough for a magic-byte reader, and the file is
# never decoded by the server.
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 64
PDF = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n" + b"0" * 64

SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
HTML = b"<!doctype html><html><body><script>alert(1)</script></body></html>"
EXE = b"MZ\x90\x00\x03" + b"\x00" * 64          # a PE renamed to .png
ZIP = b"PK\x03\x04" + b"\x00" * 64              # a zip renamed to .pdf

PRES_ROOT = os.path.join(f"{TMP}/uploads", "_presentations")


def files_on_disk(pid):
    d = os.path.join(PRES_ROOT, pid)
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


def login(c, person_id):
    r = c.post("/dev-login", data={"person_id": person_id}, follow_redirects=False)
    assert r.status_code == 302, f"dev-login {person_id}: {r.status_code}"


with TestClient(app) as c:
    from app.database import SessionLocal
    from app.models import PresentationItem, User
    _db = SessionLocal()
    _db.add(User(person_id="anna", role="superuser"))
    _db.commit()
    _db.close()

    login(c, "admin1")
    pid = c.post("/api/presentations", json={"title": "uploads"}).json()["id"]

    # ---- accepted types --------------------------------------------------
    accepted = {}
    for label, data, name, declared, want_kind, want_type in (
        ("png", PNG, "chart.png", "image/png", "image", "image/png"),
        ("jpeg", JPEG, "photo.jpg", "image/jpeg", "image", "image/jpeg"),
        ("pdf", PDF, "handout.pdf", "application/pdf", "pdf", "application/pdf"),
    ):
        r = c.post(f"/api/presentations/{pid}/upload",
                   files={"file": (name, data, declared)},
                   data={"note": f"{label} slide"})
        ok = r.status_code == 201 and r.json()["kind"] == want_kind
        check(f"{label} accepted as kind={want_kind}", ok, f"{r.status_code} {r.text[:200]}")
        if ok:
            accepted[label] = r.json()["id"]
            check(f"{label} keeps its size and original name",
                  r.json()["size_bytes"] == len(data) and r.json()["original_name"] == name,
                  r.json())
            check(f"{label} note came through the form field",
                  r.json()["note"] == f"{label} slide", r.json())

    _db = SessionLocal()
    stored = {i.id: (i.content_type, i.file_path) for i in
              _db.query(PresentationItem).filter(
                  PresentationItem.presentation_id == pid).all()}
    _db.close()
    check("stored content_type comes from the sniff, not the request",
          stored[accepted["png"]][0] == "image/png"
          and stored[accepted["jpeg"]][0] == "image/jpeg"
          and stored[accepted["pdf"]][0] == "application/pdf", stored)

    # A lying Content-Type does not change the verdict either way.
    r = c.post(f"/api/presentations/{pid}/upload",
               files={"file": ("x.pdf", PNG, "application/pdf")})
    check("PNG bytes declared as pdf are stored as an image",
          r.status_code == 201 and r.json()["kind"] == "image",
          f"{r.status_code} {r.text[:200]}")
    liar = r.json()["id"]

    # ---- refused types ---------------------------------------------------
    before = files_on_disk(pid)
    for label, data, name, declared in (
        ("svg", SVG, "logo.svg", "image/svg+xml"),
        ("svg renamed .png", SVG, "logo.png", "image/png"),
        ("html fragment (no doctype)", b"<div>not a document</div>", "frag.html", "text/html"),
        ("csv with an angle bracket", b"a;b\n1;<2\n", "data.csv", "text/csv"),
        ("exe renamed .png", EXE, "setup.png", "image/png"),
        ("zip renamed .pdf", ZIP, "book.pdf", "application/pdf"),
        ("empty declared png", b"", "empty.png", "image/png"),
    ):
        r = c.post(f"/api/presentations/{pid}/upload",
                   files={"file": (name, data, declared)})
        want = (415,) if data else (400, 415)
        check(f"{label} -> {'/'.join(map(str, want))}", r.status_code in want,
              f"{r.status_code} {r.text[:150]}")
    check("no refused upload left a file behind", files_on_disk(pid) == before,
          files_on_disk(pid))

    # ---- html slides: accepted, sandbox-served (decks made in Claude) ----
    r = c.post(f"/api/presentations/{pid}/upload",
               files={"file": ("deck.html", HTML, "text/html")})
    check("html document accepted as kind html",
          r.status_code == 201 and r.json()["kind"] == "html",
          f"{r.status_code} {r.text[:200]}")
    hid = r.json()["id"] if r.status_code == 201 else None
    if hid:
        fr = c.get(f"/presentations/{pid}/file/{hid}")
        check("html slide serves as text/html with CSP sandbox",
              fr.status_code == 200
              and fr.headers.get("content-type", "").startswith("text/html")
              and fr.headers.get("content-security-policy") == "sandbox allow-scripts"
              and fr.headers.get("x-content-type-options") == "nosniff",
              dict(fr.headers))
        c.delete(f"/api/presentations/{pid}/items/{hid}")
    r = c.post(f"/api/presentations/{pid}/upload",
               files={"file": ("deck.png", b"\xef\xbb\xbf  <!-- title -->\n<HTML><body>x</body></HTML>", "image/png")})
    check("BOM+comment html declared as png is still sniffed as html",
          r.status_code == 201 and r.json()["kind"] == "html",
          f"{r.status_code} {r.text[:200]}")
    if r.status_code == 201:
        c.delete(f"/api/presentations/{pid}/items/{r.json()['id']}")

    # ---- caps ------------------------------------------------------------
    before = files_on_disk(pid)
    big_png = PNG + b"\x00" * (5 * 1024 * 1024 + 1 - len(PNG))
    r = c.post(f"/api/presentations/{pid}/upload",
               files={"file": ("huge.png", big_png, "image/png")})
    check("5 MB + 1 image -> 413", r.status_code == 413, f"{r.status_code} {r.text[:150]}")

    big_pdf = PDF + b"0" * (20 * 1024 * 1024 + 1 - len(PDF))
    r = c.post(f"/api/presentations/{pid}/upload",
               files={"file": ("huge.pdf", big_pdf, "application/pdf")})
    check("20 MB + 1 pdf -> 413", r.status_code == 413, f"{r.status_code} {r.text[:150]}")

    big_html = HTML + b"<!--" + b"0" * (10 * 1024 * 1024 + 1) + b"-->"
    r = c.post(f"/api/presentations/{pid}/upload",
               files={"file": ("huge.html", big_html, "text/html")})
    check("10 MB + 1 html -> 413", r.status_code == 413, f"{r.status_code} {r.text[:150]}")

    check("nothing over the cap reached the disk", files_on_disk(pid) == before,
          files_on_disk(pid))

    # A 6 MB PDF is fine — the cap follows the sniffed kind, not the biggest one.
    mid_pdf = PDF + b"0" * (6 * 1024 * 1024)
    r = c.post(f"/api/presentations/{pid}/upload",
               files={"file": ("big-but-ok.pdf", mid_pdf, "application/pdf")})
    check("6 MB pdf accepted (image cap is not applied to pdf)",
          r.status_code == 201, f"{r.status_code} {r.text[:150]}")
    if r.status_code == 201:
        c.delete(f"/api/presentations/{pid}/items/{r.json()['id']}")

    # ---- the stored path is built from ids -------------------------------
    r = c.post(f"/api/presentations/{pid}/upload",
               files={"file": ("../../../../etc/evil.png", PNG, "image/png")})
    check("a ../ filename still uploads", r.status_code == 201,
          f"{r.status_code} {r.text[:200]}")
    trav = r.json()["id"]
    _db = SessionLocal()
    rel = _db.query(PresentationItem).filter(PresentationItem.id == trav).first().file_path
    _db.close()
    check("stored path is _presentations/{pid}/{item_id}.png",
          rel == os.path.join("_presentations", pid, f"{trav}.png"), rel)
    check("stored path escapes nothing", ".." not in rel, rel)
    check("the file really sits in the presentation dir",
          f"{trav}.png" in files_on_disk(pid), files_on_disk(pid))
    check("original_name is kept as metadata only",
          c.get(f"/api/presentations/{pid}").json()["items"][-1]["original_name"]
          == "../../../../etc/evil.png")

    # ---- serving ---------------------------------------------------------
    r = c.get(f"/presentations/{pid}/file/{accepted['png']}")
    check("owner GET file -> 200", r.status_code == 200, f"{r.status_code} {r.text[:150]}")
    check("served with the stored type",
          r.headers.get("content-type", "").startswith("image/png"), r.headers)
    check("served with nosniff",
          r.headers.get("x-content-type-options") == "nosniff", r.headers)
    check("served with an explicit identity encoding (never gzipped on the event loop)",
          r.headers.get("content-encoding") == "identity", dict(r.headers))
    check("served inline, named by id",
          r.headers.get("content-disposition") == f'inline; filename="{accepted["png"]}.png"',
          r.headers)
    check("served private, no-store",
          r.headers.get("cache-control") == "private, no-store", r.headers)
    check("served bytes are the stored bytes", r.content == PNG, len(r.content))

    r = c.get(f"/presentations/{pid}/file/{accepted['pdf']}")
    check("pdf served as application/pdf",
          r.status_code == 200 and r.headers.get("content-type", "").startswith("application/pdf"),
          r.headers)
    r = c.get(f"/presentations/{pid}/file/{liar}")
    check("the mislabelled .pdf is served as the image it is",
          r.headers.get("content-type", "").startswith("image/png"), r.headers)

    check("unknown item id -> 404",
          c.get(f"/presentations/{pid}/file/no-such-item").status_code == 404)

    login(c, "anna")
    r = c.get(f"/presentations/{pid}/file/{accepted['png']}")
    check("non-owner GET file -> 404 (not 403)", r.status_code == 404,
          f"{r.status_code} {r.text[:150]}")
    r = c.post(f"/api/presentations/{pid}/upload",
               files={"file": ("x.png", PNG, "image/png")})
    check("non-owner upload -> 404", r.status_code == 404, f"{r.status_code} {r.text[:150]}")

    # ---- OriginGuard -----------------------------------------------------
    login(c, "admin1")
    r = c.post(f"/api/presentations/{pid}/upload",
               content=PNG, headers={"origin": SELF})
    check("upload with a body but no Content-Type -> 403 (OriginGuard)",
          r.status_code == 403, f"{r.status_code} {r.text[:150]}")
    r = c.post(f"/api/presentations/{pid}/upload",
               files={"file": ("x.png", PNG, "image/png")},
               headers={"origin": EVIL})
    check("upload from a foreign origin -> 403", r.status_code == 403,
          f"{r.status_code} {r.text[:150]}")
    r = c.post("/api/presentations", json={"title": "csrf"}, headers={"origin": EVIL})
    check("cross-origin presentation create -> 403", r.status_code == 403,
          f"{r.status_code} {r.text[:150]}")

    # FormData from our own page is the shape that must keep working.
    r = c.post(f"/api/presentations/{pid}/upload",
               files={"file": ("ok.png", PNG, "image/png")},
               headers={"origin": SELF})
    check("FormData upload from our own origin -> 201", r.status_code == 201,
          f"{r.status_code} {r.text[:150]}")

    # ---- deleting a slide unlinks its file -------------------------------
    n_before = len(files_on_disk(pid))
    c.delete(f"/api/presentations/{pid}/items/{trav}")
    check("deleting a slide removes its file",
          f"{trav}.png" not in files_on_disk(pid) and len(files_on_disk(pid)) == n_before - 1,
          files_on_disk(pid))

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S): " + ", ".join(FAILURES))
    sys.exit(1)
print("ALL PASS")
