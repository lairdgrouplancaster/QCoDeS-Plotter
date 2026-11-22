import os
import weakref
from contextlib import nullcontext
from os import path
from typing import TYPE_CHECKING, Any, Literal

import numpy as np
from PyQt6 import QtCore, QtGui, QtPrintSupport, QtSvg
from PyQt6 import QtWidgets as qtw
from pyqtgraph import ErrorBarItem, PlotItem
from pyqtgraph.exporters import CSVExporter, ImageExporter, SVGExporter

from ._plot_host import PlotHost

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
from qplot.tools.plot_csv import (
    CsvColumns,
    HeatmapCsv,
    TransformedCsv,
    TransformedTrace,
    frozen_array,
    shared_coordinates_match,
)

from ._csv_filenames import csv_filename
from ._export_names import suggested_export_filename
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
from ._plot_csv_selection import (
    configure_csv_measurements,
    csv_exports_heatmap,
    csv_selection_description,
    selected_csv_measurements,
)
from ._plot_export_controls import FIGURE_FORMATS, PlotExportControls
from ._plot_transform_labels import native_axis_quantities
from ._preferences import (
    COPY_PLOT_IMAGE_RESOLUTION_300_DPI,
    COPY_PLOT_IMAGE_RESOLUTION_KEY,
    COPY_PLOT_IMAGE_RESOLUTION_SCREEN,
    COPY_PLOT_IMAGE_RESOLUTION_SVG,
)
from ._svg_export import svg_bytes, write_svg_stage
from ._window_controls import _window_is_retired

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
CSV_UNDERLYING_VALUES = "After Operations-panel processing"
CSV_TRANSFORMED_VALUES = "After operations and plot transforms"
CSV_HEATMAP_VALUES = "Current heatmap grid"


def _without_hover_highlight(window: Any):
    """Keep transient inspection cues out of published plot graphics."""
    highlight = window.__dict__.get("_hover_highlight")
    return highlight.suspended() if highlight is not None else nullcontext()


def _csv_shared_coordinates_match(reference: Any, candidate: Any) -> bool:
    """Keep the existing validation seam backed by the shared CSV writer."""
    return shared_coordinates_match(reference, candidate)


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
            self.setParent(owner, QtCore.Qt.WindowType.Window)
            self.setObjectName("plotExportDialog")
            while self.ui.gridLayout.count():
                item = self.ui.gridLayout.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.hide()
            self.csv_scope = qtw.QLabel(self)
            self.csv_scope.setObjectName("csvExportScope")
            self.csv_scope.setWordWrap(True)
            self.csv_scope.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            self.csv_scope.hide()
            self.controls = PlotExportControls(self)
            self.controls_scroll = qtw.QScrollArea(self)
            self.controls_scroll.setWidgetResizable(True)
            self.controls_scroll.setFrameShape(qtw.QFrame.Shape.NoFrame)
            self.controls_scroll.setWidget(self.controls)
            self.ui.gridLayout.addWidget(self.controls_scroll, 0, 0, 1, 3)
            self.feedback = qtw.QLabel(self)
            self.feedback.setWordWrap(True)
            self.feedback.hide()
            self.ui.gridLayout.addWidget(self.feedback, 1, 0, 1, 3)
            actions = qtw.QHBoxLayout()
            actions.addStretch(1)
            for button in (self.ui.copyBtn, self.ui.exportBtn, self.ui.closeBtn):
                actions.addWidget(button)
                button.show()
            self.ui.gridLayout.addLayout(actions, 2, 0, 1, 3)
            self.setWindowTitle("Export plot")
            self.ui.gridLayout.setContentsMargins(16, 16, 16, 12)
            self.ui.gridLayout.setSpacing(12)
            self.ui.gridLayout.setRowStretch(0, 1)
            self.ui.copyBtn.setText("Copy image")
            self.ui.exportBtn.setText("Save as…")
            self.ui.exportBtn.setToolTip("Choose a destination and save using these export settings.")
            self.setMinimumSize(350, 230)
            owner.destroyed.connect(self._qplot_owner_destroyed)

        def show(self, item: Any = None) -> None:
            self.updateItemList()
            super().show()
            self.selectBox.hide()
            self.fit_export_controls()

        def updateItemList(self, select: Any = None) -> None:
            # Keep the inherited exporter plumbing fixed to the complete
            # scene, including colour scales and source annotations.
            self.ui.itemTree.clear()
            item: Any = qtw.QTreeWidgetItem(["Complete plot"])
            item.gitem = self.scene
            self.ui.itemTree.addTopLevelItem(item)
            self.ui.itemTree.setCurrentItem(item)

        def fit_export_controls(self) -> None:
            if not self.isVisible():
                return
            buttons = [button for button in (self.ui.copyBtn, self.ui.exportBtn, self.ui.closeBtn)
                       if not button.isHidden()]
            width = max(button.sizeHint().width() for button in buttons)
            for button in buttons:
                button.setFixedWidth(width)
            screen = self.screen()
            bounds = screen.availableGeometry() if screen is not None else QtCore.QRect(0, 0, 1024, 768)
            self.controls.ensurePolished()
            self.controls.body_layout.activate()
            preferred = self.controls.sizeHint()
            margins = self.ui.gridLayout.contentsMargins()
            target_width = min(max(480, preferred.width() + margins.left() + margins.right()), bounds.width() - 40)
            # Resolve the real viewport width, including theme padding and
            # borders, before measuring wrapped labels at that width.
            self.resize(target_width, self.height())
            self.ui.gridLayout.activate()
            viewport = self.controls_scroll.viewport()
            horizontal = self.controls_scroll.horizontalScrollBar()
            assert viewport is not None and horizontal is not None
            content_width = viewport.width()
            content_height = self.controls.heightForWidth(content_width)
            if content_height < 0:
                content_height = preferred.height()
            action_height = max(button.sizeHint().height() for button in buttons)
            spacing = self.ui.gridLayout.verticalSpacing()
            feedback_height = (self.feedback.heightForWidth(content_width) + spacing
                               if not self.feedback.isHidden() else 0)
            # Qt can add scrollbar chrome after this layout pass. Reserve its
            # native height so that it cannot squeeze out the bottom controls.
            scroll_chrome = max(self.controls_scroll.height() - viewport.height(), horizontal.sizeHint().height())
            desired_height = (content_height + scroll_chrome + margins.top() + margins.bottom()
                              + action_height + spacing + feedback_height + 12)
            self.resize(target_width, min(desired_height, bounds.height() - 80))
            frame = self.frameGeometry()
            self.move(max(bounds.left(), min(frame.left(), bounds.right() - frame.width() + 1)),
                      max(bounds.top(), min(frame.top(), bounds.bottom() - frame.height() + 1)))

        def exportItemChanged(self, item: Any, previous: Any) -> None:
            if item is not None:
                self.updateFormatList()
            self.selectBox.hide()

        @property
        def _qplot_owner(self) -> Any | None:
            return self._qplot_owner_ref()

        def updateFormatList(self) -> None:
            """Expose only exact built-in exporters qPlot can stage safely."""
            current = self.ui.formatList.currentItem()
            current_class = getattr(current, "expClass", None)
            self.ui.formatList.clear()
            selected_row = 0
            for row, (label, exporter_class) in enumerate((*FIGURE_FORMATS, ("CSV data", CSVExporter))):
                item = _FormatExportListWidgetItem(exporter_class, label)
                self.ui.formatList.addItem(item)
                if exporter_class is current_class:
                    selected_row = row
            self.ui.formatList.setCurrentRow(selected_row)

        def exportFormatChanged(self, item: Any, previous: Any) -> None:
            """Instantiate only an exact allowlisted exporter class."""
            exporter_class = getattr(item, "expClass", None)
            scope = getattr(self, "csv_scope", None)
            if scope is not None:
                scope.hide()
            owner = self._qplot_owner
            if exporter_class is QtGui.QPdfWriter and owner is not None:
                self.currentExporter = None
                self.ui.paramTree.clear()
                self.ui.exportBtn.setEnabled(True)
                self.ui.copyBtn.setEnabled(False)
                self.ui.copyBtn.hide()
                self.feedback.hide()
                self.controls.set_exporter(None, pdf=True)
                return
            if owner is None or exporter_class not in _SAFE_PYQTGRAPH_FILE_EXPORTERS:
                self.currentExporter = None
                self.ui.paramTree.clear()
                self.ui.exportBtn.setEnabled(False)
                self.ui.copyBtn.setEnabled(False)
                self.ui.copyBtn.hide()
                self.controls.set_exporter(None)
                return
            target = owner.plot if exporter_class is CSVExporter else self.scene
            self.currentExporter = exporter_class(item=target)
            self.ui.paramTree.setParameters(self.currentExporter.parameters())
            exporter_type = type(self.currentExporter)
            if isinstance(self.currentExporter, CSVExporter):
                # Numeric exports always retain the original precision. The
                # upstream decimal-place control is unused by both our writers.
                params = self.currentExporter.parameters()
                params.setOpts(title="CSV options")
                params.child("separator").setOpts(title="Separator")
                params.child("columnMode").setOpts(title="Columns")
                params.removeChild(params.child("precision"))
                configure_csv_measurements(owner, self.currentExporter)
                self._configure_csv_data_options()
                params.child("measurements").sigValueChanged.connect(self._csv_measurement_changed)
                self._update_csv_scope()
            parameter_tips = {
                "width": "Export width in pixels. Changing this also adjusts the height to preserve the aspect ratio.",
                "height": "Export height in pixels. Changing this also adjusts the width to preserve the aspect ratio.",
                "antialias": "Smooth the edges of lines and text in the exported image.",
                "background": "Choose the export background colour, including its transparency.",
                "invertValue": "Invert image brightness while retaining colour hues.",
                "separator": "Choose the character separating values in the exported text file.",
                "columnMode": "Use separate X columns for each trace, or share one X column when coordinates match.",
                "values": "Choose which plot values to export. The description below explains the selected scope.",
                "measurements": "Choose all traces or an individual measurement, including hidden datasets.",
            }
            exporter: Any = self.currentExporter
            exporter.parameters().setOpts(title="Export options")
            titles = {"width": "Width (px)", "height": "Height (px)",
                      "antialias": "Smooth edges", "background": "Background colour",
                      "invertValue": "Invert brightness"}
            for parameter in exporter.parameters().children():
                if parameter.name() in titles:
                    parameter.setOpts(title=titles[parameter.name()])
                tip = parameter_tips.get(parameter.name())
                if tip and not parameter.opts.get("tip"):
                    parameter.setOpts(tip=tip)
            self.ui.exportBtn.setEnabled(
                exporter_type in _SAFE_PYQTGRAPH_FILE_EXPORTERS
                and (exporter_type is not CSVExporter or exporter.params["measurements"] != "empty")
            )
            self.ui.copyBtn.setEnabled(
                exporter_type in _SAFE_PYQTGRAPH_COPY_EXPORTERS
            )
            self.ui.copyBtn.setVisible(exporter_type in _SAFE_PYQTGRAPH_COPY_EXPORTERS)
            self.ui.copyBtn.setText("Copy SVG" if exporter_type is SVGExporter else "Copy image")
            self.ui.copyBtn.setToolTip(
                "Copy the complete plot using the dimensions, background and options selected in this dialog."
            )
            self.feedback.hide()
            self.controls.set_exporter(self.currentExporter)
            if exporter_type is CSVExporter:
                self._update_csv_scope()

        def _configure_csv_data_options(self) -> None:
            exporter = self.currentExporter
            owner = self._qplot_owner
            if exporter is None or owner is None:
                return
            params = exporter.params
            heatmap = csv_exports_heatmap(owner, exporter)
            modes = ([CSV_HEATMAP_VALUES] if heatmap else
                     [CSV_UNDERLYING_VALUES, CSV_TRANSFORMED_VALUES])
            values = params.names.get("values")
            if values is None:
                values = params.insertChild(1, {
                    "name": "values", "title": "Values", "type": "list",
                    "limits": modes, "value": modes[0],
                })
                values.sigValueChanged.connect(self._update_csv_scope)
            else:
                if values.value() != CSV_HEATMAP_VALUES:
                    exporter._qplot_csv_line_values = values.value()
                values.setLimits(modes)
                if not heatmap:
                    values.setValue(getattr(exporter, "_qplot_csv_line_values", modes[0]))
            columns = params.names.get("columnMode")
            if heatmap and columns is not None:
                exporter._qplot_csv_columns = columns.value()
                params.removeChild(columns)
            elif not heatmap and columns is None:
                params.addChild({
                    "name": "columnMode", "title": "Columns", "type": "list",
                    "limits": ["(x,y) per plot", "(x,y,y,y) for all plots"],
                    "value": getattr(exporter, "_qplot_csv_columns", "(x,y) per plot"),
                })

        def _csv_measurement_changed(self, *_args) -> None:
            self.controls.clear_bindings()
            self._configure_csv_data_options()
            self._update_csv_scope()
            self.feedback.hide()
            self.controls.set_exporter(self.currentExporter)

        def _update_csv_scope(self, *_args) -> None:
            scope = getattr(self, "csv_scope", None)
            owner = self._qplot_owner
            exporter = self.currentExporter
            if scope is None or owner is None or exporter is None or type(exporter) is not CSVExporter:
                return
            full_scope = owner._csv_export_scope(exporter)
            scope.setToolTip(full_scope)
            selection = selected_csv_measurements(owner, exporter)
            labels = []
            hidden = 0
            for entry in selection or ():
                visible = (entry.item.isVisible() if entry.kind == "trace"
                           else owner._heatmap_is_visible(entry.item))
                hidden += int(not visible)
                labels.append(entry.label if visible else f"{entry.label} [hidden]")
            summary = ", ".join(labels[:3])
            if len(labels) > 3:
                summary += f" and {len(labels) - 3} more"
                if hidden:
                    summary += f" ({hidden} hidden in selection)"
            elif len(labels) == 1:
                summary = "Includes the hidden measurement." if hidden else ""
            if csv_exports_heatmap(owner, exporter):
                layer = selection[0].item if selection else owner
                grid = np.asarray(layer.__dict__.get("data_grid", layer.__dict__.get("dataGrid", [])))
                shape = f" ({grid.shape[1]:,} × {grid.shape[0]:,} cells)" if grid.ndim == 2 else ""
                source = selection[0].source if selection else owner
                report = getattr(source, "_heatmap_resolution_report", None)
                processing = "The grid retains applied operations and any spatial averaging."
                if report is not None:
                    if report.spatial_mean or (report.binned and not report.aggregated):
                        processing = "Includes spatial averaging into display cells."
                    if report.sampled:
                        processing += " The source rows were sampled."
                text = f"Selected heatmap grid{shape}. {processing}"
            elif exporter.params["values"] == CSV_TRANSFORMED_VALUES:
                text = "All loaded samples, after operations and plot transforms. Zoom does not limit the export."
            else:
                text = "All loaded samples, after operations and before plot transforms. Zoom does not limit the export."
            scope.setText("\n".join(part for part in (
                summary, text, "For raw measurements, use CSV export in the database window.",
            ) if part))
            scope.show()

        @QtCore.pyqtSlot()
        def exportClicked(self) -> None:
            self.feedback.hide()
            self.selectBox.hide()
            owner = self._qplot_owner
            if owner is not None:
                selected = self.ui.formatList.currentItem()
                if selected is not None and selected.expClass is QtGui.QPdfWriter:
                    saved = owner.save_plot_pdf()
                else:
                    saved = owner._export_pyqtgraph_exporter(self.currentExporter)
                if saved:
                    self.feedback.setText("Export started." if type(self.currentExporter) is CSVExporter else "Saved.")
                    self.feedback.show()
                    self.fit_export_controls()

        @QtCore.pyqtSlot()
        def copyClicked(self) -> None:
            self.feedback.hide()
            self.selectBox.hide()
            owner = self._qplot_owner
            if owner is not None:
                if owner._copy_pyqtgraph_exporter(self.currentExporter):
                    self.feedback.setText("Copied to clipboard.")
                    self.feedback.show()
                    self.fit_export_controls()

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
                # The Qt wrapper may already be deleted; continue disposing the
                # remaining renderer resources.
                pass
            self.currentExporter = None
            self.deleteLater()
else:
    _SafePyqtgraphExportDialog = None


if TYPE_CHECKING:
    class _PlotExportBase(PlotHost, qtw.QMainWindow):
        _dataset_key: DatasetKey
        ds: Any
        param: Any
        plot: Any
        widget: Any

else:
    class _PlotExportBase(PlotHost):
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
            with _without_hover_highlight(self):
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
        if _window_is_retired(self):
            return
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
            # An already deleted dialog cannot be closed again; continue removing its
            # selection overlay.
            pass
        select_box = getattr(dialog, "selectBox", None)
        if select_box is not None:
            try:
                if select_box.scene() is scene:
                    scene.removeItem(select_box)
            except RuntimeError:
                # The Qt wrapper may already be deleted; continue disposing the
                # remaining export-dialog resources.
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


    def _csv_export_scope(self, exporter: CSVExporter) -> str:
        """Describe the exact values selected by the numeric export option."""
        selection = selected_csv_measurements(self, exporter)
        description = csv_selection_description(self, exporter)
        prefix = f"Exporting: {description}.\n" if description else ""
        if csv_exports_heatmap(self, exporter):
            layer = selection[0].item if selection else self
            grid = np.asarray(layer.__dict__.get("data_grid", layer.__dict__.get("dataGrid", [])))
            shape = f" ({grid.shape[1]:,} × {grid.shape[0]:,} cells)" if grid.ndim == 2 else ""
            return prefix + (
                f"Selected heatmap grid{shape}, including applied operations and "
                "any spatial averaging. Retains the currently loaded extent and resolution; "
                "other layers are not included. No fresh database read. "
                "Use the main-window CSV action for raw measurement values."
            )
        values = exporter.parameters().names.get("values")
        transformed = values is not None and values.value() == CSV_TRANSFORMED_VALUES
        if transformed:
            return prefix + (
                "Plot values including applied operations and native FFT, differentiation, "
                "phase-map or mean-subtraction controls. All loaded samples, in physical "
                "coordinates: no zoom clipping, rendering downsampling or logarithmic "
                "coordinate conversion. No fresh database read."
            )
        return prefix + (
            "Underlying plot samples, including applied Operations-panel processing, "
            "before native FFT, differentiation, phase-map or mean subtraction. "
            "All loaded samples; zoom and rendering downsampling do not limit the export. "
            "Use the main-window CSV action for raw measurement values."
        )


    def _capture_line_csv(self, exporter: CSVExporter, *, transformed: bool = False):
        """Detach numeric values and metadata while their Qt items are alive."""
        if not isinstance(exporter.item, PlotItem):
            raise TypeError("Must have a PlotItem selected for CSV export.")
        selection = selected_csv_measurements(self, exporter, validate=True)
        items = list(exporter.item.items)
        if selection is not None:
            if any(entry.kind != "trace" for entry in selection):
                raise ValueError("Select a trace measurement for this CSV export.")
            selected_items = [entry.item for entry in selection]
            if exporter.params["measurements"] == "all_traces" or len(exporter._qplot_csv_measurements) == 1:
                selected_items.extend(item for item in items if isinstance(item, ErrorBarItem))
            items = selected_items
        shared_x = exporter.params["columnMode"] == "(x,y,y,y) for all plots"
        if transformed:
            traces: list[TransformedTrace] = []
            for item in items:
                if isinstance(item, ErrorBarItem):
                    raise ValueError(
                        "Transformed export does not transform error bars. "
                        f"Choose {CSV_UNDERLYING_VALUES} to retain their original values."
                    )
                if not (hasattr(item, "implements") and item.implements("plotData")):
                    continue
                x, y = (item.getOriginalDataset() if hasattr(item, "getOriginalDataset")
                        else item.getData())
                if x is None:
                    continue
                options = dict(getattr(item, "opts", {}))
                parameter = getattr(self, "_trace_input_axis_parameter", None)
                axis_params = getattr(self, "axis_param", {})
                x_param = parameter(item, "x") if callable(parameter) else axis_params.get("x")
                y_param = parameter(item, "y") if callable(parameter) else axis_params.get("y")
                label_options = dict(options)
                if label_options.get("derivativeMode"):
                    # NativePlotDataItem's secant branch resets to input X.
                    label_options["fftMode"] = False
                quantities = native_axis_quantities(x_param, y_param, label_options)
                name = item.name() or f"trace{len(traces):04}"
                labels = [
                    f"{name}: {quantities[axis].label or axis}"
                    + (f" [{quantities[axis].unit}]" if quantities[axis].unit else "")
                    for axis in ("x", "y")
                ]
                headers = (labels[0], labels[1])
                traces.append(TransformedTrace(
                    frozen_array(x), frozen_array(y), headers,
                    fft=bool(options.get("fftMode")),
                    derivative=bool(options.get("derivativeMode")),
                    phase=bool(options.get("phasemapMode")),
                    subtract_mean=bool(options.get("subtractMeanMode")),
                ))
            return TransformedCsv(tuple(traces), shared_x)

        collector = CSVExporter(exporter.item)
        collector.params.restoreState(exporter.params.saveState())
        coordinates = []
        for item in items:
            if isinstance(item, ErrorBarItem):
                collector._exportErrorBarItem(item)
            elif hasattr(item, "implements") and item.implements("plotData"):
                dataset = (item.getOriginalDataset() if hasattr(item, "getOriginalDataset")
                           else item.getData())
                if dataset[0] is not None:
                    coordinates.append(frozen_array(dataset[0]))
                collector._exportPlotDataItem(item)
        columns = tuple(frozen_array(column) for dataset in collector.data for column in dataset)
        if shared_x and collector.data:
            # Error bars can supply the first coordinate column too.
            coordinates.insert(0, frozen_array(collector.data[0][0]))
        return CsvColumns(tuple(collector.header), columns, tuple(coordinates), shared_x)


    def _capture_heatmap_csv(self, layer: Any = None) -> HeatmapCsv:
        """Freeze a currently plotted grid without another source read."""
        target: Any = self if layer is None else layer
        axis_data = target.__dict__.get("axis_data")
        if not isinstance(axis_data, dict):
            raise ValueError("Heatmap coordinate data is not available.")
        x = np.asarray(axis_data.get("x", []), dtype=float)
        y = np.asarray(axis_data.get("y", []), dtype=float)
        grid = np.asarray(target.__dict__.get("data_grid", target.__dict__.get("dataGrid", [])))
        if x.ndim != 1 or y.ndim != 1:
            raise ValueError("Heatmap coordinate axes must be one-dimensional.")
        if grid.ndim != 2 or grid.shape != (y.size, x.size):
            raise ValueError(
                f"Heatmap data shape {grid.shape} does not match "
                f"axis shape {(y.size, x.size)}."
            )
        if grid.size == 0:
            raise ValueError("The currently plotted heatmap is empty.")
        names = self._heatmap_csv_column_names()
        if target is not self:
            source = target.from_win
            order = target.source_axis_order()
            if order is None:
                raise ValueError("The selected heatmap has no current coordinate mapping.")
            parameters = source.__dict__.get("axis_param", {})
            value = source.__dict__.get("display_param", source.__dict__.get("param"))
            names = (self._export_parameter_name(parameters.get(order[0]), "x"),
                     self._export_parameter_name(parameters.get(order[1]), "y"),
                     self._export_parameter_name(value, "value"))
        return HeatmapCsv(names, frozen_array(x), frozen_array(y),
                          frozen_array(grid))


    def _export_pyqtgraph_exporter(self, exporter: Any) -> bool:
        """Route an allowlisted PyQtGraph file exporter through staging."""
        if _window_is_retired(self):
            return False
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
        selection = selected_csv_measurements(self, exporter) if type(exporter) is CSVExporter else None
        filename_owner = selection[0].source if selection and len(selection) == 1 else self
        filename_provider = getattr(filename_owner, "_default_plot_pdf_filename", self._default_plot_pdf_filename)
        suggested_path = path.splitext(filename_provider())[0]
        suggested_path = f"{suggested_path}{default_suffix}"
        if type(exporter) is CSVExporter:
            dataset = getattr(filename_owner, "ds", None)
            filename = csv_filename(
                getattr(self, "config", None), path.basename(suggested_path),
                guid=getattr(dataset, "guid", None),
                captured_run_id=getattr(dataset, "captured_run_id", None),
            )
            suggested_path = path.join(path.dirname(suggested_path), path.splitext(filename)[0] + default_suffix)
        try:
            if type(exporter) is CSVExporter:
                selected_csv_measurements(self, exporter, validate=True)
            destination = choose_export_path_with_suffixes(
                self,
                caption="Export Plot",
                suggested_path=suggested_path,
                name_filter=name_filter,
                allowed_suffixes=suffixes,
                default_suffix=default_suffix,
                replace_title="Replace Plot Export?",
                file_description=description,
                use_chosen_format=type(exporter) is CSVExporter,
            )
            if destination is None:
                self.show_status("Plot export cancelled.", 3000)
                return False
            writer = lambda staging_path: exporter.export(fileName=staging_path)
            if type(exporter) is SVGExporter:
                writer = lambda staging_path: write_svg_stage(staging_path, exporter)
            elif type(exporter) is CSVExporter:
                from ._export_tasks import start_export_task

                if csv_exports_heatmap(self, exporter):
                    selection = selected_csv_measurements(self, exporter, validate=True)
                    layer = selection[0].item if selection else self
                    snapshot = (self._capture_heatmap_csv() if layer is self
                                else self._capture_heatmap_csv(layer=layer))
                else:
                    mode = exporter.parameters().names.get("values")
                    snapshot = self._capture_line_csv(
                        exporter, transformed=(mode is not None and mode.value() == CSV_TRANSFORMED_VALUES),
                    )
                separator = "\t" if default_suffix == ".tsv" else ","
                start_export_task(
                    self, title="Export plot CSV", destination=destination,
                    writer=lambda filename, cancelled, progress: snapshot.write(
                        filename, cancelled, progress, separator=separator,
                    ),
                    on_success=lambda _result: self.show_status(
                        f"Exported plot: {destination.filename}", 5000,
                    ),
                )
                return True
            with _without_hover_highlight(self):
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


    def _write_line_csv_stage(
            self,
            staging_path: str,
            exporter: CSVExporter,
            *,
            separator: str | None = None,
            ) -> bool:
        """Write underlying samples using the same frozen writer as the UI."""
        snapshot = self._capture_line_csv(exporter)
        chosen_separator = exporter.params["separator"] if separator is None else separator
        snapshot.write(staging_path, separator="\t" if chosen_separator == "tab" else ",")
        return True


    def _write_heatmap_csv_stage(
            self,
            staging_path: str,
            exporter: CSVExporter,
            *,
            separator: str | None = None,
            ) -> bool:
        """Write the current primary grid, at its loaded extent and resolution."""
        snapshot = self._capture_heatmap_csv()
        chosen_separator = exporter.params["separator"] if separator is None else separator
        snapshot.write(staging_path, separator="\t" if chosen_separator == "tab" else ",")
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
        if _window_is_retired(self):
            return False
        if type(exporter) not in _SAFE_PYQTGRAPH_COPY_EXPORTERS:
            self.show_status("Copy is disabled for that export format.", 5000)
            return False
        try:
            with _without_hover_highlight(self):
                if type(exporter) is SVGExporter:
                    clipboard = qtw.QApplication.clipboard()
                    if clipboard is None:
                        raise RuntimeError("No clipboard available.")
                    mime_data = QtCore.QMimeData()
                    mime_data.setData("image/svg+xml", QtCore.QByteArray(svg_bytes(exporter)))
                    clipboard.setMimeData(mime_data)
                    result = None
                else:
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
            return (
                (".png",),
                ".png",
                "PNG image (*.png)",
                "plot image",
            )
        if type(exporter) is CSVExporter:
            suffix = ".tsv" if exporter.params["separator"] == "tab" else ".csv"
            return (
                (".csv", ".tsv"),
                suffix,
                f"{suffix[1:].upper()} files (*{suffix})",
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
        if _window_is_retired(self):
            return False
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
        return suggested_export_filename(filename)


    @QtCore.pyqtSlot()
    def copy_plot_image(self) -> bool:
        """
        Copies the rendered plot widget to the clipboard as an image.

        Only the pyqtgraph widget is captured, so window menus, toolbars, docks,
        and any open context menu are excluded.

        """
        if _window_is_retired(self):
            return False
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
        with _without_hover_highlight(self):
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
            # The SVG binary MIME payload remains valid even when no UTF-8 plain-text
            # fallback can be supplied.
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

        # SVG Tiny omits ViewBox clipping; SVG 1.1 preserves zoomed traces
        # and heatmaps within their axes, as in the file export renderer.
        generator = QtSvg.QSvgGenerator(QtSvg.QSvgGenerator.SvgVersion.Svg11)
        generator.setOutputDevice(buffer)
        generator.setSize(size)
        generator.setViewBox(QtCore.QRect(0, 0, size.width(), size.height()))
        generator.setTitle("qPlot plot")
        generator.setDescription("Copied from qPlot")

        painter = QtGui.QPainter(generator)
        try:
            with _without_hover_highlight(self):
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
        with _without_hover_highlight(self):
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
