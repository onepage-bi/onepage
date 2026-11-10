/* OnePage — keyboard hotkeys
 *  G then D    → /
 *  G then U    → /upload
 *  G then Q    → /data-quality
 *  G then C    → /catalog
 *  G then E    → /explore (only when window.OP_EXPLORE_ON)
 *  G then K    → /quests (only when window.OP_QUESTS_ON)
 *  G then A    → /admin
 *  G then L    → /admin/audit
 *  G then ?    → /docs
 *  [           → toggle sidebar
 *  /           → focus search input on current page
 */

(function() {
  let gMode = false;
  let gTimer = null;

  function isTyping(e) {
    const tv = e.target;
    return tv.matches('input, textarea, select, [contenteditable=""], [contenteditable="true"]');
  }

  // Physical key, not the typed character: on a Russian layout `e.key`
  // for the G key is a Cyrillic letter, and the g-sequences would never fire.
  function letter(e) {
    if (typeof e.code === 'string' && /^Key[A-Z]$/.test(e.code)) return e.code[3].toLowerCase();
    return (e.key || '').toLowerCase();
  }

  window.addEventListener('keydown', e => {
    // An IME (Chinese input, and Japanese/Korean too) fires keydown while the
    // user is still composing a character; those keys belong to the composition,
    // not to us, or typing 私 in a search box would trigger a g-sequence.
    if (e.isComposing || e.keyCode === 229) return;
    if (isTyping(e)) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;

    if (letter(e) === 'g') {
      gMode = true;
      clearTimeout(gTimer);
      gTimer = setTimeout(() => { gMode = false; }, 1500);
      return;
    }

    if (gMode) {
      gMode = false;
      const k = letter(e);
      if (k === 'd') location.href = '/';
      else if (k === 'u') location.href = '/upload';
      else if (k === 'q') location.href = '/data-quality';
      else if (k === 'c') location.href = '/catalog';
      else if (k === 'e' && window.OP_EXPLORE_ON) location.href = '/explore';
      else if (k === 'k' && window.OP_QUESTS_ON) location.href = '/quests';
      else if (k === 'n') location.href = '/navigators';
      else if (k === 's') location.href = '/learn';
      else if (k === 'a') location.href = '/admin';
      else if (k === 'l') location.href = '/admin/audit';
      else if (k === '?' || k === '/') location.href = '/docs';
      return;
    }

    if (e.key === '[') {
      window.OP_UI && window.OP_UI.toggle('sidebar', 'expanded', 'collapsed');
    }

    if (e.key === '/') {
      const search = document.querySelector('input[type="text"][placeholder*="earch"], #dash-search, #users-search, #audit-search');
      if (search) { e.preventDefault(); search.focus(); }
    }
  });
})();
