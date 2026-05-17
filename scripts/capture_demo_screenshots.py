"""Generate qPlot documentation screenshots from a small synthetic database."""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
ASSET_DIR = REPO_ROOT / "docs" / "assets"
DEFAULT_WORK_DIR = Path(tempfile.gettempdir()) / "qplot-demo"
WORK_DIR = Path(os.environ.get("QPLOT_DEMO_WORKDIR", str(DEFAULT_WORK_DIR)))
DB_PATH = WORK_DIR / "qplot-demo.db"


def configure_environment():
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("MPLCONFIGDIR", str(WORK_DIR / "matplotlib"))
    os.environ["HOME"] = str(WORK_DIR / "home")
    (WORK_DIR / "home").mkdir(parents=True, exist_ok=True)
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
            datasaver.add_result((gate, float(gate_value)), (current, float(signal)))
        line_run_id = datasaver.dataset.run_id

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
                ripple = 12.0 * np.cos(5.0 * gate_value) * np.sin(4.0 * bias_value)
                datasaver.add_result(
                    (gate, float(gate_value)),
                    (bias, float(bias_value)),
                    (conductance, float(peak + ripple)),
                )
        heatmap_run_id = datasaver.dataset.run_id

    return line_run_id, heatmap_run_id


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


def capture_screenshots(line_run_id, heatmap_run_id):
    from PyQt6 import QtWidgets
    from qcodes.dataset import load_by_id

    from qplot.diagnostics import configure_logging, install_excepthook
    from qplot.windows import MainWindow

    configure_logging()
    install_excepthook()

    app = QtWidgets.QApplication(["capture_demo_screenshots"])
    main_window = MainWindow()
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

    heatmap_dataset = load_by_id(heatmap_run_id)
    main_window.updateSelected(heatmap_dataset.guid)
    settle(app)
    main_path = save_widget(app, main_window, "qplot-main-window.png")

    line_dataset = load_by_id(line_run_id)
    line_param = dependent_parameter(line_dataset, 1)
    main_window.ds = line_dataset
    main_window.openPlot(params=[line_param], show=True)
    line_window = main_window.windows[-1]
    line_window.resize(920, 620)
    wait_for(
        app,
        lambda: hasattr(line_window, "axis_data")
        and not getattr(line_window.worker, "running", False),
    )
    line_path = save_widget(app, line_window, "qplot-line-plot.png")

    heatmap_param = dependent_parameter(heatmap_dataset, 2)
    main_window.ds = heatmap_dataset
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

    dialog.close()
    main_window.close_plot_windows(confirm=False, status=False)
    main_window.close()
    settle(app, 0.2)
    app.quit()

    return main_path, line_path, heatmap_path, colorbar_path


def main():
    configure_environment()
    run_ids = build_demo_database()
    paths = capture_screenshots(*run_ids)
    for path in paths:
        print(path.relative_to(REPO_ROOT))


if __name__ == "__main__":
    main()
