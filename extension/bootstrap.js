'use strict';

const query = new URLSearchParams(location.search);
const runId = Number(query.get('run_id') || 0);
const maintenance = query.get('maintenance') === '1';
const expectedBuild = String(query.get('expected_build') || '');
const refreshId = String(query.get('refresh_id') || '');
const bridgePort = Number(query.get('bridge_port') || 0);
const bridgeToken = String(query.get('bridge_token') || '');
const HANDOFF_KEY = 'jobbot_bootstrap_handoff';

function send(message) {
  return new Promise((resolve, reject) => {
    try {
      chrome.runtime.sendMessage(message, (value) => {
        const lastError = chrome.runtime.lastError;
        if (lastError) {
          reject(new Error(lastError.message || 'Extension message failed'));
        } else if (!value?.ok) {
          reject(new Error(value?.error || 'Extension bootstrap request failed'));
        } else {
          resolve(value);
        }
      });
    } catch (error) {
      reject(error);
    }
  });
}

async function handoffState() {
  const saved = await chrome.storage.local.get(HANDOFF_KEY);
  const value = saved?.[HANDOFF_KEY];
  return value && value.refresh_id === refreshId && value.expected_build === expectedBuild ? value : null;
}

async function saveHandoff(state, error = '') {
  await chrome.storage.local.set({
    [HANDOFF_KEY]: {
      state,
      run_id: runId,
      maintenance,
      expected_build: expectedBuild,
      refresh_id: refreshId,
      error: String(error || ''),
    },
  });
}

async function persistReloadHandoff(refresh) {
  const nextRefreshId = String(refresh?.refresh_id || refreshId);
  await chrome.storage.local.set({
    // These are the documented values consumed by the new service worker on
    // startup. They must be written before runtime.reload() is requested.
    jobbot_bridge_config: { port: bridgePort, token: bridgeToken },
    jobbot_expected_extension_build: expectedBuild,
    jobbot_refresh_id: nextRefreshId,
    ...(runId ? { jobbot_active_run_id: runId } : {}),
    [HANDOFF_KEY]: {
      state: 'reload_requested',
      run_id: runId,
      maintenance,
      expected_build: expectedBuild,
      refresh_id: nextRefreshId,
      error: '',
    },
  });
}

async function bootstrap() {
  const previous = await handoffState().catch(() => null);
  if (previous?.state === 'reload_requested' || previous?.state === 'completed') return previous;

  try {
    // Keep this sequence limited to messages understood by the immediately
    // preceding build. A new handler must never be required to initiate its
    // own upgrade from a resident predecessor service worker.
    await saveHandoff('started');
    await send({ type: 'JOBBOT_CONFIGURE_BRIDGE', port: bridgePort, token: bridgeToken });
    const refresh = await send({
      type: 'JOBBOT_REFRESH_EXTENSION',
      run_id: runId,
      expected_build: expectedBuild,
      refresh_id: refreshId,
    });

    if (refresh.reload_required === true) {
      await persistReloadHandoff(refresh);
      if (typeof chrome.runtime.reload !== 'function') throw new Error('Extension runtime reload is unavailable');
      chrome.runtime.reload();
      return { ok: true, reload_required: true, refresh_id: String(refresh.refresh_id || refreshId) };
    }

    if (refresh.identity_confirmed !== true && refresh.refreshed !== true) {
      throw new Error('Extension refresh did not confirm the expected build');
    }
    if (!maintenance && runId) {
      await send({
        type: 'JOBBOT_START_RUN',
        run_id: runId,
        expected_build: expectedBuild,
        refresh_id: String(refresh.refresh_id || refreshId),
      });
    }
    await saveHandoff('completed');
    return { ok: true, identity_confirmed: true, run_started: !maintenance && !!runId };
  } catch (error) {
    await saveHandoff('failed', error?.message || error).catch(() => {});
    return { ok: false, error: String(error?.message || error) };
  }
}

bootstrap().finally(() => {
  try { window.close(); } catch (_) {}
});
