/* OnePage — typeahead over the employee directory (/api/employees/suggest).
 *
 * Attach by adding `data-user-suggest` to any text input whose VALUE must end
 * up a person_id (grant-access on a dashboard, add-user in admin). The person
 * types a name in Russian or English (or the id itself), picks a row, and the
 * input receives the person_id. With an empty directory the input keeps
 * working as the plain id field it always was — the dropdown just never opens.
 *
 * Every rendered string is employee data → textContent only, never innerHTML.
 */
(function () {
  'use strict';

  function attach(input) {
    let box = null;
    let items = [];
    let active = -1;
    let seq = 0;
    let timer = null;

    function close() {
      if (box) { box.remove(); box = null; }
      items = []; active = -1;
    }

    function pick(i) {
      const it = items[i];
      if (!it || !it.person_id) return;
      input.value = it.person_id;
      input.dispatchEvent(new Event('change', { bubbles: true }));
      close();
      input.focus();
    }

    function render() {
      if (box) { box.remove(); box = null; }  // NOT close(): that clears items
      active = -1;
      if (!items.length) return;
      box = document.createElement('div');
      box.className = 'user-suggest-box';
      items.forEach((it, i) => {
        const row = document.createElement('button');
        row.type = 'button';
        row.className = 'user-suggest-row';
        row.dataset.i = String(i);
        const name = document.createElement('span');
        name.className = 'us-name';
        name.textContent = it.name_ru || it.name_en || it.person_id || '';
        const sub = document.createElement('span');
        sub.className = 'us-sub mono';
        sub.textContent = [it.person_id, it.name_en && it.name_en !== name.textContent ? it.name_en : '', it.dept]
          .filter(Boolean).join(' · ');
        row.appendChild(name);
        row.appendChild(sub);
        row.addEventListener('mousedown', e => { e.preventDefault(); pick(i); });
        box.appendChild(row);
      });
      // Position under the input inside its offset parent.
      const wrap = input.closest('.input-wrap') || input.parentElement;
      wrap.style.position = wrap.style.position || 'relative';
      box.style.top = (input.offsetTop + input.offsetHeight + 2) + 'px';
      wrap.appendChild(box);
    }

    function highlight(delta) {
      if (!box) return;
      const rows = box.querySelectorAll('.user-suggest-row');
      if (!rows.length) return;
      active = (active + delta + rows.length) % rows.length;
      rows.forEach((r, i) => r.classList.toggle('active', i === active));
    }

    async function query(q) {
      const my = ++seq;
      let body;
      try {
        const res = await fetch('/api/employees/suggest?q=' + encodeURIComponent(q));
        if (!res.ok) return;
        body = await res.json();
      } catch (err) { return; }
      if (my !== seq) return;
      items = (body && body.items) || [];
      active = -1;
      render();
    }

    input.setAttribute('autocomplete', 'off');
    input.addEventListener('input', () => {
      clearTimeout(timer);
      const q = input.value;
      if (q.trim().length < 2) { close(); return; }
      timer = setTimeout(() => query(q), 200);
    });
    input.addEventListener('keydown', e => {
      if (!box) return;
      if (e.key === 'ArrowDown') { e.preventDefault(); highlight(1); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); highlight(-1); }
      else if (e.key === 'Enter' && active >= 0) { e.preventDefault(); pick(active); }
      else if (e.key === 'Escape') { close(); }
    });
    input.addEventListener('blur', () => setTimeout(close, 150));
  }

  document.querySelectorAll('input[data-user-suggest]').forEach(attach);
})();
