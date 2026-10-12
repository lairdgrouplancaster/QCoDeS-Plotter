"""Shared lifecycle helpers for tests that create a real qPlot main window."""

import time

from PyQt6 import QtCore, QtWidgets

_WORKER_NAMES = (
    "_database_load_worker",
    "_database_detail_worker",
    "_database_expensive_detail_worker",
    "_database_refresh_worker",
    "_database_selected_run_worker",
    "_test_database_generation_worker",
)

_POOL_NAMES = (
    "threadPool",
    "databaseLoadThreadPool",
    "databaseDetailThreadPool",
    "databaseExpensiveDetailThreadPool",
    "databaseRefreshThreadPool",
    "databaseSelectedRunThreadPool",
    "testDatabaseGenerationThreadPool",
)


def close_main_window(window, timeout_ms=12_000):
    """Cancel and join window work before releasing its database snapshots."""

    window.startupDatabaseTimer.stop()
    window.monitor.stop()
    window._automatic_refresh_shutdown = True
    window.infoBox.preview.shutdown()
    bridge = getattr(window, "_trusted_derived_bridge", None)
    if bridge is not None:
        bridge.shutdown()
    window.close_plot_windows(confirm=False, status=False)

    for worker_name in _WORKER_NAMES:
        worker = getattr(window, worker_name, None)
        cancel = getattr(worker, "cancel", None)
        if callable(cancel):
            cancel()
    window._cancel_plot_work()

    for pool_name in _POOL_NAMES:
        pool = getattr(window, pool_name)
        if not pool.waitForDone(timeout_ms):
            active_threads = pool.activeThreadCount()
            raise AssertionError(
                f"{pool_name} did not stop within {timeout_ms} ms "
                f"({active_threads} active thread(s))"
            )

    app = QtWidgets.QApplication.instance()
    if app is not None:
        app.processEvents()
    window.close_database(status=False)
    # Deleting the Qt window bypasses its close event. Finish retiring the
    # asynchronous services before deleting their owner and its reaper timers.
    deadline = time.monotonic() + timeout_ms / 1000
    while window._shutdown_background_work_active():
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"Window background work did not stop: {window._shutdown_last_diagnostics}"
            )
        if app is not None:
            app.processEvents()
        time.sleep(0.01)
    assert window.ds is None
    assert window.dataset_holder == {}
    window.hide()
    window.deleteLater()
    if app is not None:
        app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()
