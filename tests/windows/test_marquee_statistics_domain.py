"""Selection summaries must not report an unrepresentable result as zero."""

from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest
from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from tests.windows.test_native_transform_labels import (
    no_callback_errors as _no_callback_errors,
)
from tests.windows.test_numeric_sample_preservation import array_plot

no_callback_errors = _no_callback_errors


def _records(values, heatmap, mixed):
    signal = np.r_[values, 1., 2.]
    x = np.arange(len(signal))
    if heatmap:
        return [
            {"x": x, "slow": np.zeros(len(x)), "signal": signal},
            {"x": x, "slow": np.ones(len(x)),
             "signal": np.arange(3, 3 + len(x), dtype=np.int64 if mixed else float)},
        ]
    if mixed:
        return [{"x": x[:len(values)], "signal": np.asarray(values)},
                {"x": x[len(values):], "signal": np.array([1, 2], dtype=np.int64)}]
    return [{"x": x, "signal": signal}]


def _select(plot, heatmap, start, count):
    plot.set_marquee_rect(QtCore.QRectF(start - .25, -.25 if heatmap else -1.,
                                      count - .5, .5 if heatmap else 4.))


def _open_and_copy(plot, expected, qapplication):
    menu = plot._new_marquee_context_menu()
    try:
        action = next(action for action in menu.actions() if action.text() == "Stats...")
        assert action.isEnabled()
        action.trigger()
        dialog = plot._marquee_stats_dialog
        assert dialog.isVisible()
        buttons = dialog.findChild(qtw.QDialogButtonBox)
        next(button for button in buttons.buttons() if button.text() == "Copy").click()
        assert qapplication.clipboard().text() == expected
        table = dialog.findChild(qtw.QTableWidget)
        assert table.rowCount() == len(expected.splitlines())
        dialog.close()
    finally:
        menu.close()


@pytest.mark.parametrize("heatmap", [False, True], ids=["line", "heatmap"])
@pytest.mark.parametrize("mixed", [False, True], ids=["float-records", "mixed-records"])
@pytest.mark.parametrize("case", ["positive-mean", "negative-mean", "spread"])
def test_recorded_selection_refuses_unrepresentable_summaries_and_recovers(
    tmp_path, monkeypatch, qapplication, no_callback_errors, heatmap, mixed, case,
):
    tiny = np.nextafter(0., 1.)
    values = np.array([tiny, 2 * tiny] if case == "spread" else [2 * tiny, -tiny])
    if case == "negative-mean":
        values = -values
    exact = [Fraction(float(value)) for value in values]
    mean = sum(exact) / len(exact)
    assert mean != 0
    if case == "spread":
        assert float(mean) != 0 and float(abs(exact[1] - exact[0]) / 2) == 0
    else:
        assert float(mean) == 0

    with array_plot(tmp_path, monkeypatch, _records(values, heatmap, mixed),
                    heatmap=heatmap) as (_window, plot):
        assert Path(plot.ds.path_to_db).resolve() == (tmp_path / "exact_arrays.db").resolve()
        _select(plot, heatmap, 0, len(values))
        text = plot._marquee_stats_text()
        assert text is not None
        assert "X range:" in text and "Y range:" in text
        assert "outside the statistics display range" in text
        assert all(label not in text for label in ("Average:", "Standard deviation:", "Max:", "Min:"))
        assert plot.copy_marquee_stats_to_clipboard()
        assert qapplication.clipboard().text() == text
        _open_and_copy(plot, text, qapplication)

        # A valid selection in the same normally drawable acquisition still
        # produces the ordinary summaries after the explanatory dialog closes.
        _select(plot, heatmap, len(values), 2)
        recovered = plot._marquee_stats_text()
        assert recovered is not None
        assert "Average: 1.50" in recovered
        assert "Standard deviation: 5.00e-01" in recovered
        assert "outside" not in recovered
        _open_and_copy(plot, recovered, qapplication)


@pytest.mark.parametrize("heatmap", [False, True], ids=["line", "heatmap"])
@pytest.mark.parametrize("mixed", [False, True], ids=["float-records", "mixed-records"])
@pytest.mark.parametrize("constant", [False, True], ids=["exact-cancellation", "constant-zero"])
def test_recorded_selection_keeps_exact_zero_summaries(
    tmp_path, monkeypatch, qapplication, no_callback_errors, heatmap, mixed, constant,
):
    tiny = np.nextafter(0., 1.)
    values = np.zeros(2) if constant else np.array([tiny, -tiny])
    with array_plot(tmp_path, monkeypatch, _records(values, heatmap, mixed),
                    heatmap=heatmap) as (_window, plot):
        _select(plot, heatmap, 0, 2)
        text = plot._marquee_stats_text()
        assert text is not None
        assert "Average: 0.000" in text and "outside" not in text
        expected_spread = "0.000" if constant else "4.94e-324"
        assert f"Standard deviation: {expected_spread}" in text
        _open_and_copy(plot, text, qapplication)
