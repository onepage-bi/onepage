/* OnePage — command palette (⌘K / Ctrl+K) */

(function() {
  const overlay = document.getElementById('palette-overlay');
  if (!overlay) return;

  const input = document.getElementById('palette-input');
  const list  = document.getElementById('palette-list');
  let activeIdx = 0;
  let filtered = [];

  // Static commands — combined with server-supplied palette items
  const STATIC_COMMANDS = [
    { group: 'Navigate', label: 'Dashboards',        sub: 'All dashboards',   icon: 'grid',     action: 'href:/',            kbd: 'G D' },
    { group: 'Navigate', label: 'Upload',            sub: 'New dashboard',    icon: 'upload',   action: 'href:/upload',      kbd: 'G U' },
    { group: 'Navigate', label: 'Data Quality',      sub: 'Quality findings',  icon: 'alert',    action: 'href:/data-quality', kbd: 'G Q' },
    { group: 'Navigate', label: t('Catalogue'),           sub: 'Inferred tables',  icon: 'folder',   action: 'href:/catalog',     kbd: 'G C' },
    { group: 'Navigate', label: t('Explore'),       sub: 'SQL over your folder', icon: 'search', action: 'href:/explore',   kbd: 'G E' },
    { group: 'Navigate', label: t('Navigators'),        sub: t('Pages linking to reports and other systems'), icon: 'compass', action: 'href:/navigators', kbd: 'G N' },
    { group: 'Navigate', label: t('Quests'),            sub: t('Daily surprise, quiz, rating'), icon: 'gift', action: 'href:/quests', kbd: 'G K' },
    { group: 'Navigate', label: 'Admin · Users',     sub: 'User management',  icon: 'users',    action: 'href:/admin',       kbd: 'G A' },
    { group: 'Navigate', label: 'Admin · Audit log', sub: 'Activity timeline',icon: 'history',  action: 'href:/admin/audit', kbd: 'G L' },
    { group: 'Navigate', label: t('Practicum'),             sub: t('A course on the platform: lessons and checks'), icon: 'cap', action: 'href:/learn', kbd: 'G S' },
    { group: 'Navigate', label: 'Dev Guide',         sub: 'Authoring docs',   icon: 'book',     action: 'href:/docs',        kbd: 'G ?' },

    { group: 'Actions',  label: 'Toggle theme',      sub: 'Dark ↔ light',     icon: 'sun',      action: 'toggle:theme',      kbd: '' },
    { group: 'Actions',  label: 'Toggle sidebar',    sub: 'Collapse / expand',icon: 'panel',    action: 'toggle:sidebar',    kbd: '[' },
    { group: 'Actions',  label: 'Sign out',          sub: 'End session',      icon: 'logout',   action: 'href:/logout',      kbd: '' },
  ];

  // The rows below are built as an innerHTML string, and OP_PALETTE_DATA is
  // meant to carry real dashboard titles and owner names (see palette.html) —
  // free text written by whoever created the dashboard. The palette ships on
  // every shell page, so an unescaped title would run for anyone pressing ⌘K.
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

  function getCommands() {
    const server = (window.OP_PALETTE_DATA || []).map(d => ({
      group: 'Jump to dashboard',
      label: d.title,
      sub: `${d.dash_type} · v${d.current_version} · ${d.owner || '—'}`,
      icon: 'dash',
      action: `href:/d/${d.id}`,
      kbd: ''
    }));
    const questCmds = window.OP_QUESTS_ON
      ? STATIC_COMMANDS
      : STATIC_COMMANDS.filter(c => c.action !== 'href:/quests');
    const staticCmds = window.OP_EXPLORE_ON
      ? questCmds
      : questCmds.filter(c => c.action !== 'href:/explore');
    return staticCmds.concat(server);
  }

  function open() {
    overlay.classList.add('visible');
    input.value = '';
    activeIdx = 0;
    render('');
    setTimeout(() => input.focus(), 0);
  }
  function close() { overlay.classList.remove('visible'); }
  function toggle() { overlay.classList.contains('visible') ? close() : open(); }

  function fuzzy(item, q) {
    if (!q) return true;
    const hay = (item.label + ' ' + (item.sub||'') + ' ' + item.group).toLowerCase();
    return hay.includes(q.toLowerCase());
  }

  function render(q) {
    const all = getCommands();
    filtered = all.filter(c => fuzzy(c, q));
    if (!filtered.length) {
      list.innerHTML = `<div class="empty" style="padding:32px 0">No matches for "${q}"</div>`;
      return;
    }
    const groups = {};
    filtered.forEach(c => { (groups[c.group] = groups[c.group] || []).push(c); });
    let idx = 0;
    list.innerHTML = Object.entries(groups).map(([g, items]) => `
      <div class="palette-group-label">${esc(g)}</div>
      ${items.map(it => {
        const i = idx++;
        return `<div class="palette-row" data-idx="${i}" data-action="${esc(it.action)}">
          <svg class="ico"><use href="#ic-${esc(it.icon)}"/></svg>
          <div class="label-block">
            <span class="label">${esc(it.label)}</span>
            <span class="sub">${esc(it.sub || '')}</span>
          </div>
          ${it.kbd ? `<span class="meta">${esc(it.kbd)}</span>` : ''}
        </div>`;
      }).join('')}
    `).join('');
    updateActive();
  }

  function updateActive() {
    const rows = list.querySelectorAll('.palette-row');
    rows.forEach((r, i) => r.classList.toggle('active', i === activeIdx));
    const act = list.querySelector('.palette-row.active');
    if (act) act.scrollIntoView({ block: 'nearest' });
  }

  function exec(action) {
    if (!action) return;
    const [verb, arg] = action.split(':');
    close();
    if (verb === 'href') {
      window.location.href = arg;
    } else if (verb === 'toggle') {
      if (arg === 'theme') {
        const cur = document.documentElement.getAttribute('data-theme') || 'dark';
        // ADR-17b: for a person on the autumn perk, «light» IS autumn — the
        // toggle goes autumn ↔ dark instead of silently dropping the perk.
        let lightName = 'light';
        try {
          const uid = String((window.OP_USER || {}).person_id || 'anon');
          const raw = localStorage.getItem('op-quests-companion-' + uid);
          const info = raw ? JSON.parse(raw) : null;
          if (info && info.autumn) lightName = 'autumn';
        } catch (e) { /* no perk info — plain light/dark */ }
        window.OP_UI.set('theme', cur === 'dark' ? lightName : 'dark');
      } else if (arg === 'sidebar') {
        window.OP_UI.toggle('sidebar', 'expanded', 'collapsed');
      }
    }
  }

  input.addEventListener('input', e => { activeIdx = 0; render(e.target.value); });
  input.addEventListener('keydown', e => {
    const rows = list.querySelectorAll('.palette-row');
    if (e.key === 'ArrowDown') { e.preventDefault(); activeIdx = Math.min(activeIdx + 1, rows.length - 1); updateActive(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); activeIdx = Math.max(activeIdx - 1, 0); updateActive(); }
    else if (e.key === 'Enter') {
      e.preventDefault();
      const row = rows[activeIdx];
      if (row) exec(row.getAttribute('data-action'));
    } else if (e.key === 'Escape') {
      e.preventDefault();
      close();
    }
  });
  list.addEventListener('click', e => {
    const row = e.target.closest('.palette-row');
    if (row) exec(row.getAttribute('data-action'));
  });
  overlay.addEventListener('click', e => { if (e.target === overlay) close(); });
  document.addEventListener('click', e => {
    if (e.target.closest('#cmdk-pill')) open();
  });

  // Global hotkey
  // `e.code` is layout-independent: with a Russian layout `e.key` is a Cyrillic letter,
  // and Ctrl+K would fall through to the browser's address-bar search.
  window.addEventListener('keydown', e => {
    if ((e.metaKey || e.ctrlKey) && (e.code === 'KeyK' || e.key.toLowerCase() === 'k')) {
      e.preventDefault();
      toggle();
    }
  });

  window.OP_PALETTE = { open, close, toggle };
})();
