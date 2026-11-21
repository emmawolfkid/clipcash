/* Presentation only: preserve table semantics and provide keyboard scrolling. */
(function () {
  "use strict";
  Array.prototype.forEach.call(document.querySelectorAll("table.table"), function (table, index) {
    if (table.parentElement.classList.contains("table-scroll")) { return; }
    var region = document.createElement("div");
    region.className = "table-scroll";
    region.setAttribute("role", "region");
    var caption = table.querySelector("caption");
    var label = caption || table.querySelector("thead");
    if (label) {
      if (!label.id) { label.id = "table-label-" + index; }
      region.setAttribute("aria-labelledby", label.id);
    } else {
      var heading = document.querySelector("h1");
      if (heading) {
        if (!heading.id) { heading.id = "page-heading"; }
        region.setAttribute("aria-labelledby", heading.id);
      }
    }
    table.parentNode.insertBefore(region, table);
    region.appendChild(table);
    // Visual cue only: a screen reader already reaches every cell.
    var hint = document.createElement("p");
    hint.className = "table-scroll__hint";
    hint.setAttribute("aria-hidden", "true");
    hint.textContent = document.body.getAttribute("data-table-scroll-hint") || "";
    region.parentNode.insertBefore(hint, region);
    function update() {
      var scrolls = region.scrollWidth > region.clientWidth;
      hint.hidden = !scrolls;
      if (scrolls) { region.tabIndex = 0; }
      else { region.removeAttribute("tabindex"); }
    }
    if (window.ResizeObserver) { new ResizeObserver(update).observe(region); }
    else { window.addEventListener("resize", update); }
    update();
    // Browsers leave a partly clipped control where it is when it takes focus.
    region.addEventListener("focusin", function (event) {
      if (event.target !== region && event.target.matches(":focus-visible")) {
        event.target.scrollIntoView({ block: "nearest", inline: "nearest" });
      }
    });
  });
  var summary = document.querySelector(".error-summary");
  if (summary) { summary.focus(); }
}());

/* Progressive action feedback. Successes expire unless explicitly persistent; other levels stay available. */
(function () {
  "use strict";
  Array.prototype.forEach.call(document.querySelectorAll("[data-feedback]"), function (region) {
    var list = region.querySelector(".messages");
    var announcer = region.querySelector("[data-feedback-announcer]");
    var lastTaskFocus = null;
    document.addEventListener("focusin", function (event) {
      if (!region.contains(event.target)) { lastTaskFocus = event.target; }
    });
    var cards = Array.prototype.slice.call(list.children);
    // Populate an already-present live region after page load; never focus it.
    window.setTimeout(function () {
      announcer.textContent = cards.map(function (card) {
        return card.querySelector(".messages__text").textContent.trim();
      }).join(" ");
    }, 100);
    cards.forEach(function (card) {
      var close = card.querySelector("[data-feedback-close]");
      var timer;
      var hovered = false;
      var remaining = 8000;
      var started = null;
      var eligible = card.hasAttribute("data-feedback-auto") &&
        card.classList.contains("messages__item--success") &&
        !card.querySelector("a, input, select, textarea, button:not([data-feedback-close])");
      function pause() {
        window.clearTimeout(timer);
        if (started !== null) { remaining = Math.max(0, remaining - (Date.now() - started)); started = null; }
      }
      function dismiss() {
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
      card.addEventListener("pointerenter", function () { hovered = true; pause(); });
      card.addEventListener("pointerleave", function () { hovered = false; resume(); });
      card.addEventListener("focusin", pause);
      card.addEventListener("focusout", function () { window.setTimeout(resume, 0); });
      document.addEventListener("visibilitychange", resume);
      // History restoration must not replay already-consumed server feedback.
      window.addEventListener("pagehide", dismiss);
      resume();
    });
  });
}());
