'use strict';

const EXPECTED_EXTENSION_VERSION = '3.2.1';
const EXPECTED_EXTENSION_BUILD = '3.2.2-prod-ready';
const stateEl = document.getElementById('state');
const startButton = document.getElementById('start');
const stopButton = document.getElementById('stop');
const emergencyButton = document.getElementById('emergency');
const query = new URLSearchParams(location.search);
const runId = Number(query.get('run_id') || 0);
const bridgePort = Number(query.get('bridge_port') || 0);
const bridgeToken = String(query.get('bridge_token') || '');
let bridgeReady = false;

function render(value) {
  if (!value?.ok) {
    stateEl.textContent = `Bridge: ERROR\n${value?.error || 'Unknown error'}${value?.message ? `\n${value.message}` : ''}`;
    stateEl.className = 'status bad';
    return;
  }
  if (!value.run) { stateEl.textContent = JSON.stringify(value, null, 2); return; }
  const run = value.run;
  const lines = [
    `Bridge: CONNECTED`, `Run #${run.browser_run_id} — ${run.status}`,
    `Mode: ${run.mode} | Platforms: ${run.platform}`,
    `Jobs: ${run.jobs_recorded} | new ${run.jobs_new} | updated ${run.jobs_updated} | unchanged ${run.jobs_unchanged}`,
    `Current task: ${run.current_task_id || 'none'} | last progress: ${run.last_progress_at || 'never'}`,
    `Last error: ${run.last_error || 'none'}`, '',
  ];
  for (const platform of value.platforms || []) lines.push(`${platform.platform.padEnd(10)} auth=${String(platform.auth_status || '').padEnd(17)} exhausted=${platform.tasks_completed}/${platform.tasks_total} incomplete=${platform.tasks_incomplete} challenged=${platform.tasks_challenged} failed=${platform.tasks_failed} jobs=${platform.jobs_recorded}`);
  const counts = {};
  for (const task of value.tasks || []) { const key = `${task.platform}/${task.status}`; counts[key] = (counts[key] || 0) + 1; }
  lines.push(''); for (const [key, count] of Object.entries(counts).sort()) lines.push(`${key.padEnd(30)} ${count}`);
  const active = (value.tasks || []).find((task) => task.status === 'running');
  if (active) lines.push('', `Active: ${active.platform} · ${active.query_text}`, `Page ${active.page_number || active.pages_visited || 0} · results ${active.results_seen || 0} · details ${active.detail_count_read || 0} · unique ${active.unique_jobs_recorded || 0} · duplicates ${active.duplicate_sightings || 0}`, `URL: ${active.current_search_url || 'starting'}`, `Error: ${active.last_error || 'none'}`);
  stateEl.textContent = lines.join('\n'); stateEl.className = `status ${run.status === 'completed' ? 'ok' : ''}`;
}
const send = (message) => new Promise((resolve) => chrome.runtime.sendMessage(message, (value) => resolve(value || { ok: false, error: chrome.runtime.lastError?.message || 'No response from service worker' })));
async function reloadStaleExtension() { const manifest = chrome.runtime.getManifest(); const loaded = String(manifest.version || ''); const loadedBuild = String(manifest.version_name || ''); if (loaded === EXPECTED_EXTENSION_VERSION && loadedBuild === EXPECTED_EXTENSION_BUILD) return false; if (!bridgePort || !bridgeToken || !runId) throw new Error(`Extension ${loaded || 'unknown'} / ${loadedBuild || 'unknown'} is stale; reload extension/ from chrome://extensions.`); stateEl.textContent = `Updating unpacked JobBot extension ${loaded || 'unknown'} / ${loadedBuild || 'unknown'} → ${EXPECTED_EXTENSION_VERSION} / ${EXPECTED_EXTENSION_BUILD}…`; await chrome.storage.local.set({ jobbot_bridge_config: { port: bridgePort, token: bridgeToken }, jobbot_active_run_id: runId }); setTimeout(() => chrome.runtime.reload(), 100); return true; }
async function configure() { if (bridgeReady) return { ok: true }; if (!bridgePort || !bridgeToken) return { ok: false, error: 'Missing local bridge configuration. Launch this page using python -m jobbot run-now or resume.' }; const value = await send({ type: 'JOBBOT_CONFIGURE_BRIDGE', port: bridgePort, token: bridgeToken }); bridgeReady = !!value?.ok; if (!bridgeReady) render(value); return value; }
async function terminalFallback() { const saved = await chrome.storage.local.get('jobbot_last_run_state'); const last = saved.jobbot_last_run_state; if (last && Number(last.run_id) === runId && ['completed', 'partial', 'stopped', 'failed'].includes(last.status)) { render({ ok: true, run: { browser_run_id: runId, status: last.status, mode: 'run-now', platform: 'primary sources', jobs_recorded: 0, jobs_new: 0, jobs_updated: 0, jobs_unchanged: 0, last_progress_at: last.at, last_error: '' }, platforms: [] }); return true; } return false; }
async function status() { if (!runId) { render({ ok: false, error: 'Missing run_id' }); return; } try { const configured = await configure(); if (!configured.ok) { if (!(await terminalFallback())) render(configured); return; } render(await send({ type: 'JOBBOT_GET_STATUS', run_id: runId })); } catch (error) { if (!(await terminalFallback())) render({ ok: false, error: String(error?.message || error) }); } }
async function start() { startButton.disabled = true; stateEl.textContent = 'Connecting local bridge and starting / resuming normal-Chrome search…'; try { if (await reloadStaleExtension()) return; const configured = await configure(); if (!configured.ok) { startButton.disabled = false; return; } render(await send({ type: 'JOBBOT_START_RUN', run_id: runId })); startButton.disabled = false; status(); } catch (error) { render({ ok: false, error: String(error?.message || error) }); startButton.disabled = false; } }
async function stopAfter() { render(await send({ type: 'JOBBOT_STOP_AFTER_CURRENT', run_id: runId })); }
async function emergency() { if (!confirm('Stop the current read-only search immediately?')) return; render(await send({ type: 'JOBBOT_EMERGENCY_STOP', run_id: runId })); }

startButton.addEventListener('click', start); stopButton?.addEventListener('click', stopAfter); emergencyButton?.addEventListener('click', emergency); setInterval(status, 3000); status(); if (query.get('autorun') === '1') setTimeout(start, 700);
