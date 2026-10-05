"""Live logical counts must lose invalid evidence, including in Overview."""

from pathlib import Path

import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.parameters import ManualParameter

from qplot.datahandling.file_identity import database_instance
from qplot.datahandling.readSQL import materialize_run_observation
from qplot.datahandling.trusted_live_service import TrustedLiveReadService
from qplot.windows._widgets.treeWidgets import RunList, moreInfo
from tests.datahandling.test_stage5c_real_qcodes import _protected_artifact_state


def _overview(widget):
    return {
        widget.overview.item(row, 0).text(): widget.overview.item(row, 1).text()
        for row in range(widget.overview.rowCount())
    }


def _open_reader(path):
    service = TrustedLiveReadService(
        path,
        expected_database_instance=database_instance(path),
        request_timeout_seconds=30,
    )
    try:
        bootstrap = service.submit_bootstrap().wait(30)
        service.submit_basic_page(0, bootstrap.run_id_watermark).wait(30)
    except BaseException:
        service.close(timeout=30)
        raise
    return service


@pytest.mark.timeout(120)
@pytest.mark.parametrize(
    "irregular", [True, False], ids=["invalidated", "valid-growth"]
)
@pytest.mark.parametrize(
    "complete_before_refresh", [True, False], ids=["complete-first", "live-first"]
)
def test_real_qcodes_live_append_counts_and_selected_overview(
    tmp_path: Path, irregular, complete_before_refresh
):
    path = tmp_path / "live-counts.db"
    initialise_or_create_database_at(str(path), journal_mode="WAL")
    experiment = load_or_create_experiment("live_counts", sample_name="2x3")
    dataset = service = None
    run_list, overview = RunList(), moreInfo()
    try:
        x, y, z = (ManualParameter(name) for name in ("x", "y", "z"))
        measurement = Measurement(exp=experiment)
        measurement.register_parameter(x)
        measurement.register_parameter(y)
        measurement.register_parameter(z, setpoints=(x, y))
        measurement.write_period = 3600
        with measurement.run(write_in_background=False) as datasaver:
            dataset = datasaver.dataset
            run_id = datasaver.run_id
            for ix in range(2):
                for iy in range(3):
                    datasaver.add_result((x, ix), (y, iy), (z, ix * 3 + iy))
            datasaver.flush_data_to_database(block=True)
            service = _open_reader(path)
            protected = _protected_artifact_state(path)
            initial = service.submit_derived_source(run_id).wait(30)
            fields = dict(initial.run_fields)
            assert fields["result_count"] == fields["read_setpoint_count"] == 6
            assert fields["setpoint_count"] == 6
            assert tuple(fields["setpoint_shape"]) == (2, 3)
            run_list.addRuns({run_id: fields})
            run_list.setCurrentItem(run_list._item_for_guid(fields["guid"]))
            overview.set_trusted_derived_metadata(fields, (), (), {})
            assert _overview(overview)["Data points"].startswith("6")
            assert _protected_artifact_state(path) == protected

            if irregular:
                for index in range(100):
                    datasaver.add_result(
                        (x, 10 + index * 1.3), (y, 20 + index * 1.7), (z, index)
                    )
            else:
                for ix in range(2, 4):
                    for iy in range(3):
                        datasaver.add_result((x, ix), (y, iy), (z, ix * 3 + iy))
            datasaver.flush_data_to_database(block=True)

            def refresh_and_compare(completed):
                protected = _protected_artifact_state(path)
                expected = None if irregular else 12
                for _ in range(3):
                    observation = service.submit_derived_source(run_id).wait(30)
                    fields = dict(observation.run_fields)
                    assert bool(fields["is_completed"]) == completed
                    assert fields["result_count"] == (106 if irregular else 12)
                    assert fields["read_setpoint_count"] == expected
                    assert fields["setpoint_count"] == expected
                    assert fields["setpoint_shape"] == (None if irregular else (4, 3))
                    if irregular:
                        assert observation.validated_2d_layouts == ()
                    # Refreshes on watched runs and already completed runs use
                    # distinct RunList paths. Both must publish explicit clears.
                    updates = run_list.checkWatching({fields["guid"]: fields})
                    if not updates:
                        updates = run_list.updateRuns({run_id: fields})
                    cached = updates[run_id]
                    assert cached["read_setpoint_count"] == expected
                    assert cached["setpoint_count"] == expected
                    overview.update_live_run_details(cached)
                    if irregular:
                        assert "Data points" not in _overview(overview)
                    else:
                        assert _overview(overview)["Data points"].startswith("12")
                    # Cheap and selected materialisation must not resurrect
                    # invalid fields from the adapter's retained metadata.
                    cheap = service.submit_cheap_run(run_id).wait(30).as_dict()
                    expensive = service.submit_expensive_run(run_id).wait(30).as_dict()
                    selected = (
                        service.submit_selected_run(run_id).wait(30).run.as_dict()
                    )
                    for metadata in (cheap, expensive, selected):
                        assert metadata["read_setpoint_count"] == expected
                        assert metadata["setpoint_count"] == expected
                    overview.set_trusted_derived_metadata(selected, (), (), {})
                    assert ("Data points" in _overview(overview)) == (not irregular)
                fresh = _open_reader(path)
                try:
                    fresh_fields = dict(
                        fresh.submit_derived_source(run_id).wait(30).run_fields
                    )
                    for name in (
                        "read_setpoint_count",
                        "setpoint_count",
                        "setpoint_shape",
                        "point_shape",
                    ):
                        assert fields[name] == fresh_fields[name]
                finally:
                    fresh.close(timeout=30)
                assert _protected_artifact_state(path) == protected

            if not complete_before_refresh:
                refresh_and_compare(False)
        refresh_and_compare(True)
    finally:
        if service is not None:
            service.close(timeout=30)
        if dataset is not None:
            dataset.conn.close()
        experiment.conn.close()
        run_list.close()
        overview.close()
        run_list.deleteLater()
        overview.deleteLater()


def test_count_materialisation_distinguishes_omission_from_unavailable():
    prior = {"read_setpoint_count": 6}
    assert materialize_run_observation(prior)["read_setpoint_count"] == 6
    assert (
        materialize_run_observation(prior, read_setpoint_count=None)[
            "read_setpoint_count"
        ]
        is None
    )
    assert (
        materialize_run_observation(prior, read_setpoint_count=0)["read_setpoint_count"]
        == 0
    )


def test_live_overview_preserves_omitted_count_and_clears_explicit_unknown():
    widget = moreInfo()
    try:
        widget.set_trusted_derived_metadata(
            {"is_completed": False, "read_setpoint_count": 6}, (), (), {}
        )
        widget.update_live_run_details({"is_completed": False})
        assert _overview(widget)["Data points"] == "6"
        widget.update_live_run_details(
            {"is_completed": False, "read_setpoint_count": None}
        )
        assert "Data points" not in _overview(widget)
    finally:
        widget.close()
        widget.deleteLater()
