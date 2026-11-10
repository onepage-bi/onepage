"""op-agent — localhost agent #2 of the OnePage platform (engine for GreenPlum).

Listens on 127.0.0.1 ONLY. Executes the frozen SQL of an immutable generator
bundle under the analyst's PERSONAL read-only GP credential, runs the generator
natively and hands results to the conductor page. Holds no OnePage secrets and
never calls the OnePage server (boundary C5). Full design — docs/RFC_AGENT_GREENPLUM.md.

M1 — health / pairing / echo-run / NDJSON stream; M2 — GP executor (gpsource) and
native generator execution (runs); M3 — offline packaging and DPAPI credentials;
M4 — tray icon instead of a console and click-to-confirm pairing.
"""

__all__ = ["VERSION", "PROTOCOL", "APP"]

APP = "op-agent"
# Agent release version (semver string). Grows with every exe/code release.
VERSION = "0.3.0-m4"
# Page<->agent protocol version. Grows ONLY on an incompatible change of the
# HTTP contract — the page checks it in /health and asks to update the agent.
# 2: confirmed pairing (/pair/request returns mode+request_id, /pair/status).
PROTOCOL = 2
