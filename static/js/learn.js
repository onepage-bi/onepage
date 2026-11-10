/* "Working on the platform" (/learn) — the lesson path and the lesson itself.

   One page, two screens: the path (units with their badges and lesson circles)
   and the lesson (cards → quiz → task → summary). State arrives from the server
   in `#lr-root` data-state and after every check is replaced by what the
   server returned: the server computes XP, level and unlocked lessons, the page shows them.

   Correct answers are not stored here — they arrive in the check response,
   after the person has answered. Keyboard: 1…9 pick an option,
   Enter — "Next". */
(function () {
  'use strict';

  var root = document.getElementById('lr-root');
  if (!root) return;

  var state = null;
  try { state = JSON.parse(root.getAttribute('data-state') || 'null'); } catch (e) {}
  if (!state) return;
  var COURSE = root.getAttribute('data-course') || state.course || 'prep';
  if (state.style) root.setAttribute('data-style', state.style);

  var motionOK = !(window.matchMedia &&
                   window.matchMedia('(prefers-reduced-motion: reduce)').matches);

  function confetti(ox, oy) {
    // A small celebration for a finished lesson: dots in platform colours,
    // 950 ms and self-cleanup. No libraries — the server is offline.
    if (!motionOK) return;
    var box = el('div', { class: 'lr-confetti' });
    var cs = getComputedStyle(document.documentElement);
    var colors = [cs.getPropertyValue('--accent').trim() || '#2f6bff',
                  cs.getPropertyValue('--success').trim() || '#13916b',
                  cs.getPropertyValue('--warning').trim() || '#c9871f'];
    var rect = nextBtn.getBoundingClientRect();
    var cx = ox == null ? rect.left + rect.width / 2 : ox;
    var cy = oy == null ? rect.top + rect.height / 2 : oy;
    for (var i = 0; i < 16; i++) {
      var dot = el('i');
      var ang = (Math.PI * 2 * i) / 16 + Math.random() * 0.4;
      var dist = 90 + Math.random() * 130;
      dot.style.left = cx + 'px';
      dot.style.top = cy + 'px';
      dot.style.background = colors[i % colors.length];
      dot.style.setProperty('--dx', Math.round(Math.cos(ang) * dist) + 'px');
      dot.style.setProperty('--dy', Math.round(Math.sin(ang) * dist + 60) + 'px');
      dot.style.setProperty('--rot', Math.round(Math.random() * 360) + 'deg');
      dot.style.animationDelay = (i * 12) + 'ms';
      box.appendChild(dot);
    }
    document.body.appendChild(box);
    setTimeout(function () { box.remove(); }, 1400);
  }

  var pathEl = document.getElementById('lr-path');
  var lessonEl = document.getElementById('lr-lesson');
  var stepsEl = document.getElementById('lr-steps');
  var nextBtn = document.getElementById('lr-next-step');
  var hintEl = document.getElementById('lr-hint');
  var progressEl = document.getElementById('lr-progress-fill');
  var RING = 327;           // 2πr with r = 52 from the ring markup

  var cur = null;           // current lesson: steps and position

  function el(tag, attrs, text) {
    var n = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (k) {
      if (k === 'class') n.className = attrs[k];
      else n.setAttribute(k, attrs[k]);
    });
    if (text != null) n.textContent = text;
    return n;
  }

  function lockIcon() {
    // Our own badge instead of an emoji: emoji are coloured and differ between systems.
    var ns = 'http://www.w3.org/2000/svg';
    var svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('viewBox', '0 0 16 16');
    svg.setAttribute('width', '15'); svg.setAttribute('height', '15');
    svg.setAttribute('fill', 'none'); svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '1.4');
    var body = document.createElementNS(ns, 'rect');
    body.setAttribute('x', '3.5'); body.setAttribute('y', '7');
    body.setAttribute('width', '9'); body.setAttribute('height', '6');
    body.setAttribute('rx', '1.2');
    var shackle = document.createElementNS(ns, 'path');
    shackle.setAttribute('d', 'M5.8 7V5.4a2.2 2.2 0 0 1 4.4 0V7');
    svg.appendChild(body); svg.appendChild(shackle);
    return svg;
  }

  function checkIcon() {
    // A text "check mark" on an accent fill reads pale and small —
    // draw our own, the same stroke width as the lock icon.
    var ns = 'http://www.w3.org/2000/svg';
    var svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('viewBox', '0 0 16 16');
    svg.setAttribute('width', '18'); svg.setAttribute('height', '18');
    svg.setAttribute('fill', 'none'); svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '2.2');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    var path = document.createElementNS(ns, 'path');
    path.setAttribute('d', 'M3.5 8.5l3 3 6-6.5');
    svg.appendChild(path);
    return svg;
  }

  function unitArt(unit, cls) {
    return el('img', { class: cls, alt: '', loading: 'lazy',
                       src: unit.image_url || ('/static/img/learn/' + unit.image + '.webp') });
  }

  function lessonDone(id) {
    for (var u = 0; u < state.units.length; u++) {
      var ls = state.units[u].lessons;
      for (var i = 0; i < ls.length; i++) if (ls[i].id === id) return !!ls[i].done;
    }
    return false;
  }

  function firstOpen() {
    for (var u = 0; u < state.units.length; u++) {
      var ls = state.units[u].lessons;
      for (var i = 0; i < ls.length; i++) if (!ls[i].done && ls[i].unlocked) return ls[i];
    }
    return null;
  }

  function unitOf(lessonId) {
    for (var u = 0; u < state.units.length; u++) {
      var ls = state.units[u].lessons;
      for (var i = 0; i < ls.length; i++) if (ls[i].id === lessonId) return state.units[u];
    }
    return null;
  }

  /* ── Cover ─────────────────────────────────────────────────────────── */
  function renderHead() {
    var lv = state.level || {};
    document.getElementById('lr-level').textContent = lv.name || '';
    document.getElementById('lr-sub').textContent = state.subtitle || '';
    // The interface is translated, the chapters are not. Say so plainly rather
    // than leave the person guessing why the buttons are in one language and the text in another.
    var langNote = document.getElementById('lr-lang');
    if (langNote) {
      var uiLang = document.documentElement.getAttribute('lang') || 'ru';
      var mismatch = state.content_lang && uiLang.indexOf(state.content_lang) !== 0;
      langNote.hidden = !mismatch;
      if (mismatch) langNote.textContent = t('The chapters are Russian for now — the interface language is switched in the top bar.');
    }
    if (state.title) document.getElementById('lr-title').textContent = state.title;
    document.getElementById('lr-xp').textContent = String(state.xp);

    var share = state.xp_total ? state.xp / state.xp_total : 0;
    document.getElementById('lr-ring-fg').style.strokeDashoffset =
      String(Math.round(RING * (1 - Math.max(0, Math.min(1, share)))));
    document.getElementById('lr-next').textContent =
      t('{done} of {total} lessons').replace('{done}', String(state.done))
                                   .replace('{total}', String(state.lessons_total));

    var nextLesson = firstOpen();
    var label = document.getElementById('lr-continue-label');
    var note = document.getElementById('lr-continue-next');
    if (!nextLesson) {
      label.textContent = t('Start over');
      note.textContent = t('the course is complete');
    } else {
      label.textContent = state.done ? t('Continue') : t('Start');
      note.textContent = nextLesson.title +
        (lv.next_at != null
          ? ' · ' + t('{xp} XP to «{level}»')
              .replace('{level}', lv.next_name || '')
              .replace('{xp}', String(Math.max(0, lv.next_at - state.xp)))
          : '');
    }
  }

  /* ── Path ──────────────────────────────────────────────────────────── */
  function renderPath() {
    pathEl.innerHTML = '';
    state.units.forEach(function (unit, ui) {
      var opened = unit.lessons.some(function (l) { return l.unlocked; });
      var sec = el('section', {
        class: 'lr-unit' + (unit.done === unit.total ? ' is-done' : '') + (opened ? ' is-open' : '')
      });

      var head = el('div', { class: 'lr-unit-head' });
      if (unit.image || unit.image_url) head.appendChild(unitArt(unit, 'lr-unit-art'));
      var titles = el('div', { class: 'lr-unit-titles' });
      titles.appendChild(el('div', { class: 'lr-unit-n' },
        t('unit') + ' ' + String(ui + 1)));
      titles.appendChild(el('div', { class: 'lr-unit-title' }, unit.title));
      titles.appendChild(el('div', { class: 'lr-unit-sub' }, unit.subtitle || ''));
      head.appendChild(titles);
      var prog = el('div', { class: 'lr-unit-progress' });
      var bar = el('div', { class: 'lr-unit-bar' });
      var fill = el('i');
      fill.style.width = Math.round(unit.done * 100 / Math.max(1, unit.total)) + '%';
      bar.appendChild(fill);
      prog.appendChild(bar);
      prog.appendChild(el('span', {}, unit.done + '/' + unit.total));
      head.appendChild(prog);
      sec.appendChild(head);

      var track = el('div', { class: 'lr-track' });
      unit.lessons.forEach(function (lesson, li) {
        var open = lesson.unlocked && !lesson.done;
        var node = el('button', { type: 'button',
          class: 'lr-node' + (lesson.done ? ' is-done' : (open ? ' is-current' : '')) });
        if (!lesson.unlocked) node.setAttribute('disabled', 'disabled');
        var dot = el('span', { class: 'lr-dot' });
        if (lesson.done) dot.appendChild(checkIcon());
        else if (lesson.unlocked) dot.textContent = String(li + 1);
        else dot.appendChild(lockIcon());
        node.appendChild(dot);
        node.appendChild(el('span', { class: 'lr-node-label' }, lesson.title));
        node.appendChild(el('span', { class: 'lr-node-xp' }, '+' + lesson.xp + ' XP'));
        node.addEventListener('click', function () { openLesson(lesson); });
        track.appendChild(node);
      });
      if (unit.exam && unit.exam.id) {
        var ex = unit.exam;
        var exNode = el('button', { type: 'button',
          class: 'lr-node lr-node-exam' + (ex.done ? ' is-done' : (ex.unlocked ? ' is-current' : '')) });
        if (!ex.unlocked) exNode.setAttribute('disabled', 'disabled');
        var exDot = el('span', { class: 'lr-dot lr-dot-exam' });
        if (ex.done) exDot.appendChild(checkIcon());
        else if (ex.unlocked) exDot.textContent = '?';
        else exDot.appendChild(lockIcon());
        exNode.appendChild(exDot);
        exNode.appendChild(el('span', { class: 'lr-node-label' }, t('Unit test')));
        exNode.appendChild(el('span', { class: 'lr-node-xp' }, '+' + ex.xp + ' XP'));
        exNode.addEventListener('click', function () { openExam(unit); });
        track.appendChild(exNode);
      }
      sec.appendChild(track);
      pathEl.appendChild(sec);
    });
  }

  /* ── Lesson ────────────────────────────────────────────────────────── */
  function onlyNote(card) {
    // A callout is a note on what was said, not a separate idea. A card
    // holding nothing but a callout took a whole screen for one grey
    // chip: the reader pressed "Next" for two lines.
    return !!card.note && !card.heading && !card.text && !card.image &&
           !card.code_text && !(card.list || []).length && !card.table;
  }

  function buildSteps(lesson) {
    var steps = [];
    (lesson.cards || []).forEach(function (c) {
      var last = steps.length ? steps[steps.length - 1] : null;
      if (onlyNote(c) && last && last.kind === 'card') { last.cards.push(c); return; }
      steps.push({ kind: 'card', cards: [c] });
    });
    if ((lesson.takeaways || []).length || (lesson.sources || []).length) {
      steps.push({ kind: 'recap' });
    }
    (lesson.checks || []).forEach(function (c, i) {
      steps.push({ kind: 'check', check: c, index: i, picked: null, correct: null });
    });
    if (lesson.task) steps.push({ kind: 'task', task: lesson.task, done: false });
    steps.push({ kind: 'fin' });
    return steps;
  }

  function openLesson(lesson) {
    if (!lesson.unlocked) return;
    cur = { lesson: lesson, steps: buildSteps(lesson), at: 0 };
    // Who leads this lesson: on the quiz step the character no longer speaks but
    // stays nearby and reacts to the answer. Take the last one who spoke.
    cur.castWho = null;
    (lesson.cards || []).forEach(function (cc) {
      if (cc.cast && CAST_ROLES[cc.cast.who]) cur.castWho = cc.cast.who;
    });
    document.getElementById('lr-lesson-title').textContent = lesson.title;
    var sum = document.getElementById('lr-lesson-summary');
    if (sum) sum.textContent = lesson.summary || '';
    document.getElementById('lr-lesson-meta').textContent =
      (lesson.minutes ? lesson.minutes + ' ' + t('min') + ' · ' : '') + '+' + lesson.xp + ' XP';
    pathEl.hidden = true;
    lessonEl.hidden = false;
    window.scrollTo({ top: 0, behavior: 'smooth' });
    renderStep();
  }

  function openExam(unit) {
    if (!unit.exam || !unit.exam.unlocked) return;
    fetch('/api/learn/exam/' + encodeURIComponent(unit.id))
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (lesson) {
        if (!lesson) return;
        lesson.unlocked = true;
        openLesson(lesson);
      });
  }

  function closeLesson() {
    cur = null;
    lessonEl.hidden = true;
    pathEl.hidden = false;
    renderHead();
    renderPath();
  }

  function renderStep() {
    var step = cur.steps[cur.at];
    stepsEl.innerHTML = '';
    progressEl.style.width = Math.round((cur.at + 1) * 100 / cur.steps.length) + '%';
    nextBtn.hidden = false;
    nextBtn.disabled = false;
    nextBtn.textContent = t('Next');
    hintEl.textContent = '';
    var bb = document.getElementById('lr-prev-step');
    if (bb) bb.hidden = !(cur.at > 0 && step.kind !== 'fin' &&
                          !(step.kind === 'check' && step.correct != null));

    if (step.kind === 'card') {
      step.cards.forEach(renderCard);
      hintEl.textContent = t('Enter — next, Backspace — back');
      return;
    }

    if (step.kind === 'recap') {
      var recap = el('div', { class: 'lr-recap' });
      if ((cur.lesson.takeaways || []).length) {
        recap.appendChild(el('div', { class: 'lr-card-heading' }, t('What to take away')));
        var ul = el('ul', { class: 'lr-takeaways' });
        cur.lesson.takeaways.forEach(function (x) { ul.appendChild(el('li', {}, x)); });
        recap.appendChild(ul);
      }
      if ((cur.lesson.sources || []).length) {
        recap.appendChild(el('div', { class: 'lr-card-heading' }, t('Further reading')));
        var list = el('ul', { class: 'lr-sources' });
        cur.lesson.sources.forEach(function (src) {
          var li = el('li');
          var a = el('a', { href: src.href, target: '_blank', rel: 'noopener noreferrer' },
                     src.title || src.href);
          li.appendChild(a);
          list.appendChild(li);
        });
        recap.appendChild(list);
      }
      stepsEl.appendChild(recap);
      nextBtn.textContent = t('To the questions');
      hintEl.textContent = t('Enter — next, Backspace — back');
      return;
    }

    if (step.kind === 'check') {
      var total = cur.steps.filter(function (s) { return s.kind === 'check'; }).length;
      var wrap = el('div', { class: 'lr-q' });
      var kicker = t('check {n} of {total}')
        .replace('{n}', String(step.index + 1)).replace('{total}', String(total));
      var kl = kindLabel(step.check.kind);
      if (kl) kicker += ' · ' + kl;
      if (step.check.from) kicker += ' · ' + step.check.from;
      wrap.appendChild(el('div', { class: 'lr-q-kicker' }, kicker));
      wrap.appendChild(el('p', { class: 'lr-q-text' }, step.check.q || ''));
      (step.check.options || []).forEach(function (opt, i) {
        var b = el('button', { type: 'button', class: 'lr-opt', 'aria-pressed': 'false' });
        b.appendChild(el('span', { class: 'lr-opt-key' }, String(i + 1)));
        b.appendChild(el('span', {}, opt));
        b.addEventListener('click', function () { pick(step, wrap, i); });
        wrap.appendChild(b);
      });
      var qcast = cur.castWho ? castNode({ who: cur.castWho }) : null;
      if (qcast) stepsEl.appendChild(qcast);
      stepsEl.appendChild(wrap);
      var exam = cur.lesson.mode === 'exam';
      nextBtn.textContent = exam
        ? (step.index + 1 === total ? t('Submit') : t('Next'))
        : t('Answer {ctx:2}');
      nextBtn.disabled = step.picked == null;
      hintEl.textContent = exam
        ? t('1…9 to pick, Enter to continue. The review comes after you submit.')
        : t('1…9 to pick, Enter to answer');
      return;
    }

    if (step.kind === 'task') {
      var box = el('div', { class: 'lr-task' });
      var optional = !!step.task.optional;
      box.appendChild(el('div', { class: 'lr-task-kicker' },
        optional ? t('optional practice') : t('a task on the platform')));
      box.appendChild(el('p', { class: 'lr-task-text' }, step.task.text || ''));
      var row = el('div', { class: 'lr-task-row' });
      var check = el('button', { type: 'button', class: 'btn' }, t('Check'));
      row.appendChild(check);
      if (step.task.href) {
        row.appendChild(el('a', { class: 'btn ghost', href: step.task.href, target: '_blank' },
                            t('Open the page')));
      }
      var line = el('span', { class: 'lr-task-state' }, '');
      row.appendChild(line);
      box.appendChild(row);
      if (optional) {
        box.appendChild(el('div', { class: 'lr-card-note' },
          t('You do not have the rights for this action — the chapter counts without it. Come back if you get them: the practice is worth it.')));
      }
      stepsEl.appendChild(box);
      nextBtn.disabled = !(step.done || optional);
      check.addEventListener('click', function () {
        check.disabled = true;
        line.className = 'lr-task-state';
        line.textContent = t('Validating…');
        fetch('/api/learn/task/' + encodeURIComponent(cur.lesson.id), {
          method: 'POST', headers: { 'Content-Type': 'application/json' }
        }).then(function (r) { return r.json(); }).then(function (d) {
          check.disabled = false;
          step.done = !!d.done;
          line.className = 'lr-task-state' + (d.done ? ' is-done' : '');
          line.textContent = d.done ? t('Done {ctx:2}') : (d.hint || '');
          nextBtn.disabled = !d.done;
        }).catch(function () {
          check.disabled = false;
          line.textContent = t('Could not check that — try again');
        });
      });
      return;
    }

    // Lesson summary: send the answers and show what was credited.
    var unit = unitOf(cur.lesson.id);
    var fin = el('div', { class: 'lr-fin' });
    if (unit && (unit.image || unit.image_url)) fin.appendChild(unitArt(unit, 'lr-fin-art'));
    fin.appendChild(el('h3', {}, t('Lesson complete')));
    var note = el('p', {}, '');
    fin.appendChild(note);
    stepsEl.appendChild(fin);
    nextBtn.textContent = t('Back to the path');
    nextBtn.disabled = true;
    submit(note, fin);
  }

  /* ── Case roles ────────────────────────────────────────────────────
     A card may carry a line: `cast` = {who, role, say}. Who speaks is
     decided by the course content: `role` is a ready label ("warehouse planner"),
     `who` is only a colour key. Not a single phrase or job title is
     hard-coded here, so a course in another language needs no
     code change.
     There are no drawn portraits yet: until then, a circle with an initial.
     When they appear, only this function has to change. */
  var CAST_ROLES = { head: 1, planner: 1, field: 1, analyst: 1 };

  function castNode(cast) {
    var who = cast && cast.who;
    if (!who || !CAST_ROLES[who]) return null;           // unknown key — silently skip
    var role = (cast.role || '').trim();
    var box = el('div', { class: 'lr-cast', 'data-who': who });
    // the third argument of el() is TEXT, not a node: passing an element here
    // puts "[object HTMLSpanElement]" on the screen.
    var fig = el('div', { class: 'lr-cast-fig' });
    fig.appendChild(el('span', { class: 'lr-cast-mono', 'aria-hidden': 'true' },
                       (role || who).charAt(0).toUpperCase()));
    box.appendChild(fig);
    var col = el('div', { class: 'lr-cast-col' });
    if (role) col.appendChild(el('div', { class: 'lr-cast-role' }, role));
    var say = (cast.say || '').trim();
    if (say) {
      var bubble = el('div', { class: 'lr-cast-bubble' });
      bubble.appendChild(el('p', { class: 'lr-cast-say' }, say));
      col.appendChild(bubble);
    } else {
      box.classList.add('is-quiet');
    }
    box.appendChild(col);
    return box;
  }

  /* A file from the lesson: a training export or a ready sample report.
     The server builds the URL (learn.file_url) — the name is checked against a
     whitelist, a missing file gives no button at all. `download` matters: without
     it the browser opens the html sample in a tab, and the person needs a file on disk. */
  function fileNode(f) {
    var a = el('a', { class: 'lr-file', href: f.url, download: f.name });
    a.appendChild(el('span', { class: 'lr-file-ico', 'aria-hidden': 'true' }, '\u2193'));
    a.appendChild(el('span', { class: 'lr-file-name' }, f.name));
    return a;
  }

  /* The lesson code block. Two things were missing: a "copy" button —
     people copy the prompt anyway, and selecting twenty lines with a mouse is
     awkward — and in-place editing: the person types their task and their
     columns right here and leaves with a finished prompt, not a template.
     Edits live in this browser's localStorage and are sent nowhere: the
     field ends up holding column names, i.e. company data. */
  function codeNode(card) {
    var editable = !!card.editable;
    var lines = card.code_text.split('\n').length;
    var box = el('div', { class: 'lr-code', 'data-collapsed': '1' });
    var key = 'op-learn-code:' + COURSE + ':' + (cur && cur.lesson ? cur.lesson.id : '')
            + ':' + ((cur && cur.lesson && (cur.lesson.cards || []).indexOf(card)) || 0);
    var saved = null;
    if (editable) { try { saved = localStorage.getItem(key); } catch (e) {} }

    var body;
    if (editable) {
      body = el('textarea', { class: 'lr-card-code lr-code-edit', spellcheck: 'false' });
      body.value = saved != null ? saved : card.code_text;
      body.rows = Math.min(40, body.value.split('\n').length + 1);
      body.addEventListener('input', function () {
        try { localStorage.setItem(key, body.value); } catch (e) {}
      });
    } else {
      body = el('pre', { class: 'lr-card-code' });
      body.textContent = card.code_text;
    }
    var win = el('div', { class: 'lr-code-win' });
    win.appendChild(body);

    /* The main action here is to copy, not to read forty lines. So the
       block is collapsed to one glance by default, the edge is faded with a
       gradient (it shows the text continues), and next to it we say how many
       lines there are and what to do with them. Those who want to edit expand it. */
    var bar = el('div', { class: 'lr-code-bar' });
    var more = el('button', { class: 'btn xs', type: 'button' },
                  t('Expand'));
    var count = el('span', { class: 'lr-code-hint' },
      // "Prompt" no longer fits here: the same block also shows a ready
      // chunk of data to paste into a prompt. A neutral word suits both.
      tn(lines, 'a {n}-line block|{n}-line block')
        .replace('{n}', String(lines))
      + (editable ? ' · ' + t('you can edit it right here') : ''));
    more.addEventListener('click', function () {
      var open = box.getAttribute('data-collapsed') === '1';
      box.setAttribute('data-collapsed', open ? '0' : '1');
      more.textContent = open ? t('Collapse') : t('Expand');
      if (!open) box.scrollIntoView({ block: 'nearest' });
    });

    var copy = el('button', { class: 'btn sm primary lr-code-copy', type: 'button' },
                  t('Copy'));
    copy.addEventListener('click', function () {
      var text = editable ? body.value : card.code_text;
      var done = function () {
        copy.textContent = t('Copied');
        setTimeout(function () { copy.textContent = t('Copy'); }, 1600);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, function () {});
        return;
      }
      if (editable) { body.focus(); body.select(); return; }
      var sel = window.getSelection(), r = document.createRange();
      r.selectNodeContents(body); sel.removeAllRanges(); sel.addRange(r);
    });

    bar.appendChild(copy);
    bar.appendChild(more);

    /* Edits live in the browser, and without this button a person who erased
       half the prompt would be left without the original: re-reading the lesson
       is not enough — the field remembers that same edit. The button appears
       only when the text really differs from what the course gave. */
    var reset = el('button', { class: 'btn xs ghost', type: 'button' },
                   t('Restore the original'));
    function syncReset() {
      reset.hidden = !editable || body.value === card.code_text;
    }
    reset.addEventListener('click', function () {
      body.value = card.code_text;
      body.rows = Math.min(40, card.code_text.split('\n').length + 1);
      try { localStorage.removeItem(key); } catch (e) {}
      syncReset();
    });
    if (editable) {
      body.addEventListener('input', syncReset);
      syncReset();
      bar.appendChild(reset);
    }

    /* The button sat UNDER the block and read as a caption: people saw forty
       lines of monospace and started selecting them with the mouse (owner,
       13.09). Now the action is in the header, above the text: it is seen before
       the text itself and is labelled with a word, not an icon. */
    var head = el('div', { class: 'lr-code-head' });
    // A label from the content (`code_title`) beats the default: the block shows
    // both a prompt for the model and a form for yourself — their main actions differ.
    head.appendChild(el('span', { class: 'lr-code-title' },
                        card.code_title || t('Copy this text in full')));
    head.appendChild(bar);
    box.appendChild(head);
    box.appendChild(win);

    var foot = el('div', { class: 'lr-code-foot' });
    foot.appendChild(count);
    box.appendChild(foot);
    return box;
  }

  /* The final course screen.
     The card is NOT opened here: the ceremony lives in Quests, where the
     person opens any other surprise (owner, 13.09 — the scene was first built
     right here and the screen fell apart: half a page of emptiness around the capsule).
     The workshop says what exactly was received and leads away in one click. */
  function drawCourseDone(fin, note) {
    fin.classList.add('lr-done');
    var h = fin.querySelector('h3');
    if (h) h.textContent = t('Course finished');
    note.textContent = t('That was the last chapter. Below is what you take with you.');

    fin.appendChild(el('div', { class: 'lr-done-meta' },
      tn(state.lessons_total, '{n} lesson|{n} lessons')
        .replace('{n}', String(state.lessons_total))
      + ' · ' + state.xp + ' XP · '
      + t('level “{level}”').replace('{level}', (state.level && state.level.name) || '')));

    var rw = state.reward;
    if (rw) {
      var gift = el('div', { class: 'lr-gift' });
      gift.appendChild(el('img', { class: 'lr-gift-cap', alt: '',
                                   src: '/static/img/quests/capsule.png' }));
      var txt = el('div', { class: 'lr-gift-txt' });
      txt.appendChild(el('div', { class: 'lr-gift-title' }, t('A special card is waiting for you')));
      txt.appendChild(el('div', { class: 'lr-gift-sub' },
        t('It never drops in the daily surprise and cannot be traded — it exists only for a finished course. Open it in Quests.')));
      gift.appendChild(txt);
      fin.appendChild(gift);
    }

    var row = el('div', { class: 'lr-done-row' });
    if (rw) row.appendChild(el('a', { class: 'btn primary', href: '/quests' },
                               t('Open the card')));
    row.appendChild(el('a', { class: rw ? 'btn' : 'btn primary',
                              href: '/learn/' + COURSE + '/diploma' }, t('Diploma')));
    fin.appendChild(row);
  }

  function drawArt(card) {
    stepsEl.appendChild(el('img', {
      class: 'lr-card-art', loading: 'lazy', alt: '',
      src: card.image_url || ('/static/img/docs/' + card.image + '.webp')
    }));
  }

  function renderCard(card) {
    var soft = root.getAttribute('data-style') === 'soft';
    var cast = card.cast ? castNode(card.cast) : null;
    if (cast) stepsEl.appendChild(cast);
    // An illustration without a heading opens the card; a step screenshot comes
    // after the caption and text — first what we do, then where it is on screen.
    if (soft && !card.heading && (card.image || card.image_url)) drawArt(card);
    if (card.heading) stepsEl.appendChild(el('h3', { class: 'lr-card-heading' }, card.heading));
    (card.text || '').split(/\n\s*\n/).forEach(function (para) {
      if (para.trim()) stepsEl.appendChild(el('p', { class: 'lr-card-text' }, para.trim()));
    });
    if ((card.list || []).length) {
      var ul = el('ul', { class: 'lr-card-list' });
      card.list.forEach(function (x) { ul.appendChild(el('li', {}, x)); });
      stepsEl.appendChild(ul);
    }
    if (card.table && card.table.head) {
      var box = el('div', { class: 'lr-card-tablebox' });
      var tbl = el('table', { class: 'lr-card-table' });
      var thead = el('thead'), trh = el('tr');
      card.table.head.forEach(function (h) { trh.appendChild(el('th', {}, h)); });
      thead.appendChild(trh); tbl.appendChild(thead);
      var tbody = el('tbody');
      (card.table.rows || []).forEach(function (row) {
        var tr = el('tr');
        row.forEach(function (cell) { tr.appendChild(el('td', {}, cell)); });
        tbody.appendChild(tr);
      });
      tbl.appendChild(tbody); box.appendChild(tbl); stepsEl.appendChild(box);
    }
    if (card.code_text) stepsEl.appendChild(codeNode(card));
    (card.files || (card.file_url ? [{ name: card.file, url: card.file_url }] : []))
      .forEach(function (f) { stepsEl.appendChild(fileNode(f)); });
    if ((!soft || card.heading) && (card.image || card.image_url)) drawArt(card);
    if (card.note) stepsEl.appendChild(el('div', { class: 'lr-card-note' }, card.note));
  }

  // Question type label. Keys are translated in place (t() needs a literal —
  // otherwise the extractor will not see them).
  function kindLabel(kind) {
    if (kind === 'analysis') return t('case analysis');
    if (kind === 'counterfactual') return t('what breaks');
    if (kind === 'estimate') return t('estimate');
    if (kind === 'spot-the-bug') return t('spot the bug');
    if (kind === 'design') return t('design call');
    return '';
  }

  function pick(step, wrap, i) {
    if (step.correct != null) return;              // already answered
    step.picked = i;
    var opts = wrap.querySelectorAll('.lr-opt');
    [].forEach.call(opts, function (o, k) {
      o.setAttribute('aria-pressed', k === i ? 'true' : 'false');
    });
    nextBtn.disabled = false;
  }

  function answers() {
    return cur.steps.filter(function (s) { return s.kind === 'check'; })
                    .map(function (s) { return s.picked == null ? -1 : s.picked; });
  }

  function answerStep(step) {
    // The server checks the answer: the correct option arrives with the explanation.
    nextBtn.disabled = true;
    fetch('/api/learn/check', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ lesson: cur.lesson.id, answers: answers() })
    }).then(function (r) { return r.json(); }).then(function (d) {
      var res = (d && d.results && d.results[step.index]) || null;
      if (!res) { nextBtn.disabled = false; return; }
      step.correct = res.correct;
      if (d && d.state) { state = d.state; renderHead(); }
      [].forEach.call(stepsEl.querySelectorAll('.lr-opt'), function (o, i) {
        o.setAttribute('disabled', 'disabled');
        if (i === res.answer) o.classList.add('is-right');
        else if (i === step.picked) o.classList.add('is-wrong');
      });
      var why = el('div', { class: 'lr-why' + (res.correct ? ' is-right' : '') });
      why.appendChild(el('b', {}, res.correct ? t('Right. ') : t('Not quite. ')));
      why.appendChild(document.createTextNode(res.why || ''));
      stepsEl.querySelector('.lr-q').appendChild(why);
      // The character lives through the answer with the person: glad at a right one,
      // sympathetic at a wrong one. No mockery here — see lr-cast-lean.
      var cn = stepsEl.querySelector('.lr-cast');
      if (cn) {
        cn.classList.remove('is-glad', 'is-soft');
        cn.classList.add(res.correct ? 'is-glad' : 'is-soft');
      }
      nextBtn.disabled = false;
      nextBtn.textContent = res.correct ? t('Next') : t('Try again');
      hintEl.textContent = '';
    }).catch(function () { nextBtn.disabled = false; });
  }

  function submit(note, fin) {
    fetch('/api/learn/check', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ lesson: cur.lesson.id, answers: answers() })
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (d && d.state) state = d.state;
      nextBtn.disabled = false;
      if (d && d.mode === 'exam') {
        note.textContent = t('{right} of {total} · pass mark {need}')
          .replace('{right}', String(d.right))
          .replace('{total}', String((d.results || []).length))
          .replace('{need}', String(d.need));
        fin.querySelector('h3').textContent =
          d.all_correct ? t('Unit test passed') : t('Not passed yet');
        var review = el('div', { class: 'lr-review' });
        var qs = cur.steps.filter(function (x) { return x.kind === 'check'; });
        (d.results || []).forEach(function (res, i) {
          var row = el('div', { class: 'lr-review-row' + (res.correct ? ' is-right' : ' is-wrong') });
          row.appendChild(el('div', { class: 'lr-review-q' },
            (i + 1) + '. ' + ((qs[i] && qs[i].check && qs[i].check.q) || '')));
          row.appendChild(el('div', { class: 'lr-review-why' }, res.why || ''));
          review.appendChild(row);
        });
        fin.appendChild(review);
        if (d.completed) fin.appendChild(el('div', { class: 'lr-fin-xp' }, '+' + cur.lesson.xp + ' XP'));
        renderHead();
        return;
      }
      // A lesson is closed by the ANSWER to the last question: `answerStep` sends the
      // same `/api/learn/check` with all answers. So `completed` is already
      // false here, and we must rely on the fact from the state, not on who
      // reported the news first — otherwise a person who just finished the lesson
      // reads "this lesson was already completed earlier".
      var doneNow = !!(d && (d.completed || lessonDone(cur.lesson.id)));
      if (doneNow) {
        var nextLesson = firstOpen();
        if (!nextLesson) {
          // The last lesson of the course must not look ordinary: 55 minutes
          // of work used to end with the same "Credited" as any chapter.
          drawCourseDone(fin, note);
          confetti();
          setTimeout(confetti, 700);
          renderHead();
          return;
        }
        note.textContent = t('Counted. The next lesson is open.');
        fin.appendChild(el('div', { class: 'lr-fin-xp' }, '+' + cur.lesson.xp + ' XP'));
        confetti();
        nextBtn.textContent = t('Next lesson');
        nextBtn.onclick = null;
        cur.nextLesson = nextLesson;
      } else if (d && !d.task_done) {
        note.textContent = d.task_hint || t('The task is not done yet.');
      } else {
        note.textContent = t('You had already passed this lesson.');
      }
      renderHead();
    }).catch(function () {
      nextBtn.disabled = false;
      note.textContent = t('Could not save the result — try again');
    });
  }

  function advance() {
    if (!cur) return;
    var step = cur.steps[cur.at];
    if (step.kind === 'check') {
      if (cur.lesson.mode === 'exam') {
        if (step.picked == null) return;         // review is deferred until submission
        cur.at = Math.min(cur.at + 1, cur.steps.length - 1);
        renderStep();
        return;
      }
      if (step.correct == null) { answerStep(step); return; }
      if (step.correct === false) {                 // retaking this question
        step.picked = null; step.correct = null;
        renderStep();
        return;
      }
    }
    if (step.kind === 'fin') {
      if (cur.nextLesson) { var nl = cur.nextLesson; closeLesson(); openLesson(nl); }
      else closeLesson();
      return;
    }
    cur.at = Math.min(cur.at + 1, cur.steps.length - 1);
    renderStep();
  }

  function back() {
    if (!cur || cur.at === 0) return;
    var step = cur.steps[cur.at];
    if (step.kind === 'check' && step.correct != null) return;   // already answered
    cur.at -= 1;
    renderStep();
  }

  nextBtn.addEventListener('click', advance);
  var backBtn = document.getElementById('lr-prev-step');
  if (backBtn) backBtn.addEventListener('click', back);
  document.getElementById('lr-back').addEventListener('click', closeLesson);
  document.getElementById('lr-continue').addEventListener('click', function () {
    var lesson = firstOpen() ||
      (state.units[0] && state.units[0].lessons[0]);
    if (lesson) openLesson(lesson);
  });

  // Keyboard: the course is taken in one go, hands on the keys.
  document.addEventListener('keydown', function (e) {
    if (!cur || lessonEl.hidden) return;
    var tag = (e.target && e.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA' || e.metaKey || e.ctrlKey || e.altKey) return;
    var step = cur.steps[cur.at];
    if (e.key >= '1' && e.key <= '9' && step.kind === 'check' && step.correct == null) {
      var i = parseInt(e.key, 10) - 1;
      var wrap = stepsEl.querySelector('.lr-q');
      if (wrap && wrap.querySelectorAll('.lr-opt')[i]) { pick(step, wrap, i); e.preventDefault(); }
      return;
    }
    if (e.key === 'Enter' && !nextBtn.disabled) { advance(); e.preventDefault(); }
    if (e.key === 'Backspace') { back(); e.preventDefault(); }
    if (e.key === 'Escape') { closeLesson(); e.preventDefault(); }
  });

  renderHead();
  renderPath();
})();
