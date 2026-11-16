"""Arrange explicit plot ownership in tests without starting a worker."""

from qplot.windows._plot_refresh import PlotRefreshMixin
from qplot.windows._plot_refresh_controller import PlotRefreshSession


def set_plot_worker(window, worker):
    if isinstance(window, PlotRefreshMixin):
        controller = window._refresh_controller()
        controller.generation += 1
        controller._scheduled = None
        controller.session = PlotRefreshSession(
            controller.generation, worker, starting=False,
        )
    else:
        # Lightweight source doubles expose the same observation directly.
        window.worker = worker
