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
