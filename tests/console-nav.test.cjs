/* Run with Node: node tests/console-nav.test.cjs. Simulated DOM, not browser visual validation. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/js/console-nav.js'), 'utf8');

class El {
  constructor(tag, attrs = {}, children = []) {
    this.tag = tag; this.attrs = attrs; this.children = []; this.hidden = false; this.open = false; this.events = {};
    this.parent = null; children.forEach((c) => this.add(c));
  }
  add(child) { child.parent = this; this.children.push(child); return child; }
  getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  removeAttribute(name) { delete this.attrs[name]; }
  addEventListener(name, fn) { (this.events[name] ||= []).push(fn); }
  emit(name, extra = {}) { (this.events[name] || []).forEach((fn) => fn({ target: this, preventDefault() { this.prevented = true; }, ...extra })); }
  all() { return this.children.flatMap((c) => [c, ...c.all()]); }
  matches(selector) {
    const has = (a) => a in this.attrs;
    if (selector === '[data-console-section]') return has('data-console-section');
    if (selector === '[data-search]') return has('data-search');
    if (selector === '.console-nav__details') return this.attrs.class === 'console-nav__details';
    if (selector === '.console-nav__group') return this.attrs.class === 'console-nav__group';
    if (selector === '.console-nav__rail-link') return this.attrs.class === 'console-nav__rail-link';
    if (selector === 'li') return this.tag === 'li';
    return false;
  }
  querySelectorAll(selector) {
    if (selector === '[data-console-section]:not([hidden])') return this.all().filter((e) => e.matches('[data-console-section]') && !e.hidden);
    return this.all().filter((e) => e.matches(selector));
  }
  querySelector(selector) {
    if (selector.includes(':not([hidden])') && selector.includes('[data-search]')) {
      return this.all().find((e) => e.matches('[data-search]') && !e.hidden && !(e.closest('li') || {}).hidden
                               && !(e.closest('[data-console-section]') || {}).hidden) || null;
    }
    return this.querySelectorAll(selector)[0] || null;
  }
  closest(selector) { let node = this; while (node) { if (node.matches(selector)) return node; node = node.parent; } return null; }
  get nextElementSibling() { const s = this.parent.children; return s[s.indexOf(this) + 1] || null; }
}

function link(label, search, href) { return new El('a', { 'data-search': search, href }); }
function single(label, search, href) { const li = new El('li', { 'data-console-section': '' }); li.add(link(label, search, href)); return li; }
function multi(pages) {
  const li = new El('li', { 'data-console-section': '' });
  const details = li.add(new El('details', { class: 'console-nav__details' }));
  pages.forEach(([label, search, href]) => details.add(new El('li')).add(link(label, search, href)));
  li.add(new El('a', { class: 'console-nav__rail-link', href: pages[0][2], tabindex: '-1', 'aria-hidden': 'true' }));
  return li;
}

const menu = new El('nav', { id: 'console-menu' });
const finance = menu.add(new El('p', { class: 'console-nav__group' }));
const financeList = menu.add(new El('ul'));
const payments = financeList.add(multi([['Verify payments', 'verify payments hakiki malipo', '/manage/membership-payments/'],
  ['Payment methods', 'payment methods njia za malipo', '/manage/membership-payments/instructions/']]));
const withdrawals = financeList.add(multi([['Withdrawal requests', 'withdrawal requests maombi ya kutoa pesa', '/manage/payouts/']]));
const system = menu.add(new El('p', { class: 'console-nav__group' }));
const systemList = menu.add(new El('ul'));
const health = systemList.add(single('System health', 'system health hali ya mfumo jobs worker cron', '/manage/health/'));
const input = new El('input', { 'data-console-search': '' });
const empty = new El('p', { 'data-console-search-empty': '' }); empty.hidden = true;
const classes = new Set();
const documentStub = {
  documentElement: { classList: { contains: (c) => classes.has(c) } },
  getElementById: (id) => (id === 'console-menu' ? menu : null),
  querySelector: (s) => (s === '[data-console-search]' ? input : s === '[data-console-search-empty]' ? empty : null),
};
let assigned = null;
const media = { matches: true, addEventListener() {} };
const windowStub = { matchMedia: () => media, location: { assign: (url) => { assigned = url; } } };
let observer = null;
vm.runInNewContext(source, { document: documentStub, window: Object.assign(windowStub, { MutationObserver: function (fn) { observer = fn; this.observe = () => {}; } }) });

// Swahili search finds the English-named page; other sections hide; Enter opens the first match.
input.attrs.value = ''; input.value = 'hakiki malipo'; input.emit('input');
assert.equal(payments.hidden, false); assert.equal(withdrawals.hidden, true); assert.equal(health.hidden, true);
assert.equal(payments.children[0].open, true);
assert.equal(system.hidden, true); assert.equal(finance.hidden, false); assert.equal(empty.hidden, true);
input.emit('keydown', { key: 'Enter' });
assert.equal(assigned, '/manage/membership-payments/');

// English search, and a query that matches nothing.
input.value = 'cron jobs'; input.emit('input');
assert.equal(health.hidden, false); assert.equal(payments.hidden, true);
input.value = 'zzz'; input.emit('input');
assert.equal(empty.hidden, false);

// Escape clears and restores everything.
input.emit('keydown', { key: 'Escape' });
assert.equal(input.value, ''); assert.equal(payments.hidden, false); assert.equal(health.hidden, false); assert.equal(empty.hidden, true);

// Collapsed rail: section links become focusable and announced only when collapsed.
const rail = payments.children[1];
assert.equal(rail.getAttribute('tabindex'), '-1');
classes.add('sidebar-collapsed'); observer();
assert.equal(rail.getAttribute('tabindex'), null); assert.equal(rail.getAttribute('aria-hidden'), null);
classes.delete('sidebar-collapsed'); observer();
assert.equal(rail.getAttribute('aria-hidden'), 'true');
console.log('console-nav.test.cjs: search (EN/SW), Enter, Escape, empty state and collapsed rail passed');
