/*
 * Note / answer composer shown under every markdown file in the reader
 * (feed briefs and library .md files). Both readers call
 * window.MineruAnswerComposer.build(...) and append the returned node.
 *
 * - Label set depends on `answerStatus`: a line-1 marker status means the
 *   file is a question (answer wording); null means a plain note.
 * - Drafts persist to localStorage (debounced on input, flushed on
 *   visibilitychange/pagehide) and are restored on build; cleared on save.
 * - Keyboard-safe on iOS standalone: while focused, a spacer the height of
 *   the keyboard overlap lets the reader scroll the Save button above it.
 * - All UI is createElement + textContent (CSP script-src 'self'; DOMPurify
 *   would strip form controls from rendered markdown anyway).
 */
(function () {
  'use strict';

  const ANSWER_TEXT_MAX_CHARS = 4000;  // mirrors brief_answers.ANSWER_TEXT_MAX_CHARS
  const COUNTER_SHOW_AT_CHARS = 3500;
  const SAVE_TIMEOUT_MS = 15000;
  const DRAFT_DEBOUNCE_MS = 250;
  const DRAFT_KEY_PREFIX = 'mineru:answer-draft:';
  // Gap kept between the Save row and the top of the iOS keyboard.
  const KEYBOARD_CLEARANCE_PX = 16;

  const STATUS_PILLS = {
    unanswered: { label: 'Unanswered' },
    answered_pending_filing: { label: 'Answered · to file', kind: 'info' },
    answered_filed: { label: 'Filed', kind: 'info' },
  };

  const LABELS_BY_STATUS = {
    unanswered: { label: 'Your answer', placeholder: 'Type your answer…', button: 'Save answer' },
    answered_pending_filing: { label: 'Add another note', placeholder: 'Type your answer…', button: 'Save answer' },
    answered_filed: { label: 'Add a correction', placeholder: 'Type a correction…', button: 'Save correction' },
  };
  const LABELS_FOR_PLAIN_NOTE = { label: 'Add a note', placeholder: 'Type a note…', button: 'Save note' };

  function isMarkerStatus(status) {
    return Object.prototype.hasOwnProperty.call(STATUS_PILLS, status || '');
  }

  function pillFor(status) {
    return isMarkerStatus(status) ? STATUS_PILLS[status] : null;
  }

  function draftKeyFor(kind, ids) {
    if (kind === 'library') return `${DRAFT_KEY_PREFIX}library:${ids.source}/${ids.relpath}`;
    return `${DRAFT_KEY_PREFIX}brief:${ids.feedId}/${ids.filename}`;
  }

  function answerPathFor(kind, ids) {
    if (kind === 'library') {
      return `/api/library/${encodeURIComponent(ids.source)}/${encodeURIComponent(ids.relpath)}/answer`;
    }
    return `/api/brief/${encodeURIComponent(ids.feedId)}/${encodeURIComponent(ids.filename)}/answer`;
  }

  // localStorage can throw (Safari private mode, quota); drafts are best-effort.
  function readDraft(key) {
    try { return window.localStorage.getItem(key) || ''; } catch (_) { return ''; }
  }
  function writeDraft(key, text) {
    try {
      if (text) window.localStorage.setItem(key, text);
      else window.localStorage.removeItem(key);
    } catch (_) { /* best-effort */ }
  }

  function messageForSaveError(saveError) {
    if (saveError && saveError.timedOut) return "Couldn't reach the Mac. Your text is kept.";
    const httpStatus = saveError && saveError.status ? saveError.status : null;
    if (httpStatus === 409) return "That file can't take notes (not valid UTF-8).";
    return `Couldn't save (${httpStatus ? `HTTP ${httpStatus}` : 'offline'}). Your text is still here.`;
  }

  // Height follows content between the CSS min-height and max-height (50vh).
  // Collapsing to 'auto' first lets it shrink; the scroller's scrollTop is
  // pinned so the momentary collapse does not jump the reader.
  function growTextareaToContent(textarea) {
    const scroller = textarea.closest('.reader-body');
    const savedScrollTop = scroller ? scroller.scrollTop : 0;
    textarea.style.height = 'auto';
    const borderPx = textarea.offsetHeight - textarea.clientHeight;
    textarea.style.height = `${textarea.scrollHeight + borderPx}px`;
    if (scroller) scroller.scrollTop = savedScrollTop;
  }

  function keyboardOverlapPx() {
    const vv = window.visualViewport;
    if (!vv) return 0;
    return Math.max(0, window.innerHeight - (vv.offsetTop + vv.height));
  }

  // Scroll the reader so `target`'s bottom sits above the visible bottom
  // (the keyboard top when it is up). Returns true if it moved anything.
  function scrollAboveKeyboard(target) {
    const scroller = target.closest('.reader-body');
    if (!scroller) return false;
    const vv = window.visualViewport;
    const visibleBottom = vv ? vv.offsetTop + vv.height : window.innerHeight;
    const overshoot = target.getBoundingClientRect().bottom - (visibleBottom - KEYBOARD_CLEARANCE_PX);
    if (overshoot <= 0) return false;
    scroller.scrollTop += overshoot;
    return true;
  }

  function formatCount(n) {
    return Number(n).toLocaleString('en-US');
  }

  /**
   * Build a composer node.
   *
   * Args (one object):
   *   kind: 'brief' | 'library'.
   *   ids: {feedId, filename} for briefs, {source, relpath} for library files (raw, un-encoded).
   *   answerStatus: line-1 marker status or null (plain note).
   *   onSaved(response): awaited after a 200; the reader re-renders from it.
   *   notice: optional {text, actionLabel, onAction} shown in the status line on build
   *     (the reader passes "Saved." + "Next unanswered →" after a re-render).
   * Returns the composer element.
   */
  function build(options) {
    const kind = options.kind === 'library' ? 'library' : 'brief';
    const ids = options.ids || {};
    const answerStatus = isMarkerStatus(options.answerStatus) ? options.answerStatus : null;
    const labels = answerStatus ? LABELS_BY_STATUS[answerStatus] : LABELS_FOR_PLAIN_NOTE;
    const draftKey = draftKeyFor(kind, ids);

    const composer = document.createElement('div');
    composer.className = 'answer-composer';

    const inputId = `answer-input-${Date.now()}`;
    const label = document.createElement('label');
    label.className = 'answer-label';
    label.setAttribute('for', inputId);
    label.textContent = labels.label;

    const textarea = document.createElement('textarea');
    textarea.className = 'answer-input';
    textarea.id = inputId;
    textarea.rows = 6;
    textarea.maxLength = ANSWER_TEXT_MAX_CHARS;
    textarea.placeholder = labels.placeholder;
    textarea.setAttribute('autocapitalize', 'sentences');

    const saveButton = document.createElement('button');
    saveButton.type = 'button';
    saveButton.className = 'answer-save';
    saveButton.textContent = labels.button;

    const statusLine = document.createElement('p');
    statusLine.className = 'answer-status';
    statusLine.setAttribute('aria-live', 'polite');

    const counter = document.createElement('span');
    counter.className = 'answer-counter';
    counter.hidden = true;

    // Grows to the keyboard overlap while focused so the reader can scroll
    // the Save row clear of the keyboard even when the composer is last.
    const keyboardSpacer = document.createElement('div');
    keyboardSpacer.className = 'answer-keyboard-spacer';
    keyboardSpacer.setAttribute('aria-hidden', 'true');

    const actionsRow = document.createElement('div');
    actionsRow.className = 'answer-actions';
    actionsRow.appendChild(saveButton);
    actionsRow.appendChild(statusLine);
    actionsRow.appendChild(counter);

    function setStatus(text, action) {
      statusLine.textContent = text || '';
      if (!action) return;
      const actionButton = document.createElement('button');
      actionButton.type = 'button';
      actionButton.className = 'answer-next';
      actionButton.textContent = action.label;
      actionButton.addEventListener('click', action.onClick);
      statusLine.appendChild(document.createTextNode(' '));
      statusLine.appendChild(actionButton);
    }

    function updateCounter() {
      const length = textarea.value.length;
      counter.hidden = length < COUNTER_SHOW_AT_CHARS;
      counter.textContent = `${formatCount(length)} / ${formatCount(ANSWER_TEXT_MAX_CHARS)}`;
      counter.classList.toggle('at-limit', length >= ANSWER_TEXT_MAX_CHARS);
    }

    let saving = false;

    // --- Draft persistence
    let draftTimer = null;
    function flushDraft() {
      if (draftTimer) { window.clearTimeout(draftTimer); draftTimer = null; }
      writeDraft(draftKey, textarea.value);
    }
    function onVisibilityChange() {
      if (detachIfGone()) return;
      if (document.visibilityState === 'hidden') flushDraft();
    }
    function onPageHide() {
      if (detachIfGone()) return;
      flushDraft();
    }
    document.addEventListener('visibilitychange', onVisibilityChange);
    window.addEventListener('pagehide', onPageHide);
    // The reader replaces the composer wholesale on navigation; drop the
    // document-level listeners once it has been detached.
    function detachIfGone() {
      if (composer.isConnected) return false;
      flushDraft();
      document.removeEventListener('visibilitychange', onVisibilityChange);
      window.removeEventListener('pagehide', onPageHide);
      return true;
    }

    // --- Keyboard-safe scrolling
    let focused = false;
    function syncKeyboardSpacer() {
      keyboardSpacer.style.height = focused ? `${keyboardOverlapPx()}px` : '0px';
    }
    function keepSaveRowVisible() {
      if (!focused) return;
      syncKeyboardSpacer();
      scrollAboveKeyboard(actionsRow);
    }
    function onViewportChange() {
      if (detachIfGone()) return;
      keepSaveRowVisible();
    }

    textarea.addEventListener('focus', function () {
      focused = true;
      if (window.visualViewport) {
        window.visualViewport.addEventListener('resize', onViewportChange);
        window.visualViewport.addEventListener('scroll', onViewportChange);
      }
      window.requestAnimationFrame(function () {
        if (keyboardOverlapPx() > 0) {
          keepSaveRowVisible();
          return;
        }
        // No keyboard yet (desktop, or iOS before it animates in): center the
        // composer; the visualViewport resize then corrects for the keyboard.
        try { composer.scrollIntoView({ block: 'center', behavior: 'smooth' }); } catch (_) { /* older */ }
      });
    });
    textarea.addEventListener('blur', function () {
      focused = false;
      if (window.visualViewport) {
        window.visualViewport.removeEventListener('resize', onViewportChange);
        window.visualViewport.removeEventListener('scroll', onViewportChange);
      }
      syncKeyboardSpacer();
      flushDraft();
    });

    textarea.addEventListener('input', function () {
      if (detachIfGone()) return;
      growTextareaToContent(textarea);
      updateCounter();
      if (statusLine.textContent && !saving) setStatus('');
      if (draftTimer) window.clearTimeout(draftTimer);
      draftTimer = window.setTimeout(flushDraft, DRAFT_DEBOUNCE_MS);
      keepSaveRowVisible();
    });

    textarea.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        save();
      }
    });

    // --- Save
    async function save() {
      const text = textarea.value.trim();
      if (saving) return;
      if (!text) { textarea.focus(); return; }
      saving = true;
      saveButton.disabled = true;
      textarea.disabled = true;
      setStatus('Saving…');
      let response;
      try {
        response = await window.MineruApi.apiPost(answerPathFor(kind, ids), { text: text }, { timeoutMs: SAVE_TIMEOUT_MS });
      } catch (saveError) {
        saving = false;
        saveButton.disabled = false;
        textarea.disabled = false;
        setStatus(messageForSaveError(saveError));
        return;
      }
      if (draftTimer) { window.clearTimeout(draftTimer); draftTimer = null; }
      writeDraft(draftKey, '');
      textarea.value = '';
      saving = false;
      setStatus('Saved.');
      try {
        if (typeof options.onSaved === 'function') await options.onSaved(response || {});
      } finally {
        // onSaved normally replaces this node; if it bails (failed refetch), the
        // composer must come back to life rather than sit disabled under "Saved."
        saveButton.disabled = false;
        textarea.disabled = false;
      }
    }
    saveButton.addEventListener('click', save);

    composer.appendChild(label);
    composer.appendChild(textarea);
    composer.appendChild(actionsRow);
    composer.appendChild(keyboardSpacer);

    // --- Initial state: restored draft beats a carried-over notice.
    const restoredDraft = readDraft(draftKey);
    if (restoredDraft) {
      textarea.value = restoredDraft;
      setStatus('Restored your unsaved draft');
    } else if (options.notice) {
      const notice = options.notice;
      setStatus(notice.text, notice.actionLabel ? { label: notice.actionLabel, onClick: notice.onAction } : null);
    }
    updateCounter();
    // Height needs layout; grow once the node is in the document.
    window.requestAnimationFrame(function () {
      if (composer.isConnected) growTextareaToContent(textarea);
    });

    return composer;
  }

  window.MineruAnswerComposer = { build, pillFor, isMarkerStatus };
})();
