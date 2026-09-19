'use strict';

const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const WORKER = fs.readFileSync('extension/service_worker.js', 'utf8');
const AUTH_URL = 'https://www.linkedin.com/jobs/';
const SEARCH_URL = 'https://www.linkedin.com/jobs/search/?keywords=patient&location=United%20States';
const INDEED_AUTH_URL = 'https://www.indeed.com/';
const INDEED_SEARCH_URL = 'https://www.indeed.com/jobs?q=data+quality&l=United+States';
const GLASSDOOR_AUTH_URL = 'https://www.glassdoor.com/Job/index.htm';
const GLASSDOOR_SEARCH_URL = 'https://www.glassdoor.com/Job/jobs.htm?keyword=data%20quality&locT=C&locId=1';

function makeWorker({ authUrl = AUTH_URL, searchUrl = SEARCH_URL, authPage, searchPage, landingError, searchError }) {
  const calls = [];
  const messages = [];
  const tabs = new Map();
  let nextTabId = 10;
  let nextWindowId = 20;
  const chrome = {
    runtime: {
      getManifest: () => ({ version_name: '3.2.2-prod-ready.672cf88.15' }),
      getURL: (path) => `chrome-extension://jobbot/${path}`,
      onMessage: { addListener: () => {} },
      onStartup: { addListener: () => {} },
      onInstalled: { addListener: () => {} },
      reload: () => {},
    },
    storage: { local: {
      get: async () => ({ jobbot_bridge_config: { port: 43123, token: 'x'.repeat(24) } }),
      set: async () => {},
      remove: async () => {},
    } },
    windows: {
      create: async ({ url }) => {
        const id = ++nextTabId;
        const windowId = ++nextWindowId;
        const tab = { id, windowId, status: 'complete', url };
        tabs.set(id, tab);
        return { id: windowId, tabs: [tab] };
      },
      remove: async () => {},
    },
    alarms: { create: () => {}, onAlarm: { addListener: () => {} } },
    tabs: {
      get: async (id) => tabs.get(id),
      sendMessage: async (id, message) => {
        const tab = tabs.get(id);
        messages.push({ url: tab?.url, type: message.type });
        if (message.type === 'JOBBOT_INSPECT_AUTH') {
          if (tab?.url === authUrl && landingError) throw landingError;
          return authPage;
        }
        if (message.type === 'JOBBOT_INSPECT_SEARCH_EVENTUALLY') {
          if (searchError) throw searchError;
          return searchPage;
        }
        throw new Error(`unexpected content-script message for ${tab?.url}: ${message.type}`);
      },
      remove: async (id) => { tabs.delete(id); },
      update: async (id, details) => Object.assign(tabs.get(id), details),
      create: async ({ url }) => {
        const id = ++nextTabId;
        const tab = { id, windowId: ++nextWindowId, status: 'complete', url };
        tabs.set(id, tab);
        return tab;
      },
    },
  };
  const fetch = async (_url, options) => {
    const payload = JSON.parse(options.body);
    calls.push({ action: payload.action, payload });
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  };
  const sandbox = {
    chrome, console, URL, URLSearchParams, AbortController, Date, Error, JSON,
    Map, Set, Promise, String, Number, Math, Array, Object, RegExp, TypeError,
    setTimeout, clearTimeout, fetch,
  };
  vm.runInNewContext(`${WORKER}\nglobalThis.__checkAuth = checkAuth;`, sandbox, { filename: 'extension/service_worker.js' });
  return { checkAuth: sandbox.__checkAuth, calls, messages };
}

function callsFor(worker, action) {
  return worker.calls.filter((call) => call.action === action);
}

async function run() {
  const signInPage = {
    platform: 'linkedin',
    auth_state: 'sign_in_required',
    authenticated: false,
    reason: 'explicit sign-in control',
    page_url: AUTH_URL,
  };
  const signInWorker = makeWorker({ authPage: signInPage, searchPage: {} });
  const signInResult = await signInWorker.checkAuth('linkedin', 132, 7, SEARCH_URL);
  assert.deepStrictEqual(JSON.parse(JSON.stringify(signInResult)), {
    authenticated: false,
    ready: false,
    auth_state: 'sign_in_required',
    page: signInPage,
  });
  assert.deepStrictEqual(callsFor(signInWorker, 'platform_auth_result'), [{
    action: 'platform_auth_result',
    payload: {
      request_id: signInWorker.calls[0].payload.request_id,
      action: 'platform_auth_result',
      run_id: 132,
      task_id: 7,
      platform: 'linkedin',
      authenticated: false,
      auth_state: 'sign_in_required',
      reason: 'explicit sign-in control',
      page_url: AUTH_URL,
      requested_url: SEARCH_URL,
      observed_url: AUTH_URL,
    },
  }]);
  assert.strictEqual(callsFor(signInWorker, 'platform_auth_result').length, 1);
  assert.strictEqual(callsFor(signInWorker, 'platform_readiness').length, 0, 'the fixed branch must not fall through to retryable readiness');
  assert.strictEqual(signInWorker.messages.filter((message) => message.type === 'JOBBOT_INSPECT_SEARCH_EVENTUALLY').length, 0);

  const challengeWorker = makeWorker({
    authPage: { platform: 'linkedin', auth_state: 'challenged_cooldown', challenged: true, challenge_reason: 'challenge' },
    searchPage: {},
  });
  const challengeResult = await challengeWorker.checkAuth('linkedin', 132, 7, SEARCH_URL);
  assert.strictEqual(challengeResult.auth_state, 'challenged_cooldown');
  assert.strictEqual(challengeResult.ready, false);
  assert.strictEqual(callsFor(challengeWorker, 'pause_platform').length, 1);
  assert.strictEqual(callsFor(challengeWorker, 'platform_auth_result').length, 0);
  assert.strictEqual(challengeWorker.messages.filter((message) => message.type === 'JOBBOT_INSPECT_SEARCH_EVENTUALLY').length, 0);

  const usableUnknownWorker = makeWorker({
    authPage: { platform: 'linkedin', auth_state: 'unknown', authenticated: false, page_url: AUTH_URL },
    searchPage: { platform: 'linkedin', page_type: 'search', page_url: SEARCH_URL, ready: true, login_required: false, challenged: false },
  });
  const usableUnknownResult = await usableUnknownWorker.checkAuth('linkedin', 132, 7, SEARCH_URL);
  assert.deepStrictEqual(
    { authenticated: usableUnknownResult.authenticated, ready: usableUnknownResult.ready, auth_state: usableUnknownResult.auth_state },
    { authenticated: true, ready: true, auth_state: 'verified' },
  );
  assert.strictEqual(callsFor(usableUnknownWorker, 'platform_readiness').length, 1);
  assert.strictEqual(callsFor(usableUnknownWorker, 'platform_readiness')[0].payload.status, 'verified');

  const unusableUnknownWorker = makeWorker({
    authPage: { platform: 'linkedin', auth_state: 'unknown', authenticated: false, page_url: AUTH_URL },
    searchPage: { platform: 'linkedin', page_type: 'search', page_url: SEARCH_URL, ready: false, login_required: false, challenged: false },
  });
  const unusableUnknownResult = await unusableUnknownWorker.checkAuth('linkedin', 132, 7, SEARCH_URL);
  assert.deepStrictEqual(
    { authenticated: unusableUnknownResult.authenticated, ready: unusableUnknownResult.ready, auth_state: unusableUnknownResult.auth_state },
    { authenticated: false, ready: false, auth_state: 'unknown' },
  );
  assert.strictEqual(callsFor(unusableUnknownWorker, 'platform_readiness')[0].payload.status, 'retryable');
  assert.strictEqual(callsFor(unusableUnknownWorker, 'platform_auth_result').length, 0);

  for (const fixture of [
    {
      platform: 'indeed', authUrl: INDEED_AUTH_URL, searchUrl: INDEED_SEARCH_URL,
      searchPage: { platform: 'indeed', page_type: 'search', page_url: INDEED_SEARCH_URL, ready: true, login_required: false, challenged: false },
    },
    {
      platform: 'glassdoor', authUrl: GLASSDOOR_AUTH_URL, searchUrl: GLASSDOOR_SEARCH_URL,
      searchPage: { platform: 'glassdoor', page_type: 'search', page_url: GLASSDOOR_SEARCH_URL, ready: true, login_required: false, challenged: false },
    },
  ]) {
    const worker = makeWorker({
      authUrl: fixture.authUrl,
      searchUrl: fixture.searchUrl,
      landingError: new Error('content script did not respond'),
      searchPage: fixture.searchPage,
    });
    const result = await worker.checkAuth(fixture.platform, 132, 7, fixture.searchUrl);
    assert.deepStrictEqual(
      { authenticated: result.authenticated, ready: result.ready, auth_state: result.auth_state },
      { authenticated: true, ready: true, auth_state: 'verified' },
      `${fixture.platform} requested search surface must remain authoritative after landing receiver failure`,
    );
    assert(worker.messages.some((message) => message.type === 'JOBBOT_INSPECT_SEARCH_EVENTUALLY' && message.url === fixture.searchUrl));
    assert.strictEqual(callsFor(worker, 'platform_readiness')[0].payload.platform, fixture.platform);
    assert.strictEqual(callsFor(worker, 'platform_readiness')[0].payload.status, 'verified');
  }

  const unavailableSurfaceWorker = makeWorker({
    landingError: new Error('Could not establish connection. Receiving end does not exist.'),
    searchError: new Error('Could not establish connection. Receiving end does not exist.'),
  });
  let unavailableSurfaceError;
  try {
    await unavailableSurfaceWorker.checkAuth('linkedin', 132, 7, SEARCH_URL);
  } catch (error) {
    unavailableSurfaceError = error;
  }
  assert(unavailableSurfaceError, 'an unreachable requested search surface must fail closed');
  assert.match(String(unavailableSurfaceError.message), /receiving end does not exist/i);
  assert.strictEqual(callsFor(unavailableSurfaceWorker, 'platform_readiness').length, 0, 'runProduction owns retryable classification for a thrown probe failure');
  assert.strictEqual(callsFor(unavailableSurfaceWorker, 'platform_auth_result').length, 0);

  console.log('Service-worker auth return regressions passed: sign-in/challenge short-circuits, authoritative Indeed/Glassdoor fallback, and fail-closed missing surface');
}

run().catch((error) => { console.error(error); process.exitCode = 1; });
