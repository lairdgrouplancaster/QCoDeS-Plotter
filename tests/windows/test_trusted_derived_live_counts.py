"""Real QCoDeS observations must invalidate unselected rows via the Qt bridge."""

from dataclasses import replace

import pytest
from PyQt6 import QtWidgets
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.parameters import ManualParameter

from qplot.datahandling.trusted_derived_rendering import render_trusted_derived_payload
from qplot.datahandling.trusted_live_queries import TrustedSourceRevision
from qplot.datahandling.trusted_work_scheduler import TrustedWorkKind
from qplot.windows import _trusted_derived_qt as bridge_module
from qplot.windows._widgets.treeWidgets import RunList, moreInfo
from tests.datahandling.test_live_acquired_point_counts import _open_reader
from tests.datahandling.test_stage5c_real_qcodes import _protected_artifact_state
from tests.windows.test_trusted_derived_qt import (
    _FakeCoordinator,
    _publication,
    _Window,
)


@pytest.mark.timeout(120)
@pytest.mark.parametrize("complete_first", [False, True], ids=["live", "completed"])
def test_background_metadata_clears_unselected_real_qcodes_run(
    tmp_path, monkeypatch, complete_first
):
    # Control scheduling only: observations, Qt publication fences, RunList,
    # PreviewTab and the selected overview are production implementations.
    monkeypatch.setattr(bridge_module, "TrustedWorkCoordinator", _FakeCoordinator)
    path = tmp_path / "background-counts.db"
    initialise_or_create_database_at(str(path), journal_mode="WAL")
    experiment = load_or_create_experiment("background_counts", sample_name="2x3")
    service = dataset = window = bridge = None
    try:
        x, y, z = (ManualParameter(name) for name in ("x", "y", "z"))
        measurement = Measurement(exp=experiment)
        measurement.register_parameter(x)
        measurement.register_parameter(y)
        measurement.register_parameter(z, setpoints=(x, y))
        measurement.write_period = 3600
        with measurement.run(write_in_background=False) as datasaver:
            dataset = datasaver.dataset
            run_id, guid = datasaver.run_id, str(dataset.guid)
            for ix in range(2):
                for iy in range(3):
                    datasaver.add_result((x, ix), (y, iy), (z, ix * 3 + iy))
            datasaver.flush_data_to_database(block=True)
            service = _open_reader(path)
            observation = service.submit_derived_source(run_id).wait(30)
            fields = dict(observation.run_fields)
            window = _Window(service.database_instance, service, {})
            old_list = window.RunList
            window.layout().removeWidget(old_list)
            old_list.deleteLater()
            window.RunList = RunList(window)
            window.layout().addWidget(window.RunList)
            window.infoBox = moreInfo(window)
            window.RunList.addRuns({run_id: fields})
            bridge = bridge_module.TrustedDerivedQtBridge(window)
            bridge.bind_database(service.database_instance, {run_id: fields}, service)
            coordinator = bridge._coordinator
            item = window.RunList._item_for_guid(guid)
            column = window.RunList.cols.index("Setpoints")

            def publication(patch, source=observation):
                result = _publication(
                    bridge, coordinator, guid, TrustedWorkKind.METADATA
                )
                payload = render_trusted_derived_payload(
                    source, TrustedWorkKind.METADATA
                )
                metadata = dict(payload["metadata"])
                metadata["run_fields"] = tuple(patch.items())
                payload["metadata"] = tuple(metadata.items())
                return replace(result, result=payload)

            def publish(patch, source=observation):
                result = publication(patch, source)
                bridge._publish(result)
                return result

            identity = {"run_id": run_id, "guid": guid}
            regular = publish(fields)
            assert item.text(column) == "6 = 2 × 3"
            assert item in window.RunList.watching
            assert window.RunList.selectedItems() == []
            assert not bridge._selected_guid

            # A partial publication leaves acquired evidence in both consumers.
            publish(identity)
            for metadata in (
                item.run_metadata,
                window.infoBox.preview.run_metadata[guid],
            ):
                assert metadata["read_setpoint_count"] == 6
                assert metadata["setpoint_shape"] == [2, 3]

            # Falsy values must cross the bridge unchanged, including a clear.
            publish(
                {
                    **identity,
                    "read_setpoint_count": 0,
                    "storage_bytes": 0,
                    "storage_bytes_estimated": False,
                    "setpoint_shape": None,
                    "is_completed": False,
                }
            )
            for metadata in (
                item.run_metadata,
                window.infoBox.preview.run_metadata[guid],
            ):
                assert metadata["read_setpoint_count"] == 0
                assert metadata["storage_bytes"] == 0
                assert metadata["storage_bytes_estimated"] is False
                assert metadata["is_completed"] is False
                assert metadata["setpoint_shape"] is None
            publish(fields)

            for index in range(100):
                datasaver.add_result(
                    (x, 10 + index * 1.3), (y, 20 + index * 1.7), (z, index)
                )
            datasaver.flush_data_to_database(block=True)

            def refresh(completed):
                protected = _protected_artifact_state(path)
                observation = service.submit_derived_source(run_id).wait(30)
                fields = dict(observation.run_fields)
                assert fields["result_count"] == 106
                assert bool(fields["is_completed"]) == completed
                invalidated = (
                    "read_setpoint_count",
                    "setpoint_count",
                    "setpoint_shape",
                    "point_shape",
                    "setpoint_shape_source",
                )
                assert all(fields[name] is None for name in invalidated)
                # Advance the source fence; old responses may neither restore
                # obsolete shapes nor clear evidence belonging to a new source.
                stale_clear = publication(fields, observation)
                revision = TrustedSourceRevision(f"counts-{completed}".encode())
                coordinator._runs = tuple(
                    replace(run, source_revision=revision) for run in coordinator.runs
                )
                before = dict(item.run_metadata)
                bridge._publish(stale_clear)
                assert item.run_metadata == before
                for _ in range(3):
                    accepted = publish(fields, observation)
                    assert item.text(column) == "unknown"
                    for metadata in (
                        item.run_metadata,
                        window.infoBox.preview.run_metadata[guid],
                    ):
                        assert metadata["result_count"] == 106
                        assert all(metadata[name] is None for name in invalidated)
                    publish(identity)
                    before = dict(item.run_metadata)
                    bridge._publish(regular)
                    bridge._publish(
                        replace(accepted, generation=accepted.generation - 1)
                    )
                    assert item.run_metadata == before
                    assert item.text(column) == "unknown"
                    assert (item not in window.RunList.watching) == completed
                    assert window.RunList.selectedItems() == []
                    assert window._selected_run_guid is None
                if completed:
                    timestamp = item.run_metadata["completed_timestamp"]
                    publish(
                        {
                            **identity,
                            "result_count": 0,
                            "is_completed": False,
                            "completed_timestamp": None,
                        }
                    )
                    assert item.run_metadata["result_count"] == 106
                    assert item.run_metadata["is_completed"] is True
                    assert item.run_metadata["completed_timestamp"] == timestamp
                assert _protected_artifact_state(path) == protected

            if not complete_first:
                refresh(False)
        refresh(True)
    finally:
        if bridge is not None:
            bridge.shutdown()
        if window is not None:
            window.close()
            window.deleteLater()
            QtWidgets.QApplication.processEvents()
        if service is not None:
            service.close(timeout=30)
        if dataset is not None:
            dataset.conn.close()
        experiment.conn.close()
