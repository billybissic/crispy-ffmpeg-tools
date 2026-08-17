#!/usr/bin/env bash

list="$1"
target="${2:-2.5G}"
log="${3:-shrink-results.csv}"

if [[ -z "$list" || ! -f "$list" ]]; then
  echo "Usage: $0 <file-list> [target-size] [log-file]"
  echo
  echo "Examples:"
  echo "  $0 first-100-paths.txt 2.5G"
  echo "  $0 first-100-paths.txt 2.5G batch-01.csv"
  exit 1
fi

if [[ ! -f "$log" ]]; then
  echo '"status","original_size_gb","new_size_gb","saved_gb","saved_percent","original_file","output_file"' > "$log"
fi

while IFS= read -r input; do
  [[ -z "$input" ]] && continue

  if [[ ! -f "$input" ]]; then
    echo "Skipping missing file:"
    echo "  $input"
    printf '"MISSING","","","","","%s",""\n' "$input" >> "$log"
    continue
  fi

  original_bytes=$(stat -c%s "$input")

  dir="$(dirname "$input")"
  filename="$(basename "$input")"
  name="${filename%.*}"
  output="$dir/${name}.${target}.H264.mkv"

  echo
  echo "=================================================="
  echo "Processing:"
  echo "$input"
  echo "=================================================="
  echo

  "$(dirname "$0")/shrink-video.sh" "$input" "$target"
  exit_code=$?

  if [[ $exit_code -ne 0 ]]; then
    echo
    echo "FAILED:"
    echo "$input"

    original_gb=$(awk "BEGIN {printf \"%.2f\", $original_bytes / 1024 / 1024 / 1024}")
    printf '"FAILED","%s","","","","%s","%s"\n' \
      "$original_gb" "$input" "$output" >> "$log"
    continue
  fi

  if [[ ! -f "$output" ]]; then
    echo
    echo "WARNING: encoder exited successfully but output was not found:"
    echo "$output"

    original_gb=$(awk "BEGIN {printf \"%.2f\", $original_bytes / 1024 / 1024 / 1024}")
    printf '"NO_OUTPUT","%s","","","","%s","%s"\n' \
      "$original_gb" "$input" "$output" >> "$log"
    continue
  fi

  new_bytes=$(stat -c%s "$output")
  saved_bytes=$((original_bytes - new_bytes))

  original_gb=$(awk "BEGIN {printf \"%.2f\", $original_bytes / 1024 / 1024 / 1024}")
  new_gb=$(awk "BEGIN {printf \"%.2f\", $new_bytes / 1024 / 1024 / 1024}")
  saved_gb=$(awk "BEGIN {printf \"%.2f\", $saved_bytes / 1024 / 1024 / 1024}")
  saved_percent=$(awk "BEGIN {printf \"%.1f\", ($saved_bytes / $original_bytes) * 100}")

  echo
  echo "SUCCESS"
  echo "Original: ${original_gb} GB"
  echo "New:      ${new_gb} GB"
  echo "Saved:    ${saved_gb} GB (${saved_percent}%)"
  echo

  printf '"SUCCESS","%s","%s","%s","%s","%s","%s"\n' \
    "$original_gb" "$new_gb" "$saved_gb" "$saved_percent" "$input" "$output" >> "$log"

done < "$list"

echo
echo "=================================================="
echo "Batch complete"
echo "Log: $log"
echo "=================================================="
