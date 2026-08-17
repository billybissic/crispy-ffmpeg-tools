#!/usr/bin/env bash

input="$1"
target="$2"

if [[ -z "$input" || -z "$target" ]]; then
  echo "Usage: $0 <video-file> <target-size>"
  echo
  echo "Examples:"
  echo "  $0 movie.mkv 2G"
  echo "  $0 movie.mkv 2.5G"
  echo "  $0 movie.mkv 3000M"
  exit 1
fi

if [[ ! -f "$input" ]]; then
  echo "File not found: $input"
  exit 1
fi

case "$target" in
  *[Gg])
    amount="${target%[Gg]}"
    target_bytes=$(awk "BEGIN {printf \"%.0f\", $amount * 1024 * 1024 * 1024}")
    ;;
  *[Mm])
    amount="${target%[Mm]}"
    target_bytes=$(awk "BEGIN {printf \"%.0f\", $amount * 1024 * 1024}")
    ;;
  *)
    echo "Target size must end in M or G."
    echo "Example: 2.5G or 2500M"
    exit 1
    ;;
esac

current_bytes=$(stat -c%s "$input")

duration=$(ffprobe -v error \
  -show_entries format=duration \
  -of default=noprint_wrappers=1:nokey=1 \
  "$input")

width=$(ffprobe -v error \
  -select_streams v:0 \
  -show_entries stream=width \
  -of default=noprint_wrappers=1:nokey=1 \
  "$input")

height=$(ffprobe -v error \
  -select_streams v:0 \
  -show_entries stream=height \
  -of default=noprint_wrappers=1:nokey=1 \
  "$input")

# Sum all audio stream bitrates. Unknown streams get a conservative 192 kbps estimate.
audio_bitrates=$(ffprobe -v error \
  -select_streams a \
  -show_entries stream=bit_rate \
  -of csv=p=0 \
  "$input")

audio_total=0
audio_tracks=0

while IFS= read -r bitrate; do
  [[ -z "$bitrate" ]] && continue
  ((audio_tracks++))

  if [[ "$bitrate" =~ ^[0-9]+$ ]]; then
    audio_total=$((audio_total + bitrate))
  else
    audio_total=$((audio_total + 192000))
  fi
done <<< "$audio_bitrates"

# Calculate a video bitrate from the requested target budget.
# Audio/subtitles are copied unchanged, so sources with very large audio tracks
# can produce a final file larger than the requested target.
target_total_bps=$(awk "BEGIN {printf \"%.0f\", ($target_bytes * 8) / $duration}")
usable_bps=$(awk "BEGIN {printf \"%.0f\", $target_total_bps * 0.98}")
video_bps=$((usable_bps - audio_total))
video_kbps=$((video_bps / 1000))

if (( video_kbps <= 0 )); then
  echo "Target size is too small for the preserved audio streams."
  exit 1
fi

dir="$(dirname "$input")"
filename="$(basename "$input")"
name="${filename%.*}"
output="$dir/${name}.${target}.H264.mkv"

current_mb=$((current_bytes / 1024 / 1024))
target_mb=$((target_bytes / 1024 / 1024))

# Estimate final size from calculated video bitrate + preserved audio.
estimated_total_bps=$((video_bps + audio_total))
estimated_bytes=$(awk "BEGIN {printf \"%.0f\", (($estimated_total_bps * $duration) / 8) / 0.98}")
estimated_mb=$((estimated_bytes / 1024 / 1024))

echo
echo "--------------------------------------------------"
echo " Video Shrink"
echo "--------------------------------------------------"
echo
echo "Input:       $filename"
echo "Resolution:  ${width}x${height}"
echo "Current:     ${current_mb} MB"
echo "Target:      ${target_mb} MB"
echo "Audio:       ${audio_tracks} track(s)"
echo "Audio total: $((audio_total / 1000)) kbps"
echo "Video rate:  ${video_kbps} kbps"
echo "Est. output: ${estimated_mb} MB"
echo
echo "Output:"
echo "$output"
echo
echo "--------------------------------------------------"
echo

ffmpeg -i "$input" \
  -map 0:v \
  -map 0:a? \
  -map 0:s? \
  -map_metadata 0 \
  -map_chapters 0 \
  -c:v libx264 \
  -b:v "${video_kbps}k" \
  -preset slow \
  -c:a copy \
  -c:s copy \
  "$output"
