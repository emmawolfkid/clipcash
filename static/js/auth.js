/* Authentication presentation only. Never changes field names, values or requests. */
(function () {
  "use strict";
  var shell = document.querySelector(".auth-shell");
  if (!shell) { return; }
  // PRD-03A registration: show only the box for the chosen identifier (email, mobile number or
  // username). Without JavaScript all stay visible; the server uses only the chosen one either way.
  var identifierFields = { email: "id_email", phone: "id_phone_number", username: "id_username" };
  var methodInputs = shell.querySelectorAll('input[name="contact_method"]');
  function syncIdentifierFields() {
    var chosen = shell.querySelector('input[name="contact_method"]:checked');
    Object.keys(identifierFields).forEach(function (method) {
      var input = document.getElementById(identifierFields[method]);
      var group = input && input.closest(".form-field");
      if (!group) { return; }
      group.hidden = Boolean(chosen) && chosen.value !== method;
    });
  }
  if (methodInputs.length) {
    Array.prototype.forEach.call(methodInputs, function (radio) {
      radio.addEventListener("change", syncIdentifierFields);
    });
    syncIdentifierFields();
  }
  Array.prototype.forEach.call(shell.querySelectorAll("[data-password-target]"), function (toggle) {
    var input = document.getElementById(toggle.dataset.passwordTarget);
    if (!input || input.type !== "password") { return; }
    toggle.hidden = false;
    toggle.addEventListener("click", function () {
      var visible = input.type === "password";
      input.type = visible ? "text" : "password";
      toggle.setAttribute("aria-pressed", String(visible));
      toggle.textContent = (visible ? toggle.dataset.hide : toggle.dataset.show) + ": " + toggle.dataset.fieldLabel;
    });
  });
  Array.prototype.forEach.call(shell.querySelectorAll("form"), function (form) {
    form.addEventListener("submit", function (event) {
      if (event.defaultPrevented) { return; }
      if (form.dataset.submitting === "true") { event.preventDefault(); return; }
      form.dataset.submitting = "true";
      form.setAttribute("aria-busy", "true");
      Array.prototype.forEach.call(form.querySelectorAll('button[type="submit"]'), function (button) {
        // Keep controls enabled so browser form serialization is unchanged.
        button.setAttribute("aria-disabled", "true");
      });
      var status = document.getElementById("auth-submit-status");
      if (status) { status.hidden = false; }
    });
  });
  window.addEventListener("pageshow", function () {
    Array.prototype.forEach.call(shell.querySelectorAll("form"), function (form) {
      delete form.dataset.submitting;
      form.removeAttribute("aria-busy");
      Array.prototype.forEach.call(form.querySelectorAll('[aria-disabled="true"]'), function (button) {
        button.removeAttribute("aria-disabled");
      });
    });
    Array.prototype.forEach.call(shell.querySelectorAll("[data-password-target]"), function (toggle) {
      var input = document.getElementById(toggle.dataset.passwordTarget);
      if (input) { input.type = "password"; }
      toggle.setAttribute("aria-pressed", "false");
      toggle.textContent = toggle.dataset.show + ": " + toggle.dataset.fieldLabel;
    });
    var status = document.getElementById("auth-submit-status");
    if (status) { status.hidden = true; }
    if (methodInputs.length) { syncIdentifierFields(); }
  });
}());
