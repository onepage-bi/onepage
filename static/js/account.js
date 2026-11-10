/* The "My account" page: keys for the desktop application.
 *
 * The whole exchange is four requests to /api/me/device-tokens. The page is
 * deliberately simple: issuing a key is a rare, deliberate act, not a working tool.
 *
 * The main decision here is what to do with the issued string. It cannot be
 * recovered from the database by construction (the server holds only a sha256), so
 * we show it once in a prominent frame, do not silently stash it in the clipboard,
 * and say plainly that there will be no second time. We do NOT copy it automatically:
 * the person must see exactly what they are taking, and a silent clipboard write on
 * a corporate machine is also a way to lose whatever was there.
 */
(function () {
  'use strict';
  var list = document.getElementById('acc-keys');
  if (!list) return;

  var t = (window.OP_I18N && window.OP_I18N.t) ? window.OP_I18N.t : function (s) { return s; };
  var fmtDate = (window.OP_I18N && window.OP_I18N.fmtDate)
    ? window.OP_I18N.fmtDate
    : function (d) { return d.toISOString().slice(0, 10); };

  var labelInput = document.getElementById('acc-label');
  var issueBtn = document.getElementById('acc-issue');
  var statusEl = document.getElementById('acc-status');
  var issuedEl = document.getElementById('acc-issued');

  function say(text, isError) {
    statusEl.textContent = text || '';
    statusEl.className = 'acc-status' + (isError ? ' err' : '');
  }

  function when(iso) {
    if (!iso) return '—';
    var d = new Date(iso);
    return isNaN(d.getTime()) ? '—' : fmtDate(d);
  }

  function render(data) {
    var items = (data && data.items) || [];
    list.textContent = '';
    if (!items.length) {
      var empty = document.createElement('div');
      empty.className = 'acc-empty';
      empty.textContent = t('No keys yet. Issue one if you have installed the desktop app.');
      list.appendChild(empty);
      return;
    }
    items.forEach(function (it) {
      var card = document.createElement('div');
      card.className = 'acc-card';
      var row = document.createElement('div');
      row.className = 'acc-row';

      var who = document.createElement('div');
      var name = document.createElement('div');
      name.className = 'acc-key-name';
      // textContent: the label is written by a person and comes back from the server.
      name.textContent = it.label || t('Unlabelled key');
      var meta = document.createElement('div');
      meta.className = 'acc-key-meta';
      // Two WHOLE sentences instead of assembling from pieces: a catalogue key must
      // be a complete phrase, otherwise the translation gets fragments and there is
      // nothing to rebuild the word order of another language from.
      meta.textContent = it.last_used_at
        ? t('issued {p1} · valid until {p2} · last used {p3}')
            .replace('{p1}', when(it.created_at))
            .replace('{p2}', when(it.expires_at))
            .replace('{p3}', when(it.last_used_at))
        : t('issued {p1} · valid until {p2} · not used yet')
            .replace('{p1}', when(it.created_at))
            .replace('{p2}', when(it.expires_at));
      who.appendChild(name);
      who.appendChild(meta);

      var del = document.createElement('button');
      del.className = 'btn xs ghost';
      del.type = 'button';
      del.textContent = t('Revoke');
      del.addEventListener('click', function () { revoke(it.id, it.label); });

      row.appendChild(who);
      row.appendChild(del);
      card.appendChild(row);
      list.appendChild(card);
    });
  }

  function load() {
    return fetch('/api/me/device-tokens')
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r); })
      .then(render)
      .catch(function () {
        list.textContent = '';
        var e = document.createElement('div');
        e.className = 'acc-empty';
        e.textContent = t('Could not load the list of keys.');
        list.appendChild(e);
      });
  }

  function showIssued(token, expires) {
    issuedEl.textContent = '';
    issuedEl.hidden = false;

    var head = document.createElement('div');
    head.style.fontWeight = '650';
    head.style.fontSize = '14px';
    head.textContent = t('Key issued. Copy it now — it will not be shown again.');

    var code = document.createElement('code');
    code.textContent = token;

    var btn = document.createElement('button');
    btn.className = 'btn xs';
    btn.type = 'button';
    btn.textContent = t('Copy');
    btn.addEventListener('click', function () {
      // The clipboard may be unavailable (no permission, not a secure
      // context). Then we say so honestly: the string is on screen and can be
      // selected by hand — it is marked user-select: all.
      var done = function () { btn.textContent = t('Copied'); };
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(token).then(done, function () {
            btn.textContent = t('That did not work — select the key and copy it by hand');
          });
          return;
        }
      } catch (e) { /* below */ }
      btn.textContent = t('That did not work — select the key and copy it by hand');
    });

    var note = document.createElement('div');
    note.className = 'acc-note';
    note.textContent = t('Paste it into the desktop app. Valid until {p1}.')
      .replace('{p1}', when(expires));

    var wrap = document.createElement('div');
    wrap.className = 'acc-issued';
    wrap.appendChild(head);
    wrap.appendChild(code);
    wrap.appendChild(btn);
    wrap.appendChild(note);
    issuedEl.appendChild(wrap);
  }

  issueBtn.addEventListener('click', function () {
    issueBtn.disabled = true;
    say(t('Issuing a key…'));
    fetch('/api/me/device-tokens', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label: labelInput.value || '' }),
    }).then(function (r) {
      return r.json().then(function (body) { return { ok: r.ok, body: body }; });
    }).then(function (res) {
      if (!res.ok) {
        // The server replies with readable text (e.g. about the cap on the number
        // of keys) — show that, not something we made up.
        say(res.body && res.body.detail ? res.body.detail : t('Could not issue the key.'), true);
        return;
      }
      say('');
      labelInput.value = '';
      showIssued(res.body.token, res.body.expires_at);
      return load();
    }).catch(function () {
      say(t('Could not issue the key.'), true);
    }).then(function () {
      issueBtn.disabled = false;
    });
  });

  function revoke(id, label) {
    var ask = label
      ? t('Revoke the key “{p1}”? The app on that machine will stop updating reports.').replace('{p1}', label)
      : t('Revoke the unlabelled key? The app on that machine will stop updating reports.');
    if (!window.confirm(ask)) return;
    say(t('Revoking…'));
    fetch('/api/me/device-tokens/' + encodeURIComponent(id), { method: 'DELETE' })
      .then(function (r) {
        if (!r.ok) { say(t('Could not revoke the key.'), true); return; }
        say('');
        return load();
      })
      .catch(function () { say(t('Could not revoke the key.'), true); });
  }

  load();
})();
