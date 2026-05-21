from typing import Any

import numpy as np
import numpy.typing as npt
import pyqtgraph as pg
from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

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
    open_subplot = QtCore.pyqtSignal([object, str, tuple])
    sweep_moved = QtCore.pyqtSignal([int, int])
    close_sweeps_requested = QtCore.pyqtSignal([object, object])
    
    def __init__(
            self,
            *args: Any,
            **kargs: Any,
            ) -> None:
        super().__init__(*args, **kargs)
        self.sweep_id = 0
        self.sweep_lines: dict[int, Any] = {}
        self.active_sweep_line_id = None
        self.__dict__["rotate"] = None # FOR SUBPLOT CURSOR
        self.__dict__["_colorbar_manual_levels"] = None

        
    def initFrame(self) -> None:
        """
        Sets up the initial plot and starting data.

        """
        self._large_heatmap_sql_mode = False
        self._heatmap_full_axis_ranges: dict[str, tuple[float, float]] | None = None
        self._heatmap_last_view_ranges: dict[str, tuple[float, float]] | None = None
        self._heatmap_worker_downsample_info: dict[str, Any] | None = None
        self._heatmap_downsample_info: dict[str, Any] | None = None
        self._heatmap_view_reload_timer = QtCore.QTimer(self)
        self._heatmap_view_reload_timer.setSingleShot(True)
        self._heatmap_view_reload_timer.timeout.connect(
            self._reload_visible_heatmap_data
            )
        self.vb.main_moved.connect(self._schedule_visible_heatmap_reload)

        self.image = pg.ImageItem(axisOrder='row-major')
        self.image.setZValue(0) # Like *Send to back*
        # self.image.setPxMode(True)
        
        self.plot.addItem(self.image)
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

        try:
            self._update_large_heatmap_state(plot_worker)
            if not self._has_plottable_heatmap_data():
                self.show_status(
                    f"Waiting for plottable data for {self.param.name}...",
                    5000,
                    )
                self.show_plot_state(
                    "Waiting for plottable data",
                    f"{self.param.name} has no finite heatmap data yet.",
                    kind="empty",
                    )
                return

            autoLevels=self.relevel_refresh.isChecked()
            # Produce Heatmap
            self.image.setImage(
                self.dataGrid,
                autoLevels=autoLevels,
                autoRange=True
                )

            #set axis values
            xmin = min(self.axis_data["x"])
            ymin = min(self.axis_data["y"])
            xrange = max(self.axis_data["x"]) - xmin
            yrange = max(self.axis_data["y"]) - ymin

            if xrange == 0:
                xrange = xmin / 100
            if yrange == 0:
                yrange = ymin / 100

            # Link x/y axis values with Heatmap data
            heatmap_rect = QtCore.QRectF(
                xmin,
                ymin,
                xrange,
                yrange
            )
            self.__dict__["rect"] = heatmap_rect
            self.image.setRect(heatmap_rect)

            # Produce color bar on first run
            if not hasattr(self, "bar"):
                self.bar = self.plot.addColorBar(
                    self.image,
                    colorMap=self._colorbar_colormap(),
                    rounding=(
                        np.nanmax(self.dataGrid) - np.nanmin(self.dataGrid)
                        ) / 1e5,  # Add 10,000 colours
                    colorMapMenu=False,
                    )
                self._set_colorbar_tick_formatter()
                if self._colorbar_manual_levels is None:
                    self.scaleColorbar()
                else:
                    self._set_colorbar_levels(*self._colorbar_manual_levels)

            if autoLevels:
                self.__dict__["_colorbar_manual_levels"] = None
                self.scaleColorbar()
            elif self._colorbar_manual_levels is not None:
                self._set_colorbar_levels(*self._colorbar_manual_levels)
            
            self._update_hover_pixel_outline_from_index()
            if self.marquee is not None:
                self.set_marquee_rect(self.marquee)
            self._snap_sweep_lines_to_pixel_centres()
        finally:
            # Allow new workers after empty live loads or display errors.
            plot_worker.running = False


    def _update_large_heatmap_state(self, worker: Any) -> None:
        self._update_heatmap_downsample_state(worker)
        if not getattr(worker, "loaded_from_sql_sample", False):
            return

        self._large_heatmap_sql_mode = True
        worker_ranges = getattr(worker, "heatmap_axis_ranges", None)
        if worker_ranges is None or self._heatmap_full_axis_ranges is None:
            self._heatmap_full_axis_ranges = self._axis_ranges_from_data()

        if worker_ranges is not None:
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
        button.setIcon(
            self.style().standardIcon(qtw.QStyle.StandardPixmap.SP_MessageBoxWarning)
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
                info.get("source_sampled") or info.get("grid_binned")
                ):
            return dict(info)

        return self._fallback_heatmap_downsample_info(worker)


    def _fallback_heatmap_downsample_info(
            self,
            worker: Any,
            ) -> dict[str, Any] | None:
        data_grid = getattr(worker, "dataGrid", self.__dict__.get("dataGrid", None))
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
        source_sampled = bool(getattr(worker, "sampled_heatmap_source", False))
        if loaded_point_count is not None:
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
            "source_sample_limit": (
                loaded_point_count if source_sampled else None
                ),
            "source_sample_stride": getattr(
                worker,
                "_heatmap_source_sample_stride",
                None,
                ),
            "source_sample_strategy": None,
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
            try:
                count = int(value)
            except (TypeError, ValueError):
                continue
            if count > 0:
                return count

        return None


    def _full_resolution_heatmap_limit(self, worker: Any) -> int:
        try:
            return max(1, int(worker.max_full_heatmap_points))
        except (AttributeError, TypeError, ValueError):
            pass

        try:
            return max(
                1,
                int(self.config.get("runtime_settings.max_full_heatmap_points")),
                )
        except (AttributeError, KeyError, TypeError, ValueError):
            return 1


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
            source_columns = self._format_heatmap_count(
                info.get("source_grid_columns")
                )
            source_rows = self._format_heatmap_count(info.get("source_grid_rows"))
            if info.get("source_sampled") or info.get("grid_binned"):
                return (
                    "Resolution: downsampled "
                    f"{grid_columns} x {grid_rows} of "
                    f"{source_columns} x {source_rows}"
                    )
            return f"Resolution: full {grid_columns} x {grid_rows}"

        data_grid = self.__dict__.get("dataGrid")
        try:
            grid_rows, grid_columns = data_grid.shape
        except (AttributeError, ValueError):
            return "Resolution: pending"

        source_shape = self._source_heatmap_grid_shape_from_metadata()
        if source_shape is None:
            return (
                "Resolution: plotted "
                f"{self._format_heatmap_count(grid_columns)} x "
                f"{self._format_heatmap_count(grid_rows)}; source unknown"
                )

        source_rows, source_columns = source_shape
        if int(source_rows) == int(grid_rows) and int(source_columns) == int(grid_columns):
            return (
                "Resolution: full "
                f"{self._format_heatmap_count(grid_columns)} x "
                f"{self._format_heatmap_count(grid_rows)}"
                )

        return (
            "Resolution: plotted "
            f"{self._format_heatmap_count(grid_columns)} x "
            f"{self._format_heatmap_count(grid_rows)} of "
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

        if not info.get("source_sampled") and not info.get("grid_binned"):
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
        if loaded_count is not None and source_count is not None:
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
        else:
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
                lines.append(
                    "Empty sampled display bins were filled by interpolation."
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
        if ranges is None or self._axis_ranges_match(ranges, self._heatmap_last_view_ranges):
            return

        self._heatmap_last_view_ranges = ranges
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
            width = max(abs(left_high - left_low), abs(right_high - right_low), 1.0)
            tolerance = width * 0.01
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
        if hasattr(self, "hover_pixel_outline"):
            self.hover_pixel_outline.hide()


    def _heatmap_rect(self) -> QtCore.QRectF | None:
        rect = self.__dict__.get("rect")
        if isinstance(rect, QtCore.QRectF):
            return rect
        return None


    def _update_hover_pixel_outline_from_index(self) -> None:
        heatmap_rect = self._heatmap_rect()
        z_index = self.__dict__.get("z_index")
        if (
                not hasattr(self, "hover_pixel_outline")
                or heatmap_rect is None
                or not hasattr(self, "dataGrid")
                or not isinstance(z_index, list)
                ):
            if hasattr(self, "hover_pixel_outline"):
                self.hover_pixel_outline.hide()
            return

        i, j = z_index
        rows, cols = self.dataGrid.shape
        if rows <= 0 or cols <= 0 or i < 0 or j < 0 or i >= cols or j >= rows:
            self.hover_pixel_outline.hide()
            return

        cell_width = heatmap_rect.width() / cols
        cell_height = heatmap_rect.height() / rows
        if cell_width <= 0 or cell_height <= 0:
            self.hover_pixel_outline.hide()
            return

        self.hover_pixel_outline.setRect(QtCore.QRectF(
            heatmap_rect.x() + i * cell_width,
            heatmap_rect.y() + j * cell_height,
            cell_width,
            cell_height,
        ))
        self.hover_pixel_outline.show()


    def _snap_marquee_rect(self, rect: QtCore.QRectF) -> QtCore.QRectF:
        """
        Snap marquee edges to heatmap pixel boundaries.

        """
        heatmap_rect = self._heatmap_rect()
        if heatmap_rect is None or not hasattr(self, "dataGrid"):
            return rect

        rows, cols = self.dataGrid.shape
        if (
                rows <= 0
                or cols <= 0
                or heatmap_rect.width() <= 0
                or heatmap_rect.height() <= 0
                ):
            return rect

        left, right = self._snap_marquee_axis_to_cells(
            rect.left(),
            rect.right(),
            heatmap_rect.x(),
            heatmap_rect.width(),
            cols,
            )
        bottom, top = self._snap_marquee_axis_to_cells(
            rect.top(),
            rect.bottom(),
            heatmap_rect.y(),
            heatmap_rect.height(),
            rows,
            )

        return QtCore.QRectF(left, bottom, right - left, top - bottom)


    def _snap_marquee_axis_to_cells(
            self,
            low: float,
            high: float,
            origin: float,
            span: float,
            count: int,
            ) -> tuple[float, float]:
        cell_size = span / count
        min_value = origin
        max_value = origin + span
        low = min(max(low, min_value), max_value)
        high = min(max(high, min_value), max_value)

        low_index = int(np.floor((low - origin) / cell_size))
        high_index = int(np.ceil((high - origin) / cell_size))
        low_index = min(max(low_index, 0), count - 1)
        high_index = min(max(high_index, low_index + 1), count)

        return (
            origin + low_index * cell_size,
            origin + high_index * cell_size,
            )


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
                or self._heatmap_rect() is None
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
        heatmap_rect = self._heatmap_rect()
        if heatmap_rect is None or self.marquee is None:
            return None

        rows, cols = self.dataGrid.shape
        if (
                rows <= 0
                or cols <= 0
                or heatmap_rect.width() <= 0
                or heatmap_rect.height() <= 0
                ):
            return None

        rect = self._snap_marquee_rect(self.marquee.normalized())
        if rect is None:
            return None

        col_slice = self._marquee_axis_slice(
            rect.left(),
            rect.right(),
            heatmap_rect.x(),
            heatmap_rect.width(),
            cols,
            )
        row_slice = self._marquee_axis_slice(
            rect.top(),
            rect.bottom(),
            heatmap_rect.y(),
            heatmap_rect.height(),
            rows,
            )
        if row_slice is None or col_slice is None:
            return None

        return row_slice, col_slice


    def _marquee_axis_slice(
            self,
            low: float,
            high: float,
            origin: float,
            span: float,
            count: int,
            ) -> slice | None:
        if count <= 0 or span <= 0:
            return None

        cell_size = span / count
        min_value = origin
        max_value = origin + span
        low = min(max(low, min_value), max_value)
        high = min(max(high, min_value), max_value)

        start = int(np.floor((low - origin) / cell_size))
        stop = int(np.ceil((high - origin) / cell_size))
        start = min(max(start, 0), count - 1)
        stop = min(max(stop, start + 1), count)

        return slice(start, stop)
