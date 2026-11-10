"""OnePage — offline summary of requirements.txt: package / pinned version /
known (to the model) release date of that version. NO network — this is not a replacement
for pip-audit (see tools/pip_audit.sh) but a quick memory at hand: "what are we
actually running on and when did it come out", when the server is offline and the proxy blocks
PyPI.

The date is what the script itself KNOWS (the static table below), not what it
checked just now: for a version missing from the table it honestly prints
"no data" rather than a guess.

    python3 tools/deps_report.py [requirements.txt]
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# package (normalised name, PEP 503: '-'/'_'/'.' -> '-', lower) -> {version: "YYYY-MM-DD"}.
# Filled in as knowledge allows — does not claim completeness. "extras" in the
# package name (uvicorn[standard]) are not part of the key; they are stripped while parsing.
KNOWN_RELEASE_DATES: dict[str, dict[str, str]] = {
    "fastapi": {"0.115.5": "2024-11-19"},
    "uvicorn": {"0.32.1": "2024-11-01"},
    "jinja2": {"3.1.6": "2025-03-05"},
    "python-multipart": {"0.0.18": "2024-12-16"},
    "sqlalchemy": {"2.0.36": "2024-10-14"},
    "pydantic-settings": {"2.6.1": "2024-11-05"},
    "python-dotenv": {"1.0.1": "2024-01-23"},
    "aiofiles": {"24.1.0": "2024-06-24"},
    "httpx": {"0.28.1": "2024-12-06"},
    "openpyxl": {"3.1.5": "2024-06-28"},
}


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def parse_requirements(text: str) -> list[tuple[str, str, str]]:
    """Return [(name_as_in_file, extras, version), ...] for lines like
    `pkg==1.2.3` / `pkg[extra]==1.2.3`. Comments and blank lines are skipped;
    a line without an exact `==` is printed as is with version "?" (the
    repo always pins, so this is a signal, not a normal case)."""
    out = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.match(r"^([A-Za-z0-9_.-]+)(\[[A-Za-z0-9,_-]+\])?\s*==\s*([A-Za-z0-9_.+-]+)\s*$", line)
        if m:
            out.append((m.group(1), m.group(2) or "", m.group(3)))
        else:
            out.append((line, "", "?"))
    return out


def main(argv: list[str]) -> int:
    req_path = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parents[1] / "requirements.txt"
    if not req_path.exists():
        print(f"[BAD] not found: {req_path}", file=sys.stderr)
        return 1

    rows = parse_requirements(req_path.read_text(encoding="utf-8"))
    if not rows:
        print(f"(empty: {req_path})")
        return 0

    w_pkg = max(len(f"{n}{e}") for n, e, _ in rows) + 2
    w_ver = max(len(v) for _, _, v in rows) + 2

    print(f"== {req_path} — {len(rows)} packages (offline, no network) ==\n")
    header = f"{'PACKAGE':<{w_pkg}}{'VERSION':<{w_ver}}RELEASE DATE (known to the script)"
    print(header)
    print("-" * len(header))
    known = 0
    for name, extras, ver in rows:
        date = KNOWN_RELEASE_DATES.get(_norm(name), {}).get(ver, "no data")
        if date != "no data":
            known += 1
        print(f"{name + extras:<{w_pkg}}{ver:<{w_ver}}{date}")

    print(f"\n{known}/{len(rows)} versions with a known date in this script's table.")
    print("Vulnerability check (needs network) — tools/pip_audit.sh.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
