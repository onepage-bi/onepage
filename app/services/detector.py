"""
Detect dashboard type and count file slots from raw HTML.
"""
from __future__ import annotations

import re

from app.models import DASH_INTERACTIVE, DASH_SNAPSHOT, DASH_STATIC

_RE_FILE_INPUT = re.compile(r'<input[^>]+type=["\']file["\']', re.IGNORECASE)
_RE_SNAPSHOT = re.compile(
    r'<script[^>]+id=["\']bootstrap-snapshot["\']', re.IGNORECASE
)


def detect_type(html: str) -> str:
    if _RE_SNAPSHOT.search(html):
        return DASH_SNAPSHOT
    if _RE_FILE_INPUT.search(html):
        return DASH_INTERACTIVE
    return DASH_STATIC


def count_file_slots(html: str) -> int:
    return len(_RE_FILE_INPUT.findall(html))


_HEIGHT_REPORTER = """<script>
/* OnePage height reporter — injected */
(function(){
  function _opSendH() {
    var h = Math.max(
      document.documentElement.scrollHeight,
      document.documentElement.offsetHeight,
      document.body ? document.body.scrollHeight : 0,
      document.body ? document.body.offsetHeight : 0
    );
    window.parent.postMessage({ type: 'op_height', height: h }, '*');
  }
  if (document.readyState === 'complete') { _opSendH(); }
  else { window.addEventListener('load', _opSendH); }
  var _opHTimer;
  if (window.ResizeObserver) {
    new ResizeObserver(function() {
      clearTimeout(_opHTimer); _opHTimer = setTimeout(_opSendH, 80);
    }).observe(document.documentElement);
  }
  window.addEventListener('resize', function() {
    clearTimeout(_opHTimer); _opHTimer = setTimeout(_opSendH, 80);
  });
})();
</script>"""


# Link interceptor for navigators. Comments inside the string are in English,
# like the neighbouring injected scripts: the catalogue linter reads the literal as
# interface text. The full rationale is here.
#
# The page lives in an iframe without `allow-same-origin`, and
# we do not have `allow-popups-to-escape-sandbox`: both a nested frame and a tab
# opened from INSIDE the page inherit the sandbox and an opaque origin. The browser does not
# give them another site's cookies, so Power BI Report Server with
# Windows authentication shows a white screen (owner's observation,
# 29.10.2026). The inheritance cannot be removed; removing the sandbox would mean letting
# uploaded HTML read the platform session. So the tab is opened by the
# SHELL outside the sandbox, and the page only names the address.
#
# NO decision about whether the address may be followed is made here:
# the scheme and destination are checked by the parent (`viewer.js::pageLinkAction`). The code
# inside the sandbox is the page author's code, and its checks cannot be trusted
# by construction.
_LINK_BRIDGE = """<script>
/* OnePage navigator link bridge — injected for dash_type="page".
   The sandboxed page cannot open a usable tab itself: popups inherit the
   sandbox and its opaque origin, so cookies never reach the target. The
   shell opens them instead; this only names the address. */
(function(){
  function ask(url, target) {
    try {
      window.parent.postMessage({ type: 'op_open', url: String(url || ''),
                                  target: String(target || '') }, '*');
    } catch (e) { /* no parent: the page is open directly, leave it alone */ }
  }
  document.addEventListener('click', function (e) {
    if (e.defaultPrevented) return;
    var a = e.target && e.target.closest ? e.target.closest('a[href]') : null;
    if (!a) return;
    var href = a.getAttribute('href') || '';
    /* In-page anchor: ordinary scrolling, nothing to intercept. */
    if (!href || href.charAt(0) === '#') return;
    e.preventDefault();
    /* A UNC path must travel RAW: the parser reads backslashes as slashes
       and a.href would already be http://server/share — a dead tab instead
       of a copyable path. */
    ask(href.slice(0, 2) === '\\\\\\\\' ? href : (a.href || href), a.getAttribute('target') || '');
  }, true);
  /* Middle click and Ctrl+click would open the same dead sandboxed tab. */
  document.addEventListener('auxclick', function (e) {
    if (e.button !== 1) return;
    var a = e.target && e.target.closest ? e.target.closest('a[href]') : null;
    if (!a) return;
    e.preventDefault();
    var h = a.getAttribute('href') || '';
    ask(h.slice(0, 2) === '\\\\\\\\' ? h : a.href, '_blank');
  }, true);
  /* The page's own script must go through the shell too, or the author
     writes window.open and gets exactly the same empty tab. */
  try {
    window.open = function (url, target) { ask(url, target); return null; };
  } catch (e) { /* override refused: clicks are still intercepted */ }
})();
</script>"""


def inject_link_bridge(html: str) -> str:
    """Link interceptor for navigators (`dash_type="page"`).

    Injected ONLY for this type: in an ordinary report links behave as
    before, and changing that retroactively is not allowed.
    """
    injected = [False]

    def _replacer(m):
        injected[0] = True
        return m.group(1) + '\n' + _LINK_BRIDGE

    result = re.sub(r'(<head\b[^>]*>)', _replacer, html, count=1, flags=re.IGNORECASE)
    if not injected[0]:
        result = _LINK_BRIDGE + html
    return result


def inject_height_reporter(html: str) -> str:
    """Inject height reporter script for snapshot/static dashboards."""
    injected = [False]

    def _replacer(m):
        injected[0] = True
        return m.group(1) + '\n' + _HEIGHT_REPORTER

    result = re.sub(r'(<head\b[^>]*>)', _replacer, html, count=1, flags=re.IGNORECASE)
    if not injected[0]:
        result = _HEIGHT_REPORTER + html
    return result


def inject_ls_state(html: str, ls_json: str) -> str:
    """Inject saved localStorage state as window.__OP_LS__ right after <head>.

    The `<` escape neutralises any `</script>` in the saved value, exactly as
    inject_textedit does: json.dumps escapes neither `<` nor `/`, so without it
    a can_update_data collaborator could close the seed tag from stored data.
    In JSON `<` only ever occurs inside a string, where \\u003c is the same
    character — the parsed value is unchanged.
    """
    if "<" in ls_json:
        ls_json = ls_json.replace("<", "\\u003c")
    script = f'<script>window.__OP_LS__ = {ls_json};</script>'
    injected = [False]

    def _replacer(m):
        injected[0] = True
        return m.group(1) + '\n' + script

    result = re.sub(r'(<head\b[^>]*>)', _replacer, html, count=1, flags=re.IGNORECASE)
    if not injected[0]:
        result = script + html
    return result


def inject_bootstrap(html: str, slots_json: str) -> str:
    """Insert OnePage bootstrap <script> right after <head> tag."""
    bootstrap = f"""<script>
/* OnePage platform bootstrap — injected */
(function(){{
  // localStorage proxy: sandboxed iframes block real localStorage.
  // Reads from window.__OP_LS__ (injected at render time), writes back via op_save_ls.
  var _ls = window.__OP_LS__ || {{}};
  var _lsTimer;
  function _opSaveLs() {{
    clearTimeout(_lsTimer);
    _lsTimer = setTimeout(function() {{
      window.parent.postMessage({{ type: 'op_save_ls', data: _ls }}, '*');
    }}, 500);
  }}
  var _opLsProxy = {{
    getItem: function(k) {{ return Object.prototype.hasOwnProperty.call(_ls, k) ? _ls[k] : null; }},
    setItem: function(k, v) {{ _ls[k] = String(v); _opSaveLs(); }},
    removeItem: function(k) {{ delete _ls[k]; _opSaveLs(); }},
    clear: function() {{ _ls = {{}}; _opSaveLs(); }},
    get length() {{ return Object.keys(_ls).length; }},
    key: function(i) {{ return Object.keys(_ls)[i] || null; }},
  }};
  try {{
    Object.defineProperty(window, 'localStorage', {{ get: function() {{ return _opLsProxy; }}, configurable: true }});
  }} catch(_e) {{ window.localStorage = _opLsProxy; }}

  window.__OP_SLOTS__ = {slots_json};

  function _opSendH() {{
    var h = Math.max(
      document.documentElement.scrollHeight,
      document.documentElement.offsetHeight,
      document.body ? document.body.scrollHeight : 0,
      document.body ? document.body.offsetHeight : 0
    );
    window.parent.postMessage({{ type: 'op_height', height: h }}, '*');
  }}
  if (document.readyState === 'complete') {{ _opSendH(); }}
  else {{ window.addEventListener('load', _opSendH); }}
  var _opHTimer;
  if (window.ResizeObserver) {{
    new ResizeObserver(function() {{
      clearTimeout(_opHTimer); _opHTimer = setTimeout(_opSendH, 80);
    }}).observe(document.documentElement);
  }}

  window.addEventListener('load', function() {{
    window.parent.postMessage({{ type: 'op_ready', slots: window.__OP_SLOTS__ }}, '*');
  }});

  var _opLoading = false;

  window.addEventListener('message', function(e) {{
    if (!e.data || e.data.type !== 'op_file') return;
    var slot = e.data.slot;
    var file = new File([e.data.data], e.data.name);
    var inputs = document.querySelectorAll('input[type="file"]');
    if (!inputs[slot]) return;
    var dt = new DataTransfer();
    dt.items.add(file);
    _opLoading = true;
    inputs[slot].files = dt.files;
    inputs[slot].dispatchEvent(new Event('change', {{ bubbles: true }}));
    _opLoading = false;
  }});

  document.addEventListener('change', function(e) {{
    if (_opLoading) return;
    if (e.target.type !== 'file' || !e.target.files[0]) return;
    var inputs = Array.from(document.querySelectorAll('input[type="file"]'));
    var slot = inputs.indexOf(e.target);
    if (slot === -1) return;
    var file = e.target.files[0];
    var reader = new FileReader();
    reader.onload = function(ev) {{
      window.parent.postMessage({{
        type: 'op_save_file',
        slot: slot,
        name: file.name,
        data: ev.target.result
      }}, '*', [ev.target.result]);
    }};
    reader.readAsArrayBuffer(file);
  }}, true);
}})();
</script>"""
    # Insert right after <head> (case-insensitive)
    # Use lambda to avoid re.sub interpreting \uXXXX escapes in replacement string (Python 3.12+)
    injected = [False]

    def _replacer(m):
        injected[0] = True
        return m.group(1) + '\n' + bootstrap

    result = re.sub(r'(<head\b[^>]*>)', _replacer, html, count=1, flags=re.IGNORECASE)
    if not injected[0]:
        result = bootstrap + html
    return result


_ANNOTATE_CONTROLLER = """<script>
/* OnePage annotate controller — injected, dormant until activated by the parent */
(function(){
  var ACTIVE=false, pins=[], hl=null, nextId=1;
  function ensureHl(){
    if(hl) return hl;
    hl=document.createElement('div');
    hl.style.cssText='position:fixed;z-index:2147483646;pointer-events:none;border:2px solid #4c8bf5;background:rgba(76,139,245,.12);border-radius:3px;display:none';
    (document.body||document.documentElement).appendChild(hl);
    return hl;
  }
  function cssEsc(s){ return String(s).replace(/[^a-zA-Z0-9_-]/g,'\\\\$&'); }
  function selectorFor(el){
    if(!el||el.nodeType!==1) return '';
    var parts=[], cur=el, depth=0;
    while(cur&&cur.nodeType===1&&cur.tagName.toLowerCase()!=='html'&&depth<8){
      var tag=cur.tagName.toLowerCase();
      if(cur.id){ parts.unshift(tag+'#'+cssEsc(cur.id)); break; }
      var seg=tag;
      var cls=(cur.getAttribute('class')||'').trim().split(/\\s+/).filter(Boolean).slice(0,2);
      if(cls.length) seg+='.'+cls.map(cssEsc).join('.');
      var parent=cur.parentElement;
      if(parent){
        var same=Array.prototype.filter.call(parent.children,function(c){return c.tagName===cur.tagName;});
        if(same.length>1) seg+=':nth-of-type('+(same.indexOf(cur)+1)+')';
      }
      parts.unshift(seg);
      cur=cur.parentElement; depth++;
    }
    return parts.join(' > ');
  }
  function stylesOf(el){
    var cs=getComputedStyle(el), keys=['display','position','width','height','margin','padding','font-size','font-weight','color','background-color','border','flex','grid-template-columns','text-align'], o={};
    keys.forEach(function(k){ var v=cs.getPropertyValue(k); if(v&&v!=='none'&&v!=='normal'&&v!=='auto'&&v!=='0px'&&v!=='rgba(0, 0, 0, 0)') o[k]=v; });
    return o;
  }
  function rectOf(el){ var r=el.getBoundingClientRect(); return {x:Math.round(r.left),y:Math.round(r.top),w:Math.round(r.width),h:Math.round(r.height)}; }
  function placeBadge(p){ var r=p.el.getBoundingClientRect(); p.badge.style.left=(window.scrollX+r.left)+'px'; p.badge.style.top=(window.scrollY+r.top)+'px'; }
  function repositionAll(){ pins.forEach(placeBadge); }
  function onMove(e){
    if(!ACTIVE) return;
    var el=e.target;
    if(!el||el.nodeType!==1||el.__opaBadge) return;
    var r=el.getBoundingClientRect(), h=ensureHl();
    h.style.display='block'; h.style.left=r.left+'px'; h.style.top=r.top+'px'; h.style.width=r.width+'px'; h.style.height=r.height+'px';
  }
  function suppress(e){ if(ACTIVE){ e.preventDefault(); e.stopPropagation(); if(e.stopImmediatePropagation) e.stopImmediatePropagation(); } }
  function onClick(e){
    if(!ACTIVE) return;
    suppress(e);
    var el=e.target;
    if(!el||el.nodeType!==1||el.__opaBadge) return;
    var id=nextId++;
    var badge=document.createElement('div');
    badge.__opaBadge=true; badge.textContent=id;
    badge.style.cssText='position:absolute;z-index:2147483647;background:#4c8bf5;color:#fff;font:bold 11px/18px sans-serif;min-width:18px;height:18px;padding:0 4px;border-radius:9px;text-align:center;box-shadow:0 1px 4px rgba(0,0,0,.4);pointer-events:none;transform:translate(-50%,-50%)';
    (document.body||document.documentElement).appendChild(badge);
    var p={id:id,el:el,badge:badge}; pins.push(p); placeBadge(p);
    window.parent.postMessage({type:'op_pick', annotation:{
      id:id, selector:selectorFor(el), tag:el.tagName.toLowerCase(),
      classes:(el.getAttribute('class')||''), elId:el.id||'',
      text:(el.innerText||el.textContent||'').trim().slice(0,300),
      html:(el.outerHTML||'').slice(0,2000), styles:stylesOf(el), rect:rectOf(el)
    }}, '*');
  }
  function setActive(on){ ACTIVE=on; if(hl) hl.style.display='none'; (document.body||document.documentElement).style.cursor=on?'crosshair':''; }
  function clearPins(){ pins.forEach(function(p){ if(p.badge.parentNode) p.badge.parentNode.removeChild(p.badge); }); pins=[]; nextId=1; }
  function removePin(id){ for(var i=0;i<pins.length;i++){ if(pins[i].id===id){ if(pins[i].badge.parentNode) pins[i].badge.parentNode.removeChild(pins[i].badge); pins.splice(i,1); break; } } }
  /* ── op_locate: find a KPI/value in the report and flash it ─────────
     Answers the chat's "show it in the report" button. Search is textContent
     only (leaf elements, headings, th, caption): the report is a stranger's
     document, so nothing but reading and one CSS class is done to it. The
     question text `q` is carried for symmetry but never searched — a whole
     sentence never occurs in a cell. */
  var LOC_MAX=20000, locStyled=false, locTimer=null, locEl=null;
  var LOC_SKIP={SCRIPT:1,STYLE:1,NOSCRIPT:1,TEMPLATE:1,HEAD:1,IFRAME:1,CANVAS:1,OBJECT:1,VIDEO:1,AUDIO:1};
  var LOC_CSS='.op-located{outline:2px solid currentColor !important;outline-offset:2px;border-radius:6px;animation:op-located-fade 1.6s ease-out forwards}'
    +'@keyframes op-located-fade{0%{outline-color:currentColor}60%{outline-color:currentColor}100%{outline-color:transparent}}'
    +'@media (prefers-reduced-motion:reduce){.op-located{animation:none;outline-color:currentColor}}';
  function locStyle(){
    if(locStyled) return; locStyled=true;
    try{ var st=document.createElement('style'); st.id='op-located-style'; st.textContent=LOC_CSS;
      (document.head||document.documentElement).appendChild(st); }catch(err){}
  }
  function locStr(v,max){ if(typeof v!=='string'&&typeof v!=='number') return ''; return String(v).slice(0,max||200); }
  function locNorm(s){ return String(s==null?'':s).replace(/[\\s\\u00a0]+/g,' ').trim(); }
  function locFold(s){ return locNorm(s).toLowerCase(); }
  function locParseNum(tok){
    /* the chat sends the DISPLAYED value, unit and all (percent sign, currency,
       a unit word); the number is what gets compared (stand 2026-09-07). */
    var s=String(tok).replace(/[\\s\\u00a0']/g,'').replace(/^[^\\d-]+/,'').replace(/[^\\d.,]+$/,'');
    if(/^-?\\d{1,3}(?:,\\d{3})+(?:\\.\\d+)?$/.test(s)) s=s.replace(/,/g,'');
    else s=s.replace(/,/g,'.');
    if(!/^-?\\d+(?:\\.\\d+)?$/.test(s)) return null;
    var f=parseFloat(s);
    if(!isFinite(f)) return null;
    return {v:f,d:(s.split('.')[1]||'').length};
  }
  function locNumHit(text,target){
    var re=/-?\\d[\\d\\s\\u00a0.,']*\\d|-?\\d/g, m, n=0;
    while((m=re.exec(text))!==null&&n++<200){
      var p=locParseNum(m[0]); if(!p) continue;
      if(p.v===target) return true;
      var f=Math.pow(10,p.d);
      if(Math.round(target*f)/f===p.v) return true;
    }
    return false;
  }
  function locLeaves(){
    var out=[], seen=0, stack=[document.body||document.documentElement];
    while(stack.length){
      var el=stack.pop();
      if(!el||el.nodeType!==1) continue;
      var tag=String(el.tagName||'').toUpperCase();
      if(LOC_SKIP[tag]===1||el.__opaBadge) continue;
      if(++seen>LOC_MAX) break;
      /* pushed back-to-front so the stack yields document order: with two
         equally good hits the earlier one in the report wins, and the node
         budget cuts the tail of a giant report, not a random slice of it. */
      var kids=el.children||[];
      for(var i=kids.length-1;i>=0;i--){
        var k=kids[i], kt=String(k.tagName||'').toUpperCase();
        if(LOC_SKIP[kt]===1||k.__opaBadge) continue;
        stack.push(k);
      }
      /* A candidate is a real leaf (no element children at all) or a
         heading-ish cell. An element whose children were all skipped is NOT a
         candidate: its textContent would carry the <script>/<style> source we
         just refused to search. */
      if(kids.length===0||tag==='TH'||tag==='CAPTION'||/^H[1-6]$/.test(tag)) out.push(el);
    }
    return out;
  }
  /* ── where a leaf sits: its table row and its column header ──────────
     A number alone is ambiguous — «100,0%» stands in a dozen cells of one
     table. What makes a cell THE answer is its context: the row that names
     the week and the header that names the client. Outside tables the three
     nearest containers stand in for that. */
  function locCells(tr){ var out=[],k=tr.children||[],i; for(i=0;i<k.length;i++){ var t=String(k[i].tagName||'').toUpperCase(); if(t==='TD'||t==='TH') out.push(k[i]); } return out; }
  function locSpan(c){ var v=parseInt(c.getAttribute?c.getAttribute('colspan'):'',10); return isFinite(v)&&v>0?v:1; }
  function locRows(table){ var out=[],stack=[table],n=0; while(stack.length&&n++<5000){ var e=stack.pop(),k=e.children||[],i; for(i=k.length-1;i>=0;i--){ var t=String(k[i].tagName||'').toUpperCase(); if(t==='TR') out.push(k[i]); else if(t!=='TABLE') stack.push(k[i]); } } return out; }
  function locCtx(node){
    var ctx=[],tr=null,cell=null,e=node,k,i,j;
    for(k=0;k<8&&e&&e.nodeType===1;k++){ var tg=String(e.tagName||'').toUpperCase(); if((tg==='TD'||tg==='TH')&&!cell) cell=e; if(tg==='TR'){ tr=e; break; } e=e.parentNode; }
    if(tr){
      ctx.push(tr.textContent||'');
      if(cell){
        var cells=locCells(tr),idx=0;
        for(i=0;i<cells.length&&cells[i]!==cell;i++) idx+=locSpan(cells[i]);
        var table=tr.parentNode; while(table&&table.nodeType===1&&String(table.tagName||'').toUpperCase()!=='TABLE') table=table.parentNode;
        if(table&&table.nodeType===1){
          var rows=locRows(table);
          for(i=0;i<rows.length&&i<3;i++){ if(rows[i]===tr) break; var hc=locCells(rows[i]),acc=0; for(j=0;j<hc.length;j++){ var sp=locSpan(hc[j]); if(idx>=acc&&idx<acc+sp){ ctx.push(hc[j].textContent||''); break; } acc+=sp; } }
        }
      }
    } else {
      var par=node.parentNode;
      for(k=0;k<3&&par&&par.nodeType===1;k++){ var tx=par.textContent||''; if(tx.length<=600) ctx.push(tx); par=par.parentNode; }
    }
    return locFold(ctx.join(' | '));
  }
  function locLabelScore(ctx,labels){
    var sc=0,i;
    for(i=0;i<labels.length;i++){ var lf=locFold(labels[i]); if(lf.length<2||ctx.indexOf(lf)<0) continue; sc+=(/[a-z\\u0430-\\u044f]/i.test(lf)||lf.length>=3)?100+Math.min(lf.length,20):10; }
    return sc;
  }
  function locFind(value,labels){
    var nodes=locLeaves(), texts=[], i, j, best=null, bestScore=-1;
    for(i=0;i<nodes.length;i++){
      var raw=nodes[i].textContent||'';
      texts[i]=(raw.length>4000)?'':locNorm(raw);
    }
    var vf=locFold(value), vp=vf?locParseNum(vf):null, hits=0, bestLabel=0;
    if(vf){
      for(i=0;i<nodes.length;i++){
        var t1=texts[i]; if(!t1) continue;
        /* a share can be printed as 0.9999 or as 99,99 % — both are the value */
        if(locFold(t1).indexOf(vf)<0&&!(vp&&(locNumHit(t1,vp.v)||locNumHit(t1,vp.v*100)||locNumHit(t1,vp.v/100)))) continue;
        hits++;
        var ls=locLabelScore(locCtx(nodes[i]),labels);
        if(ls>bestLabel) bestLabel=ls;
        var s1=1000+ls*10-Math.min(t1.length,900);
        if(s1>bestScore){ bestScore=s1; best=nodes[i]; }
      }
      /* the honest branch: several cells carry the number and none of them
         carries the question's context — flashing the first would be a lie */
      if(best&&labels.length&&hits>1&&bestLabel===0) return null;
      if(best) return best;
    }
    for(i=0;i<nodes.length;i++){
      var t2=texts[i]; if(!t2) continue;
      var f2=locFold(t2);
      for(j=0;j<labels.length;j++){
        var lf=locFold(labels[j]);
        if(lf.length<2||f2.indexOf(lf)<0) continue;
        var s2=lf.length*1000+(1000-Math.min(t2.length,900));
        if(s2>bestScore){ bestScore=s2; best=nodes[i]; }
      }
    }
    return best;
  }
  function locFlash(el){
    locStyle();
    try{ if(locEl&&locEl!==el&&locEl.classList) locEl.classList.remove('op-located'); }catch(err){}
    clearTimeout(locTimer); locEl=el;
    try{ if(el.classList) el.classList.add('op-located'); }catch(err){}
    try{ if(el.scrollIntoView) el.scrollIntoView({block:'center',inline:'nearest',behavior:'smooth'}); }catch(err){}
    locTimer=setTimeout(function(){
      try{ if(el.classList) el.classList.remove('op-located'); }catch(err){}
      if(locEl===el) locEl=null;
    },2000);
  }
  function onLocate(m){
    var id=(typeof m.id==='string'||typeof m.id==='number')?m.id:'';
    var labels=[], src=[locStr(m.kpi)], i;
    if(Object.prototype.toString.call(m.labels)==='[object Array]')
      for(i=0;i<m.labels.length&&i<20;i++) src.push(locStr(m.labels[i]));
    for(i=0;i<src.length;i++) if(src[i]) labels.push(src[i]);
    var el=null;
    try{ el=locFind(locStr(m.value),labels); }catch(err){ el=null; }
    if(el) locFlash(el);
    try{
      window.parent.postMessage({type:'op_located', id:id, ok:!!el,
        tag: el?String(el.tagName||'').toLowerCase():'',
        text: el?locNorm(el.textContent||'').slice(0,60):''}, '*');
    }catch(err){}
  }
  document.addEventListener('mousemove', onMove, true);
  document.addEventListener('click', onClick, true);
  document.addEventListener('mousedown', suppress, true);
  document.addEventListener('mouseup', suppress, true);
  window.addEventListener('scroll', repositionAll, true);
  window.addEventListener('resize', repositionAll);
  window.addEventListener('message', function(e){
    var m=e.data; if(!m||!m.type) return;
    if(m.type==='op_annotate_on') setActive(true);
    else if(m.type==='op_annotate_off') setActive(false);
    else if(m.type==='op_annotate_clear') clearPins();
    else if(m.type==='op_annotate_remove') removePin(m.id);
    else if(m.type==='op_locate') onLocate(m);
  });
})();
</script>"""


def inject_annotate(html: str) -> str:
    """Inject the dormant annotate controller right after <head> (all dashboard types)."""
    injected = [False]

    def _replacer(m):
        injected[0] = True
        return m.group(1) + '\n' + _ANNOTATE_CONTROLLER

    result = re.sub(r'(<head\b[^>]*>)', _replacer, html, count=1, flags=re.IGNORECASE)
    if not injected[0]:
        result = _ANNOTATE_CONTROLLER + html
    return result


_THEME_CONTROLLER = r"""<script>
/* OnePage theme controller — injected. Applies saved colours for ALL viewers,
   and serves the owner/admin "Colors" editor over postMessage.
   Cannot reach <canvas> charts or cross-origin (CDN) stylesheets. */
(function(){
  var SEED = window.__OP_THEME__ || null;
  var _cv = document.createElement('canvas').getContext('2d');
  var COLOR_RE = /#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)|hsla?\([^)]*\)/g;

  function norm(c){
    if(!c) return null;
    c = String(c).trim();
    if(!c || c==='transparent' || c==='inherit' || c==='currentcolor' || c==='none') return null;
    try {
      _cv.fillStyle = '#000'; _cv.fillStyle = c; var a = _cv.fillStyle;
      _cv.fillStyle = '#fff'; _cv.fillStyle = c; var b = _cv.fillStyle;
      if(a !== b) return null;                       // invalid: canvas kept fallback
      if(a === 'rgba(0, 0, 0, 0)') return null;      // fully transparent: nothing to recolour
      return a;
    } catch(e){ return null; }
  }

  function walk(rules, cb){
    for(var i=0;i<rules.length;i++){ var r=rules[i];
      if(r.type===1 && r.style) cb(r.style);
      else if(r.cssRules) walk(r.cssRules, cb);
    }
  }
  function forEachDecl(cb){
    for(var i=0;i<document.styleSheets.length;i++){
      var rules; try{ rules=document.styleSheets[i].cssRules; }catch(e){ continue; }
      if(rules) walk(rules, cb);
    }
  }

  // ── Chart adapters (Chart.js / Plotly): <canvas> charts aren't reachable via
  //    CSS, so we recolour their config through the library API. Best-effort —
  //    plain colour strings/arrays only; gradients, colour callbacks, colour
  //    scales and baked images are left untouched (use the op:theme event for
  //    full control — see the Dev Guide). ──
  function isColorKey(k){ return /colou?r$/i.test(k) || k==='fill' || k==='stroke' || k==='bgcolor'; }
  function chartjsInstances(){
    var r=[], C=window.Chart; if(!C) return r;
    try{ if(C.instances){ for(var k in C.instances){ if(C.instances[k]) r.push(C.instances[k]); } } }catch(e){}
    if(!r.length && C.getChart){ try{ document.querySelectorAll('canvas').forEach(function(cv){ var c=C.getChart(cv); if(c) r.push(c); }); }catch(e){} }
    return r;
  }
  function plotlyGraphs(){
    var r=[]; if(!window.Plotly) return r;
    try{ document.querySelectorAll('.js-plotly-plot').forEach(function(gd){ if(gd && gd.data) r.push(gd); }); }catch(e){}
    return r;
  }
  function chartConfigs(){
    var out=[];
    chartjsInstances().forEach(function(ch){
      var cfg=ch.config||{};
      out.push({roots:[cfg.data||ch.data, cfg.options||ch.options],
        update:function(){ try{ ch.update('none'); }catch(e){ try{ ch.update(); }catch(_e){} } }});
    });
    plotlyGraphs().forEach(function(gd){
      out.push({roots:[gd.data, gd.layout],
        update:function(){ try{ window.Plotly.react(gd, gd.data, gd.layout); }catch(e){} }});
    });
    return out;
  }
  function eachColorSlot(obj, cb, depth, seen){
    depth=depth||0; seen=seen||new WeakSet();
    if(!obj || typeof obj!=='object' || depth>6 || seen.has(obj)) return;
    seen.add(obj);
    if(Array.isArray(obj)){ for(var i=0;i<obj.length;i++){ var av=obj[i]; if(av && typeof av==='object') eachColorSlot(av, cb, depth+1, seen); } return; }
    for(var key in obj){
      if(!Object.prototype.hasOwnProperty.call(obj,key)) continue;
      var v; try{ v=obj[key]; }catch(e){ continue; }
      if(v==null) continue;
      if(typeof v==='string'){ if(isColorKey(key) && norm(v)) cb(obj, key, v); }
      else if(Array.isArray(v)){
        if(isColorKey(key)){ for(var j=0;j<v.length;j++){ if(typeof v[j]==='string' && norm(v[j])) cb(v, j, v[j]); } }
        else eachColorSlot(v, cb, depth+1, seen);
      } else if(typeof v==='object'){ eachColorSlot(v, cb, depth+1, seen); }
    }
  }
  function chartColorsInto(add){
    chartConfigs().forEach(function(c){ c.roots.forEach(function(root){ eachColorSlot(root, function(o,k,val){ add(val); }); }); });
  }

  // ── Collect changeable colours + colour custom-properties ──
  function collect(){
    var colors={}, vars={};
    function add(tok){ var n=norm(tok); if(n) colors[n]=(colors[n]||0)+1; }
    function scan(v){ if(!v) return; var m; COLOR_RE.lastIndex=0; while((m=COLOR_RE.exec(v))) add(m[0]); }
    forEachDecl(function(st){
      for(var j=0;j<st.length;j++){ var prop=st[j], val=st.getPropertyValue(prop);
        if(prop.slice(0,2)==='--'){ var nv=norm((val||'').trim()); if(nv) vars[prop]=nv; }
        scan(val);
      }
    });
    var all=document.querySelectorAll('*');
    for(var k=0;k<all.length;k++){ var el=all[k];
      if(el.style && el.style.length){ for(var p=0;p<el.style.length;p++) scan(el.style.getPropertyValue(el.style[p])); }
      var f=el.getAttribute&&el.getAttribute('fill'); if(f) add(f);
      var s=el.getAttribute&&el.getAttribute('stroke'); if(s) add(s);
    }
    chartColorsInto(add);   // Chart.js / Plotly colours so the user can remap them too
    var clist=Object.keys(colors).map(function(c){return {value:c,count:colors[c]};}).sort(function(a,b){return b.count-a.count;});
    var vlist=Object.keys(vars).map(function(n){return {name:n,value:vars[n]};});
    return {colors:clist, vars:vlist};
  }

  // ── We touch only the declarations we actually recolour, and restore only
  //    those before the next apply. Round-tripping *every* declaration through
  //    the CSSOM is lossy for values that use var() inside shorthands (e.g.
  //    "border: 1px solid var(--line)") — it blanks them — so declarations
  //    without a literal colour are never rewritten. ──
  var touched=[], appliedVars=[];
  function restoreTouched(){
    touched.forEach(function(t){
      try{
        if(t.type==='decl') t.st.setProperty(t.prop, t.orig, t.pri);
        else if(t.type==='inline') t.el.style.setProperty(t.prop, t.orig, t.pri);
        else if(t.type==='attr') t.el.setAttribute(t.name, t.orig);
      }catch(e){}
    });
    touched=[];
  }

  // ── Chart baseline (original colour slots) for idempotent re-previews ──
  var CHARTBASE=null;
  function buildChartBaseline(){
    CHARTBASE={slots:[], updaters:[]};
    chartConfigs().forEach(function(c){
      c.roots.forEach(function(root){ eachColorSlot(root, function(o,k,val){ CHARTBASE.slots.push({c:o,k:k,orig:val}); }); });
      CHARTBASE.updaters.push(c.update);
    });
  }
  function applyChartTheme(remap){
    if(!CHARTBASE || !CHARTBASE.slots.length) buildChartBaseline();  // (re)build while empty to catch late charts
    if(!CHARTBASE.slots.length) return;
    var changed=false;
    CHARTBASE.slots.forEach(function(s){
      var n=norm(s.orig), nv=(n && remap[n]) ? remap[n] : s.orig;
      try{ if(s.c[s.k]!==nv){ s.c[s.k]=nv; changed=true; } }catch(e){}
    });
    if(changed) CHARTBASE.updaters.forEach(function(u){ u(); });
  }
  function dispatchThemeEvent(theme){
    try{ window.dispatchEvent(new CustomEvent('op:theme', {detail:{vars:theme.vars||{}, remap:theme.remap||{}}})); }catch(e){}
  }

  function applyTheme(theme){
    theme = theme || {};
    restoreTouched();                                        // undo only our previous edits
    appliedVars.forEach(function(n){ document.documentElement.style.removeProperty(n); }); appliedVars=[];
    var vars=theme.vars||{};
    for(var name in vars){ if(/^--[\w-]+$/.test(name)){ document.documentElement.style.setProperty(name, vars[name]); appliedVars.push(name); } }
    var remap=theme.remap||{};
    if(Object.keys(remap).length){
      var repl=function(v){
        if(!v || (v.indexOf('#')<0 && v.indexOf('rgb')<0 && v.indexOf('hsl')<0)) return v;
        return v.replace(COLOR_RE, function(tok){ var n=norm(tok); return (n && remap[n]) ? remap[n] : tok; });
      };
      forEachDecl(function(st){
        for(var j=0;j<st.length;j++){
          var prop=st[j], val=st.getPropertyValue(prop), nv=repl(val);
          if(nv!==val){ var pri=st.getPropertyPriority(prop); touched.push({type:'decl',st:st,prop:prop,orig:val,pri:pri}); try{ st.setProperty(prop, nv, pri); }catch(e){} }
        }
      });
      var all=document.querySelectorAll('*');
      for(var k=0;k<all.length;k++){ var el=all[k];
        if(el.style && el.style.length){ for(var p=0;p<el.style.length;p++){
          var iprop=el.style[p], ival=el.style.getPropertyValue(iprop), inv=repl(ival);
          if(inv!==ival){ var ipri=el.style.getPropertyPriority(iprop); touched.push({type:'inline',el:el,prop:iprop,orig:ival,pri:ipri}); try{ el.style.setProperty(iprop, inv, ipri); }catch(e){} }
        } }
        var f=el.getAttribute&&el.getAttribute('fill'); if(f){ var nf=repl(f); if(nf!==f){ touched.push({type:'attr',el:el,name:'fill',orig:f}); try{ el.setAttribute('fill', nf); }catch(e){} } }
        var s=el.getAttribute&&el.getAttribute('stroke'); if(s){ var ns=repl(s); if(ns!==s){ touched.push({type:'attr',el:el,name:'stroke',orig:s}); try{ el.setAttribute('stroke', ns); }catch(e){} } }
      }
    }
    applyChartTheme(remap);          // recolour Chart.js / Plotly via their API
    dispatchThemeEvent(theme);       // let cooperative chart code re-skin itself
  }

  // ── Colour inspector: hover highlight + click reports an element's colours ──
  var PICK=false, phl=null;
  function pHl(){ if(phl) return phl; phl=document.createElement('div'); phl.style.cssText='position:fixed;z-index:2147483646;pointer-events:none;border:2px solid #4c8bf5;background:rgba(76,139,245,.10);border-radius:3px;display:none'; (document.body||document.documentElement).appendChild(phl); return phl; }
  function pMove(e){ if(!PICK) return; var el=e.target; if(!el||el.nodeType!==1) return; var r=el.getBoundingClientRect(), h=pHl(); h.style.display='block'; h.style.left=r.left+'px'; h.style.top=r.top+'px'; h.style.width=r.width+'px'; h.style.height=r.height+'px'; }
  function pColors(el){
    var cs=getComputedStyle(el), out=[];
    function push(v){ var n=norm(v); if(n && out.indexOf(n)<0) out.push(n); }
    push(cs.backgroundColor); push(cs.color);
    push(cs.getPropertyValue('border-top-color')); push(cs.getPropertyValue('border-right-color'));
    push(cs.getPropertyValue('border-bottom-color')); push(cs.getPropertyValue('border-left-color'));
    push(cs.getPropertyValue('outline-color')); push(cs.getPropertyValue('text-decoration-color'));
    if(window.SVGElement && el instanceof SVGElement){ push(cs.getPropertyValue('fill')); push(cs.getPropertyValue('stroke')); }
    return out;
  }
  function pClick(e){
    if(!PICK) return;
    e.preventDefault(); e.stopPropagation(); if(e.stopImmediatePropagation) e.stopImmediatePropagation();
    var el=e.target; if(!el||el.nodeType!==1) return;
    window.parent.postMessage({type:'op_theme_picked', colors:pColors(el), tag:el.tagName.toLowerCase()}, '*');
  }
  function pSuppress(e){ if(PICK){ e.preventDefault(); e.stopPropagation(); if(e.stopImmediatePropagation) e.stopImmediatePropagation(); } }
  function setPick(on){ PICK=on; if(phl) phl.style.display='none'; try{ (document.body||document.documentElement).style.cursor=on?'crosshair':''; }catch(e){} }
  document.addEventListener('mousemove', pMove, true);
  document.addEventListener('click', pClick, true);
  document.addEventListener('mousedown', pSuppress, true);
  document.addEventListener('mouseup', pSuppress, true);

  window.addEventListener('message', function(e){
    var m=e.data; if(!m||!m.type) return;
    if(m.type==='op_theme_scan'){
      var data=collect();
      window.parent.postMessage({type:'op_theme_palette', colors:data.colors, vars:data.vars}, '*');
    } else if(m.type==='op_theme_apply'){
      applyTheme(m.theme||{});
    } else if(m.type==='op_theme_pick_on'){ setPick(true); }
    else if(m.type==='op_theme_pick_off'){ setPick(false); }
  });

  function applySeed(){
    if(SEED && ((SEED.vars && Object.keys(SEED.vars).length) || (SEED.remap && Object.keys(SEED.remap).length))) applyTheme(SEED);
  }
  // Apply early (paint ASAP) and again after the dashboard's own load handlers
  // and any late/async rendering — applyTheme is idempotent, so re-runs are safe.
  function scheduleSeed(){ applySeed(); setTimeout(applySeed, 250); setTimeout(applySeed, 900); }
  if(document.readyState!=='loading'){ scheduleSeed(); }
  else { document.addEventListener('DOMContentLoaded', applySeed); window.addEventListener('load', scheduleSeed); }
})();
</script>"""


def inject_theme(html: str, theme_json: str) -> str:
    """Inject the theme controller after <head>, seeded with the saved theme.

    `theme_json` is a JSON string (object) or 'null'/'{}' when no theme is saved.
    The controller applies a non-empty seed for every viewer and answers the
    owner/admin Colors editor over postMessage.
    """
    seed = f'<script>window.__OP_THEME__ = {theme_json or "null"};</script>'
    block = seed + _THEME_CONTROLLER
    injected = [False]

    def _replacer(m):
        injected[0] = True
        return m.group(1) + '\n' + block

    result = re.sub(r'(<head\b[^>]*>)', _replacer, html, count=1, flags=re.IGNORECASE)
    if not injected[0]:
        result = block + html
    return result


_TEXTEDIT_CONTROLLER = r"""<script>
/* OnePage text-edit controller — injected. Applies saved static-text overrides
   for every viewer and serves the owner/admin "Text" editor over postMessage.
   Only leaf text elements are editable; overrides are applied via textContent,
   so nothing user-entered is ever parsed as HTML. */
(function(){
  var SEED = window.__OP_TEXT__ || null;
  var EDIT=false, hl=null, editingEl=null, editingSel=null;
  var ORIG={}, applied=[];

  function q(sel){ try{ return document.querySelector(sel); }catch(e){ return null; } }
  function isLeafText(el){
    return el && el.nodeType===1 && el.children.length===0 &&
      (el.textContent||'').trim().length>0 &&
      !/^(script|style|svg|canvas|img|input|textarea|select|option|br|hr)$/i.test(el.tagName);
  }
  function cssEsc(s){ return String(s).replace(/[^a-zA-Z0-9_-]/g, function(m){ return '\\' + m; }); }
  function selectorFor(el){
    if(!el||el.nodeType!==1) return '';
    var parts=[], cur=el, depth=0;
    while(cur && cur.nodeType===1 && cur.tagName.toLowerCase()!=='html' && depth<8){
      var tag=cur.tagName.toLowerCase();
      if(cur.id){ parts.unshift(tag+'#'+cssEsc(cur.id)); break; }
      var seg=tag, parent=cur.parentElement;
      if(parent){
        var same=Array.prototype.filter.call(parent.children, function(c){ return c.tagName===cur.tagName; });
        if(same.length>1) seg+=':nth-of-type('+(same.indexOf(cur)+1)+')';
      }
      parts.unshift(seg);
      cur=cur.parentElement; depth++;
    }
    return parts.join(' > ');
  }

  function ensureHl(){
    if(hl) return hl;
    hl=document.createElement('div');
    hl.style.cssText='position:fixed;z-index:2147483646;pointer-events:none;border:2px solid #16a34a;background:rgba(22,163,74,.10);border-radius:3px;display:none';
    (document.body||document.documentElement).appendChild(hl);
    return hl;
  }
  function onMove(e){
    if(!EDIT || editingEl) return;
    var el=e.target, h=ensureHl();
    if(!isLeafText(el)){ h.style.display='none'; return; }
    var r=el.getBoundingClientRect();
    h.style.display='block'; h.style.left=r.left+'px'; h.style.top=r.top+'px'; h.style.width=r.width+'px'; h.style.height=r.height+'px';
  }

  function finalize(){
    if(!editingEl) return;
    var el=editingEl, sel=editingSel;
    editingEl=null; editingSel=null;
    el.removeAttribute('contenteditable');
    el.style.outline='';
    var text=(el.textContent||'');
    if(text!==ORIG[sel]){
      if(applied.indexOf(sel)<0) applied.push(sel);
      window.parent.postMessage({type:'op_text_edited', selector:sel, text:text, original:ORIG[sel]}, '*');
    }
  }
  function startEdit(el){
    var sel=selectorFor(el); if(!sel) return;
    if(!(sel in ORIG)) ORIG[sel]=el.textContent;
    editingEl=el; editingSel=sel;
    if(hl) hl.style.display='none';
    el.setAttribute('contenteditable','true');
    el.style.outline='2px solid #16a34a';
    el.focus();
    try{ var rng=document.createRange(); rng.selectNodeContents(el); rng.collapse(false); var s=getSelection(); s.removeAllRanges(); s.addRange(rng); }catch(e){}
  }
  function inEditing(el){ return editingEl && (el===editingEl || editingEl.contains(el)); }
  function onClick(e){
    if(!EDIT) return;
    var el=e.target;
    if(inEditing(el)) return;                 // editing current — let caret work
    e.preventDefault(); e.stopPropagation(); if(e.stopImmediatePropagation) e.stopImmediatePropagation();
    if(editingEl) finalize();
    if(isLeafText(el)) startEdit(el);
  }
  function onDown(e){
    if(!EDIT || inEditing(e.target)) return;
    e.preventDefault(); e.stopPropagation(); if(e.stopImmediatePropagation) e.stopImmediatePropagation();
  }
  function onKey(e){
    if(!editingEl) return;
    if(e.key==='Enter' && !e.shiftKey){ e.preventDefault(); finalize(); }
    else if(e.key==='Escape'){ e.preventDefault(); editingEl.textContent=ORIG[editingSel]; finalize(); }
  }
  document.addEventListener('mousemove', onMove, true);
  document.addEventListener('click', onClick, true);
  document.addEventListener('mousedown', onDown, true);
  document.addEventListener('keydown', onKey, true);
  document.addEventListener('focusout', function(e){ if(editingEl && e.target===editingEl) finalize(); }, true);

  function setEdit(on){
    EDIT=on;
    if(!on && editingEl) finalize();
    if(hl) hl.style.display='none';
    try{ (document.body||document.documentElement).style.cursor = on ? 'text' : ''; }catch(e){}
  }

  function applyTextMap(map){
    map=map||{};
    applied.forEach(function(sel){ if(!(sel in map) && (sel in ORIG)){ var el=q(sel); if(el && el.children.length===0) el.textContent=ORIG[sel]; } });
    applied=[];
    for(var sel in map){
      var el=q(sel); if(!el || el.children.length) continue;
      if(!(sel in ORIG)) ORIG[sel]=el.textContent;
      el.textContent=map[sel]; applied.push(sel);
    }
  }

  window.addEventListener('message', function(e){
    var m=e.data; if(!m||!m.type) return;
    if(m.type==='op_text_edit_on') setEdit(true);
    else if(m.type==='op_text_edit_off') setEdit(false);
    else if(m.type==='op_text_apply') applyTextMap(m.edits||{});
  });

  function applySeed(){ if(SEED && Object.keys(SEED).length) applyTextMap(SEED); }
  function scheduleSeed(){ applySeed(); setTimeout(applySeed,250); setTimeout(applySeed,900); }
  if(document.readyState!=='loading'){ scheduleSeed(); }
  else { document.addEventListener('DOMContentLoaded', applySeed); window.addEventListener('load', scheduleSeed); }
})();
</script>"""


def inject_textedit(html: str, text_map_json: str) -> str:
    """Inject the text-edit controller after <head>, seeded with saved overrides.

    `text_map_json` is a JSON object string `{selector: text}` or 'null'/'{}'.
    The `<` escape neutralises any `</script>` that could appear in user text.
    """
    safe = (text_map_json or "null").replace("<", "\\u003c")
    seed = f'<script>window.__OP_TEXT__ = {safe};</script>'
    block = seed + _TEXTEDIT_CONTROLLER
    injected = [False]

    def _replacer(m):
        injected[0] = True
        return m.group(1) + '\n' + block

    result = re.sub(r'(<head\b[^>]*>)', _replacer, html, count=1, flags=re.IGNORECASE)
    if not injected[0]:
        result = block + html
    return result


def inject_snapshot(html: str, new_json: str) -> str:
    """Replace content of <script id="bootstrap-snapshot"> with new_json.

    Same `<` escape as inject_textedit/snapshot_js_body: the snapshot is only
    validated as parseable JSON (and on the create-time promotion path, not even
    that), so `</script>` inside a string value would otherwise end the tag and
    inject markup — for share-link visitors too, since share_render reuses this.
    """
    if "<" in new_json:
        new_json = new_json.replace("<", "\\u003c")
    return re.sub(
        r'(<script[^>]*id=["\']bootstrap-snapshot["\'][^>]*>)(.*?)(</script>)',
        lambda m: m.group(1) + new_json + m.group(3),
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )


def externalize_snapshot(html: str, src_url: str) -> tuple[str, bool]:
    """Content-addressed alternative to inject_snapshot (OP_SNAPSHOT_EXTERNAL):
    empty the bootstrap-snapshot tag and insert a *classic synchronous* external
    script right after it, which assigns the JSON into the tag's textContent.
    Classic scripts execute in document order and block subsequent parsing, so
    any later dashboard code reading the tag sees the populated value — the
    semantics are identical to inlining, but the (huge) data ships on a
    versioned URL the browser can cache immutably.

    Returns (new_html, True) on success, (html, False) if no tag was found."""
    out, n = re.subn(
        r'(<script[^>]*id=["\']bootstrap-snapshot["\'][^>]*>)(.*?)(</script>)',
        lambda m: m.group(1) + m.group(3) + '<script src="' + src_url + '"></script>',
        html,
        count=1,
        flags=re.DOTALL | re.IGNORECASE,
    )
    return (out, True) if n else (html, False)


_RE_SNAPSHOT_BODY = re.compile(
    r'<script[^>]*id=["\']bootstrap-snapshot["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


def snapshot_inline_is_empty(html: str) -> bool:
    """True if the page has no <script id="bootstrap-snapshot"> or it carries no
    meaningful data (empty / whitespace / a bare {} or null placeholder).

    Used at upload to decide whether an accompanying data file should be promoted
    to snapshot.json (and inlined at render) — which is only correct when the page
    ships an *empty* placeholder. A page that already bakes its snapshot inline
    keeps it, and the data file stays a plain slot delivered via postMessage.
    """
    m = _RE_SNAPSHOT_BODY.search(html)
    if not m:
        return True
    body = m.group(1).strip()
    return body in ("", "{}", "null")
