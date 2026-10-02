"""Exercise database destination protection through real QCoDeS export actions."""

import sqlite3
import subprocess
import sys
from unittest.mock import Mock

import pytest
from PyQt6 import QtGui, QtPrintSupport
from PyQt6 import QtWidgets as qtw
from pyqtgraph.exporters import CSVExporter, ImageExporter, SVGExporter

from qplot.testdata import instruction_collection_contents
from qplot.windows import _database_actions, _export_paths
from qplot.windows import main as main_window
from tests._window_lifecycle import close_main_window
from tests.windows.test_export_paths import _database_artifact_state
from tests.windows.test_export_paths import (
    windows_stat_timestamps as windows_stat_timestamps,
)
from tests.windows.test_plot_integration import configure_temp_qplot, wait_for
from tests.windows.test_run_csv_export_race import _create_run
from tests.windows.test_svg_export_integration import select_exporter


@pytest.fixture
def source_plot(tmp_path, monkeypatch):
    configure_temp_qplot(monkeypatch, tmp_path)
    database = tmp_path / "source.db"
    run_id, guid = _create_run(database, 3)
    before = _database_artifact_state(database)
    main = main_window.MainWindow()
    try:
        main.startupDatabaseTimer.stop()
        main.monitor.stop()
        main.config.config["user_preference"]["confirm_close"] = False
        main.config.config["user_preference"]["confirm_close_all"] = False
        assert main.load_file(str(database))
        wait_for(lambda: not main._database_load_active)
        main.openPlot(guid=guid, show=True)
        plot = main.windows[-1]
        wait_for(lambda: hasattr(plot, "axis_data") and not plot.worker.running)
        plot.monitor.stop()
        errors = []
        monkeypatch.setattr(plot, "show_error", lambda *args: errors.append(args))
        monkeypatch.setattr(main, "show_error", lambda *args: errors.append(args))
        yield main, plot, run_id, guid, errors
    finally:
        close_main_window(main)
        assert _database_artifact_state(database) == before


ROUTES = (
    ("plot_csv", ".csv"), ("plot_tsv", ".tsv"),
    ("image", ".png"), ("svg", ".svg"),
    ("save_pdf", ".pdf"), ("print_pdf", ".pdf"),
    ("run_csv", ".csv"), ("selected_preview", ".csv"),
    ("run_preview", ".csv"), ("example_csv", ".csv"),
    ("csv_collection", ".csv"),
)


def choose_destination(monkeypatch, target):
    monkeypatch.setattr(qtw.QFileDialog, "getSaveFileName",
                        lambda *_args, **_kwargs: (str(target), ""))
    monkeypatch.setattr(qtw.QFileDialog, "getExistingDirectory",
                        lambda *_args, **_kwargs: str(target.parent))
    question = Mock(return_value=qtw.QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(qtw.QMessageBox, "question", question)
    return question


def export_action(source_plot, route, target, monkeypatch):
    main, plot, run_id, guid, _errors = source_plot
    if route in {"plot_csv", "plot_tsv", "image", "svg"}:
        exporter = {"image": ImageExporter, "svg": SVGExporter}.get(route, CSVExporter)
        dialog = select_exporter(plot, exporter)
        dialog.ui.exportBtn.click()
    elif route == "save_pdf":
        plot.savePlotPdfAction.trigger()
    elif route == "print_pdf":
        def accept_pdf(dialog):
            printer = dialog.printer()
            printer.setOutputFormat(QtPrintSupport.QPrinter.OutputFormat.PdfFormat)
            printer.setOutputFileName(str(target))
            return qtw.QDialog.DialogCode.Accepted

        monkeypatch.setattr(QtPrintSupport.QPrintDialog, "exec", accept_pdf)
        plot.printPlotAction.trigger()
    elif route == "run_csv":
        main.selected_run_id = run_id
        main.measurementBox.setText("*")
        assert main.exportCsvButton.isEnabled()
        main.exportCsvButton.click()
    elif route == "selected_preview":
        main.updateSelected(guid)
        main.export_preview_csv("signal")
    elif route == "run_preview":
        main.export_run_preview_csv(guid, "signal")
    else:
        monkeypatch.setattr(_database_actions, "reveal_file_in_file_manager", lambda *_: True)
        name = ("createTestDatabaseCsvAction" if route == "example_csv"
                else "exportTestDatabaseCsvCollectionAction")
        action = main.findChild(QtGui.QAction, name)
        assert action is not None
        action.trigger()


def destination_for(tmp_path, route, suffix):
    if route == "csv_collection":
        return tmp_path / instruction_collection_contents()[0][0]
    return tmp_path / f"other-measurement{suffix}"


@pytest.mark.parametrize("route,suffix", ROUTES)
@pytest.mark.parametrize("journal_mode", ["DELETE", "WAL"])
@pytest.mark.parametrize("distinct_stat_timestamps", [False, True], ids=["normal-stat", "windows-stat"])
def test_exports_reject_unloaded_qcodes_database(
    source_plot, tmp_path, monkeypatch, route, suffix, journal_mode,
    distinct_stat_timestamps, request,
):
    if distinct_stat_timestamps:
        request.getfixturevalue("windows_stat_timestamps")
    errors = source_plot[-1]
    target = destination_for(tmp_path, route, suffix)
    _create_run(target, 2)
    # This is a synthetic writer, never an inspection connection. Keep it
    # open so the WAL contains committed QCoDeS changes throughout export.
    writer = sqlite3.connect(target)
    writer.execute(f"PRAGMA journal_mode={journal_mode}")
    writer.execute("UPDATE runs SET name = 'unloaded destination'")
    writer.commit()
    before = _database_artifact_state(target)
    question = choose_destination(monkeypatch, target)
    # Even on the unfixed baseline, intercept the final syscall before it can
    # replace this test-owned database. Never forward this call to the OS.
    publication = Mock(side_effect=RuntimeError("intercepted unsafe publication"))
    staging = Mock(side_effect=RuntimeError("unexpected export staging"))
    try:
        with monkeypatch.context() as guard:
            guard.setattr(_export_paths.os, "replace", publication)
            guard.setattr(_export_paths.tempfile, "mkstemp", staging)
            export_action(source_plot, route, target, monkeypatch)
        assert _database_artifact_state(target) == before
        assert errors and "SQLite" in errors[-1][-1]
        publication.assert_not_called()
        staging.assert_not_called()
        question.assert_not_called()
    finally:
        writer.close()


@pytest.mark.parametrize("route,suffix", ROUTES)
@pytest.mark.parametrize("distinct_stat_timestamps", [False, True], ids=["normal-stat", "windows-stat"])
def test_real_exports_create_and_replace_ordinary_files(
    source_plot, tmp_path, monkeypatch, route, suffix, distinct_stat_timestamps, request,
):
    if distinct_stat_timestamps:
        request.getfixturevalue("windows_stat_timestamps")
    errors = source_plot[-1]
    target = destination_for(tmp_path, route, suffix)
    question = choose_destination(monkeypatch, target)
    export_action(source_plot, route, target, monkeypatch)
    assert not errors
    assert target.stat().st_size > 0
    question.assert_not_called()
    if route == "csv_collection":
        # Collection export deliberately has no overwrite consent workflow.
        assert all((tmp_path / name).read_bytes() == data
                   for name, data in instruction_collection_contents())
        return
    expected = target.read_bytes()
    if suffix == ".csv":
        assert b"," in expected
    elif suffix == ".tsv":
        assert b"\t" in expected
    elif suffix == ".png":
        assert expected.startswith(b"\x89PNG")
    elif suffix == ".svg":
        assert b"<svg" in expected
    else:
        assert expected.startswith(b"%PDF-")
    # Print's existing-PDF validator still gets a valid signature.
    target.write_bytes(b"%PDF-ordinary export sentinel")
    export_action(source_plot, route, target, monkeypatch)
    assert not errors
    assert target.read_bytes() != b"%PDF-ordinary export sentinel"
    assert target.stat().st_size > 0
    question.assert_called_once()


def test_plot_export_does_not_recover_unloaded_hot_journal(
    source_plot, tmp_path, monkeypatch,
):
    target = tmp_path / "other-measurement.csv"
    _create_run(target, 2)
    # A crashed synthetic writer leaves a real hot rollback journal. No
    # inspection is allowed to recover it, even though this database is ours.
    subprocess.run([
        sys.executable, "-c",
        "import os, sqlite3, sys; "
        "c = sqlite3.connect(sys.argv[1]); "
        "c.execute('PRAGMA cache_size=1'); "
        "c.execute(\"UPDATE runs SET name = ?\", ('x' * 100000,)); "
        "os._exit(0)",
        str(target),
    ], check=True, timeout=15)
    before = _database_artifact_state(target)
    assert before["-journal"]["bytes"].startswith(b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7")
    question = choose_destination(monkeypatch, target)
    publication = Mock(side_effect=AssertionError("unsafe publication"))
    with monkeypatch.context() as guard:
        guard.setattr(_export_paths.os, "replace", publication)
        export_action(source_plot, "plot_csv", target, monkeypatch)
    assert source_plot[-1] and "SQLite" in source_plot[-1][-1][-1]
    question.assert_not_called()
    publication.assert_not_called()
    assert _database_artifact_state(target) == before
