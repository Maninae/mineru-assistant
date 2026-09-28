/*
 * Pulse tab under the mail-client shell.
 *
 * Pulse renders as one wide surface in the reader column: attention banner
 * → daemon card → jobs (grouped by cadence) → heartbeat → notifications →
 * theme picker. The list column shows a slim "Pulse" title with a refresh
 * button; the sidebar stays as-is.
 */
(function () {
  'use strict';

  const { apiGet, relativeTime, absoluteTime, escapeHtml, buildErrorCard } = window.MineruApi;

  const STATUS_LABELS = {
    fresh: 'fresh',
    stale: 'stale',
    failed: 'failed',
    scheduled: 'scheduled',
    'not-scheduled': 'daemon',
    unknown: 'unknown',
  };

  const CADENCE_ORDER = ['daily', 'weekly', 'monthly', 'interval', 'always_on', 'unknown'];
  const CADENCE_LABELS = {
    daily: 'Daily',
    weekly: 'Weekly',
    monthly: 'Monthly',
    interval: 'Interval',
    always_on: 'Always-on',
    unknown: 'Other',
  };

  let refreshButton = null;
  let loading = false;

  async function enter() {
    window.MineruShell.setListHeader('⚙️ Pulse');
    // List column just carries a short "Recurring jobs & daemon" note; the
    // Pulse content itself lives in the reader column so we get the full
    // width for cards.
    const listBody = document.getElementById('list-body');
    if (listBody) {
      listBody.innerHTML = `
        <div class="today-section">
          <div class="today-title">Overview</div>
          <div style="padding:8px 16px 16px; color:var(--text-secondary); font-size:13px; line-height:1.5;">
            Recurring jobs, daemon liveness, heartbeat, and theme.
          </div>
        </div>
      `;
    }
    const actions = document.getElementById('list-header-actions');
    if (actions) {
      actions.innerHTML = '';
      refreshButton = document.createElement('button');
      refreshButton.type = 'button';
      refreshButton.className = 'icon-button';
      refreshButton.textContent = '↻ Refresh';
      refreshButton.addEventListener('click', load);
      actions.appendChild(refreshButton);
    }
    // Reader header for Pulse
    window.MineruShell.setReaderHeader('Pulse', 'Recurring jobs & daemon', []);
    window.MineruShell.setStackLevel(window.MineruShell.isMobile() ? 2 : null);
    await load();
  }

  async function load() {
    if (loading) return;
    loading = true;
    if (refreshButton) refreshButton.disabled = true;
    const target = document.getElementById('reader-body');
    target.innerHTML = '<div class="list-loading">Reading launchd…</div>';
    try {
      const data = await apiGet('/api/pulse');
      render(data);
      // Refresh coordinator heartbeat off the same fetch.
      window.MineruShell.updateCoordinatorHeartbeat(data);
    } catch (loadError) {
      target.innerHTML = '';
      target.appendChild(buildErrorCard(
        `Pulse unavailable. ${loadError.message || ''}`.trim(),
        [{ label: 'Retry', onClick: function () { load(); } }],
      ));
    } finally {
      loading = false;
      if (refreshButton) refreshButton.disabled = false;
    }
  }

  function render(data) {
    const target = document.getElementById('reader-body');
    target.innerHTML = '';
    const canvas = document.createElement('div');
    canvas.className = 'pulse-canvas';

    canvas.appendChild(renderAttentionBanner(data.jobs || []));
    canvas.appendChild(renderDaemonCard(data.daemon));

    const jobs = data.jobs || [];
    if (!jobs.length) {
      const empty = document.createElement('div');
      empty.className = 'list-empty';
      empty.textContent = 'No launchd agents found.';
      canvas.appendChild(empty);
    } else {
      renderJobsGroupedByCadence(canvas, jobs);
    }

    if (data.heartbeat) {
      const hbTitle = document.createElement('div');
      hbTitle.className = 'section-title';
      hbTitle.textContent = 'Heartbeat';
      canvas.appendChild(hbTitle);
      canvas.appendChild(renderHeartbeat(data.heartbeat));
    }

    if (window.MineruPush) {
      const notifTitle = document.createElement('div');
      notifTitle.className = 'section-title';
      notifTitle.textContent = 'Notifications';
      canvas.appendChild(notifTitle);
      const pushContainer = document.createElement('div');
      pushContainer.className = 'push-container';
      canvas.appendChild(pushContainer);
      window.MineruPush.render(pushContainer);
    }

    const themeTitle = document.createElement('div');
    themeTitle.className = 'section-title';
    themeTitle.textContent = 'Theme';
    canvas.appendChild(themeTitle);
    const picker = document.createElement('div');
    picker.className = 'theme-picker';
    canvas.appendChild(picker);
    window.MineruTheme.renderPicker(picker);

    target.appendChild(canvas);
  }

  function renderAttentionBanner(jobs) {
    const failed = jobs.filter(function (j) { return j.status === 'failed'; });
    const stale = jobs.filter(function (j) { return j.status === 'stale'; });
    if (!failed.length && !stale.length) {
      const nominal = document.createElement('div');
      nominal.className = 'pulse-banner pulse-banner-nominal';
      nominal.setAttribute('role', 'status');
      nominal.innerHTML = `
        <span class="pulse-banner-mark" aria-hidden="true">✓</span>
        <span class="pulse-banner-body">
          <span class="pulse-banner-title">All systems nominal</span>
          <span class="pulse-banner-sub">Every scheduled job is running on time.</span>
        </span>
      `;
      return nominal;
    }
    const banner = document.createElement('div');
    const flavor = failed.length ? 'failed' : 'stale';
    banner.className = `pulse-banner pulse-banner-${flavor}`;
    banner.setAttribute('role', 'alert');
    const items = failed.concat(stale);
    const jobCount = items.length;
    const titleWord = jobCount === 1 ? 'job needs' : 'jobs need';
    const title = `${jobCount} ${titleWord} attention`;
    const rows = items.map(function (job) { return renderAttentionRow(job); });
    banner.innerHTML = `
      <span class="pulse-banner-mark" aria-hidden="true">${failed.length ? '⚠' : '⏳'}</span>
      <span class="pulse-banner-body">
        <span class="pulse-banner-title">${escapeHtml(title)}</span>
        <span class="pulse-banner-list">${rows.join('')}</span>
      </span>
    `;
    return banner;
  }

  function renderAttentionRow(job) {
    // Prefer the backend's humanized display_name; the stripped kebab-case
    // basename is the fallback for jobs the backend hasn't mapped yet.
    const shortLabel = job.display_name || job.short_label || '';
    const when = job.last_output_ts || job.last_log_ts;
    const whenText = when ? `last ran ${relativeTime(when)}` : 'no recent run';
    const stateLabel = STATUS_LABELS[job.status] || job.status || '';
    return `<span class="pulse-banner-row"><b>${escapeHtml(shortLabel)}</b> · ${escapeHtml(stateLabel)} · ${escapeHtml(whenText)}</span>`;
  }

  function renderJobsGroupedByCadence(target, jobs) {
    const buckets = {};
    jobs.forEach(function (job) {
      const cadence = CADENCE_ORDER.indexOf(job.cadence) >= 0 ? job.cadence : 'unknown';
      if (!buckets[cadence]) buckets[cadence] = [];
      buckets[cadence].push(job);
    });
    CADENCE_ORDER.forEach(function (cadence) {
      const bucket = buckets[cadence];
      if (!bucket || !bucket.length) return;
      const title = document.createElement('div');
      title.className = 'section-title';
      title.textContent = CADENCE_LABELS[cadence];
      target.appendChild(title);
      const grid = document.createElement('div');
      grid.className = 'pulse-grid';
      bucket.forEach(function (job) { grid.appendChild(renderJobCard(job)); });
      target.appendChild(grid);
    });
  }

  const HEARTBEAT_OFF_DAYS = 30;
  const HEARTBEAT_STALE_DAYS = 2;

  function classifyHeartbeatAge(ageDays, backendStatus) {
    if (backendStatus === 'off' || backendStatus === 'stale' || backendStatus === 'fresh') {
      return backendStatus;
    }
    if (ageDays > HEARTBEAT_OFF_DAYS) return 'off';
    if (ageDays > HEARTBEAT_STALE_DAYS) return 'stale';
    return 'fresh';
  }

  function renderHeartbeat(heartbeat) {
    const wrap = document.createElement('div');
    wrap.className = 'heartbeat-list';
    const rows = flattenHeartbeat(heartbeat);
    if (!rows.length) {
      wrap.textContent = '(no signals recorded)';
      return wrap;
    }
    rows.forEach(function (row) {
      const line = document.createElement('div');
      line.className = 'heartbeat-row';
      const key = document.createElement('span');
      key.className = 'heartbeat-key';
      key.textContent = row.key;
      const value = document.createElement('span');
      value.className = 'heartbeat-value';
      if (row.epoch != null) {
        const ageDays = (Date.now() / 1000 - row.epoch) / 86400;
        const status = classifyHeartbeatAge(ageDays, row.status);
        if (status === 'off') {
          value.textContent = `off since ${absoluteTime(row.epoch)}`;
          line.classList.add('heartbeat-off');
        } else if (status === 'stale') {
          const ageText = `${Math.round(ageDays)} days ago`;
          value.textContent = `${ageText}  ·  ${absoluteTime(row.epoch)}`;
          line.classList.add('heartbeat-stale');
        } else {
          value.textContent = `${relativeTime(row.epoch)}  ·  ${absoluteTime(row.epoch)}`;
        }
      } else {
        value.textContent = row.raw;
      }
      line.appendChild(key);
      line.appendChild(value);
      wrap.appendChild(line);
    });
    return wrap;
  }

  function isHeartbeatLeaf(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
    return 'ts' in value || 'last_seen' in value;
  }
  function flattenHeartbeat(heartbeat) {
    const rows = [];
    Object.keys(heartbeat || {}).forEach(function (key) {
      const value = heartbeat[key];
      if (value && typeof value === 'object' && !Array.isArray(value) && !isHeartbeatLeaf(value)) {
        Object.keys(value).forEach(function (subKey) {
          rows.push(coerceRow(subKey, value[subKey]));
        });
      } else {
        rows.push(coerceRow(key, value));
      }
    });
    return rows;
  }

  function coerceRow(key, value) {
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      const ts = value.ts != null ? value.ts : value.last_seen;
      if (typeof ts === 'number' && Number.isFinite(ts)) {
        const epochSeconds = ts > 1e12 ? ts / 1000 : ts;
        if (epochSeconds >= 1e9 && epochSeconds < 1e11) {
          return { key: key, epoch: epochSeconds, status: value.status };
        }
      }
      return { key: key, raw: JSON.stringify(value) };
    }
    if (typeof value === 'number' && Number.isFinite(value)) {
      let epochSeconds = value;
      if (value > 1e12) epochSeconds = value / 1000;
      if (epochSeconds >= 1e9 && epochSeconds < 1e11) {
        return { key: key, epoch: epochSeconds };
      }
      return { key: key, raw: String(value) };
    }
    return { key: key, raw: String(value == null ? '' : value) };
  }

  function renderDaemonCard(daemon) {
    const wrap = document.createElement('div');
    wrap.className = 'daemon-card';
    const alive = !!(daemon && daemon.pid_alive);
    const dot = alive ? 'fresh' : 'failed';
    const labelText = daemon && daemon.label ? daemon.label : '';
    wrap.innerHTML = `
      <span class="daemon-emoji" aria-hidden="true">🦊</span>
      <div>
        <div class="daemon-title">Telegram daemon <span class="pulse-status"><span class="status-dot ${dot}"></span>${alive ? 'alive' : 'not running'}</span></div>
        <div class="daemon-meta">${escapeHtml(labelText)}</div>
      </div>
    `;
    return wrap;
  }

  function renderJobCard(job) {
    const card = document.createElement('div');
    card.className = 'pulse-card';
    if (job.status === 'failed') card.classList.add('pulse-card-failed');
    if (job.status === 'stale') card.classList.add('pulse-card-stale');
    const statusClass = (job.status === 'not-scheduled' || job.status === 'scheduled')
      ? 'neutral' : job.status;
    // Backend supplies display_name (humanized); the kebab-case basename is
    // the fallback path so an unmapped job still renders.
    const shortLabel = job.display_name || job.short_label || '';
    const scheduleText = job.schedule_human || '';
    const statusLabel = STATUS_LABELS[job.status] || job.status || '';
    const lastText = job.last_output_ts
      ? `Last output: ${relativeTime(job.last_output_ts)}`
      : (job.last_log_ts ? `Last log: ${relativeTime(job.last_log_ts)}` : 'No recent activity');
    const absText = job.last_output_ts || job.last_log_ts
      ? absoluteTime(job.last_output_ts || job.last_log_ts)
      : '';
    card.innerHTML = `
      <div class="pulse-label">${escapeHtml(shortLabel)}</div>
      <div class="pulse-schedule">${escapeHtml(scheduleText)}</div>
      <span class="pulse-status"><span class="status-dot ${statusClass}"></span>${escapeHtml(statusLabel)}</span>
      <div class="pulse-timestamps">
        <span>${escapeHtml(lastText)}</span>
        ${absText ? `<span>${escapeHtml(absText)}</span>` : ''}
      </div>
    `;
    return card;
  }

  window.MineruPulse = { enter };
})();
