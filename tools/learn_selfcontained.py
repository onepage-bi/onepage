#!/usr/bin/env python3
"""Check: a course question must stand on its own.

    tools/learn_selfcontained.py [app/content/learn.<course>.<lang>.json ...]

Without arguments it checks every course in both languages: app/content/learn.*.{ru,en}.json.

Why. A chapter's questions end up in the block test, where the chapter is no longer at hand,
and even inside a chapter a person reads the question ten screens after the paragraph with the
numbers. So the stem must carry everything needed to choose: the scenario and
the quantities the correct answer rests on. A reference like "by the pilot's numbers"
turns the question into a memory test of one specific paragraph — and what we want to test is
reasoning.

The rule checked here:

1. the stem has no references to the text ("in this chapter", "above", "in the example",
   "the pilot", "as we discussed");
2. every number the CORRECT option rests on is either named in the stem or
   derived from quantities named in the stem — a machine cannot check the latter,
   so such questions are printed as "check manually", not as an error.

Wrong options may introduce their own numbers: a foreign figure in a wrong answer
is the trap itself, not a gap in the stem.
"""
from __future__ import annotations

import glob
import json
import re
import sys

# Reference phrases and units. The Cyrillic alternatives are DATA: they match the
# Russian course text (app/content/learn.*.ru.json) and must stay as they are. The
# Latin alternatives do the same job for the English course (learn.*.en.json).
REF = re.compile(r"\b(пилот\w*|в этой главе|выше по тексту|в примере выше|как мы (?:разобрали|видели)|"
                 r"в тексте главы|рассмотренн\w+ выше|"
                 r"pilot\w*|in this (?:chapter|lesson|section)|this lesson|earlier in the (?:text|lesson|chapter)|"
                 r"(?:in the )?example above|as we (?:saw|discussed|covered|showed)|in the (?:chapter|lesson) text|"
                 r"(?:discussed|covered|described|shown|mentioned) (?:above|earlier)|"
                 r"previous (?:lesson|chapter|section|example)|see above)\b", re.I)
NUM = re.compile(r"\d[\d \s.,]*\s?(?:МБ|ГБ|КБ|ГиБ|МиБ|мс|мкс|с|%|"
                 r"(?:MB|GB|KB|GiB|MiB|ms|µs|us|s)(?![A-Za-z]))")


def norm(s: str) -> str:
    return s.replace(" ", " ").replace(",", ".").replace(" ", "")


def main(paths: list[str]) -> int:
    hard = soft = 0
    for path in paths:
        d = json.load(open(path, encoding="utf-8"))
        units = d.get("units") or [d]
        for u in units:
            for l in u["lessons"]:
                for i, c in enumerate(l.get("checks") or []):
                    stem = c["q"]
                    if REF.search(stem):
                        print("REFERENCE %s#%d  %s" % (l["id"], i, stem[:90]))
                        hard += 1
                    right = c["options"][c["answer"]]
                    stem_nums = {norm(m.group(0)) for m in NUM.finditer(stem)}
                    missing = [m.group(0) for m in NUM.finditer(right)
                               if norm(m.group(0)) not in stem_nums]
                    if missing:
                        print("CHECK     %s#%d  the correct answer rests on %s, which are not in the stem"
                              % (l["id"], i, ", ".join(sorted(set(missing))[:3])))
                        print("          %s" % stem[:100])
                        soft += 1
    print("\nreferences to the text: %d · questions to check manually: %d" % (hard, soft))
    return 1 if hard else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:] or sorted(glob.glob("app/content/learn.*.ru.json") + glob.glob("app/content/learn.*.en.json"))))
