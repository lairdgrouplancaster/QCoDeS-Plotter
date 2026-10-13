"""Constant traces recover from an unusable prior viewport only under Auto."""

import numpy as np
import pytest
from PyQt6 import QtTest

from tests.windows.test_native_transform_labels import click_control
from tests.windows.test_numeric_sample_preservation import array_plot
from tests.windows.test_plot_integration import wait_for


@pytest.mark.parametrize("axis", ["x", "y"])
@pytest.mark.parametrize("tiny", [1e-309, np.nextafter(0., 1.)])
def test_constant_auto_recovers_tiny_manual_view_after_refresh(
        tmp_path, monkeypatch, qapplication, axis, tiny):
    x, y = (np.zeros(4), np.arange(4.)) if axis == "x" else (np.arange(4.), np.zeros(4))
    with array_plot(tmp_path, monkeypatch, [{"x": x, "signal": y}]) as (_main, plot):
        viewbox = plot.vb
        number = 0 if axis == "x" else 1
        viewbox.disableAutoRange()
        viewbox.setRange(**{f"{axis}Range": (tiny, 2 * tiny)}, padding=0)
        QtTest.QTest.qWait(30)
        qapplication.processEvents()
        manual_range = viewbox.viewRange()[number][:]
        assert plot.line.getData() == (None, None)
        assert not plot.line.curve.isVisible()

        # Publishing the same data must respect the manual viewport.
        plot.refreshWindow(force=True)
        wait_for(lambda: not plot.worker.running)
        plot.monitor.stop()
        QtTest.QTest.qWait(30)
        assert viewbox.viewRange()[number] == manual_range
        assert plot.line.getData() == (None, None)

        plot.force_all_axes_autoscale()
        QtTest.QTest.qWait(30)
        assert plot.line._qplot_precision_error is None
        assert plot.line.curve.isVisible()
        assert not plot.plot_state_overlay.frame.isVisible()
        transform = plot.line.sceneTransform()
        assert np.isfinite(transform.m11()) and np.isfinite(transform.m22())
        for actual, expected in zip(plot.line.getData(), (x, y), strict=True):
            np.testing.assert_array_equal(actual, expected)
        for actual, expected in zip(plot.line.getOriginalDataset(), (x, y), strict=True):
            np.testing.assert_array_equal(actual, expected)


def test_subnormal_centering_stays_visible_after_settled_refresh(
        tmp_path, monkeypatch, qapplication):
    tiny = np.nextafter(0., 1.)
    x, y = np.arange(4.), np.array([tiny, tiny, 2 * tiny, 2 * tiny])
    with array_plot(tmp_path, monkeypatch, [{"x": x, "signal": y}]) as (_main, plot):
        QtTest.QTest.qWait(30)
        assert plot.line.getData() == (None, None)
        click_control(plot, "subtractMeanCheck")
        for refresh in (False, True):
            if refresh:
                plot.refreshWindow(force=True)
                wait_for(lambda: not plot.worker.running)
                plot.monitor.stop()
            QtTest.QTest.qWait(30)
            qapplication.processEvents()
            assert plot.line.curve.isVisible()
            assert plot.line._qplot_precision_error is None
            assert not plot.plot_state_overlay.frame.isVisible()
            np.testing.assert_array_equal(plot.line.getData()[1], [-0., -0., 0., 0.])
            np.testing.assert_array_equal(plot.line.getOriginalDataset()[1], y)
            assert np.isfinite(plot.line.sceneTransform().m22())
