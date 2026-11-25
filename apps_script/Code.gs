/**
 * Series Supply Contacts – Google Sheet web app.
 *
 * Tabs:
 *   Contacts  – Tier A only (named youth/student pastor with their own email). The Instantly export reads this tab.
 *   Tier B    – youth@ / students@ style inboxes with no name, saved for later.
 *   Tier C (old) – general church emails collected before Nov 10, 2026 (no longer collected).
 *
 * Paste this whole file into Extensions > Apps Script of the "Series Supply Contacts" sheet.
 * TOKEN must match the SHEET_TOKEN GitHub secret. This file lives in your Google account, not in the repo.
 */
const TOKEN = 'PASTE_THE_SAME_TOKEN_AS_YOUR_SHEET_TOKEN_SECRET';
const TAB_A = 'Contacts';
const TAB_B = 'Tier B';
const TAB_C = 'Tier C (old)';
const COLUMNS = ['added', 'status', 'tier', 'church', 'city', 'state_province', 'country', 'website',
  'first_name', 'full_name', 'role', 'email', 'email_source_url', 'youth_page_url', 'phone'];
const STATUSES = ['new', 'Instantly', 'replied', 'free series', 'paid', 'skip'];
const EMAIL_COL = COLUMNS.indexOf('email') + 1;
const STATUS_COL = COLUMNS.indexOf('status') + 1;
const TIER_COL = COLUMNS.indexOf('tier') + 1;

function tab_(name) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sh = ss.getSheetByName(name);
  if (!sh) {
    if (name === TAB_A && ss.getSheets().length === 1 && ss.getSheets()[0].getLastRow() === 0) {
      sh = ss.getSheets()[0];
      sh.setName(name);
    } else {
      sh = ss.insertSheet(name);
    }
  }
  if (sh.getLastRow() === 0 || sh.getRange(1, 1).getValue() !== 'added') {
    sh.getRange(1, 1, 1, COLUMNS.length).setValues([COLUMNS]).setFontWeight('bold');
    sh.setFrozenRows(1);
  }
  return sh;
}

function tabForTier_(tier) {
  tier = String(tier || '').trim().toUpperCase();
  if (tier === 'A') return TAB_A;
  if (tier === 'B') return TAB_B;
  return TAB_C;
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

function clean_(v) {
  return String(v || '').trim().replace(/^["']+|["']+$/g, '').trim();
}

/** email -> {sheet, row} across every tab, so nobody is added twice. */
function existingEmails_() {
  const map = {};
  [TAB_A, TAB_B, TAB_C].forEach(function (name) {
    const sh = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(name);
    if (!sh) return;
    const last = sh.getLastRow();
    if (last < 2) return;
    const vals = sh.getRange(2, EMAIL_COL, last - 1, 1).getValues();
    for (let i = 0; i < vals.length; i++) {
      const e = String(vals[i][0]).trim().toLowerCase();
      if (e) map[e] = { sheet: sh, row: i + 2 };
    }
  });
  return map;
}

function append_(sh, rows) {
  if (!rows.length) return;
  const start = sh.getLastRow() + 1;
  const need = start + rows.length - 1;
  const max = sh.getMaxRows();
  if (need > max) sh.insertRowsAfter(max, need - max + 200); // grow past 1,000 rows
  sh.getRange(start, 1, rows.length, COLUMNS.length).setValues(rows);
}

function doPost(e) {
  const lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    let body;
    try {
      body = JSON.parse(e.postData.contents);
    } catch (err) {
      return json_({ ok: false, error: 'body is not JSON' });
    }
    if (clean_(body.token) !== clean_(TOKEN)) return json_({ ok: false, error: 'bad token' });
    const existing = existingEmails_();

    if (body.action === 'mark') {
      const status = String(body.status || 'Instantly');
      if (STATUSES.indexOf(status) < 0) return json_({ ok: false, error: 'unknown status ' + status });
      let marked = 0;
      (body.emails || []).forEach(function (em) {
        const hit = existing[String(em).trim().toLowerCase()];
        if (hit) { hit.sheet.getRange(hit.row, STATUS_COL).setValue(status); marked++; }
      });
      return json_({ ok: true, marked: marked });
    }

    // default: add rows (arrays in COLUMNS order). Tier A -> Contacts, Tier B -> Tier B, anything else is dropped.
    const byTab = {};
    let skipped = 0;
    (body.rows || []).forEach(function (r) {
      if (!Array.isArray(r) || r.length !== COLUMNS.length) { skipped++; return; }
      const em = String(r[EMAIL_COL - 1]).trim().toLowerCase();
      const tier = String(r[TIER_COL - 1]).trim().toUpperCase();
      if (!em || existing[em] || (tier !== 'A' && tier !== 'B')) { skipped++; return; }
      existing[em] = true;
      const t = tabForTier_(tier);
      (byTab[t] = byTab[t] || []).push(r);
    });
    let added = 0;
    Object.keys(byTab).forEach(function (t) { append_(tab_(t), byTab[t]); added += byTab[t].length; });
    return json_({ ok: true, added: added, skipped: skipped, a: (byTab[TAB_A] || []).length, b: (byTab[TAB_B] || []).length });
  } finally {
    lock.releaseLock();
  }
}

/** GET ?token=...&status=new&tier=A&limit=200 -> rows from the matching tab(s), Tier A first, oldest first. */
function doGet(e) {
  const p = (e && e.parameter) || {};
  if (clean_(p.token) !== clean_(TOKEN)) return json_({ ok: false, error: 'bad token' });
  const wantStatus = clean_(p.status).toLowerCase();
  const wantTier = clean_(p.tier).toUpperCase() || 'A';
  const limit = parseInt(p.limit || '500', 10) || 500;
  let rows = [];
  ['A', 'B', 'C'].forEach(function (t) {
    if (wantTier.indexOf(t) < 0) return;
    const sh = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(tabForTier_(t));
    if (!sh || sh.getLastRow() < 2) return;
    const vals = sh.getRange(2, 1, sh.getLastRow() - 1, COLUMNS.length).getValues();
    vals.forEach(function (v) {
      const o = {};
      COLUMNS.forEach(function (c, i) {
        o[c] = v[i] instanceof Date ? Utilities.formatDate(v[i], 'America/Edmonton', 'yyyy-MM-dd') : String(v[i]);
      });
      if (!o.email) return;
      if (wantStatus && o.status.toLowerCase() !== wantStatus) return;
      if (o.tier.toUpperCase() !== t) return;
      rows.push(o);
    });
  });
  rows.sort(function (a, b) {
    if (a.tier !== b.tier) return a.tier < b.tier ? -1 : 1;
    return a.added < b.added ? -1 : a.added > b.added ? 1 : 0;
  });
  return json_({ ok: true, rows: rows.slice(0, limit) });
}

/**
 * Run ONCE from the editor (Run > splitTiers): moves existing Tier B rows from Contacts to the "Tier B" tab
 * and Tier C rows to "Tier C (old)", so Contacts holds Tier A only. Safe to run again (it only moves non-A rows).
 */
function splitTiers() {
  const main = tab_(TAB_A);
  const last = main.getLastRow();
  if (last < 2) return 'nothing to move';
  const vals = main.getRange(2, 1, last - 1, COLUMNS.length).getValues();
  const keep = [], toB = [], toC = [];
  vals.forEach(function (r) {
    const t = String(r[TIER_COL - 1]).trim().toUpperCase();
    if (t === 'A') keep.push(r); else if (t === 'B') toB.push(r); else if (r.join('') !== '') toC.push(r);
  });
  append_(tab_(TAB_B), toB);
  append_(tab_(TAB_C), toC);
  main.getRange(2, 1, last - 1, COLUMNS.length).clearContent();
  if (keep.length) main.getRange(2, 1, keep.length, COLUMNS.length).setValues(keep);
  const msg = 'Contacts now has ' + keep.length + ' Tier A rows. Moved ' + toB.length + ' to Tier B and ' + toC.length + ' to Tier C (old).';
  Logger.log(msg);
  return msg;
}

/** Optional: run once from the editor to add a status dropdown on every tab. */
function setupStatusDropdown() {
  const rule = SpreadsheetApp.newDataValidation().requireValueInList(STATUSES, true).setAllowInvalid(false).build();
  [TAB_A, TAB_B, TAB_C].forEach(function (name) {
    const sh = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(name);
    if (sh) sh.getRange(2, STATUS_COL, sh.getMaxRows() - 1, 1).setDataValidation(rule);
  });
}
