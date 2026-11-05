"""Raw export preserves acquisition records and exact numeric samples."""

from types import SimpleNamespace

import numpy as np

from qplot.datahandling.export_data import (
    csv_scalar_columns,
    exportable_measurement_params,
)


def test_export_parameters_keep_dependents_then_standalone_measurements():
    axis = SimpleNamespace(name="gate", depends_on_=())
    current = SimpleNamespace(name="current", depends_on_=("gate",))
    temperature = SimpleNamespace(name="temperature", depends_on_=())
    voltage = SimpleNamespace(name="voltage", depends_on_=("gate",))
    dataset = SimpleNamespace(get_parameters=lambda: [temperature, axis, current, voltage])

    assert exportable_measurement_params(dataset) == [current, voltage, temperature]


def test_export_broadcasts_singletons_per_record_without_inventing_empty_samples():
    signal = np.empty(3, dtype=object)
    signal[0] = np.array([2**63 + 1, 2**63 + 3], dtype=np.uint64)
    signal[1] = np.array([], dtype=np.uint64)
    signal[2] = np.array([0.25])
    data = {"gate": np.array([1, 2, 3]), "signal": signal}

    columns = csv_scalar_columns(data, "signal")

    np.testing.assert_array_equal(columns["gate"], [1, 1, 3])
    assert list(columns["signal"]) == [2**63 + 1, 2**63 + 3, 0.25]
    assert columns["signal"].dtype == np.dtype(object)


def test_export_flattens_nested_object_cells_without_common_dtype_rounding():
    nested = np.empty(2, dtype=object)
    first_record = np.empty(2, dtype=object)
    first_record[0] = np.array([2**63 + 1], dtype=np.uint64)
    first_record[1] = [0.5]
    nested[0] = first_record
    nested[1] = (np.array([2, 3]),)

    columns = csv_scalar_columns({"signal": nested, "gate": np.array([10, 20])})

    assert list(columns["signal"]) == [2**63 + 1, 0.5, 2, 3]
    np.testing.assert_array_equal(columns["gate"], [10, 10, 20, 20])
