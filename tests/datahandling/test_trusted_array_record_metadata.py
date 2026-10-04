"""Trusted metadata must distinguish array acquisition records from samples."""

import multiprocessing
import traceback

import numpy as np
import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.datahandling.file_identity import database_instance
from qplot.datahandling.trusted_live import TrustedLiveReader
from qplot.datahandling.trusted_live_queries import (
    TrustedMetadataQueryAdapter,
    TrustedSourceRevisionNamespace,
)
from tests.datahandling.test_stage5c_real_qcodes import (
    _load_basic_runs,
    _protected_artifact_state,
    _ReadOnlySqliteExecutor,
)
from tests.datahandling.test_trusted_live import _assert_safe_audit


def _measurement(path, *, array_x, planned, mixed, dimensions, journal_mode="DELETE"):
    initialise_or_create_database_at(str(path), journal_mode=journal_mode)
    experiment = load_or_create_experiment("array_metadata", sample_name="synthetic")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("x", paramtype="array" if array_x else "numeric")
    axes = ("x",)
    if dimensions == 2:
        measurement.register_custom_parameter("y", paramtype="numeric")
        axes += ("y",)
    measurement.register_custom_parameter("z", paramtype="array", setpoints=axes)
    if mixed:
        measurement.register_custom_parameter("scalar", paramtype="numeric", setpoints=axes)
    if planned:
        measurement.set_shapes({"z": (12,) if dimensions == 1 else (3, 4)})
    return experiment, measurement


def _append(saver, index, *, array_x=False, mixed=False, dimensions=1):
    result = [
        ("x", np.arange(4.) + index * 4 if array_x else index),
        ("z", np.arange(4.) + index),
    ]
    if dimensions == 2:
        result.append(("y", index % 2))
    if mixed:
        result.append(("scalar", index))
    saver.add_result(*result)
    saver.flush_data_to_database(block=True)


def _assert_array_counts(fields, *, planned, dimensions=1):
    assert fields["read_setpoint_count"] is None
    assert fields["setpoint_count"] == (12 if planned else None)
    assert fields["setpoint_count_source"] == ("planned" if planned else None)
    expected_shape = ([12] if dimensions == 1 else [3, 4]) if planned else None
    assert fields["setpoint_shape"] == fields["point_shape"] == expected_shape


@pytest.mark.parametrize("planned", [False, True])
@pytest.mark.parametrize("array_x,mixed,dimensions", [
    (False, False, 1), (False, True, 1), (True, False, 1),
    (False, False, 2), (False, True, 2), (True, False, 2),
])
def test_array_record_metadata(tmp_path, planned, array_x, mixed, dimensions):
    path = tmp_path / "arrays.db"
    experiment, measurement = _measurement(
        path, array_x=array_x, planned=planned, mixed=mixed, dimensions=dimensions,
    )
    try:
        with measurement.run(write_in_background=False) as saver:
            for index in range(3):
                _append(saver, index, array_x=array_x, mixed=mixed, dimensions=dimensions)
            run_id = saver.run_id
    finally:
        experiment.conn.close()
    before = _protected_artifact_state(path)
    executor = _ReadOnlySqliteExecutor(path)
    try:
        adapter = TrustedMetadataQueryAdapter(executor, path)
        _load_basic_runs(adapter)
        for _refresh in range(2):
            fields = adapter.expensive_run(run_id).as_dict()
            _assert_array_counts(fields, planned=planned, dimensions=dimensions)
            assert fields["result_count"] == (6 if mixed else 3)
            detail = adapter.selected_run_detail(run_id)
            summaries = {item.name: item for item in detail.setpoint_summaries}
            parameter_types = {item.name: item.paramtype for item in detail.parameters}
            assert parameter_types["z"] == "array"
            assert parameter_types["x"] == ("array" if array_x else "numeric")
            if dimensions == 2 and planned:
                assert tuple(summaries) == ("x", "y")
            if array_x:
                if planned:
                    assert summaries["x"].first is summaries["x"].last is None
                    assert summaries["x"].steps == (12 if dimensions == 1 else 3)
                else:
                    assert "x" not in summaries
            else:
                assert (summaries["x"].first, summaries["x"].last) == (0, 2)
    finally:
        executor.close()
    assert _protected_artifact_state(path) == before


@pytest.mark.parametrize("parameter_type,value", [
    ("numeric", 1.5), ("complex", 1 + 2j), ("text", "measured"),
])
def test_current_qcodes_parameter_types(tmp_path, parameter_type, value):
    path = tmp_path / "types.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("parameter_types", sample_name="synthetic")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("x", paramtype="numeric")
    measurement.register_custom_parameter("z", paramtype=parameter_type, setpoints=("x",))
    try:
        with measurement.run(write_in_background=False) as saver:
            saver.add_result(("x", 0), ("z", value))
            run_id = saver.run_id
    finally:
        experiment.conn.close()
    before = _protected_artifact_state(path)
    executor = _ReadOnlySqliteExecutor(path)
    try:
        adapter = TrustedMetadataQueryAdapter(executor, path)
        _load_basic_runs(adapter)
        fields = adapter.expensive_run(run_id).as_dict()
        assert fields["result_count"] == fields["setpoint_count"] == 1
        types = {item.name: item.paramtype for item in adapter.selected_run_detail(run_id).parameters}
        assert types == {"x": "numeric", "z": parameter_type}
    finally:
        executor.close()
    assert _protected_artifact_state(path) == before


def test_mixed_array_run_preserves_scalar_peer_grid(tmp_path):
    path = tmp_path / "mixed-grid.db"
    experiment, measurement = _measurement(
        path, array_x=False, planned=False, mixed=True, dimensions=2,
    )
    try:
        with measurement.run(write_in_background=False) as saver:
            for x in range(2):
                for y in range(3):
                    saver.add_result(
                        ("x", x), ("y", y), ("scalar", x * 3 + y),
                        ("z", np.arange(4.) + x * 3 + y),
                    )
            run_id = saver.run_id
    finally:
        experiment.conn.close()
    before = _protected_artifact_state(path)
    executor = _ReadOnlySqliteExecutor(path)
    try:
        adapter = TrustedMetadataQueryAdapter(executor, path)
        _load_basic_runs(adapter)
        observation = adapter.derived_source_observation(
            run_id, database_instance=database_instance(path),
            namespace=TrustedSourceRevisionNamespace.create(),
        )
        proofs = observation.validated_2d_layouts
        assert len(proofs) == 1
        assert proofs[0].shape == (2, 3)
        assert proofs[0].dependent == "scalar"
        _assert_array_counts(adapter.expensive_run(run_id).as_dict(), planned=False)
    finally:
        executor.close()
    assert _protected_artifact_state(path) == before


def _live_array_writer(path, control):
    experiment = None
    try:
        experiment, measurement = _measurement(
            path, array_x=False, planned=False, mixed=False, dimensions=1,
            journal_mode="WAL",
        )
        with measurement.run(write_in_background=False) as saver:
            _append(saver, 0)
            control.send(("ready", saver.run_id))
            while (command := control.recv()) != "stop":
                if command != "append":
                    raise ValueError(command)
                _append(saver, 1)
                control.send(("appended", None))
    except BaseException:
        control.send(("error", traceback.format_exc()))
        raise
    finally:
        if experiment is not None:
            experiment.conn.close()
        control.close()


def test_live_array_records_keep_unknown_sample_count(tmp_path):
    path = tmp_path / "live-arrays.db"
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_live_array_writer, args=(path, child))
    process.start()
    child.close()
    try:
        assert parent.poll(30), "QCoDeS writer startup timed out"
        kind, run_id = parent.recv()
        assert kind == "ready", run_id
        before = _protected_artifact_state(path)
        with TrustedLiveReader.open(path) as reader:
            reader.incarnation = 1
            adapter = TrustedMetadataQueryAdapter(reader, path)
            _load_basic_runs(adapter)
            fields = adapter.expensive_run(run_id).as_dict()
            _assert_array_counts(fields, planned=False)
            assert fields["result_count"] == 1
            assert _protected_artifact_state(path) == before
            parent.send("append")
            assert parent.poll(30), "QCoDeS writer append timed out"
            assert parent.recv() == ("appended", None)
            after_append = _protected_artifact_state(path)
            fields = adapter.expensive_run(run_id).as_dict()
            _assert_array_counts(fields, planned=False)
            assert fields["result_count"] == 2
            _assert_safe_audit(reader.audit().counters)
        _assert_safe_audit(reader.audit().counters)
        assert _protected_artifact_state(path) == after_append
    finally:
        if process.is_alive():
            parent.send("stop")
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(10)
        parent.close()
    assert process.exitcode == 0
