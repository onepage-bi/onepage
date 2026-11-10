/* A file or root path sent by the desktop application, and the "Copy"
 * button next to it. One module for three places (the sources page, the source
 * drawer and the provenance page), because copying a path breaks the same way
 * everywhere: navigator.clipboard exists only in a secure context, and the
 * platform also lives on plain http inside the network. Hence a fallback via
 * selection, and if that fails too, an honest "select it manually" caption: the
 * path string itself is marked user-select: all, so it can always be selected
 * with one click.
 *
 * The handler is delegated, one per document: server pages put buttons with
 * data-copy-path straight into the markup, while the drawer builds them from JS —
 * either way the click is handled the same, without binding to every button.
 */
(function () {
  'use strict';

  function legacyCopy(text) {
    // execCommand('copy') is deprecated, but it is the only way in an
    // insecure context; the textarea is off-screen so the layout does not jump.
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.cssText = 'position:fixed;left:-9999px;top:0;opacity:0';
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    return ok;
  }

  function copy(text, btn) {
    var idle = btn.getAttribute('data-label') || btn.textContent;
    btn.setAttribute('data-label', idle);
    function say(msg) {
      btn.textContent = msg;
      setTimeout(function () { btn.textContent = idle; }, 1600);
    }
    function fallback() {
      say(legacyCopy(text) ? t('Copied') : t('That did not work — select the path and copy it by hand'));
    }
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () { say(t('Copied')); }, fallback);
        return;
      }
    } catch (e) { /* below */ }
    fallback();
  }

  document.addEventListener('click', function (e) {
    var btn = e.target && e.target.closest ? e.target.closest('[data-copy-path]') : null;
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();   // the button lives inside cards and rows that have their own click
    copy(btn.getAttribute('data-copy-path') || '', btn);
  });

  /* A "path + Copy" row for code that builds the DOM itself (the drawer). */
  function pathRow(text) {
    var row = document.createElement('div');
    row.className = 'ds-pathrow';
    var code = document.createElement('span');
    code.className = 'mono ds-pathtext';
    code.textContent = text;
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn xs ghost ds-copy';
    btn.setAttribute('data-copy-path', text);
    btn.textContent = t('Copy path');
    row.appendChild(code);
    row.appendChild(btn);
    return row;
  }

  window.OPSourcePaths = { copy: copy, pathRow: pathRow };
})();
