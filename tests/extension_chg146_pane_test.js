'use strict';

// CHG-146 pane oracle: exercise the shipped content scripts against small
// desktop-search fixtures. Every platform must prove selection, identity,
// embedded-pane provenance, and substantive detail before completion.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

class Node {
  constructor(tag = '#root', attrs = {}) { this.tagName = tag.toUpperCase(); this.attrs = attrs; this.children = []; this.parentElement = null; }
  append(child) { child.parentElement = this; this.children.push(child); return child; }
  get textContent() { return this.children.map((child) => child.textContent).join(''); }
  get innerText() { return this.textContent; }
  get href() { return this.getAttribute('href') || ''; }
  getAttribute(name) { return this.attrs[name] ?? null; }
  contains(node) { return this === node || this.children.some((child) => child.contains(node)); }
  closest(selector) { for (let node = this; node; node = node.parentElement) if (node.matches(selector)) return node; return null; }
  matches(selector) {
    return selector.split(',').some((part) => this._matchesOne(part.trim()));
  }
  _matchesOne(selector) {
    if (!selector || this.tagName === '#TEXT') return false;
    selector = selector.replace(/:not\([^)]*\)/g, '');
    const tag = selector.match(/^([a-zA-Z][\w-]*)/);
    if (tag && this.tagName !== tag[1].toUpperCase()) return false;
    const id = selector.match(/#([\w-]+)/);
    if (id && this.getAttribute('id') !== id[1]) return false;
    for (const cls of [...selector.matchAll(/\.([\w-]+)/g)]) if (!String(this.attrs.class || '').split(/\s+/).includes(cls[1])) return false;
    for (const attr of [...selector.matchAll(/\[([\w:-]+)(?:([*^$]?=)["']?([^\]"']+)["']?)?\]/g)]) {
      const value = this.getAttribute(attr[1]); if (value === null) return false;
      if (attr[2] === '=' && value !== attr[3]) return false;
      if (attr[2] === '*=' && !value.includes(attr[3])) return false;
      if (attr[2] === '^=' && !value.startsWith(attr[3])) return false;
      if (attr[2] === '$=' && !value.endsWith(attr[3])) return false;
    }
    return true;
  }
  querySelectorAll(selector) {
    const found = []; const visit = (node) => { for (const child of node.children) { if (child.matches(selector)) found.push(child); visit(child); } }; visit(this); return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}
class TextNode extends Node { constructor(text) { super('#text'); this.text = text; } get textContent() { return this.text; } get innerText() { return this.text; } }
function parseHtml(html) {
  const root = new Node(); const stack = [root]; const token = /<!--[\s\S]*?-->|<\/?([a-zA-Z][\w-]*)([^>]*)>/g; let cursor = 0; let match;
  while ((match = token.exec(html))) {
    if (match.index > cursor) stack[stack.length - 1].append(new TextNode(html.slice(cursor, match.index)));
    cursor = token.lastIndex; if (!match[1]) continue; const raw = match[2] || '';
    if (match[0][1] === '/') { if (stack.length > 1) stack.pop(); continue; }
    const attrs = {}; for (const attr of raw.matchAll(/([\w:-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g)) attrs[attr[1]] = attr[2] ?? attr[3] ?? attr[4] ?? '';
    const node = stack[stack.length - 1].append(new Node(match[1], attrs));
    if (!/\/(?:\s*)$/.test(raw) && !['meta', 'link', 'input', 'br', 'img'].includes(match[1].toLowerCase())) stack.push(node);
  }
  return root;
}

const specs = [
  { platform: 'linkedin', id: '7201', fixture: 'linkedin_search_pane.html', scripts: ['extension/linkedin.js'], search: 'https://www.linkedin.com/jobs/search/?keywords=patient', cardSelector: 'a[href*="/jobs/view/"]', paneMarker: 'jobs-description' },
  { platform: 'indeed', id: 'ipane-1', fixture: 'indeed_search_pane.html', scripts: ['extension/indeed.js'], search: 'https://www.indeed.com/jobs?q=patient', cardSelector: 'a[href*="/viewjob?jk="]', paneMarker: 'jobDescriptionText' },
  { platform: 'glassdoor', id: 'gpane-1', fixture: 'glassdoor_search_pane.html', scripts: ['extension/glassdoor.js'], search: 'https://www.glassdoor.com/Job/remote-patient-jobs-SRCH_IL.0,6_IS11047_KO7,21.htm', cardSelector: 'a[href*="/job-listing/"]', paneMarker: 'jobDescriptionContent' },
];

function variant(spec, kind) {
  let html = fs.readFileSync(path.join('tests/fixtures', spec.fixture), 'utf8');
  if (kind === 'wrong_identity') html = html.replace(/(<h1[^>]*>)[^<]*/, '$1Stale unrelated role');
  if (kind === 'missing_description') html = html.replace(/<div[^>]*(?:id="jobDescriptionText"|data-test="jobDescriptionContent"|class="jobs-description__content")[\s\S]*?<\/div>/, '');
  if (kind === 'challenge') html = html.replace(/<main[^>]*>/, (tag) => `${tag}<div>CAPTCHA verification required</div>`);
  if (kind === 'sign_in') html = html.replace(/<main[^>]*>/, (tag) => `${tag}<h1>Sign in to continue</h1>`);
  return html;
}

async function inspect(spec, kind, options = {}) {
  const root = parseHtml(variant(spec, kind));
  const parsed = new URL(spec.search); global.location = { href: parsed.href, pathname: parsed.pathname, host: parsed.host };
  global.document = { body: root, title: `${spec.platform} jobs`, querySelector: root.querySelector.bind(root), querySelectorAll: root.querySelectorAll.bind(root) };
  const anchor = root.querySelector(spec.cardSelector);
  if (anchor) anchor.click = () => { if (options.navigate) global.location.href = options.navigate; };
  if (options.delayed) {
    let hidden = 2; const original = global.document.querySelector;
    global.document.querySelector = (selector) => {
      if ((selector.includes('jobsearch-ViewjobPane') || selector.includes('JobDetails') || selector.includes('jobs-details')) && hidden > 0) { hidden -= 1; return null; }
      return original.call(global.document, selector);
    };
  }
  let listener; global.chrome = { runtime: { onMessage: { addListener(fn) { listener = fn; } } } };
  for (const file of ['extension/selectors.js', 'extension/common.js', ...spec.scripts]) vm.runInThisContext(fs.readFileSync(file, 'utf8'), { filename: file });
  return new Promise((resolve) => listener({ type: 'JOBBOT_INSPECT_SEARCH_PANE', source_job_id: spec.id, select: true }, null, resolve));
}

global.setTimeout = (fn) => { fn(); return 0; };
(async () => {
  for (const spec of specs) {
    const success = await inspect(spec, 'success');
    assert.strictEqual(success.selected, true, `${spec.platform}: selected card`);
    assert.strictEqual(success.identity_status, 'PROVEN', `${spec.platform}: identity proof`);
    assert.strictEqual(success.search_pane, true, `${spec.platform}: embedded pane provenance`);
    assert.ok(String(success.job?.description || '').length > 40, `${spec.platform}: substantive description`);
    assert.strictEqual(success.job?.source_job_id, spec.id, `${spec.platform}: source/result identity linkage`);
    assert.ok(String(success.job?.canonical_url || '').length > 0, `${spec.platform}: canonical/result linkage`);
    if (spec.platform === 'glassdoor') assert.ok(String(success.job.canonical_url).includes('/job-listing/'), 'glassdoor: canonical/result linkage');
    assert.strictEqual(success.detail_acquisition.mode, 'search_pane');

    const delayed = await inspect(spec, 'success', { delayed: true });
    assert.strictEqual(delayed.identity_status, 'PROVEN', `${spec.platform}: delayed hydration`);
    const wrong = await inspect(spec, 'wrong_identity');
    assert.strictEqual(wrong.identity_status, 'MISMATCH', `${spec.platform}: stale identity fail-closed`);
    const missing = await inspect(spec, 'missing_description');
    assert.notStrictEqual(missing.identity_status, 'PROVEN', `${spec.platform}: missing detail fail-closed`);
    const challenge = await inspect(spec, 'challenge');
    assert.strictEqual(challenge.challenged, true, `${spec.platform}: challenge after selection`);
    const login = await inspect(spec, 'sign_in');
    assert.strictEqual(login.login_required, true, `${spec.platform}: sign-in wall`);
    const gone = await inspect(spec, 'success');
    assert.strictEqual((await inspect(spec, 'success')).identity_status, 'PROVEN');
    const missingCard = await inspect(spec, 'success');
    // The next call deliberately requests an identity not present in the scoped card set.
    const disappeared = await new Promise(async (resolve) => {
      const root = parseHtml(variant(spec, 'success')); const parsed = new URL(spec.search); global.location = { href: parsed.href, pathname: parsed.pathname, host: parsed.host };
      global.document = { body: root, title: `${spec.platform} jobs`, querySelector: root.querySelector.bind(root), querySelectorAll: root.querySelectorAll.bind(root) };
      let listener; global.chrome = { runtime: { onMessage: { addListener(fn) { listener = fn; } } } };
      for (const file of ['extension/selectors.js', 'extension/common.js', ...spec.scripts]) vm.runInThisContext(fs.readFileSync(file, 'utf8'), { filename: file });
      listener({ type: 'JOBBOT_INSPECT_SEARCH_PANE', source_job_id: 'does-not-exist', select: true }, null, resolve);
    });
    assert.strictEqual(disappeared.identity_status, 'MISSING_CARD', `${spec.platform}: selected card disappeared`);
    const navigation = await inspect(spec, 'success', { navigate: spec.platform === 'linkedin' ? 'https://www.linkedin.com/jobs/view/7201/' : spec.platform === 'indeed' ? 'https://www.indeed.com/viewjob?jk=ipane-1' : 'https://www.glassdoor.com/job-listing/patient-access-JV_?jl=gpane-1' });
    assert.strictEqual(navigation.navigation_context_lost, true, `${spec.platform}: navigation/context loss`);
    console.log(`${spec.platform} CHG-146 pane oracle passed: success, stale, delayed, incomplete, challenge, login, disappearance, navigation`);
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
