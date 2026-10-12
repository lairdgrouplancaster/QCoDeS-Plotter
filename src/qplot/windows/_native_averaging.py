"""Average displayed source curves without processing generated curves again."""

import math
from fractions import Fraction
from typing import Any

import numpy as np
import pyqtgraph as pg
from PyQt6 import QtCore
from pyqtgraph.graphicsItems.PlotDataItem import PlotDataset

from qplot.tools.sample_statistics import finite_mean, sample_fraction


def _integer_totals(total, values, weight=1):
    """Accumulate in int64 when proven safe, otherwise use Python integers."""
    lower = weight * int(np.min(values))
    upper = weight * int(np.max(values))
    if total is not None:
        lower += int(np.min(total))
        upper += int(np.max(total))
    if np.iinfo(np.int64).min <= lower and upper <= np.iinfo(np.int64).max:
        # A large unsigned input may wrap before cancellation restores an
        # int64 total; only choose this path when each operand fits too.
        if (int(np.min(values)) >= np.iinfo(np.int64).min
                and int(np.max(values)) <= np.iinfo(np.int64).max
                and (total is None or total.dtype.kind == "i")):
            weighted = values.astype(np.int64) * weight
            return weighted if total is None else total + weighted
    weighted = values.astype(object) * weight
    return weighted if total is None else total.astype(object) + weighted


def _integer_average(total, count):
    # A representable integer numerator needs only the ordinary vectorized
    # division. Otherwise convert each NumPy integer to Python before dividing
    # so conversion cannot round the total prior to forming the mean.
    exact_float_bound = 2 ** (np.finfo(float).nmant + 1)
    if (count <= exact_float_bound and total.dtype.kind != "O"
            and int(np.min(total)) >= -exact_float_bound
            and int(np.max(total)) <= exact_float_bound):
        return total / count
    return np.fromiter((int(value) / count for value in total), dtype=float, count=len(total))


def _binary_integer_samples(values):
    """Represent finite binary samples as integer numerators and one exponent."""
    if values.dtype.kind in "iu":
        return values, 0
    if values.dtype.kind != "f" or not np.all(np.isfinite(values)):
        return None
    if np.finfo(values.dtype).nmant > np.finfo(float).nmant:
        return None
    if np.all(values == np.trunc(values)):
        shift = 0
    else:
        nonzero = np.abs(values[values != 0])
        shift = max(0, 53 - math.frexp(float(np.min(nonzero)))[1])
    with np.errstate(over="ignore"):
        scaled = np.ldexp(values, shift)
    if not np.all(np.isfinite(scaled)):
        return None
    if np.min(scaled) >= -(2**63) and np.max(scaled) < 2**63:
        return scaled.astype(np.int64), shift
    return np.frompyfunc(int, 1, 1)(scaled), shift


def _add_binary_total(total, shift, values, weight=1):
    encoded = _binary_integer_samples(values)
    if encoded is None:
        return None, 0
    numerators, value_shift = encoded
    common_shift = max(shift, value_shift)
    if total is not None and shift != common_shift:
        total = total.astype(object) * (1 << (common_shift - shift))
    if value_shift != common_shift:
        numerators = numerators.astype(object) * (1 << (common_shift - value_shift))
    return _integer_totals(total, numerators, weight), common_shift


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
    _qplot_average_display_sources: tuple[tuple[Any, Any, Any], ...]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._qplot_average_display_sources = ()
        # An owned timer is cancelled with the plot's QObject, including a
        # close before the queued display update reaches the event loop.
        self._qplot_average_refresh_timer = QtCore.QTimer(self)
        self._qplot_average_refresh_timer.setSingleShot(True)
        self._qplot_average_refresh_timer.timeout.connect(self._refresh_view_averages)

    def addItem(self, item, *args, **kwargs):
        if is_native_source_curve(item) and item not in self.items:
            item._qplot_skip_average = "skipAverage" in kwargs
            super().addItem(item, *args, **kwargs)
            item.sigPlotChanged.connect(self.recomputeAverages)
            display_changed = getattr(item, "sigDisplayChanged", None)
            if display_changed is not None:
                display_changed.connect(self._queue_view_averages)
        else:
            super().addItem(item, *args, **kwargs)

    def removeItem(self, item):
        source = is_native_source_curve(item) and item in self.items
        if source:
            item.sigPlotChanged.disconnect(self.recomputeAverages)
            display_changed = getattr(item, "sigDisplayChanged", None)
            if display_changed is not None:
                display_changed.disconnect(self._queue_view_averages)
        super().removeItem(item)
        if source:
            self.recomputeAverages()

    def recomputeAverages(self, *_args):
        if not self.ctrl.averageGroup.isChecked():
            self._qplot_average_display_sources = ()
            return
        if self._qplot_recomputing_averages:
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
            self._qplot_average_display_sources = self._average_display_sources()
            self.replot()
        finally:
            self._qplot_recomputing_averages = False

    def _average_display_sources(self):
        return tuple((curve, *curve.getData()) for curve in self.curves
                     if is_native_source_curve(curve)
                     and not getattr(curve, "_qplot_skip_average", False))

    def _queue_view_averages(self, *_args):
        if (self.ctrl.averageGroup.isChecked()
                and not self._qplot_average_refresh_timer.isActive()):
            self._qplot_average_refresh_timer.start(0)

    def _refresh_view_averages(self):
        if not self.ctrl.averageGroup.isChecked():
            return
        current = self._average_display_sources()
        previous = self._qplot_average_display_sources
        if (len(current) == len(previous)
                and all(all(now is old for now, old in zip(current_row, previous_row, strict=True))
                        for current_row, previous_row in zip(current, previous, strict=True))):
            return
        self.recomputeAverages()

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
            # Integer and mixed binary means can round at intermediate
            # updates. Retain integer totals in common binary units, then
            # round their final ratio once. This also avoids Fraction replay
            # across every displayed cell on the GUI thread.
            integer_total = average._qplot_integer_total
            shift = average._qplot_total_shift
            if exact_cells:
                if integer_total is None:
                    # An integer may follow floating sources. Encode their
                    # retained samples once, then update the total per curve.
                    for weight, values in sources:
                        integer_total, shift = _add_binary_total(integer_total, shift, values, weight)
                        if integer_total is None:
                            break
                else:
                    integer_total, shift = _add_binary_total(integer_total, shift, sources[-1][1])
            average._qplot_integer_total = integer_total
            average._qplot_total_shift = shift
            if integer_total is not None:
                y = _integer_average(integer_total, count * (1 << shift))
            repair = ((mixed_sign | exact_cells)
                      & np.isfinite(average._qplot_minimum)
                      & np.isfinite(average._qplot_maximum))
            if integer_total is not None:
                repair[:] = False
            for cell_index in np.flatnonzero(repair):
                cells = np.array([values[cell_index] for _weight, values in sources],
                                 dtype=object if exact_cells else None)
                if all(weight == 1 for weight, _values in sources):
                    y[cell_index] = finite_mean(cells)
                else:
                    # Native shape replacement treats the replacement as
                    # the preceding count of curves, retaining that weight.
                    total = sum((weight * sample_fraction(value)
                                 for (weight, _values), value in zip(sources, cells, strict=True)),
                                Fraction())
                    y[cell_index] = float(total / count)
        else:
            average._qplot_mean_sources = [(count, y)]
            average._qplot_minimum = y.copy()
            average._qplot_maximum = y.copy()
            average._qplot_integer_total = _integer_totals(None, y, count) if y.dtype.kind in "iu" else None
            average._qplot_total_shift = 0
        average.setData(x, y, stepMode=curve.opts["stepMode"])
