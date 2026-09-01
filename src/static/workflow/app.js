/* Brain web UI — minimal progressive enhancement (CSP-safe, no inline JS).
 * Copy buttons fetch the same read-only export endpoints the no-JS links
 * use; failure never hides or destroys content.
 */
(function () {
  "use strict";

  function initCopyButtons() {
    var buttons = document.querySelectorAll(".copy-button[data-copy-url]");
    Array.prototype.forEach.call(buttons, function (button) {
      button.addEventListener("click", function () {
        var url = button.getAttribute("data-copy-url");
        fetch(url, { credentials: "same-origin" })
          .then(function (response) {
            if (!response.ok) {
              throw new Error("HTTP " + response.status);
            }
            return response.text();
          })
          .then(function (text) {
            if (navigator.clipboard && navigator.clipboard.writeText) {
              return navigator.clipboard.writeText(text);
            }
            throw new Error("clipboard unavailable");
          })
          .then(function () {
            button.textContent = "Copied!";
            button.classList.add("copied");
          })
          .catch(function () {
            button.textContent = "Copy failed — use the export link";
            button.classList.add("copy-failed");
          });
      });
    });
  }

  function initConfirmButtons() {
    // Extra client-side confirmation for expensive actions; the server
    // always renders its own confirmation interstitial, so this is only
    // a convenience when JS is available.
    var buttons = document.querySelectorAll('form[data-confirm] button[type="submit"]');
    Array.prototype.forEach.call(buttons, function (button) {
      button.addEventListener("click", function (event) {
        var message = button.closest("form").getAttribute("data-confirm");
        if (message && !window.confirm(message)) {
          event.preventDefault();
        }
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      initCopyButtons();
      initConfirmButtons();
    });
  } else {
    initCopyButtons();
    initConfirmButtons();
  }
})();
