"""A failed snapshot thumbnail must stay idle until the user retries it."""

import pytest
from PyQt6 import QtWidgets as qtw

from qplot.windows._database_detail_state import detail_channel
from qplot.windows.main import MainWindow
from tests._window_lifecycle import close_main_window
from tests.support.datahandling.trusted_live import (
    _artifact_state,
    _stable_artifact_state,
)
from tests.support.windows.plot_integration import (
    build_synthetic_database,
    configure_temp_qplot,
    force_snapshot_fallback,
    wait_for,
)


@pytest.mark.parametrize(
    ("action", "failures"),
    [("reselect", 1), ("refresh", 1), ("refresh", 2)],
    ids=["reselect", "manual-refresh", "persistent-failure"],
)
def test_failed_selected_thumbnail_retries_once_per_user_action(tmp_path, monkeypatch, action, failures):
    path = tmp_path / "previews.db"
    line_id, other_id = build_synthetic_database(path)
    before = _stable_artifact_state(path)
    configure_temp_qplot(monkeypatch, tmp_path)
    force_snapshot_fallback(monkeypatch)
    window = MainWindow()
    tab = window.infoBox.preview
    submitted = []

    def idle():
        return not (tab.active or tab.queue or tab._workers or tab._start_scheduled
                    or window._database_refresh_active or window._database_load_active
                    or detail_channel(window).active or detail_channel(window, True).active)

    def messages():
        return [label for label in tab.content.findChildren(qtw.QLabel)
                if label.isVisible() and label.text()]

    def assert_failed(attempts):
        wait_for(idle)
        assert len(submitted) == attempts
        assert not tab.preview_selection_labels()
        (label,) = messages()
        assert "failed" in label.text().lower()
        assert "Reselect" in label.text() and "Refresh" in label.text()
        assert "journal busy" in label.toolTip()

    def retry():
        if action == "reselect":
            window.RunList.setCurrentItem(window.RunList._item_for_guid(other))
            assert tab.current_guid == other
            window.RunList.setCurrentItem(window.RunList._item_for_guid(guid))
        else:
            window.refreshAction.trigger()

    try:
        window.startupDatabaseTimer.stop()
        window.spinBox.setValue(0)
        window.show()
        window.infoBox.setCurrentWidget(tab)
        assert window.load_file(str(path))
        wait_for(lambda: len(tab.cache) == 2 and idle())
        assert window._database_access_mode == "snapshot_fallback"
        runs = window.RunList.all_run_metadata()
        guid, other = runs[line_id]["guid"], runs[other_id]["guid"]
        window.RunList.setCurrentItem(window.RunList._item_for_guid(guid))
        assert tab.current_guid == guid and tab.preview_size == 200
        pool = tab.thread_pool
        # Keep the real pool for the rest of the window, but choose when each
        # selected worker delivers its finished signal.
        host = tab._preview_controller.host

        def submit(job):
            if job.guid == guid:
                submitted.append(job.worker)
            else:
                pool.start(job.worker)

        monkeypatch.setattr(host, "submit_worker", submit)
        window.config.update("user_preference.complex_1d", "imag")
        window.apply_current_settings()
        assert len(submitted) == 1 and submitted[0].preview_size == 64
        assert (tab.generation, guid) in tab.active
        submitted[0]._emit_finished([], PermissionError("journal busy"))
        assert_failed(1)

        # Repainting the same selection, updating its metadata, and a timer
        # refresh must each leave the failure visible and the queue empty.
        window.updateSelected(guid)
        tab.add_runs({line_id: tab.run_metadata[guid]})
        tab.prioritize_runs(selected_run_ids=[line_id])
        window.refreshMain(automatic=True)
        assert_failed(1)

        retry()
        assert tab.current_guid == guid and len(submitted) == 2
        assert submitted[1].preview_size == 64 and (tab.generation, guid) in tab.active
        wait_for(lambda: any(label.text() in {"Retrying preview...", "Generating preview..."}
                             for label in messages()))
        if failures == 2:
            # Hold access through the first explicit retry, then restore it.
            submitted[1]._emit_finished([], PermissionError("journal busy"))
            assert_failed(2)
            retry()
            assert len(submitted) == 3 and submitted[-1].preview_size == 64
        submitted[-1].run()
        assert len(submitted) == failures + 2
        assert submitted[-1].preview_size == 200 and (tab.generation, guid) in tab.active
        assert tab.thumbnail_cache[guid][0]["image"].width() == 64 and guid not in tab.cache
        wait_for(lambda: any(label.guid == guid and label.isVisible() and not label.pixmap().isNull()
                             for label in window.RunList.preview_selection_labels()))
        submitted[-1].run()
        wait_for(idle)
        assert tab.current_guid == guid and window.RunList.run_preview_is_ready(guid)
        (image,) = tab.preview_selection_labels()
        assert image.isVisible() and image.guid == guid and image.pixmap().width() == 200
        assert not messages() and guid not in tab.thumbnail_errors
    finally:
        tab.shutdown()
        for worker in tab._workers.values():
            if worker in submitted:
                worker._emit_finished([], None)
        close_main_window(window)
        assert _artifact_state(path) == before
