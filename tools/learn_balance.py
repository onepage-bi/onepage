#!/usr/bin/env python3
"""Check the course's answer options for "giveaway by shape".

    tools/learn_balance.py [app/content/learn.<course>.<lang>.json ...]

Without arguments it checks every course in both languages: app/content/learn.*.{ru,en}.json.


A course is useless if a question can be answered without reading it. The two most
common ways: the correct option is longer and more detailed than the others, and the correct option
sits in the same position. The script checks both.

"""
import json, sys, glob, collections, statistics

bad = 0
for path in (sys.argv[1:] or sorted(glob.glob("app/content/learn.*.ru.json") + glob.glob("app/content/learn.*.en.json"))):
    d = json.load(open(path, encoding="utf-8"))
    units = d.get("units") or [d]
    checks = [(l["id"], c) for u in units for l in u["lessons"] for c in l["checks"]]
    print("=" * 70)
    print(path, "—", len(checks), "questions")
    longest = 0
    ratios = []
    for lid, c in checks:
        lens = [len(o) for o in c["options"]]
        if lens.index(max(lens)) == c["answer"]:
            longest += 1
        ratio = max(lens) / max(1, min(lens))
        ratios.append((ratio, lid, c["q"][:50]))
        if ratio > 1.45:
            print("  option lengths differ by %.2fx · %s · %s" % (ratio, lid, c["q"][:60]))
            bad += 1
    dist = collections.Counter(c["answer"] for _, c in checks)
    share = {k: round(100 * v / len(checks)) for k, v in sorted(dist.items())}
    print("  correct = longest: %d of %d (norm ≤ 30%%)" % (longest, len(checks)))
    print("  correct position, %%:", share)
    if longest > 0.30 * len(checks):
        print("  BAD: the answer can be guessed by length"); bad += 1
    if any(v < 15 or v > 35 for v in share.values()) or len(share) < 4:
        print("  BAD: position skew"); bad += 1
    print("  mean length of correct %d, of wrong %d" % (
        statistics.mean([len(c["options"][c["answer"]]) for _, c in checks]),
        statistics.mean([len(o) for _, c in checks
                         for i, o in enumerate(c["options"]) if i != c["answer"]])))
print("\n%s" % ("ok" if not bad else "problems: %d" % bad))
sys.exit(1 if bad else 0)
