'use strict';

const summaryEl = document.getElementById('summary');
const identityEl = document.getElementById('identity');
const diagnosticEl = document.getElementById('diagnostic');
const runStateEl = document.getElementById('runState');
const coverageEl = document.getElementById('coverage');
const discoveryHeadEl = document.getElementById('discoveryHead');
const discoveryBodyEl = document.getElementById('discoveryBody');
const pendingCountEl = document.getElementById('pendingCount');
const filtersEl = document.getElementById('filters');
const headEl = document.getElementById('head');
const bodyEl = document.getElementById('body');
const pageEl = document.getElementById('page');
const prevButton = document.getElementById('prev');
const nextButton = document.getElementById('next');
const exportButton = document.getElementById('exportButton');
const messageEl = document.getElementById('message');
const detailEl = document.getElementById('detail');
const statusActionsEl = document.getElementById('statusActions');
const actionableViewButton = document.getElementById('actionableView');
const allViewButton = document.getElementById('allView');
const resetFiltersButton = document.getElementById('resetFilters');

const state = { page: 1, view: 'actionable', selectedJob: '', selectedIds: new Set(), lastRefresh: 'never' };
const discoveryColumns = ['result_id','platform','title_hint','company_hint','location_hint','posted_text','detail_status','detail_attempts','observed_at'];
const escapeText = (value) => String(value ?? '');

function noteError(section, error) {
  const message = error instanceof Error ? error.message : String(error);
  state.lastRefresh = new Date().toLocaleString();
  diagnosticEl.hidden = false;
  diagnosticEl.textContent = `Dashboard error · ${section}\n${message}\nLast refresh attempt: ${state.lastRefresh}`;
}

function clearErrorIfHealthy() {
  diagnosticEl.hidden = true;
  diagnosticEl.textContent = '';
}

async function api(path, options) {
  const response = await fetch(path, { cache: 'no-store', ...options });
  let payload;
  try { payload = await response.json(); } catch (_) { throw new Error(`${path}: invalid JSON (HTTP ${response.status})`); }
  if (!response.ok || payload?.ok === false) throw new Error(`${path}: ${payload?.error || payload?.message || `HTTP ${response.status}`}`);
  return payload;
}

function cells(row, columns) {
  return columns.map((column) => { const cell = document.createElement('td'); cell.textContent = escapeText(row[column]); return cell; });
}

async function refreshIdentity() {
  const value = await api('/api/identity');
  identityEl.textContent = `Database: ${value.resolved_database_path} · identity ${value.database_identity} · pid ${value.pid}`;
}

async function refreshSummary() {
  const value = await api('/api/summary');
  summaryEl.replaceChildren();
  for (const [key, number] of Object.entries(value)) {
    const card = document.createElement('div'); card.className = 'card';
    const label = document.createElement('span'); label.textContent = key.replaceAll('_', ' ');
    const total = document.createElement('b'); total.textContent = escapeText(number);
    card.append(label, total); summaryEl.append(card);
  }
}

async function refreshRun() {
  const value = await api('/api/run');
  if (!value.run) { runStateEl.textContent = 'No browser run yet.'; return; }
  const run = value.run;
  const lines = [`RUN #${run.browser_run_id} · ${run.status} · last progress ${run.last_progress_at || 'never'}`];
  if (value.current_task) {
    const task = value.current_task;
    lines.push(`ACTIVE ${task.platform} · ${task.query_text} · page ${task.page_number || 0} · results ${task.results_seen || 0} · details ${task.detail_count_read || 0}`);
    lines.push(`cards extracted ${task.cards_extracted || 0} · persisted ${task.cards_persistence_succeeded || 0}/${task.cards_persistence_attempted || 0} · failed ${task.cards_persistence_failed || 0} · duplicates ${task.duplicate_cards || 0} · pending ${task.pending_details || 0} · detail errors ${task.details_failed || 0}`);
  }
  for (const platform of value.platforms || []) {
    lines.push(`${platform.platform}: auth=${platform.auth_status} running=${platform.running} exhausted=${platform.tasks_completed}/${platform.tasks_total} deferred=${platform.deferred} discoveries=${platform.discoveries} cards=${platform.cards_persisted || 0}/${platform.cards_extracted || 0} card_errors=${platform.cards_failed || 0} duplicates=${platform.duplicate_cards || 0} details=${platform.details_complete}`);
  }
  runStateEl.textContent = lines.join('\n');
}

async function refreshCoverage() {
  const value = await api('/api/coverage');
  const lines = ['PRIMARY COVERAGE'];
  for (const [site, item] of Object.entries(value.primary || {})) lines.push(`${site}: tasks ${item.total} · exhausted ${item.exhausted} · incomplete ${item.incomplete} · challenged ${item.challenged} · auth ${item.auth_required} · deferred ${item.deferred} · failed ${item.failed} · results ${item.results} · details ${item.details}`);
  lines.push('', `SUPPLEMENTAL: ${Object.entries(value.supplemental || {}).map(([key, number]) => `${key} ${number}`).join(' · ') || 'none recorded'}`);
  coverageEl.textContent = lines.join('\n');
}

async function refreshDiscoveries() {
  const value = await api('/api/discoveries?limit=100');
  pendingCountEl.textContent = `(${value.pending} pending)`;
  discoveryHeadEl.replaceChildren(...discoveryColumns.map((column) => { const th = document.createElement('th'); th.textContent = column; return th; }));
  discoveryBodyEl.replaceChildren();
  for (const row of value.discoveries || []) {
    const tr = document.createElement('tr'); tr.append(...cells(row, discoveryColumns));
    tr.addEventListener('click', () => { if (row.source_url) window.open(row.source_url, '_blank', 'noopener'); });
    discoveryBodyEl.append(tr);
  }
}

function queryParams() {
  const params = new URLSearchParams(new FormData(filtersEl));
  params.set('view', state.view); params.set('page', String(state.page)); params.set('page_size', '50');
  return params;
}

async function refreshJobs() {
  const checkedBefore = new Set([...bodyEl.querySelectorAll('input[type="checkbox"]:checked')].map((input) => input.dataset.id));
  checkedBefore.forEach((id) => state.selectedIds.add(id));
  const value = await api(`/api/jobs?${queryParams()}`);
  headEl.replaceChildren(document.createElement('th'));
  headEl.firstChild.textContent = 'Select';
  headEl.append(...value.columns.map((column) => { const th = document.createElement('th'); th.textContent = column; return th; }));
  bodyEl.replaceChildren();
  if (!value.jobs.length && state.view === 'actionable') messageEl.textContent = 'No currently actionable jobs; choose ALL DISCOVERIES to inspect the full warehouse.';
  for (const row of value.jobs || []) {
    const tr = document.createElement('tr');
    const selectCell = document.createElement('td'); const checkbox = document.createElement('input');
    checkbox.type = 'checkbox'; checkbox.dataset.id = row.job_id; checkbox.checked = state.selectedIds.has(row.job_id);
    checkbox.addEventListener('change', () => checkbox.checked ? state.selectedIds.add(row.job_id) : state.selectedIds.delete(row.job_id));
    selectCell.append(checkbox); tr.append(selectCell, ...cells(row, value.columns));
    tr.addEventListener('click', (event) => { if (event.target !== checkbox) showJob(row.job_id); }); bodyEl.append(tr);
  }
  pageEl.textContent = `Page ${value.page} · ${value.total} jobs · view ${state.view}`;
  prevButton.disabled = state.page <= 1; nextButton.disabled = state.page * value.page_size >= value.total;
}

async function showJob(jobId) {
  state.selectedJob = jobId;
  const value = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
  detailEl.textContent = JSON.stringify(value, null, 2);
  statusActionsEl.replaceChildren();
  for (const status of ['SHORTLIST','PREPARED','APPLIED','SCREEN','INTERVIEW','FINAL','OFFER','REJECTED','SKIP','CLOSED']) {
    const button = document.createElement('button'); button.type = 'button'; button.textContent = status;
    button.addEventListener('click', async () => {
      try {
        const notes = window.prompt('Optional note') || '';
        await api(`/api/jobs/${encodeURIComponent(jobId)}/application`, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({status, notes}) });
        await Promise.all([refreshSummary(), refreshJobs(), showJob(jobId)]);
      } catch (error) { noteError(`application ${status}`, error); }
    }); statusActionsEl.append(button);
  }
}

function guarded(section, action) { return action().catch((error) => { noteError(section, error); throw error; }); }
async function refreshAll() {
  const results = await Promise.allSettled([
    guarded('identity', refreshIdentity), guarded('summary', refreshSummary), guarded('run status', refreshRun),
    guarded('coverage', refreshCoverage), guarded('discoveries', refreshDiscoveries), guarded('jobs', refreshJobs),
  ]);
  if (results.every((result) => result.status === 'fulfilled')) clearErrorIfHealthy();
  state.lastRefresh = new Date().toLocaleString();
}

actionableViewButton.addEventListener('click', () => { state.view = 'actionable'; state.page = 1; actionableViewButton.classList.add('selected'); allViewButton.classList.remove('selected'); refreshJobs().catch((error) => noteError('actionable jobs', error)); });
allViewButton.addEventListener('click', () => { state.view = 'all'; state.page = 1; allViewButton.classList.add('selected'); actionableViewButton.classList.remove('selected'); refreshJobs().catch((error) => noteError('all jobs', error)); });
filtersEl.addEventListener('submit', (event) => { event.preventDefault(); state.page = 1; refreshJobs().catch((error) => noteError('filtered jobs', error)); });
resetFiltersButton.addEventListener('click', () => { filtersEl.reset(); state.page = 1; refreshJobs().catch((error) => noteError('reset jobs', error)); });
prevButton.addEventListener('click', () => { if (state.page > 1) { state.page -= 1; refreshJobs().catch((error) => noteError('previous page', error)); } });
nextButton.addEventListener('click', () => { state.page += 1; refreshJobs().catch((error) => noteError('next page', error)); });
exportButton.addEventListener('click', async () => {
  try {
    const value = await api('/api/export-selected', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({job_ids: [...state.selectedIds]}) });
    messageEl.textContent = `Exported ${value.count} jobs to ${value.path}`;
  } catch (error) { noteError('export selected', error); }
});

refreshAll();
window.setInterval(refreshAll, 10000);
