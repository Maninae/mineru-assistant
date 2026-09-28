/*
 * Inbox tab — three-pane mail client.
 *
 * Two column surfaces:
 *   - list column: unified inbox (goToUnifiedInbox) OR a single feed (openFeed).
 *     Populated with dense .message-row rows: envelope glyph, bold title, verbose
 *     right-aligned timestamp, subtitle (feed category + optional tag chip), 2-line
 *     TLDR snippet.
 *   - reader column: opened brief letter (openBrief). Marks the source row .selected
 *     without touching the rest of the list.
 *
 * openBriefFromExternal: called by Today cards, search results, linkified paths —
 * anything that skipped the per-feed message list. Uses
 * MineruRouter.seedFeedThenPushBrief() so back-from-reader ALWAYS lands on the
 * correct message list, deterministically (see recon-report §2, spec §9).
 */
(function () {
  'use strict';

  const {
    apiGet, apiPost, relativeTime, verboseRelativeTime, absoluteTime,
    renderMarkdownSafe, decorateReaderLinks, linkifyWorkspacePaths,
    escapeHtml, bodyHasLeadingTitle, stripLeadingEmoji, buildErrorCard,
  } = window.MineruApi;

  const TODAY_HOURS = 168;  // ~7 days: unified inbox spans a week

  const state = {
    feeds: [],
    currentFeedId: null,   // null = unified inbox
    items: [],
    nextBefore: null,
    hasMore: false,
    loading: false,
    brief: null,
    requestToken: 0,
    savedListScrollTop: 0,
    markAllInFlight: false,
    inUnifiedInbox: false,
  };

  async function loadFeeds() {
    try {
      const data = await apiGet('/api/feeds');
      state.feeds = data.feeds || [];
      window.MineruShell.renderFeedSidebar(state.feeds, state.currentFeedId);
      window.MineruShell.updateGlobalUnreadBadge(sumUnread(state.feeds));
    } catch (loadError) {
      state.feeds = [];
      window.MineruShell.updateGlobalUnreadBadge(0);
    }
  }

  function sumUnread(feeds) {
    return (feeds || []).reduce(function (acc, f) { return acc + (f.unread_count || 0); }, 0);
  }

  function feedMetaFor(feedId) {
    return state.feeds.find(function (f) { return f.id === feedId; }) || null;
  }

  // Enter the tab. If we're not on a feed, land on the unified inbox.
  async function enter() {
    await loadFeeds();
    if (state.currentFeedId) {
      openFeed(state.currentFeedId);
    } else {
      await goToUnifiedInbox();
    }
  }

  // --- Unified inbox: cross-feed newest-first, rendered in the LIST column.

  async function goToUnifiedInbox(options) {
    const fromHistory = !!(options && options.fromHistory);
    state.currentFeedId = null;
    state.inUnifiedInbox = true;
    state.items = [];
    state.nextBefore = null;
    state.hasMore = false;
    state.requestToken += 1;
    window.MineruShell.setActiveFeed(null);
    if (!state.feeds.length) await loadFeeds();
    window.MineruShell.setListHeader('Inbox');
    installUnifiedMarkAllReadButton();
    window.MineruShell.clearReader();
    window.MineruShell.setStackLevel(window.MineruShell.isMobile() ? 1 : null);
    if (!fromHistory && window.MineruRouter) {
      window.MineruRouter.navigate({ view: 'inbox' });
    }
    mountSearchBarInList();
    await loadUnifiedInbox();
  }

  // Mount the search bar at the top of the list body. When the user types
  // 2+ chars, the search module shows results below and hides siblings
  // marked `.landing-below-search`. On empty/short queries, the message
  // list is re-shown.
  function mountSearchBarInList() {
    const listBody = document.getElementById('list-body');
    if (!listBody || !window.MineruSearch) return;
    // Clear so we don't stack search bars across renders.
    listBody.innerHTML = '';
    const resultsContainer = document.createElement('div');
    resultsContainer.className = 'search-results';
    resultsContainer.setAttribute('aria-live', 'polite');
    resultsContainer.hidden = true;
    window.MineruSearch.reset();
    listBody.appendChild(window.MineruSearch.buildSearchBar(resultsContainer));
    listBody.appendChild(resultsContainer);
    // Wrapper that holds the actual message list — hidden by search when a
    // query is active. Marked `.landing-below-search` so the search module's
    // existing hide logic keeps working.
    const listWrap = document.createElement('div');
    listWrap.className = 'landing-below-search';
    listWrap.id = 'list-body-inner';
    listBody.appendChild(listWrap);
  }

  // Prefer the inner render target when a search bar is mounted; fall back
  // to the plain list body otherwise. Keeps renderMessageList / error paths
  // pointed at the right slot.
  function listRenderTarget() {
    return document.getElementById('list-body-inner') || document.getElementById('list-body');
  }

  async function loadUnifiedInbox() {
    state.loading = true;
    const target = listRenderTarget();
    target.innerHTML = '<div class="list-loading">Loading…</div>';
    const requestToken = state.requestToken;
    try {
      const data = await apiGet(`/api/today?hours=${TODAY_HOURS}`);
      if (requestToken !== state.requestToken) return;
      const items = Array.isArray(data.items) ? data.items : [];
      // /api/today items don't carry the standard {filename,title,tldr,mtime,size,seen}
      // exactly — they DO (see feeds.py / handlers). Normalize into the row-render shape.
      state.items = items.map(function (it) {
        return {
          filename: it.filename,
          title: it.title,
          tldr: it.tldr,
          preview: it.preview,
          mtime: it.mtime,
          size: it.size,
          seen: !!it.seen,
          feed_id: it.feed_id,
          feed_display_name: it.feed_display_name,
          feed_emoji: it.feed_emoji,
        };
      });
      renderMessageList();
    } catch (loadError) {
      if (requestToken !== state.requestToken) return;
      target.innerHTML = '';
      target.appendChild(buildErrorCard(
        `Inbox unavailable. ${loadError.message || ''}`.trim(),
        [{ label: 'Retry', onClick: function () { loadUnifiedInbox(); } }],
      ));
    } finally {
      if (requestToken === state.requestToken) state.loading = false;
    }
  }

  function installUnifiedMarkAllReadButton() {
    // No bulk mark-all in unified mode (the endpoint is per-feed). Empty
    // actions so any prior feed's button doesn't linger.
    const actions = document.getElementById('list-header-actions');
    if (actions) actions.innerHTML = '';
  }

  // --- Per-feed message list.

  async function openFeed(feedId, options) {
    const fromHistory = !!(options && options.fromHistory);
    state.currentFeedId = feedId;
    state.inUnifiedInbox = false;
    state.items = [];
    state.nextBefore = null;
    state.hasMore = false;
    state.requestToken += 1;
    state.loading = false;
    if (!fromHistory && window.MineruRouter) {
      window.MineruRouter.navigate({ view: 'feed', feedId: feedId });
    }
    if (!state.feeds.length) await loadFeeds();
    const feed = feedMetaFor(feedId);
    window.MineruShell.setActiveFeed(feedId);
    const title = feed
      ? `${feed.emoji} ${feed.display_name}`
      : 'Feed';
    window.MineruShell.setListHeader(title);
    installMarkAllReadButton(feedId, feed);
    // Reader stays empty until the user picks a row.
    window.MineruShell.clearReader();
    window.MineruShell.setStackLevel(window.MineruShell.isMobile() ? 1 : null);
    // Clear any stale search bar/results from a previous unified-inbox
    // mount; feed views don't carry the search bar.
    const listBody = document.getElementById('list-body');
    if (listBody) listBody.innerHTML = '';
    await loadNextPage();
  }

  function installMarkAllReadButton(feedId, feed) {
    const actions = document.getElementById('list-header-actions');
    if (!actions) return;
    actions.innerHTML = '';
    if (!feed || feed.unread_count === 0) return;
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'icon-button mark-all-button';
    btn.dataset.feedId = feedId;
    btn.textContent = '✓ Mark all read';
    btn.setAttribute('aria-label', `Mark all ${feed.display_name} briefs read`);
    btn.addEventListener('click', function () { markAllRead(feedId, btn); });
    actions.appendChild(btn);
  }

  async function markAllRead(feedId, btn) {
    if (state.markAllInFlight) return;
    state.markAllInFlight = true;
    btn.disabled = true;
    const originalText = btn.textContent;
    btn.textContent = '… marking…';
    try {
      await apiPost('/api/seen', { feed_id: feedId, all: true });
      state.items.forEach(function (it) { it.seen = true; });
      renderMessageList();
      await loadFeeds();
      if (state.currentFeedId === feedId) {
        const refreshed = feedMetaFor(feedId);
        installMarkAllReadButton(feedId, refreshed);
      }
    } catch (markError) {
      btn.textContent = 'Failed — retry';
      window.setTimeout(function () { btn.textContent = originalText; btn.disabled = false; }, 1400);
      state.markAllInFlight = false;
      return;
    }
    state.markAllInFlight = false;
  }

  async function loadNextPage() {
    if (state.loading || !state.currentFeedId) return;
    state.loading = true;
    const target = listRenderTarget();
    if (state.items.length === 0) target.innerHTML = '<div class="list-loading">Loading briefs…</div>';
    const params = new URLSearchParams({ limit: '30' });
    if (state.nextBefore != null) params.set('before', String(state.nextBefore));
    const requestFeedId = state.currentFeedId;
    const requestToken = state.requestToken;
    try {
      const data = await apiGet(`/api/feed/${encodeURIComponent(requestFeedId)}?${params.toString()}`);
      if (requestToken !== state.requestToken || requestFeedId !== state.currentFeedId) return;
      state.items = state.items.concat(data.items || []);
      state.hasMore = !!data.has_more;
      state.nextBefore = data.next_before;
      renderMessageList();
    } catch (loadError) {
      if (requestToken !== state.requestToken) return;
      target.innerHTML = '';
      const actions = loadError.status === 0
        ? [{ label: 'Retry', onClick: function () { loadNextPage(); } }, { label: 'Back to Inbox', onClick: function () { goToUnifiedInbox(); } }]
        : [{ label: 'Back to Inbox', onClick: function () { goToUnifiedInbox(); } }];
      target.appendChild(buildErrorCard(`Feed unavailable. ${loadError.message || ''}`.trim(), actions));
    } finally {
      if (requestToken === state.requestToken) state.loading = false;
    }
  }

  // --- Message-list renderer (spec §4) ------------------------------------

  function renderMessageList() {
    const target = listRenderTarget();
    target.innerHTML = '';
    if (!state.items.length) {
      target.innerHTML = '<div class="list-empty">No briefs.</div>';
      return;
    }
    const list = document.createElement('div');
    list.className = 'message-list';
    state.items.forEach(function (item) {
      list.appendChild(renderMessageRow(item));
    });
    target.appendChild(list);
    if (state.hasMore) {
      const more = document.createElement('button');
      more.type = 'button';
      more.className = 'load-more-button';
      more.textContent = 'Load older';
      more.addEventListener('click', loadNextPage);
      target.appendChild(more);
    }
    // Restore captured scroll so a Back-into-list returns to the row visible
    // when the user opened the brief.
    if (state.savedListScrollTop > 0) {
      const y = state.savedListScrollTop;
      window.requestAnimationFrame(function () {
        target.scrollTop = y;
      });
    }
    // Re-apply the selected-row highlight after re-render. Necessary for
    // deep-link paths (openBriefFromExternal fires openBrief BEFORE the
    // message list finishes loading; without this the row is never marked).
    if (state.brief && state.brief.filename) {
      const feedId = state.brief.feed_id || state.currentFeedId;
      markRowSelected(feedId, state.brief.filename);
    }
  }

  function renderMessageRow(item) {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'message-row' + (item.seen ? ' seen' : '');
    row.dataset.filename = item.filename;
    if (item.feed_id) row.dataset.feedId = item.feed_id;

    // Envelope glyph: filled orange envelope for unread, muted reply arrow for seen.
    const glyphChar = item.seen ? '↩' : '✉';

    // Timestamp verbose ("3 hrs, 38 min ago"). Reference-authentic.
    const timeText = verboseRelativeTime(item.mtime);

    // Title + snippet.
    // Prefer the plain-prose `preview` (body sentence past the title, spec §4
    // "2-line preview snippet"). Fall back to `tldr` when preview is empty
    // (older briefs, or briefs where the body sits behind a decoration we
    // skipped). Drop either if it collapses to the title text.
    const rawTitle = String(item.title || item.filename || '').trim();
    const title = stripLeadingEmoji(rawTitle);
    const previewText = String(item.preview || '').trim();
    const tldrText = String(item.tldr || '').trim();
    let snippetText = '';
    if (previewText && previewText.toLowerCase() !== title.toLowerCase()) {
      snippetText = previewText;
    } else if (tldrText && tldrText.toLowerCase() !== title.toLowerCase()) {
      snippetText = tldrText;
    }
    const showTldr = snippetText.length > 0;

    // Subtitle: category (feed display name) + optional tag chip.
    // We derive category from the feed id when unified, or from the current
    // feed metadata when in a feed view.
    let categoryLabel = '';
    if (item.feed_id) {
      categoryLabel = item.feed_display_name
        || (feedMetaFor(item.feed_id) && feedMetaFor(item.feed_id).display_name)
        || item.feed_id;
    } else if (state.currentFeedId) {
      const feed = feedMetaFor(state.currentFeedId);
      if (feed) categoryLabel = feed.display_name;
    }

    // Tag chip: reserved for a real signal, NOT a per-unread badge (that
    // reads as noise when everything is unread). Fires only for recent
    // (< 6h) unread briefs in the unified inbox — a "just landed" hint that
    // fades as briefs age.
    const ageHours = item.mtime ? (Date.now() / 1000 - item.mtime) / 3600 : Infinity;
    const tagHtml = (!item.seen && state.inUnifiedInbox && ageHours < 6)
      ? `<span class="row-tag tag-alert">New</span>`
      : '';

    row.innerHTML = `
      <span class="row-glyph" aria-hidden="true">${glyphChar}</span>
      <span class="row-title">${escapeHtml(title)}</span>
      <span class="row-time">${escapeHtml(timeText)}</span>
      <span class="row-subtitle">
        ${tagHtml}
        <span class="row-category">${escapeHtml(categoryLabel)}</span>
      </span>
      ${showTldr ? `<span class="row-snippet">${escapeHtml(snippetText)}</span>` : ''}
    `;
    row.addEventListener('click', function () { openBrief(item); });
    return row;
  }

  function markRowSelected(feedId, filename) {
    const list = document.getElementById('list-body');
    if (!list) return;
    list.querySelectorAll('.message-row').forEach(function (r) { r.classList.remove('selected'); });
    // In unified mode, feed_id is on the row; in per-feed mode, only filename identifies.
    const rows = list.querySelectorAll('.message-row');
    for (let i = 0; i < rows.length; i += 1) {
      const r = rows[i];
      const rowFilename = r.dataset.filename;
      const rowFeedId = r.dataset.feedId;
      const filenameMatches = rowFilename === filename;
      const feedMatches = !rowFeedId || rowFeedId === feedId;
      if (filenameMatches && feedMatches) {
        r.classList.add('selected');
        // Also scroll it into view if it's off-screen.
        try { r.scrollIntoView({ block: 'nearest' }); } catch (_) { /* older */ }
        break;
      }
    }
  }

  // --- Brief opener + reader letter ---------------------------------------

  async function openBrief(item, options) {
    const fromHistory = !!(options && options.fromHistory);
    // Save list scroll so back-into-list keeps the reader's spot.
    const listBody = document.getElementById('list-body');
    if (listBody) state.savedListScrollTop = listBody.scrollTop || 0;
    const feedId = item.feed_id || state.currentFeedId;
    if (!fromHistory && window.MineruRouter) {
      // If we're already sitting on a feed matching this item, just push;
      // otherwise reseed so back-out-of-reader lands on the feed list.
      if (state.currentFeedId === feedId && !state.inUnifiedInbox) {
        window.MineruRouter.navigate({ view: 'brief', feedId: feedId, filename: item.filename });
      } else {
        window.MineruRouter.seedFeedThenPushBrief(feedId, item.filename);
      }
    }

    // Reader chrome
    const readerBody = document.getElementById('reader-body');
    readerBody.innerHTML = '<div class="list-loading">Loading brief…</div>';

    let data = null;
    try {
      data = await apiGet(`/api/brief/${encodeURIComponent(feedId)}/${encodeURIComponent(item.filename)}`);
      state.brief = data;
      renderReader(data, feedId, item);
      // Mark seen after successful open.
      apiPost('/api/seen', { feed_id: feedId, filename: item.filename }).then(function () {
        item.seen = true;
        loadFeeds();
      }).catch(function () { /* ledger write failed; UI still fine */ });
    } catch (readerError) {
      readerBody.innerHTML = '';
      readerBody.appendChild(buildErrorCard(
        `Brief unavailable. ${readerError.message || ''}`.trim(),
        [{ label: 'Back to inbox', onClick: function () { goToUnifiedInbox(); } }],
      ));
      const feedMeta = feedMetaFor(feedId);
      window.MineruShell.setReaderHeader(item.title || item.filename,
        { primary: feedMeta ? feedMeta.display_name : 'Brief', aux: absoluteTime(item.mtime) }, []);
    }

    // Mark the source row selected in the list. If the list isn't the right
    // feed (opened from unified or search), we'll leave it alone — the user
    // will land back on the correct feed list when they hit back thanks to
    // seedFeedThenPushBrief above.
    if (feedId === state.currentFeedId || state.inUnifiedInbox) {
      markRowSelected(feedId, item.filename);
    }

    // On mobile, drill into the reader column.
    window.MineruShell.setStackLevel(window.MineruShell.isMobile() ? 2 : null);
  }

  function renderReader(data, feedId, item) {
    const feedMeta = feedMetaFor(feedId);
    const readerBody = document.getElementById('reader-body');
    const readerTitle = data.title || data.filename;
    const rawMarkdown = data.markdown || '';
    let bodyMarkdown = rawMarkdown;
    if (!bodyHasLeadingTitle(rawMarkdown, readerTitle)) {
      bodyMarkdown = `# ${readerTitle}\n\n${rawMarkdown}`;
    }

    const letter = document.createElement('div');
    letter.className = 'reader-letter';
    const scrollAnchor = document.createElement('div');
    scrollAnchor.className = 'reader-scroll-anchor';
    scrollAnchor.setAttribute('tabindex', '-1');
    letter.appendChild(scrollAnchor);
    const body = document.createElement('div');
    body.className = 'markdown-body';
    body.innerHTML = renderMarkdownSafe(bodyMarkdown);
    decorateReaderLinks(body);
    linkifyWorkspacePaths(body);
    letter.appendChild(body);
    readerBody.innerHTML = '';
    readerBody.appendChild(letter);
    try { scrollAnchor.focus({ preventScroll: true }); } catch (_) { /* older */ }

    // Reader header — title, subline (Feed · date), circular action cluster.
    const feedLabel = feedMeta ? `${feedMeta.emoji} ${feedMeta.display_name}` : (feedId || 'Brief');
    const readerMtime = (data && data.mtime) || (item && item.mtime) || 0;
    const subline = { primary: feedLabel, aux: absoluteTime(readerMtime) };
    // Action cluster — every button here must be functional (no dead
    // affordances). The reference has three circles; we ship the three we
    // can actually wire:
    //   Copy link — brief deep-link to the clipboard (pure client-side,
    //     works over http/tailnet without new endpoints).
    //   Open JSON — the brief's /api/brief endpoint in a new tab; label is
    //     honest ("Open JSON source") because that's what it is.
    //   Refresh  — re-fetch the brief.
    // The old ◑ Mark-unread was a no-op (there's no unmark endpoint and adding
    // one is out-of-scope security review); dropped to avoid a dead button.
    const briefHashPath = `#brief/${encodeURIComponent(feedId)}/${encodeURIComponent(data.filename)}`;
    const actions = [
      window.MineruShell.makeReaderActionButton('⧉', 'Copy link', function (event) {
        const button = event && event.currentTarget;
        const briefUrl = window.location.origin + window.location.pathname + briefHashPath;
        const onSuccess = function () {
          if (!button) return;
          const originalText = button.textContent;
          button.textContent = '✓';
          window.setTimeout(function () { button.textContent = originalText; }, 900);
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(briefUrl).then(onSuccess).catch(function () { /* silent */ });
        } else {
          // Older browser fallback via a hidden textarea.
          const scratch = document.createElement('textarea');
          scratch.value = briefUrl;
          scratch.setAttribute('aria-hidden', 'true');
          scratch.style.position = 'fixed';
          scratch.style.opacity = '0';
          document.body.appendChild(scratch);
          scratch.select();
          try { document.execCommand('copy'); onSuccess(); } catch (_) { /* silent */ }
          document.body.removeChild(scratch);
        }
      }),
      window.MineruShell.makeReaderActionButton('↗', 'Open JSON source', function () {
        // /api/brief/<feed>/<file> returns the brief's JSON envelope
        // (markdown + metadata). Label is honest so users aren't surprised.
        const url = `/api/brief/${encodeURIComponent(feedId)}/${encodeURIComponent(data.filename)}`;
        window.open(url, '_blank', 'noopener');
      }),
      window.MineruShell.makeReaderActionButton('⟲', 'Refresh', function () {
        openBrief(item, { fromHistory: true });
      }),
    ];
    window.MineruShell.setReaderHeader(readerTitle, subline, actions);
  }

  // --- External openers: Today card, search result, linkified path -------

  async function openBriefFromExternal(feedId, filename, meta, options) {
    if (!feedId || !filename) return;
    if (!state.feeds.length) {
      try { await loadFeeds(); } catch (_) { /* still open the brief */ }
    }
    // Switch to Inbox tab without clobbering the reader.
    window.MineruShell.switchTab('inbox', { fromHistory: true, skipEnter: true });
    // Ensure the message list for the target feed is loaded so back-out
    // lands on a real list (not an empty placeholder).
    if (state.currentFeedId !== feedId) {
      // Silent load: openFeed with fromHistory=true does not push, so the
      // seedFeedThenPushBrief below owns the history writes.
      state.currentFeedId = feedId;
      state.inUnifiedInbox = false;
      state.items = [];
      state.nextBefore = null;
      state.hasMore = false;
      state.requestToken += 1;
      window.MineruShell.setActiveFeed(feedId);
      const feed = feedMetaFor(feedId);
      window.MineruShell.setListHeader(feed ? `${feed.emoji} ${feed.display_name}` : 'Feed');
      installMarkAllReadButton(feedId, feed);
      // Kick off the load (no await — we open the brief in parallel).
      loadNextPage();
    }
    openBrief({
      feed_id: feedId,
      filename: filename,
      title: (meta && meta.title) || null,
      mtime: (meta && meta.mtime) || 0,
      seen: false,
    }, options);
  }

  function getFeedMeta(feedId) {
    return feedMetaFor(feedId);
  }

  // Back-compat: legacy goToLanding used to render the landing. Route to
  // the unified inbox landing.
  async function goToLanding(options) {
    return goToUnifiedInbox(options);
  }

  // Called by the shell when the reader is dismissed on mobile (level 2 → 1).
  // Drops the cached brief so the next renderMessageList doesn't re-apply the
  // stale `.selected` highlight against a row whose reader is no longer visible.
  function clearActiveBrief() {
    state.brief = null;
  }

  window.MineruInbox = {
    enter, openFeed, loadFeeds, openBriefFromExternal, getFeedMeta,
    goToLanding, goToUnifiedInbox, clearActiveBrief,
  };
})();
