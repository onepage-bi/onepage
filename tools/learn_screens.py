#!/usr/bin/env python3
"""A course screen must make sense on its own.

    tools/learn_screens.py [app/content/learn.<course>.ru.json ...]

With no arguments it checks every course: app/content/learn.*.ru.json.

A card is a separate screen: the reader sees it alone, the previous one is gone,
the next one is not there yet. So the phrase "copy THIS query" on a screen with
no query is not a stylistic nitpick but broken logic: the pointer points at
nothing. That is exactly what happened in "First report" — the query sat on
the next screen and the sentence about it on the previous one.

The script looks for two classes of breakage:

  1. a pointer to a structural element (query, list, table, example, steps)
     that is not on the same card;
  2. a spatial pointer ("above", "below", "in the list on the left") that means
     nothing on a separate screen.

Exceptions are listed openly in ALLOW: "in the menu on the left" is about the
platform's real menu, not about a neighbouring card.
"""
import glob
import json
import re
import sys

# The patterns, CARRIES keys and ALLOW phrases below are DATA: Russian words
# matched against the Russian course text (learn.*.ru.json), not interface text.
NOUN = r"(?:запрос\w*|список\w*|списке|таблиц\w+|пример\w*|шаг\w*|правил\w*|" \
       r"чек-лист\w*|фрагмент\w*|схем\w+|врезк\w+)"

# "this query", "the following fragment" — a pointer before a word that
# names a DRAWN element. "this rule", "this step", "in this list" are not
# included: in prose they name what is listed right there in words, and catching
# them would drown the real breakages in noise (checked on the base course:
# 22 hits, two of them real).
DRAWN = r"(?:запрос\w*|фрагмент\w*|пример\w*|таблиц\w+|схем\w+|чек-лист\w*)"
POINTER = re.compile(r"\b(эт(?:от|у|им|ого|ому|о[тм]?)|такой|такую|следующ\w+|предыдущ\w+)\s+"
                     + DRAWN, re.I)

# "the table above", "in the list above", "the fragment below" — a pointer AFTER it.
# A bare "above"/"below" is not caught: in a course about the platform it is almost
# always a comparison ("below the threshold", "volume above the ceilings",
# "statuses 400 and above"), not a reference to a neighbouring screen.
SPATIAL = re.compile(r"\b" + NOUN + r"\s+(выше|ниже)\b|\b(выше|ниже)\s+" + NOUN, re.I)

# which card element makes the pointer honest
CARRIES = {
    "запрос": ("code_text",), "фрагмент": ("code_text",), "список": ("list",),
    "списке": ("list",), "врезк": ("note",), "таблиц": ("table",),
    "пример": ("code_text", "list", "table"), "шаг": ("list", "table"),
    "правил": ("list", "table"), "чек-лист": ("list", "table"),
    "схем": ("image", "table"),
}

# phrases where the pointer looks not at a neighbouring screen but at the interface
# Hand-checked phrases where the pointer does not look at a neighbouring screen:
# "in the menu on the left" — the platform's real menu; "the next query" in the
# chapter on lost runs — the next HTTP request, not the next card;
# "such a scheme" — the architecture described right there in words.
ALLOW = ("в меню слева", "ниже, для тех", "подробности — ниже",
         "следующий запрос на запуск", "объявляет следующий запрос",
         "делают такую схему рабочей")


def _carrier(noun: str):
    return next((v for k, v in CARRIES.items() if noun.lower().startswith(k)), ())


def _text(card) -> str:
    parts = [str(card.get(k) or "") for k in ("heading", "text", "note")]
    parts += list(card.get("list") or [])
    return "\n".join(parts)


def main(paths) -> int:
    bad = 0
    for path in paths:
        data = json.load(open(path, encoding="utf-8"))
        print("=" * 70)
        print(path)
        for unit in data.get("units") or [data]:
            for lesson in unit["lessons"]:
                for i, card in enumerate(lesson["cards"]):
                    body = _text(card)
                    low = body.lower()
                    if any(a in low for a in ALLOW):
                        continue
                    for rx in (POINTER, SPATIAL):
                        for m in rx.finditer(body):
                            noun = next((g for g in m.groups() if g and
                                         _carrier(g)), None) or ""
                            words = m.group(0).split()
                            noun = next((w for w in words if _carrier(w)), noun)
                            need = _carrier(noun)
                            if need and not any(card.get(f) for f in need):
                                print("  BAD %s#%d  \"%s\" — but this screen has "
                                      "no such thing" % (lesson["id"], i, m.group(0)))
                                bad += 1
    print("\n%s" % ("ok" if not bad else "broken pointers: %d" % bad))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:] or
                          sorted(glob.glob("app/content/learn.*.ru.json"))))
