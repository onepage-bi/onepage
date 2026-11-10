import { t } from './i18n.js';
/**
 * OnePage /explore engine v2 — "Export to Excel", without openpyxl.
 *
 * v1 exports through openpyxl: a PyPI wheel micropip installs lazily into the
 * Pyodide heap on the first export. v2 has no Python, and DuckDB has no xlsx
 * writer — `COPY … TO … (FORMAT XLSX)` is a native-only community extension
 * that is not in the wasm build and is not in our mirror. Rather than mark
 * export unsupported (the honest but unpleasant option), this is a minimal
 * writer: a ZIP container with the five parts Excel and openpyxl both need,
 * every cell an `inlineStr` or a bare number.
 *
 * What it deliberately does NOT do: shared strings (a dictionary would save
 * bytes on repetitive exports and cost a second pass), styles beyond one
 * default cell format, number formats, dates as Excel serials. A timestamp
 * exports as its ISO text — the same thing the result grid shows — which is
 * lossier than v1's real datetime cells and is called out in docs/ENGINE2.md.
 *
 * ~200 lines, no dependency, and the bytes are a real workbook: the Chrome
 * harness opens the downloaded file with openpyxl and counts its rows.
 */

const XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>';
const NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main';
const NS_R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships';

function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    // Control characters are illegal in XML 1.0 even escaped; a cell that
    // holds one would make the whole workbook unopenable, so they go.
    .replace(/[\x00-\x08\x0B\x0C\x0E-\x1F]/g, '');
}

/** 0 -> A, 25 -> Z, 26 -> AA … */
export function colName(i) {
  let s = '';
  let n = i;
  do {
    s = String.fromCharCode(65 + (n % 26)) + s;
    n = Math.floor(n / 26) - 1;
  } while (n >= 0);
  return s;
}

function cellXml(ref, v) {
  if (v === null || v === undefined || v === '') return `<c r="${ref}"/>`;
  if (typeof v === 'boolean') return `<c r="${ref}" t="b"><v>${v ? 1 : 0}</v></c>`;
  if (typeof v === 'number' && Number.isFinite(v)) return `<c r="${ref}"><v>${v}</v></c>`;
  if (typeof v === 'bigint') return `<c r="${ref}"><v>${v}</v></c>`;
  return `<c r="${ref}" t="inlineStr"><is><t xml:space="preserve">${esc(v)}</t></is></c>`;
}

function sheetXml(columns, rows, note) {
  const out = [XML_HEAD,
    `<worksheet xmlns="${NS}"><sheetData>`];
  let r = 1;
  out.push(`<row r="${r}">`
    + columns.map((c, i) => cellXml(`${colName(i)}${r}`, String(c))).join('') + '</row>');
  for (const row of rows) {
    r += 1;
    out.push(`<row r="${r}">`
      + row.map((v, i) => cellXml(`${colName(i)}${r}`, v)).join('') + '</row>');
  }
  if (note) {
    // Never hand over a silently trimmed file: the sheet says so itself —
    // and it says it the same way v1's openpyxl export does, one blank row
    // then the message, so the row count matches too.
    r += 2;
    out.push(`<row r="${r}">${cellXml(`A${r}`, note)}</row>`);
  }
  out.push('</sheetData></worksheet>');
  return out.join('');
}

const CONTENT_TYPES = XML_HEAD
  + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
  + '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
  + '<Default Extension="xml" ContentType="application/xml"/>'
  + '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
  + '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
  + '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
  + '</Types>';

const ROOT_RELS = XML_HEAD
  + `<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">`
  + `<Relationship Id="rId1" Type="${NS_R}/officeDocument" Target="xl/workbook.xml"/>`
  + '</Relationships>';

const WB_RELS = XML_HEAD
  + `<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">`
  + `<Relationship Id="rId1" Type="${NS_R}/worksheet" Target="worksheets/sheet1.xml"/>`
  + `<Relationship Id="rId2" Type="${NS_R}/styles" Target="styles.xml"/>`
  + '</Relationships>';

// One default font, fill, border and cell format. openpyxl reads styles.xml
// eagerly and a workbook without it is not one it will open.
const STYLES = XML_HEAD
  + `<styleSheet xmlns="${NS}">`
  + '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
  + '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
  + '<borders count="1"><border/></borders>'
  + '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
  + '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
  + '</styleSheet>';

function workbookXml(sheetName) {
  return XML_HEAD
    + `<workbook xmlns="${NS}" xmlns:r="${NS_R}"><sheets>`
    + `<sheet name="${esc(sheetName).slice(0, 31)}" sheetId="1" r:id="rId1"/>`
    + '</sheets></workbook>';
}

// ── ZIP ──────────────────────────────────────────────────────────────────
const CRC_TABLE = (() => {
  const tv = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
    tv[n] = c >>> 0;
  }
  return tv;
})();

function crc32(bytes) {
  let c = 0xFFFFFFFF;
  for (let i = 0; i < bytes.length; i++) c = CRC_TABLE[(c ^ bytes[i]) & 0xFF] ^ (c >>> 8);
  return (c ^ 0xFFFFFFFF) >>> 0;
}

async function deflateRaw(bytes) {
  // CompressionStream('deflate-raw') is exactly zip method 8. Where it is
  // missing the entry is STORED — a bigger file, never a broken one.
  if (typeof CompressionStream !== 'function') return null;
  try {
    const cs = new CompressionStream('deflate-raw');
    const w = cs.writable.getWriter();
    w.write(bytes);
    w.close();
    const parts = [];
    const reader = cs.readable.getReader();
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      parts.push(value);
    }
    const total = parts.reduce((a, p) => a + p.length, 0);
    const out = new Uint8Array(total);
    let off = 0;
    for (const p of parts) { out.set(p, off); off += p.length; }
    return out;
  } catch {
    return null;
  }
}

function u16(v) { return [v & 0xFF, (v >>> 8) & 0xFF]; }
function u32(v) { return [v & 0xFF, (v >>> 8) & 0xFF, (v >>> 16) & 0xFF, (v >>> 24) & 0xFF]; }

async function zip(entries) {
  const enc = new TextEncoder();
  const locals = [];
  const central = [];
  let offset = 0;
  for (const [name, text] of entries) {
    const raw = typeof text === 'string' ? enc.encode(text) : text;
    const packed = await deflateRaw(raw);
    const body = packed || raw;
    const method = packed ? 8 : 0;
    const nameBytes = enc.encode(name);
    const crc = crc32(raw);
    const head = [
      ...u32(0x04034b50), ...u16(20), ...u16(0), ...u16(method),
      ...u16(0), ...u16(0),                       // dos time/date: epoch
      ...u32(crc), ...u32(body.length), ...u32(raw.length),
      ...u16(nameBytes.length), ...u16(0),
    ];
    locals.push(new Uint8Array(head), nameBytes, body);
    central.push(new Uint8Array([
      ...u32(0x02014b50), ...u16(20), ...u16(20), ...u16(0), ...u16(method),
      ...u16(0), ...u16(0),
      ...u32(crc), ...u32(body.length), ...u32(raw.length),
      ...u16(nameBytes.length), ...u16(0), ...u16(0), ...u16(0), ...u16(0),
      ...u32(0), ...u32(offset),
    ]), nameBytes);
    offset += head.length + nameBytes.length + body.length;
  }
  const centralSize = central.reduce((a, p) => a + p.length, 0);
  const end = new Uint8Array([
    ...u32(0x06054b50), ...u16(0), ...u16(0),
    ...u16(entries.length), ...u16(entries.length),
    ...u32(centralSize), ...u32(offset), ...u16(0),
  ]);
  const all = [...locals, ...central, end];
  const total = all.reduce((a, p) => a + p.length, 0);
  const out = new Uint8Array(total);
  let off = 0;
  for (const p of all) { out.set(p, off); off += p.length; }
  return out;
}

/**
 * `{columns, rows}` -> the bytes of an .xlsx. `note` (optional) is appended
 * after a blank row, the way v1 appends its truncation warning.
 */
export async function writeXlsx(columns, rows, { sheet = t('Result {ctx:2}'), note = null } = {}) {
  return await zip([
    ['[Content_Types].xml', CONTENT_TYPES],
    ['_rels/.rels', ROOT_RELS],
    ['xl/workbook.xml', workbookXml(sheet)],
    ['xl/_rels/workbook.xml.rels', WB_RELS],
    ['xl/styles.xml', STYLES],
    ['xl/worksheets/sheet1.xml', sheetXml(columns || [], rows || [], note)],
  ]);
}

export default writeXlsx;
