/* OnePage — main.js (shared helpers, theme/sidebar persistence) */

(function() {
  const html = document.documentElement;

  // ── Tweak state persistence ─────────────────────────────────────────
  // Reads server defaults from <html data-...> and syncs with localStorage.
  ['theme','accent','density','sidebar'].forEach(k => {
    const saved = localStorage.getItem('op-ui-' + k);
    if (saved) html.setAttribute('data-' + k, saved);
  });

  window.OP_UI = {
    set(key, val) {
      html.setAttribute('data-' + key, val);
      try { localStorage.setItem('op-ui-' + key, val); } catch (e) {}
    },
    get(key) {
      return html.getAttribute('data-' + key);
    },
    toggle(key, a, b) {
      const cur = html.getAttribute('data-' + key);
      window.OP_UI.set(key, cur === a ? b : a);
    }
  };

  // ── Persistent storage ──────────────────────────────────────────────
  // Folder handles (Run, Explore), the Parquet cache and chat threads
  // all live in this origin's storage; Chrome evicts it whole under disk
  // pressure unless the site is persistent. Ask once, after a real click
  // (the request is ignored without a user gesture).
  let persistAsked = false;
  document.addEventListener('click', function onFirst() {
    if (persistAsked) return;
    persistAsked = true;
    try {
      if (navigator.storage && navigator.storage.persist) navigator.storage.persist().catch(function () {});
    } catch (e) { /* not supported */ }
  }, { once: true });

  // ── Sidebar collapse ────────────────────────────────────────────────
  function syncSidebarCollapseTitle() {
    const btn = document.getElementById('sidebar-collapse');
    if (!btn) return;
    const collapsed = html.getAttribute('data-sidebar') === 'collapsed';
    const title = collapsed ? btn.getAttribute('data-title-collapsed') : btn.getAttribute('data-title-expanded');
    if (title) btn.setAttribute('title', title);
  }
  syncSidebarCollapseTitle();

  document.addEventListener('click', e => {
    const btn = e.target.closest('#sidebar-collapse');
    if (btn) {
      window.OP_UI.toggle('sidebar', 'expanded', 'collapsed');
      syncSidebarCollapseTitle();
    }
  });

  // Click on an empty area of the sidebar (not on an interactive child)
  // also toggles collapse — hotkeys ([ / ⌘K) are not discoverable enough.
  document.addEventListener('click', e => {
    const aside = e.target.closest('aside.sidebar');
    if (!aside) return;
    if (e.target.closest('#sidebar-collapse')) return; // handled above, avoid double-toggle
    if (e.target.closest('a, button, input, select, textarea, label, .nav-item, .user-chip, .sidebar-logo')) return;
    // Don't hijack a text-selection drag ending inside the sidebar.
    const sel = window.getSelection && window.getSelection();
    if (sel && String(sel.toString()).length > 0) return;
    window.OP_UI.toggle('sidebar', 'expanded', 'collapsed');
    syncSidebarCollapseTitle();
  });

  // ── Tiny DOM helpers (also re-exposed on window) ────────────────────
  window.$  = (sel, root=document) => root.querySelector(sel);
  window.$$ = (sel, root=document) => [...root.querySelectorAll(sel)];

  // ── Toast (for inline feedback) ─────────────────────────────────────
  window.OP_TOAST = function(msg, kind='info') {
    let tv = document.getElementById('op-toast');
    if (!tv) {
      tv = document.createElement('div');
      tv.id = 'op-toast';
      tv.style.cssText = 'position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--surface-3);color:var(--ink);padding:10px 16px;border-radius:8px;box-shadow:var(--elev-2);font-size:12px;z-index:1000;font-family:var(--font-sans);transition:opacity .2s;';
      document.body.appendChild(tv);
    }
    tv.textContent = msg;
    if (kind === 'success') tv.style.color = 'var(--success)';
    else if (kind === 'error') tv.style.color = 'var(--danger)';
    else if (kind === 'warning') tv.style.color = 'var(--warning)';
    else tv.style.color = 'var(--ink)';
    tv.style.opacity = '1';
    clearTimeout(window.__opToastTimer);
    window.__opToastTimer = setTimeout(() => { tv.style.opacity = '0'; }, 2400);
  };

  // ── Table sort (generic) ────────────────────────────────────────────
  document.addEventListener('click', e => {
    const th = e.target.closest('th.sortable');
    if (!th) return;
    const table = th.closest('table');
    if (!table) return;
    const tbody = table.tBodies[0];
    if (!tbody) return;
    const idx = [...th.parentNode.children].indexOf(th);
    const isNumeric = th.classList.contains('numeric');
    const asc = !th.classList.contains('sorted-asc');
    [...th.parentNode.children].forEach(tv => tv.classList.remove('sorted-asc','sorted-desc'));
    th.classList.add(asc ? 'sorted-asc' : 'sorted-desc');
    const rows = [...tbody.querySelectorAll('tr')];
    rows.sort((a, b) => {
      const av = (a.children[idx]?.textContent || '').trim();
      const bv = (b.children[idx]?.textContent || '').trim();
      if (isNumeric) {
        const an = parseFloat(av.replace(/[^\d.-]/g, '')) || 0;
        const bn = parseFloat(bv.replace(/[^\d.-]/g, '')) || 0;
        return asc ? an - bn : bn - an;
      }
      return asc ? av.localeCompare(bv) : bv.localeCompare(av);
    });
    rows.forEach(r => tbody.appendChild(r));
  });

  // ── Toggle component ────────────────────────────────────────────────
  document.addEventListener('click', e => {
    const tv = e.target.closest('.toggle');
    if (!tv) return;
    tv.setAttribute('aria-checked', tv.getAttribute('aria-checked') === 'true' ? 'false' : 'true');
    tv.dispatchEvent(new CustomEvent('op:toggle', {
      detail: { checked: tv.getAttribute('aria-checked') === 'true' },
      bubbles: true
    }));
  });

  // ── Segmented control ──────────────────────────────────────────────
  document.addEventListener('click', e => {
    const btn = e.target.closest('.seg button');
    if (!btn) return;
    const seg = btn.parentNode;
    [...seg.children].forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
  });

  // ── Language switcher (components/lang_switch.html) ─────────────────
  // Signed in: PUT /api/me/lang writes users.lang, audit-logs it and returns
  // the op_lang cookie. Anonymous (/login): the cookie is all there is, so the
  // page writes it itself. Either way the page then reloads — re-rendering is
  // simpler and safer than re-labelling every node, and switching language is
  // a once-in-a-while act, not an interaction.
  (function () {
    const switches = document.querySelectorAll('[data-lang-switch]');
    if (!switches.length) return;

    function closeAll() {
      switches.forEach(sw => {
        sw.querySelector('.lang-menu').hidden = true;
        sw.querySelector('.lang-trigger').setAttribute('aria-expanded', 'false');
      });
    }

    document.addEventListener('click', async e => {
      const trigger = e.target.closest('[data-lang-switch] .lang-trigger');
      if (trigger) {
        const menu = trigger.parentNode.querySelector('.lang-menu');
        const open = menu.hidden;
        closeAll();
        menu.hidden = !open;
        trigger.setAttribute('aria-expanded', String(open));
        return;
      }
      const opt = e.target.closest('[data-lang-switch] .lang-opt');
      if (!opt) { closeAll(); return; }
      const lang = opt.dataset.lang;
      closeAll();
      if (lang === document.documentElement.getAttribute('lang')) return;
      if (window.OP_USER) {
        try {
          await fetch('/api/me/lang', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ lang: lang })
          });
        } catch (err) { /* fall through: the cookie below still switches it */ }
      }
      // Path=/ so the cookie also covers /login and /share; one year, like the
      // one the API sets.
      document.cookie = 'op_lang=' + encodeURIComponent(lang) +
        '; path=/; max-age=31536000; samesite=lax';
      location.reload();
    });

    document.addEventListener('keydown', e => { if (e.key === 'Escape') closeAll(); });
  })();

  // ── Data Quality badge (shell pages only) ───────────────────────────────
  (function() {
    const bell = document.getElementById('dq-bell');
    if (!bell) return;
    fetch('/api/data-quality/badge')
      .then(r => r.json())
      .catch(() => ({}))
      .then(data => {
        const badge = bell.querySelector('[data-dq-badge]');
        if (badge && data.open > 0) {
          badge.textContent = data.open;
          badge.removeAttribute('hidden');
        }
      });
  })();

})();
