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
 *   row 1        week labels over each Monday column: "Week 7, 10/12 to 10/18"
 *   week block   10 columns: Target | Mon..Sun | Total | Win / Loss
 *   person block a name row ("ORLANDO September 2026", or "WILL" followed by a
 *                "September 2026" row), then a header row starting with "Input",
 *                then section rows (only column A filled) and input rows.
 *   columns A-C  Input | Goal served (or Notes) | Unit
 *
 * Rows are tied to board ids with developer metadata, so moving or inserting
 * rows never breaks the link. Conflicts are last-write-wins per cell; edit
 * timestamps live in the hidden "_sync" tab.
 */

const CONFIG = {
  SPREADSHEET_ID: '1B5wWCAhUaPApbTRGekl6QuFzd5BUl0qtYJ0UOj85MC0',
  TAB_GID: 1594333729,   // the tracker tab
  STAMP_TAB: '_sync',    // hidden tab: last-edit timestamp of every synced cell
  WEEK_WIDTH: 10,        // Target, Mon..Sun, Total, Win / Loss
  FUTURE_WEEKS: 1,       // board edits may create week blocks this far ahead
  PUSH_CHUNK: 200
};
const MD = { member: 'itb_member', cat: 'itb_cat', task: 'itb_task', archived: 'itb_archived' };
const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const MONTH_YEAR_RE = new RegExp('\\s*\\b(' + MONTHS.join('|') + ')\\s+(\\d{4})\\s*$', 'i');

// ======================= entry points =======================

/** Board -> sheet: snapshot of every synced cell with its last-edit time. */
function doGet(e) {
  const p = (e && e.parameter) || {};
  if (!secretOk_(p.secret)) return json_({ ok: false, error: 'unauthorized' });
  return withLock_(() => {
    ensureWeeksThrough_(currentWeek_());
    return json_(Object.assign({ ok: true }, snapshot_(readModel_(), p.since)));
  }, true);
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
  withLock_(() => {
    const model = readModel_(), now = Date.now(), rg = e.range;
    const r1 = Math.min(rg.getLastRow(), model.lastRow), c1 = Math.min(rg.getLastColumn(), Math.max(model.lastCol, 3));
    const setup = Object.assign({}, NEW_SETUP), changes = [];
    for (let r = rg.getRow(); r <= r1; r++) {
      const b = blockOf_(model, r);
      if (!b) continue;
      const t = model.taskAt[r];
      for (let c = rg.getColumn(); c <= c1; c++) {
        if (c <= 3) { setup[b.id] = true; continue; }
        const pos = t && colInfo_(model, c);
        if (!pos) continue;
        if (pos.day < 0) {
          setStamp_(kt_(b.id, pos.week.week, t.id), now);
          changes.push(targetChange_(model, b, t, pos.week, now));
        } else {
          setStamp_(kc_(b.id, pos.week.week, t.id, pos.day), now);
          changes.push(cellChange_(model, b, t, pos.week, pos.day, now));
        }
      }
    }
    const setups = Object.keys(setup).filter(id => model.blockById[id]).map(id => {
      setStamp_(ks_(id), now);
      return setupOf_(model, model.blockById[id]);
    });
    pushToBoard_(setups.concat(changes));
  });
}

/** Installable onChange trigger: rows inserted or deleted change a person's inputs. */
function handleChange(e) {
  if (!e || ['INSERT_ROW', 'REMOVE_ROW'].indexOf(e.changeType) < 0) return;
  withLock_(() => {
    const model = readModel_(), now = Date.now();
    pushToBoard_(model.blocks.map(b => { setStamp_(ks_(b.id), now); return setupOf_(model, b); }));
  });
}

/** Every minute: make sure this week exists, and re-send edits the board missed. */
function syncTick() {
  withLock_(() => {
    ensureWeeksThrough_(currentWeek_());
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
    const m = readModel_();
    console.log('weeks: ' + m.weeks.map(w => 'W' + w.n + ' ' + w.week + ' @col ' + w.col).join(', '));
    m.blocks.forEach(b => console.log(b.name + ' rows ' + b.startRow + '-' + b.endRow + ': ' +
      b.cats.length + ' sections, ' + b.tasks.length + ' inputs'));
  });
}

// ======================= sheet model =======================

let NEW_SETUP = {}; // member ids whose rows got new ids in this execution

/**
 * Parses the tracker tab into person blocks, sections and inputs, giving every
 * person/section/input row a stable id (row developer metadata) as it goes.
 */
function readModel_() {
  const sh = tab_();
  const lastRow = Math.max(sh.getLastRow(), 1), lastCol = Math.max(sh.getLastColumn(), 3);
  const vals = sh.getRange(1, 1, lastRow, lastCol).getValues();
  const cell = (r, c) => (r >= 1 && r <= lastRow && c >= 1 && c <= lastCol) ? vals[r - 1][c - 1] : '';
  const text = (r, c) => String(cell(r, c)).trim();
  const weeks = parseWeeks_(vals[0], text(1, 1));
  // A stray 0 doesn't make a section heading an input ("Content" has one), a logged value does ("Finance").
  const hasWeekData = r => weeks.some(w => {
    for (let c = w.col; c < w.col + 8; c++) if (num_(cell(r, c)) > 0) return true;
    return false;
  });
  const md = rowMetadata_(sh);
  const model = { sh, lastRow, lastCol, weeks, weekByKey: {}, blocks: [], blockById: {}, taskById: {}, taskAt: {}, templates: {}, cell, text };
  weeks.forEach(w => { model.weekByKey[w.week] = w; });

  const headers = [];
  for (let r = 1; r <= lastRow; r++) if (/^input$/i.test(text(r, 1))) headers.push(r);
  headers.forEach((h, i) => {
    let titleRow = h - 1, name = stripMonth_(text(h - 1, 1));
    if (!name && h > 2) { titleRow = h - 2; name = stripMonth_(text(h - 2, 1)); }
    if (titleRow < 1) titleRow = h;
    model.blocks.push({ name: name || 'Person ' + (i + 1), titleRow, headerRow: h, startRow: Math.min(titleRow, h), cats: [], tasks: [] });
  });
  model.blocks.forEach((b, i) => { b.endRow = i + 1 < model.blocks.length ? model.blocks[i + 1].startRow - 1 : lastRow; });

  const seen = {};
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
    b.id = claim('member', b.titleRow, 'm');
    let cat = null;
    b.lastUsedRow = b.headerRow;
    for (let r = b.headerRow + 1; r <= b.endRow; r++) {
      const a = text(r, 1);
      if (!a) continue;
      b.lastUsedRow = r;
      const detail = text(r, 2) || text(r, 3) || hasWeekData(r);
      if (detail || (md.task[r] && !md.cat[r])) {
        drop('cat', r);
        if (!cat) { cat = { id: 'gen' + b.id, name: 'General', row: 0 }; b.cats.push(cat); }
        const t = { id: claim('task', r, 't'), catId: cat.id, name: a, goal: text(r, 2), unit: text(r, 3), row: r, archived: !!md.archived[r] };
        t.type = /habit/i.test(cat.name) && /^(count)?$/i.test(t.unit) ? 'check' : 'count';
        b.tasks.push(t);
        model.taskAt[r] = t;
        model.taskById[t.id] = { task: t, block: b };
        if (!model.templates.task && !t.archived) model.templates.task = r;
      } else {
        drop('task', r);
        drop('archived', r);
        cat = { id: claim('cat', r, 'c'), name: a, row: r };
        b.cats.push(cat);
        if (!model.templates.section) model.templates.section = r;
      }
    }
    model.blockById[b.id] = b;
    if (fresh) { NEW_SETUP[b.id] = true; setStamp_(ks_(b.id), Date.now()); }
  });
  return model;
}

function rowMetadata_(sh) {
  const out = {};
  Object.keys(MD).forEach(kind => {
    out[kind] = {};
    sh.createDeveloperMetadataFinder().withKey(MD[kind])
      .withLocationType(SpreadsheetApp.DeveloperMetadataLocationType.ROW).find()
      .forEach(m => {
        const r = m.getLocation().getRow().getRow();
        if (out[kind][r]) m.remove(); else out[kind][r] = { id: m.getValue(), md: m };
      });
  });
  return out;
}

/** Week blocks from the row-1 labels ("Week 3, 9/14 to 9/20"); col = the block's Target column. */
function parseWeeks_(row1, title) {
  const tm = String(title || '').match(MONTH_YEAR_RE);
  const titleMonth = tm ? MONTHS.map(m => m.toLowerCase()).indexOf(tm[1].toLowerCase()) + 1 : 0;
  let year = tm ? Number(tm[2]) : new Date().getFullYear(), prev = 0;
  const weeks = [];
  row1.forEach((v, i) => {
    const m = String(v).match(/week\s*(\d+)\s*,\s*(\d{1,2})\/(\d{1,2})/i);
    if (!m || i < 1) return;
    const mon = Number(m[2]), day = Number(m[3]);
    if (!prev && titleMonth && mon - titleMonth > 6) year--; // "December" week in a "January" tab
    if (prev && mon < prev) year++;
    prev = mon;
    weeks.push({ n: Number(m[1]), col: i, week: ymd_(year, mon, day) }); // label is over Monday (column i+1)
  });
  return weeks;
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
    cats: b.cats.map(c => ({ id: c.id, name: c.name })),
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

function pushToBoard_(changes) {
  if (!changes.length) return true;
  const p = props_();
  try {
    if (!p.url || !p.secret) throw new Error('BOARD_URL / SYNC_SECRET not set');
    for (let i = 0; i < changes.length; i += CONFIG.PUSH_CHUNK) {
      const res = UrlFetchApp.fetch(p.url + '/api/sync/inbound', {
        method: 'post', contentType: 'application/json', muteHttpExceptions: true,
        headers: { 'X-Sync-Secret': p.secret },
        payload: JSON.stringify({ currentWeek: currentWeek_(), changes: changes.slice(i, i + CONFIG.PUSH_CHUNK) })
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
        if (Number(ch.ts) > stamp_(ks_(ch.member))) { applySetup_(ch); setStamp_(ks_(ch.member), Number(ch.ts)); res.applied++; }
        else res.stale++;
      } else {
        const why = deleteBlock_(ch.member);
        if (why) res.skipped.push(why); else res.applied++;
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
    if (ex) { if (ex.name !== c.name) sh.getRange(ex.row, 1).setValue(c.name); return; }
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
      }
    } else {
      row = insertRowFrom_(sh, catEnd_(b, t.catId) || b.lastUsedRow, model.templates.task);
      clearRow_(sh, model, row);
      sh.getRange(row + ':' + row).addDeveloperMetadata(MD.task, t.id);
    }
    const abc = [String(t.name || ''), String(t.goal || ''), String(t.unit || '')];
    const cur = sh.getRange(row, 1, 1, 3).getValues()[0].map(v => String(v));
    if (abc.some((v, i) => v !== cur[i])) sh.getRange(row, 1, 1, 3).setValues([abc]);
  });

  // Inputs removed on the board keep their row (past weeks stay intact) but are struck through.
  model = readModel_();
  b = model.blockById[id];
  b.tasks.forEach(t => {
    if (keep[t.id] || t.archived) return;
    sh.getRange(t.row, 1, 1, 3).setFontLine('line-through');
    sh.getRange(t.row + ':' + t.row).addDeveloperMetadata(MD.archived, '1');
  });

  // The setup's targets apply from the current week on.
  const cw = currentWeek_();
  tasks.forEach(t => {
    const ref = model.taskById[t.id];
    if (!ref) return;
    model.weeks.forEach(w => {
      if (w.week < cw) return;
      const key = kt_(id, w.week, t.id);
      if (!(Number(ch.ts) > stamp_(key))) return;
      if (num_(model.cell(ref.task.row, w.col)) !== num_(t.target)) sh.getRange(ref.task.row, w.col).setValue(num_(t.target));
      setStamp_(key, Number(ch.ts));
    });
  });
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
  // The first block also holds the row-1 week labels: keep its name and header
  // rows and hand them to the next person instead of deleting them.
  const next = model.blocks[model.blocks.indexOf(b) + 1];
  if (!next) return 'refused to delete the only person block (it holds the week labels)';
  const title = model.text(b.titleRow, 1), m = title.match(MONTH_YEAR_RE);
  const row = sh.getRange(b.titleRow + ':' + b.titleRow);
  row.getDeveloperMetadata().forEach(x => { if (x.getKey() === MD.member) x.remove(); });
  row.addDeveloperMetadata(MD.member, next.id);
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
  const sh = tab_();
  const row1 = sh.getRange(1, 1, 1, Math.max(sh.getLastColumn(), 1)).getValues()[0];
  const weeks = parseWeeks_(row1, row1[0]);
  if (!weeks.length) return [];
  let last = weeks[weeks.length - 1];
  const created = [];
  while (last.week < target) {
    last = appendWeek_(sh, last);
    created.push(last.week);
  }
  if (!created.length) return created;
  // Tell the board right away (the targets carried over into the new week).
  const model = readModel_(), now = Date.now(), changes = [];
  created.forEach(key => {
    const w = model.weekByKey[key];
    if (!w) return;
    model.blocks.forEach(b => b.tasks.forEach(t => {
      if (t.archived) return;
      setStamp_(kt_(b.id, w.week, t.id), now);
      changes.push(targetChange_(model, b, t, w, now));
    }));
  });
  pushToBoard_(changes);
  console.log('Added week(s): ' + created.join(', '));
  return created;
}

/** Copies the last week block (formats, formulas, targets) to its right and clears the days. */
function appendWeek_(sh, last) {
  const W = CONFIG.WEEK_WIDTH, dst = last.col + W, rows = sh.getMaxRows();
  if (sh.getMaxColumns() < dst + W - 1) sh.insertColumnsAfter(sh.getMaxColumns(), dst + W - 1 - sh.getMaxColumns());
  const from = sh.getRange(1, last.col, rows, W), to = sh.getRange(1, dst, rows, W);
  from.copyTo(to); // relative formulas (Total, Win / Loss) shift to the new columns
  from.copyTo(to, SpreadsheetApp.CopyPasteType.PASTE_COLUMN_WIDTHS, false);
  const week = { n: last.n + 1, col: dst, week: addDays_(last.week, 7) };
  sh.getRange(1, dst + 1).setValue('Week ' + week.n + ', ' + md_(week.week) + ' to ' + md_(addDays_(week.week, 6)));
  const model = readModel_(), clear = [];
  const labels = [DAYS.map((d, i) => d + ' ' + md_(addDays_(week.week, i)))];
  model.blocks.forEach(b => {
    sh.getRange(b.headerRow, dst + 1, 1, 7).setValues(labels);
    b.tasks.forEach(t => clear.push(sh.getRange(t.row, dst + 1, 1, 7).getA1Notation()));
  });
  if (clear.length) sh.getRangeList(clear).clearContent();
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

function withLock_(fn, asResponse) {
  const lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    return fn();
  } catch (err) {
    console.error(err && err.stack || err);
    if (asResponse) return json_({ ok: false, error: String(err && err.message || err) });
    throw err;
  } finally {
    try { saveStamps_(); } finally { NEW_SETUP = {}; lock.releaseLock(); }
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
  if (v === false || v === '' || v === null) return 0;
  const n = Number(v);
  return isFinite(n) && n > 0 ? Math.round(n * 100) / 100 : 0;
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
