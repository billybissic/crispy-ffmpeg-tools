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


MEDIA_METADATA_SCHEMA = """
CREATE TABLE IF NOT EXISTS media_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_id INTEGER UNIQUE,
    source_fingerprint TEXT,
    current_fingerprint TEXT,
    original_path TEXT,
    current_path TEXT,
    discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sync_status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK(sync_status IN ('PENDING', 'SYNCED', 'ERROR')),
    synced_at TEXT,
    sync_version INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (queue_id) REFERENCES processing_queue(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS media_probe_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_asset_id INTEGER NOT NULL,
    queue_id INTEGER,
    stage TEXT NOT NULL CHECK(stage IN ('SOURCE', 'OUTPUT')),
    path TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    duration_seconds REAL,
    format_name TEXT,
    format_long_name TEXT,
    overall_bitrate INTEGER,
    raw_probe_json TEXT NOT NULL,
    captured_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (media_asset_id) REFERENCES media_assets(id) ON DELETE CASCADE,
    FOREIGN KEY (queue_id) REFERENCES processing_queue(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS media_streams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    probe_snapshot_id INTEGER NOT NULL,
    stream_index INTEGER NOT NULL,
    stream_type TEXT,
    codec_name TEXT,
    codec_long_name TEXT,
    profile TEXT,
    width INTEGER,
    height INTEGER,
    pixel_format TEXT,
    frame_rate TEXT,
    bitrate INTEGER,
    channels INTEGER,
    channel_layout TEXT,
    sample_rate INTEGER,
    language TEXT,
    title TEXT,
    disposition_default INTEGER,
    disposition_forced INTEGER,
    raw_stream_json TEXT NOT NULL,
    FOREIGN KEY (probe_snapshot_id) REFERENCES media_probe_snapshots(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS flow_enrichment_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_asset_id INTEGER NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK(status IN ('PENDING', 'ANALYZING', 'COMPLETE', 'FAILED')),
    priority INTEGER NOT NULL DEFAULT 100,
    reason TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (media_asset_id) REFERENCES media_assets(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS media_flow_metadata (
    media_asset_id INTEGER PRIMARY KEY,
    stimulation_score REAL,
    cognitive_load_score REAL,
    dialogue_density REAL,
    rewatch_friendliness REAL,
    preferred_phases_json TEXT,
    vibe_tags_json TEXT,
    analysis_version TEXT,
    confidence_score REAL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sync_status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK(sync_status IN ('PENDING', 'SYNCED', 'ERROR')),
    synced_at TEXT,
    sync_version INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (media_asset_id) REFERENCES media_assets(id) ON DELETE CASCADE
);
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



def _media_assets_support_probe_scan(conn: sqlite3.Connection) -> bool:
    """Return True when media_assets.queue_id is nullable for dry-run discovery."""
    rows = conn.execute("PRAGMA table_info(media_assets)").fetchall()
    if not rows:
        return True
    for row in rows:
        if row["name"] == "queue_id":
            return not bool(row["notnull"])
    return False


def _migrate_media_assets_for_probe_scan(conn: sqlite3.Connection) -> None:
    """Allow media assets to exist before they become processing_queue items."""
    if _media_assets_support_probe_scan(conn):
        return

    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            CREATE TABLE media_assets_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                queue_id INTEGER UNIQUE,
                source_fingerprint TEXT,
                current_fingerprint TEXT,
                original_path TEXT,
                current_path TEXT,
                discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                sync_status TEXT NOT NULL DEFAULT 'PENDING'
                    CHECK(sync_status IN ('PENDING', 'SYNCED', 'ERROR')),
                synced_at TEXT,
                sync_version INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (queue_id) REFERENCES processing_queue(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            INSERT INTO media_assets_new(
                id, queue_id, source_fingerprint, current_fingerprint,
                original_path, current_path, discovered_at, updated_at,
                sync_status, synced_at, sync_version
            )
            SELECT
                id, queue_id, source_fingerprint, current_fingerprint,
                original_path, current_path, discovered_at, updated_at,
                sync_status, synced_at, sync_version
            FROM media_assets
            """
        )
        conn.execute("DROP TABLE media_assets")
        conn.execute("ALTER TABLE media_assets_new RENAME TO media_assets")
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
        conn.executescript(MEDIA_METADATA_SCHEMA)
        _migrate_media_assets_for_probe_scan(conn)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_processing_queue_status ON processing_queue(status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_processed_files_path ON processed_files(path)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_media_assets_source_fingerprint ON media_assets(source_fingerprint)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_media_probe_asset_stage ON media_probe_snapshots(media_asset_id, stage)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_media_probe_fingerprint ON media_probe_snapshots(fingerprint)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_media_stream_probe ON media_streams(probe_snapshot_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_flow_enrichment_status ON flow_enrichment_queue(status, priority, id)"
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
