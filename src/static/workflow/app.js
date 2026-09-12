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
  // the action is never submitted twice. The synchronous POST is a normal
  // browser navigation, so every navigation anchor INSIDE the form must
  // also become non-actionable while it is running: leaving the page
  // mid-request aborts the browser connection. ONE deliberate, narrowly
  // scoped exception: the Section confirmation's ``Back to section``
  // anchor (``[data-confirm-exempt]``) stays enabled/clickable while the
  // request runs — the user explicitly accepts that navigating away
  // mid-request may abort the connection. Cancel and every other anchor
  // keep the full disable behaviour. When JS is absent the form is an
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
        // Disable every navigation anchor inside the form except the
        // narrowly scoped ``[data-confirm-exempt]`` escape: remove
        // non-exempt links from the tab order, suppress clicks, and give
        // them a visible disabled state. The submit button alone cannot
        // prevent an in-flight Back/Cancel from aborting the request.
        var links = form.querySelectorAll('a[href]');
        Array.prototype.forEach.call(links, function (link) {
          if (link.hasAttribute("data-confirm-exempt")) {
            return;
          }
          link.setAttribute("tabindex", "-1");
          link.setAttribute("aria-disabled", "true");
          link.classList.add("confirm-form-link-disabled");
          link.addEventListener("click", function (event) {
            event.preventDefault();
          });
        });
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

  // ---- Step 6.1: transcript-page trim & split editor (segmented versions) ----
  // The active Transcript page hosts the only 6.1 editor. Pressing
  // "Edit trim & splits" reveals small scissors on the inter-segment
  // divider lines of the CURRENTLY RENDERED bounded page (never the whole
  // transcript — no transcript text is ever loaded into client state).
  // Clicking a scissors opens a small accessible action dialog
  // (Split here / Crop from here / Crop to here / Remove split).
  // Cropped rows are hidden (never dimmed); topic inputs appear inline
  // only when splits exist (N splits => N+1 topics; zero splits => none).
  // Save submits the bounded staged metadata (range + sorted splits +
  // titles, max 200 topics) to the POST-only two-step save route; the
  // server renders the confirmation. All values are read/written via
  // DOM text/value properties — never innerHTML — and the JSON payload
  // comes from the server's json_script block. Without JS the page is a
  // normal read-only transcript: the toggle and editor never run.
  function initSegmentationControls() {
    var container = document.getElementById("transcript-doc");
    if (!container) return;
    var cropBar = document.getElementById("crop-view-bar");
    var cropMsg = document.getElementById("crop-view-msg");
    var cropToggle = document.getElementById("crop-view-toggle");
    var editToggle = document.getElementById("transcript-edit-toggle");
    var stateEl = document.getElementById("segmentation-editor-state");
    var editable = !!(
      cropBar && cropMsg && cropToggle && editToggle && stateEl &&
      document.getElementById("edit-mode-bar") &&
      document.getElementById("edit-status") &&
      document.getElementById("edit-clear-crop") &&
      document.getElementById("edit-reset") &&
      document.getElementById("edit-save") &&
      document.getElementById("segmentation-save-form")
    );

    var segmentEls = Array.prototype.slice.call(container.querySelectorAll(".transcript-segment"));
    var ordinals = segmentEls.map(function (el) {
      return parseInt(el.getAttribute("data-ordinal"), 10);
    });
    var SEGMENT_COUNT = ordinals.length ? ordinals[ordinals.length - 1] + 1 : 0;
    var viewFull = false;

    // ---- Read-only pages (historical transcript or explicit layout): the
    // working view hides cropped rows server-side and the toggle reveals
    // them in memory only — no writes, no navigation.
    if (!editable) {
      if (!cropBar || !cropToggle) return;
      var hiddenRows = segmentEls.filter(function (el) { return el.hasAttribute("hidden"); });
      cropToggle.addEventListener("click", function () {
        viewFull = !viewFull;
        hiddenRows.forEach(function (el) { el.hidden = viewFull ? false : true; });
        cropToggle.textContent = viewFull ? "Show saved working view" : "Show full transcript";
      });
      return;
    }

    var initialState;
    try {
      initialState = JSON.parse(stateEl.textContent);
    } catch (err) {
      return;
    }
    SEGMENT_COUNT = initialState.segment_count;
    function makeState() {
      return {
        start: initialState.start,
        end: initialState.end,
        splits: initialState.splits.slice(),
        titles: initialState.titles.slice(),
        titleFlags: (initialState.title_is_temporary || []).slice(),
      };
    }
    function cloneState(s) {
      return {
        start: s.start,
        end: s.end,
        splits: s.splits.slice(),
        titles: s.titles.slice(),
        titleFlags: s.titleFlags.slice(),
      };
    }
    var activeState = makeState();
    var staged = cloneState(activeState);
    var editMode = false;
    var allowLeave = false;

    var editBar = document.getElementById("edit-mode-bar");
    var editStatus = document.getElementById("edit-status");
    var editClearCrop = document.getElementById("edit-clear-crop");
    var editReset = document.getElementById("edit-reset");
    var editSave = document.getElementById("edit-save");
    var saveForm = document.getElementById("segmentation-save-form");
    var fingerprintInput = document.getElementById("segmentation-fingerprint-input");
    var startInput = document.getElementById("segmentation-start-input");
    var endInput = document.getElementById("segmentation-end-input");
    var fingerprint = fingerprintInput ? fingerprintInput.value : "";

    var scissorsButtons = [];
    var topicMarkers = [];

    function deriveSections(start, end, splits) {
      var interior = splits.filter(function (s) { return s > start && s < end; })
        .sort(function (a, b) { return a - b; });
      if (interior.length === 0) return [];
      var bounds = [start].concat(interior, [end]);
      var out = [];
      for (var i = 0; i < bounds.length - 1; i++) {
        out.push({ start: bounds[i], end: bounds[i + 1] });
      }
      return out;
    }

    // SERVER-authoritative temporary title for a canonical ordinal:
    // picked from the bounded list rendered in the editor JSON (never
    // derived from the browser clock). Index = ordinal - 1.
    function serverTemporaryTitle(ordinal) {
      var list = (initialState && initialState.temporary_titles) || [];
      return list[ordinal - 1] || "";
    }

    // Preserve topic names AND their temporary-title flags ONLY by exact
    // canonical [start,end) range match, independent of index: a section
    // whose range is unchanged keeps its title + flag (existing exact
    // ranges preserve title/provenance); a carried-over TEMPORARY section
    // whose canonical ordinal CHANGED in the revised layout regenerates
    // the appropriate SERVER title (never retains a mismatched
    // "Segment N"); a brand-new range is visibly PREFILLED with the
    // server title for its ordinal (a True temporary flag; any input
    // event makes it custom). No left-prefix heuristic, no positional
    // carry-over, no inference.
    function rebuildTitles(prevSections, prevTitles, prevFlags) {
      var sections = deriveSections(staged.start, staged.end, staged.splits);
      var byRange = {};
      var flagByRange = {};
      var ordinalByRange = {};
      prevSections.forEach(function (sec, i) {
        byRange[sec.start + ":" + sec.end] = prevTitles[i] || "";
        flagByRange[sec.start + ":" + sec.end] = !!(prevFlags && prevFlags[i]);
        ordinalByRange[sec.start + ":" + sec.end] = i + 1; // previous ordinal
      });
      var titles = [];
      var flags = [];
      sections.forEach(function (sec, index) {
        var key = sec.start + ":" + sec.end;
        var ordinal = index + 1;
        if (key in byRange) {
          titles.push(byRange[key]);
          flags.push(flagByRange[key]);
          if (flagByRange[key] && ordinalByRange[key] !== ordinal) {
            titles[titles.length - 1] = serverTemporaryTitle(ordinal);
          }
        } else {
          titles.push(serverTemporaryTitle(ordinal));
          flags.push(true); // brand-new range: server-derived temporary title
        }
      });
      staged.titleFlags = flags;
      return titles;
    }

    function scissorsIcon() {
      var NS = "http://www.w3.org/2000/svg";
      function el(name, attrs) {
        var node = document.createElementNS(NS, name);
        for (var k in attrs) node.setAttribute(k, attrs[k]);
        return node;
      }
      var svg = el("svg", {
        viewBox: "0 0 24 24", width: "12", height: "12",
        fill: "none", stroke: "currentColor", "stroke-width": "2",
        "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true",
      });
      svg.appendChild(el("circle", { cx: "6", cy: "6", r: "3" }));
      svg.appendChild(el("circle", { cx: "6", cy: "18", r: "3" }));
      svg.appendChild(el("line", { x1: "20", y1: "4", x2: "8.12", y2: "15.88" }));
      svg.appendChild(el("line", { x1: "14.47", y1: "14.48", x2: "20", y2: "20" }));
      svg.appendChild(el("line", { x1: "8.12", y1: "8.12", x2: "12", y2: "12" }));
      return svg;
    }

    function timeForOrdinal(o) {
      for (var i = 0; i < ordinals.length; i++) {
        if (ordinals[i] === o) {
          var t = segmentEls[i].querySelector(".timestamp");
          return t ? t.textContent : "";
        }
      }
      return "";
    }
    function boundaryLabel(b) {
      var t = timeForOrdinal(b);
      return "before segment " + b + (t ? " · " + t : "");
    }

    function pageHasWorkingRows(range) {
      return ordinals.some(function (o) { return o >= range.start && o < range.end; });
    }

    // One compact scissors per inter-segment divider line of THIS page
    // (never at the transcript's very start or end). The divider BEFORE
    // the page's first segment is included whenever that boundary is
    // interior to the whole transcript (ordinal > 0 and < total count) —
    // otherwise a pagination boundary before page >1 would have no
    // scissors on either page.
    function makeScissors(boundary, triggerEl) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "boundary-scissors";
      btn.setAttribute("data-boundary", String(boundary));
      btn.setAttribute("aria-haspopup", "dialog");
      btn.hidden = true;
      btn.appendChild(scissorsIcon());
      btn.addEventListener("click", function () {
        if (editMode) openBoundaryDialog(boundary, btn);
      });
      return btn;
    }
    segmentEls.forEach(function (el, i) {
      var o = ordinals[i];
      if (o <= 0 || o >= SEGMENT_COUNT) return;
      var sc = makeScissors(o, el);
      container.insertBefore(sc, el);
      scissorsButtons.push(sc);
    });

    // ---- Boundary action dialog (opened ONLY by clicking a scissors) ----
    var dialog = document.getElementById("boundary-action-dialog");
    var dialogTitle = document.getElementById("boundary-action-title");
    var dialogSummary = document.getElementById("boundary-action-summary");
    var dialogNote = document.getElementById("boundary-action-note");
    var splitBtn = document.getElementById("boundary-split");
    var cropFromBtn = document.getElementById("boundary-crop-from");
    var cropToBtn = document.getElementById("boundary-crop-to");
    var removeBtn = document.getElementById("boundary-remove-split");
    var cancelBtn = document.getElementById("boundary-cancel");
    var dialogButtons = [splitBtn, cropFromBtn, cropToBtn, removeBtn, cancelBtn];
    var selectedBoundary = null;
    var dialogTrigger = null;

    function insideRange(b) { return b > staged.start && b < staged.end; }
    function canSplit(b) { return insideRange(b) && staged.splits.indexOf(b) === -1; }
    function canRemoveSplit(b) { return staged.splits.indexOf(b) !== -1; }

    function focusFirstEnabled() {
      for (var i = 0; i < dialogButtons.length; i++) {
        var b = dialogButtons[i];
        if (b && !b.hidden && !b.disabled) { b.focus(); return; }
      }
      if (cancelBtn) cancelBtn.focus();
    }

    function openBoundaryDialog(b, triggerEl) {
      selectedBoundary = b;
      dialogTrigger = triggerEl || null;
      dialogTitle.textContent = "Boundary — " + boundaryLabel(b);
      dialogSummary.textContent = "Choose an action for the divider " + boundaryLabel(b) + ".";
      splitBtn.disabled = !canSplit(b);
      cropFromBtn.disabled = !insideRange(b);
      cropToBtn.disabled = !insideRange(b);
      removeBtn.hidden = !canRemoveSplit(b);
      removeBtn.disabled = !canRemoveSplit(b);
      if (b <= staged.start) {
        dialogNote.textContent = "This is the start of the retained range — cropping or splitting here is not possible.";
      } else if (b >= staged.end) {
        dialogNote.textContent = "This is the end of the retained range — cropping or splitting here is not possible.";
      } else if (staged.splits.indexOf(b) !== -1) {
        dialogNote.textContent = "This boundary is already a split — Remove split reverses it.";
      } else {
        dialogNote.textContent = "Split here divides the transcript into named sections; crops hide the content above or below this boundary from the staged view.";
      }
      dialog.removeAttribute("hidden");
      focusFirstEnabled();
    }
    function closeBoundaryDialog() {
      dialog.setAttribute("hidden", "");
      if (dialogTrigger && typeof dialogTrigger.focus === "function") dialogTrigger.focus();
      dialogTrigger = null;
      selectedBoundary = null;
    }
    dialog.addEventListener("click", function (event) {
      if (event.target === dialog) closeBoundaryDialog();
    });
    dialog.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        event.preventDefault();
        closeBoundaryDialog();
        return;
      }
      if (event.key !== "Tab") return;
      var focusables = dialogButtons.filter(function (b) { return b && !b.hidden && !b.disabled; });
      if (!focusables.length) return;
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

    function applySplit() {
      if (selectedBoundary === null || !canSplit(selectedBoundary)) return;
      var prevSections = deriveSections(staged.start, staged.end, staged.splits);
      var prevTitles = staged.titles;
      var prevFlags = staged.titleFlags.slice();
      staged.splits = staged.splits.concat(selectedBoundary)
        .sort(function (a, b) { return a - b; });
      staged.titles = rebuildTitles(prevSections, prevTitles, prevFlags);
      renderAll();
    }
    function applyCropFrom() {
      if (selectedBoundary === null || !insideRange(selectedBoundary)) return;
      var prevSections = deriveSections(staged.start, staged.end, staged.splits);
      var prevTitles = staged.titles;
      var prevFlags = staged.titleFlags.slice();
      staged.start = selectedBoundary;
      staged.splits = staged.splits.filter(function (s) { return s > staged.start && s < staged.end; });
      staged.titles = rebuildTitles(prevSections, prevTitles, prevFlags);
      renderAll();
    }
    function applyCropTo() {
      if (selectedBoundary === null || !insideRange(selectedBoundary)) return;
      var prevSections = deriveSections(staged.start, staged.end, staged.splits);
      var prevTitles = staged.titles;
      var prevFlags = staged.titleFlags.slice();
      staged.end = selectedBoundary;
      staged.splits = staged.splits.filter(function (s) { return s > staged.start && s < staged.end; });
      staged.titles = rebuildTitles(prevSections, prevTitles, prevFlags);
      renderAll();
    }
    function applyRemoveSplit() {
      if (selectedBoundary === null || !canRemoveSplit(selectedBoundary)) return;
      var prevSections = deriveSections(staged.start, staged.end, staged.splits);
      var prevTitles = staged.titles;
      var prevFlags = staged.titleFlags.slice();
      staged.splits = staged.splits.filter(function (s) { return s !== selectedBoundary; });
      staged.titles = rebuildTitles(prevSections, prevTitles, prevFlags);
      renderAll();
    }
    splitBtn.addEventListener("click", function () { applySplit(); closeBoundaryDialog(); });
    cropFromBtn.addEventListener("click", function () { applyCropFrom(); closeBoundaryDialog(); });
    cropToBtn.addEventListener("click", function () { applyCropTo(); closeBoundaryDialog(); });
    removeBtn.addEventListener("click", function () { applyRemoveSplit(); closeBoundaryDialog(); });
    cancelBtn.addEventListener("click", closeBoundaryDialog);

    // ---- Inline topic inputs (only when splits exist; N+1 for N splits) ----
    // ``beforeEl`` (optional) inserts the row directly before an existing
    // row instead of before the anchor segment; the row is returned so
    // callers can place an adjacent context input before it.
    function insertTopicInput(anchor, index, sec, beforeEl) {
      var row = document.createElement("div");
      row.className = "section-topic-inline";
      var label = document.createElement("label");
      label.className = "section-topic-label";
      label.setAttribute("for", "topic-" + index);
      label.textContent = "Topic " + (index + 1);
      var input = document.createElement("input");
      input.type = "text";
      input.className = "topic-input";
      input.id = "topic-" + index;
      input.maxLength = 255;
      input.value = staged.titles[index] || "";
      // A temporary (True-flag) section is auto-named by the server on
      // save ("Segment N of YYYYMMDDHHMM"); typing ANY custom name flips
      // it to custom (False flag) — "editing title makes it custom".
      input.placeholder = staged.titleFlags[index]
        ? "Auto-named — type a custom name"
        : "Name this section";
      input.setAttribute("aria-label",
        "Topic " + (index + 1) + " for the section starting at segment " + sec.start);
      input.addEventListener("input", function () {
        staged.titles[index] = input.value;
        if (input.value !== "") {
          staged.titleFlags[index] = false;
        }
        updateStatus();
      });
      row.appendChild(label);
      row.appendChild(input);
      if (beforeEl && beforeEl.parentNode) {
        beforeEl.parentNode.insertBefore(row, beforeEl);
      } else {
        anchor.parentNode.insertBefore(row, anchor);
      }
      topicMarkers.push(row);
      return row;
    }
    function clearTopicMarkers() {
      topicMarkers.forEach(function (m) {
        if (m.parentNode) m.parentNode.removeChild(m);
      });
      topicMarkers.length = 0;
    }

    // ---- Rendering ----
    var headingEls = Array.prototype.slice.call(
      container.querySelectorAll(".topic-heading")
    );
    function renderTranscriptFlow() {
      var range = editMode ? staged : activeState;
      var showFull = !editMode && viewFull;
      segmentEls.forEach(function (el, i) {
        var o = ordinals[i];
        el.hidden = showFull ? false : (o < range.start || o >= range.end);
      });
      // Server-rendered topic headings: hidden only while editing (the
      // inline topic inputs replace them); restored on exit/reset.
      headingEls.forEach(function (el) {
        el.hidden = editMode;
      });
      scissorsButtons.forEach(function (btn) {
        var b = parseInt(btn.getAttribute("data-boundary"), 10);
        // No action is valid at the staged crop endpoints (valid storage
        // never places a split there, so there is no Remove action).
        btn.hidden = !editMode || b <= staged.start || b >= staged.end;
        var isSplit = staged.splits.indexOf(b) !== -1;
        btn.classList.toggle("is-split", isSplit);
        btn.setAttribute("aria-label", boundaryLabel(b) +
          (isSplit
            ? " — split marker. Actions: remove split, crop from here, crop to here."
            : ". Actions: split here, crop from here, crop to here."));
      });
      clearTopicMarkers();
      if (editMode) {
        var sections = deriveSections(staged.start, staged.end, staged.splits);
        if (sections.length > 0) {
          var firstVisible = null;
          var firstVisibleEl = null;
          segmentEls.forEach(function (el, i) {
            if (firstVisible === null && !el.hidden) {
              firstVisible = ordinals[i];
              firstVisibleEl = el;
            }
          });
          sections.forEach(function (sec, idx) {
            var anchor = null;
            for (var i = 0; i < ordinals.length; i++) {
              if (ordinals[i] === sec.start) { anchor = segmentEls[i]; break; }
            }
            if (anchor) {
              var currentInput = insertTopicInput(anchor, idx, sec);
              // A split or crop-to exactly at the page-start boundary (the
              // first segment of this page) makes the PRECEDING section end
              // at that ordinal: it began on an earlier page, so the
              // containing rule below cannot reach it (its end equals the
              // first visible ordinal). Expose AT MOST ONE adjacent
              // preceding context input directly BEFORE the current
              // section's input so BOTH changed/new section names stay
              // editable on this page.
              if (idx > 0 && sec.start === ordinals[0] && sections[idx - 1].end === ordinals[0]) {
                insertTopicInput(anchor, idx - 1, sections[idx - 1], currentInput);
              }
            } else if (firstVisibleEl !== null && sec.start <= firstVisible && firstVisible < sec.end) {
              // The section began on an earlier page: one inline/context
              // topic input before the FIRST VISIBLE segment on this page,
              // so a first split on this page still exposes BOTH names.
              insertTopicInput(firstVisibleEl, idx, sec);
            }
          });
        }
      }
    }

    function renderCropBar() {
      if (editMode) { cropBar.hidden = true; return; }
      var hasCrop = activeState.start > 0 || activeState.end < SEGMENT_COUNT;
      cropBar.hidden = !hasCrop;
      if (hasCrop) {
        var hiddenCount = activeState.start + (SEGMENT_COUNT - activeState.end);
        var noun = hiddenCount === 1 ? "line" : "lines";
        var emptyPage = !pageHasWorkingRows(activeState);
        cropMsg.textContent = viewFull
          ? "Showing the full transcript — the saved working view hides " + hiddenCount + " " + noun + "."
          : (emptyPage
              ? "Saved working view — " + hiddenCount + " " + noun + " hidden and none on this page. The full transcript remains available."
              : "Saved working view — " + hiddenCount + " " + noun + " hidden. The full transcript remains available.");
        cropToggle.textContent = viewFull ? "Show saved working view" : "Show full transcript";
      }
    }
    cropToggle.addEventListener("click", function () {
      if (editMode) return;
      viewFull = !viewFull;
      renderAll();
    });

    // ---- Validation / staged status ----
    function validateStaged() {
      if (staged.start >= staged.end) {
        return { ok: false, message: "The crop must leave at least one segment." };
      }
      if (staged.splits.length === 0) return { ok: true, message: "" };
      var sections = deriveSections(staged.start, staged.end, staged.splits);
      for (var i = 0; i < sections.length; i++) {
        var title = staged.titles[i] || "";
        var temporary = !!staged.titleFlags[i];
        // A temporary section may stay blank (the server derives its
        // title); a custom section must be named.
        if (!temporary && title.trim() === "") {
          return { ok: false, message: "Every section needs a topic — name each resulting section." };
        }
        if (/[\u0000-\u001f\u007f]/.test(title)) {
          return { ok: false, message: "A topic contains an invalid character — control characters and newlines are not allowed." };
        }
        if (title.length > 255) {
          return { ok: false, message: "A topic is too long — at most 255 characters." };
        }
      }
      return { ok: true, message: "" };
    }

    function isDirty() {
      if (staged.start !== activeState.start || staged.end !== activeState.end) return true;
      if (staged.splits.join(",") !== activeState.splits.join(",")) return true;
      var sections = deriveSections(staged.start, staged.end, staged.splits);
      for (var i = 0; i < sections.length; i++) {
        if ((staged.titles[i] || "") !== (activeState.titles[i] || "")) return true;
        if (!!staged.titleFlags[i] !== !!activeState.titleFlags[i]) return true;
      }
      return false;
    }

    function updateStatus() {
      var dirty = isDirty();
      var valid = validateStaged();
      var above = staged.start;
      var below = SEGMENT_COUNT - staged.end;
      var sections = deriveSections(staged.start, staged.end, staged.splits);
      var parts = [];
      if (above > 0) parts.push(above + " line" + (above === 1 ? "" : "s") + " cropped above");
      if (below > 0) parts.push(below + " line" + (below === 1 ? "" : "s") + " cropped below");
      if (sections.length > 1) {
        parts.push(sections.length + " sections from " + staged.splits.length + " split" +
          (staged.splits.length === 1 ? "" : "s"));
      }
      var msg;
      if (!dirty) {
        msg = "No changes yet.";
      } else {
        msg = (parts.length ? parts.join(" · ") : "Staged changes") + " — not saved yet.";
      }
      if (!valid.ok) msg = msg + " " + valid.message;
      editStatus.textContent = msg;
      editSave.disabled = !dirty || !valid.ok;
      editReset.disabled = !dirty;
      editClearCrop.disabled = staged.start === 0 && staged.end === SEGMENT_COUNT;
    }

    function renderAll() {
      renderTranscriptFlow();
      renderCropBar();
      updateStatus();
    }

    // ---- Edit mode toggle (only reveals scissors/controls; no dialog) ----
    function setEditMode(on) {
      editMode = on;
      editBar.hidden = !on;
      editToggle.setAttribute("aria-expanded", String(on));
      editToggle.textContent = on ? "Done editing" : "Edit trim & splits";
      if (!on) closeBoundaryDialog();
      if (on) {
        staged = cloneState(activeState);
        viewFull = false;
        allowLeave = false;
      }
      renderAll();
    }
    editToggle.addEventListener("click", function () { setEditMode(!editMode); });

    // ---- Compact controls: Clear crop / Reset / Save ----
    editClearCrop.addEventListener("click", function () {
      if (staged.start === 0 && staged.end === SEGMENT_COUNT) return;
      var prevSections = deriveSections(staged.start, staged.end, staged.splits);
      var prevTitles = staged.titles;
      var prevFlags = staged.titleFlags.slice();
      staged.start = 0;
      staged.end = SEGMENT_COUNT;
      staged.splits = staged.splits.filter(function (s) { return s > 0 && s < SEGMENT_COUNT; });
      staged.titles = rebuildTitles(prevSections, prevTitles, prevFlags);
      renderAll();
    });
    editReset.addEventListener("click", function () {
      staged = cloneState(activeState);
      renderAll();
    });

    // Save: ONE confirmed immutable revision via the POST-only two-step
    // route. The server renders the confirmation before any mutation.
    function clearPayloadInputs() {
      var existing = saveForm.querySelectorAll(
        'input[name="split"], input[name="title"], input[name="title_is_temporary"]'
      );
      Array.prototype.forEach.call(existing, function (el) {
        if (el.parentNode) el.parentNode.removeChild(el);
      });
    }
    editSave.addEventListener("click", function () {
      if (!validateStaged().ok) return;
      clearPayloadInputs();
      staged.splits.forEach(function (s) {
        var input = document.createElement("input");
        input.type = "hidden";
        input.name = "split";
        input.value = String(s);
        saveForm.appendChild(input);
      });
      staged.titles.forEach(function (t) {
        var input = document.createElement("input");
        input.type = "hidden";
        input.name = "title";
        input.value = t;
        saveForm.appendChild(input);
      });
      staged.titleFlags.forEach(function (flag) {
        var input = document.createElement("input");
        input.type = "hidden";
        input.name = "title_is_temporary";
        input.value = flag ? "1" : "0";
        saveForm.appendChild(input);
      });
      if (fingerprintInput) fingerprintInput.value = fingerprint;
      if (startInput) startInput.value = String(staged.start);
      if (endInput) endInput.value = String(staged.end);
      allowLeave = true;
      saveForm.submit();
    });

    // Standard dirty-state leave warning (browser-owned dialog); a real
    // form submit suppresses it. No drafts are persisted anywhere.
    saveForm.addEventListener("submit", function () { allowLeave = true; });
    window.addEventListener("beforeunload", function (event) {
      if (!allowLeave && editMode && isDirty()) {
        event.preventDefault();
        event.returnValue = "";
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      initCopyButtons();
      initConfirmButtons();
      initConfirmForms();
      initModalDetails();
      initTagFilters();
      initSegmentationControls();
    });
  } else {
    initCopyButtons();
    initConfirmButtons();
    initConfirmForms();
    initModalDetails();
    initTagFilters();
    initSegmentationControls();
  }
})();
