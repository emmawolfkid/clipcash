/* UI-DASH-03: automatic completion of a watch session (DOM mocks; not browser verification).
 * Each scenario evaluates a fresh copy of static/js/video-watch.js. */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const original = { document: globalThis.document, window: globalThis.window, fetch: globalThis.fetch };
const flush = async () => { for (let i = 0; i < 20; i += 1) { await new Promise(r => setTimeout(r, 0)); } };
const source = readFileSync(new URL("../static/js/video-watch.js", import.meta.url), "utf8");

async function load(replies) {
  const handlers = {}; const sent = []; const delays = []; const state = { reloaded: false, interval: null };
  const status = { textContent: "" };
  const form = { getAttribute: () => "/watch/sessions/1/finish/" };
  const panel = {
    dataset: { eventsUrl: "/events/", heartbeat: "10", ended: "ended", limited: "limited",
      enough: "enough", counted: "counted", unconfirmed: "unconfirmed" },
    querySelector: sel => (sel.includes("finish") ? form : { value: "csrf-token" }),
  };
  const video = { currentTime: 12, paused: true, ended: true,
    addEventListener: (event, callback) => { handlers[event] = callback; } };
  globalThis.document = { getElementById: id => ({ "watch-panel": panel, "watch-progress": { value: 0 },
    "watch-status": status, "watch-video": video })[id] ?? null };
  globalThis.window = { addEventListener: () => {}, setInterval: cb => { state.interval = cb; },
    setTimeout: (cb, ms) => { delays.push(ms); cb(); }, location: { reload: () => { state.reloaded = true; } } };
  globalThis.fetch = async (url, options) => {
    sent.push({ url, ...options });
    const reply = replies.shift() ?? { status: 200, data: {} };
    if (reply instanceof Error) { throw reply; }
    return { status: reply.status ?? 200, json: async () => reply.data ?? {} };
  };
  new Function(source)();
  return { handlers, sent, delays, state, status };
}

const events = sent => sent.filter(s => s.url === "/events/").map(s => JSON.parse(s.body));
const finishes = sent => sent.filter(s => s.url.endsWith("/finish/"));
let passed = 0;

try {
  // 1. Normal completion: ended accepted and the session closes (open=false) -> reload, no finish.
  let t = await load([{ data: { accepted: true, open: false, progress: 100 } }]);
  t.handlers.ended(); await flush();
  assert.equal(events(t.sent).length, 1); assert.equal(events(t.sent)[0].state, "ended");
  assert.equal(finishes(t.sent).length, 0); assert(t.state.reloaded); passed += 1;

  // 2. 429 then success: retried with the SAME seq.
  t = await load([{ status: 429 }, { status: 409, data: { open: false } }]);
  t.handlers.ended(); await flush();
  const [first, second] = events(t.sent);
  assert.equal(first.seq, second.seq, "retry reuses the sequence number");
  assert.equal(finishes(t.sent).length, 0); assert(t.state.reloaded); passed += 1;

  // 3. Network failure then success: retried with backoff, same seq.
  t = await load([new Error("offline"), { status: 503 }, { data: { accepted: true, open: false } }]);
  t.handlers.ended(); await flush();
  assert.equal(events(t.sent).length, 3);
  assert.equal(new Set(events(t.sent).map(e => e.seq)).size, 1);
  assert.deepEqual(t.delays.slice(0, 2), [1200, 2500]); passed += 1;

  // 4. Retries exhausted: falls back to the finish endpoint (JSON, CSRF) and reloads.
  t = await load([429, 429, 429, 429, 429].map(status => ({ status })));
  t.handlers.ended(); await flush();
  assert.equal(events(t.sent).length, 5);
  const finish = finishes(t.sent);
  assert.equal(finish.length, 1); assert.equal(finish[0].headers.Accept, "application/json");
  assert.equal(finish[0].headers["X-CSRFToken"], "csrf-token"); assert(t.state.reloaded); passed += 1;

  // 5. Server answers but keeps the session open: the finish endpoint records the outcome.
  t = await load([{ status: 200, data: { accepted: false, open: true } }]);
  t.handlers.ended(); await flush();
  assert.equal(finishes(t.sent).length, 1); passed += 1;

  // 6. A second "ended" (e.g. replay) or heartbeat while completing sends nothing new.
  t = await load([{ status: 429 }, { status: 409, data: { open: false } }]);
  t.handlers.ended(); t.handlers.ended(); t.handlers.play(); await flush();
  assert.equal(new Set(events(t.sent).map(e => e.seq)).size, 1);
  assert(events(t.sent).every(e => e.state === "ended")); passed += 1;

  // 7. Non-terminal 429 is NOT retried (rate limiting still applies to heartbeats).
  t = await load([{ status: 429 }]);
  t.handlers.play(); await flush();
  assert.equal(events(t.sent).length, 1); assert.equal(t.status.textContent, "limited"); passed += 1;

  console.log(`PASS: ${passed} completion scenarios (DOM mocks; no visual browser claims).`);
} finally {
  Object.assign(globalThis, original);
}
