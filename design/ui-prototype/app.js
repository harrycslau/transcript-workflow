/* ============================================================
   Brain UI Prototype — Interactions (v6)
   ============================================================
   DESIGN PROTOTYPE, not production code.
   View toggle, sort-aware grouping, localStorage, table render.
   v6: Recording Detail contains the complete summary; language
   variant tabs, Copy Markdown and Regenerate confirmation work
   directly on the detail screen. Transcript and History remain
   separate navigable screens through the shared showScreen
   navigation; no sticky action bar.
   ============================================================ */

document.addEventListener('DOMContentLoaded', () => {
  // ---- State ----
  let currentScreen = 'library';
  let savedQuery = '';
  let savedScroll = 0;

  // View mode: read from localStorage, default to 'cards'
  let viewMode = 'cards';
  try { viewMode = localStorage.getItem('brain-view-mode') || 'cards'; } catch (_) {}

  // Recording data (static for prototype)
  const recordings = [
    { date: '2026-09-01', dateLabel: '1 Sep 2026, 14:30', title: 'Weekly product sync — AI features roadmap', duration: 42, status: 'transcribed', tags: ['Work','Meeting','Research'], tagClasses: ['tag-confirmed','tag-suggested',''], langs: [1,1,1,0], excerpt: 'Discussion of the Q4 AI feature roadmap including local LLM integration, on-device speech recognition improvements, and the privacy-first approach to embedding generation...', needsAttention: false },
    { date: '2026-08-31', dateLabel: '31 Aug 2026, 08:15', title: 'Morning thoughts on Cantonese language learning', duration: 18, status: 'transcribed', tags: ['Personal','Language'], tagClasses: ['tag-confirmed',''], langs: [1,1,1,0], excerpt: 'Reflecting on the progress with Cantonese pronunciation, comparing tones with Mandarin, and discussing effective study methods for tonal languages...', needsAttention: false },
    { date: '2026-08-30', dateLabel: '30 Aug 2026, 16:00', title: 'Brainstorm — personal knowledge system architecture', duration: 55, status: 'needs_review', tags: ['Idea','Research'], tagClasses: ['','tag-suggested'], langs: [0,0,0,0], excerpt: 'Ideas for building a local-first personal knowledge management system with speech-to-text, automatic summarization, and semantic search capabilities...', needsAttention: true },
    { date: '2026-08-28', dateLabel: '28 Aug 2026, 19:00', title: 'Finnish vocabulary study session — academic register', duration: 25, status: 'transcribed', tags: ['Language','Personal'], tagClasses: ['tag-confirmed',''], langs: [1,1,0,1], excerpt: 'Went through Finnish academic vocabulary list, practiced pronunciation of long compound words, and reviewed example sentences from university lectures...', needsAttention: false },
    { date: '2026-08-25', dateLabel: '25 Aug 2026, 10:45', title: 'Podcast notes — privacy in the age of cloud AI', duration: 37, status: 'transcribed', tags: ['Research'], tagClasses: ['tag-confirmed'], langs: [1,1,1,0], excerpt: 'Notes and reactions to a podcast episode about the tension between cloud-based AI convenience and the importance of local-first, privacy-preserving approaches to personal data...', needsAttention: false },
    { date: '2026-08-22', dateLabel: '22 Aug 2026, 11:00', title: 'Therapy session notes — managing perfectionism', duration: 50, status: 'transcribed', tags: ['Personal'], tagClasses: ['tag-confirmed'], langs: [1,1,0,0], excerpt: 'Discussion about perfectionism patterns, strategies for "good enough" completion, and how to maintain high standards without paralysis...', needsAttention: false },
    { date: '2026-08-20', dateLabel: '20 Aug 2026, 20:30', title: 'Cantonese conversation practice with Man Yee', duration: 30, status: 'failed', tags: ['Language','Personal'], tagClasses: ['tag-confirmed',''], langs: [0,0,0,0], excerpt: 'Practice conversation in Cantonese about weekend plans and local food recommendations in Hong Kong...', needsAttention: false },
  ];

  // ---- Elements ----
  const screens = document.querySelectorAll('.screen');
  const searchInput = document.getElementById('global-search');
  const cardView = document.getElementById('card-view');
  const tableView = document.getElementById('table-view');
  const tableBody = document.getElementById('table-body');
  const searchResults = document.getElementById('search-results');
  const noResults = document.getElementById('no-results');
  const resultsCount = document.getElementById('results-count');
  const searchCount = document.getElementById('search-count');
  const modeTrigger = document.getElementById('mode-trigger');
  const modeDropdown = document.getElementById('mode-dropdown');
  const filterDrawerTrigger = document.getElementById('filter-drawer-trigger');
  const filterDrawer = document.getElementById('filter-drawer');
  const activeFilters = document.getElementById('active-filters');
  const activeFilterChips = document.getElementById('active-filter-chips');
  const clearFiltersBtn = document.getElementById('clear-filters');
  const mobileActiveFilters = document.getElementById('mobile-active-filters');
  const mobileActiveChips = document.getElementById('mobile-active-chips');
  const mobileClearBtn = document.getElementById('mobile-clear-filters');
  const sortDesktop = document.getElementById('sort-desktop');
  const sortMobile = document.getElementById('sort-mobile');
  const viewToggleBtns = document.querySelectorAll('.view-toggle-btn');

  // ---- Screen navigation ----
  function showScreen(id, opts = {}) {
    screens.forEach(s => s.classList.remove('active'));
    document.getElementById(id)?.classList.add('active');
    currentScreen = id;
    document.querySelectorAll('[data-screen]').forEach(el => {
      el.classList.toggle('active', el.dataset.screen === id);
    });
    if (id === 'library' && opts.restoreScroll) {
      requestAnimationFrame(() => window.scrollTo(0, savedScroll || 0));
    } else if (id !== 'detail') {
      window.scrollTo(0, 0);
    }
  }

  document.querySelectorAll('[data-screen]').forEach(el => {
    el.addEventListener('click', e => { e.preventDefault(); showScreen(el.dataset.screen); });
  });

  // ---- View mode toggle ----
  function setViewMode(mode) {
    viewMode = mode;
    try { localStorage.setItem('brain-view-mode', mode); } catch (_) {}
    viewToggleBtns.forEach(btn => {
      const isActive = btn.dataset.view === mode;
      btn.classList.toggle('active', isActive);
      btn.setAttribute('aria-checked', isActive);
    });
    cardView.style.display = mode === 'cards' ? 'block' : 'none';
    tableView.style.display = mode === 'table' ? 'block' : 'none';
    if (mode === 'table') renderTable();
  }

  viewToggleBtns.forEach(btn => {
    btn.addEventListener('click', () => setViewMode(btn.dataset.view));
  });

  // Apply saved view on load
  setViewMode(viewMode);

  // ---- Table rendering ----
  function renderTable(sort, query) {
    const s = sort || sortDesktop.value;
    const sorted = sortRecordings(recordings, s);
    const q = (query || '').trim().toLowerCase();
    const filtered = q ? sorted.filter(r => r.title.toLowerCase().includes(q) || r.excerpt.toLowerCase().includes(q) || r.tags.some(t => t.toLowerCase().includes(q))) : sorted;

    tableBody.innerHTML = '';
    const showGroups = s === 'date-desc' || s === 'date-asc';
    let lastGroup = '';

    filtered.forEach(r => {
      if (showGroups) {
        const grp = r.date.slice(0, 7); // YYYY-MM
        if (grp !== lastGroup) {
          lastGroup = grp;
          const sep = document.createElement('tr');
          sep.innerHTML = `<td colspan="6" style="padding:var(--sp-3) var(--sp-3) var(--sp-2);font-size:var(--fs-xs);font-weight:600;color:var(--color-text-3);border-bottom:1px solid var(--color-border);background:var(--color-bg)">${formatMonth(grp)}</td>`;
          tableBody.appendChild(sep);
        }
      }

      const tr = document.createElement('tr');
      if (r.needsAttention) tr.classList.add('needs-attention');
      tr.setAttribute('data-detail', '');
      tr.style.cursor = 'pointer';

      const tagsHtml = r.tags.map((t, i) => `<span class="tag ${r.tagClasses[i]}" style="font-size:10px;padding:1px 6px">${t}</span>`).join('');
      const langsHtml = r.langs.map(v => `<span class="lang-dot ${v ? 'has' : 'missing'}"></span>`).join('');
      const statusLabel = r.status === 'needs_review' ? 'needs review' : r.status;

      tr.innerHTML = `
        <td style="white-space:nowrap;font-size:var(--fs-xs);color:var(--color-text-3)">${r.dateLabel}</td>
        <td>
          <div class="col-title"><a href="#">${r.title}</a></div>
          <div class="col-overview">${q ? highlightExcerpt(r.excerpt, q) : r.excerpt}</div>
        </td>
        <td style="white-space:nowrap;font-size:var(--fs-xs)">${r.duration} min</td>
        <td><div class="col-tags">${tagsHtml}</div></td>
        <td><div class="col-langs">${langsHtml}</div></td>
        <td><span class="status status-${r.status}">${statusLabel}</span></td>
      `;
      tableBody.appendChild(tr);
    });

    // Rebind click handlers for table rows
    tableBody.querySelectorAll('tr[data-detail]').forEach(row => {
      row.addEventListener('click', () => {
        savedQuery = searchInput.value;
        savedScroll = window.scrollY;
        showScreen('detail');
      });
    });
  }

  function highlightExcerpt(text, query) {
    if (!query) return text;
    const words = query.split(/\s+/).filter(Boolean);
    let result = text;
    words.forEach(w => {
      const re = new RegExp(`(${w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')})`, 'gi');
      result = result.replace(re, '<mark>$1</mark>');
    });
    return result;
  }

  // ---- Sort logic ----
  function sortRecordings(list, sortKey) {
    const arr = [...list];
    switch (sortKey) {
      case 'date-desc': arr.sort((a, b) => b.date.localeCompare(a.date)); break;
      case 'date-asc': arr.sort((a, b) => a.date.localeCompare(b.date)); break;
      case 'title-asc': arr.sort((a, b) => a.title.localeCompare(b.title)); break;
      case 'title-desc': arr.sort((a, b) => b.title.localeCompare(a.title)); break;
      case 'duration': arr.sort((a, b) => b.duration - a.duration); break;
    }
    return arr;
  }

  function formatMonth(ym) {
    const [y, m] = ym.split('-');
    const months = ['January','February','March','April','May','June','July','August','September','October','November','December'];
    return `${months[parseInt(m, 10) - 1]} ${y}`;
  }

  function isChronologicalSort(sortKey) {
    return sortKey === 'date-desc' || sortKey === 'date-asc';
  }

  // ---- Month grouping for card view ----
  function renderCardGroups(sortKey) {
    const groups = cardView.querySelectorAll('.date-group-header');
    const showGroups = isChronologicalSort(sortKey);
    groups.forEach(g => g.style.display = showGroups ? '' : 'none');
  }

  // ---- Update results count ----
  function updateResultsCount() {
    const count = recordings.length;
    resultsCount.textContent = count === 1 ? '1 recording' : `${count} recordings`;
  }

  // ---- Sort change handler ----
  function onSortChange() {
    const sortKey = sortDesktop.value;
    if (sortMobile) sortMobile.value = sortKey;
    renderCardGroups(sortKey);
    if (viewMode === 'table') renderTable(sortKey, searchInput.value);
  }

  sortDesktop.addEventListener('change', onSortChange);
  if (sortMobile) sortMobile.addEventListener('change', () => {
    sortDesktop.value = sortMobile.value;
    onSortChange();
  });

  // ---- Unified Library / Search ----
  function updateView() {
    const query = searchInput.value.trim();
    const isSearching = query.length > 0;

    cardView.style.display = isSearching || viewMode !== 'cards' ? 'none' : 'block';
    tableView.style.display = isSearching || viewMode !== 'table' ? 'none' : 'block';
    searchResults.style.display = isSearching ? 'block' : 'none';

    if (isSearching) {
      const count = Math.min(query.length * 2, 6);
      searchCount.textContent = count === 1 ? '1 result' : `${count} results`;
      noResults.style.display = count === 0 ? 'block' : 'none';
    } else {
      noResults.style.display = 'none';
      updateResultsCount();
      renderCardGroups(sortDesktop.value);
      if (viewMode === 'table') renderTable(sortDesktop.value, '');
    }
  }

  searchInput.addEventListener('input', updateView);

  // ---- Search mode dropdown ----
  modeTrigger.addEventListener('click', e => {
    e.stopPropagation();
    const isOpen = modeDropdown.classList.contains('open');
    modeDropdown.classList.toggle('open');
    modeTrigger.classList.toggle('open');
    modeTrigger.setAttribute('aria-expanded', !isOpen);
  });

  document.querySelectorAll('.search-mode-option').forEach(opt => {
    opt.addEventListener('click', () => {
      document.querySelectorAll('.search-mode-option').forEach(o => o.classList.remove('active'));
      opt.classList.add('active');
      modeTrigger.querySelector('.trigger-label').textContent = opt.textContent.trim().split('\n')[0];
      modeDropdown.classList.remove('open');
      modeTrigger.classList.remove('open');
      modeTrigger.setAttribute('aria-expanded', 'false');
    });
  });

  document.addEventListener('click', () => {
    modeDropdown.classList.remove('open');
    modeTrigger.classList.remove('open');
    modeTrigger.setAttribute('aria-expanded', 'false');
  });

  // ---- Filter chips ----
  function bindChips(container) {
    container.querySelectorAll('.filter-chip').forEach(chip => {
      chip.addEventListener('click', () => {
        chip.classList.toggle('selected');
        syncChipStates();
        updateActiveFilterDisplay();
      });
    });
  }

  function syncChipStates() {
    // Keep desktop and mobile chips in sync
    const desktopChips = document.querySelectorAll('.filter-area .filter-chip');
    const mobileChips = document.querySelectorAll('.filter-drawer .filter-chip');
    desktopChips.forEach((dc, i) => {
      if (mobileChips[i]) mobileChips[i].classList.toggle('selected', dc.classList.contains('selected'));
    });
    mobileChips.forEach((mc, i) => {
      if (desktopChips[i]) desktopChips[i].classList.toggle('selected', mc.classList.contains('selected'));
    });
  }

  function updateActiveFilterDisplay() {
    const selected = document.querySelectorAll('.filter-area .filter-chip.selected');
    const hasFilters = selected.length > 0;

    // Desktop
    activeFilters.style.display = hasFilters ? 'flex' : 'none';
    activeFilterChips.innerHTML = '';
    // Mobile
    mobileActiveFilters.style.display = hasFilters ? 'flex' : 'none';
    mobileActiveChips.innerHTML = '';

    selected.forEach(chip => {
      const val = chip.dataset.value || chip.textContent;
      [activeFilterChips, mobileActiveChips].forEach(container => {
        const el = document.createElement('span');
        el.className = 'active-filter-chip';
        el.innerHTML = `${val} <button class="remove" aria-label="Remove filter">&times;</button>`;
        el.querySelector('.remove').addEventListener('click', () => {
          chip.classList.remove('selected');
          syncChipStates();
          updateActiveFilterDisplay();
        });
        container.appendChild(el);
      });
    });
  }

  bindChips(document.querySelector('.filter-area'));
  bindChips(document.querySelector('.filter-drawer'));

  function clearAllFilters() {
    document.querySelectorAll('.filter-chip.selected').forEach(c => c.classList.remove('selected'));
    updateActiveFilterDisplay();
  }

  if (clearFiltersBtn) clearFiltersBtn.addEventListener('click', clearAllFilters);
  if (mobileClearBtn) mobileClearBtn.addEventListener('click', clearAllFilters);

  // ---- Mobile filter drawer ----
  if (filterDrawerTrigger) {
    filterDrawerTrigger.addEventListener('click', () => {
      filterDrawer.classList.toggle('open');
      filterDrawerTrigger.innerHTML = filterDrawer.classList.contains('open') ? 'Filters &#9652;' : 'Filters &#9662;';
    });
  }

  // ---- Card click → detail ----
  document.querySelectorAll('.recording-card[data-detail]').forEach(card => {
    card.addEventListener('click', () => {
      savedQuery = searchInput.value;
      savedScroll = window.scrollY;
      showScreen('detail');
    });
  });

  // ---- Back to library ----
  document.getElementById('back-to-library')?.addEventListener('click', e => {
    e.preventDefault();
    searchInput.value = savedQuery;
    updateView();
    showScreen('library', { restoreScroll: true });
  });

  // ---- Confirmation dialog ----
  document.querySelectorAll('[data-confirm]').forEach(trigger => {
    trigger.addEventListener('click', e => {
      e.preventDefault();
      e.stopPropagation();
      const overlay = document.querySelector('.confirm-overlay');
      if (overlay) { overlay.style.visibility = 'visible'; overlay.style.pointerEvents = 'auto'; }
    });
  });
  document.querySelectorAll('.confirm-cancel').forEach(btn => {
    btn.addEventListener('click', () => {
      const overlay = document.querySelector('.confirm-overlay');
      if (overlay) { overlay.style.visibility = 'hidden'; overlay.style.pointerEvents = 'none'; }
    });
  });

  // ---- Summary language variant tabs on Recording Detail (visual state only) ----
  document.querySelectorAll('.variant-tabs').forEach(tabs => {
    const note = tabs.parentElement.querySelector('.variant-note');
    tabs.querySelectorAll('.variant-tab').forEach(tab => {
      tab.addEventListener('click', () => {
        tabs.querySelectorAll('.variant-tab').forEach(t => {
          const isActive = t === tab;
          t.classList.toggle('active', isActive);
          t.setAttribute('aria-selected', String(isActive));
        });
        if (note) note.textContent = `Showing the ${tab.textContent.trim()} variant \u00b7 generated 1 Sep 2026, 15:12 \u00b7 current`;
      });
    });
  });

  // ---- Copy buttons (best-effort clipboard for the prototype) ----
  document.querySelectorAll('[data-copy]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const target = document.querySelector(btn.dataset.copy);
      if (!target || !navigator.clipboard) return;
      try {
        await navigator.clipboard.writeText(target.innerText.trim());
        const original = btn.textContent;
        btn.textContent = 'Copied';
        window.setTimeout(() => { btn.textContent = original; }, 1500);
      } catch (_) {}
    });
  });

  // ---- Keyboard shortcut ----
  document.addEventListener('keydown', e => {
    const anyDialogOpen = document.querySelector('.dialog-overlay:not([hidden])') !== null;
    if (e.key === '/' && !anyDialogOpen && document.activeElement !== searchInput) {
      e.preventDefault();
      searchInput.focus();
      if (currentScreen !== 'library') showScreen('library');
    }
    if (e.key === 'Escape') {
      searchInput.blur();
      modeDropdown.classList.remove('open');
      modeTrigger.classList.remove('open');
    }
  });

  // ---- Prototype dialogs: shared helper (Processing actions + Tag editor) ----
  function focusableIn(el) {
    const sel = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
    return Array.from(el.querySelectorAll(sel)).filter(n => n.offsetParent !== null);
  }

  function setupDialog(overlay, opts = {}) {
    const trigger = opts.trigger ? document.getElementById(opts.trigger) : null;
    const cancelSel = opts.cancelSelector || '.dialog-cancel';

    function open() {
      overlay._prevFocus = document.activeElement;
      overlay.hidden = false;
      const list = focusableIn(overlay);
      (list[0] || overlay).focus();
      if (opts.onOpen) opts.onOpen();
    }
    function close() {
      overlay.hidden = true;
      const prev = overlay._prevFocus;
      if (prev && prev.isConnected && typeof prev.focus === 'function') prev.focus();
      if (opts.onClose) opts.onClose();
    }
    if (trigger) trigger.addEventListener('click', e => { e.preventDefault(); open(); });
    overlay.addEventListener('click', e => {
      if (e.target === overlay || e.target.closest(cancelSel)) close();
    });
    overlay.addEventListener('keydown', e => {
      if (e.key === 'Escape') { e.preventDefault(); close(); return; }
      if (e.key === 'Tab') {
        const list = focusableIn(overlay);
        if (!list.length) return;
        const first = list[0], last = list[list.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    });
    return { open, close };
  }

  // ---- Processing actions dialog (prototype proposal) ----
  const processingOverlay = document.getElementById('processing-actions-dialog');
  const processingRadios = Array.from(document.querySelectorAll('input[name="routing-profile"]'));
  const PROCESSING_PROFILES = {
    european: { name: 'european', current: true },
    cantonese: { name: 'cantonese', current: false },
    mandarin: { name: 'mandarin', current: false },
  };

  function processingView(name) { return document.getElementById('pa-' + name); }

  function showProcessingView(name) {
    ['chooser', 'confirm', 'done'].forEach(v => { processingView(v).hidden = v !== name; });
    const heading = processingView(name).querySelector('h3');
    if (heading && heading.id) processingOverlay.setAttribute('aria-labelledby', heading.id);
    const first = focusableIn(processingView(name))[0];
    if (first) first.focus();
  }

  setupDialog(processingOverlay, {
    trigger: 'processing-actions-trigger',
    onOpen() {
      showProcessingView('chooser');
      processingRadios.forEach(r => { r.checked = r.value === 'european'; });
    },
  });

  document.getElementById('processing-continue').addEventListener('click', () => {
    const selected = processingRadios.find(r => r.checked);
    const profile = PROCESSING_PROFILES[selected.value];
    const confirmTitle = document.getElementById('processing-confirm-title');
    const confirmLead = document.getElementById('processing-confirm-lead');
    const bullets = document.getElementById('processing-confirm-bullets');
    if (profile.current) {
      confirmTitle.textContent = 'Keep current routing?';
      confirmLead.textContent = `The ${profile.name} routing is already confirmed. Keeping it causes no processing change and no retranscription.`;
      bullets.innerHTML = '<li>No retranscription is scheduled.</li><li>The current transcript and full history stay untouched.</li>';
    } else {
      confirmTitle.textContent = 'Reroute and retranscribe?';
      confirmLead.textContent = `Switch routing to the ${profile.name} profile and schedule a retranscription.`;
      bullets.innerHTML = '<li>A new transcript version is created only after the retranscription succeeds.</li><li>The current transcript and the full history remain preserved.</li>';
    }
    showProcessingView('confirm');
  });

  document.getElementById('processing-confirm-btn').addEventListener('click', () => {
    const selected = processingRadios.find(r => r.checked);
    const profile = PROCESSING_PROFILES[selected.value];
    document.getElementById('processing-done-lead').textContent = profile.current
      ? `Routing remains confirmed as ${profile.name}. No processing change.`
      : `Reroute to ${profile.name} with retranscription recorded as confirmed.`;
    showProcessingView('done');
  });

  // ---- Tag editor dialog (prototype proposal) ----
  const tagOverlay = document.getElementById('tag-editor-dialog');
  const tagFilter = document.getElementById('tag-filter');
  const tagOptions = document.getElementById('tag-options');
  const tagOptionsEmpty = document.getElementById('tag-options-empty');
  const customTagSection = document.getElementById('custom-tag-section');
  const customTagOptions = document.getElementById('custom-tag-options');
  const newTagInput = document.getElementById('new-tag-input');
  const createTagBtn = document.getElementById('create-tag-btn');
  const tagStatus = document.getElementById('tag-status');
  const detailTagChips = document.getElementById('detail-tag-chips');

  // Configured tags are configuration-owned; assigned state mirrors the detail row.
  // `base` keeps the confirmed/suggested distinction when a previously assigned
  // tag is re-added; a fresh assignment falls back to plain 'assigned'.
  const configuredTags = [
    { name: 'Work', base: 'confirmed', state: 'confirmed' },
    { name: 'Meeting', base: 'suggested', state: 'suggested' },
    { name: 'Research', base: 'assigned', state: 'assigned' },
    { name: 'Personal', base: 'unassigned', state: 'unassigned' },
    { name: 'Language', base: 'unassigned', state: 'unassigned' },
    { name: 'Idea', base: 'unassigned', state: 'unassigned' },
  ];
  const TAG_STATE_CLASS = { confirmed: 'tag-confirmed', suggested: 'tag-suggested', assigned: '', unassigned: '' };
  let customTags = [];

  // Concise visible labels: state is only surfaced inline for suggested tags;
  // the full state stays available to assistive technology via aria-label.
  function tagLabelText(t) {
    return t.state === 'suggested' ? t.name + ' (suggested)' : t.name;
  }
  function tagAriaLabel(t) {
    const stateWord = { confirmed: 'confirmed', suggested: 'suggested', assigned: 'assigned' }[t.state];
    return stateWord ? t.name + ', ' + stateWord : t.name + ', not assigned';
  }

  function tagChipElement(name, stateClass) {
    const chip = document.createElement('span');
    chip.className = 'tag ' + stateClass;
    chip.textContent = name;
    return chip;
  }

  function renderDetailTagRow() {
    detailTagChips.innerHTML = '';
    configuredTags.filter(t => t.state !== 'unassigned').forEach(t => {
      detailTagChips.appendChild(tagChipElement(t.name, TAG_STATE_CLASS[t.state]));
    });
    customTags.forEach(name => {
      detailTagChips.appendChild(tagChipElement(name, 'tag-manual'));
    });
  }

  function renderTagOptions() {
    const q = tagFilter.value.trim().toLowerCase();
    const matched = configuredTags.filter(t => !q || t.name.toLowerCase().includes(q));
    tagOptions.innerHTML = '';
    matched.forEach(t => {
      const li = document.createElement('li');
      const label = document.createElement('label');
      label.className = 'tag-option';
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.value = t.name;
      input.checked = t.state !== 'unassigned';
      input.setAttribute('aria-label', tagAriaLabel(t));
      label.appendChild(input);
      const chip = document.createElement('span');
      chip.className = 'tag ' + TAG_STATE_CLASS[t.state];
      chip.textContent = tagLabelText(t);
      label.appendChild(chip);
      li.appendChild(label);
      label.addEventListener('change', () => {
        t.state = input.checked ? (t.base === 'unassigned' ? 'assigned' : t.base) : 'unassigned';
        chip.textContent = tagLabelText(t);
        chip.className = 'tag ' + TAG_STATE_CLASS[t.state];
        input.setAttribute('aria-label', tagAriaLabel(t));
        renderDetailTagRow();
      });
      tagOptions.appendChild(li);
    });
    tagOptionsEmpty.hidden = matched.length > 0;
  }

  function renderCustomTags() {
    customTagSection.hidden = customTags.length === 0;
    customTagOptions.innerHTML = '';
    customTags.forEach(name => {
      const li = document.createElement('li');
      const chip = document.createElement('span');
      chip.className = 'tag tag-manual';
      chip.textContent = name;
      li.appendChild(chip);
      customTagOptions.appendChild(li);
    });
  }

  function setTagStatus(text, kind) {
    tagStatus.textContent = text;
    tagStatus.classList.toggle('error', kind === 'error');
    tagStatus.classList.toggle('ok', kind === 'ok');
  }

  function createCustomTag() {
    const name = newTagInput.value.trim();
    if (!name) { setTagStatus('Enter a tag name first.', 'error'); return; }
    const dupConfigured = configuredTags.some(t => t.name.toLowerCase() === name.toLowerCase());
    const dupCustom = customTags.some(t => t.toLowerCase() === name.toLowerCase());
    if (dupConfigured || dupCustom) { setTagStatus(`"${name}" already exists as a tag.`, 'error'); return; }
    customTags.push(name);
    newTagInput.value = '';
    renderCustomTags();
    renderDetailTagRow();
    setTagStatus(`Added custom tag "${name}" (prototype only).`, 'ok');
  }

  createTagBtn.addEventListener('click', createCustomTag);
  newTagInput.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); createCustomTag(); } });
  tagFilter.addEventListener('input', renderTagOptions);

  setupDialog(tagOverlay, {
    trigger: 'add-tag-trigger',
    onOpen() {
      tagFilter.value = '';
      renderTagOptions();
      renderCustomTags();
      setTagStatus('', '');
    },
  });

  // ---- Step 6 prototype: transcript-page trim & split editing (segmented versions) ----
  // The Transcript screen is the only 6.1 editor location. Fictional segment
  // rows are read from the DOM and small scissors controls are injected on the
  // inter-segment divider lines between them. Canonical boundaries are half-open segment
  // ordinals [start, end_exclusive); the UI shows timestamps and inclusive
  // segment labels. The edit toggle only reveals the scissors (no panel, no
  // dialog); clicking a scissors opens a small action dialog offering
  // Split here / Crop from here / Crop to here / Remove split. Cropped rows are
  // hidden in the staged view. Save commits one immutable revision holding the
  // crop range plus optional splits/topics (crop-only = zero topic sections).
  const transcriptDoc = document.getElementById('transcript-doc');
  const editToggle = document.getElementById('transcript-edit-toggle');
  const editModeBar = document.getElementById('edit-mode-bar');
  const editStatus = document.getElementById('edit-status');
  const editClearCrop = document.getElementById('edit-clear-crop');
  const editReset = document.getElementById('edit-reset');
  const editSave = document.getElementById('edit-save');
  const cropViewBar = document.getElementById('crop-view-bar');
  const cropViewMsg = document.getElementById('crop-view-msg');
  const cropViewToggle = document.getElementById('crop-view-toggle');
  const segmentedHistoryBody = document.getElementById('segmented-history-body');
  const segmentedHistoryEmpty = document.getElementById('segmented-history-empty');

  const segmentEls = Array.from(transcriptDoc.querySelectorAll('.transcript-segment'));
  const segmentTimes = segmentEls.map(el => {
    const t = el.querySelector('.segment-time');
    return t ? t.textContent : '';
  });
  const SEGMENT_COUNT = segmentEls.length;
  const END_BOUNDARY = SEGMENT_COUNT;
  const scissorsButtons = [];
  const topicMarkers = [];
  let editMode = false;
  let viewFullTranscript = false;
  let nextRevision = 1;

  function makeState() {
    return { start: 0, end: END_BOUNDARY, splits: [], titles: new Map() };
  }
  function cloneState(s) {
    return { start: s.start, end: s.end, splits: s.splits.slice(), titles: new Map(s.titles) };
  }

  // Baseline: a clean, full transcript (no trim, no splits) so the reference
  // layout stays clean until the user edits. Confirmed saves create fictional
  // revisions; the History screen owns the revision list.
  let activeState = makeState();
  let staged = cloneState(activeState);

  function boundaryTimeText(b) {
    if (b <= 0) return segmentTimes[0] || '00:00';
    if (b >= END_BOUNDARY) return 'end';
    return segmentTimes[b];
  }
  function boundaryLabel(b) {
    if (b <= 0) return 'start · ' + boundaryTimeText(0);
    if (b >= END_BOUNDARY) return 'end · ' + boundaryTimeText(END_BOUNDARY);
    return 'before segment ' + b + ' · ' + boundaryTimeText(b);
  }
  function inclusiveRangeText(start, end) {
    if (end - start <= 1) return 'segment ' + start;
    return 'segments ' + start + '–' + (end - 1);
  }
  function keyOf(sec) { return sec.start + '-' + sec.end; }

  // Sections are materialized only from the crop range + split markers. Zero
  // splits means zero sections (crop-only); N splits inside the range yield N+1
  // sections that exactly partition [start, end_exclusive).
  function deriveSections(start, end, splits) {
    const interior = splits.filter(s => s > start && s < end).slice().sort((a, b) => a - b);
    if (interior.length === 0) return [];
    const bounds = [start].concat(interior, [end]);
    const out = [];
    for (let i = 0; i < bounds.length - 1; i++) out.push({ start: bounds[i], end: bounds[i + 1] });
    return out;
  }

  // ---- Scissors controls: one compact button per inter-segment divider line ----
  function scissorsIcon() {
    const NS = 'http://www.w3.org/2000/svg';
    function el(name, attrs) {
      const node = document.createElementNS(NS, name);
      for (const k in attrs) node.setAttribute(k, attrs[k]);
      return node;
    }
    const svg = el('svg', {
      viewBox: '0 0 24 24', width: '12', height: '12',
      fill: 'none', stroke: 'currentColor', 'stroke-width': '2',
      'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true',
    });
    svg.appendChild(el('circle', { cx: '6', cy: '6', r: '3' }));
    svg.appendChild(el('circle', { cx: '6', cy: '18', r: '3' }));
    svg.appendChild(el('line', { x1: '20', y1: '4', x2: '8.12', y2: '15.88' }));
    svg.appendChild(el('line', { x1: '14.47', y1: '14.48', x2: '20', y2: '20' }));
    svg.appendChild(el('line', { x1: '8.12', y1: '8.12', x2: '12', y2: '12' }));
    return svg;
  }

  function makeScissors(boundary) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'boundary-scissors';
    btn.dataset.boundary = String(boundary);
    btn.setAttribute('aria-haspopup', 'dialog');
    btn.setAttribute('aria-label', 'Divider ' + boundaryLabel(boundary) + '. Actions: split here, crop from here, crop to here.');
    btn.appendChild(scissorsIcon());
    btn.addEventListener('click', () => { if (editMode) openBoundaryDialog(boundary); });
    return btn;
  }

  function buildScissors() {
    segmentEls.forEach((seg, i) => {
      if (i === 0) return;
      const sc = makeScissors(i);
      transcriptDoc.insertBefore(sc, seg);
      scissorsButtons.push(sc);
    });
  }

  // ---- Action validity (half-open canonical semantics) ----
  // Splits are only valid strictly inside the retained range (never at the
  // endpoints); crops must leave at least one segment; duplicates rejected.
  function insideRange(b) { return b > staged.start && b < staged.end; }
  function canCropFrom(b) { return insideRange(b); }
  function canCropTo(b) { return insideRange(b); }
  function canSplit(b) { return insideRange(b) && !staged.splits.includes(b); }
  function canRemoveSplit(b) { return staged.splits.includes(b); }

  // ---- Boundary action dialog (opened only by clicking a scissors) ----
  const boundaryDialog = setupDialog(document.getElementById('boundary-action-dialog'), {});
  let selectedBoundary = null;

  function openBoundaryDialog(b) {
    selectedBoundary = b;
    const title = document.getElementById('boundary-action-title');
    const summary = document.getElementById('boundary-action-summary');
    const note = document.getElementById('boundary-action-note');
    const splitBtn = document.getElementById('boundary-split');
    const cropFromBtn = document.getElementById('boundary-crop-from');
    const cropToBtn = document.getElementById('boundary-crop-to');
    const removeBtn = document.getElementById('boundary-remove-split');
    title.textContent = 'Boundary · ' + boundaryLabel(b);
    summary.textContent = 'Choose an action for the divider ' + boundaryLabel(b) + '.';
    splitBtn.disabled = !canSplit(b);
    cropFromBtn.disabled = !canCropFrom(b);
    cropToBtn.disabled = !canCropTo(b);
    removeBtn.disabled = !canRemoveSplit(b);
    removeBtn.hidden = !canRemoveSplit(b);
    if (b <= staged.start) {
      note.textContent = 'This is the start of the retained range — cropping or splitting here is not possible.';
    } else if (b >= staged.end) {
      note.textContent = 'This is the end of the retained range — cropping or splitting here is not possible.';
    } else if (staged.splits.includes(b)) {
      note.textContent = 'This boundary is already a split — Remove split reverses it.';
    } else {
      note.textContent = 'Split here divides the transcript into named sections; crops hide the content above or below this boundary from the staged view.';
    }
    boundaryDialog.open();
  }

  // Preserve topic titles: exact range keys survive unchanged; splitting an
  // existing section keeps the title on the left-hand part (the new right part
  // starts blank and needs a topic). Brand-new ranges start blank.
  function preserveTitles(prevSections) {
    const next = deriveSections(staged.start, staged.end, staged.splits);
    const titles = new Map();
    next.forEach(sec => {
      const key = keyOf(sec);
      let sourceKey = null;
      if (staged.titles.has(key)) {
        sourceKey = key;
      } else if (prevSections) {
        const sameStart = prevSections.find(p => p.start === sec.start && sec.end <= p.end);
        if (sameStart && staged.titles.has(keyOf(sameStart))) sourceKey = keyOf(sameStart);
      }
      titles.set(key, sourceKey ? staged.titles.get(sourceKey) : '');
    });
    return titles;
  }

  function applyCropFrom() {
    if (selectedBoundary === null || !canCropFrom(selectedBoundary)) return;
    const prevSections = deriveSections(staged.start, staged.end, staged.splits);
    staged.start = selectedBoundary;
    staged.splits = staged.splits.filter(s => s > staged.start && s < staged.end);
    staged.titles = preserveTitles(prevSections);
    renderAll();
  }
  function applyCropTo() {
    if (selectedBoundary === null || !canCropTo(selectedBoundary)) return;
    const prevSections = deriveSections(staged.start, staged.end, staged.splits);
    staged.end = selectedBoundary;
    staged.splits = staged.splits.filter(s => s > staged.start && s < staged.end);
    staged.titles = preserveTitles(prevSections);
    renderAll();
  }
  function applySplit() {
    if (selectedBoundary === null || !canSplit(selectedBoundary)) return;
    const prevSections = deriveSections(staged.start, staged.end, staged.splits);
    staged.splits = staged.splits.concat(selectedBoundary).sort((a, b) => a - b);
    staged.titles = preserveTitles(prevSections);
    renderAll();
  }
  function applyRemoveSplit() {
    if (selectedBoundary === null || !canRemoveSplit(selectedBoundary)) return;
    const prevSections = deriveSections(staged.start, staged.end, staged.splits);
    staged.splits = staged.splits.filter(s => s !== selectedBoundary);
    staged.titles = preserveTitles(prevSections);
    renderAll();
  }

  document.getElementById('boundary-split').addEventListener('click', () => { applySplit(); boundaryDialog.close(); });
  document.getElementById('boundary-crop-from').addEventListener('click', () => { applyCropFrom(); boundaryDialog.close(); });
  document.getElementById('boundary-crop-to').addEventListener('click', () => { applyCropTo(); boundaryDialog.close(); });
  document.getElementById('boundary-remove-split').addEventListener('click', () => { applyRemoveSplit(); boundaryDialog.close(); });

  // ---- Inline topic controls (rendered in the transcript flow once a split exists) ----
  function makeTopicInput(sec, index) {
    const key = keyOf(sec);
    const row = document.createElement('div');
    row.className = 'section-topic-inline';

    const label = document.createElement('label');
    label.className = 'section-topic-label';
    label.setAttribute('for', 'topic-' + index);
    label.textContent = 'Topic ' + (index + 1);

    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'topic-input';
    input.id = 'topic-' + index;
    input.maxLength = 255;
    input.value = staged.titles.get(key) || '';
    input.placeholder = 'Name this section';
    input.setAttribute('aria-label', 'Topic ' + (index + 1) + ' for the section starting at ' + boundaryLabel(sec.start));

    input.addEventListener('input', () => {
      staged.titles.set(key, input.value);
      updateStatus();
    });

    row.appendChild(label);
    row.appendChild(input);
    return row;
  }

  function makeTopicLabel(sec, index) {
    const row = document.createElement('div');
    row.className = 'section-topic-inline section-topic-readonly';
    const label = document.createElement('span');
    label.className = 'section-topic-label';
    label.textContent = 'Topic ' + (index + 1);
    const value = document.createElement('span');
    value.className = 'section-topic-value';
    value.textContent = activeState.titles.get(keyOf(sec)) || '';
    row.appendChild(label);
    row.appendChild(value);
    return row;
  }

  function clearTopicMarkers() {
    topicMarkers.forEach(m => m.remove());
    topicMarkers.length = 0;
  }

  // ---- Rendering ----
  function renderTranscriptFlow() {
    const range = editMode ? staged : activeState;
    const showFull = !editMode && viewFullTranscript;

    // Cropped rows are hidden (not dimmed) in the staged edited view and in the
    // saved cropped working view; the full transcript is one toggle away.
    segmentEls.forEach((seg, i) => {
      const hiddenRow = showFull ? false : (i < range.start || i >= range.end);
      seg.hidden = hiddenRow;
    });

    // Scissors are visible only while editing and only on dividers that touch
    // the retained range (edge dividers stay visible as crop markers).
    scissorsButtons.forEach(btn => {
      const b = parseInt(btn.dataset.boundary, 10);
      const isSplit = editMode && staged.splits.includes(b);
      btn.hidden = !editMode || b < staged.start || b > staged.end;
      btn.classList.toggle('is-split', isSplit);
      btn.setAttribute('aria-label', 'Divider ' + boundaryLabel(b) +
        (isSplit ? ' — split marker. Actions: remove split, crop from here, crop to here.' : '. Actions: split here, crop from here, crop to here.'));
    });

    // Inline topic labels/inputs in the transcript flow, only when splits exist.
    clearTopicMarkers();
    const sections = editMode
      ? deriveSections(staged.start, staged.end, staged.splits)
      : (showFull ? [] : deriveSections(activeState.start, activeState.end, activeState.splits));
    if (sections.length > 0) {
      sections.forEach((sec, i) => {
        const marker = editMode ? makeTopicInput(sec, i) : makeTopicLabel(sec, i);
        segmentEls[sec.start].before(marker);
        topicMarkers.push(marker);
      });
    }

    // Compact saved-working-view bar (non-edit) with the full-transcript toggle.
    const hasSavedCrop = activeState.start !== 0 || activeState.end !== END_BOUNDARY;
    cropViewBar.hidden = editMode || !hasSavedCrop;
    if (!editMode && hasSavedCrop) {
      const hiddenCount = activeState.start + (SEGMENT_COUNT - activeState.end);
      const noun = hiddenCount === 1 ? 'line' : 'lines';
      cropViewMsg.textContent = showFull
        ? 'Showing the full transcript — the saved working view hides ' + hiddenCount + ' ' + noun + '.'
        : 'Saved working view (revision ' + (nextRevision - 1) + ') — ' + hiddenCount + ' ' + noun + ' hidden. The full transcript and source audio remain available.';
      cropViewToggle.textContent = showFull ? 'Show saved working view' : 'Show full transcript';
    }
  }

  // ---- Validation / staged status ----
  function validateStaged() {
    if (staged.start >= staged.end) {
      return { ok: false, message: 'The crop must leave at least one segment.' };
    }
    // Crop-only (no splits) is valid and has zero topic sections.
    if (staged.splits.length === 0) return { ok: true, message: '' };
    const sections = deriveSections(staged.start, staged.end, staged.splits);
    for (const sec of sections) {
      const title = staged.titles.get(keyOf(sec)) || '';
      if (title.trim() === '') {
        return { ok: false, message: 'Every section needs a topic — give each resulting section a name.' };
      }
      if (/[\u0000-\u001f\u007f]/.test(title)) {
        return { ok: false, message: 'A topic contains an invalid character — control characters and newlines are not allowed.' };
      }
      if (title.length > 255) {
        return { ok: false, message: 'A topic is too long — at most 255 characters.' };
      }
    }
    return { ok: true, message: '' };
  }

  function isDirty() {
    if (staged.start !== activeState.start || staged.end !== activeState.end) return true;
    if (staged.splits.join(',') !== activeState.splits.join(',')) return true;
    const sections = deriveSections(staged.start, staged.end, staged.splits);
    for (const sec of sections) {
      if ((staged.titles.get(keyOf(sec)) || '') !== (activeState.titles.get(keyOf(sec)) || '')) return true;
    }
    return false;
  }

  function updateStatus() {
    const dirty = isDirty();
    const valid = validateStaged();
    const above = staged.start;
    const below = SEGMENT_COUNT - staged.end;
    const sections = deriveSections(staged.start, staged.end, staged.splits);
    const parts = [];
    if (above > 0) parts.push(above + ' line' + (above === 1 ? '' : 's') + ' cropped above');
    if (below > 0) parts.push(below + ' line' + (below === 1 ? '' : 's') + ' cropped below');
    if (sections.length > 1) parts.push(sections.length + ' sections from ' + staged.splits.length + ' split' + (staged.splits.length === 1 ? '' : 's'));
    let msg;
    if (!dirty) {
      msg = 'No changes yet.';
    } else {
      msg = (parts.length ? parts.join(' · ') : 'Staged changes') + ' — not saved yet.';
    }
    if (!valid.ok) msg = msg + ' ' + valid.message;
    editStatus.textContent = msg;
    editSave.disabled = !dirty || !valid.ok;
    editReset.disabled = !dirty;
    editClearCrop.disabled = staged.start === 0 && staged.end === END_BOUNDARY;
  }

  function renderAll() {
    renderTranscriptFlow();
    updateStatus();
  }

  // ---- Edit mode toggle (only reveals scissors/controls; opens no dialog) ----
  function setEditMode(on) {
    editMode = on;
    editModeBar.hidden = !on;
    editToggle.setAttribute('aria-expanded', String(on));
    editToggle.textContent = on ? 'Done editing' : 'Edit trim & splits';
    if (!on) boundaryDialog.close();
    if (on) {
      staged = cloneState(activeState);
      viewFullTranscript = false;
    }
    renderAll();
  }
  editToggle.addEventListener('click', () => setEditMode(!editMode));

  // ---- Compact controls: Clear crop / Reset / Save ----
  editClearCrop.addEventListener('click', () => {
    if (staged.start === 0 && staged.end === END_BOUNDARY) return;
    staged.start = 0;
    staged.end = END_BOUNDARY;
    // Splits already made by the user are retained (sections are created by
    // splits, never by the crop itself); a crop-only state clears to zero
    // sections with nothing to name.
    staged.splits = staged.splits.filter(s => s > 0 && s < END_BOUNDARY);
    staged.titles = preserveTitles();
    renderAll();
  });

  editReset.addEventListener('click', () => {
    staged = cloneState(activeState);
    renderAll();
  });

  // ---- Save: one confirmed immutable revision ----
  function updateConfirmSummary() {
    const summary = document.getElementById('segmented-confirm-summary');
    const sections = deriveSections(staged.start, staged.end, staged.splits);
    const parts = [];
    if (staged.start > 0) parts.push('crops ' + staged.start + ' line' + (staged.start === 1 ? '' : 's') + ' above');
    if (staged.end < END_BOUNDARY) parts.push('crops ' + (SEGMENT_COUNT - staged.end) + ' line' + (SEGMENT_COUNT - staged.end === 1 ? '' : 's') + ' below');
    const sectionPart = sections.length === 0
      ? 'no topic sections (crop only)'
      : sections.length + ' topic section' + (sections.length === 1 ? '' : 's') + ': ' +
        sections.map(sec => '“' + (staged.titles.get(keyOf(sec)) || '') + '”').join(', ');
    summary.textContent = 'Working range: ' + inclusiveRangeText(staged.start, staged.end) + '.' +
      (parts.length ? ' ' + parts.join(', ') + '.' : '') + ' ' + sectionPart + '.';
  }

  const segConfirmDialog = setupDialog(document.getElementById('segmented-version-confirm-dialog'), {});

  editSave.addEventListener('click', () => {
    if (!validateStaged().ok) return;
    updateConfirmSummary();
    segConfirmDialog.open();
  });

  function commitRevision() {
    const revision = nextRevision++;
    activeState = cloneState(staged);
    staged = cloneState(activeState);
    viewFullTranscript = false;
    // New split-created sections start without summaries/tags (6.2); nothing
    // is carried forward.
    if (segmentedHistoryEmpty) segmentedHistoryEmpty.remove();
    Array.from(segmentedHistoryBody.rows).forEach(existingRow => {
      const statusCell = existingRow.cells[4];
      if (statusCell && statusCell.textContent === 'active') statusCell.textContent = 'superseded';
    });
    const row = document.createElement('tr');
    const sections = deriveSections(activeState.start, activeState.end, activeState.splits);
    const cells = [
      String(revision),
      'just now',
      inclusiveRangeText(activeState.start, activeState.end),
      sections.length === 0 ? 'none (crop only)' : sections.length + ' (splits ' + activeState.splits.map(String).join(', ') + ')',
      'active',
    ];
    cells.forEach(text => {
      const td = document.createElement('td');
      td.textContent = text;
      row.appendChild(td);
    });
    segmentedHistoryBody.prepend(row);
    setEditMode(false);
  }

  document.getElementById('segmented-confirm-btn').addEventListener('click', () => {
    if (!validateStaged().ok) { segConfirmDialog.close(); return; }
    commitRevision();
    segConfirmDialog.close();
  });

  cropViewToggle.addEventListener('click', () => {
    viewFullTranscript = !viewFullTranscript;
    renderAll();
  });
  // ---- Init ----
  buildScissors();
  setEditMode(false);
  updateResultsCount();
  renderCardGroups(sortDesktop.value);
  renderDetailTagRow();
});
