#!/usr/bin/env bash

dir="${1:-.}"
output="${2:-large-videos.txt}"

if [[ ! -d "$dir" ]]; then
  echo "Directory not found: $dir"
  exit 1
fi

echo "Scanning: $dir"
echo "Finding video files larger than 4 GB..."

find "$dir" -type f \
  \( -iname "*.mkv" -o \
     -iname "*.mp4" -o \
     -iname "*.avi" -o \
     -iname "*.m4v" -o \
     -iname "*.mov" -o \
     -iname "*.wmv" -o \
     -iname "*.ts" -o \
     -iname "*.m2ts" \) \
  -size +4G \
  -printf '%s\t%p\n' 2>/dev/null \
  | sort -nr \
  | awk -F '\t' '
      function human(bytes) {
        gb = bytes / 1024 / 1024 / 1024
        return sprintf("%.2f GB", gb)
      }
      {
        size=$1
        $1=""
        sub(/^\t/, "")
        printf "%10s  %s\n", human(size), $0
      }
    ' > "$output"

count=$(wc -l < "$output")

echo
echo "Found $count video file(s) larger than 4 GB."
echo "Saved list to: $output"
