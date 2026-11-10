/* OnePage — "Quests" (/quests) page logic. Ported from the accepted
 * interactive demo: capsule reveal, reward path, quests panel, quiz banner,
 * rating panel, collection, duplicate exchange, and the full-screen quiz
 * (intro → 3-2-1 → ring timer → tiles → verdict → summary).
 *
 * Server is the source of truth for everything a demo faked with
 * localStorage: `state()`/`leaderboard()` (app/services/quests.py) and
 * `quiz_quiz.status()` drive the initial render via `#q-root[data-state]`;
 * every mutation (open/exchange/opt-in/quiz) is a fetch, and the page
 * re-fetches `/api/quests/state` afterwards rather than guessing the new
 * shape locally — the server's stricks/milestones/awards logic is not
 * duplicated here.
 *
 * PURE block (top) is exported for the node harness (tests/quests_dom.js);
 * same idiom as static/js/dashboards.js:64-67 — the guard below stops the
 * file before it touches `document` when required() from node.
 */
(function () {
  'use strict';

  // ═════════════════════════════════════════════════════════════════
  // Pure helpers (exported for node tests — no DOM/window touched here)
  // ═════════════════════════════════════════════════════════════════

  // Russian plural form index: 0 = one (1, 21, 31…), 1 = few (2-4, 22-24…),
  // 2 = many (0, 5-20, 25-30…) — same mod10/mod100 rule as folder-watch.js.
  function pluralForm(n) {
    const mod10 = n % 10, mod100 = n % 100;
    if (mod10 === 1 && mod100 !== 11) return 0;
    if (mod10 >= 2 && mod10 <= 4 && !(mod100 >= 12 && mod100 <= 14)) return 1;
    return 2;
  }

  // Mirrors app/services/quests.py LEVELS — thresholds in ascending order,
  // last one at or below `n` wins.
  const LEVELS = [
    [0, t('Novice')],
    [8, t('Practitioner')],
    [18, t('Confident user')],
    [30, t('Expert')],
  ];
  function levelLabel(n) {
    const v = Number(n) || 0;
    let name = LEVELS[0][1];
    for (const [threshold, title] of LEVELS) {
      if (v >= threshold) name = title;
    }
    return name;
  }

  // "YYYY-MM-DD" -> "DD.MM"
  function shortDate(iso) {
    if (!iso) return '';
    const parts = String(iso).split('-');
    return parts.length === 3 ? parts[2] + '.' + parts[1] : String(iso);
  }

  function fmtDay(n, total) {
    return t('Day {p1} of {p2}', {p1: (Number(n) || 1), p2: (Number(total) || 15)});
  }

  // Turns state.path (server-computed: today + 6 next working days, each
  // with rare_x2/extra milestone flags) into a render-ready model — no DOM.
  // First reward-path step that is not today: entry 1 on a working day,
  // entry 0 on a weekend/holiday (the server never marks a non-working
  // today as a step). This is what "Next surprise …" names.
  function nextStep(path) {
    return (path || []).filter(function (s) { return s && !s.today; })[0] || null;
  }

  function renderPathModel(state) {
    const path = (state && state.path) || [];
    return path.map(function (step) {
      const done = !!step.done;
      const today = !!step.today;
      let tag = null;
      if (!done) {
        if (step.extra) tag = { text: t('2 surprises'), variant: 'two' };
        else if (step.rare_x2) tag = { text: t('×2 rare'), variant: null };
      }
      return {
        date: step.day,
        offset: step.offset,
        streakN: step.streak_n,
        today: today,
        done: done,
        freeze: false, // path is forward-only; freeze is a history concept the state doesn't carry
        future: !done && !today,
        gold: !!(step.rare_x2 || step.extra),
        tag: tag,
        dayLabel: today ? t('Today') : shortDate(step.day),
      };
    });
  }

  // state.chances = [{card_id, pct}], state.card_meta = {card_id: {name,...}}
  function chanceRows(state) {
    const chances = (state && state.chances) || [];
    const meta = (state && state.card_meta) || {};
    return chances.map(function (c) {
      const m = meta[c.card_id] || {};
      return { cardId: c.card_id, name: m.name || c.card_id, pct: c.pct };
    });
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { pluralForm, levelLabel, shortDate, fmtDay, renderPathModel, chanceRows, nextStep };
  }
  if (typeof document === 'undefined') return; // node require() path stops here

  // ═════════════════════════════════════════════════════════════════
  // DOM
  // ═════════════════════════════════════════════════════════════════

  const root = document.getElementById('q-root');
  if (!root) return;

  function el(tag, props, kids) {
    const n = document.createElement(tag);
    for (const k in (props || {})) {
      if (k === 'class') n.className = props[k];
      else if (k === 'text') n.textContent = props[k];
      else if (k.startsWith('data-')) n.setAttribute(k, props[k]);
      else if (k in n) n[k] = props[k];
      else n.setAttribute(k, props[k]);
    }
    (kids || []).forEach(function (c) { if (c != null) n.appendChild(c); });
    return n;
  }
  // "tomorrow" / "on Monday" from the reward path: the first step that is
  // not today — entry 1 on a working day, entry 0 on a weekend/holiday
  // (the server never flags a non-working today as a step).
  function nextDayLabel(path) {
    const nxt = nextStep(path);
    if (!nxt || !nxt.day) return t('on the next working day');
    const d = new Date(nxt.day + 'T00:00:00'), tv = new Date();
    const diff = Math.round((d - new Date(tv.getFullYear(), tv.getMonth(), tv.getDate())) / 864e5);
    if (diff === 1) return t('tomorrow');
    // The weekday is chosen by INDEX (getDay(), language-free) and the whole
    // phrase is one key per day. Russian needs the accusative ("v sredu", not
    // "v sreda"), so the old code formatted the name in ru-RU and patched three
    // of them by string match — silently a no-op as soon as the interface was
    // not Russian, and doubly wrong once the date itself came out Chinese.
    return [t('on Sunday'), t('on Monday'), t('on Tuesday'),
            t('on Wednesday'), t('on Thursday'), t('on Friday'),
            t('on Saturday')][d.getDay()];
  }
  function toast(msg, kind) {
    if (window.OP_TOAST) window.OP_TOAST(msg, kind || 'info');
  }
  function by(id) { return document.getElementById(id); }
  // The quiz intro names the player under their own avatar (demo: NAMES[0]).
  function meName() {
    const u = window.OP_USER || {};
    return u.display_name || u.person_id || t('You');
  }

  async function api(url, opts) {
    const res = await fetch(url, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts || {}));
    let body = null;
    try { body = await res.json(); } catch (e) { /* no body */ }
    if (!res.ok) {
      const err = new Error((body && body.detail) || ('HTTP ' + res.status));
      err.status = res.status;
      err.body = body;
      throw err;
    }
    return body;
  }

  let S = {};
  try { S = JSON.parse(root.dataset.state || '{}') || {}; } catch (e) { S = {}; }

  // The demo's own quest glyphs — emoji, not the sprite: the round pastel
  // icon well reads as a sticker, and a stroked sprite icon inside it does
  // not (see the demo's `.q .ic` + QUESTS[].ic).
  const QUEST_ICON = { pres: '🎞️', fav: '⭐', theme: '🌗', grid: '▦', share: '🔗' };
  const RAR_LABEL = { common: t('Common'), rare: t('Rare'), epic: t('Epic'),
                      legendary: t('Legendary'), special: t('Special') };
  /* Special cards live in their own folder: they are not part of the set of eight, and
     their files should not be mixed up with it, not even in the URL. */
  function cardImg(cardId) {
    return /^learn-/.test(cardId)
      ? '/static/img/quests/special/' + encodeURIComponent(cardId) + '.jpg'
      : '/static/img/quests/' + encodeURIComponent(cardId) + '.jpg';
  }

  // A card's image may not exist yet: a season is declared in code, and
  // the art arrives separately and later. Without this the locked shelf of a new season
  // produces a 404 per card on EVERY opening of Quests and draws the
  // system "broken image" instead of a silhouette. An empty src is not set — the browser
  // treats it as a link to the page itself and downloads it a second time.
  function cardImgNode(cardId, alt) {
    const img = el('img', { src: cardImg(cardId), alt: alt || '' });
    img.addEventListener('error', function () {
      img.removeAttribute('src');
      img.classList.add('q-art-missing');
    }, { once: true });
    return img;
  }

  // ── Hero ─────────────────────────────────────────────────────────
  function fmtLongDate(iso) {
    if (!iso) return '';
    const d = new Date(iso + 'T00:00:00');
    if (isNaN(d.getTime())) return iso;
    return OP_I18N.fmtDate(d, { weekday: 'long', day: 'numeric', month: 'long' });
  }

  function renderHero() {
    by('q-date').textContent = fmtLongDate(S.today);
    by('q-pill-pts-val').textContent = String(S.points || 0);
    const act = activeSeason();
    by('q-pill-coll-val').textContent = act ? (act.owned + '/' + act.total) : '';

    // The capsule offers today's daily chest AND every pending surprise a
    // quest / the quiz / a milestone / an exchange has already rolled but
    // nobody has opened yet — the owner's "the prize must appear and it has to be
    // clicked". One POST /api/quests/open pops one of them.
    const eggs = availableSurprises();
    const eggAvailable = eggs > 0;
    const capBtn = by('q-cap'), openBtn = by('q-open'), capImg = by('q-cap-img');
    const nextPill = by('q-nextpill'), ctaHint = by('q-ctahint'), heroSub = by('q-hero-sub');

    capBtn.classList.toggle('q-empty', !eggAvailable);
    capImg.src = eggAvailable
      ? '/static/img/quests/capsule.png'
      : '/static/img/quests/capsule_open.png';
    openBtn.disabled = !eggAvailable;
    openBtn.textContent = eggs > 1 ? t('Open surprise ({p1})', {p1: eggs}) : t('Open surprise');
    by('q-glow').style.opacity = eggAvailable ? '1' : '.25';
    ctaHint.textContent = eggAvailable ? '' : t('You can get another one for a task or quiz');

    if (eggAvailable) {
      heroSub.textContent = eggs === 1
        ? t('Inside is one of eight cards in the set')
        : tn(eggs, 'You have {n} surprise. Each contains a card from the set|You have {n} surprises. Each contains a card from the set');
      nextPill.hidden = true;
    } else if (S.working_today === false) {
      heroSub.textContent = t('It\'s a day off — the daily surprise is given on working days. Next one {p1}.', {p1: nextDayLabel(S.path)});
      nextPill.hidden = false;
      nextPill.textContent = t('Next surprise {p1}', {p1: nextDayLabel(S.path)});
    } else {
      heroSub.textContent = t('Surprise opened. Next one {p1}.', {p1: nextDayLabel(S.path)});
      nextPill.hidden = false;
      nextPill.textContent = t('Next surprise {p1}', {p1: nextDayLabel(S.path)});
    }

    // freeze note (tooltip only — no per-week bookkeeping on the client)
    by('q-freeze-note').title = S.freeze_available
      ? t('Weekends and holidays don\'t break your streak. One missed day a week is forgiven.')
      : t('Weekends and holidays don\'t break your streak. This week\'s freeze has already been used.');
  }

  // How many surprises the hero capsule can still hand out, straight from
  // the server's own state: every rolled-but-unrevealed opening (a quest, a
  // quiz, a milestone, an exchange) sits in `pending[]`, plus today's daily
  // chest while it is unopened. The owner's rule — "the prize must appear and
  // it has to be clicked" — is exactly this count being > 0.
  function availableSurprises() {
    const pending = Array.isArray(S.pending) ? S.pending.length : 0;
    // No daily chest on a weekend or holiday — working days only (owner's rule).
    const dailyLeft = (S.working_today === false || S.opened_today) ? 0 : 1;
    return pending + dailyLeft;
  }

  // ── Reward path ──────────────────────────────────────────────────
  function renderPath() {
    const streak = Number(S.streak || 0);
    by('q-streak-val').textContent = String(streak);
    by('q-streak-label').textContent = tn(streak, 'day|days');

    // The demo's second half of the streak line: what today's click buys.
    const nextEl = by('q-streak-next');
    nextEl.textContent = '';
    if (S.opened_today || S.working_today === false) {
      tinto(nextEl, 'Next surprise: {day}',
        {day: el('b', { text: nextDayLabel(S.path).replace(/^\u0432 /, '') })});  // \u0432: the Russian "v" ("on") the ru label starts with
    } else if (streak > 0) {
      tinto(nextEl, 'Open today to extend until {n}',
        {n: el('b', { text: String(streak + 1) })});
    }

    const model = renderPathModel(S);
    const wrap = by('q-days');
    wrap.textContent = '';
    model.forEach(function (step) {
      const cls = ['q-day'];
      let ck = '';
      if (step.done) { cls.push('q-done'); ck = '✓'; }
      else if (step.today) cls.push('q-today');
      else cls.push('q-future');
      if (step.gold && !step.done) cls.push('q-gold');

      const tagEl = step.tag
        ? el('span', { class: 'q-day-tag' + (step.tag.variant === 'two' ? ' q-two' : ''), text: step.tag.text })
        : el('span', {});

      wrap.appendChild(el('div', { class: cls.join(' ') }, [
        el('div', { class: 'q-day-c' }, [
          el('img', { src: '/static/img/quests/capsule.png', alt: '' }),
          el('span', { class: 'q-ck', text: ck }),
        ]),
        el('span', { class: 'q-day-d', text: step.dayLabel }),
        tagEl,
      ]));
    });
  }

  // ── Quests panel ─────────────────────────────────────────────────
  function renderQuests() {
    const quests = S.quests || [];
    const list = by('q-qlist');
    list.textContent = '';
    quests.forEach(function (q) {
      const done = !!q.done;
      // "surprise and " was a clause glued onto a plural — untranslatable on its
      // own; the two outcomes are two whole phrases instead.
      const reward = q.surprise
        ? tn(q.points, 'surprise and {n} point|surprise and {n} points')
        : tn(q.points, '{n} point|{n} points');
      const rw = el('div', { class: 'q-q-rw' }, [el('b', { text: reward })]);
      if (done) {
        rw.appendChild(el('span', { class: 'q-q-done-label', text: t('Received') }));
      } else {
        const btns = el('span', { class: 'q-qbtns' });
        const howBtn = el('button', { class: 'btn sm q-btn-ghost', type: 'button', text: t('Show as') });
        howBtn.setAttribute('data-how', q.how);
        const goBtn = el('a', { class: 'btn sm', text: t('Go'), href: q.go });
        btns.appendChild(howBtn);
        btns.appendChild(goBtn);
        rw.appendChild(btns);
      }
      const body = [el('div', { class: 'q-q-t', text: q.title })];
      if (done) body.push(el('div', { class: 'q-q-m', text: t('Completed') }));
      list.appendChild(el('div', { class: 'q-q' + (done ? ' q-q-done' : '') }, [
        el('div', { class: 'q-q-ic', text: QUEST_ICON[q.id] || '⭐' }),
        el('div', {}, body),
        rw,
      ]));
    });
    const doneCount = quests.filter(function (q) { return q.done; }).length;
    by('q-quests-done').textContent = t('{p1} of {p2}', {p1: doneCount, p2: quests.length});
    by('q-quests-bar').style.width = (quests.length ? (100 * doneCount / quests.length) : 0) + '%';
  }

  // ── Quiz banner ──────────────────────────────────────────────────
  function renderQuizBanner() {
    const quiz = S.quiz || {};
    by('q-quiz-level').textContent = levelLabel(S.quiz_correct_total) + (S.quiz_correct_total ? t(' · correct {p1}', {p1: S.quiz_correct_total}) : '');
    by('q-quiz-day').textContent = fmtDay(quiz.bank_day, 15);
    const dayIdx = Math.max(0, (Number(quiz.bank_day) || 1) - 1);
    by('q-quiz-bar').style.width = (100 * dayIdx / 15 + (quiz.played_today ? 100 / 15 : 0)) + '%';

    const row = by('q-quiz-row');
    row.textContent = '';
    if (quiz.played_today && quiz.last) {
      row.appendChild(el('span', { class: 'q-qbrow-done' }, [
        document.createTextNode(t('Played today: ')),
        el('b', { text: t('{p1} of 3, +{p2} points', {p1: quiz.last.correct, p2: quiz.last.points}) }),
      ]));
    } else if (quiz.working_today === false || S.working_today === false) {
      row.appendChild(el('span', { class: 'q-qbrow-done',
        text: t('Quiz on working days. Next {p1}', {p1: nextDayLabel(S.path)}) }));
    } else if (!quiz.available) {
      row.appendChild(el('span', { class: 'q-qbrow-done', text: t('Today\'s questions are coming soon') }));
    } else {
      const btn = el('button', { class: 'q-cta', type: 'button', id: 'q-quiz-start', text: t('Play') });
      row.appendChild(btn);
      row.appendChild(el('span', { class: 'q-qbrow-done', text: t('3 questions, a surprise for taking part') }));
    }
  }

  // ── Rating ───────────────────────────────────────────────────────
  function renderRating() {
    by('q-optin').checked = !!S.leaderboard_optin;
    const lb = S.leaderboard || {};
    const pctEl = by('q-pct'), noteEl = by('q-pct-note');
    if (lb.total) {
      pctEl.textContent = t('top {p1}%', {p1: Math.max(0, 100 - Number(lb.percentile || 0))});
      noteEl.textContent = t('{p1} points, company median {p2}', {p1: (S.points || 0), p2: (lb.median || 0)});
    } else {
      pctEl.textContent = '—';
      noteEl.textContent = t('earn points to see your place');
    }

    const board = by('q-board');
    board.textContent = '';
    if (S.leaderboard_optin && Array.isArray(lb.rows) && lb.rows.length) {
      const top3 = lb.rows.slice(0, 3);
      const podium = el('div', { class: 'q-podium' });
      top3.forEach(function (r, i) {
        podium.appendChild(el('div', { class: 'q-pod q-' + ['g', 's', 'b'][i] }, [
          el('b', { text: String(r.points) }),
          el('span', { text: r.name }),
        ]));
      });
      board.appendChild(podium);

      const rest = lb.rows.slice(3);
      if (rest.length) {
        const table = el('table', { class: 'q-table' });
        const thead = el('tr', {}, [
          el('th', { text: '#' }), el('th', { text: t('Colleague') }), el('th', { text: t('Points') }),
        ]);
        table.appendChild(thead);
        rest.forEach(function (r) {
          const dept = r.dept ? ' · ' + r.dept : '';
          table.appendChild(el('tr', { class: r.me ? 'q-me' : '' }, [
            el('td', { class: 'q-num', text: String(r.rank) }),
            el('td', { text: r.name + dept }),
            el('td', { class: 'q-num', text: String(r.points) }),
          ]));
        });
        board.appendChild(table);
      }
    }
  }

  // ── Collection ───────────────────────────────────────────────────
  /* Seasons come from the server: the "collected N of M" count is taken from the season's
     owned/total, not counted from state.cards (that holds only the active season). If
     the server for some reason did not send seasons, one season is assembled from
     cards/card_meta so the page is not left empty. */
  function seasonList() {
    if (Array.isArray(S.seasons) && S.seasons.length) return S.seasons;
    const meta = S.card_meta || {}, cards = S.cards || {};
    const ids = Object.keys(cards);
    return [{
      key: 1, title: '', active: true, companion: !!S.companion,
      total: ids.length,
      owned: ids.filter(function (k) { return (cards[k] || 0) > 0; }).length,
      cards: ids.map(function (k) {
        const m = meta[k] || {};
        return { card_id: k, rarity: m.rarity, name: m.name, desc: m.desc };
      }),
      counts: cards,
    }];
  }
  function activeSeason() {
    const list = seasonList();
    return list.filter(function (x) { return x.active; })[0] || list[0] || null;
  }

  function renderCollection() {
    const seasons = seasonList();
    const meta = S.card_meta || {};
    const act = activeSeason();
    // The ring and the heading are about the ACTIVE season: "Collection" answers the question
    // "what am I collecting now". The past season does not compete with it for the header — it
    // is collapsed below and named there, once (owner, 05.10: the name
    // stood both in the header and on the line below, and it read as a bug).
    const ring = act || seasons[0];
    if (ring) {
      by('q-ring').style.setProperty('--pct', (ring.total ? 100 * ring.owned / ring.total : 0) + '%');
      by('q-ring-t').textContent = ring.owned + '/' + ring.total;
      if (ring.title) by('q-set-title').textContent = ring.title;
    }
    const actCounts = (act && act.counts) || {};
    // The subtitle does not promise what is not there. The companion line is only for
    // the season that grants one; other seasons have nothing to promise until
    // a reward is designed, and we will not lie about a "profile frame".
    const full = act && act.owned >= act.total;
    by('q-set-sub').textContent =
      (full && act.companion) ? t('The collection is complete. Someone lives here.')
      : full ? t('The set is complete.')
      : Object.values(actCounts).some(function (v) { return v >= 5; })
        ? t('Five matching cards can be exchanged for a new surprise.')
        : (act && act.companion)
          ? t('Collect all eight and a companion appears.')
          : t('A new set: one card per working day, eight in all.');
    renderDen();

    const wrap = by('q-coll');
    wrap.textContent = '';
    const ordered = seasons.slice().sort(function (a, b) {
      return (a.active === b.active) ? b.key - a.key : (a.active ? -1 : 1);
    });
    ordered.forEach(function (se) {
      const closed = !se.active;
      const counts = se.counts || {};
      const shelf = el('div', { class: 'q-shelf' + (closed ? ' q-shelf-closed' : '') });
      shelf.setAttribute('data-season', String(se.key));
      const head = el('div', { class: 'q-shelf-head' });
      if (closed) {
        // A showcase, not a row with a button. Three separate pills — name,
        // count and "Show" — hung in the middle of an empty shelf and read as
        // clutter (owner, 05.10). Now it is ONE element: the cards stand
        // on the shelf, next to them a heading with a subtitle, a chevron on the right. The
        // whole shelf is clickable, so there is no more "button for who knows what".
        const strip = el('div', { class: 'q-shelf-mini' });
        (se.cards || []).forEach(function (c) {
          const own = (counts[c.card_id] || 0) > 0;
          const mini = el('span', { class: 'q-mini' + (own ? '' : ' q-mini-none') });
          if (own) mini.appendChild(cardImgNode(c.card_id, (meta[c.card_id] || c).name || ''));
          mini.title = (meta[c.card_id] || c).name || c.card_id;
          strip.appendChild(mini);
        });
        const done = se.owned >= se.total;
        head.appendChild(strip);
        head.appendChild(el('div', { class: 'q-shelf-ttl' }, [
          el('b', { text: se.title || '' }),
          el('small', {
            class: done ? 'is-done' : '',
            text: done
              ? t('all {p1} collected', { p1: se.total })
              : t('{p1} of {p2} · season over', { p1: se.owned, p2: se.total }),
          }),
        ]));
        head.appendChild(el('span', { class: 'q-shelf-chev', 'aria-hidden': 'true' }));
        if (done) shelf.classList.add('is-complete');
        shelf.appendChild(head);
      }
      const grid = el('div', { class: 'q-shelf-grid' });
      // A completed season is collapsed: "Collection" shows what is being collected
      // now, and the past set neither takes up the screen nor gets confused with the current one.
      // It is not hidden — a single heading row expands it again, and
      // the choice is remembered per person (owner, 05.10: "so that the fox collection
      // disappears from the general menu").
      if (closed) {
        // The whole shelf expands, not a separate button: a "Show" button
        // next to the heading did not explain what exactly it would show. The shelf is
        // the object, so that is what gets clicked. For the keyboard it is a real button by
        // role, with Enter and Space.
        const OPEN_KEY = 'op-quests-shelf-' + se.key + '-' +
                         String((window.OP_USER || {}).person_id || 'anon');
        let open = false;
        try { open = localStorage.getItem(OPEN_KEY) === '1'; } catch (e) {}
        head.setAttribute('role', 'button');
        head.setAttribute('tabindex', '0');
        function sync() {
          head.setAttribute('aria-expanded', open ? 'true' : 'false');
          head.setAttribute('aria-label', open
            ? t('Collapse the «{p1}» set', { p1: se.title || '' })
            : t('Show every card in the «{p1}» set', { p1: se.title || '' }));
          grid.hidden = !open;
          shelf.classList.toggle('is-folded', !open);
        }
        function toggle() {
          open = !open;
          try { localStorage.setItem(OPEN_KEY, open ? '1' : ''); } catch (e) {}
          sync();
        }
        head.addEventListener('click', toggle);
        head.addEventListener('keydown', function (e) {
          if (e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar') {
            e.preventDefault();
            toggle();
          }
        });
        sync();
      }
      (se.cards || []).forEach(function (c) {
        const cardId = c.card_id;
        const m = meta[cardId] || c;
        const have = counts[cardId] || 0;
        const own = have > 0;
        const rarity = m.rarity || c.rarity;
        const rarClass = rarity ? 'q-r-' + rarity : '';
        const inner = el('div', { class: 'q-cc-in' }, [
          cardImgNode(cardId),
        ]);
        // A closed season has no lock-promise: a missing card is just a silhouette.
        if (!own && !closed) inner.appendChild(el('span', { class: 'q-cc-lock', text: '🔒' }));
        if (have > 1) inner.appendChild(el('span', { class: 'q-cc-cnt', text: '×' + have }));
        inner.appendChild(el('div', { class: 'q-cc-nm' }, [
          document.createTextNode(m.name || c.name || cardId),
          el('small', { text: RAR_LABEL[rarity] || rarity || '' }),
        ]));
        const cc = el('div', { class: 'q-cc ' + rarClass + (own ? '' : ' q-none') + (!own && closed ? ' q-gone' : '') }, [inner]);

        const ciKids = [cc];
        // Trading is only in the active season: the server answers 400 for a closed one.
        if (have >= 5 && !closed) {
          const xch = el('button', { class: 'q-xch', type: 'button', text: t('Exchange 5 for a surprise') });
          xch.setAttribute('data-xch', cardId);
          ciKids.push(xch);
        }
        grid.appendChild(el('div', { class: 'q-cc-wrap' }, ciKids));
      });
      shelf.appendChild(grid);
      wrap.appendChild(shelf);
    });
    renderSpecial();
  }

  /* Special cards — for completed Practicum courses.
     A separate shelf, not a ninth and tenth slot of the set: they do not drop,
     cannot be traded, have no rarity and do not count towards 8/8. The shelf is always
     visible: a locked card is an invitation to the Practicum, not an empty slot. */
  function renderSpecial() {
    const list = S.special || [];
    const host = by('q-collection');
    let box = by('q-special');
    if (!list.length) { if (box) box.remove(); return; }
    if (!box) {
      box = el('div', { id: 'q-special', class: 'q-special' });
      host.appendChild(box);
    }
    box.textContent = '';
    box.appendChild(el('div', { class: 'q-special-head' }, [
      document.createTextNode(t('Special {ctx:2}')),
      el('small', { text: t('for finished School courses — outside the set of eight') }),
    ]));
    const row = el('div', { class: 'q-special-row' });
    list.forEach(function (sp) {
      const inner = el('div', { class: 'q-cc-in' }, [el('img', { src: sp.art, alt: '' })]);
      if (!sp.owned) inner.appendChild(el('span', { class: 'q-cc-lock', text: '🔒' }));
      inner.appendChild(el('div', { class: 'q-cc-nm' }, [
        document.createTextNode(sp.name),
        el('small', { text: sp.owned ? t('earned') : t('for a course') }),
      ]));
      const cc = el('div', { class: 'q-cc q-cc-special' + (sp.owned ? '' : ' q-none') }, [inner]);
      const kids = [cc];
      if (!sp.owned) {
        const go = el('a', { class: 'q-xch', href: '/learn/' + sp.course });
        go.textContent = t('Take the course');
        kids.push(go);
      }
      row.appendChild(el('div', { class: 'q-cc-wrap' }, kids));
    });
    box.appendChild(row);
  }

  // ── ADR-17b "Companion": the den in the Collection block ────────────
  // Shown only when the server says the collection is complete
  // (S.companion). Two switches live here and nowhere else: show the
  // creature on workspace pages, use the autumn theme. No text beyond the
  // labels — the creature itself never speaks.
  const COMP_INTRO_KEY = 'op-companion-intro-' + String((window.OP_USER || {}).person_id || 'anon');
  const COMP_AUTUMN_KEY = 'op-companion-autumn-applied-' + String((window.OP_USER || {}).person_id || 'anon');
  const COMP_THEME_BEFORE_KEY = 'op-companion-theme-before-' + String((window.OP_USER || {}).person_id || 'anon');
  function companionImg(pose) { return '/static/img/quests/companion/' + pose + '.png'; }

  function renderDen() {
    // The block's host is its own row under the shelves. The old place (the set header)
    // is kept as a fallback in case of an old template.
    const head = by('q-den-host') || document.querySelector('#q-collection .q-sethead');
    if (!head) return;
    let den = by('q-den');
    if (!S.companion) { if (den) den.remove(); return; }
    if (!den) {
      den = el('div', { id: 'q-den', class: 'q-den' });
      head.appendChild(den);
    }
    den.textContent = '';
    // The caption is mandatory: without it the two switches hang next to the
    // current season's set and look like its settings.
    const seasons0 = seasonList();
    const compSeason = seasons0.filter(function (x) { return x.companion; })[0];
    den.appendChild(el('div', { class: 'q-den-cap' }, [
      el('b', { text: t('Companion') }),
      el('small', {
        text: compSeason
          ? t('reward for the «{p1}» set', { p1: compSeason.title })
          : t('reward for the full collection'),
      }),
    ]));
    const nook = el('div', { class: 'q-den-nook', title: t('Den') }, [
      el('img', { src: companionImg('peek'), alt: '' }),
    ]);
    const sw = el('div', { class: 'q-den-sw' });
    function mkSwitch(id, label, on, onChange) {
      const inp = el('input', { type: 'checkbox', id: id });
      inp.checked = !!on;
      inp.addEventListener('change', function () { onChange(inp.checked, inp); });
      return el('label', { class: 'q-den-row', for: id }, [inp, el('span', { text: label })]);
    }
    sw.appendChild(mkSwitch('q-den-on', t('Show the companion'), S.companion.enabled !== false, async function (on, inp) {
      inp.disabled = true;
      try {
        const r = await api('/api/quests/companion', { method: 'PUT', body: JSON.stringify({ enabled: on }) });
        S.companion = r.companion || S.companion;
        syncBadgeCache();
      } catch (e) { inp.checked = !on; toast(t('Could not save'), 'error'); }
      inp.disabled = false;
    }));
    sw.appendChild(mkSwitch('q-den-autumn', t('OnePage autumn theme'), !!S.companion.autumn, async function (on, inp) {
      inp.disabled = true;
      try {
        const r = await api('/api/quests/companion', { method: 'PUT', body: JSON.stringify({ autumn: on }) });
        S.companion = r.companion || S.companion;
        // Switching on remembers the theme it replaces; switching off puts
        // that one back — a dark-theme person is not dropped into light.
        if (window.OP_UI) {
          const cur = window.OP_UI.get('theme') || 'light';
          if (on) {
            try { localStorage.setItem(COMP_THEME_BEFORE_KEY, cur === 'autumn' ? 'light' : cur); } catch (e2) {}
            window.OP_UI.set('theme', 'autumn');
          } else {
            let back = 'light';
            try { back = localStorage.getItem(COMP_THEME_BEFORE_KEY) || 'light'; } catch (e2) {}
            window.OP_UI.set('theme', back === 'autumn' ? 'light' : back);
          }
        }
        try { localStorage.setItem(COMP_AUTUMN_KEY, on ? '1' : ''); } catch (e2) {}
        syncBadgeCache();
      } catch (e) { inp.checked = !on; toast(t('Could not save'), 'error'); }
      inp.disabled = false;
    }));
    den.appendChild(nook);
    den.appendChild(sw);
  }

  // Another device: the profile says autumn, this browser has not applied
  // it yet — apply once. A later manual theme change on this device wins
  // (the flag stays set), so the perk never fights the topbar toggle.
  function syncAutumnTheme() {
    if (!S.companion || !S.companion.autumn || !window.OP_UI) return;
    try {
      if (localStorage.getItem(COMP_AUTUMN_KEY)) return;
      localStorage.setItem(COMP_AUTUMN_KEY, '1');
    } catch (e) { return; }
    window.OP_UI.set('theme', 'autumn');
  }

  // The intro — once, on the visit where the collection became complete.
  // Three acts, no «congratulations»: (1) the page sinks into dusk, golden
  // sparks drift, the scene fades in with a slow push-in; (2) two lines,
  // one after the other; (3) an «unlocked» panel rises — the companion and
  // the autumn theme, with a button that switches the theme right here.
  // Waits for a reveal in progress (the 8th card's own scene comes first).
  let introRetries = 0;
  function maybeCompanionIntro() {
    if (!S.companion) return;
    if (document.querySelector('.q-cintro')) return;           // already on screen
    // The flag records WHICH reveal was already watched, not merely that one
    // was: re-stamping the collection (a reset on a stand, a re-grant) then
    // replays the moment without anyone having to clear the browser.
    const introStamp = String(S.companion.since || '1');
    try { if (localStorage.getItem(COMP_INTRO_KEY) === introStamp) return; } catch (e) {}
    // Only within a week of the reveal: a cleared browser months later must
    // not replay the moment the collection became complete.
    if (S.companion.since) {
      const ms = Date.parse(S.companion.since + (S.companion.since.indexOf('Z') < 0 ? 'Z' : ''));
      if (isFinite(ms) && Date.now() - ms > 7 * 86400000) {
        try { localStorage.setItem(COMP_INTRO_KEY, introStamp); } catch (e) {}
        return;
      }
    }
    // The card lands in the collection when the reward is GRANTED, before
    // its capsule is opened — so wait until every pending surprise has been
    // revealed (refreshState() after the reveal chain calls back here).
    if (S.pending && S.pending.length) return;
    if (!overlay.hidden) { if (introRetries++ < 20) setTimeout(maybeCompanionIntro, 1500); return; }
    try { localStorage.setItem(COMP_INTRO_KEY, introStamp); } catch (e) {}
    playCompanionIntro();
  }

  function playCompanionIntro() {
    const reduced = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
    const sparks = el('canvas', { class: 'q-cintro-sparks' });
    const pic = el('div', { class: 'q-cintro-pic' }, [el('img', { src: '/static/img/quests/companion/intro.jpg', alt: '' })]);
    const line1 = el('div', { class: 'q-cintro-line', text: t('Something was hiding here.') });
    const line2 = el('div', { class: 'q-cintro-sub', text: t('Now it lives in OnePage. It will drop by now and then.') });
    const panel = el('div', { class: 'q-cintro-panel' }, [
      el('div', { class: 'q-cintro-eyebrow', text: t('The full collection unlocked') }),
      el('div', { class: 'q-cintro-cards' }, [
        el('div', { class: 'q-cintro-card' }, [
          el('img', { src: companionImg('peek'), alt: '' }),
          el('div', {}, [
            el('b', { text: t('Companion') }),
            el('span', { text: t('Lives in the workspace, shows up rarely and silently. Switched off in the Collection.') }),
          ]),
        ]),
        el('div', { class: 'q-cintro-card q-cintro-card-autumn' }, [
          el('div', { class: 'q-cintro-leaf', text: '🍂' }),
          el('div', {}, [
            el('b', { text: t('Autumn theme') }),
            el('span', { text: t('The OnePage autumn theme is yours — already on: warm paper, ochre, soft shadows.') }),
          ]),
        ]),
      ]),
      el('div', { class: 'q-cintro-actions' }, [
        (function () {
          // Plainly the way out. The autumn theme is already on and STAYS on:
          // this button only closes the scene (owner, 2026-09-12: "I worry
          // they won't find where to click… let the autumn theme stay
          // by default").
          const b = el('button', { class: 'q-cta big', type: 'button', text: t('Got it') });
          b.addEventListener('click', function (ev) { ev.stopPropagation(); end(); });
          return b;
        })(),
        (function () {
          const b = el('button', { class: 'q-cintro-later', type: 'button', text: t('Restore the previous theme') });
          b.addEventListener('click', async function (ev) {
            ev.stopPropagation();
            b.disabled = true;
            await setAutumn(false);
            end();
          });
          return b;
        })(),
      ]),
    ]);
    const closeX = el('button', { class: 'q-cintro-x', type: 'button', 'aria-label': t('Close'), text: '×' });
    const layer = el('div', { class: 'q-cintro', role: 'dialog', 'aria-label': t('Something was hiding here.') }, [sparks, pic, line1, line2, panel, closeX]);
    document.body.appendChild(layer);
    closeX.addEventListener('click', function (ev) { ev.stopPropagation(); end(); });
    // Anywhere off the picture and the panel closes it too — the usual way out
    // of a full-screen scene, and the one people try first.
    layer.addEventListener('click', function (ev) {
      if (panel.contains(ev.target) || pic.contains(ev.target)) return;
      end();
    });

    // Golden sparks: a few dozen slow particles, cheap, stopped with the layer.
    let raf = null;
    if (!reduced) {
      const cx = sparks.getContext('2d');
      const P = [];
      function size() { sparks.width = window.innerWidth; sparks.height = window.innerHeight; }
      size();
      for (let i = 0; i < 46; i++) {
        P.push({ x: Math.random() * sparks.width, y: Math.random() * sparks.height,
                 r: 0.8 + Math.random() * 2.2, vy: -(0.08 + Math.random() * 0.25), vx: (Math.random() - 0.5) * 0.12,
                 a: Math.random() * Math.PI * 2, s: 0.004 + Math.random() * 0.01 });
      }
      (function tick() {
        cx.clearRect(0, 0, sparks.width, sparks.height);
        for (const q of P) {
          q.y += q.vy; q.x += q.vx; q.a += q.s;
          if (q.y < -6) { q.y = sparks.height + 6; q.x = Math.random() * sparks.width; }
          const al = 0.25 + 0.55 * (0.5 + 0.5 * Math.sin(q.a));
          cx.beginPath(); cx.arc(q.x, q.y, q.r, 0, Math.PI * 2);
          cx.fillStyle = 'rgba(255, 214, 140, ' + al.toFixed(3) + ')';
          cx.shadowColor = 'rgba(255, 200, 110, .8)'; cx.shadowBlur = 8;
          cx.fill();
        }
        raf = requestAnimationFrame(tick);
      })();
      window.addEventListener('resize', size);
      layer._sizeHandler = size;
    }

    let done = false;
    function end() {
      if (done) return; done = true;
      layer.classList.remove('is-on');
      document.removeEventListener('keydown', onKey, true);
      if (raf) cancelAnimationFrame(raf);
      if (layer._sizeHandler) window.removeEventListener('resize', layer._sizeHandler);
      setTimeout(function () { layer.remove(); renderDen(); }, 800);
    }
    function onKey(e) { if (e.key === 'Escape') { e.preventDefault(); end(); } }
    document.addEventListener('keydown', onKey, true);
    // The winner gets the theme switched for them (owner 2026-09-11): the page
    // behind the scene warms up over ~1.2 s as the panel rises. "Restore
    // previous theme" undoes it in place; the switch in the den does later.
    async function setAutumn(on) {
      try {
        const r = await api('/api/quests/companion', { method: 'PUT', body: JSON.stringify({ autumn: on }) });
        S.companion = r.companion || S.companion;
      } catch (e) { toast(t('Could not save'), 'error'); return; }
      if (!window.OP_UI) return;
      const cur = window.OP_UI.get('theme') || 'light';
      document.documentElement.classList.add('q-theme-swap');
      if (on) {
        try { if (cur !== 'autumn') localStorage.setItem(COMP_THEME_BEFORE_KEY, cur); localStorage.setItem(COMP_AUTUMN_KEY, '1'); } catch (e2) {}
        window.OP_UI.set('theme', 'autumn');
      } else {
        let back = 'light';
        try { back = localStorage.getItem(COMP_THEME_BEFORE_KEY) || 'light'; } catch (e2) {}
        window.OP_UI.set('theme', back === 'autumn' ? 'light' : back);
      }
      setTimeout(function () { document.documentElement.classList.remove('q-theme-swap'); }, 1400);
      syncBadgeCache();
    }
    // Acts: dusk (0) → picture (0.3 s) → line 1 (1.6 s) → line 2 (3.2 s) →
    // panel + the theme switches (5 s, with a gold burst)
    requestAnimationFrame(function () { requestAnimationFrame(function () { layer.classList.add('is-on'); }); });
    setTimeout(function () { layer.classList.add('is-l1'); }, 1600);
    setTimeout(function () { layer.classList.add('is-l2'); }, 3200);
    setTimeout(function () {
      layer.classList.add('is-panel');
      if (!reduced) {
        const r = panel.getBoundingClientRect();
        confetti(260, r.left + r.width / 2, r.top + 10, true);
      }
      setAutumn(true);
    }, 5000);
  }

  function renderAll() {
    renderHero();
    renderPath();
    renderQuests();
    renderQuizBanner();
    renderRating();
    renderCollection();
    syncBadgeCache();
    syncAutumnTheme();
    setTimeout(maybeCompanionIntro, 2500);
  }
  // Keep the cross-page capsule (static/js/quests.js) honest: it caches
  // «nothing to open today» for the whole day, so the one page that knows
  // the truth rewrites that cache after every render.
  function syncBadgeCache() {
    try {
      const uid = String((window.OP_USER || {}).person_id || 'anon');
      localStorage.setItem('op-quests-badge-' + uid, JSON.stringify({
        date: S.today, open: availableSurprises() <= 0, checkedAt: Date.now(),
      }));
      // ADR-17b: the same cache the badge response on the dashboards list writes.
      // The record format is one and lives in quests.js — a second copy here once
      // drifted from the first (it had no checkedAt) and cost a day of searching.
      if (typeof window.OP_QUESTS_SAVE_COMPANION === 'function') {
        window.OP_QUESTS_SAVE_COMPANION(
          (S.companion && S.companion.seed) ? S.companion : null, S.today);
      }
      // The cross-page script remembers «sent» per client event; if the server
      // does not count the quest as done (reset, another browser earlier), let
      // it send again — the server-side UNIQUE keeps it idempotent anyway.
      const EV = { theme: 'theme', grid: 'view' };
      (S.quests || []).forEach(function (q) {
        if (EV[q.id] && !q.done) localStorage.removeItem('op-quests-ev-' + EV[q.id] + '-' + uid);
      });
    } catch (e) { /* ignore */ }
  }

  // ── State refresh ────────────────────────────────────────────────
  async function refreshState() {
    try {
      const fresh = await api('/api/quests/state');
      S = Object.assign({}, S, fresh);
      renderAll();
    } catch (e) {
      toast(t('Couldn\'t update the quest status'), 'error');
    }
  }

  // ── Confetti ─────────────────────────────────────────────────────
  const confetti = (function () {
    const cv = by('q-confetti');
    const cx = cv.getContext('2d');
    let particles = [], raf = null;
    function step() {
      cx.clearRect(0, 0, cv.width, cv.height);
      particles = particles.filter(function (p) { return p.l > 0; });
      particles.forEach(function (p) {
        p.vy += 0.28; p.x += p.vx; p.y += p.vy; p.vx *= 0.985; p.r += p.vr; p.l -= p.d;
        cx.save();
        cx.globalAlpha = Math.max(0, Math.min(1, p.l * 1.5));
        cx.translate(p.x, p.y); cx.rotate(p.r);
        cx.fillStyle = p.c;
        cx.fillRect(-p.s / 2, -p.s / 2, p.s, p.s * 0.55);
        cx.restore();
      });
      raf = particles.length ? requestAnimationFrame(step) : null;
    }
    return function burst(n, x, y, gold) {
      cv.width = innerWidth; cv.height = innerHeight;
      const cols = gold ? ['#f2c14e', '#fff2b0', '#ffd166', '#2ec4b6', '#fff'] : ['#2ec4b6', '#f2c14e', '#ff7b7b', '#7fb6f0', '#fff'];
      for (let i = 0; i < n; i++) {
        const a = -Math.PI / 2 + (Math.random() - 0.5) * 1.6, v = 7 + Math.random() * 13;
        particles.push({
          x: x, y: y, vx: Math.cos(a) * v, vy: Math.sin(a) * v, r: Math.random() * Math.PI,
          vr: (Math.random() - 0.5) * 0.3, s: 4 + Math.random() * 6, c: cols[i % cols.length],
          l: 1, d: 0.006 + Math.random() * 0.006,
        });
      }
      if (!raf) raf = requestAnimationFrame(step);
    };
  })();

  // ── Reveal overlay ───────────────────────────────────────────────
  const overlay = by('q-overlay'), ovContent = by('q-ov-content');
  overlay.setAttribute('role', 'dialog');
  overlay.setAttribute('aria-modal', 'true');

  let overlayReturnFocus = null;
  // True from the moment a quiz question's clock starts (server-authoritative
  // — see quizRound) until it's answered. Gates the confirm on Escape/✕.
  let quizInProgress = false;

  function overlayFocusables() {
    if (!overlay.querySelectorAll) return [];
    return Array.prototype.filter.call(
      overlay.querySelectorAll('button, a[href], input, [tabindex]:not([tabindex="-1"])'),
      function (e) { return !e.disabled && e.offsetParent !== null; }
    );
  }
  function focusOverlayFirst() {
    const items = overlayFocusables();
    const target = items.length ? items[0] : by('q-ov-close');
    if (target && target.focus) target.focus();
  }
  // Minimal Tab trap: keep focus cycling inside the overlay while it's open.
  overlay.addEventListener('keydown', function (e) {
    if (e.key !== 'Tab' || overlay.hidden) return;
    const items = overlayFocusables();
    if (!items.length) return;
    const first = items[0], last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });

  function openOverlay() {
    overlayReturnFocus = document.activeElement;
    overlay.hidden = false;
    overlay.classList.add('q-visible');
  }
  function closeOverlay() {
    overlay.hidden = true;
    overlay.classList.remove('q-visible', 'q-lit', 'q-flash-on');
    ovContent.textContent = '';
    if (overlayReturnFocus && overlayReturnFocus.focus && document.contains(overlayReturnFocus)) {
      overlayReturnFocus.focus();
    }
    overlayReturnFocus = null;
  }
  // Gate used by the ✕ button and Escape: mid-question, closing the overlay
  // does NOT stop the server's clock (quiz timing is server-side end to
  // end — see the quiz section below), so make that explicit before letting
  // the visual close it. Reopening (the quiz banner's "Play" button, which
  // calls quizStart() again) resumes the same in-flight question — the
  // server reports `served: true` and the client skips straight back to it.
  function requestCloseOverlay() {
    if (quizInProgress) {
      const ok = (typeof window.confirm === 'function')
        ? window.confirm(t('The timer keeps running on the server. Close the quiz?'))
        : true;
      if (!ok) return;
    }
    clearQuizTimers();
    quizInProgress = false;
    closeOverlay();
  }
  by('q-ov-close').addEventListener('click', requestCloseOverlay);

  function playReveal(opening, onDone) {
    if (!opening) { if (onDone) onDone(); return; }
    const meta = (S.card_meta || {})[opening.card_id] || {};
    const name = opening.name || meta.name || opening.card_id;
    const desc = opening.desc || meta.desc || '';
    const rarity = opening.rarity || meta.rarity || 'common';

    openOverlay();
    ovContent.textContent = '';
    const stage = el('div', { class: 'q-rv' });
    const stageInner = el('div', { class: 'q-rv-stage' });
    const capo = el('img', { class: 'q-capo', src: '/static/img/quests/capsule.png', alt: '' });
    const rcard = el('div', { class: 'q-rcard' });
    const ccInner = el('div', { class: 'q-cc-in' }, [cardImgNode(opening.card_id)]);
    // Flying card carries no name plate and no ×N badge — the name, rarity
    // and duplicate count are spelled out under it (demo: `noname`/`nocount`).
    const cc = el('div', { class: 'q-cc q-r-' + rarity }, [ccInner, el('div', { class: 'q-shine' })]);
    rcard.appendChild(cc);
    stageInner.appendChild(capo);
    stageInner.appendChild(rcard);

    const txt = el('div', { class: 'q-rv-txt' }, [
      el('span', { class: 'q-rar q-' + rarity, text: RAR_LABEL[rarity] || rarity }),
      el('h3', { text: name }),
      el('p', { text: desc }),
      el('div', { class: 'q-rv-dup' }),
      el('button', { class: 'q-cta', type: 'button', text: t('To collection') }),
    ]);
    stage.appendChild(stageInner);
    stage.appendChild(txt);
    ovContent.appendChild(stage);
    focusOverlayFirst();

    overlay.classList.add('q-lit');
    capo.classList.add('q-shake');
    setTimeout(function () {
      capo.classList.remove('q-shake');
      overlay.classList.add('q-flash-on');
      capo.src = '/static/img/quests/capsule_open.png';
      capo.classList.add('q-pop');
      const r = capo.getBoundingClientRect();
      confetti(rarity === 'common' ? 140 : rarity === 'legendary' ? 420 : 260, r.left + r.width / 2, r.top + r.height * 0.45, rarity === 'legendary');
    }, 700);
    setTimeout(function () {
      capo.classList.add('q-away');
      rcard.classList.add('q-fly');
    }, 1250);
    setTimeout(function () {
      txt.classList.add('q-on');
      const dupEl = stage.querySelector('.q-rv-dup');
      const have = (S.cards && S.cards[opening.card_id]) || 0;
      const inActive = !!(S.cards && opening.card_id in S.cards);
      dupEl.textContent = have > 1 && inActive
        ? t('Already {p1} of them. Five matching cards can be exchanged for a new surprise.', {p1: have})
        : t('New card in your collection');
    }, 2500);

    txt.querySelector('.q-cta').addEventListener('click', function () {
      closeOverlay();
      if (onDone) onDone();
    });
  }

  function playRevealChain(openings) {
    const queue = (openings || []).filter(Boolean).slice();
    (function next() {
      const o = queue.shift();
      if (!o) { refreshState(); return; }
      playReveal(o, next);
    })();
  }

  // ── Open daily chest ─────────────────────────────────────────────
  let opening = false;
  async function openChest() {
    if (opening) return;
    if (availableSurprises() <= 0) {
      toast(t('No surprises for today. Complete a task or play a quiz.'));
      return;
    }
    opening = true;
    const cap = by('q-cap');
    cap.classList.add('q-shake');
    by('q-glow').style.transform = 'scale(1.4)';
    setTimeout(async function () {
      cap.classList.remove('q-shake');
      by('q-glow').style.transform = '';
      try {
        const res = await api('/api/quests/open', { method: 'POST' });
        // `kind` says which door the server opened: the daily chest (which
        // may carry a milestone `extra_opening` — that one lands in
        // `pending` for the NEXT press, so the hero keeps offering it), or
        // one already-pending surprise. Both play the same reveal.
        if (res.kind === 'daily') S.opened_today = true;
        const openings = [res.opening, res.extra_opening].filter(Boolean);
        playRevealChain(openings);
      } catch (e) {
        if (e.status === 409) toast(t('No more surprises for today'));
        else toast(t('Couldn\'t open the surprise'), 'error');
        refreshState();
      } finally {
        opening = false;
      }
    }, 900);
  }
  by('q-cap').addEventListener('click', openChest);
  by('q-open').addEventListener('click', openChest);

  // ── Quests panel actions ─────────────────────────────────────────
  by('q-qlist').addEventListener('click', function (e) {
    const how = e.target.closest && e.target.closest('[data-how]');
    if (how) {
      // Phase 7 (quests-how.js) binds this event; nothing to do here beyond
      // letting the click bubble/be re-observed by that script.
      return;
    }
  });

  // ── Exchange duplicates ──────────────────────────────────────────
  by('q-coll').addEventListener('click', async function (e) {
    const btn = e.target.closest && e.target.closest('[data-xch]');
    if (!btn) return;
    const cardId = btn.getAttribute('data-xch');
    btn.disabled = true;
    try {
      await api('/api/quests/exchange', { method: 'POST', body: JSON.stringify({ card_id: cardId }) });
      // The new surprise is `pending`, not revealed: the hero capsule shows
      // it and the person opens it there — same door as every other prize.
      toast(t('Exchanged 5 duplicates for a surprise, it\'s waiting in the header'));
      await refreshState();
      by('q-hero').scrollIntoView({ behavior: 'smooth', block: 'start' });
    } catch (e2) {
      toast((e2.body && e2.body.detail) || t('Couldn\'t exchange the cards'), 'error');
      refreshState();
    } finally {
      btn.disabled = false;
    }
  });

  // ── Leaderboard opt-in ───────────────────────────────────────────
  by('q-optin').addEventListener('change', async function () {
    const on = this.checked;
    try {
      await api('/api/quests/leaderboard-optin', { method: 'PUT', body: JSON.stringify({ optin: on }) });
      S.leaderboard_optin = on;
      await refreshState();
    } catch (e) {
      this.checked = !on;
      toast(t('Couldn\'t change the leaderboard setting'), 'error');
    }
  });

  // ── Quiz ──────────────────────────────────────────────────────────
  let quizTimers = [];
  function clearQuizTimers() { quizTimers.forEach(function (tv) { clearTimeout(tv); clearInterval(tv); }); quizTimers = []; }
  function stage(nodes) {
    overlay.classList.add('q-lit');
    openOverlay();
    ovContent.textContent = '';
    const qs = el('div', { class: 'q-qs' }, nodes);
    ovContent.appendChild(qs);
    focusOverlayFirst();
    return qs;
  }

  by('q-quiz-banner').addEventListener('click', function (e) {
    if (e.target && e.target.id === 'q-quiz-start') quizStart();
  });

  // Server timing contract: /quiz/start does NOT start the clock. When
  // `served` is false the question hasn't been handed out yet — show the
  // intro + 3-2-1, THEN call /quiz/begin (which stamps `served_at` and
  // starts the server's clock) and seed the ring from ITS `remaining_sec`.
  // When `served` is true, an attempt is already mid-flight (a resume after
  // a close/reload) — the server clock never stopped, so skip straight to
  // the question, ring seeded from the start response's own `remaining_sec`.
  async function quizStart() {
    let started;
    try {
      started = await api('/api/quests/quiz/start', { method: 'POST' });
    } catch (e) {
      toast((e.body && e.body.detail) || t('Couldn\'t start the quiz'), 'error');
      return;
    }
    if (started && started.done) { quizEnd(started.summary); return; }
    const ctx = { token: started.token, total: started.total || 3 };
    if (started.served) {
      quizInProgress = true;
      quizRound(ctx, started.index || 0, started.score || { me: 0, bot: 0 }, started.question, started.remaining_sec);
    } else {
      quizIntro(ctx);
    }
  }

  function quizIntro(ctx) {
    quizInProgress = false;
    stage([
      el('div', { class: 'q-qintro' }, [
        el('div', { class: 'q-qeyebrow', text: t('Daily quiz') }),
        el('h2', { class: 'q-qtitle' }, [
          document.createTextNode(t('Three work situations.')),
          el('br'),
          document.createTextNode(t('45 to 90 seconds per question, depending on its length.')),
        ]),
        el('div', { class: 'q-qvs' }, [
          el('div', { class: 'q-qp' }, [el('div', { class: 'q-qav', text: t('You') }), el('span', { text: meName() })]),
          el('div', { class: 'q-qx', text: t('versus') }),
          el('div', { class: 'q-qp' }, [el('div', { class: 'q-qav q-them', text: t('To') }), el('span', { text: t('Colleagues yesterday') })]),
        ]),
        el('p', { class: 'q-qhint', text: t('10 points for a correct answer, plus 5 more if you answer in under half the allotted time. You get a surprise just for taking part.') }),
        (function () {
          const btn = el('button', { class: 'q-cta q-cta-big', type: 'button', text: t('Let\'s go') });
          btn.classList.add('big');
          btn.addEventListener('click', function () { quizCountdown(ctx); });
          return btn;
        })(),
      ]),
    ]);
  }

  function quizCountdown(ctx) {
    clearQuizTimers();
    let n = 3;
    const box = stage([el('div', { class: 'q-qcount', text: '3' })]);
    const tv = setInterval(function () {
      n--;
      if (n <= 0) {
        clearInterval(tv);
        quizBegin(ctx);
        return;
      }
      const c = box.querySelector('.q-qcount');
      if (c) c.textContent = String(n);
    }, 700);
    quizTimers.push(tv);
  }

  // Starts the server's clock for the CURRENT question — question 0 after
  // the intro, questions 1 and 2 when the person clicks "Next" under the
  // verdict. Until 2026-09-10 /quiz/answer stamped the next question's
  // served_at while its explanation was still on screen, so reading it ate
  // the next question's time. /quiz/begin is idempotent and picks the
  // current index itself, so a reload lands on the same clock.
  async function quizBegin(ctx) {
    let begun;
    try {
      begun = await api('/api/quests/quiz/begin', { method: 'POST', body: JSON.stringify({ token: ctx.token }) });
    } catch (e) {
      toast((e.body && e.body.detail) || t('Couldn\'t start the question'), 'error');
      closeOverlay();
      refreshState();
      return;
    }
    quizInProgress = true;
    quizRound(ctx, begun.index || 0, begun.score || { me: 0, bot: 0 }, begun.question, begun.remaining_sec);
  }

  function quizRound(ctx, index, score, question, remainingSec) {
    clearQuizTimers();
    const total = ctx.total || 3;
    let answered = false;
    // Server-authoritative: the ring counts down from what the server says
    // is left (`remaining_sec`), never from the question's nominal `sec` —
    // a resumed/late-served question may have less than the full window.
    const T = (typeof remainingSec === 'number' && remainingSec >= 0) ? remainingSec : (question.sec || 45);
    const t0 = Date.now();
    const dots = [];
    for (let d = 0; d < total; d++) dots.push(el('i', { class: d < index ? 'q-done' : (d === index ? 'q-cur' : '') }));

    const ring = el('div', { class: 'q-qring' }, [
      (function () {
        const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('viewBox', '0 0 120 120');
        const bg = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        bg.setAttribute('class', 'q-bg'); bg.setAttribute('cx', '60'); bg.setAttribute('cy', '60'); bg.setAttribute('r', '54');
        const fg = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        fg.setAttribute('class', 'q-fg'); fg.setAttribute('cx', '60'); fg.setAttribute('cy', '60'); fg.setAttribute('r', '54');
        fg.id = 'q-ring-fg';
        svg.appendChild(bg); svg.appendChild(fg);
        return svg;
      })(),
      el('div', { class: 'q-qnum', id: 'q-quiz-num', text: String(Math.ceil(T)) }),
    ]);

    const tiles = el('div', { class: 'q-qtiles' });
    (question.o || []).forEach(function (opt, i) {
      const btn = el('button', { class: 'q-qt', type: 'button' }, [
        el('span', { class: 'q-k', text: 'ABCD'[i] }),
        el('span', { class: 'q-lbl', text: opt }),
        el('span', { class: 'q-mark', id: 'q-mk-' + i }),
      ]);
      btn.setAttribute('data-i', String(i));
      btn.addEventListener('click', function () { finish(i); });
      tiles.appendChild(btn);
    });

    const foot = el('div', { class: 'q-qfoot', id: 'q-quiz-foot' });

    stage([
      el('div', { class: 'q-qtop' }, [
        el('div', { class: 'q-qdots' }, dots),
        el('div', { class: 'q-qscore' }, [
          el('span', {}, [document.createTextNode(t('You ')), el('b', { id: 'q-sc-me', text: String(score.me) })]),
          el('span', { class: 'q-sep', text: ':' }),
          el('span', {}, [el('b', { id: 'q-sc-bot', text: String(score.bot) }), document.createTextNode(t(' Colleagues'))]),
        ]),
      ]),
      ring,
      el('h2', { class: 'q-qq2', text: question.q }),
      tiles,
      foot,
    ]);

    const fg = by('q-ring-fg');
    requestAnimationFrame(function () {
      fg.style.transition = 'stroke-dashoffset ' + T + 's linear';
      fg.style.strokeDashoffset = '339';
    });
    const tick = setInterval(function () {
      const left = Math.max(0, T - (Date.now() - t0) / 1000);
      const numEl = by('q-quiz-num');
      if (numEl) {
        numEl.textContent = String(Math.ceil(left));
        numEl.classList.toggle('q-hot', left < 10);
      }
      if (left <= 0) finish(null);
    }, 150);
    quizTimers.push(tick);
    // Server timeout allowance is sec+2s grace; the client fires the timeout
    // send at the nominal remaining time so a slow network still lands
    // inside grace.
    const timeoutT = setTimeout(function () { finish(null); }, T * 1000);
    quizTimers.push(timeoutT);

    async function finish(pick) {
      if (answered) return;
      answered = true;
      quizInProgress = false;
      clearQuizTimers();
      // Freeze the ring where it stands — without this it keeps draining
      // under the verdict for the rest of the question's window.
      const fgNow = by('q-ring-fg');
      if (fgNow) fgNow.style.transition = 'none';
      tiles.querySelectorAll('.q-qt').forEach(function (b) { b.disabled = true; });

      let res;
      try {
        res = await api('/api/quests/quiz/answer', {
          method: 'POST',
          body: JSON.stringify({ token: ctx.token, pick: pick, index: index }),
        });
      } catch (e) {
        toast((e.body && e.body.detail) || t('Couldn\'t submit the answer'), 'error');
        closeOverlay();
        refreshState();
        return;
      }

      tiles.querySelectorAll('.q-qt').forEach(function (b) {
        const i = Number(b.getAttribute('data-i'));
        if (i === res.answer_idx) b.classList.add('q-ok');
        else if (i === pick) b.classList.add('q-bad');
        // The demo also badged the opponent's tile; the server's `opponent`
        // is a day score, not a per-question pick, so only "You" is honest.
        const mark = by('q-mk-' + i);
        if (mark && i === pick) mark.appendChild(el('span', { class: 'q-av', text: t('You') }));
      });

      const newScore = { me: res.score.me, bot: res.score.bot };
      const meEl = by('q-sc-me'), botEl = by('q-sc-bot');
      if (meEl) { meEl.textContent = String(newScore.me); if (res.correct) meEl.classList.add('q-bump'); }
      if (botEl) botEl.textContent = String(newScore.bot);
      if (res.correct) {
        const fly = el('div', { class: 'q-fly-pts', text: '+' + res.points });
        ovContent.querySelector('.q-qs').appendChild(fly);
      }

      const msg = pick === null
        ? t('Time\'s up')
        : (res.correct
          ? (res.points > 10 ? t('Correct and fast! +{p1}', {p1: res.points}) : t('Correct! +{p1}', {p1: res.points}))
          : t('Missed. The correct answer is highlighted'));
      foot.textContent = '';
      foot.appendChild(el('span', { class: 'q-qmsg' + (res.correct ? ' q-good' : ''), text: msg }));
      foot.appendChild(el('p', { class: 'q-qexp', text: res.explanation || '' }));
      const nextBtn = el('button', { class: 'q-cta', type: 'button', text: res.done ? t('Result') : t('Next') });
      nextBtn.addEventListener('click', function () {
        if (res.done) {
          quizEnd(res.summary);
        } else {
          // The next question's clock starts NOW, not when the verdict was
          // built: /quiz/begin stamps served_at for the current index and
          // returns the full window. No intro/countdown for questions 2, 3.
          nextBtn.disabled = true;
          quizBegin(ctx);
        }
      });
      foot.appendChild(nextBtn);
    }
  }

  function quizEnd(summary) {
    clearQuizTimers();
    quizInProgress = false;
    if (!summary) { closeOverlay(); refreshState(); return; }
    const title = summary.correct === 3 ? t('Flawless!') : summary.correct > summary.bot ? t('You beat your colleagues') : summary.correct === summary.bot ? t('Draw') : t('Colleagues were faster today');
    const qs = stage([
      el('div', { class: 'q-qend' }, [
        el('div', { class: 'q-qeyebrow', text: t('Result') }),
        el('h2', { class: 'q-qtitle', text: title }),
        el('div', { class: 'q-qbig' }, [
          el('div', { class: 'q-qp' }, [el('div', { class: 'q-qav', text: t('You') }), el('b', { id: 'q-end-me', text: '0' })]),
          el('div', { class: 'q-qx', text: ':' }),
          el('div', { class: 'q-qp' }, [el('div', { class: 'q-qav q-them', text: t('To') }), el('b', { id: 'q-end-bot', text: '0' })]),
        ]),
        el('div', { class: 'q-qrew' }, [
          el('span', {}, [document.createTextNode('+'), el('b', { id: 'q-end-pts', text: '0' }), document.createTextNode(t(' points'))]),
          el('span', { text: t('+1 surprise') }),
        ]),
        (function () {
          const btn = el('button', { class: 'q-cta q-cta-big', type: 'button', text: t('Claim surprise') });
          btn.classList.add('big');
          btn.addEventListener('click', async function () {
            // The quiz card is rolled with `revealed_at = NULL`, i.e. it is
            // now `pending` — opening it is a POST /api/quests/open like any
            // other surprise, so send the person to the hero capsule rather
            // than playing a reveal the server has not recorded.
            closeOverlay();
            await refreshState();
            by('q-hero').scrollIntoView({ behavior: 'smooth', block: 'start' });
          });
          return btn;
        })(),
      ]),
    ]);
    void qs;
    let k = 0;
    const tv = setInterval(function () {
      k++;
      by('q-end-me').textContent = String(Math.min(summary.correct, k));
      by('q-end-bot').textContent = String(Math.min(summary.bot, k));
      by('q-end-pts').textContent = String(Math.min(summary.points, Math.round(summary.points * k / 3)));
      if (k >= 3) clearInterval(tv);
    }, 260);
    quizTimers.push(tv);
    setTimeout(function () {
      confetti(summary.correct === 3 ? 320 : 160, innerWidth / 2, innerHeight * 0.35, summary.correct === 3);
    }, 500);
  }

  // ── Escape closes the reveal/quiz overlay ────────────────────────
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !overlay.hidden) requestCloseOverlay();
  });

  // ── Reward celebration ────────────────────────────────────────────
  // A quest completes elsewhere (the server verifies on page load, or a
  // client event on another page credited it). The person must SEE the
  // payoff: a festive stage, the points counter climbing, and — when a
  // surprise was earned — a straight path to the capsule.
  const SEEN_KEY = 'op-quests-seen-done-' + String((window.OP_USER || {}).person_id || 'anon');
  function newlyDoneQuests() {
    const done = (S.quests || []).filter(function (q) { return q.done; }).map(function (q) { return q.id; });
    let seen = null;
    try { const raw = localStorage.getItem(SEEN_KEY); seen = raw ? JSON.parse(raw) : null; } catch (e) { seen = null; }
    try { localStorage.setItem(SEEN_KEY, JSON.stringify(done)); } catch (e) { /* ignore */ }
    if (!Array.isArray(seen)) return []; // first visit: nothing to celebrate retroactively
    return done.filter(function (id) { return seen.indexOf(id) < 0; });
  }
  function countUp(elm, from, to, ms) {
    if (!elm) return;
    const t0 = performance.now();
    (function tick(now) {
      const k = Math.min(1, (now - t0) / ms);
      const eased = 1 - Math.pow(1 - k, 3);
      elm.textContent = String(Math.round(from + (to - from) * eased));
      if (k < 1) requestAnimationFrame(tick);
    })(t0);
  }
  function celebrate(items) {
    if (!items.length) return;
    const pts = items.reduce(function (a, i) { return a + (i.points || 0); }, 0);
    const surprises = items.filter(function (i) { return i.surprise; }).length;
    const before = Math.max(0, (S.points || 0) - pts);
    const rows = items.map(function (i) {
      return el('div', { class: 'q-cel-row' }, [
        el('span', { class: 'q-cel-check', text: '✓' }),
        el('span', { text: i.title }),
      ]);
    });
    const chips = [el('span', { class: 'q-cel-chip' }, [document.createTextNode('+'), el('b', { id: 'q-cel-pts', text: String(0) }), document.createTextNode(t(' points'))])];
    if (surprises) chips.push(el('span', { class: 'q-cel-chip q-cel-gold', text: '+' + surprises + ' ' + (surprises === 1 ? t('surprise') : surprises < 5 ? t('surprises') : t('surprises {ctx:2}')) }));
    const btn = el('button', { class: 'q-cta q-cta-big', type: 'button', text: surprises ? t('Open surprise') : t('Great') });
    btn.addEventListener('click', function () {
      closeOverlay();
      if (surprises) { by('q-hero').scrollIntoView({ block: 'start' }); setTimeout(function () { by('q-open').click(); }, 250); }
    });
    stage([
      el('div', { class: 'q-qend q-cel' }, [
        el('div', { class: 'q-qeyebrow', text: items.length === 1 ? t('Task completed') : t('Tasks completed') }),
        el('h2', { class: 'q-qtitle', text: surprises ? t('The reward is yours!') : t('Keep it up!') }),
        el('div', { class: 'q-cel-list' }, rows),
        el('div', { class: 'q-qrew' }, chips),
        btn,
      ]),
    ]);
    setTimeout(function () { confetti(surprises ? 260 : 140, window.innerWidth / 2, window.innerHeight * 0.35, false); }, 250);
    setTimeout(function () { countUp(by('q-cel-pts'), 0, pts, 900); countUp(by('q-pill-pts-val'), before, S.points || 0, 1200); }, 500);
  }
  function celebrateOnLoad() {
    const byId = {};
    (S.quests || []).forEach(function (q) { byId[q.id] = q; });
    const items = [];
    const seenIds = {};
    (S.awards || []).forEach(function (a) {
      const q = byId[a.quest_id]; if (!q || seenIds[q.id]) return; seenIds[q.id] = 1;
      items.push({ id: q.id, title: q.title, points: a.points != null ? a.points : q.points, surprise: !!(a.surprise != null ? a.surprise : q.surprise) });
    });
    newlyDoneQuests().forEach(function (id) {
      const q = byId[id]; if (!q || seenIds[id]) return; seenIds[id] = 1;
      items.push({ id: q.id, title: q.title, points: q.points, surprise: !!q.surprise });
    });
    celebrate(items);
  }

  // ── Boot ──────────────────────────────────────────────────────────
  renderAll();
  celebrateOnLoad();
  // A client quest done right on this page (Ctrl+K → «Toggle theme»): the
  // cross-page script (quests.js) sends the event and hands the award over here.
  window.addEventListener('op:quest-award', function () { refreshState(); });
})();
