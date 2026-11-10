"""Smoke test: card draw, levels, short names (phase 2).

The statistical part: 100,000 draws with deterministic seeds;
shares must match the weights within ±1.5 percentage points.
"""
import os
import sys
import tempfile
from collections import Counter

TMP = tempfile.mkdtemp(prefix="op-quests-rng-")
os.environ.update({
    "OP_DB_PATH": f"{TMP}/onepage.db",
    "OP_UPLOADS_DIR": f"{TMP}/uploads",
    "OP_PERF_DIR": f"{TMP}/perflog",
    "OP_CACHE_WARM": "0",
    "OP_QUESTS_ENABLED": "true",
})
sys.path.insert(0, os.getcwd())
from app.services import quests as q  # noqa: E402

FAILURES = []
N = 100_000
BOOSTED = ("rare", "epic", "legendary")


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


def sample(streak, rare_x2=False, tag=""):
    """Card shares over N draws (seeds are deterministic, so the test is reproducible)."""
    c = Counter(q.draw(streak, rare_x2=rare_x2, seed=f"{tag}-{i}")["card_id"] for i in range(N))
    return {cid: c[cid] / N for cid, _r, _w in q.CARDS}


def main():
    # ── card registry ────────────────────────────────────────────
    ids = [cid for cid, _r, _w in q.CARDS]
    check("8 cards", len(q.CARDS) == 8, len(q.CARDS))
    check("ids are unique", len(set(ids)) == 8)
    check("base weights sum to 100", sum(w for _c, _r, w in q.CARDS) == 100,
          sum(w for _c, _r, w in q.CARDS))
    check("CARD_META covers every card", set(q.CARD_META) == set(ids), set(q.CARD_META) ^ set(ids))
    check("rarity in META matches CARDS",
          all(q.CARD_META[cid]["rarity"] == rar for cid, rar, _w in q.CARDS))
    check("every card has a name and a description",
          all(q.CARD_META[c]["name"] and q.CARD_META[c]["desc"] for c in ids))

    # ── determinism ──────────────────────────────────────────────
    a = q.draw(3, seed="fixed-seed")
    b = q.draw(3, seed="fixed-seed")
    check("one seed -> one result", a == b, (a, b))
    check("seed is returned in the response", a["seed"] == "fixed-seed", a["seed"])
    check("rarity agrees with card_id", a["rarity"] == q.CARD_META[a["card_id"]]["rarity"], a)
    check("roll within the weight total", 0 <= a["roll"] < sum(a["weights"]), a["roll"])
    auto = q.draw(0)
    check("without a seed one is generated", isinstance(auto["seed"], str) and len(auto["seed"]) == 16, auto["seed"])
    check("two auto draws have different seeds", q.draw(0)["seed"] != q.draw(0)["seed"])
    check("rare_x2 changes the result for the same seed",
          any(q.draw(0, seed=f"s{i}")["card_id"] != q.draw(0, rare_x2=True, seed=f"s{i}")["card_id"]
              for i in range(50)))

    # ── statistics at streak 0 ───────────────────────────────────
    base = {cid: w / 100.0 for cid, _r, w in q.CARDS}
    s0 = sample(0, tag="s0")
    for cid, _r, _w in q.CARDS:
        d = abs(s0[cid] - base[cid])
        check(f"share of {cid} ~= base weight", d <= 0.015, f"{s0[cid]:.4f} vs {base[cid]:.4f}")
    check("shares sum to 1", abs(sum(s0.values()) - 1.0) < 1e-9, sum(s0.values()))

    rare_of = lambda sh: sum(sh[cid] for cid, r, _w in q.CARDS if r in BOOSTED)  # noqa: E731
    s10 = sample(10, tag="s10")
    s0x2 = sample(0, rare_x2=True, tag="s0x2")
    r0, r10, r0x2 = rare_of(s0), rare_of(s10), rare_of(s0x2)
    check("streak 10 gives more rares than 0", r10 > r0 + 0.05, f"{r10:.4f} vs {r0:.4f}")
    check("rare_x2 raises the rare share at the same streak", r0x2 > r0 + 0.10, f"{r0x2:.4f} vs {r0:.4f}")
    check("rare weights under rare_x2 are exactly doubled",
          all(abs(wx - 2 * w0) < 1e-9 for (_c, r, _b), w0, wx
              in zip(q.CARDS, q.weights(0), q.weights(0, True)) if r in BOOSTED))
    check("common weights under rare_x2 are unchanged",
          all(wx == w0 for (_c, r, _b), w0, wx
              in zip(q.CARDS, q.weights(0), q.weights(0, True)) if r == "common"))
    check("boost caps at streak 10", q.weights(10) == q.weights(50), (q.weights(10), q.weights(50)))
    check("negative streak = 0", q.weights(-5) == q.weights(0))
    check("weights are monotonic in the streak",
          all(rare_of({c: w for (c, _r, _b), w in zip(q.CARDS, q.weights(n))})
              <= rare_of({c: w for (c, _r, _b), w in zip(q.CARDS, q.weights(n + 1))})
              for n in range(0, 12)))

    # ── chances ──────────────────────────────────────────────────
    for st, x2 in [(0, False), (4, False), (10, True), (99, True)]:
        ch = q.chances(st, x2)
        check(f"chances({st},{x2}) has 8 rows", len(ch) == 8, len(ch))
        check(f"chances({st},{x2}) sums to 100", abs(sum(c["pct"] for c in ch) - 100.0) < 0.01,
              sum(c["pct"] for c in ch))
    ch0 = {c["card_id"]: c["pct"] for c in q.chances(0)}
    check("chances(0) = base weights", all(abs(ch0[cid] - w) < 1e-9 for cid, _r, w in q.CARDS), ch0)
    check("chances matches the empirical shares at streak 0",
          all(abs(ch0[cid] / 100.0 - s0[cid]) <= 0.015 for cid in ch0))

    # ── levels ───────────────────────────────────────────────────
    for n, want in [(0, "Novice"), (7, "Novice"), (8, "Practitioner"), (17, "Practitioner"),
                    (18, "Confident user"), (29, "Confident user"),
                    (30, "Expert"), (500, "Expert")]:
        check(f"level({n}) = {want}", q.level(n) == want, q.level(n))
    check("level(None) does not crash", q.level(None) == "Novice", q.level(None))
    # The title is already translated: a Russian user gets it in Russian.
    # Cyrillic on purpose: checks the Russian language pack (ru.json).
    from app import i18n as _i18n
    _i18n.load()
    _tok = _i18n.set_lang("ru")
    try:
        for n, want in [(0, "Новичок"), (8, "Практик"), (18, "Уверенный пользователь"),
                        (30, "Эксперт")]:
            check(f"ru: level({n}) = {want}", q.level(n) == want, q.level(n))
    finally:
        _i18n.reset(_tok)
    check("LEVELS has 4 ascending tiers",
          [t for t, _n in q.LEVELS] == sorted(t for t, _n in q.LEVELS) and len(q.LEVELS) == 4)

    # ── short names ──────────────────────────────────────────────
    for dn, ovid, want in [
        ("Ivanov Ivan", "10000002", "Ivanov I."),
        ("Ivanov Ivan Petrovich", "10000002", "Ivanov I."),
        ("  Ivanov   Ivan  ", "10000002", "Ivanov I."),
        (None, "10000002", "—"),
        ("", "10000002", "—"),
        ("   ", "10000002", "—"),
        ("Ivanov", "10000002", "Ivanov"),
    ]:
        got = q.short_name(dn, ovid)
        check(f"short_name({dn!r})", got == want, f"got={got!r} want={want!r}")

    # ── quest registry (phase 3) ─────────────────────────────────
    check("five quests in a fixed order",
          [x["id"] for x in q.QUESTS] == ["pres", "fav", "theme", "grid", "share"],
          str([x["id"] for x in q.QUESTS]))
    check("quest id <= 10 chars (source <= 16)",
          all(len(x["id"]) <= 10 for x in q.QUESTS), str([x["id"] for x in q.QUESTS]))
    check("`quest:<id>` fits into quest_openings.source",
          all(len("quest:" + x["id"]) <= 16 for x in q.QUESTS))
    check("points 20/10/10/10/15",
          [x["points"] for x in q.QUESTS] == [20, 10, 10, 10, 15],
          str([x["points"] for x in q.QUESTS]))
    check("surprise only for server-verified quests",
          [x["surprise"] for x in q.QUESTS] == [True, True, False, False, True],
          str([x["surprise"] for x in q.QUESTS]))
    check("WATCHED_ACTIONS is exactly four audit actions",
          q.WATCHED_ACTIONS == ("favorite.add", "presentation_create",
                                "presentation_item_add", "share_link_create"),
          str(q.WATCHED_ACTIONS))
    check("client events: theme -> theme, view -> grid",
          q.CLIENT_EVENT_QUESTS == {"theme": "theme", "view": "grid"},
          str(q.CLIENT_EVENT_QUESTS))

    # ── phase 4 stubs ────────────────────────────────────────────
    for fn in (q.quiz_question, q.quiz_answer):
        try:
            fn()
            check(f"{fn.__name__} is a stub", False, "did not raise NotImplementedError")
        except NotImplementedError:
            check(f"{fn.__name__} is a stub", True)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
