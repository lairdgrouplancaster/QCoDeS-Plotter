"""Shared fixtures and assertions for axis auto padding overflow."""

import sys

import pytest

from qplot.windows._plot_feedback import PlotWindowFeedbackMixin

__all__ = [
    '_axis_ranges',
    'axis_messages',
    'callback_errors',
]


@pytest.fixture(autouse=True)
def callback_errors(monkeypatch):
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *error: errors.append(error))
    yield
    assert errors == []


@pytest.fixture(autouse=True)
def axis_messages(monkeypatch):
    messages = []
    original = PlotWindowFeedbackMixin.show_status

    def record(window, message, *args):
        messages.append((window, message))
        original(window, message, *args)

    monkeypatch.setattr(PlotWindowFeedbackMixin, "show_status", record)
    return messages


def _axis_ranges(plot, *, target=False):
    # Compare the ViewBox dimension that owns each semantic axis. Its linked
    # perpendicular dimension can shift by a pixel when Qt adjusts the layout.
    ranges = {}
    for axis in ("x", "y", "x2", "y2"):
        viewbox = plot._axis_scale_viewbox(axis)
        bounds = viewbox.getState()["targetRange"] if target else viewbox.viewRange()
        ranges[axis] = bounds[plot._axis_scale_axis_number(axis)][:]
    return ranges
