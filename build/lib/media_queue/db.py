from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


QUEUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS processing_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    original_path TEXT NOT NULL UNIQUE,
    processing_path TEXT NOT NULL,
    pipeline TEXT NOT NULL DEFAULT 'default',
    status TEXT NOT NULL DEFAULT 'READY_TO_MOVE',
    original_size_bytes INTEGER,
    source_fingerprint TEXT,
    final_fingerprint TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    moved_to_processing_at TEXT,
    processed_at TEXT,
    returned_at TEXT,
    last_error TEXT,
    CHECK (status IN (
        'READY_TO_MOVE',
        'IN_PROCESSING',
        'READY_TO_RETURN',
        'RETURNED',
        'FAILED',
        'SKIPPED'
    ))
)
"""

EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS processing_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (queue_id) REFERENCES processing_queue(id) ON DELETE CASCADE
)
"""

PROCESSED_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pipeline TEXT NOT NULL DEFAULT 'default',
    fingerprint TEXT NOT NULL,
    path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    queue_id INTEGER,
    processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (pipeline, fingerprint),
    FOREIGN KEY (queue_id) REFERENCES processing_queue(id) ON DELETE SET NULL
)
"""


def connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _queue_supports_skipped(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='processing_queue'"
    ).fetchone()
    return row is None or "'SKIPPED'" in (row["sql"] or "")


def _migrate_queue_for_skipped(conn: sqlite3.Connection) -> None:
    """Rebuild processing_queue so its status CHECK accepts SKIPPED."""
    if _queue_supports_skipped(conn):
        return

    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            CREATE TABLE processing_queue_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_path TEXT NOT NULL UNIQUE,
                processing_path TEXT NOT NULL,
                pipeline TEXT NOT NULL DEFAULT 'default',
                status TEXT NOT NULL DEFAULT 'READY_TO_MOVE',
                original_size_bytes INTEGER,
                source_fingerprint TEXT,
                final_fingerprint TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                moved_to_processing_at TEXT,
                processed_at TEXT,
                returned_at TEXT,
                last_error TEXT,
                CHECK (status IN (
                    'READY_TO_MOVE',
                    'IN_PROCESSING',
                    'READY_TO_RETURN',
                    'RETURNED',
                    'FAILED',
                    'SKIPPED'
                ))
            )
            """
        )
        conn.execute(
            """
            INSERT INTO processing_queue_new (
                id, original_path, processing_path, pipeline, status,
                original_size_bytes, source_fingerprint, final_fingerprint,
                created_at, updated_at, moved_to_processing_at, processed_at,
                returned_at, last_error
            )
            SELECT
                id, original_path, processing_path, pipeline, status,
                original_size_bytes, source_fingerprint, final_fingerprint,
                created_at, updated_at, moved_to_processing_at, processed_at,
                returned_at, last_error
            FROM processing_queue
            """
        )
        conn.execute("DROP TABLE processing_queue")
        conn.execute("ALTER TABLE processing_queue_new RENAME TO processing_queue")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_processing_queue_status ON processing_queue(status)"
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def init_db(db_path: str) -> None:
    with connect(db_path) as conn:
        conn.execute(QUEUE_SCHEMA)
        conn.execute(EVENTS_SCHEMA)
        conn.execute(PROCESSED_SCHEMA)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_processing_queue_status ON processing_queue(status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_processed_files_path ON processed_files(path)"
        )
        conn.commit()
        _migrate_queue_for_skipped(conn)


@contextmanager
def transaction(db_path: str) -> Iterator[sqlite3.Connection]:
    init_db(db_path)
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
