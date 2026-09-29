"""Real QCoDeS heatmaps must not silently discard complex components."""

import hashlib

import numpy as np
import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.datahandling.readonly import load_by_id_read_only
from qplot.tools.worker import loader


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
        finished, errors = [], []
        worker.emitter.finished.connect(finished.append)
        worker.emitter.errorOccurred.connect(errors.append)
        worker.run()

        if not force_sql:
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
