"""Frozen plot values and cancellable CSV serialization, independent of Qt."""

from __future__ import annotations

import csv
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from itertools import zip_longest

import numpy as np

from qplot.tools.native_transform_math import (
    _center_integer_samples,
    _center_object_samples,
    _safe_secant,
    native_fourier_transform,
)
from qplot.tools.plot_tools import _center_float_samples


def _check(cancelled: Callable[[], bool]) -> None:
    if cancelled():
        raise InterruptedError("CSV export cancelled.")


def csv_numeric_text(value: object) -> str:
    """Keep stored integer and extended-float values in untyped CSV text."""
    if isinstance(value, np.longdouble):
        return np.format_float_scientific(value, unique=True, trim="-")
    if isinstance(value, (float, np.floating)):
        return repr(float(value))
    return str(value)


def shared_coordinates_match(reference, candidate, *, cancelled=lambda: False) -> bool:
    """Validate shared columns without coercing mixed exact coordinates."""
    reference, candidate = np.asarray(reference), np.asarray(candidate)
    if reference.ndim != 1 or candidate.ndim != 1 or candidate.size > reference.size:
        return False
    for index, (left, right) in enumerate(zip(reference, candidate, strict=False)):
        if index % 1024 == 0:
            _check(cancelled)
        if isinstance(left, np.generic):
            left = left.item()
        if isinstance(right, np.generic):
            right = right.item()
        left_nan = left.is_nan() if isinstance(left, Decimal) else (
            bool(np.isnan(left)) if isinstance(left, (float, complex, np.inexact)) else False
        )
        right_nan = right.is_nan() if isinstance(right, Decimal) else (
            bool(np.isnan(right)) if isinstance(right, (float, complex, np.inexact)) else False
        )
        if left_nan or right_nan:
            if not (left_nan and right_nan):
                return False
        elif left != right:
            return False
    return True


def frozen_array(values) -> np.ndarray:
    """Detach the export from later refreshes or editing of the live plot."""
    result = np.array(values, copy=True)
    result.flags.writeable = False
    return result


@dataclass(frozen=True)
class CsvColumns:
    headers: tuple[str, ...]
    columns: tuple[np.ndarray, ...]
    coordinates: tuple[np.ndarray, ...] = ()
    shared_x: bool = False
    constants: tuple[tuple[str, object], ...] = ()

    def write(self, filename, cancelled=lambda: False, progress=lambda *_: None, *, separator=","):
        _check(cancelled)
        if self.shared_x and self.coordinates:
            reference = self.coordinates[0]
            if any(not shared_coordinates_match(reference, x, cancelled=cancelled)
                   for x in self.coordinates):
                raise ValueError(
                    "The traces have different X coordinates and cannot share one "
                    "CSV coordinate column. Select '(x,y) per plot' to retain "
                    "each trace's coordinates."
                )
        total = max((len(column) for column in self.columns), default=0)
        progress("Writing plot values", 0, total)
        with open(filename, "w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, delimiter=separator, quoting=csv.QUOTE_MINIMAL)
            writer.writerow((*self.headers, *(name for name, _ in self.constants)))
            for index, row in enumerate(zip_longest(*self.columns, fillvalue="")):
                if index % 1024 == 0:
                    _check(cancelled)
                    progress("Writing plot values", index, total)
                writer.writerow([csv_numeric_text(value) for value in (*row, *(v for _, v in self.constants))])
        _check(cancelled)
        progress("Writing plot values", total, total)
        return total


@dataclass(frozen=True)
class TransformedTrace:
    x: np.ndarray
    y: np.ndarray
    headers: tuple[str, str]
    fft: bool = False
    derivative: bool = False
    phase: bool = False
    subtract_mean: bool = False

    def values(self, cancelled=lambda: False):
        _check(cancelled)
        x, y = self.x, self.y
        if x.ndim != 1 or y.ndim != 1 or x.shape != y.shape:
            raise ValueError("Transformed CSV requires paired one-dimensional X/Y samples.")
        if not x.size:
            return x, y
        if self.derivative or self.phase:
            x = y[:-1] if self.phase else x[:-1]
            y = _safe_secant(self.x, self.y, cancelled_callback=cancelled)
        else:
            if self.subtract_mean:
                if y.dtype.kind in "iu":
                    y = _center_integer_samples(y, cancelled_callback=cancelled)
                elif y.dtype.kind == "O":
                    y = _center_object_samples(y, cancelled_callback=cancelled)
                else:
                    y = _center_float_samples(y, cancelled_callback=cancelled)
            _check(cancelled)
            if self.fft and x.size:
                x, y = native_fourier_transform(x, y, cancelled_callback=cancelled)
        _check(cancelled)
        return x, y


@dataclass(frozen=True)
class TransformedCsv:
    traces: tuple[TransformedTrace, ...]
    shared_x: bool = False
    constants: tuple[tuple[str, object], ...] = ()

    def write(self, filename, cancelled=lambda: False, progress=lambda *_: None, *, separator=","):
        headers, columns, coordinates = [], [], []
        for index, trace in enumerate(self.traces):
            _check(cancelled)
            progress("Transforming plot values", index, len(self.traces))
            x, y = trace.values(cancelled)
            coordinates.append(x)
            if self.shared_x and index:
                headers.append(trace.headers[1])
                columns.append(y)
            else:
                headers.extend(trace.headers)
                columns.extend((x, y))
        return CsvColumns(tuple(headers), tuple(columns), tuple(coordinates), self.shared_x, self.constants).write(
            filename, cancelled, progress, separator=separator,
        )


@dataclass(frozen=True)
class HeatmapCsv:
    headers: tuple[str, str, str]
    x: np.ndarray
    y: np.ndarray
    grid: np.ndarray
    constants: tuple[tuple[str, object], ...] = ()

    def write(self, filename, cancelled=lambda: False, progress=lambda *_: None, *, separator=","):
        total = self.grid.size
        _check(cancelled)
        progress("Writing current heatmap grid", 0, total)
        with open(filename, "w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, delimiter=separator, quoting=csv.QUOTE_MINIMAL,
                                lineterminator="\n")
            writer.writerow((*self.headers, *(name for name, _ in self.constants)))
            count = 0
            for j, y in enumerate(self.y):
                for i, x in enumerate(self.x):
                    if count % 1024 == 0:
                        _check(cancelled)
                        progress("Writing current heatmap grid", count, total)
                    writer.writerow((csv_numeric_text(x), csv_numeric_text(y),
                                     csv_numeric_text(self.grid[j, i]),
                                     *(csv_numeric_text(v) for _, v in self.constants)))
                    count += 1
        _check(cancelled)
        progress("Writing current heatmap grid", total, total)
        return total
