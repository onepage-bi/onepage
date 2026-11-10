/* OnePage — /admin/scopes: territory import (dry-run + real), manual grants,
 * lookup. Every server-sourced string is written with textContent — never
 * innerHTML. JSON requests always carry Content-Type: application/json
 * (OriginGuard rejects a body with no Content-Type). */
(function () {
  'use strict';

  const $file = document.getElementById('sc-file');
  const $checkBtn = document.getElementById('sc-check-btn');
  const $importBtn = document.getElementById('sc-import-btn');
  const $status = document.getElementById('sc-import-status');
  const $unresolved = document.getElementById('sc-unresolved');
  if (!$file || !$checkBtn || !$importBtn) return;

  function say($el, text, isError) {
    $el.hidden = false;
    $el.textContent = text;
    $el.style.color = isError ? 'var(--danger, #c33)' : 'var(--ink-dim)';
  }

  $file.addEventListener('change', () => {
    const has = !!$file.files.length;
    $checkBtn.disabled = !has;
    $importBtn.disabled = !has;
  });

  async function runImport(dryRun) {
    const f = $file.files[0];
    if (!f) return;
    $checkBtn.disabled = true;
    $importBtn.disabled = true;
    $unresolved.hidden = true;
    say($status, dryRun ? t('Validating…') : t('Importing…'), false);
    const fd = new FormData();
    fd.append('file', f);
    fd.append('dry_run', dryRun ? '1' : '0');
    try {
      const res = await fetch('/api/admin/scopes/import', { method: 'POST', body: fd });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        say($status, t('Error: {p1}', {p1: (body.detail || res.status)}), true);
        return;
      }
      const verb = dryRun ? t('Validation: ') : t('Done: ');
      say($status, t('{p1}{p2} rows, persons {p3} (found {p4}, not found {p5}), nodes {p6}.', {p1: verb, p2: body.total, p3: body.persons, p4: body.resolved, p5: body.unresolved, p6: body.nodes}), false);
      if (body.unresolved_sample && body.unresolved_sample.length) {
        $unresolved.hidden = false;
        $unresolved.textContent = t('Not found: {p1}', {p1: body.unresolved_sample.join(', ')});
      }
      if (!dryRun) {
        setTimeout(() => location.reload(), 1500);
      }
    } catch (err) {
      say($status, t('Network: {p1}', {p1: err}), true);
    } finally {
      $checkBtn.disabled = !$file.files.length;
      $importBtn.disabled = !$file.files.length;
    }
  }

  $checkBtn.addEventListener('click', () => runImport(true));
  $importBtn.addEventListener('click', () => runImport(false));

  // ── Manual grant ──────────────────────────────────────────────────────
  const $manualPersonId = document.getElementById('sc-manual-person_id');
  const $manualSeeAll = document.getElementById('sc-manual-see-all');
  const $manualNode = document.getElementById('sc-manual-node');
  const $manualRole = document.getElementById('sc-manual-role');
  const $manualGrantBtn = document.getElementById('sc-manual-grant-btn');
  const $manualRemoveBtn = document.getElementById('sc-manual-remove-btn');
  const $manualStatus = document.getElementById('sc-manual-status');

  async function postManual(body) {
    const res = await fetch('/api/admin/scopes/manual', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      say($manualStatus, t('Error: {p1}', {p1: (data.detail || res.status)}), true);
      return null;
    }
    return data;
  }

  $manualGrantBtn.addEventListener('click', async () => {
    const person_id = $manualPersonId.value.trim();
    if (!person_id) { say($manualStatus, t('Specify person_id'), true); return; }
    const body = { person_id };
    if ($manualSeeAll.checked) {
      body.see_all = true;
    } else if ($manualNode.value.trim()) {
      body.node = $manualNode.value.trim();
      body.role = $manualRole.value.trim() || 'HOU_ADD';
    } else {
      say($manualStatus, t('Check see_all or specify a node'), true);
      return;
    }
    const data = await postManual(body);
    if (data) say($manualStatus, t('Done: {p1}', {p1: JSON.stringify(data)}), false);
  });

  $manualRemoveBtn.addEventListener('click', async () => {
    const person_id = $manualPersonId.value.trim();
    if (!person_id) { say($manualStatus, t('Specify person_id'), true); return; }
    const data = await postManual({ person_id, remove: true });
    if (data) say($manualStatus, t('Manual grants removed for {p1}', {p1: person_id}), false);
  });

  // ── Lookup ────────────────────────────────────────────────────────────
  const $lookup = document.getElementById('sc-lookup');
  const $lookupResult = document.getElementById('sc-lookup-result');
  let seq = 0;
  async function lookup(q) {
    const my = ++seq;
    const res = await fetch('/api/admin/scopes/lookup?q=' + encodeURIComponent(q));
    if (!res.ok || my !== seq) return;
    const body = await res.json();
    if (my !== seq) return;
    $lookupResult.textContent = '';
    if (!body.found) {
      $lookupResult.textContent = t('No row in user_scopes – user won\'t see any rows in the RLS report.');
      return;
    }
    const line = document.createElement('div');
    line.textContent = body.see_all
      ? t('Full access (see_all). Roles: {p1}', {p1: (body.roles.join(', ') || '—')})
      : t('Keys: {p1}. Roles: {p2}', {p1: body.key_count, p2: (body.roles.join(', ') || '—')});
    $lookupResult.appendChild(line);
    if (!body.see_all && body.keys_sample.length) {
      const sample = document.createElement('div');
      sample.className = 'mono';
      sample.style.cssText = 'font-size:11px;color:var(--ink-faint);margin-top:4px';
      sample.textContent = body.keys_sample.join(', ');
      $lookupResult.appendChild(sample);
    }
  }

  let tv = null;
  $lookup.addEventListener('input', () => {
    clearTimeout(tv);
    const q = $lookup.value;
    if (!q.trim()) { $lookupResult.textContent = ''; return; }
    tv = setTimeout(() => lookup(q), 200);
  });
})();
