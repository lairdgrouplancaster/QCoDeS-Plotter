"""Generate qPlot documentation screenshots from a small synthetic database."""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import logging
import os
import platform
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ASSET_DIR = REPO_ROOT / "docs" / "assets"
ASSET_DIR = Path(os.environ.get("QPLOT_DEMO_ASSET_DIR", str(DEFAULT_ASSET_DIR)))
DEFAULT_WORK_DIR = Path(tempfile.gettempdir()) / "qplot-demo"
WORK_DIR = Path(os.environ.get("QPLOT_DEMO_WORKDIR", str(DEFAULT_WORK_DIR)))


def configure_environment():
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QT_SCALE_FACTOR"] = "1"
    os.environ["QT_FONT_DPI"] = "96"
    os.environ["QT_AUTO_SCREEN_SCALE_FACTOR"] = "0"
    os.environ.pop("QT_SCREEN_SCALE_FACTORS", None)
    os.environ.pop("QT_DEVICE_PIXEL_RATIO", None)
    os.environ.pop("QT_QPA_PLATFORMTHEME", None)
    os.environ["QT_STYLE_OVERRIDE"] = "Fusion"
    os.environ["TZ"] = "UTC"
    if hasattr(time, "tzset"):
        time.tzset()
    os.environ.setdefault("MPLCONFIGDIR", str(WORK_DIR / "matplotlib"))
    home_dir = WORK_DIR / "home"
    os.environ["HOME"] = str(home_dir)
    if os.name == "nt":
        # ntpath.expanduser() prefers USERPROFILE over HOME. Keep screenshot
        # generation isolated from a real qPlot configuration on Windows too.
        os.environ["USERPROFILE"] = str(home_dir)
    home_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir = WORK_DIR / "runtime"
    runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.environ["XDG_RUNTIME_DIR"] = str(runtime_dir)
    for key, subdirectory in (("XDG_CONFIG_HOME", "config"), ("XDG_CACHE_HOME", "cache"),
                              ("XDG_DATA_HOME", "data")):
        private = WORK_DIR / subdirectory
        private.mkdir(parents=True, exist_ok=True)
        os.environ[key] = str(private)
    (WORK_DIR / "matplotlib").mkdir(parents=True, exist_ok=True)


@contextmanager
def build_demo_database():
    """Keep the demo database in a fresh directory until its viewers close."""
    with tempfile.TemporaryDirectory(prefix="qplot-demo-", dir=WORK_DIR) as directory:
        database_path = Path(directory) / "qplot-demo.db"
        yield database_path, *_populate_demo_database(database_path)


def _populate_demo_database(database_path):
    import numpy as np
    from qcodes.dataset import (
        Measurement,
        initialise_or_create_database_at,
        load_or_create_experiment,
    )
    from qcodes.parameters import ManualParameter

    initialise_or_create_database_at(str(database_path), journal_mode="WAL")
    experiment = load_or_create_experiment("qplot_demo", sample_name="synthetic")

    try:
        gate = ManualParameter("gate", label="Gate voltage", unit="V")
        bias = ManualParameter("bias", label="Bias voltage", unit="mV")
        current = ManualParameter("current", label="Current", unit="nA")
        conductance = ManualParameter("conductance", label="Conductance", unit="uS")

        line_meas = Measurement(exp=experiment, name="line_demo")
        line_meas.register_parameter(gate)
        line_meas.register_parameter(current, setpoints=(gate,))
        with line_meas.run(write_in_background=False) as datasaver:
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
        with heatmap_meas.run(write_in_background=False) as datasaver:
            for gate_value in np.linspace(-2.0, 2.0, 45):
                for bias_value in np.linspace(-1.0, 1.0, 35):
                    peak = (
                        np.exp(
                            -(
                                ((gate_value - 0.3) ** 2) * 1.4
                                + ((bias_value + 0.1) ** 2) * 4.0
                            )
                        )
                        * 70.0
                    )
                    ripple = 12.0 * np.cos(5.0 * gate_value) * np.sin(4.0 * bias_value)
                    datasaver.add_result(
                        (gate, float(gate_value)),
                        (bias, float(bias_value)),
                        (conductance, float(peak + ripple)),
                    )
            heatmap_guid = datasaver.dataset.guid
        # Only this freshly generated synthetic database may be written here.
        # Fix visible dates and GUIDs without mocking the application's reader.
        line_guid = "00000000-0000-0000-0000-018cc251f400"
        heatmap_guid = "00000000-0000-0000-0000-018cc2521b10"
        for run_id, guid in ((1, line_guid), (2, heatmap_guid)):
            experiment.conn.execute(
                "UPDATE runs SET guid=?, run_timestamp=?, completed_timestamp=? WHERE run_id=?",
                (guid, 1704067200 + run_id * 10, 1704067205 + run_id * 10, run_id),
            )
        experiment.conn.execute("UPDATE experiments SET start_time=1704067200")
        experiment.conn.commit()
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


def save_widget(app, widget, filename):
    from qplot._auxiliary_paths import ensure_safe_auxiliary_path

    path = ASSET_DIR / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    wait_for(app, lambda: widget.isVisible() and widget.width() > 0)
    if widget.layout() is not None:
        widget.layout().activate()
    app.processEvents()
    # QWidget.grab performs synchronous rendering, including child widgets.
    widget.repaint()
    pixmap = widget.grab()
    if pixmap.isNull():
        raise RuntimeError(f"Could not capture {filename}")
    ensure_safe_auxiliary_path(path)
    if not pixmap.save(str(path)):
        raise RuntimeError(f"Could not save {path}")
    return path


def select_run(app, main_window, guid):
    """Wait for the selected run's current detail publication without a DataSet."""
    from qplot.datahandling.trusted_live_service import TRUSTED_LIVE_MODE

    def selection_ready():
        if main_window._selected_run_guid != guid:
            return False
        if main_window._database_access_mode != TRUSTED_LIVE_MODE:
            # Snapshot fallback publishes cached basics synchronously.
            return True
        publication = main_window._trusted_derived_bridge._selected_detail_publication
        return publication is not None and publication.run_guid == guid

    main_window.updateSelected(guid)
    wait_for(app, selection_ready)
    if main_window._selected_run_guid != guid or main_window.ds is not None:
        raise RuntimeError(f"Could not select run with GUID {guid}")


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


def verify_snapshot_cleanup(records, *, trusted_service=None):
    """Require every audited SQLite snapshot to be explicitly released."""

    if trusted_service is not None and not trusted_service.wait_closed(0):
        raise RuntimeError("Trusted reader remained open after shutdown")
    if not records and trusted_service is None:
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


def capture_screenshots(database_path, line_guid, heatmap_guid):
    import numpy as np
    from PyQt6 import QtCore, QtGui, QtWidgets, sip

    from qplot.__main__ import _configure_application_identity
    from qplot.configuration.config import config
    from qplot.windows import MainWindow

    # These configuration defaults are bound at import time. All imports of
    # qPlot occur in the isolated child, after configure_environment().
    preferences = config()
    preferences.update_many({
        "user_preference.theme": "light", "user_preference.font_size": 10,
        "user_preference.confirm_close": False,
        "user_preference.confirm_close_all": False,
        "runtime_settings.del_grace_period": 0,
        "updates.check_on_launch": False,
    })
    audit = install_snapshot_cleanup_audit()
    app = QtWidgets.QApplication(["capture_demo_screenshots"])
    _configure_application_identity(app)
    app.setStyle("Fusion")
    QtCore.QLocale.setDefault(QtCore.QLocale("en_GB"))
    font_path = Path(importlib.util.find_spec("matplotlib").origin).parent / "mpl-data/fonts/ttf/DejaVuSans.ttf"
    for path in sorted(font_path.parent.glob("DejaVuSans*.ttf")):
        if QtGui.QFontDatabase.addApplicationFont(str(path)) < 0:
            raise RuntimeError(f"Could not load the screenshot font: {path.name}")
    QtGui.QFont.insertSubstitution("Monospace", "DejaVu Sans Mono")
    app.setFont(QtGui.QFont("DejaVu Sans", 10))
    app.setQuitOnLastWindowClosed(False)
    main_window = MainWindow()
    dialog = None
    trusted_service = None
    plot_windows = []
    try:
        main_window.startupDatabaseTimer.stop()
        main_window.config.config["user_preference"]["confirm_close"] = False
        main_window.config.config["user_preference"]["confirm_close_all"] = False
        main_window.close_database(status=False)
        main_window.resize(1120, 760)
        main_window.load_file(str(database_path))
        wait_for(
            app,
            lambda: (
                not main_window._database_load_active
                and main_window.RunList.topLevelItemCount() >= 2
            ),
        )

        trusted_service = main_window._trusted_read_service
        main_window.monitor.stop()
        select_run(app, main_window, heatmap_guid)
        from qplot.windows._database_detail_state import detail_channel

        def main_ready():
            return (not detail_channel(main_window).active
                    and not detail_channel(main_window, True).active
                    and all(cell._has_rendered_previews
                            for cell in main_window.RunList.preview_cells.values())
                    and len(main_window.RunList.preview_cells) == 2)

        try:
            wait_for(app, main_ready)
        except TimeoutError as error:
            raise RuntimeError(
                f"Main window incomplete: details={detail_channel(main_window).active}, "
                f"expensive={detail_channel(main_window, True).active}, "
                f"previews={[(key, cell._has_rendered_previews) for key, cell in main_window.RunList.preview_cells.items()]}"
            ) from error
        main_window.show_status("Synthetic demonstration data")
        main_path = save_widget(app, main_window, "qplot-main-window.png")

        select_run(app, main_window, line_guid)
        main_window.open_selected_measurement("current")
        wait_for(app, lambda: len(main_window.windows) == 1)
        line_window = main_window.windows[-1]
        line_window.resize(920, 620)
        wait_for(
            app,
            lambda: (
                hasattr(line_window, "axis_data")
                and not getattr(line_window.worker, "running", False)
                and getattr(line_window, "_qplot_display_synchronized", False)
            ),
        )
        expected_x = np.linspace(-2.0, 2.0, 81)
        expected_y = np.sin(expected_x * 3) * 40 + expected_x * 5
        np.testing.assert_allclose(line_window.line.xData, expected_x)
        np.testing.assert_allclose(line_window.line.yData, expected_y, atol=1e-12)
        if line_window.ds.guid != line_guid:
            raise RuntimeError("Incorrect line dataset")
        line_window.show_status("81 synthetic samples")
        line_path = save_widget(app, line_window, "qplot-line-plot.png")

        select_run(app, main_window, heatmap_guid)
        main_window.open_selected_measurement("conductance")
        wait_for(app, lambda: len(main_window.windows) == 2)
        heatmap_window = main_window.windows[-1]
        heatmap_window.resize(980, 660)
        wait_for(
            app,
            lambda: (
                hasattr(heatmap_window, "dataGrid")
                and not getattr(heatmap_window.worker, "running", False)
                and getattr(heatmap_window, "_qplot_display_synchronized", False)
            ),
        )
        gate, bias = np.meshgrid(np.linspace(-2, 2, 45), np.linspace(-1, 1, 35))
        expected_grid = np.exp(-((gate - .3) ** 2 * 1.4 + (bias + .1) ** 2 * 4)) * 70
        expected_grid += 12 * np.cos(5 * gate) * np.sin(4 * bias)
        np.testing.assert_allclose(heatmap_window.dataGrid, expected_grid.T, atol=1e-12)
        np.testing.assert_allclose(heatmap_window.axis_data["x"], np.linspace(-1, 1, 35))
        np.testing.assert_allclose(heatmap_window.axis_data["y"], np.linspace(-2, 2, 45))
        np.testing.assert_allclose(heatmap_window.image.image, heatmap_window.dataGrid)
        if heatmap_window.ds.guid != heatmap_guid or heatmap_window.bar is None:
            raise RuntimeError("Incorrect heatmap dataset or missing colour scale")
        heatmap_window.show_status("45 × 35 synthetic samples")
        heatmap_window.open_colorbar_scale_dialog()
        dialog = heatmap_window.colorbar_scale_dialog
        dialog.resize(700, 700)
        wait_for(app, dialog.isVisible)
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
            if (main_window._shutdown_deadline_exhausted
                    or main_window._shutdown_background_work_active()):
                raise RuntimeError("Application did not shut down cleanly")
            app.processEvents()
            if audit is not None:
                readonly_module, original_attach, records = audit
                verify_snapshot_cleanup(records, trusted_service=trusted_service)
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


def run_child(*, smoke_test=False):
    """Only the private worker process imports or starts the application."""
    configure_environment()
    from screenshot_support import application_version, sha256

    import qplot

    if (not smoke_test
            and Path(qplot.__file__).resolve().parent != (REPO_ROOT / "src/qplot").resolve()):
        raise RuntimeError("Capture must run the application from this candidate checkout")
    if importlib.metadata.version("qplotter") != application_version():
        raise RuntimeError("Reinstall the candidate application: installed version differs from pyproject.toml")
    from qplot.datahandling.trusted_live import _native_extension_path

    native = _native_extension_path()
    errors = []

    class ErrorAudit(logging.Handler):
        def emit(self, record):
            errors.append(record.getMessage())

    error_audit = ErrorAudit(logging.ERROR)
    logging.getLogger().addHandler(error_audit)
    # configure_logging installs a privacy-aware hook. Keep that behaviour and
    # also make asynchronous exceptions fail the capture instead of only logging.
    from qplot.diagnostics import configure_logging, install_excepthook

    configure_logging()
    install_excepthook()
    logging.getLogger("qplot").addHandler(error_audit)
    original_hook = sys.excepthook

    def exception_hook(*arguments):
        errors.append(arguments[0].__name__)
        original_hook(*arguments)

    sys.excepthook = exception_hook
    os.environ["QPLOT_DEMO_VERIFY_CLEANUP"] = "1"
    with build_demo_database() as database:
        database_path = database[0]

        def protected_state():
            return {suffix: (path.stat().st_size, path.stat().st_mtime_ns, sha256(path))
                    for suffix in ("", "-wal", "-journal")
                    if (path := Path(str(database_path) + suffix)).exists()}

        before = protected_state()
        capture_screenshots(*database)
        if before != protected_state():
            raise RuntimeError("Capture modified the synthetic database or protected sidecars")
    if errors:
        raise RuntimeError(f"Application errors during capture: {errors}")
    from PyQt6 import QtCore

    packages = {dist.metadata["Name"]: dist.version
                for dist in importlib.metadata.distributions()}
    report = {
        "checks": dict.fromkeys(("selected_data", "rendered", "shutdown", "database_unchanged"), True),
        "environment": {
            "platform": platform.platform(), "python": platform.python_version(),
            "qt": QtCore.qVersion(), "packages": dict(sorted(packages.items())),
            "theme": "light", "style": "Fusion", "scale": 1, "font_dpi": 96,
            "locale": "en_GB", "timezone": "UTC", "font": "DejaVu Sans 10 pt",
            "font_sha256": sha256(Path(importlib.util.find_spec("matplotlib").origin).parent
                                  / "mpl-data/fonts/ttf/DejaVuSans.ttf"),
            "font_files": {path.name: sha256(path) for path in sorted(
                (Path(importlib.util.find_spec("matplotlib").origin).parent
                 / "mpl-data/fonts/ttf").glob("DejaVuSans*.ttf"))},
            "native_sha256": sha256(native),
        },
    }
    (ASSET_DIR / "capture-result.json").write_text(json.dumps(report, indent=2) + "\n")


def run_worker(staged, work, timeout, *, smoke_test=False):
    """Bound the entire capture and contain descendants on failure (POSIX)."""
    environment = dict(os.environ, QPLOT_DEMO_ASSET_DIR=str(staged),
                       QPLOT_DEMO_WORKDIR=str(work), PYTHONHASHSEED="0")
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--worker",
         *(["--smoke-test"] if smoke_test else [])],
        cwd=REPO_ROOT, env=environment, start_new_session=os.name != "nt",
    )
    try:
        if process.wait(timeout=timeout) != 0:
            raise RuntimeError("Screenshot application process failed; approved files preserved")
        if os.name != "nt":
            # Python's resource tracker can finish after the GUI worker exits.
            # Wait for observable process-group disappearance, with a deadline.
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.01)
            else:
                raise RuntimeError("Application left child processes after shutdown")
    finally:
        if os.name == "nt":
            if process.poll() is None:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               check=False, timeout=15, capture_output=True)
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=15)


def main(arguments=None):
    from screenshot_support import (
        RECORD,
        application_version,
        fingerprint,
        git,
        install_set,
        validate_images,
        validate_references,
    )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--output", type=Path, default=ASSET_DIR)
    parser.add_argument("--smoke-test", action="store_true",
                        help="Test an installed source archive without Git; never release evidence")
    parser.add_argument("--expected-version", help="Intended release version, matching pyproject.toml")
    args = parser.parse_args(arguments)
    if args.worker:
        if ASSET_DIR == DEFAULT_ASSET_DIR or ASSET_DIR.parent != WORK_DIR.parent:
            raise ValueError("The private worker requires isolated staging paths")
        run_child(smoke_test=args.smoke_test)
        return
    if args.smoke_test and args.output.resolve() == DEFAULT_ASSET_DIR.resolve():
        raise ValueError("Smoke tests must use a separate output directory")
    if args.expected_version is not None and args.expected_version != application_version():
        raise ValueError("Intended application version does not match pyproject.toml")
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    inputs = None if args.smoke_test else fingerprint()
    source_commit = None if args.smoke_test else git(REPO_ROOT, "rev-parse", "HEAD")
    validate_references()
    with tempfile.TemporaryDirectory(prefix="capture-", dir=WORK_DIR) as temporary:
        temporary = Path(temporary)
        staged = temporary / "assets"
        staged.mkdir()
        run_worker(staged, temporary / "work", args.timeout, smoke_test=args.smoke_test)
        images = validate_images(staged, exact_dimensions=True)
        report = json.loads((staged / "capture-result.json").read_text())
        if not all(report["checks"].get(key) is True for key in
                   ("selected_data", "rendered", "shutdown", "database_unchanged")):
            raise RuntimeError("Capture did not complete its application checks")
        if not args.smoke_test and inputs != fingerprint():
            raise RuntimeError("Screenshot inputs changed during generation")
        run_id = os.environ.get("GITHUB_RUN_ID")
        record = {
            "schema": 0 if args.smoke_test else 1, "source_commit": source_commit,
            "generated_at": datetime.now(UTC).isoformat(),
            "source_dirty": (None if args.smoke_test else bool(git(REPO_ROOT, "status", "--porcelain"))),
            "application_version": application_version(), "inputs": inputs,
            "generation_id": (f"{run_id}/{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"
                              if run_id else f"local/{uuid.uuid4()}"),
            "workflow_run": {
                "id": run_id, "attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
                "repository": os.environ.get("GITHUB_REPOSITORY"),
                "workflow": os.environ.get("GITHUB_WORKFLOW"),
                "url": (f"https://github.com/{os.environ.get('GITHUB_REPOSITORY')}/actions/runs/{run_id}"
                        if run_id else None),
            },
            "images": images, **report,
        }
        (staged / RECORD).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        # Leave the private capture context before touching approved outputs.
        payload = {name: (staged / name).read_bytes() for name in (*images, RECORD)}
    install_set(payload, args.output)
    print(f"Generated and validated {len(images)} screenshots: {args.output}")


if __name__ == "__main__":
    main()
