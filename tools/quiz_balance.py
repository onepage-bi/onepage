#!/usr/bin/env python3
"""Check the quiz bank for "giveaway by shape".

    tools/quiz_balance.py [file.json ...]

Without arguments it checks the standard bank: app/services/data/quiz_bank.json.

Why. A quiz is useless if a question can be answered without reading it.
Measurement of the bank on 05.10.2026: **the correct answer was the longest in 44 cases
out of 45**, the median spread of option lengths was 3.8x, in places 8.6x. So the
"click the longest" strategy gave a near-perfect score, and the points
went not to whoever thinks but to whoever noticed.

The course has had the same check for a long time (`tools/learn_balance.py`); it simply
never reached the quiz bank. The thresholds here are the same, deliberately: it is
the same person and the same way to cheat.

  • the correct option is the longest in no more than 30 % of questions;
  • within a question the longest option is no more than 1.45x the shortest;
  • each of the four correct-answer positions occurs in 15–35 % of cases.

Exit code 1 if any threshold is broken — so the check can sit
alongside the others before a commit.
"""
import collections
import glob
import json
import statistics
import sys

MAX_LONGEST_SHARE = 0.30
MAX_LEN_RATIO = 1.45
POS_MIN, POS_MAX = 0.15, 0.35

bad = 0
for path in (sys.argv[1:] or sorted(glob.glob("app/services/data/quiz_bank.json"))):
    items = json.load(open(path, encoding="utf-8"))
    if isinstance(items, dict):                       # in case of a {questions: [...]} wrapper
        items = items.get("questions") or items.get("bank") or []
    n = len(items)
    print("=" * 70)
    print(path, "—", n, "questions")
    if not n:
        print("  EMPTY")
        bad = 1
        continue

    longest = 0
    ratios = []
    pos = collections.Counter()
    for q in items:
        opts = q.get("o") or q.get("options") or []
        a = q.get("a", q.get("answer"))
        if len(opts) < 2 or not isinstance(a, int) or not (0 <= a < len(opts)):
            print("  BROKEN QUESTION id=%s" % q.get("id"))
            bad = 1
            continue
        lens = [len(x) for x in opts]
        if lens[a] == max(lens):
            longest += 1
        ratios.append((max(lens) / max(1, min(lens)), q.get("id"), (q.get("q") or "")[:52]))
        pos[a] += 1

    share = longest / n
    flag = "" if share <= MAX_LONGEST_SHARE else "  ← THRESHOLD %.0f%%" % (100 * MAX_LONGEST_SHARE)
    print("  correct is the longest: %d (%.0f%%)%s" % (longest, 100 * share, flag))
    if share > MAX_LONGEST_SHARE:
        bad = 1

    ratios.sort(reverse=True)
    med = statistics.median([r[0] for r in ratios])
    print("  length spread: median %.2fx, max %.2fx" % (med, ratios[0][0]))
    over = [r for r in ratios if r[0] > MAX_LEN_RATIO]
    if over:
        bad = 1
        print("  options of too different length (> %.2fx) — %d, worst:" % (MAX_LEN_RATIO, len(over)))
        for r, qid, text in over[:8]:
            print("    %.2fx  id=%-4s %s" % (r, qid, text))

    print("  correct position:", ", ".join(
        "%d — %d (%.0f%%)" % (k, v, 100 * v / n) for k, v in sorted(pos.items())))
    for k in range(4):
        sh = pos.get(k, 0) / n
        if not (POS_MIN <= sh <= POS_MAX):
            bad = 1
            print("    position %d is off: %.0f%% (norm %.0f–%.0f%%)"
                  % (k, 100 * sh, 100 * POS_MIN, 100 * POS_MAX))

sys.exit(1 if bad else 0)
