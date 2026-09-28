/*
 * Service worker: app shell cache + Web Push receiver.
 *
 * Two responsibilities:
 *
 *   1. Cache the shell (HTML, CSS, JS, vendored marked + purify, favicon) so
 *      the UI comes up fast on repeat visits and, more importantly, so an
 *      OFFLINE first-load still has the DOMPurify sanitizer available —
 *      without it the markdown pipeline would render unsanitized HTML.
 *      Never caches /api/* — those are freshness-sensitive.
 *
 *   2. Receive Web Push notifications sent by scripts/push_send.py, show a
 *      lock-screen notification, and — on tap — focus an existing app window
 *      or open one at the deep-link hash carried in the payload.
 *
 * The SHELL_CACHE version suffix is the cache-bust key: bump it whenever
 * SHELL_ASSETS changes so old clients drop the stale cache on activate.
 * v5: design pass — new type-scale tokens, surface roles, slim landing
 * header, reader compact meta, cadence-grouped pulse, error-card actions.
 */

const SHELL_CACHE = 'mineru-shell-v8';
const SHELL_ASSETS = [
  '/',
  '/static/css/tokens.css',
  '/static/css/layout.css',
  '/static/css/components.css',
  '/static/js/api.js',
  '/static/js/theme.js',
  '/static/js/search.js',
  '/static/js/inbox.js',
  '/static/js/library.js',
  '/static/js/pulse.js',
  '/static/js/push.js',
  '/static/js/chat.js',
  '/static/js/app.js',
  '/static/vendor/marked.min.js',
  '/static/vendor/purify.min.js',
  '/static/icons/favicon.svg',
  '/manifest.webmanifest',
];

// ---- Lifecycle ---------------------------------------------------------------

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL_ASSETS)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== SHELL_CACHE).map((k) => caches.delete(k))))
  );
  self.clients.claim();
});

// ---- Fetch: API network-first, shell cache-first ----------------------------

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin) return;

  // Network-first for the API + sandboxed HTML: stale data would be worse
  // than a spinner. Fall back to cache only if network is truly gone.
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/raw/') || url.pathname.startsWith('/sandbox/')) {
    event.respondWith(fetch(event.request).catch(() => caches.match(event.request)));
    return;
  }

  // Shell: cache-first with a background refresh.
  event.respondWith(
    caches.match(event.request).then((cached) => {
      const network = fetch(event.request).then((response) => {
        if (response && response.ok) {
          const clone = response.clone();
          caches.open(SHELL_CACHE).then((cache) => cache.put(event.request, clone)).catch(() => {});
        }
        return response;
      }).catch(() => cached);
      return cached || network;
    })
  );
});

// ---- Web Push: showNotification + notificationclick -------------------------

// Payload shape (from scripts/push_send.py):
//   {title, body, url?, tag?}
//
// Every field is defensively coerced to a string before it reaches the OS
// notification surface — a malformed payload should show SOMETHING (even
// "New brief") rather than throw and drop the push silently.
const FALLBACK_TITLE = 'Mineru';
const FALLBACK_BODY = 'New brief.';
const NOTIFICATION_ICON = '/static/icons/apple-touch-icon-180.png';
const NOTIFICATION_BADGE = '/static/icons/favicon.svg';

function parsePushPayload(event) {
  if (!event.data) return {};
  try {
    return event.data.json() || {};
  } catch (_) {
    // Non-JSON payloads shouldn't happen from our sender but keep the SW
    // from swallowing an event silently — show as body text.
    try { return { body: event.data.text() }; } catch (__) { return {}; }
  }
}

function coerceStringField(value, fallback) {
  if (typeof value !== 'string' || !value) return fallback;
  return value;
}

self.addEventListener('push', (event) => {
  const payload = parsePushPayload(event);
  const title = coerceStringField(payload.title, FALLBACK_TITLE);
  const body = coerceStringField(payload.body, FALLBACK_BODY);
  const url = typeof payload.url === 'string' ? payload.url : '';
  const tag = typeof payload.tag === 'string' ? payload.tag : undefined;

  event.waitUntil(self.registration.showNotification(title, {
    body: body,
    icon: NOTIFICATION_ICON,
    badge: NOTIFICATION_BADGE,
    tag: tag,
    // Store the deep-link so `notificationclick` can find it without re-parsing.
    data: { url: url },
  }));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();

  // Prefer focusing an already-open window over spawning a new one.
  // Deep-link hash (e.g. '#brief/morning/2026-08-20.md') is applied via
  // `focus + navigate`; a plain '/' opens the app landing.
  const deepLinkHash = (event.notification.data && event.notification.data.url) || '';
  const targetPath = '/' + (deepLinkHash.startsWith('#') ? deepLinkHash : '');

  event.waitUntil((async () => {
    const allClients = await self.clients.matchAll({
      type: 'window',
      includeUncontrolled: true,
    });
    for (const client of allClients) {
      // Same origin (we already filter by matchAll returning our client set).
      // If ANY window is open, focus it and route to the deep link.
      try {
        await client.focus();
      } catch (_ignore) { /* focus can throw on backgrounded tabs; keep going */ }
      if ('navigate' in client && deepLinkHash) {
        try { await client.navigate(targetPath); return; } catch (_ignore) { /* fall through */ }
      }
      return;
    }
    // No open window → open a fresh one at the deep link.
    await self.clients.openWindow(targetPath);
  })());
});
