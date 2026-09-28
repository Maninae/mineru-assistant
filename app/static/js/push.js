/*
 * Web Push enable / disable UI.
 *
 * Renders an "Enable notifications" control that:
 *   1. Detects support (Notification, PushManager, service worker) and shows
 *      a hint when they're missing. iOS specifically only exposes these to
 *      the PWA when the operator has "Added to Home Screen" and opened it FROM the
 *      home-screen icon (16.4+). A plain Safari tab shows the hint.
 *   2. Fetches the VAPID public key from /api/push/vapid-key, converts it to
 *      a Uint8Array for PushManager.subscribe, and stores the resulting
 *      subscription server-side via POST /api/push/subscribe.
 *   3. Reflects the current state (enabled / disabled / blocked / unsupported)
 *      and offers a "Disable" toggle that unsubscribes both browser-side and
 *      server-side.
 *
 * Errors bucket into short human sentences; nothing about the VAPID key or
 * the subscription secrets ever hits the console or the DOM.
 */
(function () {
  'use strict';

  const { apiGet, escapeHtml } = window.MineruApi;

  // "application/json" origin-match is set here so the server's write-guard
  // (Content-Type check + Origin allowlist) accepts these POSTs.
  const JSON_HEADERS = { 'Content-Type': 'application/json' };

  async function postJson(path, payload) {
    const response = await fetch(path, {
      method: 'POST',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: JSON_HEADERS,
      body: JSON.stringify(payload),
    });
    // Bucket by status so callers can react ("already unsubscribed" vs "server broke").
    if (!response.ok) {
      const err = new Error('server rejected the request');
      err.status = response.status;
      throw err;
    }
    return response.json();
  }

  // Base64url → Uint8Array. PushManager wants raw bytes for the applicationServerKey.
  function base64UrlToUint8Array(base64UrlString) {
    const padding = '='.repeat((4 - (base64UrlString.length % 4)) % 4);
    const base64 = (base64UrlString + padding).replace(/-/g, '+').replace(/_/g, '/');
    const raw = atob(base64);
    const output = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i += 1) output[i] = raw.charCodeAt(i);
    return output;
  }

  // ---- Support / state detection -------------------------------------------

  function browserSupportsPush() {
    return typeof window !== 'undefined'
      && 'serviceWorker' in navigator
      && 'PushManager' in window
      && 'Notification' in window;
  }

  async function currentSubscription() {
    if (!browserSupportsPush()) return null;
    const registration = await navigator.serviceWorker.ready;
    return registration.pushManager.getSubscription();
  }

  // ---- Public: render the control into a container ------------------------

  async function render(container) {
    container.innerHTML = '';
    const card = document.createElement('div');
    card.className = 'push-card';
    container.appendChild(card);

    if (!browserSupportsPush()) {
      card.innerHTML = `
        <div class="push-title">Push notifications</div>
        <div class="push-hint">
          Not available in this browser. On iOS, open the app from the
          <b>home screen</b> after "Add to Home Screen" (iOS 16.4+).
        </div>
      `;
      return;
    }

    if (Notification.permission === 'denied') {
      // iOS-specific escape ladder: the "blocked in browser settings" state
      // has two very different root causes on iOS (needs PWA install /
      // needs Settings → Notifications toggle) and neither is discoverable
      // from the browser. Spell out both paths.
      card.innerHTML = `
        <div class="push-title">Push notifications</div>
        <div class="push-hint">Blocked in browser settings.</div>
        <ul class="push-hint-list">
          <li>On iOS: install to Home Screen and open the app from that icon (iOS 16.4+).</li>
          <li>To re-enable: iOS Settings → Notifications → Mineru → Allow Notifications.</li>
        </ul>
      `;
      return;
    }

    let existing = null;
    try {
      existing = await currentSubscription();
    } catch (getError) {
      // getSubscription can throw on quirky iOS builds; treat as "unknown".
      existing = null;
    }

    if (existing) {
      renderEnabled(card, existing);
      return;
    }
    renderDisabled(card);
  }

  function renderEnabled(card, subscription) {
    card.innerHTML = `
      <div class="push-title">Push notifications <span class="push-badge push-badge-on">on</span></div>
      <div class="push-hint">This device gets a lock-screen ping when a new brief lands.</div>
      <div class="push-actions"></div>
    `;
    const actions = card.querySelector('.push-actions');
    const disableButton = document.createElement('button');
    disableButton.type = 'button';
    disableButton.className = 'icon-button';
    disableButton.textContent = 'Turn off';
    disableButton.addEventListener('click', function () {
      disableButton.disabled = true;
      disableForDevice(subscription).then(function () {
        render(card.parentElement);
      }).catch(function () {
        disableButton.disabled = false;
        card.querySelector('.push-hint').textContent = 'Could not turn off — try again shortly.';
      });
    });
    actions.appendChild(disableButton);
  }

  function renderDisabled(card) {
    card.innerHTML = `
      <div class="push-title">Push notifications <span class="push-badge push-badge-off">off</span></div>
      <div class="push-hint">Get a lock-screen ping when a new brief lands.</div>
      <div class="push-actions"></div>
      <div class="push-message" role="status"></div>
    `;
    const actions = card.querySelector('.push-actions');
    const message = card.querySelector('.push-message');
    const enableButton = document.createElement('button');
    enableButton.type = 'button';
    enableButton.className = 'icon-button';
    enableButton.textContent = 'Enable notifications';
    enableButton.addEventListener('click', function () {
      enableButton.disabled = true;
      message.textContent = 'Requesting permission…';
      enableForDevice().then(function () {
        message.textContent = '';
        render(card.parentElement);
      }).catch(function (enableError) {
        enableButton.disabled = false;
        message.textContent = enableError.userMessage || 'Could not enable notifications.';
      });
    });
    actions.appendChild(enableButton);
  }

  // ---- Enable / disable flows ----------------------------------------------

  async function enableForDevice() {
    // Step 1: permission. On Chrome/Firefox this can prompt synchronously;
    // on Safari it MUST originate from a user gesture (the button click).
    const permission = await Notification.requestPermission();
    if (permission !== 'granted') {
      const err = new Error('permission denied');
      err.userMessage = permission === 'denied'
        ? 'Blocked in browser settings.'
        : 'Permission not granted.';
      throw err;
    }

    // Step 2: get our VAPID public key from the server (behind gate + Host allowlist).
    let vapidResponse;
    try {
      vapidResponse = await apiGet('/api/push/vapid-key');
    } catch (keyError) {
      const err = new Error('vapid key fetch failed');
      err.userMessage = keyError.status === 503
        ? 'Server key not provisioned yet.'
        : 'Could not reach server.';
      throw err;
    }
    if (!vapidResponse || typeof vapidResponse.key !== 'string' || !vapidResponse.key) {
      const err = new Error('vapid key malformed');
      err.userMessage = 'Server returned no key.';
      throw err;
    }

    // Step 3: subscribe via the browser's push manager.
    const registration = await navigator.serviceWorker.ready;
    let subscription;
    try {
      subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: base64UrlToUint8Array(vapidResponse.key),
      });
    } catch (subscribeError) {
      const err = new Error('pushManager.subscribe failed');
      err.userMessage = 'Browser refused to subscribe.';
      throw err;
    }

    // Step 4: send the subscription JSON to the server.
    const json = subscription.toJSON();
    try {
      await postJson('/api/push/subscribe', json);
    } catch (persistError) {
      // Best-effort rollback so the browser doesn't stay half-subscribed.
      try { await subscription.unsubscribe(); } catch (_ignore) { /* no-op */ }
      const err = new Error('subscribe endpoint rejected');
      err.userMessage = 'Server rejected the subscription.';
      throw err;
    }
  }

  async function disableForDevice(subscription) {
    // Server-side prune first so a browser-side error still leaves us clean
    // on the server. The unsubscribe endpoint is idempotent, so a subsequent
    // call after a partial success just no-ops.
    try {
      await postJson('/api/push/unsubscribe', { endpoint: subscription.endpoint });
    } catch (persistError) {
      // Non-fatal: continue to browser-side unsubscribe. push_send.py will
      // eventually prune this endpoint via the 410 pathway anyway.
    }
    try {
      await subscription.unsubscribe();
    } catch (_ignore) {
      /* getSubscription() next time will re-report the state, no need to alarm the user. */
    }
  }

  window.MineruPush = { render };
})();
