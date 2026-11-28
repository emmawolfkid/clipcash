/* Run with Node: node tests/referral-share.test.cjs. Simulated DOM, not browser visual validation. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/js/referral-share.js'), 'utf8');

function fixture(share) {
  const events = {};
  const button = {
    hidden: true,
    attrs: {'data-share-title': 'ClipCash', 'data-share-text': 'Use my code ABCD2345', 'data-share-url': 'https://clipcash.example/referrals/join/ABCD2345/'},
    getAttribute(name) { return this.attrs[name] || null; },
    addEventListener(name, handler) { events[name] = handler; },
  };
  const document = {querySelectorAll: (selector) => (selector === '[data-native-share]' ? [button] : [])};
  const navigator = share ? {share} : {};
  vm.runInNewContext(source, {document, navigator});
  return {button, click: () => events.click && events.click()};
}

// Without Web Share support the button stays hidden (copy buttons and WhatsApp remain).
{
  const f = fixture(null);
  assert.equal(f.button.hidden, true);
  assert.equal(typeof f.click, 'function');
}

// With support it is shown and shares exactly the referral title, text and link.
{
  const calls = [];
  const f = fixture((data) => { calls.push(data); return Promise.resolve(); });
  assert.equal(f.button.hidden, false);
  f.click();
  assert.equal(JSON.stringify(calls), JSON.stringify([{title: 'ClipCash', text: 'Use my code ABCD2345', url: 'https://clipcash.example/referrals/join/ABCD2345/'}]));
}

// A cancelled share (rejected promise) is swallowed: no unhandled rejection.
{
  let rejected = false;
  const f = fixture(() => Promise.reject(new Error('AbortError')).catch((e) => { rejected = true; throw e; }));
  f.click();
  setTimeout(() => { assert.equal(rejected, true); console.log('referral-share.test.cjs: all assertions passed'); }, 10);
}
