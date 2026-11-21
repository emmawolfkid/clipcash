/* Action feedback: the one controller for backend-confirmed Django messages
   (templates/components/messages.html). It never infers that an action succeeded.
   Successes the server marks data-feedback-auto expire unless they hold a link or control;
   errors, warnings, information and persistent successes stay until dismissed. */
(function () {
  "use strict";
  var regions = Array.prototype.slice.call(document.querySelectorAll("[data-feedback]"));
  if (!regions.length) { return; }
  var region = regions[0];
  if (region.hasAttribute("data-feedback-ready")) { return; }
  region.setAttribute("data-feedback-ready", "");
  var list = region.querySelector(".messages");
  var DELAY = 8000;

  function text(card) { return card.querySelector(".messages__text").textContent.trim(); }
  function level(card) { return card.getAttribute("data-level"); }
  function children(element) { return Array.prototype.slice.call(element.children); }

  // A repeated include must not produce a second stack or repeated announcements; if any copy
  // of a repeated message is persistent, the kept card is persistent too.
  var seen = Object.create(null);
  regions.forEach(function (other, index) {
    children(other.querySelector(".messages")).forEach(function (card) {
      var key = level(card) + "\n" + text(card);
      if (seen[key]) {
        if (!card.hasAttribute("data-feedback-auto")) { seen[key].removeAttribute("data-feedback-auto"); }
        card.remove();
        return;
      }
      seen[key] = card;
      if (index) { list.appendChild(card); }
    });
    if (index) { other.remove(); }
  });

  var cards = children(list);
  // Fill live regions that were already present after load; never move focus to them.
  // Errors are announced urgently (role="alert"); everything else politely (role="status").
  var polite = region.querySelector("[data-feedback-announcer]");
  var urgent = region.querySelector("[data-feedback-urgent]");
  window.setTimeout(function () {
    function joined(errors) {
      return cards.filter(function (card) { return (level(card) === "error") === errors; }).map(text).join(" ");
    }
    if (urgent) { urgent.textContent = joined(true); }
    polite.textContent = urgent ? joined(false) : cards.map(text).join(" ");
  }, 100);

  var lastTaskFocus = null;
  document.addEventListener("focusin", function (event) {
    if (!region.contains(event.target)) { lastTaskFocus = event.target; }
  });

  cards.forEach(function (card) {
    var close = card.querySelector("[data-feedback-close]");
    var timer = null;
    var hovered = false;
    var remaining = DELAY;
    var started = null;
    var eligible = card.hasAttribute("data-feedback-auto") && level(card) === "success" &&
      !card.querySelector("a, input, select, textarea, button:not([data-feedback-close])");
    function pause() {
      window.clearTimeout(timer);
      timer = null;
      if (started !== null) { remaining = Math.max(0, remaining - (Date.now() - started)); started = null; }
    }
    function dismiss() {
      if (!card.isConnected) { return; }
      pause();
      var hadFocus = card.contains(document.activeElement);
      var next = card.nextElementSibling || card.previousElementSibling;
      card.remove();
      if (hadFocus) {
        if (next) { next.querySelector("[data-feedback-close]").focus(); }
        else if (lastTaskFocus && lastTaskFocus.isConnected) { lastTaskFocus.focus(); }
        else {
          // Explicit dismissal only: keep keyboard position by the closed card.
          list.tabIndex = -1;
          list.focus();
        }
      }
    }
    function resume() {
      pause();
      if (eligible && card.isConnected && !hovered && !document.hidden &&
          !card.contains(document.activeElement)) {
        started = Date.now();
        timer = window.setTimeout(dismiss, remaining);
      }
    }
    close.hidden = false;
    close.addEventListener("click", dismiss);
    card.addEventListener("keydown", function (event) {
      if (event.key === "Escape") { event.preventDefault(); dismiss(); }
    });
    card.addEventListener("pointerenter", function () { hovered = true; pause(); });
    card.addEventListener("pointerleave", function () { hovered = false; resume(); });
    card.addEventListener("focusin", pause);
    card.addEventListener("focusout", function () { window.setTimeout(resume, 0); });
    document.addEventListener("visibilitychange", resume);
    // History restoration must not replay already-consumed server feedback.
    window.addEventListener("pagehide", dismiss);
    resume();
  });
}());
