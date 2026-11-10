/* OnePage — audit.js (audit log search + JSONL export) */

(function() {
  document.getElementById('audit-search')?.addEventListener('input', e => {
    const q = e.target.value.toLowerCase();
    document.querySelectorAll('.audit-table tbody tr').forEach(r => {
      r.style.display = !q || (r.dataset.search || '').includes(q) ? '' : 'none';
    });
  });
})();

(function() {
  const fromEl = document.getElementById('audit-export-from');
  const toEl = document.getElementById('audit-export-to');
  const btn = document.getElementById('btn-audit-export');
  if (!fromEl || !toEl || !btn) return;

  // Default: last 7 days (inclusive) — a sane window to click without typing.
  const fmt = d => d.toISOString().slice(0, 10);
  const today = new Date();
  const weekAgo = new Date(today);
  weekAgo.setDate(weekAgo.getDate() - 6);
  toEl.value = fmt(today);
  fromEl.value = fmt(weekAgo);

  btn.addEventListener('click', () => {
    const from = fromEl.value, to = toEl.value;
    if (!from || !to) return;
    // GET with Content-Disposition: attachment — browser downloads and stays
    // on the page, no need for a fetch+blob dance.
    window.location.href = `/api/admin/audit-log/export?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`;
  });
})();
