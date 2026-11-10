/* The "Report folders" screen. No build and no dependencies.

   Two rules that must not be broken here:

   1. Any text that came from the agent is set ONLY through textContent.
      These strings hold paths from the person's machine: folder names are
      written by people, and innerHTML on 127.0.0.1 is an XSS in a window with
      handlers right next to it that can write to the settings file.
   2. The page remembers NOTHING between responses: an edit returns the full
      state, and we redraw the whole screen. A stitched-together view would
      diverge from the disk on the first edit that went through halfway. */
(function () {
  'use strict';

  var S = {};
  var state = null;
  var busy = false;

  function fmt(key, p1) {
    var s = S[key];
    // No string: we show the key NAME, as the mirror does: an empty caption
    // is indistinguishable from a normal page, while the key name says at once
    // that strings.json was lost and not that a button is nameless.
    if (typeof s !== 'string') return key == null ? '' : String(key);
    return s.split('{p1}').join(p1 == null ? '' : String(p1));
  }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function bytes(n) {
    if (typeof n !== 'number' || !isFinite(n) || n < 0) return '—';
    var u = ['B', 'KB', 'MB', 'GB'], i = 0, v = n;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return (i === 0 ? v : v.toFixed(1)) + ' ' + u[i];
  }

  // When it was last downloaded. The date is assembled by the AGENT
  // (`s3ui._last_pull`): a second date format on the same screen would one day
  // diverge from the first, and there is nothing to configure `Intl` with in
  // the kit.
  function lastPullText(info) {
    if (!info || !info.at) return fmt('s3_last_pull_never');
    return info.ok === false ? fmt('s3_last_pull_failed', info.at)
                             : fmt('s3_last_pull', info.at);
  }

  function post(action, body) {
    return fetch('/folders-ui/' + action, {
      method: 'POST', cache: 'no-store',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().then(function (j) { return { status: r.status, body: j }; });
    });
  }

  function showError(text) {
    var box = document.getElementById('error');
    box.textContent = text || '';
    box.hidden = !text;
  }

  // Refusal code -> English phrase. The key is built from the code, not from a
  // set of ifs: a new refusal code in `foldersio` gets its text by adding a
  // line to strings.json, and until it is there the code itself is visible on
  // screen, not emptiness.
  function errText(j) {
    if (!j || !j.error) return fmt('err_internal', '');
    var k = 'err_' + j.error;
    if (typeof S[k] === 'string') return fmt(k, j.detail || '');
    return fmt('err_internal', j.error + (j.detail ? ': ' + j.detail : ''));
  }

  function act(action, body) {
    if (busy) return Promise.resolve();
    busy = true;
    showError('');
    return post(action, body).then(function (r) {
      if (r.body && r.body.state) { state = r.body.state; render(); }
      if (r.status !== 200 || (r.body && r.body.ok === false)) {
        showError(errText(r.body));
      }
    }, function () {
      showError(fmt('err_network'));
    }).then(function () { busy = false; });
  }

  // ── drawing ───────────────────────────────────────────────────────────

  function sectionTitle(sec) {
    return sec.scenario ? fmt('section_scenario', sec.scenario)
                        : fmt('section_default');
  }

  function entryRow(sec, item) {
    var li = el('li');
    li.appendChild(el('span', 'kind', fmt(item.kind === 'file' ? 'entry_file' : 'entry_dir')));
    li.appendChild(el('span', 'raw', item.raw));
    if (item.state) {
      var ok = item.state === 'dir' || item.state === 'file';
      li.appendChild(el('span', 'tag ' + (ok ? 'ok' : 'bad'), fmt('state_' + item.state)));
    }
    var rm = el('button', 'btn link', fmt('remove'));
    rm.addEventListener('click', function () {
      if (!window.confirm(fmt('remove_confirm', item.raw))) return;
      act('entry/remove', { key: sec.key, index: item.index });
    });
    li.appendChild(rm);
    return li;
  }

  function masksBlock(sec) {
    var box = el('div');
    box.appendChild(el('h3', null, fmt('masks_title')));
    var inc = el('input'); inc.type = 'text'; inc.className = 'grow';
    inc.value = (sec.include || []).join(', ');
    var exc = el('input'); exc.type = 'text'; exc.className = 'grow';
    exc.value = (sec.exclude || []).join(', ');
    [['masks_include', inc], ['masks_exclude', exc]].forEach(function (pair) {
      var row = el('div', 'grid');
      row.appendChild(el('label', null, fmt(pair[0])));
      row.appendChild(pair[1]);
      box.appendChild(row);
    });
    var save = el('button', 'btn', fmt('masks_save'));
    save.addEventListener('click', function () {
      act('masks', {
        key: sec.key,
        include: inc.value.split(',').map(function (s) { return s.trim(); })
          .filter(Boolean),
        exclude: exc.value.split(',').map(function (s) { return s.trim(); })
          .filter(Boolean)
      });
    });
    box.appendChild(save);
    box.appendChild(el('p', 'probe', fmt('masks_hint')));
    return box;
  }

  function addBlock(sec) {
    var box = el('div');
    box.appendChild(el('h3', null, fmt('add_title')));
    var kind = document.createElement('select');
    [['dir', fmt('entry_dir')], ['file', fmt('entry_file')]].forEach(function (o) {
      var opt = document.createElement('option');
      opt.value = o[0]; opt.textContent = o[1];
      kind.appendChild(opt);
    });
    var path = el('input'); path.type = 'text'; path.className = 'grow';
    path.setAttribute('spellcheck', 'false');
    var out = el('p', 'probe', '');

    // The existence check runs ON INPUT, with a delay: a path can be not only
    // chosen with the "Browse…" button but pasted as a string from the
    // Explorer address bar or from an email, and then "I clicked with the
    // mouse, so it exists" is replaced by the program's immediate answer about
    // what lies at that path.
    var timer = 0;
    function probe(show) {
      var value = path.value.trim();
      if (!value) { out.textContent = show ? fmt('probe_empty') : ''; return; }
      post('probe', { path: value }).then(function (r) {
        var j = r.body || {};
        if (j.ok === false) { out.textContent = errText(j); return; }
        if (j.state === 'dir') {
          var t = fmt('probe_dir', j.count);
          if (show && j.names && j.names.length) {
            t += ' ' + fmt('probe_dir_names', j.names.join(', '));
          }
          out.textContent = t;
        } else if (j.state === 'file') {
          out.textContent = fmt('probe_file', bytes(j.size));
        } else if (j.state === 'denied') {
          out.textContent = fmt('probe_denied');
        } else {
          out.textContent = fmt('probe_missing');
        }
      }, function () { out.textContent = fmt('err_network'); });
    }
    path.addEventListener('input', function () {
      window.clearTimeout(timer);
      timer = window.setTimeout(function () { probe(false); }, 350);
    });

    var row = el('div', 'grid');
    row.appendChild(el('label', null, fmt('add_kind')));
    row.appendChild(kind);
    row.appendChild(el('label', null, fmt('add_path')));
    row.appendChild(path);
    // "Browse…" is the Explorer system dialog. It does NOT write to the config
    // itself: the chosen path lands in the field, the person sees it and
    // presses "Add". A dialog that wrote at once would deprive them of that
    // look, and the path is the main thing that can be mixed up here.
    var browse = el('button', 'btn', fmt('add_browse'));
    browse.addEventListener('click', function () {
      browse.disabled = true;
      post('pick', { kind: kind.value }).then(function (r) {
        var j = r.body || {};
        // Cancel and "no dialog" arrive the same way (`ok: false`) and do
        // the same nothing: the field stays usable.
        if (j.ok && j.path) { path.value = j.path; probe(true); }
        else if (j.ok === false && !j.path) { out.textContent = fmt('pick_none'); }
      }, function () { out.textContent = fmt('err_network'); })
        .then(function () { browse.disabled = false; });
    });
    row.appendChild(browse);
    var show = el('button', 'btn', fmt('add_show'));
    show.addEventListener('click', function () { probe(true); });
    row.appendChild(show);
    var add = el('button', 'btn primary', fmt('add_submit'));
    add.addEventListener('click', function () {
      act('entry/add', { key: sec.key, kind: kind.value, raw: path.value });
    });
    row.appendChild(add);
    box.appendChild(row);
    box.appendChild(out);
    box.appendChild(el('p', 'probe', fmt('add_path_hint')));
    return box;
  }

  function scenarioBlock(sec) {
    var box = el('div');
    box.appendChild(el('h3', null, fmt('scenario_title')));
    var name = el('input'); name.type = 'text'; name.className = 'grow';
    var row = el('div', 'grid');
    row.appendChild(el('label', null, fmt('scenario_name')));
    row.appendChild(name);
    var go = el('button', 'btn', fmt('scenario_submit'));
    go.addEventListener('click', function () {
      act('scenario', { key: sec.key, scenario: name.value });
    });
    row.appendChild(go);
    box.appendChild(row);
    box.appendChild(el('p', 'probe', fmt('scenario_hint')));
    return box;
  }

  function sectionCard(sec) {
    var card = el('section', 'card');
    var head = el('header');
    head.appendChild(el('h2', null, sectionTitle(sec)));
    head.appendChild(el('span', 'meta', fmt('section_report', sec.dashboard_id)));
    card.appendChild(head);

    if (sec.checked) {
      card.appendChild(el('p', 'meta',
        fmt('files_found', sec.files) + ' · ' + fmt('files_bytes', bytes(sec.bytes))));
    } else {
      card.appendChild(el('p', 'meta', fmt('not_checked')));
    }

    var check = el('button', 'btn', fmt('check'));
    check.addEventListener('click', function () {
      check.disabled = true;
      check.textContent = fmt('checking');
      act('check', { key: sec.key });
    });
    card.appendChild(check);

    var list = el('ul', 'list');
    sec.entries.forEach(function (item) { list.appendChild(entryRow(sec, item)); });
    card.appendChild(list);

    (sec.missing || []).forEach(function (m) {
      var box = el('div', 'miss');
      box.appendChild(el('code', null, m.raw));
      box.appendChild(el('div', null, fmt('reason_' + m.reason)));
      card.appendChild(box);
    });

    if (sec.duplicates && sec.duplicates.length) {
      card.appendChild(el('h3', null, fmt('duplicates_title')));
      var dl = el('ul', 'list');
      sec.duplicates.forEach(function (d) {
        var li = el('li');
        li.appendChild(el('span', 'raw', d.name + ' — ' + d.paths.join(' | ')));
        dl.appendChild(li);
      });
      card.appendChild(dl);
      card.appendChild(el('p', 'probe', fmt('duplicates_hint')));
    }

    card.appendChild(addBlock(sec));
    card.appendChild(masksBlock(sec));
    card.appendChild(scenarioBlock(sec));
    return card;
  }

  function newSection() {
    var id = window.prompt(fmt('new_section_id') + '\n' + fmt('new_section_hint'), '');
    if (id === null) return;
    var scen = window.prompt(fmt('scenario_name'), '');
    if (scen === null) return;
    act('section', { dashboard_id: id, scenario: scen });
  }

  // ── S3 bucket ─────────────────────────────────────────────────────────
  // The fields are named as in WinSCP and in the same order: the person comes
  // here with its window in front of their eyes, and "Host name" next to "Port
  // number" they recognise, while "endpoint" they do not. Building the endpoint
  // from host+port+TLS lives on the agent side (`s3ui.make_endpoint`), here
  // there are only fields.

  // A draft of a new connection. The only thing the page remembers between
  // responses, and it remembers not the disk state but what the person has not
  // saved yet: erasing that by a redraw would mean erasing their input.
  var s3draft = null;

  function s3Field(labelKey, node) {
    var row = el('div', 'grid');
    row.appendChild(el('label', null, fmt(labelKey)));
    row.appendChild(node);
    return row;
  }

  function s3Input(type, value, cls) {
    var i = document.createElement('input');
    i.type = type;
    i.className = cls || 'grow';
    i.setAttribute('spellcheck', 'false');
    if (value != null) i.value = String(value);
    return i;
  }

  function s3Card(src, isNew) {
    var card = el('section', 'card');
    var head = el('header');
    head.appendChild(el('h2', null, isNew ? fmt('s3_new') : fmt('s3_conn', src.name)));
    if (!isNew) {
      head.appendChild(el('span', 'meta',
        src.secret_set ? fmt('s3_secret_set') : fmt('s3_secret_unset')));
      // The date of the last download is in the header, not in the result of a
      // press: the person comes here with the question "is the folder fresh",
      // and the answer must not require pressing a button.
      head.appendChild(el('span', 'meta', lastPullText(src.last_pull)));
    }
    card.appendChild(head);

    var name = s3Input('text', src.name || '');
    // The name is at once the section key, the DPAPI entry name and the name
    // of the downloaded-state file. Renaming would be a move of all three, so
    // for a saved connection the field only displays.
    name.disabled = !isNew;
    var host = s3Input('text', src.host || '');
    var port = s3Input('number', src.port == null ? 443 : src.port, 'narrow');
    var tls = document.createElement('input');
    tls.type = 'checkbox';
    tls.checked = src.tls !== false;
    var tlsLabel = el('label', 'check');
    tlsLabel.appendChild(tls);
    tlsLabel.appendChild(el('span', null, fmt('s3_tls')));
    var access = s3Input('text', src.access_key || '');
    var secret = s3Input('password', '');
    secret.autocomplete = 'new-password';
    var remote = s3Input('text', src.remote || '');
    var target = s3Input('text', src.target || '');
    var region = s3Input('text', src.region || '');
    var inc = s3Input('text', (src.include || []).join(', '));
    var exc = s3Input('text', (src.exclude || []).join(', '));
    var style = document.createElement('select');
    [['true', fmt('s3_yes')], ['false', fmt('s3_no')]].forEach(function (o) {
      var opt = document.createElement('option');
      opt.value = o[0]; opt.textContent = o[1];
      style.appendChild(opt);
    });
    style.value = src.path_style === false ? 'false' : 'true';

    // This is what the bucket is connected for: the run fetches the fresh data
    // itself. The checkbox can be cleared, and then the report is computed on
    // what is already downloaded, but that is a DELIBERATE choice and not a
    // state by oversight, hence the default "yes".
    var refresh = document.createElement('input');
    refresh.type = 'checkbox';
    refresh.checked = src.refresh_before_run !== false;

    var parsed = el('p', 'probe', '');
    var result = el('p', 'probe', '');

    function form() {
      return {
        name: name.value, host: host.value, port: port.value,
        tls: tls.checked, access_key: access.value, remote: remote.value,
        target: target.value, region: region.value,
        path_style: style.value === 'true',
        refresh_before_run: refresh.checked,
        include: inc.value, exclude: exc.value
      };
    }

    // The parse of `Remote directory` into bucket and prefix is shown ALOUD and
    // computed by the agent, with the same code that will later write the
    // section. There is deliberately no parse of our own here: two parses of
    // one field would mean what is shown is not what will be saved.
    var timer = 0;
    function say() {
      post('s3/preview', form()).then(function (r) {
        var j = r.body || {};
        if (j.ok === false) { parsed.textContent = errText(j); return; }
        parsed.textContent = fmt('s3_parsed', j.endpoint) + ' ' +
          fmt('s3_parsed_remote', j.bucket) + ' ' +
          fmt('s3_parsed_prefix', j.prefix || fmt('s3_prefix_root')) + ' ' +
          fmt('s3_parsed_region', j.region);
      }, function () { parsed.textContent = fmt('err_network'); });
    }
    [name, host, port, remote, region].forEach(function (node) {
      node.addEventListener('input', function () {
        window.clearTimeout(timer);
        timer = window.setTimeout(say, 350);
      });
    });
    tls.addEventListener('change', say);

    card.appendChild(s3Field('s3_name', name));
    if (isNew) card.appendChild(el('p', 'probe', fmt('s3_name_hint')));
    var hostRow = el('div', 'grid');
    hostRow.appendChild(el('label', null, fmt('s3_host')));
    hostRow.appendChild(host);
    hostRow.appendChild(el('label', null, fmt('s3_port')));
    hostRow.appendChild(port);
    hostRow.appendChild(tlsLabel);
    card.appendChild(hostRow);
    card.appendChild(s3Field('s3_access_key', access));
    card.appendChild(s3Field('s3_secret', secret));
    card.appendChild(el('p', 'probe', fmt('s3_secret_hint')));
    card.appendChild(s3Field('s3_remote', remote));
    card.appendChild(el('p', 'probe', fmt('s3_remote_hint')));
    card.appendChild(parsed);

    // The destination folder is chosen with the same "Browse…" button as the
    // report folders: a second way to name a folder on one screen is a second
    // way to make a mistake.
    var targetRow = el('div', 'grid');
    targetRow.appendChild(el('label', null, fmt('s3_target')));
    targetRow.appendChild(target);
    var browse = el('button', 'btn', fmt('add_browse'));
    browse.addEventListener('click', function () {
      browse.disabled = true;
      post('pick', { kind: 'dir' }).then(function (r) {
        var j = r.body || {};
        if (j.ok && j.path) target.value = j.path;
        else if (j.ok === false) result.textContent = fmt('pick_none');
      }, function () { result.textContent = fmt('err_network'); })
        .then(function () { browse.disabled = false; });
    });
    targetRow.appendChild(browse);
    card.appendChild(targetRow);
    card.appendChild(el('p', 'probe', fmt('s3_target_hint')));

    card.appendChild(s3Field('s3_region', region));
    card.appendChild(s3Field('s3_path_style', style));
    card.appendChild(s3Field('s3_refresh', refresh));
    card.appendChild(el('p', 'probe', fmt('s3_refresh_hint')));
    card.appendChild(s3Field('s3_include', inc));
    card.appendChild(s3Field('s3_exclude', exc));

    var row = el('div', 'grid');
    var save = el('button', 'btn primary', fmt('s3_save'));
    save.addEventListener('click', function () {
      s3draft = null;
      act('s3/save', form());
    });
    row.appendChild(save);

    // The check is REAL: the agent goes to the storage and lists the prefix.
    // Checking field completeness on our own would mean deceiving exactly at
    // the moment when the check is of use.
    var test = el('button', 'btn', fmt('s3_test'));
    test.addEventListener('click', function () {
      // We check WHAT IS ON SCREEN, and before saving. A refusal "save first"
      // turned the button into a trap: it is there, visible, and on a new
      // connection always answered "no", and the owner rightly called it "does
      // not work". In WinSCP Login checks what was typed, and here it is the
      // same.
      var body = form();
      body.secret = secret.value;
      test.disabled = true;
      result.textContent = fmt('s3_testing');
      post('s3/test', body).then(function (r) {
        var j = r.body || {};
        if (!j || j.ok !== true) { result.textContent = errText(j); return; }
        var t = fmt('s3_test_ok', j.matched) + ' ' +
          fmt('s3_test_listed', j.listed) + ' ' +
          fmt('s3_test_bytes', bytes(j.bytes));
        if (j.names && j.names.length) {
          t += ' ' + fmt('s3_test_names', j.names.join(', '));
        }
        if (j.truncated) t += ' ' + fmt('s3_test_truncated');
        t += ' ' + fmt('s3_test_target', j.target);
        result.textContent = t;
      }, function () { result.textContent = fmt('err_network'); })
        .then(function () { test.disabled = false; });
    });
    row.appendChild(test);

    // "Check" shows what lies in the bucket; "Download now" puts it into the
    // folder. Different questions, different buttons: a person who saw "found
    // 12 files" has the right to think they are already with them, and that
    // would be untrue.
    //
    // The answer to a press arrives AT ONCE ("started"), and the progress by
    // polling: an export of forty megabytes downloads for a minute, and a
    // request hanging for that minute is indistinguishable from a hung
    // program. "Update now" in the report mirror is done exactly the same
    // way.
    var pull = el('button', 'btn', fmt('s3_pull'));
    var pollTimer = 0;

    function pullText(p) {
      var t = '';
      if (p.running) {
        t = fmt('s3_pull_progress', (p.done || 0) + '/' + (p.total || 0));
        if (p.bytes) t += ' ' + fmt('s3_pull_bytes', bytes(p.bytes));
        return t;
      }
      if (p.ok === false || p.error) {
        return fmt(p.downloaded ? 's3_pull_partial' : 's3_pull_failed',
                   p.error || '');
      }
      t = fmt('s3_pull_ok', p.downloaded || 0) + ' ' +
        fmt('s3_pull_bytes', bytes(p.bytes || 0));
      if (p.skipped) t += ' ' + fmt('s3_pull_skipped', p.skipped);
      if (p.deleted) t += ' ' + fmt('s3_pull_deleted', p.deleted);
      if (p.foreign && p.foreign.length) {
        t += ' ' + fmt('s3_pull_foreign', p.foreign.join(', '));
      }
      return t;
    }

    function poll() {
      fetch('/folders-ui/s3/progress', { cache: 'no-store' })
        .then(function (r) { return r.json(); })
        .then(function (j) {
          var p = (j && j.pulls && j.pulls[src.name]) || null;
          if (!p) { pull.disabled = false; return; }
          result.textContent = pullText(p) + ' ' + lastPullText(p.last_pull);
          if (p.running) { pollTimer = window.setTimeout(poll, 1000); }
          else { pull.disabled = false; }
        }, function () {
          // The program did not answer: we must not lie "downloaded", but
          // there is nothing to cut the display short with either: the button
          // simply becomes usable again.
          result.textContent = fmt('err_network');
          pull.disabled = false;
        });
    }

    pull.addEventListener('click', function () {
      if (isNew) { result.textContent = fmt('s3_pull_unsaved'); return; }
      pull.disabled = true;
      window.clearTimeout(pollTimer);
      result.textContent = fmt('s3_pulling');
      post('s3/pull', { name: src.name }).then(function (r) {
        var j = r.body || {};
        if (!j || j.ok !== true) {
          result.textContent = errText(j);
          pull.disabled = false;
          return;
        }
        if (j.started === false) result.textContent = fmt('s3_pull_running');
        poll();
      }, function () {
        result.textContent = fmt('err_network');
        pull.disabled = false;
      });
    });
    row.appendChild(pull);

    if (!isNew) {
      if (src.secret_set) {
        var forget = el('button', 'btn link', fmt('s3_forget'));
        forget.addEventListener('click', function () {
          if (!window.confirm(fmt('s3_forget_confirm', src.name))) return;
          act('s3/forget', { name: src.name });
        });
        row.appendChild(forget);
      }
      var drop = el('button', 'btn link', fmt('s3_remove'));
      drop.addEventListener('click', function () {
        if (!window.confirm(fmt('s3_remove_confirm', src.name))) return;
        act('s3/remove', { name: src.name });
      });
      row.appendChild(drop);
    } else {
      var cancel = el('button', 'btn link', fmt('s3_cancel'));
      cancel.addEventListener('click', function () { s3draft = null; render(); });
      row.appendChild(cancel);
    }
    card.appendChild(row);
    card.appendChild(result);
    card.appendChild(el('p', 'probe', fmt('s3_run_hint')));
    return card;
  }

  function renderS3() {
    var s3 = (state && state.s3) || {};
    document.getElementById('s3-title').textContent = fmt('s3_title');
    document.getElementById('s3-sub').textContent = fmt('s3_sub');
    document.getElementById('s3-ini').textContent =
      s3.exists ? fmt('s3_ini', s3.ini) : fmt('s3_ini_absent', s3.ini);
    document.getElementById('s3-add').textContent = fmt('s3_add');

    var prob = document.getElementById('s3-problem');
    if (s3.problem) {
      prob.textContent = fmt('s3_problem', s3.problem);
      prob.hidden = false;
    } else { prob.hidden = true; }

    var host = document.getElementById('s3-list');
    host.textContent = '';
    var list = s3.sources || [];
    list.forEach(function (src) { host.appendChild(s3Card(src, false)); });
    if (s3draft) host.appendChild(s3Card(s3draft, true));
    var empty = document.getElementById('s3-empty');
    empty.textContent = fmt('s3_empty');
    empty.hidden = !!(list.length || s3draft);
  }

  function render() {
    var s = state || {};
    document.getElementById('title').textContent = fmt('title');
    document.getElementById('subtitle').textContent = fmt('subtitle');
    document.getElementById('ini').textContent =
      s.exists ? fmt('ini_path', s.ini) : fmt('ini_absent');
    document.getElementById('reload').textContent = fmt('reload');
    document.getElementById('new-section').textContent = fmt('new_section_title');

    var prob = document.getElementById('problem');
    if (s.problem) {
      prob.textContent = fmt('problem_' + s.problem,
        (s.duplicate_keys || []).join(', '));
      prob.hidden = false;
    } else { prob.hidden = true; }

    // The promise about the person's comments is given by the AGENT
    // (`will_preserve_comments`), not by our memory of how the writing is
    // built: if a path appears on which comments are lost, the warning will
    // appear by itself.
    var cm = document.getElementById('comments');
    cm.textContent = fmt(s.comments_preserved ? 'comments_kept' : 'comments_lost');
    cm.className = 'notice' + (s.comments_preserved ? '' : ' warn');
    cm.hidden = false;

    var part = document.getElementById('partial');
    part.textContent = fmt('partial_note');
    part.hidden = !s.partial;

    var host = document.getElementById('sections');
    host.textContent = '';
    (s.sections || []).forEach(function (sec) { host.appendChild(sectionCard(sec)); });

    var empty = document.getElementById('empty');
    empty.hidden = !!(s.sections && s.sections.length);
    document.getElementById('empty-title').textContent = fmt('empty_title');
    document.getElementById('empty-hint').textContent = fmt('empty_hint');

    renderS3();
  }

  function load() {
    showError('');
    return fetch('/folders-ui/state.json', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (j) { state = j; render(); },
            function () { showError(fmt('err_network')); });
  }

  function start() {
    document.getElementById('reload').addEventListener('click', load);
    document.getElementById('new-section').addEventListener('click', newSection);
    document.getElementById('s3-add').addEventListener('click', function () {
      s3draft = { tls: true, port: 443, path_style: true };
      render();
    });
    // A 404 on strings.json and broken JSON arrive here the same way, as "no
    // texts": the screen must open without them too, and the agent speaks of
    // the reason with a banner right in the HTML (foldersui._send_page).
    fetch('/folders-ui/strings.json', { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) { S = (j && typeof j === 'object' && !Array.isArray(j)) ? j : {}; },
            function () { S = {}; })
      .then(load);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else { start(); }
})();
