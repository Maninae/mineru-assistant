/*
 * Shell coordinator — implements the MineruShell contract for the
 * three-pane mail client layout.
 *
 * The old contract was ONE pane-header + ONE pane-body swap. The new
 * contract has TWO independent header/body surfaces:
 *   - list column (folder view + message list): setListHeader / #list-body
 *   - reader column (brief / file letter):      setReaderHeader / #reader-body
 *
 * Plus:
 *   - renderFeedSidebar: draws folder groups + health dots + unread + coordinator heartbeat
 *   - switchTab: routes to inbox / library / pulse / chat
 *   - setStackLevel: on mobile, picks WHICH single column is visible
 *   - setActiveFeed, setActiveLibraryTab: highlight the current selection
 *
 * History integration is unchanged: user navigations flow through
 * MineruRouter.navigate(state); a single popstate listener re-invokes
 * MineruRouter.applyState(state) which drives the individual tab modules.
 * All back buttons (list-back, reader-back) call window.history.back()
 * so system-back (iOS swipe) agrees.
 */
(function () {
  'use strict';

  const { escapeHtml, relativeTime } = window.MineruApi;

  const TABS = {
    chat: () => window.MineruChat,
    inbox: () => window.MineruInbox,
    library: () => window.MineruLibrary,
    pulse: () => window.MineruPulse,
  };

  const state = {
    tab: 'inbox',
    activeFeedId: null,
    activeSection: 'inbox', // one of: inbox, library, pulse, chat — controls sidebar hilight
    stackLevel: null,       // 0 | 1 | 2 on mobile (null = derive on demand)
    lastCoordinatorTs: null,
  };

  const MOBILE_QUERY = '(max-width: 899px)';
  function isMobile() { return window.matchMedia(MOBILE_QUERY).matches; }

  // Set which single column is visible on mobile. Desktop grid always shows
  // all three regardless. Called by the router as views change so back
  // navigation resolves to the right column on phone.
  function setStackLevel(level) {
    const previousLevel = state.stackLevel;
    state.stackLevel = level;
    const shell = document.getElementById('app-shell');
    if (!shell) return;
    shell.classList.remove('stack-level-0', 'stack-level-1', 'stack-level-2');
    if (level === 0 || level === 1 || level === 2) {
      shell.classList.add(`stack-level-${level}`);
    }
    // Show the back button on mobile deep views so the user can drill up.
    const listBack = document.getElementById('list-back');
    const readerBack = document.getElementById('reader-back');
    if (listBack) listBack.hidden = !(isMobile() && level === 1);
    if (readerBack) readerBack.hidden = !(isMobile() && level === 2);
    // Mobile only: coming back to the list from the reader means the reader
    // is no longer on screen, so leaving a `.selected` row visible reads as a
    // stale highlight. Clear it AND tell the Inbox module to drop its cached
    // brief so a subsequent list re-render (from popstate) doesn't re-apply
    // the highlight. Desktop stays selected because both columns are visible
    // together (the highlight is the mail-client cross-reference).
    if (isMobile() && level === 1 && previousLevel === 2) {
      const listBody = document.getElementById('list-body');
      if (listBody) {
        listBody.querySelectorAll('.message-row.selected, .library-row.selected')
          .forEach(function (row) { row.classList.remove('selected'); });
      }
      if (window.MineruInbox && typeof window.MineruInbox.clearActiveBrief === 'function') {
        window.MineruInbox.clearActiveBrief();
      }
    }
  }

  function switchTab(tabId, options) {
    const tab = TABS[tabId] ? TABS[tabId]() : null;
    if (!tab) return;
    state.tab = tabId;
    state.activeSection = tabId;
    highlightActiveSection();
    if (!(options && options.fromHistory)) {
      if (window.MineruRouter) {
        // Library must carry a source in the pushed state; a bare
        // {view:'library'} produces `#library/` which openPath() rejects on
        // popstate/reload (see router.js), leaving the previous list visible.
        // Default to reports so back/reload always resolve to a real listing.
        const nextState = tabId === 'library'
          ? { view: 'library', source: 'reports', relpath: '' }
          : { view: tabId };
        window.MineruRouter.navigate(nextState);
      }
    }
    if (options && options.skipEnter) return;
    tab.enter();
  }

  function highlightActiveSection() {
    // Sidebar footer nav (Library/Pulse/Chat) + inbox row highlight.
    const inboxRow = document.getElementById('sidebar-inbox-row');
    if (inboxRow) {
      inboxRow.classList.toggle('active', state.activeSection === 'inbox' && !state.activeFeedId);
    }
    document.querySelectorAll('.sidebar-nav-btn').forEach(function (btn) {
      const isActive = btn.dataset.tab === state.activeSection;
      btn.classList.toggle('active', isActive);
      btn.setAttribute('aria-selected', isActive ? 'true' : 'false');
    });
    // Mobile bottom tabbar mirrors the section highlight so a router
    // restore doesn't leave the wrong tab bolded.
    document.querySelectorAll('.mobile-tabbar .tab-button').forEach(function (btn) {
      const isActive = btn.dataset.tab === state.activeSection;
      btn.classList.toggle('active', isActive);
    });
  }

  function wireStaticControls() {
    // Inbox row — unified "All Inbox" list
    const inboxRow = document.getElementById('sidebar-inbox-row');
    if (inboxRow) {
      inboxRow.addEventListener('click', function () {
        state.activeFeedId = null;
        switchTab('inbox');
      });
    }
    // Sidebar footer nav (Library / Pulse / Chat)
    document.querySelectorAll('.sidebar-nav-btn').forEach(function (btn) {
      btn.addEventListener('click', function () { switchTab(btn.dataset.tab); });
    });
    // Mobile bottom tabbar
    document.querySelectorAll('.mobile-tabbar .tab-button').forEach(function (btn) {
      btn.addEventListener('click', function () { switchTab(btn.dataset.tab); });
    });
    // Back buttons on list and reader headers — both call history.back().
    // For the reader-back on mobile: if the current history entry was a
    // brief opened without a list state beneath it, popToList() (see
    // MineruRouter) ensures the correct list is the parent.
    const listBack = document.getElementById('list-back');
    if (listBack) {
      listBack.addEventListener('click', function () {
        // Mobile drill-down: list-back reveals the sidebar (level 0).
        // No history push — the current #inbox/#feed state persists so
        // the system back-button can still exit the app / return to a
        // previous site. Reader-back below is the History-driven one.
        setStackLevel(0);
      });
    }
    const readerBack = document.getElementById('reader-back');
    if (readerBack) {
      readerBack.addEventListener('click', function () {
        // Reader-back walks the History stack. Because openBrief's callers
        // always push a `{view:'feed',feedId}` state directly beneath the
        // brief (via router.seedFeedThenPushBrief), this pops back onto
        // the correct feed list regardless of entry point (feed card /
        // unified inbox / search / linkified path). iOS swipe-back
        // triggers the same primitive so both agree.
        window.history.back();
      });
    }
    // Keep back-button visibility current on viewport changes.
    window.matchMedia(MOBILE_QUERY).addEventListener
      && window.matchMedia(MOBILE_QUERY).addEventListener('change', function () {
        setStackLevel(state.stackLevel);
      });
  }

  // --- List header (middle-column header) -------------------------------

  function setListHeader(title, options) {
    const opts = options || {};
    const listTitle = document.getElementById('list-title');
    if (listTitle) listTitle.textContent = title || '';
    const actions = document.getElementById('list-header-actions');
    if (actions) actions.innerHTML = '';
    // Back button behavior: on mobile, back visibility is derived from stack
    // level. On desktop, we never show the list-back button (the list is
    // always visible). Callers can hint "showBack" for mobile so the level-1
    // list can be reached from an intentional context.
    // opts.showBack is currently informational — setStackLevel drives the
    // real visibility.
    void opts.showBack;
  }

  function addListHeaderAction(node) {
    const actions = document.getElementById('list-header-actions');
    if (actions) actions.appendChild(node);
  }

  // --- Reader header (right-column header) ------------------------------

  // sub is either a plain string OR { primary, aux, statusPill }
  function setReaderHeader(title, sub, actions) {
    const readerTitle = document.getElementById('reader-title');
    const readerSubline = document.getElementById('reader-subline');
    const readerStatusPill = document.getElementById('reader-status-pill');
    const readerActions = document.getElementById('reader-header-actions');
    if (readerTitle) readerTitle.textContent = title || '';
    if (readerSubline) readerSubline.textContent = '';
    if (readerStatusPill) { readerStatusPill.hidden = true; readerStatusPill.textContent = ''; }
    if (sub && typeof sub === 'object') {
      if (readerSubline) {
        const parts = [];
        if (sub.primary) parts.push(String(sub.primary));
        if (sub.aux) parts.push(String(sub.aux));
        readerSubline.textContent = parts.join(' · ');
      }
      if (sub.statusPill && readerStatusPill) {
        readerStatusPill.hidden = false;
        readerStatusPill.textContent = String(sub.statusPill.label || '');
        readerStatusPill.className = 'reader-status-pill' + (sub.statusPill.kind === 'info' ? ' info' : '');
      }
    } else if (sub) {
      if (readerSubline) readerSubline.textContent = String(sub);
    }
    if (readerActions) {
      readerActions.innerHTML = '';
      (actions || []).forEach(function (btn) { readerActions.appendChild(btn); });
    }
  }

  function clearReader() {
    setReaderHeader('', '', []);
    const body = document.getElementById('reader-body');
    if (!body) return;
    body.innerHTML = `
      <div class="reader-empty-state">
        <span class="reader-empty-mark" aria-hidden="true">🦊</span>
        <span class="reader-empty-title">Select a brief to read</span>
      </div>
    `;
  }

  function makeReaderActionButton(label, ariaLabel, onClick) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'reader-action-btn';
    btn.textContent = label;
    btn.setAttribute('aria-label', ariaLabel || label);
    btn.addEventListener('click', onClick);
    return btn;
  }

  // --- Sidebar rendering (folder groups + coordinator heartbeat) --------

  function renderFeedSidebar(feeds, activeFeedId) {
    state.activeFeedId = activeFeedId;
    const container = document.getElementById('feed-groups');
    if (!container) return;
    container.innerHTML = '';

    // Group feeds by their `group` field. Preserve first-appearance order.
    const orderedGroups = [];
    const groupBucket = {};
    feeds.forEach(function (feed) {
      const g = feed.group || 'Feeds';
      if (!groupBucket[g]) { groupBucket[g] = []; orderedGroups.push(g); }
      groupBucket[g].push(feed);
    });

    orderedGroups.forEach(function (groupName) {
      const groupWrap = document.createElement('div');
      groupWrap.className = 'feed-group';
      const label = document.createElement('div');
      label.className = 'feed-group-label';
      label.textContent = groupName;
      groupWrap.appendChild(label);
      const list = document.createElement('div');
      list.className = 'feed-list';
      groupBucket[groupName].forEach(function (feed) {
        list.appendChild(renderFeedRow(feed, activeFeedId));
      });
      groupWrap.appendChild(list);
      container.appendChild(groupWrap);
    });

    // Refresh sidebar-inbox-row unread total (calm gray pill).
    const sumUnread = feeds.reduce(function (acc, f) { return acc + (f.unread_count || 0); }, 0);
    const inboxCount = document.getElementById('sidebar-inbox-count');
    if (inboxCount) {
      inboxCount.textContent = sumUnread > 999 ? '999+' : String(sumUnread);
      inboxCount.hidden = sumUnread === 0;
    }
    highlightActiveSection();
  }

  function renderFeedRow(feed, activeFeedId) {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'feed-row' + (feed.id === activeFeedId ? ' active' : '');
    row.dataset.feedId = feed.id;
    const health = feedHealthClass(feed);
    row.innerHTML = `
      <span class="feed-dot ${health}" aria-hidden="true"></span>
      <span class="feed-emoji" aria-hidden="true">${escapeHtml(feed.emoji || '📥')}</span>
      <span class="feed-name">${escapeHtml(feed.display_name || feed.id)}</span>
      ${renderFeedTail(feed)}
    `;
    row.addEventListener('click', function () {
      state.activeFeedId = feed.id;
      if (window.MineruRouter) {
        window.MineruRouter.navigate({ view: 'feed', feedId: feed.id });
      }
      switchTab('inbox', { fromHistory: true, skipEnter: true });
      if (window.MineruInbox) {
        window.MineruInbox.openFeed(feed.id);
      }
    });
    return row;
  }

  // Feed health from freshness signal in /api/feeds's `latest_ts`:
  //   <2d: fresh, <7d: stale, else: neutral. No failed signal here.
  // The pulse tab remains the source of truth for job failure; the sidebar
  // dot is a "is this feed still producing?" glance.
  function feedHealthClass(feed) {
    if (!feed || !feed.latest_ts) return 'neutral';
    const ageDays = (Date.now() / 1000 - feed.latest_ts) / 86400;
    if (ageDays < 2) return 'fresh';
    if (ageDays < 7) return 'stale';
    return 'neutral';
  }

  function renderFeedTail(feed) {
    const unread = Number(feed.unread_count || 0);
    if (unread > 0) {
      return `<span class="feed-envelope" aria-label="${unread} unread">✉</span>`;
    }
    const total = Number(feed.total_count || 0);
    if (total > 0) {
      return `<span class="feed-count">${escapeHtml(String(total))}</span>`;
    }
    return '';
  }

  function setActiveFeed(feedId) {
    state.activeFeedId = feedId;
    document.querySelectorAll('.feed-row').forEach(function (row) { row.classList.remove('active'); });
    // The renderFeedSidebar path also toggles, but this is the fast direct
    // route callers use when they don't want a full re-render.
    if (feedId) {
      const rows = document.querySelectorAll('.feed-row');
      // rows are in same order as feeds; no easy id lookup w/o dataset; add one:
      rows.forEach(function (row) {
        if (row.dataset && row.dataset.feedId === feedId) row.classList.add('active');
      });
    }
    highlightActiveSection();
  }

  // Legacy shim: preserve the old MineruShell.setPaneHeader signature for
  // any caller we haven't ported yet. Routes the call to the reader header
  // when a back callback is present (deep view), or clears the reader when
  // it's a landing view. Kept intentionally thin — every real caller is
  // being migrated to setListHeader / setReaderHeader.
  function setPaneHeader(emoji, title, sub, backCallback) {
    if (typeof backCallback === 'function') {
      setReaderHeader((emoji ? `${emoji} ` : '') + (title || ''), sub, []);
    } else {
      // Landing: show list header instead.
      setListHeader((emoji ? `${emoji} ` : '') + (title || ''));
      clearReader();
    }
  }

  // Coordinator heartbeat surfaced in the sidebar footer. Signal source:
  // /api/pulse's daemon.pid_alive plus its heartbeat.mineru.ts (or the
  // latest of the flat heartbeat keys). We render "Coordinator: Xm ago"
  // when we have a ts; "Coordinator: alive" if the daemon is alive but no
  // ts; "Coordinator: —" if unknown.
  function updateCoordinatorHeartbeat(pulseData) {
    const dot = document.querySelector('.coordinator-dot');
    const value = document.getElementById('coordinator-value');
    if (!value) return;
    if (!pulseData) {
      value.textContent = '—';
      if (dot) dot.className = 'coordinator-dot neutral';
      return;
    }
    const alive = !!(pulseData.daemon && pulseData.daemon.pid_alive);
    const ts = extractHeartbeatTimestamp(pulseData.heartbeat);
    // Prefer the actual daemon liveness signal (pid_alive) over the heartbeat
    // timestamp, which can drift stale even when the daemon is running fine
    // (a heartbeat file can sit untouched for months). If the ts is
    // fresh (<24h), show it; otherwise fall back to plain "alive".
    const ageHours = ts ? (Date.now() / 1000 - ts) / 3600 : Infinity;
    if (alive && ts && ageHours < 24) {
      value.textContent = relativeTime(ts);
    } else if (alive) {
      value.textContent = 'alive';
    } else {
      value.textContent = 'not running';
    }
    state.lastCoordinatorTs = ts || null;
    if (dot) {
      dot.className = 'coordinator-dot ' + (alive ? 'fresh' : 'failed');
    }
  }

  function extractHeartbeatTimestamp(heartbeat) {
    if (!heartbeat) return null;
    let newest = 0;
    function visit(node) {
      if (!node) return;
      if (typeof node === 'number' && Number.isFinite(node)) {
        let secs = node;
        if (secs > 1e12) secs = secs / 1000;
        if (secs > 1e9 && secs < 1e11 && secs > newest) newest = secs;
        return;
      }
      if (typeof node === 'object') {
        if (node.ts != null) visit(node.ts);
        if (node.last_seen != null) visit(node.last_seen);
        Object.keys(node).forEach(function (k) {
          if (k === 'ts' || k === 'last_seen' || k === 'status') return;
          visit(node[k]);
        });
      }
    }
    visit(heartbeat);
    return newest || null;
  }

  // Fetch pulse liveness for the coordinator heartbeat. Fire on load; refresh
  // opportunistically when the sidebar re-renders. Failure is silent (the
  // heartbeat just stays "—").
  async function loadCoordinatorHeartbeat() {
    try {
      const data = await window.MineruApi.apiGet('/api/pulse');
      updateCoordinatorHeartbeat(data);
    } catch (_) { /* silent */ }
  }

  // Global unread badge on the Inbox row / mobile tabbar (compat with old
  // MineruShell contract — updates the same numeric summary).
  function updateGlobalUnreadBadge(count) {
    const total = Number(count) || 0;
    const inboxCount = document.getElementById('sidebar-inbox-count');
    if (inboxCount) {
      inboxCount.textContent = total > 999 ? '999+' : String(total);
      inboxCount.hidden = total === 0;
    }
  }

  function closeSidebarOnMobile() {
    // No-op under the new shell — the sidebar/list/reader are separate
    // stack levels on mobile; nothing to close.
  }

  window.MineruShell = {
    // New contract
    setListHeader, addListHeaderAction,
    setReaderHeader, clearReader, makeReaderActionButton,
    setStackLevel, isMobile,
    // Sidebar
    renderFeedSidebar, setActiveFeed, updateGlobalUnreadBadge,
    updateCoordinatorHeartbeat, loadCoordinatorHeartbeat,
    // Tab switching
    switchTab,
    // Legacy compat (kept for any un-migrated caller)
    setPaneHeader, closeSidebarOnMobile,
  };

  function wireServiceWorker() {
    if (!('serviceWorker' in navigator)) return;
    navigator.serviceWorker.register('/sw.js').catch(function () { /* no-op */ });
  }

  document.addEventListener('DOMContentLoaded', function () {
    wireStaticControls();
    wireServiceWorker();
    loadCoordinatorHeartbeat();
    // Refresh coordinator heartbeat every minute so it doesn't sit stale.
    window.setInterval(loadCoordinatorHeartbeat, 60 * 1000);
    // Populate the sidebar feed folders on ANY landing (library/pulse/chat
    // deep-links included). Without this, opening the app on #library shows
    // an empty sidebar until the user first taps Inbox.
    if (window.MineruInbox && window.MineruInbox.loadFeeds) {
      window.MineruInbox.loadFeeds();
    }
    if (window.MineruRouter && window.MineruRouter.init) {
      window.MineruRouter.init();
    } else {
      switchTab('inbox');
    }
  });
})();
