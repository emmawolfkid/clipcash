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
    function update() {
      if (region.scrollWidth > region.clientWidth) { region.tabIndex = 0; }
      else { region.removeAttribute("tabindex"); }
    }
    if (window.ResizeObserver) { new ResizeObserver(update).observe(region); }
    else { window.addEventListener("resize", update); }
    update();
  });
  var summary = document.querySelector(".error-summary");
  if (summary) { summary.focus(); }
}());
