/* Presentation only: never infers verification from player load or playback. */
(function () {
  "use strict";
  document.querySelectorAll(".player[data-playback-error]").forEach(function (player) {
    var media = player.querySelector("video, iframe");
    var feedback = player.nextElementSibling;
    if (!media || !feedback || !feedback.classList.contains("media-feedback")) { return; }
    function failed() {
      feedback.textContent = player.dataset.playbackError;
      feedback.hidden = false;
    }
    media.addEventListener("error", failed);
    if (media.tagName === "VIDEO") {
      if (media.error) { failed(); }
      function loading() {
        if (!player.dataset.playbackLoading || media.error) { return; }
        feedback.textContent = player.dataset.playbackLoading;
        feedback.hidden = false;
      }
      media.addEventListener("loadstart", loading);
      media.addEventListener("waiting", loading);
      media.addEventListener("loadeddata", function () { feedback.hidden = true; });
      media.addEventListener("playing", function () { feedback.hidden = true; });
    }
    // Cross-origin iframe failures may not emit error; session help stays visible.
  });
}());
