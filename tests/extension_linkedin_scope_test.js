'use strict';

// Minimal DOM harness for the production content-script collector. This keeps
// the fixture test on the same JavaScript path that runs in Chrome without a
// third-party browser dependency.
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

class Node {
  constructor(tag = '#root', attrs = {}) { this.tagName = tag.toUpperCase(); this.attrs = attrs; this.children = []; this.parentElement = null; }
  append(child) { child.parentElement = this; this.children.push(child); return child; }
  get textContent() { return this.children.map((child) => child.textContent).join(''); }
  get innerText() { return this.textContent; }
  get href() { return this.getAttribute('href') || ''; }
  getAttribute(name) { return this.attrs[name] ?? null; }
  contains(node) { return this === node || this.children.some((child) => child.contains(node)); }
  matches(selector) {
    return selector.split(',').some((part) => this._matchesOne(part.trim()));
  }
  _matchesOne(selector) {
    if (!selector || this.tagName === '#TEXT') return false;
    selector = selector.replace(/:not\([^)]*\)/g, '');
    const tag = selector.match(/^([a-zA-Z][\w-]*)/);
    if (tag && this.tagName !== tag[1].toUpperCase()) return false;
    for (const cls of [...selector.matchAll(/\.([\w-]+)/g)]) {
      if (!String(this.attrs.class || '').split(/\s+/).includes(cls[1])) return false;
    }
    for (const attr of [...selector.matchAll(/\[([\w:-]+)(?:([*^$]?=)["']?([^\]"']+)["']?)?\]/g)]) {
      const value = this.getAttribute(attr[1]);
      if (value === null) return false;
      if (attr[2] === '=' && value !== attr[3]) return false;
      if (attr[2] === '*=' && !value.includes(attr[3])) return false;
      if (attr[2] === '^=' && !value.startsWith(attr[3])) return false;
      if (attr[2] === '$=' && !value.endsWith(attr[3])) return false;
    }
    return true;
  }
  querySelectorAll(selector) {
    const found = [];
    const visit = (node) => { for (const child of node.children) { if (child.matches(selector)) found.push(child); visit(child); } };
    visit(this);
    return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

class TextNode extends Node {
  constructor(text) { super('#text'); this.text = text; }
  get textContent() { return this.text; }
  get innerText() { return this.text; }
  contains(node) { return this === node; }
}

function parseHtml(html) {
  const root = new Node(); const stack = [root];
  const token = /<!--[\s\S]*?-->|<\/?([a-zA-Z][\w-]*)([^>]*)>/g;
  let cursor = 0; let match;
  while ((match = token.exec(html))) {
    if (match.index > cursor) stack[stack.length - 1].append(new TextNode(html.slice(cursor, match.index)));
    cursor = token.lastIndex;
    if (!match[1]) continue;
    const raw = match[2] || '';
    if (match[0][1] === '/') { if (stack.length > 1) stack.pop(); continue; }
    const attrs = {};
    for (const attr of raw.matchAll(/([\w:-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g)) attrs[attr[1]] = attr[2] ?? attr[3] ?? attr[4] ?? '';
    const node = stack[stack.length - 1].append(new Node(match[1], attrs));
    if (!/\/(?:\s*)$/.test(raw) && !['meta', 'link', 'input', 'br', 'img'].includes(match[1].toLowerCase())) stack.push(node);
  }
  if (cursor < html.length) stack[stack.length - 1].append(new TextNode(html.slice(cursor)));
  return root;
}

const root = parseHtml(fs.readFileSync('tests/fixtures/linkedin_scope.html', 'utf8'));
global.location = { href: 'https://www.linkedin.com/jobs/search/?keywords=patient' };
global.document = { body: root, title: 'LinkedIn jobs', querySelector: root.querySelector.bind(root), querySelectorAll: root.querySelectorAll.bind(root) };
global.window = { scrollTo() {} };
let listener;
global.chrome = { runtime: { onMessage: { addListener(fn) { listener = fn; } } } };

for (const file of ['extension/selectors.js', 'extension/common.js', 'extension/linkedin.js']) vm.runInThisContext(fs.readFileSync(file, 'utf8'), { filename: file });
assert(listener, 'LinkedIn content-script listener was not registered');
let inspected;
listener({ type: 'JOBBOT_INSPECT_SEARCH' }, null, (value) => { inspected = value; });
const ids = inspected.result_links.map((item) => item.source_job_id).sort();
assert.deepStrictEqual(ids, ['4101', '4102', '4103']);
assert.strictEqual(inspected.extraction_scope_missing, false);
assert.strictEqual(inspected.extraction_diagnostics.candidate_links_outside_scope, 3);
assert.ok(!ids.includes('4901') && !ids.includes('4902') && !ids.includes('4903'));
console.log('LinkedIn scope fixture passed: actual=3 outside_scope_excluded=3');

function inspectRoot(nextRoot, title = 'LinkedIn jobs', href = 'https://www.linkedin.com/jobs/search/?keywords=patient') {
  global.location = { href, pathname: '/jobs/search/', host: 'www.linkedin.com' };
  global.document = { body: nextRoot, title, querySelector: nextRoot.querySelector.bind(nextRoot), querySelectorAll: nextRoot.querySelectorAll.bind(nextRoot) };
  let value;
  listener({ type: 'JOBBOT_INSPECT_SEARCH' }, null, (result) => { value = result; });
  return value;
}

const structural = inspectRoot(parseHtml(fs.readFileSync('tests/fixtures/linkedin_structural_scope.html', 'utf8')));
assert.deepStrictEqual(
  structural.result_links.map((item) => item.source_job_id).sort(),
  ['5101', '5102', '5103', '5104', '5105', '5106', '5107'],
);
assert.strictEqual(structural.extraction_scope_missing, false);
assert.strictEqual(structural.extraction_diagnostics.scope_method, 'structural');
assert.strictEqual(structural.extraction_diagnostics.candidate_links_total, 12);
assert.strictEqual(structural.extraction_diagnostics.candidate_links_in_scope, 7);
assert.strictEqual(structural.extraction_diagnostics.candidate_links_outside_scope, 4);
assert.strictEqual(structural.extraction_diagnostics.chosen_root_signature, 'div.search-results-container[data-view-name=search-results-list]');
assert.strictEqual(structural.extraction_diagnostics.chosen_card_signature, 'div.result-card[data-view-name=search-result-card]');
assert.ok(!structural.result_links.some((item) => ['5901', '5902', '5903', '5910'].includes(item.source_job_id)));
console.log('LinkedIn structural scope fixture passed: actual=7 duplicate_deduped=1 outside_scope_excluded=4');

const unsafe = inspectRoot(parseHtml(`
  <main><div class="one"><a href="/jobs/view/6101/?trk=flagship3_search_srp_jobs">One</a></div>
  <div class="two"><a href="/jobs/view/6102/?trk=flagship3_search_srp_jobs">Two</a></div></main>
`));
assert.strictEqual(unsafe.extraction_scope_missing, true);
assert.deepStrictEqual(unsafe.result_links, []);
assert.strictEqual(unsafe.extraction_diagnostics.reason, 'no_safe_result_cluster');
assert.ok(unsafe.extraction_diagnostics.anchor_samples.length <= 20);
assert.ok(JSON.stringify(unsafe.extraction_diagnostics).length < 30000);
console.log('LinkedIn ambiguous scope fixture passed: fail-closed with bounded diagnostics');

const empty = inspectRoot(parseHtml('<main><div>No matching jobs found</div></main>'), 'LinkedIn jobs — no results');
assert.strictEqual(empty.extraction_scope_missing, false);
assert.deepStrictEqual(empty.result_links, []);
assert.strictEqual(empty.extraction_diagnostics.empty_state, true);
assert.strictEqual(empty.extraction_diagnostics.empty_state_reason, 'no matching jobs found');
console.log('LinkedIn verified empty state fixture passed: no false scope failure');

const pagedEmpty = inspectRoot(parseHtml('<main><h1>(19) patient enrollment specialist Jobs in United States</h1></main>'), ' (19) patient enrollment specialist Jobs in United States | LinkedIn', 'https://www.linkedin.com/jobs/search/?keywords=patient&start=25');
assert.strictEqual(pagedEmpty.extraction_scope_missing, true);
const pagedEmptyStable = inspectRoot(parseHtml('<main><h1>(19) patient enrollment specialist Jobs in United States</h1></main>'), ' (19) patient enrollment specialist Jobs in United States | LinkedIn', 'https://www.linkedin.com/jobs/search/?keywords=patient&start=25');
assert.strictEqual(pagedEmptyStable.extraction_scope_missing, false);
assert.deepStrictEqual(pagedEmptyStable.result_links, []);
assert.strictEqual(pagedEmptyStable.exhausted, true);
assert.strictEqual(pagedEmptyStable.exhaustion_reason, 'paged_empty_end_state_stable');
console.log('LinkedIn paged end-state fixture passed: no false scope failure');
