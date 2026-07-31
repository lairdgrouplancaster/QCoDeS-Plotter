from os import path
from typing import TYPE_CHECKING, Any, Literal

from PyQt6 import QtCore, QtGui, QtSvg
from PyQt6 import QtWidgets as qtw
from pyqtgraph.exporters import ImageExporter

from qplot.diagnostics import log_exception

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
    Plot-window export, PDF, and clipboard-image helpers.

    """

    @QtCore.pyqtSlot()
    def open_export_dialog(self) -> None:
        """
        Opens pyqtgraph's export dialog for this plot.

        """
        scene = self.widget.scene()
        scene.contextMenuItem = self.plot
        scene.showExportDialog()


    @QtCore.pyqtSlot()
    def save_plot_pdf(self) -> bool:
        """
        Prompts for a filename and saves the visible plot area as a PDF.

        """
        filename = qtw.QFileDialog.getSaveFileName(
            self,
            "Save Plot as PDF",
            self._default_plot_pdf_filename(),
            "PDF files (*.pdf)",
        )[0]
        if not filename:
            self.show_status("PDF export cancelled.", 3000)
            return False

        return self.save_plot_pdf_to_file(filename)


    def save_plot_pdf_to_file(self, filename: str) -> bool:
        """
        Saves the visible plot area as a PDF at ``filename``.

        """
        if not filename:
            self.show_status("PDF export cancelled.", 3000)
            return False

        if not filename.lower().endswith(".pdf"):
            filename = f"{filename}.pdf"

        try:
            saved = self._write_plot_pdf(filename)
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

        self.show_status(f"Saved PDF: {filename}", 5000)
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

        try:
            value = config.get(COPY_PLOT_IMAGE_RESOLUTION_KEY)
        except Exception:
            return COPY_PLOT_IMAGE_RESOLUTION_SCREEN

        if value in (
            COPY_PLOT_IMAGE_RESOLUTION_300_DPI,
            COPY_PLOT_IMAGE_RESOLUTION_SVG,
            ):
            return value
        return COPY_PLOT_IMAGE_RESOLUTION_SCREEN


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
