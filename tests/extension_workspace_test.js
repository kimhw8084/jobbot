'use strict';

// Deterministic Chrome API mock for the production service-worker workspace
// manager. This proves ownership decisions without launching Chrome.
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

function createChrome(seedWindows = []) {
  const windows = new Map();
  const tabs = new Map();
  let nextWindow = 100;
  let nextTab = 1000;
  const storage = {};
  const focusRequests = [];
  const removedListeners = [];
  const alarms = [];
  for (const seed of seedWindows) {
    const win = { id: seed.id, focused: !!seed.focused, state: 'normal', type: 'normal', tabs: [] };
    windows.set(win.id, win);
    for (const raw of seed.tabs || []) {
      const tab = { id: raw.id ?? nextTab++, windowId: win.id, url: raw.url || 'chrome://newtab/', status: 'complete', active: !!raw.active };
      win.tabs.push(tab); tabs.set(tab.id, tab);
    }
    nextWindow = Math.max(nextWindow, win.id + 1);
  }
  const runtime = {
    getURL: (path) => `chrome-extension://jobbot/${path}`,
    getManifest: () => ({ version: '3.2.3', version_name: '3.2.3-static-hardening.2' }),
    onMessage: { addListener() {} }, onStartup: { addListener() {} }, onInstalled: { addListener() {} },
  };
  const chrome = {
    runtime,
    storage: { local: {
      async get(keys) {
        if (!keys) return { ...storage };
        if (Array.isArray(keys)) return Object.fromEntries(keys.map((key) => [key, storage[key]]));
        if (typeof keys === 'string') return { [keys]: storage[keys] };
        return Object.fromEntries(Object.keys(keys).map((key) => [key, storage[key] ?? keys[key]]));
      },
      async set(values) { Object.assign(storage, values); },
      async remove(keys) { for (const key of (Array.isArray(keys) ? keys : [keys])) delete storage[key]; },
    }},
    tabs: {
      async get(id) { const tab = tabs.get(Number(id)); if (!tab) throw new Error('tab not found'); return { ...tab }; },
      async query() { return [...tabs.values()].map((tab) => ({ ...tab })); },
      async create(options) {
        const win = windows.get(Number(options.windowId)); if (!win) throw new Error('window not found');
        const tab = { id: nextTab++, windowId: win.id, url: options.url, status: 'complete', active: !!options.active };
        if (tab.active) for (const sibling of win.tabs) sibling.active = false;
        win.tabs.push(tab); tabs.set(tab.id, tab); return { ...tab };
      },
      async update(id, changes) {
        const tab = tabs.get(Number(id)); if (!tab) throw new Error('tab not found');
        Object.assign(tab, changes);
        if (changes.active) { const win = windows.get(tab.windowId); for (const sibling of win.tabs) if (sibling.id !== tab.id) sibling.active = false; }
        return { ...tab };
      },
      async move(id, options) {
        const tab = tabs.get(Number(id)); const from = windows.get(tab.windowId); const to = windows.get(Number(options.windowId));
        if (!tab || !to) throw new Error('move target not found');
        from.tabs = from.tabs.filter((item) => item.id !== tab.id); tab.windowId = to.id; to.tabs.push(tab); return { ...tab };
      },
      async remove(id) { const tab = tabs.get(Number(id)); if (!tab) throw new Error('tab not found'); const win = windows.get(tab.windowId); win.tabs = win.tabs.filter((item) => item.id !== tab.id); tabs.delete(tab.id); },
    },
    windows: {
      async get(id) { const win = windows.get(Number(id)); if (!win) throw new Error('window not found'); return { ...win, tabs: win.tabs.map((tab) => ({ ...tab })) }; },
      async create(options) {
        const win = { id: nextWindow++, focused: !!options.focused, state: options.state, type: options.type, tabs: [] };
        windows.set(win.id, win);
        const tab = { id: nextTab++, windowId: win.id, url: options.url, status: 'complete', active: true };
        win.tabs.push(tab); tabs.set(tab.id, tab); return { ...win, tabs: [{ ...tab }] };
      },
      update: async (id, changes) => { if (changes.focused) focusRequests.push({ id, changes }); const win = windows.get(Number(id)); Object.assign(win, changes); return { ...win, tabs: win.tabs.map((tab) => ({ ...tab })) }; },
      onRemoved: { addListener(fn) { removedListeners.push(fn); } },
    },
    alarms: { create(name, info) { alarms.push({ name, info }); }, onAlarm: { addListener() {} } },
  };
  return { chrome, windows, tabs, storage, focusRequests, removedListeners, alarms };
}

async function loadWorker(mock) {
  const context = vm.createContext({ chrome: mock.chrome, console, setTimeout, clearTimeout, URL, Date, Promise, AbortController,
    fetch: async () => ({ ok: true, json: async () => ({ ok: true }) }) });
  vm.runInContext(fs.readFileSync('extension/service_worker.js', 'utf8'), context, { filename: 'extension/service_worker.js' });
  return context.__JobBotWorkspaceTestHooks;
}

(async () => {
  // A personal window containing the rendezvous must never be adopted.
  let mock = createChrome([{ id: 1, focused: true, tabs: [
    { id: 1, url: 'https://mail.google.com/' },
    { id: 2, url: 'https://www.linkedin.com/feed/' },
    { id: 3, url: 'chrome-extension://jobbot/dashboard.html?jobbot_workspace=1&jobbot_role=anchor' },
  ] }]);
  let hooks = await loadWorker(mock);
  let state = await hooks.ensureWorkspace(3);
  assert.notStrictEqual(state.window_id, 1);
  assert.strictEqual(mock.windows.get(1).tabs.length, 2);
  assert.strictEqual(state.workspace_creation_method, 'windows.create');
  assert.strictEqual(state.controller_original_window_had_non_jobbot_tabs, true);
  const search = await hooks.createOwnedTab('search', 'https://www.linkedin.com/jobs/search/');
  assert.strictEqual(search.windowId, state.window_id);
  assert.strictEqual(search.active, false);
  const proof = await hooks.workspaceState();
  assert.strictEqual(proof.worker_tab_window_ids.search, state.window_id);
  assert.strictEqual(proof.ownership_violations, 0);

  // A freshly created window containing only the rendezvous is safe to adopt.
  mock = createChrome([{ id: 7, focused: false, tabs: [
    { id: 70, url: 'chrome-extension://jobbot/dashboard.html?jobbot_workspace=1&jobbot_role=anchor' },
  ] }]); hooks = await loadWorker(mock); state = await hooks.ensureWorkspace(70);
  assert.strictEqual(state.window_id, 7);
  assert.strictEqual(mock.windows.size, 1);
  assert.strictEqual(mock.focusRequests.length, 0);

  // Existing workspace is reused and a new controller is moved into it.
  const controller = { id: 71, url: 'chrome-extension://jobbot/dashboard.html?jobbot_workspace=1&jobbot_role=controller' };
  const existing = mock.windows.get(7); existing.tabs.push({ ...controller, windowId: 7, status: 'complete', active: false }); mock.tabs.set(71, { ...controller, windowId: 7, status: 'complete', active: false });
  const reused = await hooks.ensureWorkspace(71);
  assert.strictEqual(reused.window_id, 7);
  assert.strictEqual(mock.windows.size, 1);

  // Stale IDs reacquire a safe marker workspace; a personal marker is rejected.
  mock = createChrome([{ id: 9, focused: false, tabs: [{ id: 90, url: 'chrome-extension://jobbot/dashboard.html?jobbot_workspace=1&jobbot_role=anchor' }] }]); hooks = await loadWorker(mock);
  await hooks.saveWorkspace({ window_id: 999, anchor_tab_id: 998, workspace_generation: 4 });
  state = await hooks.ensureWorkspace(); assert.strictEqual(state.window_id, 9);
  mock = createChrome([{ id: 10, focused: true, tabs: [{ id: 100, url: 'https://example.com/' }, { id: 101, url: 'chrome-extension://jobbot/dashboard.html?jobbot_workspace=1&jobbot_role=anchor' }] }]); hooks = await loadWorker(mock);
  await hooks.saveWorkspace({ window_id: 999, anchor_tab_id: 998, workspace_generation: 4 });
  state = await hooks.ensureWorkspace(101); assert.notStrictEqual(state.window_id, 10); assert.strictEqual(mock.windows.get(10).tabs.length, 1);

  // A stale saved role id must not turn a personal tab into an owned tab.
  mock = createChrome([{ id: 11, focused: true, tabs: [
    { id: 110, url: 'https://mail.google.com/' },
    { id: 111, url: 'chrome-extension://jobbot/dashboard.html?jobbot_workspace=1&jobbot_role=anchor' },
  ] }]); hooks = await loadWorker(mock);
  await hooks.saveWorkspace({ window_id: 999, anchor_tab_id: 998, search_tab_id: 110, workspace_generation: 5 });
  state = await hooks.ensureWorkspace(111);
  assert.notStrictEqual(state.window_id, 11);
  assert.strictEqual(mock.windows.get(11).tabs.length, 1);

  // Dashboard is the only tab JobBot activates; no browser window focus call.
  const dashboard = await hooks.createOwnedTab('dashboard', 'http://127.0.0.1:8765/?jobbot_workspace=1&jobbot_role=dashboard');
  if (dashboard.jobbot_created) await hooks.activateDashboardTab(dashboard.id, state.window_id);
  assert.strictEqual(mock.focusRequests.length, 0);
  assert.strictEqual(mock.tabs.get(dashboard.id).active, true);
  assert.strictEqual(mock.tabs.get(state.anchor_tab_id).active, false);

  console.log('workspace ownership, reacquisition, no-personal-fallback, and dashboard-role tests passed');
})().catch((error) => { console.error(error); process.exitCode = 1; });
