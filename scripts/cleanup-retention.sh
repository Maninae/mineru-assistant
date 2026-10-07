#!/bin/bash
# cleanup-retention.sh — Idempotent retention policy for the Mineru workspace.
#
# Rules:
#   - logs/ (recursive) files >14d  → gzip
#                       gzipped >90d → trash
#                       (skipped when <file>.gz already exists, so a
#                        clash never aborts the run under `set -e`)
#   - briefs_*/         date-stamped files >120d → archive/briefs/<dirname>/
#                       (helper scripts, state JSON and other undated
#                        files in a briefs dir are never archived)
#   - cache/telegram_{images,files}/* >14d → trash
#   - cache/telegram_sent_images/*.json past its own expires_at → trash
#     the sidecar + its companion bytes file (retention driven by the
#     sidecar, not mtime; 'expires_at: null' means 'forever' → skipped)
#
# Uses `trash` (recoverable) — never `rm`. Safe to run repeatedly.
# Scheduled: launchd com.mineru.cleanup-retention, 2nd of each month 4:07 AM.
#
# Flags:
#   --dry-run   print what would happen, change nothing

set -euo pipefail
shopt -s nullglob

DRY=0
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY=1
  echo "[dry-run] no changes will be made"
fi

WORKSPACE="${MINERU_HOME:-$HOME/.mineru}"
cd "$WORKSPACE"

# macOS BSD `find -mtime +N` matches files older than N*24h
# A filename carrying a date (2026-10-06 or 20261006) is a dated output;
# anything else in a briefs dir is long-lived tooling or state.
DATE_STAMP_REGEX='[0-9]{4}-?[0-9]{2}-?[0-9]{2}'

run() {
  if [[ $DRY -eq 1 ]]; then
    echo "  would: $*"
  else
    "$@"
  fi
}

# 1) gzip log files older than 14 days inside logs/<job>/ AND directly under
#    logs/ (a job that writes logs/foo.log instead of logs/foo/ must not be
#    silently orphaned from retention)
echo "==> gzip logs/ files older than 14d"
while IFS= read -r -d '' f; do
  case "$f" in *.gz) continue ;; esac
  if [[ -e "$f.gz" ]]; then
    echo "  skip (already has $f.gz): $f"
    continue
  fi
  run gzip "$f"
done < <(find logs -type f -mtime +14 ! -name "*.gz" -print0 2>/dev/null)

# 2) trash gzipped logs older than 90 days
echo "==> trash logs/*/*.gz older than 90d"
while IFS= read -r -d '' f; do
  run trash "$f"
done < <(find logs -type f -name "*.gz" -mtime +90 -print0 2>/dev/null)

# 3) archive briefs_*/ files older than 120 days into archive/briefs/<dirname>/
echo "==> archive date-stamped briefs_*/ files older than 120d"
for d in briefs_*/; do
  [[ -d "$d" ]] || continue
  base="${d%/}"
  dest="archive/briefs/$base"
  while IFS= read -r -d '' f; do
    [[ "$(basename "$f")" =~ $DATE_STAMP_REGEX ]] || continue
    rel="${f#$d}"
    target_dir="$dest/$(dirname "$rel")"
    if [[ $DRY -eq 1 ]]; then
      echo "  would: mkdir -p $target_dir && mv $f $target_dir/"
    else
      mkdir -p "$target_dir"
      mv "$f" "$target_dir/"
    fi
  done < <(find "$d" -type f -mtime +120 -print0 2>/dev/null)
done

# 4) trash cache/telegram_images/* and cache/telegram_files/* older than 14
#    days (files = Telegram document downloads, unbounded in size)
echo "==> trash cache/telegram_{images,files}/* older than 14d"
for cache_dir in cache/telegram_images cache/telegram_files; do
  [[ -d "$cache_dir" ]] || continue
  while IFS= read -r -d '' f; do
    run trash "$f"
  done < <(find "$cache_dir" -type f -mtime +14 -print0 2>/dev/null)
done

# 5) trash cache/telegram_sent_images/*.json (+ companion bytes file) past
#    the per-image `expires_at` in the sidecar. This is the P3-02 sent-image
#    retention cache. Retention is per-entry, so we can't lean on `find
#    -mtime`; we walk the sidecar JSONs and compare against `expires_at`.
#    A sidecar with `expires_at: null` is the 'forever' sentinel — skip.
#    A missing / unparseable expires_at is treated as expired (defensive:
#    a broken sidecar should get swept eventually rather than linger).
echo "==> trash cache/telegram_sent_images/*.json past expires_at"
sent_dir="cache/telegram_sent_images"
if [[ -d "$sent_dir" ]]; then
  # Reference "now" once per run so all sidecars are compared against a
  # single wall-clock snapshot, avoiding a race where a sidecar written
  # at second 59 is judged expired against second 00 of the next second.
  now_iso="$(python3 -c 'import datetime; print(datetime.datetime.now().astimezone().isoformat(timespec="seconds"))')"
  for sidecar in "$sent_dir"/*.json; do
    [[ -e "$sidecar" ]] || continue
    # Python is already a hard dep of the workspace (msearch, deliver-
    # output). Parse the sidecar's expires_at + emit either "FOREVER" (skip),
    # "EXPIRED" (trash), or "KEEP" (leave alone). One subprocess per sidecar
    # is acceptable at the operator's volume (a photo per minute at most, per §4.2).
    verdict="$(python3 - "$sidecar" "$now_iso" <<'PY'
import datetime
import json
import sys

sidecar_path, now_iso = sys.argv[1], sys.argv[2]
try:
    with open(sidecar_path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
except (OSError, json.JSONDecodeError):
    print("EXPIRED")
    sys.exit(0)
expires_at = raw.get("expires_at")
if expires_at is None:
    print("FOREVER")
    sys.exit(0)
try:
    exp_dt = datetime.datetime.fromisoformat(str(expires_at))
    now_dt = datetime.datetime.fromisoformat(now_iso)
except ValueError:
    print("EXPIRED")
    sys.exit(0)
if exp_dt.tzinfo is None and now_dt.tzinfo is not None:
    exp_dt = exp_dt.replace(tzinfo=now_dt.tzinfo)
print("EXPIRED" if exp_dt <= now_dt else "KEEP")
PY
)"
    case "$verdict" in
      EXPIRED)
        # Companion bytes file shares the sidecar stem; extension can be
        # anything (.jpg, .png, .webp, etc). Enumerate siblings that share
        # the stem but are NOT the sidecar itself.
        stem_prefix="${sidecar%.json}"
        for companion in "$stem_prefix".*; do
          [[ -e "$companion" ]] || continue
          [[ "$companion" == "$sidecar" ]] && continue
          run trash "$companion"
        done
        run trash "$sidecar"
        ;;
      FOREVER|KEEP)
        # explicit no-op branches for grep-visible intent
        :
        ;;
      *)
        # Unknown verdict shape (python emitted something weird): leave
        # the sidecar alone rather than risk a wrong-side trash.
        :
        ;;
    esac
  done
fi

echo "==> cleanup-retention done"
