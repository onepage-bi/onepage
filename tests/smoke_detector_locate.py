"""op_locate — "Show in report" (P2).

Guards the *injection* side of the protocol: the command lives in the one
annotate controller (a second pass over the HTML would cost a full regex sweep
of a multi-megabyte report), it goes in exactly once per render, and nothing in
it can close the injected <script> early.

The behaviour of the controller's JS is tested separately, in a fake DOM:
    node tests/detector_locate_dom.js

Run: python tests/smoke_detector_locate.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import detector  # noqa: E402

FAIL = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else "  <-- " + str(extra)))
    if not cond:
        FAIL.append(label)


CTRL = detector._ANNOTATE_CONTROLLER
BODY = CTRL[CTRL.index(">") + 1:CTRL.rindex("</script>")]

# ── the command is there, in the annotate controller ─────────────────────
check("controller answers op_locate", "m.type==='op_locate'" in CTRL)
check("controller replies op_located", "type:'op_located'" in CTRL)
check("reply carries ok/tag/text", all(k in CTRL for k in ("ok:!!el", "tag:", "text:")))
check("flash class is op-located", "op-located" in CTRL and ".op-located{" in CTRL)
check("flash outline is 2px currentColor", "outline:2px solid currentColor" in CTRL)
check("flash has a 6px radius", "border-radius:6px" in CTRL)
check("flash fades over 1.6s", "op-located-fade 1.6s" in CTRL)
check("prefers-reduced-motion drops the animation",
      "prefers-reduced-motion:reduce" in CTRL and "animation:none" in CTRL)
check("the class is removed after 2s", "},2000);" in CTRL)
check("scrollIntoView centres the hit",
      "scrollIntoView({block:'center'" in CTRL and "behavior:'smooth'" in CTRL)
check("node budget caps the sweep", "LOC_MAX=20000" in CTRL)
check("script/style/svg-hostile subtrees are skipped",
      "SCRIPT:1" in CTRL and "STYLE:1" in CTRL and "TEMPLATE:1" in CTRL)
check("svg <text> is still searchable (no SVG in the skip list)",
      "SVG:1" not in CTRL and "TEXT:1" not in CTRL)

# ── nothing in the injected script can close it early ────────────────────
check("no bare </script> inside the controller body",
      "</script>" not in BODY, BODY.count("</script>"))
check("the controller closes exactly once", CTRL.count("</script>") == 1)
check("no unescaped </ sequence that could end the tag",
      re.search(r"</\s*script", BODY, re.I) is None)

# ── injection: once, idempotent, head-anchored ───────────────────────────
HTML = "<!doctype html><html><head><meta charset='utf-8'><title>t</title></head><body>" \
       "<div class='kpi'>Revenue</div><div class='val'>1 234,6</div></body></html>"

MARK = "m.type==='op_locate'"

out = detector.inject_annotate(HTML)
check("injected once", out.count(MARK) == 1, out.count(MARK))
check("injected right after <head>", re.search(r"<head[^>]*>\s*<script", out) is not None)
check("original markup survives", "Revenue" in out and "1 234,6" in out)
check("the same render twice is byte-identical", detector.inject_annotate(HTML) == out)
check("only the first <head> is used",
      detector.inject_annotate("<head></head><head></head>").count(MARK) == 1)

headless = "<div>no head at all</div>"
check("a headless fragment still gets the controller",
      detector.inject_annotate(headless).count(MARK) == 1)
check("a headless fragment keeps its content",
      "no head at all" in detector.inject_annotate(headless))

# Injection is NOT self-idempotent (a second call injects a second controller);
# what keeps a render to exactly one is that files.py calls it exactly once.
check("a double call would stack — hence the single call site below",
      detector.inject_annotate(out).count(MARK) == 2)

# A report whose own text contains </script> must not confuse the injection
hostile = "<html><head></head><body><pre>&lt;/script&gt; op_locate</pre></body></html>"
check("report text mentioning op_locate is not counted as an injection",
      detector.inject_annotate(hostile).count(MARK) == 1)

# ── the render path really injects it (no TestClient needed) ─────────────
src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "app", "api", "files.py"), encoding="utf-8").read()
check("files.py injects the annotate controller exactly once",
      src.count("detector.inject_annotate(") == 1, src.count("detector.inject_annotate("))

print()
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
    sys.exit(1)
print("ALL PASS")
