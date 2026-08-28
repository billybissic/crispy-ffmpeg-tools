from pathlib import Path

from media_queue.db import init_db
from media_queue.service import add_file, get_item, mark_processed, move_to_processing, return_file


def test_manual_move_roundtrip(tmp_path: Path):
    db = tmp_path / "queue.db"
    source_dir = tmp_path / "source"
    processing_dir = tmp_path / "processing"
    source_dir.mkdir()
    processing_dir.mkdir()

    source = source_dir / "sample.mkv"
    source.write_bytes(b"sample")

    init_db(db)
    queue_id = add_file(str(db), str(source), str(processing_dir))
    assert get_item(str(db), queue_id)["status"] == "READY_TO_MOVE"

    moved = move_to_processing(str(db), queue_id)
    assert moved.exists()
    assert not source.exists()
    assert get_item(str(db), queue_id)["status"] == "IN_PROCESSING"

    mark_processed(str(db), queue_id)
    assert get_item(str(db), queue_id)["status"] == "READY_TO_RETURN"

    returned = return_file(str(db), queue_id)
    assert returned == source
    assert source.exists()
    assert get_item(str(db), queue_id)["status"] == "RETURNED"


def test_returned_file_leaves_footprint_and_rescan_skips(tmp_path: Path):
    from media_queue.service import scan_directory, list_footprints

    db = tmp_path / "queue.db"
    source_dir = tmp_path / "source"
    processing_dir = tmp_path / "processing"
    source_dir.mkdir()
    processing_dir.mkdir()

    source = source_dir / "movie.mkv"
    source.write_bytes(b"original-content" * 1024)

    queue_id = add_file(str(db), str(source), str(processing_dir), pipeline="shrink-v1")
    move_to_processing(str(db), queue_id)

    staged = processing_dir / source.name
    staged.write_bytes(b"processed-content" * 512)
    mark_processed(str(db), queue_id)
    return_file(str(db), queue_id)

    footprints = list_footprints(str(db), "shrink-v1")
    assert len(footprints) == 1

    queued, processed, errors = scan_directory(
        str(db), str(source_dir), str(processing_dir), pipeline="shrink-v1"
    )
    assert (queued, processed, errors) == (0, 1, 0)

    renamed = source_dir / "renamed.mkv"
    source.rename(renamed)
    queued, processed, errors = scan_directory(
        str(db), str(source_dir), str(processing_dir), pipeline="shrink-v1"
    )
    assert (queued, processed, errors) == (0, 1, 0)


def test_different_pipeline_can_requeue_returned_file(tmp_path: Path):
    from media_queue.service import scan_directory

    db = tmp_path / "queue.db"
    source_dir = tmp_path / "source"
    processing_dir = tmp_path / "processing"
    source_dir.mkdir()
    processing_dir.mkdir()

    source = source_dir / "movie.mkv"
    source.write_bytes(b"original" * 1024)

    queue_id = add_file(str(db), str(source), str(processing_dir), pipeline="shrink-v1")
    move_to_processing(str(db), queue_id)
    staged = processing_dir / source.name
    staged.write_bytes(b"processed" * 512)
    mark_processed(str(db), queue_id)
    return_file(str(db), queue_id)

    queued, processed, errors = scan_directory(
        str(db), str(source_dir), str(processing_dir), pipeline="widescreen-v1"
    )
    assert queued == 1
    assert processed == 0
    assert errors == 0


def test_reprocess_returned_item_resets_footprint_and_state(tmp_path: Path):
    from media_queue.service import list_footprints, reprocess

    db = tmp_path / "queue.db"
    source_dir = tmp_path / "source"
    processing_dir = tmp_path / "processing"
    source_dir.mkdir()
    processing_dir.mkdir()

    source = source_dir / "movie.mkv"
    source.write_bytes(b"original-content" * 1024)

    queue_id = add_file(str(db), str(source), str(processing_dir), pipeline="shrink-v1")
    move_to_processing(str(db), queue_id)
    staged = processing_dir / source.name
    staged.write_bytes(b"processed-content" * 512)
    mark_processed(str(db), queue_id)
    return_file(str(db), queue_id)

    assert get_item(str(db), queue_id)["status"] == "RETURNED"
    assert len(list_footprints(str(db), "shrink-v1")) == 1

    returned_size = source.stat().st_size
    reprocess(str(db), queue_id)

    row = get_item(str(db), queue_id)
    assert row["status"] == "READY_TO_MOVE"
    assert row["original_size_bytes"] == returned_size
    assert row["final_fingerprint"] is None
    assert row["moved_to_processing_at"] is None
    assert row["processed_at"] is None
    assert row["returned_at"] is None
    assert row["last_error"] is None
    assert len(list_footprints(str(db), "shrink-v1")) == 0


def test_adopt_output_preserves_rendered_basename_on_return(tmp_path: Path, monkeypatch):
    from media_queue.service import adopt_output

    db = tmp_path / "queue.db"
    source_dir = tmp_path / "source"
    processing_dir = tmp_path / "processing"
    source_dir.mkdir()
    processing_dir.mkdir()

    source = source_dir / "movie.mkv"
    source.write_bytes(b"original-content" * 1024)

    queue_id = add_file(str(db), str(source), str(processing_dir), pipeline="shrink-v1")
    move_to_processing(str(db), queue_id)

    rendered = processing_dir / "movie.2.5G.H264.mkv"
    rendered.write_bytes(b"processed-content" * 512)

    # Unit test the queue contract without requiring a real media container/ffprobe.
    monkeypatch.setattr("media_queue.service.media_duration", lambda path: 100.0)

    adopted = adopt_output(str(db), queue_id, str(rendered))
    assert adopted == rendered.resolve()
    assert rendered.exists()
    assert not (processing_dir / "movie.mkv").exists()

    row = get_item(str(db), queue_id)
    assert row["status"] == "READY_TO_RETURN"
    assert Path(row["processing_path"]).name == "movie.2.5G.H264.mkv"

    returned = return_file(str(db), queue_id)
    expected = source_dir / "movie.2.5G.H264.mkv"
    assert returned == expected
    assert expected.exists()
    assert not source.exists()

    row = get_item(str(db), queue_id)
    assert row["status"] == "RETURNED"
    assert Path(row["original_path"]) == expected
