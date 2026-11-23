from collections.abc import Mapping
from typing import Any

import numpy as np
import numpy.typing as npt
import pyqtgraph as pg
from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

from qplot.datahandling.numeric_samples import numeric_isfinite
from qplot.tools.heatmap_geometry import (
    AxisGeometry,
    HeatmapGeometry,
    canonicalize_heatmap_data,
)
from qplot.tools.heatmap_resolution import (
    GridSize,
    HeatmapResolutionReport,
    resolution_report,
    source_grid_size,
)

from ._commands import command_spec, create_action
from ._display_precision import (
    DISPLAY_PRECISION_MESSAGE,
    HEATMAP_DISPLAY_RANGE_MESSAGE,
    float_display_loses_distinctions,
    heatmap_display_range_is_finite,
)
from ._heatmap_resolution_state import HeatmapResolutionState
from ._heatmap_resolution_ui import HeatmapResolutionUi
from ._heatmap_viewport import (
    AxisRanges,
    HeatmapViewportController,
    HeatmapViewportState,
)
from ._heatmap_viewport_ui import HeatmapViewportUi
from ._mixed_plots import MixedPlotMixin
from ._plot1d_snap import Plot1DSnapMixin
from ._plot2d_sweeps import Plot2DSweepMixin
from ._plot_axis_scaling import _install_flush_axis_draw_specs
from ._plot_publication import PublicationOutcome
from ._plotWin import plotWidget


class plot2d(
        Plot1DSnapMixin,
        Plot2DSweepMixin,
        MixedPlotMixin,
        plotWidget,
        ):
    """
    Plot window for 2d and higher plots, aka Heatmaps.
    Inherits and wraps several functions from qplot.windows._plotWin.plotWidget.
    PlotWidget handles majority of set up, recommend to view first.
    
    Key functions to see in plot2d:
        initFrame
        refreshPlot
        
    """
    operation_kind = "plot2d"
    _uses_heatmap_viewport = True
    _initial_load_message = "Heatmap ready; loading data..."
    get_mergables = QtCore.pyqtSignal()
    open_subplot = QtCore.pyqtSignal([object, object, tuple])
    sweep_moved = QtCore.pyqtSignal([int, float])
    close_sweeps_requested = QtCore.pyqtSignal([object, object])
    remove_dataset = QtCore.pyqtSignal([object])
    
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

        
    @QtCore.pyqtSlot(object)
    def mouseMoved(self, pos):
        """Use snap-to-trace readout before the base plot's free cursor."""
        Plot1DSnapMixin.mouseMoved(self, pos)


    @QtCore.pyqtSlot()
    def change_axis(self, key: str) -> None:
        """Keep heatmap cut rotation coupled to the axis change."""
        Plot2DSweepMixin.change_axis(self, key)


    def _heatmap_csv_units(self) -> tuple[tuple[str, str], ...]:
        return (("x_unit", self.axis_param["x"].unit), ("y_unit", self.axis_param["y"].unit),
                ("value_unit", self.display_param.unit))

    def initAxes(self) -> None:
        """Keep axis-selection state; expose swaps through Appearance."""
        super().initAxes()
        self.axes_dock.toggleViewAction().setVisible(False)


    def initFrame(self) -> None:
        """
        Create heatmap renderers and controls before the initial data read.

        """
        self.vb.set_shift_pan_axis_constraint(True)
        self._viewport_controller()
        self._resolution_state()
        self._connect_heatmap_range_controls()
        self._install_plot_control_view_range_preserver()

        for side in ("left", "right", "top", "bottom"):
            _install_flush_axis_draw_specs(self.plot.getAxis(side))

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
        self._add_plot_overlay(self.hover_pixel_outline)
        self._init_heatmap_layers()

    def _viewport_controller(self) -> HeatmapViewportController:
        controller = self.__dict__.get("_heatmap_viewport_controller")
        if controller is None:
            ui = HeatmapViewportUi(self)
            self.__dict__["_heatmap_viewport_ui"] = ui
            controller = self.__dict__["_heatmap_viewport_controller"] = HeatmapViewportController(ui)
        return controller

    @property
    def _viewport_state(self) -> HeatmapViewportState:
        controller = self.__dict__.get("_heatmap_viewport_controller")
        return controller.accepted if controller is not None else HeatmapViewportState()

    @property
    def _large_heatmap_sql_mode(self) -> bool:
        return self._viewport_state.enabled

    @property
    def _heatmap_full_axis_ranges(self) -> dict[str, tuple[float, float]] | None:
        ranges = self._viewport_state.full
        return ranges.as_dict() if ranges is not None else None

    @property
    def _heatmap_full_view_ranges(self) -> dict[str, tuple[float, float]] | None:
        ranges = self._viewport_state.full_view
        return ranges.as_dict() if ranges is not None else None

    @property
    def _heatmap_last_view_ranges(self) -> dict[str, tuple[float, float]] | None:
        ranges = self._viewport_state.displayed
        return ranges.as_dict() if ranges is not None else None

    @property
    def _heatmap_view_reload_timer(self) -> QtCore.QTimer:
        return self.__dict__["_heatmap_viewport_ui"].timer

    def _cancel_plot_refresh(self) -> None:
        controller = self.__dict__.get("_heatmap_viewport_controller")
        if controller is not None:
            controller.reset()
        state = self.__dict__.get("_heatmap_resolution_state")
        if state is not None:
            state.reset()
            self._sync_heatmap_resolution_warning()
        super()._cancel_plot_refresh()

    def _connect_heatmap_range_controls(self) -> None:
        self.plot.sigRangeChangedManually.connect(
            self._schedule_visible_heatmap_reload
            )
        self.vb.autoRange_triggered.connect(self._zoom_large_heatmap_to_all)
        self.plot.autoBtn.clicked.connect(
            lambda _button: self._zoom_large_heatmap_to_all()
            )


    def _plot_layout_controls(self) -> tuple[Any, ...]:
        """Return controls whose visibility changes resize the plot canvas."""

        return tuple(
            control
            for name in (
                "axes_dock",
                "oper_dock",
                "toolbarCo_ord",
                )
            if (control := self.__dict__.get(name)) is not None
            )


    def _install_plot_control_view_range_preserver(self) -> None:
        """Watch every dock and toolbar that can resize the heatmap canvas."""

        installed = self.__dict__.setdefault(
            "_plot_control_view_range_filters",
            set(),
            )
        for control in self._plot_layout_controls():
            control_id = id(control)
            if control_id in installed:
                continue
            control.installEventFilter(self)
            installed.add(control_id)


    def _preserve_heatmap_view_range_after_control_layout(self) -> None:
        """Restore a loaded heatmap's viewport after controls are relaid out."""

        worker = getattr(self, "worker", None)
        if worker is None or getattr(worker, "running", False):
            # Never restore a pre-refresh range over a newly published heatmap.
            return
        try:
            if not self._has_plottable_heatmap_data():
                return
        except (AttributeError, TypeError, ValueError):
            return

        try:
            viewbox = self._primary_heatmap_viewbox()
            publication_generation = getattr(self, "_qplot_publication_generation", 0)
            pending = self.__dict__.get("_plot_control_layout_restore")
            if (
                    isinstance(pending, dict)
                    and pending.get("viewbox") is viewbox
                    and pending.get("worker") is worker
                    and pending.get("publication_generation")
                    == publication_generation
                    ):
                saved_range = pending["view_range"]
            else:
                # A completed render can still have its initial auto-range
                # queued for the next paint. Do not preserve the empty viewport
                # over that newly loaded data when a control becomes visible.
                if getattr(viewbox, "_autoRangeNeedsUpdate", False):
                    viewbox.updateAutoRange()
                view_range = viewbox.viewRange()
                saved_range = (
                    tuple(float(value) for value in view_range[0]),
                    tuple(float(value) for value in view_range[1]),
                    )
        except (AttributeError, IndexError, TypeError, ValueError):
            return

        token = self.__dict__.get("_plot_control_layout_token", 0) + 1
        self.__dict__["_plot_control_layout_token"] = token
        self.__dict__["_plot_control_layout_restore"] = {
            "publication_generation": publication_generation,
            "view_range": saved_range,
            "viewbox": viewbox,
            "worker": worker,
            }

        def restore(final_pass: bool = False) -> None:
            if self.__dict__.get("_plot_control_layout_token") != token:
                return
            if (
                    getattr(self, "worker", None) is not worker
                    or getattr(worker, "running", False)
                    or getattr(self, "_qplot_publication_generation", 0)
                    != publication_generation
                    ):
                self.__dict__.pop("_plot_control_layout_restore", None)
                return
            try:
                if getattr(viewbox, "_autoRangeNeedsUpdate", False):
                    # Consume the auto-range queued by ViewBox.resizeEvent so
                    # it cannot run later and overwrite the saved viewport.
                    viewbox.updateAutoRange()
                current_range = viewbox.viewRange()
            except (AttributeError, TypeError):
                self.__dict__.pop("_plot_control_layout_restore", None)
                return
            if not np.allclose(
                    current_range,
                    saved_range,
                    rtol=1e-12,
                    atol=1e-12,
                    ):
                viewbox.setRange(
                    xRange=saved_range[0],
                    yRange=saved_range[1],
                    padding=0,
                    disableAutoRange=False,
                    )
                self._sync_secondary_heatmap_view_ranges()

            if final_pass:
                self.__dict__.pop("_plot_control_layout_restore", None)
            else:
                # A dock and its canvas resize can arrive in adjacent event-loop
                # turns. Recheck once after both layout passes have settled.
                QtCore.QTimer.singleShot(0, lambda: restore(True))

        QtCore.QTimer.singleShot(0, restore)


    def _view_range_changed_programmatically(self) -> None:
        self._schedule_visible_heatmap_reload()


    def _emit_heatmap_trace_updated(self) -> None:
        """Publish a completed heatmap render when Qt is fully initialised."""

        try:
            self.trace_updated.emit()
        except RuntimeError:
            # A few focused geometry tests construct plot2d with ``__new__``
            # and deliberately omit QMainWindow initialisation.
            return


    @QtCore.pyqtSlot(bool)
    def closeEvent(self, event: object) -> None:
        self.close_secondary_heatmaps()
        super().closeEvent(event)
      

    def initContextMenu(self) -> None:
        super().initContextMenu()
        # Opening a popup clears the transient hover readout. Keep the cut
        # target until the chosen action has run, including after menu hide.
        clear_cut_target = QtCore.QTimer(self)
        clear_cut_target.setSingleShot(True)
        clear_cut_target.setInterval(0)
        clear_cut_target.timeout.connect(
            lambda: self.__dict__.pop("_context_cut_index", None)
        )

        def capture_cut_target():
            clear_cut_target.stop()
            index = self.__dict__.get("z_index")
            self.__dict__["_context_cut_index"] = list(index) if isinstance(index, list) else None

        self.vbMenu.aboutToShow.connect(capture_cut_target)
        self.vbMenu.aboutToHide.connect(clear_cut_target.start)

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
        
        self.vbMenu.insertSeparator(h_sweep)

        self._init_colorbar_scale_controls()

        self._init_cut_keyboard_shortcuts()
        
        
    def initLabels(self) -> None:
        super().initLabels()
        self._install_plot_control_view_range_preserver()
        self.__dict__["z_index"] = None
        
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
            plot_worker.result.loaded_from_sql_heatmap
            and plot_worker.result.heatmap_axis_ranges is None
        )
        publication = self._refresh_publication_for(plot_worker)

        def clear_display():
            self._invalidate_heatmap_geometry()
            bar = self.__dict__.get("bar")
            hide = getattr(bar, "hide", None)
            if callable(hide):
                hide()

        try:
            with self._render_publication(plot_worker, clear_display):
                self._stage_heatmap_resolution(plot_worker.result)
                if not self._refresh_publication_source_is_current(
                        plot_worker,
                        clear_display=clear_display,
                        ):
                    return
                # Marker orientation must follow the newly published parameter
                # axes before geometry restoration clamps their coordinates.
                self.rotate_sweeps()
                if not self._refresh_publication_source_is_current(
                        plot_worker, clear_display=clear_display):
                    return
                self._update_large_heatmap_state(plot_worker.result)
                self.refresh_secondary_heatmaps()
                self.refresh_secondary_lines()
                if not self._refresh_publication_source_is_current(
                        plot_worker,
                        clear_display=clear_display,
                        ):
                    return
                if not self._has_plottable_heatmap_data():
                    self._invalidate_heatmap_geometry()
                    if not self._refresh_publication_source_is_current(
                            plot_worker,
                            clear_display=clear_display,
                            ):
                        return
                    self._emit_heatmap_trace_updated()
                    if not self._refresh_publication_source_is_current(
                            plot_worker,
                            clear_display=clear_display,
                            ):
                        return
                    self.show_status(
                        f"Waiting for plottable data for {self.param.name}...",
                        5000,
                        )
                    self.show_plot_state(
                        "Waiting for plottable data",
                        f"{self.param.name} has no finite heatmap data yet.",
                        kind="empty",
                    )
                    display_synchronized = self._mark_display_synchronized(
                        plot_worker,
                    )
                    if (
                        plot_worker.result.dataset_completed is True
                        and not display_synchronized
                    ):
                        return
                    if getattr(plot_worker, "_qplot_source_rejected", False):
                        clear_display()
                        return
                    self._commit_refresh_publication(plot_worker, clear_display=clear_display)
                    return

                try:
                    geometry = self._update_heatmap_geometry()
                except (TypeError, ValueError) as error:
                    self._invalidate_heatmap_geometry()
                    self._emit_heatmap_trace_updated()
                    if not self._refresh_publication_source_is_current(
                            plot_worker,
                            clear_display=clear_display,
                            ):
                        return
                    self.show_status(f"Cannot display heatmap: {error}", 10_000)
                    self.show_plot_state(
                        "Invalid heatmap geometry",
                        str(error),
                        kind="error",
                        )
                    return

                viewbox = self._primary_heatmap_viewbox()
                if not heatmap_display_range_is_finite(geometry, (viewbox.width(), viewbox.height())):
                    self._viewport_controller().cancel_pending()
                    self._reject_heatmap_color_range(
                        message=HEATMAP_DISPLAY_RANGE_MESSAGE,
                        title="Unsupported heatmap coordinate range",
                    )
                    self._emit_heatmap_trace_updated()
                    if self._refresh_publication_source_is_current(
                            plot_worker, clear_display=clear_display):
                        self._mark_display_synchronized(plot_worker)
                    return

                if float_display_loses_distinctions(self.dataGrid):
                    self._reject_heatmap_color_range(
                        message=DISPLAY_PRECISION_MESSAGE,
                        title="Unsupported heatmap display precision",
                    )
                    self._emit_heatmap_trace_updated()
                    if self._refresh_publication_source_is_current(
                            plot_worker, clear_display=clear_display):
                        self._mark_display_synchronized(plot_worker)
                    return

                color_levels = self._colorbar_manual_levels
                if color_levels is None:
                    color_levels = self._data_colorbar_levels()
                if color_levels is not None and not np.isfinite(float(color_levels[1]) - float(color_levels[0])):
                    self._reject_heatmap_color_range()
                    self._emit_heatmap_trace_updated()
                    if not self._refresh_publication_source_is_current(
                            plot_worker,
                            clear_display=clear_display,
                            ):
                        return
                    # A color rejection is a concrete display of the loaded data.
                    # A completed source must not keep replacing that error with
                    # automatic reloads; live sources and explicit refreshes remain
                    # retryable through the usual guarded publication state.
                    self._mark_display_synchronized(plot_worker)
                    return
                self._heatmap_color_range_rejected = False
                self._heatmap_publication_color_levels = color_levels
                try:
                    self._render_heatmap()
                finally:
                    self.__dict__.pop("_heatmap_publication_color_levels", None)
                if not self._refresh_publication_source_is_current(
                        plot_worker,
                        clear_display=clear_display,
                        ):
                    return

                # A trace dropped before this first image can already have put
                # the shared axes into qPlot-managed Auto with empty image bounds.
                self._refresh_trace_axis_auto_ranges()

                # Produce color bar on first run
                if not hasattr(self, "bar"):
                    colorbar_items = self._heatmap_colorbar_items()
                    self.bar = self._add_heatmap_colorbar(
                        colorbar_items,
                        self._colorbar_rounding_for_levels(*color_levels) if color_levels else 1e-5,
                        )
                    self._heatmap_colorbar_item_ids = tuple(
                        id(item) for item in colorbar_items
                        )
                    self._set_colorbar_tick_formatter()
                    # The colour bar is created after the initial plot theme has
                    # been applied, so style its axes immediately as well.
                    self.config.theme.style_plotItem(self)
                    self.apply_axis_major_tick_count_preference()
                self.bar.show()
                if color_levels is not None:
                    self._set_colorbar_levels(*color_levels)
                else:
                    self._sync_colorbar_axis_scaling()

                self._restore_heatmap_interactions()
                if not self._refresh_publication_source_is_current(
                        plot_worker,
                        clear_display=clear_display,
                        ):
                    return
                self._emit_heatmap_trace_updated()
                if not self._refresh_publication_source_is_current(
                        plot_worker,
                        clear_display=clear_display,
                        ):
                    return
                self._mark_display_synchronized(plot_worker)
                if getattr(plot_worker, "_qplot_source_rejected", False):
                    clear_display()
                    return
                self._commit_refresh_publication(plot_worker, preview_ready=True, clear_display=clear_display)
        finally:
            viewport = self.__dict__.get("_heatmap_viewport_controller")
            if viewport is not None:
                viewport.finish(publication)
            self._sync_heatmap_resolution_warning()
            if (publication is not None and publication.outcome is PublicationOutcome.COMMITTED
                    and self._qplot_publication_generation == publication.generation
                    and (viewport is None or not viewport.pending)
                    and full_sql_refresh and not self.__dict__.get("_heatmap_color_range_rejected", False)):
                self._schedule_visible_heatmap_reload()


    def _update_large_heatmap_state(self, worker: Any) -> None:
        publication = self._refresh_publication
        if publication is None:
            return
        controller = self._viewport_controller()
        state = HeatmapViewportState()
        if self._uses_heatmap_viewport and getattr(worker, "loaded_from_sql_heatmap", False):
            displayed = AxisRanges.normalise(getattr(worker, "heatmap_axis_ranges", None))
            accepted = controller.accepted
            full, full_view = accepted.full, accepted.full_view
            if displayed is None or full is None:
                full = (AxisRanges.normalise(getattr(worker, "heatmap_source_axis_ranges", None))
                        or AxisRanges.normalise(self._axis_ranges_from_data()))
                full_view = AxisRanges.normalise(self._axis_view_ranges_from_data())
            state = HeatmapViewportState(True, full, full_view, displayed)
        controller.stage(publication, state)


    def _resolution_state(self) -> HeatmapResolutionState:
        state = self.__dict__.get("_heatmap_resolution_state")
        if state is None:
            state = self.__dict__["_heatmap_resolution_state"] = HeatmapResolutionState()
        return state

    @property
    def _heatmap_resolution_report(self) -> HeatmapResolutionReport | None:
        state = self.__dict__.get("_heatmap_resolution_state")
        return state.accepted if state is not None else None

    def _stage_heatmap_resolution(self, result: Any) -> None:
        publication = self._refresh_publication
        if publication is None:
            return
        state = self._resolution_state()
        state.stage(publication, None)
        info = getattr(result, "heatmap_downsample_info", None)
        if isinstance(info, Mapping):
            explicit_report = HeatmapResolutionReport.from_metadata(info)
            if explicit_report.reduced:
                state.stage(publication, explicit_report)
                return
        if getattr(result, "heatmap_full_resolution", False) is True:
            return
        try:
            dataset = self.ds
        except (AttributeError, KeyError):
            dataset = None
        cache = getattr(dataset, "cache", None)
        shapes = getattr(getattr(cache, "rundescriber", None), "shapes", None)
        param = getattr(self, "param", None)
        source = source_grid_size(shapes, getattr(param, "name", ""),
                                  getattr(param, "depends_on_", ()),
                                  getattr(self, "axis_options", {}))
        config = self.__dict__.get("config")
        limit = getattr(result, "max_full_heatmap_points", None)
        if limit is None and config is not None:
            limit = config.get("runtime_settings.max_full_heatmap_points")
        report = resolution_report(
            result, source=source,
            displayed=GridSize.from_shape(getattr(self.__dict__.get("dataGrid"), "shape", None)),
            source_rows=getattr(dataset, "number_of_results", None),
            full_resolution_limit=limit,
        )
        if self._refresh_publication is publication:
            state.stage(publication, report)

    def _init_heatmap_downsample_warning_button(self) -> None:
        ui = HeatmapResolutionUi(self)
        self.__dict__["_heatmap_resolution_ui"] = ui
        self.heatmap_downsample_button = ui.button
        self._sync_heatmap_resolution_warning()

    def _sync_heatmap_resolution_warning(self) -> None:
        # Hidden overlay sources have no warning widget. Settle their report
        # too, so completed publications release workers and rollback arrays.
        report = self._heatmap_resolution_report
        ui = self.__dict__.get("_heatmap_resolution_ui")
        if ui is not None:
            ui.sync(report)

    def show_heatmap_downsample_dialog(self) -> None:
        self._sync_heatmap_resolution_warning()
        self.__dict__["_heatmap_resolution_ui"].show()

    def eventFilter(self, source, event):
        if (any(source is control for control in self._plot_layout_controls())
                and event.type() in {QtCore.QEvent.Type.Show, QtCore.QEvent.Type.Hide}):
            self._preserve_heatmap_view_range_after_control_layout()
        return super().eventFilter(source, event)


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
            self._sync_secondary_heatmap_view_ranges()
            return

        full_axis_ranges = self._heatmap_full_axis_ranges
        if full_axis_ranges is None:
            self._sync_secondary_heatmap_view_ranges()
            return

        self._viewport_controller().cancel_pending()
        view_ranges = self._heatmap_full_view_ranges or full_axis_ranges
        self._primary_heatmap_viewbox().setRange(
            xRange=view_ranges["x"],
            yRange=view_ranges["y"],
            padding=0,
            )

        self._reload_full_heatmap_data()
        self._sync_secondary_heatmap_view_ranges()


    def _reload_full_heatmap_data(self) -> bool:
        return self._viewport_controller().reload(full=True)


    def _schedule_visible_heatmap_reload(self, *_args: Any) -> None:
        if not self.__dict__.get("_heatmap_layer_view_sync_active", False):
            self._sync_secondary_heatmap_view_ranges()
        if self._large_heatmap_sql_mode and self._has_plottable_heatmap_data():
            self._viewport_controller().schedule_visible()


    def _reload_visible_heatmap_data(self) -> None:
        self._viewport_controller().reload()


    def _visible_heatmap_axis_ranges(self) -> dict[str, tuple[float, float]] | None:
        full_ranges = self._heatmap_full_axis_ranges
        if full_ranges is None:
            return None

        try:
            viewbox = self._primary_heatmap_viewbox()
            # The reload timer may run before the first paint, especially for
            # hidden plots. Resolve pending auto-range before treating the
            # initial empty viewport as a user-selected subset of the data.
            if getattr(viewbox, "_autoRangeNeedsUpdate", False):
                viewbox.updateAutoRange()
            view_x, view_y = viewbox.viewRange()
        except Exception:
            return None

        ranges = AxisRanges.normalise({"x": tuple(view_x), "y": tuple(view_y)})
        full = AxisRanges.normalise(full_ranges)
        visible = ranges.visible_within(full) if ranges is not None and full is not None else None
        return visible.as_dict() if visible is not None else None


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
        if heatmap_display_range_is_finite(geometry):
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

        if (
                self.__dict__.get("_heatmap_color_range_rejected", False)
                or not self.__dict__.get("_primary_heatmap_visible", True)
                ):
            self._hide_heatmap_renderers()
            return

        geometry = self._required_heatmap_geometry()
        # Render a float view; operations, raw cuts and CSV use the exact grid.
        data_grid = np.asarray(self.dataGrid, dtype=float)
        color_levels = self.__dict__.get("_heatmap_publication_color_levels")
        if color_levels is None:
            color_levels = self._current_colorbar_levels()
        if geometry.is_uniform:
            image_options: dict[str, Any] = {"autoLevels": False}
            if color_levels is not None:
                image_options["levels"] = color_levels
            self.image.setImage(data_grid, **image_options)
            self.image.setRect(QtCore.QRectF(*geometry.rect))
            self.heatmap_mesh.hide()
            self.image.show()
            self._schedule_heatmap_view_validation()
            return

        mesh_data = np.asarray(data_grid, dtype=float).copy()
        mesh_data[~np.isfinite(mesh_data)] = np.nan
        x_vertices, y_vertices = np.meshgrid(
            geometry.x.edges,
            geometry.y.edges,
            indexing="xy",
            )
        if color_levels is not None:
            self.heatmap_mesh.setLevels(color_levels, update=False)
        self.heatmap_mesh.setData(
            x_vertices,
            y_vertices,
            mesh_data,
            autoLevels=False,
            )
        self.image.hide()
        self.heatmap_mesh.show()
        self._schedule_heatmap_view_validation()


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
            z_label.setText("")


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
            ) -> tuple[int, int, float, float, float | complex] | None:
        """Return index, recorded coordinates, and value under a point."""

        geometry = self._heatmap_geometry()
        if geometry is None:
            return None
        index = geometry.index_at(x_value, y_value)
        if index is None:
            return None

        x_index, y_index = index
        samples = self.__dict__.get("complex_samples")
        if samples is not None:
            point = samples.cell_index(x_index, y_index)
            if point is None:
                return None
            value = samples.values[point]
            return (x_index, y_index, float(samples.coordinates[0][point]),
                    float(samples.coordinates[1][point]), value.item() if isinstance(value, np.generic) else value)
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


    def _marquee_move_edges(self) -> tuple[tuple[float, ...] | None, tuple[float, ...] | None]:
        geometry = self._heatmap_geometry()
        return (geometry.x.edges, geometry.y.edges) if geometry is not None else (None, None)


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
            "Zoom colour",
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

        values = selected[numeric_isfinite(selected)]
        if values.size == 0:
            return None

        if self.marquee is None:
            return None

        rows, cols = selected.shape
        rect = self._snap_marquee_rect(self.marquee.normalized())
        return self._format_marquee_stats_text(
            f"Selected grid: {cols} × {rows}", values, rect, heatmap=True,
            contribution=f"{values.size} of {selected.size} selected "
                         f"{'cells' if selected.size != 1 else 'cell'} contributed "
                         "finite values from the displayed primary heatmap.",
        )


    def _marquee_color_levels(self) -> tuple[float, float] | None:
        selected = self._marquee_selected_data()
        if selected is None:
            return None

        values = selected[numeric_isfinite(selected)]
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
        selected = np.asarray(self.dataGrid[row_slice, col_slice])
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
