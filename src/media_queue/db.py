from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

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
        'FAILED'
    ))
);

CREATE TABLE IF NOT EXISTS processing_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (queue_id) REFERENCES processing_queue(id) ON DELETE CASCADE
);

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
);

CREATE INDEX IF NOT EXISTS idx_processing_queue_status
ON processing_queue(status);

CREATE INDEX IF NOT EXISTS idx_processed_files_path
ON processed_files(path);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    db_path = Path(db_path).expanduser().resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db(db_path: str | Path) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        # Small forward migrations for databases created by the starter v0.1.
        _ensure_column(conn, "processing_queue", "pipeline", "TEXT NOT NULL DEFAULT 'default'")
        _ensure_column(conn, "processing_queue", "source_fingerprint", "TEXT")
        _ensure_column(conn, "processing_queue", "final_fingerprint", "TEXT")


@contextmanager
def transaction(db_path: str | Path):
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
