/*
 * Watch-session heartbeats. Reports position and play state only; the server
 * decides what counts (this script cannot mark a view as complete).
 * Supports the demo <video> element and the Bunny player iframe through the
 * Player.js postMessage protocol. No dependencies.
 */
(function () {
  "use strict";

  var panel = document.getElementById("watch-panel");
  if (!panel) { return; }

  var csrf = panel.querySelector("input[name=csrfmiddlewaretoken]").value;
  var eventsUrl = panel.dataset.eventsUrl;
  var heartbeatMs = Math.max(2, Number(panel.dataset.heartbeat || 10)) * 1000;
  var bar = document.getElementById("watch-progress");
  var statusEl = document.getElementById("watch-status");
  var seq = 0;
  var lastSent = 0;
  var stopped = false;
  var player = { seconds: 0, playing: false };
  var PLAYER_ORIGINS = ["https://player.mediadelivery.net", "https://iframe.mediadelivery.net"];

  function say(text) { if (statusEl) { statusEl.textContent = text; } }

  // UI-DASH-03: the final "ended" report closes the session on the server. It is retried with the
  // same sequence number (the server treats a repeated seq as a duplicate, and a closed session
  // answers 409), so a retry can never count twice. If every retry fails, the idempotent finish
  // endpoint asks the server to decide the outcome; the server, not this script, decides it.
  var RETRY_DELAYS_MS = [1200, 2500, 5000, 10000];
  var ending = false;

  function finishSession() {
    var form = panel.querySelector("form[action$='/finish/']");
    var reload = function () { window.location.reload(); };
    if (!form) { reload(); return; }
    var body = new FormData();
    body.append("csrfmiddlewaretoken", csrf);
    fetch(form.getAttribute("action"), {
      method: "POST", credentials: "same-origin", body: body,
      headers: { "Accept": "application/json", "X-CSRFToken": csrf }
    }).then(reload, reload);
  }

  function closed() {
    stopped = true;
    say(panel.dataset.ended);
    window.setTimeout(function () { window.location.reload(); }, 1500);
  }

  function deliver(body, terminal, attempt) {
    var retry = function () {
      if (attempt < RETRY_DELAYS_MS.length) {
        window.setTimeout(function () { deliver(body, terminal, attempt + 1); }, RETRY_DELAYS_MS[attempt]);
      } else {
        stopped = true;
        say(panel.dataset.ended);
        finishSession();
      }
    };
    fetch(eventsUrl, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
      body: body
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (typeof data.progress === "number" && bar) { bar.value = data.progress; }
        if (response.status === 409 || data.open === false) {
          closed();
        } else if (terminal && (response.status === 429 || response.status >= 500)) {
          say(panel.dataset.limited);
          retry();
        } else if (response.status === 429) {
          say(panel.dataset.limited);
        } else if (terminal) {
          // The server answered but kept the session open (e.g. rejected evidence): let the
          // finish endpoint record the outcome rather than leaving the session hanging.
          stopped = true;
          say(panel.dataset.ended);
          finishSession();
        } else if (data.accepted) {
          say(data.progress >= 100 ? panel.dataset.enough :
            panel.dataset.counted + " " + data.credited_seconds + " / " + data.required_seconds);
        } else {
          say(panel.dataset.unconfirmed);
        }
      });
    }).catch(function () {
      say(panel.dataset.unconfirmed);
      if (terminal) { retry(); }
    });
  }

  function send(state, force) {
    if (stopped || ending) { return; }
    var now = Date.now();
    if (!force && now - lastSent < 1500) { return; }
    lastSent = now;
    seq += 1;
    var terminal = state === "ended";
    if (terminal) { ending = true; }
    deliver(JSON.stringify({ seq: seq, position_ms: Math.max(0, Math.floor(player.seconds * 1000)), state: state }),
      terminal, 0);
  }

  // Demo / self-hosted sample: native <video> element.
  var video = document.getElementById("watch-video");
  if (video) {
    var sync = function () { player.seconds = video.currentTime || 0; player.playing = !video.paused && !video.ended; };
    video.addEventListener("play", function () { sync(); send("playing", true); });
    video.addEventListener("pause", function () { sync(); if (!video.ended) { send("paused", true); } });
    video.addEventListener("seeked", function () { sync(); send(player.playing ? "playing" : "paused", true); });
    video.addEventListener("ended", function () { sync(); send("ended", true); });
    video.addEventListener("timeupdate", sync);
  }

  // Bunny Stream player iframe (Player.js protocol).
  var frame = document.getElementById("watch-frame");
  if (frame) {
    var listen = function () {
      ["ready", "play", "pause", "ended", "timeupdate"].forEach(function (name) {
        frame.contentWindow.postMessage(JSON.stringify({
          context: "player.js", version: "0.0.11", method: "addEventListener", value: name, listener: name
        }), new URL(frame.src).origin);
      });
    };
    frame.addEventListener("load", listen);
    window.addEventListener("message", function (event) {
      if (PLAYER_ORIGINS.indexOf(event.origin) === -1 || event.source !== frame.contentWindow) { return; }
      var data = event.data;
      if (typeof data === "string") { try { data = JSON.parse(data); } catch (e) { return; } }
      if (!data || data.context !== "player.js") { return; }
      if (data.event === "ready") { listen(); }
      if (data.event === "timeupdate" && data.value && typeof data.value.seconds === "number") {
        player.seconds = data.value.seconds;
      }
      if (data.event === "play") { player.playing = true; send("playing", true); }
      if (data.event === "pause") { player.playing = false; send("paused", true); }
      if (data.event === "ended") { player.playing = false; send("ended", true); }
    });
  }

  window.setInterval(function () {
    if (player.playing) { send("playing", false); }
  }, heartbeatMs);
}());
