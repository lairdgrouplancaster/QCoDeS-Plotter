"""Shared lifecycle helpers for tests that create a real qPlot main window."""

import time

from PyQt6 import QtCore, QtWidgets, sip


def close_main_window(window, timeout_ms=12_000):
    """Cancel and join window work before releasing its database snapshots."""

    from qplot.windows._plotWin import plotWidget

    # Closing a plot intentionally retains it when another plot uses its data.
    # Once the entire test window shuts down, delete even those hidden sources.
    plots = [
        widget for widget in QtWidgets.QApplication.topLevelWidgets()
        if isinstance(widget, plotWidget)
        and getattr(widget, "threadPool", None) is window.threadPool
    ]
    window.startupDatabaseTimer.stop()
    window.monitor.stop()
    window._automatic_refresh_shutdown = True
    window.infoBox.preview.shutdown()
    bridge = getattr(window, "_trusted_derived_bridge", None)
    if bridge is not None:
        bridge.shutdown()
    window.close_plot_windows(confirm=False, status=False)

    resources = window._shutdown.resources
    resources.cancel_database_workers()
    window._cancel_plot_work()

    for pool_name, pool in resources.pools:
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
                f"Window background work did not stop: {window._shutdown.state.diagnostics}"
            )
        if app is not None:
            app.processEvents()
        time.sleep(0.01)
    assert window.ds is None
    assert window.dataset_holder == {}
    for plot in plots:
        if not sip.isdeleted(plot):
            plot.deleteLater()
    window.hide()
    window.deleteLater()
    if app is not None:
        app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()
    assert all(sip.isdeleted(plot) for plot in plots)
