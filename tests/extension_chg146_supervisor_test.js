'use strict';

const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const WORKER = fs.readFileSync('extension/service_worker.js', 'utf8').replace('const SUPERVISOR_POLL_MS=2000;', 'const SUPERVISOR_POLL_MS=1;');
const SEARCH_URL = 'https://www.indeed.com/jobs?q=patient&l=United%20States';

function makeWorker({ targetPresent = true } = {}) {
  const controls = [
    { request_id: 'focus-1', action: 'focus_window' },
    { request_id: 'recheck-1', action: 'recheck' },
    { request_id: 'emergency-1', action: 'emergency_stop' },
  ];
  const calls = [];
  const focused = [];
  const navigations = [];
  const created = [];
  let targetAvailable = targetPresent;
  const targets = { tab: { id: 42, windowId: 41, status: 'complete', active: false, url: 'https://www.indeed.com/jobs?q=patient&l=United%20States' }, window: { id: 41, state: 'normal', focused: false, type: 'normal' } };
  const chrome = {
    runtime: {
      getManifest: () => ({ version_name: '3.2.2-prod-ready.672cf88.26' }),
      getURL: (path) => `chrome-extension://jobbot/${path}`,
      onMessage: { addListener: () => {} }, onStartup: { addListener: () => {} }, onInstalled: { addListener: () => {} }, reload: () => {},
    },
    storage: { local: { get: async () => ({ jobbot_bridge_config: { port: 43123, token: 'x'.repeat(24) } }), set: async () => {}, remove: async () => {} } },
    windows: {
      get: async () => targets.window,
      update: async (id, details) => { focused.push({ id, details }); targets.window.focused = details.focused === true; return targets.window; },
      create: async (details) => { created.push(details); targetAvailable = true; return { id: 41, state: 'normal', focused: false, type: 'normal', tabs: [targets.tab] }; },
      remove: async () => {},
    },
    alarms: { create: () => {}, onAlarm: { addListener: () => {} } },
    tabs: {
      get: async () => { if (!targetAvailable) throw new Error('recorded target missing'); return targets.tab; },
      update: async (id, details) => { navigations.push({ id, details }); Object.assign(targets.tab, details); return targets.tab; },
      sendMessage: async (_id, message) => {
        calls.push({ action: 'content_probe', type: message.type });
        if (message.type === 'JOBBOT_INSPECT_AUTH') return { platform: 'indeed', authenticated: true, auth_state: 'verified', page_url: SEARCH_URL, challenged: true, challenge_reason: 'challenge remains' };
        if (message.type === 'JOBBOT_INSPECT_SEARCH_EVENTUALLY') return { platform: 'indeed', page_type: 'search', page_url: SEARCH_URL, challenged: true, challenge_reason: 'challenge remains', ready: false };
        throw new Error(`unexpected probe ${message.type}`);
      },
      remove: async () => {},
    },
  };
  const rpc = async (payload) => {
    calls.push(payload);
    if (payload.action === 'run_status') return { ok: true, run: { status: 'partial' }, platforms: [{ platform: 'indeed', worker_status: 'challenged', readiness_state: 'challenged_cooldown', owned_window: 1, window_id: 41, search_tab_id: 42, search_tab_url: SEARCH_URL }] };
    if (payload.action === 'consume_control') return { ok: true, control: controls.shift() || null };
    if (payload.action === 'next_task') return { ok: true, task: { task_id: 9, platform: 'indeed', search_url: SEARCH_URL, requested_search_url: SEARCH_URL, checkpoint_json: '{}', max_results: 1, window_days: 30 } };
    return { ok: true };
  };
  const sandbox = {
    chrome, console, URL, URLSearchParams, AbortController, Date, Error, JSON,
    Map, Set, Promise, String, Number, Math, Array, Object, RegExp, TypeError,
    Uint8Array, setTimeout, clearTimeout,
    fetch: async (_url, options) => ({ ok: true, status: 200, json: async () => rpc(JSON.parse(options.body)) }),
  };
  vm.runInNewContext(`${WORKER}\nglobalThis.__runPlatformWorker=runPlatformWorker;`, sandbox, { filename: 'extension/service_worker.js' });
  return { run: sandbox.__runPlatformWorker, calls, focused, navigations, created };
}

(async () => {
  const worker = makeWorker();
  await worker.run(146, 'indeed', '3.2.2-prod-ready.672cf88.26', 'refresh-146');
  assert.strictEqual(worker.focused.length, 1, 'Focus must foreground the recorded target only');
  assert.strictEqual(worker.focused[0].id, 41);
  assert.strictEqual(worker.focused[0].details.focused, true);
  assert.strictEqual(worker.navigations.filter((update) => update.details.url).length, 0, 'Focus and supervisor recovery must not navigate the preserved tab');
  assert.strictEqual(worker.calls.filter((call) => call.action === 'content_probe').length, 2, 'one explicit recheck performs one auth/search readiness attempt');
  assert.strictEqual(worker.calls.filter((call) => call.action === 'worker_runtime' && call.worker_status === 'challenged').length >= 1, true, 'challenge remains durably visible');
  assert.strictEqual(worker.calls.filter((call) => call.action === 'ack_control').length, 3, 'focus, recheck, and emergency controls are independently acknowledged');

  const recovered = makeWorker({ targetPresent: false });
  await recovered.run(146, 'indeed', '3.2.2-prod-ready.672cf88.26', 'refresh-146');
  assert.strictEqual(recovered.created.length, 1, 'a missing recorded target gets at most one recovery window');
  console.log('CHG-146 r2 supervisor regression passed: challenged Indeed reattaches to one target, focus has no navigation, recheck is explicit and bounded, and emergency stop remains commandable');
})().catch((error) => { console.error(error); process.exitCode = 1; });
