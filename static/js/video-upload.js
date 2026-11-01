/*
 * Direct-to-Bunny video upload (TUS 1.0.0) for the administration console.
 * The server issues a short-lived signature; the Bunny API key never reaches
 * the browser. No dependencies.
 */
(function () {
  "use strict";

  var CHUNK = 8 * 1024 * 1024;
  var panel = document.getElementById("upload-panel");
  if (!panel) { return; }

  var input = document.getElementById("upload-file");
  var progress = document.getElementById("upload-progress");
  var statusEl = document.getElementById("upload-status");
  var csrf = panel.querySelector("input[name=csrfmiddlewaretoken]").value;
  var types = (panel.dataset.types || "").split(",").filter(Boolean);
  var maxBytes = Number(panel.dataset.maxMb || 0) * 1024 * 1024;

  function say(text) { statusEl.textContent = text; }

  function b64(text) {
    return btoa(unescape(encodeURIComponent(text)));
  }

  function postJson(url, body) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
      body: JSON.stringify(body || {})
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok) { throw new Error(data.error || "Request failed (" + response.status + ")."); }
        return data;
      });
    });
  }

  function xhr(method, url, headers, body, onProgress) {
    return new Promise(function (resolve, reject) {
      var request = new XMLHttpRequest();
      request.open(method, url);
      Object.keys(headers).forEach(function (name) { request.setRequestHeader(name, headers[name]); });
      if (onProgress && request.upload) { request.upload.onprogress = onProgress; }
      request.onload = function () {
        if (request.status >= 200 && request.status < 300) { resolve(request); }
        else { reject(new Error("Upload server responded " + request.status + ".")); }
      };
      request.onerror = function () { reject(new Error("Network error during upload.")); };
      request.send(body);
    });
  }

  function upload(file, auth) {
    var metadata = Object.keys(auth.metadata || {}).map(function (key) {
      return key + " " + b64(String(auth.metadata[key]));
    }).join(",");
    var base = Object.assign({ "Tus-Resumable": "1.0.0" }, auth.headers);
    var create = Object.assign({}, base, { "Upload-Length": String(file.size), "Upload-Metadata": metadata });
    return xhr("POST", auth.endpoint, create, null).then(function (request) {
      var location = request.getResponseHeader("Location");
      if (!location) { throw new Error("Upload server did not return an upload address."); }
      var target = new URL(location, auth.endpoint).toString();
      var offset = 0;
      function next() {
        if (offset >= file.size) { return Promise.resolve(); }
        var end = Math.min(offset + CHUNK, file.size);
        var headers = Object.assign({}, base, {
          "Upload-Offset": String(offset),
          "Content-Type": "application/offset+octet-stream"
        });
        return xhr("PATCH", target, headers, file.slice(offset, end), function (event) {
          progress.value = Math.round(((offset + event.loaded) / file.size) * 100);
        }).then(function (request) {
          var reported = Number(request.getResponseHeader("Upload-Offset"));
          offset = Number.isFinite(reported) && reported > offset ? reported : end;
          progress.value = Math.round((offset / file.size) * 100);
          return next();
        });
      }
      return next();
    });
  }

  input.addEventListener("change", function () {
    var file = input.files && input.files[0];
    if (!file) { return; }
    if (types.length && types.indexOf(file.type) === -1) { say("This file type is not accepted."); return; }
    if (maxBytes && file.size > maxBytes) { say("This file is larger than the allowed size."); return; }

    input.disabled = true;
    progress.hidden = false;
    progress.value = 0;
    say("Preparing upload…");
    var started = false;

    postJson(panel.dataset.authorizeUrl, { name: file.name, size: file.size, type: file.type })
      .then(function (auth) {
        started = true;
        say("Uploading…");
        return upload(file, auth);
      })
      .then(function () { return postJson(panel.dataset.finishedUrl); })
      .then(function () {
        say("Upload complete. Bunny Stream is processing the video; this page will refresh.");
        window.setTimeout(function () { window.location.reload(); }, 2500);
      })
      .catch(function (error) {
        say(error.message + " You can choose the file again to retry.");
        input.disabled = false;
        if (started) {
          postJson(panel.dataset.failedUrl, { reason: "network" }).catch(function () {});
        }
      });
  });
}());
