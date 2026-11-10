# Performance decisions (ADR)

A registry of decisions made on the basis of measurements from the **C0–C5**
benchmark campaigns (bench: KVM VM, Intel Xeon Gold 6154 @ 3.00GHz, 4 vCPU, Ubuntu, kernel 7.0.0-15).
Every decision references a specific `run_id`: numbers are not quoted from
memory and are not re-estimated without a new run.

Primary sources (do not duplicate numbers here, only reference them):
- `bench/REPORT_RU.md` — the final report, tables 1–2, budget attribution
- `bench/RUNBOOK.md` — protocol, expectations table, sections C4/C5
- run archive: `runs/<run_id>/manifest.json` (payload sha256 + repo SHA)

---

## How to read these numbers

**Validity gates** (a cell is accepted only if all of them hold):
`errors = 0`, rate deviation ≤ ±1%, `send_lag p99 < 100 µs`, `steal < 0.5%`.
Each cell is the median of **3 VALID** runs. Spread rule: the p99
discrepancy between runs is < 10% OR < 100 µs absolute.

**Three caveats without which the numbers lie:**

1. **serve spans are NOT comparable across rungs.** The semantics differ: where the body
   is written inside the span it is 30–47 µs, where it is not, 1.0–2.6 µs. Rungs can be
   compared **only by RTT**. The same restriction applies to any future
   work with `sendfile` (the body leaves outside the span; see ADR-3).
2. **The medium is loopback.** A real network adds so much that it swamps
   almost any gain from application optimisation (ADR-9).
3. **Bench ≠ production.** OnePage's production reference point is 216 µs serve p50 versus
   6.8 µs on bare@t2: a ~30× difference, and it is about the execution environment (shared vCPU +
   Docker), not about the code.

---

## ADR-1 — Adaptive perflog sampling · **ACCEPTED**

**Problem (measured).** With `OP_PERF_SAMPLE=1.0` and ~2 krps, the perflog daemon
writer serialises a batch in a single `json.dumps` pass and holds the GIL long
enough to stall the event loop: **RTT p99 9.27 ms versus 0.73 ms with
telemetry off**. This is not theory — it is amendment #2 of campaign C1: table 1 had to be
rerun entirely at 1000 rps, because at 2000 rps the writer broke the
measurement bench itself. Before P1, `docker-compose.yml` had exactly `OP_PERF_SAMPLE:
"1.0"` — meaning the same mine was lying in production.

**Decision.** A control loop in the writer thread: an EWMA (α=0.3) estimate of the true
rps from its own batch size → `eff = clamp(TARGET/est_rps, floor, ceiling)`
→ published into a module-level float that the loop thread reads. The fixed point is
write rate = `OP_PERF_TARGET_RPS`. Every written line is stamped with the
probability with which it survived (`"sample"`), and `/admin/perf` reconstructs
unbiased counters and percentiles with weight `1/sample` (Horvitz–Thompson).

**Invariants that must not be broken by edits:**
- The stamp must equal the gate's decision — hence **one** read of
  `effective_sample()` per request.
- `_eff_sample` is written **only** by the writer thread (the `_runtime_on` convention);
  loop threads only read it.
- Responses `>= 400` are emitted **always** and **without a stamp**: their probability
  of inclusion is 1, so their weight is 1. A stamp on them would give each error an
  expected value of `2 − eff` instead of 1, and without the forced emit a rare 5xx on a loaded
  server would vanish from `/admin/errors` with probability `1 − eff`.
- Records without a `sample` field (old JSONL, client beacon, GC) get weight 1 —
  backward compatibility rests on this and nothing else.

**Second line of defence.** Serialisation in sub-batches of 1000 records with a short
GIL release between them — in case the queue does grow after all
(adaptive is off, the floor is raised).

**Settings.** `OP_PERF_ADAPTIVE` (true), `OP_PERF_TARGET_RPS` (200.0),
`OP_PERF_SAMPLE_FLOOR` (0.01). `OP_PERF_SAMPLE` is **reinterpreted as a ceiling** —
old configs keep working, but the value is now read as an "upper
bound" rather than a "fixed probability".

**Verification status: PARTIALLY PASSED.** Offline — clean: synthetic 2×100k
records are reconstructed by the weighted summary within ±0.64% on count, ±2% on
p50/p95, ±5% on p99.

Pre-registration (fixed BEFORE the runs, cell `l0 t2 40k r2000 c16 120s`,
chdriver, 3×VALID; the discriminating rate is exactly 2000 — at r1000 the pathology does not
show, which is why C1 "hid" it by rerunning its table at 1000):

| Arm | Configuration | Expected | Obtained | Verdict |
|---|---|---|---|---|
| A / A2 | telemetry off | p99 ≈ 0.73 ms (floor) | 0.740 / **0.778** | ✅ floor reproduced |
| B | `adaptive=false`, `sample=1.0` | pathology: p99 ≈ 9.27 ms and/or gate failure | **9.626**, 0/3 VALID, `send_lag_p99` failure (1474–1688 µs against a limit of 100) | ✅ reproduced |
| C | `adaptive=true`, TARGET=200 | **p99 ≤ 0.80 ms**, send_lag VALID, errors=0, eff ∈ [0.08, 0.12] | 1.526, 3/3 VALID, eff **0.1011** | ⚠️ gates ✅, **p99 ✗** |
| D | same with `_WRITE_CHUNK=64` | diagnosis check: p99 ≤ 0.80 | 1.452, 3/3 VALID, eff **0.1008** | ⚠️ **p99 ✗** |

Runs: `20260720T0725/0729/0733…-p1a`, `…0828/0832/0836…-p1a2`,
`…0738/0742/0746…-p1b`, `…0750/0754/0759…-p1c`, `…0815/0819/0823…-p1d`
(onepage `6cfee68`, for D `de5bafd`; kernel 7.0.0-27 — **not the one C0–C5
ran on**, so arm A serves as the current floor, and the historical 0.73 is
only a cross-check).

**What is proven.** The pathology was reproduced and eliminated: B never passes the gates,
C and D pass all three times. The control loop settles exactly at the
design point (eff 0.1008–0.1011 against a target of 200/2000 = 0.1, write rate
~213 rec/s versus TARGET 200). The invariant "errors are never stamped" —
0 violations over 240,000 requests.

**1800 s soak — VALID, 3.6 M requests** (`20260720T084311-…-p1soak`, sha
`de5bafd`): errors 0, rate on target, send_lag p99 = 1 µs, steal 0.227%. It
existed for one question — the only design risk registered
without an engineering safeguard ("damp the EWMA, don't complicate it before there is data"). The loop
**does not self-oscillate**: the median `sample` across 15 two-minute bins stays within
0.0991–0.1015 (a 2.3% range), not a single bin is pinned to the floor or the ceiling,
write rate 200.2 rec/s versus TARGET 200 over half an hour, `serve_us` p50
6.0 → 6.0 µs with no drift. No need to complicate the loop. `p9999` 50 ms / max
100 ms is the signature of neighbour steal on a shared vCPU (in the C1 soak it was 5.7 / 88.5),
a cost of the medium, not of the code.

**What was NOT achieved, and why this is not a defect.** The pre-registered
p99 ≤ 0.80 ms failed twice. The first diagnosis — the monolithic `json.dumps`
of the batch — was **refuted**: `_WRITE_CHUNK` 1000 → 64 moved p99 by only 74 µs
(with a spread of 123), i.e. within noise. But it halved the extreme tail
(p999 8.8 → 4.6 ms), so it was kept.

What the server telemetry says firmly: the tail lives **not in the handler**. In arm
D, **`hdr_ms` = 26 µs at both p99 and p999** — time to headers does not grow
along the tail at all; out of 25,636 sampled records only one exceeded 2 ms. The request waits
for the loop to free up, and is then served in 26 µs. This is a loop stall.

**Mechanism established: continuous GIL contention, proportional to the load of the
writer thread.** Not discrete stalls once per flush cycle — that wording
came first and had to be withdrawn, because it fails an order-of-magnitude
check: the writer wakes ~60 times per run, one stall hits 3–4
requests, so all 60 give at most ~240 affected — that is **p999, not p99**
(p99 at 2000 rps over 120 s = 2,400 requests). Continuous contention also explains
what the stalls did not: as long as the writer has something to do, it regularly
grabs the GIL, and the loop gets it back late — many requests are hit,
not a rare few.

The decisive check is arm F (`TARGET=1`, floor 0.0001 ⇒ eff ≈ 0.0006, ~1.5
records/s, the writer is effectively idle while the per-request middleware path
works as usual). **p99 = 0.794 ms against a floor of 0.778 — at floor level.**
The pre-registration was written before the run: "F ≤ 0.85 ⇒ there is no constant, the per-request
hypothesis is dead". It is dead.

Hypotheses rejected along the way (listed so that nobody resurrects them):
* **Monolithic `json.dumps`** — `_WRITE_CHUNK` 1000 → 64 moved p99 by 74 µs with a
  spread of 123. But it halved p999 (8.8 → 4.6 ms), so it was kept. This is precisely
  the signature of the mechanism: small slices reduce the maximum single GIL hold
  (p999) without changing the total contention (p99).
* **Per-request allocations → gen0 collections.** Killed twice and independently: by arm F and
  by a local probe of `PerfLogMiddleware` (20,000 synthetic requests) —
  the always-on path costs **1.87 µs per request**, gen0 fires **once per
  second** scaled to 2000 rps, whereas the hypothesis needed ~14/s.
* "Zero GC records" as evidence at all: `gcwatch` emits only gen ≥ 1 and collections
  ≥ 1 ms, it never writes gen0. A reading from an instrument not tuned to the suspect.

**Transfer function.** The cost is linear in write rate across almost three orders of magnitude:

| Arm | records/s | p99 above floor | µs per (record/s) |
|---|---|---|---|
| F | 1.5 | +16 | — (noise) |
| E2 | 26 | +119 | 4.6 |
| D | 213 | +674 | 3.2 |
| B | 2000 | +8848 | 4.4 |

**p99 ≈ 3.5 µs for every record per second.** `OP_PERF_TARGET_RPS` is a direct
lever: TARGET=200 costs ~674 µs of tail at 2000 rps, TARGET=20 ~119.

**Resolution limit.** Arms E2 and F failed spread rule #3 (21.6% and 24.7%,
both ~195 µs), and at this scale the spread is comparable to the effect itself — the F runs
overlap with the floor completely. Below ~26 records/s the effect is **not resolvable**
on this medium. This is the instrument's limit, not zero.

**Decision.** The remainder is not "accepted for lack of anything better" — it is now
**a deliberate trade-off at a known rate**: statistical resolution of the summary
versus the tail. The default `TARGET=200` stays; a tail-sensitive deployment
lowers it and pays with higher variance of the weighted estimates (which grows
as 1/eff). The earlier wording "chasing it is disproportionate, a separate
process is needed" is withdrawn: a separate process would remove GIL contention entirely, but under
OnePage's working loads the effect already lies below this bench's resolution threshold.

---

## ADR-2 — Production deploy on host networking · **ACCEPTED**

**Numbers (C1, table 1, payload 40k, r1000, tuning t1):**

| Environment | RTT p50 | RTT p99 | run_id |
|---|---|---|---|
| systemd-bare | 0.350 ms | 0.613 ms | `20260717T201328-l0-t1-40k-r1000-bare-systemd` |
| docker-hostnet | 0.374 ms (+7%) | 0.659 ms (+7%) | `20260717T204747-l0-t1-40k-r1000-docker-hostnet-r2` |
| docker-bridge | 0.470 ms (+34%) | 0.866 ms (+41%) | `20260717T203600-l0-t1-40k-r1000-docker-bridge-r2` |
| podman-rootless | 0.468 ms (+34%) | 0.962 ms (+57%) | `20260717T200227-l0-t1-40k-r1000-podman-rootless` |

Cost of the layer in the budget attribution: veth/NAT (hostnet) **+0.024 ms p50**,
docker-proxy (bridge) **+0.120 ms p50**. Moving bridge → hostnet ≈
**−20% p50 / −24% p99**. podman-rootless loses to bridge on the tail (pasta) —
it is not considered as a replacement.

**Decision.** `network_mode: host` instead of a published port. Operational details,
preconditions (Linux-only), reverse proxy, smoke test and rollback — `docs/DEPLOY.md`.

**SCOPE OF APPLICABILITY (important, easy to quote in the wrong place).** The
`docker-bridge` cell measured **the path through a published port**, i.e. `docker-proxy`
— userland forwarding for `127.0.0.1` binds. This is written both in the environment
switch script (`provision/onepage/l0_env.sh`: "docker-bridge on LOOPBACK
measures the published-port path… The veth/iptables path proper is a C3 topic"),
and in the budget attribution line, where the layer is named `docker-proxy (bridge)`.
The **container-to-container path via veth/iptables was never measured**: C3 went off into
the topic of inter-datacentre networking and never got to it.

Practical consequence: if the application is fronted by a reverse proxy **in a
container** (Traefik with the Docker provider, containerised nginx), it reaches
OnePage over the Docker network, `docker-proxy` is not on the path, and **the +34% / +41%
numbers do not apply to such a topology**. There is no need to publish a port there at all, and host
networking additionally breaks label-based discovery — a host-mode container has no address
on the Docker network. For such sites the standard path is plain bridge plus labels
(`deploy/bundle/docker-compose.traefik.yml`), not host networking.

How much cheaper veth is than host networking is **unknown**. Almost certainly the
difference is smaller than the docker-proxy delta (veth is entirely in the kernel, docker-proxy is a
user-space process), but that is reasoning, not measurement. If a
number is needed, it is a cell on the bench, not a subject for argument.

**Registered tech debt:** ProxyHeaders is not configured, so `ip` in the
perf logs is the proxy's address, not the client's. Out of scope for P2.

---

## ADR-3 — sendfile for disk-served large bodies · **DEFERRED**

**What was measured (C5).** `sendfile` cuts the server span by **2.8×** (2.58 → 0.93 ms
on a 5 MB body). This is a real win, but with three limiting factors.

**Why deferred rather than done right away:**
1. **Narrow applicability.** Render paths are served from an in-memory LRU — `sendfile`
   does not apply there by construction. There are only two honest candidates:
   `serve_slot_download` (the identity and pre-compressed passthrough branches) and
   `serve_slot_asset` (it has no LRU cache of variants — the best candidate).
   The decompress branch stays in userspace in any case.
2. **The kTLS part of the win is architecturally unavailable** — see ADR-8.
3. **The network eats the win cross-DC** (+1.75–1.79 ms, ADR-9). The value
   lies in offloading the CPU and the event loop, not in user-facing latency.

**Caveat for a future run:** after switching to `FileResponse` the server span
will be **incomparable** with the current one (the body is written outside the span) — compare only RTT
and CPU consumption. A dedicated fixture for the download route is needed
(`/render/{id}/files/{slot}`, 5 MB body, guaranteed NOT in the LRU): the campaigns' standard
payloads hit the render path. Pre-registration: cell `l0 t2 5m r100`,
3×VALID, expectation — server span ~2.8× down.

---

## ADR-4 — Rewriting in C/Rust · **DEFERRED, NOT REJECTED**

The wording matters: the data does not say "C is useless" — it says "the language costs
almost nothing, while the thickness of the stack costs a lot". These are different conclusions, and the second
points to a specific cheap step.

**Budget attribution (C2, bare@t2, payload 40k, r2000), Δ RTT p50:**

| Transition | What this layer is | Δ p50 |
|---|---|---|
| L0 full stack → L1 asyncio | web framework + full stack, **same Python** | **−0.208 ms** |
| L1 asyncio → L2 uvloop | event loop in C | ≈ 0 |
| L2 uvloop → L3 Rust/axum | language (Rust) | −0.005 ms |
| L3 Rust/axum → L4 C/epoll | language + runtime (C) | −0.016 ms |

**Changing the language costs ≈ 0.021 ms in total.** By contrast, "removing stack thickness — 0.208 ms,
ten times more" was the wording of the first revision of this ADR, and it has been **withdrawn**:
C2 compared OnePage with a toy that serves a fixed payload and never
broke that delta down. Campaign C6 did break it down (`l0` / `l0b_uvicorn` /
`l1_asyncio`, the same cell `t2 40k r2000`, p50 medians):

| Bucket | Δ p50 | What it is |
|---|---|---|
| **uvicorn access log** | **91–96 µs** | one config line; measured twice independently (`OP_ACCESS_LOG` on the full app and `CH_ACCESS_LOG` on the bare rung), agreed within 5 µs |
| OnePage's own stack | 32 µs | cookie parsing, two auth TTL caches, fingerprint, LRU — **useful work that any front end will have to do** |
| **uvicorn layer** | **75 µs** | the only thing a minimal front end would actually take away |
| closure (l0 − l1) | 198 µs | reproduces C2's 0.208 ms within 5% on a different kernel |

**Hence the honest price of step 1: ≈ 75 µs, not 208.** Against the language delta of
21 µs that is a factor of 3.5, not 10. And 91–96 µs of those 208 are recovered with **one config
line** — without a minimal front end, without owning our own HTTP/1.1 implementation and
without its class of bugs. With a metro network at +1750 µs (ADR-9), the remaining 75 µs is
an argument about CPU offloading and tails under load, not about perceived speed.

**Caveats that must accompany any citation.** The "uvicorn layer" here is uvicorn
**as we run it**: the 75 µs include its `server:`
and `date:` headers (`l1` has none) and a response in two ASGI messages versus a single
`transport.write` with the headers glued on. This is the right number for the decision
"whether to move away from uvicorn as we run it", but **not** for "the uvicorn parser costs
75 µs". Data status — see ADR-10: the cells failed the steal gate through a mechanism
they themselves generate; the direction is robust, the campaign is not closed.

The full-stack (L0) tail — p99 **1.22–1.46 ms, noise-bound**: it is determined by
the noise of the medium, not by the code. The minimalist rungs give stable p99s of
0.554–0.568 ms — and this value does not differ between L1 (Python) and L4 (C).
Capacity grows from 5000 rps (L0, the server ceiling) to 24.2k / 27.8k / 28.5k / 35.6k
(L1/L2/L3/L4 @ r40000, wrk2 c64) — but OnePage is an internal tool, and three orders of magnitude
separate the 5000 rps ceiling from the actual workload.

run_id: `20260718T195542-l0-t2-40k-r2000-cmp40k-r2`,
`20260718T084845-l1_asyncio-t2-40k-r2000-cmp40k`,
`20260718T091144-l2_uvloop-t2-40k-r2000-cmp40k`,
`20260718T204458-l3_rust-t2-40k-r2000-cmp40k-r2`,
`20260718T215049-l4_c-t2-40k-r2000-cmp40k-r2`.

**Registered path back** (so as not to decide again from scratch):

1. **Step 1 — "minimal front end".** The warm render hot path on a minimal
   Python listener bypassing uvicorn: fast-lane logic (cookie → auth TTL caches
   → prepared response from the LRU), misses are proxied to FastAPI.
   Pre-registration based on C2: RTT p50 ~0.11 ms (−210 µs versus current), p99 ~0.55 ms
   stable. These are exactly those 0.208 ms — the cheapest large win
   of all those measured.
2. **Step 2 — only if step 1 was not enough:** a C front end modelled on `l4_c`.
   Measured gain over the Python minimum: −15…−20 µs p50.
3. **A full rewrite of the application** is evaluated only after steps 1–2.
   The direct data argues against it: language ≤ 0.02 ms, network +1.8 ms dominates.
   Revisit with new numbers in hand, not before.

Verification of any step — the same bench, the same gates, 3×VALID, pre-registration
before the run.

---

## ADR-5 — QUIC · **REJECTED**

QUIC on a Python stack hits a transport ceiling of **~150–172 rps** (C5,
14-counter gate — RUNBOOK, section C5). This is below the current workload
and one and a half orders of magnitude below the ceiling of the existing HTTP path (5000 rps). The question
is closed until a non-Python QUIC terminator appears, that is, no earlier than ADR-4 step 2.

---

## ADR-6 — Further OS tuning beyond t2 · **REJECTED**

The C1 winner on the tail is **t2 (pinning + IRQ affinity)**: RTT p50 0.349 /
p99 0.594 ms (`20260717T211719-l0-t2-40k-r1000-bare-systemd`); within
the spread it equals t1 on the median and beats it on p99.

**t3 (isolcpus) trades the median for the tail, and it is a bad trade:** p50 −18%
(0.287 ms), but **p99 +69%** (1.037 ms) — `20260718T041228-l0-t3-40k-r1000-bare-systemd-r2`.
For an interactive web tool, where the user feels precisely the tail,
this is a regression, not an improvement.

**t4 (THP off)** matched t3 (0.298 / 1.051) — under a steady-state load THP
changes nothing: `20260718T041838-l0-t4-40k-r1000-bare-systemd`.

From C4: **io_uring** ≈0 for latency (with 1 recv + 1 send there is nothing to amortize),
but +3.3% throughput at the ceiling (37.8k → 39.0k @r40000); **SCHED_FIFO** ≈0
(p999 worse); **SO_BUSY_POLL** — an honest no-op on loopback (KVM without SR-IOV;
kernel bypass cannot be tested on this medium at all).

---

## ADR-7 — Disabling mitigations · **REJECTED**

`mitigations=off` yields a systematic **≈3%** (srv p50 −0.98 µs) — C4. This is
the tax for protection against speculative-execution attacks, and it is worth paying. It should not be switched off
anywhere except on an isolated benchmark bench, where this is done for the sake of a clean
comparison, not for speed.

---

## ADR-8 — kTLS · **NOT APPLICABLE (not our audience)**

The key conclusion of C5: **kernel crypto ≠ a win; the win = zero-copy.**
kTLS **without** sendfile is **worse** than userspace TLS: 8.34 vs 7.44 ms p50 on a
5 MB body. kTLS **together with** sendfile is the best TLS path: 5.94 ms (−20%).

For OnePage this is architecturally inapplicable: TLS is terminated by an external reverse proxy
(ADR-2), the application serves plaintext. The audience for this conclusion is whoever
configures the terminator, not the OnePage codebase. The only thing available to the application is
plain sendfile on ready-made bodies — ADR-3.

For reference: TLS on a 40k body costs +56 µs p50.

---

## ADR-9 — Context that any future optimization must take into account

Two numbers that set the scale for everything else:

**The environment dominates by ~30×.** The production reference point is 216 µs serve p50 versus 6.8 µs
on bare@t2 (`20260717T211719-l0-t2-40k-r1000-bare-systemd`). The difference is shared
vCPU + Docker, i.e. the execution environment, not the code. Before optimizing
microseconds in the application, it is worth checking whether it is losing hundreds of microseconds
in the environment (hence the priority of ADR-2).

**The network between DCs adds +1.75–1.79 ms p50** on a 60 KB body (C3-marked, closed-loop judge,
p50 only — amendment #15; two cells agreed within 2%;
`runs/20260719T2030-c3marked/`). The route class is LAN/metro, not a VPC switch.
Any application optimization smaller than a millisecond on a cross-DC request
is invisible to the user. At the same time, the l0 − l4_c delta **survives** the network
(614 vs 650 µs) — that is, it remains measurable, it just stops being
noticeable.

Practical conclusion: work at the "hundreds of microseconds" level (ADR-4 step 1) makes sense
for CPU offloading and tails under load, not for the speed a user perceives
on a remote client.

---

## ADR-10 — uvicorn access log off by default · **ACCEPTED**

`entrypoint.py` called `uvicorn.run()` with all defaults, which means
`access_log=True`: for every response a line is formatted and **synchronously** written
through Python logging right inside the event loop — to journald on the bench and to the
Docker log driver in production. Every warm render hit pays for it.

The cost was measured, not estimated: **91–96 µs RTT p50** at 2 krps. Twice and
independently — with the `OP_ACCESS_LOG` flag on the full application and `CH_ACCESS_LOG` on the
"bare uvicorn + minimal ASGI" rung; two different mechanisms, two different
codebases, a 5 µs discrepancy. This is **about 45%** of the entire 0.208 ms delta that was used
to justify rewriting the stack (ADR-4).

Nothing is lost: the reverse proxy in front of the application keeps its own access log,
so this was duplicate bookkeeping on the hot path. `OP_ACCESS_LOG=true`
restores the behaviour — sensible if the application runs without a proxy.

**Data status.** The C6 cells failed the steal gate (see ADR-11), so the
91–96 µs figure is **measured, but did not pass the campaign gates**. The decision was made
anyway, and here is why this is not curve-fitting: the steal artifact works **against** the
conclusion. The "fast" cells ran at 0.8–1.4% steal, the "slow" ones at 0.23–0.35%;
if steal is CPU that was genuinely taken away, the fast ones were measured with a handicap and without it would have been
even faster, i.e. 91 µs is a lower bound. Plus the spread of medians within cells is
1.4–6%, and the steal gate hits the tails, not the median.

## ADR-11 — The steal gate must not be applied where the effect correlates with idling · **METHODOLOGICAL**

C6 could not validate its own hypothesis, and this is a defect of the gate, not of the
campaign. The observation — nine runs, not a single exception:

| cell | p50 | steal | VALID |
|---|---|---|---|
| c6l0log | 0.316–0.324 | 0.23–0.33 | 3/3 |
| c6ublog | 0.288–0.292 | 0.23–0.35 | 3/3 |
| c6l0nolog | 0.218–0.231 | 0.77–1.35 | 0/3 |
| c6ubnolog | 0.194–0.204 | 0.77–1.33 | 0/3 |
| c6l1 | 0.118–0.122 | 0.84–1.44 | 0/2 |

A noisy-neighbour wave would hit the cells randomly, by time. Here steal
**sorted the cells strictly by speed**: the faster the configuration, the higher
its steal and the more surely it fails the gate. On an idle machine steal is 0.15% —
there was no wave. This is the HLT tax: an efficient server more often drops into idle and
back, and the hypervisor books that as steal. The mechanism is already documented
by amendment #6 of campaign C2 ("steal is flagged but does not gate; the HLT tax penalizes
an efficient server") — there it was noticed on throughput probes, here it hits the
latency cells.

**Rule for the future:** if the measured effect is a *speed-up*, the steal gate is
structurally biased against the hypothesis, and it must be **flagged, not gated**.
`errors`, `rate` and `send_lag` remain gates without change.

**Why C6 was not re-run.** Loosening the gate after seeing which runs
failed it is post-hoc, exactly what the discipline exists to prevent.
The honest form is to register the amendment and re-run all cells from scratch under
it. The re-run was deliberately postponed: it would not have shifted a single verdict (the thresholds
of the decision rule sit 1.5–3 times farther away than the observed values), and it would have cost
an hour and a half of bench time. The price of the decision — the C6 numbers are published with the status
"measured, gates not passed, campaign not closed", and it is in this status that they
are cited above.

**Known C6 debt:** the `c6l1` cell has **two runs instead of three** — the campaign
was stopped mid-pass, so its median is the average of two points. And it was
not checked whether uvloop is installed in the venv used to launch `l0b`: per C2 the event-loop
contribution is ≈0, but in C6 this was taken on faith, not measured.

## Backlog (after C0–C6)

| # | Item | Justification in numbers | Status |
|---|---|---|---|
| 1 | **Turn off the access log** | 91–96 µs p50, one line | ✅ done (ADR-10) |
| 2 | Adaptive perflog sampler | pathology 9.63 → gates pass | ✅ done (ADR-1) |
| 3 | Host networking in production | −20% p50 / −24% p99 vs bridge | ✅ done (ADR-2) |
| 4 | Close C6 under the ADR-11 amendment | yields publishable numbers + a third `c6l1` run | open, low priority |
| 5 | ADR-4 step 1 — minimal front end | prize is **75 µs**, not 208 | open, **re-estimated downward** |
| 6 | sendfile for on-disk delivery | server span 2.8× | open, needs a runner change (ADR-3) |
| 7 | Chase the remaining telemetry p99 | 3.5 µs per write/s; lever `OP_PERF_TARGET_RPS` | not planned — a trade-off at a known rate (ADR-1); production sees 0.5 rps, so there is nobody to benefit |
| 8 | **The slot file is compressed twice** | 2 passes × 470 ms over the same bytes | open, **higher priority** — the only half-second on the user's path (`PROD_2026-07-20.md`) |
| 9 | Incompressibility probe threshold | lets a 0.80 response through: 470 ms for 20% | open, **after item 8** — otherwise the threshold would mask the cache defect |
| 10 | 401 at 442→157 ms with zero DB queries | 4 records, a decreasing sequence | open, mechanism not established, data needed |

Items 8–10 were added from the first production telemetry of the new build. Item 8 is
**not a regression**: the same 0.79 response is present in the export from July 3, before all
the changes; the cost became noticeable because the data file grew from 1.5 MB to 15 MB.

The order 4 → 5 is deliberate: until C6 is closed under the amendment, the 75 µs figure for
item 5 has not passed the gates, and the decision about rewriting the front end rests on it.

## Status summary

| ADR | Decision | Status |
|---|---|---|
| 1 | Adaptive perflog sampling | accepted and verified (B 0/3 VALID → C/D 3/3, soak of 3.6 million requests VALID, the control loop does not self-oscillate); the p99 pre-registration failed, the mechanism is established — p99 ≈ 3.5 µs per write/s, `OP_PERF_TARGET_RPS` = the lever |
| 2 | Host networking in production | accepted |
| 3 | sendfile for on-disk delivery | deferred (needs a separate fixture) |
| 4 | Rewrite in C/Rust | deferred, not rejected — the path back is registered |
| 5 | QUIC | rejected (ceiling 150–172 rps) |
| 6 | OS tuning beyond t2 | rejected (isolcpus: p99 +69%) |
| 7 | Disabling mitigations | rejected (≈3% — the cost of protection) |
| 8 | kTLS | not applicable (TLS is terminated by the external proxy) |
| 9 | Environment and network context | must be taken into account |
| 10 | uvicorn access log off | accepted and shipped; 91–96 µs, C6 gates not passed — status in ADR-10 |
| 11 | steal gate where the effect = a speed-up | methodological: flag, do not gate |

## ADR-12 — Inferring canonical tables from observations: "suggester" status, not "automatic" · **ACCEPTED 2026-08-21**

Gates G1–G5 were registered before the run (the "A warehouse that does not exist" plan); the run is
`tools/lake_probe.py` over a copy of the production DB (393 runs, 28 with observations, 417
(file, sheet) units, 68 filtered out as summaries/fragments, 20 tables). Report:
`dist/lake_probe-2026-08-21-v2.json`.

| Gate | Result | Interpretation |
|---|---|---|
| G1 separability (≥0.90 of files with a containment gap) | **FAIL** 0.596 (0.811 by the clusterer's sim — for information, the gate was not touched) | Genuine ambiguity: `MD_SKU_BV/MD` (3 columns) is nested in `Sheet1` (19); in `po_changes_data` the sheets `OOS SOP01/02/12`, `дефициты` ("shortages"), `Лист*` ("Sheet*") (Russian sheet names, quoted verbatim as source data) are almost identical 6–9-column tables. A human has to say whether this is one table or several |
| G2 splitting by sheet name | **PASS** | `TDSheet` → `datasalesorder` / `datarealisation` / `peremeschenie`; `STOCK` (127 files) — honestly one table |
| G3 partitions | **PASS** | 20/20 tables — 100 % of files have a key; the only duplicate key (`new/2026-05`) is a reissue, flagged |
| G4 reuse across generators | **FAIL** (a fact of the corpus) | Four generators read four disjoint sets of folders; no clustering will pass this gate today |
| G5 id stability | PASS 1.0 | On a one-day history — recheck in a few weeks |

**Decision:** per the fallback declared in advance, inference ships as a
**suggester** — status `inferred` until a human confirms it, `/catalog`
shows confidence and evidence, `onepage.table()` and `/explore` work
only over `confirmed` tables. The 0.85 threshold is not revised based on the result.
Leg 2 (the Parquet lake) remains a separate bet: the saving from reusing
parsing is zero today (G4) — the only value of Leg 2 is `/explore` latency.

**Side findings:** (1) 68 summary/fragment sheets — a ready-made corpus for the
"header not recognized" hint; (2) 7 files have a `parsed`/`cached` row and
`consumed_json`, but `sniff()` produced not a single header (`RDC Data.xlsx`,
`1С РСОХ_С by SKU_*.xlsx`, `Привязки.xlsx` ("bindings"), `УПАКОВКИ.xlsx` ("packaging"), `Core NPI Delist_2.xlsx`; Russian file names quoted verbatim as source data) —
an engine defect, not a probe defect; (3) 82 files in `2026`/ReportTower/upload-* are
legacy migration observations that were never sniffed.

## ADR-13 — Semantic layer: KPI definitions are data, verified by a reproducibility gate (2026-08-22)

**Question.** Can the knowledge a NL→SQL layer needs — what the tables mean and how the
report computes its KPIs — live as a *declarative* `semantics.json` shipped with the
generator bundle, instead of in model weights or in prompt folklore?

**Experiment (pre-registered).** Generator `ops-rdc-health` v1.0.21, operational KPIs only
(stock KPIs excluded up front). Gates: S1 ≥80 % of Grand-Total cells reproduced exactly
(ratios to 1e-9); S2 every mismatch has a named cause; S3 per-question fragment ≤600 tokens;
S4 20 operator questions map to (kpi, dims, filters) by hand.

**Result.** `tools/semantics_gate.py` executes the six `cell_sql` definitions *from the JSON*
over the repo's real exports (3 sources × 2 periods + master workbook) and compares with the
authors' reference calculator: **S1 482/482 cells (312 numeric, 20–46 distinct values per
KPI), merge checkpoints match to the row (34 130 / 30 252 / 1 782).** S3: 480–510 tokens per
KPI fragment, ~310 for the 6-KPI intent menu, ~5 000 for the whole file (never sent whole).
S4: 15 ok / 4 ambiguous / 1 out of scope — gaps are a platform-level glossary (SL belongs to
another generator), an RDC `region` attribute, explicit "incomplete week" policy in answers,
a deterministic "why" decomposition recipe, and a flag for raw measures outside KPIs.

**Side findings.** `kpi_config.json` declares `transfer_order_creation`, never computed.
Single-workbook and folder modes disagree (e.g. transfer_delivery in 42/74 cells): the merge
keeps the *latest* file's row per business key, and 404 transfers had their planned delivery
date re-planned between exports — prod runs folder mode, so folder mode is the truth.

**Decision.** The NL layer targets *intent* (choose KPI, dims, period, filters) over a
`semantics.json` that ships in the bundle and must pass this gate on every version; free SQL
stays as the expert fallback. Engine for the gate is generic (rules + SQL from JSON);
bundle-specific knowledge is data. Next: platform glossary, `semantics` block accepted by the
manifest validator, gate wired into version registration, intent builder in /explore.

**ADR-13 addendum — second generator, blind (2026-08-22).** `slreport` v2.2.11: a fresh
Opus agent received only the bundle, one real `SL_23.xlsx` and the JSON shape — no reference
output, no hints. An independent gate (our own loader, its view + KPI SQL verbatim from the
JSON, compared with the generator's packed `agg`/`detail` tables) passed **12/12**: 13 955
filtered rows, all 4 743 `agg` cells on 13 dimensions equal in OrderQty/Fact/Undelivered/
FactClipped, grand SL 0.998507, 103 shortfall rows and reason totals equal, all 14 KPI queries
run. It found the non-obvious rules unaided (`min(fact, order)` numerator, `GFD делистинг`/
`Отмена сетью` zero filters (Russian source data values, quoted verbatim: "GFD delisting" / "cancelled by the chain"), THD/Сигариллы ("cigarillos") exclusion, client/shipping alias maps, ISO-Thursday
year fallback). Format defects surfaced: duplicate source headers need an explicit
`occurrence` field (the agent described the rule in prose only); the file is 71 KB /
~20k tokens — the `sl_lines` view alone is ~1.5k tokens, so per-question fragments must stay
SQL-free (≈200–300 tokens each) and the view is engine-side, never in a prompt.

**ADR-13 review round (2026-08-22, commits after fb1c36e).** Adversarial review found one
blocker and six majors, all fixed with pinned tests: (1) `{placeholder}` values from `rules`
were substituted AFTER the guard — a rule could smuggle a second statement into a view and
rewrite a catalogue view in every viewer's browser; now `normalize()` resolves placeholders
first, the guard sees the resolved text, the frozen text is the text that runs, and the
worker never templates. (2) KPI `ready` was true by construction (resolved against names
that survived); now resolved against declared names, and `intent_sql` refuses a not-ready
KPI. (3) The server had its own weaker regex guard; it now loads the worker's `guard_sql`
from `static/py/_explore.py` (stdlib-only at import) — one guard, one test suite. (4) Server
dry run and worker disagreed on table shape (`__row`, untyped inline rows); both now expose
declared columns + `__partition`/`__file` only, inline rows cast to declared types. (5) A
catalogue table is a cluster of files; `bind` now carries the matched `files` and the
worker filters `WHERE __file IN (…)`. (6) Dry run runs with `memory_limit`/`threads=1` and a
5 s `interrupt()` timer per statement, and before the INSERT so SQLite's write lock is not
held across it. (7) `explore.js`: `ready_kpis` sent, 422 falls through to bare nl2sql,
generator picked by question-form overlap, intent timeout = `HARD_QUERY_TIMEOUT_MS` (a 10 s
timeout would have killed the worker and the index). Minors: engine columns excluded from
`dims`, first-line DuckDB errors, `_SEM_DIRTY` on every `_rebuild_view` branch, value KPIs
roll up by their declared `aggregation`, non-finite literals → NULL, duplicate-header dedupe
on the engine key. Deferred: the full semantics JSON is still inlined into the /explore page
(digest + lazy fetch later).

**ADR-13 addendum — form-first routing, and squeezing the model when it is needed
(H0, 2026-08-22).** The weakest link in "Ask in plain language" (asking in Russian) was a CPU-only Ollama: seconds
per question, and a small model that reaches for a look-alike KPI. Two changes, both generic
(the report knowledge stays in `semantics.json`):

1. **`nl2sql.match_forms` — answer without the model.** Every KPI is scored against the
   question by its own `question_forms` / `short` / `name`. Score of one form = shared
   *content* stems (`_stem`, Russian function words dropped from both sides, 2-letter tokens
   kept because "SL" is the load-bearing word of this report family) **+ 2 per stem of the
   form when the whole form appears verbatim inside the question** — an author's phrase
   quoted back at us is the strongest signal available. **Confidence rule: the best KPI must
   score ≥ 2 (`FORM_MIN_SCORE`) and ≥ 2× the runner-up KPI (`FORM_LEAD`)** — so a tie can
   never be confident and a single shared word can never carry a match alone. A confident
   match returns an intent built with no model at all: the KPI, `dims: []` (the metrics
   panel is where the operator picks the cut — the one thing a form match cannot know), the
   period the question actually names (the same `ground_intent` extraction, nothing
   invented), order `value desc`, limit 10, `source: "form"`. On this path `/api/explore/intent`
   never opens a socket to Ollama; `explore.js` selects the metric's button, opens its form
   and pre-fills the period instead of dropping SQL in the editor, and never auto-runs.
   **Measured** (`tools/intent_eval.py --no-llm --data-dir ~/data/SL`, slreport 2.2.15,
   20 operator questions, 4 of them deliberately not about any metric): **16/20 = 80 %
   overall, 12/20 matched by form with 100 % precision (12 correct, 0 wrong), 4 correctly
   declined, 4 left to the model** (`ordered_volume`, `undelivered_by_reason`,
   `unassigned_reason_rows` carry no `question_forms` at all — the fix is data, not code —
   and the question "SL by retail chain, TOP-5, in July" (asked in Russian) is genuinely ambiguous between `sl_ordered` and
   `sl_top5_table`). Every form-matched intent was also executed over the real 4-file folder
   through `_explore.intent_sql` + `run_sql`: **0 SQL failures, 14/14 KPIs ready**. (Rows
   come back NULL for `month = 7` because that folder holds only months 2 and 6 of 2026 —
   the grounding is right, the data does not cover July.)
2. **Structured outputs on the model path.** `build_intent_schema` renders the shortlist as
   a JSON schema (KPI keys and the union of their dims as `enum`s, `op` as `INTENT_OPS`,
   `limit` 1..1000) and `call_ollama(prompt, fmt)` passes it as Ollama's `format`
   (constrained decoding, Ollama ≥ 0.5), behind `OP_LLM_STRUCTURED` (default true). An
   invented KPI or dim becomes unrepresentable rather than rejected. `parse_intent` stays
   the validator — a decoding constraint is not a trust boundary. An older Ollama answers
   4xx to `format`: that is logged once, retried without it, and `format` is not sent again
   for the rest of the process.

## Baseline measurements 2026-08-22 — before the redesign of Explore / Chat / presentations

Recorded on the merged HEAD (release/2026-08-20 + the one-step answers branch); they
serve as the gates for every subsequent phase: the numbers below **must not grow**.

**Hot path** (`tools/perf_baseline.py`, in-process TestClient, Chrome harness fixture;
compare only runs of this script with each other):

| route | n | hdr p50 ms | hdr p95 ms | db_n (median) | serve_us p50 | fast-lane hits |
|---|---|---|---|---|---|---|
| `/` | 61 | 0.625 | 0.819 | 3 | — | — |
| `/d/{id}` | 60 | 1.411 | 1.62 | 7 | — | — |
| `/render/{id}/` | 301 | 0.006 | 0.008 | 0 | 1.3 | 300 |

Rule: phase 5 (favourites) may add +2 queries, **only** on `/`; for phase 4 (Chat in the
viewer) and phase 6 (showing a presentation without `?pres=`), `db_n` for `/d/{id}` stays at 7;
nobody touches `/render/{id}/`.

**`/explore` on a real folder** (`tools/real_smoke.py --dir …/SL --glob 'SL_*.xlsx'
--sheet Заказы --table sl_zakazy --semantics slreport-folder-bundle-2.2.16.zip
--generator slreport`, 4 files, 53 226 rows; the Russian sheet name `Заказы`, quoted verbatim as source data, means "Orders"): parse 801 ms in total, reopening
from the Parquet cache 122 ms; semantics: `sl_orders` bound, `sl_lines` ok, 14/14 KPIs ready.
KPI time on 4 files: `sl_ytd_tiles` **618 ms**, `sl_monthly_series` **512 ms**,
`sl_weekly_series` 172 ms, the rest 84–99 ms. The two heavy ones are the trend view with
6×UNION ALL; on the 33 production files it scales to ~5 s and, together with Pyodide
overhead, hits the 30-second limit — the source of the "Query stopped" defect of 22.08.
Phase 1.4 gate: no KPI > 2 s on a real folder.

**Chrome harness** (`tests/chrome/smoke_explore_chrome.py`): default 137/137,
`--browser` 143/143, `--provider deepseek` 139 checks; first fixture index
e1 3.5 s / e2 1.5 s; zero console errors (the tier 2 routing 422 is subtracted in its own block).

### ADR-14 — Report index on the server (2026-08-23, owner decision)

**Context.** The /explore index (Parquet partitions of the source folder) lived only in each user's browser (OPFS). The first run meant parsing every file: minutes on an office laptop; users without access to the network folder could not ask questions at all. Measured 2026-08-23: 34 weekly SL files — indexing took 38 s, and that was already from cache.

**Decision.** The report run (gen-engine, after a successful publish) uploads the Parquet partitions and the manifest to the server (`data/uploads/_lake/{dashboard_id}/`); /explore and the chat in the viewer download them instead of parsing. **Everyone with access to the report can read** (`require_dashboard_access`, same as `/d/{id}`); those who can run the generator can write; only the latest run is kept; limits `OP_LAKE_PART_MAX_MB`=64, `OP_LAKE_MAX_MB`=1024; share links have no access.

**What this changes in the boundaries.** The rule "cell values never go to the model" — unchanged. The rule "cell values never go to the server" — **lifted for exactly this cache**: the server now stores the report's raw data, and access to it = access to the report. The alternatives "cache in a folder on the share" (share ACLs) and "aggregates only" (phase 8) were proposed and rejected by the owner in favour of the server.

**Consequences.** The server becomes a store of raw data: the `data/` backup covers it too; until there is a role model, a report viewer = a reader of all its rows. On a `PARSER_VERSION` mismatch the cache is ignored and the page asks for the report to be recalculated.

### ADR-14b — Server-side query execution over the lake (2026-08-24, owner decision)

**Context.** Even with the index downloaded, every chat conversation began with "Loading
the query engine": the WASM engine (pyodide + DuckDB, ~5–10 s to instantiate) was born again
after every iframe reset (10 min) and every page reload. Meanwhile the data was
already on the server (ADR-14).

**Decision.** `POST /api/dashboards/{id}/lake/exec` runs queries ON THE SERVER:
`app/services/lake_exec.py` spins up a fresh instance of the `static/py/_explore.py` module
(the same engine, native duckdb+pandas) per dashboard — a scratch directory of symlinks to the
lake partitions, a catalog/semantics snapshot from the manifest's publisher, `index_cached`
from the manifest, an LRU registry (`OP_LAKE_EXEC_INSTANCES`=2, 30 min idle). The ops mirror the
worker 1:1 — `status`/`intent`/`sql`/`export` — so on the page this is a TRANSPORT:
`send()` in explore.js routes these three ops to fetch, and the whole UI above it cannot tell the
difference. "Load index" with `OP_LAKE_EXEC` enabled (the default) activates
server mode (instant, zero downloads); the download path remains as a fallback
(flag off, server refusal). Folder mode is still local (data the
server does not have). The transport is not tied to DuckDB: an engine on GreenPlum (Van Stock)
will be able to sit behind the same ops.

**Boundaries.** Cells do not go to the MODEL — unchanged (the server returns rows to the
browser, just as `GET …/lake/parts` did before). The gate is the same `require_dashboard_access`.
Trust in SQL is the engine's own boundary (`guard_sql` + `enable_external_access=false`);
the endpoint adds no surface of its own; SQL is not written to the audit log (filter literals
may quote data values).

**Consequences.** The server spends CPU/RAM on queries: an instance's frames live in memory
(SL ≈ hundreds of MB), hence the LRU cap and the idle timeout; `requirements.txt` gained
`pandas`/`openpyxl` (full bundle). The manifest's `parser_version` must match the
native `PARSER_VERSION` — otherwise 409 and "recalculate the report" (the engine 2 index is not
executed by the server). Tests: `tests/smoke_lake_exec.py` (18 pins), the "server exec" DOM block
in `tests/explore_dom.js`, an end-to-end block in the Chrome harness (real partitions
uploaded by the browser are executed natively).

**ADR-14b addendum — slot→lake conversion on publish (2026-08-24, Van Stock).**
Agent-based reports (the first is Van Stock, GreenPlum) publish data not as a folder but as
slot files. The semantics declares the source `sources: [{"slot": "van_stock_daily.csv.gz"}]`
(validated at registration: a bare file name, no paths), and publish converts the slot
into a lake partition ON THE SERVER, best-effort after the commit (`app/services/lake_slots.py`;
a conversion failure never breaks the publish — the response carries `lake.errors`).
Formats: csv/tsv/txt (+ .gz), xlsx, json (array of objects or {columns, rows});
values are written as VARCHAR (typing is `_sem_cast`'s job), headers are normalized the same way as
in the engine (`norm_col` + `__{k+1}` for duplicates). The engine table name is
`semantics.slot_table_name(t)` = `slot__<t>` (double underscore: catalog normalization
collapses spaces and cannot mint a collision); the partition is `"slot"`;
`parser_version: "slot-1"` — a version space of its own for the conversion (decision of
2026-08-24: do NOT inherit the engine's PARSER_VERSION — that one versions Excel coercions
the slot never sees; bump only when the conversion itself changes). `lake_exec`
accepts both versions; the browser download path honestly rejects slot manifests —
slot reports are answered by the server. `bind()` gives slot tables an unconditional match
(no catalog needed), and `_catalog_snapshot` now carries the semantics even with an empty
catalog (the early returns used to lose it — a slot report has no catalog by definition).
v1 is for generators whose semantics is slot-only: the conversion replaces the manifest entirely.
The `table`/`format` keys inside `outputs.slots[i]` of the bundle manifest are tolerated by
registration but inert — the conversion is driven by the semantics. Test:
`tests/smoke_lake_slots.py` (17 pins, including a Van Stock-shaped csv.gz and an
honest note about a missing column).

### ADR-15 — Question remainder, data probe and model checker (2026-08-24, owner decision)

**Context.** "and which vans in MBU…" — the word MBU (a value of `unit_name`) silently
dropped out of the intent: the model does not know values by construction, the engine does, but nobody
asked it. On top of that, duration (ratio n/d) was shown as "274 %" instead of "2.74 days".

**Decision — three layers, from deterministic to model-based:**
1. **Question remainder** (`nl2sql.residue_words`): significant words consumed by
   neither a metric, a dimension, a period, the glossary nor the filters. Conservative:
   only Latin letters/digits and "quotes" — Russian prose never becomes a candidate.
   Travels in `/intent*` responses as the `residue` field; the page passes it to the engine as
   `intent.probe_words`.
2. **Data probe** (`_explore.intent_sql` + `_probe_value`): the word is looked up as a
   VALUE of the metric's dimensions with an exact case-insensitive hit (works at any
   cardinality). One dimension — auto-filter + a note "understood "MBU" as unit_name";
   two dimensions — `clarify` instead of SQL: the page shows CHIPS instead of a number
   (click = instant answer, the word is consumed — a second clarifying question is impossible by
   construction); none — silence (a false clarification is worse than none).
3. **Checker** (`nl2sql.build_verify_prompt`/`parse_verify`, routes
   `/api/explore/intent/verify{,/prepare}`): one advisory model check
   "question ↔ intent ↔ format" between building the intent and showing the number. The prompt is only
   the question + names/values from the question + the output format; cells do not travel. A
   {ok:false} verdict holds back the number and shows the checker's question with "calculate
   anyway"; fail-open everywhere (error / 20 s timeout / no model → we answer as usual).

**Format:** a KPI's `unit` beats the percentage (`fmtValue`): a ratio with a "days" `unit` —
"2.74 days"; a ratio without a unit whose value is >150 % stops pretending to be a share.
Engine notes now reach the chat bubbles (`rec.notes`).

**Boundaries.** Cells do not go to the model (the checker sees names and the question text);
nobody but the browser sees the result numbers. The rule for stopping clarifications is an
invariant, not a policy: a clarification exists only while there is an unconsumed word.

Tests: smoke_explore_intent (+10: remainder, prompt without cells, verdict parsing),
smoke_lake_slots (+4: probe with one/two/zero dimensions through lake/exec),
explore_dom (+9: chips instead of a number, re-entry with a filter, the checker
holds back and releases), the browser-LLM path sequence extended with
verify/prepare→provider.

## ADR-16 — Platform reference tables · **ACCEPTED 2026-08-26**

**Context.** The pilot is the SL report: the mapping `NWD BV → its code + its name` (1:1; other future reference tables are many-to-one). Codes arrive regularly, they are mapped by someone other than the generator's author, and the reference table must be reusable by several reports and sources. Today such mappings are hard-coded in slreport (`config.py: CLIENT_MAP_RAW`, `SHIPPING_POINT_MAP`) with a silent fallback to the raw value — "a new value nobody mapped" is already happening, invisibly.

**Decision.** One truth on the platform: one reference table — one row per key; no per-report override. A miss → an "Unmapped" bucket in the report, the raw code is always shown (a generator convention, not a platform one). The "apply / do not apply" reference table switch is a **run parameter** with recalculation and publish (the owner insisted); the version/mode is visible in the version caption and in lineage. A version is **always created on every import**; the `allow_pin` checkbox only allows choosing a version at run time. Effective dates are v2, not touched in v1 (`kind: simple` reserved). Column types are set by the **consumer** (the manifest), the schema is frozen; Excel import validates against the schema and rejects rows rather than silently coercing them. One key normalization rule for the whole platform (trim, collapsing whitespace including nbsp, unifying dashes, casefold); stored as entered, compared normalized; key ≤ 64 characters (the `MAX_CELL_CHARS` ceiling, otherwise a permanent false "miss"). Responsible person: a field on the reference table; empty → a notification to everyone with `can_update_data` on any consumer report. Notifications in v1: Data Quality feed + badge + banner after the run; no e-mail (no egress; an SMTP relay is a question for the admins). Menu: **"Reference tables"**, no "Master Data"/"Crystal". Boundaries: hot path `/d/{id}` — zero new queries; no report-specific heuristics on the platform; `requirements.txt` does not change (openpyxl 3.1.5 and pandas are already there → no full bundle needed beyond the usual).

**Architecture.**

- **Model**: `reference_tables` (slug `^[a-z0-9][a-z0-9_-]{0,63}$`, title, description, key_column, columns_json `[{name,type: text|number|date}]`, kind='simple', allow_pin, responsible_user_id → users, current_version, created_by/at, updated_at) · `reference_table_versions` (table_id, version_number, rows_json Text ≤ 2 MB / ≤ 20,000 rows, row_count, sha256, summary_json `{added,changed,removed}`, note, created_by/at; UNIQUE(table_id, version_number)) · `run_reference_uses` (run_id, table_id, version_number NULL, applied bool). Plus a `data_sources` row `kind="table", key_name=slug` (owner = `owner_id`, already present in `models.py:346`) — so that Data Sources / lineage / acks are keyed by `data_source_id`.
- **Manifest** (`genmanifest.py`): `inputs[]` gains `source: "platform"`, `table: <slug>`; `match` must be `<slug>.xlsx`; columns gain `type` (`_INPUT_COLUMN_KEYS` + a new `column_types(manifest)` next to `column_descriptions`; `inputs_for()` passes `source/table` through in its dict — consumers index by name and do not break). At registration: the table exists → the schema must match (400 with a diff); it does not → registration passes, the table is "not created". A new check type `ref_lookup` (`match/sheet/column/table`) — the column must be declared in `inputs[].sheets[].columns`, as for other checks. **Limitation**: a platform input moves the manifest from legacy to allow-list (`is_legacy_inputs`, `gen-engine.js:1145`) → the generator must declare all its real `inputs`. For SL this means manifest 2.2.20 gets real `inputs` (`SL_*.xlsx` + sheet/columns), which it does not have today.
- **Engine** (`gen-engine.js`): `GET …/generator/manifest` (called only by gen-engine, not the hot path; `viewer.html:823` loads it only with `generator_ctx`) returns `reference: [{slug,title,exists,current_version,allow_pin,versions≤10,responsible}]` **in one request**. UI next to the run button: for each table a checkbox "Apply reference table "…" (v7 · 25.08)", with `allow_pin` — a version select. `doRun`: for the applied tables `GET /api/reference/{slug}/versions/{n}/xlsx` → the buffer goes into `platformFiles` and is concatenated with `auxFiles` **before** dedup (`gen-engine.js:1119`); only genuine aux files go to `uploadAux` (the server rejects foreign names, `generators.py:1088`). The worker and `add_file` do not change — the generator sees `<slug>.xlsx` as a service workbook (fixed layout: sheet `data`, header in row 1, key first, plus a `_key_norm` column). For the `ref_lookup` check the engine does `GET /api/reference/{slug}/versions/{n}/keys` (normalized keys, ≤ 50k) and embeds them in the check spec; `run_checks` in `_lineage.py` compares the column's normalized values with the keys and returns `missing` (≤ 500 unique + overflow) — **a way around the 500 ceiling of `known_values`** (`_lineage.py:663` empties the set). The check runs even with "Apply" switched off (the owner needs to know anyway). `input_manifest.reference: [{slug, version, applied}]` → `start_run` writes `run_reference_uses`. Publish: `note` is **appended to** (`generators.py:1268` already writes `generated: run N`) → `… · reference table bv v7` / `… · without reference table bv` (note text rendered in English here); `run_summary` gains `unmapped: [{table, values, more, responsible}]` → `publish_warnings` draws a line "N new values in reference table X — responsible: Y" via the existing `showPublishWarnings`.
- **Generator runtime**: `onepage.norm_key(s)` in `_install_onepage` (`_lineage.py:897`) — the same function as on the server (`app/services/reference.py::norm_key`), pinned by a test with 12 cases (nbsp, dashes, case, whitespace; leading zeros are left alone).
- **Quality** (`quality.py`): in `_drift_block`, a branch for `ref_lookup` (`missing` results from `QualityResult.metric_json`) → on the server at read time `missing − keys(current_version)` (importing keys closes a miss **without** a run) → an `unmapped_values` finding on the consumer's folder source with `detail.table/slug/values/responsible`; a separate block after the loop (`quality.py:634`) → `rerun_needed` on the report if the last run used a version < the current one (or ran "without reference table" while the current version is newer). New kinds go into `quality.KINDS`, `KIND_GROUPS`/`KIND_LABELS` (`data-quality.js:31-50`); severity warn/info. "Accept" for a table check = a link to the reference table page with "Download new values template" (keys filled in, values empty).
- **Data Sources / lineage** (`datasources.py`): `KIND_TABLE="table"`, `_touch_reference()` next to both `_touch_greenplum` calls (`:498`, `:629`) from manifests already loaded — zero new queries; `certain=True`; a `tables` counter in stats; widen the kind check in `upsert_data_source` (`generators.py:1589`) and add a case to `data_sources.html` (tile: "N mismatches · recalculation needed: reports …"). The lineage node `src:{id}` appears by itself (`_source_node_id` branches only on `source_id`).
- **API** (`app/api/reference.py`, its own `_log`, `run_in_threadpool` for openpyxl as in `generators.py:1595`): `GET /reference` (page), `GET /reference/{slug}` (page), `GET/POST /api/reference`, `GET/PUT /api/reference/{slug}` (title/description/responsible/allow_pin; the schema does not change after creation), `GET …/versions`, `GET …/versions/{n}/xlsx` (Content-Disposition as in `lake.py:326`), `GET …/versions/{n}/keys`, `GET …/template?unmapped=1`, `POST …/import?dry_run=1` (multipart, `_read_capped` modelled on `presentations.py:216`, 5 MB limit; the response is a diff `{added, changed, removed, errors:[{row, column, reason}]}`; with `errors` a commit is impossible), `POST …/import` (the same file + `expected_version` optimistic locking → 409; upsert-by-key semantics; `delete_missing=1` is an explicit flag), `GET …/consumers` (from current manifests + the latest `run_reference_uses` per report). Creating a table: from a manifest (schema from `column_types`) or from the first Excel file (column type is inferred: all numbers → number, all dates → date, otherwise text; then frozen). Access: read — admin or reach to any consumer report (`_generator_dashboards`); write — the responsible person, otherwise `can_update_data` on a consumer, otherwise admin; create — admin or anyone who has at least one generator report with `can_update_data`.
- **UI** (`templates/pages/reference.html`, `reference_detail.html`, `static/js/reference.js`, modelled on `catalog.html`/`catalog.js`; `el()` + `textContent`, JSON with `Content-Type`, uploads via `FormData`): list (name, key, rows, version, responsible, consumers, open mismatches); card: grid (no virtualization needed at ≤ 20k, but pagination by 500), versions with a diff summary, responsible person, consumers with "used v6 · recalculation needed", mismatches by report, "Export to Excel", "Import from Excel" → a modal with the dry-run diff and per-row errors → "Apply". Sidebar: `nav-item` "Reference tables" between Data Sources and Data Quality.

**Deferred.** Effective dates (v2), e-mail, per-report overrides; the `known_values` cap of 500 is not raised (the workaround mechanism is already in place).

**Boundaries.** Hot path `/d/{id}` — zero new queries; no report-specific heuristics on the platform; reference table values live on the server by design — consistent with ADR-14.

### ADR-16, addendum — Semantics reads reference tables · **ACCEPTED 2026-08-27**

**Context.** ADR-16 carried a reference table only as far as the generator RUN. "Ask
in plain language" (ADR-13) did not see them: the SL semantics held `client_map` /
`shipping_point_map` as inline `rows` in the bundle — i.e. a snapshot that goes
stale the moment the responsible person edits the reference table on the platform, and
can only be fixed by re-registering a version.

**Decision.** A semantics table gets a third kind of source —
`sources: [{"reference": "<slug>"}]`, alongside `file`/`object`/`slot`. Rows are not
stored in the bundle: the server substitutes them when building the `/explore` snapshot from the
CURRENT version of the reference table, so an edit is visible in the next answer without a new
generator version.

- **Contract** (`semantics.py`): `reference` is a slug (`_SLUG_RE` mirrors
  `reference.SLUG_RE`, pinned in a test), incompatible with `file`/`file_pattern`/
  `object`/`slot`/`bundle_config`; exactly one source; `rows` are forbidden;
  a column's `source` is forbidden (the semantics column name IS the reference table's
  column name); `business_key` is mandatory and equals the reference table's key; types are
  `text|number|date`. A subset of columns is allowed.
- **Reconciliation at registration** (`reference_schema_errors` +
  `generators._check_semantics_references`): no reference table → registration
  passes; one exists → the type of an existing column and the key must match, otherwise 400
  with a diff. A column the reference table does not have is not a conflict but an additive
  extension (see the next addendum).
- **Row delivery** (`explore._bind_reference_rows`): only on the
  `/explore` path (the page snapshot and `/api/explore/catalog`) — the hot path `/d/{id}` and
  `/render/*` did not get a single query. Access is exactly the right to read the
  reference table (`reference._can_read`), with one `memo` for the whole snapshot. Rows
  travel as LISTS in the order of the declared columns under
  `tables[t]["reference"]`: 20,000 × 3 columns — 1.47 MB as lists versus
  2.33 MB as objects (measured), and reference table storage is capped at 2 MB of
  compact JSON anyway. Ceilings: `MAX_REFERENCE_ROWS = 20 000` (= `reference.MAX_ROWS`,
  NOT `inline_rows` 2,000) per table and `MAX_REFERENCE_BYTES = 4 MB` for the whole
  snapshot; beyond the budget the table arrives `unbound` with a status, and the rest of the
  report still answers.
- **Engine** (`_explore._materialize_semantics`): a reference table is
  materialized along the same inline path (a typed view over a registered
  frame), with status `reference` rather than `inline`; `semantics_status()` carries
  `references: [{table, slug, version, rows}]`, and the page writes "reference table
  sl_client_map v7". The cache key of materialized views (`_mat_key`) got a
  (slug, version) stamp — otherwise a change of reference table version would not move the key and
  the view would serve yesterday's mapping.
- **Prompt.** Rows do not reach the prompt by construction: `_intent_semantics`
  reads the FROZEN semantics from the DB, where `reference` is only a declaration;
  `schema_outline` works over empty typed tables. Pinned by
  `tests/smoke_explore_intent.py` and `tests/smoke_reference_semantics.py`.
- **Consumers.** `reference._consumers` is now computed from TWO sources:
  the manifest (`via: "run"`) and the semantics (`via: "chat"`), marked "in chat" on
  the reference table page. This also widens `_can_read`/`_can_write`: the right to a
  reference table follows the report, however the report reads it.

**Boundaries.** Hot path — zero new queries; `requirements.txt` did not change;
no report-specific heuristics on the platform.

### ADR-16, addendum — A reference table is created from its declaration · **ACCEPTED 2026-08-27**

**Context.** Only the consumer's declaration (manifest
or semantics) knows the reference table's schema, yet until now someone had to retype it by hand on
`/reference` before the first run could read anything.

**Decision.** Registering a generator version CREATES the declared reference table if
it does not exist: columns and types from the declaration, the key is the manifest's first column
(`business_key` for semantics), title = slug, `current_version = 0`,
no responsible person assigned, `allow_pin` off, plus `ensure_data_source`
(`kind: table`) and a `reference_create` audit entry with `auto: true`. No separate permission is
checked: `manage_generators` is strictly broader than the right to create a reference table for
one's own report, and a refusal here would produce a report that cannot run until an
admin repeats the same declaration by hand.

**What is frozen and what is not** (a softening of the original "the schema is frozen"):

- the KEY and the TYPES of existing columns are frozen — a mismatch with the declaration
  is a 400 with a diff, because they cannot be fixed without a decision by the reference table's owner;
- ADDING a column is allowed and done automatically
  (`reference.add_columns`); rows are not rewritten — versions stay
  immutable, and `reference.fit_rows` pads a narrow row with empty
  values ON READ (`current_rows`, `_rows_for_version`), so export,
  import and diff always see the current width;
- REMOVING a column from the declaration is not a change: the generator simply stopped
  reading it;
- a generator dropping a reference table neither deletes the reference table nor marks it
  deleted. Deletion is done only by a human: `DELETE /api/reference/{slug}` with
  write permission and confirmation in the UI (versions and `run_reference_uses` go,
  the `data_sources` row stays: the past refers to it).

**Data Quality.** An empty reference table (created, never imported) produces ONE
info entry "reference table is empty — upload an Excel file", not a warn on every key of the run:
nothing falls into an empty table, and a mismatch count would be noise. The same
fork in `publish_warnings`. Values still live in `detail`.

### ADR-16, addendum 3 — fixes from the adversarial review · **ACCEPTED 2026-08-27**

The review of the two previous addenda found six problems; the contested ones
were decided by the owner and recorded here so they are not reopened.

1. **"In chat" is not access.** A consumer from semantics (`VIA_CHAT`) NO LONGER
   widens `_can_read`/`_can_write`: the line `sources: [{"reference": slug}]`
   can be written by anyone allowed to register a generator, and it can name
   someone else's slug — if it granted access, publishing a report that merely
   MENTIONS a reference table would hand its rows to everyone who opens that
   report. The right follows only the manifest consumers
   (`reference._run_consumers`) plus the owner/creator/admin. A reader
   without rights receives the table as `unbound` with the status "no access
   to reference table …"; "in chat" remains a badge in the consumer list.
2. **Order: "rejections first, then the write".** `_write_version` puts
   bundle.zip on disk, and a 400 after that rolls back the DB but not the
   directory — so the next upload picks the same `v{n}` and dies on "versions
   are immutable". Therefore ALL validation (`_check_semantics_references` +
   `_check_reference_declarations`) runs BEFORE `_write_version` on all three
   registration paths, and creation/`add_columns` runs after. In addition, any
   exception after `save_bundle` deletes the written version directory
   (`genstorage.delete_version_dir`). In git-sync, creation was moved OUT of the
   `try` whose `except` commits: otherwise a failed sync left auto-created
   reference tables behind. The `generator_git_sync` audit entry now carries
   `reference_tables`/`reference_tables_created`, like the other two paths.
3. **Merging declarations.** A manifest and a semantics file that name the same
   slug describe one table TOGETHER: the manifest sets the column order, the
   semantics appends its own. Previously the manifest "won" the whole entry, and
   a column read only by the chat was silently never created — after which the
   chat answered NULL for it forever. The same column with different types in
   the two halves of the bundle is a 400 with a diff. Failure to add columns
   (`add_columns` → ValueError) is also a 400, not silence. A race between two
   registrations on the same slug is caught via UNIQUE(slug) in a SAVEPOINT and
   idempotently re-reads the other registration's table.
4. **One reference table — one read and one budget charge.** `_reference_state`
   memoizes (table, verdict, rows) per slug for the whole snapshot: two reports
   reading the same reference table cost one `get_table` + one `current_rows`,
   and the `MAX_REFERENCE_BYTES` budget is charged per REFERENCE TABLE, not per
   reader (otherwise a reference table that fits would crowd itself out at the
   second reader). The projection copies inside the tables stay — they are cheap
   and depend on the column set of the particular semantics.
5. **A report without file tables makes it into the chat.** A generator is no
   longer dropped from the snapshot when no catalog table matched but at least
   one reference table was bound.
6. **The worker.** `_mat_key` does not hash reference-table rows (a full
   20,000 × 32 is 7.6 MB of JSON and ~20 ms per view): the key carries (slug,
   version), and `_strip_reference_rows` makes the invariant "no cache key hashes
   rows" true by construction. Rows from the wire move into a pandas frame
   (`_SEM_REF_FRAMES`, key (generator, table, slug, version)) and are removed from
   the semantics — previously the tab held both copies, and every
   re-materialization added yet another frame: `_materialize_semantics` now
   unregisters the previous `_ep_sem*` at the start.

Small items from the same review: `create_version` fits rows to the schema width
(`fit_rows`) — the stored data is always self-consistent; `consumer_generators`
are computed from DECLARATIONS, not from consumer rows (a generator without a
dashboard is still visible to the filter and to the "Created from manifest …"
banner); `consumer_dashboards` returns only the reports the reader has access to
(the `consumers` counter remains an honest total); the filter on `/reference`
shows the selected generator as a separate disabled item if it has no reference
tables available to the reader; the `truncated` branch was removed as dead code
(`MAX_REFERENCE_ROWS` == `reference.MAX_ROWS`).

## ADR-17 — Quests: gamification without a background scheduler · **ACCEPTED 2026-09-04**

**Context.** The owner wants employees to come into OnePage every working day and learn the platform and how to work with AI. The idea is a daily "surprise" with a collectible card (modelled on ZA Bank Quest): a run of days (streak), five quests per week, a quiz, a leaderboard, a card collection. The interactive demo was accepted by the owner after ~30 iterations; the quiz bank is 45 situational questions with no mention of courses, and no user data is sent to AI (the bank is scenario text only; answers are scored by the server).

**Decision.** V1 = the full scope of the demo at once, with no cuts. The leaderboard is opt-in: by default a person sees only their own percentile, publishing name and points is an explicit opt-in, and someone who has not opted in is shown the named table only as their own percentile row (no other people's names). An admin page manages the question bank, holidays and the season date/length without a redeploy (a runtime overlay on top of env, canon `runtime_config.py`). All five quests of the first week are shown at once rather than unlocked one by one. Visuals are international, with no national or ethnic styling (the owner insisted); the character (a snow leopard) stays. Surprises are granted only for server-confirmed actions (favorite, presentation, share link) — verified after the fact from `audit_log`, not from a client message saying "I did it"; two client events (theme change, grid toggle) are an exception to this rule and therefore award only points, never a card. No background scheduler at all — only lazy computation on request (opening the day, verification, streak), the same principle as `runtime_config`/`quests_config`/the render cache.

**Architecture.**

- **Model** (`app/models.py:1283-1517`, canon `DashboardFavorite`): `quest_profiles` (`QuestProfile`, `app/models.py:1283`) — `user_id` PK FK `users.id` (1:1, not a surrogate), `points`, `streak`, `best_streak`, `last_open_date` Date, `freeze_week` String(8) ISO week of the spent freeze, `quiz_correct_total`, `leaderboard_optin`, `optin_at`, `verified_at` (the `audit_log` scan cursor), `backfilled`, `created_at`/`updated_at`. `quest_openings` (`QuestOpening`, `app/models.py:1329`) — a card-opening event, `source` ∈ `daily`/`quest:<id>`/`quiz`/`dup:<n>`/`milestone`; **UNIQUE(user_id, day, source)** — "at most one daily and one quiz opening per day" and at the same time "any number of `quest:<id>`/`dup:<n>` on one day" (each carries its own suffix); `seed`/`weights_json`/`roll` are stored verbatim — a disputed drop is reproducible. `quest_cards` (`QuestCard`, `app/models.py:1364`) — the collection, **UNIQUE(user_id, card_id)**, `count` accumulates rather than being recomputed from the log. `quest_completions` (`QuestCompletion`, `app/models.py:1384`) — **UNIQUE(user_id, quest_id)**, `evidence_json` freezes the grounds at the moment of credit, `surprise` distinguishes "discovered after the fact" from "done deliberately". `quiz_questions` (`QuizQuestion`, `app/models.py:1407`) — the bank, `id` = the id from the import (not autoincrement, so a repeated import does not duplicate), `day` — bank day 1..15, `active=False` retires old questions instead of deleting them (past attempts reference them). `quiz_attempts` (`QuizAttempt`, `app/models.py:1443`) — **UNIQUE(user_id, day)**, `answers_json` stores `{qid, served_at, pick, ms, correct, pts}` per question — a dispute is settled by recomputing from raw data. `quest_client_events` (`QuestClientEvent`, `app/models.py:1499`) — **UNIQUE(user_id, kind)**, the forging ceiling = twenty points (two kinds × 10). Indexes: `app/main.py:184-193` — `ix_audit_log_actor_created` (actor_id, created_at), without which verification would be a full scan, plus one on `user_id` of each quest table.
- **Service** `app/services/quests.py` (~1038 LOC, the only "smart" module): calendar — `today()`, `is_working()`, `season_start()` (`app/services/quests.py:152`), `season_day_index()` (`:164`), `quiz_bank_day()` (`:171`) = working day of the season modulo 15; there is no `date.today()` in the module (pinned by `smoke_quests_calendar.py`). Streak `apply_open()` (`:188`) — a gap of 0 days extends it, a gap of 1 with an unspent freeze for this ISO week also extends it and spends the freeze, otherwise it resets to 1; milestones `streak%3==0` (doubled weights for rare cards) and `streak%7==0` (a second opening). RNG `draw()` (`:323`) — the `CARDS` weight table (`:253`), `seed=secrets.token_hex`, reproducible `random.Random(seed)`, seed/weights/roll go into `quest_openings` and into the audit. Verification `verify()` (`:696`) — the first visit works from table state (audit `quests_backfill`, `app/services/quests.py:735`), then an `audit_log` scan by the `verified_at` cursor, limited to 500 rows at a time.
- **API** `app/api/quests.py` (~446 LOC): two routers in one file — `router` under `Depends(_require_quests_enabled)` (`app/api/quests.py:36-42`, a gate like `explore.py:55-61`, the whole feature is 404 when the flag is off) and `admin_router` without that gate (an administrator must be able to reach the switch even when the feature is off). `GET /quests` (`:94`) renders `pages/quests.html` via `run_in_threadpool`, state in `#q-root data-state`; `GET /api/quests/state` (`:115`) — the same state as JSON; `GET /api/quests/badge` (`:128`) — a single `EXISTS` query with no profile creation and no verification, for cheap client polling; `POST /api/quests/open` (`:148`), `POST /api/quests/exchange` (`:192`), `POST /api/quests/event` (`:217`), `POST /api/quests/quiz/start` (`:235`), `POST /api/quests/quiz/answer` (`:264`), `PUT /api/quests/leaderboard-optin` (`:292`) — all under `get_current_user`. `GET /admin/quests` (`:314`) and `GET/PUT /api/admin/quests/config` (`:349`/`:370`) under `view_admin`/`manage_users`; `POST /api/admin/quests/quiz-import{,-default}` (`:404`/`:428`) under `manage_users`, dry-run writes nothing, a broken bank is a 422 listing all errors at once. Every mutation is committed in this module (canon `favorites.py:21-28`); services only add/change rows. `app/api/share.py:269,358` adds `_log(... "share_link_create" ...)` to both share-link creation routes — **only `token[:8]` goes into the audit; the full token goes nowhere**.
- **UI** `templates/pages/quests.html` (171 lines, `catalog.html` idiom) + `static/js/quests-page.js` (~844 LOC) + `static/css/quests.css` (~467 LOC): sections `#q-hero`/`#q-cap`/`#q-open` (the capsule), `#q-path` (the streak track), `#q-quests` (quests), `#q-quiz-banner`, `#q-rating`, `#q-collection`; the opening scene `#q-overlay`/`#q-rays`/`#q-flash`/`#q-confetti`. `static/js/quests-how.js` (~536 LOC) — the "Show how" engine: a spotlight over the real DOM (`#q-spot`/`#q-ghost`/`#q-callout`) plus mini-screens for other pages (`#q-mini`, a 460px mock of Dashboards/Viewer/Presentations), exits via `#q-how-x`/Escape/click on the backdrop/`beforeunload`.
- **Global** `static/js/quests.js` (~158 LOC, gate `window.self !== window.top` and "not on /quests", idiom `autumn.js:7-29`) — a golden capsule FAB on every page via the cache `localStorage['op-quests-badge']` (one `fetch('/api/quests/badge')` per day per user, no new request on the hot path) and a delegated listener for the two client events (theme, grid) → `POST /api/quests/event` `keepalive`. `templates/base.html:42,51-52` includes the script only `{% if user and settings.quests_enabled %}`. `static/js/palette.js:19` — the "Quests" command (`icon: gift`, `kbd: G K`), filtered by `window.OP_QUESTS_ON`; `static/js/hotkeys.js:43` — hotkey `g k` (`g q` is taken by Data Quality).
- **Admin** `templates/pages/admin_quests.html` (164 lines, `admin_generators.html` idiom) + `static/js/admin-quests.js` (~345 LOC): enable toggle, season, holidays, bank import with "Check" (dry-run) → "Import", the card weight table + actual drop statistics.

**Deferred.** Chrome e2e for `/quests` (Python smokes + Node DOM harnesses cover the contract; a real browser is optional); a team leaderboard by department beyond the department label in one's own row; personal "Go" deep links for each quest (for now a quest describes the action in text, without a direct link to the screen); a video card reveal (for now static art + a CSS capsule animation).

**Boundaries.** Hot path `/d/{id}` — zero new server requests: the badge is a client `fetch` after page load, at most once a day per user via `localStorage`; nothing was added to `viewer.js`/the report render chain. `requirements.txt` did not change (all functionality is the standard library + the already-used SQLAlchemy/FastAPI/Jinja). Audit without secrets: the share token in `audit_log` is only the first 8 characters (`app/api/share.py:269,358`); the full token is never logged. Company data is not sent to AI: the quiz question bank is only scenario text about working with AI and delegation, with no real user data; answers and scoring are computed by the server (`quests_quiz.py`), and the feature makes no model requests at all.

## ADR-18 — Row-level security on slot files · **ACCEPTED 2026-09-05**

**Context.** The Delta pilot (`example of pages/DWH/delta-bundle`) is an agent
generator that reads Greenplum under an analyst's credentials and publishes a
finished dashboard + 8 slot files (7 data tables in OPC1 format + one JS
library). The report is needed not only by the office/analysts, who see
everything, but also by field managers (TMR/HOU/CM) — each only for their own
territory. Before ADR-18 the OnePage access model was "the whole dashboard or
nothing" (owner/admin/grant/public/share link): the platform had no suitable
unit for "this row is not for this viewer", and the report HTML contains no
data at all — it lives entirely in slot files, which the server had so far
served byte for byte to anyone who passed the ordinary dashboard access check.

**Decision.** Rows are filtered ON THE SERVER, on every slot-file response, not
once at publication — the same generator run serves all viewers; the only
difference is what each one receives. Key decisions:

- **Deny by default.** If there is no row in `user_scopes` for the viewer's
  `person_id` (`UserScope`, recomputed by the import), they receive an EMPTY
  slot file (valid in format, zero rows), not the full contents and not a 403 —
  the report opens, it simply shows nothing. A missing record is never read as
  "access unrestricted".
- **`see_all` is an explicit exemption**, not a side effect of a role: a
  platform administrator, the dashboard owner, or a `UserScope.see_all` record
  (the TMD role via the import, or a manual grant for the office/Planning). Only
  `see_all` sees the original file byte for byte — the server does not even run
  the filter.
- **The whole bypass perimeter is closed**, not only the main slot route:
  share links (creation of new **and** existing tokens — old tokens stop
  working as soon as RLS is enabled on the dashboard), the source HTML download
  (`/source`), slot writes (`PUT`/`DELETE …/files/{slot}`), relative-asset
  serving by the original file name (bypassing by name instead of
  `/files/{slot}`), `lake` (manifest/parts/exec) and the `explore` endpoints
  where a question is asked over a specific RLS dashboard. The list is not an
  idea for the future but a requirement: any of these paths serves the same
  slot file that `/render/{id}/files/{slot}` has already learned to filter, and
  one loophole left open reduces the filter to theatre. The whole generator
  surface belongs here too (`_assert_can_update_data`): a run's aux file is the
  entire source xlsx/csv, `get_run` returns `input_manifest`/`error_detail`, and
  running and publishing rewrite the slots, so for an RLS report the
  "update data" right does not
  replace `see_all`. And the dashboard's shared `localStorage` blob: there is one
  per report, it is saved by a viewer with full access, and it is inlined into
  everyone's HTML — under RLS it is not inlined at all (see `build_render_body`),
  otherwise the report HTML, which the ADR promises "contains no data", would
  carry other people's selections.
- **Report type and policy do not diverge**: `PUT /rls` refuses a
  non-interactive report (409), publication from a generator does not attach the
  manifest's policy to a static/snapshot report (a warning in the publication
  response), and `replace_html` refuses to turn a report with a policy into a
  non-interactive one (409 `rls_type_change`) — otherwise the rows would move
  into the HTML while the "Scope access" badge promises the opposite.
- **Formats**: OPC1 (`.bin`/`.bin.gz`, the new `app/services/opc1.py`, the
  reference is the Delta pilot encoder, `decode`/`encode` byte for byte), CSV,
  JSON, Parquet (if `pyarrow` is available) — any other extension in the
  policy's `slots` is rejected at serving time (415); this protects against a
  format slipped in by a generator that the filter cannot read safely. For
  OPC1 the dictionaries of dict columns are **rebuilt from the surviving rows**
  on every filtering pass — otherwise the file descriptor keeps carrying the
  full list of territories/clients even when the rows themselves are cut, and
  the leak happens through metadata rather than through data.
- **Cache** of filtered bytes — an in-memory LRU; the key includes
  `(dashboard_id, version, slot_index, mtime, size, policy_hash, keys_hash)`,
  so a change of file version, a change of POLICY and a change of the viewer's
  keys each miss the cache on their own, without explicit invalidation (the
  same technique as the render cache and slot ETag — invalidation by key
  rotation, not by deletion). `policy_hash` in the key is essential:
  `rls.cache.clear()` lives in one process, and `OP_WORKERS > 1` is documented
  as a supported mode — without a policy fingerprint, workers that did not
  handle the `PUT /rls` would keep serving bytes under the old (wider) policy.
  The `ETag` must include both `keys_hash` and `policy_hash`: two viewers with
  different territories must not get a shared cached response either on the
  server or in the private browser cache, and tightening a policy must
  invalidate an immutable response already served.
- **Audit** (`rls_serve`) is written only on a cache miss — on actual
  filtering, not on every request — and carries aggregates (`rows_in`,
  `rows_out`, `key_count`), never data values: the same boundary principle as
  in Data Quality and Data Sources.

**What this ADR honestly does NOT solve.** An OnePage RLS policy is a filter on
a column value in a slot file, not a projection of rights from the corporate
directory: the platform does not read AD/security groups itself; the viewer's
keys arrive only through a manual CSV import (§ scope import). Reports
registered BEFORE this ADR do not get the filter automatically and are not
required to — RLS is enabled explicitly (`PUT /api/dashboards/{id}/rls`), and
a silent manifest means "as before". Share links for an RLS report are closed
completely rather than filtered — the unit of access in OnePage is a viewer
with a session and their `user_scopes`; an anonymous token has no such unit,
and building a separate key circuit for it for the sake of one pilot is not
justified.

**Rejected alternatives.**
- **Filter on the client (in the browser, like engine v2).** Rejected
  immediately: the whole point of RLS is that other people's rows physically
  never leave the server. Sending the client everything and hiding the excess
  in JS is exactly what we are moving away from. The whole slot file in the
  DevTools Network tab is a ready-made leak.
- **A separate `visible_to` column in the file itself, filled in by the
  generator.** Rejected: this shifts keeping rights up to date onto the report
  author (they would have to know the whole territory hierarchy and keep it in
  sync with HR/DWH), while the platform already solves this problem centrally
  for Reference Tables (ADR-16) — duplicating it inside every generator is
  redundant and sooner or later drifts out of sync.
- **Filter once at publication and store N copies of the file (per
  viewer/per territory).** Rejected: there may be hundreds of copies (510 TMR
  leaves in Delta alone), any data update = reissuing hundreds of files, and an
  on-the-fly filter is still needed for new employees/reassignments between
  generator runs — so the server filter is necessary anyway, and precomputed
  copies are pure overhead on disk and on invalidation.
- **Only log a 403 on an uncovered slot, without blocking.** Rejected by the
  owner: an uncovered slot is a hole by definition (data for which nobody has
  decided a rule); fail-closed here is the same decision as "no record in
  user_scopes → empty", applied to the policy author rather than the viewer.

**Key assumption (Delta, to be verified on the first run).** The viewer's
access key is the TMR position name from the imported hierarchy
`v_positions` (`tmr_position_name`). The Delta policy compares it
against the columns `tmr_position_name`, `tmr_territory_name` and `territory`
from THREE different SQL extracts of the report — we assume this is the same
string of the same DWH hierarchy after `norm()` (trim, casefold, collapsing
spaces/nbsp on both sides), but the positions mart itself does not explicitly
guarantee it, and this is recorded as a pilot assumption, not as a fact
(details and the verification method are in the pilot's `MAPPING.md`, section
"RLS"). A mismatch is visible without access to production data:
`GET /api/admin/scopes/lookup` will show `key_count > 0` for a real viewer,
while `rls_serve` in the audit shows `rows_out: 0` where the name did not
match.

**Operational notes.** Scope import is a manual CSV upload by an administrator
(`/admin/scopes`, idiom `/admin/employees`), not an automatic pull from the
DWH: the platform does not store Greenplum credentials and pulls nothing from
there itself; the `v_positions` export is prepared and uploaded by a person on
the DWH team under the same discipline as the HR employee export (ADR
employees, "the server is not the source of truth, it mirrors the latest
export"; a repeated import fully replaces the `source='import'` rows and leaves
the `source='manual'` manual grants untouched). TMD grants (see_all across the
whole hierarchy) and `HOU_ADD` grants (one's own unit for a development team)
are a manual tool, `/api/admin/scopes/manual`, assigned by the same circle of
administrators as `manage_users`; who exactly maintains the list of see_all
users on a regular basis is an organizational question, not a technical one —
the platform only provides the form and `lookup` so one can check "what a
specific person sees" without going to the developers. Addendum 2026-09-05:
mart rows for which no account was found at import time (the employee has
never logged in, the directory has not been loaded) are stored with an empty
`person_id` and matched at login (`scopes.attach_on_login` from the SSO and
dev login, audit `rls_scope_attach`): the person gets their scope immediately,
without waiting for the next import. The import counters do not change as a
result — "not found" in the status always reflects the moment of import.

**Browser cache (fix 2026-09-05, found on the staging server).** Slot files of an
RLS report are served with `Cache-Control: private, no-cache` to ALL subjects,
including see_all, even when `?v=` matches: the URL is the same for all
viewers, and the browser serves an immutable response without revalidation —
on a shared computer or after an account switch, the next user got the
previous user's full file from the disk cache (reproduced: admin → dev-login
tmr1 in one Chrome profile, all 7 slots `fromDiskCache`). An ETag with
`keys_hash` gives the same viewer a 304, so reopening still involves no file
read. Passthrough slots (libraries) remain immutable — they contain no
viewer-dependent rows. Ordinary reports (`rls_json NULL`) are not affected by
this fix.

## ADR-19 — Foundation sprint after the reviews (2026-09-06) · **ACCEPTED**

**Context.** Four independent enterprise reviews (architecture, operations, security, data) converged on a single list of gaps: the platform is functionally mature (ADR-13..18) and production has been carrying SSO load since 2026-07-20 (`docs/PROD_2026-07-20.md`), but the operational foundation underneath it — backup, rollback, audit, database integrity, localization — had never been built. The owner bundled them into a "foundation sprint" instead of one-off hotfixes. The perimeter settles some questions in advance: the server is isolated, and access is only through Checkpoint VPN with the same AD account as SSO. Revoking the account in AD closes both the VPN and the OnePage login in one action — so the Leaver process, Single Logout and external access to share links all coincide with the same organizational lever that IT already controls. The only asset that explicitly stays OUTSIDE the perimeter is the model key (DeepSeek), which in BYOK mode lives in the user's browser; that is an owner decision (`docs/LLM_PROVIDERS.md`) and is out of scope for this sprint.

**What was accepted and why.**

- **Backup / restore / drill** (`deploy/backup.sh`, `deploy/restore.sh`, `deploy/restore-drill.sh`) — before this sprint the production instance's SQLite database did not have a single verified copy. `backup.sh` takes a consistent snapshot through the SQLite backup API (correct under WAL), archives the whole of `data/` (the DB plus the runtime overlays `llm-runtime.json`/`quests-runtime.json`, which take precedence over env) with permissions 0700/0600, with rotation and an optional second copy. `restore-drill.sh` is not a document but an executable quarterly check: a real backup → a real restore → a real instance start on the restored data → `/health`, with no dev-bypass even on the data copy.
- **`/health` with the build version + `deploy/rollback.sh`** — before this sprint the health check said nothing about WHICH build was answering, and there was no rollback from an incident at all, only "bring it up again". `settings.build_sha` is resolved from `app/BUILD` (written by `make-bundle.sh`/`make-patch.sh` from the git SHA) — the operator sees the version without SSH to the host. `rollback.sh` validates its argument (image tag, full bundle, data backup) and checks the data (`PRAGMA integrity_check`) BEFORE stopping the container — a typo in a tag must not turn into a second outage.
- **Audit export** (`app/tools/audit_export.py`, CLI + `GET /api/admin/audit-log/export`) — `audit_log` used to be visible only through the `/admin/audit` page; now it can be exported as JSONL for external incident analysis or a periodic archive, with the same code for both the CLI and the route. A connection leak in streaming was fixed along the way.
- **The `explore_answers` table** (`app/services/explore_answers.py`) — records the fact of every successful "Ask" answer (which question, against which FROZEN semantics, by which path — form/lake/model), without a single result cell and without the full SQL text (only its sha256) — the same boundary principle as `audit_log.detail` for `/api/explore/*`.
- **Feedback on an AI answer** (`explore_answers.vote`/`voted_at`, the `explore_form_suggestions` table) — next to the answer record above: only the person who asked the question can give 👍/👎 (someone else's id — 404, not 403), and the vote is the ONLY entry point into the form suggestion bank; there is no separate "suggest a form". So that the answer record can return its `id` to the page for voting, `record()` does a `flush()` inside a SAVEPOINT: a failed insert rolls back only itself and does not bring down the route's transaction (without this the request failed with 500 instead of returning an answer without `answer_id`). 👎 → a suggestion of kind B; an INDEPENDENTLY confirmed 👍 → kind A: three 👍 from one person on three different calendar days OR 👍 from two different people — "regenerated and clicked again" does not become a candidate for `question_forms`. Unreviewed suggestions are deduplicated by a partial unique index `WHERE status='new'` (after `declined`, the same question comes back into the queue as a new signal). The queue is reviewed by whoever can update the dashboard's data, and for an RLS report only by a curator with `see_all` (ADR-18): a suggestion quotes someone else's question and its filters. The table contains no result rows and no SQL, by construction.
- **Provenance** (`GET /d/{id}/provenance`) — until now a report had no single "where do these numbers come from" page: version, run, frozen generator version and its dependencies, semantics, reference tables, RLS policy, lake manifest, sources. Access is DELIBERATELY narrower than plain report viewing: the run/generator/sources block requires `can_update_dashboard_data`, and the lake manifest block additionally requires the same `see_all` as ADR-18.
- **`lake_exec` timeout + rate limit on `/pair/request` + pip-audit** — three independent patches on surfaces that previously had NO limit at all: a server-side explore SQL query could run unbounded (the timer races the DuckDB engine via `conn.interrupt()`), the local agent pairing endpoint (`agent/server.py`, unauthenticated by definition) was not protected against brute force, and the `requirements.txt` dependencies were not checked for known vulnerabilities (`tools/pip_audit.sh`, run from a machine with network access — the server is offline by a decision recorded in `prod-server-facts`).
- **Version/owner in the semantics** (`owner`, `approved_at` on a KPI, `app/services/semantics.py`) — display-only fields: who is responsible for the metric definition and when it was last approved. They do not affect `cell_sql` or "Ask" routing.
- **Accounts of inactive employees** (`app/services/account_hygiene.py`, `/api/admin/users/stale`) + a "Sample" badge in Data Quality — before this, the admin could not see whose access was still alive while their employment in the HR reference table had already ended; nor which Data Quality findings were computed on an incomplete sample of the data rather than the full set.
- **Alembic** (`app/migrations/`, `app/services/migrate.py`, `alembic.ini`, `requirements.txt`) — schema changes used to live as `ALTER TABLE`/`CREATE INDEX IF NOT EXISTS` in the lifespan, re-checked with try/except on every start. Now they are versioned revisions under an exclusive `flock`, made idempotent through the inspector rather than try/except.
- **`PRAGMA foreign_keys=ON` + `tools/fk_orphans.py`** — by default SQLite stayed silent about broken foreign keys; now the check is armed AFTER migrations (a batch ALTER would not see them), deleting a parent must explicitly deal with its children (`app/services/integrity.py`), and `tools/fk_orphans.py` finds already accumulated orphans on a copy of the production database — SQLite does not retroactively re-check what is already in the file.
- **i18n** (`app/i18n.py`, `app/locales/*.json`, `tools/i18n_*.py`, `static/locales/*.js`, `components/lang_switch.html`, `PUT /api/me/lang`) — the interface in ru/en/zh-HK; the catalog key is the Russian source string itself (a missing translation degrades to the Russian text instead of leaking an identifier); priority `users.lang` → cookie `op_lang` → `Accept-Language` → `ru`. Discipline for new strings: only through `_()`/`_h()`/`_n()` in Jinja and `t()`/`tn()`/`tinto` in JS, followed by `tools/i18n_extract.py` — otherwise the string silently stays Russian forever.
- **Chat fixes** (`FORM_MIN_SCORE` 2→4, recording every outcome of a question) — review H3a showed that a form match threshold of 2 gave false positives on questions unrelated to the report (6 on the held-out set); raising it to 4 removed them with no losses on the author bank. Separately, no question once asked disappears from the screen any more: the bubble appears immediately, and every outcome (answer/clarification/stop by the checker/error) is appended to that same bubble instead of being lost in an intermediate controller state.

**What was deferred and why.**

- **Postgres / HA** — there are still no RTO/RPO figures from the business to design against; introducing a multi-process DBMS ahead of a requirement is a cost with no measured benefit.
- **Model key in the browser (BYOK) or an egress proxy for the server** — owner decision (`docs/LLM_PROVIDERS.md`), deliberately outside the perimeter and outside this sprint.
- **Prometheus / SLO dashboard** — makes sense once IT has a Grafana to plug it into; standing up a separate stack for a single instance is not justified.
- **AD security groups in the SAML claim** — the platform does not read AD groups itself (ADR-18 findings); this is a separate future request to the ADFS administrator and does not block the current sprint.
- **Rate limit on `/login` and Single Logout** — covered by the perimeter: without a valid AD account (the same VPN condition as for SSO) a request will not reach `/login`/`/saml/acs`, and revoking the account zeroes both VPN access and the SSO session in one action — building a separate control loop for a threat that an organizational measure already covers would be redundant.

**How to verify.** `tests/smoke_backup.py` (backup/restore, rotation, `--dest2`), `tests/smoke_health_build.py` (build version in `/health`), `tests/smoke_rollback_validate.py` (validation of the `rollback.sh` argument before the container is stopped), `tests/smoke_migration.py` (Alembic revisions, idempotency), `tests/smoke_i18n.py` (catalogs, language priority, no extra DB queries), `tools/fk_orphans.py` (orphan report on a database copy, without `--fix` on the first run). Additionally, per area: `tests/smoke_fk.py` (`PRAGMA foreign_keys=ON`), `tests/smoke_audit_export.py`, `tests/smoke_explore_answers.py`, `tests/smoke_explore_feedback.py`, `tests/smoke_provenance.py`, `tests/smoke_admin_stale.py`, `tests/smoke_semantics.py` (owner/approved_at), `agent/tests/smoke_zipguard.py`.

## ADR-20 — Report chat as a grid column and an answer card (2026-09-06) · **ACCEPTED**

**Context.** On 2026-08-23 the viewer's third column was removed: with the
dock open (`--dock-w: 360px`) the chat squeezed the report "until unreadable",
and the chat became a floating widget over the report (`position: fixed`, the
comment in `main.css` next to `.v-chat`). The widget covered exactly the spot
people were asking about: to compare a number in the answer with the number
in the report, you had to close it. On top of that, the protocol was
one-way — only `{type:'op_chat_count', n}` went up, and "nothing was ever
posted back into the frame" — so the pane could have neither a width nor a
"Show in report".

**Decision — bring the column back, but with arithmetic instead of hope.** The
`.viewer` grid has three areas `vmain vchat vdock`; a closed pane is
`--chat-col: 0px`, so a report nobody is asking about lays out exactly as
before. Open:

```
.viewer[data-chat="open"] {
  grid-template-columns: minmax(560px, 1fr) minmax(0, var(--chat-col)) var(--dock-col);
}
```

- **`minmax(560px, 1fr)`** — the very floor the 2026-08-23 complaint was
  about (`REPORT_MIN_W = 560` in `viewer.js`: CSS keeps it as a track, JS as
  the subtrahend in the chat width ceiling).
- **The chat yields.** The chat track is `minmax(0, --chat-w)`, not a flat
  `--chat-w`: in a window where 560 + chat + dock do not fit, something has
  to give, and let it be the chat rather than a `.viewer` wider than the
  window (measured 2026-09-06: with a flat track at 1050px and the dock open,
  the dock slid off the edge of the screen).
- **The ceiling is arithmetic, not CSS.** `chatMaxW(avail, dock) = max(340,
  min(0.62·avail, avail − dock − 560))`, where `avail` is the width of
  `.viewer` itself (not the window: the sidebar is outside it). So closing
  the dock really does give the chat more width. "Wider" = `0.50·avail`, then
  the same clamping; the handle drags within 340…62 %, and a double click on
  the handle restores 440.
- **Overlay below 760px.** There is no width to give there: the pane takes
  over the whole of `vmain` (`@media (max-width: 760px)`), and the handle is
  hidden. Keeping it on `vchat` did not work — the 0px track squeezed the
  pane to zero.
- **The frame is created lazily, after the report's first frame.** The
  invariant "zero new requests on `/d/{id}`" holds not because the pane is
  closed but because `ensureFrame()` is deferred until `op_ready` (or
  `requestIdleCallback`/a 1.2 s timeout if the report says nothing about
  readiness) — even when `localStorage['op-viewer-chat:'+id]` remembers an
  open pane. Closing the pane does NOT tear down the frame: tearing it down
  threw away the built index, and every return to the chat rebuilt it in
  ~30 s (prod, 2026-08-23); the worker is unloaded after 10 quiet minutes.
- **The pane protocol** is extended by exactly what is listed: down
  `op_chat_width {w, wide}`, up `op_chat_count`, `op_chat_close`,
  `op_chat_wide`, `op_chat_locate`; down, in reply to the last one,
  `op_chat_located`. The `origin === location.origin` check and type
  coercion on input are done the same way as for the counter. "Show in
  report" is not a new injection but a single `op_locate` command in the
  already injected `_ANNOTATE_CONTROLLER` (`app/services/detector.py`): the
  injection is a regex over the whole HTML, and a fourth pass over a
  multi-megabyte report would cost more than the feature itself. The
  contract is `.claude/skills/dashboard-protocols/SKILL.md`.

**Consequences.** The pane and the report now compete for width, and that is
deliberate: at 1280px with the dock open the chat gets 340…360px (that is,
practically only the minimum), at 1600px up to 680px; with the dock closed,
720 and 992 respectively. "Dock + chat + report" at the same time are
comfortable from roughly 1600px, while at 1280 you have to work with one of
the two. This is arithmetic, not a measurement on a real report: **the owner
needs to check the column on the actual SL report** — the 2026-08-23
complaint was about exactly that report, and 560px is a hypothesis about its
minimum readable width, not a measured value.

### Answer card and tiers

The card's badge names WHO chose the metric, not "confidence" in general
(`tierBadge` in `explore-chat.js`): "exact form" (`source === 'form'` and
`match.score ≥ 4`), "the form matched loosely", "understood by meaning" (the
model), "SQL from semantics", "no answer". The threshold 4 is
`nl2sql.FORM_MIN_SCORE` (ADR-19, raised from 2), so the line is drawn in the
same place where the server decides whether to answer with a form at all.

"Loosely" has exactly one reachable meaning, and it is not numeric. A numeric
rule ("score below the threshold", "lead over the runner-up less than 1") is
unreachable: `match` has a single producer — `nl2sql.match_forms` — and it
returns a hit only when `best ≥ FORM_MIN_SCORE` AND `best ≥ FORM_LEAD · runner_up`.
The reachable signal is WHO did the choosing: **the model chose from the
shortlist**, and the shortlist contained someone other than the metric that
was taken (`lowConfidence`). The numeric branches are kept as a guard for a
future producer that starts reporting a rejected hit.

Instead of an empty card — a question back to the person (`clarifyReason`:
`'empty'` — zero rows, `'null'` — an aggregate over zero rows). This is a
testable invariant: an answer without a number is never drawn as a card with
an empty body. The options in such a card are only what the browser can
actually do: drop the period filter, drop one non-time filter, take a
different metric from the shortlist, go back to the input.

**Period coverage (H10)** — the same card, but with a named reason:
`partitionFacets(key)` parses the partition KEY into facets (`week` — only
if "w" starts a word, otherwise `sw2024` was read as week 20; `year-month`,
`year-month-day`, plain year), and `periodOutsideCoverage` compares the
filter and the keys ONLY at a single granularity: monthly partitions say
nothing about a question about a week, and then the card stays silent rather
than guessing. The coverage pill is computed from the index the browser
already holds (`coverageOf(indexStats, workerTables)`) — not a single new
request.

### Idempotency

- `askKey(question, {report, parentN, fixedKpi, regen})` — djb2/xor over
  the normalized text plus everything that turns the same words into a
  different question: the conversation, the parent card, the pinned metric,
  a deliberate "Regenerate". Two calls with one key — ONE request: the
  second awaits the first one's promise (`inflight`).
- `answerKey(ev)` — dedup of `answer` events (the question, `parent_n` and
  `regen`, joined with the `\u0000` separator — written as an escape in the
  source, not as a raw byte) within a **1.5 s** window: the follow-up branch
  and the main path could emit two events for one question, and then
  IndexedDB accumulated duplicates and the counter on the FAB lied.
- Writes to the thread are **serialized** (`ChatStore.add` through a promise
  chain): two answers in the same tick read the same "last n", and the
  second silently overwrote the first under the same key.
- "Cancel" on the pending card — two gates in `explore.js`: before the
  engine starts (nobody is waiting for the request — we do not run it) and
  after (the number has been computed and stays on screen A, but the card is
  not written to the thread — the chat writes its own "cancelled" line
  instead of an orphan).

### Conversations

"New conversation" deletes nothing: the conversation travels as a `#`
suffix in the record's `report_id`, and everything that needs the report
itself (title, semantics, KPI) reads `baseOf(rid)`. The open conversation is
restored by the maximum `last.ts` among this report's threads — previously,
after a reload the chat showed the FIRST conversation, and the next question
landed in the thread that "New conversation" had just closed. The counter in
the viewer header counts by `baseOf` (`countBase`): "0 questions" after
starting a new conversation would be untrue — the questions have not gone
anywhere.

### Feedback

`answer_id` is returned from ALL points where an answer is recorded
(`app/api/explore.py` + `op=intent` in `app/api/lake.py`), otherwise 👍 would
be impossible for some answers; for this, `record()` does a `flush()` inside a
SAVEPOINT — a failed insert rolls back only itself, not the route's
transaction. A vote can be cast only on YOUR OWN answer (someone else's or a
nonexistent id — 404, not 403). The vote is the only entry point into the
suggestion bank: 👎 → a suggestion of kind **B**; 👍 → kind **A**, but only
when INDEPENDENTLY confirmed — three 👍 from one person on three different
calendar days OR 👍 from two different people (`UPVOTE_DAYS_FOR_FORM = 3`,
`UPVOTERS_FOR_FORM = 2`): "clicked Regenerate three times in a minute" does
not become a candidate for `question_forms`. Unreviewed suggestions are
deduplicated by a PARTIAL unique index
`(generator_id, question_hash, kind) WHERE status='new'`: a full UNIQUE
would prevent a declined question from returning to the queue once people
started asking it again. The queue is reviewed by whoever can update the
dashboard data of this generator, and for an RLS report (ADR-18) only by a
curator with `see_all`: a suggestion quotes someone else's question and its
filters. API details — `docs/LLM_PROVIDERS.md`, "Votes and the form
suggestion bank".

### What we did not do and why

- **Asking before execution when confidence is low.** The card reports a
  loose choice AFTER the answer has been computed. An "I'm not sure, please
  clarify" dialog before running costs an extra round trip on the most
  common — confident — path; instead there is "Refine" with alternatives
  from the shortlist next to the already computed number.
- **Server-side metric exclusion.** "Ask again without this metric" lives
  on the client: `fixed_kpi` pins the chosen metric, but the server does not
  accept a list of excluded ones — that would be a new router parameter for
  the sake of one button.
- **Suggestion queue UI.** `explore_form_suggestions` is reviewed only
  through the API (`GET /api/explore/form-suggestions?generator=`, `approve` /
  `decline`); there is no page — first we need to see whether the inbox fills
  up at all, and a page for an empty inbox is an interface without confirmed
  demand.

**How to verify.** `node tests/viewer_chat_dom.js` (width clamps, persistence,
Esc, no frame before opening), `node tests/explore_chat_dom.js`
(badges, clarify, dedup, conversations, transcript), `node
tests/detector_locate_dom.js`, `tests/smoke_detector_locate.py`,
`tests/smoke_explore_answers.py`, `tests/smoke_explore_feedback.py`,
`tests/smoke_migration.py`; the column geometry only in a real browser:
`tests/chrome/smoke_viewer_chat_chrome.py`, plus
`tests/chrome/smoke_explore_chrome.py`. Author and admin guide —
`docs/CHAT_PANE.md`.

## ADR-17b — "Companion": a creature for the full Quests collection (2026-09-11) · **ACCEPTED**

**Context.** The owner wanted a reward for 8/8 cards that does not look
childish: not "congratulations, you got a pet" but "something was hiding
here" — a small creature that lives inside OnePage, rarely shows itself and
stays silent. Per the 11.09 analytics nobody has the full collection yet (the
best is 7/8), so only a handful of people will ever see the reward; its value
lies in rarity and word of mouth, not in reach.

**Decisions.**
1. The trigger is strictly 8/8 distinct cards, with no hints earlier. The
   server (`quests.companion_info`) returns a `companion {seed, since, enabled,
   autumn}` block in `/api/quests/state` and — the only new byte outside the
   Quests page — in the daily `/api/quests/badge`. `companion_since` is set
   once on mutating paths (rendering `/quests`, `POST /open`); GET never writes.
2. Surfaces: workspace pages only (`quests-companion.js::allowedPage`). Since 12.09 this is ONE page — the dashboard list: the corner the creature sits in is the corner of the WINDOW, and on pages whose content reaches that corner it would sit on top of someone's data; below the dashboard list there is always room.
   The report page, presentations, share, embed, admin — never. On `/d/{id}`
   there is not a single new request: the module and the images are loaded
   lazily from `quests.js` based on the `localStorage['op-quests-companion-<uid>']`
   cache, which is written only by the Dashboards tab (from the badge response)
   and by the Quests page itself.
3. Grown-up behaviour as a rule, not a wish: ≤1 appearance per page load, ≤2 per
   day (deterministic from seed+day so that tabs do not double it), not before
   40 s, not while an overlay is open, it runs away on Ctrl+K/Escape/a click
   outside it, no text, no sound, nothing at all under `prefers-reduced-motion`.
4. A one-time intro: a scene from Higgsfield (a fox cub behind a capsule at
   dusk) over the page, a single line "Something was hiding here.", dismissed by
   a click or after 9 s. A Den appears in the "Collection" with two toggles:
   "Show the companion" and "Autumn theme". The admin kill switch is
   `quests_companion_enabled` in the `quests-runtime.json` overlay.
5. The autumn theme is an 8/8 perk: `[data-theme="autumn"]` in `main.css` changes
   only tokens (warm paper, ochre accent). A profile flag (`autumn_theme`)
   syncs other devices once; a manual theme switch on a device wins afterwards.
6. Art: one character for everyone (owner decision), seven clips from Higgsfield
   (model wan2_7, image-to-video from cards on a green background): run_in, idle,
   head, stretch, run_out, walk_out, sleep. Normalized before slicing
   (scale 0.754, baseline at y=742 in the 960×960 source); all clips share
   one crop box, so the character does not change size between scenes.
   Delivered as WebP, ~1.4 MB in total, loaded only for owners of the collection.

**Quality iteration (11.09, after showing it to the owner).** The fox filled 56 % of a
256 px cell — about 144 px of real data on 132 px of screen, which is blurry on
retina ("you can see bare pixels"). All clips were re-shot in 1080p (1440²); the
three "travelling" clips were replaced by ONE run-in-place loop (the movement is
done by shifting the element anyway), so the common frame shrank from the full
shot to 1242², and the fox now fills 65 % of a 384 px cell — 250 px of data on
140 px of screen. A separate look-up clip was spliced onto the head-turn clip,
and the manifest got an `up` segment; mirroring covers both diagonals on the
right. The sheet is now a GRID rather than a single row: 68 frames of 384 px
would make 26112 px, and WebP refuses anything with a side larger than 16383 px
(so does the browser texture) — the pipeline writes `cols`, the player reads row
and column, and a manifest without `cols` is read as a single row.
`run_in`/`run_out`/`walk_out` are aliases of one run strip. 2.6 MB per set.

**Sprite pipeline.** `tools/companion_sprites.py` turns clips into
horizontal sprite strips plus `sheets.json`: ffmpeg only (no Pillow, no
numpy), the chain `colorkey=0x00FF00:0.30:0.12,despill=type=green`, the bounding
box from the alpha channel is computed in pure Python, `--uniform` is a common
crop box for all clips, `--webp` is the delivery format (`cwebp` is preferred;
ffmpeg on this machine has no libwebp). 7 clips, 10 frames/s, 256 px cell, 1.4 MB
in total versus 20 MB as PNG. `sheets.json`
holds `frames`/`fps`/`w`/`h`/`cols`/`file`/`bytes`; the `head` clip also has
`segments`: `center`, `arc_left`, `arc_up` and `right`. The head strip is ONE
continuous arc (facing front → looking left horizontally → up) with the
right turn spliced on, 75 frames; the sprite never needs mirroring, and
every intermediate angle, including the 45° and 135° diagonals, has a real frame.
The `sitdown` and `standup` clips are the "ran up and sat down" and "got up and
ran off" transitions: shot with pinned frames (the neighbouring clip supplies the
start and end frame), while `stretch` has `action`, the span where the
movement actually happens (the tail of the clip freezes). More fields about the
footing: `by` (lowest point of the FIRST frame), `by_end` (of the last) and
`by_max` (the lowest over the whole clip); which one counts as the ground is
set by `align` in the manifest — `start` for clips where the creature stays in
place (otherwise a stretch, which genuinely reaches lower, would bounce upward),
`floor` for running (there the lowest point is the floor, otherwise the paws
sink below the window edge) and `ramp` for the sit-down, which joins two
different takes: its offset moves linearly from the running floor to the resting
line over the length of the clip. Frame rate can also be set per clip
(`--clip-fps run=20,sitdown=12`): ten frames per second is too few for running,
the frame switching is visible to the eye. The `ey` field is the EYE line found
in the image (the darkest row of the upper body): the direction to the cursor
is computed from that point, whereas an eyeballed fraction of the box put the
origin ~20 px above the real eyes. `--gaze head` measures WHERE each frame
looks: the centre of the dark blob of eyes and nose relative to the front-facing
frame, i.e. a direction with magnitude; the player picks the frame whose measured
gaze is closest to the direction to the cursor, and error across the direction
weighs more than error along it. The manual "left horizontal" and "up" labels
were removed: the shot arc contains no horizontal gaze at all, the flattest
frame is 14° above the horizon, and the frame labelled "horizontal" made the
fox look above the cursor. `--color-ref idle` matches the exposure of the takes:
each clip is a separate shot and the tone drifts (measured 6.4 units of mean RGB
between the idle clip and the head clip in the SAME pose). Matching is done only
on an identical pose — on the first frames and only if the silhouettes coincide:
comparing per-clip means corrects the pose, not the exposure, and made the sleep
clip twice as bad. And `cx`, the character's own axis as a fraction of the cell:
computed from the LOWER 45 % of the silhouette, which the head does not move;
otherwise a head turn would drag the mirroring point and the body would jump
sideways on a side change. The pipeline's self-check is `--selftest` on a
synthetic clip.

**Browser side.** A small custom frame-by-frame player in
`static/js/quests-companion.js`. The frame is NOT changed by shifting the
background: the strip is an `<img>` inside a clipping box, moved by
`transform: translate3d`. EACH sheet has its own `<img>`, created in advance,
and a clip change only toggles visibility: assigning a new `src` empties the
element until loading and decoding finish, even from cache, and the creature
vanished for 60–130 ms on every clip change (the owner's screen recording of
11.09: at 4.15 s the right half is drawn, at 4.20 and 4.25 s only the shadow).
A sheet is shown only once the browser is done with it; otherwise the current
clip stays on screen.
Changing `background-position` repaints the whole element, and repainting a
3000+ px sheet behind a `drop-shadow` filter does not fit into a frame on a modest
machine — the image drops out and the page shows through. The filter was
replaced by a static elliptical shadow, the sprite cell is 320 px (the creature
is still one and a half times larger than it is displayed), the sheets are 3.2 MB
of files and ~105 MB of raster if all clips were in memory at once. Inside: one
`requestAnimationFrame` loop, `play`/`seek`/`stop` per clip, each at its own
frame rate, and ONE manifest request — only after the page check and only for
owners of the collection. If the manifest is unavailable or a clip is missing
from it, the module silently falls back to the earlier single PNG poses. Cursor
tracking: while the pointer is closer than 320 px, the `idle` loop stops and the
`head` clip seeks to a FRACTIONAL frame position.
The position is a single number: the ANGLE from the creature's eye to the
pointer (0 — right horizontally, π/2 — up, π — left), laid out linearly along
the arc. No axes, segments or switches: any intermediate angle is a real frame,
and stepping along the strip goes through real frames of the clip. The position
chases the target exponentially (τ = 38 ms) with a cap of 70 strip frames per
second and is rounded to a frame. There is NO blending of the neighbouring frame:
two poses cross-faded by opacity give two semi-transparent copies of the
creature with the page showing through, which reads as constant flicker; the
arc has enough frames to step honestly.
There is no mirroring at all: a flipped sprite moves the tail to the other side,
and that reads as a pose change rather than a head turn (owner: "let it sit in
one position"). So the creature sits in the bottom RIGHT corner — there the side
covered by the strip matches the direction toward the page; it runs in from the
right and leaves the same way. The arrival is played by the `sitdown` clip
rather than by swapping a frame: running at full speed and the sitting pose are
different pictures, and the seam between them was visible. After 1.2 s without a
pointer the frame smoothly returns to front-facing and `idle` resumes. Mirroring
always happens around ONE axis (`cx` of the reference clip), so a clip change does
not move the body, and the "ran up → sat down" seam plays as a turn: the body
squeezes horizontally to zero, exactly there the clip and the side change, and
it unfolds again facing the viewer. The element is 235 px: the sprite
cell is the generator's full frame, the fox fills ≈56 % of its height and its
paws fall at ≈77 %, so the element is lowered by 42 px and the paws stand 12 px
above the bottom edge of the window.

**Petting and "it will live here" (12.09).** The reaction is to STROKING the
creature with the mouse, not to a click: while the pointer is over it, the
distance travelled accumulates and is forgotten as soon as the pointer leaves,
so a hand resting in place does nothing while a moving one is felt. Threshold:
140 px within 1.5 s, at most once every 7 s; the `pet` clip was shot exactly for
this (closes its eyes, reaches toward the hand, returns to the starting pose).
For the FIRST 24 HOURS after the collection is complete, the creature lives on
workspace pages by default — this is part of the gift: it comes out on every
page, stays while the person is on it, and the appearance limit does not apply;
after 24 hours the rare quiet mode returns. The countdown runs from the
server-side `companion_since` stamp, so it is the same in all browsers and tabs.
The same mode can be switched on manually: `?fox=day` settles the creature on
workspace pages for 24 hours: it comes out on every visit, stays until the
person leaves the page, the daily limit and the dice roll do not apply, an
overlay scares it off but it returns after 12 s; `?fox=off` removes it. Stored
in the browser's localStorage, it does not affect other browsers.

**Rejected.** Hints from 5–6 cards on (they dilute the secret); the creature on
the report page (hot path, and screenshots for management); speech lines (one
extra phrase and it becomes "Clippy"); a video element per clip (gives no
frame-by-frame control for cursor tracking, much heavier) and animated GIF/APNG
(same); per-instance variations via seed — postponed to v2 (bandana).

**Verification.** `tests/smoke_quests_companion.py` (7/8 → nothing; 8/8 → block,
stamp set once, audit, toggles, overlay off, stats),
`tests/quests_companion_dom.js` (pages, budget, determinism),
`tests/smoke_migration.py` (0006), a headless-Chrome run of the intro/Den/theme/
appearance on a local server before showing it to the owner.

## ADR-21 — The "What we will read…" screen before a run (2026-10-20) · **ACCEPTED (half) + OPEN QUESTION**

**The owner's problem** (verbatim, translated): "sometimes I need to refresh a
report, but Anya renamed the files wrong, or I need to pick not all the files
from these folders; I want to control what gets read from the folders". Between
"connected the folders" and "Run" the person neither saw nor decided anything:
what exactly the engine had read became clear only from the result of the run.

**What was done.** A "What we will read…" button above Run (`static/js/gen-engine.js`)
lists the files of ALL connected folders — folder, name, size, date, the `inputs`
mask the file matched — and lets the user untick an unwanted file.
The list is built by the SAME `enumerate()` as the run, and the fate of each row
is decided by the same `excludedBy`: a screen that did its own counting would
lie one day.

No new exclusion mechanism had to be introduced — it already existed:
`input_overrides` on the (generator, source) pair with optimistic locking
on `version`. Exactly one thing in it is new: it can now be addressed the way
the person sees it before a run. `GET/PUT /api/dashboards/{id}/generator/
input-rules` accept the folder NAME rather than the source number, and create a
`data_sources` row if there has been no run on that folder yet (the drawer on
`/d/{id}/data-sources` can set a rule only on what has ALREADY been observed —
and an unwanted file needs to be untickable before the first run too). The
permission is `can_update_dashboard_data`, exactly the permission to run, with
the same `see_all` check for a report with RLS (ADR-18). In the audit log this
is the same `input_override_update` action, tagged `via: "run_preview"`;
otherwise existing queries over the log would stop seeing half of the edits. Not
a single request was added to the hot path of `/d/{id}`: both the list and the
rules are loaded when the button is pressed.

**Open question — "assign this file as input X".** The second half of the problem
("Anya renamed them wrong") is closed only by a diagnosis: the screen names the
files that matched no mask and the masks for which no file was found —
instead of a silent `not declared in inputs` and a failure on
`required_files` in the middle of the run. The assignment itself does not exist,
and it cannot be built without a schema change: the rule "read file
`Ops rdc health (1).xlsx` as `OPS_*.xlsx`" is a pair (declared input → observed
name), while `input_overrides` has only `exclude_rules` (a list of globs) and
`column_aliases` (COLUMN-level pairs, `{"<match>/<sheet>": {declared:
observed}}`). Putting file names into `column_aliases` under a reserved sheet
name is exactly the kind of trick that, half a year later, leaves nobody
understanding what the column holds.

**The minimal extension this requires** (owner decision; the schema is
someone else's area of work for today): a nullable `Text` column
`input_overrides.file_aliases`, JSON `{"<match>": ["<file name>", …]}`,
≤8 keys × ≤8 names, each name a literal file name (not a glob), `match`
validated against the `inputs` of the current version exactly as in `_clean_aliases`.
After the allow-list and BEFORE the exclusion rules, the engine sets
`matched_input = match` on the file whose name is listed, and counts such a file
as satisfying `required`. The mask semantics are not touched — the alias
NAMES a file rather than widening the glob, so no fourth semantics of "what was
read" appears and `globmatch` remains the only answer to that question. Cost:
a column in `app/models.py`, an Alembic revision, ~40 lines in the API and
~30 in the engine.

**Verification.** `tests/gen_preview_dom.js` (21 checks: list, masks, the server
rule is visible before the run, an unticked file does not get into the run, a 409
does not overwrite someone else's decision, a required mask predicts the
failure), the "input-rules" block in `tests/smoke_datasources.py` (14 checks:
rule, delivery to the engine via `source_rules`, 409, folder without a run, mask
semantics, permission).
