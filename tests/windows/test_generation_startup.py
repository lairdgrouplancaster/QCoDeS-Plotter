"""Failed generation startup must restore the accepted, read-only GUI view."""

import pytest
from PyQt6 import QtWidgets as qtw

from qplot.datahandling.file_identity import database_instance
from qplot.datahandling.readonly import replacement_wal_is_quarantined
from qplot.testdata import example_csv_contents
from qplot.windows import _test_database as generation
from qplot.windows._database_detail_state import detail_channel
from qplot.windows.main import MainWindow
from tests._window_lifecycle import close_main_window
from tests.support.windows.generation_review import choose_custom_generation
from tests.support.windows.plot_integration import (
    assert_database_artifacts_unchanged_except_trusted_shm,
    build_line_database,
    configure_temp_qplot,
    trusted_database_artifact_state,
    wait_for,
)


@pytest.mark.parametrize("failure_stage", ["release", "construct", "submit"])
def test_generation_start_failure_restores_loaded_view(tmp_path, monkeypatch, failure_stage):
    configure_temp_qplot(monkeypatch, tmp_path)
    database_path = tmp_path / "accepted.db"
    run_id, guid, _table = build_line_database(database_path, 3, journal_mode="DELETE")
    csv_path = tmp_path / "instructions.csv"
    csv_path.write_bytes(example_csv_contents())
    errors = []
    window = MainWindow()

    def idle():
        return (
            not window._database_load_active
            and not detail_channel(window).active
            and not detail_channel(window, True).active
        )

    try:
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.close_database(status=False)
        window.show_error = lambda *args: errors.append(args)
        assert window.load_file(str(database_path))
        wait_for(idle)
        window.spinBox.setValue(2.4)
        window.measurementBox.setText("signal")
        accepted_instance = database_instance(database_path)
        before = trusted_database_artifact_state(database_path)
        assert window._selected_run_guid == guid

        def fail(*_args, **_kwargs):
            raise RuntimeError(f"injected {failure_stage} failure")

        with monkeypatch.context() as scoped:
            scoped.setattr(qtw.QFileDialog, "getOpenFileName", lambda *a, **kw: (str(csv_path), ""))
            scoped.setattr(qtw.QFileDialog, "getSaveFileName", lambda *a, **kw: (str(database_path), ""))
            choose_custom_generation(scoped)
            scoped.setattr("qplot.windows._generation_dialog.confirm_action", lambda *a, **kw: True)
            if failure_stage == "release":
                release = window._prepare_test_database_replacement

                def release_then_fail(path):
                    release(path)
                    fail()

                scoped.setattr(window, "_prepare_test_database_replacement", release_then_fail)
            elif failure_stage == "construct":
                scoped.setattr(generation, "TestDatabaseGenerationWorker", fail)
            else:
                scoped.setattr(window.testDatabaseGenerationThreadPool, "start", fail)
            assert not window.generate_test_database_from_csv()

        wait_for(lambda: idle() and window._test_database_replacement_state is None)
        assert errors == [(
            "Test Database Generation Failed", "Could not generate the test database.",
            f"injected {failure_stage} failure",
        )]
        assert window._selected_run_guid == guid
        assert window.selected_run_id == run_id
        assert window.measurementBox.text() == "signal"
        assert window._loaded_database_instance == accepted_instance
        assert not replacement_wal_is_quarantined(database_path)
        assert not window._test_database_generation_active
        assert window._test_database_generation_worker is None
        assert not window._database_generation_transaction_blocks_path(database_path)
        assert window.generateTestDatabaseAction.isEnabled()
        assert window.monitor.isActive()
        assert window.monitor.interval() == 2400
        assert_database_artifacts_unchanged_except_trusted_shm(database_path, before)
    finally:
        close_main_window(window)
