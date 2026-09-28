#!/usr/bin/env python3
"""Delivers content to the operator's chat via Telegram Bot API.

Token storage:
  1. macOS Keychain (service: mineru/telegram-bot-token, mineru/telegram-chat-id)
  2. Environment variables (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)

Supports markdown-to-HTML conversion for richer Telegram formatting.

Image mode:
  Text delivery is the default and stays 100% unchanged. To send an image
  file (JPG/PNG/etc.) to the same Telegram chat, use:

      deliver-output.py --photo /path/to/img.png [--caption "text"] \
                       [--imessage <number> | --imessage-keychain <service>]

  The photo path also flows through the iMessage arm as a --file attachment
  when --imessage/--imessage-keychain is passed (caption sent as --text).

Import surface (for other scripts):
  from importlib.util import spec_from_file_location, module_from_spec
  # or simpler: subprocess.run([...deliver-output.py, "--photo", path, ...])
  Reusable function:
      send_telegram_photo(path, caption, token, chat_id) -> bool
  Callers that already have token/chat_id in hand can call it directly.
  Callers without them can use load_telegram_config() (does Keychain + env
  resolution + the allowlist check that the text path uses).
"""

import datetime
import html
import json
import mimetypes
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.telegram_fmt import md_to_telegram_html
from lib.tldr import TLDR_MAX_CHARS, extract_tldr_line, extract_title_and_tldr  # noqa: F401 — re-exported for callers/tests
from lib.imessage_format import DEFAULT_IMESSAGE_FORMAT, format_for_imessage_by_name

TELEGRAM_MSG_LIMIT = 4096
_MINERU_HOME = Path(os.environ.get("MINERU_HOME", str(Path.home() / ".mineru")))
# Step-5 audit, Finding 7/10: honor `MINERU_INJECT_QUEUE_DIR` explicitly so a
# per-profile invocation writes into THIS profile's inject queue. Falls back
# to the workspace-scoped path so tests + legacy callers keep working.
INJECT_QUEUE_DIR = Path(
    os.environ.get(
        "MINERU_INJECT_QUEUE_DIR",
        str(_MINERU_HOME / "cache" / "inject-queue"),
    )
)

# Web Push sender (fire-and-forget alongside Telegram). Non-fatal: any error
# fires a log line and the cron delivery continues normally. Short timeout so
# a slow push service can never wedge the delivery pipeline.
PUSH_SEND_SCRIPT = Path(__file__).resolve().parent / "push_send.py"
PUSH_SEND_TIMEOUT_SECONDS = 15
# Directory-prefix stripped from a brief's parent-dir name to produce the
# feed_id in the deep-link hash: briefs_morning/ → morning, briefs_pet/ → pet.
BRIEFS_DIR_PREFIX = "briefs_"

# Optional second delivery arm: an iMessage copy for a recipient without Telegram
# (e.g. a family member). Outbound only — no injection risk, so no firewall.
IMSG_BIN = os.environ.get("MINERU_IMSG_BIN", "/opt/homebrew/bin/imsg")


def find_companion_data_files(brief_path: Path) -> List[Path]:
    """Return machine-readable sidecar files a brief drops next to itself.

    Convention: a brief `foo-YYYY-MM-DD.md` may ship structured data as
    `foo-YYYY-MM-DD-*.json` in the same directory (e.g. inbox triage writes
    `triage-YYYY-MM-DD-ids.json`, the thread-id manifest a later "clear" reply
    acts on). Surfacing these in the pointer is what lets a follow-up reply be
    executed against real data instead of reconstructed by hand.
    """
    return sorted(brief_path.parent.glob(f"{brief_path.stem}-*.json"))


def build_inject_pointer(label: str, brief_path: Path, content: str) -> str:
    """Build the pointer payload mirrored into the inject queue for file deliveries.

    The main session's context gets this pointer instead of the full brief text;
    the operator still receives the complete brief in Telegram. The session Reads the
    file at the given path when the conversation needs more than the TLDR, and
    reads any companion data file before acting on a follow-up reply.
    """
    lines = [
        f"A scheduled brief (`{label}`) was delivered to the operator via Telegram "
        "(he received the full text there).",
        f"Full brief on disk: {brief_path.resolve()}",
        f"TLDR: {extract_tldr_line(content)}",
    ]
    companions = find_companion_data_files(brief_path)
    if companions:
        joined = ", ".join(str(p.resolve()) for p in companions)
        lines.append(
            f"Structured data alongside it: {joined} — machine-readable; read this "
            "before acting on any follow-up reply about this brief (e.g. \"clear\")."
        )
    lines.append("Read the file if the conversation needs details beyond the TLDR.")
    return "\n".join(lines)


_DEFAULT_KEYCHAIN_ACCOUNT = os.environ.get("MINERU_KEYCHAIN_ACCOUNT", "mineru")


def keychain_get(
    service: str, account: str = _DEFAULT_KEYCHAIN_ACCOUNT
) -> Optional[str]:
    """Read a secret from macOS Keychain.

    The default `account` honors the `MINERU_KEYCHAIN_ACCOUNT` env var
    (default `"mineru"`) so the per-profile trigger scripts can point
    the bot-token / chat-id / allowlist lookups at a different Keychain
    namespace. Callers may still override `account` explicitly.
    """
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-a", account, "-s", service, "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def load_telegram_config() -> Tuple[str, str]:
    """Resolve bot token and chat ID from Keychain or env."""
    token = (
        keychain_get("telegram-bot-token")
        or os.environ.get("TELEGRAM_BOT_TOKEN")
    )
    chat_id = (
        keychain_get("telegram-chat-id")
        or os.environ.get("TELEGRAM_CHAT_ID")
    )

    if not token or not chat_id:
        missing = []
        if not token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not chat_id:
            missing.append("TELEGRAM_CHAT_ID")
        print(
            f"ERROR: Missing {', '.join(missing)}. "
            "Store via: security add-generic-password -a mineru -s telegram-bot-token -w <TOKEN> -U",
            file=sys.stderr,
        )
        sys.exit(1)

    # Inline allowlist check — same Keychain key the daemon's guard reads.
    # We avoid importing landline.guard so an unrelated syntax error in the
    # landline package can't take down every cron delivery.
    # Semantics match landline/guard.py (in ~/Developer/claude-landline):
    # comma-separated chat IDs, stripped;
    # empty / missing allowlist = block (fail-closed).
    allowed_raw = keychain_get("telegram-allowed-chat-ids")
    allowed = {cid.strip() for cid in (allowed_raw or "").split(",") if cid.strip()}
    if not allowed:
        print(
            "ERROR: telegram-allowed-chat-ids not set in Keychain — blocking delivery",
            file=sys.stderr,
        )
        sys.exit(1)
    if str(chat_id) not in allowed:
        print(f"ERROR: chat_id {chat_id} is not in the Telegram allowlist", file=sys.stderr)
        sys.exit(1)

    return token, str(chat_id)


def chunk_message(text: str, limit: int = TELEGRAM_MSG_LIMIT) -> List[str]:
    """Split text into Telegram-sized chunks at paragraph/line/word boundaries."""
    if len(text) <= limit:
        return [text]

    chunks: List[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        cut = remaining[:limit].rfind("\n\n")
        if cut > limit // 4:
            chunks.append(remaining[:cut])
            remaining = remaining[cut + 2:]
            continue

        cut = remaining[:limit].rfind("\n")
        if cut > limit // 4:
            chunks.append(remaining[:cut])
            remaining = remaining[cut + 1:]
            continue

        cut = remaining[:limit].rfind(" ")
        if cut > limit // 4:
            chunks.append(remaining[:cut])
            remaining = remaining[cut + 1:]
            continue

        chunks.append(remaining[:limit])
        remaining = remaining[limit:]

    return chunks


def send_telegram(text: str, token: str, chat_id: str, parse_mode: str = "HTML") -> bool:
    """Send message(s) via Telegram Bot API. Stdlib only — no pip deps."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    chunks = chunk_message(text)

    for chunk in chunks:
        payload_dict = {
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload_dict["parse_mode"] = parse_mode

        payload = json.dumps(payload_dict).encode("utf-8")

        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
                if not result.get("ok"):
                    print(f"Telegram API error: {result}", file=sys.stderr)
                    return False
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            print(f"Telegram HTTP {e.code}: {body}", file=sys.stderr)
            return False
        except urllib.error.URLError as e:
            print(f"Telegram network error: {e.reason}", file=sys.stderr)
            return False

    return True


# Telegram sendPhoto caption limit (docs: 1024 chars). Text-mode uses the
# 4096-char sendMessage limit; a longer photo caption gets split so the first
# chunk rides with the photo and the tail follows as plain sendMessage calls.
TELEGRAM_PHOTO_CAPTION_LIMIT = 1024


def _sanitize_multipart_filename(name: str) -> str:
    """Return a filename safe to drop into a Content-Disposition header.

    Multipart headers use CRLF as the field terminator, so a filename that
    contains \\r or \\n could inject additional headers or body content. We
    also strip a `"` (would break the quoted-string) and any other control
    chars. Empty result falls back to a stable placeholder so we never emit
    `filename=""`, which some servers reject as malformed.
    """
    basename = os.path.basename(name or "")
    cleaned = "".join(ch for ch in basename if ch >= " " and ch not in '"\\')
    # Collapse whitespace runs — a rare tab in a filename shouldn't be
    # replayed into the header.
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or "upload.bin"


def send_telegram_photo(
    photo_path: str,
    caption: Optional[str],
    token: str,
    chat_id: str,
    parse_mode: str = "HTML",
) -> bool:
    """POST a photo to Telegram via sendPhoto as a multipart/form-data upload.

    Reuses the same {token, chat_id} the text path uses so the same allowlist
    check gates it (the caller resolves those via load_telegram_config()).
    Returns True on success, False on any API/HTTP/network error (with a
    stderr log line describing the failure — same shape as send_telegram).

    A caption longer than Telegram's 1024-char sendPhoto limit is truncated on
    the photo and the remainder is sent as a follow-up sendMessage call, so the
    caller doesn't have to think about caption length.

    HTML parse_mode: Telegram parses the caption as HTML, so an unescaped `&`,
    `<`, or `>` in the raw caption crashes the API with HTTP 400. We
    html.escape() the caption AND the tail before sending — mirrors what the
    text path already does via md_to_telegram_html for messages. Passing
    parse_mode="" (or None) skips the escape so a caller that already built
    Telegram-HTML gets its markup through intact.

    Uses only stdlib — no `requests`, no pip deps — to match the rest of
    deliver-output.py's runtime posture (cron-runnable under any Python 3.9+).
    """
    p = Path(photo_path)
    if not p.exists():
        print(f"send_telegram_photo: file not found: {p}", file=sys.stderr)
        return False
    if not p.is_file():
        print(f"send_telegram_photo: not a regular file: {p}", file=sys.stderr)
        return False

    photo_bytes = p.read_bytes()
    content_type = mimetypes.guess_type(p.name)[0] or "application/octet-stream"

    # Split a too-long caption FIRST (on the raw text so head/tail stay
    # semantically coherent), then html-escape each part independently. This
    # way the escape can't be split mid-entity (e.g. right after the `&` of
    # `&amp;`), which would be invalid HTML on the head side.
    caption_head_raw = caption or ""
    caption_tail_raw = ""
    if len(caption_head_raw) > TELEGRAM_PHOTO_CAPTION_LIMIT:
        caption_tail_raw = caption_head_raw[TELEGRAM_PHOTO_CAPTION_LIMIT:]
        caption_head_raw = caption_head_raw[:TELEGRAM_PHOTO_CAPTION_LIMIT]

    if parse_mode and parse_mode.upper() == "HTML":
        caption_head = html.escape(caption_head_raw)
    else:
        caption_head = caption_head_raw

    # Build a multipart/form-data body by hand. Keeping it in stdlib avoids a
    # `requests` dep on cron paths that already run pure-stdlib today.
    boundary = "----mineru" + uuid.uuid4().hex
    crlf = "\r\n"
    parts: List[bytes] = []

    def _add_field(name: str, value: str) -> None:
        parts.append(f"--{boundary}{crlf}".encode("utf-8"))
        parts.append(
            f'Content-Disposition: form-data; name="{name}"{crlf}{crlf}'.encode("utf-8")
        )
        parts.append(value.encode("utf-8"))
        parts.append(crlf.encode("utf-8"))

    _add_field("chat_id", chat_id)
    if caption_head:
        _add_field("caption", caption_head)
        if parse_mode:
            _add_field("parse_mode", parse_mode)

    safe_name = _sanitize_multipart_filename(p.name)
    parts.append(f"--{boundary}{crlf}".encode("utf-8"))
    parts.append(
        (
            f'Content-Disposition: form-data; name="photo"; filename="{safe_name}"{crlf}'
            f"Content-Type: {content_type}{crlf}{crlf}"
        ).encode("utf-8")
    )
    parts.append(photo_bytes)
    parts.append(crlf.encode("utf-8"))
    parts.append(f"--{boundary}--{crlf}".encode("utf-8"))
    body = b"".join(parts)

    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
            if not result.get("ok"):
                print(f"Telegram sendPhoto API error: {result}", file=sys.stderr)
                return False
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        print(f"Telegram sendPhoto HTTP {e.code}: {detail}", file=sys.stderr)
        return False
    except urllib.error.URLError as e:
        print(f"Telegram sendPhoto network error: {e.reason}", file=sys.stderr)
        return False

    # Send the caption tail (if any) as a follow-up plain-text message so we
    # never silently truncate. Escape the tail with the SAME rule the head
    # used, or Telegram will HTTP 400 on the follow-up while the head went
    # through (partial delivery is the worst failure mode).
    if caption_tail_raw:
        if parse_mode and parse_mode.upper() == "HTML":
            caption_tail = html.escape(caption_tail_raw)
        else:
            caption_tail = caption_tail_raw
        return send_telegram(caption_tail, token, chat_id, parse_mode=parse_mode)
    return True


def mask_recipient(number: str) -> str:
    """Log-safe form of an iMessage recipient: only the last 2 digits (`…89`).

    Delivery stdout lands in job logs, so a full phone number never gets printed.
    """
    digits = "".join(ch for ch in str(number) if ch.isdigit())
    return f"…{digits[-2:]}" if len(digits) >= 2 else "…"


def send_imessage_photo(
    photo_path: str,
    caption: Optional[str],
    number: str,
) -> None:
    """Send a photo via iMessage as an attachment. Non-fatal on any failure.

    Mirrors send_imessage() for the photo path: the `imsg` CLI accepts
    `--file <path>` to send an attachment, and `--text` alongside it delivers
    the caption in the same message. Any failure is logged to stderr and
    swallowed so the primary Telegram delivery is never broken by iMessage.
    """
    try:
        argv = [IMSG_BIN, "send", "--to", number, "--file", str(photo_path)]
        if caption:
            argv.extend(["--text", caption])
        r = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            print(
                f"iMessage photo send failed (non-fatal): "
                f"{r.stderr.strip().replace(number, mask_recipient(number))}",
                file=sys.stderr,
            )
        else:
            print(f"Also delivered photo via iMessage to {mask_recipient(number)}")
    except (subprocess.TimeoutExpired, FileNotFoundError) as imsg_error:
        print(
            f"iMessage photo send failed (non-fatal): {type(imsg_error).__name__}",
            file=sys.stderr,
        )


def fire_web_push_for_brief(brief_path: Path, content: str) -> None:
    """Fire push_send.py after a successful Telegram brief delivery. Non-fatal.

    Runs in a subprocess with a short timeout. Any exit code / crash / timeout
    is logged and swallowed so the cron delivery is never broken by push. The
    push payload is title + one-line teaser + deep-link hash — never the full
    brief (which stays behind the gated app).
    """
    if not PUSH_SEND_SCRIPT.exists():
        return
    try:
        title, tldr = extract_title_and_tldr(content)
        title = title or brief_path.stem
        teaser = tldr or title
        # Deep-link hash the SPA router understands: '#brief/<feed_id>/<filename>'.
        # Feed id is the parent-dir name with the `briefs_` prefix stripped.
        parent_dir_name = brief_path.parent.name
        if parent_dir_name.startswith(BRIEFS_DIR_PREFIX):
            feed_id = parent_dir_name[len(BRIEFS_DIR_PREFIX):]
            deep_link = f"#brief/{feed_id}/{brief_path.name}"
        else:
            # Brief written outside the standard briefs_* tree — still ping,
            # just without a deep link. The notification body carries the
            # title so the user still knows what landed.
            deep_link = ""
        # Tag by feed so a new brief in the same feed REPLACES the earlier
        # notification on-device instead of stacking (avoids notification spam
        # when e.g. two morning briefs land close together).
        tag = f"mineru-{parent_dir_name}"
        argv = [
            "/usr/bin/python3", str(PUSH_SEND_SCRIPT),
            "--title", title,
            "--body", teaser,
            "--tag", tag,
        ]
        if deep_link:
            argv.extend(["--url", deep_link])
        # Step-5 audit, Finding 10: thread the active profile's env through
        # to push_send.py so its KEYCHAIN_ACCOUNT + APP_DIR pin to THIS
        # profile's VAPID key + subscribers, not the framework default.
        # `os.environ` already carries MINERU_HOME / MINERU_KEYCHAIN_ACCOUNT
        # / MINERU_INJECT_QUEUE_DIR — either set by `get_profile()` on the
        # CLI's first ctx hydration, or set by the launchd plist
        # EnvironmentVariables block on a cron invocation. Copy the whole
        # ambient env (PATH, HOME, TZ, ...) alongside so push_send.py can
        # still find `/usr/bin/security`, HTTPS proxies, etc.
        subprocess.run(argv, timeout=PUSH_SEND_TIMEOUT_SECONDS, check=False,
                       capture_output=True, env={**os.environ})
    except Exception as push_error:
        # Never break the cron delivery on a push failure. Log the class only
        # so we don't smear title / teaser bytes across the log.
        print(f"push_send failed (non-fatal): {type(push_error).__name__}", file=sys.stderr)


def enqueue_for_session(label: str, content: str) -> None:
    """Drop a file into the inject queue so the daemon can silently acknowledge this delivery."""
    INJECT_QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    # The "%Y%m%dT%H%M%S" stem format is the canonical authority for the
    # inject-queue filename. The daemon-side consumer (landline/inject.py in
    # ~/Developer/claude-landline) parses it via
    # landline.config.INJECT_TIMESTAMP_FORMAT. Kept as a literal here on
    # purpose: deliver-output.py stays free of any landline.* import so a daemon
    # import-time error can never break cron deliveries. If this format ever
    # changes, update landline/config.py INJECT_TIMESTAMP_FORMAT in lockstep.
    ts = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    safe_label = re.sub(r"[^A-Za-z0-9_-]", "_", label)
    path = INJECT_QUEUE_DIR / f"{ts}-{safe_label}.json"
    path.write_text(
        json.dumps({"label": label, "content": content}, ensure_ascii=False),
        encoding="utf-8",
    )


def send_imessage(text: str, number: str) -> None:
    """Send an iMessage copy of a delivery via the `imsg` CLI. Non-fatal.

    Powers the optional --imessage / --imessage-keychain arm so a recipient
    without Telegram (e.g. a family member) gets a clean iMessage copy. Any
    failure is logged and swallowed so it never breaks the primary Telegram
    delivery. Outbound sends carry no injection risk, so no firewall here.
    """
    try:
        r = subprocess.run(
            [IMSG_BIN, "send", "--to", number, "--text", text],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            masked_stderr = r.stderr.strip().replace(number, mask_recipient(number))
            print(f"iMessage send failed (non-fatal): {masked_stderr}", file=sys.stderr)
        else:
            print(f"Also delivered {len(text)} chars via iMessage to {mask_recipient(number)}")
    except (subprocess.TimeoutExpired, FileNotFoundError) as imsg_error:
        print(f"iMessage send failed (non-fatal): {type(imsg_error).__name__}", file=sys.stderr)


def extract_imessage_recipient(argv: List[str]) -> Tuple[List[str], Optional[str]]:
    """Pop --imessage <number> / --imessage-keychain <service> from argv.

    Returns (argv_without_the_flag, resolved_number_or_None). --imessage-keychain
    reads the recipient number from the named Keychain service, keeping a family
    member's number out of git (the cron job references the service name, not the
    number). --imessage passes a number directly (used for self-tests).
    """
    out: List[str] = []
    number: Optional[str] = None
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--imessage" and i + 1 < len(argv):
            number = argv[i + 1]
            i += 2
            continue
        if tok == "--imessage-keychain" and i + 1 < len(argv):
            number = keychain_get(argv[i + 1])
            if not number:
                print(
                    f"WARNING: --imessage-keychain '{argv[i + 1]}' not found in Keychain — skipping iMessage arm",
                    file=sys.stderr,
                )
            i += 2
            continue
        out.append(tok)
        i += 1
    return out, number


def extract_imessage_format(argv: List[str]) -> Tuple[List[str], str]:
    """Pop --imessage-format <name> from argv, defaulting to DEFAULT_IMESSAGE_FORMAT.

    Selects which iMessage formatting profile (emoji map + bullet spacing) the
    reformat uses; names are the keys of lib/imessage_format.py IMESSAGE_RENDERERS
    (`pet_summary`, `household_finance`, `monthly_finance`). A report wired to the
    iMessage arm passes its own profile (e.g. `--imessage-format household_finance`).
    An unknown name degrades to the default renderer, never raises.
    """
    out: List[str] = []
    fmt = DEFAULT_IMESSAGE_FORMAT
    i = 0
    while i < len(argv):
        if argv[i] == "--imessage-format" and i + 1 < len(argv):
            fmt = argv[i + 1]
            i += 2
            continue
        out.append(argv[i])
        i += 1
    return out, fmt


def extract_photo_args(argv: List[str]) -> Tuple[List[str], Optional[str], Optional[str]]:
    """Pop --photo <path> and --caption <text> from argv.

    --photo alone is enough — --caption is optional. Both flags may appear
    anywhere in argv (parser is positional-agnostic). Returns
    (argv_without_the_flags, photo_path_or_None, caption_or_None).

    Scope guard: --caption is ONLY consumed when --photo is also present in
    argv. Otherwise it round-trips through untouched so a future text-mode
    or --raw command that legitimately wants a `--caption` flag can define
    its own semantics without this extractor stealing the value. This is
    the fix for the audit L2 finding.
    """
    photo_present = "--photo" in argv
    out: List[str] = []
    photo: Optional[str] = None
    caption: Optional[str] = None
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--photo" and i + 1 < len(argv):
            photo = argv[i + 1]
            i += 2
            continue
        if tok == "--caption" and i + 1 < len(argv) and photo_present:
            caption = argv[i + 1]
            i += 2
            continue
        out.append(tok)
        i += 1
    return out, photo, caption


def main() -> None:
    argv = sys.argv[1:]
    argv, imsg_number = extract_imessage_recipient(argv)
    argv, imsg_format = extract_imessage_format(argv)
    argv, photo_path, photo_caption = extract_photo_args(argv)

    # --photo mode: separate, minimal codepath. It reuses the same
    # token/chat_id + allowlist as the text path but never touches
    # inject-queue / web-push / iMessage-formatting for text.
    if photo_path:
        if argv:
            print(
                f"Ignoring extra args in --photo mode: {argv}",
                file=sys.stderr,
            )
        token, chat_id = load_telegram_config()
        ok = send_telegram_photo(photo_path, photo_caption, token, chat_id)
        if not ok:
            sys.exit(1)
        print(f"Delivered photo {photo_path} via Telegram")
        # Optional iMessage arm — mirrors the text path's second-delivery
        # convention. Non-fatal.
        if imsg_number:
            send_imessage_photo(photo_path, photo_caption, imsg_number)
        return

    if not argv:
        print("Usage: deliver-output.py <file_path> [--imessage <number> | --imessage-keychain <service>] [--imessage-format <name>]", file=sys.stderr)
        print("       deliver-output.py --raw <text>", file=sys.stderr)
        print("       deliver-output.py --photo <path> [--caption <text>] [--imessage <number> | --imessage-keychain <service>]", file=sys.stderr)
        sys.exit(1)

    brief_path = None
    if argv[0] == "--raw":
        label = "raw"
        content = " ".join(argv[1:])
    else:
        fp = Path(argv[0])
        if not fp.exists():
            print(f"File not found: {fp}", file=sys.stderr)
            sys.exit(1)
        label = fp.stem
        content = fp.read_text(encoding="utf-8")
        brief_path = fp

    if not content.strip():
        print("Empty content, skipping delivery", file=sys.stderr)
        sys.exit(1)

    token, chat_id = load_telegram_config()
    html_text = md_to_telegram_html(content)

    # send_telegram chunks internally — no need to pre-compute chunks here.
    if send_telegram(html_text, token, chat_id, parse_mode="HTML"):
        print(f"Delivered {len(content)} chars via Telegram")
        # Optional second arm: an iMessage copy (reformatted — no markdown, emoji
        # bullets) for a recipient without Telegram. Non-fatal + independent of
        # the Telegram/inject/push path below.
        if imsg_number:
            send_imessage(format_for_imessage_by_name(content, imsg_format), imsg_number)
        # File deliveries mirror a pointer+TLDR into the session's context (the
        # full text already reached the operator in Telegram); --raw one-liners mirror
        # as-is since they have no file to point at.
        if brief_path is not None:
            # A triage brief must ship its `*-ids.json` manifest; warn loudly if
            # it doesn't so a skipped `triage_ids.py write` shows up in the job
            # log instead of silently leaving a later "clear" reply with no IDs.
            if brief_path.stem.startswith("triage-") and not find_companion_data_files(brief_path):
                print(
                    f"WARNING: {brief_path.name} delivered with no companion *-ids.json "
                    "manifest — a 'clear' reply will have no thread IDs to act on.",
                    file=sys.stderr,
                )
            enqueue_for_session(label, build_inject_pointer(label, brief_path, content))
            # ALSO fire Web Push in parallel to Telegram so a device with the
            # PWA installed gets a lock-screen ping. Non-fatal: any error is
            # logged and swallowed. --raw one-liners skip this — there's no
            # brief to deep-link to.
            fire_web_push_for_brief(brief_path, content)
        else:
            enqueue_for_session(label, content)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
