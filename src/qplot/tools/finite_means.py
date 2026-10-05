"""Bounded finite bin means with a rare exact fallback for risky sums."""

from fractions import Fraction

import numpy as np


class FiniteBinMeans:
    """Keep only one count/sum per bin, never the original sample history."""

    def __init__(self, shape, check_cancelled=lambda: None):
        self.sums = np.zeros(shape, dtype=float)
        self.counts = np.zeros(shape, dtype=np.int64)
        self.exact = {}
        self.check_cancelled = check_cancelled
        self._largest = 0.
        self._samples = 0
        self._positive = np.zeros(shape, dtype=bool)
        self._negative = np.zeros(shape, dtype=bool)
        self._integer = np.zeros(shape, dtype=bool)
        self._absolute_sums = np.zeros(shape, dtype=float)

    def add(self, indices, values):
        self.check_cancelled()
        original = np.asarray(values).ravel()
        values = original.astype(float)
        if not values.size:
            return
        targets = np.ravel_multi_index(indices, self.sums.shape)
        np.logical_or.at(self._positive.ravel(), targets, values > 0)
        np.logical_or.at(self._negative.ravel(), targets, values < 0)
        mixed = self._positive & self._negative
        # Original object cells may retain clipped decimal bounds or mixed
        # integer records. Large integer cells also need exact replay even
        # when all samples have the same sign: conversion or summation can
        # round before their mean is formed.
        sensitive = np.zeros(self.sums.shape, dtype=bool)
        if original.dtype.kind == "O":
            sensitive.ravel()[targets] = True
        elif original.dtype.kind in "iu":
            self._integer.ravel()[targets] = True
        # Several individually exact integers can have an inexact total.
        # Retain the accumulated absolute bound and integer presence across
        # chunks, including a later floating record in the same bin.
        with np.errstate(over="ignore", invalid="ignore"):
            np.add.at(self._absolute_sums.ravel(), targets, np.abs(values))
        sensitive |= self._integer & (self._absolute_sums >= 2**53)
        self._largest = max(self._largest, float(np.max(np.abs(values))))
        self._samples += values.size
        # Leave headroom for rounded additions at the finite sum boundary.
        # This conservative global bound makes the normal path vectorized.
        # It changes only whether we retain sums for recovery, never scaling
        # the values in other bins (which could erase small measurements).
        safe_sample = (np.finfo(float).max / 2) / self._samples
        risk = (bool(self.exact) or self._largest > safe_sample
                or np.any(mixed) or np.any(sensitive))
        previous = self.sums.copy() if risk else None
        with np.errstate(over="ignore", invalid="ignore"):
            np.add.at(self.sums.ravel(), targets, values)
        np.add.at(self.counts.ravel(), targets, 1)
        if risk:
            assert previous is not None
            # Promote before overflow: waiting for a later chunk to overflow
            # would already have rounded away small cancellation residuals.
            largest = np.abs(previous)
            np.maximum.at(largest.ravel(), targets, np.abs(values))
            for index, target in enumerate(np.flatnonzero(
                    (largest > safe_sample) | mixed | sensitive)):
                if index % 1024 == 0:
                    self.check_cancelled()
                self.exact.setdefault(int(target), Fraction(float(previous.ravel()[target])))
            if self.exact:
                self._add_exact(targets, original)
                self.sums.ravel()[list(self.exact)] = 0.
        self.check_cancelled()

    def _add_exact(self, targets, values):
        selected = np.flatnonzero(np.isin(targets, list(self.exact)))
        for index, position in enumerate(selected):
            if index % 1024 == 0:
                self.check_cancelled()
            value = values[position]
            if isinstance(value, np.generic):
                value = value.item()
            self.exact[int(targets[position])] += Fraction(value)

    def begin_exact_replay(self):
        """Discard rounded history for promoted bins, retaining their counts."""
        self.check_cancelled()
        for index, target in enumerate(self.exact):
            if index % 1024 == 0:
                self.check_cancelled()
            self.exact[target] = Fraction()
        return bool(self.exact)

    def replay(self, indices, values):
        """Revisit original samples once; only promoted totals are changed."""
        self.check_cancelled()
        targets = np.ravel_multi_index(indices, self.sums.shape)
        # Retain original integer/object values instead of converting these
        # selected samples through the ordinary display-sum float dtype.
        self._add_exact(targets, np.asarray(values).ravel())
        self.check_cancelled()

    def means(self):
        self.check_cancelled()
        result = np.full(self.sums.shape, np.nan)
        np.divide(self.sums, self.counts, out=result, where=self.counts > 0)
        for index, (target, total) in enumerate(self.exact.items()):
            if index % 1024 == 0:
                self.check_cancelled()
            result.ravel()[target] = float(total / int(self.counts.ravel()[target]))
        self.check_cancelled()
        return result
