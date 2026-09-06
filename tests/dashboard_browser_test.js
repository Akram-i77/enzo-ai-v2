#!/usr/bin/env node
/*
 * Button-level guard for the ENZO dashboard, run under jsdom.
 *
 * Why this file exists
 * --------------------
 * tests/test_dashboard_js.py proves the emitted JavaScript PARSES. Parsing is
 * not enough: a handler can be missing, bound to the wrong id, or throw on the
 * first click, and the page still looks alive. This harness loads the REAL
 * generated HTML into a DOM, executes the REAL scripts, points fetch() at a
 * REAL running server, and then CLICKS EVERY BUTTON, asserting:
 *
 *   1. every onclick handler named in the HTML exists as a function
 *   2. clicking a control button issues the expected HTTP call
 *   3. clicking a tab button actually switches the visible pane
 *   4. clicking a filter button actually changes the activity filter
 *   5. no uncaught JavaScript error is raised by any of it
 *   6. the polling loop renders the activity stream and the KPI cards
 *
 * Usage:  node tests/dashboard_browser_test.js <baseUrl> <htmlPath>
 * Requires jsdom to be resolvable (NODE_PATH=/path/to/node_modules).
 * Prints "RESULT: <n> passed, <m> failed" on the last line.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const BASE = String(process.argv[2] || 'http://127.0.0.1:8077').replace(/\/$/, '');
const HTML_PATH = String(process.argv[3] || '');

let PASS = 0;
let FAIL = 0;
function ok(cond, label, extra) {
  if (cond) { PASS += 1; console.log('  \x1b[32mPASS\x1b[0m  ' + label + (extra ? '   ' + extra : '')); }
  else { FAIL += 1; console.log('  \x1b[31mFAIL\x1b[0m  ' + label + (extra ? '   ' + extra : '')); }
  return Boolean(cond);
}
function section(t) { console.log('\n=== ' + t + ' ==='); }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async function main() {
  if (!HTML_PATH || !fs.existsSync(HTML_PATH)) {
    console.log('  \x1b[31mFAIL\x1b[0m  html path missing: ' + HTML_PATH);
    console.log('RESULT: 0 passed, 1 failed');
    process.exit(1);
  }
  const html = fs.readFileSync(HTML_PATH, 'utf8');
  const jsErrors = [];
  const calls = [];

  const dom = new JSDOM(html, {
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    url: BASE + '/',
    resources: undefined,
    beforeParse(window) {
      window.fetch = function (u, opts) {
        const url = String(u);
        const method = (opts && opts.method) || 'GET';
        calls.push({ url: url, method: method });
        return fetch(new URL(url, BASE), opts);
      };
      window.addEventListener('error', (e) => jsErrors.push(String(e && e.message)));
      window.addEventListener('unhandledrejection', (e) => jsErrors.push('rejection: ' + String(e && e.reason)));
    },
  });

  const win = dom.window;
  const doc = win.document;

  // Let the first polling cycle run against the live server.
  await sleep(2500);

  section('1. inventory: every button and its handler');
  const buttons = Array.from(doc.querySelectorAll('button'));
  ok(buttons.length >= 13, 'the dashboard exposes at least 13 buttons', 'found ' + buttons.length);

  const handlers = [];
  buttons.forEach((b) => {
    const oc = b.getAttribute('onclick') || '';
    const m = oc.match(/^\s*([A-Za-z_$][\w$]*)\s*\(/);
    handlers.push({ el: b, id: b.id || '', fn: m ? m[1] : '', onclick: oc });
  });
  const missingFn = handlers.filter((h) => h.fn && typeof win[h.fn] !== 'function');
  ok(missingFn.length === 0, 'every onclick names a function that actually exists',
    missingFn.length ? 'missing: ' + missingFn.map((h) => h.fn).join(', ') : '');
  const noOnclick = buttons.filter((b) => !(b.getAttribute('onclick') || '').trim());
  ok(noOnclick.length === 0, 'no button is inert (every button has an onclick)',
    noOnclick.length ? noOnclick.map((b) => b.id || b.textContent.trim().slice(0, 20)).join(' | ') : '');

  section('2. control buttons reach the server');
  async function clickAndCollect(el, ms) {
    const before = calls.length;
    el.dispatchEvent(new win.MouseEvent('click', { bubbles: true, cancelable: true }));
    await sleep(ms || 900);
    return calls.slice(before);
  }

  const toggle = doc.getElementById('toggleBotBtn');
  ok(!!toggle, 'pause/resume button exists');
  let made = await clickAndCollect(toggle, 1200);
  ok(made.some((c) => c.url.indexOf('/api/control/toggle') >= 0 && c.method === 'POST'),
    'clicking pause/resume POSTs /api/control/toggle', JSON.stringify(made.map((c) => c.method + ' ' + c.url)));

  const scanBtn = buttons.find((b) => (b.getAttribute('onclick') || '').indexOf('triggerManualScan') === 0);
  ok(!!scanBtn, 'manual scan button exists');
  made = await clickAndCollect(scanBtn, 1500);
  ok(made.some((c) => c.url.indexOf('/api/scan') >= 0 && c.method === 'POST'),
    'clicking scan POSTs /api/scan', JSON.stringify(made.map((c) => c.method + ' ' + c.url)));

  const refreshBtn = buttons.find((b) => (b.getAttribute('onclick') || '').indexOf('refreshData') === 0);
  ok(!!refreshBtn, 'refresh button exists');
  made = await clickAndCollect(refreshBtn, 1500);
  ok(made.some((c) => c.url.indexOf('/api/state') >= 0),
    'clicking refresh re-pulls /api/state', JSON.stringify(made.map((c) => c.method + ' ' + c.url)));

  section('3. tab buttons actually switch panes');
  const tabIds = ['tabActivity', 'tabOverview', 'tabPositions', 'tabTrades', 'tabIntelligence', 'tabDiagnostics'];
  let tabsOk = 0;
  const tabProblems = [];
  for (const id of tabIds) {
    const btn = buttons.find((b) => (b.getAttribute('onclick') || '').indexOf("switchTab('" + id + "')") >= 0);
    const pane = doc.getElementById(id);
    if (!btn || !pane) { tabProblems.push(id + ':missing'); continue; }
    btn.dispatchEvent(new win.MouseEvent('click', { bubbles: true, cancelable: true }));
    await sleep(120);
    const visible = pane.classList.contains('active');
    const othersHidden = tabIds.filter((o) => o !== id)
      .every((o) => { const p = doc.getElementById(o); return !p || !p.classList.contains('active'); });
    if (visible && othersHidden) tabsOk += 1;
    else tabProblems.push(id + (visible ? '' : ':not-active') + (othersHidden ? '' : ':others-still-active'));
  }
  ok(tabsOk === tabIds.length, 'all ' + tabIds.length + ' tab buttons show exactly their own pane',
    tabProblems.length ? tabProblems.join(', ') : '');

  section('4. activity filter buttons');
  const filters = ['ALL', 'TRADE', 'ANALYSIS', 'DISCOVERY', 'UNIVERSE'];
  let fOk = 0;
  const fProblems = [];
  const feed = doc.getElementById('activityFeedContainer');
  for (const f of filters) {
    const btn = buttons.find((b) => (b.getAttribute('onclick') || '').indexOf("filterActivity('" + f + "')") >= 0);
    if (!btn) { fProblems.push(f + ':no-button'); continue; }
    btn.dispatchEvent(new win.MouseEvent('click', { bubbles: true, cancelable: true }));
    await sleep(150);
    // Measured by EFFECT, not by guessing an internal variable name: the filter
    // state must be set AND the feed must re-render for that category (either
    // matching items or the explicit "no events" notice).
    const state = win.currentActivityFilter;
    const txt = feed ? (feed.textContent || '') : '';
    const rendered = txt.indexOf('No events matching ' + f + ' filter') >= 0 ||
      (feed && feed.querySelectorAll('.activity-item').length > 0);
    if (state === f && rendered) fOk += 1;
    else fProblems.push(f + ':state=' + String(state) + (rendered ? '' : ':feed-not-rerendered'));
  }
  ok(fOk === filters.length, 'each filter button sets the filter AND re-renders the feed',
    fProblems.length ? fProblems.join(', ') : '');

  section('4b. the Gate Vetoes filter really finds Layer-0 rejections');
  {
    const uniBtn = buttons.find((b) => (b.getAttribute('onclick') || '').indexOf("filterActivity('UNIVERSE')") >= 0);
    ok(!!uniBtn, 'the 🎯 Gate Vetoes button exists');
    if (uniBtn) {
      uniBtn.dispatchEvent(new win.MouseEvent('click', { bubbles: true, cancelable: true }));
      await sleep(250);
      const items = feed ? Array.from(feed.querySelectorAll('.activity-item')) : [];
      const txt = feed ? (feed.textContent || '') : '';
      ok(items.length > 0, 'it shows the seeded veto event (not an empty pane)',
        items.length + ' item(s)');
      ok(txt.indexOf('SNIPER_FLOOD_EARLY') >= 0, 'and the veto CODE itself is on screen',
        txt.indexOf('SNIPER_FLOOD_EARLY') >= 0 ? '' : txt.slice(0, 120));
      ok(txt.indexOf('phase: migrated') >= 0 || txt.indexOf('Pump V1') >= 0,
        'the Layer-0 evidence line renders (platform/phase/fees/snipers)', txt.slice(0, 140));
      ok(txt.indexOf('top wallet:') >= 0, 'and the measured holder concentration renders');
    }
    const allBtn2 = buttons.find((b) => (b.getAttribute('onclick') || '').indexOf("filterActivity('ALL')") >= 0);
    if (allBtn2) {
      allBtn2.dispatchEvent(new win.MouseEvent('click', { bubbles: true, cancelable: true }));
      await sleep(200);
    }
  }

  section('4c. the new diagnostics cards are in the DOM with their thresholds');
  {
    const uniCard = doc.getElementById('universeGateCard');
    ok(!!uniCard, 'the 🎯 Entry Universe · Layer 0 card exists');
    if (uniCard) {
      const t = uniCard.textContent || '';
      const pills = uniCard.querySelectorAll('.stage-pill').length;
      ok(pills === 5, 'it shows all five gates as pills', 'found ' + pills);
      ok(/\d\/5 ARMED/.test(t), 'the header says how many gates are armed',
        (t.match(/\d\/5 ARMED/) || [''])[0]);
      ok(t.indexOf('$5,000') >= 0 && t.indexOf('$10,000') >= 0,
        'both market-cap floors are printed from the config');
      ok(t.indexOf('2.5') >= 0 && t.indexOf('SOL') >= 0, 'the fees floor and its declared unit');
      ok(t.indexOf('first 8 wallets') >= 0, 'the early-sniper window size');
      ok(t.indexOf('10') >= 0 && t.indexOf('HOLDER_CONCENTRATION') >= 0, 'the holder cap');
      ok(t.indexOf('no trade tape') >= 0,
        'and it states the sniper proxy limitation honestly, on the page itself');
    }
    const gCard = doc.getElementById('gmgnSourceCard');
    ok(!!gCard, 'the ⚡ GMGN Data Source card exists');
    ok(!!doc.getElementById('gmgnKeyStatus'), 'it reports the API-key status');
    ok(!!doc.getElementById('gmgnCats'), 'and the discovery categories with their counts');
    ok(!!doc.getElementById('gmgnLastError'), 'and the last provider error (not swallowed)');
    const rate = (doc.getElementById('gmgnBanStatus') || {}).textContent || '';
    ok(rate.indexOf('req/s') >= 0, 'the pace shown is the configured one', rate.slice(0, 60));
  }

  section('4d. the momentum windows are DRAWN, not merely carried in JSON');
  {
    // A momentum score cannot be judged from the score alone: 28 can mean
    // "falling right now" or "no window measurable". The seeded rejection carries
    // 1m=-7.41% / 5m=-20.00% with 1h=+68% / 24h=+292% as context, so the pane has
    // to show which windows were scored and which were not - and an unmeasurable
    // window must read n/a, never a fabricated 0.00%.
    const allBtn4d = buttons.find((b) => (b.getAttribute('onclick') || '').indexOf("filterActivity('ALL')") >= 0);
    if (allBtn4d) {
      allBtn4d.dispatchEvent(new win.MouseEvent('click', { bubbles: true, cancelable: true }));
      await sleep(350);
    }
    const t4d = feed ? (feed.textContent || '') : '';
    ok(t4d.indexOf('1m -7.41%') >= 0, 'the scored 1m window is on screen with its sign',
      t4d.indexOf('1m -7.41%') >= 0 ? '' : t4d.slice(0, 160));
    ok(t4d.indexOf('5m -20.00%') >= 0, 'and the scored 5m window', '');
    ok(t4d.indexOf('scored: 1m + 5m') >= 0, 'and the pane names WHICH windows were scored', '');
    ok(t4d.indexOf('context (not scored) 1h +68.00%') >= 0,
      'with 1h labelled context - the windows the owner removed from the score', '');
    ok(t4d.indexOf('24h +292.00%') >= 0 && t4d.indexOf('buy pressure 69.0%') >= 0,
      'and the 24h context plus the buy pressure that supplies 40% of the axis', '');
  }

  section('5. rendering from live data');
  // The filter test above left the feed on its last category; put it back on ALL
  // before judging what renders, otherwise we would be measuring our own click.
  const allBtn = buttons.find((b) => (b.getAttribute('onclick') || '').indexOf("filterActivity('ALL')") >= 0);
  if (allBtn) {
    allBtn.dispatchEvent(new win.MouseEvent('click', { bubbles: true, cancelable: true }));
    await sleep(400);
  }
  ok(jsErrors.length === 0, 'no uncaught JavaScript error while loading, polling and clicking',
    jsErrors.slice(0, 3).join(' | '));
  ok(win.stateCache !== null && typeof win.stateCache === 'object',
    'the polling loop stored server state (stateCache populated)');
  const kpi = doc.getElementById('equityValue') || doc.querySelector('.kpi-value');
  ok(!!kpi && (kpi.textContent || '').trim().length > 0, 'a KPI card shows a value, not a placeholder',
    kpi ? JSON.stringify((kpi.textContent || '').trim().slice(0, 24)) : '');
  const actPane = doc.getElementById('tabActivity');
  const items = actPane ? actPane.querySelectorAll('.activity-item').length : 0;
  const emptyMsg = actPane
    ? /no activity|no events matching|لا يوجد|nothing yet/i.test(actPane.textContent || '')
    : false;
  ok(items > 0 || emptyMsg, 'the activity stream renders items or an explicit empty state',
    'items=' + items + (emptyMsg ? ' (empty-state notice shown)' : ''));

  section('6. rug-protection UI is wired');
  const rugCard = doc.getElementById('rugProtectionCard');
  ok(!!rugCard, 'the rug-protection card exists in Diagnostics');
  const rugStatus = doc.getElementById('rugProtectionStatus');
  ok(!!rugStatus && /ARMED|OFF/.test(rugStatus.textContent || ''),
    'the rug-protection card reports layer status', rugStatus ? (rugStatus.textContent || '').trim() : '');
  // rugBadge is a helper nested inside the render function, so it is not on
  // window - it is verified by its EFFECT on the live table instead, which is
  // the stronger claim: the seeded flagged position must actually be drawn with
  // the badge and an explanatory tooltip.
  const posBody = doc.getElementById('positionsTableBody');
  const posHtml = posBody ? (posBody.innerHTML || '') : '';
  ok(posHtml.indexOf('\u{1F6A9}') >= 0,
    'a flagged open position is drawn with the rug badge', posHtml.length ? '' : 'table empty');
  ok(posHtml.indexOf('title=') >= 0 && posHtml.indexOf('\u062F\u062E\u0648\u0644 \u0645\u0634\u0628\u0648\u0628') >= 0,
    'the badge carries a tooltip naming the flags and the early stop');
  const tradesBody = doc.getElementById('tradesTableBody');
  const tradesHtml = tradesBody ? (tradesBody.innerHTML || '') : '';
  ok(tradesHtml.indexOf('stage-pill rug') >= 0,
    'a RUG_TRIPWIRE exit is drawn with its own colour pill');
  ok((tradesBody ? (tradesBody.textContent || '') : '').indexOf('RUG_TRIPWIRE') >= 0,
    'the rug exit reason text reaches the trades table verbatim');
  ok(html.indexOf('.stage-pill.rug') >= 0, 'the pill colour is defined in CSS');

  console.log('\nRESULT: ' + PASS + ' passed, ' + FAIL + ' failed');
  try { dom.window.close(); } catch (e) { /* ignore */ }
  process.exit(FAIL === 0 ? 0 : 1);
})().catch((e) => {
  console.log('  \x1b[31mFAIL\x1b[0m  harness crashed: ' + (e && e.stack ? e.stack.split('\n').slice(0, 3).join(' | ') : e));
  console.log('RESULT: ' + PASS + ' passed, ' + (FAIL + 1) + ' failed');
  process.exit(1);
});
