"""Trusted previews retain finite extended QCoDeS array samples."""

from dataclasses import replace
from fractions import Fraction
from sys import getsizeof

import numpy as np
import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.datahandling import trusted_array_preview
from qplot.datahandling.file_identity import database_instance
from qplot.datahandling.trusted_derived_rendering import (
    render_trusted_derived_payload,
    validate_trusted_derived_payload,
)
from qplot.datahandling.trusted_live_queries import (
    TrustedMetadataQueryAdapter,
    TrustedSourceRevisionNamespace,
)
from qplot.datahandling.trusted_work_scheduler import TrustedWorkKind
from tests.datahandling.test_stage5c_real_qcodes import (
    _load_basic_runs,
    _ReadOnlySqliteExecutor,
)
from tests.datahandling.test_trusted_array_preview import _blob
from tests.datahandling.test_trusted_derived_rendering import _observation
from tests.datahandling.test_trusted_plot import protected_state
from tests.test_bug_hunt_measurement_previews import _image


def _array_observation(columns):
    observation = _observation(dimensions=len(columns) - 1)
    return replace(
        observation,
        parameters=tuple(replace(parameter, paramtype="array")
                         for parameter in observation.parameters),
        sample_rows=((1, *(_blob(column) for column in columns)),),
        validated_2d_layouts=(),
    )


def _render(columns, kind=TrustedWorkKind.THUMBNAIL):
    payload = render_trusted_derived_payload(_array_observation(columns), kind)
    validate_trusted_derived_payload(payload)
    return _image(payload)


@pytest.mark.parametrize("kind", [TrustedWorkKind.THUMBNAIL, TrustedWorkKind.PREVIEW])
@pytest.mark.parametrize("dimensions,column", [(1, 0), (1, 1), (2, 0), (2, 1), (2, 2)])
def test_actual_qcodes_longdouble_previews_match_ordinary_values(tmp_path, dimensions, column, kind):
    columns = ([np.arange(3.), np.array([0., 1., 2.])] if dimensions == 1
               else [np.zeros(3), np.arange(3.), np.array([0., 1., 2.])])
    expected = _render(columns, kind)
    columns[column] = columns[column].astype(np.longdouble)
    names = ("x", "signal") if dimensions == 1 else ("y", "x", "signal")
    path = tmp_path / "extended-preview.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("Extended previews", sample_name="regression")
    measurement = Measurement(exp=experiment)
    for name in names[:-1]:
        measurement.register_custom_parameter(name, paramtype="array")
    measurement.register_custom_parameter("signal", paramtype="array", setpoints=names[:-1])
    try:
        with measurement.run(write_in_background=False, in_memory_cache=False) as saver:
            saver.add_result(*zip(names, columns, strict=True))
            run_id = saver.run_id
    finally:
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
        payload = render_trusted_derived_payload(observation, kind)
        validate_trusted_derived_payload(payload)
        assert _image(payload) == expected
    finally:
        executor.close()
    assert protected_state(path) == before


@pytest.mark.skipif(np.finfo(np.longdouble).nmant <= np.finfo(float).nmant,
                    reason="longdouble has no extra precision on this platform")
@pytest.mark.parametrize("dimensions,column", [(1, 0), (1, 1), (2, 0), (2, 1), (2, 2)])
@pytest.mark.parametrize("case", ["offset", "wide", "tiny"])
def test_extended_coordinates_and_signals_keep_relative_preview_geometry(dimensions, column, case):
    ordinary = ([np.arange(3.), np.array([0., 1., 2.])] if dimensions == 1
                else [np.arange(3.), np.arange(3.), np.array([0., 1., 2.])])
    actual = list(ordinary)
    if case == "offset":
        values = np.longdouble(1) + np.arange(3, dtype=np.longdouble) * np.finfo(np.longdouble).eps
    elif case == "wide":
        values = np.arange(3, dtype=np.longdouble) * np.ldexp(np.longdouble(1), 4000)
    else:
        values = np.arange(3, dtype=np.longdouble) * np.nextafter(np.longdouble(0), np.longdouble(1))
    actual[column] = values
    original = values.copy()
    assert _render(actual) == _render(ordinary)
    np.testing.assert_array_equal(values, original)


@pytest.mark.skipif(np.finfo(np.longdouble).nmant <= np.finfo(float).nmant,
                    reason="longdouble has no extra precision on this platform")
@pytest.mark.parametrize("mixed", [False, True])
def test_extended_duplicate_cell_means_preserve_cancellation_and_color(mixed):
    epsilon = np.finfo(np.longdouble).eps
    values = np.array([np.longdouble(1) + epsilon, -1, 0, epsilon], dtype=np.longdouble)
    columns = [np.zeros(4), np.array([0., 0., 1., 2.]), values]
    observation = _array_observation(columns)
    if mixed:
        observation = replace(observation, sample_rows=(
            (1, _blob([0.]), _blob([0.]), _blob(values[:1])),
            (2, _blob([0]), _blob([0]), _blob(np.array([-1], dtype=np.int64))),
            (3, _blob([0., 0.]), _blob([1., 2.]), _blob(values[2:])),
        ))
    payload = render_trusted_derived_payload(observation, TrustedWorkKind.THUMBNAIL)
    validate_trusted_derived_payload(payload)
    actual_image, points = _image(payload)
    expected_image, _ = _render([[0., 0., 0.], [0., 1., 2.], [.5, 0., 1.]])
    assert actual_image == expected_image
    assert points == 4


def test_extended_preview_nonfinite_samples_remain_missing():
    values = np.array([np.nan, -np.inf, np.inf, 1, 2], dtype=np.longdouble)
    assert _render([np.arange(5.), values]) == _render([np.arange(5.), values.astype(float)])
    assert _render([np.zeros(5), np.arange(5.), values]) == _render(
        [np.zeros(5), np.arange(5.), values.astype(float)],
    )


def test_extended_preview_conversion_is_cancellable():
    observation = _array_observation([np.arange(4096.), np.arange(4096, dtype=np.longdouble)])
    calls = 0

    def check():
        nonlocal calls
        calls += 1
        if calls == 8:
            raise InterruptedError("Preview cancelled")

    with pytest.raises(InterruptedError, match="cancelled"):
        render_trusted_derived_payload(observation, TrustedWorkKind.THUMBNAIL, cancel_check=check)
    assert calls == 8


@pytest.mark.skipif(np.finfo(np.longdouble).nmant <= np.finfo(float).nmant,
                    reason="longdouble has no extra precision on this platform")
def test_extended_preview_precision_budget_boundary(monkeypatch):
    values = np.longdouble(1) + np.arange(1, 4, dtype=np.longdouble) * np.finfo(np.longdouble).eps
    exact = [Fraction(*value.as_integer_ratio()) for value in values]
    required_bytes = sum(getsizeof(item) + getsizeof(item.numerator) + getsizeof(item.denominator)
                         for item in exact)
    observation = _array_observation([np.arange(3.), values])
    source_rows = observation.sample_rows
    monkeypatch.setattr(trusted_array_preview, "MAX_EXACT_SAMPLE_BYTES", required_bytes)
    expected = _render([np.arange(3.), np.arange(3.)])
    payload = render_trusted_derived_payload(observation, TrustedWorkKind.THUMBNAIL)
    assert _image(payload) == expected
    monkeypatch.setattr(trusted_array_preview, "MAX_EXACT_SAMPLE_BYTES", required_bytes - 1)
    payload = render_trusted_derived_payload(observation, TrustedWorkKind.THUMBNAIL)
    assert payload["status"] == "unsupported"
    assert "precision storage budget" in payload["description"]
    validate_trusted_derived_payload(payload)
    assert observation.sample_rows == source_rows


def test_exactly_representable_longdouble_values_do_not_use_precision_budget(monkeypatch):
    monkeypatch.setattr(trusted_array_preview, "MAX_EXACT_SAMPLE_BYTES", 0)
    ordinary = np.array([0., 1., 2.])
    assert _render([ordinary, ordinary.astype(np.longdouble)]) == _render([ordinary, ordinary])


@pytest.mark.skipif(np.finfo(np.longdouble).nmant <= np.finfo(float).nmant,
                    reason="longdouble has no extra precision on this platform")
def test_extended_precision_conversion_cancels_before_budget_exhaustion(monkeypatch):
    values = np.longdouble(1) + np.finfo(np.longdouble).eps * np.arange(1, 4097, dtype=np.longdouble)
    observation = _array_observation([np.arange(4096.), values])
    monkeypatch.setattr(trusted_array_preview, "MAX_EXACT_SAMPLE_BYTES", 1024 * 1024)
    calls = 0

    def check():
        nonlocal calls
        calls += 1
        if calls == 8:
            raise InterruptedError("Preview cancelled")

    with pytest.raises(InterruptedError, match="cancelled"):
        render_trusted_derived_payload(observation, TrustedWorkKind.THUMBNAIL, cancel_check=check)
    assert calls == 8
