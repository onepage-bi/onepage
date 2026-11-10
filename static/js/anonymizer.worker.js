/* OnePage — anonymizer.worker.js
 * Runs entirely off the main thread: parse, table/header detection, masking, export.
 * SheetJS is loaded here, not on the page, so the UI never blocks and the tab
 * cannot be frozen by a heavy file. All work is linear in the number of cells.
 */
/* global XLSX */
importScripts('/static/js/vendor/xlsx.full.min.js');

const MAX_CELLS = 300000;  // guard against pathological inputs
const SAMPLE_ROWS = 300;   // rows inspected per column for type detection
const MAX_TABLES = 50;     // anti-fragmentation guard per sheet
const HEADER_SCAN = 10;    // rows scanned for a header within a region
const HEADER_THRESHOLD = 1.6;
const PREVIEW_ROWS = 12;

let WB = null; // retained parsed workbook for the mask step

// ── Fake-data dictionaries (compact, invented English) ──────────────
// Replacements are made-up English names, places and words whatever language
// the source is in. Their only job is to look like a value of the same kind.
const FIRST_M = ['Adrian','Beatrice','Calvin','Daphne','Elliot','Fiona','Gideon','Harriet','Isaac','Juliet','Kenneth','Lydia','Martin','Nora','Oscar','Penelope','Quentin','Rosalind','Simon','Tabitha','Victor','Wendy'];
const LAST_M = ['Ashcombe','Barlow','Carrington','Dunmore','Ellery','Fairbrook','Garside','Holloway','Ingram','Jessop','Kendrew','Langley','Merriweather','Northcott','Oakley','Pennington','Radcliffe','Stanmore','Thornbury','Underhill','Whitlock','Yardley'];
const CITIES = ['Ashford','Bramwell','Clayborough','Dunsmere','Eastwick','Fallowfield','Glenhaven','Harrowgate','Kingsbury','Larkhill','Millbrook','Northam','Oakridge','Pinehurst','Redford','Stonebridge','Thistledown','Westmoor'];
const COMPANIES = ['Alder','Beacon','Copperline','Driftwood','Evergreen','Foxglove','Granite','Harbourview','Ironbark','Juniper','Keystone','Lantern','Meridian','Northwind','Orchard','Pinnacle','Quarry','Riverside','Silverleaf','Timberline'];
const COMPANY_SUFFIX = ['Ltd','LLC','Inc','Group','Partners','Trading'];
const STREETS = ['Maple','Oak','Station','Church','Mill','Park','High','Bridge','Willow','Chestnut','Elm','Orchard'];
const STREET_KIND = ['Street','Road','Lane','Avenue','Close','Way'];
const WORDS = ['report','data','analysis','object','value','period','group','status','model','region','sector','position','category','metric','unit','segment','batch','resource','channel','item'];
const DOMAINS = ['example.com','mail.example.org','corp.example.net','test.local'];

const rnd = (n) => Math.floor(Math.random() * n);
const pick = (a) => a[rnd(a.length)];
const slug = (s) => String(s).toLowerCase().replace(/[^a-z0-9]+/g, '');

function genName() { return pick(FIRST_M) + ' ' + pick(LAST_M); }
function genEmail() { return slug(pick(FIRST_M)) + '.' + slug(pick(LAST_M)) + rnd(90) + '@' + pick(DOMAINS); }
/* A phone keeps its shape: the leading "+<country code>" stays, every other
   digit is replaced by a random one, spaces, brackets and dashes stay where
   they were. A sample then shows the model the real format of the column. */
function genPhone(real) {
  const src = String(real).trim();
  const cc = /^\+\d{1,3}/.exec(src);
  const head = cc ? cc[0] : '';
  const tail = src.slice(head.length).replace(/\d/g, () => String(rnd(10)));
  return head + tail;
}
function genCity() { return pick(CITIES); }
function genCompany() { return pick(COMPANIES) + ' ' + pick(COMPANY_SUFFIX); }
function genAddress() { return (1 + rnd(120)) + ' ' + pick(STREETS) + ' ' + pick(STREET_KIND) + ', Apt ' + (1 + rnd(200)) + ', ' + pick(CITIES); }
function genPassport() { return (1000 + rnd(9000)) + ' ' + (100000 + rnd(900000)); }
function genInn(len) {
  function digit(arr, w) { let s = 0; for (let i = 0; i < w.length; i++) s += arr[i] * w[i]; return (s % 11) % 10; }
  const base = []; const total = len === 12 ? 12 : 10;
  for (let i = 0; i < total - (total === 12 ? 2 : 1); i++) base.push(rnd(10));
  if (total === 10) base.push(digit(base, [2, 4, 10, 3, 5, 9, 4, 6, 8]));
  else { base.push(digit(base, [7, 2, 4, 10, 3, 5, 9, 4, 6, 8])); base.push(digit(base, [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8])); }
  return base.join('');
}
function genSnils() {
  const d = []; for (let i = 0; i < 9; i++) d.push(rnd(10));
  let sum = 0; for (let i = 0; i < 9; i++) sum += d[i] * (9 - i);
  const cs = sum < 100 ? sum : (sum === 100 || sum === 101 ? 0 : (sum % 101 === 100 || sum % 101 === 101 ? 0 : sum % 101));
  const s = d.join('');
  return s.slice(0, 3) + '-' + s.slice(3, 6) + '-' + s.slice(6, 9) + ' ' + String(cs).padStart(2, '0');
}
/* Codes such as "ST-01", "SKU-4471" or "t-12" (Cyrillic ones too) are the
   most common key shape in exports. Replacing them with a random word loses
   both the look of the record and (see below) uniqueness. So the shape is kept
   character by character: a letter becomes a letter of the same alphabet and
   case, a digit becomes a digit, separators stay in place. From such a sample
   the model sees the real structure of the key, and a person sees it is still
   a code, not a name. */
const RU_UP = 'АБВГДЕЖЗИКЛМНОПРСТУФХЦЧШЩЭЮЯ', RU_LO = 'абвгдежзиклмнопрстуфхцчшщэюя';
const EN_UP = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', EN_LO = 'abcdefghijklmnopqrstuvwxyz';
const RE_CODE = /^[A-Za-zА-Яа-яЁё]{1,6}[-_. ]?\d{1,8}$|^\d{1,8}[-_. ]?[A-Za-zА-Яа-яЁё]{1,6}$/;
function genCode(real) {
  return String(real).replace(/[A-Za-zА-Яа-яЁё0-9]/g, ch => {
    if (ch >= '0' && ch <= '9') return String(rnd(10));
    if (RU_UP.includes(ch)) return RU_UP[rnd(RU_UP.length)];
    if (RU_LO.includes(ch)) return RU_LO[rnd(RU_LO.length)];
    if (EN_UP.includes(ch)) return EN_UP[rnd(EN_UP.length)];
    if (EN_LO.includes(ch)) return EN_LO[rnd(EN_LO.length)];
    return ch;
  });
}

function genText(real) {
  const n = Math.max(1, String(real).trim().split(/\s+/).length);
  const out = []; for (let i = 0; i < n; i++) out.push(pick(WORDS));
  const s = out.join(' '); return s.charAt(0).toUpperCase() + s.slice(1);
}
function genNumber(real, st) {
  if (st.decimals === 0 && st.isId) {
    const digits = String(Math.trunc(Math.abs(Number(real))) || 1).length || 1;
    let s = String(1 + rnd(9)); for (let i = 1; i < digits; i++) s += rnd(10);
    return Number(s);
  }
  const span = (st.max - st.min) || Math.abs(st.max) || 1;
  const v = st.min + Math.random() * span; const f = Math.pow(10, st.decimals);
  return Math.round(v * f) / f;
}
function genDate(st) { const span = (st.maxT - st.minT) || (365 * 86400000); return new Date(st.minT + Math.random() * span); }

function generate(category, real, st) {
  switch (category) {
    case 'name': return genName();
    case 'email': return genEmail();
    case 'phone': return genPhone(real);
    case 'inn': return genInn(String(real).replace(/\D/g, '').length === 12 ? 12 : 10);
    case 'snils': return genSnils();
    case 'passport': return genPassport();
    case 'city': return genCity();
    case 'company': return genCompany();
    case 'address': return genAddress();
    case 'number': return genNumber(real, st);
    case 'date': return genDate(st);
    case 'bool': return Math.random() < 0.5;
    case 'code': return genCode(real);
    case 'text': default: return genText(real);
  }
}

// ── Cell helpers ────────────────────────────────────────────────────
const E = XLSX.utils.encode_cell, D = XLSX.utils.decode_cell;
function cellAt(ws, r, c) { return ws[E({ r, c })]; }
function nonEmpty(cell) { return cell && cell.v !== undefined && cell.v !== null && cell.v !== ''; }
function fmt(cell) { return cell ? (cell.w != null ? String(cell.w) : (cell.v != null ? String(cell.v) : '')) : ''; }

// ── Type/semantic detection (per column) ────────────────────────────
const RE = {
  email: /^[^@\s]+@[^@\s]+\.[^@\s]+$/,
  phone: /^[+()\d][\s()+\-\d]{6,}$/,
  inn: /^\d{10}$|^\d{12}$/,
  snils: /^\d{3}-\d{3}-\d{3}\s?\d{2}$/,
  passport: /^\d{4}\s?\d{6}$/,
  // Personal name: two or three capitalised words, either all Cyrillic (the
  // Russian surname-name-patronymic form) or all Latin ("Irene Somers",
  // "Mary-Ann O'Neil", "J. R. Smith"). A mix of alphabets is not a name.
  name: /^[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?(?:\s[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?){1,2}$|^(?:[A-Z]\.\s?|[A-Z][a-z]+(?:['’-]?[A-Z][a-z]+)?\s)(?:[A-Z]\.\s?|[A-Z][a-z]+(?:['’-]?[A-Z][a-z]+)?\s){0,2}[A-Z][a-z]+(?:['’-]?[A-Z][a-z]+)?$/,
};
/* Two capitalised Latin words are just as often a client, a place or a
   channel ("Breezeway Ltd", "New York", "Modern Trade") as a person. A value
   containing one of these words is not taken for a personal name. Cyrillic
   values keep their original rule, without a stop list. */
const NOT_A_NAME = new Set(['ltd','llc','inc','plc','gmbh','corp','co','company','group','holding',
  'holdings','partners','trading','trade','store','stores','shop','market','mart','retail','wholesale',
  'bank','foods','services','solutions','systems','international','global','the','and','of',
  'north','south','east','west','central','new','city','region','district','street','road','avenue',
  'key','account','channel','modern','traditional','total','other','unknown','sole','trader']);
function isName(s) {
  if (!RE.name.test(s)) return false;
  if (!/^[A-Z]/.test(s)) return true;   // the Cyrillic branch matched
  return !s.toLowerCase().split(/[\s.'’-]+/).some(w => NOT_A_NAME.has(w));
}
function detectColumn(samples) {
  const non = samples.filter(nonEmpty);
  if (!non.length) return { type: 'empty', category: 'keep', mask: false };
  const tCount = {}; non.forEach(c => { tCount[c.t] = (tCount[c.t] || 0) + 1; });
  const dom = Object.keys(tCount).sort((a, b) => tCount[b] - tCount[a])[0];
  if (dom === 'd') return { type: 'date', category: 'date', mask: false };
  if (dom === 'n') {
    let decimals = 0; const distinct = new Set(); let cnt = 0;
    non.forEach(c => { if (typeof c.v === 'number') { distinct.add(c.v); cnt++; const m = /\.(\d+)/.exec(String(c.w ?? c.v)); if (m) decimals = Math.max(decimals, m[1].length); } });
    const isId = decimals === 0 && cnt > 3 && distinct.size / cnt > 0.9;
    return { type: isId ? 'id' : 'number', category: 'number', mask: false };
  }
  if (dom === 'b') return { type: 'bool', category: 'bool', mask: false };
  const sv = non.map(c => String(c.v).trim());
  const match = (re) => sv.filter(s => re.test(s)).length / sv.length;
  let category = 'text';
  if (match(RE.email) > 0.6) category = 'email';
  else if (match(RE.snils) > 0.6) category = 'snils';
  else if (match(RE.inn) > 0.6) category = 'inn';
  else if (match(RE.passport) > 0.6) category = 'passport';
  else if (match(RE.phone) > 0.6 && sv.every(s => s.replace(/\D/g, '').length >= 7)) category = 'phone';
  else if (sv.filter(isName).length / sv.length > 0.6) category = 'name';
  else if (match(RE_CODE) > 0.6) category = 'code';
  const distinct = new Set(sv).size;
  const lowCard = distinct <= 15 && distinct / sv.length < 0.3;
  const mask = category !== 'text' ? true : !lowCard;
  return { type: lowCard && category === 'text' ? 'category' : 'string', category, mask };
}

// ── Sheet segmentation → regions (linear in populated cells) ─────────
function segment(ws, range) {
  const byRow = new Map(); // r → [c,...]
  for (const k in ws) {
    if (k.charCodeAt(0) === 33) continue; // '!'
    if (!nonEmpty(ws[k])) continue;
    const a = D(k);
    if (a.r < range.s.r || a.r > range.e.r || a.c < range.s.c || a.c > range.e.c) continue;
    if (!byRow.has(a.r)) byRow.set(a.r, []);
    byRow.get(a.r).push(a.c);
  }
  const rows = [...byRow.keys()].sort((a, b) => a - b);
  // bands: runs of consecutive populated rows (a blank row splits a band)
  const bands = []; let cur = null;
  for (const r of rows) { if (cur && r === cur.e + 1) cur.e = r; else { cur = { s: r, e: r }; bands.push(cur); } }
  // within each band: column-groups split by blank columns
  const regions = [];
  for (const band of bands) {
    const colsSet = new Set();
    for (let r = band.s; r <= band.e; r++) { const cs = byRow.get(r); if (cs) for (const c of cs) colsSet.add(c); }
    const cols = [...colsSet].sort((a, b) => a - b);
    const groups = []; let g = null;
    for (const c of cols) { if (g && c === g.e + 1) g.e = c; else { g = { s: c, e: c }; groups.push(g); } }
    for (const gr of groups) regions.push({ s: { r: band.s, c: gr.s }, e: { r: band.e, c: gr.e } });
  }
  return regions;
}

function rowMergedWide(merges, rr, cs, ce) {
  const width = ce - cs + 1;
  for (const m of merges) {
    if (m.s.r <= rr && m.e.r >= rr && (m.e.c - m.s.c + 1) > width / 2) return true;
  }
  return false;
}

function scoreHeader(ws, rr, cs, ce, lastR, merges) {
  const width = ce - cs + 1; let filled = 0, str = 0; const seen = new Set();
  for (let c = cs; c <= ce; c++) { const cell = cellAt(ws, rr, c); if (nonEmpty(cell)) { filled++; if (cell.t === 's') str++; seen.add(String(cell.v)); } }
  if (filled === 0) return -1;
  let belowFill = 0, belowData = 0, bn = 0;
  for (let r = rr + 1; r <= Math.min(rr + 3, lastR); r++) {
    bn++; let f = 0, d = 0;
    for (let c = cs; c <= ce; c++) { const cell = cellAt(ws, r, c); if (nonEmpty(cell)) { f++; if (cell.t === 'n' || cell.t === 'd') d++; } }
    belowFill += f / width; belowData += (f ? d / f : 0);
  }
  if (bn) { belowFill /= bn; belowData /= bn; }
  const fillRatio = filled / width, strRatio = str / filled, uniq = seen.size / filled;
  const penalty = rowMergedWide(merges, rr, cs, ce) ? 1 : 0;
  return fillRatio * 1.0 + strRatio * 0.8 + uniq * 0.4 + belowFill * 0.5 + (belowData > 0.3 ? 0.6 : 0) - penalty;
}

function bestHeader(ws, reg, merges) {
  let best = -1, bestRow = reg.s.r;
  const last = Math.min(reg.s.r + HEADER_SCAN - 1, reg.e.r);
  for (let rr = reg.s.r; rr <= last; rr++) {
    let filled = 0; for (let c = reg.s.c; c <= reg.e.c; c++) if (nonEmpty(cellAt(ws, rr, c))) filled++;
    if (filled < 2) continue;
    const sc = scoreHeader(ws, rr, reg.s.c, reg.e.c, reg.e.r, merges);
    if (sc > best) { best = sc; bestRow = rr; }
  }
  return { row: bestRow, confident: best >= HEADER_THRESHOLD };
}

function firstNonEmptyRow(ws, range) {
  for (let r = range.s.r; r <= range.e.r; r++) for (let c = range.s.c; c <= range.e.c; c++) if (nonEmpty(cellAt(ws, r, c))) return r;
  return range.s.r;
}

// Cheap dimension estimate (sheet ranges only) — used to reject huge files early
function estimateCells(wb) {
  let total = 0;
  for (const name of wb.SheetNames) {
    const ws = wb.Sheets[name]; if (!ws || !ws['!ref']) continue;
    const r = XLSX.utils.decode_range(ws['!ref']);
    total += (r.e.r - r.s.r + 1) * (r.e.c - r.s.c + 1);
  }
  return total;
}

// ── Build the lightweight model the UI renders ──────────────────────
function buildModel(wb) {
  const sheets = []; let totalCells = 0;
  for (const name of wb.SheetNames) {
    const ws = wb.Sheets[name]; if (!ws || !ws['!ref']) continue;
    const range = XLSX.utils.decode_range(ws['!ref']);
    totalCells += (range.e.r - range.s.r + 1) * (range.e.c - range.s.c + 1);
    const merges = ws['!merges'] || [];
    let regions = segment(ws, range);
    let tables = [];
    if (regions.length === 0 || regions.length > MAX_TABLES) {
      const hr = firstNonEmptyRow(ws, range);
      tables = [{ s: range.s, e: range.e, headerRow: hr }];
    } else {
      for (const reg of regions) {
        const h = bestHeader(ws, reg, merges);
        if (!h.confident) {
          // continuation across a blank row: same column span, directly below a prior table
          const cont = [...tables].reverse().find(tv => tv.s.c === reg.s.c && tv.e.c === reg.e.c && tv.e.r < reg.s.r);
          if (cont) { cont.e.r = reg.e.r; continue; }
        }
        tables.push({ s: { ...reg.s }, e: { ...reg.e }, headerRow: h.confident ? h.row : reg.s.r });
      }
    }
    // keep only tables with at least one data row
    tables = tables.filter(tv => tv.e.r - tv.headerRow >= 1);

    const outTables = tables.map(tv => {
      const cols = [];
      for (let c = tv.s.c; c <= tv.e.c; c++) {
        const hCell = cellAt(ws, tv.headerRow, c);
        const header = nonEmpty(hCell) ? String(hCell.v) : XLSX.utils.encode_col(c);
        const samples = [];
        for (let r = tv.headerRow + 1; r <= tv.e.r && samples.length < SAMPLE_ROWS; r++) samples.push(cellAt(ws, r, c));
        const det = detectColumn(samples);
        const sv = samples.find(nonEmpty) || {};
        cols.push({ c, header, type: det.type, category: det.category, mask: det.mask, sample: fmt(sv) });
      }
      const preview = [];
      for (let r = tv.s.r; r <= Math.min(tv.s.r + PREVIEW_ROWS - 1, tv.e.r); r++) {
        const vals = {}; for (let c = tv.s.c; c <= tv.e.c; c++) vals[c] = fmt(cellAt(ws, r, c));
        preview.push({ r, vals });
      }
      return { range: { s: tv.s, e: tv.e }, headerRow: tv.headerRow, cols, preview };
    });
    sheets.push({ name, tables: outTables });
  }
  return { sheets, totalCells };
}

// ── Stats recomputed fresh at mask time (honours header overrides) ──
function computeStats(ws, start, end, c, category) {
  if (category === 'number') {
    let min = Infinity, max = -Infinity, decimals = 0, cnt = 0; const distinct = new Set();
    for (let r = start; r <= end; r++) { const cell = cellAt(ws, r, c); if (cell && typeof cell.v === 'number') { const v = cell.v; if (v < min) min = v; if (v > max) max = v; distinct.add(v); cnt++; const m = /\.(\d+)/.exec(String(cell.w ?? cell.v)); if (m) decimals = Math.max(decimals, m[1].length); } }
    if (!cnt) return { min: 0, max: 1, decimals: 0, isId: false };
    return { min, max, decimals, isId: decimals === 0 && cnt > 3 && distinct.size / cnt > 0.9 };
  }
  if (category === 'date') {
    let minT = Infinity, maxT = -Infinity, cnt = 0;
    for (let r = start; r <= end; r++) { const cell = cellAt(ws, r, c); if (cell && cell.v instanceof Date) { const tv = cell.v.getTime(); if (tv < minT) minT = tv; if (tv > maxT) maxT = tv; cnt++; } }
    if (!cnt) { const now = Date.now(); return { minT: now - 31536000000, maxT: now }; }
    return { minT, maxT };
  }
  return {};
}

/* Categories where the same value MUST get the same replacement across the
   whole workbook. Otherwise links break: "ST-01" on the sales sheet and
   "ST-01" on the warehouse sheet would become different codes, and the
   anonymised file could no longer be joined on the key — and reconciling two
   sheets is exactly what it is most often needed for.
   Numbers and dates are not included: each column has its own range, and a
   shared map would tie a price to an amount. */
const LINKED = new Set(['text', 'code', 'name', 'email', 'phone', 'inn', 'snils',
                        'passport', 'city', 'company', 'address']);


/* ── What to hand the person along with the file ───────────────────────
   Two things that were missing. First: a prompt to the model needs not a
   twenty-thousand-row file but the header and two or three rows — "the shape
   of a record". They are taken from the ALREADY anonymised workbook, so they
   can be copied without a second look.
   Second: an honest self-check. After masking, the same detectors run over the
   columns the person chose NOT to mask, and whatever is left is named: real
   PII as well as quasi-identifiers — columns where nearly every value is
   unique, so a row can be recognised even without a surname. */
const PII_CATS = new Set(['name', 'email', 'phone', 'inn', 'snils', 'passport', 'address']);

/* After masking a cell has no formatted text (`w` is deleted), and a date
   would turn into "Mon Mar 02 2026 16:00:00 GMT-0800" — from such a sample
   the model would conclude the file's dates look like that. Print them the
   way the workbook stores them: dd.mm.yyyy. */
function fmtOut(cell) {
  if (!cell) return '';
  if (cell.v instanceof Date) {
    // The workbook stores a date as UTC midnight. Local getters would move it
    // a day back in any western time zone, and the sample would lie to the
    // model about the file's dates. Read the UTC components.
    const d = cell.v, p = (n) => String(n).padStart(2, '0');
    return p(d.getUTCDate()) + '.' + p(d.getUTCMonth() + 1) + '.' + d.getUTCFullYear();
  }
  return fmt(cell);
}

function sampleForPrompt(model, rows) {
  const sh = model.sheets.find(x => x.tables.length); if (!sh) return '';
  const tv = sh.tables[0];
  const ws = WB.Sheets[sh.name]; if (!ws) return '';
  const line = (r) => tv.cols.map(col => fmtOut(cellAt(ws, r, col.c))).join(';');
  const out = [line(tv.headerRow)];
  for (let r = tv.headerRow + 1; r <= Math.min(tv.headerRow + rows, tv.range.e.r); r++) out.push(line(r));
  return out.join('\n');
}

function leftovers(model) {
  const found = [];
  for (const sh of model.sheets) {
    const ws = WB.Sheets[sh.name]; if (!ws) continue;
    for (const tv of sh.tables) {
      const start = tv.headerRow + 1, end = tv.range.e.r;
      for (const col of tv.cols) {
        if (col.mask && col.category !== 'keep') continue;
        const samples = [];
        for (let r = start; r <= end && samples.length < SAMPLE_ROWS; r++) samples.push(cellAt(ws, r, col.c));
        const det = detectColumn(samples);
        if (PII_CATS.has(det.category)) {
          found.push({ kind: 'pii_left', sheet: sh.name, column: col.header, category: det.category });
          continue;
        }
        const vals = samples.filter(nonEmpty).map(fmt);
        const textish = det.type !== 'number' && det.type !== 'date' && det.type !== 'bool'
                        && det.type !== 'empty';
        if (textish && vals.length >= 8 && new Set(vals).size / vals.length > 0.9) {
          found.push({ kind: 'unique_left', sheet: sh.name, column: col.header });
        } else if (textish && vals.length) {
          // A text column left as is is the most common leak: "city" or
          // "channel" look harmless, but in a real file they hold client
          // names. They reach the prompt sample unchanged.
          found.push({ kind: 'text_left', sheet: sh.name, column: col.header,
                       sample: String(vals[0]).slice(0, 30) });
        }
      }
    }
  }
  return found;
}

function maskAndWrite(model, bookType) {
  let count = 0;
  const bookMaps = new Map(), bookUsed = new Map();   // category → map / used replacements
  for (const sh of model.sheets) {
    const ws = WB.Sheets[sh.name]; if (!ws) continue;
    for (const tv of sh.tables) {
      const start = tv.headerRow + 1, end = tv.range.e.r;
      for (const col of tv.cols) {
        if (!col.mask || col.category === 'keep') continue;
        const stats = computeStats(ws, start, end, col.c, col.category);
        const linked = LINKED.has(col.category);
        if (linked && !bookMaps.has(col.category)) {
          bookMaps.set(col.category, new Map());
          bookUsed.set(col.category, new Set());
        }
        const map = linked ? bookMaps.get(col.category) : new Map();
        // The map MUST be bijective. `map` alone is not enough: it holds
        // "one source → one replacement", but two different values can easily
        // get the same replacement — the replacement dictionary is finite
        // (about twenty words), and on a column of 60 codes 60 entities once
        // silently collapsed into 18. The file still looks whole, analysis on
        // that column is no longer possible, and the person never learns of it.
        // So used replacements are tracked, generation is retried, and once the
        // space is exhausted a distinguishing suffix is appended.
        const used = linked ? bookUsed.get(col.category) : new Set();
        for (let r = start; r <= end; r++) {
          const cell = cellAt(ws, r, col.c);
          if (!nonEmpty(cell) || cell.f) continue; // skip empties and formulas
          const key = String(cell.v);
          let f = map.get(key);
          if (f === undefined) {
            f = generate(col.category, cell.v, stats);
            for (let k = 0; k < 64 && used.has(String(f)); k++) f = generate(col.category, cell.v, stats);
            if (used.has(String(f))) {
              // Replacement space exhausted — disambiguate with a suffix, keeping the type.
              if (typeof f === 'number') { let n = f; while (used.has(String(n))) n += 1; f = n; }
              else { let n = 2, base = String(f); while (used.has(base + ' ' + n)) n++; f = base + ' ' + n; }
            }
            used.add(String(f));
            map.set(key, f);
          }
          cell.v = f; delete cell.w;
          if (f instanceof Date) { cell.t = 'd'; if (!cell.z) cell.z = 'dd.mm.yyyy'; }
          else if (typeof f === 'number') cell.t = 'n';
          else if (typeof f === 'boolean') cell.t = 'b';
          else cell.t = 's';
          count++;
        }
      }
    }
  }
  const sample = sampleForPrompt(model, 3);
  const left = leftovers(model);
  const out = XLSX.write(WB, { type: 'array', bookType, cellStyles: true });
  return { out, count, sample, left };
}

// ── Message protocol ────────────────────────────────────────────────

/* ── Workbook audit: what travels with the file besides the visible tables ─
   Masking covers the cells a person can see. But the workbook also carries
   what they cannot see: a hidden sheet with a lookup table, a named range, a
   link to a neighbouring file on a network drive, colleagues' comments and the
   author's surname in the properties. We do NOT remove any of it ourselves —
   a stray sheet should be deleted by the person who knows why it is there.
   Our job is to name the findings before the file goes to an external model. */
function auditWorkbook(wb) {
  const out = [];
  const sheetMeta = (wb.Workbook && wb.Workbook.Sheets) || [];
  const hidden = [], veryHidden = [];
  (wb.SheetNames || []).forEach((name, i) => {
    const st = sheetMeta[i] && sheetMeta[i].Hidden;
    if (st === 1) hidden.push(name);
    else if (st === 2) veryHidden.push(name);
  });
  if (hidden.length) out.push({ kind: 'hidden_sheet', items: hidden });
  if (veryHidden.length) out.push({ kind: 'very_hidden_sheet', items: veryHidden });

  const names = ((wb.Workbook && wb.Workbook.Names) || [])
    .filter(n => n && n.Name && !/^_xlnm\./.test(n.Name));
  if (names.length) out.push({ kind: 'defined_names', items: names.slice(0, 12).map(n => n.Name), count: names.length });

  let formulas = 0, comments = 0, hiddenLines = 0;
  const external = new Set();
  (wb.SheetNames || []).forEach(name => {
    const ws = wb.Sheets[name]; if (!ws) return;
    (ws['!rows'] || []).forEach(r => { if (r && r.hidden) hiddenLines++; });
    (ws['!cols'] || []).forEach(c => { if (c && c.hidden) hiddenLines++; });
    for (const k in ws) {
      if (k.charCodeAt(0) === 33) continue;
      const cell = ws[k];
      if (!cell) continue;
      if (cell.f) {
        formulas++;
        const m = /\[[^\]]+\]/.exec(cell.f);          // [1]Sheet!A1 or [C:\path\book.xlsx]
        if (m) external.add(m[0].slice(0, 60));
        else if (/https?:\/\/|\\\\/.test(cell.f)) external.add(cell.f.slice(0, 60));
      }
      if (cell.c && cell.c.length) comments += cell.c.length;
    }
  });
  if (external.size) out.push({ kind: 'external_refs', items: [...external].slice(0, 8), count: external.size });
  if (formulas) out.push({ kind: 'formulas', count: formulas });
  if (comments) out.push({ kind: 'comments', count: comments });
  if (hiddenLines) out.push({ kind: 'hidden_lines', count: hiddenLines });

  const props = wb.Props || {};
  const who = ['Author', 'LastAuthor', 'Company', 'Manager'].filter(k => props[k]);
  if (who.length) out.push({ kind: 'doc_props', items: who.map(k => k + ': ' + String(props[k]).slice(0, 40)) });
  return out;
}

self.onmessage = (e) => {
  const m = e.data;
  try {
    if (m.type === 'parse') {
      if (m.isCsv) {
        let txt = new TextDecoder('utf-8', { fatal: false }).decode(m.buf);
        if (/�/.test(txt)) { try { txt = new TextDecoder('windows-1251').decode(m.buf); } catch (_e) { /* keep */ } }
        WB = XLSX.read(txt, { type: 'string', cellDates: true });
      } else {
        WB = XLSX.read(new Uint8Array(m.buf), { type: 'array', cellDates: true, cellNF: true, cellStyles: true });
      }
      if (estimateCells(WB) > MAX_CELLS) { WB = null; self.postMessage({ type: 'error', message: 'spreadsheet too large to process in the browser' }); return; }
      self.postMessage({ type: 'parsed', model: buildModel(WB), audit: auditWorkbook(WB) });
    } else if (m.type === 'mask') {
      const { out, count, sample, left } = maskAndWrite(m.model, m.bookType);
      self.postMessage({ type: 'masked', buf: out, count, sample, left }, [out]);
    }
  } catch (err) {
    self.postMessage({ type: 'error', message: String((err && err.message) || err) });
  }
};
