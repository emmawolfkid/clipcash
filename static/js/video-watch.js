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

  function send(state, force) {
    if (stopped) { return; }
    var now = Date.now();
    if (!force && now - lastSent < 1500) { return; }
    lastSent = now;
    seq += 1;
    fetch(eventsUrl, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
      body: JSON.stringify({ seq: seq, position_ms: Math.max(0, Math.floor(player.seconds * 1000)), state: state })
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (typeof data.progress === "number" && bar) { bar.value = data.progress; }
        if (response.status === 409 || data.open === false) {
          stopped = true;
          say(panel.dataset.ended);
          window.setTimeout(function () { window.location.reload(); }, 1500);
        } else if (response.status === 429) {
          say(panel.dataset.limited);
        } else if (data.accepted) {
          say(data.progress >= 100 ? panel.dataset.enough :
            panel.dataset.counted + " " + data.credited_seconds + " / " + data.required_seconds);
        } else {
          say(panel.dataset.unconfirmed);
        }
      });
    }).catch(function () { say(panel.dataset.unconfirmed); });
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
