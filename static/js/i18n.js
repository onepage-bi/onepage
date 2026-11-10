/* Client half of the i18n core (en / ru / zh-HK).
 *
 * Loaded `defer` right after /static/locales/<lang>.js and BEFORE every other
 * script of ours: defer scripts run in document order, so `t` exists by the
 * time any module body executes (palette.js builds its command table at load
 * time, before touching the document).
 *
 * The catalogue is a plain object keyed by the English source string; a miss
 * returns the key with its `{ctx:…}` disambiguator stripped, which is exactly
 * the English UI. `window.OP_I18N_CATALOG` for en is `{}`.
 *
 * No module system on purpose — the project ships vanilla JS with no build.
 */
(function () {
  'use strict';

  var CTX = /\s*\{ctx:[^}]*\}/;
  var PARAM = /\{(\w+)\}/g;

  // `window` and `document` are both absent in the Node test harnesses, which
  // require this very file to exercise t/tn against a fixture catalogue.
  var scope = typeof window !== 'undefined' ? window : globalThis;
  var root = typeof document !== 'undefined' && document.documentElement;
  var lang = (root && root.getAttribute('lang')) || 'en';
  var catalog = scope.OP_I18N_CATALOG || {};

  // English has two forms, Russian three (the rule folder-watch.js used to
  // carry; it now calls tn instead), Chinese one.
  function plural(n) {
    if (lang === 'ru') {
      n = Math.abs(n | 0);
      if (n % 10 === 1 && n % 100 !== 11) return 0;
      if (n % 10 >= 2 && n % 10 <= 4 && !(n % 100 >= 12 && n % 100 <= 14)) return 1;
      return 2;
    }
    if (lang === 'zh-HK') return 0;
    return n === 1 ? 0 : 1;
  }

  function fill(s, params) {
    if (!params) return s;
    return s.replace(PARAM, function (m, name) {
      var v = params[name];
      return v === undefined || v === null ? m : String(v);
    });
  }

  function t(key, params) {
    var s = catalog[key];
    if (s === undefined || s === null) s = String(key).replace(CTX, '');
    return fill(s, params);
  }

  function tn(n, key, params) {
    var s = catalog[key];
    var forms;
    if (s === undefined || s === null) {
      // Untranslated: the key IS the source text, so the rule that applies is
      // the rule of the language that WROTE it, and the form count says which
      // that is — English sources carry two forms ('{n} file|{n} files'), a
      // legacy Russian key three.
      forms = String(key).replace(CTX, '').split('|');
      var i;
      if (forms.length !== 3) {
        i = n === 1 ? 0 : 1;
      } else {
        var ru = Math.abs(n | 0);
        i = (ru % 10 === 1 && ru % 100 !== 11) ? 0
          : (ru % 10 >= 2 && ru % 10 <= 4 && !(ru % 100 >= 12 && ru % 100 <= 14)) ? 1 : 2;
      }
      return fill(forms[Math.min(i, forms.length - 1)], merge(n, params));
    }
    forms = s.split('|');
    // Clamp: a translation with fewer forms than the rule asks for must not
    // yield `undefined`.
    return fill(forms[Math.min(plural(n), forms.length - 1)], merge(n, params));
  }

  function merge(n, params) {
    var out = {n: n};
    if (params) for (var k in params) if (Object.prototype.hasOwnProperty.call(params, k)) out[k] = params[k];
    return out;
  }

  // The JS half of `_h`: a sentence whose middle carries an ELEMENT (a bold
  // number, a link) stays one key with `{name}` placeholders, and the element
  // is spliced in where the translation puts it — Chinese does not keep the
  // Russian word order, so the alternative (text + node + text appended by
  // hand) is not translatable at all. Text arrives via createTextNode, so a
  // translation can never inject markup.
  function tinto(parent, key, nodes, params) {
    var s = t(key, params);
    var re = /\{(\w+)\}/g;
    var last = 0, m;
    while ((m = re.exec(s)) !== null) {
      var node = nodes && nodes[m[1]];
      if (node === undefined || node === null) continue;
      if (m.index > last) parent.appendChild(document.createTextNode(s.slice(last, m.index)));
      parent.appendChild(node);
      last = m.index + m[0].length;
    }
    if (last < s.length) parent.appendChild(document.createTextNode(s.slice(last)));
    return parent;
  }

  var LOCALE = {ru: 'ru-RU', en: 'en-GB', 'zh-HK': 'zh-HK'};

  function locale() { return LOCALE[lang] || 'en-GB'; }

  function fmtDate(d, opts) {
    try {
      return new Intl.DateTimeFormat(locale(), opts || {}).format(d);
    } catch (e) { return String(d); }
  }

  function fmtNum(n, opts) {
    try {
      return new Intl.NumberFormat(locale(), opts || {}).format(n);
    } catch (e) { return String(n); }
  }

  // Tests only: re-seed language + catalogue without reloading the module.
  function init(nextLang, nextCatalog) {
    lang = nextLang || 'en';
    catalog = nextCatalog || {};
    scope.OP_LANG = lang;
    scope.OP_I18N.lang = lang;
    scope.OP_I18N.catalog = catalog;
  }

  scope.OP_LANG = lang;
  scope.t = t;
  scope.tn = tn;
  scope.tinto = tinto;
  scope.OP_I18N = {
    lang: lang,
    locale: locale,
    catalog: catalog,
    plural: plural,
    t: t,
    tn: tn,
    tinto: tinto,
    fmtDate: fmtDate,
    fmtNum: fmtNum,
    init: init
  };

  // Node harnesses (tests/*_dom.js) require this file through _i18n_stub.js.
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = scope.OP_I18N;
  }
})();
