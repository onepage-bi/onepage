/* Viewer shell: top bar, report iframe and the data-feed bridge.
   The bridge is copied from templates/pages/share.html (scripts block) and
   the op_ready branch of static/js/viewer.js, with addresses made local. Do
   not reinvent it: the report is user-uploaded JS, and the protocol must
   match the server's byte for byte. */
(function () {
  'use strict';
  var U = window.MirrorUI;
  var q = new URLSearchParams(location.search);
  var id = q.get('id') || '';
  var space = q.get('space') === 'local' ? 'local' : 'server';
  var n = parseInt(q.get('n') || '', 10);
  // Same alphabet as is_valid_dashboard_id in layout.py: the id goes into the iframe address.
  var ID_OK = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(id) && id.replace(/\./g, '') !== '';
  var stage = document.getElementById('stage');

  function fail(text) { stage.appendChild(U.el('div', 'fail', text)); }

  // Base address with a trailing slash: the browser resolves the report's relative assets from it.
  function base() {
    var b = '/mirror/' + encodeURIComponent(id) + '/';
    if (space === 'local') b += 'local/L' + n + '/';
    return b;
  }

  // ── Header: copy date and update from folders ────────────────────────
  // There is no second update mechanism here: the button calls the same
  // `U.updateNow`/`U.syncNow` as the row in the list, and reads the same
  // fields of `data.json` (`item.update`, `data.local_run`). They must not
  // diverge — the person sees one and the same report from two screens.
  var timer = null, pinned = false;
  var asofEl = document.getElementById('asof');
  var actsEl = document.getElementById('vacts');
  var noteEl = document.getElementById('vnotice');
  var noteMsg = document.getElementById('vnotice-msg');
  var noteActs = document.getElementById('vnotice-acts');

  function itemOf(data) {
    return ((data && data.items) || []).filter(function (x) { return x.dashboard_id === id; })[0] || null;
  }
  function runOf(item, k) {
    return (item.local_runs || []).filter(function (r) { return r.k === k; })[0] || null;
  }
  function localUrl(k) {
    return '/library/view/?id=' + encodeURIComponent(id) + '&space=local&n=' + encodeURIComponent(k);
  }

  // Run progress in the same words as under the row in the list (`runLine`).
  function runningText(run) {
    if (run.total > 0) return U.fmt('local_run_progress', run.done, run.total);
    if (run.stage) return U.fmt('local_run_stage', run.msg || run.stage);
    return U.fmt('local_run_running');
  }
  // Why it cannot be pressed. The run lock is one per program, so someone
  // else's run is as honest an answer as a `precheck` refusal.
  function whyBlocked(item, run, mine) {
    if (mine) return runningText(run);
    if (run && run.running) return U.fmt('local_run_err_busy');
    return U.errText(item.update || {});
  }
  // A road, not a dead end. Folders are chosen on the "Report folders" screen
  // (the "Browse…" button is there too), while the rights and the calculation
  // part arrive through contact with the platform — that is "Update now". The
  // other refusals have no road, and one must not be invented: "no generator"
  // is not cured by any button.
  function road(reason) {
    if (/^folders/.test(reason || '')) {
      var a = U.el('a', 'btn xs', U.str('action_open_folders_ui'));
      a.href = U.FOLDERS_URL; return [a];
    }
    if (reason === 'rights_expired' || reason === 'no_bundle' || reason === 'no_mirror') {
      return [U.button('action_sync_now', 'xs', function () { U.syncNow(refresh); })];
    }
    return [];
  }
  function note(cls, text, extras) {
    noteEl.className = 'notice' + (cls ? ' ' + cls : '');
    noteEl.hidden = false;
    noteMsg.textContent = text;
    noteActs.textContent = '';
    (extras || []).forEach(function (e) { noteActs.appendChild(e); });
  }

  function head(data) {
    var item = itemOf(data);
    if (!item || item.mirrorable === false) return;   // gone from the inventory — leave the header alone
    var localRun = space === 'local' ? runOf(item, n) : null;
    // An honest caption: the copy date, without a "fresh" verdict until we know for sure.
    if (space === 'local') {
      asofEl.className = 'asof';
      asofEl.textContent = U.fmt('report_local_only', U.dateOrDash(localRun && localRun.created_at));
    } else if (item.state === 'outdated') {
      asofEl.className = 'asof outdated';
      asofEl.textContent = U.fmt('report_outdated', U.dateOrDash(item.fetched_at));
    } else {
      asofEl.className = 'asof';
      asofEl.textContent = U.fmt('report_as_of', U.dateOrDash(item.fetched_at));
    }

    var run = data.local_run || null;
    var mine = !!(run && run.running && run.dashboard_id === id);
    var busy = !!(run && run.running && !mine);
    var upd = item.update || {};
    var can = !!upd.can && !mine && !busy;

    actsEl.textContent = '';
    var b = U.button('action_update_from_folders', 'xs' + (can ? '' : ' off'), function () {
      if (can) { pinned = false; note('', U.fmt('local_run_running'), []); U.updateNow(item, refresh); return; }
      // A button that stays silent has already cost the owner a working day:
      // pressing it expands the reason and, if there is a road, that road's button.
      pinned = true; refreshNote(item, run, mine, busy);
    });
    if (!can) { b.setAttribute('aria-disabled', 'true'); b.title = whyBlocked(item, run, mine); }
    actsEl.appendChild(b);

    // Contact with the platform is a PERMANENT button, not a "road" for three
    // refusals. A person who opened yesterday's copy must be able to fetch
    // fresh data from here without going back to the list: that is why they came.
    var sync = data.sync || {};
    var syncOff = sync.enabled === false;
    var syncRun = sync.status === 'running';
    var sb = U.button('action_sync_now', 'xs' + (syncOff || syncRun ? ' off' : ''), function () {
      if (!syncOff && !syncRun) { note('', U.fmt('sync_running'), []); U.syncNow(refresh); return; }
      // Same discipline as the button on the left: we never refuse silently.
      pinned = true;
      if (syncRun) { note('', U.fmt('sync_running'), []); return; }
      if (sync.has_key === false) {
        var a = U.el('a', 'btn xs', U.str('action_add_key'));
        a.href = '/library/';
        note('warn', U.fmt('sync_no_key'), [a]);
        return;
      }
      note('warn', U.fmt('sync_schedule_off'), []);
    });
    if (syncOff || syncRun) {
      sb.setAttribute('aria-disabled', 'true');
      sb.title = syncRun ? U.fmt('sync_running')
        : (sync.has_key === false ? U.fmt('sync_no_key') : U.fmt('sync_schedule_off'));
    }
    actsEl.appendChild(sb);

    refreshNote(item, run, mine, busy);
    clearTimeout(timer);
    if (mine || (run && run.running)) timer = setTimeout(refresh, 1500);
  }

  function refreshNote(item, run, mine, busy) {
    var upd = item.update || {};
    if (mine) { note('', runningText(run), []); return; }
    var done = run && !run.running && run.dashboard_id === id;
    if (done && run.ok === false) {
      note('bad', U.fmt('local_run_failed', U.errText(run)), road(run.reason));
      return;
    }
    if (done && run.ok === true) {
      // The frame on screen stays the old one: the fresh numbers sit in a SEPARATE
      // local version, and we will not silently replace the open report with it.
      var extras = [];
      var fresh = (item.local_runs || []).filter(function (r) { return !r.published; })
        .sort(function (a, c) { return (c.created_at || 0) - (a.created_at || 0); })[0];
      if (fresh && !(space === 'local' && fresh.k === n)) {
        var a = U.el('a', 'btn xs', U.fmt('action_open_local', U.dateOrDash(fresh.created_at)));
        a.href = localUrl(fresh.k); extras.push(a);
      }
      note('', U.fmt('local_run_done'), extras);
      return;
    }
    // The only refusal we mention without a click: it is about time, not
    // about the person, and passes by itself after contact with the platform (as in the list).
    if (!upd.can && upd.reason === 'rights_expired') {
      note('warn', U.errText(upd), road(upd.reason)); return;
    }
    if (pinned && !(upd.can && !busy)) {
      note('warn', whyBlocked(item, run, mine), road(busy ? '' : upd.reason)); return;
    }
    noteEl.hidden = true;
  }

  function refresh() {
    return U.loadData().then(head, function () {
      // The agent did not answer — this has no text of its own; leave the header
      // as is and retry less often: the report on screen does not suffer.
      clearTimeout(timer); timer = setTimeout(refresh, 5000);
    });
  }

  Promise.all([U.loadStrings(), U.loadData()]).then(function (r) {
    var data = r[1] || {};
    var item = (data.items || []).filter(function (x) { return x.dashboard_id === id; })[0];
    // We must look at `state`: the viewer address is reached from a bookmark,
    // and for a report with no copy on disk the iframe showed the delivery's
    // JSON error ("no_document") in the frame instead of an honest "no copy
    // yet". The library draws no buttons for such a report — but a bookmark
    // does not ask the buttons.
    // `not_downloaded` closes the way only to the SERVER copy: a local run
    // lives in its own directory and opens even when the platform document
    // is not on disk (for example, it was not finished downloading).
    if (!ID_OK || !item || item.mirrorable === false
        || (space !== 'local' && item.state === 'not_downloaded')
        || (space === 'local' && !(n >= 1))) {
      fail(U.fmt('report_not_downloaded'));
      return;
    }
    // Local run: its slot inventory is its OWN. Previously the viewer fed the
    // report the server version's slots in this space too — that is, old
    // data into a fresh document, silently. No such run in the inventory —
    // nothing to open, and that is more honest than an empty report.
    var localRun = null;
    if (space === 'local') {
      localRun = (item.local_runs || []).filter(function (r) { return r.k === n; })[0];
      if (!localRun) { fail(U.fmt('report_not_downloaded')); return; }
    }
    var title = item.title || '';
    document.title = title || 'OnePage';
    document.getElementById('vtitle').textContent = title;
    if (space === 'local' && localRun.bridge === 'none') {
      // There was no bridge to take — the report will open empty. We must say
      // so BEFORE the person sees it: otherwise they will decide the run
      // failed and recompute with the same outcome.
      stage.appendChild(U.el('div', 'fail', U.fmt('local_only_no_bridge')));
    }
    // The header (copy date + actions) is redrawn on every poll of
    // data.json, the report frame is NOT: reloading the iframe would throw away
    // both the fed data and everything the person clicked in the report.
    head(data);
    var slots = localRun ? localRun.slots : item.slots;
    slots = Array.isArray(slots) ? slots : [];

    var frame = document.createElement('iframe');
    // EXACTLY as in share.html: without allow-same-origin. The report gets an opaque
    // origin and cannot reach the agent's handlers or the device key.
    frame.setAttribute('sandbox', 'allow-scripts allow-forms allow-downloads allow-popups allow-modals');
    frame.title = title;
    frame.src = base();
    stage.appendChild(frame);

    window.addEventListener('message', function (e) {
      if (e.source !== frame.contentWindow) return;   // other windows are no concern of ours
      var msg = e.data;
      if (!msg || typeof msg !== 'object') return;
      // The report cannot write to the mirror: ignore silently, as share.html does.
      // (op_save_ls / op_save_file / op_save_snapshot deliberately have no handler.)
      if (msg.type !== 'op_ready') return;
      // One at a time, as in the original: slots can be tens of megabytes, and all at once
      // would double the memory peak (buffer + copy in postMessage).
      (async function () {
        for (var i = 0; i < slots.length; i++) {
          var slot = slots[i];
          try {
            // slot.slot is a POSITION in querySelectorAll('input[type=file]') inside the report;
            // it goes into op_file explicitly and as is, otherwise the data silently never arrives.
            var res = await fetch(base() + 'files/' + slot.slot + (slot.v ? '?v=' + encodeURIComponent(slot.v) : ''));
            if (!res.ok) continue;
            var buf = await res.arrayBuffer();
            frame.contentWindow.postMessage(
              { type: 'op_file', slot: slot.slot, name: slot.name, data: buf }, '*', [buf]);
          } catch (err) { console.error('slot load failed', slot.slot, err); }
        }
      })();
    });
  }).catch(function () { fail(U.fmt('report_not_downloaded')); });
})();
