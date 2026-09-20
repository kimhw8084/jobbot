'use strict';

const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const BUILD = '3.2.2-prod-ready.672cf88.22';
const WORKER = fs.readFileSync('extension/service_worker.js', 'utf8');
const plain = (value) => JSON.parse(JSON.stringify(value));

const tick = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));
async function waitFor(predicate, message, timeout = 3000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await tick(2);
  }
  assert.fail(message);
}

function deferred() {
  let release;
  const promise = new Promise((resolve) => { release = resolve; });
  return { promise, release };
}

function makeWorker({ storage = {}, runStatus = 'queued', holdExtensionBuild = false, failRunStatus = false } = {}) {
  const store = { jobbot_bridge_config: { port: 43123, token: 'x'.repeat(24) }, ...storage };
  const calls = [];
  const listeners = {};
  const extensionBuildGate = holdExtensionBuild ? deferred() : null;
  let messageListener = null;
  let alarmListener = null;
  let beginRuns = 0;
  let workerStarts = 0;
  let reloads = 0;

  async function rpc(payload) {
    calls.push(payload);
    if (payload.action === 'extension_build') {
      if (extensionBuildGate) await extensionBuildGate.promise;
      return { ok: true, build: BUILD, refresh_id: payload.refresh_id || '' };
    }
    if (payload.action === 'run_status') {
      if (failRunStatus) throw new Error('bridge unavailable during run_status');
      if (runStatus === 'missing') return { ok: false, error: 'run_not_found' };
      return { ok: true, run: { status: runStatus } };
    }
    if (payload.action === 'begin_run') { beginRuns += 1; return { ok: true }; }
    if (payload.action === 'runtime_config') return { ok: true, heartbeat_seconds: 1, lease_seconds: 2, watchdog_stall_seconds: 3 };
    if (payload.action === 'browser_event') {
      if (payload.event_type === 'worker_window_created') workerStarts += 1;
      return { ok: true };
    }
    if (payload.action === 'worker_runtime') {
      if (payload.worker_status === 'running') workerStarts += 1;
      return { ok: true };
    }
    if (payload.action === 'next_task') return { ok: true, done: true, stop: false };
    if (payload.action === 'consume_control') return { ok: true, control: null };
    if (payload.action === 'finish_run') return { ok: true, status: 'completed' };
    if (payload.action === 'ping') return { ok: true, version: 'test' };
    return { ok: true };
  }

  const chrome = {
    runtime: {
      getManifest: () => ({ version_name: BUILD }),
      getURL: (path) => `chrome-extension://jfdlmelgonjhgnabpbipjefgamedpgfb/${path}`,
      onMessage: { addListener: (fn) => { chrome.runtime.__messageListener = fn; } },
      onStartup: { addListener: (fn) => { listeners.startup = fn; } },
      onInstalled: { addListener: (fn) => { listeners.installed = fn; } },
      reload: () => { reloads += 1; },
    },
    storage: {
      local: {
        get: async (keys) => {
          if (typeof keys === 'string') return { [keys]: store[keys] };
          if (Array.isArray(keys)) return Object.fromEntries(keys.map((key) => [key, store[key]]));
          return { ...store };
        },
        set: async (values) => { Object.assign(store, values); },
        remove: async (keys) => { for (const key of (Array.isArray(keys) ? keys : [keys])) delete store[key]; },
      },
    },
    alarms: {
      create: () => {},
      onAlarm: { addListener: (fn) => { alarmListener = fn; } },
    },
    tabs: { get: async () => { throw new Error('no test tab'); }, remove: async () => {} },
    windows: {},
  };

  const sandbox = {
    chrome,
    console,
    URL,
    URLSearchParams,
    AbortController,
    Date,
    Error,
    JSON,
    Map,
    Set,
    Promise,
    String,
    Number,
    Math,
    Array,
    Object,
    RegExp,
    TypeError,
    Uint8Array,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    fetch: async (url, options) => {
      if (String(url).includes('/rpc')) {
        return { ok: true, status: 200, json: async () => rpc(JSON.parse(options.body)) };
      }
      return { ok: false, status: 404, json: async () => ({}) };
    },
  };
  vm.runInNewContext(`${WORKER}\nglobalThis.__ensureResume = ensureResume;\nglobalThis.__send = (message) => new Promise((resolve) => chrome.runtime.__messageListener(message, {}, resolve));\nglobalThis.__startupState = () => ({ activeRunId, startupRunId, startupPromise: !!startupPromise, workers: workerPromises.size });\nglobalThis.__startupPromise = () => startupPromise;`, sandbox, { filename: 'extension/service_worker.js' });
  return {
    sandbox,
    store,
    calls,
    beginRuns: () => beginRuns,
    workerStarts: () => workerStarts,
    reloads: () => reloads,
    releaseExtensionBuild: () => extensionBuildGate?.release(),
    send: (message) => sandbox.__send(message),
    ensureResume: () => sandbox.__ensureResume(),
    awaitStartup: () => sandbox.__startupPromise(),
    alarm: (name = 'jobbot-resume') => alarmListener({ name }),
    state: () => sandbox.__startupState(),
  };
}

async function waitIdle(worker) {
  await waitFor(() => {
    const state = worker.state();
    return state.activeRunId == null && state.startupRunId == null && state.startupPromise === false && state.workers === 0;
  }, 'startup state did not become idle');
}

(async () => {
  // (1) No saved run is completely idle, and (2) a later real start runs once.
  const idle = makeWorker();
  await tick();
  assert.deepStrictEqual(plain(idle.state()), { activeRunId: null, startupRunId: null, startupPromise: false, workers: 0 });
  const started = await idle.send({ type: 'JOBBOT_START_RUN', run_id: 201, expected_build: BUILD, refresh_id: 'start-201' });
  assert.deepStrictEqual(plain(started), { ok: true, started: true, run_id: 201 });
  await idle.awaitStartup();
  await waitIdle(idle);
  assert.strictEqual(idle.beginRuns(), 1, 'a post-idle JOBBOT_START_RUN begins exactly one run');

  // (3) A valid saved run resumes exactly once after the new worker loads.
  const resumed = makeWorker({ storage: { jobbot_active_run_id: 302, jobbot_expected_extension_build: BUILD, jobbot_refresh_id: 'resume-302' } });
  await waitFor(() => resumed.beginRuns() === 1, 'saved nonterminal run did not resume');
  await waitIdle(resumed);
  assert.strictEqual(resumed.beginRuns(), 1);

  // (4) A duplicate for the same genuinely starting run is idempotent; (5) another run is rejected with the owner.
  const starting = makeWorker({ holdExtensionBuild: true });
  const first = await starting.send({ type: 'JOBBOT_START_RUN', run_id: 403, expected_build: BUILD, refresh_id: 'start-403' });
  assert.deepStrictEqual(plain(first), { ok: true, started: true, run_id: 403 });
  assert.strictEqual(starting.state().startupRunId, 403);
  const duplicate = await starting.send({ type: 'JOBBOT_START_RUN', run_id: 403, expected_build: BUILD, refresh_id: 'start-403' });
  assert.deepStrictEqual(plain(duplicate), { ok: true, started: true, resumed: true, run_id: 403 });
  const other = await starting.send({ type: 'JOBBOT_START_RUN', run_id: 404, expected_build: BUILD, refresh_id: 'start-404' });
  assert.strictEqual(other.ok, false);
  assert.strictEqual(other.startup_run_id, 403);
  assert.strictEqual(other.active_run_id, 0);
  starting.releaseExtensionBuild();
  await waitIdle(starting);
  assert.strictEqual(starting.beginRuns(), 1, 'duplicate startup did not create another begin_run');

  // (6) Terminal and missing saved runs clear ownership without starting workers.
  for (const runStatus of ['completed', 'missing']) {
    const stale = makeWorker({ storage: { jobbot_active_run_id: 506, jobbot_expected_extension_build: BUILD, jobbot_refresh_id: `stale-${runStatus}` }, runStatus });
    await waitIdle(stale);
    assert.strictEqual(stale.beginRuns(), 0, `${runStatus} saved run must not begin`);
  }

  // (7) A run-status/bridge failure clears ownership so a later legitimate start proceeds.
  const failedStatus = makeWorker({ storage: { jobbot_active_run_id: 607 }, failRunStatus: true });
  await waitFor(() => failedStatus.state().startupPromise === false, 'failed resume kept startup ownership');
  const afterFailure = await failedStatus.send({ type: 'JOBBOT_START_RUN', run_id: 608, expected_build: BUILD, refresh_id: 'start-608' });
  assert.deepStrictEqual(plain(afterFailure), { ok: true, started: true, run_id: 608 });
  await waitIdle(failedStatus);
  assert.strictEqual(failedStatus.beginRuns(), 1);

  // (8) Repeated no-run alarms never wedge a later start.
  const alarmed = makeWorker();
  await Promise.all([alarmed.alarm(), alarmed.alarm(), alarmed.alarm()]);
  await tick();
  assert.strictEqual(alarmed.state().startupRunId, null);
  const afterAlarms = await alarmed.send({ type: 'JOBBOT_START_RUN', run_id: 809, expected_build: BUILD, refresh_id: 'start-809' });
  assert.deepStrictEqual(plain(afterAlarms), { ok: true, started: true, run_id: 809 });
  await waitIdle(alarmed);
  assert.strictEqual(alarmed.beginRuns(), 1);

  // (9) The documented predecessor reload handoff resumes the queued run once on the new build.
  const handoff = makeWorker({ storage: { jobbot_active_run_id: 910, jobbot_expected_extension_build: BUILD, jobbot_refresh_id: 'reload-910' } });
  await waitFor(() => handoff.beginRuns() === 1, 'reload handoff did not resume the intended queued run');
  await waitIdle(handoff);
  assert.strictEqual(handoff.beginRuns(), 1);
  assert.strictEqual(handoff.store.jobbot_active_run_id, undefined, 'completed handoff did not clear durable active run');

  // (10) Maintenance refresh with no run has no startup ownership.
  const maintenance = makeWorker({ storage: { jobbot_expected_extension_build: BUILD, jobbot_refresh_id: 'maintenance-1' } });
  await maintenance.ensureResume();
  await tick();
  assert.deepStrictEqual(plain(maintenance.state()), { activeRunId: null, startupRunId: null, startupPromise: false, workers: 0 });
  assert.strictEqual(maintenance.beginRuns(), 0);

  console.log('CHG-146 r5 startup lifecycle regression passed: run-scoped ownership, idle no-run resume, exact-once resume/start, conflict rejection, stale/failure cleanup, alarm safety, reload handoff, and maintenance idleness');
})().catch((error) => { console.error(error); process.exitCode = 1; });
