"""Trusted previews preserve the numeric meaning of decoded QCoDeS arrays."""

from dataclasses import replace

import numpy as np
import pytest

from qplot.datahandling.trusted_derived_rendering import render_trusted_derived_payload
from qplot.datahandling.trusted_work_scheduler import TrustedWorkKind
from tests.datahandling.test_trusted_array_preview import _blob
from tests.datahandling.test_trusted_derived_rendering import (
    _decode_png_rgba,
    _observation,
)


def _preview(columns):
    observation = _observation(dimensions=len(columns) - 1)
    observation = replace(
        observation,
        parameters=tuple(replace(parameter, paramtype="array")
                         for parameter in observation.parameters),
        sample_rows=((1, *(_blob(column) for column in columns)),),
        validated_2d_layouts=(),
    )
    return render_trusted_derived_payload(observation, TrustedWorkKind.THUMBNAIL)


def _image(payload):
    assert payload["status"] == "ok", payload
    image = dict(payload["images"][0])
    return _decode_png_rgba(image["bytes"]), image["sampled_points"]


@pytest.mark.parametrize("dimensions", [1, 2])
@pytest.mark.parametrize("boolean_column", ["coordinate", "signal"])
def test_boolean_array_previews_match_numeric_arrays(dimensions, boolean_column):
    columns = ([np.array([False, True]), np.array([True, False])] if dimensions == 1
               else [np.array([False, False, True, True]),
                     np.array([False, True, False, True]),
                     np.array([True, False, False, True])])
    if boolean_column == "coordinate":
        columns[-1] = columns[-1].astype(float)
    else:
        columns[:-1] = [column.astype(float) for column in columns[:-1]]
    expected = _image(_preview([column.astype(float) for column in columns]))
    assert _image(_preview(columns)) == expected


def test_array_heatmap_duplicate_cell_mean_preserves_cancellation_residual():
    actual = _preview([[0., 0., 0., 0., 0.], [0., 0., 0., 1., 2.],
                       [1e308, 1e-100, -1e308, 0., 1e-100]])
    expected = _preview([[0., 0., 0.], [0., 1., 2.], [1e-100 / 3, 0., 1e-100]])
    assert _image(actual)[0] == _image(expected)[0]
    assert _image(actual)[1] == 5


def test_array_heatmap_keeps_distinct_large_integer_coordinates():
    original = np.array([2**63, 2**63 + 1, 2**63 + 2], dtype=np.uint64)
    actual = _preview([[0., 0., 0.], original, [0., 1., 2.]])
    expected = _preview([[0., 0., 0.], [0., 1., 2.], [0., 1., 2.]])
    assert _image(actual) == _image(expected)


@pytest.mark.parametrize("column", [0, 1, 2])
def test_array_heatmap_finite_extremes_match_scaled_preview(column):
    columns = [[0., 0., 1., 1.], [0., 1., 0., 1.], [0., 1., 2., 3.]]
    columns[column] = [-1e308, -1e308, 1e308, 1e308] if column == 0 else [-1e308, 1e308, -1e308, 1e308]
    if column == 2:
        columns[column] = [-1e308, 0., 1e308, 5e307]
    normalized = [values if index != column else np.array(values) / 1e308
                  for index, values in enumerate(columns)]
    assert _image(_preview(columns)) == _image(_preview(normalized))


@pytest.mark.parametrize("column", [0, 1])
@pytest.mark.parametrize("values", [[-1e308, 0., 1e308], [0., 1e307, 2e307]])
def test_array_line_finite_extremes_match_scaled_geometry(column, values):
    columns = [[0., 1., 2.], [0., 1., 2.]]
    columns[column] = values
    normalized = [data if index != column else np.array(data) / 1e308
                  for index, data in enumerate(columns)]
    assert _image(_preview(columns)) == _image(_preview(normalized))


@pytest.mark.parametrize("columns,expected", [
    ([[False, False, True, True], [False, True, False, True], [True, False, False, True]],
     [[0., 0., 1., 1.], [0., 1., 0., 1.], [1., 0., 0., 1.]]),
    ([[0., 0., 0.], np.array([2**63, 2**63 + 1, 2**63 + 2], dtype=np.uint64), [0., 1., 2.]],
     [[0., 0., 0.], [0., 1., 2.], [0., 1., 2.]]),
    ([[0., 0., 1., 1.], [0., 1., 0., 1.], [-1e308, 0., 1e308, 5e307]],
     [[0., 0., 1., 1.], [0., 1., 0., 1.], [-1., 0., 1., .5]]),
])
def test_real_qcodes_array_previews_preserve_samples_and_database(tmp_path, columns, expected):
    from qcodes.dataset import (
        Measurement,
        initialise_or_create_database_at,
        load_or_create_experiment,
    )

    from qplot.datahandling.file_identity import database_instance
    from qplot.datahandling.trusted_live_queries import (
        TrustedMetadataQueryAdapter,
        TrustedSourceRevisionNamespace,
    )
    from tests.datahandling.test_stage5c_real_qcodes import (
        _load_basic_runs,
        _ReadOnlySqliteExecutor,
    )
    from tests.datahandling.test_trusted_plot import protected_state

    path = tmp_path / "preview.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("preview regression", sample_name="arrays")
    measurement = Measurement(exp=experiment)
    for name in ("x", "y"):
        measurement.register_custom_parameter(name, paramtype="array")
    measurement.register_custom_parameter("signal", paramtype="array", setpoints=("x", "y"))
    try:
        with measurement.run() as saver:
            # Public DataSet writes preserve Boolean NPY records, while
            # DataSaver's convenience validation restricts Boolean arrays.
            saver.dataset.add_results([{
                name: np.asarray(values)
                for name, values in zip(("x", "y", "signal"), columns, strict=True)
            }])
            run_id = saver.run_id
    finally:
        saver.dataset.conn.close()
        experiment.conn.close()
    before = protected_state(path)
    executor = _ReadOnlySqliteExecutor(path)
    try:
        adapter = TrustedMetadataQueryAdapter(executor, path)
        _load_basic_runs(adapter)
        observation = adapter.derived_source_observation(
            run_id, database_instance=database_instance(path),
            namespace=TrustedSourceRevisionNamespace.create(),
        )
        actual = render_trusted_derived_payload(observation, TrustedWorkKind.THUMBNAIL)
        assert _image(actual) == _image(_preview(expected))
    finally:
        executor.close()
    assert protected_state(path) == before


@pytest.mark.parametrize("axis", [0, 1])
@pytest.mark.parametrize("mixed", [False, True])
def test_line_preview_retains_adjacent_integer_steps(axis, mixed):
    observation = _observation()
    values = [2**53 + 1, 2**53 + 2, 2**53 + 4]
    if mixed:
        values[1:] = map(float, values[1:])
    rows = tuple((index + 1, value, other) if axis == 0 else (index + 1, other, value)
                 for index, (value, other) in enumerate(zip(values, [0., 1., 0.], strict=True)))
    expected_rows = tuple((index + 1, value - (2**53 + 1), other) if axis == 0
                          else (index + 1, other, value - (2**53 + 1))
                          for index, (value, other) in enumerate(zip([2**53 + 1, 2**53 + 2, 2**53 + 4],
                                                                   [0., 1., 0.], strict=True)))
    actual = render_trusted_derived_payload(replace(observation, sample_rows=rows), TrustedWorkKind.THUMBNAIL)
    expected = render_trusted_derived_payload(replace(observation, sample_rows=expected_rows), TrustedWorkKind.THUMBNAIL)
    assert _image(actual) == _image(expected)


@pytest.mark.parametrize("offsets,expected", [
    ([1, 1, 2, 3], [0., 1., 2.]),
    ([0, 1, 1, 2], [0., .5, 1.5]),
])
@pytest.mark.parametrize("baseline", [2**52, 2**63])
def test_repeated_large_integer_cell_means_keep_relative_colors(offsets, expected, baseline):
    values = np.array([baseline + offset for offset in offsets], dtype=np.uint64)
    actual = _preview([[0., 0., 0., 0.], [0., 0., 1., 2.], values])
    shifted = _preview([[0., 0., 0.], [0., 1., 2.], expected])
    assert _image(actual)[0] == _image(shifted)[0]
    assert _image(actual)[1] == 4
