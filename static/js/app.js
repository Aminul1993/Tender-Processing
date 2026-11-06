/*
 * Tender page: loads the tenders, handles verdict tabs / search / sorting / selection, queues tenders for
 * processing and polls /api/progress to update the run panel and each row until the job finishes.
 * All server text is inserted with textContent; only sanitised markdown uses innerHTML.
 */
(() => {
  'use strict';

  const POLL_MS = 1500;
  const POLL_RETRY_MS = 5000;
  const URGENT_DAYS = 7;   // closing within this many days: red, and counted in the summary line
  const NEAR_DAYS = 20;    // closing within this many days: amber
  const BASE_TITLE = document.title;
  const DISMISSED_KEY = 'tender-processing.dismissed-job';
  const NOTIFY_KEY = 'tender-processing.notify';
  const FILTERS_KEY = 'tender-processing.search-filters';
  const PAGE_SIZE_KEY = 'tender-processing.page-size';
  const PAGE_SIZES = [20, 50, 100, 500];  // tenders a page may hold, as /api/tenders takes them
  // How the quantity, value and EMD filters compare, by the names the API takes
  const COMPARISONS = [['gt', '>'], ['gte', '≥'], ['lt', '<'], ['lte', '≤'], ['eq', '='], ['between', 'Between']];
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  // How each verdict filter reads and looks. The API keeps "Not Eligible" / "Requires Review".
  const VERDICTS = {
    'Eligible': {label: 'Eligible', tone: 'eligible'},
    'Requires Review': {label: 'Needs review', tone: 'review'},
    'Not Eligible': {label: 'Not eligible', tone: 'not'},
    'failed': {label: 'Failed', tone: 'failed'},
  };
  const OUTCOME_ORDER = ['Eligible', 'Requires Review', 'Not Eligible', 'failed'];
  // Status column of the criteria table: class and sort rank, so Not met rows come first
  const STATUS_STYLES = [
    [/\bnot\s+met\b/, 'status-not-met', 0],
    [/\bcannot\s+determine\b/, 'status-unknown', 1],
    [/\bmet\b/, 'status-met', 2],
  ];
  const DECISIONS = {
    shortlisted: {label: 'Shortlisted', icon: 'bi-bookmark-check-fill', cls: 'decision-shortlisted'},
    dropped: {label: 'Dropped', icon: 'bi-x-circle', cls: 'decision-dropped'},
  };
  const TOAST_TONES = {success: 'eligible', warning: 'review', danger: 'not'};
  // Words a title-cased name keeps in lower case, unless one starts it
  const SMALL_WORDS = new Set(['a', 'an', 'and', 'at', 'by', 'for', 'in', 'of', 'on', 'or', 'the', 'to', 'with']);

  const $ = (id) => document.getElementById(id);
  const els = {
    pageSummary: $('page-summary'),
    filters: $('verdict-filters'),
    runPanel: $('run-panel'),
    runTitle: $('run-title'),
    runDetail: $('run-detail'),
    runStage: $('run-stage'),
    runEta: $('run-eta'),
    runProgress: $('run-progress'),
    runBar: $('run-bar'),
    runAnnounce: $('run-announce'),
    stopBtn: $('stop-btn'),
    notifyBtn: $('notify-btn'),
    runDismiss: $('run-dismiss'),
    search: $('search'),
    orgFilter: $('filter-organisation'),
    refreshBtn: $('refresh-btn'),
    filtersBtn: $('filters-btn'),
    filtersCount: $('filters-count'),
    filtersForm: $('search-filters'),
    filtersClear: $('filters-clear'),
    filtersApply: $('filters-apply'),
    decisionFilter: $('filter-decision'),
    closingFilter: $('filter-closing'),
    processBtn: $('process-btn'),
    processLabel: $('process-label'),
    selectedCount: $('selected-count'),
    body: $('tender-body'),
    selectAll: $('select-all'),
    sortButtons: [...document.querySelectorAll('.sort-button')],
    tenderPanel: $('tender-panel'),
    table: $('tender-table'),
    tableFoot: $('table-foot'),
    summary: $('table-summary'),
    tableHint: $('table-hint'),
    pager: $('pager'),
    pagePrev: $('page-prev'),
    pageNext: $('page-next'),
    pageStatus: $('page-status'),
    pageSize: $('page-size'),
    selectionBar: $('selection-bar'),
    selectionText: $('selection-text'),
    clearSelection: $('clear-selection'),
    barProcess: $('bar-process'),
    regenerateToggle: $('regenerate-toggle'),
    modal: $('details-modal'),
    modalTitle: $('details-title'),
    modalFacts: $('details-facts'),
    modalSummary: $('details-summary'),
    modalDeep: $('details-deep'),
    analysisSection: $('analysis-section'),
    modalContent: $('details-content'),
    modalFiles: $('details-files'),
    modalCriteria: $('details-criteria'),
    modalRun: $('details-run'),
    shortlistBtn: $('shortlist-btn'),
    dropBtn: $('drop-btn'),
    copyBtn: $('copy-details'),
    prevBtn: $('prev-btn'),
    nextBtn: $('next-btn'),
    toasts: $('toasts'),
    companyName: $('company-name'),
    companyEdit: $('company-edit'),
    companyModal: $('company-modal'),
    companyForm: $('company-form'),
    companySave: $('company-save'),
  };
  const detailsModal = new bootstrap.Modal(els.modal);
  const companyModal = new bootstrap.Modal(els.companyModal);
  // The organisation filter with a search box in its list, like select2. Tom Select keeps the <select>'s value
  // and fires its change event, so the rest of the page reads els.orgFilter as before. Without the library the
  // plain select still works.
  const orgPicker = window.TomSelect ? new TomSelect(els.orgFilter, {
    plugins: ['dropdown_input'],
    allowEmptyOption: true,  // "All organisations"
    maxOptions: null,
    dropdownParent: 'body',
    dropdownClass: 'ts-dropdown org-dropdown',
    placeholder: 'Search organisations',
    // Matches stay in alphabetical order, as in select2; naming $score stops it being sorted first
    sortField: [{field: '$order'}, {field: '$score'}],
    render: {no_results: () => '<div class="no-results">No organisation matches</div>'},
  }) : null;
  if (orgPicker) {
    // Tom Select does not carry the select's aria-label over, and a placeholder is not a name
    orgPicker.control.setAttribute('aria-label', els.orgFilter.getAttribute('aria-label'));
    orgPicker.control_input.setAttribute('aria-label', 'Search organisations');
  }

  const state = {
    tenders: [],              // rows of the page shown, from /api/tenders, in API order
    seen: new Map(),          // tcno -> tender, for every tender listed on any page, since a selection spans pages
    page: 1,                  // the page of TenderTiger's list shown, from 1
    pageSize: 20,             // tenders a page holds (one of PAGE_SIZES), as /api/tenders reports it
    hasMore: false,           // the page shown was full, so another may follow
    loading: null,            // {page, size} that showList() is loading, null otherwise
    rows: new Map(),          // tcno -> <tr>
    emptyRow: null,           // shown when the filters hide every tender
    selected: new Set(),      // tcnos the user ticked, on any page
    results: new Map(),       // tcno -> last result summary {status (completed/failed), verdict, checks, ...}
    active: new Map(),        // tcno -> 'queued' / 'processing' in the current job
    decisions: new Map(),     // tcno -> {decision, decided_at}
    verdictFilter: '',        // the selected tab: a verdict, 'failed', 'pending', or '' for all
    searchFilters: {},        // the TenderTiger filters the list was loaded with, as /api/tenders query parameters
    sort: {key: 'closing', order: 'asc'},  // how TenderTiger sorts the list: closing soonest first
    job: null,                // last /api/progress response, null while no job has run
    averageSeconds: null,     // average time per tender whose documents were read, for estimates
    averageRecheckSeconds: null,  // average time per tender checked against its saved criteria
    regenerate: false,        // the selection bar's Regenerate criteria switch
    pollTimer: null,
    pollFailures: 0,
    offlineToast: null,       // the "lost contact" warning, closed again once polling recovers
    notify: false,            // send a browser notification when the run finishes
    titleNote: '',            // "Done: ..." kept in the tab title until the tab is looked at again
    announced: '',
    selectionKey: '',         // what the selection bar shows, so the live region only speaks on change
    details: {tcno: null, text: '', order: []},  // the open modal: tender, copyable text, prev/next order
    deep: {tcno: null, timer: null, status: null},  // the open tender's deep analysis, polled while it runs
    // The profile editor: the blocks being edited, and the saved blocks as JSON to spot unsaved edits
    company: {blocks: [], saved: '[]'},
    companyError: null,       // the editor's last save problem, closed again on the next save
  };

  /* ---------- helpers ---------- */

  /** fetch() wrapper: parses JSON and throws an Error carrying the API's {"error": ...} message. */
  async function api(path, options = {}) {
    const headers = {Accept: 'application/json'};
    if (options.body !== undefined) headers['Content-Type'] = 'application/json';
    const response = await fetch(path, {...options, headers});
    let data = null;
    try {
      data = await response.json();
    } catch (_) {
      // Not JSON, e.g. a proxy error page; fall through to the status check
    }
    if (!response.ok) throw new Error((data && data.error) || `Request failed (HTTP ${response.status})`);
    return data;
  }

  /** Creates an element; text always goes through textContent. */
  function el(tag, {cls, text, attrs} = {}, children = []) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    if (attrs) Object.entries(attrs).forEach(([name, value]) => node.setAttribute(name, value));
    children.forEach((child) => child && node.append(child));
    return node;
  }

  const icon = (name, cls = '') => el('i', {cls: `bi ${name} ${cls}`.trim(), attrs: {'aria-hidden': 'true'}});

  /** Success toasts fade after 6 s; warnings and errors stay until closed, so they cannot be missed. */
  function toast(message, variant = 'danger') {
    const node = el('div', {
      cls: 'toast app-toast align-items-center',
      attrs: {role: variant === 'success' ? 'status' : 'alert', 'aria-atomic': 'true'},
    }, [
      el('div', {cls: 'd-flex'}, [
        el('div', {cls: 'toast-body'}, [
          el('span', {cls: `dot dot-${TOAST_TONES[variant] || 'none'}`, attrs: {'aria-hidden': 'true'}}),
          el('span', {text: message}),
        ]),
        el('button', {cls: 'btn-close btn-close-white me-2 m-auto', attrs: {type: 'button', 'data-bs-dismiss': 'toast', 'aria-label': 'Close'}}),
      ]),
    ]);
    els.toasts.append(node);
    node.addEventListener('hidden.bs.toast', () => node.remove());
    const instance = new bootstrap.Toast(node, {autohide: variant === 'success', delay: 6000});
    instance.show();
    return instance;
  }

  const plural = (count, one, many = `${one}s`) => `${count} ${count === 1 ? one : many}`;

  /** "criteria", or "criterion" for one; likewise "lead member criteria". */
  const criteriaNoun = (count, noun = 'criteria') => (count === 1 ? noun.replace(/criteria$/, 'criterion') : noun);

  /** "Meets all 6 criteria", or "Meets the 1 criterion". */
  const meetsAll = (total, noun = 'criteria') => `Meets ${total === 1 ? 'the' : 'all'} ${total} ${criteriaNoun(total, noun)}`;

  /**
   * TenderTiger's often lower-case organisations and places in title case: "national highways authority of india"
   * -> "National Highways Authority of India". Only first letters are raised, so capitals already there (PWD) stay.
   */
  const titleCase = (text) => String(text || '').replace(/[A-Za-z][A-Za-z']*/g,
    (word, offset) => (offset && SMALL_WORDS.has(word) ? word : word[0].toUpperCase() + word.slice(1)));

  /** A tender title as the table shows it, with a capital first letter (see .tender-title::first-letter). */
  const sentenceStart = (text) => String(text || '').replace(/^\s*[a-z]/, (letter) => letter.toUpperCase());

  function formatDuration(seconds) {
    if (seconds === null || seconds === undefined) return '';
    const minutes = Math.floor(seconds / 60);
    const rest = Math.round(seconds % 60);
    return minutes ? `${minutes}m ${rest}s` : `${rest}s`;
  }

  /** A rough duration for estimates: "1 min", "25 min", "1 h 10 min". */
  function formatMinutes(seconds) {
    const minutes = Math.max(1, Math.round(seconds / 60));
    if (minutes < 90) return `${minutes} min`;
    const rest = minutes % 60;
    return `${Math.floor(minutes / 60)} h${rest ? ` ${rest} min` : ''}`;
  }

  const pad = (n) => String(n).padStart(2, '0');

  /** The API's "18-Nov-2026" as the page writes dates: "18 Nov 2026". */
  const displayDate = (text) => (text ? String(text).replace(/-/g, ' ') : '');

  /** An ISO timestamp as "02 Nov 2026". */
  function formatDate(iso) {
    const date = new Date(iso);
    if (!iso || Number.isNaN(date.getTime())) return '';
    return `${pad(date.getDate())} ${MONTHS[date.getMonth()]} ${date.getFullYear()}`;
  }

  const formatClock = (date) => `${pad(date.getHours())}:${pad(date.getMinutes())}`;

  function formatDateTime(iso) {
    const date = new Date(iso);
    return iso && !Number.isNaN(date.getTime()) ? `${formatDate(iso)}, ${formatClock(date)}` : '';
  }

  /** "at 10:42 today" or "on 01 Nov 2026 at 18:20". */
  function formatWhen(iso) {
    const date = new Date(iso);
    if (!iso || Number.isNaN(date.getTime())) return '';
    return date.toDateString() === new Date().toDateString()
      ? `at ${formatClock(date)} today`
      : `on ${formatDate(iso)} at ${formatClock(date)}`;
  }

  const numberOr = (value, fallback = null) => {
    const n = Number(value);
    return value === null || value === undefined || value === '' || !Number.isFinite(n) ? fallback : n;
  };

  const crore = (amount) => (amount / 1e7).toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2});

  /** An INR tender value in crore, the unit the team reads: 511.80 (Cr). Null when there is none. */
  const valueInCrore = (tender) => {
    const amount = numberOr(tender.tender_value);
    return amount && amount > 0 && (!tender.currency || tender.currency === 'INR') ? amount : null;
  };

  /** A tender value as text: "₹511.80 Cr", or the currency's own short form. */
  function formatMoney(tender) {
    const amount = numberOr(tender.tender_value);
    if (!amount || amount <= 0) return '';
    if (valueInCrore(tender)) return `₹${crore(amount)} Cr`;
    try {
      return new Intl.NumberFormat('en-IN', {style: 'currency', currency: tender.currency, notation: 'compact'}).format(amount);
    } catch (_) {
      return `${tender.currency} ${amount.toLocaleString('en-IN')}`;
    }
  }

  function daysLeftText(days) {
    const n = numberOr(days);
    if (n === null) return '';
    return n < 0 ? 'Closed' : n === 0 ? 'Closes today' : `${plural(n, 'day')} left`;
  }

  function urgencyClass(days) {
    const n = numberOr(days);
    if (n === null) return '';
    return n <= URGENT_DAYS ? 'days-urgent' : n <= NEAR_DAYS ? 'days-near' : '';
  }

  const isClosed = (tender) => Boolean(tender) && numberOr(tender.days_left, 0) < 0;
  const isRunning = () => Boolean(state.job && state.job.status === 'running');
  const isDropped = (tcno) => {
    const decision = state.decisions.get(tcno);
    return Boolean(decision && decision.decision === 'dropped');
  };
  const verdictOf = (key) => VERDICTS[key] || {label: key || 'Unknown', tone: 'none'};

  /** 'closed', 'soon' (closing within URGENT_DAYS, today included) or 'open' (later, or no closing date given). */
  function closingStatus(tender) {
    const days = numberOr(tender.days_left);
    if (days === null) return 'open';
    return days < 0 ? 'closed' : days <= URGENT_DAYS ? 'soon' : 'open';
  }

  // The options of the decision and closing filters: whether a tender's decision / closing status falls under each
  const DECISION_FILTERS = {
    not_dropped: (decision) => decision !== 'dropped',
    shortlisted: (decision) => decision === 'shortlisted',
    none: (decision) => !decision,
    dropped: (decision) => decision === 'dropped',
  };
  const CLOSING_FILTERS = {
    open: (closing) => closing !== 'closed',  // closing soon is still open
    soon: (closing) => closing === 'soon',
    closed: (closing) => closing === 'closed',
  };

  /** Whether a tender falls under the decision and closing filters chosen ('' passes all). */
  function inStatusFilters(tender) {
    const decisionValue = els.decisionFilter.value;
    const closingValue = els.closingFilter.value;
    const decision = (state.decisions.get(tender.tcno) || {}).decision;
    return (!decisionValue || DECISION_FILTERS[decisionValue](decision))
      && (!closingValue || CLOSING_FILTERS[closingValue](closingStatus(tender)));
  }

  function pill(tone, label) {
    return el('span', {cls: `pill tone-${tone}`}, [
      el('span', {cls: 'pill-dot', attrs: {'aria-hidden': 'true'}}), document.createTextNode(label),
    ]);
  }

  /** The verdict tab a tender falls under: a verdict, 'failed', or 'pending' (not processed, or in the queue). */
  function category(tcno) {
    if (state.active.has(tcno)) return 'pending';
    const result = state.results.get(tcno);
    if (!result) return 'pending';
    return result.status === 'failed' ? 'failed' : result.verdict || 'Requires Review';
  }

  const tenderOf = (tcno) => state.seen.get(tcno);

  /** True when the list runs to more than one page: the page shown is not the first, or it was full. */
  const isPaged = () => state.page > 1 || state.hasMore;

  /** Whether the tender's criteria are saved, so a check can re-use them: the last run says so, else the list. */
  function hasSavedCriteria(tcno) {
    const result = state.results.get(tcno);
    if (result) return Boolean(result.criteria_file);
    const tender = tenderOf(tcno);
    return Boolean(tender && tender.has_saved_criteria);
  }

  /** Seconds for `full` tenders whose documents are read and `quick` re-checks; null until both were timed. */
  function estimateSeconds(full, quick) {
    if ((full && !state.averageSeconds) || (quick && !state.averageRecheckSeconds)) return null;
    return full * (state.averageSeconds || 0) + quick * (state.averageRecheckSeconds || 0);
  }

  /* ---------- tender table ---------- */

  /**
   * Loads a page of size tenders that TenderTiger finds with filters, in the order TenderTiger sorts them by (by
   * default the filters, page, size and order the list has); true when it worked.
   */
  async function loadTenders({quiet = false, filters = state.searchFilters, page = state.page, sort = state.sort,
    size = state.pageSize} = {}) {
    els.refreshBtn.disabled = true;
    try {
      const query = new URLSearchParams({
        ...filters, sort: sort.key, order: sort.order, page_size: size, ...(page > 1 ? {page} : {}),
      });
      const data = await api(`/api/tenders?${query}`);
      // TenderTiger tells the end of the list only by an empty page: stay on the page shown
      if (page > 1 && !data.tenders.length) {
        state.hasMore = false;
        renderPager();
        toast(`TenderTiger has no tenders on page ${page}, so page ${page - 1} is the last.`, 'success');
        return false;
      }
      const sameSearch = JSON.stringify(filters) === JSON.stringify(state.searchFilters);
      setSearchFilters(filters);
      state.tenders = data.tenders;
      state.page = data.page;
      state.hasMore = data.has_more;
      state.sort = {key: data.sort, order: data.order};
      if (data.page_size !== state.pageSize) {
        state.pageSize = data.page_size;
        try {
          localStorage.setItem(PAGE_SIZE_KEY, String(state.pageSize));
        } catch (_) {
          // Storage blocked: the page size lasts until the page is reloaded
        }
      }
      state.tenders.forEach((tender) => state.seen.set(tender.tcno, tender));
      // A selection carries across the pages of one search; a new search keeps only the tenders it lists
      if (!sameSearch) {
        const known = new Set(state.tenders.map((t) => t.tcno));
        state.selected.forEach((tcno) => known.has(tcno) || state.selected.delete(tcno));
      }
      state.decisions.clear();
      for (const tender of state.tenders) {
        if (tender.result) state.results.set(tender.tcno, tender.result);
        if (tender.decision) state.decisions.set(tender.tcno, tender.decision);
      }
      fillOrganisationFilter();
      renderTable();
      return true;
    } catch (error) {
      // The server's message already says what failed; a network error does not
      const reason = /tender list/i.test(error.message) ? error.message : `Could not load the tender list: ${error.message}`;
      if (quiet) {
        toast(reason);
        return false;
      }
      const retry = el('button', {cls: 'button button-secondary button-sm', text: 'Try again', attrs: {type: 'button'}});
      retry.addEventListener('click', () => loadTenders());
      els.body.replaceChildren(el('tr', {cls: 'message-row', attrs: {role: 'row'}}, [
        el('td', {attrs: {colspan: '7', role: 'cell'}}, [
          el('div', {cls: 'message-error', text: reason}),
          el('div', {cls: 'message-hint', text: 'Check the internet connection and the TenderTiger subscription, then try again.'}),
          retry,
          // Filters remembered from an earlier visit may be the problem
          filterCount(filters) ? clearFiltersButton(' ms-2') : null,
        ]),
      ]));
      state.tenders = [];
      state.rows.clear();
      state.emptyRow = null;
      renderPageSummary();
      updateCounts();
      updateSummary();
      return false;
    } finally {
      els.refreshBtn.disabled = false;
    }
  }

  function fillOrganisationFilter() {
    const current = els.orgFilter.value;
    // By the name the table shows, so one organisation sent in two cases is listed once
    const organisations = [...new Set(state.tenders.map((t) => titleCase(t.organisation)).filter(Boolean))]
      .sort((a, b) => a.localeCompare(b));
    const value = organisations.includes(current) ? current : '';
    // Full names, so a search matches any part of them; the field cuts a long name short with an ellipsis
    const options = [{value: '', text: 'All organisations'}, ...organisations.map((name) => ({value: name, text: name}))];
    if (orgPicker) {
      orgPicker.clear(true);
      orgPicker.clearOptions();
      orgPicker.addOptions(options);
      orgPicker.setValue(value, true);
      return;
    }
    els.orgFilter.replaceChildren(...options.map((option) => el('option', {text: option.text, attrs: {value: option.value}})));
    els.orgFilter.value = value;
  }

  /** "10 tenders · ₹1,114.90 Cr total value · 1 closing within 7 days", or "20 tenders on page 2 · ..." */
  function renderPageSummary() {
    if (!state.tenders.length) {
      els.pageSummary.textContent = '';
      return;
    }
    const total = state.tenders.reduce((sum, t) => sum + (valueInCrore(t) || 0), 0);
    const soon = state.tenders.filter((t) => {
      const days = numberOr(t.days_left);
      return days !== null && days >= 0 && days <= URGENT_DAYS;
    }).length;
    const parts = [`${plural(state.tenders.length, 'tender')}${isPaged() ? ` on page ${state.page}` : ''}`];
    if (total) parts.push(`₹${crore(total)} Cr total value`);
    parts.push(`${soon} closing within ${URGENT_DAYS} days`);
    // Each part stays on one line, so a narrow screen breaks between them, not inside
    els.pageSummary.replaceChildren(...parts.flatMap((part, index) => [
      index ? document.createTextNode(' · ') : null, el('span', {cls: 'text-nowrap', text: part}),
    ]).filter(Boolean));
  }

  function renderTable() {
    state.rows.clear();
    state.emptyRow = null;
    renderPageSummary();
    renderPager();
    if (!state.tenders.length) {
      const filtered = filterCount(state.searchFilters) > 0;
      els.body.replaceChildren(el('tr', {cls: 'message-row', attrs: {role: 'row'}}, [
        el('td', {attrs: {colspan: '7', role: 'cell'}}, filtered ? [
          el('div', {cls: 'mb-2', text: 'TenderTiger found no live tenders with these filters.'}), clearFiltersButton(),
        ] : [document.createTextNode('TenderTiger found no live tenders. Reload later to check again.')]),
      ]));
      applyFilters();
      return;
    }
    const clear = el('button', {cls: 'link-button', text: 'Show all tenders', attrs: {type: 'button'}});
    clear.addEventListener('click', clearFilters);
    state.emptyRow = el('tr', {cls: 'message-row', attrs: {role: 'row', hidden: ''}}, [
      el('td', {attrs: {colspan: '7', role: 'cell'}}, [
        el('div', {cls: 'mb-2', text: 'No tenders match these filters.'}), clear,
      ]),
    ]);
    els.body.replaceChildren(...state.tenders.map((tender) => {
      const row = buildRow(tender);
      state.rows.set(tender.tcno, row);
      return row;
    }), state.emptyRow);
    renderSortButtons();
    applyFilters();
  }

  function buildRow(t) {
    const reason = !t.has_document && !t.has_prequalification && !t.has_saved_criteria ? 'it has no documents'
      : isClosed(t) ? 'it has closed' : '';
    const checkbox = el('input', {
      cls: 'check row-check',
      attrs: {type: 'checkbox', 'aria-label': reason ? `Tender ${t.tcno} cannot be selected: ${reason}` : `Select tender ${t.tcno}`},
    });
    checkbox.dataset.tcno = t.tcno;
    if (reason) state.selected.delete(t.tcno);
    checkbox.checked = state.selected.has(t.tcno);
    checkbox.disabled = Boolean(reason);

    const amount = valueInCrore(t);
    const other = amount ? '' : formatMoney(t);
    const days = daysLeftText(t.days_left);
    const organisation = titleCase(t.organisation);
    const location = titleCase(t.location);
    const place = [organisation, location].filter(Boolean).join(' · ');
    const dropButton = el('button', {cls: 'row-drop', attrs: {type: 'button'}});
    dropButton.dataset.tcno = t.tcno;

    const row = el('tr', {attrs: {role: 'row'}}, [
      el('td', {cls: 'cell-check', attrs: {role: 'cell'}}, [checkbox]),
      el('td', {cls: 'cell-tender', attrs: {role: 'cell'}}, [
        el('div', {cls: 'tender-title', text: t.name || '(no description)', attrs: {title: t.name}}),
        el('div', {cls: 'tender-meta'}, [
          el('span', {cls: 'tcno', text: t.tcno}),
          t.reference ? el('span', {cls: 'ref', text: t.reference}) : null,
          t.published_date ? el('span', {text: `Published ${displayDate(t.published_date)}`}) : null,
        ]),
        place ? el('div', {cls: 'org-inline', text: place}) : null,
      ]),
      el('td', {cls: 'cell-org', attrs: {role: 'cell'}}, [
        el('div', {cls: 'org-name', text: organisation || '—', attrs: {title: organisation}}),
        location ? el('div', {cls: 'org-loc', text: location}) : null,
      ]),
      el('td', {cls: 'cell-value', attrs: {role: 'cell', 'data-label': 'Value'}}, amount
        ? [document.createTextNode(crore(amount)), el('span', {cls: 'unit', text: ' Cr'})]
        : [document.createTextNode(other || '—')]),
      el('td', {cls: 'cell-closes', attrs: {role: 'cell', 'data-label': 'Closes'}}, [
        el('div', {text: displayDate(t.closing_date) || '—'}),
        days ? el('span', {cls: `days-pill ${urgencyClass(t.days_left)}`.trim(), text: days}) : null,
      ]),
      el('td', {cls: 'cell-verdict', attrs: {role: 'cell'}}),
      el('td', {cls: 'cell-drop', attrs: {role: 'cell'}}, [dropButton]),
    ]);
    row.dataset.tcno = t.tcno;
    row.dataset.organisation = organisation;
    row.dataset.search = [t.tcno, t.name, t.reference, t.organisation, t.location].join(' ').toLowerCase();
    row.classList.toggle('is-selected', checkbox.checked);
    row.classList.toggle('is-locked', checkbox.disabled);
    updateRow(t.tcno, row);
    return row;
  }

  /** Redraws a row's eligibility cell, drop button and state classes, when what they show changed. */
  function updateRow(tcno, row = state.rows.get(tcno)) {
    if (!row) return;
    const result = state.results.get(tcno);
    const status = state.active.get(tcno);
    const decision = state.decisions.get(tcno);
    // Polling redraws every row each time; skipping unchanged ones keeps the pulse from restarting
    const signature = JSON.stringify([status, status === 'processing' ? stageLabel(tcno) : '', result, decision]);
    if (row.dataset.signature === signature) return;
    row.dataset.signature = signature;
    row.querySelector('.cell-verdict').replaceChildren(
      el('div', {cls: 'verdict-stack'}, verdictCell(tcno, result, status, decision)));

    const dropped = Boolean(decision && decision.decision === 'dropped');
    const dropButton = row.querySelector('.row-drop');
    dropButton.replaceChildren(icon(dropped ? 'bi-arrow-counterclockwise' : 'bi-x'));
    dropButton.setAttribute('aria-label', `${dropped ? 'Restore' : 'Drop'} tender ${tcno}`);
    dropButton.title = dropped ? 'Restore tender' : 'Drop tender';
    row.classList.toggle('is-dropped', dropped);
    row.dataset.category = category(tcno);
  }

  function verdictCell(tcno, result, status, decision) {
    const parts = [];
    const note = (text) => el('div', {cls: 'verdict-note', text, attrs: {title: text}});
    if (status === 'processing') {
      parts.push(pill('processing', 'Checking'), note(stageLabel(tcno)));
    } else if (status === 'queued') {
      parts.push(pill('none', 'Queued'));
    } else if (!result) {
      const tender = tenderOf(tcno);
      parts.push(pill('none', 'Not processed'));
      if (tender && !tender.has_document && !tender.has_prequalification && !tender.has_saved_criteria) {
        parts.push(note('No documents to check'));
      } else if (isClosed(tender)) parts.push(note('Closed for bids'));
      else if (hasSavedCriteria(tcno)) parts.push(note('Criteria saved: quick re-check'));
      else if (tender && !tender.has_document) parts.push(note('Pre-qualification criteria only'));
    } else {
      const failed = result.status === 'failed';
      const verdict = verdictOf(failed ? 'failed' : result.verdict);
      const button = el('button', {
        cls: 'pill-button',
        attrs: {type: 'button', 'aria-label': `${verdict.label}: open the analysis of tender ${tcno}`},
      }, [pill(verdict.tone, verdict.label), icon('bi-chevron-right', 'pill-chevron')]);
      button.dataset.tcno = tcno;
      parts.push(button, note(evidence(result)));
      const marked = !failed && result.checks ? result.checks.user_marked : 0;
      if (marked) {
        const text = `${marked} ${criteriaNoun(marked)} marked by user`;
        parts.push(el('div', {cls: 'verdict-note user-note', attrs: {title: text}}, [icon('bi-person-check'), document.createTextNode(text)]));
      }
      const jv = failed ? null : jvLine(result.checks && result.checks.jv);
      if (jv) parts.push(jv);
      if (failed) {
        const retry = el('button', {cls: 'link-button retry-btn', text: 'Retry', attrs: {type: 'button', 'aria-label': `Retry tender ${tcno}`}});
        retry.dataset.tcno = tcno;
        parts.push(retry);
      }
    }
    if (decision && DECISIONS[decision.decision]) {
      const d = DECISIONS[decision.decision];
      parts.push(el('div', {cls: `decision-line ${d.cls}`}, [
        icon(d.icon), document.createTextNode(`${d.label} ${formatDate(decision.decided_at)}`),
      ]));
    }
    return parts;
  }

  /** What the tender being processed is doing, short enough for its row: "Reading file 2 of 5…". */
  function stageLabel(tcno) {
    const current = state.job && state.job.current;
    if (!current || current.tcno !== tcno) return 'Processing…';
    if (current.steps && current.step === current.steps) return 'Comparing criteria…';
    if (current.steps) return `Reading file ${current.step} of ${current.steps - 1}…`;
    return `${current.stage}…`;
  }

  /** Why a tender got its verdict, in one line, so it can be judged from the table. */
  function evidence(result) {
    if (result.status === 'failed') return result.error_title || 'Processing failed';
    const checks = result.checks;
    let unreadable = '';
    if (checks && checks.unreadable_files) {
      unreadable = checks.unreadable_checked >= checks.unreadable_files
        ? `${plural(checks.unreadable_files, 'unreadable file')} checked by user`
        : `${plural(checks.unreadable_files, 'file')} unreadable`;
    }
    if (!checks || !checks.total) return ['No criteria found', unreadable].filter(Boolean).join(' · ');
    const parts = [];
    if (!checks.not_met.length && !checks.unknown.length) {
      parts.push(meetsAll(checks.total));
    } else if (checks.not_met.length) {
      parts.push(`Not met: ${checks.not_met.join(', ')}`);
      if (checks.unknown.length) parts.push(`${checks.unknown.length} undetermined`);
    } else {
      parts.push(`Undetermined: ${checks.unknown.join(', ')}`);
    }
    if (unreadable) parts.push(unreadable);
    return parts.join(' · ');
  }

  /**
   * The JV route under a row's verdict for bidding alone: "As JV lead: Eligible · up to 3 members", with a dot in
   * that outcome's tone. Null for a result without JV terms.
   */
  function jvLine(jv) {
    if (!jv) return null;
    let tone = 'none';
    let text = jv.allowed === 'no' ? 'JV not allowed' : 'JV not stated';
    if (jv.allowed === 'yes') {
      const outcome = verdictOf(jv.verdict || 'Requires Review');
      tone = outcome.tone;
      text = [`As JV lead: ${outcome.label}`, jv.max_members && `up to ${jv.max_members} members`].filter(Boolean).join(' · ');
    }
    return el('div', {cls: 'verdict-note jv-note', attrs: {title: text}}, [
      el('span', {cls: `dot dot-${tone}`, attrs: {'aria-hidden': 'true'}}), document.createTextNode(text),
    ]);
  }

  /** Marks the column the list is sorted by, and which way; the rows come from TenderTiger in that order. */
  function renderSortButtons() {
    const {key, order} = state.sort;
    for (const button of els.sortButtons) {
      const active = button.dataset.sort === key;
      button.closest('th').setAttribute('aria-sort', active ? (order === 'asc' ? 'ascending' : 'descending') : 'none');
      button.querySelector('.sort-arrow').className = `bi sort-arrow ${active ? (order === 'asc' ? 'bi-arrow-up' : 'bi-arrow-down') : ''}`.trim();
    }
  }

  /**
   * A column header: TenderTiger sorts the whole list again, from page 1. The same column flips the order; value
   * starts with the largest, closing date with the soonest.
   */
  function setSort(key) {
    const order = state.sort.key === key ? (state.sort.order === 'asc' ? 'desc' : 'asc') : (key === 'value' ? 'desc' : 'asc');
    showList({page: 1, sort: {key, order}});
  }

  /* ---------- filters and selection ---------- */

  function applyFilters() {
    const terms = els.search.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
    const organisation = els.orgFilter.value;
    let shown = 0;
    for (const tender of state.tenders) {
      const row = state.rows.get(tender.tcno);
      if (!row) continue;
      row.hidden = !(
        terms.every((term) => row.dataset.search.includes(term))
        && (!organisation || row.dataset.organisation === organisation)
        && (!state.verdictFilter || row.dataset.category === state.verdictFilter)
        && inStatusFilters(tender)
      );
      if (!row.hidden) shown += 1;
    }
    if (state.emptyRow) state.emptyRow.hidden = shown > 0 || !state.tenders.length;
    updateCounts();
    updateSelectionUI();
  }

  /**
   * Tab counts, of the tenders the decision and closing filters leave (so "Not dropped" leaves the dropped ones out
   * of them as well as the table).
   */
  function updateCounts() {
    const counts = {all: 0};
    for (const tender of state.tenders) {
      if (!inStatusFilters(tender)) continue;
      counts.all += 1;
      const key = category(tender.tcno);
      counts[key] = (counts[key] || 0) + 1;
    }
    document.querySelectorAll('[data-count]').forEach((node) => {
      node.textContent = counts[node.dataset.count] || 0;
    });
  }

  function setVerdictFilter(verdict) {
    state.verdictFilter = verdict || '';
    els.filters.querySelectorAll('.tab').forEach((tab) => {
      tab.setAttribute('aria-pressed', String(tab.dataset.verdict === state.verdictFilter));
    });
    applyFilters();
  }

  function clearFilters() {
    els.search.value = '';
    if (orgPicker) orgPicker.setValue('', true);
    else els.orgFilter.value = '';
    els.decisionFilter.value = '';
    els.closingFilter.value = '';
    setVerdictFilter('');
  }

  const visibleCheckboxes = () => [...els.body.querySelectorAll('tr:not([hidden]) .row-check:not(:disabled)')];

  function updateSelectionUI() {
    const boxes = visibleCheckboxes();
    const checked = boxes.filter((box) => box.checked).length;
    els.selectAll.checked = boxes.length > 0 && checked === boxes.length;
    els.selectAll.indeterminate = checked > 0 && checked < boxes.length;
    els.selectAll.disabled = boxes.length === 0;

    const count = state.selected.size;
    const label = isRunning() ? 'Add to queue' : 'Check eligibility';
    els.selectedCount.textContent = count;
    els.processBtn.disabled = count === 0;
    els.processLabel.textContent = label;
    els.barProcess.textContent = label;
    els.selectionBar.hidden = count === 0;
    els.regenerateToggle.hidden = ![...state.selected].some(hasSavedCriteria);
    els.regenerateToggle.setAttribute('aria-checked', String(state.regenerate));
    renderSelectionText(count);
    updateSummary();
  }

  /**
   * "3 selected · 1 on another page · ₹1,100.56 Cr · about 20 min · 2 from saved criteria · 1 will be checked
   * again". The live region only speaks on change.
   */
  function renderSelectionText(count) {
    const tenders = [...state.selected].map(tenderOf).filter(Boolean);
    const value = tenders.reduce((sum, t) => sum + (valueInCrore(t) || 0), 0);
    const saved = tenders.filter((t) => hasSavedCriteria(t.tcno)).length;
    const quick = state.regenerate ? 0 : saved;
    const elsewhere = [...state.selected].filter((tcno) => !state.rows.has(tcno)).length;
    const parts = [];
    if (elsewhere) parts.push(elsewhere === 1 ? '1 on another page' : `${elsewhere} on other pages`);
    if (value) parts.push(`₹${crore(value)} Cr`);
    const estimate = estimateSeconds(tenders.length - quick, quick);
    if (estimate) parts.push(`about ${formatMinutes(estimate)}`);
    if (saved) parts.push(state.regenerate ? `${saved} re-read from documents` : `${saved} from saved criteria`);
    const rechecks = tenders.filter((t) => state.results.has(t.tcno)).length;
    if (rechecks) parts.push(`${rechecks} will be checked again`);
    if (isRunning()) parts.push('joins the current run');
    const key = [count, ...parts].join('|');
    if (key === state.selectionKey) return;
    state.selectionKey = key;
    els.selectionText.replaceChildren(
      el('strong', {text: count}), document.createTextNode(` selected${parts.length ? ` · ${parts.join(' · ')}` : ''}`));
  }

  function updateSummary() {
    const shown = [...state.rows.values()].filter((row) => !row.hidden).length;
    // No footer under the empty and error states, where there is nothing to count
    els.tableFoot.hidden = !state.tenders.length;
    const page = isPaged() ? ' on this page' : '';
    els.summary.textContent = `Showing ${shown} of ${plural(state.tenders.length, 'tender')}${page} · ${state.selected.size} selected`;
  }

  /**
   * The footer's Previous / Next and where the list is ("Page 2 · tenders 21–40"), or the hint to select tenders
   * when the list fits on one page.
   */
  function renderPager() {
    const paged = isPaged();
    els.pager.hidden = !paged;
    els.tableHint.hidden = paged;
    // The size being loaded, else the one shown: a choice made while another loads, or one that failed, goes back
    els.pageSize.value = String(state.loading ? state.loading.size : state.pageSize);
    if (!paged) return;
    if (state.loading) {
      // The buttons stay enabled, so the one pressed keeps focus; showList() ignores presses meanwhile
      const {page, size} = state.loading;
      els.pageStatus.textContent = size === state.pageSize ? `Loading page ${page}…` : `Loading ${size} per page…`;
      return;
    }
    const first = (state.page - 1) * state.pageSize + 1;
    els.pageStatus.replaceChildren(el('strong', {text: `Page ${state.page}`}),
      document.createTextNode(` · tenders ${first}–${first + state.tenders.length - 1}`));
    const focused = document.activeElement;
    els.pagePrev.disabled = state.page <= 1;
    els.pageNext.disabled = !state.hasMore;
    // At the first or last page the pressed button is disabled: focus moves to the other one, not to the page.
    // Without scrolling to it, so showList() can bring the top of the list into view.
    if ((focused === els.pagePrev || focused === els.pageNext) && focused.disabled) {
      const other = focused === els.pageNext ? els.pagePrev : els.pageNext;
      if (!other.disabled) other.focus({preventScroll: true});
    }
  }

  /**
   * Previous / Next, the column headers and the page size: loads that page of the list, in that order and size,
   * keeping the selection, and brings the top of the list into view.
   */
  async function showList({page = state.page, sort = state.sort, size = state.pageSize}) {
    if (state.loading || page < 1) {
      renderPager();
      return;
    }
    state.loading = {page, size};
    els.table.setAttribute('aria-busy', 'true');
    renderPager();
    const loaded = await loadTenders({quiet: true, page, sort, size});
    state.loading = null;
    els.table.removeAttribute('aria-busy');
    renderPager();
    if (loaded && els.tenderPanel.getBoundingClientRect().top < 0) {
      const instant = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      els.tenderPanel.scrollIntoView({block: 'start', behavior: instant ? 'auto' : 'smooth'});
    }
  }

  function setSelected(tcno, selected) {
    if (selected) state.selected.add(tcno);
    else state.selected.delete(tcno);
    const row = state.rows.get(tcno);
    if (row) row.classList.toggle('is-selected', selected);
  }

  function clearSelection() {
    state.selected.clear();
    state.regenerate = false;
    els.body.querySelectorAll('.row-check').forEach((box) => { box.checked = false; });
    state.rows.forEach((row) => row.classList.remove('is-selected'));
    updateSelectionUI();
  }

  /* ---------- TenderTiger search filters ---------- */

  const amountFields = () => [...els.filtersForm.querySelectorAll('[data-amount]')];

  /** Fills each amount filter: how to compare, the amount, and the second amount of a "between" range. */
  function buildAmountFields() {
    for (const fieldset of amountFields()) {
      const name = fieldset.dataset.amount;
      const number = (field, label) => el('input', {
        cls: 'filter-input mono',
        attrs: {type: 'number', name: field, min: '0', step: 'any', inputmode: 'decimal', 'aria-label': label},
      });
      const how = el('select', {cls: 'filter-input amount-op', attrs: {name: `${name}_op`, 'aria-label': `${fieldset.dataset.label}: compare`}},
        COMPARISONS.map(([value, text]) => el('option', {text, attrs: {value}})));
      how.addEventListener('change', () => syncAmountField(fieldset));
      fieldset.querySelector('.amount-row').replaceChildren(how, number(name, fieldset.dataset.label),
        el('div', {cls: 'amount-to', attrs: {hidden: ''}}, [
          el('span', {cls: 'amount-and', text: 'and', attrs: {'aria-hidden': 'true'}}),
          number(`${name}_to`, `${fieldset.dataset.label}: up to`),
        ]));
    }
  }

  /** Shows the second amount for "between" only, and names the first one to match. */
  function syncAmountField(fieldset) {
    const name = fieldset.dataset.amount;
    const between = els.filtersForm.elements[`${name}_op`].value === 'between';
    fieldset.querySelector('.amount-to').hidden = !between;
    els.filtersForm.elements[name].setAttribute('aria-label', `${fieldset.dataset.label}${between ? ': from' : ''}`);
  }

  /** The filters filled in on the form, as /api/tenders query parameters; empty ones are left out. */
  function formFilters() {
    const filters = {};
    for (const [name, value] of new FormData(els.filtersForm)) {
      const text = String(value).trim();
      if (text) filters[name] = text;
    }
    // A comparison counts only with an amount, and a second amount only in a "between" range
    for (const {dataset: {amount: name}} of amountFields()) {
      if (filters[`${name}_op`] !== 'between') delete filters[`${name}_to`];
      if (!filters[name] && !filters[`${name}_to`]) delete filters[`${name}_op`];
    }
    return filters;
  }

  /** The first range of filters that cannot be searched, as [its field, why]; null when all can be. */
  function rangeProblem(filters) {
    const fields = els.filtersForm.elements;
    for (const {dataset: {amount: name}} of amountFields()) {
      if (filters[`${name}_op`] !== 'between') continue;
      if (!filters[name] || !filters[`${name}_to`]) {
        return [fields[filters[name] ? `${name}_to` : name], 'Enter both amounts of the range.'];
      }
      if (Number(filters[name]) > Number(filters[`${name}_to`])) {
        return [fields[`${name}_to`], 'Enter an amount at least as large as the first.'];
      }
    }
    for (const name of ['published', 'closing']) {
      const start = filters[`${name}_from`];
      const end = filters[`${name}_to`];
      if (start && end && start > end) return [fields[`${name}_to`], 'Enter a date on or after the first date.'];
    }
    return null;
  }

  /** Puts filters (as /api/tenders query parameters) into the form, replacing what it holds. */
  function fillFilterForm(filters) {
    els.filtersForm.reset();
    for (const [name, value] of Object.entries(filters)) {
      const field = els.filtersForm.elements[name];
      if (!field) continue;
      if (field.type === 'checkbox') field.checked = value === 'true';
      else field.value = value;  // a radio group's RadioNodeList checks the radio with this value
    }
    amountFields().forEach(syncAmountField);
    markEmptyDates();
  }

  /** An empty date field shows its dd-mm-yyyy format in placeholder grey (.is-empty), not as a set date. */
  function markEmptyDates() {
    els.filtersForm.querySelectorAll('input[type="date"]').forEach((field) => field.classList.toggle('is-empty', !field.value));
  }

  /** How many filters are set: a range or a comparison counts once with its amount. */
  const filterCount = (filters) => new Set(Object.keys(filters).map((name) => name.replace(/_(from|to|op)$/, ''))).size;

  /** Records the filters the list was loaded with, shows their count and keeps them for the next visit. */
  function setSearchFilters(filters) {
    state.searchFilters = filters;
    const count = filterCount(filters);
    els.filtersCount.textContent = count;
    els.filtersCount.hidden = !count;
    els.filtersBtn.setAttribute('aria-label', count ? `Filters, ${count} applied` : 'Filters');
    try {
      localStorage.setItem(FILTERS_KEY, JSON.stringify(filters));
    } catch (_) {
      // Storage blocked: the filters last until the page is reloaded
    }
  }

  function setFiltersOpen(open) {
    if (!open && els.filtersForm.contains(document.activeElement)) els.filtersBtn.focus();
    els.filtersForm.hidden = !open;
    els.filtersBtn.setAttribute('aria-expanded', String(open));
    // Closed without applying: the form shows the filters the list has again, not the unapplied edits
    if (!open) fillFilterForm(state.searchFilters);
    else els.filtersForm.elements.word.focus();
  }

  async function applySearchFilters(event) {
    event.preventDefault();
    const filters = formFilters();
    const problem = rangeProblem(filters);
    if (problem) {
      const [field, message] = problem;
      field.setCustomValidity(message);
      field.reportValidity();
      return;
    }
    els.filtersApply.disabled = true;
    const loaded = await loadTenders({quiet: true, filters, page: 1});
    els.filtersApply.disabled = false;
    if (loaded) setFiltersOpen(false);
  }

  /** Empties the form, and loads the list without filters when it has some. */
  async function clearSearchFilters() {
    fillFilterForm({});
    // Still loaded with the filters if that failed, so the form shows them again
    if (filterCount(state.searchFilters) && !(await loadTenders({quiet: true, filters: {}, page: 1}))) {
      fillFilterForm(state.searchFilters);
    }
  }

  function clearFiltersButton(cls = '') {
    const button = el('button', {cls: `link-button${cls}`, text: 'Clear filters', attrs: {type: 'button'}});
    button.addEventListener('click', clearSearchFilters);
    return button;
  }

  /* ---------- processing and progress ---------- */

  /**
   * Queues tenders: starts a run, or adds them to the running one. Tenders with saved criteria are re-checked
   * against them unless regenerate is true. Returns true on success.
   */
  async function queue(tcnos, regenerate = false) {
    try {
      applyProgress(await api('/api/process-tenders', {method: 'POST', body: JSON.stringify({tcnos, regenerate})}));
      schedulePoll();
      return true;
    } catch (error) {
      toast(`Could not start the check: ${error.message}`);
      return false;
    }
  }

  async function processSelected() {
    // Process in the order the table shows, then the tenders selected on other pages
    const shown = [...state.rows.values()].map((row) => row.dataset.tcno).filter((tcno) => state.selected.has(tcno));
    const tcnos = [...new Set([...shown, ...state.selected])];
    if (!tcnos.length) return;
    els.processBtn.disabled = els.barProcess.disabled = true;
    const queued = await queue(tcnos, state.regenerate);
    els.barProcess.disabled = false;
    if (queued) clearSelection();
    else updateSelectionUI();
  }

  async function retry(tcno, button, regenerate = false) {
    if (button) button.disabled = true;
    // Reload the list first, so the retry uses the tender as TenderTiger lists it now
    if (await loadTenders({quiet: true})) await queue([tcno], regenerate);
    else if (button) button.disabled = false;
  }

  /** The details modal's Re-check / Regenerate criteria: a re-check needs nothing from TenderTiger. */
  function checkAgain(tcno, regenerate) {
    detailsModal.hide();
    if (regenerate) retry(tcno, null, true);
    else queue([tcno]);
  }

  async function stopRun() {
    els.stopBtn.disabled = true;
    try {
      applyProgress(await api('/api/stop', {method: 'POST'}));
    } catch (error) {
      toast(`Could not stop the run: ${error.message}`);
      els.stopBtn.disabled = false;
    }
  }

  function schedulePoll(delay = POLL_MS) {
    clearTimeout(state.pollTimer);
    state.pollTimer = setTimeout(poll, delay);
  }

  async function poll() {
    try {
      const progress = await api('/api/progress');
      state.pollFailures = 0;
      if (state.offlineToast) {
        state.offlineToast.hide();
        state.offlineToast = null;
      }
      applyProgress(progress);
      if (isRunning()) schedulePoll();
    } catch (error) {
      // Keep polling: the job runs on the server regardless of a network blip
      if (state.pollFailures++ === 0) {
        state.offlineToast = toast(`Lost contact with the server (${error.message}). Retrying…`, 'warning');
      }
      schedulePoll(POLL_RETRY_MS);
    }
  }

  function applyProgress(progress) {
    const wasRunning = isRunning();
    const wasProcessing = [...state.active].filter(([, status]) => status === 'processing').map(([tcno]) => tcno);
    state.job = progress.status === 'idle' ? null : progress;
    state.averageSeconds = progress.average_seconds || null;
    state.averageRecheckSeconds = progress.average_recheck_seconds || null;
    state.active.clear();
    for (const item of progress.items || []) {
      if (item.status === 'queued' || item.status === 'processing') state.active.set(item.tcno, item.status);
      else if (item.status === 'completed' || item.status === 'failed') state.results.set(item.tcno, item);
      // A cancelled tender keeps its previous result
    }
    state.rows.forEach((row, tcno) => updateRow(tcno, row));
    wasProcessing.filter((tcno) => !state.active.has(tcno)).forEach(markLanded);
    renderRun();
    applyFilters();
    if (wasRunning && state.job && state.job.status === 'completed') runFinished(state.job);
  }

  /** A verdict that lands while the page is watched gets one short reveal on its row. */
  function markLanded(tcno) {
    const row = state.rows.get(tcno);
    if (!row || !state.results.has(tcno)) return;
    row.classList.add('just-landed');
    // Removed afterwards, so hiding and showing the row by a filter does not replay it
    setTimeout(() => row.classList.remove('just-landed'), 1600);
  }

  /** The run's outcome in verdict terms: [{key, label, tone, count}] for the verdicts that occurred. */
  function runOutcome(job) {
    const counts = new Map();
    for (const item of job.items) {
      const key = item.status === 'failed' ? 'failed' : item.status === 'completed' ? item.verdict || 'Requires Review' : null;
      if (key) counts.set(key, (counts.get(key) || 0) + 1);
    }
    return OUTCOME_ORDER.filter((key) => counts.get(key)).map((key) => ({key, ...verdictOf(key), count: counts.get(key)}));
  }

  const outcomeText = (job) => runOutcome(job).map(({count, label}) => `${count} ${label}`).join(' · ') || 'No tenders checked';

  /** The run ended while the page was open: leave a note in a background tab's title, and notify if asked to. */
  function runFinished(job) {
    const summary = outcomeText(job);
    if (document.hidden) {
      state.titleNote = `Done: ${summary}`;
      document.title = `${state.titleNote} · ${BASE_TITLE}`;
    }
    if (state.notify && canNotify() && Notification.permission === 'granted') {
      const note = new Notification('Eligibility check finished', {body: summary, tag: job.job_id});
      note.addEventListener('click', () => {
        window.focus();
        note.close();
      });
    }
  }

  /* ---------- finish notification ---------- */

  // Browsers only allow notifications on https or localhost
  const canNotify = () => 'Notification' in window && window.isSecureContext;

  function renderNotify() {
    els.notifyBtn.hidden = !(isRunning() && canNotify() && Notification.permission !== 'denied');
    els.notifyBtn.setAttribute('aria-pressed', String(state.notify));
    els.notifyBtn.querySelector('i').className = `bi ${state.notify ? 'bi-bell-fill' : 'bi-bell'}`;
    els.notifyBtn.querySelector('span').textContent = state.notify ? 'Will notify when done' : 'Notify me when done';
  }

  async function toggleNotify() {
    let enabled = !state.notify;
    if (enabled && Notification.permission !== 'granted') {
      enabled = (await Notification.requestPermission()) === 'granted';
      if (!enabled) toast('The browser blocks notifications for this page. Allow them in its site settings to be told when a run finishes.', 'warning');
    }
    state.notify = enabled;
    try {
      localStorage.setItem(NOTIFY_KEY, enabled ? '1' : '0');
    } catch (_) {
      // Storage blocked: the choice lasts until the page is reloaded
    }
    renderNotify();
  }

  /**
   * Seconds left in the run, from the average time per tender of each kind (documents read, or saved criteria
   * re-checked); null until a tender of every kind still to come has completed.
   */
  function remainingSeconds(job) {
    const averageOf = (item) => (item.rechecked ? state.averageRecheckSeconds : state.averageSeconds);
    const waiting = job.items.filter((item) => item.status === 'queued' || item.status === 'processing');
    if (waiting.some((item) => !averageOf(item))) return null;
    const current = job.current;
    return waiting.reduce((left, item) => {
      const average = averageOf(item);
      if (item.status === 'queued') return left + average;
      if (!current) return left + average;
      return left + (current.steps
        ? average * (1 - (current.step - 1) / current.steps)
        : Math.max(average - current.elapsed_seconds, 0));
    }, 0);
  }

  function dismissedJob() {
    try {
      return localStorage.getItem(DISMISSED_KEY);
    } catch (_) {
      return null;
    }
  }

  function renderRun() {
    const job = state.job;
    const running = isRunning();
    renderNotify();
    if (!job || (!running && dismissedJob() === job.job_id)) {
      els.runPanel.hidden = true;
      document.title = state.titleNote ? `${state.titleNote} · ${BASE_TITLE}` : BASE_TITLE;
      return;
    }
    els.runPanel.hidden = false;
    const active = job.total - job.cancelled;
    const done = job.completed + job.failed;

    if (running) {
      const current = job.current;
      const position = Math.min(done + 1, active);
      els.runTitle.textContent = current ? `Checking tender ${position} of ${active}` : 'Starting…';
      els.runDetail.classList.remove('run-outcome');
      els.runDetail.replaceChildren(...(current
        ? [el('span', {cls: 'tcno', text: current.tcno}), document.createTextNode(` · ${sentenceStart(current.name)}`)]
        : []));
      els.runDetail.title = els.runDetail.textContent;
      els.runStage.textContent = current ? `${current.stage} · ${formatDuration(current.elapsed_seconds)} on this tender` : '';
      els.runStage.title = els.runStage.textContent;
      els.runStage.hidden = !current;
      const remaining = remainingSeconds(job);
      els.runEta.textContent = remaining === null ? '' : `About ${formatMinutes(remaining)} left`;
      els.stopBtn.hidden = false;
      els.stopBtn.disabled = job.stop_requested;
      els.stopBtn.textContent = job.stop_requested ? 'Stopping after this tender…' : 'Stop after this tender';
      els.runDismiss.hidden = true;

      const fraction = current && current.steps ? Math.max(current.step - 1, 0) / current.steps : 0;
      const percent = active ? Math.min(100, Math.round(100 * (done + fraction) / active)) : 0;
      els.runProgress.hidden = false;
      els.runProgress.setAttribute('aria-valuenow', percent);
      els.runProgress.setAttribute('aria-valuetext', `${done} of ${plural(active, 'tender')} done`);
      els.runBar.style.width = `${percent}%`;
      document.title = `(${done}/${active}) Checking tenders · ${BASE_TITLE}`;
      announce(current ? `Checking tender ${position} of ${active}: ${sentenceStart(current.name)}` : '');
    } else {
      els.runTitle.textContent = `${job.stop_requested ? 'Stopped' : 'Finished'} ${formatWhen(job.finished_at)}`;
      // The outcome as the shortlist it is: each count shows those tenders
      const parts = runOutcome(job).map(({key, label, tone, count}) => {
        const button = el('button', {
          cls: 'run-count',
          attrs: {type: 'button', 'aria-label': `Show the ${plural(count, 'tender')} from this run: ${label}`},
        }, [el('span', {cls: `dot dot-${tone}`, attrs: {'aria-hidden': 'true'}}), document.createTextNode(`${count} ${label}`)]);
        button.addEventListener('click', () => setVerdictFilter(key));
        return button;
      });
      if (job.cancelled) parts.push(el('span', {cls: 'run-muted', text: `${job.cancelled} not started`}));
      if (!parts.length) parts.push(el('span', {cls: 'run-muted', text: 'No tenders were checked.'}));
      els.runDetail.classList.add('run-outcome');
      els.runDetail.replaceChildren(...parts);
      els.runDetail.title = '';
      els.runStage.textContent = job.failed ? 'Failed tenders can be retried from their row.' : '';
      els.runStage.hidden = !job.failed;
      els.runStage.title = '';
      els.runEta.textContent = '';
      els.stopBtn.hidden = true;
      els.runDismiss.hidden = false;
      els.runProgress.hidden = true;
      document.title = state.titleNote ? `${state.titleNote} · ${BASE_TITLE}` : BASE_TITLE;
      announce(`${els.runTitle.textContent}: ${outcomeText(job)}`);
    }
  }

  function announce(text) {
    if (!text || text === state.announced) return;
    state.announced = text;
    els.runAnnounce.textContent = text;
  }

  /* ---------- details modal ---------- */

  /** Tenders with a result, in the order the table shows them: the modal's previous / next. */
  const openableOrder = () => [...state.rows.values()]
    .filter((row) => !row.hidden && state.results.has(row.dataset.tcno) && !state.active.has(row.dataset.tcno))
    .map((row) => row.dataset.tcno);

  async function openDetails(tcno) {
    if (!els.modal.classList.contains('show') || !state.details.order.includes(tcno)) state.details.order = openableOrder();
    state.details.tcno = tcno;
    state.details.text = '';
    const tender = tenderOf(tcno);
    els.modalTitle.textContent = tender && tender.name ? tender.name : `Tender ${tcno}`;
    els.modalTitle.title = els.modalTitle.textContent;
    const days = tender ? daysLeftText(tender.days_left) : '';
    // [text, wraps]: a long organisation name may wrap; the value and the closing date stay on one line each
    const facts = tender
      ? [[titleCase(tender.organisation), true], [formatMoney(tender)],
        [tender.closing_date && `Closes ${displayDate(tender.closing_date)}${days ? ` (${days.toLowerCase()})` : ''}`]]
        .filter(([text]) => text)
      : [];
    els.modalFacts.replaceChildren(el('span', {cls: 'tcno', text: tcno}), ...facts.flatMap(([text, wraps]) => [
      document.createTextNode(' · '), el('span', {cls: wraps ? '' : 'text-nowrap', text}),
    ]));
    els.modalSummary.replaceChildren();
    stopDeep();
    els.modalDeep.hidden = true;
    els.modalDeep.replaceChildren();
    els.analysisSection.hidden = false;
    els.modalContent.replaceChildren(el('div', {cls: 'text-center py-5', attrs: {style: 'color: var(--meta)'}}, [
      el('span', {cls: 'spinner-border spinner-border-sm me-2', attrs: {'aria-hidden': 'true'}}),
      document.createTextNode('Loading analysis…'),
    ]));
    els.modalFiles.replaceChildren();
    els.modalCriteria.replaceChildren();
    els.modalCriteria.className = '';
    els.modalRun.textContent = '';
    els.copyBtn.disabled = true;
    updateDetailsNav();
    updateDecisionButtons();
    detailsModal.show();
    try {
      const result = await api(`/api/result/${encodeURIComponent(tcno)}`);
      if (state.details.tcno === tcno) renderDetails(result);
    } catch (error) {
      if (state.details.tcno === tcno) {
        els.modalContent.replaceChildren(el('div', {cls: 'message-error', text: `Could not load the analysis: ${error.message}`}));
      }
    }
  }

  function renderDetails(result) {
    els.modalSummary.replaceChildren(decisionSummary(result));
    if (result.status !== 'failed' && result.jv) els.modalSummary.append(routeComparison(result));
    if (result.status === 'completed') loadDeep(result.tcno);
    if (result.status === 'failed') {
      state.details.text = `${result.error_title || 'Processing failed'}. ${result.error_action || ''}\n\n${result.error || ''}`.trim();
      els.analysisSection.hidden = true;
    } else {
      state.details.text = result.details || '';
      els.modalContent.replaceChildren(renderMarkdown(state.details.text || 'No details were returned.'));
      if (result.checks && result.checks.user_marked) {
        els.modalContent.prepend(el('p', {
          cls: 'analysis-note', text: 'The analysis as the AI wrote it, with its own statuses. Those marked by user above replace them.',
        }));
      }
    }
    els.copyBtn.disabled = !state.details.text;
    if (result.files && result.files.length) {
      els.modalFiles.replaceChildren(el('details', {cls: 'details-files'}, [
        el('summary', {text: `${plural(result.files.length, 'file')} in the tender`}),
        el('ul', {cls: 'mt-2 mb-0'}, result.files.map((file) => el('li', {}, [fileLink(result.tcno, file)]))),
        el('p', {cls: 'folder', text: 'Result saved.'}),
      ]));
    }
    if (result.criteria_file) renderCriteriaActions(result);
    const run = [];
    if (result.processed_at) run.push(`Processed ${formatDateTime(result.processed_at)}`);
    if (result.duration_seconds !== undefined) run.push(`took ${formatDuration(result.duration_seconds)}`);
    if (result.rechecked) run.push('checked against the saved criteria');
    if (result.tokens) {
      const count = (step) => result.tokens[step].total.toLocaleString('en-IN');
      run.push(`${count('total')} LLM tokens (${count('criteria')} reading the criteria, ${count('assessment')} for the result)`);
    }
    els.modalRun.textContent = run.join(' · ');
  }

  /** Where the criteria are saved, with Re-check (against them) and Regenerate criteria (from the documents). */
  function renderCriteriaActions(result) {
    const busy = state.active.has(result.tcno);
    const button = (text, regenerate) => {
      const node = el('button', {cls: 'button button-secondary button-sm', text, attrs: {type: 'button'}});
      node.disabled = busy;
      node.addEventListener('click', () => checkAgain(result.tcno, regenerate));
      return node;
    };
    els.modalCriteria.className = 'details-criteria';
    els.modalCriteria.replaceChildren(
      el('p', {text: 'Criteria saved. Re-check compares the current company profile with them without reading the '
        + 'documents; Regenerate criteria reads the documents again.'}),
      el('div', {cls: 'actions'}, [button('Re-check', false), button('Regenerate criteria', true)]),
    );
  }

  /** The decision first: the verdict and the criteria that decide it, with requirement, company figure and source. */
  function decisionSummary(result) {
    if (result.status === 'failed') {
      const retryBtn = el('button', {cls: 'button button-primary button-sm', text: 'Retry this tender', attrs: {type: 'button'}});
      retryBtn.addEventListener('click', () => {
        detailsModal.hide();
        retry(result.tcno);
      });
      return el('div', {cls: 'summary tone-failed'}, [
        el('div', {cls: 'summary-head'}, [
          pill('failed', 'Failed'),
          el('span', {cls: 'summary-headline', text: result.error_title || 'Processing failed'}),
        ]),
        el('p', {cls: 'summary-action', text: result.error_action || 'Retry this tender.'}),
        retryBtn,
        el('details', {cls: 'technical-detail'}, [
          el('summary', {text: 'Technical detail'}),
          el('code', {text: result.error || 'No error message.'}),
        ]),
      ]);
    }

    // Each keeps its position in result.criteria, which is how a status changed by hand is saved
    const criteria = (result.criteria || []).map((criterion, index) => ({...criterion, index}));
    const notMet = criteria.filter((c) => c.status === 'not_met');
    const unknown = criteria.filter((c) => c.status === 'unknown');
    const metItems = criteria.filter((c) => c.status === 'met');
    const met = metItems.length;
    const unreadable = result.unreadable_files || [];
    const unread = unreadable.length && !unreadableChecked(result) ? unreadable.length : 0;
    const verdict = verdictOf(result.verdict);

    let headline;
    if (notMet.length) headline = `${notMet.length} ${criteriaNoun(notMet.length)} not met`;
    else if (unknown.length) headline = `${unknown.length} ${criteriaNoun(unknown.length)} could not be determined`;
    // Why a tender meeting every criterion still needs review
    else if (criteria.length && unread) headline = `${meetsAll(criteria.length)}, but ${plural(unread, 'file')} could not be read`;
    else if (criteria.length) headline = meetsAll(criteria.length);
    else headline = 'No criteria table in the analysis';

    const children = [el('div', {cls: 'summary-head'}, [
      pill(verdict.tone, verdict.label),
      el('span', {cls: 'summary-headline', text: headline}),
    ])];
    if (criteria.length) {
      const counts = [`${met} met`, `${notMet.length} not met`, `${unknown.length} undetermined`];
      children.push(el('p', {cls: 'summary-counts', text: `${criteria.length} ${criteriaNoun(criteria.length)}: ${counts.join(' · ')}`}));
    }
    const list = (items) => el('ul', {cls: 'criteria-list'}, items.map((item) => criterionItem(item, result.tcno)));
    const section = (title, cls, items) => {
      if (!items.length) return;
      children.push(el('h3', {cls: `summary-section-title ${cls}`, text: title}));
      children.push(list(items));
    };
    section('Not met', 'tone-not', notMet);
    section('Could not be determined', 'tone-review', unknown);
    // Folded away, as they decide nothing, but listed so a wrong Met can be changed too
    if (metItems.length) {
      children.push(el('details', {cls: 'met-criteria'}, [
        el('summary', {cls: 'summary-section-title tone-eligible', text: `Met (${metItems.length})`}),
        list(metItems),
      ]));
    }
    if (unreadable.length) children.push(...unreadableSection(result));
    if (!criteria.length) {
      children.push(el('p', {cls: 'summary-action mb-0', text: 'Read the full analysis below before deciding.'}));
    }
    return el('div', {cls: `summary tone-${verdict.tone}`}, children);
  }

  const unreadableChecked = (result) => (result.unreadable_checked || []).length >= (result.unreadable_files || []).length;

  /**
   * The files the AI could not read. Until the bid team checks them itself, they keep the verdict from Eligible,
   * so the section says so and offers to record the check, which is then tagged "Checked by user" with an Undo.
   */
  function unreadableSection(result) {
    const checked = unreadableChecked(result);
    const button = el('button', {
      cls: `unreadable-toggle ${checked ? 'link-button' : 'button button-secondary button-sm'}`,
      text: checked ? 'Undo' : 'I have checked these files',
      attrs: {type: 'button'},
    });
    button.disabled = state.active.has(result.tcno);
    button.addEventListener('click', () => checkUnreadable(result.tcno, !checked, button));
    return [
      el('h3', {
        cls: 'summary-section-title section-plain',
        text: checked ? 'Files the AI could not read' : 'Not checked: files that could not be read',
      }),
      // A file opens from here, to be checked by hand; the pre-qualification criteria or a note has no file
      el('ul', {cls: 'mb-0'}, result.unreadable_files.map((file) => el('li', {},
        [(result.files || []).includes(file) ? fileLink(result.tcno, file) : document.createTextNode(file)]))),
      checked
        ? el('div', {cls: 'criterion-mark'}, [userTag('Checked by user'), button])
        : el('div', {cls: 'unreadable-action'}, [
          el('p', {text: 'The verdict cannot be Eligible until someone checks these files by hand.'}), button,
        ]),
    ];
  }

  /** A tender file as a link opening it in a new tab: PDFs and images show there, Word and Excel files download. */
  function fileLink(tcno, file) {
    const path = file.split('/').map(encodeURIComponent).join('/');
    return el('a', {
      cls: 'file-link', text: file,
      attrs: {href: `/api/files/${encodeURIComponent(tcno)}/${path}`, target: '_blank', rel: 'noopener', title: `Open ${file}`},
    });
  }

  const userTag = (text) => el('span', {cls: 'user-tag'}, [icon('bi-person-check'), document.createTextNode(text)]);

  const STATUS_TEXT = {met: 'Met', not_met: 'Not met', unknown: 'Cannot determine'};
  const STATUS_CLASS = {met: 'status-met', not_met: 'status-not-met', unknown: 'status-unknown'};

  /** "2 of 6 criteria not met", "Meets all 6 criteria": a route's criteria in one line. */
  function countsNote(counts, noun = 'criteria') {
    if (!counts || !counts.total) return `No ${noun} listed`;
    const outOf = `of ${counts.total} ${criteriaNoun(counts.total, noun)}`;
    if (counts.not_met.length) return `${counts.not_met.length} ${outOf} not met`;
    if (counts.unknown.length) return `${counts.unknown.length} ${outOf} undetermined`;
    return meetsAll(counts.total, noun);
  }

  /**
   * Bidding alone against bidding as JV lead member: each route's outcome side by side, then every criterion in
   * both routes (what each asks of the company, and of the other JV members), then the other JV conditions.
   */
  function routeComparison(result) {
    const jv = result.jv;
    const checks = result.checks || {};
    const route = (title, tone, label, note) => el('div', {cls: `route tone-${tone}`}, [
      el('div', {cls: 'route-label', text: title}), pill(tone, label), el('p', {cls: 'route-note', text: note}),
    ]);
    const alone = verdictOf(result.verdict);
    let jvRoute;
    if (jv.allowed === 'yes') {
      const outcome = verdictOf(jv.verdict || 'Requires Review');
      const size = jv.max_members ? `JV of up to ${jv.max_members} members` : 'no member limit stated';
      jvRoute = route('As JV lead member', outcome.tone, outcome.label,
        `${countsNote(checks.jv && checks.jv.lead, 'lead member criteria')} · ${size}`);
    } else if (jv.allowed === 'no') {
      jvRoute = route('As JV lead member', 'none', 'Not allowed', 'The tender does not accept joint ventures');
    } else {
      jvRoute = route('As JV lead member', 'none', 'Not stated', 'The documents do not say whether a JV may bid');
    }
    const children = [
      el('h3', {cls: 'summary-section-title section-plain', text: 'Bidding alone vs as JV lead member'}),
      el('div', {cls: 'routes'}, [route('Bidding alone', alone.tone, alone.label, countsNote(checks)), jvRoute]),
    ];
    const rows = result.comparison || [];
    if (jv.allowed === 'yes' && rows.length) {
      if (rows.some(jvHelps)) {
        children.push(el('p', {cls: 'compare-note', text: 'Green rows are not met alone but met as JV lead member.'}));
      }
      children.push(comparisonTable(rows));
    }
    if (jv.conditions && jv.conditions.length) {
      children.push(el('h3', {cls: 'summary-section-title section-plain', text: 'Other JV conditions'}));
      children.push(el('ul', {cls: 'mb-0'}, jv.conditions.map((text) => el('li', {text}))));
    }
    return el('section', {cls: 'summary route-compare', attrs: {'aria-label': 'Bidding alone vs as JV lead member'}}, children);
  }

  const jvHelps = (row) => row.alone_status === 'not_met' && row.lead_status === 'met';

  /** One row per criterion: the company's figure, what bidding alone and leading a JV ask, and the others' share. */
  function comparisonTable(rows) {
    const labels = ['Criterion', 'Company', 'Bidding alone', 'As JV lead member', 'Other members · JV combined'];
    const status = (value, text) => el('span', {
      cls: `compare-status ${STATUS_CLASS[value] || ''}`.trim(), text: text || STATUS_TEXT[value] || '',
    });
    const cell = (index, children) => el('td', {attrs: {'data-label': labels[index]}}, children);
    const others = (row) => [row.other_members && `Other members: ${row.other_members}`,
      row.combined && `JV combined: ${row.combined}`].filter(Boolean);
    const body = el('tbody', {}, rows.map((row) => {
      const tr = el('tr', {}, [
        cell(0, [el('strong', {text: row.criterion}), row.source ? el('span', {cls: 'compare-source', text: row.source}) : null]),
        cell(1, [document.createTextNode(row.company || '—')]),
        cell(2, row.alone_status
          ? [document.createTextNode(row.alone_requirement || '—'), status(row.alone_status)]
          : [el('span', {cls: 'compare-muted', text: 'Only for a JV'})]),
        cell(3, [document.createTextNode(row.lead_member || 'Not stated'), status(row.lead_status, row.lead_company)]),
        cell(4, others(row).length ? others(row).map((text) => el('div', {text})) : [document.createTextNode('—')]),
      ]);
      if (jvHelps(row)) tr.classList.add('row-jv-helps');
      else if (row.alone_status === 'not_met' || row.lead_status === 'not_met') tr.classList.add('row-not-met');
      return tr;
    }));
    const table = el('table', {cls: 'table table-sm table-bordered align-top'}, [
      el('thead', {}, [el('tr', {}, labels.map((text) => el('th', {attrs: {scope: 'col'}, text})))]),
      body,
    ]);
    return el('div', {cls: 'analysis'}, [el('div', {cls: 'table-responsive'}, [table])]);
  }

  /**
   * A criterion with its facts and its status, which the bid team can change when the AI got it wrong. A status
   * changed by hand is tagged "Marked by user", with the AI's next to it and a Reset back to that.
   */
  function criterionItem(criterion, tcno) {
    const busy = state.active.has(tcno);
    const select = el('select', {
      cls: 'select status-select', attrs: {'aria-label': `Status of ${criterion.criterion}`},
    }, Object.entries(STATUS_TEXT).map(([value, text]) => el('option', {text, attrs: {value}})));
    select.value = criterion.status;
    select.disabled = busy;
    select.dataset.index = criterion.index;
    select.addEventListener('change', () => markCriterion(tcno, criterion, select.value, select));
    const children = [el('div', {cls: 'criterion-head'}, [el('div', {cls: 'criterion-name', text: criterion.criterion}), select])];
    if (criterion.marked_by_user) {
      const reset = el('button', {
        cls: 'link-button', text: 'Reset',
        attrs: {type: 'button', 'aria-label': `Reset ${criterion.criterion} to the AI's status`},
      });
      reset.disabled = busy;
      reset.addEventListener('click', () => markCriterion(tcno, criterion, null, select));
      children.push(el('div', {cls: 'criterion-mark'}, [
        userTag('Marked by user'),
        el('span', {text: `AI said: ${STATUS_TEXT[criterion.ai_status] || criterion.ai_status}`}),
        reset,
      ]));
    }
    return el('li', {cls: 'criterion'}, [
      ...children,
      el('dl', {cls: 'criterion-facts'}, [
        el('dt', {text: 'Requirement'}), el('dd', {text: criterion.requirement || '—'}),
        el('dt', {text: 'Company'}), el('dd', {text: criterion.company || '—'}),
        el('dt', {text: 'Source'}), el('dd', {cls: 'source', text: criterion.source || '—'}),
      ]),
    ]);
  }

  /**
   * Saves a criterion's status changed by hand (null: back to the AI's), then redraws the analysis, where the
   * criterion may move to another section, keeping focus on its status, and the tender's row with its new verdict.
   */
  async function markCriterion(tcno, criterion, status, select) {
    select.disabled = true;
    try {
      const result = await api(`/api/result/${encodeURIComponent(tcno)}/criteria/${criterion.index}`, {
        method: 'PUT', body: JSON.stringify({status}),
      });
      showChangedResult(result, `.status-select[data-index="${criterion.index}"]`);
    } catch (error) {
      toast(`Could not change the status of ${criterion.criterion}: ${error.message}`);
      select.value = criterion.status;
      select.disabled = false;
    }
  }

  /** Records that the files the AI could not read were checked by hand (checked false takes that back). */
  async function checkUnreadable(tcno, checked, button) {
    button.disabled = true;
    try {
      const result = await api(`/api/result/${encodeURIComponent(tcno)}/unreadable-checked`, {
        method: 'PUT', body: JSON.stringify({checked}),
      });
      showChangedResult(result, '.unreadable-toggle');
    } catch (error) {
      toast(`Could not record the check of the unreadable files: ${error.message}`);
      button.disabled = false;
    }
  }

  /**
   * Shows a result changed by hand: the tender's row and the verdict filters, and the open analysis, with focus
   * back on the control matching selector, unfolding the section it may have moved into.
   */
  function showChangedResult(result, selector) {
    const tcno = result.tcno;
    const summary = state.results.get(tcno);
    if (summary) state.results.set(tcno, {...summary, verdict: result.verdict, score: result.score, checks: result.checks});
    updateRow(tcno);
    applyFilters();
    if (state.details.tcno !== tcno) return;
    renderDetails(result);
    const control = els.modalSummary.querySelector(selector);
    if (control) {
      const folded = control.closest('details');
      if (folded) folded.open = true;
      control.focus();
    }
  }

  /* ---------- deep analysis ---------- */

  // The agents of a deep analysis, in the order they first run, by the keys /api/deep-analysis reports
  const DEEP_AGENTS = [
    ['ingestion', 'Ingestion & context'],
    ['profiler', 'Company profiler'],
    ['audit', 'Eligibility audit'],
    ['status_review', 'Discrepancy & status review'],
    ['supervisor', 'Supervisor / judge'],
  ];
  const DEEP_AGENT_NAMES = Object.fromEntries(DEEP_AGENTS);
  const DEEP_POLL_MS = 2000;
  const AUDIT_TEXT = {pass: 'Pass', fail: 'Fail', insufficient_data: 'Insufficient data', not_applicable: 'Not applicable'};
  const AUDIT_CLASS = {pass: 'status-met', fail: 'status-not-met', insufficient_data: 'status-unknown'};
  const FINAL_TEXT = {...STATUS_TEXT, not_applicable: 'Not applicable'};
  const RISK_TEXT = {high: 'High bias risk', medium: 'Medium bias risk', low: 'Low bias risk', none: 'No bias risk'};
  const ADVICE_TEXT = {keep: 'Keep the mark', revert: 'Go back to the AI\'s status', verify: 'Verify it before relying on it'};
  const OVERRIDE_TEXT = {unreadable_checked: 'Unread files marked checked', decision: 'Shortlist or drop decision', verdict: 'Verdict'};
  const ISSUE_TEXT = {missing: 'Missing', outdated: 'Outdated', mismatch: 'Inconsistent', expired: 'Expired', ambiguous: 'Unclear'};
  const percent = (value) => (value === null || value === undefined ? '—' : `${Math.round(value * 100)}%`);

  /** Loads the open tender's deep analysis and shows it. */
  async function loadDeep(tcno) {
    clearTimeout(state.deep.timer);
    state.deep.tcno = tcno;
    try {
      const deep = await api(`/api/deep-analysis/${encodeURIComponent(tcno)}`);
      if (state.deep.tcno === tcno) showDeep(deep);
    } catch (error) {
      if (state.deep.tcno !== tcno) return;
      els.modalDeep.hidden = false;
      els.modalDeep.replaceChildren(deepTitle(),
        el('p', {cls: 'message-error', text: `Could not load the deep analysis: ${error.message}`}));
    }
  }

  /** Shows a deep analysis state, polling again while it is queued or running, and says when it ends. */
  function showDeep(deep) {
    clearTimeout(state.deep.timer);
    const was = state.deep.status;
    state.deep.status = deep.status;
    renderDeep(deep);
    if (deep.status === 'queued' || deep.status === 'running') {
      state.deep.timer = setTimeout(() => loadDeep(deep.tcno), DEEP_POLL_MS);
    } else if (was === 'queued' || was === 'running') {
      announce(deep.status === 'completed' ? 'The deep analysis is ready.' : 'The deep analysis failed.');
    }
  }

  function stopDeep() {
    clearTimeout(state.deep.timer);
    state.deep = {tcno: null, timer: null, status: null};
  }

  async function startDeep(tcno, button) {
    button.disabled = true;
    try {
      const deep = await api(`/api/deep-analysis/${encodeURIComponent(tcno)}`, {method: 'POST'});
      if (state.deep.tcno !== tcno) return;
      showDeep(deep);
      // The button is gone: keep focus in the section, on its title
      const title = $('deep-title');
      if (title) title.focus();
    } catch (error) {
      toast(`Could not start the deep analysis: ${error.message}`);
      button.disabled = false;
    }
  }

  const deepTitle = () => el('h3', {cls: 'analysis-title', text: 'Deep analysis', attrs: {id: 'deep-title', tabindex: '-1'}});

  function deepButton(tcno, text) {
    const button = el('button', {cls: 'button button-secondary button-sm', text, attrs: {type: 'button'}});
    if (state.active.has(tcno)) {
      button.disabled = true;
      button.title = 'Wait until the eligibility check of this tender finishes';
    }
    button.addEventListener('click', () => startDeep(tcno, button));
    return button;
  }

  /**
   * The deep analysis section: the offer to run one, the agents at work, why the last run failed, and the last
   * report. Redrawn on every poll, keeping focus on the title if it was there.
   */
  function renderDeep(deep) {
    const running = deep.status === 'queued' || deep.status === 'running';
    const report = running ? null : deep.analysis;
    const head = el('div', {cls: 'deep-head'}, [deepTitle()]);
    if (report) {
      head.append(el('div', {cls: 'deep-actions'}, [
        el('a', {
          cls: 'link-button', text: 'Download JSON',
          attrs: {href: `/api/deep-analysis/${encodeURIComponent(deep.tcno)}/file`, download: ''},
        }),
        deepButton(deep.tcno, 'Run again'),
      ]));
    }
    const children = [head];
    if (running) children.push(deepProgress(deep));
    else if (deep.status === 'failed') children.push(deepFailure(deep, !report));
    else if (!report) children.push(deepOffer(deep.tcno));
    if (report) children.push(...deepReport(deep, deep.status === 'failed'));
    const refocus = document.activeElement && document.activeElement.id === 'deep-title';
    els.modalDeep.hidden = false;
    els.modalDeep.replaceChildren(...children);
    if (refocus) $('deep-title').focus();
  }

  function deepOffer(tcno) {
    return el('div', {cls: 'details-criteria'}, [
      el('p', {text: 'Five AI agents review the criteria, the company profile, this result and the statuses marked by '
        + 'user, settle where they disagree, and give a determination with confidence scores and data gaps. It takes a '
        + 'few minutes and leaves the result above as it is.'}),
      el('div', {cls: 'actions'}, [deepButton(tcno, 'Run deep analysis')]),
    ]);
  }

  /** The five agents: done, running (with the processing dot) or waiting, under what the running one does. */
  function deepProgress(deep) {
    const progress = deep.progress || {};
    const queued = deep.status === 'queued';
    const done = new Set(progress.done || []);
    const steps = DEEP_AGENTS.map(([key, label]) => {
      const now = queued ? 'waiting' : (progress.agent === key && 'running') || (done.has(key) ? 'done' : 'waiting');
      const mark = now === 'running'
        ? el('span', {cls: 'deep-dot', attrs: {'aria-hidden': 'true'}})
        : icon(now === 'done' ? 'bi-check-circle' : 'bi-circle');
      const round = now === 'running' && progress.round ? ` · re-examination ${progress.round}` : '';
      return el('li', {cls: `deep-agent is-${now}`}, [
        mark, el('span', {text: `${label}${round}`}), el('span', {cls: 'visually-hidden', text: ` (${now})`}),
      ]);
    });
    return el('div', {cls: 'deep-run'}, [
      el('p', {cls: 'deep-stage', text: queued ? 'Queued: one deep analysis runs at a time.' : progress.stage || 'Starting'}),
      el('ol', {cls: 'deep-agents'}, steps),
      el('p', {cls: 'deep-time', text: queued ? 'It starts when the one before it finishes.'
        : `Running for ${formatDuration(progress.elapsed_seconds || 0)}. You can close this window: the report is saved when it is done.`}),
    ]);
  }

  function deepFailure(deep, withButton) {
    return el('div', {cls: 'summary tone-failed'}, [
      el('div', {cls: 'summary-head'}, [
        pill('failed', 'Failed'), el('span', {cls: 'summary-headline', text: deep.error_title || 'The deep analysis failed'}),
      ]),
      el('p', {cls: 'summary-action', text: deep.error_action || 'Run it again.'}),
      withButton ? deepButton(deep.tcno, 'Run deep analysis again') : null,
      el('details', {cls: 'technical-detail'}, [
        el('summary', {text: 'Technical detail'}), el('code', {text: deep.error || 'No error message.'}),
      ]),
    ]);
  }

  /** The report: the determination first, then each criterion through every agent, the marks, gaps and log. */
  function deepReport(deep, failedSince) {
    const report = deep.analysis;
    const final = report.final || {};
    const verdict = verdictOf(final.determination);
    const meta = [formatDateTime(report.finished_at)];
    if (report.duration_seconds !== undefined) meta.push(`took ${formatDuration(report.duration_seconds)}`);
    if (report.rounds) meta.push(plural(report.rounds, 're-examination round'));
    if (report.tokens && report.tokens.total.total) meta.push(`${report.tokens.total.total.toLocaleString('en-IN')} LLM tokens`);
    const nodes = [el('p', {cls: 'deep-meta', text: meta.filter(Boolean).join(' · ')})];
    if (failedSince) nodes.unshift(el('p', {cls: 'deep-intro', text: 'The last deep analysis that finished:'}));
    if (deep.stale) {
      nodes.push(el('p', {cls: 'deep-stale'}, [icon('bi-exclamation-circle'), el('span', {
        text: 'The result or the company profile has changed since this deep analysis. Run it again to review them as they are now.',
      })]));
    }
    const panel = [
      el('div', {cls: 'summary-head'}, [
        pill(verdict.tone, verdict.label),
        el('span', {cls: 'summary-headline', text: final.confidence === null || final.confidence === undefined
          ? 'Confidence not given' : `Confidence ${percent(final.confidence)}`}),
      ]),
      el('p', {cls: 'summary-counts', text: final.agrees_with_current
        ? 'Agrees with the verdict above.' : `Differs from the verdict above: ${verdictOf(final.current_verdict).label}.`}),
    ];
    if (final.summary) panel.push(el('p', {cls: 'deep-summary', text: final.summary}));
    if ((final.deciding_criteria || []).length) {
      panel.push(el('p', {cls: 'summary-counts', text: `Decided by ${final.deciding_criteria.join(', ')}`}));
    }
    nodes.push(el('div', {cls: `summary tone-${verdict.tone} deep-verdict`}, panel));

    const section = (title, children) => {
      if (!children.length) return;
      nodes.push(el('h4', {cls: 'summary-section-title section-plain', text: title}), ...children);
    };
    section('Criteria matrix', (report.criteria || []).length ? [deepMatrix(report.criteria)] : []);
    section('Statuses marked by user', deepMarks(report.status_review || {}));
    section('Data gaps', listOrEmpty(report.data_gaps, (gap) => [
      el('strong', {text: gap.gap}),
      deepNote([gap.needed_for && `For ${gap.needed_for}`, gap.severity && `${gap.severity} severity`].filter(Boolean).join(' · ')),
      gap.how_to_close ? deepNote(`To close it: ${gap.how_to_close}`) : null,
    ]));
    section('Company profile checks', listOrEmpty(report.profile_flags, (flag) => [
      el('strong', {text: `${ISSUE_TEXT[flag.issue] || flag.issue}: ${flag.field}`}),
      document.createTextNode(` ${flag.detail}`),
      deepNote([flag.needed_for && `Needed for ${flag.needed_for}`, flag.severity && `${flag.severity} severity`,
        flag.verify_with && `verify with ${flag.verify_with}`].filter(Boolean).join(' · ')),
    ]));
    nodes.push(deepLog(report.audit_log || []));
    return nodes;
  }

  const deepNote = (text) => (text ? el('span', {cls: 'deep-note', text}) : null);

  /** A list of items, each drawn by item(), or nothing when there are none. */
  const listOrEmpty = (items, item) => ((items || []).length
    ? [el('ul', {cls: 'deep-list'}, items.map((entry) => el('li', {}, item(entry))))] : []);

  /** One row per criterion: its status in this result, the audit's result, and the supervisor's final status. */
  function deepMatrix(rows) {
    const labels = ['Criterion', 'This result', 'Audit', 'Final', 'Confidence'];
    const cell = (index, children) => el('td', {attrs: {'data-label': labels[index]}}, children);
    const status = (cls, text) => el('span', {cls: `compare-status ${cls || ''}`.trim(), text});
    const body = el('tbody', {}, rows.map((row) => {
      const current = row.current_status
        ? [status(STATUS_CLASS[row.current_status], STATUS_TEXT[row.current_status]),
          row.marked_by_user ? deepNote(`Marked by user · AI said ${STATUS_TEXT[row.ai_status] || row.ai_status}`) : null]
        : [el('span', {cls: 'compare-muted', text: 'Found by the audit only'})];
      const tr = el('tr', {}, [
        cell(0, [
          el('strong', {text: row.criterion}),
          row.source ? el('span', {cls: 'compare-source', text: row.source}) : null,
          row.conflict ? el('span', {cls: 'deep-conflict'}, [icon('bi-arrow-left-right'), document.createTextNode('Agents disagreed')]) : null,
        ]),
        cell(1, current),
        cell(2, row.audit_result
          ? [status(AUDIT_CLASS[row.audit_result], AUDIT_TEXT[row.audit_result]), deepNote(row.audit_reason)]
          : [el('span', {cls: 'compare-muted', text: 'Not assessed'})]),
        cell(3, [status(STATUS_CLASS[row.final_status], FINAL_TEXT[row.final_status] || row.final_status),
          deepNote(row.resolution || row.basis)]),
        cell(4, [el('span', {cls: 'deep-confidence', text: percent(row.confidence)})]),
      ]);
      if (row.final_status === 'not_met') tr.classList.add('row-not-met');
      return tr;
    }));
    const table = el('table', {cls: 'table table-sm table-bordered align-top deep-matrix'}, [
      el('thead', {}, [el('tr', {}, labels.map((text) => el('th', {attrs: {scope: 'col'}, text})))]),
      body,
    ]);
    return el('div', {cls: 'analysis'}, [el('div', {cls: 'table-responsive'}, [table])]);
  }

  /** What the status review agent found in the statuses marked by user and the other overrides. */
  function deepMarks(review) {
    const nodes = [];
    if (review.summary) nodes.push(el('p', {cls: 'deep-intro', text: review.summary}));
    const fact = (term, text) => (text ? [el('dt', {text: term}), el('dd', {text})] : []);
    const head = (name, risk) => el('div', {cls: 'criterion-head'}, [
      el('div', {cls: 'criterion-name', text: name}),
      el('span', {cls: `deep-risk risk-${risk}`, text: RISK_TEXT[risk] || risk}),
    ]);
    const items = (review.reviews || []).map((item) => el('li', {cls: 'criterion'}, [
      head(item.criterion, item.bias_risk),
      el('dl', {cls: 'criterion-facts'}, [
        ...fact('Status', item.marked_by_user
          ? `${STATUS_TEXT[item.user_status]}, marked by user; the AI said ${STATUS_TEXT[item.ai_status]}`
          : `${STATUS_TEXT[item.ai_status]}, as the AI had it`),
        ...fact('Audit', AUDIT_TEXT[item.audit_result] || 'Not assessed'),
        ...fact('Finding', item.finding),
        ...fact('Why it may be so', (item.possible_reasons || []).join('; ')),
        ...fact('Advice', ADVICE_TEXT[item.recommendation] || item.recommendation),
      ]),
    ]));
    items.push(...(review.overrides || []).map((item) => el('li', {cls: 'criterion'}, [
      head(OVERRIDE_TEXT[item.kind] || item.kind, item.bias_risk),
      el('dl', {cls: 'criterion-facts'}, [...fact('Override', item.detail), ...fact('Finding', item.finding)]),
    ])));
    if (items.length) nodes.push(el('ul', {cls: 'criteria-list'}, items));
    return nodes;
  }

  /** Every step the agents took, folded away: when, which agent, what it found. */
  function deepLog(entries) {
    return el('details', {cls: 'deep-log'}, [
      el('summary', {text: `Audit log (${plural(entries.length, 'step')})`}),
      el('ol', {}, entries.map((entry) => {
        const date = new Date(entry.at);
        const when = Number.isNaN(date.getTime()) ? '' : `${formatClock(date)}:${pad(date.getSeconds())}`;
        const who = `${DEEP_AGENT_NAMES[entry.agent] || entry.agent}${entry.round ? `, re-examination ${entry.round}` : ''}`;
        // The model the step ran on, when it called one (each agent's is set in .env)
        const model = entry.model ? [document.createTextNode(' '), el('span', {cls: 'deep-log-meta', text: entry.model})] : [];
        return el('li', {}, [
          el('span', {cls: 'deep-log-meta', text: when}), document.createTextNode(' '),
          el('strong', {text: who}), ...model, document.createTextNode(` · ${entry.event}`),
        ]);
      })),
    ]);
  }

  /** Markdown -> sanitised HTML with table styles; plain text if the libraries did not load. */
  function renderMarkdown(text) {
    const container = document.createElement('div');
    if (!window.marked || !window.DOMPurify) {
      container.append(el('pre', {cls: 'plain-text', text}));
      return container;
    }
    container.innerHTML = DOMPurify.sanitize(marked.parse(text, {gfm: true, breaks: true}));

    // The modal is h2 and "Full analysis" h3, so the model's headings start at h4
    const headings = [...container.querySelectorAll('h1, h2, h3, h4, h5, h6')];
    const top = Math.min(...headings.map((h) => Number(h.tagName[1])));
    headings.forEach((heading) => {
      const level = Math.min(6, 4 + Number(heading.tagName[1]) - top);
      const replacement = document.createElement(`h${level}`);
      replacement.append(...heading.childNodes);
      heading.replaceWith(replacement);
    });

    container.querySelectorAll('table').forEach((table) => {
      table.classList.add('table', 'table-sm', 'table-bordered', 'align-top');
      // Narrow screens stack each row as label / value pairs; the labels come from the header. File names in a
      // Source column are set in mono.
      const labels = [...table.querySelectorAll('thead th')].map((th) => th.textContent.trim());
      table.querySelectorAll('tbody tr').forEach((row) => {
        [...row.cells].forEach((cell, index) => {
          if (!labels[index]) return;
          cell.setAttribute('data-label', labels[index]);
          if (labels[index].toLowerCase() === 'source') cell.classList.add('cell-source');
        });
      });
      sortCriteriaRows(table);
      const wrapper = el('div', {cls: 'table-responsive'});
      table.replaceWith(wrapper);
      wrapper.append(table);
    });
    container.querySelectorAll('a').forEach((link) => {
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
    });
    return container;
  }

  /** In a table with a Status column: colours the status, tints Not met rows and lists them first. */
  function sortCriteriaRows(table) {
    const headers = [...table.querySelectorAll('thead th')].map((th) => th.textContent.trim().toLowerCase());
    // The JV table's "Company as lead member" column reads Met / Not met as well
    const column = headers.findIndex((header) => header === 'status' || header.startsWith('company as lead'));
    const body = table.tBodies[0];
    if (column < 0 || !body) return;
    const rank = (row) => {
      const cell = row.cells[column];
      const value = cell ? cell.textContent.trim().toLowerCase() : '';
      const match = STATUS_STYLES.find(([pattern]) => pattern.test(value));
      if (!match) return STATUS_STYLES.length;
      cell.classList.add(match[1]);
      if (match[2] === 0) row.classList.add('row-not-met');
      return match[2];
    };
    const rows = [...body.rows].map((row, index) => ({row, index, rank: rank(row)}));
    rows.sort((a, b) => a.rank - b.rank || a.index - b.index).forEach(({row}) => body.append(row));
  }

  function updateDetailsNav() {
    const index = state.details.order.indexOf(state.details.tcno);
    const focused = document.activeElement;
    els.prevBtn.disabled = index <= 0;
    els.nextBtn.disabled = index < 0 || index >= state.details.order.length - 1;
    // Reaching the first or last tender disables the button that has focus: move focus instead of losing it
    if ((focused === els.prevBtn || focused === els.nextBtn) && focused.disabled) {
      const other = focused === els.nextBtn ? els.prevBtn : els.nextBtn;
      (other.disabled ? els.modal.querySelector('[data-bs-dismiss="modal"]') : other).focus();
    }
  }

  function moveDetails(step) {
    const index = state.details.order.indexOf(state.details.tcno);
    const next = state.details.order[index + step];
    if (next) openDetails(next);
  }

  function updateDecisionButtons() {
    const entry = state.decisions.get(state.details.tcno);
    const decision = entry ? entry.decision : null;
    const set = (button, value, verb) => {
      const pressed = decision === value;
      button.setAttribute('aria-pressed', String(pressed));
      button.querySelector('span').textContent = pressed ? DECISIONS[value].label : verb;
      button.title = pressed ? 'Click again to clear the decision' : '';
    };
    set(els.shortlistBtn, 'shortlisted', 'Shortlist');
    set(els.dropBtn, 'dropped', 'Drop');
  }

  /** Saves shortlisted / dropped / null for a tender and redraws what shows it. Returns true on success. */
  async function saveDecision(tcno, decision) {
    try {
      const data = await api(`/api/decision/${encodeURIComponent(tcno)}`, {method: 'PUT', body: JSON.stringify({decision})});
      if (data.decision) state.decisions.set(tcno, data.decision);
      else state.decisions.delete(tcno);
      updateRow(tcno);
      applyFilters();
      if (state.details.tcno === tcno) updateDecisionButtons();
      return true;
    } catch (error) {
      toast(`Could not save the decision: ${error.message}`);
      return false;
    }
  }

  /** The modal's Shortlist / Drop: choosing the current decision again clears it. */
  async function toggleDecision(value) {
    const tcno = state.details.tcno;
    const entry = state.decisions.get(tcno);
    els.shortlistBtn.disabled = els.dropBtn.disabled = true;
    await saveDecision(tcno, entry && entry.decision === value ? null : value);
    els.shortlistBtn.disabled = els.dropBtn.disabled = false;
  }

  /* ---------- company profile ---------- */

  /** Loads the saved profile into the editor and opens it; a fresh load, in case someone else changed it. */
  async function openCompany() {
    els.companyEdit.disabled = true;
    try {
      const profile = await api('/api/company');
      state.company = {blocks: profile.blocks, saved: JSON.stringify(profile.blocks)};
      renderCompany(profile);
      renderCompanyForm();
      companyModal.show();
    } catch (error) {
      toast(`Could not load the company profile: ${error.message}`);
    } finally {
      els.companyEdit.disabled = false;
    }
  }

  /** The app bar's company name. */
  function renderCompany(profile) {
    els.companyName.textContent = profile.name;
  }

  /**
   * The editor form, block by block. Each section (a level-2 heading and what follows it) ends with
   * Add sub-heading and Add table, and the form ends with Add heading.
   */
  function renderCompanyForm() {
    const nodes = [];
    let section = null;  // index of the open section's heading
    const endSection = () => {
      if (section === null) return;
      const describedBy = {'aria-describedby': `profile-heading-${section}`};
      nodes.push(el('div', {cls: 'profile-section-actions'}, [
        addHeadingButton('Add sub-heading', {'data-add-sub': section, ...describedBy}),
        addHeadingButton('Add table', {'data-add-table': section, ...describedBy}),
      ]));
      section = null;
    };
    state.company.blocks.forEach((block, index) => {
      if (block.type === 'heading' && block.level <= 2) {
        endSection();
        if (block.level === 2) section = index;
      }
      const node = profileBlock(block, index);
      if (node) nodes.push(node);
    });
    endSection();
    nodes.push(el('div', {cls: 'profile-form-end'}, [addHeadingButton('Add heading', {'data-add-heading': ''})]));
    els.companyForm.replaceChildren(...nodes);
  }

  const addHeadingButton = (label, attrs) => el('button', {
    cls: 'button button-secondary button-sm profile-add-heading', attrs: {type: 'button', ...attrs},
  }, [icon('bi-plus-lg'), document.createTextNode(` ${label}`)]);

  /**
   * One block of the profile as form controls: the level-1 heading is the profile title field, other headings
   * are heading-styled fields with a remove button, tables become rows of fields, free text a text box. A ---
   * rule shows nothing. Every control carries data-block (and data-row / data-col in tables) so edits land in
   * state.company.blocks.
   */
  function profileBlock(block, index) {
    if (block.type === 'heading' && block.level === 1) {
      const input = el('input', {cls: 'profile-input', attrs: {type: 'text', id: 'profile-title', 'data-block': index}});
      input.value = block.text;
      return el('div', {cls: 'profile-title'}, [
        el('label', {cls: 'profile-label', text: 'Profile title', attrs: {for: 'profile-title'}}), input,
      ]);
    }
    if (block.type === 'heading') {
      const sub = block.level > 2;
      const kind = sub ? 'Sub-heading' : 'Heading';
      const input = el('input', {
        cls: 'profile-heading-input',
        attrs: {type: 'text', id: `profile-heading-${index}`, 'aria-label': kind, placeholder: kind, 'data-block': index},
      });
      input.value = block.text;
      // A real heading element around the field, so screen readers still move through the sections
      return el('div', {cls: `profile-heading-row profile-heading-row-${sub ? 3 : 2}`}, [
        el(sub ? 'h4' : 'h3', {cls: `profile-heading profile-heading-${sub ? 3 : 2}`}, [input]),
        el('button', {
          cls: 'icon-button profile-remove-heading',
          attrs: {type: 'button', 'aria-label': `Remove ${kind.toLowerCase()}`, 'aria-describedby': input.id, 'data-remove-heading': index},
        }, [icon('bi-trash3')]),
      ]);
    }
    if (block.type === 'table') return profileTable(block, index);
    if (block.type === 'text') {
      const box = el('textarea', {cls: 'profile-input profile-text', attrs: {rows: '4', 'aria-label': 'Notes', 'data-block': index}});
      box.value = block.text;
      return box;
    }
    return null;
  }

  /**
   * A table of the profile, every part editable: the column names are fields with a remove button, every cell
   * is a field, and the footer adds rows and columns or removes the table.
   */
  function profileTable(block, index) {
    const header = block.columns.map((column, c) => {
      const input = el('input', {
        cls: 'profile-col-input',
        attrs: {type: 'text', id: `profile-col-${index}-${c}`, 'aria-label': `Column ${c + 1} name`, placeholder: 'Column name', 'data-block': index, 'data-col': c},
      });
      input.value = column;
      return el('th', {attrs: {scope: 'col'}}, [el('div', {cls: 'profile-col'}, [
        input,
        el('button', {
          cls: 'icon-button profile-col-remove',
          attrs: {type: 'button', 'aria-label': 'Remove column', 'aria-describedby': input.id, 'data-remove-col': c, 'data-block': index},
        }, [icon('bi-x-lg')]),
      ])]);
    });
    const rows = block.rows.map((row, r) => el('tr', {}, [
      ...block.columns.map((column, c) => {
        const input = el('input', {
          cls: c === 0 ? 'profile-cell profile-cell-key' : 'profile-cell',
          attrs: {type: 'text', 'aria-label': `${column}, row ${r + 1}`, 'data-block': index, 'data-row': r, 'data-col': c},
        });
        input.value = row[c];
        return el('td', {}, [input]);
      }),
      el('td', {cls: 'profile-remove-cell'}, [
        el('button', {
          cls: 'icon-button profile-remove',
          attrs: {type: 'button', 'aria-label': `Remove row ${r + 1}${row[0] ? `: ${row[0]}` : ''}`, 'data-block': index, 'data-row': r},
        }, [icon('bi-x-lg')]),
      ]),
    ]));
    return el('div', {cls: 'profile-block', attrs: {'data-block': index}}, [
      el('div', {cls: 'profile-table-wrap'}, [
        el('table', {cls: 'profile-table'}, [
          el('thead', {}, [el('tr', {}, [
            ...header,
            el('th', {cls: 'profile-remove-cell', attrs: {scope: 'col'}}, [el('span', {cls: 'visually-hidden', text: 'Remove'})]),
          ])]),
          el('tbody', {}, rows),
        ]),
      ]),
      el('div', {cls: 'profile-table-foot'}, [
        el('button', {cls: 'link-button profile-add', attrs: {type: 'button', 'data-block': index}}, [
          icon('bi-plus-lg'), document.createTextNode(' Add row'),
        ]),
        el('button', {cls: 'link-button profile-add-col', attrs: {type: 'button', 'data-add-col': index}}, [
          icon('bi-plus-lg'), document.createTextNode(' Add column'),
        ]),
        el('button', {cls: 'link-button profile-remove-table', attrs: {type: 'button', 'data-remove-table': index}}, [
          icon('bi-trash3'), document.createTextNode(' Remove table'),
        ]),
      ]),
    ]);
  }

  /** Redraws one table after a row is added or removed, and returns its new element. */
  function redrawProfileTable(index) {
    const node = profileTable(state.company.blocks[index], index);
    els.companyForm.querySelector(`.profile-block[data-block="${index}"]`).replaceWith(node);
    return node;
  }

  function addProfileRow(index) {
    const block = state.company.blocks[index];
    block.rows.push(block.columns.map(() => ''));
    redrawProfileTable(index).querySelector('tbody tr:last-child input').focus();
  }

  /** Removes a row and moves focus to the row that took its place, or to Add row when none is left. */
  function removeProfileRow(index, row) {
    state.company.blocks[index].rows.splice(row, 1);
    const node = redrawProfileTable(index);
    const buttons = node.querySelectorAll('.profile-remove');
    (buttons[Math.min(row, buttons.length - 1)] || node.querySelector('.profile-add')).focus();
  }

  /** The index just past everything under a heading: the next heading at its level or above, or the end. */
  function headingEnd(index) {
    const {blocks} = state.company;
    let end = index + 1;
    while (end < blocks.length && !(blocks[end].type === 'heading' && blocks[end].level <= blocks[index].level)) end += 1;
    return end;
  }

  /** Where new content for a section goes: at its end, before the --- that closes it. */
  function sectionInsertAt(section) {
    const {blocks} = state.company;
    let at = headingEnd(section);
    while (at > section + 1 && blocks[at - 1].type === 'rule') at -= 1;
    return at;
  }

  const emptyTable = () => ({type: 'table', columns: ['Item', 'Value'], rows: [['', '']]});
  const hasValues = (rows) => rows.some((row) => row.some((cell) => cell.trim()));

  /** After a redraw of the whole form: focuses the first field at or after block `index`, else Add heading. */
  function focusFrom(index) {
    const next = [...els.companyForm.querySelectorAll('input[data-block], textarea[data-block]')].find((field) => Number(field.dataset.block) >= index);
    (next || els.companyForm.querySelector('[data-add-heading]')).focus();
  }

  /** Adds an empty Item / Value table at the end of a section and focuses its first column name. */
  function addProfileTable(section) {
    const at = sectionInsertAt(section);
    state.company.blocks.splice(at, 0, emptyTable());
    renderCompanyForm();
    const name = $(`profile-col-${at}-0`);
    name.focus();
    name.select();
  }

  /** Removes a table, asking first when it holds values. */
  function removeProfileTable(index) {
    if (hasValues(state.company.blocks[index].rows) && !window.confirm('Remove this table and its values?')) return;
    state.company.blocks.splice(index, 1);
    renderCompanyForm();
    focusFrom(index);
  }

  function addProfileColumn(index) {
    const block = state.company.blocks[index];
    block.columns.push('');
    block.rows.forEach((row) => row.push(''));
    redrawProfileTable(index);
    $(`profile-col-${index}-${block.columns.length - 1}`).focus();
  }

  /** Removes a column, asking first when it holds values; removing the only column removes the table. */
  function removeProfileColumn(index, col) {
    const block = state.company.blocks[index];
    if (block.columns.length === 1) {
      removeProfileTable(index);
      return;
    }
    const name = block.columns[col].trim();
    if (block.rows.some((row) => row[col].trim())
      && !window.confirm(`Remove the ${name ? `"${name}" ` : ''}column and its values?`)) return;
    block.columns.splice(col, 1);
    block.rows.forEach((row) => row.splice(col, 1));
    redrawProfileTable(index);
    $(`profile-col-${index}-${Math.min(col, block.columns.length - 1)}`).focus();
  }

  /**
   * Adds a heading with an empty Item / Value table under it, and focuses the heading: a sub-heading at the
   * end of the section whose heading is at `section`, or, without it, a numbered heading at the end of the form.
   */
  function addProfileHeading(section) {
    const {blocks} = state.company;
    let at;
    let added;
    if (section === undefined) {
      // Number it after the last numbered heading ("8. Compliance Checklist" -> "9. "); keep the --- between sections
      const numbers = blocks.filter((b) => b.type === 'heading' && b.level === 2)
        .map((b) => Number((/^\s*(\d+)\./.exec(b.text) || [])[1])).filter(Boolean);
      at = blocks.length;
      added = [{type: 'heading', level: 2, text: numbers.length ? `${Math.max(...numbers) + 1}. ` : ''}, emptyTable()];
      if (blocks.length && blocks[at - 1].type !== 'rule') added.unshift({type: 'rule'});
    } else {
      at = sectionInsertAt(section);
      added = [{type: 'heading', level: blocks[section].level + 1, text: ''}, emptyTable()];
    }
    blocks.splice(at, 0, ...added);
    renderCompanyForm();
    const input = $(`profile-heading-${at + added.length - 2}`);
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  }

  /** Removes a heading with everything under it, asking first when any of that holds a value. */
  function removeProfileHeading(index) {
    const {blocks} = state.company;
    const end = headingEnd(index);
    const filled = blocks.slice(index + 1, end).some((b) => (b.type === 'table' && hasValues(b.rows))
      || ((b.type === 'text' || b.type === 'heading') && b.text.trim()));
    const name = blocks[index].text.trim();
    if (filled && !window.confirm(`Remove ${name ? `"${name}"` : 'this heading'} and everything under it?`)) return;
    blocks.splice(index, end - index);
    renderCompanyForm();
    const next = [...els.companyForm.querySelectorAll('.profile-heading-input')].find((input) => Number(input.dataset.block) >= index);
    (next || els.companyForm.querySelector('[data-add-heading]')).focus();
  }

  /** The first heading or column without a name, with what to tell the user, or null. Mirrors the server's check. */
  function unnamedPart() {
    for (const [index, block] of state.company.blocks.entries()) {
      if (block.type === 'heading' && block.level > 1 && !block.text.replace(/^\s*\d+\.\s*/, '').trim()) {
        return {field: $(`profile-heading-${index}`), message: 'Every heading and sub-heading needs a name. Name it, or remove it.'};
      }
      const col = block.type === 'table' ? block.columns.findIndex((column) => !column.trim()) : -1;
      if (col >= 0) return {field: $(`profile-col-${index}-${col}`), message: 'Every table column needs a name. Name it, or remove the column.'};
    }
    return null;
  }

  const companyChanged = () => JSON.stringify(state.company.blocks) !== state.company.saved;

  async function saveCompany() {
    if (state.companyError) state.companyError.hide();
    const unnamed = unnamedPart();
    if (unnamed) {
      state.companyError = toast(unnamed.message);
      unnamed.field.focus();
      return;
    }
    els.companySave.disabled = true;
    try {
      const profile = await api('/api/company', {method: 'PUT', body: JSON.stringify({blocks: state.company.blocks})});
      state.company = {blocks: profile.blocks, saved: JSON.stringify(profile.blocks)};
      renderCompany(profile);
      companyModal.hide();
      toast('Company profile saved. Tenders checked from now on use it.', 'success');
    } catch (error) {
      state.companyError = toast(`Could not save the company profile: ${error.message}`);
    } finally {
      els.companySave.disabled = false;
    }
  }

  /* ---------- wiring ---------- */

  els.search.addEventListener('input', applyFilters);
  els.orgFilter.addEventListener('change', applyFilters);
  els.decisionFilter.addEventListener('change', applyFilters);
  els.closingFilter.addEventListener('change', applyFilters);
  els.refreshBtn.addEventListener('click', () => loadTenders());
  els.pagePrev.addEventListener('click', () => showList({page: state.page - 1}));
  els.pageNext.addEventListener('click', () => showList({page: state.page + 1}));
  // A new page size opens the page holding the first tender shown now, so the list keeps its place
  els.pageSize.addEventListener('change', () => {
    const size = Number(els.pageSize.value);
    showList({page: Math.floor(((state.page - 1) * state.pageSize) / size) + 1, size});
  });
  els.filtersBtn.addEventListener('click', () => setFiltersOpen(els.filtersForm.hidden));
  els.filtersForm.addEventListener('submit', applySearchFilters);
  els.filtersClear.addEventListener('click', clearSearchFilters);
  // A range problem shown on one field is gone once any filter changes
  els.filtersForm.addEventListener('input', () => {
    [...els.filtersForm.elements].forEach((field) => field.setCustomValidity && field.setCustomValidity(''));
    markEmptyDates();
  });
  els.filtersForm.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') setFiltersOpen(false);
  });
  els.processBtn.addEventListener('click', processSelected);
  els.barProcess.addEventListener('click', processSelected);
  els.clearSelection.addEventListener('click', clearSelection);
  els.stopBtn.addEventListener('click', stopRun);
  els.notifyBtn.addEventListener('click', toggleNotify);
  els.sortButtons.forEach((button) => button.addEventListener('click', () => setSort(button.dataset.sort)));

  els.filters.addEventListener('click', (event) => {
    const tab = event.target.closest('.tab');
    if (tab) setVerdictFilter(tab.dataset.verdict);
  });

  // Looking at the tab again clears the "Done: ..." note from its title
  document.addEventListener('visibilitychange', () => {
    if (document.hidden || !state.titleNote) return;
    state.titleNote = '';
    renderRun();
  });

  els.runDismiss.addEventListener('click', () => {
    try {
      if (state.job) localStorage.setItem(DISMISSED_KEY, state.job.job_id);
    } catch (_) {
      // Storage blocked: the summary is hidden until the next page load
    }
    els.runPanel.hidden = true;
  });

  els.regenerateToggle.addEventListener('click', () => {
    state.regenerate = !state.regenerate;
    updateSelectionUI();
  });

  els.selectAll.addEventListener('change', () => {
    for (const box of visibleCheckboxes()) {
      box.checked = els.selectAll.checked;
      setSelected(box.dataset.tcno, box.checked);
    }
    updateSelectionUI();
  });

  els.body.addEventListener('change', (event) => {
    if (!event.target.classList.contains('row-check')) return;
    setSelected(event.target.dataset.tcno, event.target.checked);
    updateSelectionUI();
  });

  els.body.addEventListener('click', (event) => {
    const retryBtn = event.target.closest('.retry-btn');
    if (retryBtn) {
      retry(retryBtn.dataset.tcno, retryBtn);
      return;
    }
    const pillButton = event.target.closest('.pill-button');
    if (pillButton) {
      openDetails(pillButton.dataset.tcno);
      return;
    }
    const dropButton = event.target.closest('.row-drop');
    if (dropButton) {
      saveDecision(dropButton.dataset.tcno, isDropped(dropButton.dataset.tcno) ? null : 'dropped');
      return;
    }
    if (event.target.closest('input, button, a, label, select')) return;
    // Anywhere else on a row toggles its selection, as in the design
    const row = event.target.closest('tr[data-tcno]');
    if (!row || String(window.getSelection())) return;
    const box = row.querySelector('.row-check');
    if (box && !box.disabled) box.click();
  });

  // Bootstrap returns focus only for modals opened through data attributes: send it back to the row last viewed
  els.modal.addEventListener('hidden.bs.modal', () => {
    stopDeep();
    const row = state.rows.get(state.details.tcno);
    const button = row && !row.hidden && row.querySelector('.pill-button');
    if (button) button.focus();
  });

  els.prevBtn.addEventListener('click', () => moveDetails(-1));
  els.nextBtn.addEventListener('click', () => moveDetails(1));
  els.shortlistBtn.addEventListener('click', () => toggleDecision('shortlisted'));
  els.dropBtn.addEventListener('click', () => toggleDecision('dropped'));

  els.companyEdit.addEventListener('click', openCompany);
  els.companySave.addEventListener('click', saveCompany);
  els.companyForm.addEventListener('input', (event) => {
    const {block, row, col} = event.target.dataset;
    if (block === undefined) return;
    const target = state.company.blocks[Number(block)];
    const {value} = event.target;
    if (row !== undefined) {
      target.rows[Number(row)][Number(col)] = value;
    } else if (col !== undefined) {
      // A renamed column: its cells' labels follow
      target.columns[Number(col)] = value;
      els.companyForm.querySelectorAll(`.profile-cell[data-block="${block}"][data-col="${col}"]`)
        .forEach((cell) => cell.setAttribute('aria-label', `${value}, row ${Number(cell.dataset.row) + 1}`));
    } else {
      target.text = value;
    }
  });
  els.companyForm.addEventListener('click', (event) => {
    const add = event.target.closest('.profile-add');
    if (add) {
      addProfileRow(Number(add.dataset.block));
      return;
    }
    const remove = event.target.closest('.profile-remove');
    if (remove) {
      removeProfileRow(Number(remove.dataset.block), Number(remove.dataset.row));
      return;
    }
    const addSub = event.target.closest('[data-add-sub]');
    if (addSub || event.target.closest('[data-add-heading]')) {
      addProfileHeading(addSub ? Number(addSub.dataset.addSub) : undefined);
      return;
    }
    const removeHeading = event.target.closest('[data-remove-heading]');
    if (removeHeading) {
      removeProfileHeading(Number(removeHeading.dataset.removeHeading));
      return;
    }
    // Table structure: data-* holds the table's block index (and the column for Remove column)
    const action = event.target.closest('[data-add-table], [data-remove-table], [data-add-col], [data-remove-col]');
    if (!action) return;
    const {addTable, removeTable, addCol, removeCol} = action.dataset;
    if (addTable !== undefined) addProfileTable(Number(addTable));
    else if (removeTable !== undefined) removeProfileTable(Number(removeTable));
    else if (addCol !== undefined) addProfileColumn(Number(addCol));
    else removeProfileColumn(Number(action.dataset.block), Number(removeCol));
  });
  // Ctrl+S / Cmd+S saves the profile instead of the browser's "Save page as"
  els.companyModal.addEventListener('keydown', (event) => {
    if (event.key.toLowerCase() !== 's' || !(event.ctrlKey || event.metaKey) || event.altKey) return;
    event.preventDefault();
    if (!els.companySave.disabled) saveCompany();
  });
  // Cancel, the close button and Esc all ask before throwing away unsaved edits
  els.companyModal.addEventListener('hide.bs.modal', (event) => {
    if (companyChanged() && !window.confirm('Discard your changes to the company profile?')) event.preventDefault();
  });
  els.companyModal.addEventListener('shown.bs.modal', () => {
    const first = els.companyForm.querySelector('input, textarea');
    if (first) first.focus();
  });
  els.companyModal.addEventListener('hidden.bs.modal', () => els.companyEdit.focus());

  els.copyBtn.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(state.details.text);
      toast('Copied to the clipboard.', 'success');
    } catch (_) {
      toast('The browser blocked clipboard access.');
    }
  });

  document.addEventListener('keydown', (event) => {
    if (event.key !== '/' || event.ctrlKey || event.metaKey || event.altKey) return;
    const typing = event.target instanceof Element && event.target.closest('input, textarea, select, [contenteditable="true"]');
    if (typing || document.querySelector('.modal.show')) return;
    event.preventDefault();
    els.search.focus();
  });

  (async function init() {
    try {
      state.notify = localStorage.getItem(NOTIFY_KEY) === '1' && canNotify() && Notification.permission === 'granted';
    } catch (_) {
      state.notify = false;
    }
    buildAmountFields();
    try {
      const saved = JSON.parse(localStorage.getItem(FILTERS_KEY) || '{}');
      if (saved && typeof saved === 'object' && !Array.isArray(saved)) setSearchFilters(saved);
      const size = Number(localStorage.getItem(PAGE_SIZE_KEY));
      if (PAGE_SIZES.includes(size)) state.pageSize = size;
    } catch (_) {
      // Storage blocked or unreadable: start without filters, 20 a page
    }
    fillFilterForm(state.searchFilters);
    renderSortButtons();
    await loadTenders();
    // Pick up a job that is still running, e.g. after a page reload
    try {
      applyProgress(await api('/api/progress'));
      if (isRunning()) schedulePoll();
    } catch (error) {
      toast(`Could not read the processing status: ${error.message}`);
    }
  })();
})();
