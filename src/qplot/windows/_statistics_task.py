"""Cancellable whole-measurement statistics on the window's managed pool."""

from concurrent.futures import Future
from threading import Event

from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from qplot.diagnostics import log_exception

from . import _plot_marquee
from ._dataset_statistics import read_measurement_statistics


def cancel_statistics(window):
    progress = getattr(window, "_statistics_progress", None)
    if progress is not None:
        progress.reject()


def start_statistics(window, key, parameter_name, service):
    cancel_statistics(window)
    generation = getattr(window, "_database_load_generation", None)
    stopped = Event()
    result: Future[str | None] = Future()

    def calculate():
        try:
            text = read_measurement_statistics(key, parameter_name, service, stopped.is_set)
        except Exception as error:
            result.set_exception(error.with_traceback(None))
        else:
            result.set_result(text)

    progress = qtw.QProgressDialog(
        f"Calculating statistics for {parameter_name}…", "Cancel", 0, 0, window,
    )
    progress.setWindowTitle("Measurement statistics")
    window._statistics_progress = progress
    timer = QtCore.QTimer(progress)

    def retire():
        stopped.set()
        timer.stop()
        if window._statistics_progress is progress:
            window._statistics_progress = None
        progress.hide()
        progress.deleteLater()

    def poll():
        if stopped.is_set():
            return
        if (generation != getattr(window, "_database_load_generation", None)
                or getattr(window, "_shutdown_started", False)):
            retire()
            return
        if not result.done():
            return
        retire()
        try:
            if not window._bound_plot_dataset_key_is_current(key):
                window._request_plot_source_recovery(key)
                window.show_status("Statistics cancelled because the source database changed.", 5000)
                return
            text = result.result()
        except Exception as error:
            log_exception("Dataset statistics failed", error, __name__)
            window.show_error("Statistics Failed", "Could not read the measurement.", str(error))
            return
        if text is None:
            window.show_status("No finite data points in this measurement.", 5000)
            return
        dialog = _plot_marquee.new_statistics_dialog(window, text, "Measurement statistics")
        dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    progress.destroyed.connect(stopped.set)
    progress.canceled.connect(retire)
    progress.rejected.connect(retire)
    timer.timeout.connect(poll)
    timer.start(40)
    progress.show()
    try:
        window.threadPool.start(calculate)
    except Exception:
        retire()
        raise
