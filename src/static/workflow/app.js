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

  // Progressive enhancement for the shared .confirm-form submit: the FIRST
  // submit proceeds normally (native POST -> redirect -> GET preserved)
  // while its submit button is disabled and relabelled "Running…" and the
  // form is marked busy; repeated submit events are guarded in memory so
  // the action is never submitted twice. When JS is absent the form is an
  // ordinary POST form — no inline handlers, no CSP change.
  function initConfirmForms() {
    var submitted = new WeakSet();
    var forms = document.querySelectorAll(".confirm-form");
    Array.prototype.forEach.call(forms, function (form) {
      form.addEventListener("submit", function (event) {
        if (submitted.has(form)) {
          event.preventDefault();
          return;
        }
        submitted.add(form);
        var button = form.querySelector('button[type="submit"]');
        if (button) {
          button.disabled = true;
          button.setAttribute("aria-disabled", "true");
          button.textContent = "Running…";
        }
        form.setAttribute("aria-busy", "true");
      });
    });
  }

  var FOCUSABLE_SELECTOR =
    'button:not([disabled]), [href], input:not([disabled]):not([type="hidden"]), ' +
    'select:not([disabled]), textarea:not([disabled]), ' +
    '[tabindex]:not([tabindex="-1"])';

  function focusFirst(container) {
    // Prefer the explicit "first useful control" marker (the tag filter
    // for the tag editor, the route profile select for routing), else
    // fall back to the first real focusable control in DOM order.
    // Hidden inputs are never candidates.
    var target = container.querySelector("[data-modal-focus]");
    if (!target) {
      target = container.querySelector(FOCUSABLE_SELECTOR);
    }
    if (target) {
      target.focus();
    }
  }

  // Enhanced native <details> (Routing / + Add tag): WITHOUT JS the
  // details is an ordinary inline disclosure showing the real
  // server-rendered forms. WITH JS the same content is moved into a
  // custom overlay — a fixed backdrop div wrapping a role=dialog panel —
  // presented as an accessible pop-out. This deliberately avoids
  // HTMLDialogElement's modal presentation (whose non-support fallback
  // is not a true overlay): visibility is driven by the `hidden`
  // attribute, which the stylesheet makes truly hidden. The native
  // summary stays in the document so the trigger keeps its semantics;
  // the server forms are MOVED, not duplicated, so ids/Csrf/fingerprints
  // remain valid and no innerHTML is ever built from user/config values.
  //
  // The + Add tag modal stages every change in the DOM only and commits
  // the COMPLETE selection on its own [data-modal-commit] Done submit:
  // such a panel gets a Cancel close button (never a second generated
  // Done), and (re)opening resets the staged state back to the
  // server-rendered initial values so Cancel/Escape/backdrop discards
  // everything. Routing has no commit control and keeps the generated
  // Done close button.
  function initModalDetails() {
    var details = document.querySelectorAll("details.enhanced-details");
    Array.prototype.forEach.call(details, function (detailsEl) {
      var summary = detailsEl.querySelector(":scope > summary");
      if (!summary) {
        return;
      }
      var title = (summary.textContent || "Options").trim();
      var overlay = document.createElement("div");
      overlay.className = "modal-overlay";
      overlay.setAttribute("hidden", "");
      var panel = document.createElement("div");
      panel.className = "modal-panel";
      panel.setAttribute("role", "dialog");
      panel.setAttribute("aria-modal", "true");
      var titleId = "modal-title-" + Math.random().toString(36).slice(2, 10);
      var titleEl = document.createElement("h2");
      titleEl.id = titleId;
      titleEl.className = "modal-title";
      titleEl.textContent = title;
      panel.appendChild(titleEl);
      while (summary.nextSibling) {
        panel.appendChild(summary.nextSibling);
      }
      var closeButton = document.createElement("button");
      closeButton.type = "button";
      closeButton.className = "modal-close";
      // A panel with its own [data-modal-commit] Done submit (the tag
      // editor) closes with Cancel; every other panel keeps Done.
      var commitControl = panel.querySelector("[data-modal-commit]");
      closeButton.textContent = commitControl ? "Cancel" : "Done";
      panel.appendChild(closeButton);
      panel.setAttribute("aria-labelledby", titleId);
      overlay.appendChild(panel);
      document.body.appendChild(overlay);

      function resetStagedState() {
        // The tag editor's checkbox/text state is staged in the DOM only.
        // On every (re)open, restore the server-rendered initial values:
        // form.reset() returns checkboxes to their `checked` attributes
        // (the authoritative active set) and clears the text input; the
        // client-only filter is cleared and every filtered option
        // un-hidden. Resetting the routing form is equally harmless.
        var filterInput = panel.querySelector(".tag-filter-input");
        if (filterInput) {
          filterInput.value = "";
        }
        var form = panel.querySelector("form");
        if (form) {
          form.reset();
        }
        var options = panel.querySelectorAll(".tag-option");
        Array.prototype.forEach.call(options, function (option) {
          option.hidden = false;
        });
      }

      var trigger = null;
      function openModal() {
        trigger = summary;
        resetStagedState();
        overlay.removeAttribute("hidden");
        focusFirst(panel);
      }
      function closeModal() {
        overlay.setAttribute("hidden", "");
        if (trigger && typeof trigger.focus === "function") {
          trigger.focus();
        }
        trigger = null;
      }
      // Intercept the summary trigger: prevent the native inline
      // expansion and open the overlay with the SAME moved content. The
      // toggle listener is a safety net for non-click toggling; without
      // JS neither handler exists and the details remains a plain
      // disclosure with the real forms.
      summary.addEventListener("click", function (event) {
        event.preventDefault();
        openModal();
      });
      detailsEl.addEventListener("toggle", function () {
        if (!detailsEl.hasAttribute("open")) {
          return;
        }
        detailsEl.removeAttribute("open");
        openModal();
      });
      overlay.addEventListener("click", function (event) {
        if (event.target === overlay) {
          closeModal(); // backdrop click
        }
      });
      overlay.addEventListener("keydown", function (event) {
        if (event.key === "Escape") {
          event.preventDefault();
          closeModal();
          return;
        }
        if (event.key !== "Tab") {
          return;
        }
        var focusables = panel.querySelectorAll(FOCUSABLE_SELECTOR);
        if (!focusables.length) {
          return;
        }
        var first = focusables[0];
        var last = focusables[focusables.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      });
      closeButton.addEventListener("click", closeModal);
    });
  }

  // Client-only "Filter tags" over the server-rendered option blocks:
  // hides non-matching options in memory — no GET, no writes, no network.
  // Names are read via getAttribute/textContent, never innerHTML.
  function initTagFilters() {
    var inputs = document.querySelectorAll(".tag-filter-input");
    Array.prototype.forEach.call(inputs, function (input) {
      var body = input.closest(".tag-editor-body");
      if (!body) {
        return;
      }
      var options = body.querySelectorAll(".tag-option");
      input.addEventListener("input", function () {
        var query = (input.value || "").toLowerCase();
        Array.prototype.forEach.call(options, function (option) {
          var name = (option.getAttribute("data-tag-name") || "").toLowerCase();
          option.hidden = query.length > 0 && name.indexOf(query) === -1;
        });
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      initCopyButtons();
      initConfirmButtons();
      initConfirmForms();
      initModalDetails();
      initTagFilters();
    });
  } else {
    initCopyButtons();
    initConfirmButtons();
    initConfirmForms();
    initModalDetails();
    initTagFilters();
  }
})();
