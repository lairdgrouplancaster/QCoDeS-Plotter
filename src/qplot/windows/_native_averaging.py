"""Average displayed source curves without processing generated curves again."""

from fractions import Fraction
from typing import Any

import numpy as np
import pyqtgraph as pg
from PyQt6 import QtCore
from pyqtgraph.graphicsItems.PlotDataItem import PlotDataset

from qplot.tools.sample_statistics import finite_mean


class _AveragePlotDataItem(pg.PlotDataItem):
    """Render samples already mapped, clipped and downsampled by their sources."""

    def _ignore_processing(self, *_args, **_kwargs):
        pass

    # PlotItem.addItem and its controls configure every PlotDataItem. These
    # generated items contain display coordinates, so all processing is done.
    setFftMode = _ignore_processing
    setDerivativeMode = _ignore_processing
    setPhasemapMode = _ignore_processing
    setSubtractMeanMode = _ignore_processing
    setLogMode = _ignore_processing
    setDownsampling = _ignore_processing
    setClipToView = _ignore_processing

    def _getDisplayDataset(self) -> PlotDataset | None:
        return self._dataset


def is_native_source_curve(item: object) -> bool:
    """Generated averages are render results, never transform/validation inputs."""
    return isinstance(item, pg.PlotDataItem) and not isinstance(item, _AveragePlotDataItem)


class NativePlotItem(pg.PlotItem):
    """Keep PyQtGraph's native grouping and positional averaging semantics."""

    _qplot_recomputing_averages = False
    avgCurves: dict[tuple, list[Any]]

    def addItem(self, item, *args, **kwargs):
        if is_native_source_curve(item) and item not in self.items:
            item._qplot_skip_average = "skipAverage" in kwargs
            super().addItem(item, *args, **kwargs)
            item.sigPlotChanged.connect(self.recomputeAverages)
        else:
            super().addItem(item, *args, **kwargs)

    def removeItem(self, item):
        source = is_native_source_curve(item) and item in self.items
        if source:
            item.sigPlotChanged.disconnect(self.recomputeAverages)
        super().removeItem(item)
        if source:
            self.recomputeAverages()

    def recomputeAverages(self, *_args):
        if not self.ctrl.averageGroup.isChecked() or self._qplot_recomputing_averages:
            return
        self._qplot_recomputing_averages = True
        try:
            for _count, average in tuple(self.avgCurves.values()):
                self.removeItem(average)
            self.avgCurves = {}
            # addAvgCurve adds rendered averages to PlotItem.curves. Snapshot
            # only measured inputs so those additions cannot feed back in.
            sources = tuple(curve for curve in self.curves if is_native_source_curve(curve))
            for curve in sources:
                self.addAvgCurve(curve)
            self.replot()
        finally:
            self._qplot_recomputing_averages = False

    def addAvgCurve(self, curve):
        if not is_native_source_curve(curve) or getattr(curve, "_qplot_skip_average", False):
            return
        x, y = curve.getData()
        if x is None or y is None or len(y) == 0:
            return
        # The average is display data. Widen before arithmetic without
        # modifying the measured arrays retained by the source curves.
        if y.dtype.kind in "fc":
            y = y.astype(np.result_type(y.dtype, np.float64), copy=False)
        removed: list[str] = []
        retained: list[str] = []
        for index in range(self.ctrl.avgParamList.count()):
            item = self.ctrl.avgParamList.item(index)
            target = removed if item.checkState() == QtCore.Qt.CheckState.Checked else retained
            target.append(str(item.text()))
        if self.ctrl.avgParamList.count() and not removed:
            return
        params = {
            ".".join(key) if isinstance(key, tuple) else key: value
            for key, value in self.itemMeta.get(curve, {}).items()
        }
        for key in removed:
            params.pop(key, None)
        for key in retained:
            params.setdefault(key, None)
        group_key = tuple(params.items())
        if group_key not in self.avgCurves:
            average = _AveragePlotDataItem()
            average.setPen(self.avgPen)
            average.setShadowPen(self.avgShadowPen)
            average.setAlpha(1., False)
            average.setZValue(100)
            self.addItem(average, skipAverage=True)
            self.avgCurves[group_key] = [0, average]
        self.avgCurves[group_key][0] += 1
        count, average = self.avgCurves[group_key]
        # Preserve native compatibility: combine equal-shaped Y by index and
        # keep the first X; a changed Y shape replaces the displayed samples.
        if average.yData is not None and y.shape == average.yData.shape:
            average._qplot_mean_sources.append((1, y))
            average._qplot_minimum = np.minimum(average._qplot_minimum, y)
            average._qplot_maximum = np.maximum(average._qplot_maximum, y)
            # A difference update retains small variations and constant
            # values near float's upper bound. Opposite extremes can overflow
            # that difference; bounded weights handle those cells and preserve
            # the ordinary arithmetic behavior of NaN and infinite samples.
            y = y.astype(np.result_type(y.dtype, np.float64), copy=False)
            previous = average.yData.astype(
                np.result_type(average.yData.dtype, np.float64), copy=False,
            )
            with np.errstate(over="ignore", invalid="ignore"):
                difference = y - previous
                updated = previous + difference / float(count)
                fallback = ~np.isfinite(difference)
                if np.any(fallback):
                    updated[fallback] = (
                        previous[fallback] * ((count - 1) / float(count))
                        + y[fallback] / float(count)
                    )
            y = updated
            x = average.xData
            # A rounded running mean loses small measurements when later
            # traces cancel a large offset. Revisit sensitive cells using
            # references to display samples already owned by sources.
            sources = average._qplot_mean_sources
            exact_cells = any(values.dtype.kind in "iuO" for _weight, values in sources)
            mixed_sign = (average._qplot_minimum < 0) & (average._qplot_maximum > 0)
            # Large same-sign integers can round before their mean is formed,
            # even when its correctly rounded float result is distinct. Limit
            # exact replay to those cells and the cancellation cases above.
            large_integers = exact_cells & (
                (average._qplot_minimum <= -(2**53))
                | (average._qplot_maximum >= 2**53)
            )
            repair = ((mixed_sign | large_integers)
                      & np.isfinite(average._qplot_minimum)
                      & np.isfinite(average._qplot_maximum))
            for cell_index in np.flatnonzero(repair):
                cells = np.array([values[cell_index] for _weight, values in sources],
                                 dtype=object if exact_cells else None)
                if all(weight == 1 for weight, _values in sources):
                    y[cell_index] = finite_mean(cells)
                else:
                    # Native shape replacement treats the replacement as
                    # the preceding count of curves, retaining that weight.
                    total = sum((weight * Fraction(value.item() if isinstance(value, np.generic)
                                                   else value)
                                 for (weight, _values), value in zip(sources, cells, strict=True)),
                                Fraction())
                    y[cell_index] = float(total / count)
        else:
            average._qplot_mean_sources = [(count, y)]
            average._qplot_minimum = y.copy()
            average._qplot_maximum = y.copy()
        average.setData(x, y, stepMode=curve.opts["stepMode"])
