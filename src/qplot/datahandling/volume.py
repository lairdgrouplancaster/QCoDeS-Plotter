"""Immutable, privately indexed measurement volumes and exact 2D slices.

Only the temporary index is writable. Source reads belong to the existing
protected plot loader. No dense 3D array is allocated, including for sparse
measurements; the allocation limit applies to each requested plane.
"""

import sqlite3
import tempfile
import threading
from collections import OrderedDict
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from qplot.datahandling.numeric_samples import (
    concatenate_record_samples,
    numeric_isfinite,
)
from qplot.tools.general import data2matrix


@dataclass(frozen=True)
class SliceFrame:
    x: np.ndarray
    y: np.ndarray
    values: np.ndarray
    counts: np.ndarray
    depth: float | None
    index: int
    samples: int
    raw_values: np.ndarray | None = None


class VolumeStore:
    """A captured volume, shared by slice and analysis workers until released."""

    def __init__(self, names, *, max_points=2_000_000, completed=False,
                 planned_shape=None, revision=0):
        if len(names) != 3 or len(set(names)) != 3:
            raise ValueError("A slice volume requires three distinct independent parameters.")
        self.names = tuple(names)
        self.max_points = max(1, int(max_points))
        self.completed = completed
        self.planned_shape = planned_shape
        self.revision = revision
        self.directory = tempfile.TemporaryDirectory(prefix="qplot-volume-")
        self.path = Path(self.directory.name) / "samples.db"
        self.axes = {}
        self.latest = {}
        self.sample_count = 0
        self.invalid_coordinates = 0
        self._frames = OrderedDict()
        self._cache_lock = threading.Lock()

    @classmethod
    def from_chunks(cls, names, chunks, *, check=lambda: None,
                    progress=lambda count: None, **kwargs):
        store = cls(names, **kwargs)
        coordinates: list[set[float]] = [set() for _ in names]
        try:
            # This is a new private file, never the measurement database.
            with closing(sqlite3.connect(store.path)) as target:
                target.execute("PRAGMA journal_mode=OFF")
                target.execute("PRAGMA cache_size=-4096")
                target.set_progress_handler(lambda: _interrupted(check), 1000)
                target.execute("CREATE TABLE samples(a REAL, b REAL, c REAL, value BLOB, dtype TEXT)")
                for columns in chunks:
                    check()
                    arrays = [np.asarray(column).reshape(-1) for column in columns]
                    if len(arrays) != 4 or len({a.size for a in arrays}) != 1:
                        raise ValueError("3D setpoint and measurement shapes do not match.")
                    if any(a.dtype.kind not in "biuf" for a in arrays[:3]) or arrays[3].dtype.kind not in "biufcO":
                        raise ValueError("3D viewing requires real numeric measurements and coordinates.")
                    # Coordinate conversion is checked by the source decoder.
                    # Require already validated doubles here rather than round
                    # integer/extended coordinates at an indexing boundary.
                    if any(a.dtype != np.dtype(float) for a in arrays[:3]):
                        raise ValueError("Volume coordinates must be validated floating-point values.")
                    valid = np.ones(arrays[0].size, dtype=bool)
                    for a in arrays[:3]:
                        valid &= np.isfinite(a)
                    store.invalid_coordinates += int(np.count_nonzero(~valid))
                    arrays = [a[valid] for a in arrays]
                    for name, values, known in zip(names, arrays[:3], coordinates, strict=True):
                        known.update(np.unique(values).tolist())
                        if len(known) > store.max_points:
                            raise ValueError("A 3D coordinate axis exceeds the full-resolution point limit.")
                        if values.size:
                            store.latest[name] = float(values[-1])
                    def encoded(value):
                        scalar = np.asarray(value)
                        if scalar.dtype.kind not in "biufc":
                            raise ValueError("3D viewing requires real numeric samples.")
                        return scalar.tobytes(), scalar.dtype.str
                    # Values retain their original dtype and bytes, including
                    # integers beyond 2**53 and extended floating-point samples.
                    for start in range(0, arrays[3].size, 4096):
                        check()
                        stop = start + 4096
                        target.executemany("INSERT INTO samples VALUES (?,?,?,?,?)", (
                            (float(a), float(b), float(c), *encoded(value))
                            for a, b, c, value in zip(*(a[start:stop] for a in arrays), strict=True)
                        ))
                    store.sample_count += arrays[3].size
                    progress(store.sample_count)
                for column in "abc":
                    check()
                    target.execute(f"CREATE INDEX slice_{column} ON samples({column})")
                target.commit()
            store.axes = {name: np.asarray(sorted(values), dtype=float)
                          for name, values in zip(names, coordinates, strict=True)}
            check()
            return store
        except BaseException:
            store.directory.cleanup()
            raise

    def depths(self, x, y):
        return self.axes[self.depth_name(x, y)]

    def depth_name(self, x, y):
        if x == y or x not in self.names or y not in self.names:
            raise ValueError("X and Y must be different volume parameters.")
        return next(name for name in self.names if name not in (x, y))

    def nearest_index(self, name, value):
        values = self.axes[name]
        if not values.size or value is None:
            return 0
        i = int(np.searchsorted(values, value))
        if i == values.size:
            return i - 1
        if i and abs(float(value) - values[i - 1]) <= abs(values[i] - float(value)):
            return i - 1
        return i

    def plane(self, x, y, index, *, check=lambda: None):
        depth_name = self.depth_name(x, y)
        depths = self.axes[depth_name]
        index = min(max(0, int(index)), max(0, len(depths) - 1))
        key = (x, y, index)
        check()
        with self._cache_lock:
            if key in self._frames:
                self._frames.move_to_end(key)
                return self._frames[key]
        xs, ys = self.axes[x], self.axes[y]
        if xs.size * ys.size > self.max_points:
            raise ValueError(
                "This slice exceeds max_full_heatmap_points. Choose another plane "
                "or increase the full-resolution point limit in Preferences."
            )
        depth = float(depths[index]) if depths.size else None
        column = dict(zip(self.names, "abc", strict=True))
        rows = []
        if depth is not None:
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro&immutable=1", uri=True)) as conn:
                conn.set_progress_handler(lambda: _interrupted(check), 1000)
                cursor = conn.execute(
                    f"SELECT {column[x]}, {column[y]}, value, dtype FROM samples "
                    f"WHERE {column[depth_name]}=?", (depth,),
                )
                while batch := cursor.fetchmany(4096):
                    check()
                    rows.extend(batch)
                    if len(rows) > self.max_points:
                        raise ValueError("This slice's acquired sample count exceeds max_full_heatmap_points.")
        counts = np.zeros((ys.size, xs.size), dtype=np.int64)
        values = np.full(counts.shape, np.nan)
        if rows:
            x_values = np.fromiter((row[0] for row in rows), float)
            y_values = np.fromiter((row[1] for row in rows), float)
            parts = []
            # Combine runs of one dtype to avoid an array object per sample.
            for start in range(0, len(rows), 4096):
                check()
                batch = rows[start:start + 4096]
                if len({row[3] for row in batch}) == 1:
                    parts.append(np.frombuffer(b"".join(row[2] for row in batch), dtype=batch[0][3]))
                else:
                    parts.append(np.asarray([np.frombuffer(row[2], dtype=row[3])[0].item()
                                             for row in batch], dtype=object))
            samples = concatenate_record_samples(parts)
            finite = numeric_isfinite(samples)
            np.add.at(counts, (np.searchsorted(ys, y_values[finite]),
                               np.searchsorted(xs, x_values[finite])), 1)
            if np.any(finite):
                if np.iscomplexobj(samples):
                    real = data2matrix(y_values[finite], x_values[finite], samples[finite].real, check_cancelled=check)
                    imaginary = data2matrix(y_values[finite], x_values[finite], samples[finite].imag, check_cancelled=check)
                    matrix = real + 1j * imaginary
                else:
                    matrix = data2matrix(y_values[finite], x_values[finite], samples[finite], check_cancelled=check)
                values = matrix.reindex(index=ys, columns=xs).to_numpy()
        check()
        frame = SliceFrame(xs, ys, values, counts, depth, index, len(rows))
        # Cache at most three planes and 64 MiB of approximate array storage.
        with self._cache_lock:
            self._frames[key] = frame
            while len(self._frames) > 3 or sum(
                f.values.size * (40 if f.values.dtype.kind == "O" else f.values.dtype.itemsize)
                + f.counts.nbytes for f in self._frames.values()
            ) > 64 * 1024 * 1024:
                self._frames.popitem(last=False)
        return frame


def _interrupted(check):
    try:
        check()
        return 0
    except InterruptedError:
        return 1
