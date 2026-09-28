#!/usr/bin/env bash
set -euo pipefail

# Extract audio track from video for transcription.
# Usage: extract-audio.sh <video> --out /tmp/audio.wav [--format wav|mp3|m4a]
#
# Defaults to 16kHz mono WAV (optimal for Whisper).
# Returns exit code 1 if video has no audio stream.

VIDEO="${1:-}"
shift || true
OUT=""
FORMAT="wav"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out)    OUT="${2:-}"; shift 2 ;;
    --format) FORMAT="${2:-wav}"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

[[ -z "$VIDEO" || -z "$OUT" ]] && { echo "Usage: extract-audio.sh <video> --out <path> [--format wav|mp3|m4a]" >&2; exit 2; }
[[ ! -f "$VIDEO" ]] && { echo "File not found: $VIDEO" >&2; exit 1; }

# Check if video has an audio stream
HAS_AUDIO=$(ffprobe -v error -select_streams a -show_entries stream=codec_type -of csv=p=0 "$VIDEO" 2>/dev/null | head -1)
if [[ -z "$HAS_AUDIO" ]]; then
  echo "NO_AUDIO"
  exit 0
fi

mkdir -p "$(dirname "$OUT")"

if [[ "$FORMAT" == "wav" ]]; then
  ffmpeg -hide_banner -loglevel error -y \
    -i "$VIDEO" \
    -vn -acodec pcm_s16le -ar 16000 -ac 1 \
    "$OUT"
else
  ffmpeg -hide_banner -loglevel error -y \
    -i "$VIDEO" \
    -vn \
    "$OUT"
fi

echo "$OUT"
