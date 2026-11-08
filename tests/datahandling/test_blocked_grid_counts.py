"""Independent per-measurement grids must supply proven run-list counts."""

from contextlib import closing
from dataclasses import replace

import pytest

from qplot.datahandling.file_identity import database_instance
from qplot.datahandling.trusted_derived_rendering import render_trusted_derived_payload
from qplot.datahandling.trusted_live_queries import TrustedMetadataQueryAdapter
from qplot.datahandling.trusted_query_types import (
    TRUSTED_DERIVED_MAX_ARRAY_BYTES,
    TrustedSourceRevisionNamespace,
)
from qplot.datahandling.trusted_work_scheduler import TrustedWorkKind
from qplot.testdata import (
    copy_instruction_collection,
    generate_database,
    read_specifications,
)
from tests.datahandling.test_stage5c_real_qcodes import (
    _load_basic_runs,
    _protected_artifact_state,
    _ReadOnlySqliteExecutor,
)


@pytest.mark.parametrize("paramtype", ["numeric", "complex"])
def test_bundled_blocked_grid_counts_are_available_on_first_observation(tmp_path, paramtype):
    source = copy_instruction_collection(tmp_path / "instructions")[0]
    run = next(run for run in read_specifications(source) if run.name == "complex_gate_map")
    run = replace(run, measurements=tuple(replace(m, paramtype=paramtype) for m in run.measurements))
    database = generate_database([run], tmp_path / "blocked.db")
    before = _protected_artifact_state(database)
    with closing(_ReadOnlySqliteExecutor(database)) as executor:
        adapter = TrustedMetadataQueryAdapter(executor, database)
        _load_basic_runs(adapter)
        for _ in range(2):
            fields = adapter.expensive_run(1).as_dict()
            assert fields["result_count"] == 3 * 11 * 21
            assert fields["setpoint_count"] == 11 * 21
            assert fields["read_setpoint_count"] == 11 * 21
            assert fields["setpoint_shape"] == [11, 21]
            assert {s.name: s.steps for s in adapter._setpoint_summaries[1]} == {
                "V_G": 11, "frequency": 21,
            }
        observation = adapter.derived_source_observation(
            1, database_instance=database_instance(database), namespace=TrustedSourceRevisionNamespace.create(),
        )
        assert sum(len(value) for row in observation.sample_rows for value in row
                   if isinstance(value, bytes)) <= TRUSTED_DERIVED_MAX_ARRAY_BYTES
        payload = render_trusted_derived_payload(observation, TrustedWorkKind.THUMBNAIL)
        assert payload["status"] == "ok", payload
        assert len(payload["images"]) == 3
        assert all(dict(image)["sampled_points"] == 231 for image in payload["images"])
    assert _protected_artifact_state(database) == before


@pytest.mark.parametrize("run_name,mode", [("complex_response", "imag_vs_real"), ("complex_gate_map", "phase")])
def test_snapshot_fallback_complex_previews(tmp_path, run_name, mode):
    from qplot.windows._widgets.preview import generate_run_previews

    source = copy_instruction_collection(tmp_path / "instructions")[0]
    run = next(run for run in read_specifications(source) if run.name == run_name)
    database = generate_database([run], tmp_path / "complex.db")
    before = _protected_artifact_state(database)
    with closing(_ReadOnlySqliteExecutor(database)) as executor:
        adapter = TrustedMetadataQueryAdapter(executor, database)
        _load_basic_runs(adapter)
        metadata = adapter.expensive_run(1).as_dict()
    previews = generate_run_previews(database, metadata, size=64, complex_modes=(mode, mode))
    assert {preview["parameter"] for preview in previews} == {"S11", "S21", "S12"}
    assert all(preview["complex"] and preview["complex_mode"] == mode for preview in previews)
    assert all(not preview["image"].isNull() for preview in previews)
    assert _protected_artifact_state(database) == before


def test_large_complex_grid_capture_retains_all_parameters_within_blob_budget(tmp_path):
    source = copy_instruction_collection(tmp_path / "instructions")[0]
    run = next(run for run in read_specifications(source) if run.name == "complex_gate_map")
    run = replace(run, measurements=tuple(
        replace(measurement, axes=tuple(replace(axis, points=31 if axis.name == "V_G" else 301)
                                       for axis in measurement.axes))
        for measurement in run.measurements
    ))
    database = generate_database([run], tmp_path / "complex-grid.db")
    before = _protected_artifact_state(database)
    with closing(_ReadOnlySqliteExecutor(database)) as executor:
        adapter = TrustedMetadataQueryAdapter(executor, database)
        _load_basic_runs(adapter)
        observation = adapter.derived_source_observation(
            1, database_instance=database_instance(database), namespace=TrustedSourceRevisionNamespace.create(),
        )
        assert observation.result_watermark == 3 * 31 * 301
        assert len(observation.sample_rows) < observation.result_watermark
        retained_bytes = sum(len(value) for row in observation.sample_rows for value in row
                             if isinstance(value, bytes))
        assert 0 < retained_bytes <= TRUSTED_DERIVED_MAX_ARRAY_BYTES
        payload = render_trusted_derived_payload(observation, TrustedWorkKind.THUMBNAIL)
        assert payload["status"] == "ok", payload
        images = {dict(image)["dependent"]: dict(image) for image in payload["images"]}
        assert set(images) == {"S11", "S21", "S12"}
        assert all(image["complex_mode"] == "magnitude" and image["sampled_points"] > 0
                   for image in images.values())
    assert _protected_artifact_state(database) == before
