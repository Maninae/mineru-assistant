/*
 * Small fetch wrapper shared by every tab.
 *
 * All requests go to same-origin `/api/*` paths; the server has already
 * enforced the read allowlist, so we never need cross-origin credentials.
 *
 * Errors are bucketed by HTTP status into a small set of human-readable
 * messages so the UI never leaks raw request paths to the user. The bucketed
 * message is on `.message`; the original status is on `.status` so callers
 * can key on it if they need to.
 */
(function () {
  'use strict';

  // Buckets a fetch failure into a human sentence — never leaks the URL.
  function messageForStatus(status) {
    if (status === 404) return 'Not found.';
    if (status === 403) return 'Not allowed.';
    if (status === 413) return 'Response too large.';
    if (status === 429) return 'Too many requests, try again shortly.';
    if (status >= 500 && status < 600) return 'Server error, try again shortly.';
    if (status >= 400 && status < 500) return 'Request rejected.';
    return 'Something went wrong.';
  }

  function apiError(status, path) {
    const err = new Error(messageForStatus(status));
    err.status = status;
    err.path = path; // for console debugging only, never rendered
    return err;
  }

  async function apiGet(path) {
    let response;
    try {
      // `cache: 'no-store'` skips the HTTP cache so a fresh GET after a
      // write (mark-all-read, mark-seen) doesn't serve a stale body from
      // the browser's 5-second Cache-Control window on /api/feeds. Every
      // API here is tiny and unread-count-shaped, so the cache saves nothing
      // real and costs us live correctness. Same-origin credentials still
      // apply so the loopback origin's session context is preserved.
      response = await fetch(path, { credentials: 'same-origin', cache: 'no-store' });
    } catch (networkError) {
      const err = new Error('Offline or unreachable.');
      err.status = 0;
      err.path = path;
      throw err;
    }
    if (!response.ok) throw apiError(response.status, path);
    return response.json();
  }

  async function apiPost(path, body) {
    let response;
    try {
      response = await fetch(path, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {}),
      });
    } catch (networkError) {
      const err = new Error('Offline or unreachable.');
      err.status = 0;
      err.path = path;
      throw err;
    }
    if (!response.ok) throw apiError(response.status, path);
    return response.json();
  }

  function relativeTime(unixSeconds) {
    if (unixSeconds == null || unixSeconds === 0) return 'never';
    const now = Date.now() / 1000;
    const delta = now - unixSeconds;
    if (delta < 60) return `${Math.max(1, Math.round(delta))}s ago`;
    if (delta < 3600) return `${Math.round(delta / 60)}m ago`;
    if (delta < 86400) return `${Math.round(delta / 3600)}h ago`;
    if (delta < 86400 * 14) return `${Math.round(delta / 86400)}d ago`;
    const date = new Date(unixSeconds * 1000);
    return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  }

  // Verbose form matching the mail-client reference: "3 hrs, 38 min ago",
  // "1 hr, 31 min ago", "Yesterday", "3 days ago", or a date past a week.
  // Meant for the mail message-row timestamp column, where the extra precision
  // signals recency at a glance the terse form doesn't.
  function verboseRelativeTime(unixSeconds) {
    if (unixSeconds == null || unixSeconds === 0) return '';
    const now = Date.now() / 1000;
    const delta = Math.max(0, now - unixSeconds);
    if (delta < 60) {
      const s = Math.max(1, Math.round(delta));
      return `${s} sec${s === 1 ? '' : 's'} ago`;
    }
    if (delta < 3600) {
      const m = Math.max(1, Math.round(delta / 60));
      return `${m} min ago`;
    }
    if (delta < 86400) {
      // Under a day: hours + minutes ("3 hrs, 38 min ago"). Minutes is
      // rounded down to keep the pair consistent (7:00 doesn't round up to
      // "3 hrs, 60 min").
      const hrs = Math.floor(delta / 3600);
      const mins = Math.floor((delta - hrs * 3600) / 60);
      const hrLabel = hrs === 1 ? 'hr' : 'hrs';
      if (mins === 0) return `${hrs} ${hrLabel} ago`;
      return `${hrs} ${hrLabel}, ${mins} min ago`;
    }
    if (delta < 86400 * 2) return 'Yesterday';
    if (delta < 86400 * 7) {
      const d = Math.floor(delta / 86400);
      return `${d} days ago`;
    }
    const date = new Date(unixSeconds * 1000);
    return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
  }

  function absoluteTime(unixSeconds) {
    if (unixSeconds == null || unixSeconds === 0) return '';
    const date = new Date(unixSeconds * 1000);
    return date.toLocaleString(undefined, {
      weekday: 'short', month: 'short', day: 'numeric',
      hour: 'numeric', minute: '2-digit',
    });
  }

  function humanBytes(bytes) {
    if (!bytes || bytes <= 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB'];
    let idx = 0;
    let value = bytes;
    while (value >= 1024 && idx < units.length - 1) {
      value /= 1024;
      idx += 1;
    }
    return `${value.toFixed(value >= 10 || idx === 0 ? 0 : 1)} ${units[idx]}`;
  }

  // Shared HTML-escape used by every renderer that composes with innerHTML.
  // Kept here so all interpolation sites route through one implementation.
  function escapeHtml(str) {
    return String(str == null ? '' : str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  // Aggressive normalization for the reader's body-has-title check: strips
  // leading emoji, ATX heading markers, interior `**...**` bold markers, and
  // collapses whitespace. Two title-y strings that render the same visually
  // must compare equal here. Morning briefs open with lines like
  // `☀ **Monday, August 24** — Rain until noon; two calls this afternoon.`
  // whose title (as extracted by the backend) is the same string minus the
  // leading emoji and the interior bold markers.
  function normalizeLineForTitleMatch(str) {
    return stripLeadingEmoji(String(str || ''))
      .replace(/^\s*#{1,3}\s+/, '')  // ATX heading markers
      .replace(/\*\*/g, '')          // bold markers anywhere
      .replace(/^\*+|\*+$/g, '')     // stray leading/trailing single stars
      .trim()
      .replace(/\s+/g, ' ')
      .toLowerCase();
  }

  // Find the index of the "first meaningful line" in a markdown blob: past
  // blank lines, blockquote lines (`> …`, curiosity briefs' status header),
  // and HTML comments. Returns -1 when nothing follows the preamble.
  function findFirstMeaningfulLineIndex(lines) {
    for (let i = 0; i < lines.length; i += 1) {
      const trimmed = lines[i].trim();
      if (trimmed === '') continue;
      if (trimmed.startsWith('>')) continue;
      if (trimmed.startsWith('<!--')) continue;
      return i;
    }
    return -1;
  }

  // True iff the markdown's first meaningful line normalizes to the same
  // string as `title`. Handles plain headings, `**Title**` bold paragraphs,
  // AND emoji-prefixed bold-opener paragraphs (every Morning brief).
  //
  // This is the reader's body-owns-title check: when true the reader body is
  // left alone (it already carries the title); when false the reader prepends
  // an H1 with the title so the view never renders untitled.
  function bodyHasLeadingTitle(markdown, title) {
    if (!markdown || !title) return false;
    const lines = String(markdown).split('\n');
    const idx = findFirstMeaningfulLineIndex(lines);
    if (idx < 0) return false;
    return normalizeLineForTitleMatch(lines[idx]) === normalizeLineForTitleMatch(title);
  }

  // Strip a leading `--- ... ---` YAML frontmatter block. Memory files under
  // reports/ carry a frontmatter block (description, tags) that is metadata
  // for the annotated memory tree, not body content — rendering it verbatim
  // above the reader body reads as noise. Called by the library file reader
  // before the title-dedup pass.
  function stripYamlFrontmatter(markdown) {
    const source = String(markdown || '');
    if (!source.startsWith('---')) return source;
    const lines = source.split('\n');
    // First line must be exactly `---` (allow trailing whitespace).
    if (lines[0].trim() !== '---') return source;
    for (let idx = 1; idx < lines.length; idx += 1) {
      if (lines[idx].trim() === '---') {
        // Drop the frontmatter block and any blank lines immediately after it.
        let start = idx + 1;
        while (start < lines.length && lines[start].trim() === '') start += 1;
        return lines.slice(start).join('\n');
      }
    }
    // Unterminated frontmatter — leave the doc alone rather than eat the body.
    return source;
  }

  // Strip the first heading / bold-opener line that matches `title`. Kept for
  // callers that want the strip-not-prepend shape (Library file reader, whose
  // pane-header carries the title directly).
  function stripLeadingMatchingHeading(markdown, title) {
    if (!markdown || !title) return markdown || '';
    const lines = markdown.split('\n');
    const idx = findFirstMeaningfulLineIndex(lines);
    if (idx < 0) return markdown;
    if (normalizeLineForTitleMatch(lines[idx]) !== normalizeLineForTitleMatch(title)) {
      return markdown;
    }
    // Drop the heading line, then any blank lines immediately after it.
    lines.splice(idx, 1);
    while (idx < lines.length && lines[idx].trim() === '') lines.splice(idx, 1);
    return lines.join('\n');
  }

  // Strip a single leading emoji run + whitespace from a display string. Used
  // to prevent Today-cards from carrying two competing anchors (the feed
  // emoji at the top and an inline emoji at the start of the title). Only the
  // leading run is stripped; interior emojis stay. Matches BMP + supplementary
  // pictographs, VS-16, ZWJ sequences, and combining marks. Falls back to
  // returning `str` unchanged when the browser predates the `\p{Extended_Pictographic}`
  // regex property (Safari 12 and later support it).
  const LEADING_EMOJI_RE = (() => {
    try {
      return new RegExp('^([\\p{Extended_Pictographic}\\p{Emoji_Component}\\uFE0F\\u200D\\s]+)', 'u');
    } catch (_) {
      return null;
    }
  })();
  function stripLeadingEmoji(str) {
    const source = String(str || '');
    if (!LEADING_EMOJI_RE) return source;
    return source.replace(LEADING_EMOJI_RE, '').trimStart();
  }

  // Build an error card DOM node: a bucketed sentence + one or more action
  // buttons. The sentence uses textContent so an error message never touches
  // innerHTML. Actions is a list of `{label, onClick}`; the first action is
  // the primary affordance.
  function buildErrorCard(message, actions) {
    const card = document.createElement('div');
    card.className = 'error-state';
    const msg = document.createElement('div');
    msg.className = 'error-state-message';
    msg.textContent = String(message || 'Something went wrong.');
    card.appendChild(msg);
    (actions || []).forEach(function (action) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'error-state-action';
      btn.textContent = String(action.label || 'Continue');
      btn.addEventListener('click', function () {
        if (typeof action.onClick === 'function') action.onClick();
      });
      card.appendChild(btn);
    });
    return card;
  }

  // Markdown -> HTML pipeline every reader tab shares.
  //
  // marked's parse() emits raw HTML; brief content is AI-generated from
  // outside sources (news pages, emails, calendar bodies, iMessage), so a
  // stray <script> or javascript:/onerror handler in a brief must NOT run.
  // DOMPurify strips exactly that class of content. The CSP is the second
  // line of defense (blocks inline JS execution even if a payload slips
  // through) but DOMPurify is the primary sanitizer.
  const MARKDOWN_ALLOWED_URI_RE = /^(?:https?:|mailto:|tel:|#|\/|\.\/|\.\.\/)/i;

  function renderMarkdownSafe(text) {
    const rendered = marked.parse(text || '', { breaks: true, gfm: true });
    return DOMPurify.sanitize(rendered, {
      USE_PROFILES: { html: true },
      ALLOW_DATA_ATTR: false,
      ALLOWED_URI_REGEXP: MARKDOWN_ALLOWED_URI_RE,
      FORBID_TAGS: ['style', 'form', 'iframe', 'object', 'embed', 'input', 'button', 'meta', 'link', 'script'],
      FORBID_ATTR: ['style', 'formaction', 'onload', 'onerror', 'onclick', 'onfocus', 'onblur', 'onmouseover', 'onmouseout', 'onkeyup', 'onkeydown', 'srcdoc'],
    });
  }

  function decorateReaderLinks(root) {
    // After sanitization is done: make external links open in a new tab.
    // Sanitizer already stripped javascript:/data: schemes.
    root.querySelectorAll('a').forEach(function (a) {
      a.setAttribute('target', '_blank');
      a.setAttribute('rel', 'noopener noreferrer');
    });
  }

  // Bare workspace paths that show up in briefs / reports as plain text (the
  // author typed `reports/2026-07-14-…md`) are turned into clickable links
  // that open the target in the right in-app reader. Matches ONLY these
  // conservative shapes so we never guess:
  //
  //   reports/<name>.(md|html|htm|pdf)
  //   creations/<name>.(md|html|htm|pdf)
  //   briefs_<feed>/<name>.md
  //
  // The rewriter walks text nodes (createElement + textContent — NEVER
  // innerHTML on the matched substring), so no matter what characters land
  // inside the path they can't escape into markup. Nodes already inside <a>,
  // <code>, or <pre> are skipped so we don't linkify code samples or
  // pre-linked citations. The click is routed through the app's own openers,
  // which validate the target on fetch — a stale path 404s into the reader's
  // friendly error state instead of navigating anywhere.
  //
  // The pattern deliberately does NOT match leading paths (../reports/x.md),
  // absolute paths (/Users/…/reports/x.md), or non-allowlisted extensions.
  const WORKSPACE_PATH_RE = /(?:^|(?<=[\s\(\[\{"'`>]))(reports|creations|briefs_[a-z0-9_]+)\/([A-Za-z0-9][A-Za-z0-9._\/-]*\.(?:md|html|htm|pdf))/g;
  // Inline `<code>` gets rewritten (backtick-quoted paths in briefs are the
  // common case), but block-level `<pre>` (fenced code samples) does not —
  // the ancestor walk finds <pre> above the <code> in that case and rejects.
  const LINKIFY_SKIP_TAGS = new Set(['A', 'PRE', 'SCRIPT', 'STYLE', 'TEXTAREA']);

  function linkifyWorkspacePaths(root, options) {
    if (!root) return;
    const onClickPath = (options && options.onClick) || defaultLinkifyClickHandler;
    // Collect first, mutate after — otherwise splitting a text node during
    // the walk confuses the walker's cursor.
    const textNodes = [];
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: function (node) {
        // Walk up ancestors; if any is in the skip set, reject the whole
        // subtree's leaves. This keeps `<code>reports/foo.md</code>` a code
        // sample and `<a>reports/foo.md</a>` a single link (never rewrapped).
        let ancestor = node.parentNode;
        while (ancestor && ancestor !== root) {
          if (ancestor.nodeType === 1 && LINKIFY_SKIP_TAGS.has(ancestor.tagName)) {
            return NodeFilter.FILTER_REJECT;
          }
          ancestor = ancestor.parentNode;
        }
        // Fast path: only recheck the regex when the text actually contains a
        // candidate prefix. Regex has global `g` so we must reset it below on
        // reuse; here we only test for the sentinel to skip empty scans.
        return /reports\/|creations\/|briefs_/.test(node.nodeValue)
          ? NodeFilter.FILTER_ACCEPT
          : NodeFilter.FILTER_REJECT;
      },
    });
    let currentNode = walker.nextNode();
    while (currentNode) {
      textNodes.push(currentNode);
      currentNode = walker.nextNode();
    }
    textNodes.forEach(function (textNode) {
      rewriteTextNodeWithLinks(textNode, onClickPath);
    });
  }

  function rewriteTextNodeWithLinks(textNode, onClickPath) {
    const source = textNode.nodeValue;
    WORKSPACE_PATH_RE.lastIndex = 0;
    let match = WORKSPACE_PATH_RE.exec(source);
    if (!match) return;
    const parent = textNode.parentNode;
    if (!parent) return;
    const fragment = document.createDocumentFragment();
    let cursor = 0;
    while (match) {
      const start = match.index;
      const fullPath = `${match[1]}/${match[2]}`;
      if (start > cursor) {
        fragment.appendChild(document.createTextNode(source.slice(cursor, start)));
      }
      fragment.appendChild(buildPathLink(match[1], match[2], fullPath, onClickPath));
      cursor = start + fullPath.length;
      match = WORKSPACE_PATH_RE.exec(source);
    }
    if (cursor < source.length) {
      fragment.appendChild(document.createTextNode(source.slice(cursor)));
    }
    parent.replaceChild(fragment, textNode);
  }

  function buildPathLink(prefix, rest, fullPath, onClickPath) {
    // Route: reports/… + creations/… → Library viewer; briefs_<feed>/… → brief reader.
    const link = document.createElement('a');
    link.className = 'workspace-path-link';
    // href = '#' + fullPath so the address bar shows the target on hover /
    // long-press. The click handler preventDefaults navigation; the '#'
    // fragment is inert to the browser and safe.
    link.setAttribute('href', `#${fullPath}`);
    link.setAttribute('title', `Open ${fullPath}`);
    link.textContent = fullPath;
    // Do NOT copy decorateReaderLinks' target=_blank — this is an in-app
    // navigation, not an external URL.
    link.addEventListener('click', function (event) {
      event.preventDefault();
      let target;
      if (prefix === 'reports' || prefix === 'creations') {
        target = { kind: 'library', source: prefix, relpath: rest };
      } else if (prefix.startsWith('briefs_')) {
        target = { kind: 'brief', feed_id: prefix.slice('briefs_'.length), filename: rest };
      } else {
        return;
      }
      onClickPath(target);
    });
    return link;
  }

  function defaultLinkifyClickHandler(target) {
    if (target.kind === 'library' && window.MineruLibrary && window.MineruLibrary.openPath) {
      window.MineruLibrary.openPath(target.source, target.relpath);
    } else if (target.kind === 'brief' && window.MineruInbox && window.MineruInbox.openBriefFromExternal) {
      window.MineruInbox.openBriefFromExternal(target.feed_id, target.filename);
    }
  }

  window.MineruApi = {
    apiGet, apiPost, relativeTime, verboseRelativeTime, absoluteTime, humanBytes,
    renderMarkdownSafe, decorateReaderLinks, linkifyWorkspacePaths,
    escapeHtml, stripLeadingMatchingHeading, stripYamlFrontmatter, bodyHasLeadingTitle,
    stripLeadingEmoji, buildErrorCard,
  };
})();
