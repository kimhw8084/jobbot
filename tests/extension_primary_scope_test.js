'use strict';

// Minimal DOM harness for the production content-script collectors. The test
// loads selectors/common/platform scripts exactly as Chrome does; it does not
// duplicate their extraction logic in Python.
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

const specs = [
  { platform: 'linkedin', fixture: 'linkedin_scope.html', scripts: ['extension/linkedin.js'], expected: ['4101', '4102', '4103'] },
  { platform: 'indeed', fixture: 'indeed_scope.html', scripts: ['extension/indeed.js'], expected: ['i4101', 'i4102', 'i4103'] },
  { platform: 'glassdoor', fixture: 'glassdoor_scope.html', scripts: ['extension/glassdoor.js'], expected: ['g4101', 'g4102', 'g4103'] },
];

for (const spec of specs) {
  const root = parseHtml(fs.readFileSync(path.join('tests/fixtures', spec.fixture), 'utf8'));
  const host = spec.platform === 'linkedin' ? 'www.linkedin.com' : `www.${spec.platform}.com`;
  global.location = { href: `https://${host}/jobs/search/?keywords=patient`, pathname: '/jobs/search/', host };
  global.document = { body: root, title: `${spec.platform} jobs`, querySelector: root.querySelector.bind(root), querySelectorAll: root.querySelectorAll.bind(root) };
  global.window = { scrollTo() {} };
  let listener;
  global.chrome = { runtime: { onMessage: { addListener(fn) { listener = fn; } } } };
  for (const file of ['extension/selectors.js', 'extension/common.js', ...spec.scripts]) vm.runInThisContext(fs.readFileSync(file, 'utf8'), { filename: file });
  assert(listener, `${spec.platform} content-script listener was not registered`);
  let inspected;
  listener({ type: 'JOBBOT_INSPECT_SEARCH' }, null, (value) => { inspected = value; });
  const ids = inspected.result_links.map((item) => item.source_job_id).sort();
  assert.deepStrictEqual(ids, [...spec.expected].sort());
  assert.strictEqual(inspected.extraction_scope_missing, false);
  assert.strictEqual(inspected.extraction_diagnostics.candidate_links_total, 6);
  assert.strictEqual(inspected.extraction_diagnostics.candidate_links_in_scope, 3);
  assert.strictEqual(inspected.extraction_diagnostics.candidate_links_outside_scope, 3);
  assert.strictEqual(inspected.extraction_diagnostics.in_scope_source_ids.length, 3);
  assert.strictEqual(inspected.extraction_diagnostics.outside_scope_source_ids.length, 3);
  console.log(`${spec.platform} scope fixture passed: actual=3 outside_scope_excluded=3`);
}

// Scope failure is fail-closed for both newly symmetric collectors.
for (const spec of specs.slice(1)) {
  const root = new Node();
  global.location = { href: `https://www.${spec.platform}.com/jobs/search/`, pathname: '/jobs/search/', host: `www.${spec.platform}.com` };
  global.document = { body: root, title: `${spec.platform} empty`, querySelector: root.querySelector.bind(root), querySelectorAll: root.querySelectorAll.bind(root) };
  let listener;
  global.chrome = { runtime: { onMessage: { addListener(fn) { listener = fn; } } } };
  vm.runInThisContext(fs.readFileSync(`extension/${spec.platform}.js`, 'utf8'), { filename: `extension/${spec.platform}.js` });
  let inspected;
  listener({ type: 'JOBBOT_INSPECT_SEARCH' }, null, (value) => { inspected = value; });
  assert.strictEqual(inspected.extraction_scope_missing, true);
  assert.deepStrictEqual(inspected.result_links, []);
}
console.log('Indeed and Glassdoor missing-scope fixtures passed: fail-closed');
