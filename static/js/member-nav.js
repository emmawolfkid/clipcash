/* Shared navigation. No requests, notification mutations or business-state changes. */
(function () {
  "use strict";
  var siteToggle = document.querySelector(".site-menu-toggle");
  var sidebarToggle = document.querySelector(".app-nav__toggle");
  var siteMenu = document.getElementById("site-menu");
  var sidebar = document.querySelector(".app-sidebar");
  // UI-DASH-01: the member portal sidebar names its own drawer breakpoint and opts in to
  // desktop collapse. Console shells keep the original 52rem drawer and no collapse.
  var narrow = window.matchMedia((sidebar && sidebar.getAttribute("data-drawer-query")) || "(max-width: 52rem)");
  var collapsible = Boolean(sidebar && sidebar.getAttribute("data-collapsible") !== null);
  var STORAGE_KEY = "clipcash.sidebar";
  var sidebarMenu = sidebar && sidebar.querySelector(".app-nav");
  var overlay = document.querySelector("[data-nav-overlay]");
  var panel = sidebar || siteMenu;
  var toggle = siteToggle || sidebarToggle;
  var closeButton = panel && panel.querySelector("[data-nav-close]");
  var profile = document.querySelector(".profile-menu");
  var isolated = [];
  var open = false;

  function closeProfile(restore) {
    if (!profile || !profile.open) { return; }
    profile.open = false;
    if (restore) { profile.querySelector("summary").focus(); }
  }
  if (profile) {
    document.addEventListener("click", function (event) {
      if (!profile.contains(event.target)) { closeProfile(false); }
    });
    profile.addEventListener("focusout", function () {
      window.setTimeout(function () {
        if (!profile.contains(document.activeElement)) { closeProfile(false); }
      }, 0);
    });
  }

  // UI-DASH-01: the portal top bar shows a bare select like the reference; choosing a language
  // submits the same CSRF-protected set_language form. Without JS the "Change" button remains.
  var languageForm = document.querySelector(".portal-header .language-form");
  var languageSelect = languageForm && languageForm.querySelector("select");
  if (languageSelect) {
    document.documentElement.classList.add("js-language-auto");
    languageSelect.addEventListener("change", function () { languageForm.submit(); });
  }

  if (!panel || !toggle) { return; }
  document.documentElement.classList.add("nav-enhanced");
  if (sidebar) { document.documentElement.classList.add("has-sidebar"); }
  if (sidebarToggle && siteToggle) { sidebarToggle.hidden = true; }

  // UI-06B: with a sidebar, the one Menu button controls both menus. Header-menu links and
  // forms the sidebar already offers are marked so each destination appears once; the rest
  // (e.g. the member dashboard and sign-out on console pages) join the sidebar drawer on
  // narrow screens and return to the header on wide ones.
  var shared = siteMenu && sidebarMenu;
  var siteHome = siteMenu && { parent: siteMenu.parentNode, next: siteMenu.nextSibling };
  function markDuplicates() {
    Array.prototype.forEach.call(siteMenu.querySelectorAll("a[href]"), function (link) {
      if (sidebar.querySelector('a[href="' + link.getAttribute("href") + '"]')) { link.classList.add("site-nav__duplicate"); }
    });
    Array.prototype.forEach.call(siteMenu.querySelectorAll("form[action]"), function (form) {
      if (sidebar.querySelector('form[action="' + form.getAttribute("action") + '"]')) { form.classList.add("site-nav__duplicate"); }
    });
  }
  function placeSiteMenu() {
    if (!shared) { return; }
    if (narrow.matches) {
      if (siteMenu.parentNode !== sidebar) { sidebar.appendChild(siteMenu); }
    } else if (siteMenu.parentNode !== siteHome.parent) {
      siteHome.parent.insertBefore(siteMenu, siteHome.next);
    }
  }
  if (shared) {
    markDuplicates();
    toggle.setAttribute("aria-controls", siteMenu.id + " " + sidebarMenu.id);
  } else {
    toggle.setAttribute("aria-controls", sidebarMenu ? sidebarMenu.id : panel.id);
  }

  function focusable() {
    return Array.prototype.slice.call(panel.querySelectorAll('a[href], button:not([disabled]), select:not([disabled]), input:not([disabled]):not([type="hidden"]), summary, [tabindex="0"]'))
      .filter(function (item) { return !item.hidden && item.getClientRects().length > 0 && !item.closest("[hidden], [inert]"); });
  }
  function release() {
    isolated.forEach(function (item) { item.element.inert = item.previous; });
    isolated = [];
  }
  function isolate(parent) {
    Array.prototype.forEach.call(parent.children, function (child) {
      if (child === panel || child === overlay) { return; }
      if (child.contains(panel)) { isolate(child); }
      else { isolated.push({ element: child, previous: child.inert }); child.inert = true; }
    });
  }
  function closeDrawer(restore) {
    open = false;
    release();
    document.documentElement.classList.remove("nav-open");
    if (overlay) { overlay.hidden = true; }
    panel.removeAttribute("role");
    panel.removeAttribute("aria-modal");
    if (restore) { toggle.focus(); }
    toggle.setAttribute("aria-expanded", "false");
    panel.classList.toggle("is-collapsed", narrow.matches);
  }
  function openDrawer() {
    closeProfile(false);
    open = true;
    panel.classList.remove("is-collapsed");
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "true");
    panel.setAttribute("aria-label", sidebarMenu ? sidebarMenu.getAttribute("aria-label") : siteMenu.getAttribute("aria-label"));
    toggle.setAttribute("aria-expanded", "true");
    document.documentElement.classList.add("nav-open");
    if (overlay) { overlay.hidden = false; }
    isolate(document.body);
    var current = panel.querySelector('[aria-current="page"]');
    (current || focusable()[0] || panel).focus();
    if (current && current.scrollIntoView) { current.scrollIntoView({ block: "nearest" }); }
  }
  function isCollapsed() { return document.documentElement.classList.contains("sidebar-collapsed"); }
  function labelToggle() {
    if (!collapsible) { return; }
    var key = narrow.matches ? "data-label-open" : (isCollapsed() ? "data-label-expand" : "data-label-collapse");
    var label = toggle.getAttribute(key);
    if (label) { toggle.setAttribute("aria-label", label); }
    if (!narrow.matches) { toggle.setAttribute("aria-expanded", isCollapsed() ? "false" : "true"); }
  }
  function setCollapsed(collapsed) {
    document.documentElement.classList.toggle("sidebar-collapsed", collapsed);
    try { window.localStorage.setItem(STORAGE_KEY, collapsed ? "collapsed" : "expanded"); } catch (error) { /* Optional. */ }
    labelToggle();
  }
  function apply() {
    var hadFocus = panel.contains(document.activeElement);
    // Release the old modal before changing breakpoint; never leave the document inert.
    closeDrawer(false);
    placeSiteMenu();
    toggle.hidden = !narrow.matches && !collapsible;
    labelToggle();
    if (closeButton) { closeButton.hidden = !narrow.matches; }
    if (narrow.matches && hadFocus) { toggle.focus(); }
    if (!narrow.matches && !collapsible && (document.activeElement === toggle || document.activeElement === closeButton)) {
      (focusable()[0] || panel).focus();
    }
  }
  toggle.addEventListener("click", function () {
    if (!narrow.matches) {
      if (collapsible) { setCollapsed(!isCollapsed()); }
      return;
    }
    if (open) { closeDrawer(true); } else { openDrawer(); }
  });
  if (closeButton) { closeButton.addEventListener("click", function () { closeDrawer(true); }); }
  if (overlay) { overlay.addEventListener("click", function () { closeDrawer(true); }); }
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      if (profile && profile.open) { event.preventDefault(); closeProfile(true); return; }
      if (open) { event.preventDefault(); closeDrawer(true); return; }
    }
    if (event.key === "Tab" && open) {
      var items = focusable();
      var first = items[0];
      var last = items[items.length - 1];
      if (!first) { event.preventDefault(); panel.focus(); return; }
      if (event.shiftKey && (document.activeElement === first || !panel.contains(document.activeElement))) {
        event.preventDefault(); last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || !panel.contains(document.activeElement))) {
        event.preventDefault(); first.focus();
      }
    }
  });
  panel.addEventListener("click", function (event) {
    if (open && event.target.closest("a[href]")) { closeDrawer(true); }
  });
  window.addEventListener("pagehide", function () { closeDrawer(false); closeProfile(false); });
  window.addEventListener("pageshow", apply);
  if (narrow.addEventListener) { narrow.addEventListener("change", apply); }
  else if (narrow.addListener) { narrow.addListener(apply); }
  apply();

  var tabs = document.querySelector(".tabs");
  var currentTab = tabs && tabs.querySelector(".is-current");
  if (currentTab && tabs.scrollWidth > tabs.clientWidth) { tabs.scrollLeft = currentTab.offsetLeft - tabs.offsetLeft; }
  var currentLink = sidebar && sidebar.querySelector(".is-current");
  if (currentLink && sidebar.scrollHeight > sidebar.clientHeight) {
    sidebar.scrollTop += currentLink.getBoundingClientRect().top - sidebar.getBoundingClientRect().top - sidebar.clientHeight / 2;
  }
}());
