from pathlib import Path

import pytest

from media_queue.db import connect, init_db
from media_queue.service import add_file, move_to_processing


def test_schema_migrates_to_accept_skipped(tmp_path: Path):
    db = tmp_path / "q.db"
    init_db(str(db))
    with connect(str(db)) as conn:
        conn.execute(
            """
            INSERT INTO processing_queue(original_path, processing_path, status)
            VALUES('/a', '/b', 'SKIPPED')
            """
        )
        conn.commit()
        row = conn.execute(
            "SELECT status FROM processing_queue WHERE original_path='/a'"
        ).fetchone()
        assert row["status"] == "SKIPPED"


def test_shared_claim_allows_only_one_node(tmp_path: Path):
    claims = tmp_path / "claims"
    processing_a = tmp_path / "processing-a"
    processing_b = tmp_path / "processing-b"

    # Simulate two nodes having stale independent queue DBs for the same source.
    source_a = tmp_path / "node-a" / "movie.mkv"
    source_b = tmp_path / "node-b" / "movie.mkv"
    source_a.parent.mkdir()
    source_b.parent.mkdir()
    payload = b"x" * (1024 * 1024 + 19)
    source_a.write_bytes(payload)
    source_b.write_bytes(payload)

    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"
    id_a = add_file(str(db_a), str(source_a), str(processing_a), pipeline="p")
    id_b = add_file(str(db_b), str(source_b), str(processing_b), pipeline="p")
    assert id_a is not None and id_b is not None

    moved = move_to_processing(str(db_a), id_a, str(claims))
    assert moved.is_file()

    skipped = move_to_processing(str(db_b), id_b, str(claims))
    assert skipped is None

    with connect(str(db_b)) as conn:
        row = conn.execute("SELECT status FROM processing_queue WHERE id=?", (id_b,)).fetchone()
        assert row["status"] == "SKIPPED"
