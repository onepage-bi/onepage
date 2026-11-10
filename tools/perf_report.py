#!/usr/bin/env python3
"""Report over an exported OnePage perf log.

    python3 tools/perf_report.py perf-*.jsonl
    python3 tools/perf_report.py --compare old.jsonl new.jsonl

Reads JSONL from `GET /api/admin/perf/export` or files from `data/perflog/`
directly. Changes nothing.

Why this is a separate tool rather than the `/admin/perf` page: the page computes
over the live database for a window and cannot compare two builds, and it is
exactly the comparison that answers "did it get worse after the update?".

Weighting. Every record that survived sampling carries a `sample` field — the
probability with which it survived. Estimates are reconstructed Horvitz–Thompson
style (weight 1/sample); records without the stamp weigh 1 — either sampling was
off, or they are >= 400 responses, which are always emitted. Percentiles use a weighted
empirical distribution function; otherwise a rare slow request at a small
sample is underestimated by exactly the factor it was thinned by.
"""
import argparse, collections, datetime as dt, json, math, sys


def load(path):
    recs = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return recs


def server_records(recs):
    """Server request records only: no gc, no client beacons."""
    return sorted(
        (r for r in recs if r.get("t") is None and r.get("src") == "server"),
        key=lambda r: r.get("ts", 0),
    )


def weight(r):
    s = r.get("sample")
    return 1.0 / s if isinstance(s, (int, float)) and 0 < s < 1 else 1.0


def _safe(s):
    """Request paths reach this terminal verbatim — a 404 is logged for any URL
    a stranger cares to send, including one carrying escape sequences. Strip
    control bytes so a log line cannot repaint or drive the operator's shell."""
    return "".join(ch if ch.isprintable() else "?" for ch in str(s))


def wpct(pairs, q):
    """Weighted percentile. pairs = [(value, weight), ...]."""
    if not pairs:
        return float("nan")
    pairs = sorted(pairs)
    total = sum(w for _, w in pairs)
    target, acc = total * q, 0.0
    for v, w in pairs:
        acc += w
        if acc >= target:
            return v
    return pairs[-1][0]


def stage_kind(r):
    s = r.get("stages") or {}
    if "cache_hit" in s:
        return "hit"
    if "swr" in s:
        return "swr"
    if "coalesced" in s:
        return "coalesced"
    if s:
        return "build"
    return "-"


def summarize(recs, label):
    srv = server_records(recs)
    if not srv:
        print(f"{label}: no server records")
        return None
    t0, t1 = srv[0]["ts"], srv[-1]["ts"]
    span = max(t1 - t0, 1e-9)
    est = sum(weight(r) for r in srv)
    sampled = sum(1 for r in srv if "sample" in r)

    print(f"\n{'=' * 78}\n{label}\n{'=' * 78}")
    print(f"window:    {dt.datetime.fromtimestamp(t0, dt.UTC):%Y-%m-%d %H:%M} .. "
          f"{dt.datetime.fromtimestamp(t1, dt.UTC):%H:%M} UTC  ({span/60:.0f} min)")
    print(f"records:   {len(srv)}   estimated requests: {est:.0f}   "
          f"mean rate: {est/span:.2f} rps")
    if sampled:
        print(f"sampled: {sampled} stamped records — counters weighted by 1/sample")
    else:
        print("sampled: 0 stamped records — load below the ceiling, counters are exact")
    print(f"pid:       {dict(collections.Counter(r.get('pid') for r in srv))}")
    return srv


def route_table(srv, top=14):
    by = collections.defaultdict(list)
    for r in srv:
        by[r.get("route", "?")].append(r)
    print(f"\n{'route':<38}{'n':>5}{'est.':>7}{'p50':>8}{'p95':>8}{'p99':>8}{'max':>9}{'hdr50':>8}")
    rows = sorted(by.items(), key=lambda kv: -sum(weight(r) for r in kv[1]))[:top]
    for route, rs in rows:
        # `is not None`, not `in`: a client that aborts mid-response leaves the
        # record with hdr_ms=None and status=0 (9 such rows in the 23-26.07 prod
        # export), and sorting None against a float raised TypeError here.
        d = [(r["dur_ms"], weight(r)) for r in rs if r.get("dur_ms") is not None]
        h = [(r["hdr_ms"], weight(r)) for r in rs if r.get("hdr_ms") is not None]
        if not d:
            continue
        print(f"{route[:37]:<38}{len(rs):>5}{sum(w for _, w in d):>7.0f}"
              f"{wpct(d,.50):>8.1f}{wpct(d,.95):>8.1f}{wpct(d,.99):>8.1f}"
              f"{max(v for v,_ in d):>9.1f}{wpct(h,.50):>8.1f}")


def lane_stats(srv):
    fl = [r for r in srv if (r.get("stages") or {}).get("serve_us") is not None]
    if not fl:
        print("\nfast lane: no records with serve_us (build predates fast lane, or no warm hits)")
        return
    su = [(r["stages"]["serve_us"], weight(r)) for r in fl]
    fu = [(r["stages"].get("fast_us", 0), weight(r)) for r in fl]
    print(f"\nfast lane ({len(fl)} records):")
    print(f"  serve_us  p50={wpct(su,.5):8.1f}  p95={wpct(su,.95):8.1f}  "
          f"p99={wpct(su,.99):8.1f}  max={max(v for v,_ in su):8.1f}")
    print(f"  fast_us   p50={wpct(fu,.5):8.1f}  p95={wpct(fu,.95):8.1f}  "
          f"p99={wpct(fu,.99):8.1f}")


def cache_stats(srv):
    rend = [r for r in srv if r.get("route", "").startswith("/render/{dashboard_id}/")
            and "files" not in r.get("route", "") and "asset" not in r.get("route", "")]
    if not rend:
        return
    c = collections.Counter(stage_kind(r) for r in rend)
    tot = sum(c.values())
    hit = c["hit"] + c["swr"]
    print(f"\nrender cache: {tot} lookups, {hit} hits ({hit/tot*100:.0f}%)  {dict(c)}")


def encode_stats(srv):
    """Compression: where we spend CPU and what we get in return."""
    enc = [r for r in srv if (r.get("stages") or {}).get("enc_ms")]
    if not enc:
        return
    print(f"\non-the-fly compression: {len(enc)} responses")
    print(f"{'  route':<40}{'ms':>9}{'in MB':>10}{'out MB':>10}{'ratio':>9}")
    for r in sorted(enc, key=lambda r: -r["stages"]["enc_ms"])[:12]:
        s = r["stages"]
        i, o = s.get("enc_in", 0), s.get("enc_out", 0)
        ratio = o / i if i else float("nan")
        flag = "  <- wasted CPU" if ratio > 0.75 else ""
        print(f"  {r.get('route','?')[:37]:<38}{s['enc_ms']:>9.1f}"
              f"{i/1e6:>10.1f}{o/1e6:>10.1f}{ratio:>9.2f}{flag}")


def slow_list(srv, n=10):
    print(f"\n{n} slowest:")
    for r in sorted(srv, key=lambda r: -r.get("dur_ms", 0))[:n]:
        s = r.get("stages") or {}
        top = " ".join(f"{k}={v}" for k, v in sorted(
            ((k, v) for k, v in s.items() if isinstance(v, (int, float))),
            key=lambda kv: -kv[1])[:2])
        print(f"  {dt.datetime.fromtimestamp(r['ts'], dt.UTC):%m-%d %H:%M}"
              f"{r.get('dur_ms',0):9.1f}ms hdr={r.get('hdr_ms',0):8.1f} {r.get('status')} "
              f"{r.get('route','?')[:32]:<32} db={r.get('db_ms',0):.1f}/{r.get('db_n',0)} "
              f"{r.get('bytes',0)/1e6:.1f}MB {top}")


def errors(srv):
    errs = [r for r in srv if r.get("status", 200) >= 400]
    if not errs:
        print("\nresponses >= 400: none")
        return
    print(f"\nresponses >= 400: {len(errs)}  {dict(collections.Counter(r['status'] for r in errs))}")
    for r in sorted(errs, key=lambda r: -(r.get("dur_ms") or 0))[:5]:
        print(f"  {r['status']} {(r.get('dur_ms') or 0):8.1f}ms {_safe(r.get('path',''))[:64]}")


def report(path):
    srv = summarize(load(path), path)
    if not srv:
        return None
    route_table(srv)
    lane_stats(srv)
    cache_stats(srv)
    encode_stats(srv)
    slow_list(srv)
    errors(srv)
    return srv


def compare(a_path, b_path):
    a, b = report(a_path), report(b_path)
    if not a or not b:
        return
    print(f"\n{'=' * 78}\nCOMPARISON (common routes)\n{'=' * 78}")
    def by_route(srv):
        d = collections.defaultdict(list)
        for r in srv:
            if "dur_ms" in r:
                d[r.get("route", "?")].append((r["dur_ms"], weight(r)))
        return d
    ra, rb = by_route(a), by_route(b)
    common = sorted(set(ra) & set(rb), key=lambda k: -len(rb[k]))
    print(f"{'route':<38}{'p50 old':>9}{'p50 new':>11}{'Δ':>9}   "
          f"{'p99 old':>9}{'p99 new':>11}{'Δ':>9}")
    for k in common:
        a50, b50 = wpct(ra[k], .5), wpct(rb[k], .5)
        a99, b99 = wpct(ra[k], .99), wpct(rb[k], .99)
        d50 = (b50 - a50) / a50 * 100 if a50 else float("nan")
        d99 = (b99 - a99) / a99 * 100 if a99 else float("nan")
        print(f"{k[:37]:<38}{a50:>9.2f}{b50:>11.2f}{d50:>8.0f}%   "
              f"{a99:>9.2f}{b99:>11.2f}{d99:>8.0f}%")
    print("\nCaution: different windows mean different load and a different request mix.")
    print("Only routes with comparable n and no cold start in the window are comparable.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+")
    ap.add_argument("--compare", action="store_true",
                    help="compare exactly two files: before and after")
    ns = ap.parse_args()
    if ns.compare:
        if len(ns.files) != 2:
            sys.exit("--compare needs exactly two files: old new")
        compare(*ns.files)
    else:
        for p in ns.files:
            report(p)
