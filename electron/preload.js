// Sales Buddy - Electron preload.
//
// Chromium's built-in find UI is disabled in Electron, so Ctrl+F does nothing by
// default. This preload injects a small browser-style find bar into every page.
//
// Matches are painted with the CSS Custom Highlight API (Range objects +
// ::highlight() pseudo) rather than Electron's webContents.findInPage. findInPage
// moves NATIVE focus onto the matched text in the page after every keystroke,
// which blurs our find input mid-type and can't be reliably restored from the
// renderer. The Highlight API only paints - it never touches focus or mutates the
// DOM - so the input keeps focus and the user can type the whole term.
//
// Runs in the isolated preload world (contextIsolation on, nodeIntegration off).

const { ipcRenderer } = require('electron');

const FIND_BAR_ID = 'salesbuddy-find-bar';
const HL_ALL = 'sb-find-all';
const HL_ACTIVE = 'sb-find-active';

function createFindBar() {
  if (document.getElementById(FIND_BAR_ID)) return;

  const style = document.createElement('style');
  style.textContent = `
    #${FIND_BAR_ID} {
      align-items: center;
      background: #ffffff;
      border: 1px solid #c7cdd4;
      border-radius: 6px;
      box-shadow: 0 4px 16px rgba(0, 0, 0, 0.22);
      display: none;
      gap: 4px;
      padding: 6px;
      position: fixed;
      right: 12px;
      top: 12px;
      z-index: 2147483647;
    }
    #${FIND_BAR_ID}.is-visible { display: flex; }
    #${FIND_BAR_ID} input {
      background: #ffffff;
      border: 1px solid #8c959f;
      border-radius: 4px;
      color: #1f2328;
      font: 14px/1.4 "Segoe UI", sans-serif;
      height: 32px;
      outline: none;
      padding: 4px 8px;
      width: 240px;
    }
    #${FIND_BAR_ID} input:focus {
      border-color: #0969da;
      box-shadow: 0 0 0 2px rgba(9, 105, 218, 0.2);
    }
    #${FIND_BAR_ID} .find-count {
      color: #57606a;
      font: 12px/1 "Segoe UI", sans-serif;
      min-width: 58px;
      text-align: center;
    }
    #${FIND_BAR_ID} button {
      align-items: center;
      background: transparent;
      border: 0;
      border-radius: 4px;
      color: #1f2328;
      cursor: pointer;
      display: inline-flex;
      font: 18px/1 "Segoe UI Symbol", sans-serif;
      height: 32px;
      justify-content: center;
      padding: 0;
      width: 32px;
    }
    #${FIND_BAR_ID} button:hover { background: #eaeef2; }
    ::highlight(${HL_ALL}) { background: #ffe08a; color: #1f2328; }
    ::highlight(${HL_ACTIVE}) { background: #ff9632; color: #1f2328; }
    @media (prefers-color-scheme: dark) {
      #${FIND_BAR_ID} { background: #212529; border-color: #495057; }
      #${FIND_BAR_ID} input { background: #111418; border-color: #6c757d; color: #f8f9fa; }
      #${FIND_BAR_ID} .find-count { color: #adb5bd; }
      #${FIND_BAR_ID} button { color: #f8f9fa; }
      #${FIND_BAR_ID} button:hover { background: #343a40; }
    }
  `;
  document.head.appendChild(style);

  const bar = document.createElement('div');
  bar.id = FIND_BAR_ID;
  bar.setAttribute('role', 'search');
  bar.innerHTML = `
    <input type="text" aria-label="Find in page" autocomplete="off" spellcheck="false" />
    <span class="find-count" aria-live="polite"></span>
    <button type="button" data-action="previous" title="Previous match (Shift+Enter)" aria-label="Previous match">&#8593;</button>
    <button type="button" data-action="next" title="Next match (Enter)" aria-label="Next match">&#8595;</button>
    <button type="button" data-action="close" title="Close (Esc)" aria-label="Close">&#215;</button>
  `;
  document.body.appendChild(bar);

  const input = bar.querySelector('input');
  const count = bar.querySelector('.find-count');

  const supportsHighlights = typeof CSS !== 'undefined' && CSS.highlights &&
    typeof Highlight !== 'undefined';

  let matches = [];      // Range per match, in document order
  let activeIndex = -1;

  const updateCount = () => {
    if (matches.length) {
      count.textContent = `${activeIndex + 1}/${matches.length}`;
    } else {
      count.textContent = input.value ? '0/0' : '';
    }
  };

  const clearHighlights = () => {
    matches = [];
    activeIndex = -1;
    if (supportsHighlights) {
      CSS.highlights.delete(HL_ALL);
      CSS.highlights.delete(HL_ACTIVE);
    }
  };

  const isVisible = (el) => {
    if (!el) return false;
    if (el.offsetParent !== null) return true;   // laid out and not display:none
    return el.getClientRects().length > 0;        // covers position:fixed
  };

  const collectTextNodes = () => {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        if (!node.nodeValue || !node.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
        const parent = node.parentElement;
        if (!parent) return NodeFilter.FILTER_REJECT;
        const tag = parent.tagName;
        if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT') return NodeFilter.FILTER_REJECT;
        if (parent.closest(`#${FIND_BAR_ID}`)) return NodeFilter.FILTER_REJECT;
        if (!isVisible(parent)) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    const nodes = [];
    let n;
    while ((n = walker.nextNode())) nodes.push(n);
    return nodes;
  };

  const paintActive = () => {
    if (!supportsHighlights) return;
    if (activeIndex < 0 || !matches[activeIndex]) {
      CSS.highlights.delete(HL_ACTIVE);
      return;
    }
    const range = matches[activeIndex];
    const active = new Highlight(range);
    active.priority = 1;                 // paints over the all-matches layer
    CSS.highlights.set(HL_ACTIVE, active);
    const el = range.startContainer.parentElement;
    if (el) el.scrollIntoView({ block: 'center', inline: 'nearest' });
  };

  const runSearch = (query) => {
    clearHighlights();
    if (!supportsHighlights || !query) { updateCount(); return; }
    const needle = query.toLowerCase();
    for (const node of collectTextNodes()) {
      const haystack = node.nodeValue.toLowerCase();
      let idx = haystack.indexOf(needle);
      while (idx !== -1) {
        const range = document.createRange();
        range.setStart(node, idx);
        range.setEnd(node, idx + needle.length);
        matches.push(range);
        idx = haystack.indexOf(needle, idx + needle.length);
      }
    }
    if (matches.length) {
      CSS.highlights.set(HL_ALL, new Highlight(...matches));
      activeIndex = 0;
      paintActive();
    }
    updateCount();
  };

  const step = (delta) => {
    if (!matches.length) return;
    activeIndex = (activeIndex + delta + matches.length) % matches.length;
    paintActive();
    updateCount();
  };

  // Debounce the incremental (type-ahead) search: each run walks every text node
  // on the page, so on heavy pages coalescing keystrokes avoids piling up walks.
  let searchTimer = null;
  const scheduleSearch = () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => runSearch(input.value), 120);
  };

  const show = () => {
    bar.classList.add('is-visible');
    input.focus();
    input.select();
    if (input.value) runSearch(input.value);
  };
  const close = () => {
    clearTimeout(searchTimer);
    bar.classList.remove('is-visible');
    clearHighlights();
    updateCount();
  };

  input.addEventListener('input', scheduleSearch);
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      // Commit any pending debounced search before stepping.
      clearTimeout(searchTimer);
      if (!matches.length && input.value) runSearch(input.value);
      else step(event.shiftKey ? -1 : 1);
    } else if (event.key === 'Escape') {
      event.preventDefault();
      close();
    }
  });
  bar.addEventListener('click', (event) => {
    const btn = event.target.closest('button');
    if (!btn) return;
    if (btn.dataset.action === 'previous') { step(-1); input.focus(); }
    if (btn.dataset.action === 'next') { step(1); input.focus(); }
    if (btn.dataset.action === 'close') close();
  });

  // Capture-phase Ctrl+F so it wins even when a page input has focus.
  document.addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === 'f') {
      event.preventDefault();
      show();
    } else if (event.key === 'Escape' && bar.classList.contains('is-visible')) {
      event.preventDefault();
      close();
    }
  }, true);

  ipcRenderer.on('find:show', show);
}

window.addEventListener('DOMContentLoaded', createFindBar);
