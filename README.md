# Media Processing Queue Starter

A small SQLite-backed service/CLI for staging media files into a processing directory and returning them afterward.

## Design for this version

- SQLite owns queue state, audit history, and completed-file footprints.
- Scanning/adding files does **not** move anything.
- File movement only happens through explicit CLI commands.
- Processing itself is external for now (FFmpeg scripts, HandBrake, etc.).
- No daemon, watcher, cron, or automatic movement yet.
- Moves refuse to overwrite an existing destination.
- Successfully returned files leave a persistent content footprint so future scans do not encode them again.

## Status flow

```text
READY_TO_MOVE
    |  media-queue move-to-processing ID
    v
IN_PROCESSING
    |  external processing + media-queue mark-processed ID
    v
READY_TO_RETURN
    |  media-queue return-file ID
    v
RETURNED + footprint recorded
```

Any move failure is recorded as `FAILED` with `last_error` and an audit event.

## Footprints: avoiding repeat encodes

The service creates a fast fingerprint from:

- exact file size
- SHA-256 over samples from the beginning, middle, and end of the file

Only a few MiB are read even for very large media files. The fingerprint is stored in the `processed_files` ledger after a successful return.

On later scans, an identical file is skipped even if it has been renamed or moved.

Footprints are **pipeline-aware**. Use a stable pipeline name for a particular processing recipe:

```bash
--pipeline shrink-h264-v1
```

A file completed under `shrink-h264-v1` will be skipped on later `shrink-h264-v1` scans, but it can still be queued for a genuinely different pipeline such as `widescreen-v1`.

## Install for development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Initialize

```bash
media-queue --db ./media-processing.db init
```

## Queue one file (no movement)

```bash
media-queue --db ./media-processing.db add \
  "/mnt/rdisk/Movies/movie.mkv" \
  --processing-dir "/mnt/processing" \
  --pipeline shrink-h264-v1
```

## Scan a directory

Queue media at least 4 GiB:

```bash
media-queue --db ./media-processing.db scan \
  /mnt/rdisk/Movies \
  --processing-dir /mnt/processing \
  --min-size-gb 4 \
  --pipeline shrink-h264-v1
```

Example result on a later pass:

```text
Queued/updated: 12; already processed: 83; skipped/errors: 0
```

Default extensions:

```text
.mkv .mp4 .avi .m4v .mov .wmv .ts .m2ts
```

## Inspect the queue

```bash
media-queue --db ./media-processing.db list
media-queue --db ./media-processing.db list --status READY_TO_MOVE
media-queue --db ./media-processing.db show 12
```

Inspect completed footprints:

```bash
media-queue --db ./media-processing.db footprints
media-queue --db ./media-processing.db footprints --pipeline shrink-h264-v1
```

## Explicit movement commands

Move one file into processing:

```bash
media-queue --db ./media-processing.db move-to-processing 12
```

After your external FFmpeg/HandBrake workflow finishes:

```bash
media-queue --db ./media-processing.db mark-processed 12
```

This fingerprints the processed file while it is still in the processing directory.

Return it to its original path:

```bash
media-queue --db ./media-processing.db return-file 12
```

The return command verifies the processed file has not changed since `mark-processed`, moves it back, and records the permanent footprint.

## Safety checks

- A queued source is fingerprinted before movement. If it changes between scan and move, movement is refused and you must rescan.
- A processed file is fingerprinted by `mark-processed`. If it changes before return, return is refused until it is marked again.
- Existing destination files are never overwritten.
- Completed footprints survive repeated scans and path renames as long as the SQLite database is retained.

## Failure recovery

Inspect first:

```bash
media-queue --db ./media-processing.db show 12
```

Then reset only after deciding which state is correct:

```bash
media-queue --db ./media-processing.db reset-failed 12 --to READY_TO_MOVE
```

## SQLite tables

### `processing_queue`

Important columns:

- `original_path`
- `processing_path`
- `pipeline`
- `status`
- `original_size_bytes`
- `source_fingerprint`
- `final_fingerprint`
- timestamps for move/process/return
- `last_error`

### `processed_files`

Permanent completed-file ledger:

- `pipeline`
- `fingerprint`
- last known `path`
- final `size_bytes`
- source `queue_id`
- processed/last-seen timestamps

### `processing_events`

Append-only audit trail for queue and movement events.

## Next steps after manual testing

1. Add `move-ready --limit 1`, then `--limit 10`, then `--all`.
2. Output duration/codec verification before `mark-processed`.
3. Automatic move worker after the manual workflow is proven.
4. Processor command integration.
5. Automatic return worker.
6. Retry policy and stale-job recovery.

## Rendered filename return behavior

When an external renderer creates a sidecar such as:

```text
/mnt/processing/movie.2.5G.H264.mkv
```

adopt it with:

```bash
media-queue --db ./media-processing.db adopt-output 23 \
  /mnt/processing/movie.2.5G.H264.mkv
```

`adopt-output` validates the duration, removes the staged source copy, preserves the rendered basename, and updates the queue's processing path. A subsequent:

```bash
media-queue --db ./media-processing.db return-file 23
```

returns the file to the original directory as:

```text
<original-directory>/movie.2.5G.H264.mkv
```

The command refuses to overwrite an existing destination file. After return, the queue's `original_path` is updated to the rendered file's actual returned path so `reprocess` operates on the current library file.
