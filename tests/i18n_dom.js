/* static/js/i18n.js — t / tn / plural / fmt, plus one end-to-end sweep of the
 * page modules with a REAL Russian catalogue loaded.
 *
 * The sweep is the point: with lang=en every module keeps rendering English
 * because a missing key falls through to itself, so the other harnesses
 * cannot tell a translated module from an untranslated one. Loading a ru
 * catalogue and re-running one of them proves the label actually changes — and
 * would catch a module that compares UI text to an English constant.
 *
 * Run: node tests/i18n_dom.js
 */
'use strict';
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const FAILURES = [];
function check(label, cond, extra) {
  console.log((cond ? 'PASS ' : 'FAIL ') + label + (!cond && extra ? '  <-- ' + extra : ''));
  if (!cond) FAILURES.push(label);
}

const I18N = require('./_i18n_stub');

// ── en: no catalogue, the key IS the text ────────────────────────────────
I18N.init('en', {});
check('en: a plain key comes back unchanged', t('Run') === 'Run', t('Run'));
check('en: parameters are filled',
  t('Version {n} of {date}', {n: 3, date: '01.02'}) === 'Version 3 of 01.02',
  t('Version {n} of {date}', {n: 3, date: '01.02'}));
check('en: an unknown parameter is left alone',
  t('Version {n}', {}) === 'Version {n}', t('Version {n}', {}));
check('en: a {ctx:…} disambiguator never reaches the screen',
  t('Scope {ctx:scope}') === 'Scope', t('Scope {ctx:scope}'));
check('en: tn picks the one-form', tn(1, '{n} file|{n} files') === '1 file');
check('en: tn picks the other-form', tn(3, '{n} file|{n} files') === '3 files');
check('en: tn treats 21 as plural, not one', tn(21, '{n} file|{n} files') === '21 files');
check('en: tn treats 0 as plural', tn(0, '{n} file|{n} files') === '0 files');
check('en: tn strips a {ctx:…} on a plural key too',
  tn(2, '{n} file|{n} files {ctx:2}') === '2 files', tn(2, '{n} file|{n} files {ctx:2}'));
check('en: tn also takes extra parameters',
  tn(2, '{n} file in {dir}|{n} files in {dir}', {dir: 'A'}) === '2 files in A');
// A legacy key that still carries THREE (Russian) forms keeps the Russian rule
// when nothing translates it — the form count says which language wrote it.
check('en: an untranslated 3-form plural keeps its forms AND the Russian rule',
  tn(3, '{n} слайд|{n} слайда|{n} слайдов') === '3 слайда' &&
  tn(21, '{n} слайд|{n} слайда|{n} слайдов') === '21 слайд',
  tn(3, '{n} слайд|{n} слайда|{n} слайдов'));

// ── ru: three forms, and a translation shorter than the rule asks for ────
// The Russian strings in this block are deliberate: they exercise the ru plural rule.
I18N.init('ru', {
  'Run': 'Запустить',
  '{n} file|{n} files': '{n} файл|{n} файла|{n} файлов',
  '{n} file in {dir}|{n} files in {dir}': '{n} файл в {dir}|{n} файла в {dir}|{n} файлов в {dir}',
  'Version {n} of {date}': 'Версия {n} от {date}',
  'Scope {ctx:scope}': 'Область',
  '{n} check|{n} checks': '{n} проверка',   // deliberately 1 form
});
check('ru: a translated key is used', t('Run') === 'Запустить', t('Run'));
check('ru: an untranslated key still shows English',
  t('Cancel') === 'Cancel', t('Cancel'));
check('ru: an untranslated key with a context shows English without it',
  t('Cancel {ctx:2}') === 'Cancel', t('Cancel {ctx:2}'));
check('ru: parameters survive a reordered translation',
  t('Version {n} of {date}', {n: 3, date: '01.02'}) === 'Версия 3 от 01.02');
check('ru: a context suffix is answered by the translation',
  t('Scope {ctx:scope}') === 'Область');
check('ru: tn picks the one-form', tn(1, '{n} file|{n} files') === '1 файл');
check('ru: tn picks the few-form', tn(3, '{n} file|{n} files') === '3 файла');
check('ru: tn picks the many-form', tn(11, '{n} file|{n} files') === '11 файлов');
check('ru: tn treats 21 as one, not many', tn(21, '{n} file|{n} files') === '21 файл');
check('ru: tn treats 0 as many', tn(0, '{n} file|{n} files') === '0 файлов');
check('ru: tn also takes extra parameters',
  tn(2, '{n} file in {dir}|{n} files in {dir}', {dir: 'A'}) === '2 файла в A');
check('ru: a translation with too few forms clamps instead of returning undefined',
  tn(7, '{n} check|{n} checks') === '7 проверка',
  tn(7, '{n} check|{n} checks'));
// An English key nobody translated yet carries TWO forms. Reading those under
// the Russian rule would index form 2 (undefined) or print «21 file».
check('ru: an untranslated ENGLISH plural uses the English rule, not the Russian one',
  tn(1, '{n} slide|{n} slides') === '1 slide' &&
  tn(21, '{n} slide|{n} slides') === '21 slides' &&
  tn(5, '{n} slide|{n} slides') === '5 slides',
  tn(21, '{n} slide|{n} slides'));

// ── zh-HK: one form ──────────────────────────────────────────────────────
I18N.init('zh-HK', {'{n} file|{n} files': '{n} 個檔案'});
check('zh-HK: one plural form for every count',
  tn(1, '{n} file|{n} files') === '1 個檔案' &&
  tn(9, '{n} file|{n} files') === '9 個檔案');
check('zh-HK: plural() is always 0', I18N.plural(1) === 0 && I18N.plural(5) === 0);
check('zh-HK: an untranslated key shows English', t('Run') === 'Run');

// ── Locales for dates and numbers ────────────────────────────────────────
I18N.init('en', {});
check('en uses en-GB (dd/mm), not en-US', I18N.locale() === 'en-GB');
I18N.init('ru', {});
check('ru uses ru-RU', I18N.locale() === 'ru-RU');
check('fmtNum formats without throwing', typeof I18N.fmtNum(1234.5) === 'string');
check('fmtDate formats without throwing',
  typeof I18N.fmtDate(new Date(Date.UTC(2026, 0, 2))) === 'string');

// The bug this replaced: every module formatted with a literal 'ru-RU', so a
// zh-HK page showed the weekday and month in Russian. Node ships full ICU, so
// the three languages must come back visibly different — and each in its own
// script. The date is a Monday in September, the month/weekday every module
// spells out.
{
  const day = new Date(Date.UTC(2026, 8, 7, 12, 0, 0));
  const long = {weekday: 'long', day: 'numeric', month: 'long'};
  I18N.init('ru', {});
  const ru = I18N.fmtDate(day, long);
  I18N.init('en', {});
  const en = I18N.fmtDate(day, long);
  I18N.init('zh-HK', {});
  const zh = I18N.fmtDate(day, long);

  check('fmtDate: ru spells the month in Cyrillic', /[\u0410-\u044F]/.test(ru), ru);
  check('fmtDate: en spells the weekday in English',
    /Monday/i.test(en) && /September/i.test(en), en);
  check('fmtDate: zh-HK spells the date in Chinese, not Russian',
    /[\u4e00-\u9fff]/.test(zh) && !/[\u0410-\u044F]/.test(zh), zh);
  check('fmtDate: the three languages really differ',
    ru !== en && en !== zh && ru !== zh, [ru, en, zh].join(' | '));

  // en-GB, not en-US: the numeric date is dd/mm, so the 7th of September is
  // "07/09" and never "09/07".
  I18N.init('en', {});
  const numeric = I18N.fmtDate(day, {day: '2-digit', month: '2-digit', year: 'numeric'});
  check('fmtDate: en is en-GB (dd/mm/yyyy)', /^07.09.2026$/.test(numeric), numeric);

  // Group separators differ too — ru uses a space, en a comma.
  I18N.init('ru', {});
  const nru = I18N.fmtNum(1234567);
  I18N.init('en', {});
  const nen = I18N.fmtNum(1234567);
  check('fmtNum: en groups with commas', nen === '1,234,567', nen);
  check('fmtNum: ru does not group like en', nru !== nen, nru);
  I18N.init('en', {});
}

// ── No module pins a locale of its own ───────────────────────────────────
// tools/i18n_check.py enforces this too; here it is a fast, dependency-free
// second pair of eyes over the shipped modules.
{
  const dir = path.join(ROOT, 'static', 'js');
  const walk = d => fs.readdirSync(d, {withFileTypes: true}).flatMap(e =>
    e.isDirectory() ? (e.name === 'vendor' ? [] : walk(path.join(d, e.name)))
      : (e.name.endsWith('.js') ? [path.join(d, e.name)] : []));
  const offenders = walk(dir).filter(f => path.basename(f) !== 'i18n.js')
    .filter(f => /'ru-RU'|"ru-RU"|\.toLocale(?:Date|Time)?String\(\s*['"]/
      .test(fs.readFileSync(f, 'utf8')))
    .map(f => path.relative(ROOT, f));
  check('no module outside i18n.js pins a locale literal',
    offenders.length === 0, offenders.join(', '));
}

// ── The catalogue files that ship ────────────────────────────────────────
for (const lang of ['ru', 'en', 'zh-HK']) {
  const p = path.join(ROOT, 'static', 'locales', lang + '.js');
  check('static/locales/' + lang + '.js exists', fs.existsSync(p));
  const src = fs.readFileSync(p, 'utf8');
  check('static/locales/' + lang + '.js only assigns the catalogue',
    /^\/\*.*\*\/\nwindow\.OP_I18N_CATALOG=\{.*\};\n$/s.test(src));
}
check('en.js is empty — an English page pays nothing',
  /window\.OP_I18N_CATALOG=\{\};/.test(
    fs.readFileSync(path.join(ROOT, 'static/locales/en.js'), 'utf8')));
check('ru.js carries the Russian translations',
  /window\.OP_I18N_CATALOG=\{".+\};/s.test(
    fs.readFileSync(path.join(ROOT, 'static/locales/ru.js'), 'utf8')));

// ── base.html loads the catalogue before anything that uses it ───────────
{
  const base = fs.readFileSync(path.join(ROOT, 'templates/base.html'), 'utf8');
  const cat = base.indexOf('/static/locales/');
  const core = base.indexOf('/static/js/i18n.js');
  const main = base.indexOf('/static/js/main.js');
  check('base.html loads the catalogue, then i18n.js, then main.js',
    cat > 0 && core > cat && main > core, `${cat} ${core} ${main}`);
  check('base.html carries the negotiated language on <html lang>',
    /<html lang="\{\{ current_lang\(\)/.test(base));
}

// ── The sweep: one module rendered with a Russian catalogue ──────────────
// data-quality.js is a good probe — it builds its labels through el({text}).
{
  // Deliberate Russian: a ru catalogue drives the sweep.
  I18N.init('ru', {'No data': 'Нет данных', 'Checks': 'Проверки'});
  const src = fs.readFileSync(path.join(ROOT, 'static/js/data-quality.js'), 'utf8');
  check('data-quality.js asks for its labels through t()', /\bt\('/.test(src));
  check('data-quality.js has no Russian literal left outside t()',
    !/(?:^|[^_\w'])'[^']*[\u0400-\u04FF][^']*'/m.test(
      src.replace(/\bt\('(?:\\.|[^'])*'/g, "t('')")
         .replace(/^\s*\/\/.*$/gm, '')
         .replace(/\/\*[\s\S]*?\*\//g, '')));
  check('data-quality.js keys are English (no Cyrillic inside t())',
    !/\bt\('(?:\\.|[^'])*[\u0400-\u04FF]/.test(src));
  check('t() reaches a translated label from the module\'s own catalogue',
    t('No data') === 'Нет данных');
  I18N.init('en', {});
}

// ── tinto: an element spliced into a WHOLE translated sentence ───────────
// The point of the helper is word order: Chinese puts the bold number in a
// different place than English, and only a whole key can express that.
{
  const node = (tag, text) => ({tag: tag, text: text});
  const parent = {kids: [], appendChild(n) { this.kids.push(n); }};
  global.document = {createTextNode: s => node('#text', s)};

  I18N.init('en', {});
  I18N.tinto(parent, 'Open today to extend to {n}', {n: node('b', '4')});
  check('tinto splits the English sentence around the element',
    parent.kids.length === 2 && parent.kids[0].text === 'Open today to extend to ' &&
    parent.kids[1].tag === 'b', JSON.stringify(parent.kids));

  const zh = {kids: [], appendChild(n) { this.kids.push(n); }};
  I18N.init('zh-HK', {'Open today to extend to {n}': '開啟至 {n} 天'});
  I18N.tinto(zh, 'Open today to extend to {n}', {n: node('b', '4')});
  check('tinto follows the TRANSLATION\'s word order, element in the middle',
    zh.kids.length === 3 && zh.kids[0].text === '開啟至 ' && zh.kids[1].tag === 'b' &&
    zh.kids[2].text === ' 天', JSON.stringify(zh.kids));

  const plain = {kids: [], appendChild(n) { this.kids.push(n); }};
  I18N.init('en', {});
  I18N.tinto(plain, 'No data', {});
  check('tinto with no placeholder appends one text node',
    plain.kids.length === 1 && plain.kids[0].text === 'No data');
  delete global.document;
  I18N.init('en', {});
}

console.log('');
if (FAILURES.length) {
  console.log('FAILURES: ' + FAILURES.length);
  process.exit(1);
}
console.log('ALL PASS');
