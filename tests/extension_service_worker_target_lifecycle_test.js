'use strict';

const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const WORKER = fs.readFileSync('extension/service_worker.js', 'utf8');
const SEARCH_URL = 'https://www.indeed.com/jobs?q=data+quality&l=United+States';

function makeWorker() {
  const calls = [];
  const createCalls = [];
  const removedWindows = [];
  const removedTabs = [];
  const tabs = new Map();
  const windows = new Map();
  let nextTabId = 100;
  let nextWindowId = 200;
  const foreground = { id: 7, focused: true, state: 'normal', tabs: [{ id: 70, active: true, url: 'chrome-extension://jobbot/dashboard.html?bridge_token=secret&expected_build=.15' }] };
  const chrome = {
    runtime: {
      getManifest: () => ({ version_name: '3.2.2-prod-ready.672cf88.14' }),
      getURL: (path) => `chrome-extension://jobbot/${path}`,
      onMessage: { addListener: () => {} }, onStartup: { addListener: () => {} }, onInstalled: { addListener: () => {} }, reload: () => {},
    },
    storage: { local: { get: async () => ({ jobbot_bridge_config: { port: 43123, token: 'x'.repeat(24) } }), set: async () => {}, remove: async () => {} } },
    windows: {
      create: async (details) => {
        createCalls.push({ kind: 'windows.create', details });
        const windowId = ++nextWindowId;
        const tab = { id: ++nextTabId, windowId, status: 'complete', url: details.url, active: false };
        tabs.set(tab.id, tab); windows.set(windowId, { id: windowId, state: details.state, focused: details.focused, type: details.type, tabs: [tab] });
        return { id: windowId, state: details.state, focused: details.focused, type: details.type, tabs: [tab] };
      },
      get: async (id) => windows.get(id) || (() => { throw new Error(`window ${id} missing`); })(),
      getLastFocused: async () => foreground,
      remove: async (id) => { removedWindows.push(id); windows.delete(id); },
    },
    alarms: { create: () => {}, onAlarm: { addListener: () => {} } },
    tabs: {
      get: async (id) => tabs.get(id) || (() => { throw new Error(`tab ${id} missing`); })(),
      create: async (details) => {
        createCalls.push({ kind: 'tabs.create', details });
        const tab = { id: ++nextTabId, windowId: 7, status: 'complete', url: details.url, active: details.active === true };
        tabs.set(tab.id, tab); return tab;
      },
      sendMessage: async (id, message) => {
        const tab = tabs.get(id);
        return { platform: 'indeed', page_type: 'search', page_url: tab.url, ready: true, authenticated: true, auth_state: 'verified', login_required: false, challenged: false, result_links: [{ source_job_id: 'J1' }] };
      },
      update: async (id, details) => Object.assign(tabs.get(id), details),
      remove: async (id) => { removedTabs.push(id); tabs.delete(id); },
    },
  };
  const fetch = async (_url, options) => {
    const payload = JSON.parse(options.body); calls.push(payload); return { ok: true, status: 200, json: async () => ({ ok: true }) };
  };
  const sandbox = {
    chrome, console, URL, URLSearchParams, AbortController, Date, Error, JSON,
    Map, Set, Promise, String, Number, Math, Array, Object, RegExp, TypeError,
    setTimeout, clearTimeout, fetch,
  };
  vm.runInNewContext(`${WORKER}\nglobalThis.__createBackgroundTarget=createBackgroundTarget;globalThis.__closeBackgroundTarget=closeBackgroundTarget;globalThis.__runTargetDiagnostics=runTargetDiagnostics;`, sandbox, { filename: 'extension/service_worker.js' });
  return { create: sandbox.__createBackgroundTarget, close: sandbox.__closeBackgroundTarget, diagnostics: sandbox.__runTargetDiagnostics, calls, createCalls, removedWindows, removedTabs };
}

async function run() {
  const worker = makeWorker();
  const minimized = await worker.create(SEARCH_URL, 'minimized_owned');
  assert.strictEqual(worker.createCalls.at(-1).details.state, 'minimized');
  assert.strictEqual(worker.createCalls.at(-1).details.focused, false);
  assert.strictEqual(minimized.tab.active, false);
  await worker.close(minimized);
  assert(worker.removedWindows.includes(minimized.window_id));

  const normal = await worker.create(SEARCH_URL, 'normal_owned');
  assert.strictEqual(worker.createCalls.at(-1).details.state, 'normal');
  assert.strictEqual(worker.createCalls.at(-1).details.focused, false);
  assert.strictEqual(normal.tab.active, false);
  await worker.close(normal);
  assert(worker.removedWindows.includes(normal.window_id));

  const inactive = await worker.create(SEARCH_URL, 'inactive_existing');
  assert.strictEqual(worker.createCalls.at(-1).kind, 'tabs.create');
  assert.strictEqual(worker.createCalls.at(-1).details.active, false);
  assert.strictEqual(inactive.window_id, 7);
  await worker.close(inactive);
  assert(worker.removedTabs.includes(inactive.tab.id));

  await worker.diagnostics(900, 1, 'indeed', SEARCH_URL);
  const matrixCall = worker.calls.find((call) => call.action === 'browser_event' && call.event_type === 'target_diagnostic_matrix');
  assert(matrixCall, 'diagnostic matrix must be durable through browser_event');
  assert.strictEqual(matrixCall.payload.foreground_preserved, true);
  assert.match(matrixCall.payload.foreground_before.active_tab_url, /bridge_token=%3Credacted%3E/);
  assert(!matrixCall.payload.foreground_before.active_tab_url.includes('secret'));
  assert.deepStrictEqual(matrixCall.payload.modes.map((mode) => mode.mode), ['minimized_owned', 'normal_owned', 'inactive_existing']);
  for (const mode of matrixCall.payload.modes) {
    assert.strictEqual(mode.expected_content_script_match.expected, true);
    assert.strictEqual(mode.probes.JOBBOT_INSPECT_AUTH.ok, true);
    assert.strictEqual(mode.probes.JOBBOT_INSPECT_SEARCH_EVENTUALLY.ok, true);
    assert.strictEqual(mode.closed, true);
  }
  console.log('Service-worker target lifecycle regressions passed: minimized/normal owned and inactive existing targets stay inactive, preserve foreground state, deliver both probes, and close cleanly');
}

run().catch((error) => { console.error(error); process.exitCode = 1; });
