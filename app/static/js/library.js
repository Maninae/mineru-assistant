/*
 * Library tab. Browse reports/ and creations/, open files inline.
 *
 * Shell mapping:
 *   list column   → breadcrumbs + directory rows (or source picker)
 *   reader column → opened file (markdown / image / pdf / html / text)
 *
 * History integration: every drill-in routes through MineruRouter.navigate.
 * A router-driven restore passes {fromHistory:true} so the module re-renders
 * without pushing.
 */
(function () {
  'use strict';

  const {
    apiGet, absoluteTime, humanBytes,
    renderMarkdownSafe, decorateReaderLinks, linkifyWorkspacePaths,
    escapeHtml, stripLeadingMatchingHeading, stripYamlFrontmatter, buildErrorCard,
  } = window.MineruApi;

  const DATED_FILENAME_RE = /^(\d{4}-\d{2})-\d{2}-/;
  const FULL_DATE_FILENAME_RE = /^(\d{4}-\d{2}-\d{2})-/;
  const MIN_ENTRIES_FOR_MONTH_SUBHEADERS = 3;

  function monthKeyFromEntry(entry) {
    if (!entry) return null;
    const match = String(entry.name || '').match(DATED_FILENAME_RE);
    return match ? match[1] : null;
  }
  function fullDateKeyFromEntry(entry) {
    if (!entry) return '';
    const match = String(entry.name || '').match(FULL_DATE_FILENAME_RE);
    return match ? match[1] : '';
  }
  function monthKeyToLabel(monthKey) {
    const match = String(monthKey || '').match(/^(\d{4})-(\d{2})$/);
    if (!match) return String(monthKey || '');
    const year = Number(match[1]);
    const monthIdx = Number(match[2]) - 1;
    const date = new Date(year, monthIdx, 1);
    if (Number.isNaN(date.getTime())) return String(monthKey);
    return date.toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
  }

  const SOURCES = [
    { id: 'reports', label: 'Reports', emoji: '📄' },
    { id: 'creations', label: 'Creations', emoji: '✨' },
  ];

  const state = {
    source: 'reports',
    relpath: '',
  };

  function enter() {
    renderListHeader();
    loadDirectory();
  }

  function renderListHeader() {
    const source = SOURCES.find(function (s) { return s.id === state.source; });
    window.MineruShell.setListHeader(source ? `📚 ${source.label}` : 'Library');
    // Actions: the source picker (pills)
    const actionsWrap = document.createElement('div');
    actionsWrap.className = 'library-source-pills';
    SOURCES.forEach(function (src) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'library-source-pill' + (src.id === state.source ? ' active' : '');
      btn.textContent = src.label;
      btn.addEventListener('click', function () {
        if (window.MineruRouter) {
          window.MineruRouter.navigate({ view: 'library', source: src.id, relpath: '' });
        }
        state.source = src.id;
        state.relpath = '';
        renderListHeader();
        loadDirectory();
      });
      actionsWrap.appendChild(btn);
    });
    document.getElementById('list-header-actions').innerHTML = '';
    window.MineruShell.addListHeaderAction(actionsWrap);
  }

  async function loadDirectory() {
    const target = document.getElementById('list-body');
    target.innerHTML = '<div class="list-loading">Loading…</div>';
    try {
      const path = state.relpath
        ? `/api/library/${encodeURIComponent(state.source)}/${encodeURIComponent(state.relpath)}`
        : `/api/library/${encodeURIComponent(state.source)}`;
      const data = await apiGet(path);
      if (data.kind === 'file') {
        // For a file, the list column stays showing the parent directory;
        // load the parent listing behind the file view so the two panes
        // read together.
        const parent = data.relpath.split('/').slice(0, -1).join('/');
        try {
          const parentData = parent
            ? await apiGet(`/api/library/${encodeURIComponent(state.source)}/${encodeURIComponent(parent)}`)
            : await apiGet(`/api/library/${encodeURIComponent(state.source)}`);
          renderListing(parentData, data.relpath);
        } catch (_) {
          // Parent unreachable — leave the list as-is.
        }
        renderFile(data);
        window.MineruShell.setStackLevel(window.MineruShell.isMobile() ? 2 : null);
      } else {
        renderListing(data, null);
        window.MineruShell.clearReader();
        window.MineruShell.setStackLevel(window.MineruShell.isMobile() ? 1 : null);
      }
    } catch (loadError) {
      target.innerHTML = '';
      const actions = loadError.status === 0
        ? [{ label: 'Retry', onClick: function () { loadDirectory(); } }, { label: 'Back to Library', onClick: function () { navigateTo(''); } }]
        : [{ label: 'Back to Library', onClick: function () { navigateTo(''); } }];
      target.appendChild(buildErrorCard(`Library error. ${loadError.message || ''}`.trim(), actions));
    }
  }

  function renderListing(data, activeRelpath) {
    const target = document.getElementById('list-body');
    target.innerHTML = '';
    target.appendChild(renderBreadcrumbs());
    const entries = data.entries || [];
    if (!entries.length) {
      const empty = document.createElement('div');
      empty.className = 'list-empty';
      empty.textContent = 'Empty.';
      target.appendChild(empty);
      return;
    }
    const undatedEntries = [];
    const datedEntries = [];
    entries.forEach(function (entry) {
      if (monthKeyFromEntry(entry) !== null) datedEntries.push(entry);
      else undatedEntries.push(entry);
    });
    datedEntries.sort(function (a, b) {
      const ka = fullDateKeyFromEntry(a);
      const kb = fullDateKeyFromEntry(b);
      if (ka === kb) return 0;
      return ka < kb ? 1 : -1;
    });

    const list = document.createElement('div');
    list.className = 'library-list';

    undatedEntries.forEach(function (entry) {
      list.appendChild(renderLibraryRow(entry, activeRelpath));
    });

    if (datedEntries.length >= MIN_ENTRIES_FOR_MONTH_SUBHEADERS) {
      let lastMonthKey = null;
      datedEntries.forEach(function (entry) {
        const monthKey = monthKeyFromEntry(entry);
        if (monthKey && monthKey !== lastMonthKey) {
          const header = document.createElement('div');
          header.className = 'library-month-header';
          header.textContent = monthKeyToLabel(monthKey);
          list.appendChild(header);
          lastMonthKey = monthKey;
        }
        list.appendChild(renderLibraryRow(entry, activeRelpath));
      });
    } else {
      datedEntries.forEach(function (entry) {
        list.appendChild(renderLibraryRow(entry, activeRelpath));
      });
    }
    target.appendChild(list);
  }

  function renderLibraryRow(entry, activeRelpath) {
    const row = document.createElement('button');
    row.type = 'button';
    const isActive = activeRelpath && entry.relpath === activeRelpath;
    row.className = 'library-row' + (isActive ? ' selected' : '');
    const glyph = entry.kind === 'dir' ? '📁' : glyphFor(entry.extension);
    const rowLabel = entry.display_name
      ? stripDisplayExtension(entry.display_name)
      : stripDisplayExtension(entry.name);
    let metaText;
    if (entry.kind === 'dir') {
      metaText = entry.date_label ? `${entry.date_label} · folder` : `folder · ${absoluteTime(entry.mtime)}`;
    } else if (entry.date_label) {
      metaText = `${entry.date_label} · ${humanBytes(entry.size)}`;
    } else {
      metaText = `${humanBytes(entry.size)} · ${absoluteTime(entry.mtime)}`;
    }
    row.innerHTML = `
      <span class="row-glyph">${glyph}</span>
      <span class="row-name">${escapeHtml(rowLabel)}</span>
      <span class="row-meta">${escapeHtml(metaText)}</span>
    `;
    row.addEventListener('click', function () {
      navigateTo(entry.relpath);
    });
    return row;
  }

  function stripDisplayExtension(name) {
    return String(name || '').replace(/\.(?:html?|md|markdown)$/i, '');
  }

  function navigateTo(newRelpath) {
    if (window.MineruRouter) {
      window.MineruRouter.navigate({ view: 'library', source: state.source, relpath: newRelpath || '' });
    }
    state.relpath = newRelpath || '';
    loadDirectory();
  }

  function glyphFor(extension) {
    if (extension === '.md') return '📄';
    if (extension === '.pdf') return '📑';
    if (extension === '.html' || extension === '.htm') return '🌐';
    if (['.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg'].indexOf(extension) >= 0) return '🖼️';
    if (['.txt', '.log', '.csv', '.tsv', '.json'].indexOf(extension) >= 0) return '📃';
    return '📄';
  }

  function renderBreadcrumbs() {
    const wrap = document.createElement('div');
    wrap.className = 'library-breadcrumbs';
    wrap.setAttribute('aria-label', 'Breadcrumb');
    const root = document.createElement('a');
    root.className = 'crumb';
    root.setAttribute('href', `#library/${encodeURIComponent(state.source)}`);
    root.textContent = state.source;
    root.addEventListener('click', function (event) {
      event.preventDefault();
      navigateTo('');
    });
    wrap.appendChild(root);
    if (!state.relpath) return wrap;
    const parts = state.relpath.split('/').filter(Boolean);
    let acc = '';
    parts.forEach(function (part, idx) {
      const sep = document.createElement('span');
      sep.className = 'crumb-sep';
      sep.setAttribute('aria-hidden', 'true');
      sep.textContent = '›';
      wrap.appendChild(sep);
      acc = acc ? `${acc}/${part}` : part;
      const isCurrent = idx === parts.length - 1;
      if (isCurrent) {
        const seg = document.createElement('span');
        seg.className = 'crumb current';
        seg.setAttribute('aria-current', 'page');
        seg.textContent = stripDisplayExtension(part);
        wrap.appendChild(seg);
      } else {
        const seg = document.createElement('a');
        seg.className = 'crumb';
        const targetPath = acc.split('/').map(encodeURIComponent).join('/');
        seg.setAttribute('href', `#library/${encodeURIComponent(state.source)}/${targetPath}`);
        seg.textContent = part;
        const targetRel = acc;
        seg.addEventListener('click', function (event) {
          event.preventDefault();
          navigateTo(targetRel);
        });
        wrap.appendChild(seg);
      }
    });
    return wrap;
  }

  function renderFile(data) {
    const readerBody = document.getElementById('reader-body');
    readerBody.innerHTML = '';
    const container = document.createElement('div');
    container.className = 'reader-letter';
    const scrollAnchor = document.createElement('div');
    scrollAnchor.className = 'reader-scroll-anchor';
    scrollAnchor.setAttribute('tabindex', '-1');
    container.appendChild(scrollAnchor);
    const filenameOnly = data.relpath.split('/').pop();
    const readerTitle = data.title || stripDisplayExtension(filenameOnly);

    if (data.media_kind === 'markdown') {
      const body = document.createElement('div');
      body.className = 'markdown-body';
      // Peel any leading `--- ... ---` YAML frontmatter (memory files under
      // reports/ carry description/tags metadata that shouldn't render as
      // body copy), then dedupe against the reader title.
      const withoutFrontmatter = stripYamlFrontmatter(data.markdown || '');
      const bodyMarkdown = stripLeadingMatchingHeading(withoutFrontmatter, readerTitle);
      body.innerHTML = renderMarkdownSafe(bodyMarkdown);
      decorateReaderLinks(body);
      linkifyWorkspacePaths(body);
      container.appendChild(body);
    } else if (data.media_kind === 'image') {
      const img = document.createElement('img');
      img.src = data.raw_url;
      img.alt = filenameOnly;
      img.style.maxWidth = '100%';
      img.style.borderRadius = '10px';
      container.appendChild(img);
    } else if (data.media_kind === 'pdf') {
      const embed = document.createElement('embed');
      embed.src = data.raw_url;
      embed.type = 'application/pdf';
      embed.style.width = '100%';
      embed.style.height = '80vh';
      embed.style.border = '1px solid var(--divider)';
      embed.style.borderRadius = '10px';
      container.appendChild(embed);
    } else if (data.media_kind === 'html') {
      const iframe = document.createElement('iframe');
      iframe.className = 'iframe-sandbox';
      // ============================================================
      // SECURITY: sandbox="allow-scripts" — scripts ON, nothing else.
      // NEVER add `allow-same-origin`. See inbox module for full rationale.
      // ============================================================
      iframe.setAttribute('sandbox', 'allow-scripts');
      iframe.src = data.sandbox_url;
      iframe.loading = 'lazy';
      container.appendChild(iframe);
    } else if (data.media_kind === 'text') {
      const pre = document.createElement('pre');
      pre.style.background = 'var(--surface-muted)';
      pre.style.padding = '14px 16px';
      pre.style.borderRadius = '10px';
      pre.style.overflowX = 'auto';
      pre.style.fontFamily = 'var(--font-mono)';
      pre.style.fontSize = '13px';
      fetch(data.raw_url).then(function (r) { return r.text(); }).then(function (text) {
        pre.textContent = text;
      });
      container.appendChild(pre);
    } else {
      const link = document.createElement('a');
      link.href = data.raw_url;
      link.textContent = 'Download';
      link.setAttribute('target', '_blank');
      container.appendChild(link);
    }
    readerBody.appendChild(container);

    const subline = {
      primary: absoluteTime(data.mtime),
      aux: `${humanBytes(data.size)} · ${data.relpath}`,
    };
    window.MineruShell.setReaderHeader(`📄 ${readerTitle}`, subline, []);
    try { scrollAnchor.focus({ preventScroll: true }); } catch (_) { /* older */ }
  }

  function openPath(source, relpath, options) {
    if (!source) return;
    const fromHistory = !!(options && options.fromHistory);
    state.source = source;
    state.relpath = relpath || '';
    if (!fromHistory && window.MineruRouter) {
      window.MineruRouter.navigate({ view: 'library', source: source, relpath: relpath || '' });
    }
    if (window.MineruShell && window.MineruShell.switchTab) {
      window.MineruShell.switchTab('library', { fromHistory: true, skipEnter: true });
    }
    enter();
  }

  window.MineruLibrary = { enter, openPath };
})();
