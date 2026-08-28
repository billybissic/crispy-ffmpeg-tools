#!/usr/bin/env bash
set -u

DB="${DB:-./media-processing.db}"
TARGET="${TARGET:-2.5G}"
SHRINK_SCRIPT="${SHRINK_SCRIPT:-./shrink-video.sh}"
DURATION_TOLERANCE="${DURATION_TOLERANCE:-5}"

usage() {
  cat <<'USAGE'
Usage:
  ./process-staged-batch.sh [options] ID [ID ...]

Processes queue items that are already IN_PROCESSING:

  shrink -> verify -> adopt-output -> return-file -> verify

Options:
  --db PATH               SQLite DB path (default: ./media-processing.db)
  --target SIZE           shrink-video target (default: 2.5G)
  --shrink-script PATH    shrink script (default: ./shrink-video.sh)
  --duration-tolerance S  Maximum duration difference (default: 5 seconds)
  -h, --help              Show help

Example:
  ./process-staged-batch.sh 5 91 69 88 65

The script is sequential and fail-fast. It never overwrites a destination.
USAGE
}

fail() {
  echo
  echo "ERROR: $*" >&2
  echo "Batch stopped. No later IDs were touched." >&2
  exit 1
}

IDS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --db)
      [[ $# -ge 2 ]] || fail "--db requires a value"
      DB="$2"; shift 2 ;;
    --target)
      [[ $# -ge 2 ]] || fail "--target requires a value"
      TARGET="$2"; shift 2 ;;
    --shrink-script)
      [[ $# -ge 2 ]] || fail "--shrink-script requires a value"
      SHRINK_SCRIPT="$2"; shift 2 ;;
    --duration-tolerance)
      [[ $# -ge 2 ]] || fail "--duration-tolerance requires a value"
      DURATION_TOLERANCE="$2"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    -*)
      fail "Unknown option: $1" ;;
    *)
      IDS+=("$1"); shift ;;
  esac
done

(( ${#IDS[@]} > 0 )) || { usage; exit 2; }

for cmd in media-queue ffprobe stat awk dirname basename; do
  command -v "$cmd" >/dev/null 2>&1 || fail "Missing required command: $cmd"
done

[[ -x "$SHRINK_SCRIPT" ]] || fail "Shrink script is not executable: $SHRINK_SCRIPT"

echo "IDs:    ${IDS[*]}"
echo "DB:     $DB"
echo "Target: $TARGET"
echo

passed=0

for id in "${IDS[@]}"; do
  [[ "$id" =~ ^[0-9]+$ ]] || fail "Invalid queue ID: $id"

  echo "============================================================"
  echo "PROCESS ITEM $id"
  echo "============================================================"

  before="$(media-queue --db "$DB" show "$id")" || fail "Could not read queue item $id"

  status="$(awk -F': ' '$1=="status" {print $2; exit}' <<<"$before")"
  processing_path="$(awk -F': ' '$1=="processing_path" {sub(/^processing_path: /,""); print; exit}' <<<"$before")"
  original_path="$(awk -F': ' '$1=="original_path" {sub(/^original_path: /,""); print; exit}' <<<"$before")"

  [[ "$status" == "IN_PROCESSING" ]] || fail "Item $id is $status; expected IN_PROCESSING"
  [[ -n "$processing_path" ]] || fail "Item $id has no processing_path"
  [[ -f "$processing_path" ]] || fail "Staged source missing: $processing_path"

  source_dir="$(dirname "$original_path")"
  staged_bytes="$(stat -c%s "$processing_path")"
  staged_duration="$(ffprobe -v error \
    -show_entries format=duration \
    -of default=noprint_wrappers=1:nokey=1 \
    "$processing_path")" || fail "Could not read duration: $processing_path"

  echo "Source: $processing_path"
  echo "Home:   $source_dir"
  echo
  echo "[1/5] Encoding..."

  "$SHRINK_SCRIPT" "$processing_path" "$TARGET" || fail "Encoder failed for item $id"

  staged_dir="$(dirname "$processing_path")"
  staged_filename="$(basename "$processing_path")"
  staged_name="${staged_filename%.*}"
  rendered_path="$staged_dir/${staged_name}.${TARGET}.H264.mkv"

  [[ -f "$rendered_path" ]] || fail "Expected rendered output not found: $rendered_path"

  rendered_bytes="$(stat -c%s "$rendered_path")"
  rendered_duration="$(ffprobe -v error \
    -show_entries format=duration \
    -of default=noprint_wrappers=1:nokey=1 \
    "$rendered_path")" || fail "Could not read rendered duration: $rendered_path"

  duration_diff="$(awk -v a="$staged_duration" -v b="$rendered_duration" \
    'BEGIN {d=a-b; if(d<0)d=-d; printf "%.3f", d}')"
  duration_ok="$(awk -v d="$duration_diff" -v t="$DURATION_TOLERANCE" \
    'BEGIN {print (d<=t)?1:0}')"

  echo "[2/5] Verifying render..."
  echo "      source bytes:   $staged_bytes"
  echo "      rendered bytes: $rendered_bytes"
  echo "      duration delta: ${duration_diff}s"

  [[ "$duration_ok" == "1" ]] || fail \
    "Duration mismatch for item $id exceeds ${DURATION_TOLERANCE}s"

  (( rendered_bytes < staged_bytes )) || fail \
    "Rendered output for item $id is not smaller than source"

  rendered_basename="$(basename "$rendered_path")"
  expected_destination="$source_dir/$rendered_basename"

  # Preserve the no-overwrite contract before adopt-output removes the staged source.
  [[ ! -e "$expected_destination" ]] || fail \
    "Destination already exists; refusing to overwrite: $expected_destination"

  echo "[3/5] Adopting rendered output..."
  media-queue --db "$DB" adopt-output "$id" "$rendered_path" \
    || fail "adopt-output failed for item $id"

  adopted="$(media-queue --db "$DB" show "$id")" || fail "Could not verify adopted item $id"
  adopted_status="$(awk -F': ' '$1=="status" {print $2; exit}' <<<"$adopted")"
  adopted_processing="$(awk -F': ' '$1=="processing_path" {sub(/^processing_path: /,""); print; exit}' <<<"$adopted")"

  [[ "$adopted_status" == "READY_TO_RETURN" ]] || fail \
    "Item $id is $adopted_status after adoption; expected READY_TO_RETURN"
  [[ "$adopted_processing" == "$rendered_path" ]] || fail \
    "Queue did not retain rendered processing filename for item $id"

  echo "[4/5] Returning rendered file..."
  media-queue --db "$DB" return-file "$id" \
    || fail "return-file failed for item $id"

  echo "[5/5] Verifying destination..."
  after="$(media-queue --db "$DB" show "$id")" || fail "Could not verify returned item $id"

  final_status="$(awk -F': ' '$1=="status" {print $2; exit}' <<<"$after")"
  final_path="$(awk -F': ' '$1=="original_path" {sub(/^original_path: /,""); print; exit}' <<<"$after")"

  [[ "$final_status" == "RETURNED" ]] || fail \
    "Item $id is $final_status after return; expected RETURNED"
  [[ "$final_path" == "$expected_destination" ]] || fail \
    "Returned path mismatch for item $id. Expected: $expected_destination Got: $final_path"
  [[ -f "$final_path" ]] || fail "Returned file missing: $final_path"

  final_bytes="$(stat -c%s "$final_path")"
  final_duration="$(ffprobe -v error \
    -show_entries format=duration \
    -of default=noprint_wrappers=1:nokey=1 \
    "$final_path")" || fail "Could not read returned duration: $final_path"

  final_diff="$(awk -v a="$staged_duration" -v b="$final_duration" \
    'BEGIN {d=a-b; if(d<0)d=-d; printf "%.3f", d}')"
  final_duration_ok="$(awk -v d="$final_diff" -v t="$DURATION_TOLERANCE" \
    'BEGIN {print (d<=t)?1:0}')"

  (( final_bytes == rendered_bytes )) || fail \
    "Returned size differs from rendered size for item $id"
  [[ "$final_duration_ok" == "1" ]] || fail \
    "Returned duration mismatch for item $id"

  echo "PASS $id"
  echo "  returned: $final_path"
  echo "  size:     $final_bytes bytes"
  echo "  duration delta: ${final_diff}s"
  echo

  ((passed+=1))
done

echo "============================================================"
echo "BATCH COMPLETE"
echo "Passed: $passed / ${#IDS[@]}"
echo "============================================================"
