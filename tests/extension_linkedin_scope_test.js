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
