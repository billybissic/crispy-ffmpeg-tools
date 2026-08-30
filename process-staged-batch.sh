#!/usr/bin/env bash
set -u

DB="${DB:-./media-processing.db}"
TARGET="${TARGET:-2.5G}"
SHRINK_SCRIPT="${SHRINK_SCRIPT:-./shrink-video.sh}"
DURATION_TOLERANCE="${DURATION_TOLERANCE:-5}"
ON_DURATION_MISMATCH="${ON_DURATION_MISMATCH:-stop}"
ON_EXISTING_OUTPUT="${ON_EXISTING_OUTPUT:-recover}"

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
  --on-duration-mismatch MODE
                          stop   = stop batch, preserve files (default)
                          skip   = mark FAILED, preserve files, continue
                          delete = mark FAILED, delete source + rejected render, continue
  --on-existing-output MODE
                           recover = reuse valid existing render (default)
                           stop    = stop if destination already exists
  -h, --help              Show help

Examples:
  ./process-staged-batch.sh 5 91 69 88 65
  ./process-staged-batch.sh --on-duration-mismatch delete 5 91 69 88 65

The script is sequential. Normal errors remain fail-fast. Duration mismatches
can optionally be recorded as FAILED and skipped. It never overwrites a destination.
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
    --on-existing-output)
      [[ $# -ge 2 ]] || fail "--on-existing-output requires recover or stop"
      ON_EXISTING_OUTPUT="$2"
      case "$ON_EXISTING_OUTPUT" in
        recover|stop) ;;
        *) fail "--on-existing-output must be recover or stop" ;;
      esac
      shift 2 ;;
    --on-duration-mismatch)
      [[ $# -ge 2 ]] || fail "--on-duration-mismatch requires stop, skip, or delete"
      ON_DURATION_MISMATCH="$2"
      case "$ON_DURATION_MISMATCH" in
        stop|skip|delete) ;;
        *) fail "--on-duration-mismatch must be stop, skip, or delete" ;;
      esac
      shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    -*)
      fail "Unknown option: $1" ;;
    *)
      IDS+=("$1"); shift ;;
  esac
done

(( ${#IDS[@]} > 0 )) || { usage; exit 2; }

for cmd in media-queue ffprobe stat awk dirname basename mv; do
  command -v "$cmd" >/dev/null 2>&1 || fail "Missing required command: $cmd"
done

[[ -x "$SHRINK_SCRIPT" ]] || fail "Shrink script is not executable: $SHRINK_SCRIPT"

echo "IDs:    ${IDS[*]}"
echo "DB:     $DB"
echo "Target: $TARGET"
echo "Duration mismatch policy: $ON_DURATION_MISMATCH"
echo "Existing output policy: $ON_EXISTING_OUTPUT"
echo

passed=0
recovered=0
failed_skipped=0

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

  staged_dir="$(dirname "$processing_path")"
  staged_filename="$(basename "$processing_path")"
  staged_name="${staged_filename%.*}"

  rendered_path="$staged_dir/${staged_name}.${TARGET}.H264.mkv"
  rendered_basename="$(basename "$rendered_path")"
  expected_destination="$source_dir/$rendered_basename"

  #
  # RECOVERY:
  # A previous/interrupted pipeline run may have already returned a valid
  # rendered file while leaving the original source IN_PROCESSING.
  #
  # If that destination exists, validate it before wasting time re-encoding.
  #
  if [[ -e "$expected_destination" ]]; then
    echo
    echo "[RECOVERY] Existing destination detected:"
    echo "           $expected_destination"

    [[ "$ON_EXISTING_OUTPUT" == "recover" ]] || fail \
      "Destination already exists; refusing to overwrite: $expected_destination"

    [[ -f "$expected_destination" ]] || fail \
      "Existing destination is not a regular file: $expected_destination"

    existing_duration="$(ffprobe -v error \
      -show_entries format=duration \
      -of default=noprint_wrappers=1:nokey=1 \
      "$expected_destination")" || fail \
      "Could not read existing destination duration: $expected_destination"

    existing_diff="$(awk \
      -v a="$staged_duration" \
      -v b="$existing_duration" \
      'BEGIN {d=a-b; if(d<0)d=-d; printf "%.3f", d}')"

    existing_ok="$(awk \
      -v d="$existing_diff" \
      -v t="$DURATION_TOLERANCE" \
      'BEGIN {print (d<=t)?1:0}')"

    echo "           source duration:   ${staged_duration}s"
    echo "           existing duration: ${existing_duration}s"
    echo "           duration delta:    ${existing_diff}s"

    #
    # Do NOT automatically delete or replace mismatched files.
    #
    if [[ "$existing_ok" != "1" ]]; then
      fail \
        "Existing destination duration mismatch for item $id; both files preserved"
    fi

    existing_bytes="$(stat -c%s "$expected_destination")"

    (( existing_bytes < staged_bytes )) || fail \
      "Existing destination is not smaller than source; both files preserved"

    #
    # If another rendered sidecar is already sitting in processing, this is
    # ambiguous. Preserve everything rather than choosing one automatically.
    #
    [[ ! -e "$rendered_path" ]] || fail \
      "Recovery render already exists in processing directory: $rendered_path"

    echo "           VALID existing render."
    echo "           Reusing instead of encoding."
    echo

    echo "[1/3] Moving existing render back into processing..."
    mv -- "$expected_destination" "$rendered_path" \
      || fail "Could not move existing render into processing directory"

    echo "[2/3] Adopting recovered render..."
    if ! media-queue --db "$DB" adopt-output "$id" "$rendered_path"; then

      # Best-effort rollback if DB adoption fails.
      if [[ -f "$rendered_path" && ! -e "$expected_destination" ]]; then
        mv -- "$rendered_path" "$expected_destination" || true
      fi

      fail "adopt-output failed during recovery for item $id"
    fi

    echo "[3/3] Returning recovered render..."
    media-queue --db "$DB" return-file "$id" \
      || fail "return-file failed during recovery for item $id"

    recovered_item="$(media-queue --db "$DB" show "$id")" \
      || fail "Could not verify recovered item $id"

    recovered_status="$(awk -F': ' \
      '$1=="status" {print $2; exit}' <<<"$recovered_item")"

    recovered_path="$(awk -F': ' \
      '$1=="original_path" {
        sub(/^original_path: /,"");
        print;
        exit
      }' <<<"$recovered_item")"

    [[ "$recovered_status" == "RETURNED" ]] || fail \
      "Item $id is $recovered_status after recovery; expected RETURNED"

    [[ "$recovered_path" == "$expected_destination" ]] || fail \
      "Recovered destination mismatch for item $id"

    [[ -f "$recovered_path" ]] || fail \
      "Recovered output missing: $recovered_path"

    recovered_duration="$(ffprobe -v error \
      -show_entries format=duration \
      -of default=noprint_wrappers=1:nokey=1 \
      "$recovered_path")" || fail \
      "Could not verify recovered duration"

    recovered_diff="$(awk \
      -v a="$staged_duration" \
      -v b="$recovered_duration" \
      'BEGIN {d=a-b; if(d<0)d=-d; printf "%.3f", d}')"

    recovered_ok="$(awk \
      -v d="$recovered_diff" \
      -v t="$DURATION_TOLERANCE" \
      'BEGIN {print (d<=t)?1:0}')"

    [[ "$recovered_ok" == "1" ]] || fail \
      "Recovered destination duration mismatch for item $id"

    echo
    echo "RECOVERED $id"
    echo "  returned: $recovered_path"
    echo "  size:     $existing_bytes bytes"
    echo "  duration delta: ${recovered_diff}s"
    echo

    ((passed+=1))
    ((recovered+=1))
    continue
  fi

  echo "Source: $processing_path"
  echo "Home:   $source_dir"
  echo

  echo "[1/5] Encoding..."

  "$SHRINK_SCRIPT" "$processing_path" "$TARGET" || fail "Encoder failed for item $id"

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

  if [[ "$duration_ok" != "1" ]]; then
    reason="Duration mismatch: source=${staged_duration}s output=${rendered_duration}s delta=${duration_diff}s; rejected encode"

    case "$ON_DURATION_MISMATCH" in
      stop)
        fail "Duration mismatch for item $id exceeds ${DURATION_TOLERANCE}s"
        ;;
      skip|delete)
        echo "      FAIL $id: $reason"
        media-queue --db "$DB" mark-failed "$id" --reason "$reason" \
          || fail "Could not mark item $id FAILED"

        if [[ "$ON_DURATION_MISMATCH" == "delete" ]]; then
          echo "      deleting corrupt/rejected files..."
          rm -f -- "$processing_path" "$rendered_path"
          echo "      deleted: $processing_path"
          echo "      deleted: $rendered_path"
        else
          echo "      files preserved for inspection"
        fi

        ((failed_skipped+=1))
        echo "SKIP $id"
        echo
        continue
        ;;
    esac
  fi

  (( rendered_bytes < staged_bytes )) || fail \
    "Rendered output for item $id is not smaller than source"

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
echo "Passed:         $passed"
echo "Recovered:      $recovered"
echo "Failed/skipped: $failed_skipped"
echo "Total IDs:      ${#IDS[@]}"
echo "============================================================"
