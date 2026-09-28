/*
 * Cross-workspace search — briefs (every feed) + library (reports/creations).
 *
 * UI surface: a search bar rendered at the top of the Inbox landing (mounted
 * BY inbox.js when it draws the landing). Typing debounces at 250ms and calls
 * /api/search once the query is >=2 chars; results replace the Today + Feeds
 * sections below the bar. Clearing the query (X button or empty input)
 * restores the normal landing.
 *
 * Scope toggle: Briefs / Everything. Briefs skips the library entirely; the
 * scope value maps 1:1 to the backend's ?scope=briefs|all.
 *
 * Result routing: brief results open in the brief reader (openBriefFromExternal
 * on the Inbox module); library results open in the Library viewer (openPath
 * on the Library module). Both handlers switch tabs first so the reader lands
 * in the right shell.
 *
 * Snippet highlighting is done SAFELY: the whole snippet is set via
 * textContent, then a DOM walker splits text nodes and wraps matched query
 * spans in <mark> — no `innerHTML` on unescaped strings anywhere in this file.
 */
(function () {
  'use strict';

  const { apiGet, relativeTime, escapeHtml } = window.MineruApi;

  const DEBOUNCE_MS = 250;
  const MIN_QUERY_CHARS = 2;
  const RESULT_LIMIT = 40;

  const state = {
    query: '',
    scope: 'briefs',    // 'briefs' | 'all'
    loading: false,
    lastToken: 0,       // bumped per fetch so stale responses drop
    results: null,      // null = not yet searched, [] = searched empty
    truncated: false,
    error: null,
    // Container we render into. Owned by the Inbox landing; search re-renders
    // into it as the query changes.
    resultsContainer: null,
    input: null,
    debounceHandle: null,
  };

  // Build the search bar (input + clear button + scope toggle). Returned as a
  // DocumentFragment so the caller (inbox landing) can append it anywhere.
  function buildSearchBar(resultsContainer) {
    state.resultsContainer = resultsContainer;
    const bar = document.createElement('div');
    bar.className = 'search-bar';
    bar.setAttribute('role', 'search');

    // Left: input with a leading magnifier glyph and trailing clear button.
    const inputWrap = document.createElement('div');
    inputWrap.className = 'search-input-wrap';
    const glyph = document.createElement('span');
    glyph.className = 'search-glyph';
    glyph.setAttribute('aria-hidden', 'true');
    glyph.textContent = '🔍';
    inputWrap.appendChild(glyph);

    const input = document.createElement('input');
    input.type = 'search';
    input.className = 'search-input';
    input.placeholder = 'Search briefs & library…';
    input.setAttribute('aria-label', 'Search briefs and library');
    input.autocomplete = 'off';
    input.spellcheck = false;
    input.value = state.query;
    input.addEventListener('input', function () { onQueryInput(input.value); });
    input.addEventListener('keydown', function (event) {
      if (event.key === 'Enter') {
        event.preventDefault();
        // Enter immediately fires a search, bypassing the debounce so a fast
        // typer isn't punished by the 250ms wait.
        cancelDebounce();
        runSearch(input.value);
      } else if (event.key === 'Escape') {
        event.preventDefault();
        clearSearch();
      }
    });
    // Reveal the scope toggle (Briefs / Everything) whenever the user is
    // actively working the search — either the input has focus, or there's a
    // query in flight. Collapses back to the slim idle pill only when both
    // are false, so a query keeps the scope reachable even after the input
    // blurs (e.g. tapping the scope button itself blurs the input).
    input.addEventListener('focus', function () { bar.classList.add('expanded'); });
    input.addEventListener('blur', function () {
      // rAF gives a focus-transfer to the scope button time to land before
      // we decide whether to collapse — a click on the scope pill first
      // fires blur on the input, then focus on the scope button.
      window.requestAnimationFrame(function () {
        if (!String(input.value || '').trim() && !bar.contains(document.activeElement)) {
          bar.classList.remove('expanded');
        }
      });
    });
    if (state.query) bar.classList.add('expanded');
    inputWrap.appendChild(input);
    state.input = input;

    const clearBtn = document.createElement('button');
    clearBtn.type = 'button';
    clearBtn.className = 'search-clear';
    clearBtn.setAttribute('aria-label', 'Clear search');
    clearBtn.textContent = '×';
    clearBtn.addEventListener('click', clearSearch);
    inputWrap.appendChild(clearBtn);

    bar.appendChild(inputWrap);

    // Right: scope toggle. Two segmented buttons, Briefs default.
    const scope = document.createElement('div');
    scope.className = 'search-scope';
    scope.setAttribute('role', 'tablist');
    scope.setAttribute('aria-label', 'Search scope');
    ['briefs', 'all'].forEach(function (key) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'search-scope-btn' + (state.scope === key ? ' active' : '');
      btn.setAttribute('role', 'tab');
      btn.setAttribute('aria-selected', state.scope === key ? 'true' : 'false');
      btn.dataset.scope = key;
      btn.textContent = key === 'briefs' ? 'Briefs' : 'Everything';
      btn.addEventListener('click', function () { setScope(key); });
      scope.appendChild(btn);
    });
    bar.appendChild(scope);

    // Focus the input after the bar mounts, so the landing lets the user type
    // right away without a click. requestAnimationFrame ensures the element
    // is in the DOM before we try to focus it.
    window.requestAnimationFrame(function () {
      // Only auto-focus on desktop; on mobile that pops the keyboard on every
      // landing render, which is annoying.
      if (!window.matchMedia('(max-width: 720px)').matches) {
        try { input.focus({ preventScroll: true }); } catch (_) { /* older */ }
      }
    });

    return bar;
  }

  function onQueryInput(value) {
    state.query = value;
    cancelDebounce();
    // Empty or <2 chars: clear results, restore normal landing. Called
    // explicitly instead of doing nothing so a user who backspaces a
    // 3-char query gets Today + Feeds back immediately. Also reset the
    // history entry to landing so a subsequent brief-open doesn't stack
    // on top of a stale search hash.
    if (String(value || '').trim().length < MIN_QUERY_CHARS) {
      state.results = null;
      state.error = null;
      state.truncated = false;
      renderResults();
      // Only reset if we were previously on a search hash — avoid rewriting
      // #inbox back onto #inbox for no reason.
      if (window.MineruRouter && location.hash && location.hash.indexOf('#search') === 0) {
        window.MineruRouter.replace({ view: 'inbox' });
      }
      return;
    }
    state.debounceHandle = window.setTimeout(function () {
      runSearch(state.query);
    }, DEBOUNCE_MS);
  }

  function cancelDebounce() {
    if (state.debounceHandle) {
      window.clearTimeout(state.debounceHandle);
      state.debounceHandle = null;
    }
  }

  function setScope(scope) {
    if (state.scope === scope) return;
    state.scope = scope;
    // Reflect on the buttons.
    document.querySelectorAll('.search-scope-btn').forEach(function (btn) {
      const isActive = btn.dataset.scope === scope;
      btn.classList.toggle('active', isActive);
      btn.setAttribute('aria-selected', isActive ? 'true' : 'false');
    });
    // Re-run the current query under the new scope, if there is one.
    if (String(state.query || '').trim().length >= MIN_QUERY_CHARS) {
      runSearch(state.query);
    }
  }

  async function runSearch(query) {
    const trimmed = String(query || '').trim();
    if (trimmed.length < MIN_QUERY_CHARS) return;
    // Show a loading state so the user knows the query fired.
    state.loading = true;
    state.error = null;
    renderResults();
    // Reflect the current query in the URL WITHOUT stacking history — one
    // keystroke per push would flood the back stack. Replace instead so
    // that clicking a result later pushes a real brief/library entry on
    // top of a coherent search state. The suppressReplaceOnce flag is
    // set by router-driven restores (setQuery({fromHistory:true})) to keep
    // us from clobbering the state the browser just handed us.
    if (window.MineruRouter && !state.suppressReplaceOnce) {
      window.MineruRouter.replace({ view: 'search', query: trimmed });
    }
    state.suppressReplaceOnce = false;
    const token = ++state.lastToken;
    const params = new URLSearchParams({
      q: trimmed,
      scope: state.scope,
      limit: String(RESULT_LIMIT),
    });
    try {
      const data = await apiGet(`/api/search?${params.toString()}`);
      // Guard against stale responses arriving after a newer query fired.
      if (token !== state.lastToken) return;
      state.results = Array.isArray(data.results) ? data.results : [];
      state.truncated = !!data.truncated;
      state.error = null;
    } catch (err) {
      if (token !== state.lastToken) return;
      // 404 means the backend hasn't landed /api/search yet. Treat that as
      // "search unavailable" rather than crashing the landing — the user
      // can still browse Today and per-feed views.
      state.results = [];
      state.truncated = false;
      state.error = err;
    } finally {
      if (token === state.lastToken) state.loading = false;
    }
    renderResults();
  }

  function clearSearch() {
    cancelDebounce();
    state.query = '';
    state.results = null;
    state.error = null;
    state.truncated = false;
    if (state.input) {
      state.input.value = '';
      // Give focus back so the user can start a new query without clicking.
      // Focus alone keeps the bar expanded via the focus handler in
      // buildSearchBar, so we don't need to touch .expanded here.
      try { state.input.focus({ preventScroll: true }); } catch (_) { /* older */ }
    }
    renderResults();
  }

  // Render the results panel. When state.results is null, the panel is empty
  // (search hasn't been run yet) — Inbox shows its usual Today+Feeds. When
  // there's a query in flight or results present, the results panel shows and
  // the caller's Today+Feeds sections are hidden.
  //
  // We toggle `style.display` rather than the `[hidden]` attribute: the
  // user-agent's `[hidden]{display:none}` loses to any author `display:flex`
  // on the same element (specificity tie, author stylesheet wins). That's
  // exactly the case for the .brief-list feeds section, so hidden=true left
  // it visible. Inline style is the only reliable hide here.
  function renderResults() {
    const container = state.resultsContainer;
    if (!container) return;
    container.innerHTML = '';
    const landingSiblings = document.querySelectorAll('.landing-below-search');
    if (state.results === null) {
      // Not searching: hide the panel, show the landing.
      container.style.display = 'none';
      landingSiblings.forEach(function (el) { el.style.display = ''; });
      return;
    }
    // Clear the `hidden` attribute set at construction; a search bringing
    // results back has to override that initial hide even if display isn't
    // itself set inline.
    container.removeAttribute('hidden');
    container.style.display = '';
    landingSiblings.forEach(function (el) { el.style.display = 'none'; });

    const header = document.createElement('div');
    header.className = 'search-results-header';
    const title = document.createElement('div');
    title.className = 'search-results-title';
    if (state.loading) {
      title.textContent = `Searching for “${state.query.trim()}”…`;
    } else {
      const count = state.results.length;
      const suffix = count === 1 ? 'result' : 'results';
      title.textContent = `${count} ${suffix} for “${state.query.trim()}”`;
    }
    header.appendChild(title);
    if (state.truncated && !state.loading) {
      const truncated = document.createElement('div');
      truncated.className = 'search-results-truncated';
      truncated.textContent = `Showing first ${RESULT_LIMIT}`;
      header.appendChild(truncated);
    }
    container.appendChild(header);

    if (state.loading) {
      const skel = document.createElement('div');
      skel.className = 'search-loading';
      skel.textContent = 'Loading…';
      container.appendChild(skel);
      return;
    }

    if (state.error) {
      const err = document.createElement('div');
      err.className = 'search-error';
      // Error message is bucketed by api.js — never a raw path. Safe.
      err.textContent = `Search unavailable. ${state.error.message || ''}`.trim();
      container.appendChild(err);
      return;
    }

    if (!state.results.length) {
      const empty = document.createElement('div');
      empty.className = 'search-empty';
      const mark = document.createElement('span');
      mark.className = 'search-empty-mark';
      mark.setAttribute('aria-hidden', 'true');
      mark.textContent = '🔎';
      const text = document.createElement('span');
      text.textContent = `No matches for “${state.query.trim()}”.`;
      empty.appendChild(mark);
      empty.appendChild(text);
      container.appendChild(empty);
      return;
    }

    const list = document.createElement('div');
    list.className = 'search-results-list';
    state.results.forEach(function (result) {
      list.appendChild(renderResultRow(result));
    });
    container.appendChild(list);
  }

  function renderResultRow(result) {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'search-result-row';

    // Resolve feed metadata from the Inbox's cached registry when the backend
    // didn't inline it — the real /api/search returns just feed_id, and the
    // registry (loaded on Inbox enter) has emoji/name/accent for it.
    const feedMeta = result.kind === 'brief' && window.MineruInbox && window.MineruInbox.getFeedMeta
      ? window.MineruInbox.getFeedMeta(result.feed_id)
      : null;
    const resolvedEmoji = result.feed_emoji || (feedMeta && feedMeta.emoji) || '📥';
    const resolvedFeedName = result.feed_display_name || (feedMeta && feedMeta.display_name) || result.feed_id || 'brief';

    const glyph = document.createElement('span');
    glyph.className = 'search-result-glyph';
    glyph.setAttribute('aria-hidden', 'true');
    if (result.kind === 'brief') {
      glyph.textContent = resolvedEmoji;
    } else {
      glyph.textContent = result.source === 'creations' ? '✨' : '📄';
    }
    row.appendChild(glyph);

    const body = document.createElement('div');
    body.className = 'search-result-body';

    const line1 = document.createElement('div');
    line1.className = 'search-result-title';
    // titleText uses textContent — no XSS surface at all here.
    const titleText = String(result.title || result.filename || result.relpath || '').trim();
    highlightInto(line1, titleText, state.query);
    body.appendChild(line1);

    // Suppress the snippet row when it would just repeat the title. Two
    // shapes trigger this: an explicit `matched_in==='title'` (the backend
    // already tells us the match landed in the title), or the snippet text
    // equals the title case-insensitively (older result shapes and the
    // filename fallback). Both used to render as `title\n\ntitle` in the
    // row, which read as a broken row rather than a helpful preview.
    if (result.snippet) {
      const snippetText = String(result.snippet).trim();
      const isTitleDup = result.matched_in === 'title'
        || snippetText.toLowerCase() === titleText.toLowerCase();
      if (!isTitleDup) {
        const snip = document.createElement('div');
        snip.className = 'search-result-snippet';
        highlightInto(snip, snippetText, state.query);
        body.appendChild(snip);
      }
    }

    const meta = document.createElement('div');
    meta.className = 'search-result-meta';
    const labelPieces = [];
    if (result.kind === 'brief') {
      labelPieces.push(resolvedFeedName);
    } else {
      const sourceLabel = result.source === 'creations' ? 'Creations' : 'Reports';
      labelPieces.push(sourceLabel);
      if (result.relpath) labelPieces.push(result.relpath);
    }
    if (result.matched_in) labelPieces.push(`match: ${result.matched_in}`);
    labelPieces.push(relativeTime(result.mtime));
    meta.textContent = labelPieces.join(' · ');
    body.appendChild(meta);

    row.appendChild(body);

    row.setAttribute('aria-label', `${titleText} — ${meta.textContent}`);
    row.addEventListener('click', function () { openResult(result); });

    return row;
  }

  // Wrap every case-insensitive occurrence of `query` inside `text` in
  // <mark> elements — using DOM APIs only, never innerHTML on the source
  // string. Handles the whole snippet as one text node first (textContent),
  // then splits it into text + <mark> children.
  function highlightInto(target, text, query) {
    target.textContent = ''; // reset
    const needle = String(query || '').trim();
    if (!needle) {
      target.textContent = text;
      return;
    }
    // Match every occurrence, case-insensitive, treating regex metachars in
    // the query as literal so a user searching for "a+b" doesn't blow up.
    const escaped = needle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const re = new RegExp(escaped, 'gi');
    let cursor = 0;
    let match;
    while ((match = re.exec(text)) !== null) {
      if (match.index > cursor) {
        target.appendChild(document.createTextNode(text.slice(cursor, match.index)));
      }
      const mark = document.createElement('mark');
      mark.className = 'search-highlight';
      mark.textContent = match[0];
      target.appendChild(mark);
      cursor = match.index + match[0].length;
      // Guard against zero-length matches (shouldn't happen with a literal
      // needle, but bugs love to prove me wrong).
      if (match.index === re.lastIndex) re.lastIndex += 1;
    }
    if (cursor < text.length) {
      target.appendChild(document.createTextNode(text.slice(cursor)));
    }
  }

  function openResult(result) {
    if (result.kind === 'brief') {
      if (window.MineruInbox && window.MineruInbox.openBriefFromExternal) {
        window.MineruInbox.openBriefFromExternal(result.feed_id, result.filename, {
          title: result.title,
          mtime: result.mtime,
        });
      }
    } else if (result.kind === 'library') {
      if (window.MineruLibrary && window.MineruLibrary.openPath) {
        window.MineruLibrary.openPath(result.source, result.relpath);
      }
    }
  }

  function reset() {
    // Called when Inbox re-enters the landing so a leftover query from a
    // previous session-life doesn't reappear stale.
    cancelDebounce();
    state.query = '';
    state.results = null;
    state.error = null;
    state.truncated = false;
    state.loading = false;
    state.lastToken += 1;
    state.input = null;
    state.resultsContainer = null;
  }

  // Router-driven: restore the search view from a #search/<q> hash. The
  // Inbox landing has already been re-rendered by the router, so the search
  // bar exists in the DOM by the time we get called. We populate the input
  // and run the query WITHOUT letting runSearch's own replaceState fire
  // (that would clobber the popstate state the browser just handed us).
  function setQuery(query, options) {
    const trimmed = String(query || '').trim();
    if (state.input) {
      state.input.value = trimmed;
      // Router-restored search means the scope toggle needs to be reachable
      // even without focus — otherwise a deep-link to #search/... shows the
      // slim idle pill on landing.
      const bar = state.input.closest('.search-bar');
      if (bar && trimmed) bar.classList.add('expanded');
    }
    state.query = trimmed;
    if (trimmed.length < MIN_QUERY_CHARS) {
      state.results = null;
      renderResults();
      return;
    }
    // Suppress the replaceState side-effect when the router is restoring —
    // the URL is already correct, and rewriting it would race with the
    // popstate event that just landed us here.
    if (options && options.fromHistory) {
      state.suppressReplaceOnce = true;
    }
    runSearch(trimmed);
  }

  window.MineruSearch = { buildSearchBar, reset, clearSearch, setQuery };
})();
