/* UI-DASH-01: restore the member's desktop sidebar preference before first paint (no flash).
   Presentation only. The class has an effect on wide screens only (member-portal.css). */
(function () {
  "use strict";
  try {
    if (window.localStorage.getItem("clipcash.sidebar") === "collapsed") {
      document.documentElement.classList.add("sidebar-collapsed");
    }
  } catch (error) { /* Storage unavailable: keep the expanded default. */ }
}());
