import math
import threading
from contextlib import ExitStack, closing
from dataclasses import dataclass
from fractions import Fraction
from typing import TYPE_CHECKING, Any

import numpy as np
from PyQt6 import QtCore

from qplot.datahandling import load_param_data_from_db, load_param_data_from_db_prep
from qplot.datahandling._numeric_arrays import NumericArrayReader
from qplot.datahandling._sqlite_identifiers import (
    quote_identifier_text as _sqlite_identifier,
)
from qplot.datahandling.complex_data import (
    ComplexSamples,
)
from qplot.datahandling.dimensions import ensure_supported_plot_dimensions
from qplot.datahandling.file_identity import (
    canonical_database_path,
    database_file_identity,
    database_sidecar_identities,
)
from qplot.datahandling.numeric_samples import (
    concatenate_record_samples,
)
from qplot.datahandling.plot_progress import PlotProgress
from qplot.datahandling.qcodes_cache import (
    cache_database_path,
    cache_dataset_completed,
    cache_is_live,
    cache_rundescriber,
    cache_table_name,
    snapshot_cache_parameter_state,
)
from qplot.datahandling.readonly import (
    DatabaseInstanceChangedError,
    qcodes_read_only_connection,
    sqlite_read_only_connection,
)
from qplot.datahandling.trusted_live import TrustedLiveSourceChangedError
from qplot.diagnostics import log_exception
from qplot.tools.operation_registry import OperationExecutionError
from qplot.tools.plot_processing import (
    CANCELLATION_CHUNK_SIZE as CANCELLATION_CHUNK_SIZE,
)
from qplot.tools.plot_processing import (
    MAX_FULL_HEATMAP_POINTS as MAX_FULL_HEATMAP_POINTS,
)
from qplot.tools.plot_processing import (
    MAX_SQL_HEATMAP_GRID_CELLS as MAX_SQL_HEATMAP_GRID_CELLS,
)
from qplot.tools.plot_processing import (
    MAX_SQL_HEATMAP_GRID_SIDE as MAX_SQL_HEATMAP_GRID_SIDE,
)
from qplot.tools.plot_processing import (
    MAX_TRUSTED_FULL_PLOT_DECODE_BYTES as MAX_TRUSTED_FULL_PLOT_DECODE_BYTES,
)
from qplot.tools.plot_processing import (
    PlotProcessingInput,
    PlotProcessingSpec,
    PlotProcessor,
)
from qplot.tools.plot_processing import (
    PlotWorkCancelled as PlotWorkCancelled,
)
from qplot.tools.plot_result import PlotLoadResult, capture_plot_result

from .finite_means import FiniteBinMeans
from .heatmap_geometry import (
    spatial_axis_bins,
)

if TYPE_CHECKING:
    import qcodes

MAX_SQL_HEATMAP_SOURCE_ROWS = 250_000
MAX_ARRAY_HEATMAP_AXIS_VALUES = 262_144


@dataclass(frozen=True)
class _HeatmapSpatialSummary:
    """Finite matching rows and the exact coordinate extent on each axis."""

    matching_rows: int
    x_min: int | float | None
    x_max: int | float | None
    x_count: int
    y_min: int | float | None
    y_max: int | float | None
    y_count: int


class loader(QtCore.QRunnable):
    """
    A Worker to be placed inside a QThreadPool.
    Own source acquisition, cancellation and Qt publication. Numerical work
    is delegated to the processor using captured arrays and metadata.
    
    """
    _trusted_wait_timer: QtCore.QTimer | None

    def __init__(self,
                 cache : "qcodes.dataset.data_set_cache.DataSetCacheWithDBBackend",
                 param : "qcodes.dataset.descriptions.param_spec.ParamSpec", 
                 param_dict : dict,
                 axes : dict,
                 read_data : bool = True,
                 operations: list | None = None,
                 force_sql_heatmap: bool = False,
                 max_full_heatmap_points: int = MAX_FULL_HEATMAP_POINTS,
                 max_heatmap_grid_cells: int = MAX_SQL_HEATMAP_GRID_CELLS,
                 max_heatmap_grid_side: int | None = MAX_SQL_HEATMAP_GRID_SIDE,
                 heatmap_axis_ranges: dict | None = None,
                 heatmap_full_axis_ranges: dict | None = None,
                 database_identity=None,
                 deadline=None,
                 complex_mode: str = "magnitude",
                 ):
        """
        Sets up worker with required data for run()
        
        Please note that self.__init__ is run in main thread, self.run() is ran
        in the worker thread.

        Parameters
        ----------
        cache : qcodes.dataset.data_set_cache.DataSetCacheWithDBBackend
            The cache for the dataset that is being refreshed.
        param : qcodes.dataset.descriptions.param_spec.ParamSpec
            The parameter being updated.
        param_dict : dict{str: ParamSpec}
            List of all parameter data inside the dataset.
        axes : dict{str: str}
            The selected parameter for the axes.
        read_data : bool
            Whether to read the database for new data or use current data.
            The default is True.
        operations: list
            A list containing functions to perform on the refreshed data
            before returning

        """
        super().__init__()
        self.running = True
        self.result: PlotLoadResult | None = None
        self.emitter = _emitter() # For signals
        self._cancelled = threading.Event()
        self._sql_connection_lock = threading.Lock()
        self._publication_lock = threading.RLock()
        self._sql_connection = None
        
        self.processing = PlotProcessor(
            PlotProcessingSpec(
                param, param_dict, axes, [] if operations is None else operations,
                complex_mode=complex_mode, max_full_heatmap_points=max_full_heatmap_points,
                max_heatmap_grid_cells=max_heatmap_grid_cells, max_heatmap_grid_side=max_heatmap_grid_side,
                heatmap_axis_ranges=heatmap_axis_ranges, heatmap_full_axis_ranges=heatmap_full_axis_ranges,
                chunk_size=CANCELLATION_CHUNK_SIZE, max_decode_bytes=MAX_TRUSTED_FULL_PLOT_DECODE_BYTES,
            ),
            # Retain the cancellation event, without a processor -> worker
            # reference cycle keeping the cache and source owner alive.
            cancelled=self._cancelled.is_set,
        )

        # Required working data
        self.cache = cache
        self.table_name = cache_table_name(cache)
        self.sidecar_identities = database_sidecar_identities(
            cache_database_path(cache)
        )
        self.dataset_completed: bool | None = None
        
        self.read_data = read_data
        self.force_sql_heatmap = force_sql_heatmap
        self.database_identity = database_identity
        self.deadline = deadline
        self.database_replaced = False
        self.loaded_from_sql_heatmap = False
        
    
    # Numerical state belongs to the processor; these accessors retain the
    # existing plot-window and volume-worker interface.
    @property
    def param(self) -> Any:
        return self.processing.param

    @param.setter
    def param(self, value: Any) -> None:
        self.processing.param = value

    @property
    def param_dict(self) -> dict[str, Any]:
        return self.processing.param_dict

    @param_dict.setter
    def param_dict(self, value: dict[str, Any]) -> None:
        self.processing.param_dict = value

    @property
    def axes_dict(self) -> dict[str, str]:
        return self.processing.axes_dict

    @axes_dict.setter
    def axes_dict(self, value: dict[str, str]) -> None:
        self.processing.axes_dict = value

    @property
    def complex_mode(self) -> str:
        return self.processing.complex_mode

    @complex_mode.setter
    def complex_mode(self, value: str) -> None:
        self.processing.complex_mode = value

    @property
    def display_param(self) -> Any:
        return self.processing.display_param

    @display_param.setter
    def display_param(self, value: Any) -> None:
        self.processing.display_param = value

    @property
    def operations(self) -> list:
        return self.processing.operations

    @operations.setter
    def operations(self, value: list) -> None:
        self.processing.operations = value

    @property
    def max_full_heatmap_points(self) -> int:
        return self.processing.max_full_heatmap_points

    @max_full_heatmap_points.setter
    def max_full_heatmap_points(self, value: int) -> None:
        self.processing.max_full_heatmap_points = value

    @property
    def max_heatmap_grid_cells(self) -> int:
        return self.processing.max_heatmap_grid_cells

    @max_heatmap_grid_cells.setter
    def max_heatmap_grid_cells(self, value: int) -> None:
        self.processing.max_heatmap_grid_cells = value

    @property
    def max_heatmap_grid_side(self) -> int:
        return self.processing.max_heatmap_grid_side

    @max_heatmap_grid_side.setter
    def max_heatmap_grid_side(self, value: int) -> None:
        self.processing.max_heatmap_grid_side = value

    @property
    def heatmap_axis_ranges(self) -> dict | None:
        return self.processing.heatmap_axis_ranges

    @heatmap_axis_ranges.setter
    def heatmap_axis_ranges(self, value: dict | None) -> None:
        self.processing.heatmap_axis_ranges = value

    @property
    def heatmap_full_axis_ranges(self) -> dict | None:
        return self.processing.heatmap_full_axis_ranges

    @heatmap_full_axis_ranges.setter
    def heatmap_full_axis_ranges(self, value: dict | None) -> None:
        self.processing.heatmap_full_axis_ranges = value

    @property
    def axis_data(self) -> dict[str, np.ndarray]:
        return self.processing.axis_data

    @axis_data.setter
    def axis_data(self, value: dict[str, np.ndarray]) -> None:
        self.processing.axis_data = value

    @property
    def axis_param(self) -> dict[str, Any]:
        return self.processing.axis_param

    @axis_param.setter
    def axis_param(self, value: dict[str, Any]) -> None:
        self.processing.axis_param = value

    @property
    def dataGrid(self) -> np.ndarray:
        return self.processing.dataGrid

    @dataGrid.setter
    def dataGrid(self, value: np.ndarray) -> None:
        self.processing.dataGrid = value

    @property
    def complex_samples(self) -> ComplexSamples | None:
        return self.processing.complex_samples

    @complex_samples.setter
    def complex_samples(self, value: ComplexSamples | None) -> None:
        self.processing.complex_samples = value

    @property
    def sampled_heatmap_source(self) -> bool:
        return self.processing.sampled_heatmap_source

    @sampled_heatmap_source.setter
    def sampled_heatmap_source(self, value: bool) -> None:
        self.processing.sampled_heatmap_source = value

    @property
    def aggregated_heatmap_source(self) -> bool:
        return self.processing.aggregated_heatmap_source

    @aggregated_heatmap_source.setter
    def aggregated_heatmap_source(self, value: bool) -> None:
        self.processing.aggregated_heatmap_source = value

    @property
    def loaded_point_count(self) -> int | None:
        return self.processing.loaded_point_count

    @loaded_point_count.setter
    def loaded_point_count(self, value: int | None) -> None:
        self.processing.loaded_point_count = value

    @property
    def heatmap_downsample_info(self) -> dict[str, Any] | None:
        return self.processing.heatmap_downsample_info

    @heatmap_downsample_info.setter
    def heatmap_downsample_info(self, value: dict[str, Any] | None) -> None:
        self.processing.heatmap_downsample_info = value

    @property
    def heatmap_full_resolution(self) -> bool:
        return self.processing.heatmap_full_resolution

    @heatmap_full_resolution.setter
    def heatmap_full_resolution(self, value: bool) -> None:
        self.processing.heatmap_full_resolution = value

    @property
    def heatmap_source_grid_shape(self) -> tuple[int, int] | None:
        return self.processing.heatmap_source_grid_shape

    @heatmap_source_grid_shape.setter
    def heatmap_source_grid_shape(self, value: tuple[int, int] | None) -> None:
        self.processing.heatmap_source_grid_shape = value

    @property
    def heatmap_source_axis_ranges(self) -> dict | None:
        return self.processing.heatmap_source_axis_ranges

    @heatmap_source_axis_ranges.setter
    def heatmap_source_axis_ranges(self, value: dict | None) -> None:
        self.processing.heatmap_source_axis_ranges = value

    @property
    def _spatial_heatmap_axes(self) -> tuple[np.ndarray, np.ndarray]:
        return self.processing._spatial_heatmap_axes

    @_spatial_heatmap_axes.setter
    def _spatial_heatmap_axes(self, value: tuple[np.ndarray, np.ndarray]) -> None:
        self.processing._spatial_heatmap_axes = value

    @property
    def _spatial_heatmap_indices(self) -> tuple[np.ndarray, np.ndarray]:
        return self.processing._spatial_heatmap_indices

    @_spatial_heatmap_indices.setter
    def _spatial_heatmap_indices(self, value: tuple[np.ndarray, np.ndarray]) -> None:
        self.processing._spatial_heatmap_indices = value

    @property
    def _spatial_heatmap_source_unique_counts(self) -> tuple[int, int]:
        return self.processing._spatial_heatmap_source_unique_counts

    @_spatial_heatmap_source_unique_counts.setter
    def _spatial_heatmap_source_unique_counts(self, value: tuple[int, int]) -> None:
        self.processing._spatial_heatmap_source_unique_counts = value

    @property
    def _heatmap_aggregated_source_rows(self) -> int | None:
        return self.processing._heatmap_aggregated_source_rows

    @_heatmap_aggregated_source_rows.setter
    def _heatmap_aggregated_source_rows(self, value: int | None) -> None:
        self.processing._heatmap_aggregated_source_rows = value

    @property
    def _heatmap_source_info(self) -> dict[str, Any]:
        return self.processing._heatmap_source_info

    @_heatmap_source_info.setter
    def _heatmap_source_info(self, value: dict[str, Any]) -> None:
        self.processing._heatmap_source_info = value

    @property
    def _heatmap_grid_info(self) -> dict[str, Any]:
        return self.processing._heatmap_grid_info

    @_heatmap_grid_info.setter
    def _heatmap_grid_info(self, value: dict[str, Any]) -> None:
        self.processing._heatmap_grid_info = value

    @property
    def _array_heatmap_cardinality_exact(self) -> tuple[bool, bool]:
        return self.processing._array_heatmap_cardinality_exact

    @_array_heatmap_cardinality_exact.setter
    def _array_heatmap_cardinality_exact(self, value: tuple[bool, bool]) -> None:
        self.processing._array_heatmap_cardinality_exact = value

    @property
    def _complex_parts(self) -> list[tuple[np.ndarray, np.ndarray, np.ndarray]]:
        return self.processing._complex_parts

    @_complex_parts.setter
    def _complex_parts(self, value: list[tuple[np.ndarray, np.ndarray, np.ndarray]]) -> None:
        self.processing._complex_parts = value

    @property
    def _complex_parts_bytes(self) -> int:
        return self.processing._complex_parts_bytes

    @_complex_parts_bytes.setter
    def _complex_parts_bytes(self, value: int) -> None:
        self.processing._complex_parts_bytes = value


    def _ensure_cancel_state(self) -> None:
        """Initialise cancellation state for legacy/tests using ``__new__``."""

        if not hasattr(self, "_cancelled"):
            self._cancelled = threading.Event()
        if not hasattr(self, "_sql_connection_lock"):
            self._sql_connection_lock = threading.Lock()
            self._sql_connection = None
        if not hasattr(self, "_publication_lock"):
            self._publication_lock = threading.RLock()


    def cancel(self) -> None:
        """Request cooperative cancellation and interrupt an active SQL read."""

        self._ensure_cancel_state()
        with self._publication_lock:
            self._cancelled.set()
        request = getattr(self, "_trusted_request", None)
        if request is not None:
            request.cancel()
        with self._sql_connection_lock:
            connection = self._sql_connection
        if connection is not None:
            try:
                connection.interrupt()
            except Exception:
                # The worker may be closing the connection at the same time.
                pass


    def is_cancelled(self) -> bool:
        self._ensure_cancel_state()
        return self._cancelled.is_set()


    def _check_cancelled(self) -> None:
        if self.is_cancelled():
            raise PlotWorkCancelled("Plot load cancelled.")


    def _read_only_open_kwargs(self) -> dict[str, Any]:
        """Return identity and abort controls for snapshot preparation."""
        self._require_expected_source_current()
        kwargs: dict[str, Any] = {
            "cancelled_callback": self.is_cancelled,
        }
        database_identity = getattr(self, "database_identity", None)
        if database_identity is not None:
            kwargs["expected_database_identity"] = database_identity
        deadline = getattr(self, "deadline", None)
        if deadline is not None:
            kwargs["deadline"] = deadline
        return kwargs


    def _require_expected_source_current(self) -> None:
        """Validate the exact retained-plot main and sidecar identities."""

        expected_sidecars = getattr(self, "expected_sidecar_identities", None)
        if expected_sidecars is None:
            return
        database_path = getattr(self, "expected_database_path", None)
        if database_path is None:
            database_path = cache_database_path(self.cache)
        expected_resolved_path = getattr(
            self,
            "expected_resolved_database_path",
            None,
        )
        if (
                expected_resolved_path is not None
                and canonical_database_path(database_path)
                != expected_resolved_path
                ):
            raise DatabaseInstanceChangedError(
                "The retained plot database path resolved to another source."
            )
        expected_identity = getattr(self, "database_identity", None)
        if (
                expected_identity is not None
                and database_file_identity(database_path) != expected_identity
                ):
            raise DatabaseInstanceChangedError(
                "The retained plot database was replaced."
            )
        if database_sidecar_identities(
                database_path,
                expected_resolved_path,
                ) != frozenset(expected_sidecars):
            raise DatabaseInstanceChangedError(
                "A retained plot SQLite sidecar was replaced."
            )


    def _set_sql_connection(self, connection) -> None:
        self._ensure_cancel_state()
        with self._sql_connection_lock:
            self._sql_connection = connection
        if connection is not None and self._cancelled.is_set():
            try:
                connection.interrupt()
            except Exception:
                # The connection may have closed concurrently; the worker cancellation
                # flag remains set.
                pass


    def _close_sql_connection(self, connection) -> None:
        try:
            connection.close()
        finally:
            self._set_sql_connection(None)
        self._require_expected_source_current()


    def _emit_finished(self, finished: bool, result: PlotLoadResult | None = None) -> None:
        """Emit completion unless Qt already deleted the receiver at shutdown."""

        self._ensure_cancel_state()
        with self._publication_lock:
            if finished and self._cancelled.is_set():
                finished = False
                self.running = False
            self.result = result if finished else None
            try:
                self.emitter.finished.emit(finished)
            except RuntimeError as err:
                message = str(err)
                if not (
                        "wrapped C/C++ object" in message
                        and "has been deleted" in message
                        ):
                    raise


    def _finish_cancelled(self) -> None:
        self.running = False
        self._emit_finished(False)


    def start(self, pool):
        """Wait for broker data without occupying a plot-processing thread.

        Called on the GUI thread. Submission and done checks are nonblocking;
        all capture, decoding and transform work remains off the GUI thread.
        """
        self.result = None
        from qplot.datahandling.trusted_plot import TrustedPlotDataset
        dataset = getattr(self.cache, "_dataset", None)
        if not isinstance(dataset, TrustedPlotDataset) or not self.read_data:
            pool.start(self)
            return
        try:
            self._trusted_request = dataset.service.submit_plot_prefix(
                dataset, deadline=self.deadline, heatmap_plan=self._trusted_heatmap_plan(),
            )
        except Exception as error:
            self._trusted_submission_error = error
            pool.start(self)
            return
        timer = QtCore.QTimer(self.emitter)
        self._trusted_wait_timer = timer

        def ready():
            if not self.is_cancelled() and not self._trusted_request.done:
                return
            timer.stop()
            timer.deleteLater()
            self._trusted_wait_timer = None
            if self.is_cancelled():
                self._trusted_request.cancel()
                self._trusted_request = None
                self._finish_cancelled()
            else:
                pool.start(self)

        timer.timeout.connect(ready)
        timer.start(10)

    @property
    def progress(self):
        request = getattr(self, "_trusted_request", None)
        if request is not None and not request.done:
            return request.progress or PlotProgress("Waiting to read plot data")
        if request is not None:
            return PlotProgress("Waiting to process plot data", stage=2)
        return getattr(self, "_progress", PlotProgress("Waiting to load plot data"))

    def _prepare_trusted_prefix(self):
        from qplot.datahandling.trusted_plot import TrustedPlotDataset
        dataset = getattr(self.cache, "_dataset", None)
        if not isinstance(dataset, TrustedPlotDataset) or not self.read_data:
            return
        if (error := getattr(self, "_trusted_submission_error", None)) is not None:
            raise error
        request = getattr(self, "_trusted_request", None)
        if request is None:
            # Direct worker users may run synchronously outside the GUI.
            request = dataset.service.submit_plot_prefix(
                dataset, deadline=self.deadline, heatmap_plan=self._trusted_heatmap_plan())
            self._trusted_request = request
        try:
            if self.is_cancelled():
                request.cancel()
            self._trusted_prefix = request.wait()
            self._progress = PlotProgress("Processing plot data", stage=2)
            self.dataset_length_at_start = self._trusted_prefix.row_count
            self._check_cancelled()
        finally:
            self._trusted_request = None

    def _trusted_heatmap_plan(self):
        from qplot.datahandling.trusted_heatmap import NumericHeatmapPlan
        if len(self.param.depends_on_) != 2:
            return None
        names = (self.axes_dict['x'], self.axes_dict['y'], self.param.name)
        if any(self.param_dict[name].type != 'numeric' for name in names):
            return None
        ranges = {}
        # SQL range filtering accepts either axis independently.
        for axis, values in (self.heatmap_axis_ranges or {}).items():
            try:
                low, high = sorted(float(value) for value in values)
            except (TypeError, ValueError):
                continue
            if np.isfinite(low) and np.isfinite(high) and low != high:
                ranges[axis] = (low, high)
        return NumericHeatmapPlan(
            *names, self.max_full_heatmap_points, self.max_heatmap_grid_cells,
            self.max_heatmap_grid_side,
            tuple(tuple(ranges[axis]) if axis in ranges else None for axis in ('x', 'y')),
            bool(self.operations),
        )

    def _load_trusted_numeric_heatmap(self, result):
        self._check_cancelled()
        self.dataset_completed = result.completed
        self.total_point_count_estimate = result.selected_count
        self.axis_data = dict(zip(('x', 'y'), result.axes, strict=True))
        self.axis_param = {axis: self.param_dict[self.axes_dict[axis]] for axis in ('x', 'y')}
        self.heatmap_source_grid_shape = self._heatmap_source_grid_shape_from_metadata()
        self._spatial_heatmap_axes = result.axes
        indices = np.nonzero(np.isfinite(result.grid))
        self._spatial_heatmap_indices = (indices[1], indices[0])
        self._spatial_heatmap_source_unique_counts = result.unique_counts
        self._array_heatmap_cardinality_exact = result.cardinality_exact
        self._heatmap_aggregated_source_rows = result.matching_count
        self.aggregated_heatmap_source = True
        self.sampled_heatmap_source = False
        self._heatmap_source_info = {
            'row_count': result.selected_count, 'estimated_range_rows': result.matching_count,
            'sampled': False, 'aggregated': True, 'sample_limit': None, 'sample_stride': None,
            'strategy': 'spatial mean',
            'axis_ranges': self._normalised_heatmap_axis_ranges(self.heatmap_axis_ranges),
        }
        source_shape = self.heatmap_source_grid_shape or result.unique_counts[::-1]
        _, _, self.dataGrid = self.processing._spatial_aggregation_grid(result.grid[indices], *source_shape)
        full_ranges = self._normalised_heatmap_axis_ranges(self.heatmap_full_axis_ranges)
        if full_ranges is not None:
            self.heatmap_source_axis_ranges = full_ranges
        elif self.heatmap_axis_ranges is None and result.matching_count:
            self.heatmap_source_axis_ranges = dict(zip(('x', 'y'), result.bounds, strict=True))
        self.loaded_from_sql_heatmap = True
        self.loaded_point_count = len(indices[0])
        self.heatmap_downsample_info = self.processing._heatmap_downsample_info()
        self.read_data = False

    def _plot_connection(self, *, decode_arrays=False):
        prefix = getattr(self, "_trusted_prefix", None)
        if prefix is not None:
            self._require_expected_source_current()
            return prefix.connect(decode_arrays=decode_arrays, cancelled=self.is_cancelled)
        from qplot.datahandling.trusted_plot import TrustedPlotDataset
        if isinstance(getattr(self.cache, "_dataset", None), TrustedPlotDataset):
            raise RuntimeError("The trusted plot prefix is unavailable; reopen the plot.")
        factory = qcodes_read_only_connection if decode_arrays else sqlite_read_only_connection
        return factory(cache_database_path(self.cache), **self._read_only_open_kwargs())

    def run(self):
        self.result = None
        try:
            self._run()
        finally:
            request = getattr(self, "_trusted_request", None)
            if request is not None:
                # Validation or source fencing may fail before consuming the
                # asynchronously submitted capture.
                request.cancel()
                self._trusted_request = None
            self._trusted_prefix = None

    def _run(self):
        try:
            self._check_cancelled()
            self._require_expected_source_current()
            ensure_supported_plot_dimensions(
                getattr(self.param, "name", "Measurement"),
                getattr(self.param, "depends_on_", ()),
            )
            if len(getattr(self.param, "depends_on_", ())) > 1:
                self.processing._validate_heatmap_parameter_types()
            elif len(self.param.depends_on_) == 1:
                for name in self.param.depends_on_:
                    if getattr(self.param_dict[name], "type", None) == "complex":
                        self.processing._reject_complex_line(name)
            self._prepare_trusted_prefix()
            self._progress = PlotProgress("Processing plot data", stage=2)
            cache = self.cache
            cache_live = cache_is_live(cache)
            from qplot.datahandling.trusted_heatmap import NumericHeatmap
            trusted_heatmap = isinstance(getattr(self, '_trusted_prefix', None), NumericHeatmap)
            if trusted_heatmap:
                self._load_trusted_numeric_heatmap(self._trusted_prefix)

            # Completion checks and cache preparation can query SQLite, so do
            # them here rather than on the GUI thread. In-memory live caches
            # are already authoritative and should never be read via SQLite.
            if self.read_data:
                if cache_live:
                    self.dataset_completed = cache_dataset_completed(cache)
                    self.read_data = False
                else:
                    completion_conn = self._plot_connection(decode_arrays=True)
                    self._set_sql_connection(completion_conn)
                    try:
                        self._check_cancelled()
                        (
                            parameter_complete,
                            self.dataset_completed,
                        ) = load_param_data_from_db_prep(
                            cache,
                            self.param,
                            connection=completion_conn,
                            )
                    finally:
                        self._close_sql_connection(completion_conn)
                    self._check_cancelled()
                    self.read_data = not parameter_complete

            self._check_cancelled()
            use_sql_heatmap = (
                self.read_data
                and not cache_live
                and self._should_use_sql_heatmap()
                )
            if use_sql_heatmap:
                self._load_large_heatmap_from_sql()

            elif not trusted_heatmap:
                if self.read_data:
                    prefix = getattr(self, "_trusted_prefix", None)
                    if prefix is not None:
                        shape = self._parameter_shape()
                        planned_bytes = (math.prod(shape) * len(self.param_dict) * 128
                                         if shape else 0)
                        if max(prefix.decode_bytes, planned_bytes) > MAX_TRUSTED_FULL_PLOT_DECODE_BYTES:
                            raise ValueError("Full-resolution plot data exceeds the 1 GiB decoding budget.")
                    write_status, read_status, existing_data = (
                        snapshot_cache_parameter_state(cache, self.param.name)
                        )
                    conn = self._plot_connection(decode_arrays=True)
                    self._set_sql_connection(conn)
                    try:
                        self._check_cancelled()
                        (
                            self.updated_read_status,
                            self.updated_write_status,
                            self.cache_data
                        ) = load_param_data_from_db(
                            conn,
                            self.table_name,
                            cache_rundescriber(cache),
                            self.param.name,
                            write_status,
                            read_status,
                            existing_data,
                            check_cancelled=self._check_cancelled,
                            max_decode_bytes=MAX_TRUSTED_FULL_PLOT_DECODE_BYTES,
                        )
                    finally:
                        self._close_sql_connection(conn)

                    self._check_cancelled()
                    data = self.cache_data[self.param.name]
                    write_status = self.updated_write_status

                else:
                    # Capture the acquired extent with its arrays, including
                    # cache-only processing after a run's final publication.
                    write_status, _, cached_data = snapshot_cache_parameter_state(
                        cache, self.param.name,
                    )
                    data = cached_data[self.param.name]

                self._check_cancelled()
                self.processing.prepare(PlotProcessingInput(
                    data, write_status, self._parameter_shape(),
                ))

        except PlotWorkCancelled:
            self._finish_cancelled()
            return
        except (DatabaseInstanceChangedError, TrustedLiveSourceChangedError) as err:
            self.database_replaced = True
            if self.is_cancelled():
                self._finish_cancelled()
                return
            self.emitter.errorOccurred.emit(err)
            self._emit_finished(False)
            return
        except Exception as err: # Raise error in main thread
            if self.is_cancelled():
                self._finish_cancelled()
                return
            log_exception("Plot worker failed", err, __name__)
            self.emitter.errorOccurred.emit(err)
            self._emit_finished(False) # False: Failed
            return

        try:
            if self.operations:
                self._progress = PlotProgress("Applying transforms", stage=2)
            processed = self.processing.finish()
            completed_result = capture_plot_result(self, processed=processed)

        except PlotWorkCancelled:
            self._finish_cancelled()
            return
        except DatabaseInstanceChangedError as err:
            self.database_replaced = True
            if self.is_cancelled():
                self._finish_cancelled()
                return
            self.emitter.errorOccurred.emit(err)
            self._emit_finished(False)
            return
        except Exception as err:
            if self.is_cancelled():
                self._finish_cancelled()
                return
            log_exception("Plot processing failed", err, __name__)
            self.emitter.errorOccurred.emit(err)
            self._emit_finished(False)
            return

        try:
            self._require_expected_source_current()
        except DatabaseInstanceChangedError as err:
            self.database_replaced = True
            if self.is_cancelled():
                self._finish_cancelled()
                return
            self.emitter.errorOccurred.emit(err)
            self._emit_finished(False)
            return

        # Callback
        self._progress = PlotProgress("Preparing display", stage=2)
        self._emit_finished(True, completed_result)


    def _should_use_sql_heatmap(self):
        if len(getattr(self.param, "depends_on_", ())) <= 1:
            return False

        if self.force_sql_heatmap:
            if getattr(self, "operations", None):
                raise OperationExecutionError(
                    "Operations cannot be applied to a heatmap detail reload."
                    )
            return True

        setpoint_count = self._large_heatmap_point_count()
        if setpoint_count is None:
            return False

        requires_bounded_load = self.processing._requires_bounded_heatmap(setpoint_count)
        return requires_bounded_load or getattr(
            self, "_variable_multidimensional_array_records", False,
        )


    def _large_heatmap_point_count(self):
        self._variable_multidimensional_array_records = False
        source_grid_shape = self._heatmap_source_grid_shape_from_metadata()
        planned_count = 0
        if source_grid_shape is not None:
            source_grid_rows, source_grid_columns = source_grid_shape
            planned_count = int(source_grid_rows * source_grid_columns)
            self.heatmap_source_grid_shape = source_grid_shape
            # Small planned shapes still need array-header inspection: the
            # stored record shapes need not be identical to the scan shape.
            if planned_count > self.max_full_heatmap_points or not self._has_array_heatmap_columns():
                self.total_point_count_estimate = planned_count
                return planned_count

        conn = self._plot_connection()
        self._set_sql_connection(conn)
        try:
            self._check_cancelled()
            if self._has_array_heatmap_columns():
                # A storage row can contain millions of samples. Inspect
                # bounded NPY headers, stopping as soon as the limit is crossed.
                setpoint_count = 0
                first_shapes = None
                with closing(self._array_heatmap_records(conn)) as records:
                    for values, size in records:
                        shapes = tuple(value.shape if isinstance(value, NumericArrayReader)
                                       else () for value in values)
                        if first_shapes is None:
                            first_shapes = shapes
                        elif any(shape != first and max(len(shape), len(first)) > 1
                                 for first, shape in zip(first_shapes, shapes, strict=True)):
                            # QCoDeS' np.array(records, dtype=object) retry can
                            # also broadcast/fail for ragged multidimensional
                            # records, before our normaliser ever receives data.
                            self._variable_multidimensional_array_records = True
                        setpoint_count += size
                        if setpoint_count > self.max_full_heatmap_points:
                            break
                setpoint_count = max(planned_count, setpoint_count)
            else:
                setpoint_count = self._selected_parameter_row_count(conn)
        finally:
            self._close_sql_connection(conn)

        if setpoint_count is not None:
            self.total_point_count_estimate = setpoint_count
        return setpoint_count


    def _heatmap_source_grid_shape_from_metadata(self):
        self.processing.planned_shape = self._parameter_shape()
        return self.processing._heatmap_source_grid_shape_from_metadata()


    def _parameter_shape(self):
        shapes = getattr(cache_rundescriber(self.cache), "shapes", None)
        if not isinstance(shapes, dict):
            return None

        return shapes.get(self.param.name)


    def _shape_size(self, shape):
        if shape is None:
            return None

        try:
            dimensions = [int(dimension) for dimension in shape]
        except (TypeError, ValueError):
            return None

        if not dimensions or any(dimension <= 0 for dimension in dimensions):
            return None

        return math.prod(dimensions)


    def _load_large_heatmap_from_sql(self):
        # SQLite aggregates treat QCoDeS complex BLOBs as numbers, so reject
        # every declared complex column before the bounded SQL path can
        # compute a misleading grid.
        self.processing._validate_heatmap_parameter_types()
        conn = self._plot_connection()
        self._set_sql_connection(conn)
        full_arrays = None
        try:
            self._check_cancelled()
            rowid_min, rowid_max = self._rowid_span(conn)
            self._check_cancelled()
            self.heatmap_source_grid_shape = (
                self._heatmap_source_grid_shape_from_metadata()
                )
            if (
                getattr(self, "_variable_multidimensional_array_records", False)
                and self.total_point_count_estimate <= self.max_full_heatmap_points
            ):
                full_arrays = self._read_full_array_heatmap_arrays(conn)
            x_data, y_data, z_data = (
                full_arrays if full_arrays is not None
                else self._read_heatmap_arrays(conn, rowid_min, rowid_max)
            )
        finally:
            self._close_sql_connection(conn)

        self._check_cancelled()
        if full_arrays is not None:
            # Keep normal full-resolution pivot/operation semantics for the
            # compatibility decoder, including sparse-grid size checks.
            data = dict(zip(
                (self.axes_dict["x"], self.axes_dict["y"], self.param.name),
                full_arrays, strict=True,
            ))
            axes, _params, data_grid = self.processing.for_unshaped_2d(
                data, np.ones(z_data.size, dtype=bool), z_data,
            )
            x_axis, y_axis = axes["x"], axes["y"]
        else:
            x_axis, y_axis, data_grid = self.processing._heatmap_grid_from_arrays(
                x_data, y_data, z_data,
            )
        self.axis_data = {
            "x": x_axis,
            "y": y_axis,
            }
        self.axis_param = {
            "x": self.param_dict[self.axes_dict["x"]],
            "y": self.param_dict[self.axes_dict["y"]],
            }
        self.dataGrid = data_grid
        self.loaded_from_sql_heatmap = True
        self.loaded_point_count = int(z_data.size)
        self.heatmap_downsample_info = self.processing._heatmap_downsample_info()

        # The direct SQL path deliberately does not populate QCoDeS' full
        # in-memory cache. Keep future refreshes on the database path.
        self.read_data = False


    def _read_full_array_heatmap_arrays(self, conn):
        """Decode compatible small records in order, with bounded BLOB reads."""
        buffered = []
        source_count = 0
        with closing(self._array_heatmap_chunks(conn)) as chunks:
            for size, arrays in chunks:
                self._check_cancelled()
                source_count += size
                # A live acquisition may grow after the header preflight.
                # Switch to bounded rendering rather than grow the buffer;
                # operations instead raise their usual resolution-limit error.
                if self.processing._requires_bounded_heatmap(source_count):
                    return None
                buffered.append(arrays)
        self._check_cancelled()
        self.total_point_count_estimate = source_count
        return tuple(
            concatenate_record_samples([arrays[axis] for arrays in buffered])
            if buffered else np.array([], dtype=float)
            for axis in range(3)
        )


    def _rowid_span(self, conn):
        self._check_cancelled()
        table = _sqlite_identifier(self.table_name)
        row = conn.execute(f"SELECT MIN(rowid), MAX(rowid) FROM {table}").fetchone()
        if row is None or row[0] is None or row[1] is None:
            return None, None

        return int(row[0]), int(row[1])


    def _read_heatmap_arrays(self, conn, rowid_min, rowid_max):
        self._check_cancelled()
        if rowid_min is None or rowid_max is None:
            self._heatmap_source_info = {
                "row_count": 0,
                "estimated_range_rows": None,
                "sampled": False,
                "aggregated": False,
                "sample_limit": MAX_SQL_HEATMAP_SOURCE_ROWS,
                "sample_stride": None,
                "strategy": "empty",
                "axis_ranges": self._normalised_heatmap_axis_ranges(
                    self.heatmap_axis_ranges
                    ),
                }
            return (
                np.array([], dtype=float),
                np.array([], dtype=float),
                np.array([], dtype=float),
                )

        if self._has_array_heatmap_columns():
            return self._read_array_heatmap_arrays(conn)

        table = _sqlite_identifier(self.table_name)
        x_column = _sqlite_identifier(self.axes_dict["x"])
        y_column = _sqlite_identifier(self.axes_dict["y"])
        z_column = _sqlite_identifier(self.param.name)
        # Match _arrays_from_values: an observation contributes only when
        # both coordinates and its signal are finite. Apply this before
        # MIN/MAX, cardinalities and AVG so invalid samples cannot distort
        # bounds or poison a bin containing valid samples. BETWEEN also
        # excludes missing values, including SQLite NULLs.
        max_finite = float(np.finfo(float).max)
        finite_where_sql = " AND ".join(
            f"{column} BETWEEN {-max_finite} AND {max_finite}"
            for column in (x_column, y_column, z_column)
            )
        selected_count = self._selected_parameter_row_count(conn)
        # Retain the acquired row count as a conservative loading bound;
        # summaries and returned arrays count only valid observations.
        row_count = selected_count
        if row_count is None:
            row_count = rowid_max - rowid_min + 1
        columns = f"{x_column}, {y_column}, {z_column}"
        axis_ranges = self._normalised_heatmap_axis_ranges(self.heatmap_axis_ranges)
        self.total_point_count_estimate = row_count
        self._heatmap_source_info = {
            "row_count": int(row_count),
            "estimated_range_rows": int(row_count),
            "sampled": False,
            "aggregated": False,
            "sample_limit": MAX_SQL_HEATMAP_SOURCE_ROWS,
            "sample_stride": None,
            "strategy": "all",
            "axis_ranges": axis_ranges,
            }

        axis_where_sql, parameters = self._heatmap_where_clause()
        if axis_where_sql:
            where_sql = f"{finite_where_sql} AND {axis_where_sql}"
            range_summary = self._heatmap_spatial_summary(
                conn,
                where_sql,
                parameters,
                )
            range_row_count = range_summary.matching_rows
            self._heatmap_estimated_range_rows = range_row_count
            self._heatmap_source_info.update({
                "estimated_range_rows": range_row_count,
                "strategy": "visible range",
                })
            if range_row_count > MAX_SQL_HEATMAP_SOURCE_ROWS:
                return self._spatially_aggregated_heatmap_arrays(
                    conn,
                    where_sql,
                    parameters,
                    range_summary,
                    )

            self.sampled_heatmap_source = False
            self.aggregated_heatmap_source = False
            cursor = conn.execute(
                (
                    f"SELECT {columns} FROM {table} "
                    f"WHERE {where_sql} ORDER BY rowid"
                    ),
                parameters,
                )
            return self._arrays_from_cursor(cursor)

        if row_count <= MAX_SQL_HEATMAP_SOURCE_ROWS:
            self.sampled_heatmap_source = False
            self.aggregated_heatmap_source = False
            cursor = conn.execute(
                (
                    f"SELECT {columns} FROM {table} "
                    f"WHERE {finite_where_sql} ORDER BY rowid"
                    ),
                )
            arrays = self._arrays_from_cursor(cursor)
            self._heatmap_source_info["estimated_range_rows"] = int(arrays[2].size)
            return arrays

        summary = self._heatmap_spatial_summary(conn, finite_where_sql, ())
        return self._spatially_aggregated_heatmap_arrays(
            conn,
            finite_where_sql,
            (),
            summary,
            )


    def _has_array_heatmap_columns(self):
        return any(
            getattr(param, "type", None) in ("array", "complex")
            for param in (
                self.param,
                self.param_dict[self.axes_dict["x"]],
                self.param_dict[self.axes_dict["y"]],
            )
        )


    def _array_heatmap_records(self, conn):
        """Yield one aligned record, without materialising any array BLOB."""
        names = (self.axes_dict["x"], self.axes_dict["y"], self.param.name)
        array_columns = [
            getattr(self.param_dict[name], "type", None) in ("array", "complex")
            for name in names
        ]
        columns = ", ".join(
            "NULL" if is_array else _sqlite_identifier(name)
            for name, is_array in zip(names, array_columns, strict=True)
        )
        table = _sqlite_identifier(self.table_name)
        selected = f"{_sqlite_identifier(self.param.name)} IS NOT NULL"
        last_rowid = 0
        while True:
            self._check_cancelled()
            # Finish each bounded row query before decoding. Array payloads
            # are opened separately and always with readonly=True.
            rows = conn.execute(
                f"SELECT rowid, {columns} FROM {table} "
                f"WHERE rowid > ? AND {selected} ORDER BY rowid LIMIT 128",
                (last_rowid,),
            ).fetchall()
            if not rows:
                return
            for rowid, *values in rows:
                self._check_cancelled()
                with ExitStack() as stack:
                    for index, (name, is_array) in enumerate(zip(names, array_columns, strict=True)):
                        self._check_cancelled()
                        if is_array:
                            blob = stack.enter_context(conn.blobopen(
                                self.table_name, name, rowid, readonly=True,
                            ))
                            values[index] = NumericArrayReader(blob)
                            if values[index].dtype.kind == "c" and name != self.param.name:
                                self.processing._reject_complex_heatmap(
                                    name,
                                    coordinate=name != self.param.name,
                                )
                    sizes = [value.size if isinstance(value, NumericArrayReader) else 1
                             for value in values]
                    # QCoDeS also expands a singleton dependent array (or
                    # numeric scalar) when its setpoints supply more samples.
                    # Scalar storage must not turn all-empty array records
                    # into measurements. Keep singleton payloads unexpanded
                    # until the existing bounded chunk broadcast below.
                    array_sizes = {name: value.size for name, value in zip(names, values, strict=True)
                                   if isinstance(value, NumericArrayReader)}
                    # Scalar outputs expand to the first array in QCoDeS'
                    # output-first tree, independently of displayed axis order.
                    # Empty output records stay empty with singleton setpoints.
                    first_size = next((array_sizes[name] for name in
                                       (self.param.name, *self.param.depends_on_)
                                       if name in array_sizes), 1)
                    size = 0 if first_size == 0 else max(array_sizes.values(), default=1)
                    if any(value_size not in (1, size) for value_size in sizes):
                        raise ValueError("Heatmap setpoint and measurement shapes do not match.")
                    yield values, size
            last_rowid = rows[-1][0]


    def _array_heatmap_chunks(self, conn):
        chunk_size = max(1, min(CANCELLATION_CHUNK_SIZE, MAX_SQL_HEATMAP_SOURCE_ROWS))
        with closing(self._array_heatmap_records(conn)) as records:
            for values, size in records:
                for start in range(0, size, chunk_size):
                    self._check_cancelled()
                    stop = min(size, start + chunk_size)
                    decoded = [
                        value.read(0 if value.size == 1 else start,
                                   1 if value.size == 1 else stop,
                                   self._check_cancelled)
                        if isinstance(value, NumericArrayReader) else value
                        for value in values
                    ]
                    yield stop - start, self.processing._arrays_from_values(*decoded)


    def _array_heatmap_visible_values(self, arrays):
        x_data, y_data, z_data = arrays
        valid = np.ones(z_data.size, dtype=bool)
        for axis, values in (("x", x_data), ("y", y_data)):
            bounds = self.processing._grid_axis_bounds(axis)
            if bounds is not None:
                valid &= (values >= bounds[0]) & (values <= bounds[1])
        return x_data[valid], y_data[valid], z_data[valid]


    def _read_array_heatmap_arrays(self, conn):
        """Keep a bounded sample buffer, or stream two passes into a mean grid."""
        self._complex_parts = []
        self._complex_parts_bytes = 0
        buffered: list[tuple[np.ndarray, np.ndarray, np.ndarray]] | None = []
        source_count = matching_count = 0
        # Bound coordinate bookkeeping independently of display preferences.
        # Once saturated, retain extrema and a cardinality lower bound, then
        # bin on the second pass. The default display budget fits in this set.
        axis_limit = MAX_ARRAY_HEATMAP_AXIS_VALUES
        unique: list[set[float] | None] = [set(), set()]
        precision_sensitive = [False, False]
        lower = [np.inf, np.inf]
        upper = [-np.inf, -np.inf]
        with closing(self._array_heatmap_chunks(conn)) as chunks:
            for size, arrays in chunks:
                self._check_cancelled()
                source_count += size
                arrays = self._array_heatmap_visible_values(arrays)
                matching_count += arrays[2].size
                if buffered is not None:
                    if matching_count <= MAX_SQL_HEATMAP_SOURCE_ROWS:
                        if arrays[2].size:
                            buffered.append(arrays)
                    else:
                        buffered = None
                for axis, values in enumerate(arrays[:2]):
                    if not values.size:
                        continue
                    lower[axis] = min(lower[axis], float(np.min(values)))
                    upper[axis] = max(upper[axis], float(np.max(values)))
                    axis_unique = unique[axis]
                    precision_sensitive[axis] |= self.processing._coordinate_conversion_may_round(values)
                    if axis_unique is not None:
                        axis_unique.update(np.unique(values).tolist())
                        # Check accumulated exact coordinates before the set
                        # is bounded: a collision may span acquisition records.
                        if precision_sensitive[axis]:
                            self.processing._float_heatmap_coordinates(
                                np.array(list(axis_unique), dtype=object),
                                self.axes_dict[("x", "y")[axis]],
                            )
                        if len(axis_unique) > axis_limit:
                            unique[axis] = None
                    elif precision_sensitive[axis]:
                        self.processing._float_heatmap_coordinates(
                            values, self.axes_dict[("x", "y")[axis]],
                        )

        self.total_point_count_estimate = source_count
        self.sampled_heatmap_source = False
        self.aggregated_heatmap_source = buffered is None
        self._heatmap_source_info = {
            "row_count": source_count,
            "estimated_range_rows": matching_count,
            "sampled": False,
            "aggregated": buffered is None,
            "sample_limit": MAX_SQL_HEATMAP_SOURCE_ROWS,
            "sample_stride": None,
            "strategy": "spatial mean" if buffered is None else "all",
            "axis_ranges": self._normalised_heatmap_axis_ranges(self.heatmap_axis_ranges),
        }
        if buffered is not None:
            self._check_cancelled()
            return tuple(
                concatenate_record_samples([arrays[axis] for arrays in buffered])
                if buffered else np.array([], dtype=float)
                for axis in range(3)
            )

        counts = tuple(len(values) if values is not None else axis_limit + 1
                       for values in unique)
        bins = self.processing._bounded_grid_shape(*counts)
        exact = [values is not None and len(values) <= count
                 for values, count in zip(unique, bins, strict=True)]
        axes = [
            np.array(sorted(values), dtype=float) if exact[axis] and values is not None
            else self.processing._scaled_axis_indices(
                np.array([lower[axis], upper[axis]]), bins[axis],
                (lower[axis], upper[axis]),
            )[0]
            for axis, values in enumerate(unique)
        ]
        means = FiniteBinMeans((axes[1].size, axes[0].size), self._check_cancelled)

        def accumulate(*, replay=False):
            self._complex_parts = []
            self._complex_parts_bytes = 0
            # Both passes use the same private snapshot, frozen bin geometry,
            # and visible-value filtering. Close each iterator (including its
            # read-only BLOBs) before starting the optional exact replay.
            with closing(self._array_heatmap_chunks(conn)) as chunks:
                for _size, arrays in chunks:
                    self._check_cancelled()
                    x_data, y_data, z_data = self._array_heatmap_visible_values(arrays)
                    indices = [
                        np.searchsorted(axes[axis], values) if exact[axis]
                        else self.processing._scaled_axis_indices(
                            values, bins[axis], (lower[axis], upper[axis]),
                        )[1]
                        for axis, values in enumerate((x_data, y_data))
                    ]
                    if replay:
                        means.replay((indices[1], indices[0]), z_data)
                    else:
                        means.add((indices[1], indices[0]), z_data)

        accumulate()
        if means.begin_exact_replay():
            accumulate(replay=True)

        self._check_cancelled()
        y_indices, x_indices = np.nonzero(means.counts)
        z_data = means.means()[y_indices, x_indices]
        self._spatial_heatmap_axes = (axes[0], axes[1])
        self._spatial_heatmap_indices = (x_indices, y_indices)
        self._spatial_heatmap_source_unique_counts = (counts[0], counts[1])
        self._array_heatmap_cardinality_exact = (unique[0] is not None, unique[1] is not None)
        self._heatmap_aggregated_source_rows = matching_count
        full_ranges = self._normalised_heatmap_axis_ranges(self.heatmap_full_axis_ranges)
        if full_ranges is not None:
            self.heatmap_source_axis_ranges = full_ranges
        elif self.heatmap_axis_ranges is None:
            self.heatmap_source_axis_ranges = {
                axis: (lower[index], upper[index]) for index, axis in enumerate(("x", "y"))
            }
        return axes[0][x_indices], axes[1][y_indices], z_data


    def _heatmap_spatial_summary(self, conn, where_sql, parameters) -> _HeatmapSpatialSummary:
        self._check_cancelled()
        table = _sqlite_identifier(self.table_name)
        x_column = _sqlite_identifier(self.axes_dict["x"])
        y_column = _sqlite_identifier(self.axes_dict["y"])
        row = conn.execute(
            (
                f"SELECT COUNT(*), MIN({x_column}), MAX({x_column}), "
                f"COUNT(DISTINCT {x_column}), MIN({y_column}), "
                f"MAX({y_column}), COUNT(DISTINCT {y_column}) "
                f"FROM {table} WHERE {where_sql}"
                ),
            parameters,
            ).fetchone()
        if row is None or row[0] is None:
            return _HeatmapSpatialSummary(0, None, None, 0, None, None, 0)

        return _HeatmapSpatialSummary(
            int(row[0]),
            row[1],
            row[2],
            int(row[3] or 0),
            row[4],
            row[5],
            int(row[6] or 0),
            )


    def _spatially_aggregated_heatmap_arrays(
            self,
            conn,
            where_sql,
            parameters,
            summary: _HeatmapSpatialSummary,
            ):
        if (
                summary.matching_rows <= 0
                or summary.x_min is None
                or summary.x_max is None
                or summary.y_min is None
                or summary.y_max is None
                or summary.x_count <= 0
                or summary.y_count <= 0
                ):
            self.sampled_heatmap_source = False
            self.aggregated_heatmap_source = False
            return self.processing._arrays_from_values([], [], [])

        x_bins, y_bins = self.processing._bounded_grid_shape(summary.x_count, summary.y_count)
        table = _sqlite_identifier(self.table_name)
        x_column = _sqlite_identifier(self.axes_dict["x"])
        y_column = _sqlite_identifier(self.axes_dict["y"])
        z_column = _sqlite_identifier(self.param.name)

        x_exact = x_bins >= summary.x_count
        y_exact = y_bins >= summary.y_count
        query_parameters: list[float | int] = []
        if x_exact:
            x_group_sql = x_column
        else:
            x_centres, x_lower_edge, x_width, x_scale = self._spatial_axis_bins(
                float(summary.x_min),
                float(summary.x_max),
                summary.x_count,
                x_bins,
                )
            x_group_sql = f"MIN(CAST(({x_column} / ? - ?) / ? AS INTEGER), ?)"
            query_parameters.extend((x_scale, x_lower_edge, x_width, x_bins - 1))

        if y_exact:
            y_group_sql = y_column
        else:
            y_centres, y_lower_edge, y_width, y_scale = self._spatial_axis_bins(
                float(summary.y_min),
                float(summary.y_max),
                summary.y_count,
                y_bins,
                )
            y_group_sql = f"MIN(CAST(({y_column} / ? - ?) / ? AS INTEGER), ?)"
            query_parameters.extend((y_scale, y_lower_edge, y_width, y_bins - 1))

        if (
                (not x_exact and x_centres.size == 0)
                or (not y_exact and y_centres.size == 0)
                ):
            self.sampled_heatmap_source = False
            self.aggregated_heatmap_source = False
            return self.processing._arrays_from_values([], [], [])

        grouped_source_sql = (
            f"SELECT {x_group_sql} AS x_group, {y_group_sql} AS y_group, "
            f"{z_column} AS z_value FROM {table} WHERE {where_sql}"
        )
        cursor = conn.execute(
            (
                "SELECT x_group, y_group, AVG(z_value), COUNT(*), "
                "MIN(z_value), MAX(z_value) FROM ("
                + grouped_source_sql +
                ") GROUP BY x_group, y_group ORDER BY y_group, x_group"
                ),
            (*query_parameters, *parameters),
            )
        x_groups = []
        y_groups = []
        z_values: list[float | None] = []
        failed_means = {}
        aggregated_source_rows = 0
        for row_number, (x_group, y_group, z_value, bin_rows, z_min, z_max) in enumerate(cursor):
            if row_number % 1024 == 0:
                self._check_cancelled()
            largest = max(abs(float(z_min)), abs(float(z_max)))
            safe_sample = (np.finfo(float).max / 2) / int(bin_rows)
            if (z_value is None or not math.isfinite(z_value) or largest > safe_sample
                    or z_min < 0 < z_max):
                failed_means[(x_group, y_group)] = (len(z_values), int(bin_rows))
            x_groups.append(x_group)
            y_groups.append(y_group)
            z_values.append(z_value)
            aggregated_source_rows += int(bin_rows)

        if failed_means:
            # SQLite AVG can lose a cancellation residual before a risky sum
            # overflows. Replay all risky groups from the original values in
            # one bounded pass, even when AVG happened to return finite data.
            totals = dict.fromkeys(failed_means, Fraction())
            cursor = conn.execute(grouped_source_sql, (*query_parameters, *parameters))
            for row_number, (x_group, y_group, value) in enumerate(cursor):
                if row_number % 1024 == 0:
                    self._check_cancelled()
                key = (x_group, y_group)
                if key in totals:
                    totals[key] += Fraction(value)
            for index, (key, total) in enumerate(totals.items()):
                if index % 1024 == 0:
                    self._check_cancelled()
                position, count = failed_means[key]
                z_values[position] = float(total / count)

        x_group_data = np.asarray(x_groups, dtype=float)
        y_group_data = np.asarray(y_groups, dtype=float)
        z_data = np.asarray(z_values, dtype=float)
        finite = (
            np.isfinite(x_group_data)
            & np.isfinite(y_group_data)
            & np.isfinite(z_data)
            )

        if x_exact:
            x_centres = np.unique(x_group_data[np.isfinite(x_group_data)])
            x_index_data = np.searchsorted(x_centres, x_group_data[finite])
        else:
            x_index_data = x_group_data[finite].astype(np.int64)
        if y_exact:
            y_centres = np.unique(y_group_data[np.isfinite(y_group_data)])
            y_index_data = np.searchsorted(y_centres, y_group_data[finite])
        else:
            y_index_data = y_group_data[finite].astype(np.int64)

        z_data = z_data[finite]
        self._spatial_heatmap_axes = (x_centres, y_centres)
        self._spatial_heatmap_indices = (x_index_data, y_index_data)
        self._spatial_heatmap_source_unique_counts = (summary.x_count, summary.y_count)
        self._heatmap_aggregated_source_rows = aggregated_source_rows
        full_axis_ranges = self._normalised_heatmap_axis_ranges(
            self.heatmap_full_axis_ranges
            )
        if full_axis_ranges is not None:
            self.heatmap_source_axis_ranges = full_axis_ranges
        elif self.heatmap_axis_ranges is None:
            self.heatmap_source_axis_ranges = {
                "x": (float(summary.x_min), float(summary.x_max)),
                "y": (float(summary.y_min), float(summary.y_max)),
                }
        self.sampled_heatmap_source = False
        self.aggregated_heatmap_source = True
        self._heatmap_source_info.update({
            "estimated_range_rows": summary.matching_rows,
            "sampled": False,
            "aggregated": True,
            "sample_limit": None,
            "sample_stride": None,
            "strategy": "spatial mean",
            })
        return x_centres[x_index_data], y_centres[y_index_data], z_data


    @staticmethod
    def _spatial_axis_bins(lower, upper, source_count, bin_count):
        return spatial_axis_bins(lower, upper, source_count, bin_count)


    def _selected_parameter_row_count(self, conn):
        self._check_cancelled()
        table = _sqlite_identifier(self.table_name)
        z_column = _sqlite_identifier(self.param.name)
        row = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {z_column} IS NOT NULL"
            ).fetchone()
        if row is None or row[0] is None:
            return None

        return int(row[0])


    def _heatmap_where_clause(self):
        ranges = self.heatmap_axis_ranges or {}
        clauses: list[str] = []
        parameters: list[float] = []

        for axis in ("x", "y"):
            axis_range = ranges.get(axis)
            if axis_range is None:
                continue

            try:
                low, high = sorted(float(value) for value in axis_range)
            except (TypeError, ValueError):
                continue

            if not (np.isfinite(low) and np.isfinite(high)) or low == high:
                continue

            column = _sqlite_identifier(self.axes_dict[axis])
            clauses.append(f"{column} BETWEEN ? AND ?")
            parameters.extend((low, high))

        return " AND ".join(clauses), parameters


    def _normalised_heatmap_axis_ranges(self, ranges):
        if not ranges:
            return None

        normalised = {}
        for axis in ("x", "y"):
            axis_range = ranges.get(axis)
            if axis_range is None:
                return None

            try:
                low, high = sorted(float(value) for value in axis_range)
            except (TypeError, ValueError):
                return None

            if not (np.isfinite(low) and np.isfinite(high)) or low == high:
                return None

            normalised[axis] = (low, high)

        return normalised


    def _arrays_from_cursor(self, cursor):
        x_values = []
        y_values = []
        z_values = []

        for row_number, (x_value, y_value, z_value) in enumerate(cursor):
            if row_number % 1024 == 0:
                self._check_cancelled()
            x_values.append(x_value)
            y_values.append(y_value)
            z_values.append(z_value)

        return self.processing._arrays_from_values(x_values, y_values, z_values)


        
    


    
    
        
    


        
class _emitter(QtCore.QObject):
    """
    QRunnable cannot emit signals, use of QObject can
    """
    printer = QtCore.pyqtSignal([str]) # FOR USE IN PLACE OF PRINT()
    finished = QtCore.pyqtSignal([bool]) # Callback to main to say fetch data
    errorOccurred = QtCore.pyqtSignal([Exception]) # Errors do not display in threads
    
