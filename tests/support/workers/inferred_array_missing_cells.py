"""Shared fixtures and assertions for inferred array missing cells."""

from contextlib import contextmanager
from fractions import Fraction

import numpy as np
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.datahandling.readonly import load_by_id_read_only
from tests.support.windows.complex_line_data import database_state

__all__ = [
    '_assert_columns',
    '_source',
    '_stored_columns',
]


@contextmanager
def _source(tmp_path, *, widths=(2,), heatmap=False, shaped=False,
            dtype=float, other_type="array", direct=False, output_type="array",
            nonfinite=False, missing_output=False, empty_dtype=None,
            text_value="literal None"):
    path = tmp_path / "inferred-arrays.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("Missing inferred basis cells", "regression")
    measurement = Measurement(exp=experiment)
    # Numeric output exercises an absent first declared array in its tree.
    measurement.register_custom_parameter("x", paramtype="numeric" if output_type == "numeric" else "array")
    if heatmap:
        measurement.register_custom_parameter("slow", paramtype="array")
    axes = ("slow", "x") if heatmap else ("x",)
    measurement.register_custom_parameter("signal", paramtype="array", setpoints=axes)
    measurement.register_custom_parameter("other", paramtype=other_type, setpoints=axes)
    measurement.register_custom_parameter("converted", paramtype=output_type,
                                          setpoints=axes, basis=("signal", "other"))
    if shaped:
        measurement.set_shapes({
            name: (2, 3) if heatmap else (6,)
            for name in ("signal", "other", "converted")
        })
    try:
        with measurement.run(write_in_background=False, in_memory_cache=False) as saver:
            for row, width in enumerate(widths):
                shape = () if width is None else (width,)
                size = 1 if width is None else width
                x = np.arange(size, dtype=float).reshape(shape)
                values = np.arange(size, dtype=dtype).reshape(shape)
                if not size and empty_dtype is not None:
                    x, values = x.astype(empty_dtype), values.astype(empty_dtype)
                if np.dtype(dtype).kind in "iu":
                    values += np.asarray(2**63 + 1 if np.dtype(dtype).kind == "u" else 2**60 + 1, dtype=dtype)
                elif dtype is np.longdouble:
                    values = np.longdouble(1) + values * np.finfo(np.longdouble).eps
                else:
                    values += 100 + 10 * row
                if nonfinite:
                    values.flat[:3] = [np.nan, np.inf, -np.inf][:min(size, 3)]
                columns = {"x": x, "signal": values.copy(), "converted": values.copy()}
                if heatmap:
                    columns["slow"] = np.full(shape, row, dtype=float)
                if output_type == "numeric":
                    columns["x"], columns["converted"] = float(row), float(100 + row)
                columns["other"] = {
                    "array": values.copy(), "numeric": 7.5,
                    "text": text_value, "complex": 2 + 3j,
                }[other_type]
                if direct:
                    # Public DataSet writes allow valid mixed storage without
                    # DataSaver flattening scalar-root trees into array fields.
                    saver.dataset.add_results([
                        {name: value for name, value in columns.items() if name != absent}
                        for absent in ("signal", "other")
                    ])
                else:
                    saver.add_result(*columns.items())
                if missing_output:
                    saver.dataset.add_results([
                        {name: value for name, value in columns.items() if name != "converted"},
                    ])
            saver.flush_data_to_database(block=True)
            run_id, guid = saver.run_id, saver.dataset.guid
        saver.dataset.conn.close()
        experiment.conn.close()
        protected = database_state(path)
        dataset = load_by_id_read_only(run_id, str(path))
        try:
            yield path, guid, dataset
        finally:
            dataset.conn.close()
            assert database_state(path) == protected
    finally:
        saver.dataset.conn.close()
        experiment.conn.close()


def _stored_columns(dataset, end=None):
    names = [parameter.name for parameter in dataset.get_parameters()]
    quoted = ",".join('"' + name + '"' for name in names)
    rows = dataset.conn.execute(
        f'SELECT {quoted} FROM "{dataset.table_name}" WHERE converted IS NOT NULL ORDER BY id'
        + (f" LIMIT {int(end)}" if end is not None else ""),
    ).fetchall()
    expected = {name: [] for name in names}
    for row in rows:
        present_arrays = [value for value in row if isinstance(value, np.ndarray)]
        size = max((value.size for value in present_arrays), default=1)
        for name, value in zip(names, row, strict=True):
            if value is None:
                expected[name].extend([None] * size)
            elif isinstance(value, np.ndarray):
                expected[name].extend(value.ravel() if value.size != 1 else [value.item()] * size)
            else:
                expected[name].extend([value] * size)
    return expected, len(rows)


def _assert_columns(actual, expected):
    assert actual.keys() == expected.keys()
    for name, recorded in expected.items():
        values = actual[name]
        assert values.size == len(recorded), name
        for value, original in zip(values, recorded, strict=True):
            if original is None:
                assert value is None or np.isnan(value), (name, value)
            elif isinstance(original, str):
                assert value == original
            elif isinstance(original, (complex, np.complexfloating)):
                assert value == original or (np.isnan(value) and np.isnan(original))
            elif np.isnan(original):
                assert np.isnan(value)
            elif not np.isfinite(original):
                assert value == original
            elif isinstance(original, (int, np.integer)):
                assert Fraction(value) == Fraction(int(original))
            else:
                assert Fraction(*value.as_integer_ratio()) == Fraction(*original.as_integer_ratio())
