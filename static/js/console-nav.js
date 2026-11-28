/* OPS-01: console menu search and collapsed-rail accessibility. Presentation only: no requests.
   Search matches each page's English and Swahili names (data-search) and only ever lists links the
   server rendered for this staff member, so it can never reveal a page they may not open. */
(function () {
  "use strict";
  var menu = document.getElementById("console-menu");
  var input = document.querySelector("[data-console-search]");
  var empty = document.querySelector("[data-console-search-empty]");
  if (!menu) { return; }
  var sections = Array.prototype.slice.call(menu.querySelectorAll("[data-console-section]"));
  var details = Array.prototype.slice.call(menu.querySelectorAll(".console-nav__details"));
  var initiallyOpen = details.map(function (d) { return d.open; });

  function normalise(text) {
    return (text || "").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "").trim();
  }

  function filter() {
    var query = normalise(input.value);
    var words = query ? query.split(/\s+/) : [];
    var matches = 0;
    sections.forEach(function (section) {
      var links = Array.prototype.slice.call(section.querySelectorAll("[data-search]"));
      var hit = false;
      links.forEach(function (link) {
        var haystack = normalise(link.getAttribute("data-search"));
        var found = words.every(function (word) { return haystack.indexOf(word) !== -1; });  // Every word, any order.
        var row = link.closest("li") === section ? null : link.closest("li");
        if (row) { row.hidden = !found; }
        hit = hit || found;
      });
      section.hidden = !hit;
      var box = section.querySelector(".console-nav__details");
      if (box && query) { box.open = hit; }
      if (hit) { matches += 1; }
    });
    if (!query) { details.forEach(function (d, i) { d.open = initiallyOpen[i]; }); }
    Array.prototype.forEach.call(menu.querySelectorAll(".console-nav__group"), function (heading) {
      var list = heading.nextElementSibling;
      heading.hidden = Boolean(list && list.querySelectorAll("[data-console-section]:not([hidden])").length === 0
                               && list.querySelector("[data-console-section]"));
    });
    if (empty) { empty.hidden = matches > 0; }
  }

  if (input) {
    input.addEventListener("input", filter);
    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        var first = menu.querySelector("[data-console-section]:not([hidden]) li:not([hidden]) > [data-search], " +
                                       "[data-console-section]:not([hidden]) > [data-search]");
        if (first) { event.preventDefault(); window.location.assign(first.getAttribute("href")); }
      }
      if (event.key === "Escape") { input.value = ""; filter(); }
    });
  }

  // Collapsed desktop rail: the section icon links replace the expandable sections, so they become
  // focusable and announced only then (html.sidebar-collapsed is set by member-nav.js).
  var rail = Array.prototype.slice.call(menu.querySelectorAll(".console-nav__rail-link"));
  var wide = window.matchMedia("(min-width: 64rem)");
  function syncRail() {
    var collapsed = wide.matches && document.documentElement.classList.contains("sidebar-collapsed");
    rail.forEach(function (link) {
      if (collapsed) { link.removeAttribute("tabindex"); link.removeAttribute("aria-hidden"); }
      else { link.setAttribute("tabindex", "-1"); link.setAttribute("aria-hidden", "true"); }
    });
  }
  if (window.MutationObserver) {
    new window.MutationObserver(syncRail).observe(document.documentElement, {attributes: true, attributeFilter: ["class"]});
  }
  if (wide.addEventListener) { wide.addEventListener("change", syncRail); }
  syncRail();
}());
