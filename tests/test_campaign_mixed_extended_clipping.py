"""Clipping mixed QCoDeS array records compares recorded values exactly."""

from decimal import Decimal, getcontext
from fractions import Fraction

import numpy as np
import pytest

from qplot.tools.plot_tools import pass_filter
from tests.windows.test_differentiation_integration import (
    apply_operations,
    operation_option,
)
from tests.windows.test_native_transform_labels import (
    no_callback_errors as _no_callback_errors,
)
from tests.windows.test_numeric_sample_preservation import array_plot
from tests.windows.test_plot_csv_precision import _export_from_dialog, _open_csv_dialog

no_callback_errors = _no_callback_errors


@pytest.mark.parametrize("operation,expected", [
    ("Limit Maximum", [1., 2., 2.5, 2.5]),
    ("Limit Minimum", [2.5, 2.5, 3., 4.]),
])
@pytest.mark.parametrize("shaped", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_mixed_extended_integer_records_clip_through_actual_control(
        tmp_path, monkeypatch, operation, expected, shaped, reverse, no_callback_errors):
    records = [
        {"x": np.array([0., 1.]), "signal": np.array([1., 2.], dtype=np.longdouble)},
        {"x": np.array([2., 3.]), "signal": np.array([3, 4], dtype=np.int64)},
    ]
    if reverse:
        records = records[::-1]
        expected = expected[2:] + expected[:2]
    with array_plot(tmp_path, monkeypatch, records, shape=(4,) if shaped else None) as (_window, plot):
        original = plot.line.getOriginalDataset()[1].copy()
        assert original.dtype.kind == "O"
        # Capture operation errors without showing a modal dialog. The source
        # helper independently checks the protected database bytes and mtimes.
        errors = []
        monkeypatch.setattr(plot, "show_error", lambda *args: errors.append(args))
        option = operation_option(plot, operation)
        option.input.setChecked(True)
        option.operation_row.input.setText("2.5")
        _worker, finished, raised = apply_operations(plot)
        assert finished == [True], [str(error) for error in raised]
        assert raised == []
        assert errors == []
        np.testing.assert_array_equal(np.asarray(plot.line.getOriginalDataset()[1], float), expected)
        np.testing.assert_array_equal(plot.line.getData()[1], expected)
        assert plot.line._qplot_precision_error is None
        dialog = _open_csv_dialog(plot)
        rows = _export_from_dialog(monkeypatch, plot, dialog, tmp_path / "clipped.csv", ",")
        assert [Decimal(row[1]) for row in rows[1:]] == list(map(Decimal, expected))
        np.testing.assert_array_equal(original, [3., 4., 1., 2.] if reverse else [1., 2., 3., 4.])


@pytest.mark.parametrize("which", ["low", "high"])
def test_extended_clipping_preserves_exact_retained_samples_and_specials(which):
    sign = 1 if which == "low" else -1
    tiny = np.nextafter(np.longdouble(0), np.longdouble(1))
    values = np.array([
        sign * (np.longdouble(1) + np.finfo(np.longdouble).eps),
        sign * tiny, np.longdouble("-0"), np.longdouble("nan"),
        sign * np.longdouble("inf"), sign * 4,
    ], dtype=object)
    originals = values.copy()
    context = getcontext().copy()
    actual = pass_filter(which, Decimal(sign) * Decimal("2.5"), {"y": values, "z": None})["y"]
    for index in (0, 1, 2):
        assert Fraction(actual[index]) == Fraction(*originals[index].as_integer_ratio())
    assert Decimal(str(actual[2])).is_signed()
    assert np.isnan(float(actual[3]))
    assert actual[4] == actual[5] == Decimal(sign) * Decimal("2.5")
    assert getcontext().prec == context.prec
    assert all(now is old for now, old in zip(values, originals, strict=True))


def test_extended_clipping_noop_retains_original_scalar_objects():
    values = np.array([np.longdouble("0.1"), 2, np.longdouble("-inf")], dtype=object)
    actual = pass_filter("low", Decimal("3"), {"y": values, "z": None})["y"]
    assert actual is not values
    assert all(now is old for now, old in zip(actual, values, strict=True))


@pytest.mark.parametrize("which", ["low", "high"])
def test_extended_clipping_compares_decimal_threshold_without_narrowing(which):
    middle = np.longdouble("0.1")
    values = np.array([
        np.nextafter(middle, np.longdouble("-inf")), middle,
        np.nextafter(middle, np.longdouble("inf")), 1,
    ], dtype=object)
    exact = [Fraction(*value.as_integer_ratio()) if isinstance(value, np.floating)
             else Fraction(value) for value in values]
    bound = Decimal("0.1")
    select = min if which == "low" else max
    expected = [select(value, Fraction(bound)) for value in exact]
    actual = pass_filter(which, bound, {"y": values, "z": None})["y"]
    assert [Fraction(value) for value in actual] == expected
    assert np.unique(actual).size == len(set(expected))


@pytest.mark.parametrize("cancel_at", [2, 12])
def test_extended_clipping_is_cancellable_without_changing_source(cancel_at):
    values = np.full(8192, np.longdouble("0.1"), dtype=object)
    values[-1] = 4
    original = values.copy()
    checks = 0

    def cancelled():
        nonlocal checks
        checks += 1
        return checks >= cancel_at

    # Platforms whose longdouble is float64 use the unchanged vectorized path.
    if np.finfo(np.longdouble).nmant <= np.finfo(float).nmant:
        pytest.skip("Platform has no extended floating dtype")
    with pytest.raises(InterruptedError, match="cancelled"):
        pass_filter("low", Decimal("2.5"), {"y": values, "z": None}, cancelled)
    assert checks == cancel_at
    assert all(now is old for now, old in zip(values, original, strict=True))
