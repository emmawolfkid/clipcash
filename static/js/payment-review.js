/* UX-01: the confirm button stays unavailable until the transaction ID is typed and every
   confirmation is ticked. Convenience only: the server enforces the same rules on submit. */
(function () {
  "use strict";
  var form = document.querySelector("[data-review-form]");
  if (!form) { return; }
  var button = form.querySelector("[data-review-submit]");
  var hint = form.querySelector("[data-review-hint]");
  var reference = form.querySelector("input[name='verified_reference']");
  var checks = form.querySelectorAll(".payment-review__checks input[type='checkbox']");
  if (!button || !reference || !checks.length) { return; }
  var readyText = hint ? hint.textContent : "";
  var waitText = button.getAttribute("data-wait-text") || "";
  if (hint && !hint.id) { hint.id = "review-hint"; }
  if (hint) { button.setAttribute("aria-describedby", hint.id); }

  function update() {
    var ready = reference.value.trim() !== "" &&
      Array.prototype.every.call(checks, function (box) { return box.checked; });
    button.disabled = !ready;
    if (hint && waitText) { hint.textContent = ready ? readyText : waitText; }
  }
  reference.addEventListener("input", update);
  Array.prototype.forEach.call(checks, function (box) { box.addEventListener("change", update); });
  // One submission per click: a second click while the page loads does nothing.
  form.addEventListener("submit", function () {
    window.setTimeout(function () { button.disabled = true; }, 0);
  });
  update();
}());
