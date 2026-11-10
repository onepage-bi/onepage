/* Library page. The data comes in one request, GET /library/data.json
   (the shape is described in the task decisions; the agent must serve it).
   The page computes nothing about freshness itself: the report state arrives
   ready-made in the `state` field, so the "fresh/outdated" logic lives in one
   place — in Python. */
(function () {
  'use strict';
  var U = window.MirrorUI, $ = function (i) { return document.getElementById(i); };
  var timer = null;

  // Toasts, POST, buttons and the actions themselves ("update now", "update
  // from folders") live in common.js: the viewer header calls exactly the same
  // actions, and there must be no second copy — refusal codes and texts would drift.
  var toast = U.toast, post = U.post, button = U.button, errText = U.errText;
  function syncNow() { U.syncNow(refresh); }

  // The state lives in TWO places, and that is not a duplicate: the calm line
  // is in the panel above the list (as on the platform), the alarm is a
  // separate bar below it. Previously everything was one box that turned
  // green across the whole width on "just updated": a color that always
  // burns stops being a signal by the very day yellow is needed.
  function renderStatus(sync, hasItems) {
    var msg = $('status-msg'), acts = $('status-actions'), bar = $('bar');
    var alert = $('alert'), alertMsg = $('alert-msg'), alertActs = $('alert-actions');
    acts.textContent = ''; alertActs.textContent = '';
    bar.hidden = true; alert.hidden = true; alert.className = 'notice';
    var st = sync.status || 'idle', text = '', cls = '';
    // No schedule (the icon did not come up or the worker has no synchronizer):
    // neither "Update now" nor "Retry" would start anything, and a button
    // that stays silent is worse than none. We say it plainly and draw no
    // actions — the key state is secondary here, nobody could use it anyway.
    var off = sync.enabled === false, into = acts;
    if (off) {
      text = U.fmt('sync_schedule_off'); cls = 'warn';
    } else if (st === 'running') {
      // Until the manifest arrives, the reports do not exist yet: a bar at
      // this moment would show zero of zero, and the text "Updating copies…"
      // without a number read as a hang. We name the stage, draw no bar.
      var known = sync.total > 0;
      if (!known) text = U.fmt('sync_manifest');
      else if (sync.downloaded) text = U.fmt('sync_running_bytes', sync.done, sync.total, sync.downloaded);
      else text = U.fmt('sync_running_progress', sync.done, sync.total);
      if (known) { bar.hidden = false; $('bar-fill').style.width = Math.min(100, 100 * sync.done / sync.total) + '%'; }
    } else if (st === 'unavailable') {
      text = U.fmt('sync_server_unavailable', U.fmt('time_in_minutes', sync.next_retry_min)); cls = 'warn';
    } else if (st === 'no_key') {
      text = U.fmt('sync_no_key'); cls = 'warn';
    } else if (st === 'key_expired' || st === 'key_rejected') {
      text = U.fmt(st === 'key_expired' ? 'sync_key_expired' : 'sync_key_rejected'); cls = 'bad';
    } else if (st === 'no_disk_space') {
      text = U.fmt('sync_no_disk_space', sync.missing); cls = 'bad';
    } else if (st === 'mirror_full') {
      // Its own ceiling, not the disk: the advice here is DIFFERENT (remove
      // reports or raise the limit), hence a separate branch. "Free some space"
      // on an empty disk would send the person to clean up what needs no cleaning.
      text = U.fmt('sync_mirror_full', sync.missing); cls = 'bad';
    } else if (sync.last_sync_at) {
      text = U.fmt('sync_done', U.when(sync.last_sync_at));
    }
    // The alarm state is a bar: it carries a long explanatory text that would
    // not fit into one line of the panel anyway, and cutting the explanation
    // on precisely a bad day is the worst thing one can do.
    if (cls) {
      alert.hidden = false; alert.classList.add(cls);
      alertMsg.textContent = text; msg.textContent = '';
      into = alertActs;
    } else {
      msg.textContent = hasItems || text ? text : '';
    }
    if (st === 'unavailable' || st === 'no_disk_space' || st === 'mirror_full') {
      into.appendChild(button('action_retry', 'sm', syncNow));
    } else if (st === 'no_key') {
      into.appendChild(button('action_add_key', 'sm primary', openKey));
    } else if (st === 'key_expired' || st === 'key_rejected') {
      into.appendChild(button('action_replace_key', 'sm primary', openKey));
    }
    if (!off && st !== 'running') acts.appendChild(button('action_sync_now', 'sm', syncNow));
  }

  function openUrl(it) { return '/library/view/?id=' + encodeURIComponent(it.dashboard_id); }
  // Address of a local run: the same viewer window, a different version space.
  function localUrl(it, k) { return openUrl(it) + '&space=local&n=' + encodeURIComponent(k); }
  function pendingRuns(it) {
    return (it.local_runs || []).filter(function (r) { return !r.published; });
  }

  // Starting an offline run is shared (common.js): the same button is in the
  // viewer header, and the request must be one and the same.
  function updateNow(it) { U.updateNow(it, refresh); }

  // The report state is split in two, as on the platform: in the column — a
  // badge of one or two words, next to the title — an explanation in small
  // type. One long phrase per table row does not fit, and a green "Up to date"
  // on all twenty rows is not information but noise.
  function stateLine(it) {
    switch (it.state) {
      case 'fresh': return ['success', U.fmt('report_fresh'), ''];
      case 'outdated': return ['warning', U.fmt('report_state_outdated'),
        U.fmt('report_outdated', U.dateOrDash(it.fetched_at))];
      case 'local_only': return ['warning', U.fmt('report_state_local'),
        U.fmt('report_local_only', U.dateOrDash(it.local_at || it.fetched_at))];
      case 'not_downloaded': return ['', U.fmt('report_state_absent'), U.fmt('report_not_downloaded')];
      // The agent may not know the copy date (the field does not arrive at all) — then a dash,
      // not blank and certainly not "31.12.1969".
      default: return ['', U.fmt('report_state_copy'), ''];
    }
  }

  // The offline run's progress bar on the card it is computing. It has no
  // place of its own on the page, deliberately: the person pressed the button
  // on the card, and the answer must arrive at the same place.
  function runLine(it, run) {
    if (!run || run.dashboard_id !== it.dashboard_id) return null;
    if (run.running) {
      if (run.total > 0) return U.el('div', 's', U.fmt('local_run_progress', run.done, run.total));
      if (run.stage) return U.el('div', 's', U.fmt('local_run_stage', run.msg || run.stage));
      return U.el('div', 's', U.fmt('local_run_running'));
    }
    if (run.ok === false) return U.el('div', 's bad', U.fmt('local_run_failed', errText(run)));
    if (run.ok === true) return U.el('div', 's ok', U.fmt('local_run_done'));
    return null;
  }

  // The catch-up question. Precisely a question: the person publishes, not the
  // program (owner's decision — "when the server is back, ASK"). The button
  // leads to the report page, which will take the built files from the agent.
  function catchup(it, sync) {
    var runs = pendingRuns(it);
    if (!runs.length) return null;
    var box = U.el('div', 'notice');
    box.appendChild(U.el('div', 't', U.str('catchup_title')));
    // The "publish?" question is asked when the sync has PASSED AGAIN: while
    // the platform is down there is nowhere to publish anyway, and the question
    // would be mockery. Until then — a calm line saying the numbers are intact.
    var back = sync && sync.status !== 'unavailable' && sync.last_sync_at
      && sync.last_sync_at >= (it.local_at || 0);
    if (!back) {
      box.appendChild(U.el('div', '', U.fmt('catchup_waiting', U.dateOrDash(it.local_at))));
      if (runs.some(function (r) { return r.bridge === 'none'; })) {
        box.appendChild(U.el('div', 'why', U.str('local_only_no_bridge')));
      }
      return box;
    }
    box.appendChild(U.el('div', '', U.fmt('catchup_question', U.dateOrDash(it.local_at))));
    box.appendChild(U.el('div', 'why', U.str('catchup_how')));
    if (it.platform_url) {
      var a = U.el('a', 'btn primary', U.str('action_publish_on_platform'));
      a.href = it.platform_url; a.target = '_blank'; a.rel = 'noopener';
      box.appendChild(a);
    }
    // No bridge — the report will open empty, and we must say so BEFORE
    // the person sees it and decides the run failed.
    if (runs.some(function (r) { return r.bridge === 'none'; })) {
      box.appendChild(U.el('div', 'why', U.str('local_only_no_bridge')));
    }
    return box;
  }

  // A table row, as in "Dashboards": the title is a link, the state a badge,
  // the copy date a monospaced column, the actions on the right. It used to be
  // cards, and twenty outlined boxes with buttons of different widths read as
  // a patchwork quilt: the eye had nothing to hold onto vertically.
  function card(it, run, sync) {
    var tr = U.el('tr'), skipped = it.mirrorable === false;
    var openable = !skipped && it.state !== 'not_downloaded';

    // ── Title and the state explanation next to it ──
    var tdT = U.el('td', 'col-title'), title = U.el('div', 'row-title');
    var name;
    if (openable) { name = U.el('a', 'name', it.title || ''); name.href = openUrl(it); }
    else { name = U.el('span', 'name', it.title || ''); }
    title.appendChild(name);
    var sl = skipped ? null : stateLine(it);
    // An unknown reason code — show nothing: "draft" there would be a lie.
    var why = skipped
      ? (it.skip_reason === 'rls' ? U.fmt('report_skipped_rls')
         : it.skip_reason === 'draft' ? U.fmt('report_skipped_draft') : '')
      : sl[2];
    if (skipped && !why) console.warn('unknown skip_reason', it.skip_reason);
    if (why) title.appendChild(U.el('span', 'name-desc', why));
    tdT.appendChild(title); tr.appendChild(tdT);

    // ── State ──
    var tdS = U.el('td', 'col-state');
    var pill = U.el('span', 'pill' + (skipped ? '' : (sl[0] ? ' ' + sl[0] : '')));
    pill.appendChild(U.el('span', 'dot'));
    pill.appendChild(document.createTextNode(skipped ? U.fmt('report_state_skipped') : sl[1]));
    tdS.appendChild(pill); tr.appendChild(tdS);

    // ── Copy date ──
    tr.appendChild(U.el('td', 'col-updated mono muted',
                        skipped ? '—' : U.dateOrDash(it.local_at || it.fetched_at)));

    // ── Actions. The order is deliberate: secondary on the left, the main one
    // at the far right, so that "Open" stands on one vertical for ALL rows
    // regardless of whether the report has folders. While the buttons lay
    // directly in a flex row with `space-between`, there were sometimes one,
    // sometimes two, and "Open" of neighbouring reports landed at different
    // widths. ──
    var tdA = U.el('td', 'col-actions'), acts = U.el('div', 'row-actions');
    // "Update from folders" — only when the agent said it can: a button that
    // answers with a refusal is worse than a missing one. While a run is in
    // progress there is no button on any row: the run lock is one per program.
    if (!skipped && it.update && it.update.can && !(run && run.running)) {
      acts.appendChild(button('action_update_from_folders', 'xs ghost', function () { updateNow(it); }));
    }
    if (skipped) {
      // A link only if the agent gave the platform address; the path must not be invented.
      if (it.platform_url) {
        var ap = U.el('a', 'btn xs ghost', U.str('action_open_on_platform'));
        ap.href = it.platform_url; ap.target = '_blank'; ap.rel = 'noopener'; acts.appendChild(ap);
      }
    } else if (openable) {
      var o = U.el('a', 'btn xs', U.str('action_open_report')); o.href = openUrl(it); acts.appendChild(o);
    }
    tdA.appendChild(acts); tr.appendChild(tdA);

    // ── Second line: run progress, the catch-up question, local versions. It
    // does not fit into one table row, and moving it out of the table would
    // tear it off the report it belongs to. When there is nothing to say there
    // is no separate row — the list never has empty separators. ──
    var sub = U.el('div', 'sub'), has = false;
    if (!skipped) {
      var rl = runLine(it, run); if (rl) { sub.appendChild(rl); has = true; }
      var cu = catchup(it, sync); if (cu) { sub.appendChild(cu); has = true; }
      // Open the local version with a separate link: the server copy does not
      // go anywhere, and a fresh run must not replace it.
      pendingRuns(it).forEach(function (r) {
        // Its own caption, not a repeat of the state: the line above already says
        // "Changed here …", and a second identical one did not read as a button.
        var lv = U.el('a', 'btn xs ghost', U.fmt('action_open_local', U.dateOrDash(r.created_at)));
        lv.href = localUrl(it, r.k); sub.appendChild(lv); has = true;
      });
      if (it.update && !it.update.can && it.update.reason === 'rights_expired') {
        // The only refusal we mention without a click: it is about time, not
        // about the person, and passes by itself after contact with the platform.
        sub.appendChild(U.el('div', 's', U.str('local_run_err_rights_expired'))); has = true;
      }
    }
    if (!has) return [tr];
    var tr2 = U.el('tr', 'sub-row'), td2 = U.el('td');
    td2.colSpan = 4; td2.appendChild(sub); tr2.appendChild(td2);
    return [tr, tr2];
  }

  // ── Folders ──────────────────────────────────────────────────────────
  // Reports lie in the same folders as on the platform: the folder name comes
  // in the manifest and lives in the copy state, so the sections are drawn
  // offline too, when there is nobody to ask. A report with no folder goes to
  // "No folder" FIRST — like "Unsorted" on the platform.
  function byFolder(items) {
    var map = {}, order = [];
    items.forEach(function (i) {
      var f = typeof i.folder === 'string' ? i.folder : '';
      if (!map[f]) { map[f] = []; order.push(f); }
      map[f].push(i);
    });
    order.sort(function (a, b) {
      if (!a) return -1;                       // "No folder" always first
      if (!b) return 1;
      return a.toLowerCase() < b.toLowerCase() ? -1 : 1;
    });
    return order.map(function (f) { return { name: f, items: map[f] }; });
  }
  function folderName(f) { return f || U.str('folder_none'); }
  // A single folder that is also "No folder" is not sections but a redundant
  // line above the list: there is nothing to group.
  function needFolders(groups) {
    return groups.length > 1 || (groups.length === 1 && !!groups[0].name);
  }

  // A table with its own header — exactly the "Dashboards" columns, no extras.
  function table(groups, run, sync) {
    var wrap = U.el('div', 'tbl-wrap'), t = U.el('table', 'tbl');
    var thead = U.el('thead'), htr = U.el('tr');
    [['col-title', 'column_report'], ['col-state', 'column_state'],
     ['col-updated', 'column_updated'], ['col-actions', '']].forEach(function (c) {
      htr.appendChild(U.el('th', c[0], c[1] ? U.str(c[1]) : ''));
    });
    thead.appendChild(htr); t.appendChild(thead);
    var tb = U.el('tbody'), withFolders = needFolders(groups);
    groups.forEach(function (g) {
      if (withFolders) {
        var ftr = U.el('tr', 'folder-row'), ftd = U.el('td');
        ftd.colSpan = 4;
        var head = U.el('div', 'folder-head');
        head.appendChild(U.el('span', 'folder-name', folderName(g.name)));
        head.appendChild(U.el('span', 'count mono', String(g.items.length)));
        ftd.appendChild(head); ftr.appendChild(ftd); tb.appendChild(ftr);
      }
      g.items.forEach(function (i) {
        card(i, run, sync).forEach(function (n) { tb.appendChild(n); });
      });
    });
    t.appendChild(tb); wrap.appendChild(t);
    return wrap;
  }

  // ── Tiles ────────────────────────────────────────────────────────────
  // The report frame is real, from the mirror: the document is on disk, and
  // showing it in the same iframe as the viewer is cheaper than drawing an
  // invented picture. The sandbox is the same as the viewer's and share.html's:
  // without allow-same-origin, so the report cannot reach the agent's handlers
  // or the device key. No data is fed into the frame (no bridge) — exactly as
  // on the platform, where the preview also shows the document without slots.
  var PREVIEW_MAX = 1500000;                   // like OP_PREVIEW_MAX_BYTES on the platform
  var MAX_INFLIGHT = 3;
  var inflight = 0, queue = [], observer = null;

  function pump() {
    while (inflight < MAX_INFLIGHT && queue.length) {
      var f = queue.shift();
      if (!f.dataset.src || f.src) continue;
      inflight++;
      var released = false;
      var release = function () {
        if (released) return;
        released = true; inflight--; pump();
      };
      f.addEventListener('load', function () { f.style.opacity = '1'; release(); }, { once: true });
      f.addEventListener('error', release, { once: true });
      // Watchdog: a hung frame must not hold up the queue of the others.
      window.setTimeout(release, 20000);
      f.src = f.dataset.src;
    }
  }

  function tile(it, run, sync) {
    var skipped = it.mirrorable === false;
    var openable = !skipped && it.state !== 'not_downloaded';
    var a = U.el(openable ? 'a' : 'div', 'dash-card');
    if (openable) a.href = openUrl(it);

    var prev = U.el('div', 'preview');
    var heavy = (it.size || 0) > PREVIEW_MAX;
    if (!openable || heavy) {
      prev.appendChild(U.el('div', 'preview-note',
        !openable ? U.str('preview_absent')
                  : U.fmt('preview_heavy', bytes(it.size))));
    } else {
      var f = document.createElement('iframe');
      f.className = 'preview-frame';
      f.setAttribute('sandbox', 'allow-scripts');
      f.setAttribute('scrolling', 'no');
      f.tabIndex = -1;
      f.dataset.src = '/mirror/' + encodeURIComponent(it.dashboard_id) + '/';
      prev.appendChild(f);
    }
    a.appendChild(prev);

    var sl = skipped ? null : stateLine(it);
    var meta = U.el('div', 'meta-row');
    var pill = U.el('span', 'pill' + (skipped ? '' : (sl[0] ? ' ' + sl[0] : '')));
    pill.appendChild(U.el('span', 'dot'));
    pill.appendChild(document.createTextNode(skipped ? U.fmt('report_state_skipped') : sl[1]));
    meta.appendChild(pill);
    a.appendChild(meta);
    a.appendChild(U.el('div', 'name-row', it.title || ''));
    a.appendChild(U.el('div', 'foot-row',
                       skipped ? '' : U.dateOrDash(it.local_at || it.fetched_at)));
    return a;
  }

  function grid(groups, run, sync) {
    var g = U.el('div', 'dash-grid'), withFolders = needFolders(groups);
    groups.forEach(function (gr) {
      if (withFolders) {
        var head = U.el('div', 'folder-sec');
        head.appendChild(U.el('span', 'folder-name', folderName(gr.name)));
        head.appendChild(U.el('span', 'count mono', String(gr.items.length)));
        g.appendChild(head);
      }
      gr.items.forEach(function (i) { g.appendChild(tile(i, run, sync)); });
    });
    return g;
  }

  // Frames load only when the tile has appeared on screen, and no more than
  // three at once: twenty reports starting simultaneously would eat both memory
  // and disk — the same discipline as `pumpPreviews` on the platform.
  function startPreviews(root) {
    if (observer) { observer.disconnect(); observer = null; }
    queue.length = 0;
    var frames = root.querySelectorAll('.preview-frame');
    if (!frames.length) return;
    Array.prototype.forEach.call(root.querySelectorAll('.preview'), function (p) {
      var f = p.querySelector('.preview-frame');
      if (f) f.style.transform = 'scale(' + (p.offsetWidth / 1280) + ')';
    });
    if (typeof IntersectionObserver !== 'function') {
      Array.prototype.forEach.call(frames, function (f) { queue.push(f); });
      pump(); return;
    }
    observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (!en.isIntersecting) return;
        var f = en.target.querySelector('.preview-frame');
        if (f && f.dataset.src && !f.src) queue.push(f);
        observer.unobserve(en.target);
      });
      pump();
    }, { rootMargin: '200px' });
    Array.prototype.forEach.call(root.querySelectorAll('.preview'), function (p) {
      observer.observe(p);
    });
  }

  function bytes(n) {
    if (!n) return '';
    var mb = n / 1048576;
    return (mb >= 10 ? Math.round(mb) : Math.round(mb * 10) / 10) + ' MB';
  }

  // The chosen view is remembered on this machine. localStorage, not agent
  // state: it is a preference of the eyes, not a program setting, and on a
  // person's two computers it may well differ.
  var VIEW_KEY = 'op-mirror-view';
  function savedView() {
    try { return window.localStorage.getItem(VIEW_KEY) === 'grid' ? 'grid' : 'table'; }
    catch (e) { return 'table'; }
  }
  function saveView(v) {
    try { window.localStorage.setItem(VIEW_KEY, v); } catch (e) { /* private mode */ }
  }
  var view = savedView();

  function render(data) {
    var items = Array.isArray(data.items) ? data.items : [], sync = data.sync || {};
    var run = data.local_run || null;
    $('title').textContent = U.str('library_title'); document.title = U.str('library_title');
    $('subtitle').textContent = U.str('library_subtitle');
    var copies = items.filter(function (i) { return i.mirrorable !== false; });
    var skipped = items.filter(function (i) { return i.mirrorable === false; });
    renderStatus(sync, items.length > 0);

    var rights = $('rights');
    rights.hidden = !data.rights_blocked;
    if (data.rights_blocked) rights.textContent = U.fmt('local_update_blocked_rights', U.dateOrDash(sync.last_sync_at));

    var sec = $('copies'); sec.textContent = '';
    $('count').textContent = copies.length ? U.fmt('library_count', copies.length) : '';
    $('count-sep').hidden = !copies.length;
    if (!copies.length) {
      var e = U.el('div', 'empty'); e.appendChild(U.el('h3', '', U.str('library_empty_title')));
      // "Add a key" to a person who has a key and simply has no reports is mockery (strings_README).
      var noAccess = sync.status === 'idle' && sync.last_sync_at && !items.length;
      e.appendChild(U.el('p', '', U.str(noAccess ? 'library_empty_no_access' : 'library_empty_hint')));
      // Without a schedule the key can be saved, but no update will start
      // from it — we do not show the button: a promise in an empty library
      // sounds louder than the honest state line above it.
      if (!noAccess && sync.enabled !== false && sync.status !== 'running' && !sync.has_key) e.appendChild(button('action_add_key', 'primary', openKey));
      var wrap = U.el('div', 'tbl-wrap'); wrap.appendChild(e); sec.appendChild(wrap);
    } else {
      var groups = byFolder(copies);
      sec.appendChild(view === 'grid' ? grid(groups, run, sync)
                                      : table(groups, run, sync));
      if (view === 'grid') startPreviews(sec);
    }
    $('skipped-wrap').hidden = !skipped.length;
    $('skipped-head').textContent = skipped.length ? U.fmt('library_skipped_head') : '';
    var sk = $('skipped'); sk.textContent = '';
    // Always as a table, even in tiles mode: there is nothing to show a frame
    // of for a report that is not on disk, and an empty tile looks like a breakage.
    skipped.forEach(function (i) { card(i, run, sync).forEach(function (n) { sk.appendChild(n); }); });

    clearTimeout(timer);
    // We poll while an offline run is going too: it has its own progress, and without this
    // the "Calculating…" bar would freeze until the next click.
    if (sync.status === 'running' || (run && run.running)) timer = setTimeout(refresh, 1500);
    window.__keyTtl = data.key_ttl_days || 90;
  }

  function refresh() {
    return U.loadData().then(render).catch(function () {
      // The local agent is not answering — that is not "the platform is unavailable"; no text of its own, we log to the console.
      console.error('library data failed');
      clearTimeout(timer); timer = setTimeout(refresh, 5000);
    });
  }

  function openKey() {
    $('kh-where').textContent = U.fmt('key_hint_where'); $('kh-once').textContent = U.fmt('key_hint_once');
    $('kh-scope').textContent = U.fmt('key_hint_scope', window.__keyTtl || 90);
    $('key-save').textContent = U.str('action_add_key'); $('key-input').value = '';
    $('key-dialog').showModal(); $('key-input').focus();
  }
  $('key-cancel').addEventListener('click', function () { $('key-dialog').close(); });
  $('key-form').addEventListener('submit', function (ev) {
    ev.preventDefault();
    var key = $('key-input').value.trim(); $('key-input').value = '';   // clear the field right away
    if (!key) return;
    $('key-dialog').close();
    post('/library/key', { key: key }).then(function (r) { if (r.ok) toast(U.fmt('key_added')); }).then(refresh);
  });

  function initViewSeg() {
    var seg = $('view-seg');
    Array.prototype.forEach.call(seg.querySelectorAll('button'), function (b) {
      b.textContent = U.str(b.dataset.view === 'grid' ? 'view_grid' : 'view_table');
      if (b.dataset.view === view) b.classList.add('active');
      else b.classList.remove('active');
      b.addEventListener('click', function () {
        if (view === b.dataset.view) return;
        view = b.dataset.view; saveView(view);
        initViewSeg(); refresh();
      });
    });
  }

  U.loadStrings().then(function () { initViewSeg(); return refresh(); });
})();
