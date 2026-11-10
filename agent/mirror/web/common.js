/* Shared by both pages: texts, substitution, time, data fetching.
   No build step, no dependencies. Any text from the server goes only through
   textContent: report names are written by people, and innerHTML here would
   be XSS on 127.0.0.1 with access to the key via the agent's other handlers. */
(function () {
  'use strict';
  // There may be no texts at all: in a kit without the mirror/ directory both
  // strings.json and this file are missing. So the reason is given to the
  // person by the AGENT — with a bar inserted straight into the HTML
  // (libui._send_page): JS cannot say it, it does not exist on such a machine.
  // What remains here is the second half of that same failure — do not draw a
  // page with empty labels (see fmt).
  var S = {};

  // Exactly as many parameters as the string has: it used to substitute only
  // {p1} and {p2}, while `sync_running_bytes` declares three — and during a
  // download the person read "downloaded {p3}". Take all arguments after the key.
  function fmt(key) {
    var s = S[key], args = arguments;
    // No string — show the KEY name, as the tray icon does (schedule.text).
    // Blank looked like "as designed": a nameless button and an empty status
    // are indistinguishable from a normal page, while the key name tells
    // right away where to look. There is still no invented text here.
    if (typeof s !== 'string') return key == null ? '' : String(key);
    return s.replace(/\{p(\d+)\}/g, function (whole, n) {
      var v = args[Number(n)];
      // An undeclared parameter is left as is: blank in its place would hide
      // the mismatch between string and code, and that must stay visible.
      return v == null ? whole : String(v);
    });
  }
  function pad(n) { return n < 10 ? '0' + n : '' + n; }
  // Time arrives as a number of milliseconds (libui.epoch_ms) or not at all.
  // We check the input ourselves: new Date(null) and new Date('') are VALID
  // dates in 1970, isNaN does not catch them, and the mirror printed
  // "31.12.1969 16:00" instead of blank. A false date is worse than a missing
  // one here: this program is opened to answer "how fresh are my numbers".
  function ms(ts) {
    if (typeof ts !== 'number' || !isFinite(ts) || ts <= 0) return null;
    return ts;
  }
  function dateTime(ts) {
    var t = ms(ts);
    if (t === null) return '';
    var d = new Date(t);
    if (isNaN(d.getTime())) return '';
    return pad(d.getDate()) + '.' + pad(d.getMonth() + 1) + '.' + d.getFullYear() + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }
  // A date we do not know is a dash, as in the icon's status line
  // (schedule.fmt_when/fmt_size). "Copy from —" is honest; "Copy from " reads
  // as truncated text, i.e. as a broken program.
  function dateOrDash(ts) { return dateTime(ts) || '—'; }
  // Relative time only within a day: beyond that "5 d ago" loses meaning (strings_README).
  function when(ts) {
    var t = ms(ts);
    if (t === null) return '';
    var m = Math.floor((Date.now() - t) / 60000);
    if (m < 1) return fmt('time_just_now');
    if (m < 60) return fmt('time_minutes_ago', m);
    if (m < 1440) return fmt('time_hours_ago', Math.floor(m / 60));
    return dateTime(ts);
  }
  function applyStrings(j) {
    S = (!!j && typeof j === 'object' && !Array.isArray(j)) ? j : {};
  }
  function loadStrings() {
    // 404 (no file) and broken JSON arrive here the same way — as "no texts",
    // not as an exception: the page must open without them.
    return fetch('/library/strings.json', { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) { applyStrings(j); }, function () { applyStrings(null); });
  }
  function loadData() {
    return fetch('/library/data.json', { cache: 'no-store' }).then(function (r) {
      if (!r.ok) throw new Error('data ' + r.status);
      return r.json();
    });
  }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  // ── Actions. BOTH pages call them from here ─────────────────────────────
  // They lived in library.js while updating existed only in the list. The
  // button in the viewer header is the same action, not a second one: two
  // copies of this code would drift apart in both refusal codes and texts.
  // So the mechanism is single, and the pages only decide where to draw it.

  function toast(text) {
    var t = el('div', 'toast', text); t.setAttribute('role', 'status');
    document.body.appendChild(t); setTimeout(function () { t.remove(); }, 3500);
  }
  function post(path, body) {
    return fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
  }
  function button(key, cls, fn) {
    var b = el('button', 'btn' + (cls ? ' ' + cls : ''), fmt(key)); b.type = 'button';
    b.addEventListener('click', fn); return b;
  }
  // The agent's answer, parsed into an object. Unreadable JSON and an
  // unanswered request arrive here the same way — as an object with a reason
  // code, because further on they are shown with one and the same line.
  function postJson(path, body) {
    return post(path, body).then(function (r) {
      return r.json().then(function (j) { return j; }, function () { return {}; });
    }, function () { return { reason: 'no_agent' }; });
  }
  // The agent names a refusal with a CODE: the label for a known code is in
  // strings.json; a rare one (folders.ini refusals) has none — then we show
  // the agent's own text rather than invent a second one.
  function errText(j) {
    var key = 'local_run_err_' + (j && j.reason ? j.reason : '');
    var s = fmt(key);
    return s === key ? ((j && j.why) || key) : s;
  }
  // The button must either start the update or say why it did not: the agent
  // used to answer {"started": false} with no reason, the page redrew the same
  // thing, and the person pressed the button a second time. A network failure
  // (the agent did not answer) has no text of its own — `after` shows it.
  function syncNow(after) {
    return postJson('/library/sync').then(function (j) {
      var reason = j && j.started === false ? j.reason : '';
      if (reason === 'already_running') toast(fmt('sync_running'));
      else if (reason === 'no_schedule') toast(fmt('sync_schedule_off'));
      if (after) after();
    });
  }
  function updateNow(it, after) {
    return postJson('/library/local-run', { dashboard_id: it.dashboard_id }).then(function (j) {
      if (!j || j.started !== true) toast(fmt('local_run_failed', errText(j)));
      if (after) after();
    });
  }
  // A road, not a dead end: the refusal "no folders chosen" names the screen
  // where they are chosen (agent/foldersui.py). The address is the same as in
  // the program window (`appwindow.FOLDERS_PATH`).
  var FOLDERS_URL = '/folders-ui/';

  window.MirrorUI = { fmt: fmt, when: when, dateTime: dateTime, dateOrDash: dateOrDash,
    loadStrings: loadStrings, loadData: loadData, el: el,
    toast: toast, post: post, postJson: postJson, button: button,
    errText: errText, syncNow: syncNow, updateNow: updateNow,
    FOLDERS_URL: FOLDERS_URL,
    str: function (k) { return fmt(k); } };
})();
