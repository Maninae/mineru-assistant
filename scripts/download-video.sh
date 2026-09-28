#!/usr/bin/env bash
set -euo pipefail

# Download a video (Instagram / YouTube / TikTok / Twitter-X / etc.) with yt-dlp,
# keeping yt-dlp fresh and walking the Instagram login-wall fallback chain automatically.
# Usage: download-video.sh <URL> --out /tmp/vce/video.mp4 [--cookies-from-browser safari|chrome] [--max-age-days N]
#
# Why this script exists (codifying hard-won plumbing, so the download step isn't ad-hoc):
#   - The #1 Instagram failure is a STALE yt-dlp, not auth. Instagram breaks the extractor
#     every few months; a build older than ~90 days fails on every reel with a bogus
#     "login required / rate-limit reached" error. So we auto-upgrade yt-dlp when it's stale,
#     and force an upgrade + retry the first time a download fails. Cookies are a later fallback.
#   - ffmpeg/whisper are stable and are NOT auto-updated here; only yt-dlp rots on Instagram's schedule.
#
# Output: writes the merged mp4 to --out and prints the path on success. Exit 1 on failure
# (with a clear message pointing at the remaining manual fallbacks: browser tool / ask the user).

DEFAULT_MAX_AGE_DAYS=30

usage() {
  cat >&2 <<'EOF'
Usage: download-video.sh <URL> --out <path.mp4> [--cookies-from-browser safari|chrome] [--max-age-days N]
EOF
  exit 2
}

URL="${1:-}"
shift || true
OUT=""
COOKIE_BROWSER=""
MAX_AGE_DAYS="$DEFAULT_MAX_AGE_DAYS"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) OUT="${2:-}"; shift 2 ;;
    --cookies-from-browser) COOKIE_BROWSER="${2:-}"; shift 2 ;;
    --max-age-days) MAX_AGE_DAYS="${2:-$DEFAULT_MAX_AGE_DAYS}"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; usage ;;
  esac
done

[[ -z "$URL" || -z "$OUT" ]] && usage
command -v yt-dlp >/dev/null 2>&1 || { echo "yt-dlp not installed (brew install yt-dlp)" >&2; exit 1; }

UPGRADED_THIS_RUN=0

# Upgrade yt-dlp via whichever installer owns it. Non-fatal: a failed upgrade (offline, etc.)
# just logs and returns, and the download attempt still runs on the existing version.
upgrade_ytdlp() {
  [[ "$UPGRADED_THIS_RUN" == "1" ]] && return 0
  UPGRADED_THIS_RUN=1
  if command -v brew >/dev/null 2>&1 && brew list --formula yt-dlp >/dev/null 2>&1; then
    echo "Upgrading yt-dlp via Homebrew..." >&2
    brew upgrade yt-dlp >&2 2>&1 || echo "warn: brew upgrade yt-dlp failed; continuing on current version" >&2
  else
    echo "Upgrading yt-dlp via yt-dlp -U..." >&2
    yt-dlp -U >&2 2>&1 || echo "warn: yt-dlp -U failed; continuing on current version" >&2
  fi
}

# Days since the installed yt-dlp's release date (its version is a YYYY.MM.DD stamp).
# Echoes an integer age in days, or 9999 if it can't be parsed (treat as stale → upgrade).
ytdlp_age_days() {
  local ver ver_date rel_epoch now_epoch
  ver="$(yt-dlp --version 2>/dev/null | head -1)"
  ver_date="$(echo "$ver" | cut -d. -f1-3)"   # strip any nightly suffix
  # BSD date (macOS) first, then GNU date fallback.
  rel_epoch="$(date -j -f "%Y.%m.%d" "$ver_date" +%s 2>/dev/null \
             || date -d "${ver_date//./-}" +%s 2>/dev/null || echo "")"
  [[ -z "$rel_epoch" ]] && { echo 9999; return; }
  now_epoch="$(date +%s)"
  echo $(( (now_epoch - rel_epoch) / 86400 ))
}

# One yt-dlp download attempt. Extra args (e.g. cookies) are passed through.
try_download() {
  yt-dlp --no-update -f "bv*+ba/b" --merge-output-format mp4 -o "$OUT" "$@" "$URL"
}

mkdir -p "$(dirname "$OUT")"

# 1. Proactively refresh yt-dlp if it's older than the staleness threshold.
AGE="$(ytdlp_age_days)"
if (( AGE > MAX_AGE_DAYS )); then
  echo "yt-dlp is ${AGE}d old (>${MAX_AGE_DAYS}d threshold) — refreshing before download." >&2
  upgrade_ytdlp
fi

# 2. Plain attempt.
if try_download; then
  echo "$OUT"; exit 0
fi

# 3. First failure → force-upgrade (covers a <threshold build that Instagram just broke) and retry.
echo "Download failed; force-upgrading yt-dlp and retrying (the usual Instagram fix)." >&2
upgrade_ytdlp
if try_download; then
  echo "$OUT"; exit 0
fi

# 4. Still failing → try browser cookies (login-gated / genuinely private content).
for browser in ${COOKIE_BROWSER:-safari chrome}; do
  echo "Retrying with --cookies-from-browser $browser ..." >&2
  if try_download --cookies-from-browser "$browser"; then
    echo "$OUT"; exit 0
  fi
done

echo "FAILED to download $URL after update + cookie fallbacks." >&2
echo "Next manual fallbacks (see the video-content-extraction skill): use the browser tool to" >&2
echo "screenshot the page, or ask the user to share the file / screen-record." >&2
exit 1
