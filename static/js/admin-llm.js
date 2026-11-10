/* OnePage — /admin: LLM runtime-config card (GET/PUT /api/admin/llm).
 * Keys are write-only: the form never receives them back, only *_set flags. */
(function () {
  'use strict';

  const $form = document.getElementById('form-llm');
  if (!$form) return;
  const $msg = document.getElementById('llm-msg');
  const $eff = document.getElementById('llm-effective');

  function keyState(el, isSet) {
    if (el) el.textContent = isSet ? t('· set') : t('· not set');
  }

  async function load() {
    const res = await fetch('/api/admin/llm');
    if (!res.ok) { $msg.textContent = t('Failed to load config'); return; }
    const body = await res.json();
    const eff = body.effective;
    $form.elements.llm_provider.value = eff.provider;
    $form.elements.llm_browser.checked = !!eff.browser;
    $form.elements.llm_model.value = body.overrides.llm_model || '';
    keyState(document.getElementById('llm-bkey-state'), eff.browser_key_set);
    keyState(document.getElementById('llm-akey-state'), eff.api_key_set);
    $eff.textContent = t('now: {p1}{p2} · {p3}', {p1: eff.provider, p2: (eff.browser ? t(' · browser') : t(' · server')), p3: (eff.model || '—')});
  }

  $form.addEventListener('submit', async e => {
    e.preventDefault();
    const body = {
      llm_provider: $form.elements.llm_provider.value,
      llm_browser: $form.elements.llm_browser.checked,
    };
    const model = $form.elements.llm_model.value.trim();
    body.llm_model = model || undefined;
    const reset = [];
    if (!model) reset.push('llm_model');
    const bkey = $form.elements.llm_browser_key.value;
    if (bkey) body.llm_browser_key = bkey;
    const akey = $form.elements.llm_api_key.value;
    if (akey) body.llm_api_key = akey;
    if (reset.length) body.reset = reset;
    $msg.textContent = t('Saving…');
    const res = await fetch('/api/admin/llm', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      let detail = res.status;
      try { detail = (await res.json()).detail || detail; } catch (err) {}
      $msg.textContent = t('Error: {p1}', {p1: detail});
      return;
    }
    $form.elements.llm_browser_key.value = '';
    $form.elements.llm_api_key.value = '';
    $msg.textContent = t('Saved');
    await load();
  });

  // ── DeepSeek allowlist ─────────────────────────────────────────────
  const $aList = document.getElementById('llm-access-list');
  const $aMsg = document.getElementById('llm-access-msg');
  const $aGrant = document.getElementById('llm-access-grant');
  const $aNote = document.getElementById('llm-access-note');
  const $aInput = document.getElementById('llm-access-input');

  async function loadAccess() {
    if (!$aList) return;
    const res = await fetch('/api/admin/llm/access');
    if (!res.ok) return;
    const body = await res.json();
    $aGrant.hidden = !body.can_assign;
    $aNote.hidden = body.can_assign;
    $aList.textContent = '';
    for (const it of body.items) {
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;align-items:center;gap:8px;padding:4px 0;border-bottom:1px solid var(--border)';
      const name = document.createElement('span');
      name.className = 'mono';
      name.style.fontSize = '12px';
      name.textContent = it.person_id + (it.display_name ? ' · ' + it.display_name : '');
      row.appendChild(name);
      const grow = document.createElement('span');
      grow.style.flex = '1';
      row.appendChild(grow);
      if (body.can_assign) {
        const del = document.createElement('button');
        del.type = 'button';
        del.className = 'btn sm ghost';
        del.textContent = t('Revoke');
        del.addEventListener('click', async () => {
          del.disabled = true;
          const r = await fetch('/api/admin/llm/access/' + encodeURIComponent(it.person_id), { method: 'DELETE' });
          if (!r.ok) { $aMsg.textContent = t('Failed to revoke'); del.disabled = false; return; }
          await loadAccess();
        });
        row.appendChild(del);
      }
      $aList.appendChild(row);
    }
    if (!body.items.length) {
      const empty = document.createElement('div');
      empty.className = 'mono';
      empty.style.cssText = 'font-size:11px;color:var(--ink-faint);padding:4px 0';
      empty.textContent = t('List is empty – DeepSeek is not available to anyone');
      $aList.appendChild(empty);
    }
  }

  document.getElementById('llm-access-add')?.addEventListener('click', async () => {
    const id = ($aInput.value || '').trim();
    if (!id) return;
    $aMsg.textContent = '';
    const r = await fetch('/api/admin/llm/access/' + encodeURIComponent(id), { method: 'PUT' });
    if (!r.ok) {
      let detail = r.status;
      try { detail = (await r.json()).detail || detail; } catch (err) {}
      $aMsg.textContent = t('Error: {p1}', {p1: detail});
      return;
    }
    $aInput.value = '';
    await loadAccess();
  });

  load();
  loadAccess();
})();
