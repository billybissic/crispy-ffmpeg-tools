from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any


def probe_media(path: str | Path) -> dict[str, Any]:
    """Capture a full ffprobe snapshot suitable for later Flow enrichment."""
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(p)

    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_format",
            "-show_streams",
            "-show_chapters",
            "-of", "json",
            str(p),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _int_or_none(value: Any) -> int | None:
    if value in (None, "", "N/A"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if value in (None, "", "N/A"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _stream_language(stream: dict[str, Any]) -> str | None:
    tags = stream.get("tags") or {}
    return tags.get("language")


def _stream_title(stream: dict[str, Any]) -> str | None:
    tags = stream.get("tags") or {}
    return tags.get("title")


def ensure_media_asset(
    conn: sqlite3.Connection,
    *,
    queue_id: int | None,
    path: str | Path,
    fingerprint: str,
    source_fingerprint: str | None = None,
) -> int:
    p = Path(path).expanduser().resolve()
    source_fp = source_fingerprint or fingerprint

    row = None
    if queue_id is not None:
        row = conn.execute(
            "SELECT id FROM media_assets WHERE queue_id=?",
            (queue_id,),
        ).fetchone()

    # A probe-scan intentionally runs before a processing_queue row exists.
    # Match that discovery record later by original path + source fingerprint,
    # then attach the queue id when the file enters the processing workflow.
    if row is None:
        row = conn.execute(
            """
            SELECT id FROM media_assets
            WHERE original_path=? AND source_fingerprint=?
            ORDER BY id DESC LIMIT 1
            """,
            (str(p), source_fp),
        ).fetchone()

    if row is None:
        cur = conn.execute(
            """
            INSERT INTO media_assets(
                queue_id, source_fingerprint, current_fingerprint,
                original_path, current_path
            ) VALUES (?, ?, ?, ?, ?)
            RETURNING id
            """,
            (queue_id, source_fp, fingerprint, str(p), str(p)),
        )
        return int(cur.fetchone()[0])

    asset_id = int(row["id"])
    conn.execute(
        """
        UPDATE media_assets
        SET queue_id=COALESCE(queue_id, ?),
            current_fingerprint=?, current_path=?,
            source_fingerprint=COALESCE(source_fingerprint, ?),
            updated_at=CURRENT_TIMESTAMP,
            sync_status='PENDING'
        WHERE id=?
        """,
        (queue_id, fingerprint, str(p), source_fp, asset_id),
    )
    return asset_id


def store_probe_snapshot(
    conn: sqlite3.Connection,
    *,
    queue_id: int | None,
    path: str | Path,
    fingerprint: str,
    stage: str,
    source_fingerprint: str | None = None,
    probe: dict[str, Any] | None = None,
) -> int:
    stage = stage.upper()
    if stage not in {"SOURCE", "OUTPUT"}:
        raise ValueError(f"Invalid probe stage: {stage}")

    p = Path(path).expanduser().resolve()
    if probe is None:
        probe = probe_media(p)

    asset_id = ensure_media_asset(
        conn,
        queue_id=queue_id,
        path=p,
        fingerprint=fingerprint,
        source_fingerprint=source_fingerprint,
    )

    existing = conn.execute(
        """
        SELECT id FROM media_probe_snapshots
        WHERE media_asset_id=? AND stage=? AND fingerprint=?
        ORDER BY id DESC LIMIT 1
        """,
        (asset_id, stage, fingerprint),
    ).fetchone()
    if existing is not None:
        return int(existing["id"])

    fmt = probe.get("format") or {}
    raw_json = json.dumps(probe, sort_keys=True, separators=(",", ":"))

    cur = conn.execute(
        """
        INSERT INTO media_probe_snapshots(
            media_asset_id, queue_id, stage, path, fingerprint, size_bytes,
            duration_seconds, format_name, format_long_name, overall_bitrate,
            raw_probe_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id
        """,
        (
            asset_id,
            queue_id,
            stage,
            str(p),
            fingerprint,
            p.stat().st_size,
            _float_or_none(fmt.get("duration")),
            fmt.get("format_name"),
            fmt.get("format_long_name"),
            _int_or_none(fmt.get("bit_rate")),
            raw_json,
        ),
    )
    snapshot_id = int(cur.fetchone()[0])

    for stream in probe.get("streams") or []:
        disposition = stream.get("disposition") or {}
        conn.execute(
            """
            INSERT INTO media_streams(
                probe_snapshot_id, stream_index, stream_type,
                codec_name, codec_long_name, profile,
                width, height, pixel_format, frame_rate, bitrate,
                channels, channel_layout, sample_rate,
                language, title, disposition_default, disposition_forced,
                raw_stream_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                _int_or_none(stream.get("index")) or 0,
                stream.get("codec_type"),
                stream.get("codec_name"),
                stream.get("codec_long_name"),
                stream.get("profile"),
                _int_or_none(stream.get("width")),
                _int_or_none(stream.get("height")),
                stream.get("pix_fmt"),
                stream.get("avg_frame_rate") or stream.get("r_frame_rate"),
                _int_or_none(stream.get("bit_rate")),
                _int_or_none(stream.get("channels")),
                stream.get("channel_layout"),
                _int_or_none(stream.get("sample_rate")),
                _stream_language(stream),
                _stream_title(stream),
                _int_or_none(disposition.get("default")),
                _int_or_none(disposition.get("forced")),
                json.dumps(stream, sort_keys=True, separators=(",", ":")),
            ),
        )

    conn.execute(
        """
        INSERT INTO flow_enrichment_queue(media_asset_id, reason)
        VALUES (?, ?)
        ON CONFLICT(media_asset_id) DO NOTHING
        """,
        (asset_id, 'metadata captured during dry-run probe scan' if queue_id is None else 'metadata captured during media processing'),
    )

    return snapshot_id


def update_media_asset_location(
    conn: sqlite3.Connection,
    *,
    queue_id: int,
    path: str | Path,
    fingerprint: str,
) -> None:
    p = Path(path).expanduser().resolve()
    conn.execute(
        """
        UPDATE media_assets
        SET current_path=?, current_fingerprint=?,
            updated_at=CURRENT_TIMESTAMP, sync_status='PENDING'
        WHERE queue_id=?
        """,
        (str(p), fingerprint, queue_id),
    )


def metadata_stats(conn: sqlite3.Connection) -> dict[str, int]:
    queries = {
        "assets": "SELECT COUNT(*) FROM media_assets",
        "source_snapshots": "SELECT COUNT(*) FROM media_probe_snapshots WHERE stage='SOURCE'",
        "output_snapshots": "SELECT COUNT(*) FROM media_probe_snapshots WHERE stage='OUTPUT'",
        "streams": "SELECT COUNT(*) FROM media_streams",
        "flow_pending": "SELECT COUNT(*) FROM flow_enrichment_queue WHERE status='PENDING'",
        "flow_complete": "SELECT COUNT(*) FROM flow_enrichment_queue WHERE status='COMPLETE'",
    }
    return {name: int(conn.execute(sql).fetchone()[0]) for name, sql in queries.items()}
