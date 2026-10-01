"""qPlot's integer-safe mapping for PyQtGraph's native line controls."""

import numpy as np
import pyqtgraph as pg
from pyqtgraph.graphicsItems.PlotDataItem import PlotDataset


def _safe_difference(values: np.ndarray) -> np.ndarray:
    """Subtract integers exactly, then convert differences for division."""
    if values.dtype.kind in "iu":
        # Narrow integers fit in int64 even after subtraction. For 64-bit
        # integers, Python integers cover the full signed/unsigned difference
        # range. Converting samples to float first would erase adjacent steps
        # above 2**53; casting uint64 to int64 would wrap at the signed boundary.
        dtype = np.int64 if values.dtype.itemsize < 8 else object
        return np.diff(values.astype(dtype)).astype(np.float64)
    return np.diff(values)


class NativePlotDataItem(pg.PlotDataItem):
    """Keep original measurements while mapping derivatives without overflow."""

    _datasetMapped: PlotDataset | None

    def _getDisplayDataset(self) -> PlotDataset | None:
        # Isolate the private PyQtGraph mapping hook here. Seed only the mapped
        # cache; upstream still owns log scaling, clipping, downsampling, curve
        # updates and cache invalidation through its ordinary native setters.
        source = self._dataset
        if (source is not None and self._datasetMapped is None
                and (self.opts["derivativeMode"] or self.opts["phasemapMode"])):
            # Native derivative/phase mapping uses original samples, overriding
            # mean subtraction and (in phase mode) FFT. Phase mode replaces both
            # coordinates, so FFT/log-X must not discard its first sample.
            x = source.y[:-1] if self.opts["phasemapMode"] else source.x[:-1]
            y = _safe_difference(source.y) / _safe_difference(source.x)
            mapped = PlotDataset(x, y)
            if True in self.opts["logMode"]:
                mapped.applyLogMapping(self.opts["logMode"])
            self._datasetMapped = mapped
        return super()._getDisplayDataset()
