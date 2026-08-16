from typing import Any

import numpy as np
import numpy.typing as npt
import pyqtgraph as pg
from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

from qplot.tools.heatmap_geometry import (
    AxisGeometry,
    HeatmapGeometry,
    canonicalize_heatmap_data,
)

from . import _colorbar
from ._commands import command_spec, create_action
from ._plot2d_colorbar import Plot2DColorbarMixin
from ._plot2d_sweeps import Plot2DSweepMixin
from ._plotWin import plotWidget

_COLORBAR_COLORMAPS = _colorbar._COLORBAR_COLORMAPS
_HEATMAP_VIEW_RELOAD_DEBOUNCE_MS = 450
_HEATMAP_VIEW_RELOAD_MIN_FRACTION = 0.95


class plot2d(Plot2DSweepMixin, Plot2DColorbarMixin, plotWidget):
    """
    Plot window for 2d and higher plots, aka Heatmaps.
    Inherits and wraps several functions from qplot.windows._plotWin.plotWidget.
    PlotWidget handles majority of set up, recommend to view first.
    
    Key functions to see in plot2d:
        initFrame
        refreshPlot
        
    """
    operation_kind = "plot2d"
    open_subplot = QtCore.pyqtSignal([object, object, tuple])
    sweep_moved = QtCore.pyqtSignal([int, float])
    close_sweeps_requested = QtCore.pyqtSignal([object, object])
    
    def __init__(
            self,
            *args: Any,
            **kargs: Any,
            ) -> None:
        super().__init__(*args, **kargs)
        self.sweep_lines: dict[int, Any] = {}
        self.active_sweep_line_id = None
        self.__dict__["rotate"] = None # FOR SUBPLOT CURSOR
        self.__dict__["_colorbar_manual_levels"] = None

        
    def initFrame(self) -> None:
        """
        Sets up the initial plot and starting data.

        """
        self.vb.set_shift_pan_axis_constraint(True)
        self._large_heatmap_sql_mode = False
        self._heatmap_full_axis_ranges: dict[str, tuple[float, float]] | None = None
        self._heatmap_full_view_ranges: dict[str, tuple[float, float]] | None = None
        self._heatmap_last_view_ranges: dict[str, tuple[float, float]] | None = None
        self._heatmap_worker_downsample_info: dict[str, Any] | None = None
        self._heatmap_downsample_info: dict[str, Any] | None = None
        self._heatmap_view_reload_timer = QtCore.QTimer(self)
        self._heatmap_view_reload_timer.setSingleShot(True)
        self._heatmap_view_reload_timer.timeout.connect(
            self._reload_visible_heatmap_data
            )
        self._connect_heatmap_range_controls()

        self.image = pg.ImageItem(axisOrder='row-major')
        self.image.setZValue(0) # Like *Send to back*
        self.heatmap_mesh = pg.PColorMeshItem()
        self.heatmap_mesh.setZValue(0)
        self.heatmap_mesh.hide()

        self.plot.addItem(self.image)
        self.plot.addItem(self.heatmap_mesh)
        self.hover_pixel_outline = qtw.QGraphicsRectItem()
        self.hover_pixel_outline.setPen(
            pg.mkPen((255, 255, 255, 190), width=1.5, cosmetic=True)
        )
        self.hover_pixel_outline.setBrush(QtGui.QBrush(QtCore.Qt.BrushStyle.NoBrush))
        self.hover_pixel_outline.setZValue(10)
        self.hover_pixel_outline.hide()
        self.plot.addItem(self.hover_pixel_outline)
        
        # Wait for loader to finish to enure needed data is collected.
        self.load_data()
        self.show_status("Heatmap ready; loading data...", 5000)


    def _connect_heatmap_range_controls(self) -> None:
        self.plot.sigRangeChangedManually.connect(
            self._schedule_visible_heatmap_reload
            )
        self.vb.autoRange_triggered.connect(self._zoom_large_heatmap_to_all)
        self.plot.autoBtn.clicked.connect(
            lambda _button: self._zoom_large_heatmap_to_all()
            )


    def _view_range_changed_programmatically(self) -> None:
        self._schedule_visible_heatmap_reload()
      

    def initRefresh(self, refresh: Any) -> None:
        super().initRefresh(refresh)
        
        self.toolbarRef.addSeparator()
        self.toolbarRef.addWidget(qtw.QLabel("On refresh:  "))
        
        self.toolbarRef.addWidget(qtw.QLabel("Re-Map Colors "))
        
        self.relevel_refresh = qtw.QCheckBox()
        self.relevel_refresh.setToolTip("Autoscale the heatmap colour range on each refresh")
        self.relevel_refresh.toggled.connect(self._colorbar_auto_refresh_changed)
        self.toolbarRef.addWidget(self.relevel_refresh)
     
    
    def initContextMenu(self) -> None:
        super().initContextMenu()

        autoColor = create_action("heatmap.autoscale_color", self)
        self.register_shortcut(autoColor, command_spec("heatmap.autoscale_color"))
        autoColor.triggered.connect(self.scaleColorbar)
        self.vbMenu.insertAction(self.autoscaleSep, autoColor)

        actions = self.vbMenu.actions()
        
        sep = self.vbMenu.insertSeparator(actions[3])
        
        ### Sweep control
        h_sweep = create_action("heatmap.horizontal_cut", self)
        self.register_shortcut(h_sweep, command_spec("heatmap.horizontal_cut"))
        h_sweep.triggered.connect(lambda _: self.openSweep("h"))
        self.vbMenu.insertAction(sep, h_sweep)
        
        v_sweep = create_action("heatmap.vertical_cut", self)
        self.register_shortcut(v_sweep, command_spec("heatmap.vertical_cut"))
        v_sweep.triggered.connect(lambda _: self.openSweep("v"))
        self.vbMenu.insertAction(sep, v_sweep)
        
        # Link finish update with check for rotation of sweep cursor
        self.end_wait.connect(self.rotate_sweeps)
        self.vbMenu.insertSeparator(h_sweep)

        self._init_colorbar_scale_controls()

        for key, text in (
                (QtCore.Qt.Key.Key_Left, "Move selected cut left"),
                (QtCore.Qt.Key.Key_Right, "Move selected cut right"),
                (QtCore.Qt.Key.Key_Up, "Move selected cut up"),
                (QtCore.Qt.Key.Key_Down, "Move selected cut down"),
                ):
            action = QtGui.QAction(text, self)
            action.setShortcut(QtGui.QKeySequence(key))
            action.setShortcutContext(QtCore.Qt.ShortcutContext.WindowShortcut)
            action.triggered.connect(
                lambda _, key=key: self.move_sweep_with_arrow_key(key)
                )
            self.addAction(action)
        
        
    def initLabels(self) -> None:
        super().initLabels()
        self.__dict__["z_index"] = None
        
        self.pos_labels["y"].setText(self.pos_labels["y"].text() + ";")
        
        posLabelx = qtw.QLabel("z= ")
        self.toolbarCo_ord.addWidget(posLabelx)
        self.pos_labels["z"] = posLabelx
        self._init_heatmap_downsample_warning_button()
        
###############################################################################
    
    def refreshPlot(self, finished: bool = True, worker: Any = None) -> None:
        """
        Updates plot based on data produced by the thread worker. Data is 
        assigned in plotWidget.refreshPlot, then all plot items are produced
        here.

        Parameters
        ----------
        finished : bool
            In the event the worker had to abort, finished is False and refresh
            is not ran.
        """
        plot_worker = worker if worker is not None else self.worker
        if not super().refreshPlot(finished, worker=worker):
            plot_worker.running = False
            return

        full_sql_refresh = bool(
            getattr(plot_worker, "loaded_from_sql_heatmap", False)
            and getattr(plot_worker, "heatmap_axis_ranges", None) is None
            )

        try:
            self._update_large_heatmap_state(plot_worker)
            if not self._has_plottable_heatmap_data():
                self._invalidate_heatmap_geometry()
                self.show_status(
                    f"Waiting for plottable data for {self.param.name}...",
                    5000,
                    )
                self.show_plot_state(
                    "Waiting for plottable data",
                    f"{self.param.name} has no finite heatmap data yet.",
                    kind="empty",
                    )
                self._mark_display_synchronized(plot_worker)
                return

            try:
                self._update_heatmap_geometry()
            except (TypeError, ValueError) as error:
                self._invalidate_heatmap_geometry()
                self.show_status(f"Cannot display heatmap: {error}", 10_000)
                self.show_plot_state(
                    "Invalid heatmap geometry",
                    str(error),
                    kind="error",
                    )
                return

            autoLevels = self.relevel_refresh.isChecked()
            self._render_heatmap()

            # Produce color bar on first run
            if not hasattr(self, "bar"):
                self.bar = self.plot.addColorBar(
                    self._heatmap_colorbar_items(),
                    colorMap=self._colorbar_colormap(),
                    rounding=self._data_colorbar_rounding(),
                    colorMapMenu=False,
                    )
                self._set_colorbar_tick_formatter()
                if self._colorbar_manual_levels is None:
                    self.scaleColorbar()
                else:
                    self._set_colorbar_levels(*self._colorbar_manual_levels)

            # The dependent-variable label may have changed when an operation
            # such as differentiation was added or removed.
            self._sync_colorbar_axis_scaling()
            if autoLevels:
                self.__dict__["_colorbar_manual_levels"] = None
                self.scaleColorbar()
            elif self._colorbar_manual_levels is not None:
                self._set_colorbar_levels(*self._colorbar_manual_levels)
            
            self._restore_heatmap_interactions()
            self._mark_display_synchronized(plot_worker)
        finally:
            # Allow new workers after empty live loads or display errors.
            plot_worker.running = False
            self._ensure_refresh_monitor()
            if full_sql_refresh:
                self._schedule_visible_heatmap_reload()


    def _update_large_heatmap_state(self, worker: Any) -> None:
        self._update_heatmap_downsample_state(worker)
        if not getattr(worker, "loaded_from_sql_heatmap", False):
            self._large_heatmap_sql_mode = False
            self._heatmap_full_axis_ranges = None
            self._heatmap_full_view_ranges = None
            self._heatmap_last_view_ranges = None
            timer = self.__dict__.get("_heatmap_view_reload_timer")
            if timer is not None:
                timer.stop()
            return

        self._large_heatmap_sql_mode = True
        worker_ranges = getattr(worker, "heatmap_axis_ranges", None)
        if worker_ranges is None or self._heatmap_full_axis_ranges is None:
            source_ranges = self._normalise_axis_ranges(
                getattr(worker, "heatmap_source_axis_ranges", None)
                )
            self._heatmap_full_axis_ranges = (
                source_ranges or self._axis_ranges_from_data()
                )
            self._heatmap_full_view_ranges = self._axis_view_ranges_from_data()

        self._heatmap_last_view_ranges = self._normalise_axis_ranges(worker_ranges)


    def _init_heatmap_downsample_warning_button(self) -> None:
        resolution_label = qtw.QLabel("Resolution: pending")
        resolution_label.setObjectName("heatmapResolutionStatusLabel")
        resolution_label.setToolTip("Heatmap source and plotted grid resolution.")
        self.heatmap_resolution_label = resolution_label
        self.toolbarCo_ord.addWidget(resolution_label)

        parent = self._heatmap_downsample_button_parent()
        self.__dict__["_heatmap_downsample_button_parent"] = parent
        parent.installEventFilter(self)
        self.heatmap_downsample_button = self._new_heatmap_downsample_button(parent)

        self._update_heatmap_downsample_button()
        self._update_heatmap_resolution_label()


    def _heatmap_downsample_button_parent(self) -> qtw.QWidget:
        viewport = self.widget.viewport() if hasattr(self.widget, "viewport") else None
        if isinstance(viewport, qtw.QWidget):
            return viewport
        return self.widget


    def _new_heatmap_downsample_button(
            self,
            parent: qtw.QWidget,
            ) -> qtw.QToolButton:
        button = qtw.QToolButton(parent)
        button.setObjectName("heatmapDownsampleWarningButton")
        style = self.style()
        if style is not None:
            button.setIcon(
                style.standardIcon(qtw.QStyle.StandardPixmap.SP_MessageBoxWarning)
                )
        button.setIconSize(QtCore.QSize(18, 18))
        button.setAutoRaise(False)
        button.setFixedSize(28, 28)
        button.setText("")
        button.setToolButtonStyle(
            QtCore.Qt.ToolButtonStyle.ToolButtonIconOnly
            )
        button.setToolTip("This heatmap is downsampled. Click for details.")
        button.clicked.connect(
            lambda _checked=False: self.show_heatmap_downsample_dialog()
            )
        return button


    def _position_heatmap_downsample_button(self) -> None:
        button = self.__dict__.get("heatmap_downsample_button")
        parent = self.__dict__.get("_heatmap_downsample_button_parent")
        if button is None or parent is None:
            return

        margin = 8
        button.move(
            max(margin, parent.width() - button.width() - margin),
            max(margin, parent.height() - button.height() - margin),
            )
        button.raise_()


    def eventFilter(self, source, event):
        if (
                event.type() == QtCore.QEvent.Type.Resize
                and source is self.__dict__.get("_heatmap_downsample_button_parent")
                ):
            self._position_heatmap_downsample_button()

        return super().eventFilter(source, event)


    def _update_heatmap_downsample_state(self, worker: Any) -> None:
        self._heatmap_worker_downsample_info = self._heatmap_downsample_info_from_worker(
            worker
            )
        self._refresh_heatmap_downsample_info()


    def _refresh_heatmap_downsample_info(self) -> None:
        merged: dict[str, Any] = {}
        worker_info = self.__dict__.get("_heatmap_worker_downsample_info")
        if isinstance(worker_info, dict):
            merged.update(worker_info)

        self._heatmap_downsample_info = merged or None
        self._update_heatmap_downsample_button()
        self._update_heatmap_resolution_label()


    def _heatmap_downsample_info_from_worker(
            self,
            worker: Any,
            ) -> dict[str, Any] | None:
        info = getattr(worker, "heatmap_downsample_info", None)
        if isinstance(info, dict) and (
                info.get("source_sampled")
                or info.get("source_aggregated")
                or info.get("grid_binned")
                ):
            return dict(info)

        return self._fallback_heatmap_downsample_info(worker)


    def _fallback_heatmap_downsample_info(
            self,
            worker: Any,
            ) -> dict[str, Any] | None:
        data_grid = getattr(worker, "dataGrid", self.__dict__.get("dataGrid", None))
        if data_grid is None:
            return None
        try:
            grid_rows, grid_columns = data_grid.shape
        except (AttributeError, ValueError):
            return None

        if grid_rows <= 0 or grid_columns <= 0:
            return None

        source_rows, source_columns = self._source_heatmap_grid_shape(worker)
        source_cell_count = self._source_heatmap_cell_count(
            source_rows,
            source_columns,
            worker,
            )
        if source_cell_count is None:
            return None

        limit = self._full_resolution_heatmap_limit(worker)
        grid_cell_count = int(grid_rows * grid_columns)
        grid_reduced = grid_cell_count < int(source_cell_count)
        if (
                source_rows is not None
                and source_columns is not None
                and (
                    int(grid_rows) < int(source_rows)
                    or int(grid_columns) < int(source_columns)
                    )
                ):
            grid_reduced = True

        if int(source_cell_count) <= limit or not grid_reduced:
            return None

        loaded_point_count = getattr(worker, "loaded_point_count", None)
        source_aggregated = bool(
            getattr(worker, "aggregated_heatmap_source", False)
            )
        source_sampled = bool(getattr(worker, "sampled_heatmap_source", False))
        if loaded_point_count is not None and not source_aggregated:
            try:
                source_sampled = source_sampled or int(loaded_point_count) < int(
                    source_cell_count
                    )
            except (TypeError, ValueError):
                pass

        return {
            "source_row_count": getattr(
                worker,
                "total_point_count_estimate",
                source_cell_count,
                ),
            "estimated_range_rows": getattr(
                worker,
                "_heatmap_estimated_range_rows",
                None,
                ),
            "loaded_point_count": loaded_point_count,
            "source_sampled": source_sampled,
            "source_aggregated": source_aggregated,
            "aggregated_source_row_count": getattr(
                worker,
                "_heatmap_aggregated_source_rows",
                None,
                ),
            "source_sample_limit": (
                loaded_point_count if source_sampled else None
                ),
            "source_sample_stride": getattr(
                worker,
                "_heatmap_source_sample_stride",
                None,
                ),
            "source_sample_strategy": None,
            "source_aggregation_strategy": (
                "spatial mean" if source_aggregated else None
                ),
            "axis_ranges": getattr(worker, "heatmap_axis_ranges", None),
            "unique_x_count": source_columns,
            "unique_y_count": source_rows,
            "exact_cell_count": source_cell_count,
            "source_grid_columns": source_columns,
            "source_grid_rows": source_rows,
            "source_grid_cell_count": source_cell_count,
            "grid_columns": int(grid_columns),
            "grid_rows": int(grid_rows),
            "grid_cell_count": grid_cell_count,
            "grid_binned": True,
            "grid_cell_limit": limit,
            "full_resolution_point_limit": limit,
            "empty_bins_filled": bool(source_sampled),
            }


    def _source_heatmap_grid_shape(
            self,
            worker: Any,
            ) -> tuple[int | None, int | None]:
        shape = getattr(worker, "heatmap_source_grid_shape", None)
        if shape is None and hasattr(worker, "_heatmap_source_grid_shape_from_metadata"):
            try:
                shape = worker._heatmap_source_grid_shape_from_metadata()
            except (AttributeError, TypeError, ValueError):
                shape = None
        if shape is None:
            shape = self._source_heatmap_grid_shape_from_metadata()
        if shape is None:
            return None, None

        try:
            source_rows, source_columns = shape
            source_rows = int(source_rows)
            source_columns = int(source_columns)
        except (TypeError, ValueError):
            return None, None

        if source_rows <= 0 or source_columns <= 0:
            return None, None

        return source_rows, source_columns


    def _source_heatmap_grid_shape_from_metadata(self) -> tuple[int, int] | None:
        try:
            dataset = self.ds
        except (AttributeError, KeyError):
            return None

        cache = getattr(dataset, "cache", None)
        shapes = getattr(getattr(cache, "rundescriber", None), "shapes", None)
        if not isinstance(shapes, dict):
            return None

        shape = shapes.get(self.param.name)
        if shape is None:
            return None

        try:
            dimensions = [int(dimension) for dimension in shape]
        except (TypeError, ValueError):
            return None

        depends_on = list(getattr(self.param, "depends_on_", ()))
        if len(dimensions) != len(depends_on):
            return None

        axes = self.axis_options
        try:
            x_dimension = depends_on.index(axes["x"])
            y_dimension = depends_on.index(axes["y"])
        except (KeyError, ValueError):
            return None

        if (
                x_dimension >= len(dimensions)
                or y_dimension >= len(dimensions)
                or dimensions[x_dimension] <= 0
                or dimensions[y_dimension] <= 0
                ):
            return None

        return dimensions[y_dimension], dimensions[x_dimension]


    def _source_heatmap_cell_count(
            self,
            source_rows: int | None,
            source_columns: int | None,
            worker: Any,
            ) -> int | None:
        if source_rows is not None and source_columns is not None:
            return int(source_rows * source_columns)

        try:
            dataset_count = getattr(self.ds, "number_of_results", None)
        except (AttributeError, KeyError):
            dataset_count = None

        for value in (
                getattr(worker, "total_point_count_estimate", None),
                dataset_count,
                ):
            if value is None:
                continue
            try:
                count = int(value)
            except (TypeError, ValueError):
                continue
            if count > 0:
                return count

        return None


    def _full_resolution_heatmap_limit(self, worker: Any) -> int:
        try:
            configured_limit = worker.max_full_heatmap_points
        except AttributeError:
            configured_limit = self.config.get(
                "runtime_settings.max_full_heatmap_points"
                )
        return max(1, int(configured_limit))


    def _update_heatmap_downsample_button(self) -> None:
        visible = self.__dict__.get("_heatmap_downsample_info") is not None
        button = self.__dict__.get("heatmap_downsample_button")
        if button is None:
            return

        button.setVisible(visible)
        if visible:
            self._position_heatmap_downsample_button()


    def _update_heatmap_resolution_label(self) -> None:
        label = self.__dict__.get("heatmap_resolution_label")
        if label is None:
            return

        text = self._heatmap_resolution_label_text()
        label.setText(text)
        if self._heatmap_downsample_info is not None:
            label.setToolTip(self._heatmap_downsample_dialog_text())
        else:
            label.setToolTip(text)


    def _heatmap_resolution_label_text(self) -> str:
        info = self.__dict__.get("_heatmap_downsample_info")
        if isinstance(info, dict):
            grid_columns = self._format_heatmap_count(info.get("grid_columns"))
            grid_rows = self._format_heatmap_count(info.get("grid_rows"))
            info_source_columns = self._format_heatmap_count(
                info.get("source_grid_columns")
                )
            info_source_rows = self._format_heatmap_count(
                info.get("source_grid_rows")
                )
            if (
                    info.get("source_sampled")
                    or info.get("source_aggregated")
                    or info.get("grid_binned")
                    ):
                return (
                    "Resolution: downsampled "
                    f"{grid_columns} x {grid_rows} of "
                    f"{info_source_columns} x {info_source_rows}"
                    )
            return f"Resolution: full {grid_columns} x {grid_rows}"

        data_grid = self.__dict__.get("dataGrid")
        if data_grid is None:
            return "Resolution: pending"
        try:
            plotted_grid_rows, plotted_grid_columns = data_grid.shape
        except (AttributeError, ValueError):
            return "Resolution: pending"

        source_shape = self._source_heatmap_grid_shape_from_metadata()
        if source_shape is None:
            return (
                "Resolution: plotted "
                f"{self._format_heatmap_count(plotted_grid_columns)} x "
                f"{self._format_heatmap_count(plotted_grid_rows)}; source unknown"
                )

        source_rows, source_columns = source_shape
        if (
                int(source_rows) == int(plotted_grid_rows)
                and int(source_columns) == int(plotted_grid_columns)
                ):
            return (
                "Resolution: full "
                f"{self._format_heatmap_count(plotted_grid_columns)} x "
                f"{self._format_heatmap_count(plotted_grid_rows)}"
                )

        return (
            "Resolution: plotted "
            f"{self._format_heatmap_count(plotted_grid_columns)} x "
            f"{self._format_heatmap_count(plotted_grid_rows)} of "
            f"{self._format_heatmap_count(source_columns)} x "
            f"{self._format_heatmap_count(source_rows)}"
            )


    def show_heatmap_downsample_dialog(self) -> None:
        if self._heatmap_downsample_info is None:
            return

        self._new_heatmap_downsample_dialog().exec()


    def _new_heatmap_downsample_dialog(self) -> qtw.QMessageBox:
        dialog = qtw.QMessageBox(
            qtw.QMessageBox.Icon.Warning,
            "Downsampled Heatmap",
            self._heatmap_downsample_dialog_text(),
            qtw.QMessageBox.StandardButton.Ok,
            self,
            )
        dialog.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        return dialog


    def _heatmap_downsample_dialog_text(self) -> str:
        info = self._heatmap_downsample_info or {}
        lines = ["This heatmap is displayed from downsampled data."]

        if (
                not info.get("source_sampled")
                and not info.get("source_aggregated")
                and not info.get("grid_binned")
                ):
            return "\n".join(lines)

        lines.append("")

        axis_ranges = info.get("axis_ranges")
        if isinstance(axis_ranges, dict):
            x_range = self._format_heatmap_axis_range(axis_ranges.get("x"))
            y_range = self._format_heatmap_axis_range(axis_ranges.get("y"))
            if x_range and y_range:
                lines.extend([
                    "Visible range loaded:",
                    f"X: {x_range}",
                    f"Y: {y_range}",
                    "",
                    ])

        source_grid_columns = self._format_heatmap_count(
            info.get("source_grid_columns")
            )
        source_grid_rows = self._format_heatmap_count(info.get("source_grid_rows"))
        source_grid_cells = self._format_heatmap_count(
            info.get("source_grid_cell_count")
            )
        grid_columns = self._format_heatmap_count(info.get("grid_columns"))
        grid_rows = self._format_heatmap_count(info.get("grid_rows"))
        grid_cells = self._format_heatmap_count(info.get("grid_cell_count"))
        full_limit = self._format_heatmap_count(
            info.get("full_resolution_point_limit")
            )
        if info.get("grid_binned"):
            lines.extend([
                "Full-resolution grid:",
                f"The source heatmap grid is {source_grid_columns} x "
                f"{source_grid_rows} = {source_grid_cells} cells.",
                "The full-resolution heatmap limit is "
                f"{full_limit} points.",
                f"The plotted grid is {grid_columns} x {grid_rows} = "
                f"{grid_cells} cells.",
                "Values falling into the same plotted grid cell were averaged.",
                "",
                ])

        lines.append("Source rows:")
        loaded_count = info.get("loaded_point_count")
        source_count = info.get("source_row_count")
        aggregated_count = info.get("aggregated_source_row_count")
        if info.get("source_aggregated") and aggregated_count is not None:
            lines.append(
                "All "
                f"{self._format_heatmap_count(aggregated_count)} matching "
                "source rows contributed to "
                f"{self._format_heatmap_count(loaded_count)} spatial mean cells."
                )
        elif loaded_count is not None and source_count is not None:
            lines.append(
                "Loaded "
                f"{self._format_heatmap_count(loaded_count)} finite "
                "source rows from "
                f"{self._format_heatmap_count(source_count)} database rows."
                )
        elif loaded_count is not None:
            lines.append(
                "Loaded "
                f"{self._format_heatmap_count(loaded_count)} finite source rows."
                )
        else:
            lines.append("The number of loaded source rows is unknown.")
        estimated_rows = info.get("estimated_range_rows")
        if estimated_rows not in (None, info.get("source_row_count")):
            lines.append(
                "Estimated rows in the visible range: "
                f"{self._format_heatmap_count(estimated_rows)}."
                )
        if info.get("source_sampled"):
            sample_limit = self._format_heatmap_count(
                info.get("source_sample_limit")
                )
            stride = info.get("source_sample_stride")
            if isinstance(stride, (int, float)) and stride > 1:
                lines.append(
                    "Database rows were stride-sampled before plotting "
                    "(about every "
                    f"{self._format_heatmap_ordinal(stride)} matching row), "
                    "capped at "
                    f"{sample_limit} rows."
                    )
            else:
                lines.append(
                    "Database rows were uniformly sampled before plotting, capped at "
                    f"{sample_limit} rows."
                    )
        elif not info.get("source_aggregated"):
            lines.append("All matching source rows were read before display binning.")

        lines.extend(["", "Display grid:"])
        unique_x = self._format_heatmap_count(info.get("unique_x_count"))
        unique_y = self._format_heatmap_count(info.get("unique_y_count"))
        if info.get("grid_binned"):
            lines.append(
                "Loaded values were averaged into a "
                f"{grid_columns} x {grid_rows} display grid."
                )
            lines.append(
                "The source x/y positions would form up to "
                f"{unique_x} x {unique_y} cells."
                )
            if info.get("empty_bins_filled"):
                if info.get("source_sampled"):
                    lines.append(
                        "Empty sampled display bins were filled by interpolation."
                        )
                else:
                    lines.append(
                        "Empty display bins were filled by interpolation."
                        )
        else:
            lines.append(
                "The sampled source rows were displayed on an exact "
                f"{grid_columns} x {grid_rows} grid."
                )

        return "\n".join(lines)


    @staticmethod
    def _format_heatmap_count(value: Any) -> str:
        try:
            return f"{int(value):,}"
        except (TypeError, ValueError):
            return "unknown"


    @staticmethod
    def _format_heatmap_ordinal(value: Any) -> str:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return "unknown"

        suffix = "th"
        if number % 100 not in (11, 12, 13):
            suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")

        return f"{number:,}{suffix}"


    @staticmethod
    def _format_heatmap_axis_range(value: Any) -> str | None:
        try:
            low, high = value
            low = float(low)
            high = float(high)
        except (TypeError, ValueError):
            return None

        if not np.isfinite(low) or not np.isfinite(high):
            return None

        return f"{low:.6g} to {high:.6g}"


    def _axis_ranges_from_data(self) -> dict[str, tuple[float, float]] | None:
        ranges = {}
        for axis in ("x", "y"):
            values = np.asarray(self.axis_data.get(axis, []), dtype=float)
            finite = values[np.isfinite(values)]
            if finite.size == 0:
                return None

            low = float(np.nanmin(finite))
            high = float(np.nanmax(finite))
            if low == high:
                return None

            ranges[axis] = (low, high)

        return ranges


    def _axis_view_ranges_from_data(
            self,
            ) -> dict[str, tuple[float, float]] | None:
        try:
            geometry = HeatmapGeometry.from_centres(
                self.axis_data.get("x", []),
                self.axis_data.get("y", []),
                )
        except (TypeError, ValueError):
            return None

        return {
            "x": (float(geometry.x.edges[0]), float(geometry.x.edges[-1])),
            "y": (float(geometry.y.edges[0]), float(geometry.y.edges[-1])),
            }


    def _zoom_large_heatmap_to_all(self) -> None:
        if not self._large_heatmap_sql_mode:
            return

        full_axis_ranges = self._heatmap_full_axis_ranges
        if full_axis_ranges is None:
            return

        self._heatmap_view_reload_timer.stop()
        view_ranges = self._heatmap_full_view_ranges or full_axis_ranges
        self.vb.setRange(
            xRange=view_ranges["x"],
            yRange=view_ranges["y"],
            padding=0,
            )

        self._reload_full_heatmap_data()


    def _reload_full_heatmap_data(self) -> bool:
        full_axis_ranges = self._heatmap_full_axis_ranges
        if full_axis_ranges is None or self._heatmap_last_view_ranges is None:
            return False
        if getattr(getattr(self, "worker", None), "running", False):
            self._heatmap_view_reload_timer.start(
                _HEATMAP_VIEW_RELOAD_DEBOUNCE_MS
                )
            return False

        return self.load_data(
            force_sql_heatmap=True,
            heatmap_axis_ranges=None,
            heatmap_full_axis_ranges=full_axis_ranges,
            status_message=f"Loading full heatmap for {self.param.name}...",
            )


    def _schedule_visible_heatmap_reload(self, *_args: Any) -> None:
        if not self._large_heatmap_sql_mode:
            return

        if not self._has_plottable_heatmap_data():
            return

        self._heatmap_view_reload_timer.start(_HEATMAP_VIEW_RELOAD_DEBOUNCE_MS)


    def _reload_visible_heatmap_data(self) -> None:
        if not self._large_heatmap_sql_mode:
            return

        if getattr(getattr(self, "worker", None), "running", False):
            self._heatmap_view_reload_timer.start(_HEATMAP_VIEW_RELOAD_DEBOUNCE_MS)
            return

        ranges = self._visible_heatmap_axis_ranges()
        if ranges is None:
            self._reload_full_heatmap_data()
            return
        if self._axis_ranges_match(ranges, self._heatmap_last_view_ranges):
            return

        self.load_data(
            force_sql_heatmap=True,
            heatmap_axis_ranges=ranges,
            heatmap_full_axis_ranges=self._heatmap_full_axis_ranges,
            status_message=f"Loading visible data for {self.param.name}...",
            )


    def _visible_heatmap_axis_ranges(self) -> dict[str, tuple[float, float]] | None:
        full_ranges = self._heatmap_full_axis_ranges
        if full_ranges is None:
            return None

        try:
            view_x, view_y = self.vb.viewRange()
        except Exception:
            return None

        ranges = self._normalise_axis_ranges({
            "x": tuple(view_x),
            "y": tuple(view_y),
            })
        if ranges is None:
            return None

        clamped = {}
        is_zoomed = False
        for axis in ("x", "y"):
            full_low, full_high = full_ranges[axis]
            low, high = ranges[axis]
            full_width = full_high - full_low
            if full_width <= 0:
                return None

            low = max(full_low, min(full_high, low))
            high = max(full_low, min(full_high, high))
            if high <= low:
                return None

            if (high - low) < full_width * _HEATMAP_VIEW_RELOAD_MIN_FRACTION:
                is_zoomed = True

            clamped[axis] = (low, high)

        return clamped if is_zoomed else None


    def _normalise_axis_ranges(
            self,
            ranges: dict[str, tuple[float, float]] | None,
            ) -> dict[str, tuple[float, float]] | None:
        if ranges is None:
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


    def _axis_ranges_match(
            self,
            left: dict[str, tuple[float, float]],
            right: dict[str, tuple[float, float]] | None,
            ) -> bool:
        if right is None:
            return False

        for axis in ("x", "y"):
            left_low, left_high = left[axis]
            right_low, right_high = right[axis]
            width = max(abs(left_high - left_low), abs(right_high - right_low))
            coordinate_scale = max(
                abs(left_low),
                abs(left_high),
                abs(right_low),
                abs(right_high),
                1.0,
                )
            tolerance = max(
                width * 0.01,
                np.finfo(float).eps * coordinate_scale * 16,
                )
            if (
                    abs(left_low - right_low) > tolerance
                    or abs(left_high - right_high) > tolerance
                    ):
                return False

        return True


    def _has_plottable_heatmap_data(self) -> bool:
        x_data = np.asarray(self.axis_data.get("x", []), dtype=float)
        y_data = np.asarray(self.axis_data.get("y", []), dtype=float)
        z_data = np.asarray(self.dataGrid, dtype=float)

        return bool(
            x_data.size > 0
            and y_data.size > 0
            and z_data.size > 0
            and np.any(np.isfinite(z_data))
            )


    def _update_heatmap_geometry(self) -> HeatmapGeometry:
        """Build and install geometry from the current setpoint centres."""

        self.__dict__.pop("heatmap_geometry", None)
        self.__dict__.pop("rect", None)
        self._reset_heatmap_hover()
        x_centres, y_centres, data_grid = canonicalize_heatmap_data(
            self.axis_data["x"],
            self.axis_data["y"],
            self.dataGrid,
            )
        geometry = HeatmapGeometry.from_centres(x_centres, y_centres)

        axis_data = dict(self.axis_data)
        axis_data["x"] = x_centres
        axis_data["y"] = y_centres
        self.__dict__["axis_data"] = axis_data
        self.__dict__["dataGrid"] = data_grid
        self.__dict__["heatmap_geometry"] = geometry
        self.__dict__["rect"] = QtCore.QRectF(*geometry.rect)
        return geometry


    def _heatmap_geometry(self) -> HeatmapGeometry | None:
        geometry = self.__dict__.get("heatmap_geometry")
        if isinstance(geometry, HeatmapGeometry):
            return geometry
        return None


    def _required_heatmap_geometry(self) -> HeatmapGeometry:
        geometry = self._heatmap_geometry()
        if geometry is None:
            raise RuntimeError("Heatmap geometry is not available.")
        return geometry


    def _render_heatmap(self) -> None:
        """Render uniform grids as images and rectilinear grids as meshes."""

        geometry = self._required_heatmap_geometry()
        data_grid = np.asarray(self.dataGrid)
        if geometry.is_uniform:
            self.image.setImage(
                data_grid,
                autoLevels=False,
                )
            self.image.setRect(QtCore.QRectF(*geometry.rect))
            self.heatmap_mesh.hide()
            self.image.show()
            return

        mesh_data = np.asarray(data_grid, dtype=float).copy()
        mesh_data[~np.isfinite(mesh_data)] = np.nan
        x_vertices, y_vertices = np.meshgrid(
            geometry.x.edges,
            geometry.y.edges,
            indexing="xy",
            )
        self.heatmap_mesh.setData(
            x_vertices,
            y_vertices,
            mesh_data,
            autoLevels=False,
            )
        self.image.hide()
        self.heatmap_mesh.show()


    def _heatmap_colorbar_items(self) -> list[Any]:
        return [self.image, self.heatmap_mesh]


    def _hide_heatmap_renderers(self) -> None:
        for item_name in ("image", "heatmap_mesh"):
            item = self.__dict__.get(item_name)
            if item is not None:
                item.hide()


    def _reset_heatmap_hover(self) -> None:
        self.hide_hover_pixel_outline()
        labels = self.__dict__.get("pos_labels")
        if not isinstance(labels, dict):
            return
        index_label = labels.get("index")
        if index_label is not None:
            index_label.setText("")
        z_label = labels.get("z")
        if z_label is not None:
            z_label.setText("z =")


    def _invalidate_heatmap_geometry(self) -> None:
        self.__dict__.pop("heatmap_geometry", None)
        self.__dict__.pop("rect", None)
        self._hide_heatmap_renderers()
        self._reset_heatmap_hover()
        if self.__dict__.get("marquee") is not None:
            self.clear_marquee()
        self._set_sweep_lines_visible(False)


    def _restore_heatmap_interactions(self) -> None:
        self._set_sweep_lines_visible(True)
        marquee = self.__dict__.get("marquee")
        if isinstance(marquee, QtCore.QRectF):
            if self._marquee_intersects_heatmap(marquee):
                self.set_marquee_rect(marquee)
            else:
                self.clear_marquee()
        self._snap_sweep_lines_to_pixel_centres()


    def _set_sweep_lines_visible(self, visible: bool) -> None:
        for line in self.__dict__.get("sweep_lines", {}).values():
            set_visible = getattr(line, "setVisible", None)
            if callable(set_visible):
                set_visible(visible)


    def _marquee_intersects_heatmap(self, rect: QtCore.QRectF) -> bool:
        geometry = self._heatmap_geometry()
        if geometry is None:
            return False
        normalised = rect.normalized()
        left, bottom, right, top = geometry.bounds
        return bool(
            normalised.right() > left
            and normalised.left() < right
            and normalised.bottom() > bottom
            and normalised.top() < top
            )


    def heatmap_sample_at(
            self,
            x_value: float,
            y_value: float,
            ) -> tuple[int, int, float, float, float] | None:
        """Return index, recorded coordinates, and value under a point."""

        geometry = self._heatmap_geometry()
        if geometry is None:
            return None
        index = geometry.index_at(x_value, y_value)
        if index is None:
            return None

        x_index, y_index = index
        return (
            x_index,
            y_index,
            geometry.x.centre(x_index),
            geometry.y.centre(y_index),
            float(self.dataGrid[y_index, x_index]),
            )


    def show_hover_pixel_outline(self, i: int, j: int) -> None:
        """
        Move the hover outline to the heatmap pixel at the given data indices.

        Parameters
        ----------
        i : int
            Column index within the heatmap data grid.
        j : int
            Row index within the heatmap data grid.
        """
        self.__dict__["z_index"] = [i, j]
        self._update_hover_pixel_outline_from_index()


    def hide_hover_pixel_outline(self) -> None:
        """
        Hide the heatmap hover outline and clear the saved hover index.

        """
        self.__dict__["z_index"] = None
        outline = self.__dict__.get("hover_pixel_outline")
        if outline is not None:
            outline.hide()


    def _update_hover_pixel_outline_from_index(self) -> None:
        geometry = self._heatmap_geometry()
        z_index = self.__dict__.get("z_index")
        if (
                not hasattr(self, "hover_pixel_outline")
                or geometry is None
                or not isinstance(z_index, list)
                or len(z_index) != 2
                ):
            if hasattr(self, "hover_pixel_outline"):
                self.hover_pixel_outline.hide()
            return

        i, j = z_index
        try:
            cell_rect = geometry.cell_rect(i, j)
        except (IndexError, TypeError):
            self.__dict__["z_index"] = None
            self.hover_pixel_outline.hide()
            return

        self.hover_pixel_outline.setRect(QtCore.QRectF(*cell_rect))
        self.hover_pixel_outline.show()


    def _snap_marquee_rect(self, rect: QtCore.QRectF) -> QtCore.QRectF:
        """
        Snap marquee edges to heatmap pixel boundaries.

        """
        geometry = self._heatmap_geometry()
        if geometry is None:
            return rect

        try:
            left, right = geometry.x.snap_interval(rect.left(), rect.right())
            bottom, top = geometry.y.snap_interval(rect.top(), rect.bottom())
        except ValueError:
            return rect

        return QtCore.QRectF(left, bottom, right - left, top - bottom)


    def _snap_translated_marquee_rect(
            self,
            rect: QtCore.QRectF,
            original: QtCore.QRectF,
            handle: Any,
            ) -> None:
        """Preserve selected cell counts during Shift-drag translation."""

        geometry = self._heatmap_geometry()
        if geometry is None:
            super()._snap_translated_marquee_rect(rect, original, handle)
            return

        original = original.normalized()
        snapped = self._snap_marquee_rect(QtCore.QRectF(rect).normalized())
        adjusted = QtCore.QRectF(snapped)
        try:
            if "w" in handle or "e" in handle:
                left, right = self._translated_marquee_axis_bounds(
                    geometry.x,
                    original.left(),
                    original.right(),
                    snapped.left(),
                    snapped.right(),
                    anchor_low="w" in handle,
                    )
                adjusted.setLeft(left)
                adjusted.setRight(right)
            if "n" in handle or "s" in handle:
                bottom, top = self._translated_marquee_axis_bounds(
                    geometry.y,
                    original.top(),
                    original.bottom(),
                    snapped.top(),
                    snapped.bottom(),
                    anchor_low="s" in handle,
                    )
                adjusted.setTop(bottom)
                adjusted.setBottom(top)
        except ValueError:
            super()._snap_translated_marquee_rect(rect, original, handle)
            return

        rect.setRect(
            adjusted.left(),
            adjusted.top(),
            adjusted.width(),
            adjusted.height(),
            )


    @staticmethod
    def _translated_marquee_axis_bounds(
            axis: AxisGeometry,
            original_low: float,
            original_high: float,
            snapped_low: float,
            snapped_high: float,
            *,
            anchor_low: bool,
            ) -> tuple[float, float]:
        original_cells = axis.slice_for_interval(original_low, original_high)
        target_cells = axis.slice_for_interval(snapped_low, snapped_high)
        cell_count = original_cells.stop - original_cells.start

        if anchor_low:
            start = target_cells.start
            stop = min(start + cell_count, axis.count)
            start = max(0, stop - cell_count)
        else:
            stop = target_cells.stop
            start = max(0, stop - cell_count)
            stop = min(axis.count, start + cell_count)

        return axis.edges[start], axis.edges[stop]


    def _add_marquee_color_context_action(self, menu: qtw.QMenu) -> QtGui.QAction:
        action = self._add_marquee_context_action(
            menu,
            "Zoom color",
            self.zoom_marquee_color,
            )
        if self._marquee_color_levels() is None:
            action.setEnabled(False)
            action.setToolTip("No finite data range inside the marquee.")
        return action


    def zoom_marquee_color(self) -> bool:
        levels = self._marquee_color_levels()
        if levels is None:
            return False

        return self.setColorbarManualRange(*levels)


    def _marquee_stats_text(self) -> str | None:
        selected = self._marquee_selected_data()
        if selected is None:
            return None

        values = selected[np.isfinite(selected)]
        if values.size == 0:
            return None

        if self.marquee is None:
            return None

        rows, cols = selected.shape
        rect = self._snap_marquee_rect(self.marquee.normalized())
        return self._format_marquee_stats_text(f"{cols}×{rows} points", values, rect)


    def _marquee_color_levels(self) -> tuple[float, float] | None:
        selected = self._marquee_selected_data()
        if selected is None:
            return None

        values = selected[np.isfinite(selected)]
        if values.size == 0:
            return None

        vmin = float(values.min())
        vmax = float(values.max())
        if not np.isfinite(vmin) or not np.isfinite(vmax) or vmin >= vmax:
            return None

        return vmin, vmax


    def _marquee_selected_data(self) -> npt.NDArray[np.float64] | None:
        if (
                self.__dict__.get("marquee") is None
                or self._heatmap_geometry() is None
                or "dataGrid" not in self.__dict__
                ):
            return None

        slices = self._marquee_cell_slices()
        if slices is None:
            return None

        row_slice, col_slice = slices
        selected = np.asarray(self.dataGrid[row_slice, col_slice], dtype=float)
        if selected.size == 0:
            return None

        return selected


    def _marquee_cell_slices(self) -> tuple[slice, slice] | None:
        geometry = self._heatmap_geometry()
        if geometry is None or self.marquee is None:
            return None

        rect = self._snap_marquee_rect(self.marquee.normalized())
        try:
            col_slice = geometry.x.slice_for_interval(rect.left(), rect.right())
            row_slice = geometry.y.slice_for_interval(rect.top(), rect.bottom())
        except ValueError:
            return None

        return row_slice, col_slice
