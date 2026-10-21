// Minimal SpreadsheetApp mock + HTTP wrapper around Code.gs, for local end-to-end tests.
const fs = require('fs'), vm = require('vm'), http = require('http'), { execFileSync } = require('child_process');
const [codePath, csvPath, port, boardUrl] = process.argv.slice(2);

function parseCsv(text) {
  const rows = []; let row = [], cur = '', q = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (q) { if (ch === '"') { if (text[i + 1] === '"') { cur += '"'; i++; } else q = false; } else cur += ch; }
    else if (ch === '"') q = true;
    else if (ch === ',') { row.push(cur); cur = ''; }
    else if (ch === '\n') { row.push(cur); rows.push(row); row = []; cur = ''; }
    else if (ch !== '\r') cur += ch;
  }
  if (cur || row.length) { row.push(cur); rows.push(row); }
  return rows;
}
const conv = v => (v !== '' && !isNaN(Number(v)) ? Number(v) : v);

let mdSeq = 1;
class Sheet {
  constructor(name, id, data) {
    this.name = name; this.id = id; this.hidden = false;
    this.cols = Math.max(26, ...data.map(r => r.length));
    this.rows = data.map(r => ({ cells: r.map(conv), md: [], strike: false }));
    while (this.rows.length < 50) this.rows.push({ cells: [], md: [] });
  }
  getSheetId() { return this.id; } getName() { return this.name; } hideSheet() { this.hidden = true; }
  getMaxRows() { return this.rows.length; } getMaxColumns() { return this.cols; }
  getLastRow() { for (let r = this.rows.length; r >= 1; r--) if (this.rows[r - 1].cells.some(v => v !== '' && v != null)) return r; return 0; }
  getLastColumn() { let m = 0; this.rows.forEach(r => r.cells.forEach((v, i) => { if (v !== '' && v != null) m = Math.max(m, i + 1); })); return m; }
  get(r, c) { const v = this.rows[r - 1].cells[c - 1]; return v == null ? '' : v; }
  shown(r, c) { const row = this.rows[r - 1]; return row.disp && row.disp[c - 1] != null ? row.disp[c - 1] : String(this.get(r, c)); }
  set(r, c, v) { if (r > this.rows.length || c > this.cols) throw new Error('out of range ' + r + ',' + c); const row = this.rows[r - 1]; if (row.disp) delete row.disp[c - 1]; row.cells[c - 1] = v; }
  getRange(a, b, c, d) {
    if (typeof a === 'string') { const m = a.match(/^(\d+):(\d+)$/); return new Range(this, +m[1], 1, +m[2] - +m[1] + 1, this.cols, true); }
    return new Range(this, a, b, c || 1, d || 1, false);
  }
  getRangeList(list) {
    const each = fn => list.forEach(s => { const [r, c, nr, nc] = s.split(',').map(Number); fn(new Range(this, r, c, nr, nc)); });
    return { clearContent: () => each(g => g.clearContent()), setValue: v => each(g => g.setValue(v)), setFormulaR1C1: f => each(g => g.setFormulaR1C1(f)) };
  }
  insertRowAfter(r) { this.insertRowsAfter(r, 1); }
  insertRowsAfter(r, n) { for (let i = 0; i < n; i++) this.rows.splice(r, 0, { cells: [], md: [] }); }
  insertColumnsAfter(c, n) { this.cols += n; }
  deleteRows(r, n) { this.rows.splice(r - 1, n); }
  moveRows(range, dest) { const [row] = this.rows.splice(range.r - 1, 1); this.rows.splice(dest - 1 > range.r - 1 ? dest - 2 : dest - 1, 0, row); }
  clearContents() { this.rows.forEach(r => { r.cells = []; }); }
  createDeveloperMetadataFinder() {
    const sheet = this; let key = null;
    const f = { withKey(k) { key = k; return f; }, withLocationType() { return f; },
      find() { const out = []; sheet.rows.forEach(row => row.md.forEach(m => { if (key === null || m.key === key) out.push(new MD(sheet, row, m)); })); return out; } };
    return f;
  }
}
class MD {
  constructor(sheet, row, m) { this.sheet = sheet; this.row = row; this.m = m; }
  getKey() { return this.m.key; } getValue() { return this.m.value; }
  remove() { this.row.md = this.row.md.filter(x => x !== this.m); }
  getLocation() { const s = this.sheet, row = this.row; return { getLocationType: () => 'ROW', getRow: () => ({ getRow: () => s.rows.indexOf(row) + 1 }) }; }
}
class Range {
  constructor(s, r, c, nr, nc, whole) { Object.assign(this, { s, r, c, nr, nc, whole }); }
  getSheet() { return this.s; } getRow() { return this.r; } getColumn() { return this.c; }
  getLastRow() { return this.r + this.nr - 1; } getLastColumn() { return this.c + this.nc - 1; } getNumRows() { return this.nr; }
  getA1Notation() { return [this.r, this.c, this.nr, this.nc].join(','); }
  getValues() { const o = []; for (let i = 0; i < this.nr; i++) { const row = []; for (let j = 0; j < this.nc; j++) row.push(this.s.get(this.r + i, this.c + j)); o.push(row); } return o; }
  getValue() { return this.s.get(this.r, this.c); }
  getDisplayValues() { const o = []; for (let i = 0; i < this.nr; i++) { const row = []; for (let j = 0; j < this.nc; j++) row.push(this.s.shown(this.r + i, this.c + j)); o.push(row); } return o; }
  setValue(v) { this.s.set(this.r, this.c, v); return this; }
  setValues(vs) { vs.forEach((row, i) => row.forEach((v, j) => this.s.set(this.r + i, this.c + j, v))); return this; }
  clearContent() { for (let i = 0; i < this.nr; i++) for (let j = 0; j < this.nc; j++) if (this.r + i <= this.s.rows.length) this.s.rows[this.r + i - 1].cells[this.c + j - 1] = ''; return this; }
  copyFmt(dst) { for (let i = 0; i < this.nr; i++) { const src = this.s.rows[this.r + i - 1], to = dst.s.rows[dst.r + i - 1]; to.fmt = (src.fmt || []).slice(0); to.strike = !!src.strike; } }
  copyTo(dst, type) { if (type === 'F') return this.copyFmt(dst); if (type) return; const v = this.getValues(); dst.setValues(v); this.copyFmt(dst); }
  getFormulasR1C1() { return this.getValues().map(row => row.map(v => typeof v === 'string' && v[0] === '=' ? v : '')); }
  setFormulaR1C1(f) { this.s.set(this.r, this.c, f); return this; }
  setFontLine(l) { for (let i = 0; i < this.nr; i++) this.s.rows[this.r + i - 1].strike = l === 'line-through'; return this; }
  getFontLines() { const o = []; for (let i = 0; i < this.nr; i++) { const row = this.s.rows[this.r + i - 1]; o.push(Array(this.nc).fill(row && row.strike ? 'line-through' : 'none')); } return o; }
  addDeveloperMetadata(key, value) { this.s.rows[this.r - 1].md.push({ key, value, id: mdSeq++ }); return this; }
  getDeveloperMetadata() { const row = this.s.rows[this.r - 1]; return row.md.map(m => new MD(this.s, row, m)); }
}

const tracker = new Sheet('September 2026', 1594333729, parseCsv(fs.readFileSync(csvPath, 'utf8')));
if (process.env.DATE_LABELS) { // week labels and day headers stored as dates that only *display* as text
  tracker.rows.forEach(row => {
    row.disp = {};
    row.cells.forEach((v, i) => {
      if (typeof v === 'string' && /^(Week \d|Mon|Tue|Wed|Thu|Fri|Sat|Sun) /.test(v)) { row.disp[i] = v; row.cells[i] = new Date(2026, 7, 31 + i); }
    });
  });
}
// Input rows that have a week-1 total get formulas in every Total / Win-Loss cell and a "task" format.
tracker.rows.forEach(row => {
  if (row.cells[11] === '' || row.cells[11] == null) return;
  row.fmt = Array(tracker.cols).fill('task');
  for (let col = 4; col + 9 <= tracker.cols; col += 10) { row.cells[col + 7] = '=SUM(RC[-7]:RC[-1])'; row.cells[col + 8] = '=IF(R1C1)'; }
});
if (process.env.HEADER_ARRAY) { // like the real tracker: one array formula per header row, the rows below only show its results
  tracker.rows.forEach(row => {
    for (let col = 4; col + 9 <= tracker.cols; col += 10) {
      if (String(row.cells[0]).trim() === 'Input') { row.cells[col + 7] = '={"Week total";ARRAYFORMULA(IF($BL$29:$BL$32="","",MMULT(N($BM$29:$BS$32),SEQUENCE(7,1,1,0))))}'; row.cells[col + 8] = '={"Win / Loss";ARRAYFORMULA($BT$29:$BT$32)}'; }
      else if (typeof row.cells[col + 7] === 'string' && row.cells[col + 7][0] === '=') { row.cells[col + 7] = 0; row.cells[col + 8] = '0%'; }
    }
  });
}
const sheets = [tracker];
const ss = {
  getSheets: () => sheets, getSheetByName: n => sheets.find(s => s.name === n) || null,
  insertSheet: n => { const s = new Sheet(n, 99, []); sheets.push(s); return s; },
  getSpreadsheetTimeZone: () => 'America/New_York'
};
const props = { BOARD_URL: boardUrl, SYNC_SECRET: 's3cret' };
const log = [];
const ctx = {
  console, Date, Math, JSON, Number, String, Object, Array, RegExp, isFinite, Error,
  SpreadsheetApp: { openById: () => ss, DeveloperMetadataLocationType: { ROW: 'ROW' }, CopyPasteType: { PASTE_COLUMN_WIDTHS: 'W', PASTE_FORMAT: 'F' } },
  LockService: { getScriptLock: () => ({ waitLock() {}, tryLock() { return true; }, releaseLock() {} }) },
  CacheService: { getScriptCache: () => ({ get: k => (global.CACHE || (global.CACHE = {}))[k] || null, put: (k, v) => { (global.CACHE || (global.CACHE = {}))[k] = v; } }) },
  PropertiesService: { getScriptProperties: () => ({ getProperty: k => props[k] == null ? null : props[k], setProperty: (k, v) => { props[k] = v; }, deleteProperty: k => { delete props[k]; }, getProperties: () => Object.assign({}, props), setProperties: o => { Object.assign(props, o); } }) },
  ContentService: { createTextOutput: t => ({ text: t, setMimeType() { return this; } }), MimeType: { JSON: 'json' } },
  Utilities: {
    getUuid: () => require('crypto').randomUUID(),
    DigestAlgorithm: { MD5: 'md5' },
    computeDigest: (alg, s) => [...require('crypto').createHash(alg).update(s).digest()],
    base64Encode: bytes => Buffer.from(bytes).toString('base64'),
    formatDate: (d, tz, f) => {
      const p = new Intl.DateTimeFormat('en-CA', { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit', weekday: 'short' }).formatToParts(d);
      const g = t => p.find(x => x.type === t).value;
      if (f === 'u') return String(['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].indexOf(g('weekday')) + 1);
      return g('year') + '-' + g('month') + '-' + g('day');
    }
  },
  UrlFetchApp: {
    fetch(url, o) {
      log.push('fetch ' + url);
      const args = ['-s', '-m', '20', '-o', '-', '-w', '\n%{http_code}', url];
      if (o && o.method === 'post') args.push('-X', 'POST', '-H', 'Content-Type: application/json', '-H', 'X-Sync-Secret: ' + o.headers['X-Sync-Secret'], '--data-binary', '@-');
      let out; try { out = execFileSync('curl', args, { input: (o && o.payload) || '' }).toString(); } catch (e) { out = '\n000'; }
      const i = out.lastIndexOf('\n'), code = Number(out.slice(i + 1));
      return { getResponseCode: () => code, getContentText: () => out.slice(0, i) };
    }
  }
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(codePath, 'utf8') + '\nthis.__api = {doGet, doPost, handleEdit, handleChange, syncTick, ensureCurrentWeek, readModel_, withLock_, ensureWeeksThrough_, CacheService: this.CacheService};', ctx);
const api = ctx.__api;

function dump() {
  return tracker.rows.map((r, i) => ({ row: i + 1, a: r.cells[0] || '', md: r.md.map(m => m.key + '=' + m.value), strike: !!r.strike, fmt: (r.fmt || [])[0] || '', cells: r.cells }))
    .filter(r => r.a || r.md.length || r.strike);
}

http.createServer((req, res) => {
  let body = '';
  req.on('data', d => { body += d; });
  req.on('end', () => {
    const u = new URL(req.url, 'http://x');
    let out;
    if (u.pathname === '/down') { global.DOWN = u.searchParams.get('v') === '1'; res.end('{}'); return; }
    if (global.DOWN && u.pathname === '/exec') { res.writeHead(503); res.end('down'); return; }
    try {
      if (u.pathname === '/exec' && req.method === 'GET') out = api.doGet({ parameter: Object.fromEntries(u.searchParams) }).text;
      else if (u.pathname === '/exec') out = api.doPost({ postData: { contents: body } }).text;
      else if (u.pathname === '/edit') { // simulate a user edit: {row, col, value | values}
        const e = JSON.parse(body);
        const vals = e.values || [[e.value]];
        tracker.cols = Math.max(tracker.cols, e.col + vals[0].length - 1);
        const rg = tracker.getRange(e.row, e.col, vals.length, vals[0].length);
        rg.setValues(vals);
        if (!e.noTrigger) api.handleEdit({ range: rg });
        out = JSON.stringify({ ok: true });
      } else if (u.pathname === '/editQueued') { // the user edits now; Google runs the trigger later
        const e = JSON.parse(body), vals = e.values || [[e.value]];
        const rg = tracker.getRange(e.row, e.col, vals.length, vals[0].length);
        rg.setValues(vals);
        (global.QUEUE = global.QUEUE || []).push({ rg, t0: Date.now() });
        out = '{"ok":true}';
      } else if (u.pathname === '/runQueued') { // ...and here it finally runs, knowing only when it was triggered
        (global.QUEUE || []).splice(0).forEach(q => {
          const real = Date.now; let first = true;
          Date.now = () => { if (first) { first = false; return q.t0; } return real(); };
          try { api.handleEdit({ range: q.rg }); } finally { Date.now = real; }
        });
        out = '{"ok":true}';
      } else if (u.pathname === '/deleteRow') { const e = JSON.parse(body); tracker.deleteRows(e.row, 1); api.handleChange({ changeType: 'REMOVE_ROW' }); out = '{"ok":true}'; }
      else if (u.pathname === '/unstrike') { const e = JSON.parse(body); tracker.getRange(e.row, 1, 1, 3).setFontLine('none'); if (!e.noTrigger) api.handleChange({ changeType: 'FORMAT' }); out = '{"ok":true}'; } // strike taken off by hand
      else if (u.pathname === '/moveRow') { const e = JSON.parse(body); tracker.moveRows(tracker.getRange(e.row + ':' + e.row), e.dest); api.handleChange({ changeType: 'OTHER' }); out = '{"ok":true}'; }
      else if (u.pathname === '/insertRow') { // inherit: copy the strike of the row above, as Google Sheets copies formatting
        const e = JSON.parse(body); tracker.insertRowAfter(e.after);
        if (e.inherit) { const src = tracker.rows[e.inherit === 'below' ? e.after + 1 : e.after - 1]; tracker.rows[e.after].strike = !!(src && src.strike); tracker.rows[e.after].fmt = ((src && src.fmt) || []).slice(0); }
        api.handleChange({ changeType: 'INSERT_ROW' }); out = '{"ok":true}'; }
      else if (u.pathname === '/setMd') { // replace a row's sync tags, e.g. an old "input" tag on a heading row
        const e = JSON.parse(body), row = tracker.rows[e.row - 1];
        row.md = row.md.filter(m => m.key.indexOf('itb_') !== 0).concat([{ key: e.key, value: e.value, id: mdSeq++ }]);
        global.CACHE = {}; out = '{"ok":true}';
      }
      else if (u.pathname === '/install') { global.CACHE = {}; api.withLock_(() => api.readModel_()); out = '{"ok":true}'; } // as installTriggers / checkSetup
      else if (u.pathname === '/tick') { api.syncTick(); out = JSON.stringify({ ok: true, props }); }
      else if (u.pathname === '/week') { if (u.searchParams.get('to')) { global.CACHE = {}; api.withLock_(() => api.ensureWeeksThrough_(u.searchParams.get('to'))); } else api.ensureCurrentWeek(); out = JSON.stringify({ ok: true }); }
      else if (u.pathname === '/dump') out = JSON.stringify({ rows: dump(), row1: tracker.rows[0].cells, props, log: log.splice(0), stamps: (ss.getSheetByName('_sync') || { rows: [] }).rows.filter(r => r.cells[0]).length });
      else if (u.pathname === '/cell') out = JSON.stringify(tracker.get(+u.searchParams.get('r'), +u.searchParams.get('c')));
    } catch (err) { out = JSON.stringify({ ok: false, error: String(err.stack || err) }); }
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(out || '{}');
  });
}).listen(+port, () => console.log('mock sheet on ' + port));
