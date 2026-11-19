/* Enhance backend-confirmed Django messages only; never infer action success. */
(function () {
  "use strict";
  var regions = document.querySelectorAll(".toast-region");
  if (!regions.length) { return; }
  var region = regions[0];
  // A repeated include must not produce a second stack or repeated announcements.
  Array.prototype.forEach.call(regions, function (other) {
    if (other !== region) {
      while (other.firstChild) { region.appendChild(other.firstChild); }
      other.remove();
    }
  });
  var seen = new Map();
  Array.prototype.forEach.call(region.querySelectorAll(".toast"), function (toast) {
    var key = toast.dataset.level + ":" + toast.querySelector(".toast__text").textContent.trim();
    if (seen.has(key)) {
      if (toast.dataset.persistent) { seen.get(key).dataset.persistent = "true"; }
      toast.remove();
      return;
    }
    seen.set(key, toast);
  });
  Array.prototype.forEach.call(region.querySelectorAll(".toast"), function (toast) {
    if (toast.dataset.ready) { return; }
    toast.dataset.ready = "true";
    var close = toast.querySelector(".toast__close");
    close.hidden = false;
    var timer;
    var remaining = 7000;
    var started;
    var hovered = false;
    var focused = false;
    // Links and explicitly persistent successes require a decision from the user.
    var automatic = toast.dataset.level === "success" && !toast.dataset.persistent && !toast.querySelector("a, input, select, textarea");
    function dismiss() {
      window.clearTimeout(timer);
      if (toast.contains(document.activeElement)) {
        var items = Array.prototype.slice.call(region.querySelectorAll(".toast"));
        var next = items[items.indexOf(toast) + 1] || items[items.indexOf(toast) - 1];
        var destination = next ? next.querySelector(".toast__close") : document.getElementById("content");
        if (destination) { destination.focus(); }
      }
      toast.remove();
      if (!region.querySelector(".toast")) { region.remove(); }
    }
    function pause() {
      if (timer) {
        window.clearTimeout(timer);
        timer = null;
        remaining = Math.max(0, remaining - (Date.now() - started));
      }
    }
    function resume() {
      if (!automatic || hovered || focused || document.hidden || timer) { return; }
      started = Date.now();
      timer = window.setTimeout(dismiss, remaining);
    }
    close.addEventListener("click", dismiss);
    toast.addEventListener("keydown", function (event) {
      if (event.key === "Escape") { event.preventDefault(); dismiss(); }
    });
    toast.addEventListener("mouseenter", function () { hovered = true; pause(); });
    toast.addEventListener("mouseleave", function () { hovered = false; resume(); });
    toast.addEventListener("focusin", function () { focused = true; pause(); });
    toast.addEventListener("focusout", function (event) {
      if (!toast.contains(event.relatedTarget)) { focused = false; resume(); }
    });
    document.addEventListener("visibilitychange", function () { if (document.hidden) { pause(); } else { resume(); } });
    resume();
  });
  region.classList.add("is-enhanced");
}());
