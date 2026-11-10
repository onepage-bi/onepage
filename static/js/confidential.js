/* Password form of a "strictly confidential" report.
 *
 * Does exactly one thing: hands the password to the server and, if it matched, reloads
 * the page — the lock is lifted on the server, and the regular viewer draws the report.
 * There is no "client-side unlock" here and there cannot be: the server
 * decides, the page only asks.
 */
(function () {
  'use strict';
  var root = document.getElementById('conf-gate');
  if (!root) return;

  var form = document.getElementById('conf-form');
  var pw = document.getElementById('conf-pw');
  var btn = document.getElementById('conf-go');
  var msg = document.getElementById('conf-msg');
  var dash = root.getAttribute('data-dash') || '';

  function say(text, bad) {
    msg.textContent = text;
    msg.className = 'conf-msg' + (bad ? ' is-bad' : '');
  }

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var value = pw.value || '';
    if (!value) { pw.focus(); return; }
    btn.disabled = true;
    say(t('Validating…'), false);

    fetch('/api/dashboards/' + encodeURIComponent(dash) + '/unlock', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: value })
    }).then(function (r) {
      if (r.ok) {
        // Do not leave the password in the field: the next person at this computer
        // would see it in form autofill after going "back".
        pw.value = '';
        location.reload();
        return null;
      }
      btn.disabled = false;
      pw.select();
      if (r.status === 429) {
        say(t('Too many attempts. Wait a little and try again.'), true);
      } else if (r.status === 403) {
        say(t('Wrong password.'), true);
      } else {
        say(t('Could not open it: error {p1}.').replace('{p1}', String(r.status)), true);
      }
      return null;
    }).catch(function () {
      btn.disabled = false;
      say(t('The server did not answer. Try again.'), true);
    });
  });
})();
