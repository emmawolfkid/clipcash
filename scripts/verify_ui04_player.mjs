/* DOM mocks for message/feedback contracts. This is not browser verification. */
import assert from "node:assert/strict";

const original = { document: globalThis.document, window: globalThis.window, fetch: globalThis.fetch };
const handlers = {};
const messages = {};
const sent = [];
const posted = [];
const replies = [];
const bar = { value: 0 };
const status = { textContent: "" };
const panel = {
  dataset: { eventsUrl: "/events/", heartbeat: "10", ended: "ended", limited: "limited",
    enough: "enough", counted: "counted", unconfirmed: "unconfirmed" },
  querySelector: () => ({ value: "csrf-token" }),
};
const video = { currentTime: 2.5, paused: false, ended: false,
  addEventListener: (event, callback) => { handlers[event] = callback; } };
const frame = { src: "https://iframe.mediadelivery.net/embed/1/2",
  contentWindow: { postMessage: (...args) => posted.push(args) },
  addEventListener: (event, callback) => { messages[event] = callback; } };
let interval;
let reloaded = false;
const flush = () => new Promise(resolve => setTimeout(resolve, 0));
try {
  globalThis.document = { getElementById: id => ({ "watch-panel": panel, "watch-progress": bar,
    "watch-status": status, "watch-video": video, "watch-frame": frame })[id] };
  globalThis.window = {
    addEventListener: (event, callback) => { messages[event] = callback; },
    setInterval: callback => { interval = callback; },
    setTimeout: callback => callback(), location: { reload: () => { reloaded = true; } },
  };
  globalThis.fetch = async (url, options) => {
    sent.push({ url, ...options });
    const reply = replies.shift();
    if (reply instanceof Error) { throw reply; }
    return { status: reply.status ?? 200, json: async () => reply.data };
  };
  await import("../static/js/video-watch.js");
  messages.load();
  assert.equal(posted.length, 5);
  assert(posted.every(([, origin]) => origin === "https://iframe.mediadelivery.net"));
  const event = { origin: "https://iframe.mediadelivery.net", source: frame.contentWindow,
    data: { context: "player.js", event: "play" } };
  messages.message({ ...event, origin: "https://untrusted.example" });
  messages.message({ ...event, source: {} });
  assert.equal(sent.length, 0, "Untrusted origins/sources cannot report playback");

  replies.push({ data: { accepted: true, progress: 25, credited_seconds: 3, required_seconds: 12 } });
  handlers.play(); await flush();
  assert.equal(status.textContent, "counted 3 / 12");
  assert.equal(bar.value, 25);
  assert.deepEqual(JSON.parse(sent[0].body), { seq: 1, position_ms: 2500, state: "playing" });
  assert.equal(sent[0].credentials, "same-origin");
  assert.equal(sent[0].headers["X-CSRFToken"], "csrf-token");

  replies.push({ status: 429, data: {} }); handlers.pause(); await flush();
  assert.equal(status.textContent, "limited"); assert.equal(bar.value, 25);
  replies.push({ status: 403, data: { accepted: false } }); handlers.pause(); await flush();
  assert.equal(status.textContent, "unconfirmed");
  replies.push(new Error("offline")); handlers.pause(); await flush();
  assert.equal(status.textContent, "unconfirmed");
  replies.push({ data: { accepted: true, progress: 100, credited_seconds: 12, required_seconds: 12 } });
  handlers.pause(); await flush(); assert.equal(status.textContent, "enough");
  replies.push({ status: 409, data: { open: false } }); handlers.ended(); await flush();
  assert.equal(status.textContent, "ended"); assert(reloaded);
  const count = sent.length; handlers.play(); interval(); await flush();
  assert.equal(sent.length, count, "Closed sessions stop event reports");

  const mediaHandlers = {};
  const feedback = { hidden: true, textContent: "", classList: { contains: () => true } };
  const media = { tagName: "VIDEO", error: {}, addEventListener: (name, fn) => { mediaHandlers[name] = fn; } };
  globalThis.document = { querySelectorAll: () => [{ querySelector: () => media,
    nextElementSibling: feedback, dataset: { playbackError: "Unavailable; time may not count" } }] };
  await import("../static/js/media-feedback.js");
  assert.equal(feedback.hidden, false); assert.match(feedback.textContent, /Unavailable/);
  mediaHandlers.playing(); assert.equal(feedback.hidden, true);
  mediaHandlers.error(); assert.equal(feedback.hidden, false);
  console.log("PASS: 9 player/feedback scenarios (DOM mocks; no visual browser claims).");
} finally {
  Object.assign(globalThis, original);
}
