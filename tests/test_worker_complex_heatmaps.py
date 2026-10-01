"""Real QCoDeS heatmaps must not silently discard complex components."""

import hashlib

import numpy as np
import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

import qplot.tools.worker as worker_module
from qplot.datahandling.readonly import load_by_id_read_only
from qplot.tools.worker import loader


def _run_worker(worker):
    finished, errors = [], []
    worker.emitter.finished.connect(finished.append)
    worker.emitter.errorOccurred.connect(errors.append)
    worker.run()
    return finished, errors


def _bare_worker():
    worker = loader.__new__(loader)
    worker.axes_dict = {"x": "fast", "y": "slow"}
    worker.param = type(
        "Param",
        (),
        {"name": "signal", "type": "numeric", "depends_on_": ("slow", "fast")},
    )()
    worker.param_dict = {
        name: type("Param", (), {"name": name, "type": "numeric"})()
        for name in ("slow", "fast")
    }
    return worker


@pytest.mark.parametrize("complex_axis", ["fast", "slow"])
def test_arrays_from_values_rejects_the_named_complex_coordinate(complex_axis):
    worker = _bare_worker()
    coordinates = {
        "fast": np.array([1 + 10j, 2 + 20j, 3 + 30j]),
        "slow": np.array([4 + 40j, 5 + 50j, 6 + 60j]),
    }
    coordinates["slow" if complex_axis == "fast" else "fast"] = np.arange(3)

    with pytest.raises(ValueError, match=complex_axis):
        worker._arrays_from_values(
            coordinates["fast"],
            coordinates["slow"],
            np.arange(3),
        )


@pytest.mark.parametrize("complex_axis", ["fast", "slow"])
def test_shaped_loader_rejects_the_named_complex_coordinate(complex_axis):
    worker = _bare_worker()
    slow = np.broadcast_to(np.arange(2)[:, None], (2, 3)).copy()
    fast = np.broadcast_to(np.arange(3), (2, 3)).copy()
    if complex_axis == "slow":
        slow = slow + 10j * (slow + 1)
    else:
        fast = fast + 10j * (fast + 1)

    with pytest.raises(ValueError, match=complex_axis):
        worker.for_shaped_2d(
            {"slow": slow, "fast": fast},
            np.arange(6).reshape(2, 3),
        )


@pytest.mark.parametrize("shaped", [False, True], ids=["unshaped", "shaped"])
@pytest.mark.parametrize("complex_values", [False, True], ids=["real", "complex"])
@pytest.mark.parametrize("force_sql", [False, True], ids=["cache", "bounded-sql"])
def test_real_qcodes_heatmap_preserves_real_values_or_rejects_complex(
    tmp_path, shaped, complex_values, force_sql,
):
    path = tmp_path / "heatmap.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("heatmap", sample_name="test")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("x")
    measurement.register_custom_parameter("y")
    measurement.register_custom_parameter(
        "signal", setpoints=("y", "x"),
        paramtype="complex" if complex_values else "numeric",
    )
    if shaped:
        measurement.set_shapes({"signal": (2, 3)})

    with measurement.run() as datasaver:
        for y in range(2):
            for x in range(3):
                value = x + 10 * y
                if complex_values:
                    value += 1j * (100 + x + 10 * y)
                datasaver.add_result(("x", x), ("y", y), ("signal", value))
        run_id = datasaver.dataset.run_id

    before = hashlib.sha256(path.read_bytes()).digest()
    dataset = load_by_id_read_only(run_id, str(path))
    try:
        params = {param.name: param for param in dataset.get_parameters()}
        worker = loader(
            dataset.cache, params["signal"], params,
            {"x": "x", "y": "y"},
            force_sql_heatmap=force_sql,
        )
        finished, errors = _run_worker(worker)

        if not force_sql and not complex_values:
            assert worker.cache_data["signal"]["signal"].shape == (
                (2, 3) if shaped else (6,)
            )

        if complex_values:
            assert finished == [False]
            assert len(errors) == 1
            assert "complex" in str(errors[0]).lower()
            assert "signal" in str(errors[0])
            assert not hasattr(worker, "dataGrid")
        else:
            assert finished == [True]
            assert errors == []
            np.testing.assert_array_equal(worker.axis_data["x"], [0, 1, 2])
            np.testing.assert_array_equal(worker.axis_data["y"], [0, 1])
            np.testing.assert_array_equal(
                worker.dataGrid, [[0, 1, 2], [10, 11, 12]],
            )
    finally:
        dataset.conn.close()

    assert hashlib.sha256(path.read_bytes()).digest() == before
    assert not path.with_name(path.name + "-journal").exists()
    assert not path.with_name(path.name + "-wal").exists()


@pytest.mark.parametrize("complex_axis", ["fast", "slow"])
@pytest.mark.parametrize("shaped", [False, True], ids=["unshaped", "shaped"])
@pytest.mark.parametrize("force_sql", [False, True], ids=["cache", "bounded-sql"])
def test_complex_scalar_heatmap_coordinates_are_rejected_without_publication(
    tmp_path, monkeypatch, complex_axis, shaped, force_sql,
):
    path = tmp_path / f"complex_{complex_axis}.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("coordinates", sample_name="test")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter(
        "slow",
        paramtype="complex" if complex_axis == "slow" else "numeric",
    )
    measurement.register_custom_parameter(
        "fast",
        paramtype="complex" if complex_axis == "fast" else "numeric",
    )
    measurement.register_custom_parameter(
        "signal",
        setpoints=("slow", "fast"),
        paramtype="numeric",
    )
    if shaped:
        measurement.set_shapes({"signal": (2, 3)})

    with measurement.run() as datasaver:
        for slow_index in range(2):
            for fast_index in range(3):
                slow = slow_index + (10j * (slow_index + 1) if complex_axis == "slow" else 0)
                fast = fast_index + 1 + (10j * (fast_index + 1) if complex_axis == "fast" else 0)
                datasaver.add_result(
                    ("slow", slow),
                    ("fast", fast),
                    ("signal", slow_index * 10 + fast_index),
                )
        run_id = datasaver.dataset.run_id

    before = hashlib.sha256(path.read_bytes()).digest()
    dataset = load_by_id_read_only(run_id, str(path))
    try:
        params = {param.name: param for param in dataset.get_parameters()}
        worker = loader(
            dataset.cache,
            params["signal"],
            params,
            {"x": "fast", "y": "slow"},
            force_sql_heatmap=force_sql,
        )
        if force_sql:
            monkeypatch.setattr(worker_module, "MAX_SQL_HEATMAP_SOURCE_ROWS", 1)
            monkeypatch.setattr(
                worker,
                "_heatmap_spatial_summary",
                lambda *args: pytest.fail(
                    "SQLite aggregation ran before complex-coordinate validation"
                ),
            )
        finished, errors = _run_worker(worker)

        assert finished == [False]
        assert len(errors) == 1
        assert "complex" in str(errors[0]).lower()
        assert "coordinate" in str(errors[0]).lower()
        assert complex_axis in str(errors[0])
        assert not hasattr(worker, "dataGrid")
    finally:
        dataset.conn.close()

    assert hashlib.sha256(path.read_bytes()).digest() == before
    assert not path.with_name(path.name + "-journal").exists()
    assert not path.with_name(path.name + "-wal").exists()
