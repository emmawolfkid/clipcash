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
      media.addEventListener("playing", function () { feedback.hidden = true; });
    }
    // Cross-origin iframe failures may not emit error; session help stays visible.
  });
}());
