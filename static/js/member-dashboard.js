/* UI-DASH-01: member dashboard presentation only — balance privacy toggle and featured-video
   carousel. No requests, no business state; video links still go through the server-side
   playback authorization on the detail page. */
(function () {
  "use strict";
  var BALANCE_KEY = "clipcash.balanceHidden";

  // Balance visibility: hides the figure on screen (and from assistive tech) when asked.
  var toggle = document.querySelector("[data-balance-toggle]");
  var value = document.querySelector("[data-balance-value]");
  var mask = document.querySelector("[data-balance-mask]");
  if (toggle && value && mask) {
    var setHidden = function (hidden, persist) {
      value.hidden = hidden;
      mask.hidden = !hidden;
      toggle.setAttribute("aria-pressed", hidden ? "true" : "false");
      toggle.setAttribute("aria-label", toggle.getAttribute(hidden ? "data-label-show" : "data-label-hide") || "");
      if (persist) {
        try { window.localStorage.setItem(BALANCE_KEY, hidden ? "1" : "0"); } catch (error) { /* Optional. */ }
      }
    };
    var stored = false;
    try { stored = window.localStorage.getItem(BALANCE_KEY) === "1"; } catch (error) { stored = false; }
    toggle.hidden = false;
    setHidden(stored, false);
    toggle.addEventListener("click", function () { setHidden(!value.hidden, true); });
  }

  // Carousel: native scroll-snap track; dots scroll to a slide and follow manual swipes.
  Array.prototype.forEach.call(document.querySelectorAll("[data-carousel]"), function (carousel) {
    var track = carousel.querySelector("[data-carousel-track]");
    var dotsGroup = carousel.querySelector("[data-carousel-dots]");
    if (!track || !dotsGroup) { return; }
    var slides = Array.prototype.slice.call(track.querySelectorAll("[data-carousel-slide]"));
    var dots = Array.prototype.slice.call(dotsGroup.querySelectorAll("[data-carousel-dot]"));
    var current = 0;
    dotsGroup.hidden = false;

    function mark(index) {
      current = index;
      dots.forEach(function (dot, i) {
        if (i === index) { dot.setAttribute("aria-current", "true"); }
        else { dot.removeAttribute("aria-current"); }
      });
      // Off-screen slides leave the tab order so keyboard users land on the visible one.
      slides.forEach(function (slide, i) { slide.inert = i !== index; });
    }
    function go(index) {
      var target = slides[index];
      if (!target) { return; }
      // The track is the slides' offsetParent (position: relative), so offsetLeft is track-relative.
      track.scrollTo({ left: target.offsetLeft, behavior: "smooth" });
      mark(index);
    }
    dots.forEach(function (dot, index) {
      dot.addEventListener("click", function () { go(index); });
      dot.addEventListener("keydown", function (event) {
        var next = null;
        if (event.key === "ArrowRight") { next = (index + 1) % dots.length; }
        if (event.key === "ArrowLeft") { next = (index - 1 + dots.length) % dots.length; }
        if (next === null) { return; }
        event.preventDefault();
        go(next);
        dots[next].focus();
      });
    });
    var pending = null;
    track.addEventListener("scroll", function () {
      window.clearTimeout(pending);
      pending = window.setTimeout(function () {
        var width = track.clientWidth || 1;
        var index = Math.round(track.scrollLeft / width);
        if (index !== current && slides[index]) { mark(index); }
      }, 80);
    }, { passive: true });
    mark(0);
  });
}());
