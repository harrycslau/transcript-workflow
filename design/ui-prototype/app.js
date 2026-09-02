/* ============================================================
   Brain UI Prototype — Interactions (v3)
   ============================================================
   DESIGN PROTOTYPE, not production code.
   View toggle, sort-aware grouping, localStorage, table render.
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

  document.querySelectorAll('.topbar-btn[data-screen], .back-btn[data-screen], .topbar-brand[data-screen]').forEach(el => {
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

  // ---- Detail page tabs ----
  document.querySelectorAll('.detail-nav-tab[data-panel]').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.detail-nav-tab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.panel[data-panel]').forEach(p => p.classList.remove('active'));
      tab.classList.add('active');
      document.querySelector(`.panel[data-panel="${tab.dataset.panel}"]`)?.classList.add('active');
    });
  });

  // ---- Language tabs ----
  document.querySelectorAll('.lang-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      tab.closest('.lang-tabs').querySelectorAll('.lang-tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
    });
  });

  // ---- Provenance toggle ----
  document.querySelectorAll('.provenance-toggle').forEach(btn => {
    btn.addEventListener('click', () => {
      btn.classList.toggle('open');
      btn.nextElementSibling?.classList.toggle('open');
    });
  });

  // ---- Copy button ----
  document.querySelectorAll('.copy-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const orig = btn.textContent;
      btn.textContent = 'Copied!';
      btn.classList.add('copied');
      setTimeout(() => { btn.textContent = orig; btn.classList.remove('copied'); }, 2000);
    });
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

  // ---- Keyboard shortcut ----
  document.addEventListener('keydown', e => {
    if (e.key === '/' && document.activeElement !== searchInput) {
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

  // ---- Init ----
  updateResultsCount();
  renderCardGroups(sortDesktop.value);
});
