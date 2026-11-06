"""Export responsiveness, cancellation and GUI publication with real Qt owners."""

import gc
import threading
import weakref
from pathlib import Path

import pytest
from PyQt6 import QtCore, QtWidgets, sip

from qplot.diagnostics import configure_logging
from qplot.windows._export_paths import (
    UnsafeExportDestinationError,
    prepare_export_destination,
)
from qplot.windows.main import MainWindow
from tests._window_lifecycle import close_main_window
from tests.windows.test_plot_integration import configure_temp_qplot, wait_for


@pytest.fixture
def export_window(tmp_path, monkeypatch):
    configure_temp_qplot(monkeypatch, tmp_path)
    configure_logging(tmp_path / "qplot.log", force=True)
    window = MainWindow()
    window.startupDatabaseTimer.stop()
    window.monitor.stop()
    window.config.config["user_preference"]["confirm_close"] = False
    window.config.config["user_preference"]["confirm_close_all"] = False
    errors = []
    monkeypatch.setattr(window, "show_error", lambda *args: errors.append(args))
    try:
        yield window, errors
    finally:
        close_main_window(window)


def _start(window, target, writer, **kwargs):
    from qplot.windows._export_tasks import start_export_task

    destination = prepare_export_destination(
        window, str(target), replacement_confirmed=target.exists(),
    )
    return start_export_task(
        window, title="Export raw measurement CSV", destination=destination,
        writer=writer, **kwargs,
    )


def _assert_no_stage(target):
    assert not list(target.parent.glob(f".{target.name}.*"))


def test_export_task_keeps_gui_responsive_and_validates_on_gui(export_window, tmp_path):
    window, errors = export_window
    gui_thread = threading.get_ident()
    started, release = threading.Event(), threading.Event()
    ticks, validation_threads = [], []
    timer = QtCore.QTimer(window)
    timer.setInterval(1)
    timer.timeout.connect(lambda: ticks.append(True))
    timer.start()
    target = tmp_path / "responsive.csv"

    def writer(name, cancelled, progress):
        assert threading.get_ident() != gui_thread
        progress("Writing CSV", 0, 2)
        started.set()
        assert release.wait(5), "test did not release blocked writer"
        Path(name).write_text("x,y\n1,7\n2,9\n", encoding="utf-8")
        progress("Writing CSV", 2, 2)
        return {"rows": 2}

    def before_publish():
        validation_threads.append(threading.get_ident())
        assert threading.get_ident() == gui_thread
        assert not target.exists()

    task = _start(window, target, writer, before_publish=before_publish)
    try:
        wait_for(lambda: started.is_set() and len(ticks) >= 3)
        assert not task.done
        assert not target.exists()
        assert window._shutdown.resources.inspect().active
        release.set()
        wait_for(lambda: task.done)
        assert task.error is None
        assert task.result == {"rows": 2}
        assert target.read_text(encoding="utf-8") == "x,y\n1,7\n2,9\n"
        assert validation_threads == [gui_thread]
        assert not errors
        _assert_no_stage(target)
    finally:
        release.set()
        timer.stop()
        wait_for(lambda: task.done)


def test_cancelled_export_cannot_replace_existing_approved_output(export_window, tmp_path):
    window, errors = export_window
    target = tmp_path / "cancelled.csv"
    target.write_bytes(b"previous export")
    started, release = threading.Event(), threading.Event()
    validation = []

    def writer(name, cancelled, progress):
        Path(name).write_bytes(b"partially written data")
        started.set()
        assert release.wait(5)
        # Even a writer which finishes just as cancellation arrives must not
        # publish its completed stage.
        Path(name).write_bytes(b"finished but cancelled export")
        return True

    task = _start(
        window, target, writer, before_publish=lambda: validation.append(True),
    )
    try:
        wait_for(started.is_set)
        task.cancel()
        release.set()
        wait_for(lambda: task.done)
        assert task.cancelled
        assert target.read_bytes() == b"previous export"
        assert not validation
        assert not errors
        _assert_no_stage(target)
    finally:
        release.set()
        wait_for(lambda: task.done)


def test_export_task_rejects_source_validator_failure(export_window, tmp_path):
    window, errors = export_window
    target = tmp_path / "replaced-source.csv"
    target.write_bytes(b"previous export")

    def source_changed():
        raise RuntimeError("source instance changed")

    task = _start(
        window, target,
        lambda name, _cancelled, _progress: Path(name).write_bytes(b"stale result"),
        before_publish=source_changed,
    )
    wait_for(lambda: task.done)
    assert isinstance(task.error, RuntimeError)
    assert "source instance changed" in str(task.error)
    assert target.read_bytes() == b"previous export"
    assert task.row.text(1) == "Failed"
    assert "source instance changed" in task.row.toolTip(1)
    assert not errors
    _assert_no_stage(target)


def test_export_task_rejects_concurrent_target_replacement(export_window, tmp_path):
    window, errors = export_window
    target = tmp_path / "replaced-target.csv"
    target.write_bytes(b"approved old output")
    started, release = threading.Event(), threading.Event()

    def writer(name, _cancelled, _progress):
        Path(name).write_bytes(b"new export")
        started.set()
        assert release.wait(5)
        return True

    task = _start(window, target, writer)
    try:
        wait_for(started.is_set)
        target.write_bytes(b"someone else's newer output")
        release.set()
        wait_for(lambda: task.done)
        assert isinstance(task.error, UnsafeExportDestinationError)
        assert target.read_bytes() == b"someone else's newer output"
        assert task.row.text(1) == "Failed"
        assert str(task.error) in task.row.toolTip(1)
        assert not errors
        _assert_no_stage(target)
    finally:
        release.set()
        wait_for(lambda: task.done)


def test_main_window_shutdown_cancels_and_tracks_export_worker(export_window, tmp_path):
    window, errors = export_window
    target = tmp_path / "shutdown.csv"
    target.write_bytes(b"previous export")
    started, observed_cancel = threading.Event(), threading.Event()

    def writer(name, cancelled, _progress):
        Path(name).write_bytes(b"unfinished export")
        started.set()
        for _ in range(500):
            if cancelled():
                observed_cancel.set()
                return True
            observed_cancel.wait(0.01)
        raise AssertionError("shutdown never cancelled the export")

    task = _start(window, target, writer)
    wait_for(started.is_set)
    assert window._shutdown.resources.inspect().active
    window.close()
    wait_for(lambda: task.done and window._shutdown.ready)
    assert observed_cancel.is_set()
    assert task.cancelled
    assert target.read_bytes() == b"previous export"
    assert not errors
    assert not window._shutdown.resources.inspect().active
    _assert_no_stage(target)


def test_shutdown_cleans_queued_exports_even_after_escalation(export_window, tmp_path):
    window, errors = export_window
    first_target, queued_target = tmp_path / "active.csv", tmp_path / "queued.csv"
    first_target.write_bytes(b"previous active export")
    queued_target.write_bytes(b"previous queued export")
    started, release = threading.Event(), threading.Event()
    queued_writes = []

    def active_writer(name, _cancelled, _progress):
        Path(name).write_bytes(b"unfinished active export")
        started.set()
        assert release.wait(5)
        return True

    def queued_writer(name, _cancelled, _progress):
        queued_writes.append(name)
        Path(name).write_bytes(b"unexpected queued export")
        return True

    first = _start(window, first_target, active_writer)
    queued = _start(window, queued_target, queued_writer)
    try:
        wait_for(started.is_set)
        assert not queued.done
        window.close()
        window._shutdown.resources.escalate_cleanup()
        assert first.cancelled and queued.cancelled
        assert window._shutdown.resources.inspect().active
        release.set()
        wait_for(lambda: first.done and queued.done and window._shutdown.ready)
        assert not queued_writes
        assert first_target.read_bytes() == b"previous active export"
        assert queued_target.read_bytes() == b"previous queued export"
        assert not window._export_task_manager.tasks
        assert not window._shutdown.resources.inspect().active
        assert not errors
        _assert_no_stage(first_target)
        _assert_no_stage(queued_target)
    finally:
        release.set()
        wait_for(lambda: first.done and queued.done)


class _PlotOwner(QtWidgets.QMainWindow):
    """A plot retained by an overlay after its close event."""

    closed = QtCore.pyqtSignal(object)

    def closeEvent(self, event):
        self._closed = True
        self.closed.emit(self)
        event.accept()


def test_closed_retained_plot_cannot_publish_or_be_retained_by_history(export_window, tmp_path):
    window, errors = export_window
    owner = _PlotOwner()
    owner.threadPool = window.threadPool
    target = tmp_path / "closed-plot.csv"
    target.write_bytes(b"previous plot export")
    started, release = threading.Event(), threading.Event()
    successes = []

    def writer(name, _cancelled, _progress):
        Path(name).write_bytes(b"closed plot export")
        started.set()
        assert release.wait(5)
        return True

    task = _start(owner, target, writer, on_success=successes.append)
    try:
        wait_for(started.is_set)
        owner.close()
        assert not sip.isdeleted(owner)
        assert task.cancelled
        release.set()
        wait_for(lambda: task.done)
        assert target.read_bytes() == b"previous plot export"
        assert not successes
        assert not errors
        _assert_no_stage(target)
    finally:
        release.set()
        wait_for(lambda: task.done)
        owner.deleteLater()
        QtWidgets.QApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    # Progress history remains available while a closed plot and its datasets
    # are free to retire. This also catches retained destination validators.
    owner_reference = weakref.ref(owner)
    del owner
    gc.collect()
    assert task in window._export_task_manager.history
    assert owner_reference() is None


def test_many_queued_exports_allocate_only_the_active_stage(export_window, tmp_path, monkeypatch):
    from qplot.windows._export_paths import StagedExport

    window, _errors = export_window
    entered, release = threading.Event(), threading.Event()
    prepared = []
    original_prepare = StagedExport.prepare

    def checked_prepare(stage):
        manager = window._export_task_manager
        assert not any(task.stage.parent_fd is not None or task.stage.stage_fd is not None
                       for task in manager.tasks if task.stage is not stage)
        original_prepare(stage)
        prepared.append(stage)

    monkeypatch.setattr(StagedExport, "prepare", checked_prepare)

    def first_writer(filename, _cancelled, _progress):
        entered.set()
        assert release.wait(10)
        Path(filename).write_text("first\n", encoding="utf-8")
        return True

    def queued_writer(filename, _cancelled, _progress):
        Path(filename).write_text("queued\n", encoding="utf-8")
        return True

    first = _start(window, tmp_path / "first.csv", first_writer)
    try:
        wait_for(entered.is_set)
        queued = [_start(window, tmp_path / f"queued-{index}.csv", queued_writer)
                  for index in range(25)]
        assert prepared == [first.stage]
        assert len(list(tmp_path.glob(".*.csv.*"))) == 1
        assert all(not task.stage.stage_path and task.stage.parent_fd is None
                   and task.stage.stage_fd is None for task in queued)
        for task in queued[::2]:
            task.cancel()
        window._export_task_manager.poll()
        assert all(task.done and task.stage.closed for task in queued[::2])
        assert prepared == [first.stage]
        release.set()
        wait_for(lambda: not window._export_task_manager.tasks)
        assert first.stage.published
        assert all(task.stage.published for task in queued[1::2])
        assert len(prepared) == 1 + len(queued[1::2])
        assert not list(tmp_path.glob(".*.csv.*"))
    finally:
        release.set()
        window._export_task_manager.cancel_all()
        wait_for(lambda: not window._export_task_manager.tasks)


def test_queued_export_rechecks_approval_when_it_starts(export_window, tmp_path):
    window, _errors = export_window
    entered, release = threading.Event(), threading.Event()
    queued_writes = []

    def first_writer(filename, _cancelled, _progress):
        entered.set()
        assert release.wait(10)
        Path(filename).write_text("first", encoding="utf-8")
        return True

    def queued_writer(filename, _cancelled, _progress):
        queued_writes.append(filename)
        Path(filename).write_text("queued", encoding="utf-8")
        return True

    first = _start(window, tmp_path / "first.csv", first_writer)
    try:
        wait_for(entered.is_set)
        changed_target = tmp_path / "changed.csv"
        changed = _start(window, changed_target, queued_writer)
        following = _start(window, tmp_path / "following.csv", queued_writer)
        changed_target.write_text("created since approval", encoding="utf-8")
        release.set()
        wait_for(lambda: not window._export_task_manager.tasks)
        assert first.stage.published and following.stage.published
        assert isinstance(changed.error, UnsafeExportDestinationError)
        assert changed.stage.closed and not changed.stage.published
        assert changed_target.read_text(encoding="utf-8") == "created since approval"
        assert len(queued_writes) == 1
        _assert_no_stage(changed_target)
    finally:
        release.set()
        window._export_task_manager.cancel_all()
        wait_for(lambda: not window._export_task_manager.tasks)


def test_completion_callback_can_close_window_with_queued_exports(export_window, tmp_path):
    window, errors = export_window
    entered, release = threading.Event(), threading.Event()
    queued_writes = []

    def first_writer(filename, _cancelled, _progress):
        entered.set()
        assert release.wait(5)
        Path(filename).write_text("completed", encoding="utf-8")
        return True

    first = _start(window, tmp_path / "completed.csv", first_writer,
                   before_publish=lambda: window._export_task_manager.poll(),
                   on_success=lambda _result: window.close())
    queued = _start(window, tmp_path / "queued.csv",
                    lambda *_args: queued_writes.append(True))
    try:
        wait_for(entered.is_set)
        release.set()
        wait_for(lambda: first.done and queued.done and window._shutdown.ready)
        assert first.stage.published
        assert queued.cancelled and not queued.stage.published
        assert queued_writes == []
        assert errors == []
        assert not window._export_task_manager.tasks
        assert not window._shutdown.resources.inspect().active
    finally:
        release.set()
        window._export_task_manager.cancel_all()
        wait_for(lambda: not window._export_task_manager.tasks)
