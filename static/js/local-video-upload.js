/* OPS-01: local video uploads. Shows upload progress and records the duration the browser measures
   (used only when the MP4 header has none). Without JavaScript the form still posts normally. */
(function () {
  "use strict";
  Array.prototype.forEach.call(document.querySelectorAll("[data-local-upload-form]"), function (box) {
    var form = box.querySelector("form");
    var file = box.querySelector("[data-local-video], input[name=media]");
    var duration = box.querySelector("[data-local-duration], input[name=reported_duration]");
    var progress = box.querySelector("[data-local-progress]");
    var status = box.querySelector("[data-local-status]");
    if (!form || !file) { return; }

    file.addEventListener("change", function () {
      if (!duration || !file.files || !file.files[0] || !window.URL) { return; }
      var probe = document.createElement("video");
      var url = URL.createObjectURL(file.files[0]);
      probe.preload = "metadata";
      function record() {
        if (isFinite(probe.duration) && probe.duration > 0) {
          duration.value = String(Math.round(probe.duration));
          URL.revokeObjectURL(url);
          return true;
        }
        return false;
      }
      probe.addEventListener("loadedmetadata", function () {
        // Files saved by some recorders do not state their length; seeking far ahead makes the
        // browser work it out (durationchange then reports the real value).
        if (!record()) { probe.currentTime = 1e9; }
      });
      probe.addEventListener("durationchange", record);
      probe.addEventListener("error", function () { URL.revokeObjectURL(url); });
      probe.src = url;
    });

    if (!window.FormData || !window.XMLHttpRequest || !progress) { return; }
    form.addEventListener("submit", function (event) {
      if (!file.files || !file.files.length || (form.checkValidity && !form.checkValidity())) { return; }
      event.preventDefault();
      var request = new XMLHttpRequest();
      var button = form.querySelector("[type=submit]");
      if (button) { button.disabled = true; }
      progress.hidden = false;
      request.upload.addEventListener("progress", function (e) {
        if (!e.lengthComputable) { return; }
        var percent = Math.round((e.loaded / e.total) * 100);
        progress.value = percent;
        if (status) { status.textContent = (status.getAttribute("data-uploading") || "").replace("{percent}", percent + "%"); }
        if (percent >= 100 && status) { status.textContent = status.getAttribute("data-processing") || ""; }
      });
      request.addEventListener("load", function () {
        var target = null;
        try { target = JSON.parse(request.responseText).redirect; } catch (error) { target = null; }
        if (target) { window.location.assign(target); return; }
        // Validation errors come back as the page itself: show it.
        document.open(); document.write(request.responseText); document.close();
      });
      request.addEventListener("error", function () {
        if (status) { status.textContent = status.getAttribute("data-failed") || ""; }
        if (button) { button.disabled = false; }
      });
      request.open("POST", form.getAttribute("action") || window.location.href);
      request.setRequestHeader("X-Requested-With", "XMLHttpRequest");
      request.send(new FormData(form));
    });
  });
}());
