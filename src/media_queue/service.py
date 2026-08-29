from __future__ import annotations

import hashlib
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Iterable

from .db import connect, init_db, transaction

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".m2ts"}
FINGERPRINT_SAMPLE_SIZE = 1024 * 1024


def fingerprint_file(path: str | Path, sample_size: int = FINGERPRINT_SAMPLE_SIZE) -> str:
    """Fast content fingerprint using file size plus first/middle/last samples."""
    p = Path(path)
    size = p.stat().st_size
    digest = hashlib.sha256()
    digest.update(b"media-queue-qfp1\0")
    digest.update(str(size).encode("ascii"))
    digest.update(b"\0")

    if size == 0:
        return f"qfp1:{size}:{digest.hexdigest()}"

    offsets = [0]
    if size > sample_size:
        offsets.append(max(0, (size // 2) - (sample_size // 2)))
        offsets.append(max(0, size - sample_size))

    # De-duplicate offsets for very small files.
    seen: set[int] = set()
    with p.open("rb") as fh:
        for offset in offsets:
            if offset in seen:
                continue
            seen.add(offset)
            fh.seek(offset)
            block = fh.read(sample_size)
            digest.update(offset.to_bytes(8, "big", signed=False))
            digest.update(len(block).to_bytes(8, "big", signed=False))
            digest.update(block)

    return f"qfp1:{size}:{digest.hexdigest()}"


def _event(conn: sqlite3.Connection, queue_id: int, event_type: str, detail: str | None = None) -> None:
    conn.execute(
        "INSERT INTO processing_events(queue_id, event_type, detail) VALUES (?, ?, ?)",
        (queue_id, event_type, detail),
    )


def _processed_match(conn: sqlite3.Connection, fingerprint: str, pipeline: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM processed_files WHERE pipeline=? AND fingerprint=?",
        (pipeline, fingerprint),
    ).fetchone()


def is_already_processed(db_path: str, source: str | Path, pipeline: str = "default") -> bool:
    init_db(db_path)
    p = Path(source).expanduser().resolve()
    fp = fingerprint_file(p)
    with connect(db_path) as conn:
        row = _processed_match(conn, fp, pipeline)
        if row:
            conn.execute(
                "UPDATE processed_files SET path=?, last_seen_at=CURRENT_TIMESTAMP WHERE id=?",
                (str(p), row["id"]),
            )
            conn.commit()
            return True
    return False


def add_file(
    db_path: str,
    source: str,
    processing_dir: str,
    pipeline: str = "default",
    fingerprint: str | None = None,
) -> int | None:
    init_db(db_path)
    src = Path(source).expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(f"Source file not found: {src}")

    fp = fingerprint or fingerprint_file(src)
    processing_root = Path(processing_dir).expanduser().resolve()
    destination = processing_root / src.name

    with transaction(db_path) as conn:
        processed = _processed_match(conn, fp, pipeline)
        if processed:
            conn.execute(
                "UPDATE processed_files SET path=?, last_seen_at=CURRENT_TIMESTAMP WHERE id=?",
                (str(src), processed["id"]),
            )
            return None

        existing = conn.execute(
            "SELECT * FROM processing_queue WHERE original_path=?", (str(src),)
        ).fetchone()

        if existing is None:
            cur = conn.execute(
                """
                INSERT INTO processing_queue(
                    original_path, processing_path, pipeline, original_size_bytes, source_fingerprint
                ) VALUES (?, ?, ?, ?, ?)
                RETURNING id
                """,
                (str(src), str(destination), pipeline, src.stat().st_size, fp),
            )
            queue_id = int(cur.fetchone()[0])
        else:
            queue_id = int(existing["id"])
            # If a returned/failed path now contains different content, it is a new candidate.
            next_status = existing["status"]
            if existing["source_fingerprint"] != fp and existing["status"] in {"RETURNED", "FAILED"}:
                next_status = "READY_TO_MOVE"
            conn.execute(
                """
                UPDATE processing_queue
                SET processing_path=?, pipeline=?, original_size_bytes=?, source_fingerprint=?,
                    status=?, updated_at=CURRENT_TIMESTAMP,
                    last_error=CASE WHEN ?='READY_TO_MOVE' THEN NULL ELSE last_error END
                WHERE id=?
                """,
                (
                    str(destination), pipeline, src.stat().st_size, fp,
                    next_status, next_status, queue_id,
                ),
            )

        _event(conn, queue_id, "QUEUED", f"pipeline={pipeline}; destination={destination}; fingerprint={fp}")
        return queue_id


def scan_directory(
    db_path: str,
    root: str,
    processing_dir: str,
    min_size_bytes: int = 0,
    extensions: Iterable[str] = VIDEO_EXTENSIONS,
    pipeline: str = "default",
) -> tuple[int, int, int]:
    root_path = Path(root).expanduser().resolve()
    if not root_path.is_dir():
        raise NotADirectoryError(root_path)

    allowed = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in extensions}
    queued = 0
    already_processed = 0
    skipped_errors = 0

    for path in root_path.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in allowed:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            skipped_errors += 1
            continue
        if size < min_size_bytes:
            continue

        try:
            fp = fingerprint_file(path)
            queue_id = add_file(
                db_path, str(path), processing_dir,
                pipeline=pipeline, fingerprint=fp,
            )
            if queue_id is None:
                already_processed += 1
            else:
                queued += 1
        except (OSError, sqlite3.Error):
            skipped_errors += 1

    return queued, already_processed, skipped_errors


def list_items(db_path: str, status: str | None = None) -> list[sqlite3.Row]:
    init_db(db_path)
    with connect(db_path) as conn:
        if status:
            return conn.execute(
                "SELECT * FROM processing_queue WHERE status = ? ORDER BY original_size_bytes DESC, id",
                (status,),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM processing_queue ORDER BY original_size_bytes DESC, id"
        ).fetchall()


def list_footprints(db_path: str, pipeline: str | None = None) -> list[sqlite3.Row]:
    init_db(db_path)
    with connect(db_path) as conn:
        if pipeline:
            return conn.execute(
                "SELECT * FROM processed_files WHERE pipeline=? ORDER BY processed_at DESC, id DESC",
                (pipeline,),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM processed_files ORDER BY processed_at DESC, id DESC"
        ).fetchall()


def get_item(db_path: str, queue_id: int) -> sqlite3.Row:
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM processing_queue WHERE id = ?", (queue_id,)).fetchone()
        if row is None:
            raise KeyError(f"Queue item {queue_id} not found")
        return row


def move_to_processing(db_path: str, queue_id: int) -> Path:
    with transaction(db_path) as conn:
        row = conn.execute("SELECT * FROM processing_queue WHERE id = ?", (queue_id,)).fetchone()
        if row is None:
            raise KeyError(f"Queue item {queue_id} not found")
        if row["status"] != "READY_TO_MOVE":
            raise ValueError(f"Item {queue_id} is {row['status']}, expected READY_TO_MOVE")

        source = Path(row["original_path"])
        destination = Path(row["processing_path"])
        if not source.is_file():
            raise FileNotFoundError(source)
        if destination.exists():
            raise FileExistsError(f"Refusing to overwrite: {destination}")

        current_fp = fingerprint_file(source)
        if row["source_fingerprint"] and current_fp != row["source_fingerprint"]:
            raise ValueError("Source changed after it was queued; rescan before moving")

        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(source), str(destination))
            conn.execute(
                """
                UPDATE processing_queue
                SET status='IN_PROCESSING', moved_to_processing_at=CURRENT_TIMESTAMP,
                    updated_at=CURRENT_TIMESTAMP, last_error=NULL
                WHERE id=?
                """,
                (queue_id,),
            )
            _event(conn, queue_id, "MOVED_TO_PROCESSING", str(destination))
        except Exception as exc:
            conn.execute(
                "UPDATE processing_queue SET status='FAILED', last_error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (str(exc), queue_id),
            )
            _event(conn, queue_id, "MOVE_FAILED", str(exc))
            raise

        return destination


def mark_processed(db_path: str, queue_id: int) -> None:
    with transaction(db_path) as conn:
        row = conn.execute("SELECT * FROM processing_queue WHERE id=?", (queue_id,)).fetchone()
        if row is None:
            raise KeyError(f"Queue item {queue_id} not found")
        if row["status"] != "IN_PROCESSING":
            raise ValueError(f"Item {queue_id} is {row['status']}, expected IN_PROCESSING")

        processed_path = Path(row["processing_path"])
        if not processed_path.is_file():
            raise FileNotFoundError(processed_path)
        final_fp = fingerprint_file(processed_path)

        conn.execute(
            """
            UPDATE processing_queue
            SET status='READY_TO_RETURN', processed_at=CURRENT_TIMESTAMP,
                final_fingerprint=?, updated_at=CURRENT_TIMESTAMP, last_error=NULL
            WHERE id=?
            """,
            (final_fp, queue_id),
        )
        _event(conn, queue_id, "MARKED_PROCESSED", f"fingerprint={final_fp}")


def return_file(db_path: str, queue_id: int) -> Path:
    with transaction(db_path) as conn:
        row = conn.execute("SELECT * FROM processing_queue WHERE id=?", (queue_id,)).fetchone()
        if row is None:
            raise KeyError(f"Queue item {queue_id} not found")
        if row["status"] != "READY_TO_RETURN":
            raise ValueError(f"Item {queue_id} is {row['status']}, expected READY_TO_RETURN")

        source = Path(row["processing_path"])
        original = Path(row["original_path"])
        # Preserve the adopted/rendered basename, but return it to the source directory.
        destination = original.parent / source.name
        if not source.is_file():
            raise FileNotFoundError(source)
        if destination.exists():
            raise FileExistsError(f"Refusing to overwrite: {destination}")

        current_fp = fingerprint_file(source)
        if row["final_fingerprint"] and current_fp != row["final_fingerprint"]:
            raise ValueError("Processed file changed after mark-processed/adopt-output; adopt or mark it again before returning")

        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(source), str(destination))
            final_size = destination.stat().st_size
            final_fp = fingerprint_file(destination)
            conn.execute(
                """
                UPDATE processing_queue
                SET status='RETURNED', returned_at=CURRENT_TIMESTAMP,
                    original_path=?, final_fingerprint=?,
                    updated_at=CURRENT_TIMESTAMP, last_error=NULL
                WHERE id=?
                """,
                (str(destination), final_fp, queue_id),
            )
            conn.execute(
                """
                INSERT INTO processed_files(pipeline, fingerprint, path, size_bytes, queue_id)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(pipeline, fingerprint) DO UPDATE SET
                    path=excluded.path,
                    size_bytes=excluded.size_bytes,
                    queue_id=excluded.queue_id,
                    last_seen_at=CURRENT_TIMESTAMP
                """,
                (row["pipeline"], final_fp, str(destination), final_size, queue_id),
            )
            _event(
                conn,
                queue_id,
                "RETURNED",
                f"{source} -> {destination}; footprint={final_fp}",
            )
        except Exception as exc:
            conn.execute(
                "UPDATE processing_queue SET status='FAILED', last_error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (str(exc), queue_id),
            )
            _event(conn, queue_id, "RETURN_FAILED", str(exc))
            raise

        return destination



def media_duration(path: str | Path) -> float:
    """Return container duration in seconds using ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    value = result.stdout.strip()
    if not value or value == "N/A":
        raise ValueError(f"Could not determine duration: {path}")
    return float(value)


def adopt_output(db_path: str, queue_id: int, output_path: str, tolerance_seconds: float = 5.0) -> Path:
    """Adopt an external renderer's output as the staged file for a queue item.

    Supports IN_PROCESSING/READY_TO_RETURN and repairs RETURNED items that were
    accidentally returned before the rendered sidecar was adopted. The rendered
    output must match the source duration within tolerance_seconds.
    """
    rendered = Path(output_path).expanduser().resolve()
    if not rendered.is_file():
        raise FileNotFoundError(rendered)

    with transaction(db_path) as conn:
        row = conn.execute("SELECT * FROM processing_queue WHERE id=?", (queue_id,)).fetchone()
        if row is None:
            raise KeyError(f"Queue item {queue_id} not found")
        if row["status"] not in {"IN_PROCESSING", "READY_TO_RETURN", "RETURNED"}:
            raise ValueError(
                f"Item {queue_id} is {row['status']}, expected IN_PROCESSING, READY_TO_RETURN, or RETURNED"
            )

        staged = Path(row["processing_path"]).expanduser().resolve()
        original = Path(row["original_path"]).expanduser().resolve()

        # For an item already returned by mistake, the original is back at its source path.
        reference = staged if staged.is_file() else original
        if not reference.is_file():
            raise FileNotFoundError(
                f"Neither staged nor original reference exists: {staged} / {original}"
            )

        reference_duration = media_duration(reference)
        rendered_duration = media_duration(rendered)
        diff = abs(reference_duration - rendered_duration)
        if diff > tolerance_seconds:
            raise ValueError(
                f"Duration mismatch: source={reference_duration:.3f}s, "
                f"output={rendered_duration:.3f}s, diff={diff:.3f}s "
                f"(tolerance {tolerance_seconds:.3f}s)"
            )

        # If it was already returned incorrectly, remove its incorrect completed footprint
        # and move the returned original back into staging before replacing it.
        if row["status"] == "RETURNED":
            conn.execute("DELETE FROM processed_files WHERE queue_id=?", (queue_id,))
            if staged.exists():
                raise FileExistsError(f"Cannot repair RETURNED item; staging path already exists: {staged}")
            staged.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(original), str(staged))

        if rendered == staged:
            adopted = staged
        else:
            # The rendered file passed validation. Remove the staged source, but keep
            # the renderer's basename so the final library file visibly carries the
            # local-processing label (for example .2.5G.H264.mkv).
            if staged.exists():
                staged.unlink()
            adopted = rendered

        final_fp = fingerprint_file(adopted)
        conn.execute(
            """
            UPDATE processing_queue
            SET status='READY_TO_RETURN', processed_at=CURRENT_TIMESTAMP,
                returned_at=NULL, processing_path=?, final_fingerprint=?,
                updated_at=CURRENT_TIMESTAMP, last_error=NULL
            WHERE id=?
            """,
            (str(adopted), final_fp, queue_id),
        )
        _event(
            conn, queue_id, "ADOPTED_OUTPUT",
            f"adopted={adopted}; duration_diff={diff:.3f}s; fingerprint={final_fp}",
        )
        return adopted


def reprocess(db_path: str, queue_id: int) -> None:
    """Reset a completed RETURNED item so the current returned file can be processed again.

    This does not move any files. It removes the completed footprint, refreshes the
    source fingerprint/size from original_path, clears completion timestamps and the
    final fingerprint, and returns the item to READY_TO_MOVE.
    """
    with transaction(db_path) as conn:
        row = conn.execute("SELECT * FROM processing_queue WHERE id=?", (queue_id,)).fetchone()
        if row is None:
            raise KeyError(f"Queue item {queue_id} not found")
        if row["status"] != "RETURNED":
            raise ValueError(f"Item {queue_id} is {row['status']}, expected RETURNED")

        original = Path(row["original_path"]).expanduser().resolve()
        staged = Path(row["processing_path"]).expanduser().resolve()

        if not original.is_file():
            raise FileNotFoundError(original)
        if staged.exists():
            raise FileExistsError(
                f"Refusing to reprocess while staging path exists: {staged}"
            )

        source_fp = fingerprint_file(original)
        source_size = original.stat().st_size

        # Remove any completed-footprint record created by the prior run.
        conn.execute("DELETE FROM processed_files WHERE queue_id=?", (queue_id,))

        conn.execute(
            """
            UPDATE processing_queue
            SET status='READY_TO_MOVE',
                original_size_bytes=?,
                source_fingerprint=?,
                final_fingerprint=NULL,
                moved_to_processing_at=NULL,
                processed_at=NULL,
                returned_at=NULL,
                last_error=NULL,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (source_size, source_fp, queue_id),
        )
        _event(
            conn, queue_id, "REPROCESS_QUEUED",
            f"source={original}; size={source_size}; fingerprint={source_fp}",
        )


def mark_failed(db_path: str, queue_id: int, reason: str) -> None:
    """Move a queue item into the terminal FAILED state with an audit event.

    This function intentionally does not delete or move files. Filesystem cleanup
    is a separate caller decision so marking an item failed is never implicitly
    destructive.
    """
    reason = reason.strip()
    if not reason:
        raise ValueError("Failure reason must not be empty")

    with transaction(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM processing_queue WHERE id=?",
            (queue_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Queue item {queue_id} not found")
        if row["status"] == "RETURNED":
            raise ValueError(
                f"Item {queue_id} is RETURNED; use reprocess before marking a completed item failed"
            )

        conn.execute(
            """
            UPDATE processing_queue
            SET status='FAILED',
                last_error=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (reason, queue_id),
        )
        _event(conn, queue_id, "FAILED", reason)


def reset_failed(db_path: str, queue_id: int, status: str = "READY_TO_MOVE") -> None:
    if status not in {"READY_TO_MOVE", "IN_PROCESSING", "READY_TO_RETURN"}:
        raise ValueError("Invalid reset status")
    with transaction(db_path) as conn:
        row = conn.execute("SELECT status FROM processing_queue WHERE id=?", (queue_id,)).fetchone()
        if row is None:
            raise KeyError(f"Queue item {queue_id} not found")
        if row["status"] != "FAILED":
            raise ValueError(f"Item {queue_id} is not FAILED")
        conn.execute(
            "UPDATE processing_queue SET status=?, last_error=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, queue_id),
        )
        _event(conn, queue_id, "RESET", status)
