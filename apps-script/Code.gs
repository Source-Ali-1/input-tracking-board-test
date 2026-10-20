/**
 * Input Tracking Board <-> Google Sheet two-way sync (sheet side).
 *
 * Paste into Extensions > Apps Script of the tracker spreadsheet, set the
 * script properties BOARD_URL and SYNC_SECRET, run installTriggers() once and
 * deploy as a web app. See SETUP.md.
 *
 *   sheet edit  -> handleEdit (installable onEdit) -> POST {BOARD_URL}/api/sync/inbound
 *   board edit  -> board POSTs here -> doPost writes the cells
 *   catch-up    -> board GETs doGet every minute (full snapshot with timestamps);
 *                  syncTick re-sends edits the board missed while it was down
 *   new week    -> ensureCurrentWeek (Monday trigger, also checked every minute)
 *                  appends the next 10-column week block
 *
 * Layout this script understands (the tracker tab):
 *   person block a name row ("ORLANDO"), a month row ("September 2026"), then a
 *                header row starting with "Input" whose day headers ("Mon 8/31",
 *                "Tue 9/1", ...) mark the weeks, then section rows (only column A
 *                filled) and input rows.
 *   week block   10 columns: Target | Mon..Sun | Total | Win / Loss
 *   columns A-C  Input | Goal served (or Notes) | Unit ("check" = a daily tick box, anything else = a number)
 *
 * Rows are tied to board ids with developer metadata, so moving or inserting
 * rows never breaks the link. Conflicts are last-write-wins per cell; edit
 * timestamps live in the hidden "_sync" tab.
 */

const CONFIG = {
  // LOCAL TEST: your copy. Switch back to 1B5wWCAhUaPApbTRGekl6QuFzd5BUl0qtYJ0UOj85MC0 (the real tracker) after testing.
  SPREADSHEET_ID: '1n9YNg9ZCJOK8oWQbdBsCtu-n730Vc_dQt44-_SOz23U',
  TAB_GID: 1594333729,   // the tracker tab
  STAMP_TAB: '_sync',    // hidden tab: last-edit timestamp of every synced cell
  WEEK_WIDTH: 10,        // Target, Mon..Sun, Total, Win / Loss
  FUTURE_WEEKS: 1,       // board edits may create week blocks this far ahead
  PUSH_CHUNK: 200
};
const MD = { member: 'itb_member', cat: 'itb_cat', task: 'itb_task', archived: 'itb_archived' };
const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const MONTH_NAME = '(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\\.?';
const MONTH_YEAR_RE = new RegExp('\\s*\\b' + MONTH_NAME + '\\s+(\\d{4})\\s*$', 'i'); // "ORLANDO September 2026"
const MONTH_ROW_RE = new RegExp('^\\s*' + MONTH_NAME + '(\\s+\\d{4})?\\s*$', 'i');     // a row that is only "Oct 2026"
function monthNum_(name) { return ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'].indexOf(String(name).slice(0, 3).toLowerCase()) + 1; }

// ======================= entry points =======================

/** Board -> sheet: snapshot of every synced cell with its last-edit time. */
function doGet(e) {
  const p = (e && e.parameter) || {};
  if (!secretOk_(p.secret)) return json_({ ok: false, error: 'unauthorized' });
  // Normally answered straight from the remembered layout, without waiting for (or blocking) edits.
  try {
    const model = readModel_(true);
    if (model) return json_(Object.assign({ ok: true }, snapshot_(model, p.since)));
  } catch (err) {
    console.warn('quick snapshot failed, taking the slow path: ' + err);
  } finally {
    STAMPS = null; // read only: nothing to save
  }
  // The layout changed (or isn't remembered yet): read it properly, without holding up edits for long.
  return withLock_(() => {
    ensureWeeksThrough_(currentWeek_());
    const model = readModel_();
    noteSetups_(model); // a changed person's setup is stamped now, so the board takes it
    return json_(Object.assign({ ok: true }, snapshot_(model, p.since)));
  }, true, 1500);
}

/** Board -> sheet: apply the board's changes (last write wins). */
function doPost(e) {
  let body;
  try { body = JSON.parse(e.postData.contents); } catch (err) { return json_({ ok: false, error: 'bad json' }); }
  if (!secretOk_(body.secret)) return json_({ ok: false, error: 'unauthorized' });
  return withLock_(() => {
    const result = applyBoardChanges_(Array.isArray(body.changes) ? body.changes : []);
    return json_(Object.assign({ ok: true }, result));
  }, true);
}

/** Installable onEdit trigger: push the edited cells to the board right away. */
function handleEdit(e) {
  if (!e || !e.range || e.range.getSheet().getSheetId() !== CONFIG.TAB_GID) return;
  // When the edit happened. Runs of this trigger queue up behind each other, so the time a run
  // gets to work can be many seconds later; stamping with that would let an older sheet edit
  // overwrite a board entry made while it waited.
  const edited = Date.now();
  let out = [];
  withLock_(() => { out = collectEdit_(e.range, edited); });
  pushToBoard_(out, { started: edited }); // outside the lock, so the next edit doesn't wait for the board
}

/** The changes one edit makes, stamped with the time of the edit (never over a newer stamp). */
function collectEdit_(rg, edited) {
  const model = readModel_(), now = Date.now();
  const r1 = Math.min(rg.getLastRow(), model.lastRow), c1 = Math.min(rg.getLastColumn(), Math.max(model.lastCol, 3));
  const changes = [];
  let structural = Object.keys(NEW_SETUP).length > 0;
  for (let r = rg.getRow(); r <= r1; r++) {
    const b = blockOf_(model, r);
    if (!b) continue;
    const t = model.taskAt[r];
    if (!t) structural = true; // a section, name or new row: people's inputs may have changed
    for (let c = rg.getColumn(); c <= c1; c++) {
      if (c <= 3) { structural = true; continue; } // name / goal / unit: picked up by noteSetups_ below
      const pos = t && colInfo_(model, c);
      if (!pos) continue;
      const key = pos.day < 0 ? kt_(b.id, pos.week.week, t.id) : kc_(b.id, pos.week.week, t.id, pos.day);
      if (stamp_(key) >= edited) continue; // the board wrote this cell after the edit: its value stands
      setStamp_(key, edited);
      changes.push(pos.day < 0 ? targetChange_(model, b, t, pos.week, edited) : cellChange_(model, b, t, pos.week, pos.day, edited));
    }
  }
  if (!structural) return changes;
  const setups = noteSetups_(model);
  Object.keys(NEW_SETUP).forEach(id => { // brand-new person / rows that just got ids
    if (model.blockById[id] && !setups.some(x => x.member === id)) { setStamp_(ks_(id), now); setups.push(setupOf_(model, model.blockById[id])); }
  });
  return setups.concat(changes);
}

/** Installable onChange trigger: rows inserted, deleted, moved or sorted can change a person's inputs. */
function handleChange(e) {
  if (!e || e.changeType === 'EDIT' || e.changeType === 'FORMAT') return;
  withLock_(() => pushToBoard_(noteSetups_(readModel_())));
}

/** Every minute: make sure this week exists, and re-send edits the board missed. */
function syncTick() {
  withLock_(() => {
    ensureWeeksThrough_(currentWeek_());
    const cache = CacheService.getScriptCache();
    if (!cache.get('formulasChecked')) { // e.g. a person block copied by hand
      ensureFormulas_(readModel_());
      cache.put('formulasChecked', '1', 1800);
    }
    pushToBoard_(noteSetups_(readModel_())); // anything about people's rows that changed unnoticed
    const sp = PropertiesService.getScriptProperties();
    const since = Number(sp.getProperty('BACKLOG_SINCE') || 0);
    if (!since) return;
    sp.deleteProperty('BACKLOG_SINCE');
    const model = readModel_(), map = stamps_().map, changes = [];
    Object.keys(map).forEach(k => {
      if (map[k] < since) return;
      const ch = changeFromKey_(model, k, map[k]);
      if (ch) changes.push(ch);
    });
    changes.sort((a, b) => (a.kind === 'setup' ? 0 : 1) - (b.kind === 'setup' ? 0 : 1));
    pushToBoard_(changes); // re-queues itself if the board is still unreachable
  });
}

/** Weekly trigger (Monday 00:05): add the new week's block. Safe to run any time. */
function ensureCurrentWeek() {
  withLock_(() => ensureWeeksThrough_(currentWeek_()));
}

/** Run from the editor to repair every Week total / Win-Loss cell now (syncTick also does it every 30 min). */
function fixFormulas() {
  withLock_(() => console.log('Week total / Win-Loss cells rewritten: ' + ensureFormulas_(readModel_())));
}

/** Run once from the editor to (re)create all triggers. */
function installTriggers() {
  ScriptApp.getProjectTriggers().forEach(t => ScriptApp.deleteTrigger(t));
  const ss = spreadsheet_();
  ScriptApp.newTrigger('handleEdit').forSpreadsheet(ss).onEdit().create();
  ScriptApp.newTrigger('handleChange').forSpreadsheet(ss).onChange().create();
  ScriptApp.newTrigger('syncTick').timeBased().everyMinutes(1).create();
  ScriptApp.newTrigger('ensureCurrentWeek').timeBased().onWeekDay(ScriptApp.WeekDay.MONDAY).atHour(0).nearMinute(5).create();
  withLock_(() => readModel_()); // tag every row with its id now
}

/** Run from the editor to check the board can be reached and the layout parses. */
function checkSetup() {
  const p = props_();
  console.log('BOARD_URL=' + p.url + ', SYNC_SECRET ' + (p.secret ? 'set' : 'MISSING'));
  if (p.url) console.log('board health: ' + UrlFetchApp.fetch(p.url + '/health', { muteHttpExceptions: true }).getContentText());
  withLock_(() => {
    bumpStructure_(); // time a full read, then a cached one
    let t0 = Date.now();
    readModel_();
    const cold = Date.now() - t0;
    t0 = Date.now();
    const m = readModel_();
    console.log('reading the sheet: ' + cold + ' ms full, ' + (Date.now() - t0) + ' ms cached');
    const hdr = m.blocks[0] && m.blocks[0].headerRow;
    if (hdr) console.log('first header row ' + hdr + ': ' + [4, 5, 6, 11, 12, 13, 14, 15, 16].map(c => '"' + m.text(hdr, c) + '"').join(' '));
    console.log('weeks: ' + m.weeks.map(w => 'W' + w.n + ' ' + w.week + ' @col ' + w.col).join(', '));
    m.blocks.forEach(b => console.log(b.name + ' rows ' + b.startRow + '-' + b.endRow + ': ' +
      b.cats.length + ' sections, ' + b.tasks.length + ' inputs'));
  });
}

// ======================= sheet model =======================

let NEW_SETUP = {}; // member ids whose rows got new ids in this execution
let BOARD_ROWS = {}; // input ids the board added in this execution

/**
 * Parses the tracker tab into person blocks, sections and inputs, giving every
 * person/section/input row a stable id (row developer metadata) as it goes.
 */
function readModel_(cachedOnly) {
  const sh = tab_();
  const lastRow = Math.max(sh.getLastRow(), 1), lastCol = Math.max(sh.getLastColumn(), 3);
  const range = sh.getRange(1, 1, lastRow, lastCol);
  const vals = range.getValues();
  // Labels are read as shown: day headers may be dates or formulas formatted as text.
  const shown = range.getDisplayValues();
  const cell = (r, c) => (r >= 1 && r <= lastRow && c >= 1 && c <= lastCol) ? vals[r - 1][c - 1] : '';
  const text = (r, c) => (r >= 1 && r <= lastRow && c >= 1 && c <= lastCol) ? String(shown[r - 1][c - 1]).trim() : '';
  const weeks = parseWeeks_(shown);
  // An input has a unit, a goal or a KPI in some week. A row with none of them is a section heading
  // ("Content", "Habits"), even with a stray number logged in it ("Finance").
  const hasKpi = r => weeks.some(w => num_(cell(r, w.col)) > 0);
  const model = { sh, lastRow, lastCol, weeks, weekByKey: {}, blocks: [], blockById: {}, taskById: {}, taskAt: {}, templates: {}, cell, text };
  weeks.forEach(w => { model.weekByKey[w.week] = w; });

  // Reading the row ids (developer metadata) is by far the slowest step, so the parsed
  // structure is cached. The key changes whenever anything that shapes it changes:
  // columns A-C, the header rows, which label-only rows hold data, or ids we wrote.
  // The prefix changes whenever the cached shape changes (model3: blocks carry idRow), so an
  // update never reuses a structure an older script cached.
  const sig = shown.map((row, i) => {
    const r = i + 1, base = row[0] + '\t' + row[1] + '\t' + row[2];
    if (/^input$/i.test(String(row[0]).trim())) return base + '\t' + row.join('\t');
    return row[0] && !row[1] && !row[2] && hasKpi(r) ? base + '\t*' : base;
  }).join('\n');
  const key = 'model3:' + structureEpoch_() + ':' + Utilities.base64Encode(Utilities.computeDigest(Utilities.DigestAlgorithm.MD5, sig));
  const cache = CacheService.getScriptCache(), hit = cache.get(key);
  if (hit) return hydrateModel_(model, JSON.parse(hit));
  if (cachedOnly) return null; // callers without the lock mustn't tag rows

  const md = rowMetadata_(sh);
  const headers = [];
  for (let r = 1; r <= lastRow; r++) if (/^input$/i.test(text(r, 1))) headers.push(r);
  headers.forEach((h, i) => {
    // The name row is right above the header, or above a month row ("September 2026", "Oct").
    // The person's id tag may sit on either of those two rows (or the header): it is kept wherever
    // it is, so the person never gets a second id. The name always comes from the row that has one.
    const prev = i ? headers[i - 1] : 0;
    const cands = [h - 1, h - 2].filter(r => r > prev && r >= 1);
    const named = r => { const t = text(r, 1); return !!stripMonth_(t) && !MONTH_ROW_RE.test(t) && !/^input$/i.test(t); };
    const tagged = cands.concat([h]).filter(r => md.member[r]);
    const titleRow = tagged.find(named) || cands.find(named) || tagged[0] || cands[0] || h;
    // Two tags: keep the id the sync already knows (it has a stamp), so the board keeps its person.
    const known = r => stamp_(ks_(md.member[r].id)) > 0;
    const idRow = md.member[titleRow] && known(titleRow) ? titleRow
      : tagged.find(known) || (md.member[titleRow] ? titleRow : tagged[0]) || titleRow;
    tagged.forEach(r => { if (r !== idRow) { md.member[r].md.remove(); delete md.member[r]; } }); // one id per person
    const name = stripMonth_(text(titleRow, 1));
    model.blocks.push({ name: name || 'Person ' + (i + 1), titleRow, idRow, headerRow: h, startRow: Math.min(titleRow, idRow, h), cats: [], tasks: [] });
  });
  model.blocks.forEach((b, i) => { b.endRow = i + 1 < model.blocks.length ? model.blocks[i + 1].startRow - 1 : lastRow; });

  const seen = {}, added = [], fix = {}; // added: input rows that got their id just now; fix: blocks needing formulas
  let fresh = false;
  const claim = (kind, r, prefix) => {
    const ex = md[kind][r];
    if (ex && !seen[ex.id]) { seen[ex.id] = true; return ex.id; }
    if (ex) ex.md.remove(); // a copied row carried someone else's id
    const id = newId_(prefix);
    sh.getRange(r + ':' + r).addDeveloperMetadata(MD[kind], id);
    seen[id] = true;
    fresh = true;
    return id;
  };
  const drop = (kind, r) => { if (md[kind][r]) { md[kind][r].md.remove(); delete md[kind][r]; } };

  model.blocks.forEach(b => {
    fresh = false;
    b.id = claim('member', b.idRow, 'm');
    let cat = null;
    b.lastUsedRow = b.headerRow;
    for (let r = b.headerRow + 1; r <= b.endRow; r++) {
      const a = text(r, 1);
      if (!a) continue;
      b.lastUsedRow = r;
      // BOARD_ROWS: an input the board is adding right now (its KPI is written after its row).
      const detail = text(r, 2) || text(r, 3) || hasKpi(r);
      if (detail || (md.task[r] && BOARD_ROWS[md.task[r].id])) {
        drop('cat', r);
        if (!cat) { cat = { id: 'gen' + b.id, name: 'General', row: 0 }; b.cats.push(cat); }
        const hadId = !!md.task[r];
        const t = { id: claim('task', r, 't'), catId: cat.id, name: a, goal: text(r, 2), unit: text(r, 3), row: r, archived: !!md.archived[r] };
        t.type = typeForUnit_(t.unit);
        if (!hadId) added.push({ b, t });
        b.tasks.push(t);
        model.taskAt[r] = t;
        model.taskById[t.id] = { task: t, block: b };
        if (!model.templates.task && !t.archived) model.templates.task = r;
      } else {
        drop('task', r);
        cat = { id: claim('cat', r, 'c'), name: a, row: r, archived: !!md.archived[r] };
        b.cats.push(cat);
        if (!model.templates.section) model.templates.section = r;
      }
    }
    model.blockById[b.id] = b;
    if (fresh) { NEW_SETUP[b.id] = true; setStamp_(ks_(b.id), Date.now()); fix[b.id] = true; }
  });
  // A row someone inserted by hand gets the look of its neighbours. Rows of a block that is
  // being tagged for the first time (installing the sync) keep theirs. Both get the formulas.
  added.forEach(x => { if (x.b.tasks.some(o => o !== x.t && md.task[o.row])) completeRow_(model, x.b, x.t); fix[x.b.id] = true; });
  Object.keys(fix).forEach(id => ensureFormulas_(model, id));
  if (Object.keys(BOARD_ROWS).length) return model; // not cached: those rows are classified by an exception
  const plain = { templates: model.templates, blocks: model.blocks.map(b => Object.assign({}, b, { tasks: b.tasks.map(t => Object.assign({}, t)) })) };
  cache.put(key, JSON.stringify(plain), 21600);
  return model;
}

/** Rebuilds the lookup maps of a model from its cached structure. */
function hydrateModel_(model, plain) {
  model.templates = plain.templates || {};
  model.blocks = plain.blocks;
  model.blocks.forEach(b => {
    model.blockById[b.id] = b;
    b.tasks.forEach(t => {
      model.taskAt[t.row] = t;
      model.taskById[t.id] = { task: t, block: b };
    });
  });
  return model;
}

/** Cached structures are tied to this number; bump it whenever the script changes row ids. */
function structureEpoch_() {
  return PropertiesService.getScriptProperties().getProperty('STRUCTURE_EPOCH') || '0';
}

function bumpStructure_() {
  const next = String(Number(structureEpoch_()) + 1);
  PropertiesService.getScriptProperties().setProperty('STRUCTURE_EPOCH', next);
  return next;
}

/** Every row id in one metadata search (one search per key was much slower). */
function rowMetadata_(sh) {
  const out = {};
  Object.keys(MD).forEach(kind => { out[kind] = {}; });
  const kindOf = {};
  Object.keys(MD).forEach(kind => { kindOf[MD[kind]] = kind; });
  sh.createDeveloperMetadataFinder()
    .withLocationType(SpreadsheetApp.DeveloperMetadataLocationType.ROW).find()
    .forEach(m => {
      const kind = kindOf[m.getKey()];
      if (!kind) return;
      const r = m.getLocation().getRow().getRow();
      if (out[kind][r]) m.remove(); else out[kind][r] = { id: m.getValue(), md: m };
    });
  return out;
}

/**
 * Week blocks from the Monday headers ("Mon 8/31") of the "Input" header row that has
 * the most of them (some people's header rows are incomplete). The year comes from the
 * first "Month YYYY" in column A. col = the block's Target column, left of Monday.
 */
function parseWeeks_(shown) {
  let mondays = [], tm = null;
  shown.forEach(row => {
    if (!tm) tm = String(row[0]).match(MONTH_YEAR_RE);
    if (!tm) { const only = String(row[0]).match(MONTH_ROW_RE); if (only && only[2]) tm = [only[0], only[1], only[2].trim()]; }
    if (!/^input$/i.test(String(row[0]).trim())) return;
    const found = [];
    row.forEach((v, i) => {
      const m = String(v).trim().match(/^mon\w*\.?\s+(\d{1,2})\/(\d{1,2})/i);
      if (m && i >= 1) found.push({ i, mon: Number(m[1]), day: Number(m[2]) });
    });
    if (found.length > mondays.length) mondays = found;
  });
  const titleMonth = tm ? monthNum_(tm[1]) : 0;
  let year = tm ? Number(tm[2]) : new Date().getFullYear(), prev = 0;
  return mondays.map((x, n) => {
    if (!prev && titleMonth && x.mon - titleMonth > 6) year--; // a December week in a "January" tracker
    if (prev && x.mon < prev) year++;
    prev = x.mon;
    return { n: n + 1, col: x.i, week: ymd_(year, x.mon, x.day) }; // Monday is column x.i + 1
  });
}

function colInfo_(model, c) {
  for (const w of model.weeks) {
    const off = c - w.col;
    if (off >= 0 && off < CONFIG.WEEK_WIDTH) return off === 0 ? { week: w, day: -1 } : off <= 7 ? { week: w, day: off - 1 } : null;
  }
  return null;
}

function blockOf_(model, r) {
  return model.blocks.find(b => r >= b.startRow && r <= b.endRow) || null;
}

/** The week block whose targets count as a person's current targets. */
function currentBlockWeek_(model) {
  const cw = currentWeek_();
  let best = null;
  model.weeks.forEach(w => { if (w.week <= cw) best = w; });
  return best || model.weeks[model.weeks.length - 1] || null;
}

// ======================= change messages =======================

function setupOf_(model, b) {
  const w = currentBlockWeek_(model);
  return {
    kind: 'setup', member: b.id, name: b.name,
    // A section removed on the board is left out, unless someone has since added an input under it.
    cats: b.cats.filter(c => !c.archived || b.tasks.some(t => t.catId === c.id && !t.archived)).map(c => ({ id: c.id, name: c.name })),
    tasks: b.tasks.filter(t => !t.archived).map(t => ({
      id: t.id, catId: t.catId, name: t.name, goal: t.goal, unit: t.unit, type: t.type,
      target: w ? num_(model.cell(t.row, w.col)) : 0
    })),
    ts: stamp_(ks_(b.id))
  };
}

function cellChange_(model, b, t, w, d, ts) {
  return { kind: 'cell', member: b.id, week: w.week, task: t.id, day: d,
    value: num_(model.cell(t.row, w.col + 1 + d)), target: num_(model.cell(t.row, w.col)), ts };
}

function targetChange_(model, b, t, w, ts) {
  return { kind: 'target', member: b.id, week: w.week, task: t.id, value: num_(model.cell(t.row, w.col)), ts };
}

function changeFromKey_(model, key, ts) {
  const p = key.split('|'), b = model.blockById[p[1]];
  if (!b) return null;
  if (p[0] === 's') return setupOf_(model, b);
  const ref = model.taskById[p[3]], w = model.weekByKey[p[2]];
  if (!ref || !w) return null;
  if (p[0] === 't') return targetChange_(model, b, ref.task, w, ts);
  if (p[0] === 'c') return cellChange_(model, b, ref.task, w, Number(p[4]), ts);
  return null;
}

function snapshot_(model, since) {
  const weeks = model.weeks.filter(w => !since || w.week >= since);
  const cells = [], targets = [];
  model.blocks.forEach(b => b.tasks.forEach(t => weeks.forEach(w => {
    targets.push([b.id, w.week, t.id, num_(model.cell(t.row, w.col)), stamp_(kt_(b.id, w.week, t.id))]);
    for (let d = 0; d < 7; d++) {
      cells.push([b.id, w.week, t.id, d, num_(model.cell(t.row, w.col + 1 + d)), stamp_(kc_(b.id, w.week, t.id, d))]);
    }
  })));
  const members = model.blocks.map(b => {
    const s = setupOf_(model, b);
    return { id: b.id, name: s.name, cats: s.cats, tasks: s.tasks, setupTs: s.ts };
  });
  return { currentWeek: currentWeek_(), weeks: weeks.map(w => w.week), members, cells, targets };
}

function pushToBoard_(changes, diag) {
  if (!changes.length) return true;
  const p = props_();
  try {
    if (!p.url || !p.secret) throw new Error('BOARD_URL / SYNC_SECRET not set');
    for (let i = 0; i < changes.length; i += CONFIG.PUSH_CHUNK) {
      const res = UrlFetchApp.fetch(p.url + '/api/sync/inbound', {
        method: 'post', contentType: 'application/json', muteHttpExceptions: true,
        headers: { 'X-Sync-Secret': p.secret },
        payload: JSON.stringify({ currentWeek: currentWeek_(), changes: changes.slice(i, i + CONFIG.PUSH_CHUNK),
          diag: diag ? { started: diag.started, sent: Date.now() } : undefined })
      });
      if (res.getResponseCode() !== 200) throw new Error('HTTP ' + res.getResponseCode());
    }
    return true;
  } catch (err) {
    // Remember where the backlog starts; syncTick re-sends everything stamped since then.
    const since = changes.reduce((m, c) => Math.min(m, c.ts || Date.now()), Date.now());
    const sp = PropertiesService.getScriptProperties(), cur = Number(sp.getProperty('BACKLOG_SINCE') || 0);
    sp.setProperty('BACKLOG_SINCE', String(cur ? Math.min(cur, since) : since));
    console.warn('Board unreachable, will retry: ' + err);
    return false;
  }
}

// ======================= applying board changes =======================

function applyBoardChanges_(changes) {
  const res = { applied: 0, stale: 0, skipped: [] };
  const horizon = addDays_(currentWeek_(), 7 * CONFIG.FUTURE_WEEKS);
  const latest = changes.filter(c => (c.kind === 'cell' || c.kind === 'target') && /^\d{4}-\d{2}-\d{2}$/.test(c.week) && c.week <= horizon)
    .map(c => c.week).sort().pop();
  if (latest) ensureWeeksThrough_(latest);
  let model = readModel_();
  const writes = [];
  changes.forEach(ch => {
    if (ch.kind === 'setup' || ch.kind === 'delete') {
      flushWrites_(model.sh, writes); // structural changes move rows; write pending cells first
      if (ch.kind === 'setup') {
        if (Number(ch.ts) > stamp_(ks_(ch.member))) {
          applySetup_(ch);
          setStamp_(ks_(ch.member), Number(ch.ts));
          noteSetups_(readModel_(), ch.member);
          res.applied++;
        } else {
          res.stale++;
          // The sheet had this person (it has a newer stamp) but no longer has their block: they were
          // deleted here, or their block now carries another id. The board drops its stale copy.
          if (!model.blockById[ch.member]) (res.gone = res.gone || []).push(ch.member);
        }
      } else {
        const why = deleteBlock_(ch.member);
        if (why) { res.skipped.push(why); (res.refused = res.refused || []).push(ch.member); } else res.applied++;
      }
      model = readModel_();
      return;
    }
    if (ch.kind !== 'cell' && ch.kind !== 'target') return;
    const b = model.blockById[ch.member], ref = model.taskById[ch.task], w = model.weekByKey[ch.week];
    const day = Number(ch.day), isTarget = ch.kind === 'target';
    if (!b || !ref || !w || (!isTarget && !(day >= 0 && day <= 6))) {
      res.skipped.push(ch.kind + ' ' + ch.member + '/' + ch.task + ' ' + ch.week);
      return;
    }
    const key = isTarget ? kt_(b.id, w.week, ref.task.id) : kc_(b.id, w.week, ref.task.id, day);
    if (!(Number(ch.ts) > stamp_(key))) { res.stale++; return; }
    const col = isTarget ? w.col : w.col + 1 + day, cur = model.cell(ref.task.row, col), v = num_(ch.value);
    if (num_(cur) !== v) writes.push({ row: ref.task.row, col, value: typeof cur === 'boolean' ? v > 0 : v });
    setStamp_(key, Number(ch.ts));
    res.applied++;
  });
  flushWrites_(model.sh, writes);
  return res;
}

function flushWrites_(sh, writes) {
  writes.splice(0).forEach(w => sh.getRange(w.row, w.col).setValue(w.value));
}

/** Make a person's block match the board: name, sections, inputs and current targets. */
function applySetup_(ch) {
  const id = ch.member, name = String(ch.name || '').trim() || 'Unnamed';
  const cats = Array.isArray(ch.cats) ? ch.cats : [], tasks = Array.isArray(ch.tasks) ? ch.tasks : [];
  let model = readModel_(), b = model.blockById[id];
  if (!b) {
    createBlock_(id, name);
    model = readModel_();
    b = model.blockById[id];
    if (!b) throw new Error('could not create a block for ' + name);
  }
  const sh = model.sh;
  if (b.name !== name) {
    const title = model.text(b.titleRow, 1), m = title.match(MONTH_YEAR_RE);
    sh.getRange(b.titleRow, 1).setValue(name + (m && stripMonth_(title) ? m[0] : ''));
  }

  cats.forEach(c => {
    if (c.id === 'gen' + id) return; // the implicit group for inputs above any section has no row
    model = readModel_();
    b = model.blockById[id];
    const ex = b.cats.find(x => x.id === c.id);
    if (ex) {
      if (ex.name !== c.name) sh.getRange(ex.row, 1).setValue(c.name);
      if (ex.archived) { setArchived_(sh, ex.row, false); bumpStructure_(); }
      return;
    }
    const r = insertRowFrom_(sh, b.lastUsedRow, model.templates.section);
    clearRow_(sh, model, r);
    sh.getRange(r, 1).setValue(c.name);
    sh.getRange(r + ':' + r).addDeveloperMetadata(MD.cat, c.id);
  });

  const keep = {};
  tasks.forEach(t => {
    keep[t.id] = true;
    model = readModel_();
    b = model.blockById[id];
    let ref = model.taskById[t.id], row;
    if (ref && ref.block.id === id) {
      row = ref.task.row;
      if (ref.task.catId !== t.catId) {
        const dest = catEnd_(b, t.catId);
        if (dest && dest !== row) {
          if (dest >= sh.getMaxRows()) sh.insertRowAfter(dest);
          sh.moveRows(sh.getRange(row + ':' + row), dest + 1);
          model = readModel_();
          ref = model.taskById[t.id];
          row = ref.task.row;
        }
      }
      if (ref.task.archived) {
        sh.getRange(row, 1, 1, 3).setFontLine('none');
        sh.getRange(row + ':' + row).getDeveloperMetadata().forEach(m => { if (m.getKey() === MD.archived) m.remove(); });
        bumpStructure_(); // the visible text didn't change, so the cached structure must be dropped
      }
    } else {
      row = insertRowFrom_(sh, catEnd_(b, t.catId) || b.lastUsedRow, model.templates.task);
      clearRow_(sh, model, row);
      sh.getRange(row + ':' + row).addDeveloperMetadata(MD.task, t.id);
      BOARD_ROWS[t.id] = true;
    }
    const abc = [String(t.name || ''), String(t.goal || ''), String(t.unit || '')];
    const cur = sh.getRange(row, 1, 1, 3).getDisplayValues()[0];
    if (abc.some((v, i) => v !== cur[i])) sh.getRange(row, 1, 1, 3).setValues([abc]);
  });

  // Inputs removed on the board keep their row (past weeks stay intact) but are struck through.
  // From this week on the board no longer has them, so their day cells are cleared there too.
  model = readModel_();
  b = model.blockById[id];
  let archived = false;
  const cw0 = currentWeek_(), cleared = [];
  b.tasks.forEach(t => {
    if (keep[t.id] || t.archived) return;
    sh.getRange(t.row, 1, 1, 3).setFontLine('line-through');
    sh.getRange(t.row + ':' + t.row).addDeveloperMetadata(MD.archived, '1');
    model.weeks.filter(w => w.week >= cw0).forEach(w => {
      cleared.push(sh.getRange(t.row, w.col + 1, 1, 7).getA1Notation());
      for (let d = 0; d < 7; d++) setStamp_(kc_(id, w.week, t.id, d), Number(ch.ts));
    });
    archived = true;
  });
  if (cleared.length) sh.getRangeList(cleared).clearContent();
  const boardCats = {};
  cats.forEach(c => { boardCats[c.id] = true; });
  b.cats.forEach(c => {
    if (!c.row || c.archived || boardCats[c.id]) return;
    setArchived_(sh, c.row, true);
    archived = true;
  });
  if (archived) { bumpStructure_(); model = readModel_(); }

  // The setup's KPIs apply from the week the board's editor was on (never before this week).
  const cw = currentWeek_();
  const from = /^\d{4}-\d{2}-\d{2}$/.test(ch.fromWeek || '') && ch.fromWeek > cw ? ch.fromWeek : cw;
  // When the board says which KPIs were changed, only those are written (others may be stale).
  const only = Array.isArray(ch.changedTargets) ? ch.changedTargets : null;
  tasks.forEach(t => {
    const ref = model.taskById[t.id];
    if (!ref || (only && only.indexOf(t.id) < 0)) return;
    model.weeks.forEach(w => {
      if (w.week < from) return;
      const key = kt_(id, w.week, t.id);
      if (!(Number(ch.ts) > stamp_(key))) return;
      if (num_(model.cell(ref.task.row, w.col)) !== num_(t.target)) sh.getRange(ref.task.row, w.col).setValue(num_(t.target));
      setStamp_(key, Number(ch.ts));
    });
  });
  // Weeks the sheet doesn't have yet get these KPIs when they're created (see ensureWeeksThrough_).
  const kpis = {};
  tasks.forEach(t => { if (!only || only.indexOf(t.id) >= 0) kpis[t.id] = num_(t.target); });
  PropertiesService.getScriptProperties().setProperty('kpi|' + id, JSON.stringify({ from, ts: Number(ch.ts), kpis }));
  ensureFormulas_(readModel_(), id);
}

/** The Unit column decides how an input is logged: "check" (or done, yes/no, ✓) is a daily tick box. */
function typeForUnit_(unit) {
  return /^(check|checkbox|tick|done|yes\s*\/\s*no|y\s*\/\s*n|✓|✔|☑)$/i.test(String(unit).trim()) ? 'check' : 'count';
}

/** Gives a hand-added input row the formatting of the nearest input row (ensureFormulas_ adds the formulas). */
function completeRow_(model, b, t) {
  const sh = model.sh, cols = sh.getMaxColumns();
  const near = b.tasks.filter(o => o !== t && !o.archived)
    .sort((x, y) => Math.abs(x.row - t.row) - Math.abs(y.row - t.row) || x.row - y.row)[0];
  if (!near) return;
  sh.getRange(near.row, 1, 1, cols).copyTo(sh.getRange(t.row, 1, 1, cols), SpreadsheetApp.CopyPasteType.PASTE_FORMAT, false);
}

// One formula per input row and week (Target | Mon..Sun | Total | Win / Loss), relative to its
// own row, so rows, people and weeks copied by the script or by hand compute their own numbers.
// A ticked box counts as 1. No KPI that week: both stay blank.
const TOTAL_R1C1 = '=IF(RC[-8]="","",SUM(RC[-7]:RC[-1])+COUNTIF(RC[-7]:RC[-1],TRUE))';
const WINLOSS_R1C1 = '=IF(RC[-9]="","",IF(RC[-1]>=RC[-9],"Win",TEXT(IFERROR(RC[-1]/RC[-9],0),"0%")))';

/**
 * Puts those formulas on every input row of person `onlyId` (or of everyone) and returns how many
 * cells it wrote. A header-row array formula ({"Week total"; ARRAYFORMULA(...)}) is replaced by
 * its label: it covers fixed rows and columns, so a copied person gets #REF! (the rows it would
 * spill into are taken) and a copied week keeps adding up the week before.
 */
function ensureFormulas_(model, onlyId) {
  const sh = model.sh, weeks = model.weeks;
  if (!weeks.length) return 0;
  const cols = Math.min(sh.getMaxColumns(), weeks[weeks.length - 1].col + 9);
  const f = sh.getRange(1, 1, model.lastRow, cols).getFormulasR1C1();
  const fx = (r, c) => String((f[r - 1] || [])[c - 1] || '');
  const norm = s => s.replace(/\s+/g, '').toUpperCase();
  const jobs = [
    { k: 8, label: 'Week total', formula: TOTAL_R1C1, labels: [], cells: [] },
    { k: 9, label: 'Win / Loss', formula: WINLOSS_R1C1, labels: [], cells: [] }
  ];
  model.blocks.forEach(b => {
    if (onlyId && b.id !== onlyId) return;
    weeks.forEach(w => jobs.forEach(j => {
      const c = w.col + j.k;
      if (c > cols) return;
      if (fx(b.headerRow, c)) j.labels.push(sh.getRange(b.headerRow, c).getA1Notation());
      b.tasks.forEach(t => {
        if (norm(fx(t.row, c)) !== norm(j.formula)) j.cells.push(sh.getRange(t.row, c).getA1Notation());
      });
    }));
  });
  let n = 0;
  jobs.forEach(j => { // labels first, so no array formula is left over the rows being written
    if (j.labels.length) sh.getRangeList(j.labels).setValue(j.label);
    n += j.labels.length;
  });
  jobs.forEach(j => {
    if (j.cells.length) sh.getRangeList(j.cells).setFormulaR1C1(j.formula);
    n += j.cells.length;
  });
  return n;
}

/**
 * Fingerprints each person's setup (name, sections, inputs; not KPIs) and compares it with the last
 * one seen, so changes no trigger reported (rows dragged, sorted, deleted) still reach the board.
 * A changed person is stamped as edited now and returned. `boardWrote` is a person whose rows the
 * board just changed: their new fingerprint is recorded without counting as a sheet edit.
 */
function noteSetups_(model, boardWrote) {
  const sp = PropertiesService.getScriptProperties(), all = sp.getProperties(), save = {}, changed = [], now = Date.now();
  model.blocks.forEach(b => {
    const s = setupOf_(model, b);
    const shape = [s.name, s.cats, s.tasks.map(t => [t.id, t.catId, t.name, t.goal, t.unit])];
    const h = Utilities.base64Encode(Utilities.computeDigest(Utilities.DigestAlgorithm.MD5, JSON.stringify(shape)));
    const prev = all['h|' + b.id];
    if (prev === h) return;
    save['h|' + b.id] = h;
    if (prev && b.id !== boardWrote) {
      setStamp_(ks_(b.id), now);
      s.ts = now;
      changed.push(s);
    }
  });
  if (Object.keys(save).length) sp.setProperties(save);
  return changed;
}

/** Strikes a row through and marks it removed, or undoes that. */
function setArchived_(sh, row, on) {
  sh.getRange(row, 1, 1, 3).setFontLine(on ? 'line-through' : 'none');
  const r = sh.getRange(row + ':' + row);
  r.getDeveloperMetadata().forEach(m => { if (m.getKey() === MD.archived) m.remove(); });
  if (on) r.addDeveloperMetadata(MD.archived, '1');
}

/** Last row of a section (its last input, or the section row itself). */
function catEnd_(b, catId) {
  const cat = b.cats.find(c => c.id === catId);
  if (!cat) return null;
  return b.tasks.filter(t => t.catId === catId).reduce((m, t) => Math.max(m, t.row), cat.row || b.headerRow);
}

/** New person at the bottom, copying an existing block's name/month/header rows. */
function createBlock_(id, name) {
  const model = readModel_(), sh = model.sh;
  const tpl = model.blocks.find(b => b.headerRow - b.titleRow === 2) || model.blocks[0];
  if (!tpl) throw new Error('no existing person block to copy the layout from');
  const n = tpl.headerRow - tpl.titleRow + 1, cols = sh.getMaxColumns(), last = sh.getLastRow(), at = last + 1;
  sh.insertRowsAfter(last, n);
  sh.getRange(tpl.titleRow, 1, n, cols).copyTo(sh.getRange(at, 1, n, cols));
  for (let r = at; r < at + n; r++) {
    sh.getRange(r + ':' + r).getDeveloperMetadata().forEach(m => { if (m.getKey().indexOf('itb_') === 0) m.remove(); });
  }
  const my = monthYear_(currentWeek_());
  sh.getRange(at, 2, 1, cols - 1).clearContent();
  sh.getRange(at, 1).setValue(n >= 3 ? name : name + ' ' + my);
  if (n >= 3) sh.getRange(at + 1, 1).setValue(my);
  sh.getRange(at + ':' + at).addDeveloperMetadata(MD.member, id);
}

/** Person removed on the board. Returns a reason when it can't be done. */
function deleteBlock_(id) {
  const model = readModel_(), sh = model.sh, b = model.blockById[id];
  if (!b) return null; // already gone
  if (b.startRow > 1) {
    sh.deleteRows(b.startRow, b.endRow - b.startRow + 1);
    return null;
  }
  // The top block's rows may hold sheet-wide things (frozen rows, title formatting):
  // keep its name, month and header rows and hand them to the next person.
  const next = model.blocks[model.blocks.indexOf(b) + 1];
  if (!next) return 'refused to delete the only person block (its header row defines the weeks)';
  const title = model.text(b.titleRow, 1), m = title.match(MONTH_YEAR_RE);
  sh.getRange(b.idRow + ':' + b.idRow).getDeveloperMetadata().forEach(x => { if (x.getKey() === MD.member) x.remove(); });
  sh.getRange(b.titleRow + ':' + b.titleRow).addDeveloperMetadata(MD.member, next.id);
  sh.getRange(b.titleRow, 1).setValue(next.name + (m ? m[0] : ''));
  sh.deleteRows(b.headerRow + 1, next.headerRow - b.headerRow);
  return null;
}

function insertRowFrom_(sh, afterRow, templateRow) {
  sh.insertRowAfter(afterRow);
  const r = afterRow + 1, cols = sh.getMaxColumns();
  if (templateRow) {
    const t = templateRow >= r ? templateRow + 1 : templateRow;
    sh.getRange(t, 1, 1, cols).copyTo(sh.getRange(r, 1, 1, cols)); // keeps the Total / Win-Loss formulas
  }
  sh.getRange(r + ':' + r).getDeveloperMetadata().forEach(m => { if (m.getKey().indexOf('itb_') === 0) m.remove(); });
  return r;
}

function clearRow_(sh, model, r) {
  const a1 = [sh.getRange(r, 1, 1, 3).getA1Notation()];
  model.weeks.forEach(w => a1.push(sh.getRange(r, w.col, 1, 8).getA1Notation()));
  sh.getRangeList(a1).clearContent();
  sh.getRange(r, 1, 1, 3).setFontLine('none');
}

// ======================= automatic weeks =======================

/** Appends week blocks until the week starting `target` exists. */
function ensureWeeksThrough_(target) {
  const cache = CacheService.getScriptCache();
  if ((cache.get('lastWeek') || '') >= target) return []; // checked recently, no need to re-read the sheet
  const sh = tab_();
  const weeks = parseWeeks_(sh.getRange(1, 1, Math.max(sh.getLastRow(), 1), Math.max(sh.getLastColumn(), 1)).getDisplayValues());
  if (!weeks.length) return [];
  let last = weeks[weeks.length - 1];
  const created = [];
  while (last.week < target) {
    const next = appendWeek_(sh, last);
    if (!next) break; // the space is already used (e.g. a week added by hand in another format)
    last = next;
    created.push(last.week);
  }
  cache.put('lastWeek', last.week, 600);
  // The KPIs copied into a new week aren't edits, so they're not stamped: KPIs the board already
  // set for that week win, and the board picks the week up on its next check. KPIs the board's
  // editor set "from" one of these weeks on are written in now.
  if (created.length) {
    const model = readModel_(), all = PropertiesService.getScriptProperties().getProperties();
    model.blocks.forEach(b => {
      const k = all['kpi|' + b.id] ? JSON.parse(all['kpi|' + b.id]) : null;
      if (!k) return;
      created.forEach(wk => {
        const w = model.weekByKey[wk];
        if (!w || wk < k.from) return;
        b.tasks.forEach(t => {
          const key = kt_(b.id, wk, t.id);
          if (!(t.id in k.kpis) || !(k.ts > stamp_(key))) return;
          if (num_(model.cell(t.row, w.col)) !== k.kpis[t.id]) model.sh.getRange(t.row, w.col).setValue(k.kpis[t.id]);
          setStamp_(key, k.ts);
        });
      });
    });
    console.log('Added week(s): ' + created.join(', '));
  }
  return created;
}

/** Copies the last week block (formats, formulas, targets) to its right and clears the days. */
function appendWeek_(sh, last) {
  const W = CONFIG.WEEK_WIDTH, dst = last.col + W, rows = sh.getMaxRows();
  if (sh.getMaxColumns() < dst + W - 1) sh.insertColumnsAfter(sh.getMaxColumns(), dst + W - 1 - sh.getMaxColumns());
  // Never write over anything: if those columns already hold something, leave them alone.
  const used = sh.getRange(1, dst, Math.max(sh.getLastRow(), 1), W).getDisplayValues().some(row => row.some(v => String(v).trim() !== ''));
  if (used) {
    console.warn('Not adding the week of ' + addDays_(last.week, 7) + ': columns ' + dst + '-' + (dst + W - 1) +
      ' already have content. If a week was added by hand, give its day headers the "Mon 10/26" format.');
    return null;
  }
  const from = sh.getRange(1, last.col, rows, W), to = sh.getRange(1, dst, rows, W);
  from.copyTo(to); // relative formulas (Total, Win / Loss) shift to the new columns
  from.copyTo(to, SpreadsheetApp.CopyPasteType.PASTE_COLUMN_WIDTHS, false);
  const week = { n: last.n + 1, col: dst, week: addDays_(last.week, 7) };
  // If the sheet has "Week N, m/d to m/d" labels over the blocks, give the new block one too.
  const above = sh.getRange(1, dst, rows, 2).getDisplayValues();
  above.forEach((pair, i) => pair.forEach((v, j) => {
    if (/^week\s*\d+/i.test(String(v).trim())) {
      sh.getRange(i + 1, dst + j).setValue('Week ' + week.n + ', ' + md_(week.week) + ' to ' + md_(addDays_(week.week, 6)));
    }
  }));
  const model = readModel_(), clear = [];
  const labels = [DAYS.map((d, i) => d + ' ' + md_(addDays_(week.week, i)))];
  model.blocks.forEach(b => {
    sh.getRange(b.headerRow, dst + 1, 1, 7).setValues(labels);
    b.tasks.forEach(t => clear.push(sh.getRange(t.row, dst + 1, 1, 7).getA1Notation()));
  });
  if (clear.length) sh.getRangeList(clear).clearContent();
  ensureFormulas_(readModel_()); // a copied header-row array formula would still add up the week before
  return week;
}

// ======================= timestamps (_sync tab) =======================

let STAMPS = null;

function stamps_() {
  if (STAMPS) return STAMPS;
  const ss = spreadsheet_();
  let sh = ss.getSheetByName(CONFIG.STAMP_TAB);
  if (!sh) { sh = ss.insertSheet(CONFIG.STAMP_TAB); sh.hideSheet(); }
  const map = {}, rowOf = {}, last = sh.getLastRow();
  if (last) sh.getRange(1, 1, last, 2).getValues().forEach((r, i) => { if (r[0]) { map[r[0]] = Number(r[1]) || 0; rowOf[r[0]] = i + 1; } });
  STAMPS = { sh, map, rowOf, last, dirty: {} };
  return STAMPS;
}

function stamp_(key) { return stamps_().map[key] || 0; }

function setStamp_(key, ts) {
  const s = stamps_();
  s.map[key] = ts;
  s.dirty[key] = ts;
}

function saveStamps_() {
  const s = STAMPS;
  STAMPS = null;
  if (!s) return;
  const keys = Object.keys(s.dirty);
  if (!keys.length) return;
  const known = keys.filter(k => s.rowOf[k]), fresh = keys.filter(k => !s.rowOf[k]);
  if (known.length > 50) { // many updates: rewrite the table in one call
    const rows = Object.keys(s.map).map(k => [k, s.map[k]]);
    ensureRows_(s.sh, rows.length);
    s.sh.clearContents();
    s.sh.getRange(1, 1, rows.length, 2).setValues(rows);
    return;
  }
  known.forEach(k => s.sh.getRange(s.rowOf[k], 2).setValue(s.dirty[k]));
  if (fresh.length) {
    ensureRows_(s.sh, s.last + fresh.length);
    s.sh.getRange(s.last + 1, 1, fresh.length, 2).setValues(fresh.map(k => [k, s.dirty[k]]));
  }
}

function ensureRows_(sh, n) {
  if (sh.getMaxRows() < n) sh.insertRowsAfter(sh.getMaxRows(), n - sh.getMaxRows());
}

function kc_(m, w, t, d) { return ['c', m, w, t, d].join('|'); }
function kt_(m, w, t) { return ['t', m, w, t].join('|'); }
function ks_(m) { return 's|' + m; }

// ======================= small helpers =======================

function withLock_(fn, asResponse, waitMs) {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(waitMs || 30000)) {
    if (asResponse) return json_({ ok: false, busy: true });
    throw new Error('the script is busy, try again');
  }
  let ok = false;
  try {
    const result = fn();
    ok = true;
    return result;
  } catch (err) {
    console.error(err && err.stack || err);
    if (asResponse) return json_({ ok: false, error: String(err && err.message || err) });
    throw err;
  } finally {
    // Only a run that finished keeps its timestamps: a failed one may have stamped writes it never made.
    try { if (ok) saveStamps_(); else STAMPS = null; } finally { NEW_SETUP = {}; BOARD_ROWS = {}; lock.releaseLock(); }
  }
}

let SS = null;
function spreadsheet_() { return SS || (SS = SpreadsheetApp.openById(CONFIG.SPREADSHEET_ID)); }

function tab_() {
  const sh = spreadsheet_().getSheets().find(s => s.getSheetId() === CONFIG.TAB_GID);
  if (!sh) throw new Error('tracker tab (gid ' + CONFIG.TAB_GID + ') not found');
  return sh;
}

function props_() {
  const sp = PropertiesService.getScriptProperties();
  return { url: String(sp.getProperty('BOARD_URL') || '').replace(/\/+$/, ''), secret: sp.getProperty('SYNC_SECRET') || '' };
}

function secretOk_(s) {
  const secret = props_().secret;
  return !!secret && s === secret;
}

function json_(o) {
  return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON);
}

function num_(v) {
  if (v === true) return 1;
  if (v === false || v === '' || v === null || v === undefined) return 0;
  let n = Number(v);
  if (!isFinite(n)) {
    const t = String(v).trim();
    if (/^(x|✓|✔|☑|✅|y|yes|done|true)$/i.test(t)) return 1;
    const lead = t.match(/^\d+(\.\d+)?/); // "3 calls" -> 3
    n = lead ? Number(lead[0]) : 0;
  }
  return n > 0 ? Math.round(n * 100) / 100 : 0;
}

function newId_(prefix) { return prefix + Utilities.getUuid().replace(/-/g, '').slice(0, 15); }
function stripMonth_(s) { return String(s || '').replace(MONTH_YEAR_RE, '').trim(); }
function pad_(n) { return (n < 10 ? '0' : '') + n; }
function ymd_(y, m, d) { return y + '-' + pad_(m) + '-' + pad_(d); }
function md_(s) { return Number(s.slice(5, 7)) + '/' + Number(s.slice(8, 10)); }

function addDays_(s, n) {
  const d = new Date(s + 'T00:00:00Z');
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

/** Monday of the current week, in the spreadsheet's time zone. */
function currentWeek_() {
  const tz = spreadsheet_().getSpreadsheetTimeZone(), now = new Date();
  const dow = Number(Utilities.formatDate(now, tz, 'u')); // 1 = Monday
  return addDays_(Utilities.formatDate(now, tz, 'yyyy-MM-dd'), 1 - dow);
}

function monthYear_(week) {
  const mid = addDays_(week, 3); // the month most of the week falls in
  return MONTHS[Number(mid.slice(5, 7)) - 1] + ' ' + mid.slice(0, 4);
}
