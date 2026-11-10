/* OnePage navigator links — the shell side of the link bridge.
 *
 * A navigator (dash_type="page") lives in a sandboxed iframe. A tab opened
 * from INSIDE that iframe inherits the sandbox and its opaque origin, so the
 * target never receives cookies — Power BI Report Server with Windows auth
 * shows a white screen (owner's observation, 29.10.2026). The injected
 * bridge (detector._LINK_BRIDGE) therefore only names the address; the
 * SHELL decides and opens. Two shells exist — the owner viewer (viewer.js)
 * and the public share page (share.html) — and they must apply the same
 * rules, so the rules live here, once.
 *
 * Nothing in here trusts the page: the iframe runs the author's code, and
 * every address is checked by `pageLinkAction` before anything happens.
 */
(function (root) {
  'use strict';

  // Pure: address + shell origin -> decision. Kept free of DOM so the tests
  // can run it under node. `opts.standalone` is the share page: there is no
  // platform session to walk through, so "own address, same tab" would only
  // land an anonymous reader on /login — every link goes to a new tab.
  function pageLinkAction(href, target, base, opts) {
    var standalone = !!(opts && opts.standalone);
    var raw = String(href == null ? '' : href).trim();
    if (!raw) return { kind: 'none' };
    // UNC first, BEFORE parsing: the URL parser treats backslashes as
    // ordinary separators and «\\server\share» would become
    // «https://server/share» — a meaningless tab instead of a path.
    if (raw.slice(0, 2) === '\\\\') return { kind: 'copy', url: raw };
    var u;
    try { u = new URL(raw, base); } catch (e) { return { kind: 'bad', url: raw }; }
    var scheme = (u.protocol || '').toLowerCase();
    // The single hard refusal, and it is not an address policy: this is code
    // execution in the platform origin, i.e. a full sandbox escape.
    if (scheme === 'javascript:' || scheme === 'data:' || scheme === 'vbscript:') {
      return { kind: 'refuse', url: u.href };
    }
    // A network folder the browser will NEVER open — a security rule, not
    // our gap. The honest answer is the path itself, as for source paths.
    if (scheme === 'file:') return { kind: 'copy', url: raw };
    var sameOrigin = false;
    try { sameOrigin = u.origin === new URL(base).origin; } catch (e) { sameOrigin = false; }
    // Own address — same tab: the person walks the platform, not outwards,
    // and there is no reason to multiply tabs.
    if (sameOrigin && target !== '_blank' && !standalone) return { kind: 'same', url: u.href };
    return { kind: 'new', url: u.href };
  }

  // DOM half: carry the decision out. `note` is the box under the top bar
  // (#page-open-note); without one the fallbacks are alert/prompt, because
  // the person clicked and must learn what happened.
  function makeOpener(cfg) {
    cfg = cfg || {};
    // Local `t`: the i18n extractor only sees calls spelled t(<literal>), so the key must be written that way.
    var t = cfg.t || function (s) { return (typeof root.t === 'function') ? root.t(s) : s; };
    var noteEl = function () { return cfg.note ? cfg.note() : document.getElementById('page-open-note'); };

    function note(text) {
      var box = noteEl();
      if (!box) { alert(text); return; }
      box.textContent = text;
      box.hidden = false;
      clearTimeout(note._t);
      note._t = setTimeout(function () { box.hidden = true; }, 8000);
    }

    function copyPath(path) {
      var box = noteEl();
      if (!box) { prompt(t('Copy the path:'), path); return; }
      box.textContent = '';
      box.hidden = false;
      clearTimeout(note._t);
      var label = document.createElement('span');
      label.textContent = t('Browsers never open a network folder. The path:') + ' ';
      var code = document.createElement('code');
      code.textContent = path;
      var btn = document.createElement('button');
      btn.className = 'btn xs';
      btn.textContent = t('Copy');
      btn.addEventListener('click', function () {
        try {
          navigator.clipboard.writeText(path);
          btn.textContent = t('Copied');
        } catch (err) {
          // Old path without the clipboard API — same fallback as source-paths.js
          var ta = document.createElement('textarea');
          ta.value = path; document.body.appendChild(ta); ta.select();
          try { document.execCommand('copy'); btn.textContent = t('Copied'); } catch (e2) {}
          document.body.removeChild(ta);
        }
      });
      box.appendChild(label); box.appendChild(code); box.appendChild(btn);
    }

    function open(raw, target) {
      var act = pageLinkAction(raw, target, location.href, { standalone: !!cfg.standalone });
      if (act.kind === 'none') return;
      if (act.kind === 'bad') {
        note(t('Address not understood: {p1}').replace('{p1}', String(raw)));
        return;
      }
      if (act.kind === 'refuse') {
        note(t('The platform does not open links like this.'));
        return;
      }
      if (act.kind === 'copy') { copyPath(act.url); return; }
      if (act.kind === 'same') { location.assign(act.url); return; }
      // No 'noopener' in features: with it window.open returns null ALWAYS, by
      // spec, and the "browser blocked the tab" note stayed up after every tab
      // that did open. The opener is cleared by hand — same effect.
      var w = window.open(act.url, '_blank');
      if (w) { try { w.opener = null; } catch (e) { /* cross-origin — already detached */ } }
      else note(t('The browser blocked the tab — allow pop-ups for this page.'));
    }

    return { open: open, note: note, copyPath: copyPath };
  }

  var api = { pageLinkAction: pageLinkAction, makeOpener: makeOpener };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (root) root.OP_PAGE_LINKS = api;
})(typeof window !== 'undefined' ? window : globalThis);
