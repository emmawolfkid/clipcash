/*
 * Small-screen menu (member, advertiser and administration layouts).
 * Without JavaScript every menu stays visible. With it, narrow screens get one Menu
 * button (aria-expanded / aria-controls), and Escape closes the menu. Pages with both
 * a site menu and a sidebar share the header's button, and links the sidebar already
 * offers are left out of the site menu, so each destination appears once.
 */
(function () {
  "use strict";
  var narrow = window.matchMedia("(max-width: 52rem)");

  function connect(toggle, panels) {
    function isOpen() { return toggle.getAttribute("aria-expanded") === "true"; }
    function apply() {
      if (narrow.matches) {
        panels.forEach(function (panel) {
          if (panel.contains(document.activeElement)) { toggle.setAttribute("aria-expanded", "true"); }
        });
        toggle.hidden = false;
        panels.forEach(function (panel) { panel.classList.toggle("is-collapsed", !isOpen()); });
      } else {
        toggle.hidden = true;
        panels.forEach(function (panel) { panel.classList.remove("is-collapsed"); });
      }
    }
    toggle.setAttribute("aria-expanded", "false");
    toggle.addEventListener("click", function () {
      toggle.setAttribute("aria-expanded", isOpen() ? "false" : "true");
      apply();
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && narrow.matches && isOpen()) {
        toggle.setAttribute("aria-expanded", "false");
        apply();
        toggle.focus();
      }
    });
    if (narrow.addEventListener) { narrow.addEventListener("change", apply); }
    else if (narrow.addListener) { narrow.addListener(apply); }
    apply();
  }

  function markDuplicates(siteMenu, sidebarMenu) {
    Array.prototype.forEach.call(siteMenu.querySelectorAll("a[href]"), function (link) {
      if (sidebarMenu.querySelector('a[href="' + link.getAttribute("href") + '"]')) {
        link.classList.add("site-nav__duplicate");
      }
    });
    Array.prototype.forEach.call(siteMenu.querySelectorAll("form[action]"), function (form) {
      if (sidebarMenu.querySelector('form[action="' + form.getAttribute("action") + '"]')) {
        form.classList.add("site-nav__duplicate");
      }
    });
  }

  var tabs = document.querySelector(".tabs");
  var currentTab = tabs && tabs.querySelector(".is-current");
  if (currentTab && tabs.scrollWidth > tabs.clientWidth) {
    tabs.scrollLeft = currentTab.offsetLeft - tabs.offsetLeft;
  }

  var siteToggle = document.querySelector(".site-menu-toggle");
  var sidebarToggle = document.querySelector(".app-nav__toggle");
  var siteMenu = siteToggle && document.getElementById(siteToggle.getAttribute("aria-controls"));
  var sidebarMenu = sidebarToggle && document.getElementById(sidebarToggle.getAttribute("aria-controls"));

  if (siteMenu && sidebarMenu) {
    sidebarToggle.hidden = true;
    siteToggle.setAttribute("aria-controls", siteMenu.id + " " + sidebarMenu.id);
    markDuplicates(siteMenu, sidebarMenu);
    connect(siteToggle, [siteMenu, sidebarMenu, sidebarMenu.parentElement]);
  } else if (siteMenu) {
    connect(siteToggle, [siteMenu]);
  } else if (sidebarMenu) {
    connect(sidebarToggle, [sidebarMenu]);
  }
}());
