"""Current database information through public Qt controls and live QCoDeS."""

import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from PyQt6 import QtCore
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.parameters import ManualParameter

from qplot.datahandling import database as database_module
from qplot.datahandling.trusted_live_queries import TrustedMetadataQueryAdapter
from qplot.datahandling.trusted_live_service import (
    TrustedReadRequestCancelledError,
    TrustedReadRequestDeadlineError,
)
from qplot.diagnostics import configure_logging
from qplot.windows import _database_actions as actions
from qplot.windows.main import MainWindow
from tests._window_lifecycle import close_main_window
from tests.datahandling.test_trusted_live import (
    _artifact_state,
    _assert_protected_artifacts_unchanged,
)
from tests.windows.test_plot_integration import (
    configure_temp_qplot,
    force_snapshot_fallback,
    wait_for,
)


@contextmanager
def live_measurement(path, journal_mode="WAL"):
    initialise_or_create_database_at(str(path), journal_mode=journal_mode)
    experiment = load_or_create_experiment("database information", sample_name="test")
    x, signal = ManualParameter("x"), ManualParameter("signal")
    measurement = Measurement(exp=experiment)
    measurement.register_parameter(x)
    measurement.register_parameter(signal, setpoints=(x,))
    measurement.write_period = 3600
    dataset = None
    try:
        with measurement.run(write_in_background=False) as saver:
            dataset = saver.dataset
            for value in range(3):
                saver.add_result((x, value), (signal, 2 * value))
            saver.flush_data_to_database(block=True)
            yield saver
    finally:
        if dataset is not None:
            dataset.conn.close()
        experiment.conn.close()


@pytest.fixture
def loaded_live_window(tmp_path, monkeypatch):
    configure_temp_qplot(monkeypatch, tmp_path / "settings")
    configure_logging(tmp_path / "diagnostics" / "qplot.log", force=True)
    path = tmp_path / "active.db"
    with live_measurement(path) as saver:
        window = MainWindow()
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        errors, dialogs = [], []
        monkeypatch.setattr(window, "show_error", lambda *args: errors.append(args))

        def inspect_dialog(dialog):
            dialogs.append({
                dialog.table.item(row, 0).text(): dialog.table.item(row, 1).text()
                for row in range(dialog.table.rowCount())
            })
            return 0

        monkeypatch.setattr(actions.DatabaseInfoDialog, "exec", inspect_dialog)
        before = _artifact_state(path)
        try:
            assert window.load_database_path(str(path))
            wait_for(lambda: not window._database_load_active)
            window.monitor.stop()
            assert window._database_access_mode == "trusted_live", errors
            _assert_protected_artifacts_unchanged(before, _artifact_state(path))
            yield window, path, saver, errors, dialogs
        finally:
            before_close = _artifact_state(path)
            window.close_database(status=False)
            close_main_window(window)
            _assert_protected_artifacts_unchanged(before_close, _artifact_state(path))


def show_information(window, dialogs):
    count = len(dialogs)
    path = Path(window.fileTextbox.text())
    before = _artifact_state(path)
    window.databaseInfoButton.click()
    wait_for(lambda: not window._database_info_active)
    assert len(dialogs) == count + 1
    after = _artifact_state(path)
    if before["-wal"] is not None:
        _assert_protected_artifacts_unchanged(before, after)
    else:
        for suffix in ("", "-wal", "-journal"):
            assert before[suffix] == after[suffix]
    return dialogs[-1]


def test_live_info_dialog_reports_current_counts(loaded_live_window, monkeypatch):
    window, path, saver, errors, dialogs = loaded_live_window

    def reject_snapshot(*_args, **_kwargs):
        raise AssertionError("Live information must use the trusted service")

    monkeypatch.setattr(database_module, "sqlite_read_only_connection", reject_snapshot)
    rows = show_information(window, dialogs)
    assert rows["Runs"] == rows["Experiments"] == "1"
    assert rows["Latest run ID"] == "1"
    assert rows["Latest run GUID"] == saver.dataset.guid
    assert rows["Latest run status"] == "running or incomplete"

    # Counts must come from the current live transaction, not the run-list cache.
    experiment = load_or_create_experiment("second experiment", sample_name="test")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("value")
    dataset = None
    try:
        with measurement.run(write_in_background=False) as second:
            dataset = second.dataset
            second.add_result(("value", 7))
        current = _artifact_state(path)
        rows = show_information(window, dialogs)
        assert rows["Runs"] == rows["Experiments"] == "2"
        assert rows["Latest run ID"] == "2"
        assert rows["Latest run status"] == "completed"
        assert errors == []
        _assert_protected_artifacts_unchanged(current, _artifact_state(path))
    finally:
        if dataset is not None:
            dataset.conn.close()
        experiment.conn.close()


class BlockingRequest:
    def __init__(self, error=None):
        self.started = threading.Event()
        self.release = threading.Event()
        self.cancelled = False
        self.error = error or TrustedReadRequestCancelledError("Info request cancelled")

    def wait(self):
        self.started.set()
        assert self.release.wait(10), "Info request did not receive cancellation"
        raise self.error

    def cancel(self):
        self.cancelled = True
        self.release.set()
        return True


@pytest.mark.parametrize("action", ["close", "switch", "reload", "shutdown"])
def test_pending_info_is_cancellable_and_stale_results_are_discarded(
    loaded_live_window, monkeypatch, tmp_path, action,
):
    window, path, _saver, errors, dialogs = loaded_live_window
    request = BlockingRequest()
    service = window._trusted_read_service
    submitted = []

    def submit(**kwargs):
        submitted.append(kwargs)
        return request

    monkeypatch.setattr(service, "submit_database_info", submit)
    window.databaseInfoButton.click()
    wait_for(request.started.is_set)
    old_generation = window._database_info_generation
    old_path = window.fileTextbox.text()
    # The Qt event loop stays usable, and a repeated click shares the request.
    heartbeat = []
    QtCore.QTimer.singleShot(0, lambda: heartbeat.append(True))
    window.databaseInfoButton.click()
    wait_for(lambda: bool(heartbeat))
    assert len(submitted) == 1
    assert time.monotonic() < submitted[0]["deadline"] < time.monotonic() + 11

    if action == "close":
        window.close_database(status=False)
    elif action == "reload":
        assert window.load_file(str(path), force=True)
    elif action == "switch":
        other = tmp_path / "other.db"
        with live_measurement(other, journal_mode="DELETE"):
            pass
        assert window.load_database_path(str(other))
        wait_for(lambda: not window._database_load_active)
        assert window.fileTextbox.text().lower() == str(other).lower()
    else:
        window.close()
    wait_for(lambda: not window._database_info_active)
    assert request.cancelled
    window.database_info_finished(old_generation, old_path, [("Runs", "999")], None)
    assert dialogs == [] and errors == []
    if action in {"reload", "switch"}:
        wait_for(lambda: not window._database_load_active)
        rows = show_information(window, dialogs)
        assert rows["Runs"] == "1"


@pytest.mark.parametrize("error", [
    TrustedReadRequestCancelledError("Cancelled unexpectedly"),
    TrustedReadRequestDeadlineError("Database information deadline expired"),
    RuntimeError("Database information query failed"),
], ids=["cancellation", "deadline", "query-error"])
def test_request_failure_clears_busy_state_and_allows_retry(
    loaded_live_window, monkeypatch, error,
):
    window, _path, _saver, errors, dialogs = loaded_live_window
    request = BlockingRequest(error)
    request.release.set()
    service = window._trusted_read_service
    original_submit = service.submit_database_info
    monkeypatch.setattr(service, "submit_database_info", lambda **_kwargs: request)
    window.databaseInfoButton.click()
    wait_for(lambda: not window._database_info_active)
    assert dialogs == [] and len(errors) == 1
    monkeypatch.setattr(service, "submit_database_info", original_submit)
    assert show_information(window, dialogs)["Runs"] == "1"


def test_actual_broker_deadline_clears_busy_state_and_allows_retry(
    loaded_live_window, monkeypatch,
):
    window, _path, _saver, errors, dialogs = loaded_live_window
    original_info = TrustedMetadataQueryAdapter.database_info

    def delayed_info(adapter):
        # Delay before the first transaction. The real broker enforces the
        # request deadline; no reader transaction is held during this delay.
        time.sleep(0.2)
        return original_info(adapter)

    with monkeypatch.context() as pending:
        pending.setattr(TrustedMetadataQueryAdapter, "database_info", delayed_info)
        pending.setattr(actions, "monotonic", lambda: time.monotonic() - 9.9)
        window.databaseInfoButton.click()
        wait_for(lambda: not window._database_info_active)
    assert dialogs == [] and len(errors) == 1
    assert "deadline" in errors[0][2].lower() or "expired" in errors[0][2].lower()
    assert window._database_info_worker is None
    assert show_information(window, dialogs)["Runs"] == "1"


def test_missing_trusted_service_does_not_fall_back(loaded_live_window, monkeypatch):
    window, _path, _saver, errors, dialogs = loaded_live_window
    service = window._trusted_read_service
    monkeypatch.setattr(window, "_trusted_read_service", None)
    window.databaseInfoButton.click()
    assert len(errors) == 1
    assert "trusted reader session is unavailable" in errors[0][2]
    assert dialogs == [] and not window._database_info_active
    monkeypatch.setattr(window, "_trusted_read_service", service)


def test_snapshot_fallback_info_and_original_report(tmp_path, monkeypatch):
    configure_temp_qplot(monkeypatch, tmp_path / "settings")
    configure_logging(tmp_path / "diagnostics" / "qplot.log", force=True)
    path = tmp_path / "completed.db"
    with live_measurement(path, journal_mode="DELETE"):
        pass
    before = _artifact_state(path)
    force_snapshot_fallback(monkeypatch)
    window = MainWindow()
    window.startupDatabaseTimer.stop()
    window.monitor.stop()
    window.config.config["user_preference"]["confirm_close"] = False
    window.config.config["user_preference"]["confirm_close_all"] = False
    errors, dialogs = [], []
    monkeypatch.setattr(window, "show_error", lambda *args: errors.append(args))
    monkeypatch.setattr(
        actions.DatabaseInfoDialog, "exec",
        lambda dialog: dialogs.append(dict(dialog._rows)) or 0,
    )
    try:
        assert window.load_database_path(str(path))
        wait_for(lambda: not window._database_load_active)
        assert window._database_access_mode == "snapshot_fallback"
        window.databaseInfoButton.click()
        wait_for(lambda: not window._database_info_active)
        assert dialogs[-1]["Runs"] == "1" and errors == []
        assert "Runs: 1" in database_module.database_info_report(str(path))
        assert ("Experiments", "1") in database_module.database_info_rows(str(path))
    finally:
        window.close_database(status=False)
        close_main_window(window)
    after = _artifact_state(path)
    for suffix in ("", "-wal", "-journal"):
        assert before[suffix] == after[suffix]
