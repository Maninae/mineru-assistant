#!/usr/bin/env bash
set -euo pipefail

# Extract frames from a video with smart sampling based on duration.
# Usage: extract-frames.sh <video> --out-dir /tmp/frames [--mode auto|dense|sparse|scene]
#
# Modes:
#   auto   — dense (every 2s) for ≤60s, medium (every 5s) for ≤600s, sparse (every 15s) for longer
#   dense  — every 2 seconds
#   sparse — every 15 seconds
#   scene  — scene-change detection (threshold 0.3)
#
# Output: numbered JPEGs in --out-dir, prints count and file list.

usage() {
  cat >&2 <<'EOF'
Usage: extract-frames.sh <video-file> --out-dir <dir> [--mode auto|dense|sparse|scene] [--max-frames N]
EOF
  exit 2
}

VIDEO="${1:-}"
shift || true
OUT_DIR=""
MODE="auto"
MAX_FRAMES=50

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out-dir) OUT_DIR="${2:-}"; shift 2 ;;
    --mode)    MODE="${2:-auto}"; shift 2 ;;
    --max-frames) MAX_FRAMES="${2:-50}"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; usage ;;
  esac
done

[[ -z "$VIDEO" || -z "$OUT_DIR" ]] && usage
[[ ! -f "$VIDEO" ]] && { echo "File not found: $VIDEO" >&2; exit 1; }

mkdir -p "$OUT_DIR"

# Get duration in seconds
DURATION=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$VIDEO" 2>/dev/null | cut -d. -f1)
DURATION=${DURATION:-0}

# Determine interval based on mode
if [[ "$MODE" == "auto" ]]; then
  if (( DURATION <= 60 )); then
    INTERVAL=2
  elif (( DURATION <= 600 )); then
    INTERVAL=5
  else
    INTERVAL=15
  fi
elif [[ "$MODE" == "dense" ]]; then
  INTERVAL=2
elif [[ "$MODE" == "sparse" ]]; then
  INTERVAL=15
elif [[ "$MODE" == "scene" ]]; then
  INTERVAL=0  # scene detection mode
else
  echo "Unknown mode: $MODE" >&2; usage
fi

PATTERN="$OUT_DIR/frame-%04d.jpg"

if [[ "$MODE" == "scene" ]]; then
  ffmpeg -hide_banner -loglevel error -y \
    -i "$VIDEO" \
    -vf "select='gt(scene,0.3)',setpts=N/FRAME_RATE/TB" \
    -frames:v "$MAX_FRAMES" \
    -vsync vfr \
    "$PATTERN"
else
  ffmpeg -hide_banner -loglevel error -y \
    -i "$VIDEO" \
    -vf "fps=1/$INTERVAL" \
    -frames:v "$MAX_FRAMES" \
    "$PATTERN"
fi

COUNT=$(ls "$OUT_DIR"/frame-*.jpg 2>/dev/null | wc -l | tr -d ' ')
echo "Extracted $COUNT frames to $OUT_DIR (duration=${DURATION}s, mode=$MODE, interval=${INTERVAL}s)"
ls "$OUT_DIR"/frame-*.jpg 2>/dev/null
