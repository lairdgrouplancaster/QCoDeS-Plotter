"""Exercise final Qt ownership cleanup in its own application process."""

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from qplot.__main__ import _finalize_confirmed_process_shutdown
from qplot.windows.main import MainWindow

_OWNERSHIP_PROBE = textwrap.dedent(
    """
    import sys
    from pathlib import Path

    from PyQt6 import QtCore, QtWidgets, sip
    from qcodes.dataset import (
        Measurement, initialise_or_create_database_at, load_or_create_experiment,
    )

    from qplot.__main__ import _finalize_confirmed_process_shutdown
    from qplot.configuration.config import config
    from qplot.diagnostics import configure_logging
    from qplot.windows._database_detail_state import detail_channel
    from qplot.windows._plotWin import plotWidget
    from qplot.windows.main import MainWindow
    from tests.windows.test_plot_integration import wait_for

    directory = Path(sys.argv[1])
    close_before_quit = sys.argv[2] == "closed"
    config.default_path = str(directory / "settings")
    config.default_file = str(directory / "settings" / config.config_file_name)
    configure_logging(directory / "qplot.log", force=True)
    database = directory / "teardown.db"
    initialise_or_create_database_at(str(database), journal_mode="DELETE")
    experiment = load_or_create_experiment("teardown", sample_name="test")
    guids = []
    try:
        for dimensions in (1, 2, 1):
            measurement = Measurement(exp=experiment, name=f"plot{dimensions}d")
            measurement.register_custom_parameter("x")
            if dimensions == 2:
                measurement.register_custom_parameter("y")
            measurement.register_custom_parameter(
                "signal", setpoints=("y", "x") if dimensions == 2 else ("x",),
            )
            with measurement.run(write_in_background=False) as saver:
                guids.append(saver.dataset.guid)
                for y in range(2 if dimensions == 2 else 1):
                    for x in range(3):
                        values = [("x", float(x)), ("signal", float(x + y))]
                        if dimensions == 2:
                            values.append(("y", float(y)))
                        saver.add_result(*values)
    finally:
        experiment.conn.close()

    app = QtWidgets.QApplication([])
    window = MainWindow()
    window.startupDatabaseTimer.stop()
    window.monitor.stop()
    window.config.config["user_preference"]["confirm_close"] = False
    window.config.config["user_preference"]["confirm_close_all"] = False
    window.config.config["runtime_settings"]["del_grace_period"] = 0
    assert window.load_file(str(database))
    wait_for(lambda: not window._database_load_active and not detail_channel(window).active)

    # Hold every plot and its child wrappers deliberately, including a hidden
    # overlay source. Closing a window must not leave native ownership for
    # QApplication's unordered Python-wrapper cleanup to dismantle.
    plots = []
    children = []
    for guid, visible in ((guids[0], True), (guids[1], True), (guids[2], False)):
        count = len(window.windows)
        window.openPlot(guid=guid, show=visible)
        wait_for(lambda: len(window.windows) > count)
        plot = window.windows[-1]
        wait_for(lambda: hasattr(plot, "axis_data") and not plot.worker.running)
        plot.monitor.stop()
        plots.append(plot)
        children.extend((plot.widget, plot.plot_state_overlay,
                         plot.plot_state_overlay.progress_timer))
    assert [type(plot).__name__ for plot in plots] == ["plot1d", "plot2d", "plot1d"]
    if close_before_quit:
        window.close_plot_windows(confirm=False, status=False)
        assert not window.windows
    assert all(not sip.isdeleted(plot) for plot in plots)

    QtCore.QTimer.singleShot(0, window.quit_application)
    QtCore.QTimer.singleShot(15000, lambda: app.exit(91))
    assert app.exec() == 0
    assert window._shutdown_ready
    assert not window._shutdown_background_work_active()

    class FailSafe:
        armed = True
        disarmed = False

        def watchdog_operational(self):
            return True

        def wait_for_forced_exit(self):
            raise AssertionError("Quiescent cleanup requested forced exit")

        def disarm(self):
            assert sip.isdeleted(app)
            self.disarmed = True

    fail_safe = FailSafe()
    original_delete = sip.delete
    application_deletions = []

    def observed_delete(item):
        if item is app:
            # Fail before entering native cleanup if owner-first destruction
            # regresses, instead of deliberately provoking a process crash.
            assert all(sip.isdeleted(plot) for plot in plots), "Live plot owners at QApplication deletion"
            assert all(sip.isdeleted(child) for child in children), "Live plot children at QApplication deletion"
            assert sip.isdeleted(window)
            assert not any(isinstance(widget, plotWidget) for widget in app.topLevelWidgets())
            application_deletions.append(True)
        original_delete(item)

    sip.delete = observed_delete
    try:
        _finalize_confirmed_process_shutdown(
            app, window, fail_safe, retain_objects=False,
        )
    except BaseException:
        # Avoid the very destructor under test during an assertion failure.
        import os
        import traceback
        traceback.print_exc()
        sys.stderr.flush()
        os._exit(1)
    assert application_deletions == [True]
    assert fail_safe.disarmed
    print("OWNERSHIP_TEARDOWN_COMPLETED", flush=True)
    """
)


@pytest.mark.parametrize("plot_state", ["open", "closed"])
def test_finalization_destroys_retained_plot_owners_before_application(tmp_path, plot_state):
    result = subprocess.run(
        [sys.executable, "-c", _OWNERSHIP_PROBE, str(tmp_path), plot_state],
        cwd=Path(__file__).resolve().parents[2],
        env=dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONFAULTHANDLER="1"),
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OWNERSHIP_TEARDOWN_COMPLETED" in result.stdout
    assert "Fatal Python error" not in result.stderr


@pytest.mark.parametrize(
    "armed,retain_objects,active,deadline_exhausted,expected",
    [
        (False, False, False, False, []),
        (True, True, False, False, ["disarm"]),
        (True, True, True, False, ["wait"]),
        (True, False, True, False, ["wait"]),
        (True, False, False, True, ["wait"]),
    ],
)
def test_finalization_preserves_retained_or_busy_qt_ownership(
    monkeypatch, armed, retain_objects, active, deadline_exhausted, expected,
):
    from PyQt6 import sip

    operations = []
    window = SimpleNamespace(_shutdown_deadline_exhausted=deadline_exhausted)
    fail_safe = SimpleNamespace(
        armed=armed,
        disarm=lambda: operations.append("disarm"),
        wait_for_forced_exit=lambda: operations.append("wait"),
    )
    monkeypatch.setattr(MainWindow, "_shutdown_background_work_active", lambda _self: active)
    monkeypatch.setattr(sip, "delete", lambda _item: operations.append("delete"))

    _finalize_confirmed_process_shutdown(
        object(), window, fail_safe, retain_objects=retain_objects,
    )

    assert operations == expected


def test_watchdog_failure_during_plot_cleanup_stops_further_destruction(monkeypatch):
    from PyQt6 import sip

    from qplot.windows import _plotWin

    class PlotOwner:
        pass

    first, second = PlotOwner(), PlotOwner()
    operations = []
    watchdog_healthy = True
    window = SimpleNamespace(_shutdown_deadline_exhausted=False)
    app = SimpleNamespace(topLevelWidgets=lambda: [first, second])
    fail_safe = SimpleNamespace(
        armed=True,
        watchdog_operational=lambda: watchdog_healthy,
        disarm=lambda: operations.append("disarm"),
        wait_for_forced_exit=lambda: operations.append("wait"),
    )

    def delete_owner(item):
        nonlocal watchdog_healthy
        operations.append(item)
        watchdog_healthy = False

    monkeypatch.setattr(MainWindow, "_shutdown_background_work_active", lambda _self: False)
    monkeypatch.setattr(_plotWin, "plotWidget", PlotOwner)
    monkeypatch.setattr(sip, "isdeleted", lambda _item: False)
    monkeypatch.setattr(sip, "delete", delete_owner)

    _finalize_confirmed_process_shutdown(app, window, fail_safe, retain_objects=False)

    assert operations == [first, "wait"]
