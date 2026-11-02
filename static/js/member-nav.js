/*
 * Member menu toggle for small screens. Without JavaScript the menu is always
 * visible; with it, narrow screens get a Menu button (aria-expanded).
 */
(function () {
  "use strict";
  var toggle = document.querySelector(".app-nav__toggle");
  var menu = document.getElementById("member-menu");
  if (!toggle || !menu) { return; }
  var narrow = window.matchMedia("(max-width: 52rem)");

  function apply() {
    if (narrow.matches) {
      toggle.hidden = false;
      var open = toggle.getAttribute("aria-expanded") === "true";
      menu.classList.toggle("is-collapsed", !open);
    } else {
      toggle.hidden = true;
      menu.classList.remove("is-collapsed");
    }
  }

  toggle.setAttribute("aria-expanded", "false");
  toggle.addEventListener("click", function () {
    toggle.setAttribute("aria-expanded", toggle.getAttribute("aria-expanded") === "true" ? "false" : "true");
    apply();
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && narrow.matches && toggle.getAttribute("aria-expanded") === "true") {
      toggle.setAttribute("aria-expanded", "false");
      apply();
      toggle.focus();
    }
  });
  if (narrow.addEventListener) { narrow.addEventListener("change", apply); }
  apply();
}());
