/* OnePage — upload.js (HTML file detection + slot configuration) */

(function() {
  // Heavy dashboards degrade browser load time; grid preview becomes placeholder at OP_PREVIEW_MAX_BYTES
  const WEIGHT_WARN_BYTES = 2 * 1024 * 1024;

  const fileInput = document.getElementById('html-file-input');
  const dropZone  = document.getElementById('drop-zone');
  const readout   = document.getElementById('upload-readout');
  const slotsWrap = document.getElementById('slot-inputs');
  const slotsBody = document.getElementById('slot-inputs-body');
  const steps     = document.querySelectorAll('#upload-steps .step');

  function setSteps(activeIdx) {
    steps.forEach((s, i) => {
      s.classList.toggle('done',   i < activeIdx);
      s.classList.toggle('active', i === activeIdx);
    });
  }

  // file.name is whoever named the file on disk, not this user, and the page
  // is admin/superuser-only — a crafted filename would run in an elevated
  // session the moment the file is picked, before anything is published.
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

  function setReadout(state) {
    if (!readout) return;
    readout.innerHTML = state;
  }

  // ── Drag/drop ───────────────────────────────────────────────────────
  if (dropZone) {
    dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('dragover'); });
    dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));
    dropZone.addEventListener('drop', e => {
      e.preventDefault();
      dropZone.classList.remove('dragover');
      if (e.dataTransfer.files.length) {
        fileInput.files = e.dataTransfer.files;
        fileInput.dispatchEvent(new Event('change'));
      }
    });
  }

  // ── Type detection (client-side, mirrors detector.py) ──────────────
  async function detect(file) {
    const text = await file.text();
    const hasFileInput = /<input[^>]*type=["']?file["']?/i.test(text);
    const hasSnapshot  = /<script[^>]*id=["']bootstrap-snapshot["']/i.test(text);
    let type = 'static';
    if (hasFileInput) type = 'interactive';
    else if (hasSnapshot) type = 'snapshot';
    // count slots
    const matches = text.match(/<input[^>]*type=["']?file["']?[^>]*>/gi) || [];
    return { type, slots: matches.length, size: file.size, hasSnapshot };
  }

  fileInput?.addEventListener('change', async () => {
    const file = fileInput.files[0];
    if (!file) return;

    setSteps(1);
    const sizeKb = (file.size/1024).toFixed(1);
    const sizeMb = (file.size/(1024*1024)).toFixed(1);
    const warningLine = file.size > WEIGHT_WARN_BYTES
      ? t('<div class="ro-line"><span class="k">⚠</span><span class="v warning">Heavy dashboard ({p1} MB): the page will load slowly for viewers and preview in grid will show placeholder. Move data to slot file — see /docs</span></div>', {p1: sizeMb})
      : '';
    setReadout(`
      <div class="ro-head"><svg class="ico ico-12"><use href="#ic-scan"/></svg> ${t('Detection in progress…')}</div>
      <div class="ro-line"><span class="k">file</span><span class="v">${esc(file.name)}</span></div>
      <div class="ro-line"><span class="k">size</span><span class="v">${sizeKb} KB</span></div>
      ${warningLine}
      <div class="ro-line"><span class="k">parsing</span><span class="v accent">…</span></div>
    `);

    let info;
    try { info = await detect(file); }
    catch (e) {
      setReadout(`<div class="ro-head" style="color:var(--danger)">${t('Detection failed: {msg}', {msg: e.message})}</div>`);
      return;
    }

    setReadout(`
      <div class="ro-head"><svg class="ico ico-12"><use href="#ic-check"/></svg> ${t('Detection complete')}</div>
      <div class="ro-line"><span class="k">file</span><span class="v">${esc(file.name)}</span></div>
      <div class="ro-line"><span class="k">size</span><span class="v">${sizeKb} KB</span></div>
      ${warningLine}
      <div class="ro-line"><span class="k">detected_type</span><span class="v accent">${info.type}</span></div>
      ${info.type === 'interactive' ? `<div class="ro-line"><span class="k">file_inputs</span><span class="v">${t('{n} found', {n: info.slots})}</span></div>` : ''}
      ${info.type === 'snapshot' ? `<div class="ro-line"><span class="k">snapshot_tag</span><span class="v success">${t('#bootstrap-snapshot present')}</span></div>` : ''}
      ${info.type === 'static' ? `<div class="ro-line"><span class="k">data_hooks</span><span class="v">${t('none — will be served as-is')}</span></div>` : ''}
    `);

    setSteps(2);

    // Render slot inputs. Field names must be data_file_N to bind to the
    // POST /api/dashboards endpoint.
    const DATA_ACCEPT = '.xlsx,.xlsm,.xls,.csv,.json,.bin,.gz,.br';
    if (info.type === 'interactive' && info.slots > 0 && slotsWrap && slotsBody) {
      slotsWrap.style.display = '';
      slotsBody.innerHTML = '';
      for (let i = 0; i < info.slots; i++) {
        const wrap = document.createElement('div');
        wrap.className = 'field';
        wrap.innerHTML = `
          <label class="field-label">${t('Slot {i} — initial data file', {i: i})}</label>
          <input type="file" name="data_file_${i}" class="input sm" accept="${DATA_ACCEPT}"/>
        `;
        slotsBody.appendChild(wrap);
      }
      setSteps(3);
    } else if (info.type === 'snapshot' && slotsWrap && slotsBody) {
      // A snapshot page may ship a small inline snapshot and keep its heavy
      // dataset out of the HTML, delivered as a separate slot file (e.g. the
      // SL report's sl_bc_data.bin.gz). Offer one optional data slot.
      slotsWrap.style.display = '';
      slotsBody.innerHTML = `
        <div class="field">
          <label class="field-label">${t('Data file (optional) — slot 0')}</label>
          <input type="file" name="data_file_0" class="input sm" accept="${DATA_ACCEPT}"/>
          <div class="sub" style="font-size:11px;color:var(--ink-dim);margin-top:4px">
            ${t('Large out-of-HTML dataset (binary / JSON). Delivered to the dashboard at view time.')}
          </div>
        </div>
      `;
      setSteps(3);
    } else {
      slotsWrap && (slotsWrap.style.display = 'none');
      setSteps(3);
    }
  });
})();
