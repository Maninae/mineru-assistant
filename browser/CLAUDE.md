# browser/ — Mineru Browser Automation Server

A persistent HTTP server (port 9471) that keeps a stealth-patched Chromium alive across calls, exposing ref-based accessibility tree snapshots + element interaction over JSON for LLM-driven agents.

## ⚠️ Which Python — and 3.9 Compatibility (READ FIRST)

**The server runs on homebrew Python 3.14** (`/opt/homebrew/bin/python3`), because that's the ONLY interpreter on this machine where `cloakbrowser` and `playwright_stealth` are installed. System `/usr/bin/python3` (3.9) does **not** have them — spawning the server there yields a process that 500s on every browser launch with `No module named 'playwright_stealth'`.

`bin/browser` enforces this: `_server_python()` validates that the chosen interpreter can import the stealth deps before spawning the server (preferring `$MINERU_BROWSER_PYTHON`, then `/opt/homebrew/bin/python3`, then the current interpreter, then PATH), and warns loudly if none qualify. **Do not** revert it to `sys.executable` — the CLI's `#!/usr/bin/env python3` shebang is PATH-dependent and a launchd/sandbox/stripped-PATH caller will land on system 3.9.

**Keep the code 3.9-compatible anyway** (defensive — shares idioms with the daemon, and protects the validated-fallback path). **Python 3.10+ syntax crashes on 3.9.**

| Forbidden | Use Instead |
|-----------|-------------|
| `str \| None` | `Optional[str]` from `typing` |
| `dict[str, Any]` (PEP 585 builtin generics) | `Dict[str, Any]` from `typing` |
| `match x:` | `if/elif` chains |
| `def f(x: int) -> str:` (in 3.9-only modules) | Comment-style `# type: (int) -> str` (see `actions.py`, `lifecycle.py`, `accessibility.py`) |

`server.py` uses real annotations because it's the entrypoint; `actions.py`/`lifecycle.py`/`accessibility.py` use comment-style annotations to stay safely 3.9-compatible. Match the style of the file you're editing.

**After any edit, compile-check:**

```bash
python3 -c "import py_compile, glob; [py_compile.compile(f, doraise=True) for f in glob.glob('$MINERU_HOME/browser/*.py')]; print('OK')"
```

---

## Architecture

```
                     bin/browser (CLI wrapper)
                              │
                              │ HTTP POST /action
                              ▼
            ┌──────────────────────────────────┐
            │  server.py  (HTTPServer:9471)    │
            │  ┌────────────────────────────┐  │
            │  │  BrowserManager            │  │  ← OWNS the threading.Lock
            │  │   _lock, _context,         │  │
            │  │   _tabs, _cdp_sessions,    │  │
            │  │   _refs (RefRegistry)      │  │
            │  └────────────────────────────┘  │
            └────────┬────────┬────────┬───────┘
                     │        │        │
                     ▼        ▼        ▼
              lifecycle.py  actions.py  accessibility.py
                   │            │            │
                   │            │            └─ RefRegistry, build_snapshot
                   │            └─ dispatch + act_click/type/select/...
                   │            (stateless helpers)
                   └─ launch_context, cleanup
                      (lazy imports: sync_playwright, cloakbrowser, Stealth)
                              │
                              ▼
                       config.py  (constants + logger; imported by everything)
```

**One direction of dependency:** `server.py` → `actions.py`/`lifecycle.py`/`accessibility.py` → `config.py`. Helpers never import from `server.py`.

---

## Modules

| File | Owns | Does NOT own |
|------|------|--------------|
| `config.py` | All module-level constants (`PORT`, `PID_FILE`, `LOG_DIR`, `HEADLESS`, `CDP_ENDPOINT`, `CLOAK_EXECUTABLE`, `USE_CLOAK`, `PERSISTENT_PROFILE`), role taxonomy (`INTERACTIVE_ROLES`, `VISIBLE_ROLES`), shared `logger`. | Any state. Pure config. |
| `lifecycle.py` | `launch_context()` and `cleanup()` — the 3-mode browser launch (CDP / CloakBrowser / standalone fallback), teardown of context/CDP sessions/playwright/stealth CM. Uses **lazy imports** for `sync_playwright`, `Stealth`, `cloakbrowser` so the module is cheap to load. | The lock. The tab dict (mutates a passed-in dict; doesn't own it). HTTP. |
| `accessibility.py` | `RefRegistry` class (assigns `e1`, `e2`, … to interactive elements) and `build_snapshot()` (CDP `Accessibility.getFullAXTree` → flat indented text + ref map, with ARIA fallback). Owns the role-filtering logic. | The lock. CDP session creation. Action execution. |
| `actions.py` | All element-interaction primitives: `resolve_element`, `cdp_center`, `humanized_move_and_click`, `clear_field`, and the action handlers (`act_click`, `act_type`, `act_fill`, `act_select`, `act_check`, `act_hover`, `act_scroll`, `act_scroll_into_view`, `act_click_coords`). Plus the `dispatch()` router. All functions are **stateless** — they take `(page, cdp, backend_id, ...)` and return a result dict. | The lock. CDP session caching. Ref→backend_id lookup (caller passes `ref_info` in). |
| `server.py` | `BrowserManager` (single `threading.Lock`, tab dict, CDP session cache, registry), `BrowserHandler` (HTTP routing on `/health`, `/tabs`, `/action`), PID file management, signal handlers, the `main()` entrypoint, screenshot/cookie/upload methods that don't belong in helpers. | Stealth/launch details. Action implementations. Accessibility tree walking. |

---

## Key Design Decisions

**BrowserManager is the only thing that holds the lock.** Every public method (`open_tab`, `snapshot`, `act`, `navigate`, `upload`, `screenshot`, `cookies`, `close_tab`, `list_tabs`, `evaluate`, `responsebody`, `wait`, `status`, `shutdown`) wraps its body in `with self._lock:`. Helpers in `actions.py`/`lifecycle.py`/`accessibility.py` **assume the caller already holds it** — they take primitive args, not the manager. This makes the locking story trivial to audit: search for `self._lock` in `server.py` and you've seen every critical section.

**Helpers are stateless functions.** No `self`, no module-level mutable state (other than `config.logger`). All inputs are passed explicitly: page, CDP session, backend_id, ref_info. This means they're trivially unit-testable and the dispatch surface in `actions.py` is just a switch statement. The `dispatch()` router takes a `get_cdp` callable (closure injected by `BrowserManager.act`) so `actions.py` doesn't need to know how CDP sessions are cached.

**`lifecycle.py` uses lazy imports.** `sync_playwright`, `playwright_stealth.Stealth`, and `cloakbrowser` are imported **inside `launch_context()`**, not at module top. Reason: importing them is expensive (Playwright spins up native bindings on import) and the daemon/CLI may import `browser.config` constants without ever launching a browser. Keep it lazy.

**No storage-state persistence on cleanup.** `lifecycle.cleanup()` deliberately does **not** call `context.storage_state(path=...)`. CloakBrowser uses a persistent profile (cookies live in Chromium's encrypted DB), CDP mode uses the real Chrome profile, and the standalone fallback's session cookies are ephemeral by design. Writing plaintext cookie JSON on every shutdown is a C1 security risk (see SECURITY.md).

**Single targetId space, not per-tab counters.** `RefRegistry._ref_counter` is a monotonic int incremented across the whole server, reset per snapshot. Refs (`e1`, `e2`, …) are unique within a snapshot but get reused across snapshots — callers must take a new snapshot after any DOM-changing action.

---

## Runtime Environment

| What | Value |
|------|-------|
| Python | `/usr/bin/python3` (system Python 3.9+) |
| Entrypoint (direct) | `python3 $MINERU_HOME/browser/server.py` |
| Entrypoint (normal) | `bin/browser <action>=<value>...` — auto-starts the server if not running |
| HTTP | `127.0.0.1:9471` (localhost only) |
| PID file | `/tmp/mineru-browser-server.pid` |
| Logs | `$MINERU_HOME/logs/browser-server/server.log` (rotating handler not configured — plain `FileHandler`) |
| CLI stdout/stderr | `$MINERU_HOME/logs/browser-server/stdout.log`, `stderr.log` |
| Persistent profile | `$MINERU_HOME/cache/cloakbrowser-profile/` (mode 700) |

`server.py` runs as a long-lived foreground process. It writes a PID file on start, refuses to start if a live process already holds it, and removes it on clean exit. SIGTERM/SIGINT trigger `BrowserManager.shutdown()` → `_remove_pid()` → `sys.exit(0)`.

**Auto-start (`bin/browser`):** `_ensure_server()` checks `/health`; if down, cleans stale PID, `subprocess.Popen([sys.executable, SERVER_SCRIPT], start_new_session=True)`, polls `/health` every 0.5s for up to 10s. Logs to `stdout.log`/`stderr.log`. So `bin/browser action=open ...` Just Works from a cold shell.

**Path bootstrapping:** When `server.py` is run as a script, the script's parent (`browser/`) ends up on `sys.path` but its grandparent doesn't, so `from browser.config import ...` would fail. Lines 45-47 of `server.py` prepend the grandparent. Don't remove that.

---

## CloakBrowser Stack (Stealth)

Three layers, all required for the anti-bot story to hold:

| Layer | What it is | What it does |
|-------|-----------|--------------|
| **Patchright** | Patched Playwright fork (`backend="patchright"` in the wrapper) | Suppresses the `Runtime.Enable` CDP command leak — the canonical "is this Playwright?" detection. Patches Chromium binary fingerprints (`navigator.webdriver`, plugins, etc.) at the C++ level rather than via JS injection. |
| **CloakBrowser wrapper** | Python package (`import cloakbrowser`) | Manages the patched Chromium binary, handles stealth args (`--disable-blink-features=AutomationControlled`, etc.), sets locale/timezone via **binary launch flags** instead of CDP `Emulation.setLocaleOverride` (which itself is detectable), and owns the Playwright lifecycle internally (`launch_persistent_context` does everything). |
| **CloakBrowser binary** | `~/.cloakbrowser/chromium-145.0.7632.109.2/Chromium.app/Contents/MacOS/Chromium` | Source-patched Chromium 145. Passes reCAPTCHA v3, Cloudflare bot detection, and most fingerprinting suites. |

**Mode selection (`lifecycle.launch_context`):**
1. If `MINERU_BROWSER_CDP` is set → CDP attach (no stealth — it's a real Chrome).
2. Else if `USE_CLOAK` and the binary exists → CloakBrowser persistent context.
3. Else → fallback to standard Chromium + `playwright_stealth.Stealth`.

`humanize=False` is passed to the wrapper because we run our own Bézier-curve mouse moves and keystroke timing in `actions.humanized_move_and_click` / `act_type(humanize=True)`. Don't enable both — they'll fight.

---

## Persistent Profile

| Aspect | Detail |
|--------|--------|
| Location | `$MINERU_HOME/cache/cloakbrowser-profile/` |
| Permissions | `700` (owner-only) — load-bearing security control |
| Contents | Cookies (in Chromium's encrypted DB, obfuscated with `--use-mock-keychain` — NOT true OS encryption), localStorage, IndexedDB, service workers, cached fonts. |
| Survives | Server restarts. Closing all tabs. SIGTERM. Mac reboot. |
| Does not survive | Manually `trash`ing the directory. `rm -rf` (don't). |
| Plaintext export | **Forbidden by default.** `cookies export` requires an explicit `path` arg and validates it (see security section). No "default dump" path exists. |

The profile is created lazily on first CloakBrowser launch (`persistent_profile.mkdir(parents=True, exist_ok=True)` in `lifecycle.py`).

**Cookie management actions** (`BrowserManager.cookies`): `export` / `import` / `list`. Export requires `path`, writes JSON at mode `0600` via `os.open(... O_CREAT|O_TRUNC, 0o600)`, and logs the operation. Import reads from a validated path.

---

## Threading Model

Single `threading.Lock` on `BrowserManager._lock`. Every public method acquires it for the duration of the operation.

| Method | Holds lock for |
|--------|----------------|
| `open_tab` | Browser launch (if needed) + `goto` + `wait_for_load_state` |
| `snapshot` | Full CDP tree fetch + walk |
| `act` | Single action (click / type / select / hover / scroll) |
| `navigate` | `goto` + load wait |
| `evaluate` | JS execution |
| `responsebody` | The full `wait_for_timeout` window |
| `upload` | File chooser interception or set_input_files |
| `screenshot` | `page.screenshot()` |
| `cookies` | Export/import/list |
| `close_tab` | CDP detach + page.close |
| `list_tabs` | Iteration |
| `status` | Liveness check |
| `shutdown` | Full teardown |

**Implication:** HTTP requests serialize through one browser. This is intentional — Playwright/CDP are not thread-safe per-context, and tab interleaving would create unpredictable focus/keyboard state. If you need parallelism (e.g. a second profile on the same Mac running its own isolated browser), export `MINERU_BROWSER_PORT` + `MINERU_BROWSER_PID_FILE` (and a per-profile `MINERU_HOME`, which drives `PERSISTENT_PROFILE`) for that profile's session. `bin/browser` reads the same env, so the CLI client and the server land on the same port + pid file, and the primary profile's server on `9471` keeps running untouched.

`responsebody` holds the lock during its entire timeout — this can block other requests for up to `timeout_ms` (default 10s). Keep that timeout tight when calling it.

---

## How to Add a New Action

Worked example: adding `act_drag` (drag from one ref to another).

1. **Add the implementation to `actions.py`.** Stateless function taking `(page, cdp, backend_id, ref_info, ...)`. Match the existing style (comment-style type hints, return a dict with `"action"` key):

   ```python
   def act_drag(page, cdp, backend_id, ref_info, dx, dy):
       # type: (Any, Any, int, Dict, int, int) -> Dict[str, Any]
       el = resolve_element(page, cdp, backend_id)
       box = el.bounding_box(timeout=5000) if el else None
       if not box:
           raise ValueError("Cannot resolve element for drag — take a new snapshot")
       sx, sy = box["x"] + box["width"]/2, box["y"] + box["height"]/2
       page.mouse.move(sx, sy); page.mouse.down()
       page.mouse.move(sx + dx, sy + dy, steps=20); page.mouse.up()
       return {"action": "drag", "ref": ref_label(ref_info), "dx": dx, "dy": dy}
   ```

2. **Add a dispatch case in `actions.dispatch()`.** Place inside the ref-required block (after `backend_id = ref_info["backendDOMNodeId"]`) unless the action doesn't need a ref:

   ```python
   elif kind == "drag":
       dx = int(request.get("dx", 0))
       dy = int(request.get("dy", 0))
       return act_drag(page, cdp, backend_id, ref_info, dx, dy)
   ```

   Update the `else` branch's error message to include the new kind.

3. **Wire HTTP routing in `server.py`** — only needed if it's a **new top-level action** (not a new `kind` under `act`). For a new `kind`, no `server.py` change is needed; `BrowserHandler.do_POST` already forwards `kind` and `request` to `manager.act()` which calls `actions.dispatch()`.

   If it IS a new top-level action (like `screenshot` / `upload`), add a method on `BrowserManager` (with `with self._lock:`), then add an `elif action == "drag":` branch in `BrowserHandler.do_POST`.

4. **Update the docstring** at the top of `server.py` (the `Actions:` list) and `bin/browser`'s usage doc.

5. **Compile-check + smoke test:** `curl -X POST 127.0.0.1:9471/action -d '{"action":"act","targetId":"...","kind":"drag",...}'`.

---

## How to Debug

| Symptom | Where to look |
|---------|---------------|
| Server won't start | `$MINERU_HOME/logs/browser-server/stderr.log` (uncaught import errors land here) |
| Action returns 500 | `$MINERU_HOME/logs/browser-server/server.log` — `BrowserHandler.do_POST` logs full tracebacks via `logger.error` |
| Browser visible behavior | `MINERU_BROWSER_HEADLESS=false bin/browser action=open targetUrl="..."` — pops the window |
| Server already running but stale | `cat /tmp/mineru-browser-server.pid; bin/browser action=stop; trash /tmp/mineru-browser-server.pid` |
| CloakBrowser missing | Logs `"Starting standard Chromium (CloakBrowser not available)"` — check `~/.cloakbrowser/chromium-145.0.7632.109.2/Chromium.app/Contents/MacOS/Chromium` exists |
| Refs say "Unknown ref" | DOM changed — call `snapshot` again. Refs are invalidated on `navigate` and never persist across snapshots. |
| CDP tree empty | `build_snapshot` falls back to ARIA snapshot (no refs). Log line: `"CDP accessibility tree failed: ..."`. Usually means the page isn't fully loaded — `wait` first. |

**Curl examples:**

```bash
# Health
curl -s http://127.0.0.1:9471/health | jq

# Open a tab
curl -s -X POST http://127.0.0.1:9471/action \
  -H 'Content-Type: application/json' \
  -d '{"action":"open","targetUrl":"https://example.com"}'

# Snapshot
curl -s -X POST http://127.0.0.1:9471/action \
  -H 'Content-Type: application/json' \
  -d '{"action":"snapshot","targetId":"tab_abc12345"}'

# Click ref e1
curl -s -X POST http://127.0.0.1:9471/action \
  -H 'Content-Type: application/json' \
  -d '{"action":"act","targetId":"tab_abc12345","kind":"click","ref":"e1"}'

# Stop
curl -s -X POST http://127.0.0.1:9471/action \
  -H 'Content-Type: application/json' \
  -d '{"action":"stop"}'
```

**Force-restart:** `bin/browser action=stop; sleep 1; bin/browser action=status` (status auto-starts).

---

## Viewport Overrides (mobile-width screenshots)

`open` and `screenshot` accept an optional viewport override so a mobile-width full-page screenshot is a single call. **Backward compatible:** omitted, both actions keep the launched context's default viewport (previously 1440×900 in Cloak mode / 1280×900 in standalone), so every existing caller is unaffected.

Three input shapes, all folded into `{width, height}` by `resolve_viewport()` in `server.py`. Priority order (first non-empty wins): explicit `viewport` dict → named `device` preset → flat `width`+`height`.

| Preset (`device=`) | Size (CSS px) | Notes |
|---|---|---|
| `iphone` | 393 × 852 | iPhone 14 Pro |
| `iphone-se` | 375 × 667 | Smallest phone we regularly test |
| `pixel` | 412 × 915 | Pixel phones |
| `narrow` | 375 × 812 | Generic narrow |
| `mobile` | 390 × 844 | Generic modern phone |
| `tablet` | 820 × 1180 | iPad Air |
| `desktop` | 1440 × 900 | Explicit "back to desktop" |

**On `open` — set the viewport before first navigation** so the initial render sees the target size (first paint, CSS media queries, hydration all match the mobile viewport):

```bash
# One shot: open a mobile view, full-page screenshot
browser action=open targetUrl="https://example.com" device=iphone
# → {"targetId": "tab_x", "viewport": {"width": 393, "height": 852}, ...}
browser action=screenshot targetId=tab_x path=/tmp/mobile.png fullPage=true
```

**On `screenshot` — resize this tab, then shoot** (useful for retaking one open tab at multiple widths):

```bash
# Same tab, mobile shot without reopening
browser action=screenshot targetId=tab_x path=/tmp/mobile.png viewport='{"width":390,"height":844}' fullPage=true
# The tab keeps the new viewport after the shot; take another shot with device=desktop to go back.
browser action=screenshot targetId=tab_x path=/tmp/desktop.png device=desktop
```

Direct HTTP (both `open` and `screenshot`):

```json
{"action": "open", "targetUrl": "...", "viewport": {"width": 390, "height": 844}}
{"action": "screenshot", "targetId": "tab_x", "device": "iphone", "fullPage": true}
```

Notes:
- A viewport override on `screenshot` **persists** on the tab after the shot — we do not restore the previous size. This matches what a caller usually wants (a phone-view tab stays a phone-view tab).
- The screenshot response now includes `viewport` (actual size) and `fullPage` (boolean). Existing fields are unchanged.
- An unknown `device=` name or a malformed `viewport` dict returns HTTP 400 from the server — a typo lands as an error, not a silent no-op.

## Security Constraints

Full policy in `$MINERU_HOME/SECURITY.md`. Highlights enforced in this directory:

- **Cookies never leave the persistent profile by default.** `cookies export` requires an explicit `path` arg — there is no implicit dump on shutdown (`lifecycle.cleanup` deliberately omits `storage_state(path=...)`). This is the C1 mitigation referenced in `lifecycle.py`.
- **Cookie path validation** (`BrowserManager._validate_cookie_path`): exported/imported paths must resolve under `$MINERU_HOME/` or `/tmp/`, and **must not be a symlink** (symlink check happens on the raw path, before resolution). Files are written at mode `0600` via `os.open` with `O_CREAT|O_TRUNC`.
- **Cookie ops are audited:** `logger.info("Cookie export: %d cookies to %s", ...)` on every export. Grep `server.log` for `"Cookie export"` to find exfiltration attempts.
- **Persistent profile permissions:** `cloakbrowser-profile/` is `700`. The Chromium cookie DB is obfuscated with `--use-mock-keychain`, **not** truly OS-encrypted — the directory permission is the load-bearing control. Treat the directory itself as a secret.
- **Prompt injection chain:** `evaluate` can read `document.cookie` / `localStorage`. A compromised page that gets injected content into the LLM could chain `evaluate` → `cookies export` to exfiltrate. The prompt injection firewall (see `$MINERU_HOME/scripts/firewall/`) screens external content before it reaches the LLM; `evaluate` and `cookies export` both log every call.
- **Localhost-only HTTP:** `HTTPServer(("127.0.0.1", PORT), ...)`. Never bind to `0.0.0.0`. There is no auth on `/action` — anyone with local shell access can drive the browser.
- **Redacted credential fills (`redact=true`):** `type`/`fill` accept a `redact` flag. When set, the filled value is scrubbed from the HTTP response AND from server logs — **including the exception path** (Playwright embeds the value in its call-log error string; `dispatch()` scrubs raw + JSON-escaped forms at the raise site, and `do_POST` scrubs again as belt-and-suspenders). Each redacted fill logs a metadata-only audit line (`"Redacted fill: ref=… target=… url=…"`, grep it like `"Cookie export"`). The sole caller is `scripts/op-relogin-fill.py` (1Password re-login, see SECURITY.md §6). If you touch the `type`/`fill` handlers or `do_POST`'s error logging, re-run the leak probe: redact-fill a non-fillable ref (e.g. a button) with a quote-containing canary, then grep all three logs for it (expect 0).
- **DOM-readback window:** a just-filled password lives in the field's DOM `value` (browsers mask visually, not in the DOM), so `evaluate`/`screenshot` between fill and submit would pull it back into context. `op-relogin-fill.py --submit-ref` clicks submit inside the same call to keep that window near-zero; never `evaluate` a password field's value.

---

## Quick Reference: Action Surface

| HTTP action | Required args | Notes |
|-------------|---------------|-------|
| `open` | `targetUrl` (or `url`) | Returns `targetId`. Waits for `domcontentloaded` + `networkidle` (capped at 10s). |
| `navigate` | `targetId`, `url` | Invalidates refs for that tab. |
| `snapshot` | `targetId` | Resets ref counter, fetches CDP AX tree, returns flat text + ref map. |
| `act` | `targetId`, `kind`, usually `ref` | Sub-kinds: `click`, `click-coords`, `type`, `fill`, `select`, `check`, `hover`, `scroll`, `scrollIntoView`. |
| `wait` | `targetId` | `until` ∈ {`networkidle`, `domcontentloaded`, `load`}. |
| `upload` | `targetId`, `ref`, `paths` (list) | `via_click=true` for styled buttons that open a file chooser. |
| `evaluate` | `targetId`, `expression` | Optional `arg`. Returns `{"action":"evaluate","result":...}`. Security-sensitive (see above). |
| `responsebody` | `targetId`, `urlPattern` | fnmatch pattern. Blocks for full `timeout_ms`. |
| `screenshot` | `targetId` | Optional `path` (writes file) or returns base64. `fullPage=true` for whole-page. Optional viewport override — see below. |
| `open` viewport | `viewport` / `device` / `width`+`height` | See below. Backward-compatible: omitting all three keeps the launched context's default viewport. |
| `close` | `targetId` | Idempotent — returns `already_closed` if gone. |
| `cookies` | `operation` ∈ {`export`,`import`,`list`} | `export`/`import` need `path`. Optional `domain` filter. |
| `status` | — | Returns `{status, tabs, pid, port}`. Works even with no browser launched. |
| `stop` | — | Graceful shutdown via background thread (so HTTP response fires first). |
