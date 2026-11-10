/* OnePage — /admin/quests: season/holiday overlay config + quiz bank import.
 * Every server-sourced string is written with textContent — never innerHTML.
 *
 * Same idiom as admin-employees.js / admin_generators.html's inline script:
 * fetch + Content-Type: application/json (OriginGuard), window.OP_TOAST for
 * outcomes, a local error box for the detail. `parseHolidays` and
 * `validateBankText` are pure — exported for tests/admin_quests_dom.js
 * before anything below touches `document`.
 */
(function () {
  'use strict';

  const ISO_DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

  // Parse the holidays textarea: one ISO date per line, blank lines
  // ignored, duplicates dropped, result sorted. Lines that don't match
  // YYYY-MM-DD (or aren't a real calendar date) come back in `errors`
  // verbatim so the caller can show the offending line.
  function parseHolidays(text) {
    const lines = String(text || '').split('\n').map(l => l.trim()).filter(l => l.length > 0);
    const dates = new Set();
    const errors = [];
    for (const line of lines) {
      if (!ISO_DATE_RE.test(line) || !isRealDate(line)) {
        errors.push(line);
        continue;
      }
      dates.add(line);
    }
    return { dates: Array.from(dates).sort(), errors };
  }

  function isRealDate(iso) {
    const [y, m, d] = iso.split('-').map(Number);
    const dt = new Date(Date.UTC(y, m - 1, d));
    return dt.getUTCFullYear() === y && dt.getUTCMonth() === m - 1 && dt.getUTCDate() === d;
  }

  // Validate pasted/uploaded quiz-bank JSON client-side, before it ever
  // reaches the server's dry-run: must parse and be an array. Per-item
  // shape validation stays server-side (quests_quiz.import_bank owns it
  // and returns a structured error report) — this is just the "is this
  // even JSON, is it a list" gate.
  function validateBankText(text) {
    const raw = String(text || '').trim();
    if (!raw) return { ok: false, bank: null, error: t('Empty') };
    let parsed;
    try {
      parsed = JSON.parse(raw);
    } catch (e) {
      return { ok: false, bank: null, error: t('Invalid JSON: {p1}', {p1: e.message}) };
    }
    if (!Array.isArray(parsed)) {
      return { ok: false, bank: null, error: t('Bank must be a list (JSON array)') };
    }
    return { ok: true, bank: parsed, error: null };
  }

  // Node test hook only (tests/admin_quests_dom.js) — exported
  // unconditionally, before anything below touches `document`/`window`.
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { parseHolidays, validateBankText };
  }

  if (typeof document === 'undefined') return; // node require() path stops here

  function toast(msg, kind) {
    if (typeof window.OP_TOAST === 'function') window.OP_TOAST(msg, kind);
  }

  async function errorDetailFrom(res, fallback) {
    try {
      const j = await res.json();
      if (j && j.detail !== undefined) {
        if (typeof j.detail === 'string') return j.detail;
        return JSON.stringify(j.detail);
      }
      return fallback;
    } catch (e) {
      return fallback;
    }
  }

  async function putConfig(body) {
    return fetch('/api/admin/quests/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  }

  // ── Status: enabled toggle + season fields ──────────────────────────
  (function () {
    const $toggle = document.getElementById('qa-enabled-toggle');
    const $start = document.getElementById('qa-season-start');
    const $length = document.getElementById('qa-season-length');
    const $save = document.getElementById('qa-status-save');
    const $msg = document.getElementById('qa-status-msg');
    if (!$save) return;

    $save.addEventListener('click', async () => {
      $msg.textContent = '';
      const $comp = document.getElementById('qa-companion-toggle');
      // Only when touched: an untouched toggle must not mint an overlay entry
      // for a field the administrator never decided on.
      const compNow = $comp ? $comp.getAttribute('aria-checked') === 'true' : undefined;
      const compWas = $comp ? $comp.getAttribute('data-initial') === 'true' : undefined;
      const body = {
        enabled: $toggle ? $toggle.getAttribute('aria-checked') === 'true' : undefined,
        companion_enabled: ($comp && compNow !== compWas) ? compNow : undefined,
        season_start: $start ? $start.value : undefined,
        season_length: $length ? parseInt($length.value, 10) : undefined,
      };
      if (body.season_length !== undefined && Number.isNaN(body.season_length)) {
        delete body.season_length;
      }
      try {
        const res = await putConfig(body);
        if (res.ok) {
          toast(t('Saved'), 'success');
          location.reload();
          return;
        }
        const detail = await errorDetailFrom(res, t('Save error ({p1})', {p1: res.status}));
        $msg.textContent = detail;
        toast(detail, 'error');
      } catch (e) {
        $msg.textContent = t('Network: {p1}', {p1: e});
        toast(t('Network: {p1}', {p1: e}), 'error');
      }
    });

    document.querySelectorAll('[data-reset]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const name = btn.dataset.reset;
        try {
          const res = await putConfig({ reset: [name] });
          if (res.ok) {
            toast(t('Saved'), 'success');
            location.reload();
            return;
          }
          toast(await errorDetailFrom(res, t('Reset error ({p1})', {p1: res.status})), 'error');
        } catch (e) {
          toast(t('Network: {p1}', {p1: e}), 'error');
        }
      });
    });
  })();

  // ── Holidays ────────────────────────────────────────────────────────
  (function () {
    const $textarea = document.getElementById('qa-holidays');
    const $save = document.getElementById('qa-holidays-save');
    const $status = document.getElementById('qa-holidays-status');
    if (!$save || !$textarea) return;

    $save.addEventListener('click', async () => {
      $status.textContent = '';
      const { dates, errors } = parseHolidays($textarea.value);
      if (errors.length) {
        $status.textContent = t('Invalid rows: {p1}', {p1: errors.join(', ')});
        toast(t('Invalid dates found'), 'error');
        return;
      }
      // Reflect the normalized (deduped, sorted) form back into the box.
      $textarea.value = dates.join('\n');
      try {
        const res = await putConfig({ holidays: dates });
        if (res.ok) {
          $status.textContent = t('Saved · {p1} dates', {p1: dates.length});
          toast(t('Saved'), 'success');
          return;
        }
        const detail = await errorDetailFrom(res, t('Save error ({p1})', {p1: res.status}));
        $status.textContent = detail;
        toast(detail, 'error');
      } catch (e) {
        $status.textContent = t('Network: {p1}', {p1: e});
        toast(t('Network: {p1}', {p1: e}), 'error');
      }
    });

    document.getElementById('qa-reset-holidays')?.addEventListener('click', async () => {
      try {
        const res = await putConfig({ reset: ['holidays'] });
        if (res.ok) {
          toast(t('Saved'), 'success');
          location.reload();
          return;
        }
        toast(await errorDetailFrom(res, t('Reset error ({p1})', {p1: res.status})), 'error');
      } catch (e) {
        toast(t('Network: {p1}', {p1: e}), 'error');
      }
    });
  })();

  // ── Quiz bank ───────────────────────────────────────────────────
  (function () {
    const $textarea = document.getElementById('qa-bank-json');
    const $file = document.getElementById('qa-bank-file');
    const $check = document.getElementById('qa-bank-check');
    const $import = document.getElementById('qa-bank-import');
    const $default = document.getElementById('qa-bank-default');
    const $report = document.getElementById('qa-bank-report');
    if (!$check) return;

    // Import stays disabled until a dry-run on this EXACT text succeeds —
    // resets whenever the text changes underneath it.
    let dryRunOkFor = null;

    function setDryRunOk(text) {
      dryRunOkFor = text;
      $import.disabled = false;
    }
    function clearDryRunOk() {
      dryRunOkFor = null;
      $import.disabled = true;
    }

    $textarea.addEventListener('input', clearDryRunOk);

    $file.addEventListener('change', () => {
      const f = $file.files[0];
      if (!f) return;
      const reader = new FileReader();
      reader.onload = () => {
        $textarea.value = String(reader.result || '');
        clearDryRunOk();
      };
      reader.readAsText(f);
    });

    function renderReport(report) {
      $report.textContent = '';
      if (!report) return;
      const lines = [];
      lines.push((report.ok ? 'OK' : t('Errors')) + t(' · questions: ') + (report.count || 0) +
        t(' · days: ') + (report.days || '—') + (report.dry_run ? t(' · (check, no save)') : ''));
      (report.errors || []).forEach((e) => {
        const loc = e.id !== undefined ? ('id ' + e.id) : (t('row {p1}', {p1: e.index}));
        lines.push('  ' + loc + (e.field ? ' [' + e.field + ']' : '') + ': ' + (e.reason || ''));
      });
      $report.textContent = lines.join('\n');
    }

    async function runImport(bank, dryRun) {
      const res = await fetch('/api/admin/quests/quiz-import', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ bank, dry_run: dryRun }),
      });
      let body;
      try {
        body = await res.json();
      } catch (e) {
        body = null;
      }
      if (res.status === 422 && body && body.detail !== undefined) {
        body = normalizeDetailReport(body.detail, dryRun);
      }
      return { res, report: body };
    }

    // The server's 422 `detail` is now the FULL report object
    // `{ok,count,days,errors[],dry_run}` (finding 18). Fall back gracefully
    // if an older server or a generic FastAPI validation error ever sends
    // `detail` as a plain list instead.
    function normalizeDetailReport(detail, dryRun) {
      if (Array.isArray(detail)) {
        return { ok: false, count: 0, days: undefined, errors: detail, dry_run: dryRun };
      }
      if (detail && typeof detail === 'object') {
        return {
          ok: !!detail.ok,
          count: detail.count || 0,
          days: detail.days,
          errors: Array.isArray(detail.errors) ? detail.errors : [],
          dry_run: detail.dry_run !== undefined ? detail.dry_run : dryRun,
          i18n_line: detail.i18n_line,
          i18n_coverage: detail.i18n_coverage,
        };
      }
      return { ok: false, count: 0, days: undefined, errors: [{ reason: String(detail) }], dry_run: dryRun };
    }

    $check.addEventListener('click', async () => {
      const { ok, bank, error } = validateBankText($textarea.value);
      if (!ok) {
        $report.textContent = error;
        clearDryRunOk();
        return;
      }
      try {
        const { res, report } = await runImport(bank, true);
        renderReport(report);
        if (res.ok && report && report.ok) {
          setDryRunOk($textarea.value);
          toast(t('Validation passed'), 'success');
        } else {
          clearDryRunOk();
          toast(t('Bank has errors'), 'error');
        }
      } catch (e) {
        $report.textContent = t('Network: {p1}', {p1: e});
        clearDryRunOk();
      }
    });

    $import.addEventListener('click', async () => {
      const { ok, bank, error } = validateBankText($textarea.value);
      if (!ok || dryRunOkFor !== $textarea.value) {
        $report.textContent = error || t('First run validation without text changes');
        clearDryRunOk();
        return;
      }
      try {
        const { res, report } = await runImport(bank, false);
        renderReport(report);
        if (res.ok && report && report.ok) {
          toast(t('Imported'), 'success');
          clearDryRunOk();
          location.reload();
        } else {
          toast(t('Import not completed'), 'error');
        }
      } catch (e) {
        $report.textContent = t('Network: {p1}', {p1: e});
      }
    });

    $default.addEventListener('click', async () => {
      $report.textContent = '';
      try {
        let res = await fetch('/api/admin/quests/quiz-import-default', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ dry_run: true }),
        });
        let report = await res.json().catch(() => null);
        if (res.status === 422 && report && report.detail !== undefined) {
          report = normalizeDetailReport(report.detail, true);
        }
        renderReport(report);
        if (!res.ok || !report || !report.ok) {
          toast(t('Built-in bank failed validation'), 'error');
          return;
        }
        res = await fetch('/api/admin/quests/quiz-import-default', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ dry_run: false }),
        });
        report = await res.json().catch(() => null);
        if (res.status === 422 && report && report.detail !== undefined) {
          report = normalizeDetailReport(report.detail, false);
        }
        renderReport(report);
        if (res.ok && report && report.ok) {
          toast(t('Built-in bank loaded'), 'success');
          location.reload();
        } else {
          toast(t('Import not completed'), 'error');
        }
      } catch (e) {
        $report.textContent = t('Network: {p1}', {p1: e});
        toast(t('Network: {p1}', {p1: e}), 'error');
      }
    });
  })();
})();
