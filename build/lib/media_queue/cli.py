from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .db import init_db
from .service import (
    VIDEO_EXTENSIONS,
    add_file,
    adopt_output,
    get_item,
    list_footprints,
    list_items,
    mark_processed,
    mark_failed,
    move_to_processing,
    reprocess,
    reset_failed,
    return_file,
    scan_directory,
)


def human_size(value: int | None) -> str:
    if value is None:
        return "-"
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.2f} {unit}"
        size /= 1024
    return str(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="media-queue")
    parser.add_argument("--db", default="media-processing.db", help="SQLite database path")
    parser.add_argument(
        "--claims-dir",
        default=os.environ.get("MEDIA_QUEUE_CLAIMS_DIR"),
        help="Shared claim directory for distributed queues (or MEDIA_QUEUE_CLAIMS_DIR)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Create/upgrade the SQLite database")

    p = sub.add_parser("add", help="Queue one file")
    p.add_argument("file")
    p.add_argument("--processing-dir", required=True)
    p.add_argument("--pipeline", default="default", help="Processing profile/footprint namespace")

    p = sub.add_parser("scan", help="Recursively queue matching media files")
    p.add_argument("root")
    p.add_argument("--processing-dir", required=True)
    p.add_argument("--min-size-gb", type=float, default=0)
    p.add_argument("--ext", action="append", help="Extension to include; repeatable")
    p.add_argument("--pipeline", default="default", help="Processing profile/footprint namespace")

    p = sub.add_parser("list", help="List queue items")
    p.add_argument("--status", choices=["READY_TO_MOVE", "IN_PROCESSING", "READY_TO_RETURN", "RETURNED", "FAILED", "SKIPPED"])

    p = sub.add_parser("footprints", help="List completed-file fingerprints")
    p.add_argument("--pipeline", help="Only show one processing profile")

    p = sub.add_parser("show", help="Show one queue item")
    p.add_argument("id", type=int)

    p = sub.add_parser("move-to-processing", help="Explicitly move one queued file")
    p.add_argument("id", type=int)

    p = sub.add_parser("mark-processed", help="Mark staged file ready to return")
    p.add_argument("id", type=int)

    p = sub.add_parser("adopt-output", help="Adopt a rendered sidecar as the processed staged file")
    p.add_argument("id", type=int)
    p.add_argument("output")
    p.add_argument("--duration-tolerance", type=float, default=5.0)

    p = sub.add_parser("return-file", help="Explicitly move one processed file back")
    p.add_argument("id", type=int)

    p = sub.add_parser("reprocess", help="Reset a RETURNED item to READY_TO_MOVE")
    p.add_argument("id", type=int)

    p = sub.add_parser("mark-failed", help="Mark a queue item FAILED without deleting files")
    p.add_argument("id", type=int)
    p.add_argument("--reason", required=True, help="Reason recorded in last_error and processing_events")

    p = sub.add_parser("reset-failed", help="Reset a failed item after inspection")
    p.add_argument("id", type=int)
    p.add_argument("--to", default="READY_TO_MOVE", choices=["READY_TO_MOVE", "IN_PROCESSING", "READY_TO_RETURN"])

    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        if args.command == "init":
            init_db(args.db)
            print(f"Initialized: {Path(args.db).resolve()}")

        elif args.command == "add":
            queue_id = add_file(args.db, args.file, args.processing_dir, pipeline=args.pipeline)
            if queue_id is None:
                print(f"Skipped: already processed for pipeline '{args.pipeline}'")
            else:
                print(f"Queued item {queue_id}")

        elif args.command == "scan":
            extensions = args.ext if args.ext else VIDEO_EXTENSIONS
            minimum = int(args.min_size_gb * 1024**3)
            queued, processed, errors = scan_directory(
                args.db, args.root, args.processing_dir, minimum, extensions,
                pipeline=args.pipeline,
            )
            print(f"Queued/updated: {queued}; already processed: {processed}; skipped/errors: {errors}")

        elif args.command == "list":
            rows = list_items(args.db, args.status)
            print(f"{'ID':>5}  {'STATUS':<17} {'SIZE':>11}  {'PIPELINE':<16} ORIGINAL")
            for row in rows:
                print(
                    f"{row['id']:>5}  {row['status']:<17} "
                    f"{human_size(row['original_size_bytes']):>11}  {row['pipeline']:<16} {row['original_path']}"
                )

        elif args.command == "footprints":
            rows = list_footprints(args.db, args.pipeline)
            print(f"{'ID':>5}  {'SIZE':>11}  {'PIPELINE':<16} PATH")
            for row in rows:
                print(f"{row['id']:>5}  {human_size(row['size_bytes']):>11}  {row['pipeline']:<16} {row['path']}")

        elif args.command == "show":
            row = get_item(args.db, args.id)
            for key in row.keys():
                print(f"{key}: {row[key]}")

        elif args.command == "move-to-processing":
            path = move_to_processing(args.db, args.id, args.claims_dir)
            if path is None:
                print(f"SKIPPED: item {args.id} is already claimed by another processing node")
                raise SystemExit(3)
            print(f"Moved to: {path}")

        elif args.command == "mark-processed":
            mark_processed(args.db, args.id)
            print(f"Item {args.id} marked READY_TO_RETURN")

        elif args.command == "adopt-output":
            path = adopt_output(args.db, args.id, args.output, args.duration_tolerance)
            print(f"Adopted rendered output: {path}")
            print(f"Item {args.id} marked READY_TO_RETURN")

        elif args.command == "return-file":
            path = return_file(args.db, args.id)
            print(f"Returned to: {path}")

        elif args.command == "reprocess":
            reprocess(args.db, args.id)
            print(f"Item {args.id} reset to READY_TO_MOVE for reprocessing")

        elif args.command == "mark-failed":
            mark_failed(args.db, args.id, args.reason)
            print(f"Item {args.id} marked FAILED")
            print(f"Reason: {args.reason}")

        elif args.command == "reset-failed":
            reset_failed(args.db, args.id, args.to)
            print(f"Item {args.id} reset to {args.to}")

    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
