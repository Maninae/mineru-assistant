# Mineru web app — onboarding

Read-only, tailnet-only web UI over the Mineru workspace. A flat, light **three-pane mail client**: on desktop, three persistent columns (folder sidebar | message list | reader); on mobile, a drill-down stack (folders → list → reader). Default theme is `mail` (the legacy `forest`/`ocean`/`warm`/`dark` themes still work — they retint the same shell). Coexists with Telegram. Binds `127.0.0.1:$MINERU_WEBAPP_PORT` (default 5195); tailnet exposure is exclusively via `tailscale serve` (never funnel).

This file is the onboarding doc for the web app; the module map and security contracts below are authoritative.

## Hard rules

- READ-ONLY over the workspace. The single writable surface is the state dir (`<state>` below = `$MINERU_APP_STATE_DIR`, default `$MINERU_HOME/app-state`). Any other write path is a bug. Never write under `app/`: in a deployed install it is a symlink into the engine checkout.
- **Python 3.9-compatible**. The launchd target is `/usr/bin/python3` (3.9.6). No `match`, no PEP 604 `X | Y` unions in signatures, no `list[int]` at module scope in annotations that get evaluated at import time. Use `typing.List` / `typing.Optional` / `typing.Dict`.
- **Stdlib only.** No pip installs. `marked.min.js` is vendored under `static/vendor/` (offline build-time fetch; the served page never reaches the internet).
- **Never `rm`.** Use `/usr/bin/trash` if you must delete something you created under `app/`.
- Bind `127.0.0.1` ONLY. Never `0.0.0.0`. Tailnet exposure happens outside this process.

## Module map

Coordinator + modules (each file is one nameable responsibility, kept small on purpose):

| File | Responsibility |
|---|---|
| `server.py` | HTTP dispatcher + entry point. Owns the route table and the `MineruRequestHandler` that walks it. Does NOT contain security enforcement or per-route logic; those live in the next two files. |
| `handlers.py` | Every route handler (`handle_feeds_index`, `handle_feed_page`, `handle_brief_detail`, `handle_library`, `handle_raw_library`, `handle_sandbox_library`, `handle_pulse`, `handle_seen_post`, plus static/manifest/sw handlers). Each returns `(status, headers, body_bytes)`. |
| `http_helpers.py` | Security choke point. Owns `is_path_inside_allowlist` (allowlist enforcement), `safe_serve_file` (extension allowlist + HTML sandbox CSP), `apply_security_headers`, `BASE_SECURITY_HEADERS` (CSP / nosniff / no-referrer / X-Frame-Options: DENY on every response), `json_response`, `error_response`, `bytes_response`, and `MAX_JSON_BODY_BYTES`. |
| `config.py` | Env-driven bind config + Host/Origin allowlists, state dir, `FEED_REGISTRY` (loaded from `feeds.json`, see Configuration), `LIBRARY_SOURCES`, allowed extensions, MIME map, resolved `READ_ALLOWLIST`. |
| `launchd_jobs.py` | Loads the Pulse job registry (`launchd-jobs.json`): per-label display names + freshness signal specs. |
| `feeds.py` | Feed directory walking, TLDR/title extraction, `preview` snippet extraction, mtime-cursor pagination, brief path resolution. |
| `library.py` | Reports + creations tree listing and safe path resolution. |
| `pulse.py` | Composes the `/api/pulse` snapshot (jobs + daemon liveness + heartbeat). Coordinator only; OS reads and math live below. |
| `search.py` | Backs `/api/search`: substring (never regex) scan of every brief and (optionally) reports/creations, tier-ranked (title > tldr > filename > body), bounded by `SEARCH_BODY_MAX_BYTES` per file and `SEARCH_MAX_FILES_SCANNED` per request. Same short-TTL caching rhythm as the sidebar. |
| `pulse_launchd.py` | launchd plist discovery, parsing, log-mtime lookup, and the human-schedule string builder. |
| `pulse_freshness.py` | `JOB_SIGNAL_TABLE` (loaded from the job registry via `launchd_jobs.py`; per-label real artifact source: `newest_in_dir` / `file_mtime` / `neutral`), cadence math, and the `fresh / stale / failed / scheduled / not-scheduled` classifier. Reads the job's REAL output location (e.g. `memory/daily/` for daily-consolidation, the state file for keepsake-autodeploy), not the stdout/stderr log — log rotation would otherwise freeze the mtime and mark healthy jobs as failed. Jobs that legitimately produce no visible artifact (`daemon-watchdog`, `cleanup-retention`) are marked neutral → `scheduled` status. |
| `seen_ledger.py` | App-private read/seen state at `<state>/seen.json`; atomic writes, FIFO cap at `SEEN_LIST_MAX`, chmod 600. |
| `unlock_gate.py` | Passphrase gate (`POST /api/unlock`, `GET /lock`, cookie validation, lockout tracker). The app's ONE Keychain access lives here — default service `mineru-webapp-passphrase-hash` / account `mineru`, overridable via `PASSPHRASE_KEYCHAIN_{SERVICE,ACCOUNT}`. See "Security contracts" #12-16. |
| `push_subscriptions.py` | Web Push subscription store at `<state>/push-subscriptions.json` (0600, atomic-write, endpoint-dedup, FIFO cap `MAX_SUBSCRIPTIONS`). Validates the browser PushSubscription shape (endpoint = https:// or loopback; p256dh/auth base64url in length windows). Never logs endpoints or keys. |
| `push_endpoints.py` | Three handlers: `GET /api/push/vapid-key`, `POST /api/push/subscribe`, `POST /api/push/unsubscribe`. Pure HTTP layer — subscribe validates via `push_subscriptions.is_valid_subscription_shape` then calls into the store. Both POSTs sit behind the dispatcher's Content-Type/Origin/OPTIONS write-guards. |
| `webpush_crypto.py` | Pure-function Web Push crypto: RFC 8291 aes128gcm encryption + RFC 8292 VAPID JWT signing/verification. No I/O, no globals. Imports `cryptography` (49.0.0). Correctness proof: `tests/test_webpush_rfc8291.py` reproduces the RFC 8291 §5 known-answer vector byte-for-byte. |
| `static/` | Frontend shell. `index.html`, `lock.html` (passphrase gate), `css/` (including `lock.css`), `js/` (including `lock.js`), `vendor/marked.min.js` + `vendor/purify.min.js`, `manifest.webmanifest`, `sw.js`, `icons/`. |
| `feeds.default.json` / `launchd-jobs.default.json` | Bundled generic registries, used when the operator's `$MINERU_HOME/config/` file is absent. |
| `deploy/set-passphrase.sh` | Interactive helper: prompts twice with hidden input, SHA-256s the passphrase, stores the hash in Keychain. Deleting the Keychain item + restarting reverts to option A. |
| `qa/` | Device-true screenshot harness. `shoot.py` drives Playwright + CDP `Emulation.setSafeAreaInsetsOverride` so `env(safe-area-inset-*)` resolves to real iPhone 17 Pro values in headless Chromium; `hardware_overlay.py` draws the Dynamic Island / corner masks / home indicator / status-bar mock on every shot. Own `CLAUDE.md`; usage + the CDP reproducer + the useyourloaf source for the device numbers live there. Use it whenever you touch chrome-adjacent CSS or `sw.js`. |

## Shell / DOM contract (frontend)

The old contract was a single `#pane-header` + `#pane-body` that every view swapped into. That's gone. `index.html`'s `.app-shell` now has three persistent regions:

- `aside.sidebar` (`#sidebar`) — folder list (`#feed-groups`, rows grouped by each feed's `group` field), a bottom-pinned nav to Library/Pulse/Chat (`.sidebar-footer-nav`), and a live "Coordinator" heartbeat (`#coordinator-heartbeat`, polls `/api/pulse` every 60s).
- `section.list-column` (`#list-column`) — its own header (`#list-header`: `#list-back`, `#list-title`, `#list-header-actions`) and its own body (`#list-body`). Renders the message list.
- `section.reader-column` (`#reader-column`) — its own header (`#reader-header`: `#reader-back`, `#reader-title`, `#reader-status-pill`, `#reader-subline`, `#reader-header-actions`) and its own body (`#reader-body`). Renders the opened brief.

Message rows are `.message-row` elements: `.row-glyph` (unread envelope / seen reply-arrow), `.row-title`, `.row-time`, `.row-subtitle` (feed category + optional "New" tag chip, itself split into `.row-category` and the tag), and `.row-snippet` (the 2-line preview).

`window.MineruShell` (built in `static/js/app.js`) is the coordinator API every tab module drives — two independent header/body pairs instead of one:
- `setListHeader(title, options)` / `addListHeaderAction(node)` — the list column's own header.
- `setReaderHeader(title, sub, actions)` / `clearReader()` / `makeReaderActionButton(label, ariaLabel, onClick)` — the reader column's header, its empty state, and a helper for the reader's action-button cluster (copy link, open JSON, refresh).
- `setStackLevel(0|1|2)` — mobile-only: which single column is visible (0 sidebar, 1 list, 2 reader). Desktop's CSS grid shows all three regardless of level.
- `renderFeedSidebar(feeds, activeFeedId)` / `setActiveFeed(feedId)` — sidebar folder rendering, grouped by each feed's `group`.
- `switchTab(tabId, options)` — routes between inbox / library / pulse / chat.
- `setPaneHeader(emoji, title, sub, backCallback)` is kept as a compat shim (routes to `setReaderHeader`, or to `setListHeader` + `clearReader` when no `backCallback` is passed) for any caller not yet migrated to the new pair.

Desktop shows all three columns at once. Mobile collapses to a drill-down stack — `.app-shell` gets a `.stack-level-{0,1,2}` class and only that one column is visible; the on-screen back buttons (`#list-back`, `#reader-back`) both ultimately resolve through `window.history.back()` so they agree with iOS swipe-back.

## Back-button model

Reader-back must land on the SAME feed list the brief was opened from, regardless of entry point (a row in that feed's own list, a Today card, a search result, a linkified path). `router.js`'s `seedFeedThenPushBrief(feedId, filename)` is the primitive: it rewrites the current history entry to `{view:'feed', feedId}` (or reuses it if already there) and pushes `{view:'brief', feedId, filename}` on top, so `history.back()` from the reader deterministically resolves to that feed's list no matter which surface opened the brief. `MineruInbox.openBrief` / `openBriefFromExternal` both route through it. On mobile, the on-screen back buttons are wired to `history.back()` (list-back is an exception — it just drops to stack level 0 without a history push, since the sidebar is a peer view, not a parent).

## API surface

All GET except one POST:

```
GET  /api/feeds                              feed registry + unread/latest for the sidebar; each feed object carries a `group` key (sidebar folder grouping, e.g. "Daily"/"System"; feeds without one default to "Feeds")
GET  /api/feed/<id>?before=<cursor>&limit=N  newest-first page of feed items (cursor is an opaque base64 (mtime,filename) token — echo it back, never parse it); each item carries an additive `preview` field (first real body sentence past the title/TLDR, used for the row's 2-line snippet)
GET  /api/brief/<feed_id>/<filename>         raw markdown + metadata for one brief
GET  /api/library/<source>[/<relpath>]       directory listing OR file metadata (reports, creations)
GET  /raw/library/<source>/<relpath>         raw bytes with correct MIME (never HTML)
GET  /sandbox/library/<source>/<relpath>     HTML from creations/, served with strict CSP for iframe
GET  /api/pulse                              jobs, daemon, heartbeat
GET  /api/today?hours=<N>                    cross-feed "what landed recently" list, newest-first, capped at TODAY_ITEMS_MAX (hours default TODAY_HOURS_DEFAULT, clamped to [1, 168]); response carries `truncated: true` when clipped; items carry the same additive `preview` field as /api/feed/<id>
GET  /api/search?q=<q>&scope=<briefs|all>&limit=<N>   literal-substring search (never regex; case-insensitive). Scope defaults to `briefs` (all `briefs_*` feeds); `all` also walks reports/ + creations/. Limit defaults to 40, clamped to [1, 100]. Empty/missing q or q longer than 200 chars → 400. Results ranked title > tldr > filename > body, newest-first within a tier. Response: `{query, scope, truncated, results: [{kind, feed_id?|source, filename?|relpath, title, snippet, mtime, matched_in}]}`
POST /api/seen                               body: {feed_id, filename} OR {source, relpath} OR {feed_id, all: true} (bulk-mark every brief in the feed)
POST /api/unlock                             body: {"passphrase": "..."} — passphrase gate. Rate-limited/lockout, mints session cookie on match. See "Security contracts" #12-16.
GET  /lock                                   lock screen HTML (served automatically for navigational GETs while locked; direct-requestable too)
GET  /api/push/vapid-key                     {"key": "<base64url uncompressed P-256 public>"}; 503 if `<state>/vapid-public.txt` is missing (run `engine/app-deploy/gen-vapid.py`).
POST /api/push/subscribe                     body: the browser PushSubscription JSON `{endpoint, keys:{p256dh, auth}}`. Dedup by endpoint. Same write-guards as `/api/seen`.
POST /api/push/unsubscribe                   body: {endpoint}. Idempotent (unknown endpoint → 200 removed=false).
```

Static: `GET /`, `GET /static/*`, `GET /manifest.webmanifest`, `GET /sw.js`.

## Security contracts

1. Server binds `127.0.0.1` only (see `config.HOST`).
2. Every filesystem path from a request is resolved and re-checked against `config.READ_ALLOWLIST` via `http_helpers.is_path_inside_allowlist`. Symlink escapes reject.
3. Extension allowlist for file serving is `config.ALLOWED_EXTENSIONS`. HTML never goes through `/raw/library/*`; it must be requested via `/sandbox/library/*` and the frontend wraps it in `<iframe sandbox>`.
4. **Every response carries these headers** (via `http_helpers.apply_security_headers`): a strict Content-Security-Policy (`default-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; font-src 'self' data:; frame-src 'self'; object-src 'none'; base-uri 'self'; form-action 'none'`), `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`. The sandbox-HTML route uses `SANDBOX_HTML_HEADERS` instead (same nosniff/no-referrer, sandbox-specific CSP, `X-Frame-Options: SAMEORIGIN`). No route can bypass these — they're re-imposed after per-route overrides.
9. **Host-header allowlist** (`server.py` dispatch, `config.build_allowed_hosts`): only loopback (`127.0.0.1:<port>`, `localhost:<port>`) plus the configured `TAILNET_HOSTNAME` / `SERVE_HOSTNAMES` are accepted; any other/forged/missing Host → `421`. This is the DNS-rebinding defense for the loopback origin (a malicious page can't rebind to 127.0.0.1 and read briefs same-origin).
10. **Write-guard on state-changers** (`server.py::enforce_write_guards`): `POST /api/seen` requires `Content-Type: application/json` (else `415`) and, if an `Origin` header is present, it must be in the allowlist (else `403`). `OPTIONS` always → `405` so a CORS preflight never succeeds. Together these kill the CSRF vector (a cross-origin `text/plain` "simple" POST can't slip through).
11. **Sandbox-HTML CSP is deliberately relaxed** to `script-src 'unsafe-inline'` (see `SANDBOX_HTML_CSP` in `http_helpers.py`) so author-trusted `creations/` HTML (e.g. the encrypted `anthology.html` reader) can run its own inline scripts. This is safe ONLY because the iframe is `sandbox="allow-scripts"` **without** `allow-same-origin` (null origin: no app cookies/localStorage/DOM/`/api` access). **NEVER add `allow-same-origin` to that iframe, never widen the sandbox CSP beyond `script-src 'unsafe-inline'`, and never apply it to the app shell** (the shell keeps `script-src 'self'`).

12. **Passphrase gate (option B) is enabled iff the Keychain has `mineru-webapp-passphrase-hash` (account `mineru`)**. Absent → gate OFF, byte-identical to today's open-on-tailnet behavior (option A). Presence is probed ONCE at startup by `unlock_gate.probe_gate_at_startup()`; that log line ("passphrase-gate: ENABLED" / "disabled (option A)") is authoritative for the process lifetime. Set/rotate with `deploy/set-passphrase.sh`; revert to option A by `security delete-generic-password -a mineru -s mineru-webapp-passphrase-hash` + restart.

13. **The Keychain surface is one function, one hardcoded service.** `unlock_gate.read_passphrase_hash_from_keychain()` is the app's ENTIRE `security` shell-out. Service and account are literal constants (`PASSPHRASE_KEYCHAIN_SERVICE` / `PASSPHRASE_KEYCHAIN_ACCOUNT`); the subprocess never receives user input. Do NOT add other Keychain reads — if a future need appears, extend this one function auditably, or the audit story ("what secrets can the webapp reach?") stops being one-line.

14. **Gate enforcement lives in the dispatcher (`server.py::dispatch`) BEFORE route matching.** When enabled, only the exempt set in `EXEMPT_WHEN_LOCKED_{POST,GET}_PATHS` (POST `/api/unlock`, GET `/lock`, GET `/static/css/{lock,tokens}.css`, GET `/static/js/lock.js`, GET `/static/icons/{favicon.svg,apple-touch-icon-180.png}`) reaches the ROUTES table without a valid cookie. Gated API → `401 {"error":"locked"}`; gated navigational GET (Accept: text/html) → the lock screen HTML (200). NEVER add a route to the exempt set that could render app data. **When the gate is OFF (option A), `handle_unlock` and `handle_lock_screen` themselves return `404` — option A never advertises the gate and never touches `<state>/unlock-*.json`.**

15. **`POST /api/unlock` runs the full hash pipeline on EVERY request** (timing-oracle fix). Order: (1) `is_gate_enabled()` → `404` when off. (2) Persistent lockout check — no compute for locked-out callers, `429` + `Retry-After`. (3) `parse_unlock_body(body)` returns `(passphrase, was_valid_shape)`; every bad-shape path (`413`-shaped oversized body, bad JSON, wrong-type payload, missing/empty/non-string/oversized `passphrase` field) collapses to `DUMMY_PASSPHRASE_FOR_TIMING_UNIFORMITY` and `was_valid_shape=False`. (4) Fresh Keychain read; any error becomes `expected_hash=None`, which then compares against `DUMMY_HASH_FOR_TIMING_UNIFORMITY`. (5) `hmac.compare_digest` ALWAYS runs (same-length hex, no length oracle). (6) Success requires ALL of `was_valid_shape and expected_hash is not None and compare_matched`; anything else → one recorded failure + uniform `401 {"error":"incorrect"}`. This closes the ~35x wall-clock gap the auditor called out between "reached hash compare" and "rejected at parse". On success: `secrets.token_urlsafe(32)`, atomic 0600 write to `<state>/unlock-tokens.json` (prune-expired-on-load), `Set-Cookie: mineru_unlock=... HttpOnly; SameSite=Strict; Path=/; Max-Age=604800` plus a **conditional `Secure`** attribute (see next contract), reset lockout. Dispatcher write-guards (Content-Type=application/json, Origin allowlist, OPTIONS→405, Host allowlist) still run before this handler because it declares `wants_body=True`.

15a. **`Set-Cookie: Secure` is conditional on request Host** (`unlock_gate.cookie_secure_for_host`). Loopback Hosts (`127.0.0.1`, `localhost`, `[::1]`, optionally `:port`) → **Secure OMITTED**; anything else (in practice the configured `TAILNET_HOSTNAME`) → **Secure PRESENT**. Rationale: the app binds plain HTTP always; TLS is terminated by `tailscale serve` and proxied to `127.0.0.1:<port>`, so the request Host is the correct signal for "is the real transport secure." Browsers refuse to store Secure cookies over `http://`, so unconditionally setting Secure traps loopback dev/QA in an unlock loop (200 succeeds → cookie silently dropped → next request re-locked). Loopback traffic never leaves the machine, so non-Secure is safe there. The Host has already passed the Host-allowlist check upstream (contract #9), so `cookie_secure_for_host` only ever sees an allowlisted value; unknown/empty Hosts fail safe (Secure on). HttpOnly + SameSite=Strict + Path=/ are unconditional. Do NOT relax Secure for the tailnet Host, and do NOT extend the loopback set beyond the values above.

16. **Lockout policy** (OWASP-style exponential backoff): global (single passphrase, single user — NOT per-IP; tailnet IPs are spoofable and per-IP throttling is theatre here). First `LOCKOUT_FAILURES_BEFORE_BACKOFF=3` failures cost nothing (fat-finger grace); after that the window is `LOCKOUT_BASE_SECONDS=5` × 2^k, capped at `LOCKOUT_MAX_SECONDS=3600` (~1h). Persisted to `<state>/unlock-lockout.json` (0600) so a restart does not reset an active brute-force window. Locked-out response is `429` with `Retry-After` and a generic body.
5. **Brief markdown is sanitized client-side** with DOMPurify (vendored at `static/vendor/purify.min.js`) before any `.innerHTML = …`. See `MineruApi.renderMarkdownSafe` in `static/js/api.js`. CSP is the second line of defense; DOMPurify is the primary one because brief content is AI-generated from external sources (email, calendar, iMessage, news).
6. No Keychain, no secrets, no tokens. `cache/`, `landline.json`, `memory/` (except the heartbeat-state.json read) are not reachable through any route.
7. GET handlers are side-effect-free. The lone `POST /api/seen` only writes under `<state>/` and only after the identifier resolves to a real file inside the read allowlist. Ledger caps per-bucket at `SEEN_LIST_MAX = 5000` with FIFO eviction.
8. Logs never carry brief contents (only paths and counts).

## Configuration

Per-profile isolation: nothing instance-specific is hardcoded. Env vars (set by the rendered launchd plist):

| Env var | Default | Controls |
|---|---|---|
| `MINERU_HOME` | `~/.mineru` | Workspace root; every read path derives from it |
| `MINERU_APP_STATE_DIR` | `$MINERU_HOME/app-state` | Runtime state: `seen.json`, `unlock-tokens.json`, `unlock-lockout.json`, `push-subscriptions.json` (0600), `vapid-public.txt` |
| `MINERU_WEBAPP_PORT` | `5195` | Loopback bind port |
| `MINERU_TAILNET_HOSTNAME` | empty | Tailscale-service name added to the Host/Origin allowlists; empty = loopback only |
| `MINERU_SERVE_HOSTNAMES` | empty | Comma-separated extra `tailscale serve` names (bare and `name:port`) |
| `MINERU_FEEDS_FILE` | `$MINERU_HOME/config/feeds.json` | Sidebar feed list (example: `engine/config/feeds.example.json`) |
| `MINERU_LAUNCHD_JOBS_FILE` | `$MINERU_HOME/config/launchd-jobs.json` | Pulse job registry (example: `engine/config/launchd-jobs.example.json`) |
| `LAUNCHD_LABEL_PREFIX` | `com.mineru.` | launchd label namespace Pulse discovers and strips |
| `PASSPHRASE_KEYCHAIN_{SERVICE,ACCOUNT}` | `mineru-webapp-passphrase-hash` / `mineru` | Passphrase-gate Keychain item |

One-time migration for an install that predates the state-dir move (stop the server first):

```bash
mv ~/.mineru/app/state ~/.mineru/app-state
```

## How to add a feed

Feeds are data, not code. Copy `engine/config/feeds.example.json` to `$MINERU_HOME/config/feeds.json` (or set `MINERU_FEEDS_FILE`) and add an entry: `id`, `dirs` (paths relative to `$MINERU_HOME`; absolute paths and `..` are rejected), `display_name`, `emoji`, `accent` (a `tokens.css` accent name), optional `group` (sidebar folder; default "Feeds"). The file REPLACES the bundled `app/feeds.default.json`, so keep the generic feeds you want. Restart the server; the sidebar, `READ_ALLOWLIST`, and `feeds.py` pick it up. To give the job behind it a Pulse card, add it to `$MINERU_HOME/config/launchd-jobs.json` (schema in `app/launchd-jobs.default.json`).

## How to add a theme

Add a `:root[data-theme="foo"] { --token: value; … }` block to `static/css/tokens.css`, then add `{id, label, swatch}` to the `THEMES` array in `static/js/theme.js`. That's it; the picker in Pulse renders it and localStorage persists it. `mail` (flat, light) is the current default — it lives in `tokens.css` + `theme.js` `THEMES` exactly like every other theme; `forest`/`ocean`/`warm`/`dark` are the legacy set and still work.

## How to run for dev

```bash
MINERU_HOME=/tmp/mineru-dev MINERU_WEBAPP_PORT=5299 /usr/bin/python3 app/server.py
```

The startup log lists the port and every allowlisted read root.

## Tailnet exposure (production)

Run once per install (port = `MINERU_WEBAPP_PORT`):

```bash
tailscale serve --bg --https=443 http://127.0.0.1:5195
```

The server's own launchd plist is rendered from `engine/launchd/` at install time (see `mineru cron install`).
