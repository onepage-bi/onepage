/* OnePage — anonymizer.js (UI thread)
 * 100% client-side. All parsing/masking runs in anonymizer.worker.js so the tab
 * never freezes. This file only renders config and handles the download.
 * Tables and header rows are detected automatically; the header row is overridable.
 */
(function () {
  'use strict';

  const MAX_BYTES = 10 * 1024 * 1024;
  const MAX_CELLS = 300000;

  const CATEGORIES = ['keep', 'name', 'email', 'phone', 'inn', 'snils', 'passport', 'city', 'company', 'address', 'code', 'text', 'number', 'date', 'bool'];
  const CAT_LABEL = { keep: t('— keep —'), name: t('Full name'), email: t('Email'), phone: t('Phone'), inn: t('INN'), snils: t('SNILS'), passport: t('Passport'), city: t('City'), company: t('Company'), address: t('Address'), code: t('Code'), text: t('Text'), number: t('Number'), date: t('Date'), bool: t('Boolean') };
  const PII = ['name', 'email', 'phone', 'inn', 'snils', 'passport', 'address'];
  const MIME = {
    xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    xlsb: 'application/vnd.ms-excel.sheet.binary.macroEnabled.12',
    xls: 'application/vnd.ms-excel', csv: 'text/csv',
    ods: 'application/vnd.oasis.opendocument.spreadsheet',
  };

  let MODEL = null, BASE = 'data', OUT_EXT = 'xlsx', busy = false;

  const $drop = document.getElementById('anon-drop');
  const $input = document.getElementById('anon-file-input');
  const $config = document.getElementById('anon-config');
  const $sheets = document.getElementById('anon-sheets');
  const $meta = document.getElementById('anon-file-meta');
  const $summary = document.getElementById('anon-summary');
  const $run = document.getElementById('btn-anon-run');

  let worker = null, watch = null;

  function setRunIdle() { busy = false; $run.disabled = false; $run.textContent = t('Generate masked file'); }
  function clearWatch() { if (watch) { clearTimeout(watch); watch = null; } }
  function startWatch(ms, label) {
    clearWatch();
    watch = setTimeout(() => {
      recreateWorker();
      setRunIdle();
      window.OP_TOAST(label, 'error');
      if (!MODEL) resetDrop();
    }, ms);
  }

  function recreateWorker() { try { if (worker) worker.terminate(); } catch (e) { /* ignore */ } attachWorker(); }

  function attachWorker() {
    try {
      const wsrc = ($config && $config.dataset.worker) || '/static/js/anonymizer.worker.js';
      worker = new Worker(wsrc);
    } catch (e) {
      window.OP_TOAST && window.OP_TOAST(t('Could not start the masking engine'), 'error');
      return;
    }
    worker.onmessage = onWorkerMessage;
    worker.onerror = () => {
      clearWatch(); recreateWorker(); setRunIdle();
      window.OP_TOAST(t('Masking engine crashed — the file may be too large'), 'error');
      if (!MODEL) resetDrop();
    };
  }

  function onWorkerMessage(e) {
    const m = e.data;
    clearWatch();
    if (m.type === 'parsed') {
      MODEL = m.model;
      renderAudit(m.audit || []);
      if (!MODEL.sheets.length || !MODEL.sheets.some(s => s.tables.length)) {
        window.OP_TOAST(t('No tables found in the file'), 'warning'); resetDrop(); return;
      }
      $config.style.display = 'block'; $drop.style.display = 'none';
      renderConfig();
    } else if (m.type === 'masked') {
      setRunIdle();
      try {
        const blob = new Blob([m.buf], { type: MIME[OUT_EXT] || 'application/octet-stream' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url; a.download = BASE + '__masked.' + OUT_EXT;
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 2000);
        window.OP_TOAST(t('Masked {n} cells — file downloaded', {n: m.count}), 'success');
        renderResult(m.sample || '', m.left || []);
      } catch (err) { window.OP_TOAST(t('Could not save the file'), 'error'); }
    } else if (m.type === 'error') {
      setRunIdle();
      window.OP_TOAST(m.message ? t('Failed: {msg}', {msg: m.message}) : t('Could not process the file'), 'error');
      if (!MODEL) resetDrop();
    }
  }

  attachWorker();

  // ── Rendering ───────────────────────────────────────────────────────
  function esc(s) { return String(s).replace(/[&<>"]/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch])); }

  function renderConfig() {
    $sheets.innerHTML = '';
    MODEL.sheets.forEach((sheet, si) => {
      if (!sheet.tables.length) return;
      const sh = document.createElement('div');
      sh.innerHTML = `<div class="sheet-block-title">${t('Sheet: {name} · {n} table(s)', {name: esc(sheet.name), n: sheet.tables.length})}</div>`;
      sheet.tables.forEach((tbl, ti) => sh.appendChild(renderTable(si, ti, tbl)));
      $sheets.appendChild(sh);
    });
    bind();
    updateSummary();
  }

  function renderTable(si, ti, tbl) {
    const block = document.createElement('div');
    block.className = 'sheet-block';
    const dataRows = tbl.range.e.r - tbl.headerRow;
    // header-row selector from preview
    const opts = tbl.preview.map(p => {
      const label = Object.keys(p.vals).slice(0, 4).map(c => p.vals[c]).filter(Boolean).join(' | ').slice(0, 50) || t('(empty)');
      return `<option value="${p.r}" ${p.r === tbl.headerRow ? 'selected' : ''}>${t('row {n}: {label}', {n: p.r + 1, label: esc(label)})}</option>`;
    }).join('');
    const colRows = tbl.cols.map((col, ci) => {
      const pii = PII.includes(col.category);
      const sel = CATEGORIES.map(c => `<option value="${c}" ${c === col.category ? 'selected' : ''}>${CAT_LABEL[c]}</option>`).join('');
      return `<tr>
        <td><input type="checkbox" class="anon-mask" data-s="${si}" data-t="${ti}" data-ci="${ci}" ${col.mask ? 'checked' : ''}></td>
        <td class="col-name">${esc(col.header)} ${pii ? `<span class="pii-flag">${t('PII')}</span>` : ''}</td>
        <td><span class="type-pill">${col.type}</span></td>
        <td class="col-sample" title="${esc(col.sample)}">${esc(col.sample) || '—'}</td>
        <td><select class="select sm anon-cat" data-s="${si}" data-t="${ti}" data-ci="${ci}">${sel}</select></td>
      </tr>`;
    }).join('');
    block.innerHTML = `
      <div class="tbl-head">
        <span class="mono" style="font-size:11px;color:var(--ink-dim)">${t('Table {n} · {rows} data rows · {cols} cols', {n: ti + 1, rows: Math.max(0, dataRows), cols: tbl.cols.length})}</span>
        <label class="hdr-pick">${t('header row:')}
          <select class="select sm anon-header" data-s="${si}" data-t="${ti}">${opts}</select>
        </label>
      </div>
      <table class="anon-cols"><thead><tr>
        <th style="width:54px">${t('Mask')}</th><th>${t('Column')}</th><th style="width:80px">${t('Type')}</th>
        <th>${t('Sample')}</th><th style="width:140px">${t('Replace with')}</th>
      </tr></thead><tbody>${colRows}</tbody></table>`;
    return block;
  }


  /* Workbook audit findings: what travels with the file besides the visible
     tables. We remove nothing — deleting a stray sheet is for the person who
     knows why it is there; our job is to say so before the file is sent. */
  const AUDIT_TEXT = {
    hidden_sheet:      () => t('Hidden sheets — they travel with the file and are one click from being seen'),
    very_hidden_sheet: () => t('Sheets hidden from the sheet menu — visible only in the VBA editor'),
    defined_names:     (f) => t('Named ranges ({n}) — often a lookup table or a link to another workbook', {n: f.count || (f.items || []).length}),
    external_refs:     (f) => t('Links to other files ({n}) — the path itself may reveal a project or a person', {n: f.count || (f.items || []).length}),
    formulas:          (f) => t('Formulas ({n}) — a formula keeps the source even where the value looks harmless', {n: f.count}),
    comments:          (f) => t('Cell notes ({n}) — masking does not touch them', {n: f.count}),
    hidden_lines:      (f) => t('Hidden rows or columns ({n})', {n: f.count}),
    doc_props:         () => t('The file remembers who made it'),
  };

  function renderAudit(findings) {
    const box = document.getElementById('anon-audit');
    if (!box) return;
    if (!findings.length) { box.hidden = true; box.textContent = ''; return; }
    const head = document.createElement('div');
    head.className = 'anon-audit-head';
    head.textContent = t('Before you send this file anywhere, check what else is inside');
    const ul = document.createElement('ul');
    ul.className = 'anon-audit-list';
    findings.forEach(f => {
      const li = document.createElement('li');
      const line = document.createElement('div');
      line.textContent = (AUDIT_TEXT[f.kind] || (() => f.kind))(f);
      li.appendChild(line);
      if (f.items && f.items.length) {
        const sub = document.createElement('div');
        sub.className = 'anon-audit-items';
        sub.textContent = f.items.join(' · ') + ((f.count && f.count > f.items.length) ? ' …' : '');
        li.appendChild(sub);
      }
      ul.appendChild(li);
    });
    const tail = document.createElement('div');
    tail.className = 'anon-audit-items';
    tail.textContent = t('Masking replaces cell values. It does not remove any of the above — delete what you do not need in Excel and run the file through again.');
    box.textContent = '';
    box.appendChild(head); box.appendChild(ul); box.appendChild(tail);
    box.hidden = false;
  }


  /* After masking the person needs two things the file does not give them:
     a ready sample for the "data" block of a prompt to the model (header and
     three rows — the whole prompt, not twenty thousand rows) and an honest
     answer to "what is left". */
  function renderResult(sample, left) {
    const box = document.getElementById('anon-result');
    if (!box) return;
    box.textContent = '';
    const head = document.createElement('div');
    head.className = 'anon-audit-head';
    head.textContent = t('Masked file saved. Two things before you use it');
    box.appendChild(head);

    if (sample) {
      const p1 = document.createElement('div');
      p1.style.fontSize = '13px';
      p1.textContent = t('A ready sample for the prompt: header plus three masked rows — paste this, not the file.');
      const pre = document.createElement('pre');
      pre.className = 'anon-sample';
      pre.textContent = sample;
      const btn = document.createElement('button');
      btn.type = 'button'; btn.className = 'btn sm';
      btn.textContent = t('Copy sample');
      btn.addEventListener('click', () => {
        navigator.clipboard.writeText(sample)
          .then(() => window.OP_TOAST(t('Sample copied'), 'success'))
          .catch(() => window.OP_TOAST(t('Could not copy — select the text manually'), 'warning'));
      });
      box.appendChild(p1); box.appendChild(pre); box.appendChild(btn);
    }

    const p2 = document.createElement('div');
    p2.style.cssText = 'font-size:13px;margin-top:14px';
    if (!left.length) {
      p2.textContent = t('Self-check: nothing that looks like personal data is left in the columns you kept.');
      box.appendChild(p2);
    } else {
      p2.textContent = t('Self-check — what stayed unmasked:');
      const ul = document.createElement('ul');
      ul.className = 'anon-audit-list';
      left.forEach(f => {
        const li = document.createElement('li');
        li.textContent =
          f.kind === 'pii_left'
            ? t('«{col}» on sheet «{sheet}» still looks like personal data ({cat})', {col: f.column, sheet: f.sheet, cat: f.category})
          : f.kind === 'unique_left'
            ? t('«{col}» on sheet «{sheet}»: almost every value is unique — a row can be recognised by it', {col: f.column, sheet: f.sheet})
            : t('«{col}» on sheet «{sheet}» kept its real values (e.g. «{sample}») — tick it if those are client or site names', {col: f.column, sheet: f.sheet, sample: f.sample || ''});
        ul.appendChild(li);
      });
      box.appendChild(p2); box.appendChild(ul);
    }
    box.hidden = false;
    box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  function bind() {
    $sheets.querySelectorAll('.anon-mask').forEach(cb => cb.addEventListener('change', () => {
      MODEL.sheets[+cb.dataset.s].tables[+cb.dataset.t].cols[+cb.dataset.ci].mask = cb.checked;
      updateSummary();
    }));
    $sheets.querySelectorAll('.anon-cat').forEach(sel => sel.addEventListener('change', () => {
      const col = MODEL.sheets[+sel.dataset.s].tables[+sel.dataset.t].cols[+sel.dataset.ci];
      col.category = sel.value;
      if (sel.value !== 'keep') {
        const cb = $sheets.querySelector(`.anon-mask[data-s="${sel.dataset.s}"][data-t="${sel.dataset.t}"][data-ci="${sel.dataset.ci}"]`);
        if (cb && !cb.checked) { cb.checked = true; col.mask = true; }
      }
      updateSummary();
    }));
    $sheets.querySelectorAll('.anon-header').forEach(sel => sel.addEventListener('change', () => {
      const tbl = MODEL.sheets[+sel.dataset.s].tables[+sel.dataset.t];
      const newRow = +sel.value;
      tbl.headerRow = newRow;
      const pv = tbl.preview.find(p => p.r === newRow);
      if (pv) tbl.cols.forEach(col => { const tv = pv.vals[col.c]; if (tv) col.header = tv; });
      renderConfig(); // re-label headers; mask/category choices persist (stored on model)
    }));
  }

  function updateSummary() {
    let masked = 0, total = 0;
    MODEL.sheets.forEach(s => s.tables.forEach(tv => tv.cols.forEach(c => { total++; if (c.mask && c.category !== 'keep') masked++; })));
    $summary.textContent = t('Will mask {masked} of {total} columns', {masked, total});
  }

  // ── Load / run ──────────────────────────────────────────────────────
  async function handleFile(file) {
    if (!file || busy) return;
    if (file.size > MAX_BYTES) { window.OP_TOAST(t('File is larger than 10 MB'), 'error'); return; }
    const parts = file.name.split('.');
    const ext = (parts.pop() || 'xlsx').toLowerCase();
    OUT_EXT = ext === 'xlsm' ? 'xlsx' : ext;
    BASE = parts.join('.') || 'data';
    $drop.querySelector('.title').textContent = t('Reading…');
    try {
      const buf = await file.arrayBuffer();
      $meta.textContent = file.name + ' · ' + (file.size / 1024).toFixed(0) + ' KB';
      if (!worker) attachWorker();
      startWatch(45000, t('File is too large or could not be read — cancelled'));
      worker.postMessage({ type: 'parse', buf, isCsv: ext === 'csv' }, [buf]);
    } catch (err) { window.OP_TOAST(t('Could not read this file'), 'error'); resetDrop(); }
  }

  function resetDrop() {
    $drop.querySelector('.title').textContent = t('Drop a spreadsheet here, or click to browse');
    $drop.style.display = 'flex'; $config.style.display = 'none';
    MODEL = null; $input.value = '';
  }

  function run() {
    if (!MODEL || busy) return;
    busy = true; $run.disabled = true; $run.textContent = t('Working…');
    if (!worker) attachWorker();
    startWatch(60000, t('Masking is taking too long — cancelled'));
    worker.postMessage({ type: 'mask', model: MODEL, bookType: OUT_EXT });
  }

  // ── Wiring ──────────────────────────────────────────────────────────
  $input.addEventListener('change', e => handleFile(e.target.files[0]));
  $drop.addEventListener('dragover', e => { e.preventDefault(); $drop.classList.add('dragging'); });
  $drop.addEventListener('dragleave', () => $drop.classList.remove('dragging'));
  $drop.addEventListener('drop', e => { e.preventDefault(); $drop.classList.remove('dragging'); if (e.dataTransfer.files[0]) handleFile(e.dataTransfer.files[0]); });
  $run.addEventListener('click', run);
  document.getElementById('btn-anon-reset').addEventListener('click', resetDrop);
  document.getElementById('btn-mask-all').addEventListener('click', () => { MODEL.sheets.forEach(s => s.tables.forEach(tv => tv.cols.forEach(c => { if (c.category !== 'keep') c.mask = true; }))); renderConfig(); });
  document.getElementById('btn-keep-all').addEventListener('click', () => { MODEL.sheets.forEach(s => s.tables.forEach(tv => tv.cols.forEach(c => { c.mask = false; }))); renderConfig(); });
})();
