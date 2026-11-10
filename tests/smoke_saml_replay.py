"""Regression: a SAML assertion must authenticate exactly once.

Signature validation says an assertion is genuine; it does not say it is fresh.
Until its NotOnOrAfter — minutes, with ADFS defaults — the same bytes replayed
at /saml/acs authenticate again, and that endpoint is unauthenticated by
definition. process_saml_response used to call process_response() with no
request_id at all, so python3-saml's InResponseTo check
(`if in_response_to is not None and request_id is not None`) could never fire
either: nothing tied a response to the attempt that asked for it.

This pins the half that can be tested without a live IdP — single use. The
other half (the InResponseTo binding) is exercised by a real ADFS round trip.
"""
import os
import sys
import tempfile
import time

TMP = tempfile.mkdtemp(prefix="op-samlreplay-")
os.environ.update({
    "OP_DB_PATH": f"{TMP}/onepage.db",
    "OP_UPLOADS_DIR": f"{TMP}/uploads",
    "OP_PERF_DIR": f"{TMP}/perflog",
    "OP_DEV_BYPASS_AUTH": "true",
    "OP_INITIAL_ADMIN": "admin1",
})

sys.path.insert(0, os.getcwd())
from app.database import Base, engine  # noqa: E402
from app.auth import samlreplay  # noqa: E402
from app.api import auth as auth_api  # noqa: E402

Base.metadata.create_all(bind=engine)

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


AID = "_a1b2c3d4-assertion-id-from-adfs"

check("first presentation is accepted", samlreplay.claim(AID) is True)
check("replay of the same assertion is refused", samlreplay.claim(AID) is False)
check("still refused on a third try", samlreplay.claim(AID) is False)
check("a different assertion is unaffected", samlreplay.claim(AID + "-other") is True)

# No id to key on: we cannot prove single use, so we must not pretend to —
# the assertion still had to pass signature and time-window validation.
check("empty assertion id does not block login", samlreplay.claim("") is True)

# Survives a fresh module state the way it must survive a worker restart:
# the record is in SQLite, not in a process-local set.
samlreplay._ready = False
check("replay refused after re-init (state is in SQLite, not memory)",
      samlreplay.claim(AID) is False)

# Pruning must not resurrect a live assertion, only forget expired ones.
from sqlalchemy import text  # noqa: E402
with engine.connect() as conn:
    n_before = conn.execute(text("SELECT COUNT(*) FROM saml_assertions")).scalar()
    conn.execute(text("UPDATE saml_assertions SET seen_at = :old WHERE assertion_id = :a"),
                 {"old": time.time() - samlreplay._RETENTION_S - 60, "a": AID + "-other"})
    conn.commit()
samlreplay._last_prune = 0.0
samlreplay.claim("trigger-a-prune")
with engine.connect() as conn:
    rows = {r[0] for r in conn.execute(text("SELECT assertion_id FROM saml_assertions"))}
check("expired id pruned", (AID + "-other") not in rows, str(rows))
check("live id kept", AID in rows, str(rows))
check("table does not grow without bound", len(rows) < n_before + 3, f"{len(rows)} vs {n_before}")

# The ACS reads the id from this cookie; login writes it. A rename on one side
# only would silently disable the binding, so pin the wiring.
check("request-id cookie name is shared by both halves",
      auth_api.SAML_REQUEST_COOKIE == "op_saml_req", auth_api.SAML_REQUEST_COOKIE)
check("request-id cookie is short lived", 0 < auth_api._SAML_REQUEST_TTL_S <= 15 * 60,
      str(auth_api._SAML_REQUEST_TTL_S))

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S): " + ", ".join(FAILURES))
    sys.exit(1)
print("ALL PASS")
