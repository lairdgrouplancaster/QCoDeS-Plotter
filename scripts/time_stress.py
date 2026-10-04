"""Run qPlot with timing instrumentation for 2D refresh performance checks.

This manual profiling helper opens qPlot with a custom 2D plot subclass. When a
2D plot refreshes with new data, it writes timing rows to its own new CSV file in the
configured qPlot directory, usually `~/.qplot`. Use it only for local
performance investigation.
"""

import csv
import sys
import tempfile
from time import time

import numpy as np
from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from qplot import config
from qplot.windows import MainWindow, plot2d


class test2d(plot2d):
    timer = QtCore.pyqtSignal([object, float, int])

    def load_data(self, *args, **kwargs):
        self._timing_started_at = time()
        return super().load_data(*args, **kwargs)

    def refreshPlot(self, finished=True, worker=None):
        current_worker = worker if worker is not None else getattr(self, "worker", None)
        super().refreshPlot(finished, worker=worker)
        if not finished or current_worker is not getattr(self, "worker", None):
            return
        if not hasattr(self, "dataGrid"):
            return

        current_length = int(np.asarray(self.dataGrid).size)
        previous_length = getattr(self, "_timed_data_length", None)
        if current_length == previous_length:
            return

        self._timed_data_length = current_length
        started_at = getattr(self, "_timing_started_at", time())
        self.timer.emit(self, time() - started_at, current_length)


class testMain(MainWindow):
    def openWin(self, widget, *args, **kwargs):
        timed_widget = test2d if widget is plot2d else widget
        window_count = len(self.windows)
        super().openWin(timed_widget, *args, **kwargs)
        if len(self.windows) <= window_count:
            return

        window = self.windows[-1]
        if isinstance(window, test2d):
            window.timer.connect(save_time_log)


@QtCore.pyqtSlot(object, float, int)
def save_time_log(win, run_time, data_length):
    if getattr(win, "_closed", False) and getattr(win, "_merged_trace_users", 0) <= 0:
        return
    file = getattr(win, "_timing_log_file", None)
    if file is None:
        file = tempfile.NamedTemporaryFile(
            mode="w",
            newline="",
            prefix="qplot-timing-",
            suffix=".csv",
            dir=win.config.default_path,
            delete=False,
        )
        win._timing_log_file = file
        win._timing_log_path = file.name
        win.closed.connect(close_time_log)
        win.destroyed.connect(lambda *_args: file.close())
    if file.closed:
        return
    print(f"writing {file.name}")
    csv.writer(file).writerow([data_length, run_time])
    file.flush()


@QtCore.pyqtSlot(object)
def close_time_log(win):
    if getattr(win, "_merged_trace_users", 0) <= 0:
        file = getattr(win, "_timing_log_file", None)
        if file is not None:
            file.close()


if __name__ == "__main__":
    conf = config()
    app = qtw.QApplication(sys.argv)
    w = testMain()
    app.exec()
