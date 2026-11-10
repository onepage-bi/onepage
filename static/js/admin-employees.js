/* OnePage — /admin/employees: CSV import + directory search.
 * Every employee-sourced string is written with textContent — never innerHTML. */
(function () {
  'use strict';

  const $file = document.getElementById('emp-file');
  const $btn = document.getElementById('emp-import-btn');
  const $status = document.getElementById('emp-import-status');
  const $search = document.getElementById('emp-search');
  const $results = document.getElementById('emp-search-results');
  if (!$file || !$btn) return;

  function say(text, isError) {
    $status.hidden = false;
    $status.textContent = text;
    $status.style.color = isError ? 'var(--danger, #c33)' : 'var(--ink-dim)';
  }

  $file.addEventListener('change', () => { $btn.disabled = !$file.files.length; });

  $btn.addEventListener('click', async () => {
    const f = $file.files[0];
    if (!f) return;
    $btn.disabled = true;
    say(t('Loading… {ctx:2}'), false);
    const fd = new FormData();
    fd.append('file', f);
    try {
      const res = await fetch('/api/admin/employees/import', { method: 'POST', body: fd });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        say(t('Error: {p1}', {p1: (body.detail || res.status)}), true);
        $btn.disabled = false;
        return;
      }
      const staleMsg = body.stale_users_count
        ? t(' Active OnePage accounts for inactive employees: {p1}.', {p1: body.stale_users_count})
        : '';
      say(t('Done: {p1} rows, +{p2} new, ~{p3} updated, −{p4} deactivated.{p5} Refreshing page…', {p1: body.total, p2: body.added, p3: body.updated, p4: body.deactivated, p5: staleMsg}), false);
      setTimeout(() => location.reload(), 1200);
    } catch (err) {
      say(t('Network: {p1}', {p1: err}), true);
      $btn.disabled = false;
    }
  });

  // Directory search — same endpoint the user-suggest typeahead uses.
  let seq = 0;
  async function search(q) {
    const my = ++seq;
    const res = await fetch('/api/employees/suggest?q=' + encodeURIComponent(q));
    if (!res.ok || my !== seq) return;
    const body = await res.json();
    if (my !== seq) return;
    $results.textContent = '';
    for (const it of body.items) {
      const row = document.createElement('div');
      row.className = 'emp-row';
      const name = document.createElement('span');
      name.textContent = it.name_ru || it.name_en;
      const sub = document.createElement('span');
      sub.className = 'mono';
      sub.style.cssText = 'font-size:11px;color:var(--ink-dim)';
      sub.textContent = [it.person_id, it.name_en !== (it.name_ru || it.name_en) ? it.name_en : '', it.dept, it.title]
        .filter(Boolean).join(' · ');
      row.appendChild(name);
      row.appendChild(sub);
      $results.appendChild(row);
    }
    if (!body.items.length && q.trim().length >= 2) {
      const empty = document.createElement('div');
      empty.className = 'mono';
      empty.style.cssText = 'font-size:12px;color:var(--ink-faint);padding:4px 0';
      empty.textContent = t('Nothing found');
      $results.appendChild(empty);
    }
  }

  let tv = null;
  $search.addEventListener('input', () => {
    clearTimeout(tv);
    const q = $search.value;
    if (q.trim().length < 2) { $results.textContent = ''; return; }
    tv = setTimeout(() => search(q), 200);
  });
})();
