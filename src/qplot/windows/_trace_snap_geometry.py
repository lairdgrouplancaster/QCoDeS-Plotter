"""Cached full-resolution geometry for local, screen-space trace capture."""

from typing import Any

import numpy as np
from PyQt6 import QtGui

_BLOCK_SIZE = 256
_BLOCK_OFFSETS = np.arange(_BLOCK_SIZE)


class TraceSnapGeometry:
    """Index acquisition-order blocks, including segments crossing block edges.

    Bounds live in mapped data coordinates, so pan, zoom, resizing and axis
    reassignment do not rebuild the index. Only nearby blocks are mapped to
    screen pixels. Original indices and physical values remain aligned.
    """

    def __init__(self, x, y, raw_x, raw_y, connect="all"):
        arrays = [np.asarray(values, dtype=float) for values in (x, y, raw_x, raw_y)]
        count = min(values.size for values in arrays)
        x, y, self.raw_x, self.raw_y = [values[:count] for values in arrays]
        finite = np.isfinite(x) & np.isfinite(y) & np.isfinite(self.raw_x) & np.isfinite(self.raw_y)
        self.indices = np.flatnonzero(finite)
        self.x, self.y = x[finite], y[finite]
        adjacent = np.diff(self.indices) == 1
        if isinstance(connect, np.ndarray):
            self.connected = np.zeros(adjacent.size, dtype=bool)
            valid = self.indices[:-1] < connect.size
            self.connected[valid] = adjacent[valid] & connect[self.indices[:-1][valid]].astype(bool)
        elif connect == "all":
            # PyQtGraph deliberately joins finite samples in this mode.
            self.connected = np.ones(adjacent.size, dtype=bool)
        elif connect == "pairs":
            self.connected = adjacent & (self.indices[:-1] % 2 == 0)
        else:
            # 'auto' and 'finite' leave a gap at non-finite samples.
            self.connected = adjacent
        starts = np.arange(0, self.x.size, _BLOCK_SIZE)
        ends = np.minimum(starts + _BLOCK_SIZE, self.x.size - 1)
        if starts.size:
            self.x_min = np.minimum(np.minimum.reduceat(self.x, starts), self.x[ends])
            self.x_max = np.maximum(np.maximum.reduceat(self.x, starts), self.x[ends])
            self.y_min = np.minimum(np.minimum.reduceat(self.y, starts), self.y[ends])
            self.y_max = np.maximum(np.maximum.reduceat(self.y, starts), self.y[ends])
        else:
            self.x_min = self.x_max = self.y_min = self.y_max = np.array([])

    def nearest(self, scene_pos: Any, viewbox: Any, radius: float, *, segments: bool):
        """Return a measured sample's original index and capture distance squared.

        A segment captures its closer endpoint, while the capture distance is
        measured to the segment itself. No interpolated measurement is reported.
        """
        transform = viewbox.childTransform() * viewbox.sceneTransform()
        origin = viewbox.mapSceneToView(scene_pos)
        scene_origin = viewbox.mapViewToScene(origin)
        linear = QtGui.QTransform(
            transform.m11(), transform.m12(), transform.m21(), transform.m22(), 0, 0,
        )
        inverse, invertible = linear.inverted()
        if invertible:
            dx = radius * (abs(inverse.m11()) + abs(inverse.m21()))
            dy = radius * (abs(inverse.m12()) + abs(inverse.m22()))
            blocks = np.flatnonzero(
                (self.x_max >= np.nextafter(origin.x() - dx, -np.inf))
                & (self.x_min <= np.nextafter(origin.x() + dx, np.inf))
                & (self.y_max >= np.nextafter(origin.y() - dy, -np.inf))
                & (self.y_min <= np.nextafter(origin.y() + dy, np.inf))
            )
        else:
            blocks = np.arange(self.x_min.size)
        candidates = (blocks[:, None] * _BLOCK_SIZE + _BLOCK_OFFSETS).ravel()
        candidates = candidates[candidates < self.x.size]
        if not candidates.size:
            return None

        def screen_offsets(indices):
            # Work near the pointer, preserving narrow sweeps far from zero.
            x = self.x[indices] - origin.x()
            y = self.y[indices] - origin.y()
            return (
                scene_origin.x() - scene_pos.x() + x * transform.m11() + y * transform.m21(),
                scene_origin.y() - scene_pos.y() + x * transform.m12() + y * transform.m22(),
            )

        with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
            px, py = screen_offsets(candidates)
            distances = np.hypot(px, py)
            distances[~np.isfinite(distances)] = np.inf
            best = int(np.argmin(distances))
            index, distance = int(candidates[best]), float(distances[best])
            if segments and self.connected.size:
                starts = candidates[candidates < self.connected.size]
                starts = starts[self.connected[starts]]
                if starts.size:
                    ax, ay = screen_offsets(starts)
                    bx, by = screen_offsets(starts + 1)
                    vx, vy = bx - ax, by - ay
                    length = np.hypot(vx, vy)
                    ux = np.divide(vx, length, out=np.zeros_like(vx), where=length > 0)
                    uy = np.divide(vy, length, out=np.zeros_like(vy), where=length > 0)
                    along = np.clip(-(ax * ux + ay * uy), 0, length)
                    segment_distance = np.hypot(ax + along * ux, ay + along * uy)
                    segment_distance[~np.isfinite(segment_distance)] = np.inf
                    best_segment = int(np.argmin(segment_distance))
                    if segment_distance[best_segment] < distance:
                        distance = float(segment_distance[best_segment])
                        index = int(starts[best_segment])
                        if along[best_segment] > length[best_segment] / 2:
                            index += 1
        if not distance <= radius:
            return None
        return int(self.indices[index]), distance * distance
