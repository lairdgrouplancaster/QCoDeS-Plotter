from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import numpy as np
from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from qplot.windows._dataset_handle import DatasetKey, TraceKey
from qplot.windows._plotWin import plotWidget
from qplot.windows._widgets import (
    expandingComboBox,
    picker_1d,
)


@contextmanager
def _blocked_signals(widget: Any) -> Iterator[None]:
    """Block one widget only for the duration of a synchronous update."""

    signals_were_blocked = widget.blockSignals(True)
    try:
        yield
    finally:
        widget.blockSignals(bool(signals_were_blocked))


@dataclass(frozen=True)
class _OverlayState:
    visible: bool
    title: str
    detail: str
    detail_visible: bool
    stylesheet: str


@dataclass(frozen=True)
class _CutAxisState:
    """One coherent, committed cut-axis UI and display snapshot."""

    sweep_indep: str
    fixed_indep: str
    axis_selection: dict[str, str]
    slider_range: tuple[int, int]
    slider_index: int
    slider_enabled: bool
    text: str
    fixed_index: int
    fixed_value: float
    fixed_data: np.ndarray
    axis_data: dict[str, np.ndarray]
    axis_param: dict[str, Any]
    data_grid: np.ndarray
    display_param: Any
    overlay: _OverlayState | None
    refresh_pending: bool
    refresh_pending_force: bool


@dataclass
class _AxisChangeTransaction:
    """A requested axis pair plus the coherent state it supersedes."""

    identifier: int
    prior: _CutAxisState
    requested_sweep: str
    requested_fixed: str
    requested_index: int = 0
    worker: Any | None = None
    owns_pending_refresh: bool = False
    finalized: bool = False


class sweeper(plotWidget):
    """
    A plotWidget which displays a 1d sweep on an 2d plot.
    Produced throught he context menu of the plotItem in plot2d.
    
    Produces a cursor on its source plot2d to display the location of the sweep.
    Both plots become linked, any changes to the cursor or sweep will update
    their counterpart.
    """
    operation_kind = "sweeper"
    sweep_moved = QtCore.pyqtSignal([int, str, str, float, object])
    merge_compatibility_changed = QtCore.pyqtSignal()
    remove_sweep = QtCore.pyqtSignal([int])
    
    def __init__(self,
                 dataset_key: DatasetKey,  # Had to handle separately to *args
                 sweep_id : int,
                 sweep_indep : str,
                 fixed_indep : str, 
                 fixed_value : float,
                 *args, 
                 **kargs
                 ):
        self.sweep_id = sweep_id
        self.sweep_indep = sweep_indep
        self.fixed_indep = fixed_indep
        self.fixed_value = float(fixed_value)
        self.fixed_index = 0
        self._axis_change_serial = 0
        self._axis_change_transaction: _AxisChangeTransaction | None = None
        
        self.line: Any = None
        
        super().__init__(dataset_key, *args, **kargs)
        
        
    def _set_cut_trace_identity(self) -> None:
        """Give this cut a unique internal key and a distinguishable label."""

        self.label = f"{self.label} [cut {self.sweep_id + 1}]"
        self._trace_key = TraceKey(
            self._dataset_key,
            self.param.name,
            sweep_id=self.sweep_id,
            )


    def initAxes(self):
        """
        Adds to left toolbar to allow for sweep parameter control

        """
        self._set_cut_trace_identity()

        # Got back to default before line picker
        super().initAxes()
        
        # Set correct display on axis picker
        with _blocked_signals(self.axis_dropdown["x"]):
            self.axis_dropdown["x"].setCurrentIndex(
                self.axis_dropdown["x"].findText(self.sweep_indep)
                )
        
        # Disable y axis box, for display only
        with _blocked_signals(self.axis_dropdown["y"]):
            self.axis_dropdown["y"].setEditable(True)
            y_axis_line_edit = self.axis_dropdown["y"].lineEdit()
            if y_axis_line_edit is not None:
                y_axis_line_edit.setReadOnly(True)
            self.axis_dropdown["y"].setDisabled(True)
            self.axis_dropdown["y"].setCurrentText(self.param.name)
        
        # add line control
        main_line = picker_1d(self, self.config, [self.label])
        main_line.option_box.setCurrentIndex(0)
        main_line.option_box.setDisabled(True)
        main_line.del_box.setDisabled(True)
        main_line.axis_side.setDisabled(True)
        main_line.color_box.setColor(self.config.theme.colors[0])
        main_line.color_box.selectedColor.connect(
            lambda col: self.line.setPen(col)
            )
        main_line.color_box.selectedColor.connect( # emit update to main
            lambda _: self.update_sweep()
            )
        self.axes_dock.addWidget(main_line)
        main_line.adjustSize()
        
        # Add picker for changing sweep location using x axis options since
        # y param is not in that
        self.picker = fixed_var_picker(
            self, 
            [self.axis_dropdown["x"].itemText(i) for i in range(self.axis_dropdown["x"].count())],
            )
        self.axes_dock.addWidget(self.picker)
        
        # Set up picker options
        self.picker.option_box.setCurrentIndex(
            self.picker.option_box.findText(self.fixed_indep)
            )
        self._axis_selection = {
            "x": self.sweep_indep,
            "y": self.fixed_indep,
            }
        self.picker.option_box.currentIndexChanged.connect(self.change_fixed_param)
        self.picker.slider.valueChanged.connect(self.change_index)
        self.picker.slider.setEnabled(False)
        
        # Push all widgets to top
        self.axes_dock.content_layout.addStretch()
        
        
    def initFrame(self):
        """
        Sets up the initial plot and starting data.
        
        Note, is copy of plot1d.initFrame

        """
        self.line = self.plot.plot(connect="all")
        
        # Wait for loader to finish to enure needed data is collected.
        self.load_data()
        self.show_status("Cut plot ready; loading data...", 5000)


    def _capture_overlay_state(self) -> _OverlayState | None:
        overlay = self.__dict__.get("plot_state_overlay")
        if overlay is None:
            return None
        return _OverlayState(
            visible=not overlay.frame.isHidden(),
            title=overlay.title_label.text(),
            detail=overlay.detail_label.text(),
            detail_visible=not overlay.detail_label.isHidden(),
            stylesheet=overlay.frame.styleSheet(),
            )


    def _capture_axis_state(self) -> _CutAxisState:
        slider = self.picker.slider
        axis_data = {
            key: np.array(value, copy=True)
            for key, value in self.__dict__.get("axis_data", {}).items()
            }
        return _CutAxisState(
            sweep_indep=self.sweep_indep,
            fixed_indep=self.fixed_indep,
            axis_selection=dict(
                self.__dict__.get(
                    "_axis_selection",
                    {"x": self.sweep_indep, "y": self.fixed_indep},
                    )
                ),
            slider_range=(slider.minimum(), slider.maximum()),
            slider_index=slider.value(),
            slider_enabled=slider.isEnabled(),
            text=self.picker.text_box.text(),
            fixed_index=self.fixed_index,
            fixed_value=float(self.fixed_value),
            fixed_data=np.array(
                self.__dict__.get("fixed_indep_data", np.asarray([])),
                copy=True,
                ),
            axis_data=axis_data,
            axis_param=dict(self.__dict__.get("axis_param", {})),
            data_grid=np.array(
                self.__dict__.get("dataGrid", np.empty((0, 0))),
                copy=True,
                ),
            display_param=self.__dict__.get("display_param"),
            overlay=self._capture_overlay_state(),
            refresh_pending=bool(self.__dict__.get("_refresh_pending", False)),
            refresh_pending_force=bool(
                self.__dict__.get("_refresh_pending_force", False)
                ),
            )


    @staticmethod
    def _set_combo_text(combo: Any, text: str) -> None:
        with _blocked_signals(combo):
            index = combo.findText(text)
            if index >= 0:
                combo.setCurrentIndex(index)
            else:
                combo.setCurrentText(text)


    def _set_axis_controls(self, sweep_indep: str, fixed_indep: str) -> None:
        self._set_combo_text(self.axis_dropdown["x"], sweep_indep)
        self._set_combo_text(self.picker.option_box, fixed_indep)


    def _set_slider_state(
            self,
            slider_range: tuple[int, int],
            index: int,
            enabled: bool,
            ) -> None:
        slider = self.picker.slider
        with _blocked_signals(slider):
            slider.setRange(*slider_range)
            set_value = getattr(slider, "setValue", None)
            if callable(set_value):
                set_value(index)
            slider.setEnabled(enabled)


    def _restore_overlay_state(self, state: _OverlayState | None) -> None:
        if state is None:
            return
        overlay = self.__dict__.get("plot_state_overlay")
        if overlay is None:
            return
        overlay.title_label.setText(state.title)
        overlay.detail_label.setText(state.detail)
        overlay.detail_label.setVisible(state.detail_visible)
        overlay.frame.setStyleSheet(state.stylesheet)
        if state.visible:
            overlay.frame.show()
            overlay.frame.raise_()
        else:
            overlay.frame.hide()


    def _restore_axis_state(
            self,
            transaction: _AxisChangeTransaction,
            *,
            preserve_overlay: bool,
            ) -> None:
        state = transaction.prior
        self._set_axis_controls(state.sweep_indep, state.fixed_indep)
        self.sweep_indep = state.sweep_indep
        self.fixed_indep = state.fixed_indep
        self._axis_selection = dict(state.axis_selection)
        self.fixed_index = state.fixed_index
        self.fixed_value = state.fixed_value
        self.fixed_indep_data = np.array(state.fixed_data, copy=True)
        self.axis_data = {
            key: np.array(value, copy=True)
            for key, value in state.axis_data.items()
            }
        self.axis_param = dict(state.axis_param)
        self.dataGrid = np.array(state.data_grid, copy=True)
        self.display_param = state.display_param
        self._set_slider_state(
            state.slider_range,
            state.slider_index,
            state.slider_enabled,
            )
        self.picker.text_box.setText(state.text)

        line = self.__dict__.get("line")
        if line is not None:
            line.setData(
                x=self.axis_data.get("x", np.asarray([])),
                y=self.axis_data.get("y", np.asarray([])),
                )
        set_labels = getattr(self, "_set_param_axis_labels", None)
        if callable(set_labels) and {"x", "y"}.issubset(self.axis_param):
            set_labels()
        if not preserve_overlay:
            self._restore_overlay_state(state.overlay)

        if transaction.owns_pending_refresh and transaction.worker is None:
            self._refresh_pending = state.refresh_pending
            self._refresh_pending_force = state.refresh_pending_force


    def _commit_axis_controls(self, transaction: _AxisChangeTransaction) -> None:
        self._set_axis_controls(
            transaction.requested_sweep,
            transaction.requested_fixed,
            )
        self.sweep_indep = transaction.requested_sweep
        self.fixed_indep = transaction.requested_fixed
        self._axis_selection = {
            "x": transaction.requested_sweep,
            "y": transaction.requested_fixed,
            }


    def _commit_loaded_cut(self, transaction: _AxisChangeTransaction) -> None:
        x_data = np.asarray(self.axis_data.get("x", []))
        fixed_data = np.asarray(self.axis_data.get("y", []))
        data_grid = np.asarray(self.dataGrid)
        row_count = min(fixed_data.size, data_grid.shape[0])
        column_count = min(x_data.size, data_grid.shape[1])

        self._commit_axis_controls(transaction)
        self.fixed_indep_data = fixed_data[:row_count]
        self.axis_data["x"] = x_data[:column_count]
        self.dataGrid = data_grid[:row_count, :column_count]
        self.fixed_index = min(transaction.requested_index, row_count - 1)
        self.fixed_value = float(self.fixed_indep_data[self.fixed_index])
        self.axis_param["y"] = getattr(self, "display_param", self.param)
        self._set_slider_state((0, row_count - 1), self.fixed_index, True)
        self.picker.text_box.setText(
            self.formatNum(self.fixed_indep_data[self.fixed_index])
            )
        self._set_param_axis_labels()
        self.update_sweep()
        self.merge_compatibility_changed.emit()


    def _commit_empty_cut(self, transaction: _AxisChangeTransaction) -> None:
        self._commit_axis_controls(transaction)
        self.fixed_indep_data = np.asarray([])
        self.axis_data = {"x": np.asarray([]), "y": np.asarray([])}
        self.dataGrid = np.empty((0, 0))
        self.fixed_index = 0
        self.fixed_value = 0.0
        if "y" in self.axis_param:
            self.axis_param["y"] = getattr(self, "display_param", self.param)
        self._set_slider_state((0, 0), 0, False)
        self.picker.text_box.setText("")
        self.line.setData([], [])
        if {"x", "y"}.issubset(self.axis_param):
            self._set_param_axis_labels()
        self.trace_updated.emit()
        self.merge_compatibility_changed.emit()


    def _finalize_axis_change(
            self,
            transaction: _AxisChangeTransaction,
            outcome: str,
            *,
            preserve_overlay: bool = False,
            ) -> bool:
        """Commit or roll back the current transaction exactly once."""

        if transaction.finalized:
            return False
        if self.__dict__.get("_axis_change_transaction") is not transaction:
            return False

        transaction.finalized = True
        self._axis_change_transaction = None
        try:
            if outcome == "success":
                self._commit_loaded_cut(transaction)
            elif outcome == "empty":
                self._commit_empty_cut(transaction)
            else:
                self._restore_axis_state(
                    transaction,
                    preserve_overlay=preserve_overlay,
                    )
        except Exception:
            if outcome in {"success", "empty"}:
                self._restore_axis_state(transaction, preserve_overlay=False)
            raise
        finally:
            # No transaction is allowed to own a blocked slider after returning.
            self.picker.slider.blockSignals(False)
        return True


    def _active_transaction_for_worker(
            self,
            worker: Any,
            ) -> _AxisChangeTransaction | None:
        transaction = self.__dict__.get("_axis_change_transaction")
        if transaction is None or transaction.worker is not worker:
            return None
        return transaction


    def _refresh_worker_will_start(self, worker: Any) -> None:
        transaction = self.__dict__.get("_axis_change_transaction")
        if transaction is not None and transaction.worker is None:
            transaction.worker = worker


    def load_data(self, *args: Any, **kwargs: Any) -> bool:
        transaction = self.__dict__.get("_axis_change_transaction")
        try:
            launched = super().load_data(*args, **kwargs)
        except Exception:
            if transaction is not None:
                self._finalize_axis_change(transaction, "failure")
            raise

        if (
                not launched
                and transaction is not None
                and self.__dict__.get("_axis_change_transaction") is transaction
                ):
            self._finalize_axis_change(
                transaction,
                "failure",
                preserve_overlay=True,
                )
        return launched


    def _source_database_is_current(self) -> bool:
        is_current = super()._source_database_is_current()
        if not is_current:
            transaction = self.__dict__.get("_axis_change_transaction")
            if transaction is not None:
                self._finalize_axis_change(
                    transaction,
                    "failure",
                    preserve_overlay=True,
                    )
        return is_current


    def err_raiser(self, err: Exception, worker: Any | None = None) -> None:
        transaction = self._active_transaction_for_worker(worker)
        cancelled = bool(
            worker is not None
            and getattr(worker, "is_cancelled", lambda: False)()
            )
        try:
            super().err_raiser(err, worker=worker)
        finally:
            if transaction is not None:
                self._finalize_axis_change(
                    transaction,
                    "failure",
                    preserve_overlay=not cancelled,
                    )


    @QtCore.pyqtSlot(bool)
    def refreshPlot(self, finished : bool = True, worker=None):
        """
        Event handler for worker callback
        Fetches values from data from worker in super().refreshPlot
        
        Updates display (see self.update_sweep) and slider as needed

        Parameters
        ----------
        finished : bool
            In the event the worker had to abort, finished is False and refresh
            is not ran.

        """
        plot_worker = worker if worker is not None else self.worker
        transaction = self._active_transaction_for_worker(plot_worker)
        try:
            refreshed = super().refreshPlot(finished, worker=worker)
        except Exception:
            if transaction is not None:
                self._finalize_axis_change(transaction, "failure")
            raise

        if not refreshed:
            if transaction is not None:
                cancelled = bool(
                    getattr(plot_worker, "is_cancelled", lambda: False)()
                    )
                self._finalize_axis_change(
                    transaction,
                    "failure",
                    preserve_overlay=bool(not cancelled and not finished),
                    )
            return

        try:
            x_data = np.asarray(self.axis_data.get("x", []))
            fixed_data = np.asarray(self.axis_data.get("y", []))
            data_grid = np.asarray(self.dataGrid)
            has_cut_data = (
                x_data.size > 0
                and fixed_data.size > 0
                and data_grid.ndim == 2
                and data_grid.shape[0] > 0
                and data_grid.shape[1] > 0
                )
            if transaction is not None:
                outcome = "success" if has_cut_data else "empty"
                self._finalize_axis_change(transaction, outcome)
            elif not has_cut_data:
                self.fixed_indep_data = np.asarray([])
                self.axis_data = {"x": np.asarray([]), "y": np.asarray([])}
                self.dataGrid = np.empty((0, 0))
                self.fixed_index = 0
                self._set_slider_state((0, 0), 0, False)
                self.picker.text_box.setText("")
                self.line.setData([], [])
                self.trace_updated.emit()
            else:
                row_count = min(fixed_data.size, data_grid.shape[0])
                column_count = min(x_data.size, data_grid.shape[1])
                self.fixed_indep_data = fixed_data[:row_count]
                self.axis_data["x"] = x_data[:column_count]
                self.dataGrid = data_grid[:row_count, :column_count]
                self.fixed_index = self._fixed_index_for_value(self.fixed_value)
                self.fixed_value = float(self.fixed_indep_data[self.fixed_index])
                self.axis_param["y"] = getattr(self, "display_param", self.param)
                self._set_slider_state(
                    (0, row_count - 1),
                    self.fixed_index,
                    True,
                    )
                self.picker.text_box.setText(
                    self.formatNum(self.fixed_indep_data[self.fixed_index])
                    )
                self._set_param_axis_labels()
                self.update_sweep()

            if not has_cut_data:
                self.show_status(
                    f"Waiting for plottable data for {self.param.name}...",
                    5000,
                    )
                self.show_plot_state(
                    "Waiting for plottable cut data",
                    f"{self.param.name} has no cut data yet.",
                    kind="empty",
                    )
            self._mark_display_synchronized(plot_worker)
        except Exception:
            active = self._active_transaction_for_worker(plot_worker)
            if active is not None:
                self._finalize_axis_change(active, "failure")
            raise
        finally:
            plot_worker.running = False
            self.picker.slider.blockSignals(False)
            self._ensure_refresh_monitor()
        
        
    @property
    def axis_options(self) -> dict:
        """
        Alter axis_options for correct data fetch from worker

        Returns
        -------
        dict
            The required axes data.

        """
        return {"x": self.axis_dropdown["x"].currentText(), "y": self.picker.option_box.currentText()}
        
    
    @QtCore.pyqtSlot(bool)
    def closeEvent(self, event):
        """
        Post close admin, emits to 2d main plot  to remove sweep cursor

        Parameters
        ----------
        unused byt reauired by slot
        
        """
        transaction = self.__dict__.get("_axis_change_transaction")
        if transaction is not None:
            self._finalize_axis_change(transaction, "failure")
        try:
            super().closeEvent(event)
            self.remove_sweep.emit(self.sweep_id)
        finally:
            self.picker.slider.blockSignals(False)
        
###############################################################################
# Events/Slots
    
    def update_sweep(self, emit = True):
        """
        Refresh 1d plot when there is a change in parameter or value
        
        Parameters
        ----------
        emit : bool, optional
            Whether to emit a signal to parent 2d plot. The default is True.

        """
        # Get correct row for y data
        fixed_values = np.asarray(getattr(self, "fixed_indep_data", []))
        if fixed_values.size > self.fixed_index:
            self.fixed_value = float(fixed_values[self.fixed_index])
        elif not hasattr(self, "fixed_value"):
            self.fixed_value = float(self.fixed_index)
        self.axis_data["y"] = self.dataGrid[self.fixed_index, :]
        
        # update line
        self.line.setData(
            x=self.axis_data["x"], 
            y=self.axis_data["y"],
            )
        self.trace_updated.emit()
        
        if emit:
            # Tell source graph to update scan line on source graph
            self.sweep_moved.emit(
                self.sweep_id,
                *self.axis_options.values(),
                self.fixed_value,
                self.line.opts['pen']
                )


    @QtCore.pyqtSlot(int)
    def change_index(self, index):
        """
        Event handler for picker.slider changing value
        Changes index value of the fixed parameter and refreshes plot.

        Parameters
        ----------
        index : int
            Value slider was changed to.

        """
        # Update display box
        self.picker.text_box.setText(
            self.formatNum(self.fixed_indep_data[index])
            )
        
        # Update plot
        self.fixed_index = index
        self.fixed_value = float(self.fixed_indep_data[index])
        self.update_sweep()
        
        self._set_param_axis_labels()


    def _cancel_current_refresh(self) -> None:
        worker = self.__dict__.get("worker")
        if worker is None or not getattr(worker, "running", False):
            return
        if getattr(worker, "is_cancelled", lambda: False)():
            return
        cancel = getattr(worker, "cancel", None)
        if callable(cancel):
            cancel()


    def _supersede_axis_change(self) -> None:
        transaction = self.__dict__.get("_axis_change_transaction")
        if transaction is None:
            return
        worker = transaction.worker
        self._finalize_axis_change(transaction, "failure")
        if worker is not None and getattr(worker, "running", False):
            cancel = getattr(worker, "cancel", None)
            if callable(cancel):
                cancel()


    def _begin_axis_change(
            self,
            requested_sweep: str,
            requested_fixed: str,
            ) -> None:
        self._supersede_axis_change()
        try:
            prior = self._capture_axis_state()
        except Exception:
            self._set_axis_controls(self.sweep_indep, self.fixed_indep)
            self.picker.slider.blockSignals(False)
            raise
        self._axis_change_serial += 1
        transaction = _AxisChangeTransaction(
            identifier=self._axis_change_serial,
            prior=prior,
            requested_sweep=requested_sweep,
            requested_fixed=requested_fixed,
            )
        self._axis_change_transaction = transaction
        try:
            self._set_axis_controls(requested_sweep, requested_fixed)
            with _blocked_signals(self.picker.slider):
                self.picker.slider.setEnabled(False)
            self.picker.slider.blockSignals(False)
            self.show_plot_state(
                "Updating cut axes",
                f"Loading {requested_sweep} at fixed {requested_fixed}.",
                kind="loading",
                )
            self._cancel_current_refresh()
            self.refreshWindow(force=True)
        except Exception:
            self._finalize_axis_change(transaction, "failure")
            raise

        if self.__dict__.get("_axis_change_transaction") is not transaction:
            return
        if transaction.worker is not None:
            return
        if self.__dict__.get("_refresh_pending", False):
            transaction.owns_pending_refresh = not prior.refresh_pending
            return
        self._finalize_axis_change(transaction, "failure")
            
    
    @QtCore.pyqtSlot(int)
    def change_fixed_param(self, index):
        """
        Event handler for fixed parameter dropdown selector. (picker.option_box)
        Updates the parameter on the x axis of the sweep and resets fixed 
        parameter index to 0.
        If the parameter changed to is the current sweep parameter, switches 
        them.

        Parameters
        ----------
        Unused but required by slot

        """
        del index
        requested_fixed = self.picker.option_box.currentText()
        requested_sweep = self.axis_dropdown["x"].currentText()
        transaction = self.__dict__.get("_axis_change_transaction")
        previous_fixed = (
            transaction.requested_fixed
            if transaction is not None
            else self.fixed_indep
            )
        if requested_fixed == requested_sweep:
            requested_sweep = previous_fixed
        self._begin_axis_change(requested_sweep, requested_fixed)
            
    
    @QtCore.pyqtSlot()
    def change_axis(self, key : str):
        """
        Event handler for x axis dropdown selector.
        Updates the parameter on the x axis of the sweep and resets fixed 
        parameter index to 0.
        If the parameter changed to is the current fixed parameter, switches 
        them.
        

        Parameters
        ----------
        key : str
            The key of which box to change. Will be x in all cases but required
            by definition in parent

        """
        del key
        requested_sweep = self.axis_dropdown["x"].currentText()
        requested_fixed = self.picker.option_box.currentText()
        transaction = self.__dict__.get("_axis_change_transaction")
        previous_sweep = (
            transaction.requested_sweep
            if transaction is not None
            else self.sweep_indep
            )
        if requested_sweep == requested_fixed:
            requested_fixed = previous_sweep
        self._begin_axis_change(requested_sweep, requested_fixed)
            
            
    @QtCore.pyqtSlot(int, float)
    def update_sweep_line(self, sweep_id, fixed_value):
        """
        Event handler for moving sweep cursor on source plot.

        Parameters
        ----------
        sweep_id : int
            The sweep id of the line moved. Confirms that this is the intened
            plot to adjust
        fixed_value : float
            The physical fixed-axis coordinate selected on the source plot.

        """
        if sweep_id != self.sweep_id:
            return

        self.fixed_value = float(fixed_value)
        if not getattr(self, "fixed_indep_data", np.asarray([])).size:
            return
        self.fixed_index = self._fixed_index_for_value(self.fixed_value)
        
        with _blocked_signals(self.picker.slider):
            self.picker.slider.setValue(self.fixed_index)
        self.picker.text_box.setText(
            self.formatNum(self.fixed_indep_data[self.fixed_index])
            )
        
        self.update_sweep(emit = False)


    def _fixed_index_for_value(self, value: float) -> int:
        values = np.asarray(self.fixed_indep_data, dtype=float)
        finite = np.flatnonzero(np.isfinite(values))
        if finite.size == 0:
            return 0
        nearest = int(np.argmin(np.abs(values[finite] - float(value))))
        return int(finite[nearest])

    
class fixed_var_picker(qtw.QWidget):
    """
    A custom QWidget which contains other widgets to interact with and control
    the static/fixed parameter of the heat map while viewing the 1d sweep.
    
    Contains:
        self.option_box: Changing the fixed parameter
        self.slider: change which value the plot is looking at
        self.text_box: visual display of current sweep value
        
    Note, uses custom HBoxLayout to set up context menu within dock widget.
    See qplot.windows._widgets.toolbar
    """
    
    
    def __init__(self, main, items):
        super().__init__()
        
        layout = qtw.QVBoxLayout(self)
        
        # Set up layouts with customised context menus
        row_1 = main.axes_dock.HBox_context(main.axes_dock.event_filter)
        row_2 = main.axes_dock.HBox_context(main.axes_dock.event_filter)
        
        row_1.addWidget(qtw.QLabel("Fixed Varaible: "))
        
        # Create box to change paramater
        self.option_box = expandingComboBox()
        self.option_box.addItems(items)
        row_1.addWidget(self.option_box)
        
        # Switches fixed parameter
        self.slider = qtw.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.slider.setTickPosition(qtw.QSlider.TickPosition.TicksBelow)
        row_2.addWidget(self.slider)
        
        # Update user to change
        self.text_box = qtw.QLineEdit()
        self.text_box.setReadOnly(True)
        self.text_box.setMaximumWidth(main._label_width)
        row_2.addWidget(self.text_box)

        layout.addLayout(row_1)
        layout.addLayout(row_2)
