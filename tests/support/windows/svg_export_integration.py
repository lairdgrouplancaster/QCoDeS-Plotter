"""Shared fixtures and assertions for svg export integration."""

import pyqtgraph as pg
import pytest
from PyQt6 import QtGui
from PyQt6 import QtWidgets as qtw
from qcodes.dataset.sqlite.database import connect

from qplot.testdata import enable_generation_provenance_for_writer
from qplot.windows import main as main_window
from tests._window_lifecycle import close_main_window
from tests.support.windows.plot_integration import (
    build_synthetic_database,
    configure_temp_qplot,
    database_artifact_state,
    prepare_generated_database_for_live_writes,
    wait_for,
)

__all__ = [
    'real_plot',
    'select_exporter',
]


@pytest.fixture(params=[1, 2], ids=["line", "heatmap"])
def real_plot(tmp_path, monkeypatch, request):
    configure_temp_qplot(monkeypatch, tmp_path)
    database = tmp_path / "measurement.db"
    prepare_generated_database_for_live_writes(database)
    run_ids = build_synthetic_database(database)
    # Keep a synthetic writer open so the real WAL/SHM remain present during
    # viewing and exports. Capture protected files after fixture generation.
    writer = connect(database)
    enable_generation_provenance_for_writer(writer)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("UPDATE runs SET name = name || '_export'")
    writer.commit()
    before = database_artifact_state(database, hash_shm=False)
    before.pop("-shm")  # SQLite's pinned VFS may update only this sidecar
    guid = writer.execute(
        "SELECT guid FROM runs WHERE run_id = ?", (run_ids[request.param - 1],),
    ).fetchone()[0]
    main = main_window.MainWindow()
    try:
        main.startupDatabaseTimer.stop()
        main.monitor.stop()
        main.config.config["user_preference"]["confirm_close"] = False
        main.config.config["user_preference"]["confirm_close_all"] = False
        main.close_database(status=False)
        assert main.load_file(str(database))
        wait_for(lambda: not main._database_load_active)
        prior_plot_count = len(main.windows)
        main.openPlot(guid=guid, show=True)
        wait_for(lambda prior_plot_count=prior_plot_count: len(main.windows) > prior_plot_count)
        plot = main.windows[-1]
        wait_for(lambda: hasattr(plot, "axis_data") and not plot.worker.running)
        plot.monitor.stop()
        errors = []
        monkeypatch.setattr(plot, "show_error", lambda *args: errors.append(args))
        if request.param == 1:
            plot.line.setPen(pg.mkPen("#e01020", width=3))
            plot.line.setSymbol("o")  # Qt 6.11 emits standalone Z in closed paths
            plot.line.setSymbolBrush("#e01020")
            plot._ensure_trace_axis_viewboxes(top=True, right=True)
            plot.plot.showAxis("top")
            plot.plot.showAxis("right")
            plot.plot.setLabel("top", "Secondary gate", units="V")
            plot.plot.setLabel("right", "Secondary current", units="nA")
            secondary = pg.PlotDataItem([-.9, 0, .9], [100, 300, 100],
                                        pen=pg.mkPen("#007090", width=3))
            plot.top_right_vb.addItem(secondary)
            plot.top_right_vb.setRange(xRange=(-1.5, 1.5), yRange=(0, 400), padding=0)
        # A closed curved overlay exercises Qt paths under an anisotropic
        # data-to-view transform. Its out-of-range part must remain clipped.
        path = QtGui.QPainterPath()
        path.moveTo(-100, 0)
        path.cubicTo(-.5, .3, .5, .3, 100, 0)
        path.lineTo(100, -.2)
        path.closeSubpath()
        overlay = qtw.QGraphicsPathItem(path)
        overlay.setPen(pg.mkPen("#8030a0", width=2))
        overlay.setBrush(pg.mkBrush(128, 48, 160, 60))
        plot.vb.addItem(overlay, ignoreBounds=True)
        qtw.QApplication.processEvents()
        yield plot, database, errors, request.param
    finally:
        close_main_window(main)
        try:
            after = database_artifact_state(database, hash_shm=False)
            after.pop("-shm")
            assert after == before
        finally:
            writer.close()


def select_exporter(plot, exporter_type):
    plot.exportPlotAction.trigger()  # File > Export Plot
    qtw.QApplication.processEvents()
    dialog = plot.widget.scene().exportDialog
    assert dialog.isVisible()
    assert dialog.ui.itemTree.currentItem().gitem is plot.widget.scene()
    for row in range(dialog.ui.formatList.count()):
        if dialog.ui.formatList.item(row).expClass is exporter_type:
            dialog.ui.formatList.setCurrentRow(row)
            break
    assert type(dialog.currentExporter) is exporter_type
    return dialog
