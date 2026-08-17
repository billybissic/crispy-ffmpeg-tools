#!/usr/bin/env bash

# Usage:
#   ./convert-x-to-16x9-nogrow-shrink.sh /path/to/videos
#   ./convert-x-to-16x9-nogrow-shrink.sh /path/to/videos --high-action
#   ./convert-x-to-16x9-nogrow-shrink.sh /path/to/videos --shrink 10
#   ./convert-x-to-16x9-nogrow-shrink.sh /path/to/videos --high-action --shrink 10

set -u

dir="${1:-.}"
shift || true

high_action=0
shrink_percent=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --high-action)
      high_action=1
      shift
      ;;
    --shrink)
      if [[ $# -lt 2 || ! "$2" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
        echo "--shrink requires a numeric percentage, e.g. --shrink 10"
        exit 1
      fi
      shrink_percent="$2"
      shift 2
      ;;
    -h|--help)
      echo "Usage: $0 [directory] [--high-action] [--shrink percent]"
      echo
      echo "Examples:"
      echo "  $0 /path/to/videos"
      echo "  $0 /path/to/videos --shrink 8"
      echo "  $0 /path/to/videos --high-action --shrink 10"
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      echo "Usage: $0 [directory] [--high-action] [--shrink percent]"
      exit 1
      ;;
  esac
done

if [[ ! -d "$dir" ]]; then
  echo "Directory not found: $dir"
  exit 1
fi

# Keep this intentionally modest. 50%+ belongs in the dedicated shrink-video workflow.
if awk "BEGIN {exit !($shrink_percent < 0 || $shrink_percent >= 50)}"; then
  echo "--shrink must be at least 0 and less than 50 for this converter."
  exit 1
fi

shopt -s nullglob nocaseglob

for input in "$dir"/*.{mkv,mp4,avi,m4v,mov,wmv,ts,m2ts}; do
  filename="$(basename "$input")"
  name="${filename%.*}"

  if [[ "$filename" == *.16x9.mkv ]]; then
    echo "Skipping already converted: $filename"
    continue
  fi

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

  source_bitrate=$(ffprobe -v error \
    -select_streams v:0 \
    -show_entries stream=bit_rate \
    -of default=noprint_wrappers=1:nokey=1 \
    "$input")

  duration=$(ffprobe -v error \
    -show_entries format=duration \
    -of default=noprint_wrappers=1:nokey=1 \
    "$input")

  if [[ -z "$width" || -z "$height" || ! "$width" =~ ^[0-9]+$ || ! "$height" =~ ^[0-9]+$ ]]; then
    echo "Could not determine video dimensions: $filename"
    continue
  fi

  if [[ -z "$duration" || "$duration" == "N/A" ]]; then
    echo "Could not determine duration: $filename"
    continue
  fi

  # Preserve HD/UHD vertical resolution; only upscale SD sources to 720p.
  if (( height < 720 )); then
    scale_filter="scale=1280:720:flags=lanczos"
    output="$dir/${name}.720P.16x9.mkv"
  else
    scale_filter="scale=-2:${height}:flags=lanczos"
    output="$dir/${name}.${height}P.16x9.mkv"
  fi

  if [[ -f "$output" ]]; then
    echo "Skipping existing output: $(basename "$output")"
    continue
  fi

  # Quality ceilings by resolution class.
  if (( width >= 3840 || height >= 2160 )); then
    if (( high_action )); then
      ceiling_kbps=20000
    else
      ceiling_kbps=14000
    fi
  elif (( width >= 1920 || height >= 1080 )); then
    if (( high_action )); then
      ceiling_kbps=8000
    else
      ceiling_kbps=6000
    fi
  else
    if (( high_action )); then
      ceiling_kbps=3500
    else
      ceiling_kbps=2550
    fi
  fi

  source_bytes=$(stat -c%s "$input")

  # Sum copied audio bitrates. Some containers don't expose per-stream bitrate;
  # use a conservative fallback for those tracks.
  audio_total_bps=0
  while IFS= read -r abr; do
    [[ -z "$abr" ]] && continue
    if [[ "$abr" =~ ^[0-9]+$ ]]; then
      audio_total_bps=$((audio_total_bps + abr))
    else
      audio_total_bps=$((audio_total_bps + 192000))
    fi
  done < <(ffprobe -v error \
    -select_streams a \
    -show_entries stream=bit_rate \
    -of csv=p=0 \
    "$input")

  # Calculate the total average bitrate represented by the actual source file.
  source_total_bps=$(awk "BEGIN {printf \"%.0f\", ($source_bytes * 8) / $duration}")

  # Default mode is effectively no-growth, with 2% room for muxing/rounding.
  # --shrink N instead targets approximately N percent below the source size.
  if awk "BEGIN {exit !($shrink_percent > 0)}"; then
    target_total_bps=$(awk "BEGIN {printf \"%.0f\", $source_total_bps * ((100 - $shrink_percent) / 100)}")
    overhead_factor="0.985"
  else
    target_total_bps="$source_total_bps"
    overhead_factor="0.98"
  fi

  usable_total_bps=$(awk "BEGIN {printf \"%.0f\", $target_total_bps * $overhead_factor}")
  size_cap_video_bps=$((usable_total_bps - audio_total_bps))
  size_cap_kbps=$((size_cap_video_bps / 1000))

  if (( size_cap_kbps <= 0 )); then
    echo "Cannot reach requested size while copying all audio: $filename"
    continue
  fi

  # Start from the quality ceiling, then cap it by source bitrate when known,
  # and finally by the file-size target. The lowest wins.
  target_kbps=$ceiling_kbps

  if [[ "$source_bitrate" =~ ^[0-9]+$ ]]; then
    source_kbps=$((source_bitrate / 1000))
    if (( source_kbps < target_kbps )); then
      target_kbps=$source_kbps
    fi
  fi

  if (( size_cap_kbps < target_kbps )); then
    target_kbps=$size_cap_kbps
  fi

  echo
  echo "Converting: $filename"
  echo "       -> $(basename "$output")"
  if (( high_action )); then
    echo "     mode: HIGH ACTION"
  else
    echo "     mode: NORMAL"
  fi
  if awk "BEGIN {exit !($shrink_percent > 0)}"; then
    echo "   shrink: ${shrink_percent}% target"
  else
    echo "   shrink: no-growth mode"
  fi
  echo "  bitrate: ${target_kbps}k (quality ceiling ${ceiling_kbps}k, size cap ${size_cap_kbps}k)"
  echo

  ffmpeg -i "$input" \
    -vf "crop='if(gt(a,5/3),ih*5/3,iw)':'if(gt(a,5/3),ih,iw*3/5)',${scale_filter}" \
    -c:v libx264 -b:v "${target_kbps}k" -preset slow \
    -c:a copy \
    "$output"

  if [[ $? -ne 0 ]]; then
    echo "FAILED: $filename"
  fi
done
