#!/usr/bin/env python3
"""Seed a demo workspace, so a fresh stand shows a working product.

Why this exists. A freshly started OnePage has no users besides the bootstrap
administrator, no folders and no reports: the first screen a newcomer sees is
an empty list, which says nothing about what the platform does. This script
fills that screen from fixtures that already live in the repository — no
invented data, no commercial data, nothing fetched from the network.

What it seeds:

  1. three accounts — ``admin`` (administrator), ``analyst`` (may upload) and
     ``viewer`` (read only) — and prints a ready ``OP_LOCAL_USERS=`` line with
     hashes made by ``app.auth.local.make_hash``, which is the only documented
     way to sign in with a password;
  2. a folder («Demo») with two reports: the synthetic snapshot report from
     ``tools/docs_fixtures/demo_report.html`` (invented numbers) and the
     Practicum report ``static/learn/files/delta-report.html``, which holds no
     data of its own and receives it over the ``op_ready`` -> ``op_file``
     bridge;
  3. the data slot of the second one — ``static/learn/files/delta-sales.csv``
     (21 840 deterministic rows), so the report is live and interactive rather
     than an empty tile;
  4. a navigator (``dash_type=page``) linking to both reports, so the
     «Navigators» section is not empty either.

Two transports, one code path:

  ``--url http://localhost:8000``  talks HTTP to a running stand and signs in
                                   through ``/dev-login``
                                   (``OP_DEV_BYPASS_AUTH=true``);
  ``--inproc``                     drives the app in process through
                                   ``fastapi.testclient.TestClient``, for CI
                                   with no container.

The run is idempotent: everything this script creates carries the ``Demo: ``
title prefix, and a second run removes what it recognises as its own before
creating it again — so there is exactly one copy of each, whatever happened
before. ``--reset`` performs that removal and stops.

Dependencies are stdlib plus httpx (already required by the project). Nothing
here drives a browser: the API contract is the same one `tools/docs_shots.py`
and `tools/learn_shots.py` use through Chrome
(``POST /api/dashboards`` multipart -> ``PATCH /api/dashboards/{id}``).
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# Everything this script owns is recognisable by this prefix, which is what
# makes a re-run idempotent without storing any state of its own.
PREFIX = "Demo: "
FOLDER = "Demo"

SNAPSHOT_REPORT = REPO / "tools" / "docs_fixtures" / "demo_report.html"
DELTA_REPORT = REPO / "static" / "learn" / "files" / "delta-report.html"
DELTA_DATA = REPO / "static" / "learn" / "files" / "delta-sales.csv"

R1_TITLE = PREFIX + "shelf availability"
R1_DESC = "Demo report — every figure is invented"
R2_TITLE = PREFIX + "lost sales by store"
R2_DESC = "Practicum course report — reads its data from a slot file"
NAV_TITLE = PREFIX + "navigator"
NAV_DESC = "A link hub page: one place that points at the demo reports"

USERS = [
    ("admin", "administrator", "Demo Administrator"),
    ("analyst", "superuser", "Demo Analyst"),
    ("viewer", "visitor", "Demo Viewer"),
]

DEFAULT_PASSWORD = "onepage-demo"


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

def open_client(args, stack: contextlib.ExitStack):
    """Return an httpx-compatible client for either transport.

    `TestClient` is itself an httpx client, so both branches answer the same
    calls and the seeding code below never asks which one it got.
    """
    if args.inproc:
        # Defaults only for what is missing: a CI run should not have to spell
        # out the dev door, and an operator's own env must still win.
        os.environ.setdefault("OP_DEV_BYPASS_AUTH", "true")
        os.environ.setdefault("OP_INITIAL_ADMIN", args.login)
        os.environ.setdefault("OP_CACHE_WARM", "0")
        # A DATABASE OF ITS OWN, unless the caller named one. `--inproc` is
        # advertised for CI, but run in a checkout it inherited the default
        # `data/onepage.db` and wrote accounts, a folder, three dashboards and
        # a 2.3 MB upload into the developer's real working data. A seeding
        # tool quietly editing live data is the last thing anyone expects.
        if not os.environ.get("OP_DB_PATH"):
            sandbox = tempfile.mkdtemp(prefix="onepage-seed-inproc-")
            os.environ["OP_DB_PATH"] = os.path.join(sandbox, "onepage.db")
            os.environ.setdefault("OP_UPLOADS_DIR", os.path.join(sandbox, "uploads"))
            os.environ.setdefault("OP_PERF_DIR", os.path.join(sandbox, "perflog"))
            print(f"--inproc: isolated database at {sandbox}\n"
                  f"          (set OP_DB_PATH to seed a specific one)")
        from fastapi.testclient import TestClient

        from app.main import app
        # The context manager runs the lifespan: migrations, the initial
        # administrator and the startup preflight all happen here.
        return stack.enter_context(TestClient(app))
    import httpx
    return stack.enter_context(httpx.Client(base_url=args.url.rstrip("/"), timeout=120.0))


def login(c, person_id: str) -> None:
    r = c.post("/dev-login", data={"person_id": person_id}, follow_redirects=False)
    if r.status_code != 302:
        raise SystemExit(
            f"could not sign in as {person_id!r} through /dev-login "
            f"(HTTP {r.status_code}).\n"
            "    The stand needs OP_DEV_BYPASS_AUTH=true and an existing "
            "account; OP_INITIAL_ADMIN=<id> creates one at startup.\n"
            "    Pass --login <person_id> if this stand's administrator has "
            "another id."
        )


def need(r, what: str, ok=(200, 201, 303, 302)):
    if r.status_code not in ok:
        raise SystemExit(f"{what} failed: HTTP {r.status_code} {r.text[:300]}")
    return r


# ---------------------------------------------------------------------------
# Reading the stand's own state
# ---------------------------------------------------------------------------

_DASH_DATA = re.compile(
    r'<script id="dash-data" type="application/json">(.*?)</script>', re.S)
# The navigators page renders server-side; one anchor per navigator.
_NAV_CARD = re.compile(
    r'<a class="nav-card" href="/d/([0-9a-f-]{36})">\s*'
    r'<div class="nav-card-name">(.*?)</div>', re.S)


def home(c) -> dict:
    """The home page's own JSON payload: reports, folders, presentations."""
    r = need(c.get("/", follow_redirects=False), "GET /", ok=(200,))
    m = _DASH_DATA.search(r.text)
    if not m:
        raise SystemExit("GET / carried no #dash-data payload — is this OnePage?")
    return json.loads(m.group(1))


def navigators(c) -> list[tuple[str, str]]:
    r = c.get("/navigators", follow_redirects=False)
    if r.status_code == 404:
        return []  # the section is switched off (OP_NAVIGATORS_ENABLED=false)
    need(r, "GET /navigators", ok=(200,))
    out = []
    for dash_id, title in _NAV_CARD.findall(r.text):
        out.append((dash_id, _unescape(title.strip())))
    return out


def _unescape(s: str) -> str:
    import html as _html
    return _html.unescape(s)


def ours_folders(c) -> list[int]:
    """Folders this script created. `--reset` promised to remove what an
    earlier run seeded, and used to leave the folder and the accounts behind —
    an empty «Demo» stayed on the home screen and two logins kept working."""
    try:
        r = c.get("/api/folders")
        if r.status_code != 200:
            return []
        rows = r.json()
        rows = rows.get("folders", rows) if isinstance(rows, dict) else rows
        return [f["id"] for f in rows if str(f.get("name", "")) == FOLDER]
    except Exception:
        return []


def ours(c) -> list[tuple[str, str]]:
    """Every dashboard this script recognises as its own, (id, title)."""
    found = [(d["id"], d["title"]) for d in home(c)["items"]
             if d["title"].startswith(PREFIX)]
    found += [(i, t) for i, t in navigators(c) if t.startswith(PREFIX)]
    return found


# ---------------------------------------------------------------------------
# Seeding steps
# ---------------------------------------------------------------------------

def ensure_users(c) -> list[str]:
    """Create the three demo accounts. An existing one is a 409, not a fault."""
    made = []
    for person_id, role, name in USERS:
        r = c.post("/api/admin/users",
                   json={"person_id": person_id, "role": role,
                         "display_name": name})
        if r.status_code in (200, 201):
            made.append(person_id)
        elif r.status_code != 409:
            need(r, f"creating user {person_id}")
    return made


def ensure_folder(c, name: str) -> int | None:
    r = c.post("/api/folders", json={"name": name})
    if r.status_code in (200, 201):
        return r.json()["id"]
    if r.status_code == 409:
        for f in home(c)["folders"]:
            if f["name"] == name:
                return f["id"]
        # A folder nobody can see from here (it holds nothing yet and the
        # caller is not allowed to manage folders): leave the reports at root
        # rather than fail the whole seeding over a cosmetic grouping.
        return None
    need(r, f"creating folder {name!r}")
    return None


def drop(c, dash_id: str) -> None:
    r = c.delete(f"/api/dashboards/{dash_id}")
    if r.status_code not in (200, 204, 404):
        need(r, f"deleting dashboard {dash_id}")


def _created_id(r, what: str) -> str:
    """Both upload routes answer 303 to /d/{id}."""
    need(r, what, ok=(303, 302))
    loc = r.headers.get("location", "")
    m = re.search(r"/d/([0-9a-f-]{36})", loc)
    if not m:
        raise SystemExit(f"{what}: unexpected redirect {loc!r}")
    return m.group(1)


def create_report(c, title: str, desc: str, html: Path,
                  data: Path | None = None) -> str:
    files = {"html_file": (html.name, html.read_bytes(), "text/html")}
    if data is not None:
        # The create route takes the first data slot inline, so the report is
        # never published for even a moment without its data.
        files["data_file_0"] = (data.name, data.read_bytes(), "text/csv")
    r = c.post("/api/dashboards",
               data={"title": title, "description": desc},
               files=files, follow_redirects=False)
    return _created_id(r, f"uploading {title!r}")


def publish(c, dash_id: str, folder_id: int | None) -> None:
    body: dict = {"is_published": True}
    if folder_id is not None:
        body["folder_id"] = folder_id
    need(c.patch(f"/api/dashboards/{dash_id}", json=body),
         f"publishing {dash_id}", ok=(200,))


NAV_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Demo — navigator</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ margin: 0; padding: 32px;
         font: 16px/1.5 -apple-system, "Segoe UI", system-ui, sans-serif; }}
  h1 {{ font-size: 22px; margin: 0 0 6px; }}
  p.lead {{ margin: 0 0 24px; opacity: .75; }}
  ul {{ list-style: none; margin: 0; padding: 0; max-width: 640px; }}
  li {{ margin: 0 0 12px; }}
  a {{ display: block; padding: 14px 16px; border: 1px solid currentColor;
      border-radius: 10px; text-decoration: none; color: inherit; }}
  a:hover {{ background: rgba(127, 127, 127, .12); }}
  .name {{ font-weight: 600; }}
  .desc {{ opacity: .7; font-size: 14px; }}
</style>
</head>
<body>
<h1>Demo</h1>
<p class="lead">A navigator page: the links to the demo reports gathered in
one place. A page like this is one HTML file its author writes.</p>
<ul>
  <li><a href="/d/{r1}"><span class="name">{t1}</span><br>
      <span class="desc">{d1}</span></a></li>
  <li><a href="/d/{r2}"><span class="name">{t2}</span><br>
      <span class="desc">{d2}</span></a></li>
</ul>
</body>
</html>
"""


def create_navigator(c, r1: str, r2: str) -> str | None:
    html = NAV_HTML.format(r1=r1, r2=r2, t1=R1_TITLE, d1=R1_DESC,
                           t2=R2_TITLE, d2=R2_DESC).encode("utf-8")
    r = c.post("/api/navigators",
               data={"title": NAV_TITLE, "description": NAV_DESC},
               files={"html_file": ("demo-navigator.html", html, "text/html")},
               follow_redirects=False)
    if r.status_code == 404:
        print("note: the «Navigators» section is switched off on this stand "
              "(OP_NAVIGATORS_ENABLED=false) — skipped")
        return None
    return _created_id(r, "uploading the navigator")


# ---------------------------------------------------------------------------
# Password line
# ---------------------------------------------------------------------------

def local_users_line(password: str) -> str:
    from app.auth.local import make_hash
    return "OP_LOCAL_USERS=" + ",".join(
        f"{person_id}={make_hash(password)}" for person_id, _, _ in USERS)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        description="Seed a demo workspace (users, folder, two reports, "
                    "a navigator) on a OnePage stand.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--url", help="base URL of a running stand, "
                                 "e.g. http://localhost:8000")
    g.add_argument("--inproc", action="store_true",
                   help="drive the app in process (TestClient), for CI")
    p.add_argument("--login", default="admin",
                   help="person_id to sign in as through /dev-login "
                        "(default: admin)")
    p.add_argument("--password", default=DEFAULT_PASSWORD,
                   help="password behind the printed OP_LOCAL_USERS hashes")
    p.add_argument("--reset", action="store_true",
                   help="delete what an earlier run seeded, then stop")
    p.add_argument("--with-generator", action="store_true",
                   help="not implemented: a generator bundle needs "
                        "static/pyodide or the CDN")
    args = p.parse_args(argv)

    if args.with_generator:
        print("--with-generator is not implemented: a generator bundle needs "
              "either the vendored static/pyodide subset (make assets) or CDN "
              "egress, neither of which this script may assume.",
              file=sys.stderr)
        return 2
    if not args.inproc and not args.url:
        args.url = "http://localhost:8000"
    for f in (SNAPSHOT_REPORT, DELTA_REPORT, DELTA_DATA):
        if not f.exists():
            raise SystemExit(f"fixture missing: {f}")

    with contextlib.ExitStack() as stack:
        c = open_client(args, stack)
        login(c, args.login)

        # Idempotency: whatever an earlier run left is removed first, so the
        # workspace ends up with exactly one copy of each item.
        existing = ours(c)
        for dash_id, title in existing:
            drop(c, dash_id)
        if existing:
            print(f"removed {len(existing)} item(s) from an earlier run")
        if args.reset:
            # Reports are removed above; now the folder. Accounts are NOT deleted
            # on purpose: during the stand's life analyst or viewer may have been
            # granted rights and uploaded their own work, and removing the account would take
            # someone else's work with it. This cannot be kept quiet — the leftover is reported out loud.
            gone = 0
            for fid in ours_folders(c):
                r = c.delete(f"/api/folders/{fid}")
                gone += 1 if r.status_code in (200, 204) else 0
            if gone:
                print(f"removed the «{FOLDER}» folder")
            print("reset done — accounts %s were left in place (they may hold "
                  "work of their own; delete them in /admin if you want them "
                  "gone)" % ", ".join(u[0] for u in USERS))
            return 0

        made = ensure_users(c)
        print("users: " + (", ".join(made) + " created"
                           if made else "all three already present"))

        folder_id = ensure_folder(c, FOLDER)
        r1 = create_report(c, R1_TITLE, R1_DESC, SNAPSHOT_REPORT)
        publish(c, r1, folder_id)
        r2 = create_report(c, R2_TITLE, R2_DESC, DELTA_REPORT, DELTA_DATA)
        publish(c, r2, folder_id)
        nav = create_navigator(c, r1, r2)
        print(f"reports: /d/{r1}, /d/{r2}"
              + (f"\nnavigator: /d/{nav}" if nav else ""))

        # In process there is nothing listening, but the five lines below are
        # the first thing a newcomer reads: they name the agreed dev address
        # rather than printing bare paths that go nowhere.
        base = "http://localhost:8000" if args.inproc else args.url.rstrip("/")
        print()
        print("1. Stand:     " + base
              + (" (seeded in process — start the app to open it)"
                 if args.inproc else ""))
        print("2. Accounts:  admin (administrator), analyst (may upload), "
              "viewer (read only) — sign in at "
              f"{base}/dev-login")
        print(f"3. Open:      {base}/ (two reports in «{FOLDER}»), "
              f"{base}/d/{r2} — interactive, reads the seeded CSV"
              + (f", {base}/navigators" if nav else ""))
        print("4. Off by default (OP_EXPLORE_ENABLED=true) and needs `make assets`: the «Explore» tab (/explore) and "
              "the report chat pane — they load the vendored browser engine "
              "(duckdb-wasm / pyodide), which is not in git.")
        # The Docker path does not read .env: docker-compose.yml has no env_file, and
        # only the explicit `environment:` keys reach the container. The previous
        # wording advised putting the line into .env and promised a login
        # that would never happen.
        print("5. Password sign-in: add the line below to the `environment:` "
              "block of docker-compose.yml (doubling every $ as $$), or to "
              ".env if you run without Docker (`make run`); restart, then "
              f"sign in at {base}/login with the password {args.password!r}.")
        print()
        print(local_users_line(args.password))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
