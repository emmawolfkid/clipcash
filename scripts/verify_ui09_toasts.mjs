/* Deterministic DOM/clock contracts for static/js/toasts.js, the one action-feedback controller
   (templates/components/messages.html). These do not claim browser or screen-reader QA; see
   apps/core/tests/test_action_feedback.py (CLIPCASH_FEEDBACK_BROWSER=1) for Chromium checks. */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const DELAY = 8000;

export function verifyToasts() {
  const source = readFileSync(new URL("../static/js/toasts.js", import.meta.url), "utf8");
  let passed = 0;

  function setup(specs, second = []) {
    let now = 0;
    let sequence = 0;
    const timers = new Map();
    const listeners = {};
    const on = (target, name, fn) => { (target[name] = target[name] || []).push(fn); };
    const document = {
      hidden: false, activeElement: null,
      addEventListener: (name, fn) => on(listeners, "document:" + name, fn),
    };
    const fire = (name, data = {}) => (listeners[name] || []).forEach(fn => fn(data));

    function node(tag, { classes = [], attrs = {}, text = "" } = {}) {
      const handlers = {};
      const self = {
        tag, classes: new Set(classes), attrs: new Map(Object.entries(attrs)), children: [], parent: null,
        textContent: text, hidden: false, tabIndex: 0, connectedRoot: false,
        hasAttribute: name => self.attrs.has(name),
        getAttribute: name => (self.attrs.has(name) ? self.attrs.get(name) : null),
        setAttribute: (name, value) => { self.attrs.set(name, String(value)); },
        removeAttribute: name => { self.attrs.delete(name); },
        appendChild: child => {
          if (child.parent) { child.parent.children.splice(child.parent.children.indexOf(child), 1); }
          child.parent = self; self.children.push(child); return child;
        },
        remove: () => {
          if (self.parent) { self.parent.children.splice(self.parent.children.indexOf(self), 1); self.parent = null; }
          self.connectedRoot = false;
        },
        get isConnected() { let n = self; while (n.parent) { n = n.parent; } return n.connectedRoot; },
        get nextElementSibling() { const s = self.parent ? self.parent.children : []; return s[s.indexOf(self) + 1] || null; },
        get previousElementSibling() { const s = self.parent ? self.parent.children : []; return s[s.indexOf(self) - 1] || null; },
        contains: other => { for (let n = other; n; n = n.parent) { if (n === self) { return true; } } return false; },
        querySelector: selector => {
          for (const child of self.children) {
            if (matches(child, selector)) { return child; }
            const found = child.querySelector(selector);
            if (found) { return found; }
          }
          return null;
        },
        focus: () => { document.activeElement = self; },
        addEventListener: (name, fn) => on(handlers, name, fn),
        fire: (name, data = {}) => (handlers[name] || []).forEach(fn => fn(data)),
      };
      return self;
    }
    function matches(element, selector) {
      return selector.split(",").map(part => part.trim()).some(part => {
        if (part.startsWith(".")) { return element.classes.has(part.slice(1)); }
        if (part.startsWith("[")) { return element.hasAttribute(part.slice(1, -1)); }
        if (part === "button:not([data-feedback-close])") {
          return element.tag === "button" && !element.hasAttribute("data-feedback-close");
        }
        return element.tag === part;
      });
    }
    function makeCard(spec) {
      const level = spec.level || "success";
      const auto = spec.auto ?? level === "success";
      const card = node("li", { classes: ["messages__item", `messages__item--${level}`],
                               attrs: { "data-level": level, ...(auto ? { "data-feedback-auto": "" } : {}) } });
      card.appendChild(node("span", { classes: ["messages__text"], text: spec.text || "Saved" }));
      if (spec.link) { card.appendChild(node("a")); }
      card.close = card.appendChild(node("button", { classes: ["messages__close"], attrs: { "data-feedback-close": "" } }));
      card.close.hidden = true;
      card.close.addEventListener = (name, fn) => { if (name === "click") { card.close.click = fn; } };
      return card;
    }
    function makeRegion(values) {
      const region = node("div", { attrs: { "data-feedback": "" } });
      region.connectedRoot = true;
      region.polite = region.appendChild(node("div", { attrs: { "data-feedback-announcer": "" } }));
      region.urgent = region.appendChild(node("div", { attrs: { "data-feedback-urgent": "" } }));
      region.list = region.appendChild(node("ul", { classes: ["messages"] }));
      values.map(makeCard).forEach(card => region.list.appendChild(card));
      return region;
    }
    const regions = [makeRegion(specs), ...(second.length ? [makeRegion(second)] : [])];
    const items = [...regions[0].list.children, ...(regions[1] ? regions[1].list.children : [])];
    document.querySelectorAll = selector => (selector === "[data-feedback]" ? regions.filter(r => r.isConnected) : []);
    const field = node("input");
    field.connectedRoot = true;
    const window = {
      setTimeout: (fn, delay) => { timers.set(++sequence, { fn, at: now + delay }); return sequence; },
      clearTimeout: id => timers.delete(id),
      addEventListener: (name, fn) => on(listeners, "window:" + name, fn),
    };
    const context = vm.createContext({ document, window, Object, Array, Date: { now: () => now } });
    function tick(duration) {
      now += duration;
      let due;
      while ((due = [...timers].filter(([, timer]) => timer.at <= now).sort((a, b) => a[1].at - b[1].at)[0])) {
        timers.delete(due[0]);
        due[1].fn();
      }
    }
    const run = () => vm.runInContext(source, context);
    run();
    tick(0);
    const pending = () => [...timers.values()].filter(timer => timer.at > now).length;
    return { regions, document, field, timers, tick, run, fire, items, pending, list: regions[0].list };
  }
  const alive = t => t.list.children;
  function test(name, fn) { fn(); passed++; }

  test("ordinary success expires after eight seconds", () => {
    const t = setup([{}]); t.tick(DELAY - 1); assert.equal(alive(t).length, 1); t.tick(1); assert.equal(alive(t).length, 0);
  });
  test("errors, warnings and info persist", () => {
    const t = setup([{ level: "error" }, { level: "warning" }, { level: "info" }]);
    t.tick(60000); assert.equal(alive(t).length, 3); assert.equal(t.pending(), 0);
  });
  test("explicitly persistent or actionable successes persist", () => {
    const t = setup([{ auto: false }, { text: "Follow this link", link: true }]);
    t.tick(60000); assert.equal(alive(t).length, 2);
  });
  test("errors are announced urgently and the rest politely, after load", () => {
    const t = setup([{ text: "Saved" }, { level: "error", text: "Try again" }, { level: "info", text: "Waiting" }]);
    assert.equal(t.regions[0].urgent.textContent, ""); t.tick(100);
    assert.equal(t.regions[0].urgent.textContent, "Try again");
    assert.equal(t.regions[0].polite.textContent, "Saved Waiting");
    assert.equal(t.document.activeElement, null);
  });
  test("close buttons are revealed for keyboard use", () => {
    const t = setup([{ level: "error" }]); assert.equal(t.items[0].close.hidden, false);
  });
  test("hover pauses and resumes remaining time", () => {
    const t = setup([{}]); const card = t.items[0]; t.tick(2000); card.fire("pointerenter"); t.tick(10000);
    assert.equal(alive(t).length, 1); card.fire("pointerleave"); t.tick(5999); assert.equal(alive(t).length, 1);
    t.tick(1); assert.equal(alive(t).length, 0);
  });
  test("focus pauses and leaving hover does not resume while focused", () => {
    const t = setup([{}]); const card = t.items[0]; card.fire("pointerenter"); card.close.focus(); card.fire("focusin");
    card.fire("pointerleave"); t.tick(20000); assert.equal(alive(t).length, 1);
    t.field.focus(); card.fire("focusout"); t.tick(0); t.tick(DELAY); assert.equal(alive(t).length, 0);
  });
  test("hidden documents pause dismissal", () => {
    const t = setup([{}]); t.document.hidden = true; t.fire("document:visibilitychange"); t.tick(20000);
    assert.equal(alive(t).length, 1); t.document.hidden = false; t.fire("document:visibilitychange"); t.tick(DELAY);
    assert.equal(alive(t).length, 0);
  });
  test("closing moves focus to the next card, then back to the task", () => {
    const t = setup([{ level: "error" }, { level: "info" }]);
    t.fire("document:focusin", { target: t.field });
    t.items[0].close.focus(); t.items[0].close.click();
    assert.equal(alive(t).length, 1); assert.equal(t.document.activeElement, t.items[1].close);
    t.items[1].close.click(); assert.equal(t.document.activeElement, t.field);
  });
  test("Escape closes only the focused card", () => {
    const t = setup([{ level: "error" }, { level: "info" }]); t.items[0].close.focus();
    let prevented = false; t.items[0].fire("keydown", { key: "Escape", preventDefault: () => { prevented = true; } });
    assert(prevented); assert.deepEqual(alive(t), [t.items[1]]); assert.equal(t.document.activeElement, t.items[1].close);
  });
  test("duplicate includes combine into one stack and deduplicate", () => {
    const t = setup([{ text: "Saved" }], [{ text: "Saved" }, { level: "error", text: "Saved" }]);
    assert(!t.regions[1].isConnected); assert.equal(alive(t).length, 2);
    t.tick(100); assert.equal(t.regions[0].urgent.textContent, "Saved"); assert.equal(t.regions[0].polite.textContent, "Saved");
  });
  test("deduplication retains the persistent copy", () => {
    const t = setup([{}], [{ auto: false }]);
    assert.equal(alive(t).length, 1); t.tick(20000); assert.equal(alive(t).length, 1); assert.equal(t.pending(), 0);
  });
  test("reinitialization does not add timers", () => {
    const t = setup([{}]); const before = t.pending(); t.run(); assert.equal(t.pending(), before);
    assert.equal(t.items[0].close.hidden, false);
  });
  test("leaving the page dismisses consumed feedback", () => {
    const t = setup([{ level: "error" }, { level: "warning" }]); t.fire("window:pagehide"); assert.equal(alive(t).length, 0);
  });
  test("no messages is a no-op", () => {
    vm.runInNewContext(source, { document: { querySelectorAll: () => [] } });
  });
  return `PASS: ${passed} notification behavior scenarios (DOM/clock mocks).`;
}

console.log(verifyToasts());
