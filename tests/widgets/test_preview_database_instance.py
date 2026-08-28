import os
import sqlite3
import threading
from pathlib import Path
from time import perf_counter

import numpy as np
import pytest
from PyQt6 import QtWidgets as qtw

from qplot.datahandling import readonly as readonly_module
from qplot.datahandling.file_identity import DatabaseInstance, database_instance
from qplot.datahandling.readonly import (
    DatabaseInstanceChangedError,
    replacement_wal_is_quarantined,
    sqlite_read_only_connection,
)
from qplot.datahandling.trusted_presentation import (
    TRUSTED_PRESENTATION_MAX_ERROR_BYTES,
)
from qplot.testdata import RunSpecification, generate_database
from qplot.windows._widgets import preview as preview_module
from qplot.windows._widgets.preview import (
    DraggablePreviewImageLabel,
    PreviewCard,
    PreviewTab,
    generate_run_previews,
)

_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_WINDOWS_OPEN_SIDECAR_REPLACEMENT_UNAVAILABLE = pytest.mark.skipif(
    os.name == "nt",
    reason="Windows does not permit replacing an open SQLite WAL/SHM sidecar.",
)


class _RecordingThreadPool:
    def __init__(self):
        self.started = []

    def start(self, worker):
        self.started.append(worker)


def _preview_database(path: Path, seed: int):
    specification = RunSpecification(
        dimensions=1,
        measured_name="current",
        measured_label="Current",
        measured_unit="nA",
        v_sd_start=-1.0,
        v_sd_stop=1.0,
        v_sd_points=5,
    )
    generate_database(
        [specification],
        path,
        rng=np.random.default_rng(seed),
    )

    connection = sqlite_read_only_connection(path)
    try:
        run_id, guid, table_name, run_description = connection.execute(
            "SELECT run_id, guid, result_table_name, run_description FROM runs"
        ).fetchone()
        columns = tuple(
            row[1]
            for row in connection.execute(
                f'PRAGMA table_info("{table_name}")'
            )
        )
        axis = next(column for column in columns if column.lower() == "v_sd")
        values = tuple(
            row[0]
            for row in connection.execute(
                f'SELECT current FROM "{table_name}" ORDER BY id'
            )
        )
    finally:
        connection.close()

    metadata = {
        "run_id": run_id,
        "guid": guid,
        "result_table_name": table_name,
        "result_count": len(values),
        "run_description": run_description,
        "measure_parameters": ["current"],
        "sweep_parameters": [axis],
    }
    return metadata, columns, values


def _artifact_state(database_path: Path):
    state = {}
    for suffix in ("", *_SQLITE_SIDECAR_SUFFIXES):
        artifact_path = Path(f"{database_path}{suffix}")
        if not artifact_path.exists():
            state[suffix] = None
            continue
        artifact_stat = artifact_path.stat()
        state[suffix] = (
            artifact_path.read_bytes(),
            artifact_stat.st_dev,
            artifact_stat.st_ino,
            artifact_stat.st_nlink,
            artifact_stat.st_size,
            artifact_stat.st_mtime_ns,
        )
    return state


def _start_selected_preview(database_source, metadata):
    preview = PreviewTab(preview_size=40)
    thread_pool = _RecordingThreadPool()
    preview.thread_pool = thread_pool
    preview.set_database_runs(
        database_source,
        {metadata["run_id"]: metadata},
    )
    preview.set_current_run(
        type("Dataset", (), {"guid": metadata["guid"]})()
    )
    return preview, thread_pool.started[0]


def _open_wal_source(database_path: Path):
    writer = sqlite3.connect(database_path)
    assert writer.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
    writer.execute("PRAGMA wal_autocheckpoint = 0")
    writer.execute("UPDATE runs SET name = name WHERE run_id = 1")
    writer.commit()
    assert Path(f"{database_path}-wal").is_file()
    assert Path(f"{database_path}-shm").is_file()
    return writer


def _replace_sidecar(database_path: Path, suffix: str):
    sidecar_path = Path(f"{database_path}{suffix}")
    old_identity = (sidecar_path.stat().st_dev, sidecar_path.stat().st_ino)
    replacement_path = Path(f"{database_path}{suffix}.replacement")
    replacement_path.write_bytes(sidecar_path.read_bytes())
    os.replace(replacement_path, sidecar_path)
    new_identity = (sidecar_path.stat().st_dev, sidecar_path.stat().st_ino)
    assert new_identity != old_identity


def _assert_replaced_preview_published_nothing(preview, ready):
    assert ready == []
    assert preview.cache == {}
    assert preview.thumbnail_cache == {}
    assert preview.errors == {}
    assert preview.thumbnail_errors == {}
    assert preview.current_guid is None
    assert preview.database_instance is None


def test_preview_binding_promotes_first_sidecar_then_rejects_its_replacement(
    tmp_path,
    monkeypatch,
):
    logical_path = str(tmp_path / "promoted-sidecar.db")
    accepted = DatabaseInstance(logical_path, logical_path, (1, 7))
    first_sidecar = DatabaseInstance(
        logical_path,
        logical_path,
        (1, 7),
        sidecar_identities=frozenset({(2, 11)}),
    )
    replacement = DatabaseInstance(
        logical_path,
        logical_path,
        (1, 7),
        sidecar_identities=frozenset({(2, 12)}),
    )
    observations = iter((first_sidecar, replacement))
    quarantined = []
    monkeypatch.setattr(
        preview_module,
        "capture_database_instance",
        lambda _path: next(observations),
    )
    monkeypatch.setattr(
        preview_module,
        "quarantine_wal_for_replaced_database",
        quarantined.append,
    )
    binding = preview_module._PreviewDatabaseBinding(accepted)

    assert binding.require_current() == first_sidecar
    with pytest.raises(DatabaseInstanceChangedError):
        binding.require_current()

    assert binding.database_instance == first_sidecar
    assert quarantined == [logical_path]


@pytest.mark.parametrize("sidecar_suffix", ["-wal", "-shm"])
@_WINDOWS_OPEN_SIDECAR_REPLACEMENT_UNAVAILABLE
def test_preview_rejects_sidecar_swap_before_sqlite_open(
    tmp_path,
    monkeypatch,
    sidecar_suffix,
):
    database_path = tmp_path / "before-open.db"
    metadata, _columns, _values = _preview_database(database_path, seed=11)
    writer = _open_wal_source(database_path)
    try:
        preview, worker = _start_selected_preview(database_path, metadata)
        accepted_instance = worker.database_instance
        assert accepted_instance.sidecar_identities
        _replace_sidecar(database_path, sidecar_suffix)
        replacement_state = _artifact_state(database_path)

        open_calls = []

        def reject_open(*args, **kwargs):
            open_calls.append((args, kwargs))
            raise AssertionError("A sidecar-swapped preview opened SQLite")

        monkeypatch.setattr(
            preview_module,
            "sqlite_read_only_connection",
            reject_open,
        )
        ready = []
        replacements = []
        completions = []
        preview.previewsReady.connect(lambda *args: ready.append(args))
        preview.databaseReplaced.connect(replacements.append)
        worker.signals.finished.connect(lambda *args: completions.append(args))

        worker.run()

        assert open_calls == []
        assert len(completions) == 1
        assert completions[0][3] == []
        assert isinstance(completions[0][4], DatabaseInstanceChangedError)
        assert replacements == [accepted_instance.logical_path]
        _assert_replaced_preview_published_nothing(preview, ready)
        assert replacement_wal_is_quarantined(database_path)
        assert _artifact_state(database_path) == replacement_state
    finally:
        writer.close()


@pytest.mark.parametrize("sidecar_suffix", ["-wal", "-shm"])
@_WINDOWS_OPEN_SIDECAR_REPLACEMENT_UNAVAILABLE
def test_preview_rejects_sidecar_swap_during_read(
    tmp_path,
    monkeypatch,
    sidecar_suffix,
):
    database_path = tmp_path / "during-read.db"
    metadata, _columns, _values = _preview_database(database_path, seed=12)
    writer = _open_wal_source(database_path)
    try:
        preview, worker = _start_selected_preview(database_path, metadata)
        accepted_instance = worker.database_instance
        original_table_columns = preview_module._table_columns
        replacement_states = []

        def table_columns_then_replace(cursor, table_name):
            columns = original_table_columns(cursor, table_name)
            _replace_sidecar(database_path, sidecar_suffix)
            replacement_states.append(_artifact_state(database_path))
            return columns

        monkeypatch.setattr(
            preview_module,
            "_table_columns",
            table_columns_then_replace,
        )
        ready = []
        replacements = []
        completions = []
        preview.previewsReady.connect(lambda *args: ready.append(args))
        preview.databaseReplaced.connect(replacements.append)
        worker.signals.finished.connect(lambda *args: completions.append(args))

        worker.run()

        assert len(replacement_states) == 1
        assert len(completions) == 1
        assert completions[0][3] == []
        assert isinstance(completions[0][4], DatabaseInstanceChangedError)
        assert replacements == [accepted_instance.logical_path]
        _assert_replaced_preview_published_nothing(preview, ready)
        assert replacement_wal_is_quarantined(database_path)
        assert _artifact_state(database_path) == replacement_states[0]
    finally:
        writer.close()


@pytest.mark.parametrize("sidecar_suffix", ["-wal", "-shm"])
@_WINDOWS_OPEN_SIDECAR_REPLACEMENT_UNAVAILABLE
def test_preview_rejects_sidecar_swap_after_worker_before_ui_publication(
    tmp_path,
    sidecar_suffix,
):
    database_path = tmp_path / "before-publication.db"
    metadata, _columns, _values = _preview_database(database_path, seed=13)
    writer = _open_wal_source(database_path)
    try:
        preview, worker = _start_selected_preview(database_path, metadata)
        generation = preview.generation
        worker.preview_size = preview.preview_size
        previews = generate_run_previews(
            worker.database_instance,
            metadata,
            size=worker.preview_size,
        )
        assert previews
        preview._store_cached("older-guid", previews)
        preview.thumbnail_cache["older-thumbnail"] = previews
        preview.errors["older-error"] = "stale"
        preview.thumbnail_errors["older-thumbnail-error"] = "stale"
        _replace_sidecar(database_path, sidecar_suffix)
        replacement_state = _artifact_state(database_path)
        ready = []
        replacements = []
        preview.previewsReady.connect(lambda *args: ready.append(args))
        preview.databaseReplaced.connect(replacements.append)

        preview._worker_finished(
            generation,
            metadata["guid"],
            previews,
            None,
            worker,
        )

        assert replacements == [worker.database_path]
        _assert_replaced_preview_published_nothing(preview, ready)
        assert preview.findChildren(DraggablePreviewImageLabel) == []
        assert replacement_wal_is_quarantined(database_path)
        assert _artifact_state(database_path) == replacement_state
    finally:
        writer.close()


@pytest.mark.parametrize("sidecar_suffix", ["-wal", "-shm"])
@_WINDOWS_OPEN_SIDECAR_REPLACEMENT_UNAVAILABLE
def test_preview_rechecks_sidecars_after_cache_mutation_before_signal(
    tmp_path,
    monkeypatch,
    sidecar_suffix,
):
    database_path = tmp_path / "during-cache-publication.db"
    metadata, _columns, _values = _preview_database(database_path, seed=14)
    writer = _open_wal_source(database_path)
    try:
        preview, worker = _start_selected_preview(database_path, metadata)
        generation = preview.generation
        worker.preview_size = preview.preview_size
        previews = generate_run_previews(
            worker.database_instance,
            metadata,
            size=worker.preview_size,
        )
        assert previews
        real_store_cached = preview._store_cached
        replacement_states = []

        def store_then_replace(guid, completed_previews):
            real_store_cached(guid, completed_previews)
            _replace_sidecar(database_path, sidecar_suffix)
            replacement_states.append(_artifact_state(database_path))

        monkeypatch.setattr(preview, "_store_cached", store_then_replace)
        ready = []
        replacements = []
        preview.previewsReady.connect(lambda *args: ready.append(args))
        preview.databaseReplaced.connect(replacements.append)

        preview._worker_finished(
            generation,
            metadata["guid"],
            previews,
            None,
            worker,
        )

        assert len(replacement_states) == 1
        assert replacements == [worker.database_path]
        _assert_replaced_preview_published_nothing(preview, ready)
        assert preview.findChildren(DraggablePreviewImageLabel) == []
        assert replacement_wal_is_quarantined(database_path)
        assert _artifact_state(database_path) == replacement_states[0]
    finally:
        writer.close()


@pytest.mark.parametrize("sidecar_suffix", ["-wal", "-shm"])
@_WINDOWS_OPEN_SIDECAR_REPLACEMENT_UNAVAILABLE
def test_preview_rechecks_sidecars_after_ready_signal_before_selected_render(
    tmp_path,
    monkeypatch,
    sidecar_suffix,
):
    database_path = tmp_path / "during-ready-signal.db"
    metadata, _columns, _values = _preview_database(database_path, seed=15)
    writer = _open_wal_source(database_path)
    try:
        preview, worker = _start_selected_preview(database_path, metadata)
        generation = preview.generation
        worker.preview_size = preview.preview_size
        previews = generate_run_previews(
            worker.database_instance,
            metadata,
            size=worker.preview_size,
        )
        assert previews
        replacement_states = []
        render_calls = []
        replacements = []

        def replace_during_ready(_guid, _previews):
            _replace_sidecar(database_path, sidecar_suffix)
            replacement_states.append(_artifact_state(database_path))

        preview.previewsReady.connect(replace_during_ready)
        preview.databaseReplaced.connect(replacements.append)
        monkeypatch.setattr(
            preview,
            "_show_previews",
            lambda completed_previews: render_calls.append(completed_previews),
        )

        preview._worker_finished(
            generation,
            metadata["guid"],
            previews,
            None,
            worker,
        )

        assert len(replacement_states) == 1
        assert replacements == [worker.database_path]
        assert render_calls == []
        assert preview.cache == {}
        assert preview.thumbnail_cache == {}
        assert preview.current_guid is None
        assert preview.database_instance is None
        assert replacement_wal_is_quarantined(database_path)
        assert _artifact_state(database_path) == replacement_states[0]
    finally:
        writer.close()


@pytest.mark.parametrize("sidecar_suffix", ["-wal", "-shm"])
@_WINDOWS_OPEN_SIDECAR_REPLACEMENT_UNAVAILABLE
def test_preview_rechecks_sidecars_after_selected_render(
    tmp_path,
    monkeypatch,
    sidecar_suffix,
):
    database_path = tmp_path / "during-selected-render.db"
    metadata, _columns, _values = _preview_database(database_path, seed=16)
    writer = _open_wal_source(database_path)
    try:
        preview, worker = _start_selected_preview(database_path, metadata)
        generation = preview.generation
        worker.preview_size = preview.preview_size
        previews = generate_run_previews(
            worker.database_instance,
            metadata,
            size=worker.preview_size,
        )
        assert previews
        real_show_previews = preview._show_previews
        replacement_states = []
        replacements = []

        def render_then_replace(completed_previews):
            real_show_previews(completed_previews)
            assert any(
                isinstance(preview.content_layout.itemAt(index).widget(), PreviewCard)
                for index in range(preview.content_layout.count())
            )
            _replace_sidecar(database_path, sidecar_suffix)
            replacement_states.append(_artifact_state(database_path))

        monkeypatch.setattr(preview, "_show_previews", render_then_replace)
        preview.databaseReplaced.connect(replacements.append)

        preview._worker_finished(
            generation,
            metadata["guid"],
            previews,
            None,
            worker,
        )

        assert len(replacement_states) == 1
        assert replacements == [worker.database_path]
        assert preview.cache == {}
        assert preview.thumbnail_cache == {}
        assert preview.current_guid is None
        assert preview.database_instance is None
        assert not any(
            isinstance(preview.content_layout.itemAt(index).widget(), PreviewCard)
            for index in range(preview.content_layout.count())
        )
        assert replacement_wal_is_quarantined(database_path)
        assert _artifact_state(database_path) == replacement_states[0]
    finally:
        writer.close()


def test_preview_rejects_replacement_before_sqlite_open_and_preserves_source(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "a.db"
    replacement_path = tmp_path / "b.db"
    metadata_a, columns_a, values_a = _preview_database(database_path, seed=1)
    _metadata_b, columns_b, values_b = _preview_database(replacement_path, seed=2)
    assert columns_b == columns_a
    assert not np.allclose(values_b, values_a)

    for suffix in _SQLITE_SIDECAR_SUFFIXES:
        Path(f"{database_path}{suffix}").write_bytes(
            f"original {suffix}".encode()
        )

    preview, worker = _start_selected_preview(database_path, metadata_a)
    accepted_instance = preview.database_instance
    assert isinstance(accepted_instance, DatabaseInstance)
    assert worker.database_instance is accepted_instance
    assert worker.database_path == accepted_instance.logical_path
    assert worker.resolved_database_path == accepted_instance.resolved_path
    assert worker.database_identity == accepted_instance.identity

    replacements = []
    ready = []
    completions = []
    preview.databaseReplaced.connect(replacements.append)
    preview.previewsReady.connect(lambda *args: ready.append(args))
    worker.signals.finished.connect(lambda *args: completions.append(args))

    replacement_states = []
    open_arguments = []
    original_open = preview_module.sqlite_read_only_connection

    def replace_before_sqlite_open(database_source, *args, **kwargs):
        os.replace(replacement_path, database_path)
        replacement_states.append(_artifact_state(database_path))
        open_arguments.append((database_source, kwargs))
        return original_open(database_source, *args, **kwargs)

    monkeypatch.setattr(
        preview_module,
        "sqlite_read_only_connection",
        replace_before_sqlite_open,
    )
    worker.run()

    assert len(completions) == 1
    assert isinstance(completions[0][4], DatabaseInstanceChangedError)
    assert open_arguments == [(
        accepted_instance.logical_path,
        {
            "timeout": 10,
            "expected_database_identity": accepted_instance.identity,
            "cancelled_callback": worker._cancelled.is_set,
        },
    )]
    assert replacements == [accepted_instance.logical_path]
    assert ready == []
    assert preview.cache == {}
    assert preview.errors == {}
    assert preview.current_guid is None
    assert preview.database_instance is None
    assert _artifact_state(database_path) == replacement_states[0]


def test_preview_revalidates_after_read_before_cache_acceptance(tmp_path):
    database_path = tmp_path / "a.db"
    replacement_path = tmp_path / "b.db"
    metadata_a, columns_a, values_a = _preview_database(database_path, seed=3)
    _metadata_b, columns_b, values_b = _preview_database(replacement_path, seed=4)
    assert columns_b == columns_a
    assert not np.allclose(values_b, values_a)

    preview, worker = _start_selected_preview(database_path, metadata_a)
    generation = preview.generation
    previews = generate_run_previews(worker.database_instance, metadata_a, size=40)
    assert previews

    preview._store_cached("older-guid", previews)
    preview.errors["failed-guid"] = "old preview error"
    replacements = []
    ready = []
    preview.databaseReplaced.connect(replacements.append)
    preview.previewsReady.connect(lambda *args: ready.append(args))

    os.replace(replacement_path, database_path)
    replacement_state = _artifact_state(database_path)
    preview._worker_finished(
        generation,
        metadata_a["guid"],
        previews,
        None,
        worker,
    )

    assert replacements == [worker.database_instance.logical_path]
    assert ready == []
    assert preview.cache == {}
    assert preview.errors == {}
    assert preview.current_guid is None
    assert preview.findChildren(DraggablePreviewImageLabel) == []
    messages = [label.text() for label in preview.findChildren(qtw.QLabel)]
    assert "Database replaced; reloading previews..." in messages
    assert _artifact_state(database_path) == replacement_state


def test_unchanged_database_preview_is_cached_and_selected_without_source_writes(
    tmp_path,
):
    database_path = tmp_path / "a.db"
    metadata, _columns, _values = _preview_database(database_path, seed=5)
    source_state = _artifact_state(database_path)
    accepted_instance = database_instance(database_path)

    preview, worker = _start_selected_preview(accepted_instance, metadata)
    ready = []
    replacements = []
    preview.previewsReady.connect(lambda *args: ready.append(args))
    preview.databaseReplaced.connect(replacements.append)
    worker.run()

    guid = metadata["guid"]
    assert worker.database_instance is accepted_instance
    assert replacements == []
    assert guid in preview.thumbnail_cache
    assert len(preview.thread_pool.started) == 2

    preview.thread_pool.started[1].run()

    assert len(ready) == 2
    assert ready[-1][0] == guid
    assert preview.current_guid == guid
    assert preview.cache[guid] == ready[-1][1]
    images = preview.findChildren(DraggablePreviewImageLabel)
    assert len(images) == 1
    assert images[0].guid == guid
    assert _artifact_state(database_path) == source_state


def test_generate_run_previews_pre_cancelled_raises_interrupted_error(monkeypatch):
    open_calls = []

    def reject_database_open(*args, **kwargs):
        open_calls.append((args, kwargs))
        raise AssertionError("A pre-cancelled preview attempted to open its database")

    monkeypatch.setattr(
        preview_module,
        "sqlite_read_only_connection",
        reject_database_open,
    )
    cancelled = threading.Event()
    cancelled.set()

    with pytest.raises(InterruptedError, match="cancelled"):
        generate_run_previews(
            "pre-cancelled.db",
            {},
            is_cancelled=cancelled.is_set,
        )

    assert open_calls == []


def test_preview_cancel_interrupts_snapshot_copy_and_publishes_no_partial_preview(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "cancel-preview-copy.db"
    metadata, _columns, _values = _preview_database(database_path, seed=7)
    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
    finally:
        connection.close()
    assert database_path.read_bytes()[18:20] == b"\x02\x02"
    assert not Path(f"{database_path}-wal").exists()
    source_state = _artifact_state(database_path)

    preview, worker = _start_selected_preview(database_path, metadata)
    ready = []
    completions = []
    preview.previewsReady.connect(lambda *args: ready.append(args))
    worker.signals.finished.connect(lambda *args: completions.append(args))
    snapshot_directories = []
    real_temporary_directory = readonly_module.tempfile.TemporaryDirectory

    def tracked_temporary_directory(*args, **kwargs):
        snapshot = real_temporary_directory(*args, **kwargs)
        snapshot_directories.append(Path(snapshot.name))
        return snapshot

    monkeypatch.setattr(
        readonly_module.tempfile,
        "TemporaryDirectory",
        tracked_temporary_directory,
    )
    monkeypatch.setattr(readonly_module, "SNAPSHOT_COPY_CHUNK_BYTES", 1024)
    monkeypatch.setattr(
        readonly_module,
        "_clone_file_if_supported",
        lambda *_args: False,
    )
    real_copy = readonly_module._copy_file_cooperatively
    copy_checkpoint = threading.Event()

    def controlled_copy(
        source,
        destination,
        *,
        cancelled_callback=None,
        deadline=None,
    ):
        assert callable(cancelled_callback)
        destination = Path(destination)
        copy_checks = 0

        def pause_after_partial_chunk():
            nonlocal copy_checks
            cancelled = bool(cancelled_callback())
            if not cancelled and destination.name == "database.db":
                copy_checks += 1
                if copy_checks == 5:
                    copy_checkpoint.set()
                    worker._cancelled.wait(2)
                    cancelled = bool(cancelled_callback())
            return cancelled

        return real_copy(
            source,
            destination,
            cancelled_callback=pause_after_partial_chunk,
            deadline=deadline,
        )

    monkeypatch.setattr(
        readonly_module,
        "_copy_file_cooperatively",
        controlled_copy,
    )
    thread = threading.Thread(target=worker.run)
    thread.start()
    assert copy_checkpoint.wait(2), "Preview never reached a partial snapshot copy"

    cancel_started = perf_counter()
    worker.cancel()
    thread.join(1)
    qtw.QApplication.processEvents()

    assert not thread.is_alive()
    assert perf_counter() - cancel_started < 1
    assert worker.is_cancelled()
    assert len(completions) == 1
    assert completions[0][3:] == ([], None)
    assert ready == []
    assert preview.cache == {}
    assert snapshot_directories
    assert all(not path.exists() for path in snapshot_directories)
    assert _artifact_state(database_path) == source_state


def test_preview_cancel_wins_final_result_publication_race(tmp_path):
    worker = preview_module.PreviewWorker(
        19,
        tmp_path / "publication-race.db",
        "preview-guid",
        {},
        40,
    )
    published = []

    class RecordingSignal:
        @staticmethod
        def emit(*args):
            published.append(args)

    class RecordingSignals:
        finished = RecordingSignal()

    worker.signals = RecordingSignals()

    class TrackedPublicationLock:
        def __init__(self):
            self._lock = threading.RLock()
            self.publisher = None
            self.publisher_waiting = threading.Event()

        def __enter__(self):
            if threading.current_thread() is self.publisher:
                self.publisher_waiting.set()
            self._lock.acquire()
            return self

        def __exit__(self, _exc_type, _exc_value, _traceback):
            self._lock.release()

    publication_lock = TrackedPublicationLock()
    worker._publication_lock = publication_lock
    publication_errors = []

    def publish_result():
        try:
            worker._emit_finished([{"parameter": "late"}], None)
        except BaseException as error:
            publication_errors.append(error)

    publisher = threading.Thread(target=publish_result)
    publication_lock.publisher = publisher
    with publication_lock:
        publisher.start()
        assert publication_lock.publisher_waiting.wait(2)
        worker.cancel()
        assert worker.is_cancelled()
        assert published == []

    publisher.join(2)

    assert not publisher.is_alive()
    assert publication_errors == []
    assert len(published) == 1
    assert published[0][:3] == (worker, 19, "preview-guid")
    assert published[0][3:] == ([], None)


def test_preview_worker_detaches_traceback_data_before_qt_publication(
    tmp_path,
    monkeypatch,
):
    worker = preview_module.PreviewWorker(
        19,
        tmp_path / "bounded-error.db",
        "preview-guid",
        {},
        40,
    )
    published = []

    class RecordingSignal:
        @staticmethod
        def emit(*args):
            published.append(args)

    class RecordingSignals:
        finished = RecordingSignal()

    worker.signals = RecordingSignals()
    raw_secret = "raw-preview-secret-" + "x" * (1024 * 1024)

    def fail_with_retained_local(*_args, **_kwargs):
        raw_metadata = {"run_description": raw_secret}
        generated_array = np.full((1024, 1024), 7.0)
        assert raw_metadata and generated_array.size
        raise RuntimeError("preview failed")

    monkeypatch.setattr(
        preview_module,
        "generate_run_previews",
        fail_with_retained_local,
    )

    worker.run()

    assert len(published) == 1
    error = published[0][-1]
    assert error == "preview failed"
    assert isinstance(error, str)
    assert raw_secret not in repr(published)


def test_preview_worker_publishes_fresh_bounded_instance_change_error(
    tmp_path,
    monkeypatch,
):
    worker = preview_module.PreviewWorker(
        20,
        tmp_path / "replaced.db",
        "preview-guid",
        {},
        40,
    )
    published = []
    originals = []

    class RecordingSignal:
        @staticmethod
        def emit(*args):
            published.append(args)

    class RecordingSignals:
        finished = RecordingSignal()

    worker.signals = RecordingSignals()
    raw_secret = "raw-instance-secret-" + "x" * (1024 * 1024)

    def fail_with_instance_change(*_args, **_kwargs):
        retained_metadata = {"run_description": raw_secret}
        error = DatabaseInstanceChangedError(raw_secret)
        originals.append(error)
        assert retained_metadata
        raise error

    monkeypatch.setattr(
        preview_module,
        "generate_run_previews",
        fail_with_instance_change,
    )

    worker.run()

    assert len(published) == 1
    error = published[0][-1]
    assert isinstance(error, DatabaseInstanceChangedError)
    assert error is not originals[0]
    assert error.__traceback__ is None
    assert error.__context__ is None
    assert error.__cause__ is None
    assert len(str(error).encode("utf-8")) <= TRUSTED_PRESENTATION_MAX_ERROR_BYTES
    assert raw_secret not in repr(published)


def test_stale_generation_completion_cannot_publish_preview(tmp_path):
    database_path = tmp_path / "a.db"
    metadata, _columns, _values = _preview_database(database_path, seed=6)
    accepted_instance = database_instance(database_path)
    preview, stale_worker = _start_selected_preview(accepted_instance, metadata)
    stale_generation = preview.generation
    stale_previews = generate_run_previews(
        stale_worker.database_instance,
        metadata,
        size=40,
    )

    preview.set_database_runs(
        accepted_instance,
        {metadata["run_id"]: metadata},
    )
    preview.set_current_run(
        type("Dataset", (), {"guid": metadata["guid"]})()
    )
    current_generation = preview.generation
    current_worker = preview._workers[(current_generation, metadata["guid"])]
    ready = []
    replacements = []
    preview.previewsReady.connect(lambda *args: ready.append(args))
    preview.databaseReplaced.connect(replacements.append)

    preview._worker_finished(
        stale_generation,
        metadata["guid"],
        stale_previews,
        None,
        stale_worker,
    )

    assert replacements == []
    assert ready == []
    assert preview.cache == {}
    assert preview.current_guid == metadata["guid"]
    assert preview.active == {(current_generation, metadata["guid"])}
    assert preview._workers[(current_generation, metadata["guid"])] is current_worker
