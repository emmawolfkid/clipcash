/* REF-01: native sharing for the referral link. The button stays hidden unless the browser
   supports navigator.share; copy buttons and the WhatsApp link work everywhere else. */
(function () {
  "use strict";
  if (typeof navigator.share !== "function") { return; }
  Array.prototype.forEach.call(document.querySelectorAll("[data-native-share]"), function (button) {
    button.hidden = false;
    button.addEventListener("click", function () {
      var data = {
        title: button.getAttribute("data-share-title") || "",
        text: button.getAttribute("data-share-text") || "",
        url: button.getAttribute("data-share-url") || ""
      };
      navigator.share(data).catch(function () { /* Cancelled or refused: nothing to do. */ });
    });
  });
}());
