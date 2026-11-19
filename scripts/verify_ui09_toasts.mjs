/* Deterministic DOM/clock contracts; these do not claim browser or screen-reader QA. */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

export function verifyToasts() {
  const source = readFileSync(new URL("../static/js/toasts.js", import.meta.url), "utf8");
  let passed = 0;
  function setup(specs, second = []) {
    let now = 0;
    let sequence = 0;
    const timers = new Map();
    const events = {};
    const document = { hidden: false, activeElement: null, addEventListener: (name, fn) => { events[name] = fn; } };
    const content = { focus: () => { document.activeElement = content; } };
    document.getElementById = () => content;
    function makeToast(spec) {
      const handlers = {};
      const toast = {
        dataset: { level: spec.level || "success", ...(spec.persistent ? { persistent: "true" } : {}) },
        text: spec.text || "Saved", removed: false,
        addEventListener: (event, fn) => { handlers[event] = fn; },
        contains: target => target === close || target === toast,
        querySelector: selector => selector === ".toast__text" ? { textContent: toast.text } : selector === ".toast__close" ? close : spec.link ? {} : null,
        remove: () => { toast.removed = true; },
        fire: (event, data = {}) => handlers[event](data),
      };
      const close = { hidden: true, addEventListener: (_, fn) => { close.click = fn; }, focus: () => { document.activeElement = close; } };
      toast.close = close;
      return toast;
    }
    function makeRegion(values) {
      const items = values.map(makeToast);
      const region = {
        items, removed: false, classes: new Set(),
        querySelectorAll: () => items.filter(item => !item.removed),
        querySelector: () => items.find(item => !item.removed) || null,
        classList: { add: value => region.classes.add(value) },
        remove: () => { region.removed = true; },
        appendChild: item => { item.parent.items.splice(item.parent.items.indexOf(item), 1); items.push(item); item.parent = region; },
        get firstChild() { return items[0]; },
      };
      items.forEach(item => { item.parent = region; });
      return region;
    }
    const regions = [makeRegion(specs), ...(second.length ? [makeRegion(second)] : [])];
    document.querySelectorAll = () => regions.filter(region => !region.removed);
    const window = {
      setTimeout: (fn, delay) => { timers.set(++sequence, { fn, at: now + delay }); return sequence; },
      clearTimeout: id => timers.delete(id),
    };
    const context = vm.createContext({ document, window, Map, Date: { now: () => now } });
    function tick(duration) {
      now += duration;
      for (const [id, timer] of [...timers]) {
        if (timer.at <= now) { timers.delete(id); timer.fn(); }
      }
    }
    const run = () => vm.runInContext(source, context);
    run();
    return { regions, document, content, timers, tick, run, events, items: regions[0].items };
  }
  function test(name, fn) { fn(); passed++; }
  test("ordinary success expires after seven seconds", () => {
    const t = setup([{}]); t.tick(6999); assert(!t.items[0].removed); t.tick(1); assert(t.regions[0].removed);
  });
  test("errors, warnings and info persist", () => {
    const t = setup([{ level: "error" }, { level: "warning" }, { level: "info" }]);
    t.tick(60000); assert(t.items.every(item => !item.removed)); assert.equal(t.timers.size, 0);
  });
  test("explicitly persistent or actionable successes persist", () => {
    const t = setup([{ persistent: true }, { text: "Follow this link", link: true }]);
    t.tick(60000); assert(t.items.every(item => !item.removed));
  });
  test("hover pauses and resumes remaining time", () => {
    const t = setup([{}]); t.tick(2000); t.items[0].fire("mouseenter"); t.tick(10000);
    assert(!t.items[0].removed); t.items[0].fire("mouseleave"); t.tick(4999); assert(!t.items[0].removed);
    t.tick(1); assert(t.items[0].removed);
  });
  test("focus pauses and leaving hover does not resume while focused", () => {
    const t = setup([{}]); const item = t.items[0]; item.fire("mouseenter"); item.fire("focusin");
    item.fire("mouseleave"); t.tick(20000); assert(!item.removed);
    item.fire("focusout", { relatedTarget: t.content }); t.tick(7000); assert(item.removed);
  });
  test("moving focus inside does not restart timer", () => {
    const t = setup([{}]); const item = t.items[0]; item.fire("focusin");
    item.fire("focusout", { relatedTarget: item.close }); t.tick(20000); assert(!item.removed);
  });
  test("hidden documents pause dismissal", () => {
    const t = setup([{}]); t.document.hidden = true; t.events.visibilitychange(); t.tick(20000);
    assert(!t.items[0].removed); t.document.hidden = false; t.events.visibilitychange(); t.tick(7000); assert(t.items[0].removed);
  });
  test("close returns keyboard focus to main content", () => {
    const t = setup([{ level: "error" }]); t.document.activeElement = t.items[0].close;
    t.items[0].close.click(); assert.equal(t.document.activeElement, t.content); assert(t.regions[0].removed);
  });
  test("Escape closes only the focused toast and focuses the next", () => {
    const t = setup([{ level: "error" }, { level: "info" }]); t.document.activeElement = t.items[0].close;
    let prevented = false; t.items[0].fire("keydown", { key: "Escape", preventDefault: () => { prevented = true; } });
    assert(prevented); assert(t.items[0].removed); assert(!t.items[1].removed); assert.equal(t.document.activeElement, t.items[1].close);
  });
  test("duplicate includes combine into one stack and deduplicate", () => {
    const t = setup([{ text: "Saved" }], [{ text: "Saved" }, { level: "error", text: "Saved" }]);
    assert(t.regions[1].removed); assert.equal(t.items.filter(item => !item.removed).length, 2);
  });
  test("reinitialization does not add timers", () => {
    const t = setup([{}]); assert.equal(t.timers.size, 1); t.run(); assert.equal(t.timers.size, 1);
    assert.equal(t.items[0].close.hidden, false); assert(t.regions[0].classes.has("is-enhanced"));
  });
  test("deduplication retains the persistent copy", () => {
    const t = setup([{}], [{ persistent: true }]);
    assert.equal(t.items.filter(item => !item.removed).length, 1);
    t.tick(20000); assert(!t.items[0].removed); assert.equal(t.timers.size, 0);
  });
  test("no messages is a no-op", () => {
    vm.runInNewContext(source, { document: { querySelectorAll: () => [] } });
  });
  return `PASS: ${passed} notification behavior scenarios (DOM/clock mocks).`;
}

console.log(verifyToasts());
