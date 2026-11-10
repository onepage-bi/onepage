#!/usr/bin/env python3
"""A report as a single file — from the command line (same as the button in "Versions").

The usual way is the "Download as a single file with data" button in the "Versions" panel
(`GET /api/dashboards/{id}/standalone`). This script exists for exactly two
cases where there is no button: the platform is down, or files must be built in bulk.

The build is the same: `app/api/files.build_render_body` + `app/services/
standalone.build`. There is deliberately NO copy of the bridge here — two builds
would drift apart silently, and that is exactly the mistake we have already made.

Run it WHERE THE DATA LIVES; it sends nothing anywhere:

    docker exec onepage-onepage-1 python -m tools.standalone_export \\
        <dashboard-id> --out /tmp/report.html

    # fetch it locally without leaving a file on the server:
    ssh <host> 'docker exec onepage-onepage-1 python -m tools.standalone_export \\
        <id> --out /dev/stdout' > ~/Downloads/report.html

The script does not parse or print slot contents — only their length.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api.files import build_render_body            # noqa: E402
from app.database import SessionLocal                  # noqa: E402
from app.i18n import _                                 # noqa: E402
from app.models import Dashboard                       # noqa: E402
from app.services import standalone                    # noqa: E402


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dashboard_id")
    ap.add_argument("--out", type=Path, required=True,
                    help="where to put the file (/dev/stdout — write to the stream)")
    a = ap.parse_args(argv)

    db = SessionLocal()
    try:
        dash = db.query(Dashboard).filter(Dashboard.id == a.dashboard_id).first()
        if not dash:
            print(f"!! no report {a.dashboard_id}", file=sys.stderr)
            return 1
        # Same rule as the endpoint: ADR-18 forbids an artifact with rows
        # without a filter, and it cannot be bypassed via the console.
        if dash.rls_enabled:
            print("!! the report has row-level access (RLS) — it is never exported "
                  "as a single file", file=sys.stderr)
            return 2
        body, _extern = build_render_body(dash, snapshot_external=False)
        doc, notes = standalone.build(
            dash, body,
            # Same keys as the endpoint: the default language is English, and
            # the script must produce byte for byte what the endpoint gives without a cookie.
            t_no_gzip=_('This browser is too old: no DecompressionStream.'),
            t_failed=_('The data embedded in this file did not load: '))
    finally:
        db.close()

    for n in notes:
        # Records are structures (they also go to the audit log), so we print them as
        # they are rather than joining them into a string: joining is exactly where the script broke.
        print("   " + json.dumps(n, ensure_ascii=False), file=sys.stderr)
    size_mb = len(doc) / 1048576
    if len(doc) > standalone.MAX_BYTES:
        print(f"!! got {size_mb:.1f} MB against a limit of "
              f"{standalone.MAX_BYTES / 1048576:.0f} MB", file=sys.stderr)
        return 3
    a.out.write_bytes(doc)
    print(f"==> {a.out}  ({size_mb:.1f} MB)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
