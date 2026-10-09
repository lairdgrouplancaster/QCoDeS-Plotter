"""Real exclusive-lock errors delivered through Qt to the public main window."""

import sqlite3
import sys
import time
from unittest.mock import patch

import pytest
from PyQt6 import QtTest, QtWidgets

from qplot import diagnostics
from qplot.datahandling.file_identity import logical_database_path
from qplot.datahandling import database as database_module
from qplot.datahandling.trusted_live import (
    TrustedLiveBusyTimeoutError,
    TrustedLiveSourceIOError,
    TrustedLiveUnsupportedSourceError,
)
from qplot.datahandling.trusted_live_supervisor import TrustedLiveReaderSupervisor
from qplot.datahandling.trusted_presentation import TRUSTED_PRESENTATION_MAX_ERROR_BYTES
from qplot.testdata import RunSpecification, generate_database
from qplot.windows import _database_actions as database_actions
from qplot.windows import main as main_window
from tests._window_lifecycle import close_main_window

pytestmark = pytest.mark.timeout(90)


def _process_until(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QtWidgets.QApplication.processEvents()
        if predicate():
            return
        QtTest.QTest.qWait(5)
    raise AssertionError("Database worker did not finish within the bounded wait")


def _artifacts(path):
    state = {}
    for suffix in ("", "-wal", "-journal", "-shm"):
        artifact = path.with_name(path.name + suffix)
        state[suffix] = (
            (artifact.read_bytes(), artifact.stat().st_mtime_ns, artifact.stat().st_ino)
            if artifact.exists() else None
        )
    return state


def _create_database(path, name):
    generate_database([RunSpecification(1, name, name, "V", 0.0, 1.0, 3)], path)
    # Only the test-owned writer changes journal mode, before any qPlot read.
    with sqlite3.connect(path) as writer:
        assert writer.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
    writer.close()


@pytest.fixture
def error_window(tmp_path, monkeypatch):
    home = tmp_path / "settings"
    monkeypatch.setattr(main_window.config, "default_path", str(home))
    monkeypatch.setattr(
        main_window.config, "default_file", str(home / main_window.config.config_file_name),
    )
    diagnostics.configure_logging(log_file=tmp_path / "qplot.log", force=True)
    errors = []
    uncaught = []
    requested_titles = {}
    original_box_init = QtWidgets.QMessageBox.__init__

    def record_box_init(box, *args, **kwargs):
        original_box_init(box, *args, **kwargs)
        if len(args) >= 3:
            requested_titles[box] = args[1]

    monkeypatch.setattr(QtWidgets.QMessageBox, "__init__", record_box_init)

    def dismiss_error(box):
        # Exercise the real show_error and QMessageBox construction; dismiss
        # only its modal event loop so this test cannot block on user input.
        # Qt ignores message-box window titles on macOS. Assert the title
        # supplied to its real constructor, together with the rendered body.
        title = requested_titles[box]
        if sys.platform != "darwin":
            assert box.windowTitle() == title
        assert box.icon() == QtWidgets.QMessageBox.Icon.Warning
        errors.append((title, box.text(), box.detailedText()))
        return QtWidgets.QMessageBox.StandardButton.Ok

    monkeypatch.setattr(QtWidgets.QMessageBox, "exec", dismiss_error)
    monkeypatch.setattr(sys, "excepthook", lambda *args: uncaught.append(args))
    window = main_window.MainWindow()
    window.startupDatabaseTimer.stop()
    window.config.update("user_preference.default_refresh_rate", 0.0)
    window.config.update("user_preference.auto_plot", False)
    window.spinBox.setValue(0.0)
    window._apply_refresh_interval(0.0)
    window.show()
    try:
        yield window, errors, uncaught
    finally:
        close_main_window(window)
        diagnostics._reset_logging_for_tests()
    assert not uncaught


def _wait_published_metadata(window):
    _wait_idle(window)

    def derived_idle():
        bridge = window._trusted_derived_bridge
        coordinator = bridge.coordinator
        return (
            not bridge.background_active()
            and (coordinator is None or (
                not coordinator.active and coordinator.snapshot().pending_count == 0
            ))
        )

    # Initial load completion precedes the progressive metadata that refines
    # RunList. Settle it before taking preservation snapshots, while unlocked.
    _process_until(derived_idle)
    _wait_idle(window)


def _wait_idle(window):
    _process_until(lambda: (
        not window._database_load_active
        and not window._database_detail_active
        and not window._database_expensive_detail_active
        and not window._database_refresh_active
        and window.databaseLoadThreadPool.activeThreadCount() == 0
        and window.databaseDetailThreadPool.activeThreadCount() == 0
        and window.databaseExpensiveDetailThreadPool.activeThreadCount() == 0
        and window.databaseRefreshThreadPool.activeThreadCount() == 0
    ))


@pytest.mark.parametrize("prior_loaded", [False, True], ids=["initial", "switch"])
def test_public_load_exclusive_lock_reports_error_and_allows_retry(
    tmp_path, error_window, prior_loaded,
):
    window, errors, uncaught = error_window
    target = tmp_path / "locked.db"
    _create_database(target, "target_signal")
    previous = tmp_path / "previous.db"
    if prior_loaded:
        _create_database(previous, "previous_signal")
        assert window.load_file(str(previous))
        _wait_published_metadata(window)
        assert not errors
    prior_instance = window._loaded_database_instance
    prior_runs = window.RunList.all_run_metadata()
    prior_preview = window.infoBox.preview.database_path
    before = _artifacts(target)
    writer = sqlite3.connect(target)
    try:
        writer.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        assert window.load_file(str(target))
        worker = window._database_load_worker
        service = worker.trusted_service
        finished = QtTest.QSignalSpy(worker.signals.finished)
        _process_until(lambda: bool(errors) or bool(uncaught))
        _wait_idle(window)
        assert time.monotonic() - started < 20
        assert not uncaught
        assert len(finished) == 1
        payload = finished[0][-1]
        assert isinstance(payload, str)
        assert len(payload.encode("utf-8")) <= TRUSTED_PRESENTATION_MAX_ERROR_BYTES
        assert "Traceback" not in payload
        assert errors == [(
            "Database Load Failed", f"Could not load database {target}.", payload,
        )]
        assert any(word in payload.lower() for word in ("locked", "timed out", "deadline"))
        assert not window._database_load_active
        assert window._database_load_state is None
        assert window._database_load_worker is None
        assert not window._pending_trusted_read_services
        assert window.RunList.isEnabled()
        assert window.infoBox.isEnabled()
        _process_until(lambda: service.closed)
        liveness = service.liveness()
        assert not liveness.dispatcher_alive
        assert not liveness.control_alive
        assert not liveness.helper_alive
        assert not liveness.receiver_alive
        assert not liveness.open_supervisor_endpoints
        assert not liveness.unreaped_incarnations
        assert not liveness.resource_cleanup_pending
        assert not liveness.outstanding_requests
        assert window._loaded_database_instance == prior_instance
        assert window.RunList.all_run_metadata() == prior_runs
        assert window.infoBox.preview.database_path == prior_preview
        if prior_loaded:
            window.refreshMain()
            _wait_idle(window)
            assert len(errors) == 1
            assert window.RunList.all_run_metadata() == prior_runs
            assert window.RunList.topLevelItemCount() > 0
            guid = next(iter(prior_runs.values()))["guid"]
            item = window.RunList._item_for_guid(guid)
            window.RunList.setCurrentItem(item)
            item.setSelected(True)
            _process_until(lambda: window._selected_run_guid == guid)
            assert window.run_idBox.text() == str(window.RunList.run_id_for_guid(guid))
            assert len(errors) == 1
        assert _artifacts(target) == before
    finally:
        writer.rollback()
        writer.close()

    assert window.load_file(str(target))
    _wait_idle(window)
    assert len(errors) == 1
    assert window._loaded_database_instance.logical_path == logical_database_path(target)
    assert window.RunList.topLevelItemCount() > 0
    assert _artifacts(target) == before


@pytest.mark.parametrize("prior_loaded", [False, True], ids=["initial", "switch"])
@pytest.mark.parametrize(
    "failure, cloud, cloud_hint",
    [("io", True, True), ("prefetch", True, True),
     ("io", False, False), ("busy", True, False)],
    ids=["cloud-io", "cloud-download-timeout", "local-io", "cloud-locked"],
)
def test_cloud_load_error_details_explain_recovery_and_allow_retry(
    tmp_path, error_window, monkeypatch, prior_loaded, failure, cloud, cloud_hint,
):
    window, errors, uncaught = error_window
    directory = tmp_path / ("OneDrive" if cloud else "local")
    directory.mkdir()
    target = directory / "target.db"
    _create_database(target, "target_signal")
    if prior_loaded:
        previous = tmp_path / "previous.db"
        _create_database(previous, "previous_signal")
        assert window.load_file(str(previous))
        _wait_published_metadata(window)
    prior_instance = window._loaded_database_instance
    prior_runs = window.RunList.all_run_metadata()
    prior_preview = window.infoBox.preview.database_path
    before = _artifacts(target)
    technical_error = {
        "io": TrustedLiveSourceIOError(
            "SQLite could not read the trusted source: disk I/O error"
        ),
        "prefetch": TimeoutError("Timed out waiting for OneDrive to download the database."),
        "busy": TrustedLiveBusyTimeoutError("Database is locked."),
    }[failure]

    def fail_file_access(*args, **kwargs):
        raise technical_error

    with monkeypatch.context() as fault:
        fault.setattr(
            database_module, "database_is_likely_cloud_placeholder",
            lambda _path: failure == "prefetch",
        )
        if failure == "prefetch":
            fault.setattr(
                database_module, "prefetch_database_file_with_timeout",
                fail_file_access,
            )
        else:
            fault.setattr(
                TrustedLiveReaderSupervisor, "open",
                fail_file_access,
            )
        with patch.object(database_module, "database_access_error") as snapshot_probe:
            assert window.load_file(str(target))
            service = window._database_load_worker.trusted_service
            _process_until(lambda: bool(errors) or bool(uncaught))
            _wait_idle(window)
            snapshot_probe.assert_not_called()
    assert not uncaught
    assert len(errors) == 1
    title, message, details = errors[0]
    assert title == "Database Load Failed"
    assert message == f"Could not load database {target}."
    assert str(technical_error) in details
    assert len(details.encode("utf-8")) <= TRUSTED_PRESENTATION_MAX_ERROR_BYTES
    assert "Traceback" not in details
    if cloud_hint:
        assert "Start or restart OneDrive" in details
        assert "auxiliary files" in details
        assert "may not be running" in details
        assert "whole folder as always available" in details
        assert "open the database again in qPlot" in details
    else:
        assert details == str(technical_error)
    assert window._database_load_state is None
    assert window._database_load_worker is None
    assert window.RunList.isEnabled()
    assert window.infoBox.isEnabled()
    _process_until(lambda: service.closed)
    assert window._loaded_database_instance == prior_instance
    assert window.RunList.all_run_metadata() == prior_runs
    assert window.infoBox.preview.database_path == prior_preview
    assert _artifacts(target) == before

    assert window.load_file(str(target))
    _wait_published_metadata(window)
    assert len(errors) == 1
    assert window._loaded_database_instance.logical_path == logical_database_path(target)
    assert window.RunList.topLevelItemCount() > 0
    assert _artifacts(target) == before


@pytest.mark.parametrize("snapshot", [False, True], ids=["trusted", "snapshot"])
def test_refresh_and_detail_workers_present_sanitized_errors(
    tmp_path, error_window, snapshot, monkeypatch,
):
    window, errors, uncaught = error_window
    path = tmp_path / "loaded.db"
    _create_database(path, "signal")
    if snapshot:
        # Select the documented fallback outcome, then exercise its real SQL
        # readers and Qt workers, including both legacy detail paths.
        with patch.object(
            TrustedLiveReaderSupervisor, "open",
            side_effect=TrustedLiveUnsupportedSourceError("snapshot detail test"),
        ):
            assert window.load_file(str(path))
            _wait_idle(window)
    else:
        assert window.load_file(str(path))
        _wait_idle(window)
    _wait_published_metadata(window)
    assert not errors
    assert window._database_access_mode == (
        database_actions.SNAPSHOT_FALLBACK_MODE if snapshot else database_actions.TRUSTED_LIVE_MODE
    )
    worker_types = {}
    for name in (
        "DatabaseRefreshWorker", "DatabaseDetailWorker", "DatabaseExpensiveDetailWorker",
    ):
        worker_type = worker_types[name] = getattr(database_actions, name)

        def observed_worker(*args, worker_type=worker_type, **kwargs):
            if snapshot:
                # Immutable snapshot reads do not wait for the writer lock.
                kwargs["deadline"] = time.monotonic() - 1
            worker = worker_type(*args, **kwargs)
            # Observe before submission: an expired deadline can finish before
            # the controller's start method returns on a fast CI runner.
            worker._test_finished_spy = QtTest.QSignalSpy(worker.signals.finished)
            return worker

        monkeypatch.setattr(database_actions, name, observed_worker)
    runs = window.RunList.all_run_metadata()
    before = _artifacts(path)
    writer = sqlite3.connect(path)
    try:
        writer.execute("BEGIN EXCLUSIVE")
        window.refreshMain()
        refresh = window._database_refresh_worker._test_finished_spy
        _process_until(lambda: bool(errors) or bool(uncaught))
        _wait_idle(window)
        assert not uncaught
        assert errors[0][0] == "Refresh Failed"
        assert isinstance(refresh[0][-1], str)
        assert window._database_refresh_worker is None
        assert not window._database_refresh_pending
        assert window.RunList.all_run_metadata() == runs

        if snapshot:
            statuses = []
            window.statusBar().messageChanged.connect(statuses.append)
            window._start_database_detail_load(str(path), runs)
            cheap = window._database_detail_worker._test_finished_spy
            expensive = window._database_expensive_detail_worker._test_finished_spy
            _wait_idle(window)
            assert not uncaught
            assert isinstance(cheap[0][-1], str)
            assert isinstance(expensive[0][-1], str)
            assert any("Run detail loading failed:" in message for message in statuses)
            assert any("Setpoint and size loading failed:" in message for message in statuses)
            assert window._database_detail_worker is None
            assert window._database_expensive_detail_worker is None
        assert window.RunList.all_run_metadata() == runs
        assert _artifacts(path) == before
    finally:
        writer.rollback()
        writer.close()
    for name, worker_type in worker_types.items():
        monkeypatch.setattr(database_actions, name, worker_type)
    window.refreshMain()
    window._start_database_detail_load(str(path), runs)
    _wait_idle(window)
    assert len(errors) == 1
    assert _artifacts(path) == before
