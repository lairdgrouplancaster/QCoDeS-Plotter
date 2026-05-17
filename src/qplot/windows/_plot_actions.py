import os

import numpy as np
import pandas as pd
from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from qplot.datahandling.readonly import load_by_guid_read_only, load_by_id_read_only
from qplot.diagnostics import log_exception

from .plot1d import plot1d
from .plot2d import plot2d


class PlotActionsMixin:
    """
    Plot launching, preview plotting, CSV export, and plot dataset tracking.

    The mixin expects the owning window to provide MainWindow's widgets and
    state, including windows, dataset_holder, config, threadPool, and status
    helpers.
    """

    @QtCore.pyqtSlot(object)
    def onClose(self, win):
        """
        Event handler for closing a Plot window.

        """
        self.windows.remove(win)
        self.remove_ds_at(win._guid)
        self.post_admin()
        self.show_status(f"Closed {win.label}", 3000)
        del win


    @QtCore.pyqtSlot(object, str, tuple)
    def openWin(self, widget, guid_or_ds, *args, show=True, **kargs):
        """
        Handles opening Plot window, widget.

        """
        if len(args) == 1 and isinstance(args[0], (tuple, list)):
            args = tuple(args[0])

        if isinstance(guid_or_ds, str):
            ds = None
            guid = guid_or_ds
        else:
            ds = guid_or_ds
            guid = ds.guid

        self.add_ds_at(guid, ds=ds)

        win = widget(
            guid,
            *args,
            self.config,
            self.threadPool,
            self.dataset_holder,
            show=show,
            **kargs,
        )

        self.windows.append(win)

        win.closed.connect(self.onClose)
        win.make_ds.connect(self.add_ds_at)
        win.previewTraceDropRequested.connect(self.add_dropped_preview_to_plot)
        if win.__class__.__name__ == "plot1d":
            win.get_mergables.connect(lambda: self.get_1d_wins(win))
            win.remove_dataset.connect(self.remove_ds_at)

        elif win.__class__.__name__ == "plot2d":
            win.open_subplot.connect(self.openWin)
            win.close_sweeps_requested.connect(self.close_sweeps_from_plot)

        elif win.__class__.__name__ == "sweeper":
            for item in self.windows:
                if item.ds == win.ds and item.param == win.param and isinstance(item, plot2d):
                    win.sweep_moved.connect(item.update_sweep_line)
                    win.remove_sweep.connect(item.remove_sweep)
                    item.sweep_moved.connect(win.update_sweep_line)
                    break

        else:
            raise TypeError(f"Unknown window of type: {win.__class__.__name__}")

        if show:
            win.update_theme(self.config)

            win.move(self.x, self.y)
            win.show()

            tolerance = 30
            self.x += win.width
            if self.x + win.width - tolerance > self.screenrect.right():
                self.x = self.screenrect.left()
                self.y += win.height

                if self.y + win.height - tolerance > self.screenrect.bottom():
                    self.y = self.screenrect.top()


    @QtCore.pyqtSlot(object, object)
    def close_sweeps_from_plot(self, source_win, sweep_ids):
        """
        Close cut windows associated with a heatmap cut-line action.

        """
        target_ids = set(sweep_ids)
        if not target_ids:
            return

        for item in list(self.windows):
            if item.__class__.__name__ != "sweeper":
                continue

            try:
                same_source = item.ds == source_win.ds and item.param == source_win.param
                should_close = same_source and item.sweep_id in target_ids
            except AttributeError:
                continue

            if should_close:
                item.close()


    @QtCore.pyqtSlot(str)
    def updateSelected(self, guid):
        """
        Loads the selected run into memory and updates the details pane.

        """
        self.show_status("Loading selected run...", 0)
        try:
            if self.dataset_holder.get(guid, 0) == 0:
                self.ds = load_by_guid_read_only(guid)
            else:
                self.ds = self.dataset_holder[guid]["dataset"]
        except Exception as err:
            log_exception("Selected run load failed", err, __name__)
            self.show_error("Run Load Failed", f"Could not load run with GUID {guid}.", str(err))
            return

        self.selected_run_id = self.ds.run_id
        self.run_idBox.blockSignals(True)
        self.run_idBox.setText(str(self.ds.run_id))
        self.run_idBox.blockSignals(False)

        if hasattr(self.ds, "snapshot"):
            snap = self.ds.snapshot
        else:
            snap = None

        paramspec = self.ds.get_parameters()
        structure = {"Data points": self.ds.number_of_results}
        for param in paramspec:
            if len(param.depends_on) > 0:
                structure[param.name] = {
                    "unit": param.unit,
                    "label": param.label,
                    "axes": list(param.depends_on_),
                }
            else:
                structure[param.name] = {
                    "unit": param.unit,
                    "label": param.label,
                }
        info = {
            "Data Structure": structure,
            "MetaData": self.ds.metadata,
            "Snapshot": snap,
        }
        self.infoBox.setInfo(info, self.ds)
        self.show_status(
            f"Selected run {self.ds.run_id} with {self.ds.number_of_results:,} points.",
            5000,
        )


    @QtCore.pyqtSlot()
    def openRun(self):
        """
        Plots the requested measurement for the requested run.

        """
        ds = self._dataset_for_plot_target()
        if ds is None:
            return

        params = self._selected_measurement_params(ds)
        if params is None:
            return

        self.ds = ds
        self.openPlot(params=params)


    @QtCore.pyqtSlot()
    def open_selected_run_all(self):
        """
        Opens every plottable measurement in the currently selected table row.

        """
        if self.ds is None:
            self.show_status("Select a run before plotting all measurements.", 5000)
            return

        self.openPlot()


    @QtCore.pyqtSlot()
    def exportRunCsv(self):
        """
        Exports the requested run and measurement data to a CSV file.

        """
        ds = self._dataset_for_plot_target()
        if ds is None:
            return

        params = self._selected_measurement_params(ds)
        if params is None:
            return

        self._export_measurement_csv(ds, params)


    @QtCore.pyqtSlot(str)
    def export_preview_csv(self, parameter_name):
        """
        Exports the measurement represented by a selected-run preview image.

        """
        if not self.ds:
            self.show_status("Select a run before exporting a preview.", 5000)
            return

        self._export_preview_csv(self.ds, parameter_name)


    @QtCore.pyqtSlot(str, str)
    def export_run_preview_csv(self, guid, parameter_name):
        """
        Exports the measurement represented by a run-table preview image.

        """
        if not guid:
            self.show_status("Select a run before exporting a preview.", 5000)
            return

        try:
            ds = self._dataset_for_guid(guid)
        except Exception as err:
            log_exception("Preview CSV run load failed", err, __name__)
            self.show_error("Run Load Failed", f"Could not load run with GUID {guid}.", str(err))
            return

        self._export_preview_csv(ds, parameter_name)


    def _export_preview_csv(self, dataset, parameter_name):
        param = self._measurement_param_by_name(dataset, parameter_name)
        if param is None:
            self.show_status(f"No preview export found for {parameter_name}.", 5000)
            return

        self._export_measurement_csv(dataset, [param])


    def _export_measurement_csv(self, ds, params):
        if not params:
            self.show_status("No plottable measurements to export for this run.", 5000)
            return

        default_name = self._default_export_filename(ds, params)
        filename = qtw.QFileDialog.getSaveFileName(
            self,
            "Export CSV",
            default_name,
            "CSV files (*.csv)",
        )[0]
        if not filename:
            self.show_status("CSV export cancelled.", 3000)
            return
        if not filename.lower().endswith(".csv"):
            filename = f"{filename}.csv"

        try:
            frame = self._measurement_dataframe(ds, params)
            frame.to_csv(filename, index=False)
        except Exception as err:
            log_exception("CSV export failed", err, __name__)
            self.show_error(
                "CSV Export Failed",
                "Could not export the selected measurement data.",
                str(err),
            )
            return

        self.show_status(f"Exported CSV: {filename}", 5000)


    @QtCore.pyqtSlot(str)
    def openPlot(self, guid: str = None, params: list = None, show: bool = True):
        """
        Opens plot windows for the selected or requested run.

        """
        if not self.ds and not guid:
            self.show_status("Select a run before opening plots.", 5000)
            return

        self.show_status("Opening plots...", 0)

        try:
            if not self.ds or (guid and self.ds.guid != guid):
                if self.dataset_holder.get(guid, 0) == 0:
                    ds = load_by_guid_read_only(guid)
                else:
                    ds = self.dataset_holder[guid]["dataset"]
            else:
                ds = self.ds
        except Exception as err:
            log_exception("Plot run load failed", err, __name__)
            self.show_error("Run Load Failed", "Could not load the selected run.", str(err))
            return

        if not params:
            params = ds.get_parameters()

        opened = 0
        skipped = 0
        try:
            for param in params:
                if param.depends_on == "":
                    continue

                depends_on = param.depends_on_
                skip = False

                if len(depends_on) == 1:
                    for win in self.windows:
                        if win.ds == ds and win.param == param and isinstance(win, plot1d):
                            skipped += 1
                            skip = True
                            break
                    if skip:
                        continue

                    self.openWin(
                        plot1d,
                        ds,
                        param,
                        refrate=self.spinBox.value(),
                        show=show,
                    )
                    opened += 1

                else:
                    for win in self.windows:
                        if win.ds == ds and win.param == param and isinstance(win, plot2d):
                            skipped += 1
                            skip = True
                            break
                    if skip:
                        continue

                    self.openWin(
                        plot2d,
                        ds,
                        param,
                        refrate=self.spinBox.value(),
                        show=show,
                    )
                    opened += 1

            self.post_admin()

            if opened:
                noun = "plot" if opened == 1 else "plots"
                self.show_status(f"Opened {opened} {noun}.", 5000)
            elif skipped:
                self.show_status("Selected plot windows are already open.", 5000)
            else:
                self.show_status("No plottable parameters found for this run.", 5000)

        except Exception as err:
            try:
                ds.conn.close()
            except Exception:
                pass
            log_exception("Plot open failed", err, __name__)
            self.show_error("Plot Open Failed", "Could not open plot windows.", str(err))


    def open_param_by_index(self, index: int):
        """
        Open the indexed dependent parameter for the selected run.

        """
        if not self.ds:
            self.show_status("Select a run before opening a parameter.", 5000)
            return

        params = [param for param in self.ds.get_parameters() if param.depends_on != ""]
        if index >= len(params):
            self.show_status(f"Run has no parameter {index + 1}.", 5000)
            return

        self.openPlot(params=[params[index]])


    @QtCore.pyqtSlot(str)
    def open_preview_plot(self, parameter_name):
        """
        Open the plot represented by a double-clicked preview image.

        """
        if not self.ds:
            self.show_status("Select a run before opening a preview plot.", 5000)
            return

        for param in self.ds.get_parameters():
            if param.name == parameter_name and param.depends_on != "":
                self.openPlot(params=[param])
                return

        self.show_status(f"No preview plot found for {parameter_name}.", 5000)


    @QtCore.pyqtSlot(str, str)
    def open_run_preview_plot(self, guid, parameter_name):
        """
        Open the plot represented by a double-clicked run-table preview image.

        """
        if not guid:
            self.show_status("Select a run before opening a preview plot.", 5000)
            return

        if not self.ds or self.ds.guid != guid:
            try:
                if self.dataset_holder.get(guid, 0) == 0:
                    self.ds = load_by_guid_read_only(guid)
                else:
                    self.ds = self.dataset_holder[guid]["dataset"]
            except Exception as err:
                log_exception("Preview plot run load failed", err, __name__)
                self.show_error("Run Load Failed", f"Could not load run with GUID {guid}.", str(err))
                return

        self.open_preview_plot(parameter_name)


    @QtCore.pyqtSlot(object, str, str)
    def add_dropped_preview_to_plot(self, target_win, guid, parameter_name):
        """
        Add a run-table preview trace to the plot it was dropped onto.

        """
        self.add_trace_to_plot(target_win, guid, parameter_name)


    def add_trace_to_plot(self, target_win, source_guid, parameter_name, param=None):
        """
        Adds a plottable 1D parameter to an existing compatible 1D plot.

        """
        if target_win is None or not hasattr(target_win, "option_boxes"):
            self.show_status("Drop traces onto a compatible line plot.", 5000)
            return False

        if param is None:
            try:
                param = self._parameter_from_guid(source_guid, parameter_name)
            except Exception as err:
                log_exception("Trace source run load failed", err, __name__)
                self.show_error(
                    "Run Load Failed",
                    f"Could not load run with GUID {source_guid}.",
                    str(err),
                )
                return False

        if param is None or not getattr(param, "depends_on", ""):
            self.show_status(f"No preview plot found for {parameter_name}.", 5000)
            return False

        if len(getattr(param, "depends_on_", ())) != 1:
            self.show_status("Only 1D measurements can be added as traces.", 5000)
            return False

        if tuple(param.depends_on_) != tuple(target_win.param.depends_on_):
            self.show_status(
                f"Cannot add {parameter_name}; the plot axes do not match.",
                5000,
            )
            return False

        from_win = self._plot_window_for_param(source_guid, param)
        if from_win == target_win:
            self.show_status(f"Skipped {target_win.label}; source and target are the same.", 5000)
            return False

        if from_win is None:
            from_win = self._open_hidden_trace_window(source_guid, param, target_win)
            if from_win is None:
                return False

        self.get_1d_wins(target_win)
        if target_win.option_boxes[-1].isEnabled():
            box = target_win.option_boxes[-1]
        else:
            target_win.add_option_box()
            box = target_win.option_boxes[-1]

        index = box.option_box.findText(from_win.label)
        if index < 0:
            self.show_status(
                f"Cannot add {parameter_name}; it is already shown or incompatible.",
                5000,
            )
            if not from_win.visible:
                from_win.close()
            return False

        box.option_box.setCurrentIndex(index)
        from_win.close()
        return True


    def _parameter_from_guid(self, guid, parameter_name):
        ds = self._dataset_for_guid(guid)
        return self._measurement_param_by_name(ds, parameter_name)


    def _measurement_param_by_name(self, dataset, parameter_name):
        for param in dataset.get_parameters():
            if param.name == parameter_name:
                return param
        return None


    def _dataset_for_guid(self, guid):
        if self.dataset_holder.get(guid, 0) != 0:
            return self.dataset_holder[guid]["dataset"]
        return load_by_guid_read_only(guid)


    def _plot_window_for_param(self, guid, param):
        for win in self.windows:
            try:
                if win.ds.guid == guid and win.param.name == param.name:
                    return win
            except AttributeError:
                continue
        return None


    def _open_hidden_trace_window(self, source_guid, param, target_win):
        before = set(id(win) for win in self.windows)
        self.openPlot(guid=source_guid, params=[param], show=False)

        for win in reversed(self.windows):
            if id(win) in before:
                continue
            try:
                if win.ds.guid == source_guid and win.param.name == param.name:
                    if win.ds.running and not target_win.monitor.isActive():
                        target_win.monitorIntervalChanged(target_win.spinBox.value())
                        target_win.toolbarRef.show()
                    return win
            except AttributeError:
                continue

        self.show_status(f"Could not prepare {param.name} for adding to the plot.", 5000)
        return None


    def _selected_measurement_params(self, dataset):
        """
        Returns the measurement parameters requested by the Measurement field.

        """
        params = [param for param in dataset.get_parameters() if param.depends_on != ""]
        measurement = self.measurementBox.text().strip()

        if measurement in ("", "*"):
            return params

        try:
            index = int(measurement)
        except ValueError:
            self.show_status("Measurement must be a number or *.", 5000)
            return None

        if index < 1 or index > len(params):
            self.show_status(
                f"Run {dataset.run_id} has no measurement {index}.",
                5000,
            )
            return None

        return [params[index - 1]]


    def _dataset_for_plot_target(self):
        """
        Loads the dataset requested by the Run field.

        """
        if not self.fileTextbox.text():
            self.show_status("Load a database before plotting or exporting.", 5000)
            return None

        if self.selected_run_id is None:
            self.show_status("Enter a Run ID before plotting or exporting.", 5000)
            return None

        try:
            return load_by_id_read_only(self.selected_run_id)
        except Exception as error:
            log_exception("Run ID load failed", error, __name__)
            self.show_error(
                "Run Load Failed",
                f"Could not load Run ID {self.selected_run_id}.",
                str(error),
            )
            return None


    def _measurement_dataframe(self, dataset, params):
        """
        Builds a flat CSV-friendly dataframe for the selected measurement data.

        """
        frames = []
        prefix_columns = len(params) > 1
        for param in params:
            param_data = dataset.get_parameter_data(param.name).get(param.name, {})
            columns = {}
            for name, values in param_data.items():
                column_name = f"{param.name}.{name}" if prefix_columns else name
                columns[column_name] = pd.Series(np.asarray(values).ravel())
            frames.append(pd.DataFrame(columns))

        return pd.concat(frames, axis=1) if frames else pd.DataFrame()


    def _default_export_filename(self, dataset, params):
        """
        Returns a default CSV export path.

        """
        database_folder = os.path.dirname(self.fileTextbox.text())
        measurement = "all" if len(params) != 1 else params[0].name
        filename = self._safe_filename(f"run_{dataset.run_id}_{measurement}.csv")
        return os.path.join(database_folder or os.getcwd(), filename)


    def _safe_filename(self, filename):
        """
        Replaces path-hostile characters in a suggested filename.

        """
        return "".join(char if char.isalnum() or char in "._-" else "_" for char in filename)


    @QtCore.pyqtSlot(str)
    def add_ds_at(self, guid: str, ds=None):
        """
        Tracks a dataset used by one or more plot windows.

        """
        if self.dataset_holder.get(guid, 0) == 0:
            ds = load_by_guid_read_only(guid) if ds is None else ds
            assert ds.guid == guid

            self.dataset_holder[guid] = {
                "dataset": ds,
                "users": 1,
                "del_timer": None,
            }
        else:
            self.dataset_holder[guid]["users"] += 1
            if self.dataset_holder[guid]["del_timer"] is not None:
                self.dataset_holder[guid]["del_timer"].stop()
                self.dataset_holder[guid]["del_timer"] = None


    @QtCore.pyqtSlot(str)
    def remove_ds_at(self, guid: str):
        """
        Decreases the user count for a cached plot dataset.

        """
        if self.dataset_holder.get(guid, 0) == 0:
            self.show_status("Trying to remove dataset that does not exist.", 5000)
            return

        self.dataset_holder[guid]["users"] -= 1

        if self.dataset_holder[guid]["users"] <= 0:
            del_time = self.config.get("runtime_settings.del_grace_period")

            if del_time == 0:
                self.dataset_holder.pop(guid)

            elif self.dataset_holder[guid]["del_timer"] is None:
                del_timer = QtCore.QTimer()
                del_timer.setSingleShot(True)
                self.dataset_holder[guid]["del_timer"] = del_timer
                del_timer.timeout.connect(lambda guid=guid: self.dataset_holder.pop(guid))
                del_timer.start(int(del_time * 1000))


    def post_admin(self):
        """
        Updates the plot windows' internal track of other open windows.

        """
        for item in self.windows:
            if isinstance(item, plot1d):
                self.get_1d_wins(item)


    def get_1d_wins(self, win):
        """
        Finds compatible 1D plot windows for adding secondary traces to win.

        """
        wins = []

        for item in self.windows:
            try:
                if item.param.depends_on == win.param.depends_on:
                    if item.label not in win.lines.keys():
                        wins.append(item)

                elif (
                    item.__class__.__name__ == "sweeper"
                    and item.axis_options["x"] == win.param.depends_on
                ):
                    if item.label not in win.lines.keys():
                        wins.append(item)
            except AttributeError:
                continue

        win.update_line_picker(wins)
