// Generators launcher (/generators, RFC E.5).
//   * Lazy grid previews + client-side title search — verbatim copy of the
//     themes.html inline script (same idioms, moved out because the create
//     dialog below makes this file big enough to earn its own path).
//   * Create-flow dialog: fetch /api/generators for the picker (only rendered
//     for admin/superuser — non-privileged users never see the trigger), then
//     POST /api/dashboards/generated and redirect to the returned url.
(function () {
  const frames = document.querySelectorAll('.preview-frame');
  const scale = 1280;
  function fit() {
    document.querySelectorAll('.preview').forEach(p => {
      const f = p.querySelector('.preview-frame');
      if (f) f.style.transform = `scale(${p.clientWidth / scale})`;
    });
  }
  fit();
  window.addEventListener('resize', fit);
  if ('IntersectionObserver' in window) {
    const io = new IntersectionObserver((entries) => {
      entries.forEach(en => {
        if (!en.isIntersecting) return;
        const f = en.target;
        if (f.dataset.src && !f.src) {
          f.src = f.dataset.src;
          f.addEventListener('load', () => { f.style.opacity = 1; }, { once: true });
        }
        io.unobserve(f);
      });
    }, { rootMargin: '200px' });
    frames.forEach(f => io.observe(f));
  } else {
    frames.forEach(f => { f.src = f.dataset.src; f.style.opacity = 1; });
  }

  const search = document.getElementById('dash-search');
  search?.addEventListener('input', () => {
    const q = search.value.trim().toLowerCase();
    document.querySelectorAll('.dash-card').forEach(c => {
      c.style.display = (!q || (c.dataset.search || '').includes(q)) ? '' : 'none';
    });
  });

  // ---- Create-flow dialog -------------------------------------------------
  const overlay = document.getElementById('gen-create-overlay');
  if (!overlay) return; // not rendered for users without upload_dashboard rights

  const form = document.getElementById('gen-create-form');
  const select = document.getElementById('gen-create-select');
  const titleInput = document.getElementById('gen-create-title');
  const msg = document.getElementById('gen-create-msg');
  const submitBtn = document.getElementById('gen-create-submit');
  let loaded = false;

  function esc(s) {
    return String(s).replace(/[&<>"']/g, c => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
  }

  function setMsg(text, color) {
    msg.textContent = text;
    msg.style.color = color;
  }

  async function loadGenerators() {
    select.innerHTML = `<option value="">${t('Loading…')}</option>`;
    select.disabled = true;
    try {
      const res = await fetch('/api/generators');
      if (!res.ok) throw new Error(t('Server error {status}', {status: res.status}));
      const gens = await res.json();
      loaded = true;
      if (!gens.length) {
        select.innerHTML = `<option value="">${t('No generators registered')}</option>`;
        submitBtn.disabled = true;
        setMsg(t('No generators registered — ask an administrator'), 'var(--ink-dim)');
        return;
      }
      select.disabled = false;
      submitBtn.disabled = false;
      select.innerHTML = gens.map(g =>
        `<option value="${g.id}">${esc(g.name)} v${esc(g.version_number)} · ${esc(g.author || '—')}</option>`
      ).join('');
    } catch (err) {
      select.innerHTML = `<option value="">${t('Failed to load')}</option>`;
      submitBtn.disabled = true;
      setMsg(String(err.message || err), 'var(--danger)');
    }
  }

  function openDialog() {
    overlay.classList.add('visible');
    setMsg('', 'var(--ink-dim)');
    titleInput.value = '';
    titleInput.focus();
    if (!loaded) loadGenerators();
  }
  function closeDialog() {
    overlay.classList.remove('visible');
  }

  document.querySelectorAll('.js-gen-create-trigger').forEach(b => b.addEventListener('click', openDialog));
  document.getElementById('gen-create-cancel')?.addEventListener('click', closeDialog);
  document.getElementById('gen-create-close')?.addEventListener('click', closeDialog);
  overlay.addEventListener('click', (e) => { if (e.target === overlay) closeDialog(); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && overlay.classList.contains('visible')) closeDialog();
  });

  form?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const title = titleInput.value.trim();
    const generatorId = select.value;
    if (!title) { setMsg(t('Title is required'), 'var(--danger)'); return; }
    if (!generatorId) { setMsg(t('Pick a generator'), 'var(--danger)'); return; }

    submitBtn.disabled = true;
    const orig = submitBtn.textContent;
    submitBtn.textContent = t('Creating…');
    setMsg('', 'var(--ink-dim)');
    try {
      const res = await fetch('/api/dashboards/generated', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title, generator_id: Number(generatorId) }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || t('Server error {status}', {status: res.status}));
      }
      const data = await res.json();
      window.location = data.url;
    } catch (err) {
      setMsg(String(err.message || err), 'var(--danger)');
      submitBtn.disabled = false;
      submitBtn.textContent = orig;
    }
  });
})();
