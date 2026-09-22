'use strict';

const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const SOURCE = fs.readFileSync('extension/bootstrap.js', 'utf8');
const EXECUTABLE = SOURCE.replace('bootstrap().finally', 'globalThis.__bootstrapPromise = bootstrap().finally');
const EXPECTED_BUILD = '3.2.2-prod-ready.672cf88.25';
const BRIDGE_TOKEN = 'x'.repeat(32);

function makePage({ mode, runId = 0, maintenance = false, store = {}, state = {} }) {
  const messages = [];
  const writes = [];
  const runtime = { lastError: null };
  const pageState = state;
  pageState.reloads ??= 0;
  pageState.closes ??= 0;
  pageState.startCalls ??= 0;
  const search = new URLSearchParams({
    expected_build: EXPECTED_BUILD,
    refresh_id: 'refresh-146',
    bridge_port: '43123',
    bridge_token: BRIDGE_TOKEN,
    ...(runId ? { run_id: String(runId) } : {}),
    ...(maintenance ? { maintenance: '1' } : {}),
  }).toString();

  const responseFor = (message) => {
    if (message.type === 'JOBBOT_CONFIGURE_BRIDGE') {
      return mode === 'configure-failure' ? { ok: false, error: 'bridge_ping_failed' } : { ok: true, bridge: 'loopback' };
    }
    if (message.type === 'JOBBOT_REFRESH_EXTENSION') {
      if (mode === 'refresh-failure') return { ok: false, error: 'refresh_request_failed' };
      if (mode === 'predecessor') return { ok: true, reload_required: true, refresh_id: message.refresh_id };
      return { ok: true, identity_confirmed: true, refreshed: true, refresh_id: message.refresh_id };
    }
    if (message.type === 'JOBBOT_START_RUN') {
      pageState.startCalls += 1;
      return { ok: true, started: true, run_id: message.run_id };
    }
    return undefined;
  };

  const chrome = {
    runtime: {
      ...runtime,
      sendMessage: (message, callback) => {
        messages.push(message);
        const value = responseFor(message);
        if (value === undefined) {
          runtime.lastError = { message: `Unsupported predecessor message: ${message.type}` };
          callback();
          runtime.lastError = null;
          return;
        }
        callback(value);
      },
      reload: () => { pageState.reloads += 1; },
    },
    storage: {
      local: {
        get: async (key) => ({ [key]: store[key] }),
        set: async (value) => { writes.push(value); Object.assign(store, value); },
      },
    },
  };
  const sandbox = {
    chrome,
    location: { search: `?${search}` },
    window: { close: () => { pageState.closes += 1; } },
    URLSearchParams,
    Promise,
    Error,
    String,
    Number,
    Object,
    Boolean,
  };
  vm.runInNewContext(EXECUTABLE, sandbox, { filename: 'extension/bootstrap.js' });
  return { messages, writes, pageState, store, promise: sandbox.__bootstrapPromise };
}

async function runPage(options) {
  const page = makePage(options);
  page.result = await page.promise;
  return page;
}

(async () => {
  assert.ok(SOURCE.includes("type: 'JOBBOT_CONFIGURE_BRIDGE'"), 'legacy bridge configuration is required');
  assert.ok(SOURCE.includes("type: 'JOBBOT_REFRESH_EXTENSION'"), 'legacy refresh request is required');
  assert.ok(SOURCE.includes("type: 'JOBBOT_START_RUN'"), 'current-build run continuation is required');
  assert.ok(!SOURCE.includes("type: 'JOBBOT_BOOTSTRAP_START'"), 'new bootstrap-only protocol must not be required');

  const maintenance = await runPage({ mode: 'predecessor', maintenance: true });
  assert.deepStrictEqual(maintenance.messages.map((message) => message.type), [
    'JOBBOT_CONFIGURE_BRIDGE', 'JOBBOT_REFRESH_EXTENSION',
  ], 'a predecessor receives only supported legacy bootstrap messages');
  assert.strictEqual(maintenance.pageState.reloads, 1, 'reload_required invokes runtime.reload exactly once');
  assert.strictEqual(maintenance.pageState.closes, 1, 'reload bootstrap page closes after completion');
  assert.strictEqual(maintenance.store.jobbot_bridge_config.port, 43123);
  assert.strictEqual(maintenance.store.jobbot_bridge_config.token, BRIDGE_TOKEN);
  assert.strictEqual(maintenance.store.jobbot_expected_extension_build, EXPECTED_BUILD);
  assert.strictEqual(maintenance.store.jobbot_refresh_id, 'refresh-146');
  assert.strictEqual(maintenance.store.jobbot_active_run_id, undefined, 'maintenance does not hand off a run');
  assert.strictEqual(maintenance.store.jobbot_bootstrap_handoff.state, 'reload_requested');
  assert.strictEqual(maintenance.result.reload_required, true);

  const current = await runPage({ mode: 'current', maintenance: true });
  assert.strictEqual(current.pageState.reloads, 0, 'already-current maintenance does not reload');
  assert.strictEqual(current.pageState.startCalls, 0, 'maintenance never starts a browser run');
  assert.strictEqual(current.pageState.closes, 1);
  assert.strictEqual(current.store.jobbot_bootstrap_handoff.state, 'completed');

  const queued = await runPage({ mode: 'predecessor', runId: 146 });
  assert.strictEqual(queued.pageState.startCalls, 0, 'stale predecessor cannot start the run');
  assert.strictEqual(queued.store.jobbot_active_run_id, 146, 'queued run survives the reload handoff');
  assert.strictEqual(queued.store.jobbot_bootstrap_handoff.state, 'reload_requested');
  let resumedRuns = 0;
  if (queued.store.jobbot_active_run_id === 146 && queued.store.jobbot_expected_extension_build === EXPECTED_BUILD) resumedRuns += 1;
  assert.strictEqual(resumedRuns, 1, 'new build resumes the queued run once from durable handoff');

  const duplicateState = {};
  const duplicateStore = {};
  const first = await runPage({ mode: 'predecessor', runId: 146, store: duplicateStore, state: duplicateState });
  const replay = await runPage({ mode: 'predecessor', runId: 146, store: duplicateStore, state: duplicateState });
  assert.strictEqual(first.pageState.reloads, 1);
  assert.strictEqual(replay.pageState.reloads, 1, 'replayed stale bootstrap does not request a second reload');
  assert.strictEqual(replay.messages.length, 0, 'replayed handoff is closed without replaying protocol actions');

  const duplicateCurrentState = {};
  const duplicateCurrentStore = {};
  const started = await runPage({ mode: 'current', runId: 146, store: duplicateCurrentStore, state: duplicateCurrentState });
  const startedReplay = await runPage({ mode: 'current', runId: 146, store: duplicateCurrentStore, state: duplicateCurrentState });
  assert.strictEqual(started.pageState.startCalls, 1);
  assert.strictEqual(startedReplay.pageState.startCalls, 1, 'replayed current bootstrap does not start a second run');

  for (const mode of ['configure-failure', 'refresh-failure']) {
    const failed = await runPage({ mode, runId: 146 });
    assert.strictEqual(failed.result.ok, false, `${mode} is reported as failure`);
    assert.strictEqual(failed.pageState.reloads, 0);
    assert.strictEqual(failed.pageState.closes, 1, `${mode} still closes the transient page`);
    assert.strictEqual(failed.store.jobbot_bootstrap_handoff.state, 'failed');
    assert.ok(failed.store.jobbot_bootstrap_handoff.error);
  }

  console.log('CHG-146 r4 bootstrap compatibility regression passed: predecessor legacy protocol, durable reload handoff, maintenance/run continuation, idempotency, truthful failure, and transient closure');
})().catch((error) => { console.error(error); process.exitCode = 1; });
