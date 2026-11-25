/* UX-02: choosing "Other provider" opens the advanced provider-code field. Convenience only:
   the field is always reachable inside its <details>, and the server validates every value. */
(function () {
  "use strict";
  var form = document.querySelector("[data-method-form]");
  if (!form) { return; }
  var preset = form.querySelector("select[name='preset']");
  var advanced = form.querySelector("[data-advanced]");
  if (!preset || !advanced) { return; }
  preset.addEventListener("change", function () {
    if (preset.value === "OTHER") {
      advanced.open = true;
      var code = advanced.querySelector("input[name='method']");
      if (code) { code.focus(); }
    }
  });
}());
