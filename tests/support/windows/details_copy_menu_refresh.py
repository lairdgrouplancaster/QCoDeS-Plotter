"""Shared fixtures and assertions for details copy menu refresh."""

import hashlib
import sys
from contextlib import closing
from pathlib import Path

import pytest
from PyQt6 import QtCore, sip
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.dataset.sqlite.database import connect
from qcodes.parameters import ManualParameter
from qcodes.station import Station

from qplot.diagnostics import configure_logging
from qplot.windows._database_detail_state import detail_channel
from qplot.windows.main import MainWindow
from tests._window_lifecycle import close_main_window
from tests.support.windows.plot_integration import configure_temp_qplot, wait_for

__all__ = [
    'details_window',
]


def _protected_artifacts(database):
    artifacts = {}
    for suffix in ("", "-wal", "-journal"):
        path = Path(f"{database}{suffix}")
        if not path.exists():
            artifacts[suffix] = None
            continue
        stat = path.stat()
        artifacts[suffix] = (
            stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    return artifacts


@pytest.fixture
def details_window(tmp_path, monkeypatch, qapplication):
    configure_temp_qplot(monkeypatch, tmp_path)
    configure_logging(tmp_path / "qplot.log", force=True)
    database = tmp_path / "menu-refresh.db"
    initialise_or_create_database_at(str(database), journal_mode="WAL")
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *error: errors.append(error))
    with closing(connect(str(database))) as writer:
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        experiment = load_or_create_experiment("live copy", "unicode", conn=writer)
        note = ManualParameter("operator", initial_value="Snapshot α & <sample> 🧪")
        station = Station(note)
        measurement = Measurement(exp=experiment, station=station, name="Run α & <sample> 🧪")
        measurement.register_custom_parameter("drive")
        measurement.register_custom_parameter("signal", setpoints=("drive",))
        with measurement.run(write_in_background=False) as saver:
            for drive in (-3., 0., 4.):
                saver.add_result(("drive", drive), ("signal", 7 * drive + 1))
            saver.dataset.add_metadata("operator_note", "Metadata α & <sample> 🧪")
        state = {"protected": _protected_artifacts(database), "objects": []}
        window = MainWindow()
        service = None
        monkeypatch.setattr(window, "show_error", lambda *error: errors.append(error))
        try:
            window.startupDatabaseTimer.stop()
            window.monitor.stop()
            window.config.config["user_preference"]["confirm_close"] = False
            window.config.config["user_preference"]["confirm_close_all"] = False
            window.config.config["runtime_settings"]["del_grace_period"] = 0
            window.spinBox.setValue(0)
            window.show()
            assert window.load_file(str(database))
            wait_for(lambda: not window._database_load_active and not detail_channel(window).active)
            service = window._trusted_read_service
            assert service is not None
            item = window.RunList._item_for_guid(saver.dataset.guid)
            # ExtendedSelection's default command can toggle an already
            # selected row off when native modifier-key state remains held.
            window.RunList.clearSelection()
            window.RunList.setCurrentItem(
                item, 0, QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect,
            )
            assert window.RunList.selectedItems() == [item]
            bridge = window._trusted_derived_bridge
            try:
                wait_for(lambda: bridge.coordinator is not None
                         and not bridge.coordinator.active
                         and not bridge.coordinator.snapshot().pending_count
                         and not bridge.background_active()
                         and bridge._selected_detail_publication is not None
                         and bridge._detail_display_guid == saver.dataset.guid)
            except AssertionError as error:
                coordinator = bridge.coordinator
                raise AssertionError(
                    f"Selected details unavailable: displayed={bridge._detail_display_guid!r}, "
                    f"publication={bridge._selected_detail_publication is not None}, "
                    f"background={bridge.background_active()}, "
                    f"coordinator={coordinator.snapshot() if coordinator is not None else None}"
                ) from error
            yield window, saver.dataset, database, state
        finally:
            close_main_window(window)
            assert service is None or service.closed
            assert _protected_artifacts(database) == state["protected"]
            assert all(sip.isdeleted(obj) for obj in state["objects"])
            assert errors == []
