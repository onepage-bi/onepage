#!/usr/bin/env python3
"""Build REPORT_GUIDE.html out of REPORT_GUIDE.md.

    tools/report_guide_html.py            # rebuild
    tools/report_guide_html.py --check    # fail if the html is out of date

WHY THIS EXISTS. The html twin was written by hand in July 2026 and then sat
untouched while the markdown grew: by November it was three and a half months
behind, described features that had changed, and nothing in the repository
pointed that out. A second copy with no generator does not stay in sync — it
only looks like documentation. Now there is one source and one command, and
`--check` puts the question into the test run so the answer stops depending on
memory.

No markdown library: the project ships no such dependency and this guide uses
a small, known subset — headings, paragraphs, lists, tables, fenced code,
block quotes, and inline code/bold/links. A hand-written converter for that
subset is a few hundred lines and has no supply chain. It is deliberately
strict: anything it does not recognise is passed through escaped rather than
guessed at, so a new construct shows up as visible plain text instead of
silently disappearing.

ON THE DESIGN. Nothing here is invented. The page is built out of OnePage's
own design system, with the values lifted from `static/css/main.css`: the
OKLCH neutral ramp on hue 250, electric cobalt `oklch(0.700 0.180 245)` as the
accent, the semantic success/warning/danger triple, the 2–8 px radius family,
Inter Tight for prose and JetBrains Mono for anything that is a number, a
label or code. The dark slab that carries the title is the idiom `docs.css`
already uses for the Dev Guide hero. A guide to the platform that looked like
a stranger to the platform would be its own counter-example.

The fonts are referenced by RELATIVE path on purpose: opened from a clone the
page shows the product's real faces, and anywhere else it degrades to the
system stack without a request leaving the machine. The page stays a single
self-contained file — no build step, no network, open it and read.

Numbers printed in the header are COUNTED from the document (sections,
anti-patterns, the revision date the markdown states itself). Hard-coding them
would re-create, in the generator, exactly the staleness this generator
exists to end.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "REPORT_GUIDE.md"
OUT = ROOT / "REPORT_GUIDE.html"

# ── the product's fonts, by relative path ─────────────────────────────────
# `local()` comes first: if Inter Tight is installed on the system, the file
# is never read at all. The latin+cyrillic subsets match what the product
# ships; `unicode-range` means the cyrillic files are fetched only if the
# page actually contains such characters.
FONTS = """
@font-face{font-family:'Inter Tight';font-weight:400;font-display:swap;
  src:local('Inter Tight'),url(static/fonts/inter-tight-400-latin.woff2) format('woff2')}
@font-face{font-family:'Inter Tight';font-weight:400;font-display:swap;
  unicode-range:U+0301,U+0400-045F,U+0490-0491,U+04B0-04B1,U+2116;
  src:local('Inter Tight'),url(static/fonts/inter-tight-400-cyrillic.woff2) format('woff2')}
@font-face{font-family:'Inter Tight';font-weight:500;font-display:swap;
  src:local('Inter Tight'),url(static/fonts/inter-tight-500-latin.woff2) format('woff2')}
@font-face{font-family:'Inter Tight';font-weight:500;font-display:swap;
  unicode-range:U+0301,U+0400-045F,U+0490-0491,U+04B0-04B1,U+2116;
  src:local('Inter Tight'),url(static/fonts/inter-tight-500-cyrillic.woff2) format('woff2')}
@font-face{font-family:'Inter Tight';font-weight:600;font-display:swap;
  src:local('Inter Tight'),url(static/fonts/inter-tight-600-latin.woff2) format('woff2')}
@font-face{font-family:'Inter Tight';font-weight:600;font-display:swap;
  unicode-range:U+0301,U+0400-045F,U+0490-0491,U+04B0-04B1,U+2116;
  src:local('Inter Tight'),url(static/fonts/inter-tight-600-cyrillic.woff2) format('woff2')}
@font-face{font-family:'JetBrains Mono';font-weight:400;font-display:swap;
  src:local('JetBrains Mono'),url(static/fonts/jetbrains-mono-400-latin.woff2) format('woff2')}
@font-face{font-family:'JetBrains Mono';font-weight:400;font-display:swap;
  unicode-range:U+0301,U+0400-045F,U+0490-0491,U+04B0-04B1,U+2116;
  src:local('JetBrains Mono'),url(static/fonts/jetbrains-mono-400-cyrillic.woff2) format('woff2')}
@font-face{font-family:'JetBrains Mono';font-weight:500;font-display:swap;
  src:local('JetBrains Mono'),url(static/fonts/jetbrains-mono-500-latin.woff2) format('woff2')}
"""

# ── tokens: values taken from static/css/main.css, not invented ───────────
STYLE = FONTS + """
:root{
  --font-sans:"Inter Tight","Inter",-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  --font-mono:"JetBrains Mono",ui-monospace,"SF Mono",Menlo,monospace;
  --bg:oklch(0.985 0.002 250); --s1:oklch(1 0 0); --s2:oklch(0.965 0.003 250);
  --s3:oklch(0.945 0.004 250); --inset:oklch(0.955 0.003 250);
  --line:oklch(0.860 0.008 250); --line-soft:oklch(0.905 0.006 250);
  --line-strong:oklch(0.755 0.010 250);
  --ink:oklch(0.205 0.010 250); --ink-muted:oklch(0.395 0.010 250);
  --ink-dim:oklch(0.555 0.010 250); --ink-faint:oklch(0.685 0.008 250);
  --accent:oklch(0.700 0.180 245);
  --accent-soft:color-mix(in oklab,var(--accent) 12%,transparent);
  --success:oklch(0.580 0.140 155); --warning:oklch(0.660 0.150 70);
  --danger:oklch(0.575 0.205 25);
  --danger-soft:color-mix(in oklab,var(--danger) 10%,transparent);
  --r4:4px; --r6:6px; --r8:8px;
  --ease:cubic-bezier(.2,.8,.2,1);
}
/* The dark theme is declared twice, and guarded: the system preference must
   not override a person's explicit choice, and an explicit choice must work
   whatever the system says. Components are styled through tokens only. */
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --bg:oklch(0.165 0.005 250); --s1:oklch(0.205 0.006 250); --s2:oklch(0.245 0.007 250);
  --s3:oklch(0.285 0.008 250); --inset:oklch(0.145 0.005 250);
  --line:oklch(0.305 0.008 250); --line-soft:oklch(0.255 0.007 250);
  --line-strong:oklch(0.395 0.010 250);
  --ink:oklch(0.965 0.003 250); --ink-muted:oklch(0.775 0.006 250);
  --ink-dim:oklch(0.605 0.008 250); --ink-faint:oklch(0.475 0.008 250);
  --success:oklch(0.700 0.140 155); --warning:oklch(0.770 0.140 75);
  --danger:oklch(0.665 0.195 25);
}}
:root[data-theme="dark"]{
  --bg:oklch(0.165 0.005 250); --s1:oklch(0.205 0.006 250); --s2:oklch(0.245 0.007 250);
  --s3:oklch(0.285 0.008 250); --inset:oklch(0.145 0.005 250);
  --line:oklch(0.305 0.008 250); --line-soft:oklch(0.255 0.007 250);
  --line-strong:oklch(0.395 0.010 250);
  --ink:oklch(0.965 0.003 250); --ink-muted:oklch(0.775 0.006 250);
  --ink-dim:oklch(0.605 0.008 250); --ink-faint:oklch(0.475 0.008 250);
  --success:oklch(0.700 0.140 155); --warning:oklch(0.770 0.140 75);
  --danger:oklch(0.665 0.195 25);
}
*{box-sizing:border-box}
html{scroll-behavior:smooth;-webkit-text-size-adjust:100%}
@media (prefers-reduced-motion:reduce){html{scroll-behavior:auto}
  *{animation-duration:.01ms!important;transition-duration:.01ms!important}}
body{margin:0;background:var(--bg);color:var(--ink);
  font:400 16px/1.62 var(--font-sans);
  font-feature-settings:"cv05","ss03";-webkit-font-smoothing:antialiased}
::selection{background:color-mix(in oklab,var(--accent) 22%,transparent)}
code,pre,.mono{font-family:var(--font-mono);font-variant-ligatures:none}

/* ── title slab: dark in every theme, the docs.css idiom ──────────────── */
.hero{background:oklch(0.135 0.008 255);color:oklch(0.965 0.003 250);
  border-bottom:1px solid oklch(0.285 0.010 255)}
.hero-in{max-width:1180px;margin:0 auto;padding:48px 32px 0}
.eyebrow{font:500 11px/1 var(--font-mono);letter-spacing:.14em;text-transform:uppercase;
  color:oklch(0.700 0.180 245);margin:0 0 18px;display:flex;align-items:center;gap:12px}
.eyebrow::after{content:"";height:1px;flex:1;
  background:linear-gradient(90deg,oklch(0.395 0.010 250),transparent)}
.hero h1{font:600 clamp(28px,4.2vw,42px)/1.14 var(--font-sans);letter-spacing:-.022em;
  margin:0 0 16px;max-width:22ch;text-wrap:balance}
.hero-lead{max-width:62ch;margin:0 0 32px;color:oklch(0.775 0.006 250);font-size:16.5px}
.hero-lead strong{color:oklch(0.965 0.003 250);font-weight:500}
.hero-lead code{background:oklch(0.245 0.007 250);color:oklch(0.900 0.005 250);
  padding:1px 5px;border-radius:var(--r4);font-size:.9em}
/* Readout row: the numbers are counted from the document, not typed in. */
.readout{display:flex;flex-wrap:wrap;gap:0;margin:0;border-top:1px solid oklch(0.255 0.007 250)}
.readout div{padding:16px 32px 18px 0;margin-right:32px;border-right:1px solid oklch(0.255 0.007 250)}
.readout div:last-child{border-right:0}
.readout b{display:block;font:500 25px/1.1 var(--font-mono);font-variant-numeric:tabular-nums;
  letter-spacing:-.02em;color:oklch(0.965 0.003 250)}
.readout span{display:block;margin-top:5px;font-size:12.5px;color:oklch(0.605 0.008 250)}

/* ── two columns: the rail and the prose ──────────────────────────────── */
.wrap{max-width:1180px;margin:0 auto;padding:0 32px 96px;
  display:grid;grid-template-columns:216px minmax(0,1fr);gap:56px;align-items:start}
.rail{position:sticky;top:0;max-height:100vh;overflow-y:auto;
  padding:40px 0 40px;font-size:13px;line-height:1.4;
  scrollbar-width:thin;scrollbar-color:var(--line) transparent}
.rail-h{font:500 10px/1 var(--font-mono);letter-spacing:.14em;text-transform:uppercase;
  color:var(--ink-faint);margin:0 0 14px}
.rail a{display:flex;gap:9px;color:var(--ink-dim);text-decoration:none;
  padding:5px 0 5px 11px;margin-left:-13px;border-left:2px solid transparent;
  transition:color var(--ease) 120ms,border-color var(--ease) 120ms}
.rail a i{font:400 11px/1.5 var(--font-mono);color:var(--ink-faint);
  font-variant-numeric:tabular-nums;font-style:normal;min-width:19px}
.rail a:hover{color:var(--ink)}
.rail a.cur{color:var(--accent);border-left-color:var(--accent)}
.rail a.cur i{color:var(--accent)}

.doc{min-width:0;padding:52px 0 0;max-width:74ch}
.doc>p,.doc>ul,.doc>ol{margin:0 0 18px}
.doc>p{color:var(--ink-muted)}
.doc>p strong,.doc li strong{color:var(--ink);font-weight:600}

/* The section number hangs in the margin, in mono — it is an index, not a word. */
h2{font:600 24px/1.25 var(--font-sans);letter-spacing:-.018em;
  margin:56px 0 18px;padding-top:28px;border-top:1px solid var(--line-soft);
  scroll-margin-top:16px;position:relative;text-wrap:balance}
h2 .num{position:absolute;left:-72px;top:30px;width:56px;text-align:right;
  font:500 12px/1.6 var(--font-mono);color:var(--ink-faint);
  font-variant-numeric:tabular-nums;letter-spacing:0}
h2:first-child{margin-top:0;padding-top:0;border-top:0}
@media (max-width:1100px){h2 .num{position:static;display:inline-block;width:auto;
  text-align:left;margin-right:10px;color:var(--accent)}}
h3{font:600 17px/1.35 var(--font-sans);letter-spacing:-.01em;
  margin:32px 0 10px;color:var(--ink);scroll-margin-top:16px}
.anchor{opacity:0;margin-left:10px;font:400 .62em/1 var(--font-mono);
  color:var(--ink-faint);text-decoration:none;vertical-align:middle;
  transition:opacity var(--ease) 120ms}
h2:hover .anchor,h3:hover .anchor,.anchor:focus-visible{opacity:1}
.anchor:hover{color:var(--accent)}

a{color:var(--accent);text-decoration-color:color-mix(in oklab,var(--accent) 35%,transparent);
  text-underline-offset:3px}
a:hover{text-decoration-color:var(--accent)}
:focus-visible{outline:2px solid var(--accent);outline-offset:3px;border-radius:var(--r4)}

ul,ol{padding-left:24px}
li{margin:0 0 8px;color:var(--ink-muted)}
li::marker{color:var(--ink-faint)}

code{background:var(--inset);color:var(--ink);border:1px solid var(--line-soft);
  padding:.5px 5px;border-radius:var(--r4);font-size:.875em;white-space:nowrap;
  -webkit-box-decoration-break:clone;box-decoration-break:clone}
pre{background:var(--inset);border:1px solid var(--line-soft);border-radius:var(--r8);
  padding:18px 20px;overflow-x:auto;margin:0 0 22px;
  box-shadow:0 1px 0 0 color-mix(in oklab,var(--ink) 4%,transparent)}
pre code{background:none;border:0;padding:0;white-space:pre;
  font-size:13px;line-height:1.6;color:var(--ink-muted)}

/* A callout is a voice from the margin, so it is a marked margin, not a card. */
blockquote{margin:0 0 22px;padding:2px 0 2px 20px;
  border-left:2px solid var(--accent);color:var(--ink-muted)}
blockquote p{margin:0}
blockquote strong{color:var(--ink)}

table{border-collapse:separate;border-spacing:0;width:100%;margin:0 0 24px;
  font-size:14.5px;font-variant-numeric:tabular-nums;
  border:1px solid var(--line);border-radius:var(--r8);overflow:hidden;display:table}
.tw{overflow-x:auto;margin:0 0 24px;-webkit-overflow-scrolling:touch}
.tw table{margin:0}
th{background:var(--s2);color:var(--ink);font:600 12.5px/1.4 var(--font-sans);
  text-align:left;padding:11px 14px;border-bottom:1px solid var(--line);
  white-space:nowrap;letter-spacing:.01em}
td{padding:11px 14px;border-bottom:1px solid var(--line-soft);
  color:var(--ink-muted);vertical-align:top}
tbody tr:last-child td{border-bottom:0}
tbody tr:hover td{background:var(--s2)}
td:first-child{color:var(--ink);font-weight:500}
td code{white-space:normal}

hr{border:0;height:1px;background:var(--line-soft);margin:40px 0}
.doc>hr:first-child{display:none}
.doc h2:first-of-type{margin-top:0;padding-top:0;border-top:0}

.tail{margin:56px 0 0;padding-top:20px;border-top:1px solid var(--line-soft);
  color:var(--ink-faint);font-size:12.5px;max-width:74ch}
.tail code{white-space:normal}

/* Theme toggle: the only control on the page. */
.theme{position:fixed;right:18px;top:18px;z-index:10;
  display:flex;align-items:center;gap:7px;height:30px;padding:0 12px;
  background:color-mix(in oklab,var(--s1) 86%,transparent);
  border:1px solid var(--line);border-radius:999px;color:var(--ink-dim);
  font:500 11px/1 var(--font-mono);letter-spacing:.06em;text-transform:uppercase;
  cursor:pointer;backdrop-filter:blur(8px);
  transition:color var(--ease) 120ms,border-color var(--ease) 120ms}
.theme:hover{color:var(--ink);border-color:var(--line-strong)}

@media (max-width:1100px){
  .wrap{grid-template-columns:1fr;gap:0;padding:0 24px 72px}
  .rail{display:none}
  .doc{padding-top:30px}
  .hero-in{padding:36px 24px 0}
  .readout div{padding-right:22px;margin-right:22px}
}
@media (max-width:620px){
  body{font-size:15.5px}
  .hero-in{padding:28px 16px 0}
  .wrap{padding:0 16px 56px}
  .readout div{border-right:0;padding:12px 0 0;margin:0;width:50%}
  .readout b{font-size:21px}
  th{white-space:normal}
}
@media print{
  .theme,.rail,.anchor{display:none}
  body{background:#fff;color:#111;font-size:11pt}
  .hero{background:#fff;color:#111;border-bottom:1px solid #ccc}
  .hero h1,.readout b,.hero-lead strong{color:#111}
  .hero-lead,.readout span{color:#444}
  .wrap{display:block;max-width:none;padding:0}
  .doc{max-width:none}
  h2{break-after:avoid}pre,table,blockquote{break-inside:avoid}
  a{color:#111;text-decoration:none}
}
"""

SCRIPT = """
// The rail highlights the section being read. IntersectionObserver rather
// than a scroll listener: the browser reports the intersection, so there is
// no need to compute positions on every frame.
(function(){
  var links={},cur=null;
  document.querySelectorAll('.rail a').forEach(function(a){
    links[a.getAttribute('href').slice(1)]=a;});
  var heads=[].slice.call(document.querySelectorAll('.doc h2[id]'));
  if(!heads.length||!window.IntersectionObserver)return;
  function mark(id){var a=links[id];if(!a||a===cur)return;
    if(cur)cur.classList.remove('cur');a.classList.add('cur');cur=a;}
  var seen={};
  var io=new IntersectionObserver(function(es){
    es.forEach(function(e){seen[e.target.id]=e.isIntersecting?e.boundingClientRect.top:null;});
    // The active heading is the lowest one that has already passed the top of
    // the screen: otherwise, scrolling up, the highlight jumps to the next section.
    var best=null;
    heads.forEach(function(h){var r=h.getBoundingClientRect();
      if(r.top<140)best=h.id;});
    mark(best||heads[0].id);
  },{rootMargin:'-130px 0px -70% 0px',threshold:[0,1]});
  heads.forEach(function(h){io.observe(h);});
})();

// Theme: auto -> light -> dark. Reads and writes sit in try/catch — in a
// private window, and for a file opened from disk, storage may throw, and the
// page must keep working.
(function(){
  var btn=document.querySelector('.theme');if(!btn)return;
  var NAMES={auto:'Auto',light:'Light',dark:'Dark'},ORDER=['auto','light','dark'];
  function read(){try{return localStorage.getItem('op-guide-theme')||'auto'}catch(e){return 'auto'}}
  function apply(m){
    if(m==='auto')document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme',m);
    btn.textContent=NAMES[m];btn.setAttribute('aria-label','Theme: '+NAMES[m]);}
  var mode=read();apply(ORDER.indexOf(mode)<0?'auto':mode);
  btn.addEventListener('click',function(){
    mode=ORDER[(ORDER.indexOf(mode)+1)%3];apply(mode);
    try{localStorage.setItem('op-guide-theme',mode)}catch(e){}});
})();
"""

INLINE_CODE = re.compile(r"`([^`]+)`")
BOLD = re.compile(r"\*\*([^*]+)\*\*")
ITALIC = re.compile(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])")
LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
NUMBERED = re.compile(r"^(\d+[a-z]?)\.\s+(.*)$")


def plain(text: str) -> str:
    return ITALIC.sub(r"\1", BOLD.sub(r"\1", INLINE_CODE.sub(r"\1", text)))


def slug(text: str) -> str:
    """GitHub-compatible anchor, so links work in both renderings."""
    t = plain(text).lower().strip()
    t = re.sub(r"[^\w\s-]", "", t, flags=re.UNICODE)
    # Every space becomes a hyphen and is NOT collapsed: GitHub strips the
    # punctuation first and only then replaces spaces, so "snapshot + data"
    # gives `snapshot--data` with two hyphens. Collapsing gave a dead link.
    return re.sub(r"\s", "-", t)


def inline(text: str) -> str:
    """Escape first, then re-introduce the few markups we support."""
    out = html.escape(text, quote=False)
    out = INLINE_CODE.sub(lambda m: f"<code>{m.group(1)}</code>", out)
    out = BOLD.sub(lambda m: f"<strong>{m.group(1)}</strong>", out)
    out = ITALIC.sub(lambda m: f"<em>{m.group(1)}</em>", out)
    out = LINK.sub(
        lambda m: f'<a href="{html.escape(m.group(2), quote=True)}">{m.group(1)}</a>', out)
    return out


def blocks(lines: list[str]) -> tuple[list[str], list[tuple[int, str, str]], list[tuple[str, int]]]:
    """Markdown -> html fragments, plus the headings and the tables we saw.

    Headings feed the rail. Each table is reported as (section anchor, row
    count) so the header can state how many anti-patterns the summary holds
    without re-parsing the html it just produced.
    """
    out: list[str] = []
    heads: list[tuple[int, str, str]] = []
    tables: list[tuple[str, int]] = []
    section = ""
    i = 0
    while i < len(lines):
        line = lines[i]

        if line.startswith("```"):
            i += 1
            body: list[str] = []
            while i < len(lines) and not lines[i].startswith("```"):
                body.append(lines[i])
                i += 1
            i += 1
            out.append("<pre><code>" + html.escape("\n".join(body), quote=False)
                       + "</code></pre>")
            continue

        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            text = line[level:].strip()
            tag = f"h{min(level, 3)}"
            anchor = slug(text)
            heads.append((min(level, 3), anchor, text))
            if level == 2:
                section = anchor
            if level == 1:
                i += 1
                continue  # the document title lives in the slab, not in the prose
            num = ""
            m = NUMBERED.match(text)
            if m and level == 2:
                num = f'<span class="num">§{m.group(1)}</span>'
            out.append(f'<{tag} id="{anchor}">{num}{inline(text)}'
                       f'<a class="anchor" href="#{anchor}" aria-label="Link to this section">#</a>'
                       f'</{tag}>')
            i += 1
            continue

        if line.strip() in ("---", "***", "___"):
            out.append("<hr>")
            i += 1
            continue

        if line.startswith(">"):
            body = []
            while i < len(lines) and lines[i].startswith(">"):
                body.append(lines[i].lstrip(">").strip())
                i += 1
            out.append("<blockquote><p>" + inline(" ".join(body)) + "</p></blockquote>")
            continue

        if (line.lstrip().startswith("|") and i + 1 < len(lines)
                and set(lines[i + 1].replace("|", "").replace(" ", "")) <= set("-:")
                and lines[i + 1].count("|") >= 2):
            def cells(row: str) -> list[str]:
                return [c.strip() for c in row.strip().strip("|").split("|")]
            head = cells(lines[i])
            i += 2
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append(cells(lines[i]))
                i += 1
            tables.append((section, len(rows)))
            # A wrapper with its own scroll: a table wider than the column
            # must not make the whole page scroll sideways.
            out.append('<div class="tw"><table><thead><tr>'
                       + "".join(f"<th>{inline(c)}</th>" for c in head)
                       + "</tr></thead><tbody>"
                       + "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r)
                                 + "</tr>" for r in rows)
                       + "</tbody></table></div>")
            continue

        if re.match(r"^(\s*)([-*]|\d+\.)\s+", line):
            ordered = not re.match(r"^(\s*)([-*])\s+", line)
            tag = "ol" if ordered else "ul"
            items: list[str] = []
            while i < len(lines):
                mm = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", lines[i])
                if mm:
                    items.append(inline(mm.group(3)))
                    i += 1
                elif lines[i].startswith("  ") and lines[i].strip() and items:
                    items[-1] += " " + inline(lines[i].strip())  # continuation line
                    i += 1
                else:
                    break
            out.append(f"<{tag}>" + "".join(f"<li>{it}</li>" for it in items) + f"</{tag}>")
            continue

        if not line.strip():
            i += 1
            continue

        para = []
        while (i < len(lines) and lines[i].strip()
               and not lines[i].startswith(("#", ">", "|", "```"))
               and not re.match(r"^(\s*)([-*]|\d+\.)\s+", lines[i])
               and lines[i].strip() not in ("---", "***", "___")):
            para.append(lines[i].strip())
            i += 1
        out.append("<p>" + inline(" ".join(para)) + "</p>")
    return out, heads, tables


def render(md: str) -> str:
    lines = md.split("\n")
    frags, heads, tables = blocks(lines)

    # ── title slab ─────────────────────────────────────────────────────────
    h1 = next((t for lvl, _a, t in heads if lvl == 1), "OnePage")
    lead = ""
    after_h1 = md.split("\n", 1)[1] if md.startswith("# ") else md
    m = re.search(r"\n\n(\*\*[^\n]+(?:\n[^\n#>|][^\n]*)*)", "\n" + after_h1)
    if m:
        lead = inline(" ".join(x.strip() for x in m.group(1).split("\n")))
        frags = [f for f in frags if f != "<p>" + lead + "</p>"]

    # The numbers in the slab are COUNTED from the document. Typing them in
    # would re-create, in the generator, the very staleness it was written to end.
    sections = sum(1 for lvl, _a, t in heads if lvl == 2 and NUMBERED.match(t))
    anti = sum(n for anchor, n in tables if anchor.startswith("12-"))
    rev = re.search(r"Revised (\d{4}-\d{2}-\d{2})", md)

    stats = [(str(sections), "sections"),
             (str(anti), "anti-patterns in the summary")]
    if rev:
        stats.append((rev.group(1), "revised"))
    readout = ('<div class="readout">'
               + "".join(f"<div><b>{html.escape(v)}</b><span>{html.escape(k)}</span></div>"
                         for v, k in stats)
               + "</div>")

    rail = []
    for lvl, anchor, text in heads:
        if lvl != 2:
            continue
        m2 = NUMBERED.match(text)
        # The label is cut at the first bracket, dash or colon: a full heading
        # such as "1. How the platform serves a report (the model every rule
        # follows from)" takes five lines in a 216 px column and cannot be read.
        body = m2.group(2) if m2 else plain(text)
        short = re.split(r"\s+\(|\s+—\s+|:\s+", plain(body))[0]
        mark = f"§{m2.group(1)}" if m2 else ""
        rail.append(f'<a href="#{anchor}"><i>{mark}</i><span>'
                    f'{html.escape(short, quote=False)}</span></a>')

    title = plain(h1)
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html.escape(title)}</title>\n"
        '<meta name="color-scheme" content="light dark">\n'
        f"<style>{STYLE}</style>\n</head>\n<body>\n"
        '<button class="theme" type="button">Auto</button>\n'
        '<header class="hero"><div class="hero-in">\n'
        '<p class="eyebrow">Engineering guide</p>\n'
        f"<h1>{inline(h1)}</h1>\n"
        + (f'<p class="hero-lead">{lead}</p>\n' if lead else "")
        + readout + "\n</div></header>\n"
        '<div class="wrap">\n'
        '<nav class="rail"><p class="rail-h">Sections</p>' + "".join(rail) + "</nav>\n"
        '<main class="doc">\n' + "\n".join(frags) + "\n"
        '<p class="tail">Built from <code>REPORT_GUIDE.md</code> by '
        '<code>tools/report_guide_html.py</code>. Edit the markdown — it is the source; '
        "this page is rebuilt from it, and the test run catches any drift.</p>\n"
        "</main>\n</div>\n"
        f"<script>{SCRIPT}</script>\n</body>\n</html>\n"
    )


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="do not write; check that the html matches the markdown")
    args = ap.parse_args(argv)

    built = render(SRC.read_text(encoding="utf-8"))
    if args.check:
        have = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if have == built:
            print("ok    REPORT_GUIDE.html matches the markdown")
            return 0
        print("DRIFT REPORT_GUIDE.html is out of date — rebuild: tools/report_guide_html.py")
        print("  markdown %s, html %s"
              % (hashlib.sha256(built.encode()).hexdigest()[:12],
                 hashlib.sha256(have.encode()).hexdigest()[:12]))
        return 1
    OUT.write_text(built, encoding="utf-8")
    print("built %s (%d KB) from %s" % (OUT.name, len(built) // 1024, SRC.name))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
