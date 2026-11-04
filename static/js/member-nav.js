/*
 * Sidebar menu toggle for small screens (member and administration layouts).
 * Without JavaScript the menu is always visible; with it, narrow screens get a
 * Menu button (aria-expanded / aria-controls). Escape closes the menu.
 */
(function () {
  "use strict";
  var narrow = window.matchMedia("(max-width: 52rem)");
  Array.prototype.forEach.call(document.querySelectorAll(".app-nav__toggle, .site-menu-toggle"), function (toggle) {
    var menu = document.getElementById(toggle.getAttribute("aria-controls"));
    if (!menu) { return; }
    function apply() {
      if (narrow.matches) {
        if (menu.contains(document.activeElement)) { toggle.setAttribute("aria-expanded", "true"); }
        toggle.hidden = false;
        menu.classList.toggle("is-collapsed", toggle.getAttribute("aria-expanded") !== "true");
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
    else if (narrow.addListener) { narrow.addListener(apply); }
    apply();
  });
}());
