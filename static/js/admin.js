/* OnePage — admin.js (user CRUD + filters) */

(function() {

  // ── Search filter ───────────────────────────────────────────────────
  document.getElementById('users-search')?.addEventListener('input', e => {
    const q = e.target.value.toLowerCase();
    document.querySelectorAll('#users-table tbody tr').forEach(r => {
      r.style.display = !q || (r.dataset.search || '').includes(q) ? '' : 'none';
    });
  });

  // ── Role segmented control ──────────────────────────────────────────
  document.addEventListener('click', async e => {
    const btn = e.target.closest('[data-role-seg] button[data-role]');
    if (!btn) return;
    const seg = btn.parentNode;
    const uid = seg.dataset.roleSeg;
    const role = btn.dataset.role;
    try {
      const res = await fetch(`/api/admin/users/${uid}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ role })
      });
      if (!res.ok) throw new Error();
      window.OP_TOAST(t('Role changed to {role}', {role: role}), 'success');
    } catch {
      window.OP_TOAST(t('Role change failed'), 'error');
    }
  });

  // ── Active toggle ───────────────────────────────────────────────────
  document.addEventListener('op:toggle', async e => {
    const tv = e.target.closest('[data-toggle-active]');
    if (!tv) return;
    const uid = tv.dataset.toggleActive;
    try {
      await fetch(`/api/admin/users/${uid}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ is_active: e.detail.checked })
      });
    } catch {
      window.OP_TOAST(t('Status change failed'), 'error');
    }
  });

  // ── Delete user ─────────────────────────────────────────────────────
  document.querySelectorAll('.btn-delete-user').forEach(b => {
    b.addEventListener('click', async () => {
      const uid = b.dataset.uid;
      const name = b.dataset.name;
      if (!confirm(t('Delete user {name}? Their permissions and audit history are preserved.', {name: name}))) return;
      const res = await fetch(`/api/admin/users/${uid}`, { method: 'DELETE' });
      if (res.ok) b.closest('tr').remove();
      else window.OP_TOAST(t('Delete failed'), 'error');
    });
  });

  // ── Stale accounts: OnePage active, but HR marks the person inactive ──
  const staleTbody = document.querySelector('#stale-users-table tbody');
  const staleEmpty = document.getElementById('stale-users-empty');
  const staleCount = document.getElementById('stale-users-count');
  const staleNorecord = document.getElementById('stale-users-norecord');

  function fmtSeen(iso) {
    if (!iso) return '—';
    // Matches the "Last login" column's own formatting (server sends
    // naive-UTC isoformat(); no timezone math here, just trim to minutes).
    return iso.replace('T', ' ').slice(0, 16);
  }

  async function loadStaleUsers() {
    if (!staleTbody) return;
    try {
      const res = await fetch('/api/admin/users/stale');
      if (!res.ok) return;
      const data = await res.json();
      renderStaleUsers(data);
    } catch {
      // Non-critical panel — the main user table above still works.
    }
  }

  function renderStaleUsers(data) {
    const items = data.items || [];
    staleTbody.textContent = '';
    staleEmpty.hidden = items.length !== 0;
    if (staleCount) staleCount.textContent = items.length ? String(items.length) : '';
    items.forEach(it => {
      const tr = document.createElement('tr');
      tr.dataset.uid = it.id;
      const tdId = document.createElement('td');
      tdId.className = 'mono';
      tdId.textContent = it.person_id;
      const tdName = document.createElement('td');
      tdName.textContent = it.display_name || '—';
      const tdRole = document.createElement('td');
      tdRole.className = 'mono muted';
      tdRole.textContent = it.role || '—';
      const tdSeen = document.createElement('td');
      tdSeen.className = 'mono muted';
      tdSeen.textContent = fmtSeen(it.last_seen);
      const tdAction = document.createElement('td');
      const btn = document.createElement('button');
      btn.className = 'btn xs ghost';
      btn.type = 'button';
      btn.textContent = t('Deactivate');
      btn.addEventListener('click', () => deactivateStale(it.id, it.person_id, tr, btn));
      tdAction.appendChild(btn);
      tr.append(tdId, tdName, tdRole, tdSeen, tdAction);
      staleTbody.appendChild(tr);
    });
    if (staleNorecord) {
      staleNorecord.textContent = t('without reference table entry: {p1}', {p1: (data.no_employee_record || 0)});
    }
  }

  async function deactivateStale(uid, person_id, row, btn) {
    if (!confirm(t('Deactivate {p1}? Sessions will be closed.', {p1: person_id}))) return;
    btn.disabled = true;
    try {
      const res = await fetch(`/api/admin/users/${uid}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ is_active: false })
      });
      if (!res.ok) throw new Error();
      row.remove();
      if (staleTbody.children.length === 0 && staleEmpty) staleEmpty.hidden = false;
      if (staleCount) staleCount.textContent = String(staleTbody.children.length || '');
      // Keep the main table's toggle/row in sync without a full reload.
      const mainToggle = document.querySelector(`#users-table [data-toggle-active="${uid}"]`);
      if (mainToggle) mainToggle.setAttribute('aria-checked', 'false');
      window.OP_TOAST(t('{p1} deactivated', {p1: person_id}), 'success');
    } catch {
      window.OP_TOAST(t('Failed to deactivate'), 'error');
      btn.disabled = false;
    }
  }

  loadStaleUsers();

  // ── Add user form ───────────────────────────────────────────────────
  document.getElementById('form-add-user')?.addEventListener('submit', async e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const body = Object.fromEntries(fd);
    const msg = document.getElementById('add-user-msg');
    try {
      const res = await fetch('/api/admin/users', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      if (!res.ok) throw new Error((await res.json()).detail || t('failed'));
      msg.textContent = t('Added {id}', {id: body.person_id});
      msg.style.color = 'var(--success)';
      window.OP_TOAST(t('Added {id}', {id: body.person_id}), 'success');
      setTimeout(() => location.reload(), 700);
    } catch (err) {
      msg.textContent = String(err.message || err);
      msg.style.color = 'var(--danger)';
    }
  });
})();
