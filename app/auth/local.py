"""Password sign-in for stands OUTSIDE the corporate perimeter (OP_LOCAL_USERS).

The product authenticates through ADFS only; a demo host on the public
internet has no IdP, and the password-less dev bypass must never face it.
This is the third door: a fixed list of accounts with password hashes,
carried in one environment variable so nothing new touches the database.

    OP_LOCAL_USERS="admin1=pbkdf2_sha256$600000$<salt>$<digest>,tmr1=..."

Empty (the default) keeps the route and the form invisible. A hash is made
with `python -m app.auth.local hash` — never store the password itself.
Only accounts that already exist in `users` can sign in: the list grants a
password, not an account (same rule as the dev bypass).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sys

ALGO = "pbkdf2_sha256"
ITERATIONS = 600_000
# Verified against when the account is unknown, so a wrong name costs the
# same time as a wrong password and the list cannot be enumerated by timing.
_DUMMY = f"{ALGO}$1000${'00' * 16}${'00' * 32}"


def make_hash(password: str, iterations: int = ITERATIONS) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations)
    return f"{ALGO}${iterations}${salt}${digest.hex()}"


def verify(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt, digest = stored.split("$")
        if algo != ALGO:
            return False
        got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(got.hex(), digest)
    except (ValueError, TypeError):
        return False


def parse(spec: str) -> dict[str, str]:
    """`id=hash,id=hash` → {id: hash}. Malformed entries raise, so a typo in
    .env fails loudly at the first sign-in attempt instead of locking one
    account out silently."""
    out: dict[str, str] = {}
    for raw in (spec or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        person_id, sep, stored = raw.partition("=")
        person_id = person_id.strip()
        if not sep or not person_id or stored.count("$") != 3:
            raise ValueError(f"OP_LOCAL_USERS: malformed entry for {person_id or raw[:20]!r}")
        out[person_id] = stored.strip()
    return out


def enabled() -> bool:
    from app.config import settings
    return bool(settings.local_users.strip())


def check(person_id: str, password: str) -> bool:
    """Constant-time-ish: unknown names still pay for one PBKDF2 round."""
    from app.config import settings
    users = parse(settings.local_users)
    stored = users.get(person_id)
    if stored is None:
        verify(password, _DUMMY)
        return False
    return verify(password, stored)


def _main(argv: list[str]) -> int:
    if len(argv) >= 1 and argv[0] == "hash":
        if len(argv) >= 2:
            pw = argv[1]
        else:
            import getpass
            pw = getpass.getpass("password: ")
            if pw != getpass.getpass("again: "):
                print("passwords differ", file=sys.stderr)
                return 2
        print(make_hash(pw))
        return 0
    print("usage: python -m app.auth.local hash [password]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
