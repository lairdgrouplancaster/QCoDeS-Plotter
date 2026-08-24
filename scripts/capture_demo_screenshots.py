"""Generate qPlot documentation screenshots from a small synthetic database."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ASSET_DIR = REPO_ROOT / "docs" / "assets"
ASSET_DIR = Path(os.environ.get("QPLOT_DEMO_ASSET_DIR", str(DEFAULT_ASSET_DIR)))
DEFAULT_WORK_DIR = Path(tempfile.gettempdir()) / "qplot-demo"
WORK_DIR = Path(os.environ.get("QPLOT_DEMO_WORKDIR", str(DEFAULT_WORK_DIR)))
DB_PATH = WORK_DIR / "qplot-demo.db"


def configure_environment():
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("MPLCONFIGDIR", str(WORK_DIR / "matplotlib"))
    home_dir = WORK_DIR / "home"
    os.environ["HOME"] = str(home_dir)
    if os.name == "nt":
        # ntpath.expanduser() prefers USERPROFILE over HOME. Keep screenshot
        # generation isolated from a real qPlot configuration on Windows too.
        os.environ["USERPROFILE"] = str(home_dir)
    home_dir.mkdir(parents=True, exist_ok=True)
    (WORK_DIR / "matplotlib").mkdir(parents=True, exist_ok=True)


def build_demo_database():
    from qcodes.dataset import (
        Measurement,
        initialise_or_create_database_at,
        load_or_create_experiment,
    )
    from qcodes.parameters import ManualParameter

    for path in (DB_PATH, DB_PATH.with_suffix(".db-shm"), DB_PATH.with_suffix(".db-wal")):
        path.unlink(missing_ok=True)

    initialise_or_create_database_at(str(DB_PATH))
    experiment = load_or_create_experiment("qplot_demo", sample_name="synthetic")

    try:
        gate = ManualParameter("gate", label="Gate voltage", unit="V")
        bias = ManualParameter("bias", label="Bias voltage", unit="mV")
        current = ManualParameter("current", label="Current", unit="nA")
        conductance = ManualParameter("conductance", label="Conductance", unit="uS")

        line_meas = Measurement(exp=experiment, name="line_demo")
        line_meas.register_parameter(gate)
        line_meas.register_parameter(current, setpoints=(gate,))
        with line_meas.run() as datasaver:
            for gate_value in np.linspace(-2.0, 2.0, 81):
                signal = np.sin(gate_value * 3.0) * 40.0 + gate_value * 5.0
                datasaver.add_result(
                    (gate, float(gate_value)),
                    (current, float(signal)),
                )
            line_guid = datasaver.dataset.guid

        heatmap_meas = Measurement(exp=experiment, name="heatmap_demo")
        heatmap_meas.register_parameter(gate)
        heatmap_meas.register_parameter(bias)
        heatmap_meas.register_parameter(conductance, setpoints=(gate, bias))
        with heatmap_meas.run() as datasaver:
            for gate_value in np.linspace(-2.0, 2.0, 45):
                for bias_value in np.linspace(-1.0, 1.0, 35):
                    peak = np.exp(
                        -(
                            ((gate_value - 0.3) ** 2) * 1.4
                            + ((bias_value + 0.1) ** 2) * 4.0
                        )
                    ) * 70.0
                    ripple = (
                        12.0
                        * np.cos(5.0 * gate_value)
                        * np.sin(4.0 * bias_value)
                    )
                    datasaver.add_result(
                        (gate, float(gate_value)),
                        (bias, float(bias_value)),
                        (conductance, float(peak + ripple)),
                    )
            heatmap_guid = datasaver.dataset.guid
    finally:
        experiment.conn.close()

    return line_guid, heatmap_guid


def wait_for(app, predicate, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            app.processEvents()
            return
        time.sleep(0.03)
    raise TimeoutError("Timed out waiting for screenshot state")


def settle(app, duration=0.4):
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.03)


def save_widget(app, widget, filename):
    path = ASSET_DIR / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    widget.raise_()
    widget.activateWindow()
    settle(app, 0.3)
    pixmap = widget.grab()
    if pixmap.isNull():
        raise RuntimeError(f"Could not capture {filename}")
    if not pixmap.save(str(path)):
        raise RuntimeError(f"Could not save {path}")
    return path


def dependent_parameter(dataset, dimensions):
    for param in dataset.get_parameters():
        if param.depends_on and len(param.depends_on_) == dimensions:
            return param
    raise RuntimeError(f"No {dimensions}D dependent parameter in run {dataset.run_id}")


def select_dataset(main_window, guid):
    """Select a run through qPlot's dataset ownership and cleanup path."""

    main_window.updateSelected(guid)
    dataset = main_window.ds
    if dataset is None or dataset.guid != guid:
        raise RuntimeError(f"Could not select run with GUID {guid}")
    return dataset


def install_snapshot_cleanup_audit():
    """Record private snapshot handles when the smoke test requests an audit."""

    if os.environ.get("QPLOT_DEMO_VERIFY_CLEANUP") != "1":
        return None

    from qplot.datahandling import readonly as readonly_module

    records = []
    original_attach = readonly_module._attach_snapshot_cleanup

    def record_snapshot_connection(connection, snapshot):
        snapshot_directory = Path(snapshot.name)
        original_attach(connection, snapshot)
        records.append((connection, snapshot_directory))

    readonly_module._attach_snapshot_cleanup = record_snapshot_connection
    return readonly_module, original_attach, records


def verify_snapshot_cleanup(records):
    """Require every audited SQLite snapshot to be explicitly released."""

    if not records:
        raise RuntimeError("No private snapshot connections were audited")

    for connection, snapshot_directory in records:
        try:
            connection.cursor()
        except (sqlite3.ProgrammingError, RuntimeError):
            pass
        else:
            raise RuntimeError(
                f"Snapshot connection remained open: {snapshot_directory}"
            )
        if snapshot_directory.exists():
            raise RuntimeError(f"Snapshot directory remained: {snapshot_directory}")


def capture_screenshots(line_guid, heatmap_guid):
    from PyQt6 import QtCore, QtWidgets, sip

    from qplot.diagnostics import configure_logging, install_excepthook
    from qplot.windows import MainWindow

    configure_logging()
    install_excepthook()

    audit = install_snapshot_cleanup_audit()
    app = QtWidgets.QApplication(["capture_demo_screenshots"])
    app.setQuitOnLastWindowClosed(False)
    main_window = MainWindow()
    dialog = None
    plot_windows = []
    try:
        main_window.startupDatabaseTimer.stop()
        main_window.config.config["user_preference"]["confirm_close"] = False
        main_window.config.config["user_preference"]["confirm_close_all"] = False
        main_window.close_database(status=False)
        main_window.resize(1120, 760)
        main_window.load_file(str(DB_PATH))
        wait_for(
            app,
            lambda: (
                not main_window._database_load_active
                and main_window.RunList.topLevelItemCount() >= 2
            ),
        )

        heatmap_dataset = select_dataset(main_window, heatmap_guid)
        settle(app)
        main_path = save_widget(app, main_window, "qplot-main-window.png")

        line_dataset = select_dataset(main_window, line_guid)
        line_param = dependent_parameter(line_dataset, 1)
        main_window.openPlot(params=[line_param], show=True)
        line_window = main_window.windows[-1]
        line_window.resize(920, 620)
        wait_for(
            app,
            lambda: hasattr(line_window, "axis_data")
            and not getattr(line_window.worker, "running", False),
        )
        line_path = save_widget(app, line_window, "qplot-line-plot.png")

        heatmap_dataset = select_dataset(main_window, heatmap_guid)
        heatmap_param = dependent_parameter(heatmap_dataset, 2)
        main_window.openPlot(params=[heatmap_param], show=True)
        heatmap_window = main_window.windows[-1]
        heatmap_window.resize(980, 660)
        wait_for(
            app,
            lambda: hasattr(heatmap_window, "dataGrid")
            and not getattr(heatmap_window.worker, "running", False),
        )
        heatmap_window.open_colorbar_scale_dialog()
        dialog = heatmap_window.colorbar_scale_dialog
        dialog.resize(620, 660)
        settle(app)
        heatmap_path = save_widget(app, heatmap_window, "qplot-heatmap.png")
        colorbar_path = save_widget(app, dialog, "qplot-color-scale-dialog.png")

        return main_path, line_path, heatmap_path, colorbar_path
    finally:
        try:
            if dialog is not None:
                dialog.close()
            plot_windows = list(main_window.windows)
            main_window.close_plot_windows(confirm=False, status=False)
            main_window.close()
            wait_for(app, lambda: main_window._shutdown_ready)
            app.processEvents()
            if audit is not None:
                readonly_module, original_attach, records = audit
                verify_snapshot_cleanup(records)
        finally:
            if audit is not None:
                readonly_module._attach_snapshot_cleanup = original_attach
            # Destroy every top-level Qt object while QApplication is still
            # alive.  Leaving closed plot windows to Python interpreter
            # teardown can crash Qt's offscreen platform plugin on Linux.
            for widget in (dialog, *plot_windows, main_window):
                if widget is not None and not sip.isdeleted(widget):
                    widget.deleteLater()
            app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
            app.processEvents()
            app.quit()


def main():
    configure_environment()
    run_ids = build_demo_database()
    paths = capture_screenshots(*run_ids)
    for path in paths:
        try:
            display_path = path.relative_to(REPO_ROOT)
        except ValueError:
            display_path = path
        print(display_path)


if __name__ == "__main__":
    main()
