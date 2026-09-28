/*
 * URL/history router for the three-pane shell.
 *
 * The routing model didn't change (History API + hash), but applyState now
 * updates TWO independent surfaces (list column, reader column) instead of
 * one main pane. Views map to columns:
 *
 *   #inbox                 → LIST: unified "All Inbox" (cross-feed). READER: empty.
 *   #feed/<id>             → LIST: that feed's messages.               READER: empty.
 *   #brief/<feed>/<file>   → LIST: that feed's messages (loaded if missing).
 *                            READER: the brief letter. Row marked .selected.
 *   #library/<src>[/<rel>] → LIST: source's tree.                      READER: file (if a file), else empty.
 *   #search/<q>            → LIST: search results.                     READER: empty.
 *   #pulse                 → LIST+READER: pulse fills the reader column, sidebar stays.
 *   #chat                  → LIST+READER: chat placeholder in reader column.
 *
 * Mobile drill-down levels (setStackLevel(N)):
 *   0 = sidebar (folders), 1 = list column, 2 = reader column.
 * Views map to levels:
 *   inbox/feed/library-dir/search/pulse/chat → 1
 *   brief/library-file                       → 2
 *
 * Back-button fix (spec §9):
 *   history.back() is the single primitive for both list-back and reader-
 *   back. When a brief is opened from a context that skipped its feed list
 *   (Today card, search result, linkified path), openBriefFromExternal
 *   PUSHES a {view:'feed', feedId} state BEFORE pushing {view:'brief',...},
 *   so back from the reader always lands on the feed list, deterministically.
 */
(function () {
  'use strict';

  if ('scrollRestoration' in history) {
    history.scrollRestoration = 'manual';
  }

  function stateToHash(state) {
    if (!state) return 'inbox';
    switch (state.view) {
      case 'inbox':  return 'inbox';
      case 'pulse':  return 'pulse';
      case 'chat':   return 'chat';
      case 'feed':   return 'feed/' + encodeURIComponent(state.feedId || '');
      case 'brief':
        return 'brief/' + encodeURIComponent(state.feedId || '')
          + '/' + encodeURIComponent(state.filename || '');
      case 'library':
        if (state.relpath) {
          const encodedPath = String(state.relpath).split('/').filter(Boolean).map(encodeURIComponent).join('/');
          return 'library/' + encodeURIComponent(state.source || '') + '/' + encodedPath;
        }
        return 'library/' + encodeURIComponent(state.source || '');
      case 'search': return 'search/' + encodeURIComponent(state.query || '');
      default:       return 'inbox';
    }
  }

  function hashToState(hashStr) {
    const raw = String(hashStr || '').replace(/^#/, '');
    if (!raw || raw === 'inbox' || raw === '/') return { view: 'inbox' };
    const parts = raw.split('/');
    const kind = parts[0];
    if (kind === 'inbox') return { view: 'inbox' };
    if (kind === 'pulse') return { view: 'pulse' };
    if (kind === 'chat')  return { view: 'chat' };
    if (kind === 'feed' && parts[1]) {
      return { view: 'feed', feedId: safeDecode(parts[1]) };
    }
    if (kind === 'brief' && parts[1] && parts[2]) {
      return { view: 'brief', feedId: safeDecode(parts[1]), filename: safeDecode(parts[2]) };
    }
    if (kind === 'library') {
      // Bare `#library` (deep-link, sidebar-nav-click default, or reload of a
      // sourceless state) collapses to the reports source at the root. Without
      // this default, applyState below hands `source: undefined` to
      // openPath(), which early-returns and leaves whatever the previous view
      // rendered on screen (the reload-shows-stale-feed bug).
      if (!parts[1]) {
        return { view: 'library', source: 'reports', relpath: '' };
      }
      const source = safeDecode(parts[1]);
      if (parts.length > 2) {
        const relpath = parts.slice(2).map(safeDecode).join('/');
        return { view: 'library', source: source, relpath: relpath };
      }
      return { view: 'library', source: source, relpath: '' };
    }
    if (kind === 'search' && parts.length >= 2) {
      const query = parts.slice(1).map(safeDecode).join('/');
      return { view: 'search', query: query };
    }
    return null;
  }

  function safeDecode(segment) {
    try { return decodeURIComponent(segment); } catch (_) { return String(segment || ''); }
  }

  function sameState(a, b) {
    if (!a || !b) return false;
    return a.view === b.view
      && (a.feedId || '') === (b.feedId || '')
      && (a.filename || '') === (b.filename || '')
      && (a.source || '') === (b.source || '')
      && (a.relpath || '') === (b.relpath || '')
      && (a.query || '') === (b.query || '');
  }

  // Map a state to the mobile stack level (0 folders, 1 list, 2 reader).
  function stackLevelFor(state) {
    if (!state) return 1;
    switch (state.view) {
      case 'brief': return 2;
      case 'library':
        // Library file view lives in the reader column; dir view in the list.
        return state.relpath && /\.[a-z0-9]{1,5}$/i.test(state.relpath) ? 2 : 1;
      default: return 1;
    }
  }

  function applyState(state) {
    if (!state) state = { view: 'inbox' };
    const shell = window.MineruShell;
    if (!shell) return;
    // Set mobile stack level up front; individual renderers may re-set it
    // when they've done work that changes which surface should be visible.
    shell.setStackLevel(stackLevelFor(state));

    switch (state.view) {
      case 'inbox':
        shell.switchTab('inbox', { fromHistory: true, skipEnter: true });
        if (window.MineruInbox && window.MineruInbox.goToUnifiedInbox) {
          window.MineruInbox.goToUnifiedInbox({ fromHistory: true });
        }
        break;
      case 'pulse':
        shell.switchTab('pulse', { fromHistory: true });
        break;
      case 'chat':
        shell.switchTab('chat', { fromHistory: true });
        break;
      case 'feed':
        shell.switchTab('inbox', { fromHistory: true, skipEnter: true });
        if (window.MineruInbox && window.MineruInbox.openFeed) {
          window.MineruInbox.openFeed(state.feedId, { fromHistory: true });
        }
        break;
      case 'brief':
        shell.switchTab('inbox', { fromHistory: true, skipEnter: true });
        if (window.MineruInbox && window.MineruInbox.openBriefFromExternal) {
          window.MineruInbox.openBriefFromExternal(state.feedId, state.filename, null, { fromHistory: true });
        }
        break;
      case 'library':
        shell.switchTab('library', { fromHistory: true, skipEnter: true });
        if (window.MineruLibrary && window.MineruLibrary.openPath) {
          // Default a missing source to 'reports' so a bare {view:'library'}
          // state (sidebar-nav restore, popstate to a sourceless entry,
          // reload of `#library`) still renders instead of no-op'ing.
          window.MineruLibrary.openPath(state.source || 'reports', state.relpath || '', { fromHistory: true });
        }
        break;
      case 'search':
        shell.switchTab('inbox', { fromHistory: true, skipEnter: true });
        if (window.MineruInbox && window.MineruInbox.goToUnifiedInbox) {
          Promise.resolve(window.MineruInbox.goToUnifiedInbox({ fromHistory: true })).then(function () {
            if (window.MineruSearch && window.MineruSearch.setQuery) {
              window.MineruSearch.setQuery(state.query || '', { fromHistory: true });
            }
          });
        }
        break;
      default:
        shell.switchTab('inbox', { fromHistory: true });
    }
  }

  function navigate(state) {
    if (!state) return;
    const targetHash = '#' + stateToHash(state);
    if (history.state && sameState(history.state, state) && location.hash === targetHash) {
      return;
    }
    history.pushState(state, '', targetHash);
  }

  function replace(state) {
    if (!state) return;
    const targetHash = '#' + stateToHash(state);
    history.replaceState(state, '', targetHash);
  }

  // Ensure a `{view:'feed', feedId}` state lives directly beneath the CURRENT
  // history entry, so a subsequent history.back() from a brief always lands
  // on that feed's message list. Called by openBriefFromExternal (Today card
  // taps, search result opens, linkified path clicks) so the reader-back
  // path is deterministic regardless of entry point.
  //
  // Mechanic: replaceState(feed) rewrites the current entry to the feed
  // state; the caller then pushState(brief) on top. The user's PREVIOUS
  // history entry (whatever was before them clicking into the brief) is
  // preserved one step back, so back-out-of-brief → feed list, and back-
  // out-of-feed-list → the original context.
  function seedFeedThenPushBrief(feedId, filename) {
    if (!feedId || !filename) return;
    const feedState = { view: 'feed', feedId: feedId };
    const briefState = { view: 'brief', feedId: feedId, filename: filename };
    // If we're already on the correct feed state, skip the reseed and just
    // push the brief (this is the "open from the feed's message row" path).
    if (history.state && history.state.view === 'feed' && history.state.feedId === feedId) {
      history.pushState(briefState, '', '#' + stateToHash(briefState));
      return;
    }
    // Rewrite the current entry to the feed state, then push the brief on
    // top. The URL reflects both moves.
    history.replaceState(feedState, '', '#' + stateToHash(feedState));
    history.pushState(briefState, '', '#' + stateToHash(briefState));
  }

  function init() {
    const parsed = hashToState(location.hash);
    if (!parsed) {
      history.replaceState({ view: 'inbox' }, '', '#inbox');
      applyState({ view: 'inbox' });
      return;
    }
    if (parsed.view === 'inbox') {
      history.replaceState(parsed, '', '#inbox');
    } else if (parsed.view === 'brief') {
      // Deep-link to a brief: seed [inbox, feed, brief] so back-out-of-brief
      // lands on the feed list, and back-out-of-that lands on inbox.
      history.replaceState({ view: 'inbox' }, '', '#inbox');
      const feedState = { view: 'feed', feedId: parsed.feedId };
      history.pushState(feedState, '', '#' + stateToHash(feedState));
      history.pushState(parsed, '', '#' + stateToHash(parsed));
    } else {
      // Other deep links: seed [inbox, target] so OS back returns to landing.
      history.replaceState({ view: 'inbox' }, '', '#inbox');
      history.pushState(parsed, '', '#' + stateToHash(parsed));
    }
    applyState(parsed);
  }

  window.addEventListener('popstate', function (event) {
    let state = event.state;
    if (!state) {
      state = hashToState(location.hash);
      if (!state) {
        history.replaceState({ view: 'inbox' }, '', '#inbox');
        state = { view: 'inbox' };
      }
    }
    applyState(state);
  });

  window.MineruRouter = {
    init: init,
    navigate: navigate,
    replace: replace,
    applyState: applyState,
    stateToHash: stateToHash,
    hashToState: hashToState,
    seedFeedThenPushBrief: seedFeedThenPushBrief,
  };
})();
