import csv
import os
import weakref
from os import path
from typing import TYPE_CHECKING, Any, Literal

import numpy as np
from PyQt6 import QtCore, QtGui, QtPrintSupport, QtSvg
from PyQt6 import QtWidgets as qtw
from pyqtgraph.exporters import CSVExporter, ImageExporter, SVGExporter

try:
    from pyqtgraph.GraphicsScene.exportDialog import (
        ExportDialog as _PyqtgraphExportDialog,
    )
    from pyqtgraph.GraphicsScene.exportDialog import (
        FormatExportListWidgetItem as _FormatExportListWidgetItem,
    )
except ImportError:  # pragma: no cover - fail-closed compatibility path
    _PyqtgraphExportDialog = None
    _FormatExportListWidgetItem = None

from qplot.diagnostics import log_exception

from ._export_paths import (
    ExportDestinationTransaction,
    UnsafeExportDestinationError,
    approve_export_path,
    choose_export_path,
    choose_export_path_with_suffixes,
    collect_protected_database_files,
    prepare_export_destination,
    write_export_atomically,
)
from ._preferences import (
    COPY_PLOT_IMAGE_RESOLUTION_300_DPI,
    COPY_PLOT_IMAGE_RESOLUTION_KEY,
    COPY_PLOT_IMAGE_RESOLUTION_SCREEN,
    COPY_PLOT_IMAGE_RESOLUTION_SVG,
)

if TYPE_CHECKING:
    from ._dataset_handle import DatasetKey

_MAX_EXPORTED_IMAGE_SIZE = 20_000
_HIGH_DPI_COPY_RESOLUTION = 300
_INCHES_PER_METER = 39.370_078_740_157_48
_DpiAxis = Literal["x", "y"]


_UnsafePrintDestinationError = UnsafeExportDestinationError
_PrintPdfDestination = ExportDestinationTransaction
_SAFE_PYQTGRAPH_FILE_EXPORTERS = (ImageExporter, CSVExporter, SVGExporter)
_SAFE_PYQTGRAPH_COPY_EXPORTERS = (ImageExporter, SVGExporter)


class _PrintPdfExportCancelled(RuntimeError):
    """Raised when a user declines qPlot's exact PDF replacement prompt."""


class _DisabledPyqtgraphExportDialog:
    """Fail-closed cache object for GraphicsScene.showExportDialog()."""

    def show(self, _item: Any = None) -> None:
        return

    def close(self) -> None:
        return


if (
        _PyqtgraphExportDialog is not None
        and _FormatExportListWidgetItem is not None
        ):
    class _SafePyqtgraphExportDialog(_PyqtgraphExportDialog):
        """PyQtGraph's format UI with qPlot-owned file publication."""

        def __init__(self, scene: Any, owner: Any):
            self._qplot_owner_ref = weakref.ref(owner)
            super().__init__(scene)
            owner.destroyed.connect(self._qplot_owner_destroyed)

        @property
        def _qplot_owner(self) -> Any | None:
            return self._qplot_owner_ref()

        def updateFormatList(self) -> None:
            """Expose only exact built-in exporters qPlot can stage safely."""
            current = self.ui.formatList.currentItem()
            current_class = getattr(current, "expClass", None)
            self.ui.formatList.clear()
            selected_row = 0
            for row, exporter_class in enumerate(_SAFE_PYQTGRAPH_FILE_EXPORTERS):
                item = _FormatExportListWidgetItem(
                    exporter_class,
                    QtCore.QCoreApplication.translate(
                        "Exporter",
                        exporter_class.Name,
                    ),
                )
                self.ui.formatList.addItem(item)
                if exporter_class is current_class:
                    selected_row = row
            self.ui.formatList.setCurrentRow(selected_row)

        def exportFormatChanged(self, item: Any, previous: Any) -> None:
            """Instantiate only an exact allowlisted exporter class."""
            exporter_class = getattr(item, "expClass", None)
            if exporter_class not in _SAFE_PYQTGRAPH_FILE_EXPORTERS:
                self.currentExporter = None
                self.ui.paramTree.clear()
                self.ui.exportBtn.setEnabled(False)
                self.ui.copyBtn.setEnabled(False)
                return
            super().exportFormatChanged(item, previous)
            exporter_type = type(self.currentExporter)
            self.ui.exportBtn.setEnabled(
                exporter_type in _SAFE_PYQTGRAPH_FILE_EXPORTERS
            )
            self.ui.copyBtn.setEnabled(
                exporter_type in _SAFE_PYQTGRAPH_COPY_EXPORTERS
            )

        @QtCore.pyqtSlot()
        def exportClicked(self) -> None:
            self.selectBox.hide()
            owner = self._qplot_owner
            if owner is not None:
                owner._export_pyqtgraph_exporter(self.currentExporter)

        @QtCore.pyqtSlot()
        def copyClicked(self) -> None:
            self.selectBox.hide()
            owner = self._qplot_owner
            if owner is not None:
                owner._copy_pyqtgraph_exporter(self.currentExporter)

        @QtCore.pyqtSlot()
        def _qplot_owner_destroyed(self) -> None:
            self.dispose()

        def dispose(self) -> None:
            """Remove both the dialog and its separately scene-owned box."""
            self.close()
            try:
                if self.selectBox.scene() is self.scene:
                    self.scene.removeItem(self.selectBox)
            except RuntimeError:
                pass
            self.currentExporter = None
            self.deleteLater()
else:
    _SafePyqtgraphExportDialog = None


if TYPE_CHECKING:
    class _PlotExportBase(qtw.QMainWindow):
        _dataset_key: DatasetKey
        ds: Any
        param: Any
        plot: Any
        widget: Any

        def show_error(
                self,
                title: str,
                message: str,
                details: str | None = None,
                ) -> None: ...
        def show_status(self, message: str, timeout: int = 5000) -> None: ...
else:
    class _PlotExportBase:
        pass


class PlotExportMixin(_PlotExportBase):
    """
    Plot-window printing, export, PDF, and clipboard-image helpers.

    """

    @QtCore.pyqtSlot()
    def print_plot(self) -> bool:
        """
        Opens the native print dialog and prints the visible plot area.

        """
        try:
            printer = self._create_plot_printer()
            dialog = QtPrintSupport.QPrintDialog(printer, self)
            dialog.setWindowTitle("Print Plot")
            dialog.setMinMax(1, 1)
            dialog.setFromTo(1, 1)
            dialog.setOption(
                QtPrintSupport.QAbstractPrintDialog.PrintDialogOption.PrintPageRange,
                False,
            )
            # Keep Qt's generic file field disabled: on some platforms that
            # dialog probes the selected path before returning. Native/system
            # PDF destinations may still be exposed on the configured printer
            # after acceptance; those are redirected through staging below.
            dialog.setOption(
                QtPrintSupport.QAbstractPrintDialog.PrintDialogOption.PrintToFile,
                False,
            )

            if dialog.exec() != qtw.QDialog.DialogCode.Accepted:
                self.show_status("Plot printing cancelled.", 3000)
                return False

            pdf_destination = self._prepare_print_pdf_destination(printer)
            if pdf_destination is not None:
                if not self._print_plot_pdf_atomically(printer, pdf_destination):
                    raise RuntimeError(
                        "The plot PDF could not be rendered or published."
                    )
                self.show_status(
                    f"Printed plot to PDF: {pdf_destination.filename}",
                    5000,
                )
                return True

            if not self._render_plot_to_printer(printer):
                raise RuntimeError("The plot could not be rendered to the printer.")
        except _PrintPdfExportCancelled:
            self.show_status("Plot printing cancelled.", 3000)
            return False
        except _UnsafePrintDestinationError as err:
            self.show_status("Could not print plot to PDF.", 5000)
            if hasattr(self, "show_error"):
                self.show_error(
                    "Print to PDF Failed",
                    "qPlot could not safely use the selected PDF destination.",
                    str(err),
                )
            return False
        except Exception as err:
            log_exception("Plot printing failed", err, __name__)
            self.show_status("Could not print plot.", 5000)
            if hasattr(self, "show_error"):
                self.show_error(
                    "Print Failed",
                    "Could not print the plot.",
                    str(err),
                )
            return False

        self.show_status("Plot sent to printer.", 3000)
        return True


    def _prepare_print_pdf_destination(
            self,
            printer: QtPrintSupport.QPrinter,
            ) -> _PrintPdfDestination | None:
        """
        Validates a concrete PDF path returned by the system print dialog.

        An empty output filename denotes a physical printer. Other opaque
        print-to-file destinations are rejected because qPlot cannot redirect
        them through its atomic publication path.

        """
        output_file_name = getattr(printer, "outputFileName", None)
        if not callable(output_file_name):
            return None

        selected_path = str(output_file_name() or "")
        if not selected_path:
            return None
        if selected_path.strip().casefold() == "file:":
            raise _UnsafePrintDestinationError(
                "The printer did not provide a concrete PDF filename."
            )

        output_format = getattr(printer, "outputFormat", None)
        if (
                not callable(output_format)
                or output_format()
                != QtPrintSupport.QPrinter.OutputFormat.PdfFormat
                ):
            raise _UnsafePrintDestinationError(
                "Only PDF file output can be staged safely."
            )

        selected_path = path.abspath(selected_path)
        if path.splitext(selected_path)[1].casefold() != ".pdf":
            raise _UnsafePrintDestinationError(
                "The selected print destination must end in .pdf."
            )

        # The system dialog cannot give qPlot the target signature it observed
        # while asking about replacement. Capture it now and require qPlot's
        # own confirmation for this exact existing file; a file that appeared
        # after the system prompt must never inherit unrelated consent.
        destination = approve_export_path(
            self,
            selected_path,
            replace_title="Replace Printed PDF?",
            file_description="printed PDF file",
            existing_target_validator=(
                PlotExportMixin._validate_existing_print_pdf
            ),
        )
        if destination is None:
            raise _PrintPdfExportCancelled
        return destination


    def _print_plot_pdf_atomically(
            self,
            printer: QtPrintSupport.QPrinter,
            destination: _PrintPdfDestination,
            ) -> bool:
        """Renders a page-formatted PDF beside its target, then publishes it."""
        return write_export_atomically(
            destination,
            lambda staging_path: self._write_print_pdf_stage(
                printer,
                staging_path,
            ),
        )


    def _write_print_pdf_stage(
            self,
            printer: QtPrintSupport.QPrinter,
            staging_path: str,
            ) -> bool:
        """Redirects the configured PDF printer to one private staging file."""
        if path.splitext(staging_path)[1].casefold() != ".pdf":
            raise _UnsafePrintDestinationError(
                "The private print staging path is not a PDF file."
            )

        printer.setOutputFileName(staging_path)
        actual_path = str(printer.outputFileName() or "")
        if (
                path.normcase(path.realpath(path.abspath(actual_path)))
                != path.normcase(path.realpath(path.abspath(staging_path)))
                ):
            raise _UnsafePrintDestinationError(
                "The printer did not accept qPlot's private staging path."
            )
        if printer.outputFormat() != QtPrintSupport.QPrinter.OutputFormat.PdfFormat:
            raise _UnsafePrintDestinationError(
                "The printer did not retain PDF output while staging."
            )

        return self._paint_plot_to_device(printer)


    def _revalidate_print_pdf_destination(
            self,
            destination: _PrintPdfDestination,
            ) -> None:
        """Rejects a destination that changed while its PDF was rendering."""
        destination.validate()


    def _protected_database_identities(
            self,
            ) -> frozenset[object]:
        """Compatibility wrapper for the former Print-only collector."""
        return collect_protected_database_files(self).identities


    @staticmethod
    def _ensure_print_target_is_not_protected(
            filename: str,
            protected_identities: frozenset[object],
            ) -> None:
        """Compatibility check for retained Print target identities."""
        from qplot.datahandling.file_identity import database_file_identity

        if database_file_identity(filename) in protected_identities:
            raise _UnsafePrintDestinationError(
                "The selected PDF path refers to an input database file."
            )


    @staticmethod
    def _print_directory_identity(file_stat: os.stat_result) -> tuple[int, int, int]:
        """Returns stable identity fields for a selected output directory."""
        return (
            int(file_stat.st_dev),
            int(file_stat.st_ino),
            int(file_stat.st_mode),
        )


    @staticmethod
    def _print_target_signature(filename: str) -> tuple[object, ...] | None:
        """Compatibility wrapper around the shared target inspection."""
        destination = prepare_export_destination(
            None,
            filename,
            replacement_confirmed=True,
            existing_target_validator=(
                PlotExportMixin._validate_existing_print_pdf
            ),
        )
        return destination.target_signature


    @staticmethod
    def _validate_existing_print_pdf(filename: str) -> bool:
        """Retain Print's format check for an existing replacement target."""
        try:
            with open(filename, "rb") as existing_file:
                header = existing_file.read(5)
        except OSError as err:
            raise _UnsafePrintDestinationError(
                "The selected PDF file could not be read safely."
            ) from err
        if header != b"%PDF-":
            raise _UnsafePrintDestinationError(
                "The selected destination is not an existing PDF file."
            )
        return True


    def _create_plot_printer(self) -> QtPrintSupport.QPrinter:
        """
        Creates a printer whose device-space sizing matches the plot view.

        pyqtgraph uses cosmetic pens and device-sized text for several plot
        elements.  Screen-resolution printer coordinates preserve their
        intended physical proportions while the print engine retains vector
        output.

        """
        printer = QtPrintSupport.QPrinter(
            QtPrintSupport.QPrinter.PrinterMode.ScreenResolution
        )
        printer.setCreator("qPlot")

        title = "qPlot plot"
        window_title = getattr(self, "windowTitle", None)
        if callable(window_title):
            candidate = window_title().strip()
            if candidate:
                title = candidate
        printer.setDocName(title)

        widget = self.__dict__.get("widget")
        if widget is not None:
            orientation = QtGui.QPageLayout.Orientation.Portrait
            if widget.width() > widget.height():
                orientation = QtGui.QPageLayout.Orientation.Landscape
            printer.setPageOrientation(orientation)

        return printer


    def _render_plot_to_printer(
            self,
            printer: QtGui.QPagedPaintDevice,
            ) -> bool:
        """
        Renders to a physical printer while rejecting direct file output.

        """
        self._ensure_printer_has_no_file_output(printer)
        return self._paint_plot_to_device(printer)


    def _paint_plot_to_device(
            self,
            printer: QtGui.QPagedPaintDevice,
            ) -> bool:
        """
        Renders the current plot view into a paged device's printable area.

        Rendering the graphics view through ``QPainter`` retains vector line
        work and text while excluding the surrounding plot-window controls.

        """
        widget = self.__dict__.get("widget")
        if widget is None or not hasattr(widget, "render"):
            return False

        source = QtCore.QRect(widget.rect())
        if source.width() < 1 or source.height() < 1:
            return False

        painter = QtGui.QPainter()
        if not painter.begin(printer):
            return False

        print_finished = False
        try:
            printable = QtCore.QRectF(painter.viewport())
            destination = self._fit_print_rect(source.size(), printable)
            if destination.isEmpty():
                return False

            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            painter.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing)
            painter.fillRect(printable, QtGui.QColor("white"))
            widget.render(
                painter,
                destination,
                source,
                QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
            )
        finally:
            print_finished = painter.end()

        return print_finished


    def _ensure_printer_has_no_file_output(
            self,
            printer: QtGui.QPagedPaintDevice,
            ) -> None:
        """
        Rejects direct print-to-file output before the backend opens its path.

        Concrete PDF output is redirected to a private sibling file before
        this physical-printer entry point is called. Any filename remaining
        here would bypass that atomic staging path.

        """
        output_file_name = getattr(printer, "outputFileName", None)
        if not callable(output_file_name):
            return

        output_path = str(output_file_name() or "")
        if not output_path:
            return
        raise _UnsafePrintDestinationError(
            "Direct printer output bypassed qPlot's PDF staging path: "
            f"{output_path}"
        )


    @staticmethod
    def _fit_print_rect(
            source_size: QtCore.QSize,
            printable: QtCore.QRectF,
            ) -> QtCore.QRectF:
        """
        Fits and centres a plot inside a printable rectangle without cropping.

        """
        if (
                source_size.width() < 1
                or source_size.height() < 1
                or printable.isEmpty()
                ):
            return QtCore.QRectF()

        fitted_size = QtCore.QSizeF(source_size)
        fitted_size.scale(
            printable.size(),
            QtCore.Qt.AspectRatioMode.KeepAspectRatio,
        )
        destination = QtCore.QRectF(QtCore.QPointF(), fitted_size)
        destination.moveCenter(printable.center())
        return destination

    @QtCore.pyqtSlot()
    def open_export_dialog(self) -> None:
        """
        Opens a fail-closed PyQtGraph format dialog for this plot.

        """
        try:
            dialog = self._install_safe_pyqtgraph_export_dialog()
            if dialog is None:
                self.show_status("Plot file export is unavailable.", 5000)
                if hasattr(self, "show_error"):
                    self.show_error(
                        "Plot Export Unavailable",
                        "This PyQtGraph version cannot export files safely.",
                    )
                return
            dialog.show(self.plot)
        except Exception as err:
            log_exception("Plot export dialog setup failed", err, __name__)
            self.show_status("Plot file export is unavailable.", 5000)
            if hasattr(self, "show_error"):
                self.show_error(
                    "Plot Export Unavailable",
                    "This PyQtGraph version cannot export files safely.",
                    str(err),
                )


    def _install_safe_pyqtgraph_export_dialog(self) -> Any | None:
        """Eagerly own the scene's generic exporter cache, or disable it."""
        scene = self.widget.scene()
        dialog = getattr(scene, "exportDialog", None)
        if (
                _SafePyqtgraphExportDialog is not None
                and type(dialog) is _SafePyqtgraphExportDialog
                and getattr(dialog, "_qplot_owner", None) is self
                ):
            return dialog

        self._dispose_pyqtgraph_export_dialog(scene, dialog)
        if _SafePyqtgraphExportDialog is None:
            scene.exportDialog = _DisabledPyqtgraphExportDialog()
            return None

        dialog = _SafePyqtgraphExportDialog(scene, self)
        scene.exportDialog = dialog
        return dialog


    @staticmethod
    def _dispose_pyqtgraph_export_dialog(scene: Any, dialog: Any) -> None:
        """Dispose a cached stock/safe dialog and its scene selection item."""
        if dialog is None or isinstance(dialog, _DisabledPyqtgraphExportDialog):
            return
        if (
                _SafePyqtgraphExportDialog is not None
                and type(dialog) is _SafePyqtgraphExportDialog
                ):
            dialog.dispose()
            return
        try:
            dialog.close()
        except RuntimeError:
            pass
        select_box = getattr(dialog, "selectBox", None)
        if select_box is not None:
            try:
                if select_box.scene() is scene:
                    scene.removeItem(select_box)
            except RuntimeError:
                pass
        delete_later = getattr(dialog, "deleteLater", None)
        if callable(delete_later):
            delete_later()


    def _dispose_plot_export_dialog(self) -> None:
        """Close the plot's exporter when its owning plot window closes."""
        widget = self.__dict__.get("widget")
        scene = widget.scene() if widget is not None else None
        if scene is None:
            return
        dialog = getattr(scene, "exportDialog", None)
        self._dispose_pyqtgraph_export_dialog(scene, dialog)
        scene.exportDialog = _DisabledPyqtgraphExportDialog()


    def _export_pyqtgraph_exporter(self, exporter: Any) -> bool:
        """Route an allowlisted PyQtGraph file exporter through staging."""
        specification = self._pyqtgraph_export_specification(exporter)
        if specification is None:
            self.show_status("That PyQtGraph export format is disabled.", 5000)
            if hasattr(self, "show_error"):
                self.show_error(
                    "Plot Export Disabled",
                    "qPlot cannot guarantee safe publication for that format.",
                )
            return False

        suffixes, default_suffix, name_filter, description = specification
        suggested_path = path.splitext(self._default_plot_pdf_filename())[0]
        suggested_path = f"{suggested_path}{default_suffix}"
        try:
            destination = choose_export_path_with_suffixes(
                self,
                caption="Export Plot",
                suggested_path=suggested_path,
                name_filter=name_filter,
                allowed_suffixes=suffixes,
                default_suffix=default_suffix,
                replace_title="Replace Plot Export?",
                file_description=description,
            )
            if destination is None:
                self.show_status("Plot export cancelled.", 3000)
                return False
            writer = lambda staging_path: exporter.export(fileName=staging_path)
            if (
                    type(exporter) is CSVExporter
                    and getattr(self, "operation_kind", None) == "plot2d"
                    ):
                writer = lambda staging_path: self._write_heatmap_csv_stage(
                    staging_path,
                    exporter,
                    )
            saved = write_export_atomically(
                destination,
                writer,
            )
        except Exception as err:
            log_exception("PyQtGraph plot export failed", err, __name__)
            self.show_status("Could not export plot.", 5000)
            if hasattr(self, "show_error"):
                self.show_error(
                    "Plot Export Failed",
                    "Could not safely export the plot.",
                    str(err),
                )
            return False

        if not saved:
            self.show_status("Could not export plot.", 5000)
            return False
        self.show_status(f"Exported plot: {destination.filename}", 5000)
        return True


    def _write_heatmap_csv_stage(
            self,
            staging_path: str,
            exporter: CSVExporter,
            ) -> bool:
        """Write the canonical, currently plotted heatmap as long-form rows.

        ``dataGrid[row, column]`` is paired with the Y centre at ``row`` and
        the X centre at ``column``.  This deliberately snapshots the displayed
        grid, so a viewport-loaded or downsampled large heatmap exports that
        current resolution without initiating another database read.
        """
        axis_data = self.__dict__.get("axis_data")
        if not isinstance(axis_data, dict):
            raise ValueError("Heatmap coordinate data is not available.")

        x_centres = np.asarray(axis_data.get("x", []), dtype=float)
        y_centres = np.asarray(axis_data.get("y", []), dtype=float)
        data_grid = np.asarray(self.__dict__.get("dataGrid", []))
        expected_shape = (y_centres.size, x_centres.size)
        if x_centres.ndim != 1 or y_centres.ndim != 1:
            raise ValueError("Heatmap coordinate axes must be one-dimensional.")
        if data_grid.ndim != 2 or data_grid.shape != expected_shape:
            raise ValueError(
                f"Heatmap data shape {data_grid.shape} does not match "
                f"axis shape {expected_shape}."
            )
        if data_grid.size == 0:
            raise ValueError("The currently plotted heatmap is empty.")

        delimiter = "\t" if exporter.params["separator"] == "tab" else ","
        column_names = self._heatmap_csv_column_names()
        rows_written = 0
        with open(staging_path, "w", encoding="utf-8", newline="") as csv_file:
            csv_writer = csv.writer(
                csv_file,
                delimiter=delimiter,
                quoting=csv.QUOTE_MINIMAL,
                lineterminator="\n",
            )
            csv_writer.writerow(column_names)
            for y_index, y_centre in enumerate(y_centres):
                for x_index, x_centre in enumerate(x_centres):
                    csv_writer.writerow((
                        float(x_centre),
                        float(y_centre),
                        data_grid[y_index, x_index],
                    ))
                    rows_written += 1

        if rows_written != data_grid.size or os.path.getsize(staging_path) == 0:
            raise RuntimeError("Heatmap CSV serialization produced no usable data.")
        return True


    def _heatmap_csv_column_names(self) -> tuple[str, str, str]:
        """Return stable parameter names for heatmap coordinate/value columns."""
        axis_param = self.__dict__.get("axis_param")
        if not isinstance(axis_param, dict):
            axis_param = {}
        value_param = self.__dict__.get(
            "display_param",
            self.__dict__.get("param"),
        )
        return (
            self._export_parameter_name(axis_param.get("x"), "x"),
            self._export_parameter_name(axis_param.get("y"), "y"),
            self._export_parameter_name(value_param, "value"),
        )


    @staticmethod
    def _export_parameter_name(parameter: Any, fallback: str) -> str:
        """Use a QCoDeS parameter name, with a meaningful safe fallback."""
        name = getattr(parameter, "name", None)
        if name is None or not str(name).strip():
            return fallback
        return str(name)


    def _copy_pyqtgraph_exporter(self, exporter: Any) -> bool:
        """Allow only known clipboard-only PyQtGraph exporters."""
        if type(exporter) not in _SAFE_PYQTGRAPH_COPY_EXPORTERS:
            self.show_status("Copy is disabled for that export format.", 5000)
            return False
        try:
            result = exporter.export(copy=True)
        except Exception as err:
            log_exception("PyQtGraph plot copy failed", err, __name__)
            self.show_status("Could not copy plot.", 5000)
            return False
        if result is False:
            self.show_status("Could not copy plot.", 5000)
            return False
        self.show_status("Plot copied to clipboard.", 3000)
        return True


    @staticmethod
    def _pyqtgraph_export_specification(
            exporter: Any,
            ) -> tuple[tuple[str, ...], str, str, str] | None:
        """Describe exporters that accept a private same-suffix filename."""
        if type(exporter) is ImageExporter:
            patterns = tuple(exporter.getSupportedImageFormats())
            suffixes = tuple(
                suffix
                for pattern in patterns
                if (suffix := path.splitext(pattern)[1])
            )
            if not suffixes:
                return None
            return (
                suffixes,
                suffixes[0],
                ";;".join(
                    f"{suffix[1:].upper()} image (*{suffix})"
                    for suffix in suffixes
                ),
                "plot image",
            )
        if type(exporter) is CSVExporter:
            return (
                (".csv", ".tsv"),
                ".csv",
                "CSV files (*.csv);;TSV files (*.tsv)",
                "plot data file",
            )
        if type(exporter) is SVGExporter:
            return (
                (".svg",),
                ".svg",
                "SVG files (*.svg)",
                "SVG file",
            )
        return None


    @QtCore.pyqtSlot()
    def save_plot_pdf(self) -> bool:
        """
        Prompts for a filename and saves the visible plot area as a PDF.

        """
        try:
            destination = choose_export_path(
                self,
                caption="Save Plot as PDF",
                suggested_path=self._default_plot_pdf_filename(),
                name_filter="PDF files (*.pdf)",
                required_suffix=".pdf",
                replace_title="Replace PDF File?",
                file_description="PDF file",
            )
        except Exception as err:
            log_exception("Plot PDF destination rejected", err, __name__)
            self.show_status("Could not use that PDF destination.", 5000)
            if hasattr(self, "show_error"):
                self.show_error(
                    "PDF Export Failed",
                    "qPlot could not safely use the selected PDF destination.",
                    str(err),
                )
            return False
        if destination is None:
            self.show_status("PDF export cancelled.", 3000)
            return False

        return self.save_plot_pdf_to_file(destination)


    def save_plot_pdf_to_file(
            self,
            filename: str | ExportDestinationTransaction,
            ) -> bool:
        """
        Saves the visible plot area as a PDF at ``filename``.

        """
        if not filename:
            self.show_status("PDF export cancelled.", 3000)
            return False

        try:
            if isinstance(filename, ExportDestinationTransaction):
                destination = filename
            else:
                destination = prepare_export_destination(
                    self,
                    filename,
                    required_suffix=".pdf",
                )
            saved = write_export_atomically(destination, self._write_plot_pdf)
        except Exception as err:
            log_exception("Plot PDF export failed", err, __name__)
            self.show_status("Could not save plot PDF.", 5000)
            if hasattr(self, "show_error"):
                self.show_error(
                    "PDF Export Failed",
                    "Could not save the plot PDF.",
                    str(err),
                )
            return False

        if not saved:
            self.show_status("Could not save plot PDF.", 5000)
            return False

        self.show_status(f"Saved PDF: {destination.filename}", 5000)
        return True


    def _write_plot_pdf(self, filename: str) -> bool:
        """
        Saves the already-laid-out plot widget as a single-page PDF.

        """
        pixmap = self._plot_image_pixmap()
        if pixmap.isNull():
            return False

        image = pixmap.toImage()
        if image.isNull():
            return False

        size = image.size()
        if size.width() < 1 or size.height() < 1:
            return False

        writer = QtGui.QPdfWriter(filename)
        writer.setCreator("qPlot")
        writer.setTitle("qPlot plot")
        writer.setResolution(72)
        page_size = QtGui.QPageSize(
            QtCore.QSizeF(size.width(), size.height()),
            QtGui.QPageSize.Unit.Point,
            "qPlot plot",
            )
        page_layout = QtGui.QPageLayout(
            page_size,
            QtGui.QPageLayout.Orientation.Portrait,
            QtCore.QMarginsF(0, 0, 0, 0),
            QtGui.QPageLayout.Unit.Point,
            )
        writer.setPageLayout(page_layout)

        painter = QtGui.QPainter()
        if not painter.begin(writer):
            return False

        try:
            target = QtCore.QRectF(0, 0, size.width(), size.height())
            painter.fillRect(target, QtGui.QColor("white"))
            painter.drawImage(target, image)
        finally:
            painter.end()

        return True


    def _default_plot_pdf_filename(self) -> str:
        """
        Returns a suggested PDF export filename for the current plot.

        """
        run_id = getattr(self.ds, "run_id", "plot")
        param_name = getattr(getattr(self, "param", None), "name", "plot")
        filename = self._safe_plot_export_filename(f"run_{run_id}_{param_name}.pdf")

        try:
            database_folder = path.dirname(self._dataset_key.database_path)
        except Exception:
            database_folder = ""

        if database_folder:
            return path.join(database_folder, filename)
        return path.abspath(filename)


    @staticmethod
    def _safe_plot_export_filename(filename: str) -> str:
        """
        Replaces path-hostile characters in a suggested plot export filename.

        """
        return "".join(char if char.isalnum() or char in "._-" else "_" for char in filename)


    @QtCore.pyqtSlot()
    def copy_plot_image(self) -> bool:
        """
        Copies the rendered plot widget to the clipboard as an image.

        Only the pyqtgraph widget is captured, so window menus, toolbars, docks,
        and any open context menu are excluded.

        """
        resolution = self._copy_plot_image_resolution()
        if resolution == COPY_PLOT_IMAGE_RESOLUTION_300_DPI:
            return self.copy_plot_image_at_dpi(_HIGH_DPI_COPY_RESOLUTION)
        if resolution == COPY_PLOT_IMAGE_RESOLUTION_SVG:
            return self.copy_plot_image_as_svg()

        return self.copy_plot_image_at_screen_resolution()


    def copy_plot_image_at_screen_resolution(self) -> bool:
        """
        Copies the rendered plot widget at the current screen resolution.

        """
        clipboard = qtw.QApplication.clipboard()
        if clipboard is None:
            self.show_status("No clipboard available.", 5000)
            return False

        pixmap = self._plot_image_pixmap()
        if pixmap.isNull():
            self.show_status("Could not copy plot image.", 5000)
            return False

        clipboard.setImage(pixmap.toImage())
        self.show_status("Plot image copied to clipboard.", 3000)
        return True


    def _copy_plot_image_resolution(self) -> str:
        """
        Returns the configured resolution mode for plot-image clipboard copies.

        """
        config = self.__dict__.get("config")
        if config is None:
            return COPY_PLOT_IMAGE_RESOLUTION_SCREEN
        return config.get(COPY_PLOT_IMAGE_RESOLUTION_KEY)


    def _plot_image_pixmap(self) -> QtGui.QPixmap:
        """
        Captures the plot widget without the surrounding QMainWindow chrome.

        """
        return self.widget.grab()


    def copy_plot_image_as_svg(self) -> bool:
        """
        Copies the current plot area to the clipboard as SVG.

        """
        clipboard = qtw.QApplication.clipboard()
        if clipboard is None:
            self.show_status("No clipboard available.", 5000)
            return False

        try:
            svg_bytes = self._plot_svg_bytes()
        except Exception as err:
            log_exception("Plot SVG copy failed", err, __name__)
            self.show_status("Could not copy plot SVG.", 5000)
            return False

        if not svg_bytes:
            self.show_status("Could not copy plot SVG.", 5000)
            return False

        mime_data = QtCore.QMimeData()
        mime_data.setData("image/svg+xml", QtCore.QByteArray(svg_bytes))
        try:
            mime_data.setText(svg_bytes.decode("utf-8"))
        except UnicodeDecodeError:
            pass
        clipboard.setMimeData(mime_data)
        self.show_status("Plot SVG copied to clipboard.", 3000)
        return True


    def _plot_svg_bytes(self) -> bytes:
        """
        Renders the current plot area as SVG bytes.

        """
        widget = self.__dict__.get("widget")
        if widget is None or not hasattr(widget, "scene"):
            return b""

        size = widget.size()
        if size.width() < 1 or size.height() < 1:
            return b""

        scene = widget.scene()
        if scene is None:
            return b""

        data = QtCore.QByteArray()
        buffer = QtCore.QBuffer(data)
        if not buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly):
            return b""

        generator = QtSvg.QSvgGenerator()
        generator.setOutputDevice(buffer)
        generator.setSize(size)
        generator.setViewBox(QtCore.QRect(0, 0, size.width(), size.height()))
        generator.setTitle("qPlot plot")
        generator.setDescription("Copied from qPlot")

        painter = QtGui.QPainter(generator)
        try:
            scene.render(
                painter,
                QtCore.QRectF(0, 0, size.width(), size.height()),
                self._plot_svg_source_rect(widget),
                )
        finally:
            painter.end()
            buffer.close()

        return bytes(data.data())


    def _plot_svg_source_rect(self, widget: Any) -> QtCore.QRectF:
        """
        Returns the scene rectangle currently visible in the plot widget.

        """
        viewport_transform = getattr(widget, "viewportTransform", None)
        if viewport_transform is not None:
            return QtCore.QRectF(
                viewport_transform().inverted()[0].mapRect(widget.rect())
                )

        plot = self.__dict__.get("plot")
        if plot is not None:
            return QtCore.QRectF(plot.sceneBoundingRect())

        return QtCore.QRectF(widget.scene().sceneRect())


    def copy_plot_image_at_dpi(self, dpi: float) -> bool:
        """
        Renders the plot image at a target DPI and copies it to the clipboard.

        """
        try:
            size = self._plot_image_size_for_dpi(dpi)
        except Exception as err:
            log_exception("Plot image DPI sizing failed", err, __name__)
            self.show_status("Could not copy plot image at requested resolution.", 5000)
            return False

        if not size.isValid() or size.width() < 1 or size.height() < 1:
            self.show_status("Could not copy plot image at requested resolution.", 5000)
            return False

        if not self.copy_plot_image_at_size(size.width(), size.height()):
            return False

        clipboard = qtw.QApplication.clipboard()
        if clipboard is not None:
            image = clipboard.image()
            if not image.isNull():
                self._set_image_dpi(image, dpi)
                clipboard.setImage(image)

        self.show_status(
            "Plot image copied to clipboard at "
            f"{dpi:g} dpi ({size.width()} x {size.height()} px).",
            3000,
            )
        return True


    def copy_plot_image_at_size(self, width: int, height: int) -> bool:
        """
        Renders the plot image at a chosen pixel size and copies it to the clipboard.

        """
        size = QtCore.QSize(int(width), int(height))
        if size.width() < 1 or size.height() < 1:
            self.show_status("Plot image size must be at least 1 x 1 px.", 5000)
            return False
        if (
            size.width() > _MAX_EXPORTED_IMAGE_SIZE
            or size.height() > _MAX_EXPORTED_IMAGE_SIZE
            ):
            self.show_status(
                "Plot image size must be no larger than "
                f"{_MAX_EXPORTED_IMAGE_SIZE} x {_MAX_EXPORTED_IMAGE_SIZE} px.",
                5000,
                )
            return False

        clipboard = qtw.QApplication.clipboard()
        if clipboard is None:
            self.show_status("No clipboard available.", 5000)
            return False

        try:
            image = self._plot_image_at_size(size)
        except Exception as err:
            log_exception("Plot image copy at size failed", err, __name__)
            self.show_status("Could not copy plot image at requested size.", 5000)
            return False

        if image.isNull():
            self.show_status("Could not copy plot image at requested size.", 5000)
            return False

        clipboard.setImage(image)
        self.show_status("Plot image copied to clipboard.", 3000)
        return True


    def _plot_image_at_size(self, size: QtCore.QSize) -> QtGui.QImage:
        """
        Renders the current plot area into a QImage of the requested size.

        """
        exporter = self._plot_image_exporter()
        if exporter is None:
            return QtGui.QImage()

        return self._export_plot_image(exporter, size)


    def _plot_image_exporter(self) -> ImageExporter | None:
        """
        Returns an image exporter for the visible plot area.

        """
        item = self._plot_image_export_item()
        if item is None:
            return None
        return ImageExporter(item)


    def _plot_image_export_item(self) -> Any | None:
        """
        Returns the graphics object used for high-resolution image exports.

        """
        widget = self.__dict__.get("widget")
        if widget is not None and hasattr(widget, "scene"):
            scene = widget.scene()
            if scene is not None:
                return scene

        return self.__dict__.get("plot")


    def _export_plot_image(self, exporter: ImageExporter, size: QtCore.QSize) -> QtGui.QImage:
        """
        Renders an exporter into a QImage of the requested size.

        """
        params = exporter.parameters()
        params.param("width").setValue(
            size.width(),
            blockSignal=exporter.widthChanged,
            )
        params.param("height").setValue(
            size.height(),
            blockSignal=exporter.heightChanged,
            )
        return exporter.export(toBytes=True)


    def _plot_image_size_for_dpi(self, dpi: float) -> QtCore.QSize:
        """
        Returns the output pixel size needed to copy the plot at ``dpi``.

        """
        exporter = self._plot_image_exporter()
        if exporter is None:
            return QtCore.QSize()

        params = exporter.parameters()
        base_width = int(params["width"])
        base_height = int(params["height"])
        width = round(base_width * float(dpi) / self._plot_image_source_dpi("x"))
        height = round(base_height * float(dpi) / self._plot_image_source_dpi("y"))

        return QtCore.QSize(
            min(_MAX_EXPORTED_IMAGE_SIZE, max(1, width)),
            min(_MAX_EXPORTED_IMAGE_SIZE, max(1, height)),
            )


    def _plot_image_source_dpi(self, axis: _DpiAxis) -> float:
        """
        Returns the logical screen DPI used to convert screen pixels to inches.

        """
        widget = self.__dict__.get("widget")
        method_name = "logicalDpiX" if axis == "x" else "logicalDpiY"
        method = getattr(widget, method_name, None)
        if method is not None:
            dpi = float(method())
            if dpi > 0:
                return dpi

        screen = qtw.QApplication.primaryScreen()
        if screen is not None:
            dpi = (
                screen.logicalDotsPerInchX()
                if axis == "x"
                else screen.logicalDotsPerInchY()
                )
            if dpi > 0:
                return float(dpi)

        return 96.0


    def _set_image_dpi(self, image: QtGui.QImage, dpi: float) -> None:
        """
        Stores DPI metadata on a rendered QImage.

        """
        dots_per_meter = round(float(dpi) * _INCHES_PER_METER)
        image.setDotsPerMeterX(dots_per_meter)
        image.setDotsPerMeterY(dots_per_meter)
